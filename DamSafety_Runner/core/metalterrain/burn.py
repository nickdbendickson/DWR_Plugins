"""BoannGrid channel burn + crossing reconnection (conditioner Stage 4 / 4c).

Two operations on a DEM, both pure numpy on top of the fill, flow and audit pieces:

  burn_channel(dem, path, depth)       lower the ground along an ordered channel path so it falls
                                       monotonically downstream (an enforced-fall invert burn);
                                       only ever lowers, never raises.

  reconnect_crossings(dem, ...)        find filled depressions that a real channel drains into -
                                       a channel blocked by a road/rail fill with no culvert - and
                                       carve a notch through the barrier so the channel reconnects,
                                       instead of leaving it dammed into a reservoir the fill
                                       invented. The "biggest true gap" the conditioner closes.

A crossing is detected by flow, not by shape alone: a depression whose cells carry at least
`min_accum_cells` of upstream drainage area is a blocked channel, not a puddle. Each carve is
recorded for the delta-log (its pour point, exit, barrier length and the drainage area it
reconnects), so nothing the conditioner does to the ground is silent.
"""
from collections import deque

import numpy as np

from . import fill as F
from . import flow as FL
from . import audit as A

NODATA = F.NODATA


def burn_channel(dem, path, depth=0.0):
    """Lower `dem` along `path` (an ordered list of (row, col), upstream -> downstream) so it never
    rises going downstream, burning `depth` m below the running minimum. Returns a new array; only
    the path cells change and they only ever drop."""
    out = np.asarray(dem, dtype=np.float64).copy()
    run = np.inf
    for r, c in path:
        run = min(run, float(out[r, c]))
        out[r, c] = run - depth
    return out


def _carve_path(dem, valid, depr, floor):
    """Multi-source BFS from the depression `depr` outward over valid cells to the nearest cell
    below `floor` (the downstream channel). Returns the path (list of (r,c)) from a depression-edge
    cell across the barrier to that exit, or None. 4-connected."""
    ny, nx = dem.shape
    parent = -np.ones((ny, nx), dtype=np.int64)
    q = deque()
    for r, c in np.argwhere(depr):
        parent[r, c] = r * nx + c          # a source points to itself
        q.append((r, c))
    exit_rc = None
    while q:
        r, c = q.popleft()
        if (not depr[r, c]) and valid[r, c] and dem[r, c] < floor:
            exit_rc = (r, c)
            break
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and valid[rr, cc] and parent[rr, cc] < 0:
                parent[rr, cc] = r * nx + c
                q.append((rr, cc))
    if exit_rc is None:
        return None
    path = []
    r, c = exit_rc
    while True:
        path.append((r, c))
        p = int(parent[r, c])
        pr, pc = divmod(p, nx)
        if (pr, pc) == (r, c):
            break
        r, c = pr, pc
    path.reverse()                         # depression edge -> ... -> exit
    return path


def reconnect_crossings(dem, min_accum_cells=20, cell_m=1.0, eps=1e-5, backend="auto",
                        filled=None, labels=None, items=None, accum=None, log=None):
    """Carve a notch through the barrier of every filled depression a real channel drains into (at
    least `min_accum_cells` of upstream area), so the channel reconnects. Returns (conditioned_dem,
    records). Each record: id, pour (r,c), exit (r,c), barrier_cells, accum_cells, floor_elev_m,
    spill_before_m. Pass precomputed fill / census / accum to avoid recomputing."""
    a = np.asarray(dem, dtype=np.float64)
    valid = np.isfinite(a) & (a != NODATA)
    if filled is None:
        filled, _ = F.fill_dem(a, eps=eps, backend=backend, log=log)
    if labels is None or items is None:
        labels, items = A.depression_census(a, filled, cell_m=cell_m)
    if accum is None:
        _dirn, accum, _ = FL.flow_accum(a, filled=filled, backend=backend)
    out = a.copy()
    records = []
    for it in items:
        m = labels == it["id"]
        amax = float(np.nanmax(np.where(m, accum, np.nan)))
        if amax < min_accum_cells:
            continue                       # a puddle, not a blocked channel
        floor = it["floor_elev_m"]
        path = _carve_path(a, valid, m, floor)
        if path is None or len(path) < 2:
            continue
        exit_rc = path[-1]
        ze = float(a[exit_rc])
        n = len(path)
        for k, (r, c) in enumerate(path):
            t = k / (n - 1)
            ramp = floor + t * (ze - floor)          # floor at the depression, exit level downstream
            if ramp < out[r, c]:
                out[r, c] = ramp                     # carve: only ever lower
        records.append({"id": it["id"], "pour": path[0], "exit": exit_rc,
                        "barrier_cells": n, "accum_cells": amax, "floor_elev_m": floor,
                        "spill_before_m": it["spill_elev_m"]})
    return out, records
