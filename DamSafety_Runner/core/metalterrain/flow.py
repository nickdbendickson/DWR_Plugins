"""BoannGrid D4 flow directions + accumulation on the depression-filled surface (Apple GPU via
Metal, with a CPU build of the same kernels for the cloud).

    from nd_breach_runner.core.metalterrain import flow
    direction, accum, info = flow.flow_accum(dem, backend="auto")   # NaN / -9999 = NoData

`direction` is the single D4 downstream neighbour per cell (0=N, 1=E, 2=S, 3=W, 4=outlet/sink,
-1=NoData). `accum` is the drainage area in CELL COUNTS (1 at a ridge, up to the basin size at an
outlet); multiply by the cell area for m^2. It is 4-connected to match the local-inertial flood
engine, and is computed on the filled surface from core/metalterrain.fill, so the network is
acyclic and the accumulation has a unique fixed point reached from any thread order.

It reuses core/metalflood's backend (backends.Spec) and shim, exactly as the fill does.
"""
import ctypes
import os

import numpy as np

from ..metalflood import backends as be
from . import fill as F

HERE = os.path.dirname(os.path.abspath(__file__))
KERNEL_SOURCE = os.path.join(HERE, "kernels_flow.metal")
CPU_SOURCE = os.path.join(HERE, "cpu_kernels_flow.cpp")
KERNEL_SOURCE8 = os.path.join(HERE, "kernels_flow8.metal")
CPU_SOURCE8 = os.path.join(HERE, "cpu_kernels_flow8.cpp")
SHIM = os.path.join(os.path.dirname(os.path.abspath(be.__file__)), "mf_shim.h")  # metalflood's
KERNELS = ("k_d4_dir", "k_accum_a", "k_accum_b")
KERNELS8 = ("k_d8_dir", "k8_accum_a", "k8_accum_b")
NODATA = F.NODATA
BIG = np.float32(1.0e18)              # NoData sentinel in the surface: never a lower neighbour

# D4 direction codes (match kernels_flow.metal): NONE = outlet / sink
N, E, S, W, NONE = 0, 1, 2, 3, 4
_DRC = {N: (-1, 0), E: (0, 1), S: (1, 0), W: (0, -1)}
# D8 adds the four diagonals (match kernels_flow8.metal); NONE8 = outlet / sink
NE, SE, SW, NW, NONE8 = 4, 5, 6, 7, 8
_DRC8 = {N: (-1, 0), E: (0, 1), S: (1, 0), W: (0, -1),
         NE: (-1, 1), SE: (1, 1), SW: (1, -1), NW: (-1, -1)}


def drc(conn):
    """The direction-code -> (dr, dc) map for `conn` (4 or 8)."""
    return _DRC if conn == 4 else _DRC8


def none_code(conn):
    """The outlet/sink direction code for `conn` (4 or 8)."""
    return NONE if conn == 4 else NONE8


class TParams(ctypes.Structure):
    """struct TParams of kernels_flow.metal / kernels_flow8.metal (identical layout)."""
    _fields_ = [(n, ctypes.c_uint32) for n in ("nx", "ny", "r0", "c0", "wr", "wc", "pad", "pad2")]


def _plan(p):
    g = (p.wc, p.wr, 1)
    return [("k_d4_dir", g), ("k_accum_a", g), ("k_accum_b", g)]


def _plan8(p):
    g = (p.wc, p.wr, 1)
    return [("k_d8_dir", g), ("k8_accum_a", g), ("k8_accum_b", g)]


SPEC = be.Spec("mf4", KERNEL_SOURCE, SHIM, CPU_SOURCE, KERNELS, TParams, _plan)
SPEC8 = be.Spec("mf8", KERNEL_SOURCE8, SHIM, CPU_SOURCE8, KERNELS8, TParams, _plan8)
_SPEC_FOR = {4: SPEC, 8: SPEC8}


def _layout(ny, nx):
    f, u8, u = np.float32, np.uint8, np.uint32
    n = ny * nx
    return [("w", f, n), ("ok", u8, n), ("dir", u8, n), ("a0", u, n), ("a1", u, n), ("st", u, 4)]


def _topo_accum(direction, valid, conn):
    """Drainage area (cell counts) on a direction field by Kahn topological order: every cell
    starts at 1, and a cell hands its total to its downstream neighbour once all of its own donors
    have been counted. Level-synchronous, so each level is one vectorised numpy step and the cost is
    O(cells + levels), not the gather iteration's O(cells x longest flow path).

    It gives the same numbers as the kernels' fixed point (1 + sum of donors), because the network
    is acyclic: every direction points to a strictly lower cell. Returns (accum int64 flat, levels,
    every_valid_cell_drained)."""
    ny, nx = direction.shape
    n = ny * nx
    d = direction.ravel()
    ok = valid.ravel()
    down = np.full(n, -1, dtype=np.int64)
    for code, (dr, dc) in drc(conn).items():
        idx = np.nonzero(ok & (d == code))[0]
        if idx.size:
            r, c = np.divmod(idx, nx)
            r2, c2 = r + dr, c + dc
            inb = (r2 >= 0) & (r2 < ny) & (c2 >= 0) & (c2 < nx)
            t = np.full(idx.size, -1, dtype=np.int64)
            t[inb] = r2[inb] * nx + c2[inb]
            down[idx] = t
    has = down >= 0
    has[has] = ok[down[has]]              # the kernels only count donors into a valid cell
    down[~has] = -1
    acc = ok.astype(np.int64)
    indeg = np.bincount(down[has], minlength=n).astype(np.int64)
    frontier = np.nonzero(ok & (indeg == 0))[0]
    drained = int(frontier.size)
    levels = 0
    while frontier.size:
        f = frontier[down[frontier] >= 0]
        if not f.size:
            break
        t = down[f]
        np.add.at(acc, t, acc[f])
        np.subtract.at(indeg, t, 1)
        tu = np.unique(t)
        frontier = tu[indeg[tu] == 0]
        drained += int(frontier.size)
        levels += 1
    return acc, levels, drained == int(ok.sum())


