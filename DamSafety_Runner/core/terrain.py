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
INFLOW_DEEP_FACTOR = 1.3      # v3.20.8: a warning only (the pick is kept): the automatic
                              # inflow's ground is more than this times the dam height below the
                              # top of dam (DWR-3034, no dam line: 53.3 ft below the 30.7 ft
                              # dam's top, 1.74 H, maybe a deeper coulee beside the dam)
INFLOW_DEEP = "CHECK THE INFLOW DEPTH"


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
                         near_m=600.0, top_m=None, pool=None, water_grid=None, water_line=None,
                         info=None, surface_ac=None, water_read=None):
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
    ESE was, which turned the downstream side round.
    3.20.12: with `water_grid` (the DEM at WATER_SIDE_CELL_M round the dam line) and `top_m`,
    a dam line first asks which side holds the reservoir's flat water (water_side): when it
    answers, the side is the line's chord normal away from that water. `water_line` is the
    line for that test when `crest_line` is None (a batch's CREST_LINE: the side otherwise
    keeps the point rules 2-4). When the test has no answer, nothing changes. `info` (a dict)
    gets "side_how" (SIDE_* codes) and, from the water test, "water".
    3.20.12 (review): the water test does not override the rules above when they disagree and
    the other side of the line holds a substantial DRY closed basin (dry_basin: deep by the
    depth rule, at least DRY_BASIN_DEPTH_H x H, DRY_BASIN_MIN_AC or DRY_BASIN_SHARE of
    `surface_ac` (the dam's surface area at the top, acres) when known, and filling to above
    the water's level) while the water is pond-sized (water_extent, read again with
    `water_read`, the terrain's read(bounds, cell), over WATER_AREA_PAD_M: less than
    DRY_BASIN_WATER_SHARE of the basin's area and of `surface_ac` when known), the dry
    reservoir behind a dam with a pond below it; nor when its water
    is much smaller than lower flat water on the other side (water_side, `detail` "both"). The
    side is then the old rule's, and info "side_conflict" holds both readings (the caller warns
    CHECK THE INFLOW SIDE).
    3.20.12 (lead): when the water test decides the side and the old rules (as 3.20.11 ran them)
    point the other way, the water's side is kept and info "side_conflict" (kind
    CONFLICT_WATER_KEPT) holds both readings too: the override is never silent (the caller warns
    CHECK THE INFLOW SIDE, side_conflict_text). Rules that agree, or an old rule with no
    answer, add nothing."""
    info = {} if info is None else info
    water = tentative = None
    wline = crest_line if crest_line is not None and len(crest_line) >= 2 else water_line
    if (water_grid is not None and top_m is not None and wline is not None
            and len(wline) >= 2):
        wn = chord_normal(wline)
        if wn is not None:
            detail = {}
            up, wb, why = water_side(water_grid, wline, wn, dam_height_m, top_m, detail=detail)
            info["water"] = {"why": why}
            if up:
                below = (float(top_m) - wb["level"]) / float(dam_height_m)
                info["water"].update(level_ft=round(wb["level"] / 0.3048, 2),
                                     below_top_h=round(below, 2),
                                     area_ac=round(wb["area_ac"], 2))
                water = ((-up * wn[0], -up * wn[1]), (
                    "crest-line normal away from the reservoir's flat water ({:.1f} ft, {:.2f} H "
                    "below the top of dam)".format(wb["level"] / 0.3048, below)), wb)
            elif detail.get("both"):
                info["water"]["both"] = detail["both"]
                sg = detail["both"]["up"]
                tentative = (-sg * wn[0], -sg * wn[1])
    old_failed = False
    try:
        od, ohow, ocode = _old_side(pool_grid, dam_xy, dam_height_m, crest_line, crest_xy,
                                    near_m, top_m, pool)
    except Exception as e:                          # noqa: BLE001
        if water is None and tentative is None:
            raise
        od, ohow, ocode = None, "the pool rule failed: {}".format(e), SIDE_NONE
        old_failed = True

    def reading(d, how, code):
        return {"side_how": code, "how": how,
                "direction": None if d is None else [round(float(d[0]), 3), round(float(d[1]), 3)]}

    if water is not None:
        wd, whow, wb = water
        if od is not None and od[0] * wd[0] + od[1] * wd[1] < 0 and pool_grid is not None:
            basin = dry_basin(pool_grid, wline, wd, dam_height_m, dam_xy)
            big = dry_basin_is_reservoir(basin, dam_height_m, wb["level"], surface_ac)
            if big:
                warea = wb["area_ac"]
                if water_read is not None:
                    try:
                        warea = max(warea, water_extent(water_read, water_grid.dx, wline,
                                                        -up_side(wd, wline), wb["level"]))
                    except Exception:               # noqa: BLE001 - the window's area then
                        pass
                basin["water_area_ac"] = warea
                big = pond_sized(warea, basin, surface_ac)
            if big:
                info["side_conflict"] = {
                    "kind": CONFLICT_DRY_BASIN,
                    "kept": reading(od, ohow, ocode),
                    "water": dict(reading(wd, whow, SIDE_WATER), **info["water"]),
                    "dry_basin": {"area_ac": round(basin["area_ac"], 2),
                                  "depth_h": round(basin["depth_h"], 2),
                                  "fills_to_ft": round(basin["spill_m"] / 0.3048, 2),
                                  "water_area_ac": round(basin["water_area_ac"], 2)},
                    "why": ("a dry closed basin of {:.1f} ac, {:.2f} H deep, filling to {:.1f} ft "
                            "(above that water, {:.1f} ac) is against the line on the side the "
                            "water test takes as downstream".format(
                                basin["area_ac"], basin["depth_h"], basin["spill_m"] / 0.3048,
                                basin["water_area_ac"]))}
                info["side_how"] = ocode
                return od, ohow
        if od is not None and od[0] * wd[0] + od[1] * wd[1] < 0:
            # 3.20.12 (lead, 2026-11-17): the water's side is kept (it fixed 17 real dams, 0
            # regressions), but the override is never silent: the caller warns CHECK THE
            # INFLOW SIDE with both readings
            info["side_conflict"] = {
                "kind": CONFLICT_WATER_KEPT,
                "kept": reading(wd, whow, SIDE_WATER),
                "old": reading(od, ohow, ocode),
                "water": dict(reading(wd, whow, SIDE_WATER), **info["water"]),
                "why": side_conflict_text({"kind": CONFLICT_WATER_KEPT,
                                           "old": {"side_how": ocode, "how": ohow},
                                           "water": info["water"]})}
        elif od is None and old_failed:
            # 3.20.12 (final review): the old rules raised, so the water's side could not be
            # compared with anything; keep it, but never silently
            info["side_conflict"] = {
                "kind": CONFLICT_NO_OTHER_RULE,
                "kept": reading(wd, whow, SIDE_WATER),
                "water": dict(info["water"]),
                "why": "the pool / ground rules could not be run ({}); the side comes from the "
                       "reservoir's flat water alone".format(ohow)}
        info["side_how"] = SIDE_WATER
        return wd, whow
    if tentative is not None:
        thow = "crest-line normal away from the higher flat water"
        if od is None:
            # no other rule answers: the higher water, to check
            info["side_conflict"] = {"kind": CONFLICT_NO_OTHER_RULE,
                                     "kept": reading(tentative, thow, SIDE_WATER),
                                     "water": dict(info["water"]),
                                     "why": "no other rule gives a side; " + info["water"]["why"]}
            info["side_how"] = SIDE_WATER
            return tentative, "{} ({})".format(thow, info["water"]["why"])
        if od[0] * tentative[0] + od[1] * tentative[1] < 0:
            info["side_conflict"] = {"kind": CONFLICT_SMALL_HIGHER_WATER,
                                     "kept": reading(od, ohow, ocode),
                                     "water": dict(reading(tentative, thow, SIDE_WATER),
                                                   **info["water"]),
                                     "why": info["water"]["why"]}
    info["side_how"] = ocode
    return od, ohow


def _old_side(pool_grid, dam_xy, dam_height_m, crest_line, crest_xy, near_m, top_m, pool):
    """downstream_direction's rules 1-4 (the pool, the lower side, the traces): (direction or
    None, how, SIDE_* code)."""
    def unit(v):
        n = math.hypot(v[0], v[1])
        return (v[0] / n, v[1] / n) if n > 1e-9 else None

    away = None
    if pool_grid is not None:
        # L05: reuse the caller's pool_mask when it passed one (identity test, so a None from a
        # caller whose pool_mask raised still falls through to the recompute and the same error).
        pool = pool_at_dam(pool if pool is not None else
                           pool_mask(pool_grid, dam_height_m, dam_xy, top_m, crest_line),
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
            ref, how, code = away, "crest-line normal away from the pool", SIDE_POOL
            if ref is None and pool_grid is not None:
                lower = _lower_side(pool_grid, crest_line, n, dam_height_m)
                if lower:
                    ref = (lower * n[0], lower * n[1])
                    how = "crest-line normal on the lower side (no pool against the dam)"
                    code = SIDE_LOWER
            if ref is None:
                ref, how = trace_dir(dam_xy), "crest-line normal on the downstream trace's side"
                code = SIDE_TRACE_LINE
            if ref is not None:
                if n[0] * ref[0] + n[1] * ref[1] < 0:
                    n = (-n[0], -n[1])
                return n, how, code
    if away is not None:
        return away, "away from the pool behind the dam", SIDE_POOL_POINT
    v = trace_dir(dam_xy)
    if v is not None:
        return v, "downstream trace from the dam point", SIDE_TRACE_POINT
    v = trace_dir(crest_xy)
    if v is not None:
        return v, ("downstream trace from the crest (highest ground near the dam point)"), \
            SIDE_TRACE_CREST
    return None, "no crest line, no pool and no downstream trace", SIDE_NONE


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


INFLOW_TOE_INNER_M = 5.0         # v3.13.0 (Nick, "would do 2"): the toe search runs from this far
                                 # downstream of the dam line ...
INFLOW_TOE_DS_SLOPE_DEFAULT = 3.0  # ... out to H x (downstream slope + 1), H the dam height (m) and
                                 # the slope H:1V (the dam's ds_slope_h; 3:1, i.e. 4H, unknown)
INFLOW_TOE_MIN_OUTER_M = 15.0    # the outer limit is never nearer than this (a low dam: 5-15 m,
                                 # Nick's earlier strip) ...
INFLOW_TOE_BAND_M = (5.0, 30.0)  # ... and is this band, with a warning, when the dam height is not
                                 # known (also toe_band_mask's default)
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


def toe_band_for_dam(h_m, ds_slope_h=None, inner_m=None, min_outer_m=None, cap_m=None):
    """v3.13.0 (Nick: the toe search's outer limit is H x (downstream slope + 1)): the toe band
    (inner, outer) m downstream of the dam line for a dam `h_m` high with a downstream slope of
    `ds_slope_h`:1 (INFLOW_TOE_DS_SLOPE_DEFAULT when unknown). The outer limit is at least
    `min_outer_m` (INFLOW_TOE_MIN_OUTER_M) and more than the inner limit, and never past `cap_m`
    (INFLOW_MAX_FROM_DAM_M). Returns ((inner, outer), how, known) - known is False when the dam
    height is unknown and INFLOW_TOE_BAND_M is used (the caller warns)."""
    lo = INFLOW_TOE_INNER_M if inner_m is None else float(inner_m)
    cap = INFLOW_MAX_FROM_DAM_M if cap_m is None else float(cap_m)
    least = INFLOW_TOE_MIN_OUTER_M if min_outer_m is None else float(min_outer_m)
    try:
        h = float(h_m)
    except (TypeError, ValueError):
        h = float("nan")
    if not (h > 0 and np.isfinite(h)):
        lo, hi = INFLOW_TOE_BAND_M[0] if inner_m is None else lo, INFLOW_TOE_BAND_M[1]
        return (lo, max(min(hi, cap), lo + 1.0)), (
            "dam height not known: the default {:.0f}-{:.0f} m band".format(lo, hi)), False
    try:
        z = float(ds_slope_h)
        z_how = "{:g}:1 downstream slope".format(z)
    except (TypeError, ValueError):
        z = float("nan")
    if not (z > 0 and np.isfinite(z)):
        z = INFLOW_TOE_DS_SLOPE_DEFAULT
        z_how = "downstream slope not known, {:g}:1".format(z)
    raw = h * (z + 1.0)
    hi = min(max(raw, least, lo + 1.0), cap)
    how = "H x (slope + 1) = {:.1f} m x {:g} = {:.1f} m ({})".format(h, z + 1.0, raw, z_how)
    if hi > raw + 1e-6:
        how += ", at least {:.0f} m".format(hi)
    elif hi < raw - 1e-6:
        how += ", capped at {:.0f} m".format(hi)
    return (lo, hi), how, True


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


def _toe_band_geom(grid, crest_line, downstream):
    """L05: the band-independent toe geometry of `grid` - the distance to the dam line and the
    signed downstream offset (the cosine to the nearest segment's downstream normal). It does
    not depend on the band limits, so toe_band_mask thresholds it and toe_band_inflow reuses
    the one array as the band widens, instead of rebuilding the meshgrid and _line_foot each
    time. Returns (dl, side), the single source of the band rule."""
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    dl, fx, fy, seg = _line_foot(X, Y, crest_line)
    n = _segment_normals(crest_line, downstream)
    with np.errstate(invalid="ignore", divide="ignore"):
        side = ((X - fx) * n[seg, 0] + (Y - fy) * n[seg, 1]) / np.where(dl > 0, dl, 1.0)
    return dl, side


def toe_band_mask(grid, crest_line, downstream, band_m=None):
    """v3.12.10: cells of `grid` in the toe band: band_m[0]..band_m[1] m from the dam line, on
    the downstream side of the segment they are nearest (the offset from the nearest point of
    the line within 60 degrees of that segment's normal turned downstream), so the band runs
    along the full crest length, follows a bent line, and does not wrap round the ends of the
    line onto the upstream side. Returns (mask, distance from the line)."""
    lo, hi = INFLOW_TOE_BAND_M if band_m is None else (float(band_m[0]), float(band_m[1]))
    dl, side = _toe_band_geom(grid, crest_line, downstream)
    return (dl >= lo) & (dl <= hi) & (side >= 0.5), dl


def toe_band_inflow(grid, crest_line, downstream, dam_height_m, top_m, band_m=None,
                    pool=None, max_from_dam_m=None, dam_xy=None):
    """v3.12.10 (Nick, DWR-4031: "the inflow point at the dam toe at the lowest elevation";
    v3.13.0, "would do 2"): the lowest cell of the toe band (toe_band_mask: `band_m`, by default
    toe_band_for_dam - 5 m to H x (3 + 1) - downstream of the dam line along its full length;
    workflow passes the dam's own downstream slope) on `grid`, which should be the fine DEM
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
    cap = INFLOW_MAX_FROM_DAM_M if max_from_dam_m is None else float(max_from_dam_m)
    lo, hi = (toe_band_for_dam(dam_height_m, cap_m=cap)[0] if band_m is None
              else (float(band_m[0]), float(band_m[1])))
    hi = min(hi, cap)
    if hi <= lo:
        return None, "{}: the band {:.0f}-{:.0f} m is empty".format(NO_TOE, lo, hi)
    a = np.asarray(crest_line, dtype=float).reshape(-1, 2)
    if not _segment_normals(a, downstream).any():
        return None, ("{}: the dam line runs with the flow, its downstream side is not "
                      "known".format(NO_TOE))
    z = grid.masked()
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    dl, side = _toe_band_geom(grid, a, downstream)       # L05: band-independent, reused below
    ok = np.isfinite(z) & (z < float(top_m))
    if pool is not None:
        pr = np.floor((pool.y0 - Y) / pool.dx).astype(int)
        pc = np.floor((X - pool.x0) / pool.dx).astype(int)
        inside = (pr >= 0) & (pr < pool.ny) & (pc >= 0) & (pc < pool.nx)
        in_pool = np.zeros(z.shape, dtype=bool)
        in_pool[inside] = pool.data[pr[inside], pc[inside]] > 0.5
        ok &= ~(in_pool & (dl > 0.7072 * pool.dx))
    ref = dam_xy if dam_xy is not None else tuple(a.mean(axis=0))
    dref = np.hypot(X - ref[0], Y - ref[1])
    need = float(top_m) - INFLOW_TOE_DROP * float(dam_height_m)
    half = float(top_m) - 0.5 * float(dam_height_m)
    widest = min(cap, hi + 60.0)
    first = first_half = pick = None
    outer = hi
    while True:
        band = (dl >= lo) & (dl <= outer) & (side >= 0.5)   # L05: same rule as toe_band_mask
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
    dl = float(dl[r, c])                                 # L05: from the hoisted array (identical)
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


# --- 3.20.12: the reservoir's flat water decides the side of a dam line -----------------------
# (validation 2026-11-17, 124 DWR dams with their own DWR line: the toe-band inflow was on the
# reservoir on 17 of them - DWR-2624, 2979, 2983, 2791, 2713, 2824, 2635, 2662, 2809, 2760 ... -
# because a full hydro-flattened reservoir drains at its own level (its fill is ~0, so pool_mask
# does not see it), pool_at_dam took a borrow pit or a road-held low below the dam as the pool,
# and _lower_side found the water surface 20-80 m upstream lower than the downstream face)
WATER_SIDE_DROP = INFLOW_TOE_DROP  # reservoir water lies 0 .. this x H below the top of dam (deeper
                                   # water can be the toe: Chase Lake 0.9 H below DWR-5420)
WATER_LEVEL_TIE_M = 0.3            # two water levels this close are one level (a causeway over one
                                   # pond, DWR-3161); also the most water may lie above the top
WATER_SIDE_SHARE = 2.0 / 3.0       # a water body is on one side when this share of its cells near
                                   # the line is
WATER_SIDE_CELL_M = 2.0            # the water test reads the DEM at this cell (or its own) ...
WATER_SIDE_PAD_M = 80.0            # ... over the dam line's box padded by this
INFLOW_WATER_DROP = 1.5            # the inflow water check: flat water this x H below the top
                                   # or less, touching the line on the inflow's side
WATER_ON_CELLS = 2                 # ... the inflow is on a body with a cell this many cells away
WATER_SIDE_AREA_RATIO = 0.5        # the side's water is not surely the reservoir's when its area
                                   # is less than this share of flat water (0 .. INFLOW_WATER_DROP
                                   # x H below the top) touching the other side: a pond below a
                                   # drawn-down reservoir (the old rule decides, with a warning)
DRY_BASIN_DEPTH_H = 0.5            # a dry closed basin against the line on the side the water test
DRY_BASIN_MIN_AC = 1.0             # takes as downstream outweighs it (a dry reservoir) when it is
DRY_BASIN_SHARE = 0.25             # this x H deep (the depth rule's fill), this big and this share
                                   # of the dam's surface area at the top when known, and fills to
                                   # WATER_LEVEL_TIE_M above the water's level or more; a borrow
                                   # pit or a road-held low below the dam is smaller, shallower or
                                   # fills below the reservoir's water (DWR-2791)
DRY_BASIN_WATER_SHARE = 0.25       # ... and the water is pond-sized: its area (read out to
WATER_AREA_PAD_M = 300.0           # this round the line) less than this share of the basin's and
                                   # of the dam's surface area at the top when known (DWR-7449:
                                   # 6.4 ac of reservoir water against a 17.5 ac road-held low
                                   # filling above it; S = 15 ac)
INFLOW_SIDE = "CHECK THE INFLOW SIDE"
# side_conflict "kind" (downstream_direction info): which reading the side was taken from
CONFLICT_WATER_KEPT = "water_kept"          # the water's side, against the old rules (lead)
CONFLICT_DRY_BASIN = "dry_basin"            # the old side: a dry reservoir across the line
CONFLICT_SMALL_HIGHER_WATER = "small_higher_water"  # the old side: the higher water is small
CONFLICT_NO_OTHER_RULE = "no_other_rule"    # the higher water's side: nothing else answers
SIDE_FOUND_LINE = "found_line"     # inflow_point.side_how codes (run_report.json)
SIDE_WATER = "reservoir_water"
SIDE_POOL = "pool"
SIDE_LOWER = "lower_side"
SIDE_TRACE_LINE = "line_trace"
SIDE_POOL_POINT = "pool_point"
SIDE_TRACE_POINT = "trace_point"
SIDE_TRACE_CREST = "trace_crest"
SIDE_NONE = "none"
SIDE_FLIPPED = "water_check_flip"
# the side codes that do not come from pool_mask: once one of these decides the side, a pool
# cell on the downstream side is not the reservoir (pool_downstream_off)
SIDES_NOT_POOL = (SIDE_WATER, SIDE_FOUND_LINE, SIDE_FLIPPED)


# the old rules (_old_side) in plain words, for the CHECK THE INFLOW SIDE warning
OLD_RULE_WORDS = {
    SIDE_POOL: "a closed pool against the dam line taken as the reservoir",
    SIDE_LOWER: "the lower ground beside the dam line taken as downstream",
    SIDE_TRACE_LINE: "the downhill flow path from the dam",
    SIDE_POOL_POINT: "a closed pool near the dam point taken as the reservoir",
    SIDE_TRACE_POINT: "the downhill flow path from the dam point",
    SIDE_TRACE_CREST: "the downhill flow path from the crest",
}


def old_rule_words(code, how=None):
    """The old rule (a SIDE_* code from _old_side) in plain words, else `how`."""
    return OLD_RULE_WORDS.get(code) or how or "the pool / ground rules"


def side_conflict_text(conflict):
    """3.20.12 (lead): both readings of a CONFLICT_WATER_KEPT side_conflict in plain words, for
    the CHECK THE INFLOW SIDE warning: "the reservoir's flat water (L ft, X H below the top of
    dam) puts the reservoir on one side of the dam line; the older rule (...) pointed the other
    way"."""
    w = conflict.get("water") or {}
    old = conflict.get("old") or {}
    lvl, below = w.get("level_ft"), w.get("below_top_h")
    where = ("" if lvl is None or below is None else
             " ({:.1f} ft, {:.2f} H below the top of dam)".format(float(lvl), float(below)))
    return ("the reservoir's flat water{} puts the reservoir on one side of the dam line; the "
            "older rule ({}) pointed the other way".format(
                where, old_rule_words(old.get("side_how"), old.get("how"))))


def pool_downstream_off(pool, crest_line, downstream):
    """3.20.12 (review, DWR-2791): `pool` (a Grid of 0/1, pool_mask) with its cells on the
    downstream side of the dam line (_toe_band_geom offset > 0) set to 0. When the side came
    from the reservoir's flat water, the dam point check's found line or the inflow water check
    (SIDES_NOT_POOL), the pool mask's cells below the dam are a borrow pit or a road-held low,
    not the reservoir: kept, they would drop the real toe from the toe band (and the
    find_inflow_point fallback) and put the inflow on the dam's face. Returns `pool` unchanged
    when there is no pool, line or side."""
    if pool is None or downstream is None or crest_line is None or len(crest_line) < 2:
        return pool
    a = np.asarray(crest_line, dtype=float).reshape(-1, 2)
    _dl, side = _toe_band_geom(pool, a, downstream)
    data = np.where(np.nan_to_num(side, nan=0.0) > 0.0, 0.0, pool.data)
    return pool.like(data, nodata=pool.nodata)


def water_window(read, line, native_cell=None, pad_m=None, cover_xy=None):
    """3.20.12: the DEM round the dam line `line` - its box padded by WATER_SIDE_PAD_M (or
    pad_m) and reaching `cover_xy` - read with read(bounds, cell) at WATER_SIDE_CELL_M, or
    `native_cell` when coarser: water_side and inflow_water_hit find flat water on it."""
    a = np.asarray(line, dtype=float).reshape(-1, 2)
    pad = WATER_SIDE_PAD_M if pad_m is None else float(pad_m)
    cell = WATER_SIDE_CELL_M
    if native_cell and float(native_cell) > cell:
        cell = float(native_cell)
    x0, y0 = a[:, 0].min() - pad, a[:, 1].min() - pad
    x1, y1 = a[:, 0].max() + pad, a[:, 1].max() + pad
    if cover_xy is not None:
        x0, x1 = min(x0, cover_xy[0] - 4 * cell), max(x1, cover_xy[0] + 4 * cell)
        y0, y1 = min(y0, cover_xy[1] - 4 * cell), max(y1, cover_xy[1] + 4 * cell)
    return read((x0, y0, x1, y1), cell)


def flat_water(grid):
    """3.20.12: hydro-flattened water on `grid`, by the dam point check's rule
    (dam_point_check.FLAT_RANGE_M, WATER_SPREAD_M, WATER_MIN_AC): connected cells whose 3 x 3
    range is at most FLAT_RANGE_M, at least WATER_MIN_AC, their p98 - p2 at most WATER_SPREAD_M.
    Returns (label array, [{"id", "level" (the median, m), "area_ac"}])."""
    from scipy.ndimage import maximum_filter, minimum_filter, label
    from . import dam_point_check as dpc
    z = grid.masked()
    zf = np.where(np.isfinite(z), z, -1e6)
    rng = maximum_filter(zf, 3) - minimum_filter(zf, 3)
    flat = np.isfinite(z) & (rng <= dpc.FLAT_RANGE_M)
    lab, n = label(flat, structure=np.ones((3, 3), bool))
    out = []
    if n == 0:
        return lab, out
    cell_ac = grid.dx * grid.dx / dpc.ACRE
    counts = np.bincount(lab.ravel())
    for lid in np.flatnonzero(counts * cell_ac >= dpc.WATER_MIN_AC):
        if lid == 0:
            continue
        v = z[lab == lid]
        p2, p50, p98 = np.percentile(v, [2, 50, 98])
        if p98 - p2 <= dpc.WATER_SPREAD_M:
            out.append({"id": int(lid), "level": float(p50), "area_ac": float(v.size * cell_ac)})
    return lab, out


def chord_normal(line):
    """The unit normal (left of A -> B) of the chord of `line`, or None."""
    a = np.asarray(line, dtype=float).reshape(-1, 2)
    if len(a) < 2:
        return None
    t = a[-1] - a[0]
    L = math.hypot(t[0], t[1])
    if L <= 1e-9:
        return None
    return (float(-t[1] / L), float(t[0] / L))


def _touching_water(grid, line, n, lab, bodies, touch_m=None):
    """The water bodies within touch_m (POOL_TOUCH_M, at least 3 cells) of `line`, each with
    its cells near the line on the +n side (`plus`) and the -n side (`minus`), the side by the
    toe band's rule (_toe_band_geom: the cosine to the nearest segment's normal, at least 0.5)."""
    if not bodies:
        return []
    touch = max(POOL_TOUCH_M, 3.0 * grid.dx) if touch_m is None else float(touch_m)
    dl, side = _toe_band_geom(grid, np.asarray(line, dtype=float).reshape(-1, 2), n)
    near = dl <= touch
    out = []
    for b in bodies:
        m = (lab == b["id"]) & near
        p, q = int((m & (side >= 0.5)).sum()), int((m & (side <= -0.5)).sum())
        if p + q:
            out.append(dict(b, plus=p, minus=q))
    return out


def water_side(grid, crest_line, n, dam_height_m, top_m, touch_m=None, detail=None):
    """3.20.12: which side of the dam line holds the reservoir, from hydro-flattened water
    (flat_water) on `grid` (the DEM at WATER_SIDE_CELL_M round the line): water bodies whose
    level is 0 .. WATER_SIDE_DROP x H below the top of dam (up to WATER_LEVEL_TIE_M above it)
    and that reach within touch_m (POOL_TOUCH_M) of the line with at least WATER_SIDE_SHARE of
    their near cells on one side. Water on one side only: that side; on both: the higher level,
    unless the two are within WATER_LEVEL_TIE_M (one level both sides, a causeway: no answer).
    Water deeper than WATER_SIDE_DROP x H can be the toe (a lake below a low dam, DWR-5420).
    Review: no answer either when the side's water has less than WATER_SIDE_AREA_RATIO of the
    area of lower flat water (to INFLOW_WATER_DROP x H below the top) touching the other side
    (a pond below a drawn-down reservoir): `detail` (a dict) then gets "both" (the side the
    higher water gives, "up", and both bodies) for the caller's warning.
    Returns (+1 / -1 along the chord normal `n`, the body or None, why)."""
    h, top = float(dam_height_m), float(top_m)
    if not (h > 0 and math.isfinite(h) and math.isfinite(top)):
        return 0, None, "dam height or top of dam not known"
    lab, bodies = flat_water(grid)
    bodies = [b for b in bodies if top - INFLOW_WATER_DROP * h <= b["level"] <=
              top + WATER_LEVEL_TIE_M]
    near = {}                                   # every touching body on one side, by side
    for b in _touching_water(grid, crest_line, n, lab, bodies, touch_m):
        p, q = b["plus"], b["minus"]
        if max(p, q) < WATER_SIDE_SHARE * (p + q):
            continue
        near.setdefault(1 if p > q else -1, []).append(dict(b, cells=max(p, q)))
    best = {}
    for sg, bs in near.items():
        bs = [b for b in bs if b["level"] >= top - WATER_SIDE_DROP * h]
        if bs:
            best[sg] = max(bs, key=lambda b: b["level"])
    if not best:
        if not any(top - WATER_SIDE_DROP * h <= b["level"] for b in bodies):
            return 0, None, "no flat water 0-{:.2f} H below the top of dam at the line".format(
                WATER_SIDE_DROP)
        return 0, None, "flat water near the crest level does not touch one side of the line"
    if len(best) == 2:
        if abs(best[1]["level"] - best[-1]["level"]) < WATER_LEVEL_TIE_M:
            return 0, None, "flat water at one level on both sides of the line"
        up = 1 if best[1]["level"] > best[-1]["level"] else -1
    else:
        up = next(iter(best))
    wb = best[up]
    lower = [b for b in near.get(-up, []) if b["level"] < wb["level"] and
             wb["area_ac"] < WATER_SIDE_AREA_RATIO * b["area_ac"]]
    if lower:
        lo = max(lower, key=lambda b: b["area_ac"])

        def desc(b):
            return {"level_ft": round(b["level"] / 0.3048, 2),
                    "below_top_h": round((top - b["level"]) / h, 2),
                    "area_ac": round(b["area_ac"], 2)}
        if detail is not None:
            detail["both"] = {"up": up, "higher": desc(wb), "lower": desc(lo)}
        return 0, None, ("flat water on both sides of the line: the higher ({:.1f} ft, {:.1f} "
                         "ac) is much smaller than the lower ({:.1f} ft, {:.1f} ac)".format(
                             wb["level"] / 0.3048, wb["area_ac"], lo["level"] / 0.3048,
                             lo["area_ac"]))
    return up, wb, "flat water on one side"


def dry_basin(pool_grid, line, side_dir, dam_height_m, dam_xy=None):
    """3.20.12 (review): the closed basin (depth rule: fill more than max(0.5 m, 15 % of H), as
    pool_mask) reaching within POOL_TOUCH_M of `line` (pool_at_dam), its cells on the
    `side_dir` side of the line (the toe band's side rule) on `pool_grid` (the 2 km pool read).
    Returns None, or {"area_ac", "depth_h" (its deepest fill / H), "spill_m" (the level it
    fills to)}."""
    from . import dam_point_check as dpc
    if pool_grid is None or line is None or len(line) < 2 or side_dir is None:
        return None
    h = float(dam_height_m)
    z = pool_grid.masked()
    filled = priority_flood(z)
    fill = filled - z
    good = np.isfinite(fill)
    deep = good & (fill > max(0.5, 0.15 * h))
    a = np.asarray(line, dtype=float).reshape(-1, 2)
    xy = dam_xy if dam_xy is not None else tuple(a.mean(axis=0))
    comp = pool_at_dam(deep, pool_grid, xy, a)
    _dl, side = _toe_band_geom(pool_grid, a, side_dir)
    on = comp & (np.nan_to_num(side, nan=0.0) >= 0.5)
    if not on.any():
        return None
    return {"area_ac": float(on.sum() * pool_grid.dx * pool_grid.dx / dpc.ACRE),
            "depth_h": float(fill[on].max() / h), "spill_m": float(filled[on].max())}


def up_side(direction, line):
    """+1 / -1: the side of `line`'s chord normal that `direction` points to."""
    n = chord_normal(line)
    return 1 if n[0] * direction[0] + n[1] * direction[1] >= 0 else -1


def water_extent(read, cell, line, sg, level_m, pad_m=None):
    """3.20.12 (review): the area (acres) of the flat water at `level_m` (within
    WATER_LEVEL_TIE_M) touching `line` on its chord normal's `sg` side, on the DEM read with
    read(bounds, cell) over the line's box padded by WATER_AREA_PAD_M (the 80 m water window
    cuts a reservoir off)."""
    g = water_window(read, line, cell, pad_m=WATER_AREA_PAD_M if pad_m is None else pad_m)
    lab, bodies = flat_water(g)
    bodies = [b for b in bodies if abs(b["level"] - float(level_m)) < WATER_LEVEL_TIE_M]
    n = chord_normal(line)
    tot = 0.0
    for b in _touching_water(g, line, n, lab, bodies):
        if (b["plus"] if sg > 0 else b["minus"]) >= WATER_SIDE_SHARE * (b["plus"] + b["minus"]):
            tot += b["area_ac"]
    return tot


def pond_sized(water_ac, basin, surface_ac=None):
    """3.20.12 (review): is flat water of `water_ac` acres pond-sized next to the dry basin
    (dry_basin): less than DRY_BASIN_WATER_SHARE of its area, and of `surface_ac` (the dam's
    surface area at the top) when known?"""
    if not basin or water_ac is None:
        return False
    if water_ac >= DRY_BASIN_WATER_SHARE * basin["area_ac"]:
        return False
    try:
        if surface_ac is not None and float(surface_ac) > 0 and \
                water_ac >= DRY_BASIN_WATER_SHARE * float(surface_ac):
            return False
    except (TypeError, ValueError):
        pass
    return True


def dry_basin_is_reservoir(basin, dam_height_m, level_m, surface_ac=None):
    """3.20.12 (review): does the dry basin (dry_basin) outweigh flat water at `level_m` on the
    other side of the line: at least DRY_BASIN_DEPTH_H x H deep, DRY_BASIN_MIN_AC and
    DRY_BASIN_SHARE of `surface_ac` (when known) big, and filling to WATER_LEVEL_TIE_M above
    that water or more (a borrow pit or a road-held low below the dam fills below the
    reservoir's water)?"""
    if not basin:
        return False
    need = DRY_BASIN_MIN_AC
    try:
        if surface_ac is not None and float(surface_ac) > 0:
            need = max(need, DRY_BASIN_SHARE * float(surface_ac))
    except (TypeError, ValueError):
        pass
    return (basin["depth_h"] >= DRY_BASIN_DEPTH_H and basin["area_ac"] >= need and
            basin["spill_m"] >= float(level_m) + WATER_LEVEL_TIE_M)


def inflow_water_hit(grid, line, downstream, dam_height_m, top_m, inflow_xy, touch_m=None):
    """3.20.12, the inflow water check: is the inflow on hydro-flattened water (flat_water on
    `grid`) whose level is no more than INFLOW_WATER_DROP x H below the top of dam (and at most
    WATER_LEVEL_TIE_M above it), and that touches the dam line (within touch_m, POOL_TOUCH_M)
    on the inflow's side (`downstream`)? On the water: a cell of the body within WATER_ON_CELLS
    cells, the inflow's ground at most WATER_LEVEL_TIE_M above its level (the lowest cell of the
    toe band is often the shore cell beside the flat). Returns None, or {"level_m",
    "below_top_h", "area_ac", "other_level_m", "other_area_ac"}: other_level_m is the highest
    flat water (up to the top of dam) touching the line on the other side (other_area_ac its
    area), or None."""
    h, top = float(dam_height_m), float(top_m)
    if inflow_xy is None or not (h > 0 and math.isfinite(h) and math.isfinite(top)):
        return None
    r, c = grid.rc(inflow_xy[0], inflow_xy[1])
    n = chord_normal(line)
    if not grid.inside(r, c) or n is None:
        return None
    zi = grid.masked()[r, c]
    if not np.isfinite(zi):
        return None
    lab, bodies = flat_water(grid)
    bodies = [b for b in bodies if b["level"] <= top + WATER_LEVEL_TIE_M]
    lev = {b["id"]: b["level"] for b in bodies if b["level"] >= top - INFLOW_WATER_DROP * h}
    k = WATER_ON_CELLS
    win = lab[max(0, r - k):r + k + 1, max(0, c - k):c + k + 1]
    ids = [int(i) for i in np.unique(win) if int(i) in lev and zi <= lev[int(i)] +
           WATER_LEVEL_TIE_M]
    if not ids:
        return None
    lid = min(ids, key=lambda i: abs(zi - lev[i]))
    sg = 1 if n[0] * downstream[0] + n[1] * downstream[1] >= 0 else -1
    near = _touching_water(grid, line, n, lab, bodies, touch_m)
    mine = next((b for b in near if b["id"] == lid), None)
    if mine is None or (mine["plus"] if sg > 0 else mine["minus"]) == 0:
        return None
    other = [b for b in near if b["id"] != lid and (b["minus"] if sg > 0 else b["plus"])]
    oth = max(other, key=lambda b: b["level"]) if other else None
    return {"level_m": mine["level"], "below_top_h": round((top - mine["level"]) / h, 2),
            "area_ac": round(mine["area_ac"], 2),
            "other_level_m": None if oth is None else oth["level"],
            "other_area_ac": None if oth is None else round(oth["area_ac"], 2)}


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
    line's normal on the inflow's side).
    3.13.5: (b) is a problem only with no `downstream` and no `crest_line`; else a 'notes'
    entry (DWR-6141: the trace ran along the toe, a false CHECK THE DAM POINT). Returns
    {'ok', 'problems', ...}."""
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
            # 3.13.5 (DWR-6141 Taskers Coulee): (b) measures the inflow from the dam POINT
            # along the trace's first 200 m. When the trace first runs along the toe (parallel
            # to the dam) and the inflow is off to one side of mid-crest - a bent or long dam -
            # it says "behind" for a point that is downstream (6141: 33 m NE of the dam line,
            # trace east along the toe then north, cos 0.59 with the line normal). With a
            # downstream side or a dam line, (c), (d) and (f) test the side directly, so (b)
            # is only a note there; it is still a problem with neither.
            msg = ("the inflow point is behind the dam relative to the flow "
                   "trace's direction ({:.2f}, {:.2f})".format(*fd))
            if downstream is None and not has_line:
                out["problems"].append(msg)
            else:
                out["notes"] = out.get("notes", []) + [msg + " (not a problem: the "
                               "downstream side / dam line checks decide)"]
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


def edge_flux_shares(depth, valid=None):
    """Each edge's share (0-1) of the water leaving the grid, estimated from the max depths on
    it. LISFLOOD-FP reports only the total boundary outflow (Qout), not per edge, so the edges
    are weighed by the sum of depth**(5/3) along them (Manning unit discharge at the same slope
    and roughness). A screening estimate for which edge to move out, not a flow."""
    d = np.where(np.isfinite(depth), depth, 0.0)
    if valid is not None:
        d = np.where(valid, d, 0.0)
    d = np.clip(d, 0.0, None) ** (5.0 / 3.0)
    w = {"N": float(d[0, :].sum()), "S": float(d[-1, :].sum()),
         "W": float(d[:, 0].sum()), "E": float(d[:, -1].sum())}
    tot = sum(w.values())
    return {k: (v / tot if tot > 0 else 0.0) for k, v in w.items()}


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
