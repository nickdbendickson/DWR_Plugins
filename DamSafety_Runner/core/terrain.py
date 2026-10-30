"""Terrain helpers: where to put the breach inflow, which way the flood goes,
and how big the model domain has to be."""
import heapq
import math

import numpy as np


_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def priority_flood(dem):
    """Fill depressions so every cell drains to the grid edge (Barnes et al.
    2014, with a tiny increment so flats drain). NaN cells act as outlets."""
    a = np.asarray(dem, dtype=float)
    ny, nx = a.shape
    filled = a.copy()
    closed = np.zeros(a.shape, dtype=bool)
    heap = []
    nan = ~np.isfinite(a)
    edge = np.zeros(a.shape, dtype=bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    # cells next to NaN are outlets too
    if nan.any():
        from scipy.ndimage import binary_dilation
        edge |= binary_dilation(nan, structure=np.ones((3, 3), bool))
    edge &= ~nan
    closed[nan] = True
    for r, c in zip(*np.nonzero(edge)):
        heapq.heappush(heap, (filled[r, c], r, c))
        closed[r, c] = True
    eps = 1e-5
    while heap:
        z, r, c = heapq.heappop(heap)
        for dr, dc in _NB:
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not closed[rr, cc]:
                closed[rr, cc] = True
                if filled[rr, cc] <= z:
                    filled[rr, cc] = z + eps
                heapq.heappush(heap, (filled[rr, cc], rr, cc))
    return filled


def trace_downstream(grid, start_xy, max_length_m):
    """Steepest-descent path on the depression-filled DEM from start_xy until
    max_length_m or the grid edge. Returns a list of (x, y)."""
    filled = priority_flood(grid.masked())
    r, c = grid.rc(*start_xy)
    if not grid.inside(r, c):
        raise ValueError("the start point is outside the planning DEM")
    path = [grid.xy(r, c)]
    length = 0.0
    visited = {(r, c)}
    ny, nx = filled.shape
    while length < max_length_m:
        best, best_slope = None, 0.0
        for dr, dc in _NB:
            rr, cc = r + dr, c + dc
            if not (0 <= rr < ny and 0 <= cc < nx) or (rr, cc) in visited:
                continue
            if not np.isfinite(filled[rr, cc]):
                continue
            dist = math.hypot(dr, dc) * grid.dx
            s = (filled[r, c] - filled[rr, cc]) / dist
            if s > best_slope:
                best, best_slope = (rr, cc), s
        if best is None:
            break
        length += math.hypot(best[0] - r, best[1] - c) * grid.dx
        r, c = best
        visited.add(best)
        path.append(grid.xy(r, c))
        if r in (0, ny - 1) or c in (0, nx - 1):
            break
    return path, length


NO_DOWNSTREAM = "no cell on the downstream side of the crest qualified"
POOL_HALF_M = 1000.0          # half-width of the terrain read to find the pool (depression)


POOL_SHALLOW_M = 0.1          # v3.12.10: fill that counts in a shallow pool against the dam
POOL_TOUCH_M = 60.0           # v3.12.10: a pool this close to the dam line (or point) is its own
INFLOW_MAX_FROM_DAM_M = 150.0  # v3.12.10: the inflow search never goes further from the dam line
INFLOW_FAR = "CHECK THE INFLOW POINT"


def dam_distance(X, Y, dam_xy, crest_line=None):
    """Distance (m) of every (X, Y) from the dam line (the DWR crest line), else the dam point."""
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    a = None
    if crest_line is not None and len(crest_line) >= 2:
        a = np.asarray(crest_line, dtype=float).reshape(-1, 2)
    if a is None:
        return np.hypot(X - dam_xy[0], Y - dam_xy[1])
    best = np.full(X.shape, np.inf)
    for (x0, y0), (x1, y1) in zip(a[:-1], a[1:]):
        vx, vy = x1 - x0, y1 - y0
        L2 = vx * vx + vy * vy
        t = np.clip(((X - x0) * vx + (Y - y0) * vy) / L2, 0.0, 1.0) if L2 > 0 else 0.0
        best = np.minimum(best, np.hypot(x0 + t * vx - X, y0 + t * vy - Y))
    return best


def pool_at_dam(mask, grid, dam_xy, crest_line=None, touch_m=None):
    """v3.12.10: the parts (8-connected) of `mask` that reach within touch_m (default
    max(POOL_TOUCH_M, 3 cells)) of the dam line, else the dam point. A depression elsewhere
    (DWR-4031: a closed low 450 m ESE of the dam) is not this dam's reservoir."""
    from scipy.ndimage import label
    mask = np.asarray(mask, dtype=bool)
    out = np.zeros(mask.shape, dtype=bool)
    if not mask.any():
        return out
    lab, n = label(mask, structure=np.ones((3, 3), dtype=bool))
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    touch = max(POOL_TOUCH_M, 3.0 * grid.dx) if touch_m is None else float(touch_m)
    ids = np.unique(lab[mask & (dam_distance(X, Y, dam_xy, crest_line) <= touch)])
    ids = ids[ids > 0]
    return np.isin(lab, ids) if ids.size else out


def pool_mask(grid, dam_height_m, dam_xy=None, top_m=None, crest_line=None):
    """Cells of a closed depression on `grid` (the reservoir behind an embankment): filled
    (priority flood from the grid edges) minus ground deeper than max(0.5 m, 15 % of the dam
    height). A downstream toe or channel drains off the grid and is not in it. The grid must
    reach well past the reservoir (POOL_HALF_M) or the basin drains through the grid edge.

    v3.12.10: with `dam_xy` and `top_m`, a SHALLOW pool against the dam is added: more than
    POOL_SHALLOW_M of fill, ground between the top of dam and half the dam height below it,
    in a part that reaches the dam line (pool_at_dam). A hydro-flattened reservoir is a flat at
    its water surface with little fill behind the embankment (DWR-4031: 1.1 ft, against the
    3.1 ft the depth rule needs) and was not a pool at all."""
    z = grid.masked()
    fill = priority_flood(z) - z
    good = np.isfinite(fill)
    deep = good & (fill > max(0.5, 0.15 * float(dam_height_m)))
    if dam_xy is None or top_m is None:
        return deep
    h = float(dam_height_m)
    with np.errstate(invalid="ignore"):
        shallow = good & (fill > POOL_SHALLOW_M) & (z >= float(top_m) - 0.5 * h) & \
            (z <= float(top_m))
    return deep | pool_at_dam(shallow, grid, dam_xy, crest_line)


def _lower_side(grid, crest_line, n, dam_height_m, offsets=(20.0, 40.0, 60.0, 80.0)):
    """v3.12.10: +1 when the ground along `n` (unit normal of the crest line) is clearly lower
    than on the other side (median of samples 20-80 m off the line), -1 for the other side,
    0 when it is not clear (less than max(0.5 m, 10 % of the dam height) apart)."""
    a = np.asarray(crest_line, dtype=float).reshape(-1, 2)
    pts = list(a) + [0.5 * (p + q) for p, q in zip(a[:-1], a[1:])]
    z = grid.masked()
    side = {1: [], -1: []}
    for s in offsets:
        s = max(s, 1.5 * grid.dx)
        for p in pts:
            for sg in (1, -1):
                r, c = grid.rc(p[0] + sg * s * n[0], p[1] + sg * s * n[1])
                if grid.inside(r, c) and np.isfinite(z[r, c]):
                    side[sg].append(float(z[r, c]))
    if len(side[1]) < 3 or len(side[-1]) < 3:
        return 0
    diff = float(np.median(side[1]) - np.median(side[-1]))
    if abs(diff) < max(0.5, 0.1 * float(dam_height_m)):
        return 0
    return -1 if diff > 0 else 1


def downstream_direction(pool_grid, dam_xy, dam_height_m, crest_line=None, crest_xy=None,
                         near_m=600.0, top_m=None):
    """Unit vector from the crest towards downstream, and how it was found, or (None, why).

      1. the crest line's normal, the side away from the pool (else the clearly lower side,
         else the side the trace takes);
      2. no crest line: away from the pool (the centroid of the depression within near_m);
      3. no pool: the steepest-descent trace from the dam point;
      4. else the trace from place_from_crest's crest (`crest_xy`).
    The trace alone is the weakest: on a dry or hydro-flattened basin it descends into the
    reservoir (Absaraka DWR-2801), which is why the pool comes first.
    v3.12.10: the pool is only the depression that reaches the dam line (pool_at_dam), with the
    shallow pool pool_mask finds when `top_m` is given. Before, any depression within near_m
    counted: on DWR-4031 the hydro-flattened reservoir was not a pool and a closed low 450 m
    ESE was, which turned the downstream side round."""
    def unit(v):
        n = math.hypot(v[0], v[1])
        return (v[0] / n, v[1] / n) if n > 1e-9 else None

    away = None
    if pool_grid is not None:
        pool = pool_at_dam(pool_mask(pool_grid, dam_height_m, dam_xy, top_m, crest_line),
                           pool_grid, dam_xy, crest_line)
        X, Y = np.meshgrid(pool_grid.x_centers(), pool_grid.y_centers())
        near = pool & (np.hypot(X - dam_xy[0], Y - dam_xy[1]) <= near_m)
        if near.sum() >= 3:
            away = unit((dam_xy[0] - float(X[near].mean()), dam_xy[1] - float(Y[near].mean())))

    def trace_dir(start):
        if pool_grid is None or start is None:
            return None
        try:
            path, _l = trace_downstream(pool_grid, start, 300.0)
        except Exception:                           # noqa: BLE001
            return None
        return unit(flow_direction(path)) if len(path) >= 2 else None

    if crest_line is not None and len(crest_line) >= 2:
        a, b = np.asarray(crest_line[0], dtype=float), np.asarray(crest_line[-1], dtype=float)
        t = unit((b[0] - a[0], b[1] - a[1]))
        if t is not None:
            n = (-t[1], t[0])
            ref, how = away, "crest-line normal away from the pool"
            if ref is None and pool_grid is not None:
                lower = _lower_side(pool_grid, crest_line, n, dam_height_m)
                if lower:
                    ref = (lower * n[0], lower * n[1])
                    how = "crest-line normal on the lower side (no pool against the dam)"
            if ref is None:
                ref, how = trace_dir(dam_xy), "crest-line normal on the downstream trace's side"
            if ref is not None:
                if n[0] * ref[0] + n[1] * ref[1] < 0:
                    n = (-n[0], -n[1])
                return n, how
    if away is not None:
        return away, "away from the pool behind the dam"
    v = trace_dir(dam_xy)
    if v is not None:
        return v, "downstream trace from the dam point"
    v = trace_dir(crest_xy)
    if v is not None:
        return v, "downstream trace from the crest (highest ground near the dam point)"
    return None, "no crest line, no pool and no downstream trace"


def find_inflow_point(grid, dam_xy, dam_height_m, top_m, radius_m=None, downstream=None,
                      pool=None, crest_line=None, max_from_dam_m=None):
    """The breach inflow goes in just downstream of the embankment. Look for
    the lowest DEM cell within the usual radius that is clearly below the reservoir (at least
    half the dam height below the top of dam); ties (a flat or hydro-flattened floor) go to the
    nearest. A wide embankment can put the toe outside that ring, so a second search goes out
    to 400 m and takes the nearest qualifying cell, not the lowest cell in the circle (a far
    valley is not the toe).

    `downstream` (unit vector, see downstream_direction) keeps only cells on the downstream side
    of the crest, and `pool` (a Grid of 0/1, see pool_mask) drops cells in the reservoir: the
    lowest cell near a dry dam can be the reservoir floor (Absaraka DWR-2801). When no cell
    passes those, the unconstrained pick is returned and the note starts with NO_DOWNSTREAM.

    v3.12.10: no pick is further than `max_from_dam_m` (INFLOW_MAX_FROM_DAM_M) from the dam
    line (`crest_line`), else the dam point. Before, the wider search had no limit and took a
    farmyard draw 393 m from DWR-4031. When the only low-enough ground is further out, the
    lowest ground within the limit (on the downstream side) is used and the note starts with
    INFLOW_FAR.

    Returns ((x, y), note) or (None, reason)."""
    if radius_m is None:
        radius_m = max(90.0, 8.0 * dam_height_m)
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    d = np.hypot(X - dam_xy[0], Y - dam_xy[1])
    z = grid.masked()
    need = top_m - 0.5 * dam_height_m
    ok = np.ones(z.shape, dtype=bool)
    if downstream is not None:
        ok &= ((X - dam_xy[0]) * downstream[0] + (Y - dam_xy[1]) * downstream[1]) > 0.0
    if pool is not None:
        pr = np.floor((pool.y0 - Y) / pool.dx).astype(int)
        pc = np.floor((X - pool.x0) / pool.dx).astype(int)
        inside = (pr >= 0) & (pr < pool.ny) & (pc >= 0) & (pc < pool.nx)
        in_pool = np.zeros(z.shape, dtype=bool)
        in_pool[inside] = pool.data[pr[inside], pc[inside]] > 0.5
        ok &= ~in_pool
    constrained = downstream is not None or pool is not None
    cap = INFLOW_MAX_FROM_DAM_M if max_from_dam_m is None else float(max_from_dam_m)
    dline = dam_distance(X, Y, dam_xy, crest_line)
    lim = (dline, cap, "dam line" if crest_line is not None and len(crest_line) >= 2
           else "dam point")
    pt, note = _pick_inflow(X, Y, d, z, need, top_m, radius_m, ok, lim)
    if pt is None and constrained and not note.startswith(INFLOW_FAR):
        pt, note2 = _pick_inflow(X, Y, d, z, need, top_m, radius_m, np.ones(z.shape, dtype=bool),
                                 lim)
        if pt is not None:
            return pt, "{}; used {} (check it)".format(NO_DOWNSTREAM, note2)
    return pt, note


INFLOW_TOE_BAND_M = (5.0, 15.0)  # v3.12.10 (Nick: "use a 5-15 m toe strip"): the inflow is the
                                 # lowest ground this far downstream of the dam line, along its
                                 # full length
INFLOW_TOE_CELL_M = 1.0          # ... read at this cell (or the DEM's own, when coarser)
INFLOW_TOE_DROP = 0.75           # the strip's pick is the toe only when its ground is this
                                 # fraction of the dam height below the top of dam; else it is
                                 # the dam's face (DWR-2801: the 5-15 m strip is all face,
                                 # 13-15 ft below the 27.5 ft dam's top, and its lowest cell was
                                 # a pad at the south end) and the strip widens
INFLOW_TOE_WIDEN_M = 10.0        # ... its outer edge moving out by this, up to 60 m more and
                                 # never past the cap
INFLOW_TOE_SIDE_MIN = 0.25       # a segment of the dam line whose normal is closer than this
                                 # (cosine) to square with the downstream side has no known side
NO_TOE = "no toe band"


def _line_foot(X, Y, line):
    """Distance of every (X, Y) from the polyline `line`, the nearest point on it (fx, fy) and
    the index of the segment it is on."""
    a = np.asarray(line, dtype=float).reshape(-1, 2)
    best = np.full(X.shape, np.inf)
    fx = np.zeros(X.shape)
    fy = np.zeros(X.shape)
    seg = np.zeros(X.shape, dtype=int)
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(a[:-1], a[1:])):
        vx, vy = x1 - x0, y1 - y0
        L2 = vx * vx + vy * vy
        t = np.clip(((X - x0) * vx + (Y - y0) * vy) / L2, 0.0, 1.0) if L2 > 0 else \
            np.zeros(X.shape)
        px, py = x0 + t * vx, y0 + t * vy
        dd = np.hypot(px - X, py - Y)
        m = dd < best
        best = np.where(m, dd, best)
        fx = np.where(m, px, fx)
        fy = np.where(m, py, fy)
        seg = np.where(m, i, seg)
    return best, fx, fy, seg


