"""DamSafety GPU flood engine: runs the LISFLOOD-FP input files the Runner writes (.par,
.dem.asc, .bci, .bdy, optional .n.asc / .start / .gauge) with the local-inertial scheme of
LISFLOOD-FP's "acceleration" solver on the Apple GPU (Metal), and writes the result files the
Runner reads (.max, .maxVc, .inittm, .maxtm, .mxe, .mass, .discharge, depth snapshots).

It is core/builtin_lisflood.py's scheme moved onto the GPU: the same faces, FREE edges,
donor-cell limiter, time step and outputs, but every step runs as GPU kernels
(kernels.metal) over the wet part of the grid, and the host only looks in every few dozen
steps (a "batch") for output times, the wet window and progress. Where there is no Apple GPU
the same kernel source runs as a C++ build on one CPU core (backends.CpuBackend).

Arithmetic is float32 (Apple GPUs have no float64). Bed elevations are stored relative to the
lowest cell so depths keep about 1e-5 m resolution, and volumes are summed in float64 on the
host. Expect results to match the float64 built-in engine to within a few mm of depth.

Run it alone:  python -m metalflood run.par [-backend metal|cpu] [-v]"""
import math
import os
import time

import numpy as np

from . import deckio as bl
from . import backends as be

NAME = "DamSafety GPU flood engine (local inertial on Metal, LISFLOOD-FP compatible)"
VERSION = "0.1.0"
G = bl.G
ALPHA = bl.ALPHA
DRY_M = bl.DRY_M
MAX_HFLOW = bl.MAX_HFLOW
VEL_MIN_DEPTH_M = bl.VEL_MIN_DEPTH_M
NODATA = bl.NODATA
# Steps per batch, and the window margin: the front moves at most one cell per step, so a
# margin larger than the batch keeps every wet cell inside the window the batch runs on.
BATCH_STEPS = 64
WINDOW_MARGIN = BATCH_STEPS + 4

EngineError = bl.EngineError
Cancelled = bl.Cancelled

_FREE_BITS = {"N": 1, "S": 2, "E": 4, "W": 8}
S_T, S_DT, S_HMAX, S_HNEXT, S_VIN, S_VOUT, S_CLIP, S_STEPS, S_MINDT = range(9)
S_RMIN, S_RMAX, S_CMIN, S_CMAX, S_QIN, S_QOUT, S_LASTDT = range(9, 16)
MF_FIX = 1.0e-4       # kernels.metal: fixed-point unit of the summed outflow and clip


def _f(st, k):
    return float(st[k:k + 1].view(np.float32)[0])


def _setf(st, k, v):
    st[k:k + 1].view(np.float32)[0] = v


