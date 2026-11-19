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
SHIM = os.path.join(os.path.dirname(os.path.abspath(be.__file__)), "mf_shim.h")  # metalflood's
KERNELS = ("k_d4_dir", "k_accum_a", "k_accum_b")
NODATA = F.NODATA
BIG = np.float32(1.0e18)              # NoData sentinel in the surface: never a lower neighbour

# direction codes (match kernels_flow.metal)
N, E, S, W, NONE = 0, 1, 2, 3, 4
_DRC = {N: (-1, 0), E: (0, 1), S: (1, 0), W: (0, -1)}


class TParams(ctypes.Structure):
    """struct TParams of kernels_flow.metal, field for field."""
    _fields_ = [(n, ctypes.c_uint32) for n in ("nx", "ny", "r0", "c0", "wr", "wc", "pad", "pad2")]


def _plan(p):
    g = (p.wc, p.wr, 1)
    return [("k_d4_dir", g), ("k_accum_a", g), ("k_accum_b", g)]


SPEC = be.Spec("mf4", KERNEL_SOURCE, SHIM, CPU_SOURCE, KERNELS, TParams, _plan)


def _layout(ny, nx):
    f, u8, u = np.float32, np.uint8, np.uint32
    n = ny * nx
    return [("w", f, n), ("ok", u8, n), ("dir", u8, n), ("a0", u, n), ("a1", u, n), ("st", u, 4)]


def flow_accum(dem, eps=1e-5, backend="auto", filled=None, sweeps_per_batch=0, max_sweeps=0,
               log=None):
    """D4 flow direction and accumulation for `dem` (2-D; NaN or -9999 = NoData). Returns
    (direction, accum, info). `direction` is int8 (0-3 = N/E/S/W, 4 = outlet/sink, -1 = NoData);
    `accum` is float64 drainage area in cell counts, NaN at NoData. Pass `filled` (the output of
    fill.fill_dem) to skip re-filling."""
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

    bk = be.make_backend(backend, log=log, spec=SPEC)
    try:
        arr = bk.allocate(_layout(ny, nx))
        arr["w"][:] = w32.ravel()
        arr["ok"][:] = valid.ravel().astype(np.uint8)
        arr["dir"][:] = NONE
        ones = valid.ravel().astype(np.uint32)      # 1 at valid cells, 0 at NoData
        arr["a0"][:] = ones
        arr["a1"][:] = ones
        p = TParams()
        p.nx, p.ny, p.r0, p.c0, p.wr, p.wc, p.pad, p.pad2 = nx, ny, 0, 0, ny, nx, 0, 0
        k = int(sweeps_per_batch) or max(16, nx + ny)
        nvalid = int(valid.sum())
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
                "converged": changed == 0, "valid": nvalid}
        if not info["converged"]:
            info["note"] = "accumulation did not converge in {} sweeps (raise max_sweeps)".format(
                cap)
        return direction, accum, info
    finally:
        bk.close()
