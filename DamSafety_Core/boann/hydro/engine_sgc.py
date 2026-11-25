"""DamSafety GPU flood engine, SGC variant: the ACC local-inertial engine (engine.py) WITH
LISFLOOD-FP's sub-grid channels (Neal et al. 2012). A cell may carry a rectangular channel
narrower than the cell and below the DEM that conveys and stores a flood the grid cannot resolve;
the kernels are kernels_sgc.metal (CPU build cpu_kernels_sgc.cpp), validated cell-for-cell against
the float64 reference sgc_ref.py.

Channel geometry comes from either the LISFLOOD-FP SGC rasters named in the .par
(SGCwidth / SGCbank / SGCbed, with SGCn), the same files core/lisflood.py writes for LISFLOOD-FP,
or - when none are given and build_from_dem is on - core/sgc.build on the DEM. State is the ACC
depth extended below the DEM in channel cells (h in [-D, 0] is water in the channel); depths are
reported ABOVE the bank, as LISFLOOD-FP SGC reports them.

Run it alone:  python -m metalflood.engine_sgc run.par [-backend metal|cpu]"""
import math
import os
import time

import numpy as np

from . import deckio as bl
from . import backends as be
from . import engine as mfe

NAME = "DamSafety GPU flood engine (local inertial + sub-grid channels, LISFLOOD-FP SGC compatible)"
VERSION = "0.1.0"
G = bl.G
NODATA = bl.NODATA
DRY_M = bl.DRY_M
BATCH_STEPS = mfe.BATCH_STEPS
DEFAULT_CHANNEL_N = 0.035

EngineError = bl.EngineError
Cancelled = bl.Cancelled

S_T, S_DT, S_HMAX = mfe.S_T, mfe.S_DT, mfe.S_HMAX
S_MINDT, S_RMIN, S_CMIN = mfe.S_MINDT, mfe.S_RMIN, mfe.S_CMIN
S_LASTDT, S_STEPS, S_VIN, S_VOUT, S_CLIP, S_QIN, S_QOUT = (
    mfe.S_LASTDT, mfe.S_STEPS, mfe.S_VIN, mfe.S_VOUT, mfe.S_CLIP, mfe.S_QIN, mfe.S_QOUT)
MF_FIX = mfe.MF_FIX
_f, _setf = mfe._f, mfe._setf


def storage(h, w, D, dx):
    """Cell water volume (m3), the piecewise SGC storage (mirrors kernels_sgc.sgc_storage)."""
    chan = w > 0
    Vbank = w * dx * D
    return np.where(chan & (h < 0.0), np.maximum(w * dx * (h + D), 0.0),
                    np.where(chan, Vbank + dx * dx * np.maximum(h, 0.0),
                             dx * dx * np.maximum(h, 0.0)))


class Deck(mfe.Deck):
    """An ACC deck plus sub-grid-channel geometry: per-cell width w, bankfull depth D (bed = z-D)
    and channel Manning n, and the in-channel start state."""

    def __init__(self, par_path, build_from_dem=True):
        super().__init__(par_path)
        wd = os.path.dirname(os.path.abspath(par_path))
        par = self.par

        def path(key):
            return os.path.join(wd, str(par[key]))

        ny, nx = self.ny, self.nx
        w = np.zeros((ny, nx))
        D = np.zeros((ny, nx))
        start_depth = np.zeros((ny, nx))
        self.channel_n = float(par.get("sgcn", DEFAULT_CHANNEL_N))
        self.sgc_source = "none"
        if "sgcwidth" in par and "sgcbed" in par:
            wg, _, _, _, wn = bl._load("SGC width file", bl._read_asc, path("sgcwidth"), (ny, nx))
            bg, _, _, _, bn = bl._load("SGC bed file", bl._read_asc, path("sgcbed"), (ny, nx))
            w = np.where((wg == wn) | ~np.isfinite(wg) | (wg <= 0), 0.0, wg)
            if "sgcbank" in par:
                kg, _, _, _, kn = bl._load("SGC bank file", bl._read_asc, path("sgcbank"), (ny, nx))
                bank = np.where((kg == kn) | ~np.isfinite(kg), self.z, kg)
            else:
                bank = self.z
            D = np.where(w > 0, np.maximum(bank - np.where((bg == bn) | ~np.isfinite(bg),
                                                           self.z, bg), 0.0), 0.0)
            self.sgc_source = "rasters"
        elif build_from_dem:
            w, D, start_depth = self._build_from_dem()
            self.sgc_source = "sgc.build"
        w = np.where(self.valid, w, 0.0)
        D = np.where((w > 0) & self.valid, D, 0.0)
        w = np.where(D > 0, w, 0.0)                 # a channel needs a positive depth too
        self.w, self.D = w, D
        self.channel_n2 = self.channel_n ** 2
        # start state (water level relative to the DEM, negative in channel): channel cells start at
        # their start depth above the bed (empty = at the bed, h = -D); floodplain start water (the
        # startfile, h0 >= 0) wins where it is present.
        h_chan = np.where(w > 0, start_depth - D, 0.0)
        self.h0 = np.where(self.valid, np.where(self.h0 > 0.0, self.h0, h_chan), 0.0)

    def _build_from_dem(self):
        """(w, D, start_depth) from the host's channel builder (host.py; the Runner lends its
        core/sgc.build) on this DEM. Returns zeros if no host lent one, so the engine still runs
        (as plain ACC)."""
        try:
            from . import host
            build = host.get("channel_builder")
        except Exception:
            build = None
        if build is None:
            return (np.zeros((self.ny, self.nx)),) * 3
        z = np.where(self.valid, self.z, NODATA)
        inflow_rc = self.src_cells[0][:2] if self.src_cells else None
        w, D, start_depth, self.channel_n = build(z, self.xll, self.yll, self.dx, NODATA,
                                                  inflow_rc, self.channel_n)
        return (np.asarray(w, float), np.asarray(D, float), np.asarray(start_depth, float))