class Deck:
    """The inputs of one .par file, read with deckio's readers (builtin_lisflood's)."""

    def __init__(self, par_path):
        wd = os.path.dirname(os.path.abspath(par_path))
        par = bl.read_par(par_path)
        self.par = par

        def path(key):
            return os.path.join(wd, str(par[key]))

        for key in ("demfile", "sim_time"):
            if key not in par:
                raise EngineError("the .par file has no '{}'".format(key))
        z, xll, yll, dx, nod = bl._load("DEM file", bl._read_asc, path("demfile"))
        z = np.where(z == nod, np.nan, z)
        self.ny, self.nx = z.shape
        self.xll, self.yll, self.dx = xll, yll, dx

        def num(key, default):
            v = par.get(key, default)
            try:
                return float(v)
            except (TypeError, ValueError):
                raise EngineError("the .par file's '{}' is not a number ({!r})".format(key, v))
        self.sim = num("sim_time", None)
        self.massint = num("massint", 300.0)
        self.saveint = num("saveint", self.sim + 10.0)
        self.arr_depth = num("inittm_depth", 0.0)
        self.dt0 = float(par.get("initial_tstep", 10.0))
        n_def = num("fpfric", 0.06)
        n = np.full(z.shape, n_def)
        if "manningfile" in par:
            ng, _, _, _, nn = bl._load("Manning's n file", bl._read_asc, path("manningfile"),
                                       z.shape)
            n = np.where((ng == nn) | ~np.isfinite(ng) | (ng <= 0), n_def, ng)
        h0 = np.zeros(z.shape)
        if "startfile" in par:
            s, _, _, _, sn = bl._load("start-depth file", bl._read_asc, path("startfile"),
                                      z.shape)
            h0 = np.where((s == sn) | ~np.isfinite(s), 0.0, s)
        pts, self.free = (bl._load("boundary (.bci) file", bl.read_bci, path("bcifile"))
                          if "bcifile" in par else ([], set()))
        bdy = bl._load("inflow (.bdy) file", bl.read_bdy, path("bdyfile")) \
            if "bdyfile" in par else {}
        ytop = yll + self.ny * dx
        by_name = {}
        for x, y, name in pts:
            c = int(np.floor((x - xll) / dx))
            r = int(np.floor((ytop - y) / dx))
            if not (0 <= r < self.ny and 0 <= c < self.nx):
                raise EngineError("point source {} ({:.1f}, {:.1f}) is outside the DEM".format(
                    name, x, y))
            by_name.setdefault(name, []).append((r, c))
        self.series = []                  # [(name, t_s, q m3/s per cell)]
        self.src_cells = []               # [(row, col, series index)]
        for name, cells in by_name.items():
            if name not in bdy:
                raise EngineError("the .bdy file has no series '{}'".format(name))
            ts, vals = bdy[name]
            k = len(self.series)
            self.series.append((name, np.asarray(ts, float), np.asarray(vals, float) * dx))
            for r, c in cells:
                self.src_cells.append((r, c, k))
        self.gauges = bl._load("gauge file", bl.read_gauges, path("gaugefile")) \
            if "gaugefile" in par else []
        self.solver = "fv1" if "fv1" in par else "acceleration"
        # LISFLOOD-FP 8.3's ACC options: cfl (0.7), theta (1.0: Bates et al. 2010; the Runner's
        # damped rerun writes 0.7) and the 2-D friction term (on unless "1Dfriction")
        self.cfl = num("cfl", ALPHA)
        self.theta = num("theta", 1.0)
        if not (0.0 < self.cfl <= 1.0 and 0.0 < self.theta <= 1.0):
            raise EngineError("cfl and theta must be in (0, 1] (cfl {}, theta {})".format(
                self.cfl, self.theta))
        self.fric2d = "1dfriction" not in par
        self.valid = np.isfinite(z) & (z != NODATA)
        self.z = np.where(self.valid, z, 0.0)
        self.n = np.where(self.valid, n, 0.0)
        self.h0 = np.where(self.valid, np.maximum(h0, 0.0), 0.0)
        res = os.path.join(wd, str(par.get("dirroot", "results")))
        os.makedirs(res, exist_ok=True)
        self.root = os.path.join(res, str(par.get("resroot", "run")))


class Engine:
    def __init__(self, deck, backend):
        self.d = d = deck
        self.b = backend
        ny, nx = d.ny, d.nx
        n_values = sum(len(s[1]) for s in d.series)
        layout = be.buffer_layout(ny, nx, len(d.src_cells), len(d.series), n_values)
        a = self.a = backend.allocate(layout)
        # bed relative to the lowest valid cell: float32 then resolves depths to ~1e-5 m
        self.zref = float(d.z[d.valid].min()) if d.valid.any() else 0.0
        a["z"][:] = np.where(d.valid, d.z - self.zref, 0.0).ravel()
        a["n2"][:] = (d.n ** 2).ravel()
        a["h"][:] = d.h0.ravel()
        a["ok"][:] = d.valid.ravel()
        a["scl"][:] = 1.0
        a["hmax"][:] = 0.0                  # LISFLOOD-FP: maxima from the first step on
        a["tmax"][:] = NODATA
        a["arr"][:] = NODATA
        off = 0
        for k, (_, ts, qs) in enumerate(d.series):
            a["ser_off"][k] = off
            a["ser_t"][off:off + len(ts)] = ts
            a["ser_q"][off:off + len(ts)] = qs
            off += len(ts)
        a["ser_off"][len(d.series)] = off
        for i, (r, c, k) in enumerate(d.src_cells):
            a["s_cell"][i] = r * nx + c
            a["s_series"][i] = k
        st = a["st"]
        st[S_HMAX] = np.float32(a["h"].max()).view(np.uint32)
        self.src_rc = (np.array([s[0] for s in d.src_cells], int),
                       np.array([s[1] for s in d.src_cells], int))
        p = self.p = be.Params()
        p.nx, p.ny = nx, ny
        p.free_mask = sum(_FREE_BITS[e] for e in d.free)
        p.n_src, p.n_series = len(d.src_cells), len(d.series)
        p.dx, p.dt0, p.alpha, p.g = d.dx, d.dt0, d.cfl, G
        p.theta, p.fric2d = d.theta, int(d.fric2d)
        p.dry, p.maxhflow, p.velmin, p.arr_depth = DRY_M, MAX_HFLOW, VEL_MIN_DEPTH_M, d.arr_depth
        self.win = self._window_from(self.h2() > 0)

    def h2(self):
        return self.a["h"].reshape(self.d.ny, self.d.nx)

    def _bbox(self, rmin, rmax, cmin, cmax):
        d = self.d
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
        """Up to nsteps steps from model time t until t_event is reached (with clip, the last
        step is cut to land on it; without, it passes it as LISFLOOD-FP passes its output
        times). Returns the batch's state words as a dict."""
        st = self.a["st"]
        r0, r1, c0, c1 = self.win
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
            # every cell wet during the batch lies in this box; cells still wet from before
            # are inside it too unless they dried, which needs no window
            self.win = self._bbox(rmin, rmax, cmin, cmax) if rmin <= rmax \
                else self._bbox(1, 0, 1, 0)
        return out


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto"):
    """Run a .par file (paths in it are relative to its folder), the same call as
    builtin_lisflood.run_par. backend: 'auto', 'metal' or 'cpu'. Returns a dict."""
    def say(msg):
        if log:
            log(msg)

    deck = Deck(par_path)
    try:
        bk = be.make_backend(backend, log=say)
    except be.BackendError as e:
        raise EngineError(str(e))
    try:
        return _run(deck, bk, progress, cancel, say)
    finally:
        bk.close()


