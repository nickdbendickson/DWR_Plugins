"""Boann, sub-grid local inertial: the local-inertial ("acceleration") scheme of engine.py run on a
COARSE grid whose cells each keep their r x r fine (LiDAR) sub-cells as stage tables (subgrid.py),
after Nithila Devi and Kuiry (2024, Water Resources Research 60, e2023WR035334). A coarse cell
stores and conveys the channel a flat coarse cell would lose, the step is r times longer and there
are r*r fewer cells, and the result is still a fine-resolution inundation map.

subli_ref.py states the scheme and the two places it departs from the paper on purpose (exact stage
curves instead of fitted polynomials; the face's compound Manning conveyance instead of one mean
face depth). kernels_subli.metal is its GPU / CPU kernel.

Integration model (it consumes an ordinary Runner deck unchanged, exactly as engine_subfv.py):
  - the deck DEM is treated as the FINE (LiDAR) grid;
  - 'subgrid <r>' in the .par sets the coarsening factor (1-8, default 4); the solver runs on
    cells of dx * r;
  - every output raster is downscaled back to the deck's own resolution (the paper's Eq. 12: a
    fine cell's depth is the coarse cell's surface less its own ground), so Hazard / Screening
    read them exactly as from any other engine.

Deck keywords it reads beyond the plain engine's:
  subgrid <r>     coarsening factor
  upwind          the paper's UPWIND flux weighting with its adaptive theta (its Eq. 7 and 8), which
                  is OFF by default: the step is then the plain engine's default (Bates et al. 2010)
  noupwind        the weighting off, whatever DEFAULT_WEIGHTING says
  cfl, 1Dfriction, physicalinflow, initial_tstep, inittm_depth: as the plain engine.
'theta' is read and ignored: the plain engine's fixed-theta centred weighting is not offered here
(the Runner's rerun of a run that lost its balance therefore changes only cfl for this engine).

Why the weighting is off by default (tests/test_subli_kernel.py pins the numbers): it blends a
face's discharge with the face upwind ALONG THE GRID AXIS. Where the flow crosses the grid at an
angle - any meandering reach - that neighbour sits partly outside the channel, and the blend takes
momentum out of the flow. On the 5 m meandering test valley run on 20 m cells the maximum depths
are within 2 cm of the fine run without it and about 0.2 m too deep with it (as far off as plain
20 m cells), at every coarsening factor; on a straight reach it changes nothing. It is there for a
low-friction run that oscillates.

Outputs per fine cell: maximum depth and water-surface elevation from the coarse cell's highest
surface; arrival time and time of maximum of its coarse cell, where the fine cell got wet; maximum
velocity of its coarse cell (face discharge over the WET face area), where the fine cell got wet.

float32 on the device; volumes summed in float64 on the host.

Run it alone:  python -m metalflood.engine_subli run.par [-backend metal|cpu] [-v]"""
import math
import os
import time

import numpy as np

from . import deckio as bl
from . import backends as be
from . import subli_backend as lb
from . import subgrid as sg
from . import engine as mfe

NAME = "Boann, sub-grid local inertial (coarse ACC with LiDAR sub-cells, on the GPU via Metal)"
VERSION = "0.1.0"
G = bl.G
NODATA = bl.NODATA
DRY_M = bl.DRY_M
MAX_HFLOW = bl.MAX_HFLOW
BATCH_STEPS = mfe.BATCH_STEPS
WINDOW_MARGIN = mfe.WINDOW_MARGIN
DEFAULT_R = 4
MAX_R = 8
DEFAULT_WEIGHTING = "none"

EngineError = bl.EngineError
Cancelled = bl.Cancelled

S_T, S_DT, S_HMAX, S_STEPS, S_MINDT = mfe.S_T, mfe.S_DT, mfe.S_HMAX, mfe.S_STEPS, mfe.S_MINDT
S_VIN, S_VOUT, S_CLIP, S_QIN, S_QOUT, S_LASTDT = (
    mfe.S_VIN, mfe.S_VOUT, mfe.S_CLIP, mfe.S_QIN, mfe.S_QOUT, mfe.S_LASTDT)
S_RMIN, S_RMAX, S_CMIN, S_CMAX = mfe.S_RMIN, mfe.S_RMAX, mfe.S_CMIN, mfe.S_CMAX
MF_FIX = mfe.MF_FIX
_f, _setf = mfe._f, mfe._setf