class Engine:
    def __init__(self, deck, backend):
        self.d = d = deck
        self.b = backend
        ny, nx = d.ny, d.nx
        n_values = sum(len(s[1]) for s in d.series)
        layout = be.sgc_buffer_layout(ny, nx, len(d.src_cells), len(d.series), n_values)
        a = self.a = backend.allocate(layout)
        self.zref = float(d.z[d.valid].min()) if d.valid.any() else 0.0
        a["z"][:] = np.where(d.valid, d.z - self.zref, 0.0).ravel()
        a["n2"][:] = (d.n ** 2).ravel()
        a["nc2"][:] = d.channel_n2
        a["w"][:] = d.w.ravel()
        a["Dd"][:] = d.D.ravel()
        a["h"][:] = d.h0.ravel()
        a["ok"][:] = d.valid.ravel()
        a["scl"][:] = 1.0
        a["hmax"][:] = 0.0
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
        st[S_HMAX] = np.float32(max(self._celerity_h().max(), DRY_M)).view(np.uint32)
        self.src_rc = (np.array([s[0] for s in d.src_cells], int),
                       np.array([s[1] for s in d.src_cells], int))
        p = self.p = be.Params()
        p.nx, p.ny = nx, ny
        p.free_mask = sum(mfe._FREE_BITS[e] for e in d.free)
        p.n_src, p.n_series = len(d.src_cells), len(d.series)
        p.dx, p.dt0, p.alpha, p.g = d.dx, d.dt0, d.cfl, G
        p.theta, p.fric2d = d.theta, int(d.fric2d)
        p.inflow_lf_order = int(d.inflow_lf_order)
        p.dry, p.maxhflow = DRY_M, bl.MAX_HFLOW
        p.velmin, p.arr_depth = bl.VEL_MIN_DEPTH_M, d.arr_depth
        self.win = self._window_from(self.wet2())

    def h2(self):
        return self.a["h"].reshape(self.d.ny, self.d.nx)

    def _celerity_h(self):
        h = self.h2()
        w, D = self.d.w, self.d.D
        return np.where(w > 0, np.maximum(h + D, 0.0), np.maximum(h, 0.0))

    def wet2(self):
        return self._celerity_h() > 0.0

    def volume(self):
        return float(storage(self.h2(), self.d.w, self.d.D, self.d.dx).sum(dtype=np.float64))

    # window bookkeeping identical to the ACC engine
    _bbox = mfe.Engine._bbox
    _window_from = mfe.Engine._window_from
    batch = mfe.Engine.batch


def run_par(par_path, progress=None, cancel=None, log=None, backend="auto", build_from_dem=True):
    def say(msg):
        if log:
            log(msg)

    deck = Deck(par_path, build_from_dem=build_from_dem)
    mfe.refuse_rain(deck, "sub-grid channel (SGC)")
    mfe.refuse_breach(deck, "sub-grid channel (SGC)")
    try:
        bk = be.make_backend(backend, log=say, spec=be.SGC_SPEC)
    except be.BackendError as e:
        raise EngineError(str(e))
    try:
        return _run(deck, bk, progress, cancel, say)
    finally:
        bk.close()