def _run(d, bk, progress, cancel, say):
    if d.solver == "fv1":
        say("The GPU engine runs 'fv1' with its local-inertial scheme.")
    eng = Engine(d, bk)
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
        qx = a["qx"].reshape(ny, nx + 1)
        qy = a["qy"].reshape(ny + 1, nx)
        out = []
        for kind, ga, gb, sign in gauge_cells:
            q = qx[ga, gb] if kind == "x" else qy[ga, gb]
            out.append(sign * float(np.asarray(q, np.float64).sum()) * dx)
        return out

    def volume():
        return float(a["h"].sum(dtype=np.float64)) * dx * dx

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
    vin = vout = clip = 0.0
    qin = qout = 0.0
    batches = 0
    t_start = time.time()
    last_prog = t_start

    def write_mass(tt):
        vol = volume()
        verr = vol - (vol0 + vin - vout)
        area = float((a["h"] > DRY_M).sum()) * dx * dx
        mass_f.write("{:<12.3f} {:<10.4f} {:<10.4f} {:<12d} {:<12.1f} {:<12.3f} {:<12.4f} "
                     "{:<10.4f} {:<13.4f} {:<14.6f} {:<12.4f} {:<12.4f}\n".format(
                         tt, last_dt, min_dt if steps else last_dt, steps, area, vol, qin,
                         0.0, qout, 0.0, verr - write_mass.last_err, 0.0))
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
            # never cut a step to land on a time (as LISFLOOD-FP): a cut step is short, and the
            # Runner reads a short step in the .mass file as a collapsing solver
            clip = False
            want = int(math.ceil((t_event - t) / max(mean_dt, 1e-3))) + 1
            n = max(1, min(BATCH_STEPS, want))
            out = eng.batch(t, t_event, n, clip)
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
            if clip and (t_event - t) - out["t_local"] <= 2e-3:
                t = t_event
            else:
                if not out["steps"] and out["t_local"] < (t_event - t) - 2e-3:
                    raise EngineError("the GPU engine made no progress at t = {:.1f} s".format(t))
                t += out["t_local"]
                if (t_event - t) <= 2e-3:      # float32 rounding just short of the event
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
             "steps": steps, "batches": batches, "sim_time_s": t, "wall_s": round(wall, 2),
             "cells": nx * ny, "injected_m3": vin, "outflow_m3": vout, "final_volume_m3": vol,
             "mass_error_m3": vol - (vol0 + vin - vout), "clipped_m3": clip}
    say("GPU engine ({}, {}): {} steps, {:.2f} h simulated in {:.1f} min; volume error {:.3g} m3 "
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