def _segment_normals(line, downstream):
    """Unit normal of every segment of `line`, turned to the downstream side; (0, 0) for a
    segment of no length or one that runs with the flow (|cos| < INFLOW_TOE_SIDE_MIN)."""
    a = np.asarray(line, dtype=float).reshape(-1, 2)
    v = np.diff(a, axis=0)
    L = np.hypot(v[:, 0], v[:, 1])
    n = np.zeros_like(v)
    ok = L > 1e-9
    n[ok, 0] = v[ok, 1] / L[ok]
    n[ok, 1] = -v[ok, 0] / L[ok]
    c = n[:, 0] * downstream[0] + n[:, 1] * downstream[1]
    n[c < 0] *= -1.0
    n[np.abs(c) < INFLOW_TOE_SIDE_MIN] = 0.0
    return n


def toe_band_mask(grid, crest_line, downstream, band_m=None):
    """v3.12.10: cells of `grid` in the toe band: band_m[0]..band_m[1] m from the dam line, on
    the downstream side of the segment they are nearest (the offset from the nearest point of
    the line within 60 degrees of that segment's normal turned downstream), so the band runs
    along the full crest length, follows a bent line, and does not wrap round the ends of the
    line onto the upstream side. Returns (mask, distance from the line)."""
    lo, hi = INFLOW_TOE_BAND_M if band_m is None else (float(band_m[0]), float(band_m[1]))
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    dl, fx, fy, seg = _line_foot(X, Y, crest_line)
    n = _segment_normals(crest_line, downstream)
    with np.errstate(invalid="ignore", divide="ignore"):
        side = ((X - fx) * n[seg, 0] + (Y - fy) * n[seg, 1]) / np.where(dl > 0, dl, 1.0)
    return (dl >= lo) & (dl <= hi) & (side >= 0.5), dl