def flow_accum(dem, eps=1e-5, backend="auto", filled=None, sweeps_per_batch=0, max_sweeps=0,
               conn=4, log=None, method="topo"):
    """Flow direction and accumulation for `dem` (2-D; NaN or -9999 = NoData). Returns
    (direction, accum, info).

    `conn` is the connectivity: 4 (default, D4 - 4-connected, matches the local-inertial flood
    engine) or 8 (D8 - distance-weighted steepest descent, the standard GIS / WhiteboxTools
    drainage area for channel extraction). `direction` is int8 (-1 = NoData; for D4, 0-3 = N/E/S/W
    and 4 = outlet/sink; for D8, 0-7 = N/E/S/W/NE/SE/SW/NW and 8 = outlet/sink - use flow.drc(conn)
    and flow.none_code(conn)). `accum` is float64 drainage area in cell counts, NaN at NoData. Pass
    `filled` (the output of fill.fill_dem) to skip re-filling.

    `method` is how the drainage area is summed once the kernels have set the directions:
      "topo" (default, Runner 3.50.0): Kahn topological order on the host, O(cells + levels).
      "iterate": the kernels' gather sweeps until nothing changes (the pre-3.50.0 path). Each sweep
          moves area one cell downstream, so it needs about as many sweeps as the longest flow path;
          a 2001 x 2001 prairie-pothole grid on the CPU build was still running after 12 minutes.
    Both give identical directions and identical drainage areas (tests/test_metalterrain_flow.py
    pins that); "iterate" stays for that comparison."""
    if conn not in (4, 8):
        raise ValueError("conn must be 4 or 8")
    if method not in ("topo", "iterate"):
        raise ValueError("method must be 'topo' or 'iterate'")
    spec = _SPEC_FOR[conn]
    none_c = none_code(conn)
    a = np.asarray(dem, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError("dem must be 2-D")
    ny, nx = a.shape
    valid = np.isfinite(a) & (a != NODATA)
    if filled is None:
        filled, _fi = F.fill_dem(a, eps=eps, backend=backend, log=log)
    else:
        filled = np.asarray(filled, dtype=np.float64)
    # work in a datum-shifted float32 surface (relief, not altitude) so the eps drainage ramp
    # survives float32 and flats get a strict steepest-descent order, as the fill does.
    datum = float(filled[valid].min()) if valid.any() else 0.0
    w32 = np.where(valid, filled - datum, BIG).astype(np.float32)

    bk = be.make_backend(backend, log=log, spec=spec)
    try:
        arr = bk.allocate(_layout(ny, nx))
        arr["w"][:] = w32.ravel()
        arr["ok"][:] = valid.ravel().astype(np.uint8)
        arr["dir"][:] = none_c
        ones = valid.ravel().astype(np.uint32)      # 1 at valid cells, 0 at NoData
        arr["a0"][:] = ones
        arr["a1"][:] = ones
        p = TParams()
        p.nx, p.ny, p.r0, p.c0, p.wr, p.wc, p.pad, p.pad2 = nx, ny, 0, 0, ny, nx, 0, 0
        nvalid = int(valid.sum())
        if method == "topo":
            # one sweep sets the directions (k_d*_dir); the area is then summed on the host
            bk.run_batch(p, 1)
            direction = arr["dir"].reshape(ny, nx).astype(np.int16)
            direction = np.where(valid, direction, -1).astype(np.int8)
            acc, levels, drained = _topo_accum(direction, valid, conn)
            accum = np.where(valid, acc.reshape(ny, nx).astype(np.float64), np.nan)
            info = {"backend": bk.kind, "device": bk.device, "cells": nx * ny, "sweeps": 1,
                    "converged": bool(drained), "valid": nvalid, "conn": conn,
                    "method": "topo", "levels": levels}
            if not drained:
                info["note"] = "the direction field has a cycle; some cells were never drained"
            return direction, accum, info
        k = int(sweeps_per_batch) or max(16, nx + ny)
        cap = int(max_sweeps) or max(200 * (nx + ny), 2 * nvalid)
        sweeps, changed = 0, 1
        while changed and sweeps < cap:
            arr["st"][0] = 0
            bk.run_batch(p, k)
            changed = int(arr["st"][0])
            sweeps += k
        direction = arr["dir"].reshape(ny, nx).astype(np.int16)
        direction = np.where(valid, direction, -1).astype(np.int8)
        accum = arr["a0"].reshape(ny, nx).astype(np.float64)
        accum = np.where(valid, accum, np.nan)
        info = {"backend": bk.kind, "device": bk.device, "cells": nx * ny, "sweeps": sweeps,
                "converged": changed == 0, "valid": nvalid, "conn": conn, "method": "iterate"}
        if not info["converged"]:
            info["note"] = "accumulation did not converge in {} sweeps (raise max_sweeps)".format(
                cap)
        return direction, accum, info
    finally:
        bk.close()