def _run(d, bk, progress, cancel, say):
    eng = Engine(d, bk)
    a = eng.a
    ny, nx, dx = d.ny, d.nx, d.dx
    xll, yll = d.xll, d.yll
    ytop = yll + ny * dx
    root = d.root
    sim, massint, saveint = d.sim, d.massint, d.saveint

    gauge_cells = []
    for gx, gy, dd, wdt in d.gauges:
        k = max(1, int(round(wdt / dx)))
        c = int(np.floor((gx - xll) / dx))
        r = int(np.floor((ytop - gy) / dx))
        if dd in ("E", "W"):
            gauge_cells.append(("x", np.clip(np.arange(r, r + k), 0, ny - 1),
                                np.clip(c, 0, nx), 1.0 if dd == "E" else -1.0))
        else:
            gauge_cells.append(("y", np.clip(r, 0, ny), np.clip(np.arange(c, c + k), 0, nx - 1),
                                -1.0 if dd == "N" else 1.0))

    def gauge_q():
        qx = (a["qxc"] + a["qxf"]).reshape(ny, nx + 1)
        qy = (a["qyc"] + a["qyf"]).reshape(ny + 1, nx)
        out = []
        for kind, ga, gb, sign in gauge_cells:
            q = qx[ga, gb] if kind == "x" else qy[ga, gb]
            out.append(sign * float(np.asarray(q, np.float64).sum()))   # already m3/s per face
        return out

    def h_out():
        return np.maximum(eng.h2(), 0.0).astype(np.float64)    # depth above the DEM

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
    vol0 = eng.volume()
    vin = vout = clip = 0.0
    qin = qout = 0.0
    batches = 0
    t_start = time.time()
    last_prog = t_start

    def write_mass(tt):
        vol = eng.volume()
        verr = vol - (vol0 + vin - vout)
        area = float((h_out() > DRY_M).sum()) * dx * dx
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
            want = int(math.ceil((t_event - t) / max(mean_dt, 1e-3))) + 1
            n = max(1, min(BATCH_STEPS, want))
            out = eng.batch(t, t_event, n, clip=False)
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
                raise EngineError("the SGC engine made no progress at t = {:.1f} s".format(t))
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
    cells = ny * nx
    hmax = a["hmax"].reshape(shape).astype(np.float64)
    vmax = a["vmax"][:cells].reshape(shape).astype(np.float64)
    dv = a["vmax"][cells:].reshape(shape).astype(np.float64)
    arrival = a["arr"].reshape(shape).astype(np.float64)
    tmax = a["tmax"].reshape(shape).astype(np.float64)
    bl._write_asc(root + ".max", np.where(valid, hmax, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxVc", np.where(valid, vmax, NODATA), xll, yll, dx)
    bl._write_asc(root + ".maxDV", np.where(valid, dv, NODATA), xll, yll, dx)
    bl._write_asc(root + ".inittm", np.where(valid, arrival, NODATA), xll, yll, dx, fmt="%.4f")
    bl._write_asc(root + ".maxtm", np.where(valid & (hmax > 0), tmax, NODATA), xll, yll, dx,
                  fmt="%.4f")
    bl._write_asc(root + ".mxe", np.where(valid & (hmax > 0), d.z + hmax, NODATA), xll, yll, dx)
    vol = eng.volume()
    wall = time.time() - t_start
    stats = {"engine": NAME, "version": VERSION, "scheme": "sgc", "backend": bk.kind,
             "device": bk.device, "steps": steps, "batches": batches, "sim_time_s": t,
             "wall_s": round(wall, 2), "cells": nx * ny, "injected_m3": vin, "outflow_m3": vout,
             "final_volume_m3": vol, "mass_error_m3": vol - (vol0 + vin - vout), "clipped_m3": clip,
             "sgc_source": d.sgc_source, "channel_cells": int((d.w > 0).sum())}
    # where the time went (backends.Profile); timing only, no result depends on it
    stats["profile"] = bk.profile() if hasattr(bk, "profile") else None
    say("SGC engine ({}, {}): {} steps, {:.2f} h in {:.1f} min; {} channel cells; volume error "
        "{:.3g} m3 of {:.4g} m3 injected.".format(bk.kind, bk.device, steps, t / 3600.0,
        wall / 60.0, stats["channel_cells"], stats["mass_error_m3"], vin))
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
        stats = run_par(args[-1], log=print, backend=backend)
    except (EngineError, OSError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 1
    print("wall {:.1f} s; mass error {:.3g} m3".format(stats["wall_s"], stats["mass_error_m3"]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