class Deck(mfe.Deck):
    """An ordinary Runner deck, plus the sub-grid coarsening factor r (deck keyword 'subgrid') and
    the flux weighting ('upwind' / 'noupwind')."""

    def __init__(self, par_path):
        super().__init__(par_path)
        try:
            r = int(self.par.get("subgrid", DEFAULT_R))
        except (TypeError, ValueError):
            raise EngineError("the .par file's 'subgrid' is not a whole number ({!r})".format(
                self.par.get("subgrid")))
        if r < 1 or r > MAX_R:
            raise EngineError("subgrid r must be between 1 and {} (got {})".format(MAX_R, r))
        self.subgrid_r = r
        if "noupwind" in self.par:
            self.weighting = "none"
        elif "upwind" in self.par:
            self.weighting = "upwind"
        else:
            self.weighting = DEFAULT_WEIGHTING


def pack_tables(a, t, okc):
    """Write a subgrid.Tables into the kernel's buffers (s, zb, ok and the four face buffers) and
    return the run's datum z0. Every elevation goes in RELATIVE, so float32 keeps depths to ~1e-5 m
    whatever the terrain's elevation: a cell's sorted sub-elevations as heights above its own lowest
    sub-cell, that lowest sub-cell and the face profiles as heights above z0, the lowest sub-cell
    of any active cell."""
    okc = np.asarray(okc, bool)
    z0 = float(t.zbed[okc].min()) if okc.any() else 0.0
    a["s"][:] = (t.s - t.zbed[..., None]).reshape(-1)
    a["zb"][:] = (t.zbed - z0).reshape(-1)
    a["ok"][:] = okc.reshape(-1)
    zfx, nf2x, zfy, nf2y = sg.all_faces(t)
    a["zfx"][:] = (zfx - z0).reshape(-1)
    a["nf2x"][:] = nf2x.reshape(-1)
    a["zfy"][:] = (zfy - z0).reshape(-1)
    a["nf2y"][:] = nf2y.reshape(-1)
    return z0


def set_volume(a, t, okc, V):
    """Put the cell volumes V (m3) in the kernel's state: V, the depth above each cell's lowest
    sub-cell, and the first step's deepest water."""
    V = np.where(okc, np.maximum(np.asarray(V, float), 0.0), 0.0)
    eta = sg.invert_eta(V, t.s, t.csum, t.asub)
    d = np.where(okc, np.maximum(eta - t.zbed, 0.0), 0.0)
    a["V"][:] = V.reshape(-1)
    a["d"][:] = d.reshape(-1)
    a["st"][S_HMAX] = np.float32(a["d"].max() if d.size else 0.0).view(np.uint32)