def toe_band_inflow(grid, crest_line, downstream, dam_height_m, top_m, band_m=None,
                    pool=None, max_from_dam_m=None, dam_xy=None):
    """v3.12.10 (Nick, DWR-4031: "the inflow point at the dam toe at the lowest elevation";
    "use a 5-15 m toe strip"): the lowest cell of the toe band (toe_band_mask: 5-15 m
    downstream of the dam line along its full length) on `grid`, which should be the fine DEM
    (1 m). Ties (a flat) go to the cell nearest the dam line, then the dam point. Cells not
    below the top of dam do not count, nor cells in `pool` (a Grid of 0/1, the reservoir found
    at the search cell) more than half a pool cell's diagonal from the line (nearer, a coarse
    pool cell can straddle the line). When the band's lowest ground is not INFLOW_TOE_DROP of
    the dam height below the top of dam, it is the dam's face, and the outer edge moves out
    INFLOW_TOE_WIDEN_M at a time (up to 60 m more, never past max_from_dam_m): the first band
    that reaches it is used, else the first that reaches half the dam height, else the first.
    Returns ((x, y), note) or (None, reason starting with NO_TOE): the caller falls back to
    find_inflow_point."""
    if crest_line is None or len(crest_line) < 2:
        return None, "{}: no dam line".format(NO_TOE)
    if downstream is None:
        return None, "{}: the downstream side of the dam line is not known".format(NO_TOE)
    lo, hi = INFLOW_TOE_BAND_M if band_m is None else (float(band_m[0]), float(band_m[1]))
    cap = INFLOW_MAX_FROM_DAM_M if max_from_dam_m is None else float(max_from_dam_m)
    hi = min(hi, cap)
    if hi <= lo:
        return None, "{}: the band {:.0f}-{:.0f} m is empty".format(NO_TOE, lo, hi)
    a = np.asarray(crest_line, dtype=float).reshape(-1, 2)
    if not _segment_normals(a, downstream).any():
        return None, ("{}: the dam line runs with the flow, its downstream side is not "
                      "known".format(NO_TOE))
    z = grid.masked()
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    ok = np.isfinite(z) & (z < float(top_m))
    if pool is not None:
        pr = np.floor((pool.y0 - Y) / pool.dx).astype(int)
        pc = np.floor((X - pool.x0) / pool.dx).astype(int)
        inside = (pr >= 0) & (pr < pool.ny) & (pc >= 0) & (pc < pool.nx)
        in_pool = np.zeros(z.shape, dtype=bool)
        in_pool[inside] = pool.data[pr[inside], pc[inside]] > 0.5
        dl0 = _line_foot(X, Y, a)[0]
        ok &= ~(in_pool & (dl0 > 0.7072 * pool.dx))
    ref = dam_xy if dam_xy is not None else tuple(a.mean(axis=0))
    dref = np.hypot(X - ref[0], Y - ref[1])
    need = float(top_m) - INFLOW_TOE_DROP * float(dam_height_m)
    half = float(top_m) - 0.5 * float(dam_height_m)
    widest = min(cap, hi + 60.0)
    first = first_half = pick = None
    outer = hi
    while True:
        band, dl = toe_band_mask(grid, a, downstream, (lo, outer))
        sel = band & ok
        if sel.any():
            idx = np.flatnonzero(sel)
            k = idx[np.lexsort((dref.ravel()[idx], np.round(dl.ravel()[idx], 1),
                                np.round(z.ravel()[idx], 2)))[0]]
            r, c = np.unravel_index(k, z.shape)
            cand = (r, c, outer)
            if first is None:
                first = cand
            if first_half is None and z[r, c] <= half:
                first_half = cand
            if z[r, c] <= need:
                pick = cand
                break
        if outer >= widest - 1e-6:
            pick = first_half if first_half is not None else first
            break
        outer = min(widest, outer + INFLOW_TOE_WIDEN_M)
    if pick is None:
        return None, ("{}: no DEM cell below the top of dam (outside the pool) {:.0f}-{:.0f} m "
                      "downstream of the dam line".format(NO_TOE, lo, widest))
    r, c, outer = pick
    dl = _line_foot(X[r:r + 1, c:c + 1], Y[r:r + 1, c:c + 1], a)[0][0, 0]
    drop = (float(top_m) - z[r, c]) / 0.3048
    note = ("lowest ground at the downstream toe: {:.0f}-{:.0f} m downstream of the dam line "
            "along its full length ({:g} m DEM), {:.0f} m from the line, {:.1f} ft below the "
            "top of dam".format(lo, outer, grid.dx, dl, drop))
    if outer > hi + 1e-6:
        note += (" (band widened from {:.0f} m: the ground nearer is the dam's face, less than "
                 "{:.0f} % of the dam height below its top)".format(hi, 100 * INFLOW_TOE_DROP))
    if z[r, c] > half:
        note += "; less than half the dam height below the top of dam"
    elif z[r, c] > need:
        note += "; less than {:.0f} % of the dam height below the top of dam".format(
            100 * INFLOW_TOE_DROP)
    return (float(X[r, c]), float(Y[r, c])), note


