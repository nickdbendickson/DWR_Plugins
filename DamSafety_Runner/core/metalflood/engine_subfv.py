"""Boann, sub-grid finite volume: the HLLC shock-capturing scheme (engine_fv.py) run on a COARSE
grid whose cells each keep their r x r fine (LiDAR) sub-cells as stage tables (subgrid.py). A coarse
cell conveys and stores the channel a flat coarse cell would lose, so the run is r*r cheaper than a
fully-resolved run yet still produces a fine-resolution inundation map.

Integration model (so it consumes an ordinary Runner deck unchanged):
  - the deck DEM is treated as the FINE (LiDAR) grid;
  - a 'subgrid <r>' keyword in the .par sets the coarsening factor; the solver runs on a grid of
    dx * r cells built from the fine DEM as sub-grid tables (kernels_subfv.metal / subfv_hllc_ref.py);
  - every output raster is downscaled back to the deck's native (fine) resolution with
    subgrid.fine_depth, so Hazard / Screening read them exactly as from any other engine.

The per-cell state is water VOLUME V and the two momenta; the shared deck reader, the .mass writer,
the batching and the max-raster outputs are the engine_fv.py / engine_sgc.py machinery. float32 on
the GPU; volumes summed in float64 on the host.

Run it alone:  python -m metalflood.engine_subfv run.par [-backend metal|cpu] [-v]"""
import math
import os
import time

import numpy as np

from . import deckio as bl
from . import backends as be
from . import subfv_backend as sb
from . import subgrid as sg
from . import engine as mfe
from . import engine_fv as mff

NAME = "Boann, sub-grid finite volume (coarse HLLC with LiDAR sub-cells, on the GPU via Metal)"
VERSION = "0.2.0"
G = bl.G
NODATA = bl.NODATA
DRY_M = bl.DRY_M
BATCH_STEPS = mff.BATCH_STEPS
DEFAULT_R = 4
MAX_R = 8                                   # kernel per-cell scratch cap (MF_MSUB = r*r <= 64)

EngineError = bl.EngineError
Cancelled = bl.Cancelled

S_T, S_DT, S_WMAX = mff.S_T, mff.S_DT, mff.S_WMAX
S_MINDT, S_STEPS = mff.S_MINDT, mff.S_STEPS
S_VIN, S_VOUT, S_CLIP, S_QIN, S_QOUT, S_LASTDT = (
    mff.S_VIN, mff.S_VOUT, mff.S_CLIP, mff.S_QIN, mff.S_QOUT, mff.S_LASTDT)
MF_FIX = mff.MF_FIX
_f, _setf = mfe._f, mfe._setf


