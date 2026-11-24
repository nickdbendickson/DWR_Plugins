"""BoannGrid depression-fill.

The default (Runner 3.87.0) is a single-pass Priority-Flood+eps (Barnes, Lehman and Mulla 2014)
in C++, D4 or D8, on one CPU core on every platform including a Mac with an Apple GPU. A priority
queue is serial work the GPU cannot speed up. It copies the relaxation's float32 arithmetic (the
same datum-shifted float32 ground, w = max(z, float32(w_low + eps)), the datum added back in
float64), so it settles on the relaxation's own fixed point: within a float32 step or two (the
relaxation's 1e-6 m dead band) and with an identical census, flow network and carve list
downstream (tests/test_metalterrain_pf.py, on a real Absaraka DEM). A float64 fill (the oracle
core/terrain.priority_flood's arithmetic) is NOT used: rounded back to float32 by the flow step it
made flats the relaxation does not have and moved flow directions and carve IDs (#507 review).

The older Planchon & Darboux (2002) whole-grid relaxation stays available as method="relax"
(Metal GPU or the CPU build of the same kernel). It is the cross-check, not the default: water
moves about one cell per sweep, so a 3.5 M-cell DEM took minutes instead of under a second.

    from nd_breach_runner.core.metalterrain import fill
    filled, info = fill.fill_dem(dem, eps=1e-5)                  # priority-flood
    filled, info = fill.fill_dem(dem, method="relax", backend="auto")

Outlets are the grid border and cells next to NoData (as in priority_flood); NoData cells act
as outlets for their valid neighbours, so a basin enclosed by NoData drains to that NoData
instead of filling without bound.
"""
import ctypes
import heapq
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
_METHODS = ("auto", "priority-flood", "relax")
_NB8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
_NB4 = ((-1, 0), (1, 0), (0, -1), (0, 1))

_PF_LIB = None
_FALLBACK_LOGGED = set()


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


def _log_once(log, key, msg):
    if log is None or key in _FALLBACK_LOGGED:
        return
    _FALLBACK_LOGGED.add(key)
    log(msg)


def _pf_library(log=None):
    """The BoannGrid CPU library, cached. Raises BackendError / OSError when it cannot be built
    or loaded (no compiler and no prebuilt library, or the shared library will not open)."""
    global _PF_LIB
    if _PF_LIB is None:
        path = be.build_cpu_library(SPEC, log)
        lib = ctypes.CDLL(path)
        lib.bg_priority_flood.restype = ctypes.c_longlong
        lib.bg_priority_flood.argtypes = [
            ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_uint8),
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_float),
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_float]
        _PF_LIB = lib
    return _PF_LIB


def _shifted(a, valid):
    """The relaxation's working inputs: datum (min valid elevation) and the datum-shifted ground
    rounded to float32 once. Both fills start from exactly these numbers."""
    datum = float(a[valid].min()) if valid.any() else 0.0
    zc = np.where(valid, a - datum, 0.0)                 # datum-shifted ground, float64
    return datum, zc.astype(np.float32)


def _priority_flood_py(z32, valid, outlet, eps, conn):
    """Pure-Python twin of bg_priority_flood: the same float32 arithmetic
    (w[j] = max(z[j], float32(w[i] + eps))), D4 or D8. Returns (w float32, unreached).
    w holds ground on NoData cells; the caller masks those to NaN."""
    ny, nx = valid.shape
    z = np.asarray(z32, dtype=np.float32)
    w = np.array(z, dtype=np.float32, copy=True)
    e = np.float32(eps)
    closed = np.array(~valid)
    nbs = _NB8 if conn == 8 else _NB4
    heap = []
    for r, c in zip(*np.nonzero(valid & outlet)):
        closed[r, c] = True
        heap.append((float(w[r, c]), int(r) * nx + int(c)))
    heapq.heapify(heap)
    while heap:
        _level, i = heapq.heappop(heap)
        r, c = divmod(i, nx)
        up = np.float32(w[r, c] + e)                     # float32 + float32 stays float32
        for dr, dc in nbs:
            rr, cc = r + dr, c + dc
            if rr < 0 or rr >= ny or cc < 0 or cc >= nx or closed[rr, cc]:
                continue
            closed[rr, cc] = True
            v = z[rr, cc] if z[rr, cc] > up else up
            w[rr, cc] = v
            heapq.heappush(heap, (float(v), rr * nx + cc))
    unreached = int(np.count_nonzero(valid & ~closed))
    return w, unreached


def _fill_pf_python(a, valid, outlet, eps, conn):
    """method='auto' fallback when the C++ library cannot be built or loaded: the same fill in
    pure Python (slow, same numbers)."""
    datum, z32 = _shifted(a, valid)
    w, unreached = _priority_flood_py(z32, valid, outlet, eps, conn)
    filled = np.where(valid, w.astype(np.float64) + datum, np.nan)
    info = {"backend": "python", "device": "Python, priority-flood (one core)",
            "cells": int(a.size), "sweeps": 0, "converged": True, "eps": float(eps),
            "conn": int(conn), "method": "priority-flood", "unreached": int(unreached)}
    return filled, info