def _pick_inflow(X, Y, d, z, need, top_m, radius_m, ok, lim=None):
    if lim is None:
        lim = (d, np.inf, "dam point")
    dline, cap, what = lim
    radii = [radius_m]
    wider = max(radius_m, 400.0)
    if wider > radius_m + 1.0:
        radii.append(wider)
    last_drop, last_r = 0.0, radius_m
    for rad in radii:
        near = (d <= rad) & np.isfinite(z) & ok
        if not near.any():
            last_r = rad
            continue
        last_drop = (top_m - float(np.nanmin(z[near]))) / 0.3048
        last_r = rad
        sel = near & (z <= need)
        if not sel.any():
            continue
        if not (sel & (dline <= cap)).any():
            # v3.12.10: only ground beyond the limit is low enough: not this dam's toe
            far = float(dline[sel].min())
            cand = (dline <= cap) & np.isfinite(z) & ok & (z < top_m)
            if not cand.any():
                return None, ("{}: the nearest ground at least half the dam height below the "
                              "top of dam is {:.0f} m from the {} (limit {:.0f} m) and nothing "
                              "nearer is below the top of dam; give the inflow point below the "
                              "dam".format(INFLOW_FAR, far, what, cap))
            idx = np.flatnonzero(cand)
            k = idx[np.lexsort((dline.ravel()[idx], np.round(z.ravel()[idx], 3)))[0]]
            r, c = np.unravel_index(k, z.shape)
            return (float(X[r, c]), float(Y[r, c])), (
                "{}: the nearest ground at least half the dam height below the top of dam is "
                "{:.0f} m from the {} (limit {:.0f} m); used the lowest ground within {:.0f} m, "
                "{:.0f} m from the {}, {:.1f} ft below the top of dam".format(
                    INFLOW_FAR, far, what, cap, cap, dline[r, c], what,
                    (top_m - z[r, c]) / 0.3048))
        sel &= dline <= cap
        idx = np.flatnonzero(sel)
        if rad > radius_m + 1.0:
            # the wider search: the NEAREST low-enough cell (the lowest of a 400 m circle may
            # be a far valley, not the toe); ties to the lower
            k = idx[np.lexsort((z.ravel()[idx], d.ravel()[idx]))[0]]
        else:
            # the usual radius: the LOWEST cell, ties to the nearest. The nearest low-enough
            # cell can be the upstream face of the embankment or the floor of a dry reservoir
            # (found by the synthetic-valley test: (-25, 5), upstream of the dam at x = 0)
            k = idx[np.lexsort((d.ravel()[idx], np.round(z.ravel()[idx], 3)))[0]]
        r, c = np.unravel_index(k, z.shape)
        drop = (top_m - z[r, c]) / 0.3048
        if rad > radius_m + 1.0:
            note = ("toe is outside the usual {:.0f} m search; nearest ground {:.0f} m "
                    "from the dam point, {:.1f} ft below the top of dam"
                    .format(radius_m, d[r, c], drop))
        else:
            note = ("lowest ground within {:.0f} m of the dam point, {:.1f} ft below "
                    "the top of dam".format(rad, drop))
        return (float(X[r, c]), float(Y[r, c])), note
    return None, ("the lowest ground within {:.0f} m of the dam point is only {:.1f} ft "
                  "below the top of dam; give the inflow point below the dam"
                  .format(last_r, last_drop))