class Engine:
    def __init__(self, deck, backend):
        self.d = d = deck
        self.b = backend
        r = self.r = d.subgrid_r
        nyf, nxf = d.ny, d.nx
        self.nyf, self.nxf = nyf, nxf
        # pad the fine grid up to a whole multiple of r (pad = dry, invalid, high ground)
        nyp = ((nyf + r - 1) // r) * r
        nxp = ((nxf + r - 1) // r) * r
        hi = float(d.z[d.valid].max()) + 1000.0 if d.valid.any() else 1000.0
        dem = np.full((nyp, nxp), hi)
        dem[:nyf, :nxf] = np.where(d.valid, d.z, hi)
        nn = np.full((nyp, nxp), 0.03)
        nn[:nyf, :nxf] = np.where(d.valid & (d.n > 0), d.n, 0.03)
        vpad = np.zeros((nyp, nxp), bool)
        vpad[:nyf, :nxf] = d.valid
        h0p = np.zeros((nyp, nxp))
        h0p[:nyf, :nxf] = np.where(d.valid, d.h0, 0.0)
        self.dem_fine = dem
        dxf = d.dx
        dxc = self.dxc = dxf * r
        # fine_shape: the open-edge profiles come from the deck's true last row / column, not the pad
        t = self.t = sg.build_from_fine(dem, nn, dxc, r, fine_shape=(nyf, nxf))
        nyc, nxc = self.nyc, self.nxc = t.ny, t.nx
        # a coarse cell is active if any of its fine sub-cells is valid
        okc = self.okc = vpad.reshape(nyc, r, nxc, r).any(axis=(1, 3))
        n_values = sum(len(s[1]) for s in d.series)
        layout = lb.subli_buffer_layout(nyc, nxc, len(d.src_cells), len(d.series), n_values, r)
        a = self.a = backend.allocate(layout)
        self.z0 = pack_tables(a, t, okc)
        a["scl"][:] = 1.0
        a["hmax"][:] = 0.0
        a["tmax"][:] = NODATA
        a["arr"][:] = NODATA
        # the fine start water placed in each coarse cell, levelled inside it
        set_volume(a, t, okc, (dxf * dxf) * h0p.reshape(nyc, r, nxc, r).sum(axis=(1, 3)))
        off = 0
        for k, (_, ts, qs) in enumerate(d.series):
            a["ser_off"][k] = off
            a["ser_t"][off:off + len(ts)] = ts
            a["ser_q"][off:off + len(ts)] = qs
            off += len(ts)
        a["ser_off"][len(d.series)] = off
        # each fine source cell feeds its coarse cell (several in one cell just add up)
        for i, (rr, cc, k) in enumerate(d.src_cells):
            a["s_cell"][i] = (rr // r) * nxc + (cc // r)
            a["s_series"][i] = k
        self.src_rc = (np.array([s[0] // r for s in d.src_cells], int),
                       np.array([s[1] // r for s in d.src_cells], int))
        p = self.p = lb.SubLIParams()
        p.nx, p.ny = nxc, nyc
        p.free_mask = sum(mfe._FREE_BITS[e] for e in d.free)
        p.n_src, p.n_series = len(d.src_cells), len(d.series)
        p.r, p.m = r, r * r
        p.weighting = lb.WEIGHTING[d.weighting]
        p.friction = lb.F_COMPOUND
        p.fric2d, p.inflow_lf_order = int(d.fric2d), int(d.inflow_lf_order)
        p.dx, p.dt0, p.alpha, p.g = dxc, d.dt0, d.cfl, G
        p.dry, p.maxhflow, p.arr_depth = DRY_M, MAX_HFLOW, d.arr_depth
        p.wsub = dxf
        self.win = self._window_from(self.depth2() > 0)

    def volume2(self):
        return self.a["V"].reshape(self.nyc, self.nxc)

    def depth2(self):
        """Depth above each coarse cell's lowest sub-cell, as the kernel last left it."""
        return self.a["d"].reshape(self.nyc, self.nxc)

    def eta2(self):
        """Water-surface elevation of each coarse cell (the deck's datum), from its volume."""
        e = sg.invert_eta(self.volume2().astype(np.float64), self.t.s, self.t.csum, self.t.asub)
        return np.where(self.okc, e, self.t.zbed)

    def volume(self):
        return float(self.a["V"].sum(dtype=np.float64))

    def _bbox(self, rmin, rmax, cmin, cmax):
        if len(self.src_rc[0]):
            rmin, rmax = min(rmin, self.src_rc[0].min()), max(rmax, self.src_rc[0].max())
            cmin, cmax = min(cmin, self.src_rc[1].min()), max(cmax, self.src_rc[1].max())
        if rmin > rmax:
            return (0, 1, 0, 1)
        m = WINDOW_MARGIN
        return (max(0, rmin - m), min(self.nyc, rmax + m + 1),
                max(0, cmin - m), min(self.nxc, cmax + m + 1))

    def _window_from(self, wet):
        rows = np.flatnonzero(wet.any(axis=1))
        cols = np.flatnonzero(wet.any(axis=0))
        if not len(rows):
            return self._bbox(1, 0, 1, 0)
        return self._bbox(rows[0], rows[-1], cols[0], cols[-1])

    def batch(self, t, t_event, nsteps, clip, full=False):
        """Up to nsteps steps from model time t until t_event, on the window of coarse cells that
        holds every wet cell with room for the batch's front (full=True: the whole grid). Returns
        the batch's state words as a dict."""
        st = self.a["st"]
        r0, r1, c0, c1 = (0, self.nyc, 0, self.nxc) if full else self.win
        p = self.p
        p.r0, p.c0, p.wr, p.wc = r0, c0, r1 - r0, c1 - c0
        p.t0, p.t_end = t, t_event - t
        p.clip_end = int(bool(clip))
        hm = st[S_HMAX]
        st[:] = 0
        st[S_HMAX] = hm
        _setf(st, S_MINDT, 1e30)
        st[S_RMIN] = st[S_CMIN] = 0xFFFFFFFF
        self.b.run_batch(p, nsteps)
        out = {"t_local": _f(st, S_T), "dt": _f(st, S_LASTDT), "steps": int(st[S_STEPS]),
               "min_dt": _f(st, S_MINDT), "vin": _f(st, S_VIN), "vout": _f(st, S_VOUT),
               "clip": int(st[S_CLIP]) * MF_FIX, "qin": _f(st, S_QIN),
               "qout": int(st[S_QOUT]) * MF_FIX}
        rmin, rmax, cmin, cmax = (int(st[k]) for k in (S_RMIN, S_RMAX, S_CMIN, S_CMAX))
        if out["steps"]:
            self.win = self._bbox(rmin, rmax, cmin, cmax) if rmin <= rmax \
                else self._bbox(1, 0, 1, 0)
        return out


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto", full_grid=False):
    """Run a .par deck with the sub-grid local-inertial scheme. backend: 'auto', 'metal' or 'cpu'.
    full_grid=True steps every coarse cell every step instead of the wet window (for tests: the
    result must not change)."""
    def say(msg):
        if log:
            log(msg)

    deck = Deck(par_path)
    mfe.refuse_rain(deck, "sub-grid local-inertial")
    try:
        bk = be.make_backend(backend, spec=lb.SUBLI_SPEC, log=say)
    except be.BackendError as e:
        raise EngineError(str(e))
    try:
        return _run(deck, bk, progress, cancel, say, full_grid)
    finally:
        bk.close()


def _run(d, bk, progress, cancel, say, full_grid=False):
    eng = Engine(d, bk)
    a = eng.a
    r = eng.r
    nyf, nxf = eng.nyf, eng.nxf
    nyc, nxc = eng.nyc, eng.nxc
    dx = d.dx
    xll, yll = d.xll, d.yll
    root = d.root
    sim, massint, saveint = d.sim, d.massint, d.saveint

    def fine_depth_now():
        """Current water depth downscaled to the fine grid, cropped to the deck extent."""
        return sg.fine_depth(eng.eta2(), eng.dem_fine, r)[:nyf, :nxf]

    mass_f = open(root + ".mass", "w")
    mass_f.write("Time         Tstep      MinTstep   NumTsteps    Area         Vol         "
                 "Qin         Hds        Qout          Qerror         Verror       "
                 "Rain-Inf+Evap\n")

    t = 0.0
    steps, min_dt, last_dt = 0, 1e9, d.dt0
    mean_dt = d.dt0
    next_mass = massint
    snap_k = 0
    next_snap = 0.0 if saveint <= sim else None
    vol0 = eng.volume()
    vin = vout = clip = 0.0
    qin = qout = 0.0
    batches = 0
    t_start = time.time()
    last_prog = t_start

    def write_mass(tt):
        vol = eng.volume()
        verr = vol - (vol0 + vin - vout)
        # the wet area at the fine resolution (what a fine run would report)
        area = float((fine_depth_now() > DRY_M).sum()) * dx * dx
        mass_f.write("{:<12.3f} {:<10.4f} {:<10.4f} {:<12d} {:<12.1f} {:<12.3f} {:<12.4f} "
                     "{:<10.4f} {:<13.4f} {:<14.6f} {:<12.4f} {:<12.4f}\n".format(
                         tt, last_dt, min_dt if steps else last_dt, steps, area, vol, qin,
                         0.0, qout, 0.0, verr - write_mass.last_err, 0.0))
        write_mass.last_err = verr
        write_mass.last_t = tt
        mass_f.flush()
    write_mass.last_err = 0.0
    write_mass.last_t = 0.0

    try:
        while t < sim - 1e-6:
            if cancel and cancel():
                raise Cancelled("cancelled")
            if next_snap is not None and t >= next_snap - 1e-6:
                bl._write_asc("{}-{:04d}.wd".format(root, snap_k), fine_depth_now(), xll, yll, dx)
                snap_k += 1
                next_snap = snap_k * saveint if snap_k * saveint <= sim + 1e-9 else None
            t_event = min(sim, next_mass, next_snap if next_snap is not None else sim)
            # never cut a step to land on a time (as the plain engine and LISFLOOD-FP): the Runner
            # reads a short step in the .mass file as a collapsing solver
            want = int(math.ceil((t_event - t) / max(mean_dt, 1e-3))) + 1
            n = max(1, min(BATCH_STEPS, want))
            out = eng.batch(t, t_event, n, False, full=full_grid)
            batches += 1
            if out["steps"]:
                vin += out["vin"]
                vout += out["vout"]
                clip += out["clip"]
                steps += out["steps"]
                min_dt = min(min_dt, out["min_dt"])
                last_dt = out["dt"]
                qin, qout = out["qin"], out["qout"]
                mean_dt = out["t_local"] / out["steps"]
            if not out["steps"] and out["t_local"] < (t_event - t) - 2e-3:
                raise EngineError("Boann (sub-grid local inertial) made no progress at "
                                  "t = {:.1f} s".format(t))
            t += out["t_local"]
            if (t_event - t) <= 2e-3:          # float32 rounding just short of the event
                t = max(t, t_event)
            if t >= next_mass - 1e-6:
                write_mass(t)
                next_mass += massint
            now = time.time()
            if progress and now - last_prog > 2.0:
                progress(t, sim)
                last_prog = now
        if write_mass.last_t < t - 1e-6:
            write_mass(t)
        if next_snap is not None and t >= next_snap - 1e-9:
            bl._write_asc("{}-{:04d}.wd".format(root, snap_k), fine_depth_now(), xll, yll, dx)
    finally:
        mass_f.close()

    # downscale the time-maxima to the deck's own (fine) resolution
    valid = d.valid
    shape = (nyc, nxc)
    hmax_c = a["hmax"].reshape(shape).astype(np.float64)
    fd_max = sg.fine_depth(eng.t.zbed + hmax_c, eng.dem_fine, r)[:nyf, :nxf]
    # as the plain engine (and LISFLOOD-FP): maxima only for water over 1 mm
    fd_max = np.where(valid & (fd_max > DRY_M), fd_max, 0.0)

    def up(x):
        return np.repeat(np.repeat(x.reshape(shape).astype(np.float64), r, axis=0),
                         r, axis=1)[:nyf, :nxf]
    wetf = valid & (fd_max > 0.0)
    bl._write_asc(root + ".max", np.where(valid, fd_max, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxVc", np.where(valid, np.where(wetf, up(a["vmax"]), 0.0), NODATA),
                  xll, yll, dx)
    bl._write_asc(root + ".inittm", np.where(wetf, up(a["arr"]), NODATA), xll, yll, dx,
                  fmt="%.4f")
    bl._write_asc(root + ".maxtm", np.where(wetf, up(a["tmax"]), NODATA), xll, yll, dx,
                  fmt="%.4f")
    bl._write_asc(root + ".mxe", np.where(wetf, d.z + fd_max, NODATA), xll, yll, dx)

    vol = eng.volume()
    wall = time.time() - t_start
    stats = {"engine": NAME, "version": VERSION, "backend": bk.kind, "device": bk.device,
             "scheme": "subli", "weighting": d.weighting, "subgrid_r": r,
             "coarse_cells": nyc * nxc, "fine_cells": nyf * nxf, "cells": nyf * nxf,
             "steps": steps, "batches": batches, "sim_time_s": t, "wall_s": round(wall, 2),
             "injected_m3": vin, "outflow_m3": vout, "final_volume_m3": vol,
             "mass_error_m3": vol - (vol0 + vin - vout), "clipped_m3": clip}
    # where the time went (backends.Profile); timing only, no result depends on it
    stats["profile"] = bk.profile() if hasattr(bk, "profile") else None
    say("Boann, sub-grid local inertial ({}, {}): r={}, {} weighting, {} coarse cells ({}x fewer), "
        "{} steps, {:.2f} h in {:.1f} min; volume error {:.3g} m3 of {:.4g} m3 injected.".format(
            bk.kind, bk.device, r, d.weighting, nyc * nxc, r * r, steps, t / 3600.0, wall / 60.0,
            stats["mass_error_m3"], vin))
    return stats


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="metalflood.engine_subli")
    ap.add_argument("par")
    ap.add_argument("-backend", default="auto", choices=["auto", "metal", "cpu"])
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args(argv)
    stats = run_par(a.par, backend=a.backend, log=(print if a.v else None))
    for k, v in stats.items():
        print("  {:16s} {}".format(k, v))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
