"""Boann, shock-capturing: the HLLC finite-volume solver. The same LISFLOOD-FP input/output files as the
local-inertial engine (engine.py), but run with the well-balanced HLLC finite-volume scheme of
kernels_fv.metal. It resolves hydraulic jumps and dam-break fronts the diffusive ACC scheme
smooths, at the cost of a shorter time step (CFL on |u|+c, not sqrt(g h)).

Selected by a 'fv' keyword in the .par file, or by calling run_par here directly. Everything
outside the per-cell state - the deck reader, the .mass/.max/.discharge writers, the batching and
windowing - is shared with engine.py. Arithmetic is float32 (Apple GPUs have no float64); the bed
is stored relative to the lowest cell and volumes are summed in float64 on the host.

Run it alone:  python -m metalflood.engine_fv run.par [-backend metal|cpu] [-v]"""
import math
import os
import time

import numpy as np

from . import deckio as bl
from . import backends as be
from . import engine as mfe
from .engine import Deck, _f, _setf

NAME = "Boann, shock-capturing (HLLC finite volume, on the GPU via Metal)"
VERSION = "0.3.0"     # 0.3.0 (Runner 3.98.0): FV1 adds rain on the grid (new input; FV2/DG2 refuse
                      # it still). 0.2.0 (Runner 3.93.0): a face against NoData is a reflecting wall
G = bl.G
DRY_M = bl.DRY_M
NODATA = bl.NODATA
BATCH_STEPS = 64
WINDOW_MARGIN = BATCH_STEPS + 4

EngineError = bl.EngineError
Cancelled = bl.Cancelled

_FREE_BITS = {"N": 1, "S": 2, "E": 4, "W": 8}
# state words (kernels_fv.metal): S_WMAX carries the max wave speed, not the max depth
S_T, S_DT, S_WMAX, S_WNEXT, S_VIN, S_VOUT, S_CLIP, S_STEPS, S_MINDT = range(9)
S_RMIN, S_RMAX, S_CMIN, S_CMAX, S_QIN, S_QOUT, S_LASTDT = range(9, 16)
S_RAIN, S_RAIND = 16, 17        # FV1 rainfall: rate this step, depth this batch (mirrors ACC)
MF_FIX = 1.0e-4