def _side(xy, origin, downstream):
    """Signed distance of xy past `origin` along the unit vector `downstream` (> 0: downstream)."""
    return (xy[0] - origin[0]) * downstream[0] + (xy[1] - origin[1]) * downstream[1]


def _downstream_trace(grid, crest_xy, radius_m, downstream):
    """The steepest-descent trace from the crest, keeping only cells on the downstream side.
    v3.10.18: on a low embankment between two flat (hydro-flattened) water surfaces the trace
    from the crest can step into the reservoir (DWR-3161: 0.8 ft lower on the pool side,
    40 m south of a dam whose downstream is north). Then the trace is started again a few
    cells out on the downstream side. Returns (cells [(x, y)], start offset m)."""
    try:
        path, _length = trace_downstream(grid, crest_xy, radius_m)
    except Exception:                               # noqa: BLE001
        path = []
    if downstream is None:
        return path[1:], 0.0
    keep = [p for p in path[1:] if _side(p, crest_xy, downstream) > 0.5 * grid.dx]
    if keep:
        return keep, 0.0
    for k in (1, 2, 3, 5, 8):
        off = k * grid.dx
        if off > radius_m:
            break
        start = (crest_xy[0] + downstream[0] * off, crest_xy[1] + downstream[1] * off)
        r, c = grid.rc(*start)
        if not grid.inside(r, c):
            break
        try:
            path, _length = trace_downstream(grid, start, radius_m)
        except Exception:                           # noqa: BLE001
            continue
        # the cells the trace reaches from there (not the start), still downstream
        keep = [p for p in path[1:] if _side(p, crest_xy, downstream) > 0.5 * grid.dx]
        if keep:
            return keep, off
    return [], 0.0


def streambed_inflow(grid, crest_xy, dam_height_m, top_m, radius_m=150.0, downstream=None):
    """Lowest cell on the downstream trace, not the nearest low ground.

    Follow steepest descent from the crest. Prefer the first cell at least half
    the dam height below the crest. If none, take the lowest cell on that path
    within radius_m. A cell off the trace is not the streambed. `downstream` (unit vector,
    v3.10.18) keeps only cells on the downstream side of the crest (_downstream_trace).
    Returns ((x, y), note) or (None, reason)."""
    cells, off = _downstream_trace(grid, crest_xy, radius_m, downstream)
    if not cells:
        return None, ("no cell of the downstream trace on the downstream side of the crest"
                      if downstream is not None else "downstream trace did not leave the crest")
    z = grid.masked()
    need = top_m - 0.5 * dam_height_m
    best = None
    lowest = None
    for x, y in cells:
        r, c = grid.rc(x, y)
        if not grid.inside(r, c) or not np.isfinite(z[r, c]):
            continue
        drop = top_m - float(z[r, c])
        dist = math.hypot(x - crest_xy[0], y - crest_xy[1])
        if lowest is None or drop > lowest[0]:
            lowest = (drop, dist, x, y)
        if best is None and drop >= (top_m - need):
            best = (drop, dist, x, y)
    pick = best or lowest
    if pick is None:
        return None, "no streambed cell on the downstream trace"
    drop_ft = pick[0] / 0.3048
    how = "first streambed cell at least half the dam height below the crest" if best else (
        "lowest streambed cell on the downstream trace")
    if off > 0:
        how += " (traced from {:.0f} m out on the downstream side: the trace from the crest " \
               "went into the reservoir)".format(off)
    # v3.10.16: top_m is the top of dam (place_from_crest), so the drop is below the top of dam
    return (pick[2], pick[3]), ("{}; {:.0f} m downstream, {:.1f} ft below the top of dam"
                                 .format(how, pick[1], drop_ft))


CREST_BAND_FT = 3.0     # v3.10.16: the crest is ground within this (or 10% of the dam height) of
                        # the top of dam; higher ground nearby is a hill, not the embankment


def crest_band_m(dam_height_m):
    """Half-width (m) of the elevation band around the top of dam that counts as the crest."""
    return max(CREST_BAND_FT * 0.3048, 0.1 * float(dam_height_m or 0.0))