def _fill_pf(a, valid, outlet, eps, conn, log):
    """C++ Priority-Flood+eps on the relaxation's own float32 datum-shifted inputs."""
    ny, nx = a.shape
    datum, z32 = _shifted(a, valid)
    z32 = np.ascontiguousarray(z32)
    ok = np.ascontiguousarray(valid, dtype=np.uint8)
    outb = np.ascontiguousarray(outlet, dtype=np.uint8)
    w = np.empty((ny, nx), dtype=np.float32)
    lib = _pf_library(log)
    fp = ctypes.POINTER(ctypes.c_float)
    n = lib.bg_priority_flood(
        z32.ctypes.data_as(fp),
        ok.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
        outb.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
        w.ctypes.data_as(fp),
        int(ny), int(nx), int(conn), float(np.float32(eps)))
    if n < 0:
        raise ValueError("bg_priority_flood rejected the grid")
    filled = np.where(valid, w.astype(np.float64) + datum, np.nan)   # as _fill_relax adds it back
    info = {"backend": "cpu", "device": "CPU, priority-flood (one core)",
            "cells": nx * ny, "sweeps": 0, "converged": True, "eps": float(eps),
            "conn": int(conn), "method": "priority-flood", "unreached": int(n)}
    return filled, info


def _fill_relax(a, eps, backend, sweeps_per_batch, max_sweeps, log, conn):
    """Today's Planchon & Darboux relaxation, unchanged. `backend` is 'auto', 'metal' or 'cpu'."""
    ny, nx = a.shape
    valid = np.isfinite(a) & (a != NODATA)
    # Datum shift: the kernel runs in float32, where eps (1e-5 m) is lost to rounding once
    # elevations reach ~170 m (ULP(300 m) ~ 3e-5 > eps), so flats on ND terrain (~300 m) would
    # fill FLAT instead of with the eps drainage ramp priority_flood lays down. Relaxing in
    # (z - datum), datum = min valid elevation, keeps working values small (relief, not altitude)
    # so eps stays representable; the datum is added back in float64 at the end.
    datum, z32 = _shifted(a, valid)
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
                "converged": changed == 0, "eps": float(eps), "conn": int(conn),
                "method": "relax",
                "unreached": 0 if changed == 0 else int(np.sum(valid & ~np.isfinite(filled)))}
        if not info["converged"]:
            info["note"] = "did not converge in {} sweeps (raise max_sweeps)".format(cap)
        return filled, info
    finally:
        bk.close()


def fill_dem(dem, eps=1e-5, backend="auto", sweeps_per_batch=0, max_sweeps=0, log=None, conn=8,
             method="auto"):
    """Depression-fill `dem` (2-D array; NaN or -9999 = NoData). Returns (filled, info).
    `filled` is float64 with NoData left as NaN. `eps` is the minimal drainage slope increment
    (m) so flats drain, matching priority_flood.

    `method` (Runner 3.87.0): 'auto' or 'priority-flood' is the C++ Priority-Flood+eps (the
    default). 'relax' is the Planchon & Darboux kernel. `backend` ('auto', 'metal' or 'cpu')
    applies only to 'relax'; the priority-flood always runs on one CPU core.

    'auto', when the C++ library cannot be built or loaded, uses the Metal relaxation if a
    Metal GPU is present, otherwise a pure-Python twin of the C++ fill (same numbers, slow).
    'priority-flood' raises instead of falling back.

    `conn` is how water may leave a cell: 8 (default, as priority_flood) lets it drain across
    a corner; 4 only to the four edge neighbours, which is how the ACC flood engine and D4 flow
    (flow.flow_accum's default) move water. A D8-filled surface still has D4 pits wherever the
    only way down is diagonal, so D4 flow on it had dead ends; flow.flow_accum refills those
    with conn=4."""
    if conn not in (4, 8):
        raise ValueError("conn must be 4 or 8")
    if method not in _METHODS:
        raise ValueError("method must be auto, priority-flood or relax")
    a = np.asarray(dem, dtype=np.float64)
    if a.ndim != 2:
        raise ValueError("dem must be 2-D")
    if method == "relax":
        return _fill_relax(a, eps, backend, sweeps_per_batch, max_sweeps, log, conn)
    valid = np.isfinite(a) & (a != NODATA)
    outlet = _outlets(valid, conn)
    try:
        return _fill_pf(a, valid, outlet, eps, conn, log)
    except (be.BackendError, OSError, AttributeError) as err:
        if method == "priority-flood":
            raise
        gpu, why = be.metal_available()
        if gpu:
            _log_once(log, "metal",
                      "priority-flood library unavailable ({}); using the Metal relaxation fill".format(err))
            return _fill_relax(a, eps, "metal", sweeps_per_batch, max_sweeps, log, conn)
        _log_once(log, "python",
                  "priority-flood library unavailable ({}); using the pure-Python priority-flood "
                  "(no Metal GPU: {})".format(err, why))
        return _fill_pf_python(a, valid, outlet, eps, conn)