class Deck(mfe.Deck):
    """An ordinary Runner deck, plus the sub-grid coarsening factor r (deck keyword 'subgrid')."""

    def __init__(self, par_path):
        super().__init__(par_path)
        r = int(self.par.get("subgrid", DEFAULT_R))
        if r < 1 or r > MAX_R:
            raise EngineError("subgrid r must be between 1 and {} (got {})".format(MAX_R, r))
        self.subgrid_r = r


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
        self.nyp, self.nxp = nyp, nxp
        hi = float(d.z[d.valid].max()) + 1000.0 if d.valid.any() else 1000.0
        dem = np.full((nyp, nxp), hi)
        dem[:nyf, :nxf] = np.where(d.valid, d.z, hi)
        nn = np.full((nyp, nxp), 0.03)
        nn[:nyf, :nxf] = d.n
        vpad = np.zeros((nyp, nxp), bool)
        vpad[:nyf, :nxf] = d.valid
        h0p = np.zeros((nyp, nxp))
        h0p[:nyf, :nxf] = np.where(d.valid, d.h0, 0.0)
        self.dem_fine = dem
        dxf = d.dx
        dxc = dxf * r
        self.dxc = dxc
        # fine_shape: the open-edge profiles come from the deck's true last row / column, not the pad
        t = self.t = sg.build_from_fine(dem, nn, dxc, r, fine_shape=(nyf, nxf))
        nyc, nxc = t.ny, t.nx
        self.nyc, self.nxc = nyc, nxc
        # a coarse cell is active if any of its fine sub-cells is valid
        okc = vpad.reshape(nyc, r, nxc, r).any(axis=(1, 3))
        self.okc = okc
        n_values = sum(len(s[1]) for s in d.series)
        layout = sb.subfv_buffer_layout(nyc, nxc, len(d.src_cells), len(d.series), n_values, r)
        a = self.a = backend.allocate(layout)
        a["s"][:] = t.s.reshape(-1).astype(np.float32)
        a["nc2"][:] = (t.ncell ** 2).reshape(-1).astype(np.float32)
        a["ok"][:] = okc.reshape(-1).astype(np.uint8)
        zfx, nf2x, zfy, nf2y = sg.all_faces(t)
        a["zfx"][:] = zfx.reshape(-1).astype(np.float32)
        a["nf2x"][:] = nf2x.reshape(-1).astype(np.float32)
        a["zfy"][:] = zfy.reshape(-1).astype(np.float32)
        a["nf2y"][:] = nf2y.reshape(-1).astype(np.float32)
        a["eta"][:] = t.zbed.reshape(-1).astype(np.float32)
        a["hmax"][:] = 0.0
        a["tmax"][:] = NODATA
        a["arr"][:] = NODATA
        # initial volume: the fine start water placed in each coarse cell (asub = dxf^2)
        v0 = (dxf * dxf) * h0p.reshape(nyc, r, nxc, r).sum(axis=(1, 3))
        a["V"][:] = np.where(okc, v0, 0.0).reshape(-1).astype(np.float32)
        off = 0
        for k, (_, ts, qs) in enumerate(d.series):
            a["ser_off"][k] = off
            a["ser_t"][off:off + len(ts)] = ts
            a["ser_q"][off:off + len(ts)] = qs
            off += len(ts)
        a["ser_off"][len(d.series)] = off
        # map each fine source cell to its coarse cell (several in one cell just accumulate)
        for i, (rr, cc, k) in enumerate(d.src_cells):
            a["s_cell"][i] = (rr // r) * nxc + (cc // r)
            a["s_series"][i] = k
        self.src_rc = (np.array([s[0] // r for s in d.src_cells], int),
                       np.array([s[1] // r for s in d.src_cells], int))
        p = self.p = sb.SubFVParams()
        p.nx, p.ny = nxc, nyc
        p.free_mask = sum(mfe._FREE_BITS[e] for e in d.free)
        p.n_src, p.n_series = len(d.src_cells), len(d.series)
        p.r, p.m = r, r * r
        p.dx, p.dt0, p.alpha, p.g = dxc, d.dt0, d.cfl, G
        p.dry, p.arr_depth = DRY_M, d.arr_depth
        p.wsub = dxf
        # first step's dt: still-water wave speed of the deepest cell
        h0c = self.depth2()
        h0max = float(h0c.max()) if h0c.size else 0.0
        w0 = math.sqrt(G * h0max) if h0max > DRY_M else math.sqrt(G * DRY_M)
        a["st"][S_WMAX] = np.float32(w0).view(np.uint32)

    def eta2(self):
        V = self.a["V"].reshape(self.nyc, self.nxc)
        e = sg.invert_eta(V, self.t.s, self.t.csum, self.t.asub)
        return np.where(self.okc, e, self.t.zbed)

    def depth2(self):
        return np.maximum(self.eta2() - self.t.zbed, 0.0)

    def volume(self):
        return float(self.a["V"].sum(dtype=np.float64))

    def batch(self, t, t_event, nsteps, clip):
        """Advance the full coarse grid one batch (no wet-window shrinking; coarse grids are small)."""
        st = self.a["st"]
        p = self.p
        p.r0, p.c0, p.wr, p.wc = 0, 0, self.nyc, self.nxc
        p.t0, p.t_end = t, t_event - t
        p.clip_end = int(bool(clip))
        wm = st[S_WMAX]
        st[:] = 0
        st[S_WMAX] = wm
        _setf(st, S_MINDT, 1e30)
        self.b.run_batch(p, nsteps)
        return {"t_local": _f(st, S_T), "dt": _f(st, S_LASTDT), "steps": int(st[S_STEPS]),
                "min_dt": _f(st, S_MINDT), "vin": _f(st, S_VIN), "vout": _f(st, S_VOUT),
                "clip": int(st[S_CLIP]) * MF_FIX, "qin": _f(st, S_QIN),
                "qout": int(st[S_QOUT]) * MF_FIX}


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto"):
    """Run a .par deck with the sub-grid finite-volume scheme. backend: 'auto', 'metal' or 'cpu'."""
    def say(msg):
        if log:
            log(msg)

    deck = Deck(par_path)
    mfe.refuse_rain(deck, "sub-grid FV")
    try:
        bk = be.make_backend(backend, spec=sb.SUBFV_SPEC, log=say)
    except be.BackendError as e:
        raise EngineError(str(e))
    try:
        return _run(deck, bk, progress, cancel, say)
    finally:
        bk.close()


def _run(d, bk, progress, cancel, say):
    eng = Engine(d, bk)
    a = eng.a
    r = eng.r
    nyf, nxf = eng.nyf, eng.nxf
    dx = d.dx
    xll, yll = d.xll, d.yll
    root = d.root
    sim, massint, saveint = d.sim, d.massint, d.saveint

    def fine_depth_now():
        """Current water depth downscaled to the fine grid, cropped to the deck extent."""
        fd = sg.fine_depth(eng.eta2(), eng.dem_fine, r)
        return fd[:nyf, :nxf]

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
        area = float((eng.depth2() > DRY_M).sum()) * eng.dxc * eng.dxc
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
            want = int(math.ceil((t_event - t) / max(mean_dt, 1e-3))) + 1
            n = max(1, min(BATCH_STEPS, want))
            out = eng.batch(t, t_event, n, False)
            batches += 1
            if out["steps"]:
                vin += out["vin"]; vout += out["vout"]; clip += out["clip"]
                steps += out["steps"]
                min_dt = min(min_dt, out["min_dt"])
                last_dt = out["dt"]
                qin, qout = out["qin"], out["qout"]
                mean_dt = out["t_local"] / out["steps"]
            if not out["steps"] and out["t_local"] < (t_event - t) - 2e-3:
                raise EngineError("Boann (sub-grid FV) made no progress at t = {:.1f} s".format(t))
            t += out["t_local"]
            if (t_event - t) <= 2e-3:
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

    # downscale the time-maxima to the deck's native (fine) resolution
    valid = d.valid
    hmax_c = a["hmax"].reshape(eng.nyc, eng.nxc).astype(np.float64)
    eta_max_c = hmax_c + eng.t.zbed                       # max water-surface elevation per coarse cell
    fd_max = sg.fine_depth(eta_max_c, eng.dem_fine, r)[:nyf, :nxf]
    up = lambda x: np.repeat(np.repeat(x, r, axis=0), r, axis=1)[:nyf, :nxf]
    vmax_f = up(a["vmax"].reshape(eng.nyc, eng.nxc).astype(np.float64))
    arr_f = up(a["arr"].reshape(eng.nyc, eng.nxc).astype(np.float64))
    tmax_f = up(a["tmax"].reshape(eng.nyc, eng.nxc).astype(np.float64))
    wetf = fd_max > DRY_M
    bl._write_asc(root + ".max", np.where(valid, fd_max, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxVc", np.where(valid, vmax_f, NODATA), xll, yll, dx)
    bl._write_asc(root + ".inittm", np.where(valid, arr_f, NODATA), xll, yll, dx, fmt="%.4f")
    bl._write_asc(root + ".maxtm", np.where(valid & wetf, tmax_f, NODATA), xll, yll, dx, fmt="%.4f")
    bl._write_asc(root + ".mxe", np.where(valid & wetf, d.z + fd_max, NODATA), xll, yll, dx)

    vol = eng.volume()
    wall = time.time() - t_start
    stats = {"engine": NAME, "version": VERSION, "backend": bk.kind, "device": bk.device,
             "scheme": "subfv", "subgrid_r": r, "coarse_cells": eng.nyc * eng.nxc,
             "fine_cells": nyf * nxf, "steps": steps, "batches": batches, "sim_time_s": t,
             "wall_s": round(wall, 2), "injected_m3": vin, "outflow_m3": vout,
             "final_volume_m3": vol, "mass_error_m3": vol - (vol0 + vin - vout), "clipped_m3": clip}
    say("Boann, sub-grid FV ({}, {}): r={}, {} coarse cells ({}x fewer), {} steps, {:.2f} h in "
        "{:.1f} min; volume error {:.3g} m3 of {:.4g} m3 injected.".format(
            bk.kind, bk.device, r, eng.nyc * eng.nxc, r * r, steps, t / 3600.0, wall / 60.0,
            stats["mass_error_m3"], vin))
    return stats


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="metalflood.engine_subfv")
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
