"""BoannGrid depression-fill: Planchon & Darboux (2002) relaxation on the Apple GPU (Metal),
with a CPU build of the same kernel for the cloud and computers without a Metal GPU.

It returns the depression-filled surface - every cell drains to an outlet - and agrees with
core/terrain.priority_flood (Barnes et al., the serial ground truth) to float precision. It
reuses core/metalflood's backend (backends.Spec) and shim; only the kernel, its Params and its
one-kernel dispatch plan are new.

    from nd_breach_runner.core.metalterrain import fill
    filled, info = fill.fill_dem(dem, eps=1e-5, backend="auto")   # NaN / -9999 = NoData

Outlets are the grid border and cells next to NoData (as in priority_flood); NoData cells act
as outlets for their valid neighbours, so a basin enclosed by NoData drains to that NoData
instead of filling without bound - the bug the Phase-A CPU conditioner hit.
"""
import ctypes
import os

import numpy as np

from ..metalflood import backends as be

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_SOURCE = os.path.join(HERE, "kernels_fill.metal")
CPU_SOURCE = os.path.join(HERE, "cpu_kernels_fill.cpp")
SHIM = os.path.join(os.path.dirname(os.path.abspath(be.__file__)), "mf_shim.h")  # reuse metalflood's
KERNELS = ("k_pd_relax",)
NODATA = -9999.0
BIG = np.float32(1.0e18)              # a start level no real terrain reaches


class TParams(ctypes.Structure):
    """struct TParams of kernels_fill.metal, field for field."""
    _fields_ = [(n, ctypes.c_uint32) for n in ("nx", "ny", "r0", "c0", "wr", "wc", "conn")] + \
               [(n, ctypes.c_float) for n in ("eps", "tol", "pad2")]


def _plan(p):
    return [("k_pd_relax", (p.wc, p.wr, 1))]


SPEC = be.Spec("mt", KERNEL_SOURCE, SHIM, CPU_SOURCE, KERNELS, TParams, _plan)


def _layout(ny, nx):
    f, u = np.float32, np.uint32
    return [("z", f, ny * nx), ("ok", np.uint8, ny * nx), ("w", f, ny * nx), ("st", u, 4)]


def _outlets(valid, conn=8):
    """Boolean grid of outlet cells: the grid border and valid cells next to NoData (8-adjacent,
    as core/terrain.priority_flood seeds them; 4-adjacent for a D4 fill). numpy only, so no scipy
    dependency."""
    ny, nx = valid.shape
    out = np.zeros_like(valid)
    out[0, :] = out[-1, :] = out[:, 0] = out[:, -1] = True
    nod = ~valid
    if nod.any():
        adj = np.zeros_like(valid)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if (dx == 0 and dy == 0) or (conn == 4 and dx != 0 and dy != 0):
                    continue
                ys0, ys1 = max(0, dy), ny + min(0, dy)
                xs0, xs1 = max(0, dx), nx + min(0, dx)
                yd0, yd1 = max(0, -dy), ny + min(0, -dy)
                xd0, xd1 = max(0, -dx), nx + min(0, -dx)
                adj[yd0:yd1, xd0:xd1] |= nod[ys0:ys1, xs0:xs1]
        out |= adj
    return out & valid


def fill_dem(dem, eps=1e-5, backend="auto", sweeps_per_batch=0, max_sweeps=0, log=None, conn=8):
    """Depression-fill `dem` (2-D array; NaN or -9999 = NoData). Returns (filled, info).
    `filled` is float64 with NoData left as NaN. `eps` is the minimal drainage slope increment
    (m) so flats drain, matching priority_flood. `backend`: 'auto', 'metal' or 'cpu'.

    `conn` (Runner 3.57.0) is how water may leave a cell: 8 (default, as priority_flood) lets it
    drain across a corner; 4 only to the four edge neighbours, which is how the ACC flood engine
    and D4 flow (flow.flow_accum's default) move water. A D8-filled surface still has D4 pits
    wherever the only way down is diagonal, so D4 flow on it had dead ends (Absaraka at 30 m:
    180,730 of them); flow.flow_accum refills those with conn=4."""
    if conn not in (4, 8):
        raise ValueError("conn must be 4 or 8")
    a = np.asarray(dem, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError("dem must be 2-D")
    ny, nx = a.shape
    valid = np.isfinite(a) & (a != NODATA)
    # Datum shift: the kernel runs in float32, where eps (1e-5 m) is lost to rounding once
    # elevations reach ~170 m (ULP(300 m) ~ 3e-5 > eps), so flats on ND terrain (~300 m) would
    # fill FLAT instead of with the eps drainage ramp priority_flood lays down. Relaxing in
    # (z - datum), datum = min valid elevation, keeps working values small (relief, not altitude)
    # so eps stays representable; the datum is added back in float64 at the end.
    datum = float(a[valid].min()) if valid.any() else 0.0
    zc = np.where(valid, a - datum, 0.0)                 # datum-shifted ground, float64
    z32 = zc.astype(np.float32)
    outlet = _outlets(valid, conn)
    w0 = np.where(valid, BIG, NODATA).astype(np.float32)
    w0[outlet] = z32[outlet]

    bk = be.make_backend(backend, log=log, spec=SPEC)
    try:
        arr = bk.allocate(_layout(ny, nx))
        arr["z"][:] = z32.ravel()
        arr["ok"][:] = valid.ravel().astype(np.uint8)
        arr["w"][:] = w0.ravel()
        p = TParams()
        p.nx, p.ny, p.r0, p.c0, p.wr, p.wc, p.conn = nx, ny, 0, 0, ny, nx, int(conn)
        p.eps, p.tol, p.pad2 = float(eps), 1.0e-6, 0.0
        k = int(sweeps_per_batch) or max(16, nx + ny)
        # The filled wave travels ~1 cell/sweep along the drainage path, so the worst case is the
        # longest drainage path, which is O(valid cells) (a winding channel), not O(perimeter).
        # Cap at 2x the valid-cell count (a loose bound on any simple path), with the perimeter as
        # a floor for tiny grids. Convergence (change-counter 0) still stops early in the usual case.
        nvalid = int(valid.sum())
        cap = int(max_sweeps) or max(200 * (nx + ny), 2 * nvalid)
        sweeps, changed = 0, 1
        while changed and sweeps < cap:
            arr["st"][0] = 0
            bk.run_batch(p, k)
            changed = int(arr["st"][0])
            sweeps += k
        filled = arr["w"].reshape(ny, nx).astype(np.float64) + datum   # undo the datum shift
        filled = np.where(valid, filled, np.nan)
        info = {"backend": bk.kind, "device": bk.device, "cells": nx * ny, "sweeps": sweeps,
                "converged": changed == 0, "eps": float(eps), "conn": int(conn)}
        if not info["converged"]:
            info["note"] = "did not converge in {} sweeps (raise max_sweeps)".format(cap)
        return filled, info
    finally:
        bk.close()