def embankment_crest(grid, dam_xy, dam_height_m, top_m, radius_m=400.0):
    """v3.10.16: the crest of THIS dam near a label point.

    The ground within `crest_band_m` of the top of dam is the embankment crest: the given point
    is kept when it is on it, else the nearest such cell is the crest. Without such ground, the
    highest ground NOT above the band. Ground well above the top of dam (a hill within the
    search radius) is never the crest: before 3.10.16 the highest ground in the circle was,
    and the breach inflow was put on a hillside (DWR-3161: a 1937.8 ft hill 400 m away from a
    point on a 1844.4 ft dam).
    Returns (crest_xy, crest_z_m, note) or (None, None, reason)."""
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    z = grid.masked()
    d = np.hypot(X - dam_xy[0], Y - dam_xy[1])
    near = (d <= radius_m) & np.isfinite(z)
    if not near.any():
        return None, None, "no DEM within {:.0f} m of the dam point".format(radius_m)
    band = crest_band_m(dam_height_m)
    on = near & (np.abs(z - top_m) <= band)
    if on.any():
        dd = np.where(on, d, np.inf)
        rr, cc = np.unravel_index(int(np.argmin(dd)), dd.shape)
        if dd[rr, cc] <= max(grid.dx, 1e-9) * 1.5:
            return (float(dam_xy[0]), float(dam_xy[1])), float(z[rr, cc]), \
                "the dam point is on the crest"
        return (float(X[rr, cc]), float(Y[rr, cc])), float(z[rr, cc]), \
            "nearest ground within {:.1f} ft of the top of dam".format(band / 0.3048)
    low = near & (z <= top_m + band)
    if not low.any():
        return None, None, ("all ground within {:.0f} m of the dam point is more than {:.1f} ft "
                            "above the top of dam: the point is not at this dam (or the top of "
                            "dam is in another datum)".format(radius_m, band / 0.3048))
    k = np.lexsort((d.ravel(), -np.where(low, z, -np.inf).ravel()))[0]
    rr, cc = np.unravel_index(int(k), z.shape)
    return (float(X[rr, cc]), float(Y[rr, cc])), float(z[rr, cc]), \
        "highest ground not above the top of dam (no ground within {:.1f} ft of it)".format(
            band / 0.3048)


def place_from_crest(grid, dam_xy, dam_height_m, top_m, radius_m=400.0, downstream=None):
    """A label point is not always the embankment. Find the crest of this dam
    (`embankment_crest`: ground at the top of dam, never a higher hill), then the nearest toe
    at least half the dam height below the TOP OF DAM.
    v3.10.18: `downstream` (unit vector from downstream_direction) keeps every pick on the
    downstream side of the crest; before, this fallback ignored it and put DWR-3161's inflow
    40 m into the reservoir while the flow trace ran the other way.
    Returns (inflow_xy, crest_xy, note) or (None, crest_xy or None, reason)."""
    crest, crest_z, cnote = embankment_crest(grid, dam_xy, dam_height_m, top_m, radius_m)
    if crest is None:
        return None, None, cnote
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    z = grid.masked()
    moved = math.hypot(crest[0] - dam_xy[0], crest[1] - dam_xy[1])
    inflow, why = streambed_inflow(grid, crest, dam_height_m, top_m, radius_m, downstream)
    if inflow is not None:
        r, c = grid.rc(*inflow)
        if not (grid.inside(r, c) and np.isfinite(z[r, c]) and z[r, c] < top_m):
            inflow, why = None, "the downstream trace did not go below the top of dam"
    if inflow is None:
        inflow, why = find_inflow_point(grid, crest, dam_height_m, top_m, radius_m,
                                        downstream=downstream)
        if inflow is not None and why.startswith(NO_DOWNSTREAM):
            inflow = None                    # only the upstream side qualified: not used
    if inflow is None:
        # Half the height is the normal toe. A label point can miss it by a foot on a
        # coarse DEM, so the fallback accepts 8 ft or 40 percent of the height.
        need = min(top_m - 8.0 * 0.3048, top_m - 0.4 * dam_height_m)
        d2 = np.hypot(X - crest[0], Y - crest[1])
        sel = (d2 <= max(radius_m, 800.0)) & np.isfinite(z) & (z <= need)
        if downstream is not None:
            sel &= _side((X, Y), crest, downstream) > 0.0
        if not sel.any():
            return None, crest, why
        dd = np.where(sel, d2, np.inf)
        r, c = np.unravel_index(np.argmin(dd), dd.shape)
        drop = (top_m - float(z[r, c])) / 0.3048
        inflow = (float(X[r, c]), float(Y[r, c]))
        why = ("toe is {:.0f} m from the crest, {:.1f} ft below the top of dam "
               "(half-height search missed)".format(d2[r, c], drop))
    if moved < 1e-6:
        return inflow, None, "{}; {}".format(cnote, why)
    return inflow, crest, ("crest moved {:.0f} m from the given point ({}); {}"
                           .format(moved, cnote, why))


SIDE_PROBLEM = "CHECK THE DAM POINT"