class Engine:
    def __init__(self, deck, backend, limiter=None):
        self.d = d = deck
        self.b = backend
        ny, nx = d.ny, d.nx
        # rainfall rides as one more series after the inflow series (index n_series), as in engine.py
        self.series = list(d.series) + ([("rain",) + tuple(d.rain)] if d.rain is not None else [])
        n_values = sum(len(s[1]) for s in self.series)
        spec = getattr(backend, "spec", None)
        # each 2nd-order scheme needs its own layout: FV2 adds the previous-step state; DG2 stores
        # the three modal coefficients of each conserved variable packed (and their RK2 copy).
        self.is_dg2 = spec is be.DG2_SPEC
        is_fv2 = spec is be.FV2_SPEC
        self.scheme = "dg2" if self.is_dg2 else ("fv2" if is_fv2 else "fv")
        lay = (be.dg2_buffer_layout if self.is_dg2 else
               be.fv2_buffer_layout if is_fv2 else be.fv_buffer_layout)
        layout = lay(ny, nx, len(d.src_cells), len(self.series), n_values)
        a = self.a = backend.allocate(layout)
        self.zref = float(d.z[d.valid].min()) if d.valid.any() else 0.0
        a["z"][:] = np.where(d.valid, d.z - self.zref, 0.0).ravel()
        a["n2"][:] = (d.n ** 2).ravel()
        if self.is_dg2:
            # DG2: the cell-average depth is modal coefficient 0; the slopes start at 0 (flat within
            # each cell), which is exact and well balanced for a lake at rest over a piecewise-
            # constant bed. The discharge modes (Uhu, Uhv) start at 0.
            a["Uh"][0::3] = d.h0.ravel()
        else:
            a["h"][:] = d.h0.ravel()
        a["ok"][:] = d.valid.ravel()
        a["hmax"][:] = 0.0
        a["tmax"][:] = NODATA
        a["arr"][:] = NODATA
        off = 0
        for k, (_, ts, qs) in enumerate(self.series):
            a["ser_off"][k] = off
            a["ser_t"][off:off + len(ts)] = ts
            a["ser_q"][off:off + len(ts)] = qs
            off += len(ts)
        a["ser_off"][len(self.series)] = off
        for i, (r, c, k) in enumerate(d.src_cells):
            a["s_cell"][i] = r * nx + c
            a["s_series"][i] = k
        # first step's dt: the still-water wave speed of the deepest cell (velocities start at 0)
        hgrid = self.h2()
        h0max = float(hgrid.max()) if hgrid.size else 0.0
        w0 = math.sqrt(G * h0max) if h0max > DRY_M else 0.0
        a["st"][S_WMAX] = np.float32(w0).view(np.uint32)
        self.src_rc = (np.array([s[0] for s in d.src_cells], int),
                       np.array([s[1] for s in d.src_cells], int))
        p = self.p = be.Params()
        p.nx, p.ny = nx, ny
        p.free_mask = sum(_FREE_BITS[e] for e in d.free)
        p.n_src, p.n_series = len(d.src_cells), len(d.series)
        p.dx, p.dt0, p.alpha, p.g = d.dx, d.dt0, d.cfl, G
        p.dry, p.arr_depth = DRY_M, d.arr_depth
        p.rain = int(d.rain is not None)          # FV1 rainfall (ACC-matched); FV2/DG2 refuse it
        self.rain_area = float(d.valid.sum()) * d.dx * d.dx
        if self.is_dg2:
            # the DG2 slope limiter: on by default (float32 rest stability), the deck's
            # 'nolimitslopes' or an explicit limiter=False turns it off.
            lim = getattr(deck, "dg2_limiter", True) if limiter is None else bool(limiter)
            p.fric2d = 1 if lim else 0
            self.limiter = bool(lim)
        self.win = self._window_from(hgrid > 0)

    def h2(self):
        if self.is_dg2:
            return self.a["Uh"][0::3].reshape(self.d.ny, self.d.nx)
        return self.a["h"].reshape(self.d.ny, self.d.nx)

    def _bbox(self, rmin, rmax, cmin, cmax):
        d = self.d
        if d.rain is not None:
            return (0, d.ny, 0, d.nx)        # rain falls on every cell: the window is the grid
        if len(self.src_rc[0]):
            rmin, rmax = min(rmin, self.src_rc[0].min()), max(rmax, self.src_rc[0].max())
            cmin, cmax = min(cmin, self.src_rc[1].min()), max(cmax, self.src_rc[1].max())
        if rmin > rmax:
            return (0, 1, 0, 1)
        m = WINDOW_MARGIN
        return (max(0, rmin - m), min(d.ny, rmax + m + 1),
                max(0, cmin - m), min(d.nx, cmax + m + 1))

    def _window_from(self, wet):
        rows = np.flatnonzero(wet.any(axis=1))
        cols = np.flatnonzero(wet.any(axis=0))
        if not len(rows):
            return self._bbox(1, 0, 1, 0)
        return self._bbox(rows[0], rows[-1], cols[0], cols[-1])

    def batch(self, t, t_event, nsteps, clip):
        st = self.a["st"]
        r0, r1, c0, c1 = self.win
        p = self.p
        p.r0, p.c0, p.wr, p.wc = r0, c0, r1 - r0, c1 - c0
        p.t0, p.t_end = t, t_event - t
        p.clip_end = int(bool(clip))
        wm = st[S_WMAX]
        st[:] = 0
        st[S_WMAX] = wm
        _setf(st, S_MINDT, 1e30)
        st[S_RMIN] = st[S_CMIN] = 0xFFFFFFFF
        self.b.run_batch(p, nsteps)
        out = {"t_local": _f(st, S_T), "dt": _f(st, S_LASTDT), "steps": int(st[S_STEPS]),
               "min_dt": _f(st, S_MINDT), "vin": _f(st, S_VIN), "vout": _f(st, S_VOUT),
               "clip": int(st[S_CLIP]) * MF_FIX, "qin": _f(st, S_QIN),
               "qout": int(st[S_QOUT]) * MF_FIX,
               "vrain": _f(st, S_RAIND) * getattr(self, "rain_area", 0.0)}
        rmin, rmax, cmin, cmax = (int(st[k]) for k in (S_RMIN, S_RMAX, S_CMIN, S_CMAX))
        if out["steps"]:
            self.win = self._bbox(rmin, rmax, cmin, cmax) if rmin <= rmax \
                else self._bbox(1, 0, 1, 0)
        return out


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto", order=1,
            limiter=None):
    """Run a .par file with the shock-capturing scheme. backend: 'auto', 'metal' or 'cpu'.
    order 1 = first-order HLLC (FV1); order 2 = 2nd-order MUSCL + SSP-RK2 (FV2); order 3 = 2nd-order
    RKDG2 (discontinuous Galerkin). limiter (DG2 only): the local slope limiter; None reads the deck
    (on unless the .par has 'nolimitslopes'), True/False overrides it."""
    def say(msg):
        if log:
            log(msg)

    deck = Deck(par_path)
    if int(order) >= 2:                    # FV1 adds rainfall (ACC-matched); FV2/DG2 do not yet
        mfe.refuse_rain(deck, "FV2 / DG2")
    spec = (be.DG2_SPEC if int(order) >= 3 else
            be.FV2_SPEC if int(order) >= 2 else be.FV_SPEC)
    lim = getattr(deck, "dg2_limiter", True) if limiter is None else bool(limiter)
    try:
        bk = be.make_backend(backend, spec=spec, log=say)
    except be.BackendError as e:
        raise EngineError(str(e))
    try:
        return _run(deck, bk, progress, cancel, say, limiter=lim)
    finally:
        bk.close()


def _run(d, bk, progress, cancel, say, limiter=False):
    eng = Engine(d, bk, limiter=limiter)
    a = eng.a
    ny, nx, dx = d.ny, d.nx, d.dx
    xll, yll = d.xll, d.yll
    ytop = yll + ny * dx
    root = d.root
    sim, massint, saveint = d.sim, d.massint, d.saveint

    gauge_cells = []
    for gx, gy, dd, w in d.gauges:
        k = max(1, int(round(w / dx)))
        c = int(np.floor((gx - xll) / dx))
        r = int(np.floor((ytop - gy) / dx))
        if dd in ("E", "W"):
            gauge_cells.append(("x", np.clip(np.arange(r, r + k), 0, ny - 1),
                                np.clip(c, 0, nx), 1.0 if dd == "E" else -1.0))
        else:
            gauge_cells.append(("y", np.clip(r, 0, ny), np.clip(np.arange(c, c + k), 0, nx - 1),
                                -1.0 if dd == "N" else 1.0))

    def gauge_q():
        fx = a["fxh"].reshape(ny, nx + 1)
        fy = a["fyh"].reshape(ny + 1, nx)
        out = []
        for kind, ga, gb, sign in gauge_cells:
            q = fx[ga, gb] if kind == "x" else fy[ga, gb]
            out.append(sign * float(np.asarray(q, np.float64).sum()) * dx)
        return out

    def volume():
        return float(eng.h2().sum(dtype=np.float64)) * dx * dx

    def h_out():
        return eng.h2().astype(np.float64)

    mass_f = open(root + ".mass", "w")
    mass_f.write("Time         Tstep      MinTstep   NumTsteps    Area         Vol         "
                 "Qin         Hds        Qout          Qerror         Verror       "
                 "Rain-Inf+Evap\n")
    dis_f = open(root + ".discharge", "w") if d.gauges else None
    if dis_f:
        dis_f.write("Time" + "".join("  gauge{}".format(i + 1) for i in range(len(d.gauges)))
                    + "\n")

    t = 0.0
    steps, min_dt, last_dt = 0, 1e9, d.dt0
    mean_dt = d.dt0
    next_mass = massint
    snap_k = 0
    next_snap = 0.0 if saveint <= sim else None
    vol0 = volume()
    vin = vout = clip = vrain = 0.0
    qin = qout = 0.0
    batches = 0
    t_start = time.time()
    last_prog = t_start

    def write_mass(tt):
        vol = volume()
        verr = vol - (vol0 + vin - vout)       # vin already includes rain (folded in below)
        area = float((eng.h2() > DRY_M).sum()) * dx * dx
        mass_f.write("{:<12.3f} {:<10.4f} {:<10.4f} {:<12d} {:<12.1f} {:<12.3f} {:<12.4f} "
                     "{:<10.4f} {:<13.4f} {:<14.6f} {:<12.4f} {:<12.4f}\n".format(
                         tt, last_dt, min_dt if steps else last_dt, steps, area, vol, qin,
                         0.0, qout, 0.0, verr - write_mass.last_err, vrain))
        write_mass.last_err = verr
        write_mass.last_t = tt
        mass_f.flush()
        if dis_f:
            dis_f.write("{:.3f}".format(tt) + "".join(" {:.4f}".format(v) for v in gauge_q())
                        + "\n")
            dis_f.flush()
    write_mass.last_err = 0.0
    write_mass.last_t = 0.0

    try:
        while t < sim - 1e-6:
            if cancel and cancel():
                raise Cancelled("cancelled")
            if next_snap is not None and t >= next_snap - 1e-6:
                bl._write_asc("{}-{:04d}.wd".format(root, snap_k), h_out(), xll, yll, dx)
                snap_k += 1
                next_snap = snap_k * saveint if snap_k * saveint <= sim + 1e-9 else None
            t_event = min(sim, next_mass, next_snap if next_snap is not None else sim)
            cut = False      # the clip_end flag; NOT the clipped-volume total `clip`
            want = int(math.ceil((t_event - t) / max(mean_dt, 1e-3))) + 1
            n = max(1, min(BATCH_STEPS, want))
            out = eng.batch(t, t_event, n, cut)
            batches += 1
            if out["steps"]:
                vin += out["vin"] + out["vrain"]   # fold rain into injected (as the ACC engine does)
                vout += out["vout"]
                vrain += out["vrain"]
                clip += out["clip"]
                steps += out["steps"]
                min_dt = min(min_dt, out["min_dt"])
                last_dt = out["dt"]
                qin, qout = out["qin"], out["qout"]
                mean_dt = out["t_local"] / out["steps"]
            if cut and (t_event - t) - out["t_local"] <= 2e-3:
                t = t_event
            else:
                if not out["steps"] and out["t_local"] < (t_event - t) - 2e-3:
                    raise EngineError("Boann (shock-capturing) made no progress at t = {:.1f} s".format(t))
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
            bl._write_asc("{}-{:04d}.wd".format(root, snap_k), h_out(), xll, yll, dx)
    finally:
        mass_f.close()
        if dis_f:
            dis_f.close()

    valid = d.valid
    shape = (ny, nx)
    hmax = a["hmax"].reshape(shape).astype(np.float64)
    vmax = a["vmax"].reshape(shape).astype(np.float64)
    arrival = a["arr"].reshape(shape).astype(np.float64)
    tmax = a["tmax"].reshape(shape).astype(np.float64)
    bl._write_asc(root + ".max", np.where(valid, hmax, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxVc", np.where(valid, vmax, NODATA), xll, yll, dx)
    bl._write_asc(root + ".inittm", np.where(valid, arrival, NODATA), xll, yll, dx, fmt="%.4f")
    bl._write_asc(root + ".maxtm", np.where(valid & (hmax > 0), tmax, NODATA), xll, yll, dx,
                  fmt="%.4f")
    bl._write_asc(root + ".mxe", np.where(valid & (hmax > 0), d.z + hmax, NODATA), xll, yll, dx)
    vol = volume()
    wall = time.time() - t_start
    stats = {"engine": NAME, "version": VERSION, "backend": bk.kind, "device": bk.device,
             "scheme": getattr(getattr(bk, "spec", None), "name", "fv"),
             "steps": steps, "batches": batches, "sim_time_s": t,
             "wall_s": round(wall, 2), "cells": nx * ny, "injected_m3": vin, "outflow_m3": vout,
             "final_volume_m3": vol, "mass_error_m3": vol - (vol0 + vin - vout),
             "clipped_m3": clip, "rain_m3": vrain}
    # where the time went (backends.Profile); timing only, no result depends on it
    stats["profile"] = bk.profile() if hasattr(bk, "profile") else None
    say("Boann, shock-capturing ({}, {}): {} steps, {:.2f} h simulated in {:.1f} min; volume error {:.3g} m3 "
        "of {:.4g} m3 injected.".format(bk.kind, bk.device, steps, t / 3600.0, wall / 60.0,
                                        stats["mass_error_m3"], vin))
    return stats


def main(argv):
    args = [x for x in argv[1:] if not x.startswith("-")]
    backend = "auto"
    for i, x in enumerate(argv):
        if x in ("-backend", "--backend") and i + 1 < len(argv):
            backend = argv[i + 1]
            args = [y for y in args if y != backend]
    if "-version" in argv or not args:
        print(NAME + " " + VERSION)
        return 0
    try:
        stats = run_par(args[-1], log=print, backend=backend,
                        progress=(lambda t, s: print("t = {:.2f} h".format(t / 3600.0)))
                        if "-v" in argv else None)
    except (EngineError, OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 1
    print("wall {:.1f} s".format(stats["wall_s"]))
    return 0