def inflow_side_check(dam_xy, inflow_xy, path, downstream=None, crest_line=None,
                      max_from_dam_m=None):
    """v3.10.18: is the breach inflow on the downstream side, as the flow trace from it says?
    Flags (a) a trace from the inflow that runs back over the dam point, (b) an inflow
    behind the dam relative to the trace's own direction, (c) an inflow on the other side of
    the crest from `downstream`.
    v3.12.10, checks that do not lean on how the point was picked (on DWR-4031 (a)-(c) passed a
    point 393 m away whose trace ran against the downstream side, cos -0.80):
    (d) the trace runs against `downstream` (cosine < 0), (e) the inflow is more than
    `max_from_dam_m` (INFLOW_MAX_FROM_DAM_M) from the dam line (`crest_line`) or point: 'far'
    (a note, not one of the side 'problems'), (f) with a crest line and no (d): the trace runs back towards the dam line (against the
    line's normal on the inflow's side). Returns {'ok', 'problems', ...}."""
    d0 = math.hypot(inflow_xy[0] - dam_xy[0], inflow_xy[1] - dam_xy[1])
    out = {"ok": True, "problems": [], "inflow_from_dam_m": round(d0, 1)}
    has_line = crest_line is not None and len(crest_line) >= 2
    cap = INFLOW_MAX_FROM_DAM_M if max_from_dam_m is None else float(max_from_dam_m)
    dl = float(dam_distance(np.array([inflow_xy[0]]), np.array([inflow_xy[1]]), dam_xy,
                            crest_line if has_line else None)[0])
    if has_line:
        out["inflow_from_dam_line_m"] = round(dl, 1)
    if dl > cap:
        # a distance, not a side disagreement: reported apart from 'problems' (and 'ok')
        out["far"] = ("the inflow point is {:.0f} m from the {}, more than {:.0f} m: it is "
                      "probably not this dam's toe".format(
                          dl, "dam line" if has_line else "dam point", cap))
    fd = None
    if path is not None and len(path) >= 2:
        dmin = min(math.hypot(p[0] - dam_xy[0], p[1] - dam_xy[1]) for p in path[1:])
        out["trace_min_to_dam_m"] = round(dmin, 1)
        if d0 > 1.0 and dmin < 0.5 * d0:
            out["problems"].append("the flow trace from the inflow point runs back over the dam "
                                   "point ({:.0f} m from it)".format(dmin))
        fd = flow_direction(path)
        out["trace_direction"] = [round(fd[0], 3), round(fd[1], 3)]
        nf = math.hypot(fd[0], fd[1])
        fd = (fd[0] / nf, fd[1] / nf) if nf > 1e-9 else None
        if fd is not None and d0 > 1.0 and _side(inflow_xy, dam_xy, fd) < 0:
            out["problems"].append("the inflow point is behind the dam relative to the flow "
                                   "trace's direction ({:.2f}, {:.2f})".format(*fd))
    if downstream is not None:
        out["downstream"] = [round(downstream[0], 3), round(downstream[1], 3)]
        if d0 > 1.0 and _side(inflow_xy, dam_xy, downstream) < 0:
            out["problems"].append("the inflow point is on the upstream side of the crest "
                                   "(downstream is ({:.2f}, {:.2f}))".format(*downstream))
    against = False
    if downstream is not None and fd is not None:
        cos = fd[0] * downstream[0] + fd[1] * downstream[1]
        out["trace_vs_downstream"] = round(cos, 2)
        if cos < 0:
            against = True
            out["problems"].append("the flow trace from the inflow point runs against the "
                                   "downstream side (cosine {:.2f}): the downstream side or the "
                                   "inflow point is wrong".format(cos))
    if has_line and fd is not None:
        a = np.asarray(crest_line, dtype=float).reshape(-1, 2)
        tx, ty = a[-1, 0] - a[0, 0], a[-1, 1] - a[0, 1]
        nt = math.hypot(tx, ty)
        if nt > 1e-9:
            n = (-ty / nt, tx / nt)
            mid = (0.5 * (a[0, 0] + a[-1, 0]), 0.5 * (a[0, 1] + a[-1, 1]))
            if _side(inflow_xy, mid, n) < 0:
                n = (-n[0], -n[1])
            cos = fd[0] * n[0] + fd[1] * n[1]
            out["trace_vs_dam_line_normal"] = round(cos, 2)
            if cos < 0 and not against:
                out["problems"].append("the flow trace from the inflow point runs back towards "
                                       "the dam line (cosine {:.2f} with the line's normal on "
                                       "the inflow's side)".format(cos))
    out["ok"] = not out["problems"]
    return out


def nearest_line_distance(xy, lines):
    """Shortest distance (m) from xy to any polyline in `lines` ([[(x, y), ...], ...])."""
    best = None
    px, py = float(xy[0]), float(xy[1])
    for ln in lines or []:
        a = np.asarray(ln, dtype=float).reshape(-1, 2)
        if len(a) == 1:
            d = math.hypot(a[0, 0] - px, a[0, 1] - py)
        else:
            p0, p1 = a[:-1], a[1:]
            v = p1 - p0
            L2 = (v ** 2).sum(axis=1)
            t = np.clip(((px - p0[:, 0]) * v[:, 0] + (py - p0[:, 1]) * v[:, 1]) /
                        np.where(L2 > 0, L2, 1.0), 0.0, 1.0)
            d = float(np.hypot(p0[:, 0] + t * v[:, 0] - px, p0[:, 1] + t * v[:, 1] - py).min())
        best = d if best is None else min(best, d)
    return best


def flow_direction(path):
    """Unit vector of the first ~200 m of a traced path."""
    if len(path) < 2:
        return (1.0, 0.0)
    x0, y0 = path[0]
    acc = 0.0
    x1, y1 = path[-1]
    for i in range(1, len(path)):
        acc += math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1])
        if acc >= 200.0:
            x1, y1 = path[i]
            break
    dx, dy = x1 - x0, y1 - y0
    n = math.hypot(dx, dy) or 1.0
    return (dx / n, dy / n)


def path_bounds(path, buffer_m, snap=1.0):
    xs = [p[0] for p in path]
    ys = [p[1] for p in path]
    b = [min(xs) - buffer_m, min(ys) - buffer_m, max(xs) + buffer_m, max(ys) + buffer_m]
    return tuple(math.floor(v / snap) * snap if i < 2 else math.ceil(v / snap) * snap
                 for i, v in enumerate(b))


# ----------------------------------------------------------------------------
# Polygons from masks (for the ANUGA mesh)
# ----------------------------------------------------------------------------
def douglas_peucker(points, tol):
    pts = np.asarray(points, dtype=float)
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = pts[i], pts[j]
        ab = b - a
        n = np.hypot(*ab)
        seg = pts[i + 1:j]
        if n == 0:
            d = np.hypot(*(seg - a).T)
        else:
            d = np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / n
        k = int(np.argmax(d))
        if d[k] > tol:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return pts[keep]


def _have_contourpy():
    try:
        import contourpy  # noqa: F401
        return True
    except ImportError:
        return False


def _outline_contourpy(grid, mask):
    import contourpy
    m = np.pad(mask.astype(float), 1)
    xs = np.concatenate([[grid.x_centers()[0] - grid.dx], grid.x_centers(),
                         [grid.x_centers()[-1] + grid.dx]])
    ys = np.concatenate([[grid.y_centers()[0] + grid.dx], grid.y_centers(),
                         [grid.y_centers()[-1] - grid.dx]])
    gen = contourpy.contour_generator(xs, ys, m)
    lines = gen.lines(0.5)
    if not lines:
        raise ValueError("empty mask")
    return max(lines, key=lambda l: abs(_area(l)))


def _outline_cells(grid, mask):
    """Built-in outline (3.4.4): the cell-edge boundary of the largest region of `mask`
    (8-connected, holes filled, corner-only contacts closed so that the ring is a simple
    polygon). Used when contourpy is not installed - QGIS 3.28's Python has none (found by the
    real-QGIS smoke run on macOS, 2026-10-02). Follows cell edges where contourpy cuts corners
    at the cell-edge midpoints; after the simplification the two outlines lie within about one
    cell plus the simplification tolerance of each other (measured on blobs and a flood-shaped
    mask, 10 m cells: 9-12 m apart with a 10 m tolerance; 18 m where a corner-only contact was
    closed)."""
    m = np.asarray(mask, bool)
    if not m.any():
        raise ValueError("empty mask")
    try:
        from scipy import ndimage
        lab, n = ndimage.label(m, structure=np.ones((3, 3), bool))
        if n > 1:
            sizes = np.bincount(lab.ravel())
            sizes[0] = 0
            m = lab == int(np.argmax(sizes))
        m = ndimage.binary_fill_holes(m)
    except ImportError:                  # without scipy: the largest ring of the whole mask
        pass
    m = np.pad(m, 1)
    for _ in range(50):                  # close corner-only contacts (pinch vertices)
        a, b, c, d = m[:-1, :-1], m[:-1, 1:], m[1:, :-1], m[1:, 1:]
        p1 = a & d & ~b & ~c
        p2 = b & c & ~a & ~d
        if not (p1.any() or p2.any()):
            break
        m[:-1, 1:] |= p1
        m[:-1, :-1] |= p2
    # directed boundary edges between cell corners (row, col of the padded array): every
    # vertex then has exactly one outgoing edge, so the rings follow without ambiguity
    nxt = {}
    rr, cc = np.nonzero(m)
    for r, c in zip(rr.tolist(), cc.tolist()):
        if not m[r - 1, c]:
            nxt[(r, c)] = (r, c + 1)
        if not m[r, c + 1]:
            nxt[(r, c + 1)] = (r + 1, c + 1)
        if not m[r + 1, c]:
            nxt[(r + 1, c + 1)] = (r + 1, c)
        if not m[r, c - 1]:
            nxt[(r + 1, c)] = (r, c)
    best, best_area = None, -1.0
    x_left = float(grid.x_centers()[0]) - 0.5 * grid.dx
    y_top = float(grid.y_centers()[0]) + 0.5 * grid.dx
    while nxt:
        start, _ = next(iter(nxt.items()))
        ring, v = [], start
        while v in nxt:
            ring.append(v)
            v = nxt.pop(v)
        pts = np.array([[x_left + (c - 1) * grid.dx, y_top - (r - 1) * grid.dx] for r, c in ring])
        a = abs(_area(pts)) if len(pts) >= 3 else 0.0
        if a > best_area:
            best, best_area = pts, a
    if best is None or len(best) < 3:
        raise ValueError("empty mask")
    return best


def mask_outline(grid, mask, simplify_m, backend=None):
    """Outer boundary of the largest region of `mask` as an (N, 2) array of
    x, y (open ring, counter-clockwise). contourpy when it is installed, else the built-in
    cell-edge tracer (`backend` "contourpy" / "cells" forces one; tests)."""
    use = backend or ("contourpy" if _have_contourpy() else "cells")
    ring = _outline_contourpy(grid, mask) if use == "contourpy" else _outline_cells(grid, mask)
    if np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    ring = douglas_peucker(np.vstack([ring, ring[:1]]), simplify_m)[:-1]
    if _area(ring) < 0:
        ring = ring[::-1]
    return ring


def _area(ring):
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def disk_mask(grid, center_xy, radius_m):
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    return np.hypot(X - center_xy[0], Y - center_xy[1]) <= radius_m


def dilate(mask, dist_m, dx):
    from scipy.ndimage import distance_transform_edt
    if dist_m <= 0:
        return mask.copy()
    return distance_transform_edt(~mask) * dx <= dist_m


def largest_component(mask, seed_rc=None):
    from scipy.ndimage import label
    lab, n = label(mask, structure=np.ones((3, 3), bool))
    if n <= 1:
        return mask
    if seed_rc is not None and lab[seed_rc] > 0:
        return lab == lab[seed_rc]
    sizes = np.bincount(lab.ravel())
    sizes[0] = 0
    return lab == int(np.argmax(sizes))


# ----------------------------------------------------------------------------
# Domain edges: does the flood leave the model deeper than the tolerance?
# ----------------------------------------------------------------------------
EDGES = ("N", "S", "E", "W")


def edge_depths(depth, valid=None):
    """Largest depth on each edge row/column of the grid (m)."""
    d = np.where(np.isfinite(depth), depth, 0.0)
    if valid is not None:
        d = np.where(valid, d, 0.0)
    return {"N": float(d[0, :].max()), "S": float(d[-1, :].max()),
            "W": float(d[:, 0].max()), "E": float(d[:, -1].max())}


def expand_bounds(bounds, edges, step_m, limits=None):
    xmin, ymin, xmax, ymax = bounds
    if "W" in edges:
        xmin -= step_m
    if "E" in edges:
        xmax += step_m
    if "S" in edges:
        ymin -= step_m
    if "N" in edges:
        ymax += step_m
    return (xmin, ymin, xmax, ymax)


def bounds_size(bounds):
    return bounds[2] - bounds[0], bounds[3] - bounds[1]
