"""3.20.11: the dam point checked on the run's own DEM before it is used (Nick, batch of 44,
2026-11-16: "Auto-move clear cases, warn").

The DWR inventory point of a dam is often off the embankment: on the reservoir water, on a knoll
beside the pond, on the upstream face. The point-only path then reads the top of dam from the
wrong ground and can put the breach inflow in the pond. Before the dam point is used, the run
reads its DEM around the point (2 m, average resampling; 4 m = 2 x 2 block means of it) and

1. finds the hydro-flattened water and dry closed basins near the point (the pond),
2. finds the embankment between the pond and the low ground below it (a polyline along the
   crest, not a straight chord),
3. compares it with what the point-only path would do with the point as given (the "replay",
   computed by the caller: core/workflow._dam_point_check), and
4. decides: "moved" (the found embankment becomes the dam line, exactly like a user dam line;
   only when a trigger fires and every gate passes), "kept" (the point is on the embankment,
   or nothing clear was found: findings go to the report block and the log only) or "review"
   (a likely misplacement, or a trigger with a failed gate: the point is kept and a REVIEW
   warning says why).

Pure numpy / scipy.ndimage: no QGIS and no import of workflow, so it is tested without a run.
Ported from the batch-44 screen (screen_points.py, 2026-11-16) and its review; the critique of
the design (dpc_critique.md) added the neighbour-dam guards, the pond-side inflow trigger, the
closed-line rule, the on-crest parts for the crest raise and the "DEM cell unknown" skip.

All lengths are metres and z is DEM metres (after z_to_m)."""
import math
import os
import threading
from collections import OrderedDict

import numpy as np

FT = 0.3048
ACRE = 4046.8564224

VERSION = 1
MODES = ("off", "warn", "move")
# Nick 2026-11-16 chose "move" (auto-move clear cases, warn); the hold-out validation of
# 2026-11-17 left one move 30 m off the dam line (DWR-2713, a side dike of the same pond) that
# no principled gate removes, so 3.20.11 shipped "warn". 3.20.13 (Nick 2026-11-17: "dam point
# should be at the center of the embankment in most all cases"): "move" is the default again
# (the hold-out's 2 wrong moves of 43 were both DWR-2713), and a batch dam with a DWR dam line
# uses that line instead (core/dwr_lines.py: the check is then skipped, "dam line given")
DEFAULT_MODE = "move"
ENV = "NDBREACH_DAM_POINT_CHECK"

# --- the search (screen_points.py) -------------------------------------------------------
FLAT_RANGE_M = 0.005      # most a 3 x 3 block of hydro-flattened water may vary
WATER_SPREAD_M = 0.05     # most one water body may vary (p98 - p2)
WATER_MIN_AC = 0.25       # smallest water body / closed basin
SEARCH_POOL_M = 400.0     # how far from the point a pond may start
WATER_FOUND_M = 1000.0    # "the DEM has flat water at all" (hydro-flattened) within this
READ_HALF_M = 1100.0      # half-width of the DEM read
CELL_WATER_M = 2.0        # cell of the water search (read with average resampling)
CELL_EMB_M = 4.0          # cell of the embankment search (2 x 2 block mean of the 2 m read)
MAX_NATIVE_CELL_M = 5.0   # coarser DEMs are not checked

# --- the decision ------------------------------------------------------------------------
KEEP_M = 15.0             # the point this close to the embankment is on it
CENTRE_MIN_M = 10.0       # C1 (3.20.13): a point on the embankment this far or more from the
#                           found line's centre (its length midpoint) moves to it ("move" mode,
#                           every gate and V passing); closer, it is the centre already
NEAR_M = 30.0             # A1 / A2: the embankment is more than this away
MAX_MOVE_M = 400.0        # G3
HR_MIN, HR_MAX = 0.8, 1.25            # G2: embankment height / H
LEN_MIN_M, LEN_MIN_H = 40.0, 3.0      # G4: line length >= max(40 m, 3 H)
AREA_RATIO = (0.1, 5.0)               # G6: pond area / inventory top-of-dam area
LINE_ON_CREST = 0.90                  # G4
LINE_BELOW_POOL = 0.05                # G4
LINE_PERP95_M = 15.0                  # G4: at least this; the band widens with the fill:
LINE_PERP95_H = 2.0                   #     max(15 m, 2 H) (validation 2026-11-17)
VERIFY_DROP_H = (0.5, 1.3)            # V: mid-crest top minus toe inflow, in H
A3_SIDE_M = 10.0          # A3: the replay's inflow this far on the pond side of the crest
A2_H = 0.25               # A2: the top read this far (x H, at least A2_MIN_FT) off the crest
A2_MIN_FT = 3.0
POINT_RIDGE_M = 20.0      # G7a: another embankment ridge (>= 0.5 H) this near the point
DOWNSTREAM_M = KEEP_M     # G7b: the point this far on the low side of the found crest
LOW_GROUND_H, LOW_GROUND_M = 0.25, 1.0    # G7b: a point on the low side moves only from the
#                                           low ground below the embankment (not on a fill)
POND_BETWEEN_FRAC = 0.2   # G7f: this share of the way from the point to the crest is the pond
OTHER_LINE_M = 30.0       # G7c: the found line this near another dam's crest line
OTHER_ON_LINE_M = 25.0    # G7c: another dam's point this near the found line owns it
OTHER_H_FIT = (0.8, 1.25)             # G7c: ... or nearer than this point and of its height
CREST_LENGTH_RATIO = (None, 3.0)      # G7d: found line / inventory crest length (no lower
#                                       bound: the line found is often only the part between
#                                       the pond and the low ground; validation 2026-11-17)
CLOSED_GAP_FRAC = 0.25    # G4c: end gap under this share of the length is a closed line
CLOSED_TURN_DEG = 270.0   # G4c: net turning (after Douglas-Peucker) over this is closed
# G1 / G7b: a closed basin is a depression deeper than max(0.3 m, 0.1 H) of at least
# WATER_MIN_AC (a dry or drained reservoir; a pit or a plunge pool is smaller)
CLOSED_CREST_H = 0.2      # G1: the low side's closed basin fills to within this x H of the crest
FAR_WARN_M = 50.0         # no trigger, a clean embankment on flat water this far: REVIEW
DOUBTFUL_FAR_M = 100.0    # G1 failed, embankment this far, only A2 / A3: a log note only

MOVED_PREFIX = "DAM POINT MOVED"
REVIEW_PREFIX = "REVIEW: Dam point check"
FOUND_LINE_SOURCE = "dam line found on the DEM (dam point check)"
FOUND_LINE_FILE = "dam_line_found.geojson"
RESAMPLING_AVERAGE = ("average: the DEM read at 2 m with GDAL average resampling (whatever the "
                      "run's DEM resampling), 4 m = 2 x 2 block means of it")
RESAMPLING_SOURCE = ("the terrain source's own read at 2 m (no average warp available), 4 m = "
                     "2 x 2 block means of it")

_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


# ======================================================================== settings ========
def normalize_mode(value, default=DEFAULT_MODE):
    """off | warn | move from a setting value: True = move, False = off, None / "" = default;
    on / yes / 1 / true = move; off / no / 0 / false / none = off. ValueError otherwise."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if value is True:
        return "move"
    if value is False:
        return "off"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if int(value) in (0, 1, 2) and float(value) == int(value):
            return MODES[int(value)]
    v = str(value).strip().lower()
    if v in MODES:
        return v
    if v in ("on", "yes", "true", "1", "auto"):
        return "move"
    if v in ("no", "false", "0", "none", "disabled"):
        return "off"
    if v in ("review", "warn only", "warn-only", "check"):
        return "warn"
    raise ValueError("dam point check '{}': use off, warn or move".format(value))


def env_mode(environ=None):
    """The NDBREACH_DAM_POINT_CHECK (or DAM_POINT_CHECK) override: off / 0 / no / false = off,
    warn, move / on / 1 / yes / true = move; None when unset or not understood."""
    e = os.environ if environ is None else environ
    v = e.get(ENV) or e.get("DAM_POINT_CHECK")
    if v is None or not str(v).strip():
        return None
    try:
        return normalize_mode(v, None)
    except ValueError:
        return None


# ======================================================================== when ============
def should_check(dam, st, inflow_xy, native_cell, mode=None):
    """(True, None) or (False, reason). Precedence as the design's section 3 (row 3, a reused
    result on the dam, is the caller's: it comes before row 4 because a move sets the dam
    line). Row 8: a top of dam the run filled from the DEM (dam._top_from_dem: the single-dam
    tool's and the cascade's fills) does not count as given. `mode`: the effective mode (the
    environment override already applied), else st.dam_point_check."""
    mode = normalize_mode(getattr(st, "dam_point_check", DEFAULT_MODE) if mode is None else mode)
    if mode == "off":
        return False, "off"
    if getattr(st, "proposed", None):
        return False, "proposed dam"
    if getattr(st, "fixed_plan", None) is not None:
        return False, "fixed plan (sensitivity, nested or refine rerun)"
    if getattr(dam, "crest_line_xy", None):
        return False, "dam line given"
    if getattr(st, "midcrest_line", None):
        return False, "crest line given"
    if getattr(st, "breach_xy", None) is not None:
        return False, "breach position given"
    if inflow_xy is not None:
        return False, "breach inflow point given"
    if getattr(dam, "top_of_dam_ft", None) is not None and not getattr(dam, "_top_from_dem",
                                                                        False):
        return False, "top of dam given"
    if native_cell is None:
        return False, "DEM cell unknown"
    if native_cell > MAX_NATIVE_CELL_M:
        return False, "DEM coarser than {:g} m ({:.1f} m; needs LiDAR)".format(
            MAX_NATIVE_CELL_M, native_cell)
    h = getattr(dam, "dam_height_ft", None)
    try:
        if h is None or not float(h) > 0:
            return False, "no dam height"
    except (TypeError, ValueError):
        return False, "no dam height"
    return True, None


# ======================================================================== reading =========
def average_reader(terrain):
    """(read(bounds, cell) -> Grid, label): the terrain source's average read (GdalTerrain.
    read_average, found through the wrappers' .base), else its own read."""
    t = terrain
    for _i in range(6):
        if t is None:
            break
        fn = getattr(t, "read_average", None)
        if callable(fn):
            return fn, RESAMPLING_AVERAGE
        t = getattr(t, "base", None)
    return terrain.read, RESAMPLING_SOURCE


def read_bounds(dam_xy, half=READ_HALF_M):
    """The read window: snapped to whole 4 m cells, so the same point always reads the same
    cells (the run and preview agree)."""
    c = CELL_EMB_M
    n = int(math.ceil(2.0 * half / c))
    x0 = math.floor((dam_xy[0] - half) / c) * c
    y1 = math.ceil((dam_xy[1] + half) / c) * c
    return (x0, y1 - n * c, x0 + n * c, y1)


def block_mean(g2, k=2):
    """The k x k block mean of a Grid (NaN only where the whole block is NaN)."""
    from .grid import Grid
    a = np.asarray(g2.masked(), dtype=float)
    ny, nx = a.shape[0] // k * k, a.shape[1] // k * k
    b = a[:ny, :nx].reshape(ny // k, k, nx // k, k)
    ok = np.isfinite(b)
    n = ok.sum(axis=(1, 3))
    s = np.where(ok, b, 0.0).sum(axis=(1, 3))
    out = np.where(n > 0, s / np.maximum(n, 1), np.nan)
    return Grid(out, g2.x0, g2.y0, g2.dx * k)


def read_grids(terrain, dam_xy, half=READ_HALF_M):
    """(g2, g4, resampling text): the DEM around the point at 2 m (average) and 4 m."""
    read, how = average_reader(terrain)
    g2 = read(read_bounds(dam_xy, half), CELL_WATER_M)
    from .grid import Grid
    g2 = Grid(np.asarray(g2.masked(), dtype=float), g2.x0, g2.y0, g2.dx)
    return g2, block_mean(g2, int(round(CELL_EMB_M / CELL_WATER_M))), how


def _zat(g, x, y):
    r, c = g.rc(x, y)
    if not g.inside(r, c):
        return float("nan")
    return float(g.data[r, c])


# ======================================================================== water ===========
def water_bodies(g2):
    """Hydro-flattened water: connected cells whose 3x3 range is <= FLAT_RANGE_M, area >=
    WATER_MIN_AC, spread <= WATER_SPREAD_M, grown through cells at the same level (3 cm).
    Returns (label array, [{id, wse_m, area_ac, cx, cy}])."""
    from scipy.ndimage import maximum_filter, minimum_filter, label
    z = g2.masked()
    zf = np.where(np.isfinite(z), z, -1e6)
    rng = maximum_filter(zf, 3) - minimum_filter(zf, 3)
    flat = np.isfinite(z) & (rng <= FLAT_RANGE_M)
    lab, n = label(flat, structure=np.ones((3, 3), bool))
    if n == 0:
        return np.zeros(z.shape, int), []
    counts = np.bincount(lab.ravel())
    cell_a = g2.dx * g2.dx
    keep = np.flatnonzero(counts * cell_a >= WATER_MIN_AC * ACRE)
    keep = keep[keep > 0]
    raw = []
    for lid in keep:
        m = lab == lid
        vals = z[m]
        p2, p98 = np.percentile(vals, [2, 98])
        if p98 - p2 > WATER_SPREAD_M:
            continue
        raw.append((int(m.sum()), float(np.median(vals)), m))
    raw.sort(key=lambda t: -t[0])
    out = np.zeros(z.shape, int)
    levels = {}
    k = 0
    for _n, wse, m in raw:
        if (out[m] > 0).mean() > 0.5:
            continue
        same = np.isfinite(z) & (np.abs(z - wse) <= 0.03)
        sl, _ = label(same, structure=np.ones((3, 3), bool))
        ids = np.unique(sl[m])
        grown = np.isin(sl, ids[ids > 0]) | m
        hit = [h for h in np.unique(out[grown]) if h > 0 and abs(levels[h] - wse) <= 0.03]
        if hit:
            lid2 = hit[0]
        else:
            k += 1
            lid2 = k
            levels[lid2] = wse
        out[grown & (out == 0)] = lid2
    bodies = []
    for lid2 in range(1, k + 1):
        gm = out == lid2
        if not gm.any():
            continue
        rr, cc = np.nonzero(gm)
        bodies.append({"id": lid2, "wse_m": levels[lid2],
                       "area_ac": float(gm.sum() * cell_a / ACRE),
                       "cx": float(g2.x0 + (cc.mean() + 0.5) * g2.dx),
                       "cy": float(g2.y0 - (rr.mean() + 0.5) * g2.dx)})
    return out, bodies


def _edt_m(mask, dx):
    from scipy.ndimage import distance_transform_edt
    if not mask.any():
        return np.full(mask.shape, np.inf)
    return distance_transform_edt(~mask) * dx


def _edt_idx(mask, dx):
    from scipy.ndimage import distance_transform_edt
    d, ind = distance_transform_edt(~mask, return_indices=True)
    return d * dx, ind


def _resample_mask(mask_src, gsrc, gdst):
    """Nearest lookup of a bool mask from grid gsrc onto grid gdst."""
    X, Y = np.meshgrid(gdst.x_centers(), gdst.y_centers())
    r = np.floor((gsrc.y0 - Y) / gsrc.dx).astype(int)
    c = np.floor((X - gsrc.x0) / gsrc.dx).astype(int)
    ok = (r >= 0) & (r < gsrc.ny) & (c >= 0) & (c < gsrc.nx)
    out = np.zeros(X.shape, bool)
    out[ok] = mask_src[r[ok], c[ok]]
    return out


# ======================================================================== the line ========
def _douglas_peucker(pts, tol):
    pts = np.asarray(pts, float)
    if len(pts) <= 2:
        return pts
    a, b = pts[0], pts[-1]
    ab = b - a
    L = math.hypot(ab[0], ab[1])
    if L == 0:
        d = np.hypot(pts[:, 0] - a[0], pts[:, 1] - a[1])
    else:
        d = np.abs(ab[0] * (pts[:, 1] - a[1]) - ab[1] * (pts[:, 0] - a[0])) / L
    k = int(np.argmax(d))
    if d[k] <= tol:
        return np.vstack([a, b])
    left = _douglas_peucker(pts[:k + 1], tol)
    right = _douglas_peucker(pts[k:], tol)
    return np.vstack([left[:-1], right])


def crest_polyline(mid, X4, Y4, dx, tol=CELL_EMB_M):
    """The crest line through the mid-line cells: the cells as a graph (neighbours within
    about 2.9 cells, so the small gaps of a dilated piece are bridged), its longest path from
    two shortest-path passes (any cell -> farthest cell -> farthest again), ordered along that
    path and simplified with Douglas-Peucker at `tol` m. [(x, y)] with >= 2 vertices, or None.
    Replaces the screen's straight 2-point chord (DWR-10553 had 14% of its chord on the crest)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import dijkstra
    rr, cc = np.nonzero(mid)
    n = len(rr)
    if n < 2:
        return None
    pts = np.column_stack([X4[rr, cc], Y4[rr, cc]]).astype(float)
    if n > 4000:                                    # never on a dam; keep it bounded anyway
        step = int(math.ceil(n / 4000.0))
        pts = pts[::step]
        n = len(pts)
    reach = 2.9 * dx
    i_all, j_all, w_all = [], [], []
    for i in range(n):
        d = np.hypot(pts[i + 1:, 0] - pts[i, 0], pts[i + 1:, 1] - pts[i, 1])
        j = np.flatnonzero(d <= reach) + i + 1
        if len(j):
            i_all.append(np.full(len(j), i))
            j_all.append(j)
            w_all.append(d[j - i - 1])
    if not i_all:
        return None
    I, J, W = np.concatenate(i_all), np.concatenate(j_all), np.concatenate(w_all)
    G = coo_matrix((W, (I, J)), shape=(n, n)).tocsr()
    d0 = dijkstra(G, directed=False, indices=0)
    fin = np.isfinite(d0)
    # the largest connected part holds node 0 or not: start from the farthest reachable node
    a = int(np.argmax(np.where(fin, d0, -1)))
    da, pred = dijkstra(G, directed=False, indices=a, return_predecessors=True)
    b = int(np.argmax(np.where(np.isfinite(da), da, -1)))
    path = [b]
    while path[-1] != a and pred[path[-1]] >= 0:
        path.append(int(pred[path[-1]]))
    if len(path) < 2:
        return None
    line = _douglas_peucker(pts[path[::-1]], tol)
    if len(line) < 2 or math.hypot(*(line[-1] - line[0])) == 0 and len(line) == 2:
        return None
    return [(float(p[0]), float(p[1])) for p in line]


def line_length(line):
    p = np.asarray(line, float).reshape(-1, 2)
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) >= 2 else 0.0


def _seg_dist(px, py, line):
    """Distance from points (arrays) to a polyline."""
    p = np.asarray(line, float).reshape(-1, 2)
    best = np.full(np.shape(px), np.inf)
    for (x0, y0), (x1, y1) in zip(p[:-1], p[1:]):
        sx, sy = x1 - x0, y1 - y0
        L2 = sx * sx + sy * sy
        t = np.zeros(np.shape(px)) if L2 == 0 else np.clip(((px - x0) * sx + (py - y0) * sy) / L2,
                                                           0.0, 1.0)
        best = np.minimum(best, np.hypot(px - (x0 + t * sx), py - (y0 + t * sy)))
    return best


def _extended(line, ext):
    """The polyline with its end segments carried on straight by `ext` m."""
    p = np.asarray(line, float).reshape(-1, 2)
    a = p[0] - p[1]
    b = p[-1] - p[-2]
    na, nb = math.hypot(*a) or 1.0, math.hypot(*b) or 1.0
    return np.vstack([p[0] + a / na * ext, p, p[-1] + b / nb * ext])


def sample_line(line, step=CELL_WATER_M):
    """Points every `step` m along a polyline (both ends included)."""
    p = np.asarray(line, float).reshape(-1, 2)
    out = [p[0]]
    for a, b in zip(p[:-1], p[1:]):
        L = math.hypot(*(b - a))
        k = max(1, int(math.ceil(L / step)))
        for i in range(1, k + 1):
            out.append(a + (b - a) * (i / k))
    return np.asarray(out)


def turning_deg(line):
    """|net signed turning| of the line simplified by Douglas-Peucker at max(8 m, 0.04 L), in
    degrees. The summed |turn| of the raw polyline counts zig-zag noise and end hooks (364-1681
    deg on correctly found lines in the 2026-11-17 hold-out); a ring or a U still nets > 270."""
    p = np.asarray(line, float).reshape(-1, 2)
    if len(p) < 3:
        return 0.0
    q = _douglas_peucker(p, max(8.0, 0.04 * line_length(p)))
    if len(q) < 3:
        return 0.0
    v = np.diff(q, axis=0)
    ang = np.arctan2(v[:, 1], v[:, 0])
    d = np.diff(ang)
    d = (d + np.pi) % (2 * np.pi) - np.pi
    return float(abs(np.degrees(d.sum())))


def line_closed(line, mid_pts=None, dx=CELL_EMB_M):
    """(closed, why): a ring dike, a lagoon or an off-channel reservoir. The end gap is under
    CLOSED_GAP_FRAC of the length, the line turns more than CLOSED_TURN_DEG, or the crest
    cells the line leaves out run from one end of it to the other (the other half of a ring:
    the longest path through a ring is only half of it)."""
    L = line_length(line)
    p = np.asarray(line, float).reshape(-1, 2)
    if L <= 0:
        return False, ""
    gap = math.hypot(*(p[-1] - p[0]))
    if gap < CLOSED_GAP_FRAC * L:
        return True, "the line found closes on itself (ends {:.0f} m apart on a {:.0f} m line)" \
            .format(gap, L)
    tr = turning_deg(line)
    if tr > CLOSED_TURN_DEG:
        return True, "the line found turns {:.0f} deg (a ring or a U)".format(tr)
    if mid_pts is not None and len(mid_pts) > 8:
        mp = np.asarray(mid_pts, float)
        d = _seg_dist(mp[:, 0], mp[:, 1], line)
        out = mp[d > 2.5 * dx]
        if len(out) >= 4:
            # do the left-out cells connect the two ends? (graph within 2.9 cells)
            ends = [p[0], p[-1]]
            near = [np.hypot(out[:, 0] - e[0], out[:, 1] - e[1]) <= 4.0 * dx for e in ends]
            if near[0].any() and near[1].any():
                from scipy.sparse import coo_matrix
                from scipy.sparse.csgraph import connected_components
                n = len(out)
                I, J = [], []
                for i in range(n):
                    dd = np.hypot(out[i + 1:, 0] - out[i, 0], out[i + 1:, 1] - out[i, 1])
                    j = np.flatnonzero(dd <= 2.9 * dx) + i + 1
                    I.extend([i] * len(j))
                    J.extend(j.tolist())
                G = coo_matrix((np.ones(len(I)), (I, J)), shape=(n, n))
                _k, lab = connected_components(G, directed=False)
                if set(lab[near[0]]) & set(lab[near[1]]):
                    return True, ("the crest found goes on around from one end of the line to "
                                  "the other (a ring dike or lagoon)")
    return False, ""


def on_crest_parts(line, z_fn, crest_m, tol_m, step=1.0, min_len_m=4.0, min_gap_m=5.0):
    """The parts of `line` whose ground (z_fn(x, y) arrays) is within tol_m of crest_m: what
    the crest raise may burn to the top of dam. A spillway notch or a low saddle on the line
    is left out, so the model does not close it. A dip shorter than min_gap_m (a rut, a
    cattle path, a 2 m DEM pit) is not a gap: trim_parts would cut a model cell out of the
    raise round it. [[(x, y), ...], ...] (each >= 2 points)."""
    pts = sample_line(line, step)
    z = np.asarray(z_fn(pts[:, 0], pts[:, 1]), float)
    ok = np.isfinite(z) & (z >= crest_m - tol_m)
    if min_gap_m and ok.any():
        seg = np.hypot(*np.diff(pts, axis=0).T)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        i, n = 0, len(ok)
        while i < n:
            if ok[i]:
                i += 1
                continue
            j = i
            while j < n and not ok[j]:
                j += 1
            # a dip with crest on both sides, shorter than min_gap_m (crest to crest)
            if i > 0 and j < n and cum[j] - cum[i - 1] <= min_gap_m and \
                    np.isfinite(z[i:j]).all():
                ok[i:j] = True
            i = j
    parts, cur = [], []
    for p, o in zip(pts, ok):
        if o:
            cur.append((float(p[0]), float(p[1])))
        else:
            if len(cur) >= 2 and line_length(cur) >= min_len_m:
                parts.append(cur)
            cur = []
    if len(cur) >= 2 and line_length(cur) >= min_len_m:
        parts.append(cur)
    out = []
    for part in parts:
        simp = _douglas_peucker(np.asarray(part), 0.5)
        out.append([(float(a), float(b)) for a, b in simp])
    return out


def trim_parts(parts, line, half_m, end_m=1.5):
    """The crest-raise parts with each end that faces a gap in the line (a spillway notch, a
    low saddle) cut back by half_m, the half-width the raise burns round a line
    (lisflood.raise_crest: max(12 m, model cell) / 2), so the burn does not reach into the gap.
    The line's own ends are kept. Parts left shorter than 2 m are dropped."""
    if not parts:
        return []
    L = np.asarray(line, float).reshape(-1, 2)
    ends = (L[0], L[-1])

    def at_end(q):
        return min(math.hypot(q[0] - e[0], q[1] - e[1]) for e in ends) <= end_m
    out = []
    for p in parts:
        q = sample_line(p, 0.5)
        cum = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(q, axis=0).T))])
        a, b = 0.0, float(cum[-1])
        if half_m and not at_end(q[0]):
            a += half_m
        if half_m and not at_end(q[-1]):
            b -= half_m
        if b - a < 2.0:
            continue
        keep = q[(cum >= a - 1e-9) & (cum <= b + 1e-9)]
        if len(keep) < 2:
            continue
        simp = _douglas_peucker(keep, 0.5)
        out.append([(float(x), float(y)) for x, y in simp])
    return out


def crest_tol_m(h_m):
    """How far below the crest a cell still counts as crest (G4's on-crest test)."""
    return max(0.6, 0.25 * h_m)


def end_tol_m(h_m):
    """The tighter tolerance of the line's ends (trim and extension) and of the crest raise's
    on-crest parts: max(0.3 m, 0.1 H). A spillway notch is deeper than this."""
    return max(0.3, 0.1 * h_m)


def _bilin(g, x, y):
    return np.asarray(g.sample_bilinear(np.atleast_1d(x), np.atleast_1d(y)), float)


def snap_to_crest(line, g, h_m, spacing=CELL_EMB_M):
    """The mid-line (halfway between the pond and the low ground, which sits on the
    downstream half of the crest or its shoulder) moved onto the crest: every `spacing` m, the
    centre of the highest ground (within 0.15 m of the top) on the perpendicular profile,
    +-max(8 m, H). Simplified with Douglas-Peucker at 2 m."""
    if not line or len(line) < 2:
        return line
    pts = sample_line(line, spacing)
    if len(pts) < 2:
        return line
    R = max(12.0, 1.2 * float(h_m))
    offs = np.arange(-R, R + 1e-6, 1.0)
    out = []
    n = len(pts)
    for i in range(n):
        a, b = pts[max(0, i - 2)], pts[min(n - 1, i + 2)]
        t = b - a
        L = math.hypot(t[0], t[1])
        if L == 0:
            out.append(pts[i])
            continue
        nv = np.array([-t[1], t[0]]) / L
        prof = pts[i][None, :] + offs[:, None] * nv[None, :]
        z = _bilin(g, prof[:, 0], prof[:, 1])
        if not np.isfinite(z).any():
            out.append(pts[i])
            continue
        top = np.nanmax(z)
        sel = np.isfinite(z) & (z >= top - 0.15)
        out.append(pts[i] + float(np.mean(offs[sel])) * nv)
    simp = _douglas_peucker(np.asarray(out), 1.0)
    return [(float(p[0]), float(p[1])) for p in simp]


def _line_z(line, g, q=50):
    """The `q`th percentile of the ground along a line (sampled every 2 m), or None."""
    if not line or len(line) < 2:
        return None
    p = sample_line(line, CELL_WATER_M)
    z = _bilin(g, p[:, 0], p[:, 1])
    z = z[np.isfinite(z)]
    return float(np.percentile(z, q)) if z.size else None


def refine_line(line, g, crest_m, h_m, step=None):
    """The crest line's ends fixed on the DEM `g`: ends that run off the crest (the mid-line
    bends down a face where the pond narrows at an abutment) are trimmed back to the last
    point on the crest (within end_tol_m), then each end is carried on along its own direction
    while the ground stays at crest level, until it climbs into the abutment (higher than the
    crest by more than end_tol_m) or drops off (by more than end_tol_m), at most
    max(30 m, half the line). Returns the line (>= 2 points) or the input."""
    if not line or len(line) < 2:
        return line
    step = float(step or g.dx)
    tol = end_tol_m(h_m)
    pts = sample_line(line, step)
    z = _bilin(g, pts[:, 0], pts[:, 1])
    tt = max(0.2, 0.05 * h_m)              # the core: strictly on the crest
    ok = np.flatnonzero(np.isfinite(z) & (z >= crest_m - tt) & (z <= crest_m + tol))
    if len(ok) < 2:
        return line
    i0, i1 = int(ok[0]), int(ok[-1])
    if (i1 - i0) * step < 0.3 * line_length(line):
        return line                       # mostly off the crest: leave it to the gates
    core = pts[i0:i1 + 1]
    L = line_length(core)
    cap = max(30.0, 0.5 * L)

    offs = np.arange(-4.0, 4.0 + 1e-6, 1.0)

    def extend(end, prev, own):
        v = end - prev
        n = math.hypot(v[0], v[1])
        if n == 0:
            return []
        u = v / n
        out = []
        t = step
        p0 = end
        # the rest of the line (all but the last 20 m at this end): the walk stops when it
        # comes back to it (a ring dike)
        far = core[own]
        while t <= cap:
            p = p0 + u * step
            nv = np.array([-u[1], u[0]])
            prof = p[None, :] + offs[:, None] * nv[None, :]
            zp = _bilin(g, prof[:, 0], prof[:, 1])
            if np.isfinite(zp).any():           # stay on the crest's centre as it turns
                sel = np.isfinite(zp) & (zp >= np.nanmax(zp) - 0.15)
                p = p + float(np.mean(offs[sel])) * nv
            zz = float(_bilin(g, p[0], p[1])[0])
            if not math.isfinite(zz) or zz < crest_m - tol or zz > crest_m + tol:
                break
            if len(far) and float(np.min(np.hypot(far[:, 0] - p[0], far[:, 1] - p[1]))) \
                    < 3.0 * step:
                break
            w = p - p0
            nw = math.hypot(w[0], w[1])
            if nw > 0:
                u = 0.7 * u + 0.3 * (w / nw)
                u = u / (math.hypot(u[0], u[1]) or 1.0)
            out.append(p)
            p0 = p
            t += step
        return out
    # the direction of each end over its last quarter (at least 20 m)
    k = max(1, int(round(max(20.0, 0.25 * line_length(core)) / step)))
    m = max(1, int(round(20.0 / step)))
    idx = np.arange(len(core))
    head = extend(core[0], core[min(k, len(core) - 1)], idx > m)
    tail = extend(core[-1], core[max(0, len(core) - 1 - k)], idx < len(core) - 1 - m)
    if head and tail and float(np.min(np.hypot(*(np.asarray(head)[:, None, :] -
                                                  np.asarray(tail)[None, :, :]).T))) < 3 * step:
        tail = []                          # the two ends met (a ring): keep one walk only
    full = np.vstack([np.asarray(head[::-1]).reshape(-1, 2), core,
                      np.asarray(tail).reshape(-1, 2)])
    simp = _douglas_peucker(full, 1.0)
    return [(float(a), float(b)) for a, b in simp]


def line_quality(line, g2, wlab, crest_m, level_m, h_m, mid_pts=None):
    """G4 on a polyline: sampled every 2 m. on_crest (z >= crest - max(0.6 m, 0.25 H)),
    on_water (flat water), below_pool (z < pond level - 0.1 m), perp95_m (the 95th percentile
    distance of the crest cells from the line) and length_m."""
    pts = sample_line(line, CELL_WATER_M)
    z = np.array([_zat(g2, x, y) for x, y in pts])
    fin = np.isfinite(z)
    n = max(1, int(fin.sum()))
    on_w = 0
    for (x, y) in pts:
        r, c = g2.rc(x, y)
        if g2.inside(r, c) and wlab[r, c] > 0:
            on_w += 1
    q = {"length_m": line_length(line),
         "on_crest": float((fin & (z >= crest_m - crest_tol_m(h_m))).sum()) / n,
         "on_water": float(on_w) / max(1, len(pts)),
         "below_pool": (float((fin & (z < level_m - 0.1)).sum()) / n
                        if level_m is not None else 0.0)}
    if mid_pts is not None and len(mid_pts):
        mp = np.asarray(mid_pts, float)
        d = _seg_dist(mp[:, 0], mp[:, 1], line)
        # crest cells past the line's ends (where the mid-line bends down a face at an
        # abutment, trimmed by refine_line) are not "off the line"
        de = _seg_dist(mp[:, 0], mp[:, 1], _extended(line, 2000.0))
        # and only the mid-line cells at crest level (the mid-line runs down a face where the
        # pond narrows at an abutment: those cells are not crest)
        zm = _bilin(g2, mp[:, 0], mp[:, 1])
        keep = (de >= d - CELL_EMB_M) & np.isfinite(zm) & (zm >= crest_m - end_tol_m(h_m))
        q["perp95_m"] = float(np.percentile(d[keep], 95)) if keep.any() else 0.0
    else:
        q["perp95_m"] = 0.0
    return q


# ======================================================================== embankment ======
def emb_score(e):
    """Lower is better: |ln(height / H)| (0.7 when the low side is water), the distance from
    the point / 300 m, +1 when not confirmed."""
    sc = (0.7 if e["ds_water"] else abs(math.log(max(e["height_ratio"], 0.05)))) + \
        e["point_to_crest_m"] / 300.0
    if not e["confirmed"]:
        sc += 1.0
    return sc


def emb_for_pool(g4, z4, X4, Y4, P, level, h_m, dam_xy, lowwater=None, basin=None):
    """The embankment between pool P (at `level`) and the low ground next to it; every
    qualifying low area is tried and ranked with emb_score. A dict or None."""
    from scipy.ndimage import label
    thr = max(0.6, 0.25 * h_m)
    D = max(60.0, 6.0 * h_m)
    gap = max(40.0, 8.0 * h_m)
    dP, iP = _edt_idx(P, g4.dx)
    fin = np.isfinite(z4)
    Lmask = ~P & fin & (z4 < level - thr) & (dP <= D)
    if lowwater is not None:
        Lmask |= ~P & lowwater & (dP <= D)
    lab, n = label(Lmask, structure=np.ones((3, 3), bool))
    out = []
    for lid in range(1, n + 1):
        m = lab == lid
        if m.sum() < 4:
            continue
        w = float(dP[m].min())
        wet = lowwater is not None and lowwater[m].mean() > 0.5
        if w > (max(gap, 100.0) if wet else gap):
            continue
        e = _emb_from_low(g4, z4, X4, Y4, P, dP, iP, m, w, level, h_m, dam_xy, lowwater, gap,
                          basin)
        if e is not None:
            e["score"] = emb_score(e)
            out.append(e)
    if not out:
        return None
    out.sort(key=lambda e: e["score"])
    out[0]["n_low_areas"] = len(out)
    return out[0]


def _emb_from_low(g4, z4, X4, Y4, P, dP, iP, Lc, w, level, h_m, dam_xy, lowwater, gap,
                  basin=None):
    from scipy.ndimage import label, binary_dilation
    x, y = dam_xy
    fin = np.isfinite(z4)
    dL, iL = _edt_idx(Lc, g4.dx)
    wet = lowwater is not None and lowwater[Lc].mean() > 0.5
    gap_used = max(gap, 100.0) if wet else gap
    mid = ~P & ~Lc & fin & (np.abs(dP - dL) <= g4.dx * 1.01) & (dP + dL <= gap_used) & \
        (z4 >= level - 0.1)
    if mid.sum() < 3:
        return None
    k = np.flatnonzero((Lc & (dP <= w + 1e-6)).ravel())[0]
    tx, ty = X4.ravel()[k], Y4.ravel()[k]
    mlab, mn = label(binary_dilation(mid, np.ones((3, 3), bool), iterations=2),
                     structure=np.ones((3, 3), bool))
    if mn > 1:
        dm = []
        for j in range(1, mn + 1):
            mm = mid & (mlab == j)
            if mm.any():
                dm.append((float(np.min(np.hypot(X4[mm] - tx, Y4[mm] - ty))), j))
        dm.sort()
        mid &= mlab == dm[0][1]
    if mid.sum() < 3:
        return None
    zm = z4[mid]
    crest_z = float(np.percentile(zm, 75))
    rr, cc = np.nonzero(mid)
    pts = np.column_stack([X4[rr, cc], Y4[rr, cc]])
    pr, pc = iP[0][rr, cc], iP[1][rr, cc]
    lr, lc = iL[0][rr, cc], iL[1][rr, cc]
    v = np.column_stack([X4[lr, lc] - X4[pr, pc], Y4[lr, lc] - Y4[pr, pc]])
    nv = np.hypot(v[:, 0], v[:, 1])
    nv[nv == 0] = np.nan
    mid_ds = v / nv[:, None]
    cen = pts.mean(axis=0)
    kk = int(np.argmin(np.hypot(pts[:, 0] - cen[0], pts[:, 1] - cen[1])))
    cx, cy = float(pts[kk, 0]), float(pts[kk, 1])
    ds_dir = tuple(float(t) for t in mid_ds[kk]) if np.all(np.isfinite(mid_ds[kk])) else None
    line = crest_polyline(mid, X4, Y4, g4.dx)
    dmid = _edt_m(mid, g4.dx)
    near_low = Lc & (dmid <= gap)
    if not near_low.any():
        near_low = Lc
    low_z = float(np.nanmin(z4[near_low]))
    dpts = np.hypot(pts[:, 0] - x, pts[:, 1] - y)
    hgt = crest_z - low_z
    ratio = hgt / h_m if h_m > 0 else float("nan")
    ds_water = bool(lowwater is not None and lowwater[near_low].mean() > 0.5)
    # the lowest ground below: is it a closed basin that fills to within CLOSED_CREST_H x H
    # of this crest (a reservoir held by this very embankment)? Then the "pond" is the next
    # dam's backwater against this dam's downstream face, and its dry reservoir looks like
    # the low side (review 2026-11-17). A road-culvert hollow or a dugout below a dam spills
    # lower than that (DWR-2760, 2662, 4441, 10554: 0.27-0.33 H below the crest).
    zl = np.where(near_low & np.isfinite(z4), z4, np.inf)
    low_rc = np.unravel_index(int(np.argmin(zl)), zl.shape)
    ds_closed = False
    if basin is not None and basin[0][low_rc]:
        spill = float(z4[low_rc] + basin[1][low_rc])
        ds_closed = bool(spill >= crest_z - CLOSED_CREST_H * h_m)
    conf = False if ds_water else bool(0.6 <= ratio <= 1.8 and crest_z - level >= 0.3)
    return {"crest_z_m": crest_z, "crest_z90_m": float(np.percentile(zm, 90)),
            "crest_xy": (cx, cy), "line": line,
            "length_m": line_length(line) if line else 0.0,
            "mid": mid, "mid_pts": pts, "mid_ds": mid_ds, "low_z_m": low_z, "ds_dir": ds_dir,
            "rise_above_level_m": crest_z - level, "height_over_low_m": hgt,
            "height_ratio": ratio, "barrier_m": w * 2.0,
            "point_to_crest_m": float(dpts.min()), "gap_m": gap, "ds_water": ds_water,
            "ds_closed": ds_closed, "low_rc": (int(low_rc[0]), int(low_rc[1])),
            "ridge_only": False, "confirmed": conf}


def ridge_near_point(g4, z4, X4, Y4, h_m, dam_xy, radius=250.0):
    """A raised band (white top-hat with a square of side 2 R, R = max(30 m, 4 H)) at least
    0.5 H high, longer than max(40 m, 4 H) and twice its width, within `radius` of the point.
    A dict like _emb_from_low's (ridge_only True), or None."""
    from scipy.ndimage import grey_opening, label
    R = max(30.0, 4.0 * h_m)
    k = int(2 * math.ceil(R / g4.dx) + 1)
    if not np.isfinite(z4).any():
        return None
    zf = np.where(np.isfinite(z4), z4, np.nanmin(z4))
    th = zf - grey_opening(zf, size=(k, k))
    x, y = dam_xy
    d = np.hypot(X4 - x, Y4 - y)
    rid = (th >= 0.5 * h_m) & (d <= radius + R)
    lab, n = label(rid, structure=np.ones((3, 3), bool))
    best = None
    for lid in range(1, n + 1):
        m = lab == lid
        if m.sum() < 5:
            continue
        pts = np.column_stack([X4[m], Y4[m]])
        cen = pts.mean(axis=0)
        _u, _s, vt = np.linalg.svd(pts - cen, full_matrices=False)
        tt = (pts - cen) @ vt[0]
        ww = (pts - cen) @ vt[1]
        L = float(tt.max() - tt.min())
        W = float(ww.max() - ww.min()) + g4.dx
        if L < max(40.0, 4.0 * h_m) or L < 2.0 * W:
            continue
        dmin = float(d[m].min())
        if dmin > radius:
            continue
        if best is None or dmin < best[0]:
            best = (dmin, m, pts)
    if best is None:
        return None
    dmin, m, pts = best
    zz = z4[m]
    top = zz >= np.percentile(zz, 80)
    cp = pts[top]
    crest_z = float(np.percentile(zz, 90))
    cc = cp.mean(axis=0)
    kk = int(np.argmin(np.hypot(cp[:, 0] - cc[0], cp[:, 1] - cc[1])))
    cx, cy = float(cp[kk, 0]), float(cp[kk, 1])
    line = crest_polyline(m & (z4 >= np.percentile(zz, 80)), X4, Y4, g4.dx) or \
        crest_polyline(m, X4, Y4, g4.dx)
    near = np.hypot(X4 - cx, Y4 - cy) <= 2 * R
    low_z = float(np.nanmin(z4[near]))
    hgt = crest_z - low_z
    Lr = line_length(line) if line else 0.0
    conf = bool(0.6 <= hgt / h_m <= 1.8 and Lr > max(60.0, 4.0 * h_m))
    return {"crest_z_m": crest_z, "crest_z90_m": crest_z, "crest_xy": (cx, cy), "line": line,
            "length_m": Lr, "mid": m, "mid_pts": pts, "mid_ds": None, "low_z_m": low_z,
            "ds_dir": None, "rise_above_level_m": float("nan"), "height_over_low_m": hgt,
            "height_ratio": hgt / h_m, "barrier_m": float("nan"), "point_to_crest_m": dmin,
            "gap_m": 2 * R, "ds_water": False, "ridge_only": True, "confirmed": conf}


def fill_across_water(g2, wlab, bodies, dam_xy, h_m):
    """Is the point on a raised fill with water AT ONE LEVEL on both sides (a road fill over
    backwater, DWR-3161)? 12 lines through the point, out to max(80 m, 4 H) each way. A dict
    (width_m, level_m, rise_m) or None."""
    x, y = dam_xy
    zp = _zat(g2, x, y)
    rr, cc = g2.rc(x, y)
    if not np.isfinite(zp) or (g2.inside(rr, cc) and wlab[rr, cc] > 0):
        return None
    lev = {b["id"]: b["wse_m"] for b in bodies}
    R = max(80.0, 4.0 * h_m)
    steps = np.arange(g2.dx, R + 1e-6, g2.dx)

    def first(ux, uy):
        for t in steps:
            r, c = g2.rc(x + ux * t, y + uy * t)
            if not g2.inside(r, c):
                return None
            if wlab[r, c] > 0:
                return int(wlab[r, c]), float(t)
        return None
    best = None
    for a in np.radians(np.arange(0, 180, 15)):
        ux, uy = math.sin(a), math.cos(a)
        f1, f2 = first(ux, uy), first(-ux, -uy)
        if f1 is None or f2 is None:
            continue
        l1, l2 = lev[f1[0]], lev[f2[0]]
        if abs(l1 - l2) > 0.1 or zp <= max(l1, l2):
            continue
        w = f1[1] + f2[1]
        if best is None or w < best["width_m"]:
            best = {"width_m": w, "level_m": 0.5 * (l1 + l2), "rise_m": zp - max(l1, l2)}
    return best


def _nearest_on_line(line, xy):
    """The point of a polyline nearest xy."""
    p = np.asarray(line, float).reshape(-1, 2)
    best, bp = np.inf, p[0]
    for a, b in zip(p[:-1], p[1:]):
        s = b - a
        L2 = float(s @ s)
        t = 0.0 if L2 == 0 else min(1.0, max(0.0, float((np.asarray(xy) - a) @ s) / L2))
        q = a + t * s
        d = math.hypot(xy[0] - q[0], xy[1] - q[1])
        if d < best:
            best, bp = d, q
    return bp


def _local_ds(emb, xy):
    """(signed distance from the crest line along the local downstream direction there, that
    direction) or (None, None): positive is the low (downstream) side, negative the pond side.
    The direction is the one of the crest cell nearest the line point nearest xy."""
    if not emb or emb.get("mid_ds") is None:
        return None, None
    mp, md = emb["mid_pts"], emb["mid_ds"]
    line = emb.get("line")
    base = _nearest_on_line(line, xy) if line and len(line) >= 2 else None
    ref = xy if base is None else base
    j = int(np.argmin(np.hypot(mp[:, 0] - ref[0], mp[:, 1] - ref[1])))
    if not np.all(np.isfinite(md[j])):
        return None, None
    if base is None:
        base = mp[j]
    s = (xy[0] - base[0]) * md[j][0] + (xy[1] - base[1]) * md[j][1]
    return float(s), (float(md[j][0]), float(md[j][1]))


def _ridge_ds(emb, wlab, bodies, g2, g4, X4, Y4, h_m):
    """Critique 3: a ridge found with no pond held behind it (ridge only) but flat water beside
    it gets a downstream direction, away from the water, so the A3 test can still tell an
    inflow on the pond side (the upstream face). In place; nothing when no water is near."""
    ids = [b["id"] for b in bodies if b.get("edge_dist_m", np.inf) <= SEARCH_POOL_M]
    if not ids or emb.get("mid") is None:
        return
    W = _resample_mask(np.isin(wlab, ids), g2, g4)
    if not W.any():
        return
    dW, iW = _edt_idx(W, g4.dx)
    rr, cc = np.nonzero(emb["mid"])
    if not rr.size or float(np.min(dW[rr, cc])) > max(60.0, 6.0 * h_m):
        return
    wx, wy = X4[iW[0][rr, cc], iW[1][rr, cc]], Y4[iW[0][rr, cc], iW[1][rr, cc]]
    pts = np.column_stack([X4[rr, cc], Y4[rr, cc]])
    v = pts - np.column_stack([wx, wy])
    nv = np.hypot(v[:, 0], v[:, 1])
    nv[nv == 0] = np.nan
    emb["mid_pts"], emb["mid_ds"] = pts, v / nv[:, None]
    cx, cy = emb["crest_xy"]
    k = int(np.argmin(np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)))
    if np.all(np.isfinite(emb["mid_ds"][k])):
        emb["ds_dir"] = tuple(float(t) for t in emb["mid_ds"][k])
    emb["ds_from_water"] = True


def survey(g2, g4, dam_xy, h_m, sa_ac=None):
    """What the DEM shows around the point (screen_points.independent without the planning
    trace): the water, the pond the embankment holds, the embankment as a polyline, its line
    quality, whether the point is on water, on another ridge or on the low side, and a fill
    across water. A dict; its arrays are left out by public()."""
    from scipy.ndimage import label, binary_dilation
    from .terrain import priority_flood
    x, y = dam_xy
    sv = {"h_m": h_m, "dam_xy": (float(x), float(y))}
    wlab, bodies = water_bodies(g2)
    sv["wlab"], sv["bodies"], sv["g2"], sv["g4"] = wlab, bodies, g2, g4
    X2, Y2 = np.meshgrid(g2.x_centers(), g2.y_centers())
    for b in bodies:
        m = wlab == b["id"]
        b["edge_dist_m"] = float(np.hypot(X2[m] - x, Y2[m] - y).min())
    sv["water_found_1000m"] = any(b["edge_dist_m"] <= WATER_FOUND_M for b in bodies)
    rr, cc = g2.rc(x, y)
    sv["point_on_water"] = bool(g2.inside(rr, cc) and wlab[rr, cc] > 0)
    sv["point_wid"] = int(wlab[rr, cc]) if sv["point_on_water"] else None
    sv["point_z_m"] = _zat(g2, x, y)
    sv["point_fill"] = fill_across_water(g2, wlab, bodies, dam_xy, h_m)

    z4 = g4.masked()
    filled = priority_flood(z4)
    fill = filled - z4
    X4, Y4 = np.meshgrid(g4.x_centers(), g4.y_centers())
    sv["X4"], sv["Y4"] = X4, Y4
    dpt = np.hypot(X4 - x, Y4 - y)
    cell4 = g4.dx * g4.dx
    cands = []
    for b in bodies:
        if b["edge_dist_m"] > SEARCH_POOL_M:
            continue
        P = _resample_mask(wlab == b["id"], g2, g4)
        P = (binary_dilation(P, np.ones((3, 3), bool)) & (np.abs(z4 - b["wse_m"]) <= 0.15)) | P
        cands.append({"kind": "water", "P": P, "level": b["wse_m"], "area_ac": b["area_ac"],
                      "edge_dist_m": b["edge_dist_m"], "wid": b["id"]})
    thr = max(0.3, 0.1 * h_m)
    dep = np.isfinite(fill) & (fill > thr)
    lab, n = label(dep, structure=np.ones((3, 3), bool))
    basin = np.zeros(dep.shape, bool)          # the closed basins of WATER_MIN_AC or more
    if n:
        cnt = np.bincount(lab.ravel())
        big = np.flatnonzero(cnt * cell4 >= WATER_MIN_AC * ACRE)
        basin = np.isin(lab, big[big > 0])
    r4, c4 = g4.rc(x, y)
    sv["point_in_basin"] = bool(g4.inside(r4, c4) and basin[r4, c4])
    if n:
        for lid in np.flatnonzero(cnt * cell4 >= WATER_MIN_AC * ACRE):
            if lid == 0:
                continue
            m = lab == lid
            ed = float(dpt[m].min())
            if ed > SEARCH_POOL_M:
                continue
            cands.append({"kind": "dry (closed basin)", "P": m,
                          "level": float(np.nanmedian(filled[m])),
                          "area_ac": float(m.sum() * cell4 / ACRE), "edge_dist_m": ed})
    for cd in cands:
        low_w = None
        if cd["kind"] == "water":
            lw2 = np.zeros(wlab.shape, bool)
            for b in bodies:
                if b["id"] != cd["wid"] and b["wse_m"] < cd["level"] - 0.1:
                    lw2 |= wlab == b["id"]
            low_w = _resample_mask(lw2, g2, g4) if lw2.any() else None
        cd["emb"] = emb_for_pool(g4, z4, X4, Y4, cd["P"], cd["level"], h_m, dam_xy, low_w,
                                 (basin & ~cd["P"], fill))
        e = cd["emb"]
        if e is None:
            cd["score"] = 50.0 + cd["edge_dist_m"] / 100.0
            continue
        sc = emb_score(e)
        if cd["kind"] != "water":
            sc += 0.15
        if sa_ac and sa_ac > 0 and not (0.03 <= cd["area_ac"] / sa_ac <= 15.0):
            sc += 1.0
        cd["score"] = sc
    cands.sort(key=lambda c: c["score"])
    sv["n_cands"] = len(cands)
    sv["n_water_400"] = sum(1 for c in cands if c["kind"] == "water")
    pick = cands[0] if cands else None
    if pick is not None:
        sv.update(pool_kind=pick["kind"], P=pick["P"], level_m=pick["level"],
                  pool_area_ac=pick["area_ac"], pool_edge_m=pick["edge_dist_m"],
                  pool_wid=pick.get("wid"), emb=pick["emb"])
    else:
        sv.update(pool_kind="dry (no pool or closed basin within 400 m)", P=None, level_m=None,
                  pool_area_ac=None, pool_edge_m=None, pool_wid=None, emb=None)
    if sv["emb"] is None:
        r_ = ridge_near_point(g4, z4, X4, Y4, h_m, dam_xy)
        if r_ is not None:
            _ridge_ds(r_, wlab, bodies, g2, g4, X4, Y4, h_m)
            sv["emb"] = r_
            sv["pool_kind"] += "; embankment ridge only"
    emb = sv["emb"]
    sv["line_q"] = None
    sv["closed"] = (False, "")
    sv["point_side_m"] = None
    sv["emb_ds_at_point"] = None
    sv["point_ridge"] = None
    if emb is not None:
        if emb.get("line"):
            emb["line_raw"] = emb["line"]
            emb["crest_mid_p75_m"] = emb["crest_z_m"]
            # twice: the second pass profiles square to the snapped line, not the raw mid-line
            snapped = snap_to_crest(snap_to_crest(emb["line"], g2, h_m), g2, h_m)
            c0 = _line_z(snapped, g2)
            line = refine_line(snapped, g2, c0 if c0 is not None else emb["crest_z_m"], h_m)
            c1 = _line_z(line, g2)
            emb["line"] = line
            if c1 is not None:
                emb["crest_z_m"] = c1
                emb["height_over_low_m"] = c1 - emb["low_z_m"]
                emb["height_ratio"] = emb["height_over_low_m"] / h_m
                if not emb.get("ridge_only") and not emb["ds_water"]:
                    emb["confirmed"] = bool(0.6 <= emb["height_ratio"] <= 1.8 and
                                            c1 - (sv["level_m"] or -1e9) >= 0.3)
            emb["length_m"] = line_length(emb["line"])
            lvl = sv["level_m"] if not emb.get("ridge_only") else None
            sv["line_q"] = line_quality(emb["line"], g2, wlab, emb["crest_z_m"], lvl, h_m,
                                        emb["mid_pts"])
            sv["closed"] = line_closed(emb["line"], emb["mid_pts"], g4.dx)
        s, dsp = _local_ds(emb, dam_xy)
        sv["point_side_m"], sv["emb_ds_at_point"] = s, dsp
        sv["pond_between"] = _pond_between(sv, emb, dam_xy)
        # critique 2: another embankment ridge (>= 0.5 H) at the point, not the one found
        rp = ridge_near_point(g4, z4, X4, Y4, h_m, dam_xy, radius=POINT_RIDGE_M)
        if rp is not None and not emb.get("ridge_only"):
            mp = np.asarray(emb["mid_pts"], float)
            rpts = np.asarray(rp["mid_pts"], float)
            dd = min(float(np.min(np.hypot(mp[:, 0] - q[0], mp[:, 1] - q[1]))) for q in
                     rpts[::max(1, len(rpts) // 200)])
            if dd > 3.0 * g4.dx:
                sv["point_ridge"] = {"height_m": rp["height_over_low_m"],
                                     "length_m": rp["length_m"],
                                     "distance_m": rp["point_to_crest_m"]}
    return sv


def _pond_between(sv, emb, xy):
    """The share of the way from the point to the nearest point of the found crest line that
    is the pond (the 4 m mask), the first and last 10 m left out; 0 when the point is on the
    pond itself or there is no line / pond."""
    P = sv.get("P")
    line = emb.get("line")
    if P is None or not line or len(line) < 2:
        return 0.0
    g4 = sv["g4"]
    r, c = g4.rc(*xy)
    if g4.inside(r, c) and P[r, c]:
        return 0.0
    if sv.get("point_on_water") and sv.get("point_wid") == sv.get("pool_wid"):
        return 0.0
    b = _nearest_on_line(line, xy)
    L = math.hypot(xy[0] - b[0], xy[1] - b[1])
    if L <= 20.0:
        return 0.0
    t = np.arange(10.0, L - 10.0 + 1e-6, 2.0) / L
    px, py = xy[0] + (b[0] - xy[0]) * t, xy[1] + (b[1] - xy[1]) * t
    n = 0
    for x, y in zip(px, py):
        rr, cc = g4.rc(x, y)
        if g4.inside(rr, cc) and P[rr, cc]:
            n += 1
    return float(n) / max(1, len(t))


def inflow_facts(sv, inflow_xy):
    """The replay's inflow against the water, the pond and the found crest: on_water, its
    level, on_pool_water (the chosen pond's body or water within 0.15 m of its level; None when
    no pond), in_pool (the 4 m pond mask), side_m (signed, along the local downstream direction
    from the nearest crest cell: negative is the pond side) and upstream (side_m < -A3_SIDE_M)."""
    f = {"on_water": False, "water_wse_m": None, "on_pool_water": None, "in_pool": False,
         "side_m": None, "upstream": False}
    if inflow_xy is None:
        return f
    g2, wl = sv["g2"], sv["wlab"]
    ix, iy = inflow_xy
    r, c = g2.rc(ix, iy)
    lid = 0
    if g2.inside(r, c):
        win = wl[max(0, r - 1):r + 2, max(0, c - 1):c + 2]
        ids = win[win > 0]
        lid = int(np.bincount(ids).argmax()) if ids.size else 0
    if lid:
        b = [bb for bb in sv["bodies"] if bb["id"] == lid][0]
        f["on_water"], f["water_wse_m"] = True, b["wse_m"]
        lvl = sv.get("level_m")
        if lvl is not None:
            f["on_pool_water"] = bool(lid == sv.get("pool_wid") or abs(b["wse_m"] - lvl) <= 0.15)
    P = sv.get("P")
    if P is not None:
        g4 = sv["g4"]
        r4, c4 = g4.rc(ix, iy)
        f["in_pool"] = bool(g4.inside(r4, c4) and P[r4, c4])
    s, _d = _local_ds(sv.get("emb"), inflow_xy)
    if s is not None:
        f["side_m"] = s
        f["upstream"] = bool(s < -A3_SIDE_M)
    return f


# ======================================================================== decision ========
def classify(sv, rp, h_ft):
    """OK / CHECK / LIKELY MISPLACED and the plain reasons (the screen's classify, without the
    planning trace). `rp` is the replay (top_m, inflow_xy, inflow_why, method, drop_m, drop_h,
    direction); sv["inflow"] is inflow_facts(). Moves nothing."""
    H = float(h_ft)
    reasons, sev = [], [0]

    def add(level, text):
        sev[0] = max(sev[0], level)
        reasons.append(text)

    ind = sv.get("inflow") or {}
    emb = sv.get("emb")
    conf = bool(emb and emb["confirmed"] and sv.get("point_fill") is None)
    lv = 2 if conf else 1
    weak = "" if conf else " (embankment estimate weak)"
    if rp.get("inflow_xy") is not None:
        pw = ind.get("on_pool_water")
        if ind.get("on_water") and pw:
            add(2, "the breach inflow would be on the reservoir surface ({:.1f} ft water "
                   "level)".format(ind["water_wse_m"] / FT))
        elif ind.get("on_water") and pw is None:
            add(1, "the breach inflow would be on a flat water surface ({:.1f} ft); no pond "
                   "found to tell which side of the dam it is".format(ind["water_wse_m"] / FT))
        elif ind.get("on_water"):
            add(0, "note: the breach inflow is on a lower water surface ({:.1f} ft, below the "
                   "pond at {:.1f} ft): tailwater, not the pond".format(
                       ind["water_wse_m"] / FT, sv["level_m"] / FT))
        elif ind.get("in_pool"):
            add(lv, "the breach inflow would be inside the {} behind the embankment found{}"
                .format("pond" if sv.get("pool_kind") == "water" else "closed basin", weak))
        elif ind.get("upstream") and conf:
            add(lv, "the breach inflow would be {:.0f} m on the pond side of the embankment "
                    "found (on its upstream face or in the pond)".format(-ind["side_m"]))
    else:
        add(1, "the run could not place the breach inflow from the point ({})".format(
            str(rp.get("inflow_why", "")).split("; give")[0]))
    if rp.get("drop_h") is not None:
        dh = rp["drop_h"]
        if dh > VERIFY_DROP_H[1]:
            add(1, "the inflow is {:.1f} ft = {:.2f} x H below the top read at the point".format(
                rp["drop_m"] / FT, dh))
        elif dh < VERIFY_DROP_H[0]:
            add(1, "the inflow is only {:.1f} ft = {:.2f} x H below the top read at the point"
                .format(rp["drop_m"] / FT, dh))
    m = str(rp.get("method", ""))
    if "INFLOW_FAR" in m:
        add(1, "the run's inflow search found low ground far from the point")
    if "NO_DOWNSTREAM" in m:
        add(1, "no cell on the downstream side qualified for the inflow")
    if m == "place_from_crest":
        add(1, "the run fell back to placing the inflow from the crest")
    pf = sv.get("point_fill")
    if pf is not None:
        add(1, "the point is on a fill {:.0f} m wide with water at one level on both sides "
               "({:.1f} ft, the point {:.1f} ft above it): a road fill over backwater rather "
               "than a dam holding a pond".format(pf["width_m"], pf["level_m"] / FT,
                                                   pf["rise_m"] / FT))
    elif emb is not None:
        dpc = emb["point_to_crest_m"]
        diff_ft = (rp["top_m"] - emb["crest_z_m"]) / FT
        if dpc > 150:
            add(lv, "the point is {:.0f} m from the embankment found{}".format(dpc, weak))
        elif dpc > 50:
            add(1, "the point is {:.0f} m from the embankment found{}".format(dpc, weak))
        lim_hi = max(5.0, 0.3 * H)
        lim_ck = max(3.0, 0.15 * H)
        err = abs(diff_ft)
        big = err > 0.5 * H or (err > lim_hi and rp.get("inflow_xy") is None)
        lt = lv if big else 1
        if diff_ft > lim_hi and dpc > NEAR_M:
            add(lt, "the DEM near the point reads the top of dam {:.1f} ft above the crest "
                    "found ({:.1f} ft): a knoll or high ground beside the pond".format(
                        diff_ft, emb["crest_z_m"] / FT))
        elif diff_ft < -lim_hi and sv.get("point_on_water"):
            add(lv, "the point is on the reservoir water: the top read there is the water "
                    "level, {:.1f} ft below the crest found ({:.1f} ft)".format(
                        -diff_ft, emb["crest_z_m"] / FT))
        elif diff_ft < -lim_hi and dpc > NEAR_M:
            add(lt, "the point is off the embankment on lower ground: the top read there is "
                    "{:.1f} ft below the crest found ({:.1f} ft)".format(
                        -diff_ft, emb["crest_z_m"] / FT))
        elif abs(diff_ft) > lim_ck:
            add(1, "the top read at the point is {:.1f} ft {} the crest found ({:.1f} ft)".format(
                abs(diff_ft), "above" if diff_ft > 0 else "below", emb["crest_z_m"] / FT))
        if emb.get("ds_water"):
            add(1, "water on both sides of the embankment found (tailwater or a lake below): "
                   "its height cannot be checked")
        if emb.get("ridge_only"):
            add(1, "dry: no pond or closed basin; judged by the embankment ridge only "
                   "({:.0f} m long, {:.1f} ft high)".format(emb["length_m"],
                                                       emb["height_over_low_m"] / FT))
        elif not conf:
            add(1, "embankment not confirmed (crest {:.1f} ft above the low ground, H {:.1f} "
                   "ft)".format(emb["height_over_low_m"] / FT, H))
        dsp = sv.get("emb_ds_at_point")
        if conf and rp.get("direction") is not None and dsp is not None:
            if rp["direction"][0] * dsp[0] + rp["direction"][1] * dsp[1] < 0:
                add(1, "the run's downstream side is opposite the embankment's")
    else:
        add(1, nothing_found_text(sv, H))
    if sv.get("point_on_water") and not any("on the reservoir water" in r for r in reasons):
        fine = bool(emb is not None and abs((rp["top_m"] - emb["crest_z_m"]) / FT)
                    <= max(3.0, 0.15 * H) and rp.get("drop_h") is not None
                    and VERIFY_DROP_H[0] <= rp["drop_h"] <= VERIFY_DROP_H[1])
        if fine:
            add(0, "note: the point touches the water's edge, but the top read there matches "
                   "the crest found")
        else:
            add(1, "the point is on the water surface")
    if sev[0] == 0 and rp.get("drop_h") is not None:
        reasons.insert(0, "the point is on the embankment found; inflow {:.1f} ft = {:.2f} x H "
                          "below the top".format(rp["drop_m"] / FT, rp["drop_h"]))
    return ["OK", "CHECK", "LIKELY MISPLACED"][sev[0]], reasons


def nothing_found_text(sv, h_ft):
    h_m = float(h_ft) * FT
    return ("no pond, closed basin or embankment ridge found on the DEM within {:.0f} m ({}); "
            "the limits are flat water >= {:.2f} ac, a closed basin > {:.1f} ft deep and a "
            "ridge >= {:.1f} ft (0.5 H) high, so a shallow pond or a low embankment can be "
            "missed".format(SEARCH_POOL_M, sv.get("pool_kind", "dry"), WATER_MIN_AC,
                            max(0.3, 0.1 * h_m) / FT, 0.5 * float(h_ft)))


def _line_near(line_a, line_b, step=4.0):
    """Mean distance of line_a's samples from line_b."""
    p = sample_line(line_a, step)
    return float(np.mean(_seg_dist(p[:, 0], p[:, 1], line_b)))


GATE_TEXT = {
    "G1": "the reservoir is not flat water on this DEM, or the embankment is not confirmed",
    "G2": "the embankment height does not match the dam height",
    "G3": "the embankment is more than {:.0f} m from the point".format(MAX_MOVE_M),
    "G4": "the crest line found is not clean (off the crest, over water or too short)",
    "G4c": "the line found is closed or nearly closed (a ring dike or lagoon)",
    "G5": "not exactly one flat water body within {:.0f} m".format(SEARCH_POOL_M),
    "G6": "the pond area does not match the inventory area",
    "G7a": "the point is on another embankment ridge",
    "G7b": "the point is on the low (downstream) side of the embankment found",
    "G7f": "the pond lies between the point and the embankment found",
    "G7c": "the embankment found is nearer another dam",
    "G7d": "the line found does not match the inventory crest length",
    "G7e": "the pond level does not match the normal pool",
    "C1": "the point is on the embankment but off its centre",
    "V": "the embankment was found but the inflow below it did not check out",
}


def decide(sv, rp, h_m, sa_ac=None, crest_length_m=None, normal_pool_m=None, others=None,
           other_lines=None, mode=DEFAULT_MODE):
    """The decision before the check after a move (V, the caller's). Returns a dict:
    outcome ("move" = every gate passed, the caller runs V; "kept"; "review"), flag, reasons,
    triggers [(code, text)], gates_failed [(code, text)], warn (True: a REVIEW warning),
    would_move (warn mode: every gate passed). `others`: other inventory dam points
    [(id, (x, y))]; `other_lines`: other dams' crest lines (work CRS)."""
    h_ft = h_m / FT
    flag, reasons = classify(sv, rp, h_ft)
    out = {"flag": flag, "reasons": reasons, "triggers": [], "gates_failed": [],
           "warn": False, "would_move": False}
    emb = sv.get("emb")
    ind = sv.get("inflow") or {}
    if emb is None:
        out["outcome"] = "kept"
        out["kept_why"] = "nothing found"
        return out
    dpc = float(emb["point_to_crest_m"])
    diff_ft = (rp["top_m"] - emb["crest_z_m"]) / FT
    lim_ck = max(3.0, 0.15 * h_ft)
    on_pond_water = bool(sv.get("point_on_water") and sv.get("point_fill") is None and
                         sv.get("point_wid") is not None and
                         (sv.get("point_wid") == sv.get("pool_wid") or
                          _wse(sv, sv["point_wid"]) is not None and sv.get("level_m") is not None
                          and abs(_wse(sv, sv["point_wid"]) - sv["level_m"]) <= 0.15))
    trig = []
    if dpc > NEAR_M and (on_pond_water or (sv.get("point_on_water") and diff_ft < -lim_ck)):
        trig.append(("A1", "the point is on the reservoir water, {:.0f} m from the embankment"
                           .format(dpc) if on_pond_water else
                     "the point is on the reservoir water and the DEM near it reads the top "
                     "of dam {:.1f} ft low".format(-diff_ft)))
    if dpc > NEAR_M and abs(diff_ft) > max(A2_MIN_FT, A2_H * h_ft):
        trig.append(("A2", "the DEM near the point reads the top of dam {:.1f} ft {}".format(
            abs(diff_ft), "high" if diff_ft > 0 else "low")))
    if rp.get("inflow_xy") is not None and (ind.get("on_pool_water") or ind.get("in_pool") or
                                            ind.get("upstream")):
        trig.append(("A3", "the breach inflow would be on the reservoir side of the embankment"
                     if not ind.get("on_pool_water") else
                     "the breach inflow would be on the reservoir"))
    if rp.get("failed"):
        trig.append(("A4", "the breach inflow could not be placed from the point"))
    out["triggers"] = trig
    codes = {t[0] for t in trig}
    near = dpc <= KEEP_M
    g1 = _gate_g1(sv, emb)
    hr = emb["height_ratio"]
    gate_kw = dict(sa_ac=sa_ac, crest_length_m=crest_length_m, normal_pool_m=normal_pool_m,
                   others=others, other_lines=other_lines)
    if not trig:
        out["outcome"] = "review" if flag == "LIKELY MISPLACED" else "kept"
        out["warn"] = flag == "LIKELY MISPLACED"
        out["kept_why"] = ("on the embankment ({:.0f} m)".format(dpc) if near else
                           "no clear sign the point is off the dam" if flag != "OK" else
                           "the top and the inflow check out")
        if not out["warn"] and g1 is None and dpc > FAR_WARN_M and HR_MIN <= hr <= HR_MAX:
            # validation 2026-11-17: points 79-126 m off their dam were kept silently
            out["outcome"], out["warn"] = "review", True
            out["kept_why"] = None
            out["reasons"].insert(0, "the point is {:.0f} m from a clean embankment of the dam's "
                                     "height on the DEM (its pond is flat water)".format(dpc))
        if near and out["outcome"] == "kept":
            return _centre_snap(out, sv, emb, h_m, dpc, g1, hr, mode, gate_kw)
        return out
    if near and not codes & {"A3", "A4"}:
        out["outcome"] = "kept"
        out["kept_why"] = "on the embankment ({:.0f} m)".format(dpc)
        return _centre_snap(out, sv, emb, h_m, dpc, g1, hr, mode, gate_kw)
    g = _gates(sv, emb, h_m, dpc, codes, g1, hr, **gate_kw)
    out["gates_failed"] = g
    out["neighbours"] = (None if others is None else len(others))
    if g:
        out["outcome"] = "review"
        out["warn"] = True
        if g1 is not None and dpc > DOUBTFUL_FAR_M and codes <= {"A2", "A3"} and \
                flag != "LIKELY MISPLACED":
            # validation 2026-11-17: an embankment that fails G1 this far away is mostly another
            # structure (a road fill, a dam 100-700 m off), and A2 / A3 are measured against it
            out["warn"] = False
            out["outcome"] = "kept"
            out["kept_why"] = ("the embankment found is {:.0f} m away and not clean ({}): a note "
                               "only".format(dpc, g1))
        return out
    if normalize_mode(mode) == "warn":
        out["outcome"] = "review"
        out["warn"] = True
        out["would_move"] = True
        return out
    out["outcome"] = "move"
    return out


def _centre_snap(out, sv, emb, h_m, dpc, g1, hr, mode, gate_kw):
    """3.20.13 C1: a point kept because it is on the embankment (within KEEP_M of the found
    line) but at least CENTRE_MIN_M from the line's centre moves to the centre in "move" mode
    when every gate passes (the caller's V too). Warn mode: a note in kept_because only (no
    REVIEW warning). Returns `out`."""
    line = emb.get("line")
    if not line or len(line) < 2:
        return out
    lp = np.asarray(line, float)
    seg = np.hypot(*np.diff(lp, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    half = 0.5 * cum[-1]
    k = int(np.clip(np.searchsorted(cum, half, side="right") - 1, 0, len(seg) - 1))
    t = (half - cum[k]) / seg[k] if seg[k] > 0 else 0.0
    mid = lp[k] + t * (lp[k + 1] - lp[k])
    dmid = float(math.hypot(sv["dam_xy"][0] - mid[0], sv["dam_xy"][1] - mid[1]))
    out["centre_m"] = round(dmid, 1)
    if dmid < CENTRE_MIN_M:
        return out
    g = _gates(sv, emb, h_m, dpc, set(), g1, hr, **gate_kw)
    others = gate_kw.get("others")
    out["neighbours"] = (None if others is None else len(others))
    if g:
        out["gates_failed"] = g
        out["kept_why"] += ", {:.0f} m from its centre (not moved there: {})".format(
            dmid, g[0][1])
        return out
    if normalize_mode(mode) != "move":
        out["kept_why"] += ", {:.0f} m from its centre (\"move\" would put it there)".format(dmid)
        return out
    out["triggers"] = [("C1", "the point is on the embankment, {:.0f} m from its centre"
                        .format(dmid))]
    out["centre_snap"] = True
    out["kept_why"] = None
    out["outcome"] = "move"
    return out


def _gates(sv, emb, h_m, dpc, codes, g1, hr, sa_ac=None, crest_length_m=None,
           normal_pool_m=None, others=None, other_lines=None):
    """The gates G1-G7e of a move to the found embankment: [(code, why)] of those that fail."""
    g = []
    lq = sv.get("line_q") or {}
    if g1 is not None:
        g.append(("G1", g1))
    if not (HR_MIN <= hr <= HR_MAX):
        g.append(("G2", "the embankment is {:.2f} x the dam height".format(hr)))
    if dpc > MAX_MOVE_M:
        g.append(("G3", "the embankment is {:.0f} m from the point".format(dpc)))
    Lmin = max(LEN_MIN_M, LEN_MIN_H * h_m)
    if not emb.get("line"):
        g.append(("G4", "no crest line could be traced"))
    else:
        bad = []
        if lq.get("on_crest", 0) < LINE_ON_CREST:
            bad.append("{:.0f}% on the crest".format(100 * lq.get("on_crest", 0)))
        if lq.get("on_water", 0) > 0:
            bad.append("{:.0f}% over water".format(100 * lq["on_water"]))
        if lq.get("below_pool", 0) > LINE_BELOW_POOL:
            bad.append("{:.0f}% below the pond level".format(100 * lq["below_pool"]))
        if lq.get("perp95_m", 0) > perp95_limit_m(h_m):
            bad.append("crest cells up to {:.0f} m off the line".format(lq["perp95_m"]))
        if lq.get("length_m", 0) < Lmin:
            bad.append("{:.0f} m long (shorter than {:.0f} m)".format(lq.get("length_m", 0), Lmin))
        if bad:
            g.append(("G4", "the crest line found is " + ", ".join(bad)))
        cl = sv.get("closed") or (False, "")
        if cl[0]:
            g.append(("G4c", cl[1]))
    nw = ponds_at_level(sv, h_m)
    if nw != 1:
        g.append(("G5", "{} flat water bodies within {:.0f} m at the pond's level or above "
                        "(tailwater left out)".format(nw, SEARCH_POOL_M)))
    if sa_ac and sa_ac > 0 and sv.get("pool_area_ac") is not None:
        ratio = sv["pool_area_ac"] / float(sa_ac)
        if not (AREA_RATIO[0] <= ratio <= AREA_RATIO[1]):
            g.append(("G6", "the pond is {:.2f} x the inventory area".format(ratio)))
    # --- critique 2: never move onto a neighbouring dam -------------------------------
    pr = sv.get("point_ridge")
    if pr is not None:
        g.append(("G7a", "the point is on another embankment ridge ({:.1f} ft high, {:.0f} m "
                         "long)".format(pr["height_m"] / FT, pr["length_m"])))
    ps = sv.get("point_side_m")
    if ps is not None and ps > DOWNSTREAM_M:
        # a point below the embankment moves only from the low ground there (a point given
        # on the valley floor below the dam); a point higher than that stands on something of
        # its own: the next dam down the coulee, a road fill (review 2026-11-17)
        zp = sv.get("point_z_m")
        rise = (zp - emb["low_z_m"]) if zp is not None and math.isfinite(zp) else float("inf")
        low_ok = rise <= max(LOW_GROUND_M, LOW_GROUND_H * h_m) and \
            not sv.get("point_in_basin")
        if not ("A2" in codes and low_ok):
            g.append(("G7b", "the point is {:.0f} m on the low (downstream) side of the "
                             "embankment found{}".format(
                                 ps, "" if low_ok else ", in a closed basin of its own"
                                 if sv.get("point_in_basin") else
                                 ", {:.1f} ft above the low ground there".format(rise / FT)
                                 if math.isfinite(rise) else "")))
    if (sv.get("pond_between") or 0.0) >= POND_BETWEEN_FRAC and \
            (ps is None or ps < -DOWNSTREAM_M):
        g.append(("G7f", "the pond lies between the point and the embankment found ({:.0f}% of "
                         "the way): the point may be on another dam above the pond".format(
                             100.0 * sv["pond_between"])))
    line = emb.get("line")
    if line:
        lp = np.asarray(line, float)
        d_self = float(_seg_dist(np.array([sv["dam_xy"][0]]), np.array([sv["dam_xy"][1]]),
                                 lp)[0])
        h_found = float(emb["height_over_low_m"])
        for o in others or ():
            oid, oxy = o[0], o[1]
            oh = o[2] if len(o) > 2 else None
            d_o = float(_seg_dist(np.array([oxy[0]]), np.array([oxy[1]]), lp)[0])
            if d_o >= d_self:
                continue
            try:
                fits = bool(oh) and float(oh) > 0 and \
                    OTHER_H_FIT[0] <= h_found / (float(oh) * FT) <= OTHER_H_FIT[1]
            except (TypeError, ValueError):
                fits = False
            if d_o <= OTHER_ON_LINE_M or fits:
                g.append(("G7c", "the embankment found is {:.0f} m from dam {}{} and {:.0f} m "
                                 "from this point".format(
                                     d_o, oid, " (its height fits it)" if fits and
                                     d_o > OTHER_ON_LINE_M else "", d_self)))
                break
        for ol in other_lines or ():
            try:
                if len(ol) >= 2 and _line_near(line, ol) <= OTHER_LINE_M:
                    g.append(("G7c", "the embankment found is another dam's crest line"))
                    break
            except Exception:                              # noqa: BLE001 - a bad line
                continue
        if crest_length_m and crest_length_m > 0:
            r = lq.get("length_m", 0.0) / float(crest_length_m)
            lo, hi = CREST_LENGTH_RATIO
            if (lo is not None and r < lo) or (hi is not None and r > hi):
                g.append(("G7d", "the line found is {:.0f} m, {:.2f} x the inventory crest "
                                 "length".format(lq.get("length_m", 0.0), r)))
    if normal_pool_m is not None and sv.get("level_m") is not None:
        lo = normal_pool_m - 0.5 * h_m
        hi = normal_pool_m + max(3.0 * FT, 0.15 * h_m)
        if not (lo <= sv["level_m"] <= hi):
            g.append(("G7e", "the pond level {:.1f} ft is not near the normal pool {:.1f} ft"
                      .format(sv["level_m"] / FT, normal_pool_m / FT)))
    return g


def _wse(sv, wid):
    for b in sv.get("bodies") or ():
        if b["id"] == wid:
            return b["wse_m"]
    return None


def _gate_g1(sv, emb):
    """G1's reason, or None when the pond is flat water held by a confirmed embankment whose
    low side is dry, open ground."""
    if sv.get("pool_kind") == "water" and emb["confirmed"] and not emb.get("ridge_only") \
            and not emb.get("ds_water") and not emb.get("ds_closed") and \
            sv.get("point_fill") is None:
        return None
    return ("no flat water on this DEM within {:.0f} m (it may not be hydro-flattened)"
            .format(WATER_FOUND_M) if not sv.get("water_found_1000m") else
            "the embankment's low side is water" if emb.get("ds_water") else
            "the point is on a fill across water" if sv.get("point_fill") else
            "judged by a ridge only" if emb.get("ridge_only") else
            "the pond is a dry basin, not flat water" if sv.get("pool_kind") != "water" else
            "the embankment's low side is a closed basin (another reservoir?)"
            if emb.get("ds_closed") else "the embankment is not confirmed")


def perp95_limit_m(h_m):
    """G4: crest cells this far off the line (95th percentile): max(15 m, 2 H). The band of
    cells halfway between the pond and the low ground widens with the fill's footprint."""
    return max(LINE_PERP95_M, LINE_PERP95_H * float(h_m))


def ponds_at_level(sv, h_m):
    """G5: the flat water bodies within SEARCH_POOL_M at the pond's level or above (less
    max(0.5 m, 0.25 H)); tailwater and ponds below the dam are not a second pond."""
    lvl = sv.get("level_m")
    tol = max(0.5, 0.25 * float(h_m))
    return sum(1 for b in sv.get("bodies") or ()
               if b.get("edge_dist_m", 1e9) <= SEARCH_POOL_M and
               (lvl is None or b["wse_m"] >= lvl - tol))


def verify_ok(z_mc_m, inflow_z_m, h_m, inflow_on_pond):
    """V: the toe inflow below the mid-crest of the found line is 0.5-1.3 H below it and not
    in the pond. (ok, drop / H)."""
    if z_mc_m is None or inflow_z_m is None or not (math.isfinite(z_mc_m) and
                                                    math.isfinite(inflow_z_m)) or h_m <= 0:
        return False, None
    dh = (z_mc_m - inflow_z_m) / h_m
    return bool(VERIFY_DROP_H[0] <= dh <= VERIFY_DROP_H[1] and not inflow_on_pond), dh


# ======================================================================== the record ======
def _r(v, n=1):
    try:
        return None if v is None or not math.isfinite(float(v)) else round(float(v), n)
    except (TypeError, ValueError):
        return None


def record(outcome, mode, dam_xy, skipped_because=None, sv=None, rp=None, dec=None,
           verify=None, moved_xy=None, seconds=None, native_cell=None, resampling=None,
           sa_ac=None, error=None):
    """The run_report "dam_point_check" block (ft for elevations, m for distances)."""
    rec = {"version": VERSION, "mode": mode, "outcome": outcome,
           "skipped_because": skipped_because,
           "original_xy": [round(float(dam_xy[0]), 2), round(float(dam_xy[1]), 2)],
           "moved_m": None, "seconds": _r(seconds, 2)}
    if error:
        rec["error"] = error
    if sv is None:
        return rec
    emb = sv.get("emb")
    rec["dem"] = {"cells_m": [CELL_WATER_M, CELL_EMB_M], "native_cell_m": _r(native_cell, 2),
                  "resampling": resampling, "hydro_flattened": bool(sv.get("water_found_1000m")),
                  "water_bodies_400m": sv.get("n_water_400", 0),
                  "ponds_at_level_400m": ponds_at_level(sv, sv.get("h_m") or 0.0)}
    if sv.get("level_m") is not None:
        rec["pool"] = {"kind": sv.get("pool_kind"), "level_ft": _r(sv["level_m"] / FT, 2),
                       "area_ac": _r(sv.get("pool_area_ac"), 2),
                       "area_vs_inventory": (_r(sv["pool_area_ac"] / sa_ac, 3)
                                             if sa_ac and sv.get("pool_area_ac") else None)}
    else:
        rec["pool"] = {"kind": sv.get("pool_kind")}
    if emb is not None:
        lq = sv.get("line_q") or {}
        e = {"line_xy": [[round(x, 2), round(y, 2)] for x, y in (emb.get("line") or [])],
             "length_m": _r(lq.get("length_m", emb.get("length_m"))),
             "crest_ft": _r(emb["crest_z_m"] / FT, 2),
             "crest_p90_ft": _r(emb["crest_z90_m"] / FT, 2),
             "height_ft": _r(emb["height_over_low_m"] / FT), "height_x_h": _r(
                 emb["height_ratio"], 2), "confirmed": bool(emb["confirmed"]),
             "ridge_only": bool(emb.get("ridge_only")), "low_side_water": bool(emb["ds_water"]),
             "low_side_closed": bool(emb.get("ds_closed")),
             "distance_m": _r(emb["point_to_crest_m"]),
             "downstream": ([round(v, 3) for v in emb["ds_dir"]] if emb.get("ds_dir") else None),
             "line_on_crest": _r(lq.get("on_crest"), 3),
             "line_on_water": _r(lq.get("on_water"), 3),
             "line_below_pool": _r(lq.get("below_pool"), 3),
             "line_perp95_m": _r(lq.get("perp95_m")),
             "closed": bool((sv.get("closed") or (False,))[0]),
             "point_side_m": _r(sv.get("point_side_m")),
             "pond_between": _r(sv.get("pond_between"), 2)}
        rec["embankment"] = e
    else:
        rec["embankment"] = None
    if rp is not None:
        ind = sv.get("inflow") or {}
        rec["point_only"] = {
            "top_ft": _r(rp.get("top_m", float("nan")) / FT, 2),
            "top_minus_crest_ft": (_r((rp["top_m"] - emb["crest_z_m"]) / FT, 2)
                                   if emb is not None else None),
            "on_water": bool(sv.get("point_on_water")),
            "fill_across_water_m": _r((sv.get("point_fill") or {}).get("width_m")),
            "inflow_xy": ([round(float(v), 2) for v in rp["inflow_xy"]]
                          if rp.get("inflow_xy") is not None else None),
            "inflow_how": rp.get("inflow_why"), "inflow_on_pool": bool(
                ind.get("on_pool_water") or ind.get("in_pool")),
            "inflow_side_m": _r(ind.get("side_m")), "inflow_failed": bool(rp.get("failed")),
            "drop_x_h": _r(rp.get("drop_h"), 2)}
    if dec is not None:
        rec["flag"] = dec["flag"]
        rec["triggers"] = [" ".join(t) for t in dec["triggers"]]
        rec["gates_failed"] = [" ".join(t) for t in dec["gates_failed"]]
        rec["reasons"] = list(dec["reasons"])
        rec["would_move"] = bool(dec.get("would_move"))
        if dec.get("gates_failed") is not None and "neighbours" in dec:
            # G7c reads the other inventory dams the caller passed; None: not evaluated
            rec["neighbours_checked"] = dec["neighbours"]
        if dec.get("kept_why"):
            rec["kept_because"] = dec["kept_why"]
        if dec.get("centre_m") is not None:
            rec["centre_m"] = dec["centre_m"]          # 3.20.13: point to the found line's centre
        if dec.get("centre_snap") and outcome == "moved":
            rec["centre_snap"] = True                  # 3.20.13 C1
    if verify is not None:
        rec["verify"] = verify
    if moved_xy is not None:
        rec["modelled_xy"] = [round(float(moved_xy[0]), 2), round(float(moved_xy[1]), 2)]
        rec["moved_m"] = _r(math.hypot(moved_xy[0] - dam_xy[0], moved_xy[1] - dam_xy[1]))
    return rec


def public(rec):
    """The block without private keys (the run_report copy)."""
    return {k: v for k, v in (rec or {}).items() if not str(k).startswith("_")}


def _plural_join(items):
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def warning_text(rec):
    """The DAM POINT MOVED warning (first in the list, pinned there by pin_first)."""
    e = rec.get("embankment") or {}
    trig = [t.split(" ", 1)[1] if " " in t else t for t in rec.get("triggers") or []]
    near = (rec.get("embankment") or {}).get("distance_m") or 0.0
    where = ("the point given is {:.0f} m from the embankment on the DEM".format(near)
             if near > KEEP_M else "the point given is on the embankment on the DEM")
    return ("{}: {} ({}). The run uses that embankment as the dam line ({:.0f} m, crest "
            "{:.1f} ft) with the dam point at mid-crest, {:.0f} m from the point given. Check "
            "it on the inflow map; the line is in inputs/{} to accept as DAM_LINES or "
            "CREST_LINE. To use the point as given, give a dam line or the top of dam, or set "
            "\"Check the dam point on the DEM\" to warn or off.".format(
                MOVED_PREFIX, where, _plural_join(trig), e.get("length_m") or 0.0,
                e.get("crest_ft") or 0.0, rec.get("moved_m") or 0.0, FOUND_LINE_FILE))


def review_text(rec):
    """The REVIEW warning (added only for LIKELY MISPLACED or a trigger with a failed gate)."""
    reasons = rec.get("reasons") or []
    first = reasons[0] if reasons else "the point could not be confirmed on the embankment"
    t = ("{}: {}. The point is used as given; if it is not on the dam, give a dam line "
         "(DAM_LINES or CREST_LINE) or the top of dam.".format(REVIEW_PREFIX, first))
    e = rec.get("embankment") or {}
    if e.get("line_xy"):
        if rec.get("would_move"):
            t += (" An embankment {:.0f} m away passed every check and would be used as the dam "
                  "line with \"Check the dam point on the DEM\" set to move (it is set to warn); "
                  "the line is in inputs/{}.".format(e.get("distance_m") or 0.0, FOUND_LINE_FILE))
        else:
            gf = rec.get("gates_failed") or []
            why = gf[0].split(" ", 1)[1] if gf and " " in gf[0] else ""
            t += " An embankment {:.0f} m away was found but not used{}; it is in inputs/{}.".format(
                e.get("distance_m") or 0.0, " ({})".format(why) if why else "", FOUND_LINE_FILE)
    return t


def log_line(rec):
    o = rec.get("outcome")
    if o == "skipped":
        return "Dam point check: skipped ({})".format(rec.get("skipped_because"))
    if o == "moved":
        return "Dam point check: moved {:.0f} m to the embankment found on the DEM ({})".format(
            rec.get("moved_m") or 0.0, "; ".join(rec.get("triggers") or []))
    if o == "kept":
        return "Dam point check: kept ({}){}".format(
            rec.get("kept_because", ""), "; " + "; ".join(rec.get("reasons") or [])
            if rec.get("flag") not in (None, "OK") else "")
    if o == "review":
        return "Dam point check: review ({})".format("; ".join(rec.get("reasons") or [])[:400])
    if o == "error":
        return "Dam point check: not done ({})".format(rec.get("error"))
    return "Dam point check: {}".format(o)


def batch_text(rec):
    """The batch column Dam_Point_Check."""
    if not rec:
        return ""
    o = rec.get("outcome")
    if o == "moved":
        return "moved {:.0f} m{}".format(rec.get("moved_m") or 0.0, " (to the embankment's "
                                         "centre)" if rec.get("centre_snap") else "")
    if o == "skipped":
        return "off" if rec.get("skipped_because") == "off" else \
            "skipped: {}".format(rec.get("skipped_because"))
    if o == "review":
        return "review{}".format(" (would move)" if rec.get("would_move") else "")
    if o == "kept":
        return "kept" if rec.get("kept_because", "").startswith(("on the", "the top")) else \
            "kept ({})".format(rec.get("kept_because") or "")
    return str(o or "")


def report_line(rec):
    """The "Dam location" text for run_report.md and the docx (report_docx.dam_location_text,
    stdlib), or None (no check block, or the check was skipped)."""
    from .report_docx import dam_location_text
    return dam_location_text(rec)


def pin_first(warnings):
    """Keep the DAM POINT MOVED warning first: later insert(0) calls (_check_inflow_site, the
    inflow depth check, the far-inflow warning) must not push it down. In place."""
    if not warnings:
        return warnings
    for i, w in enumerate(list(warnings)):
        if isinstance(w, str) and w.startswith(MOVED_PREFIX) and i > 0:
            warnings.insert(0, warnings.pop(i))
            break
    return warnings


def found_line_side(dam):
    """(direction, how) for a dam line the dam point check found: the line's normal on the side
    the embankment was found to fall away from its pond (dam._dpc_ds_dir), or None. Any other
    dam line, a user's included, keeps terrain.downstream_direction (workflow._downstream_inflow,
    reservoir_carve.downstream_for), which since 3.20.12 asks the reservoir's flat water first
    (terrain.water_side); the toe pick on either is then checked (workflow._inflow_water_check)."""
    if getattr(dam, "_crest_line_source", None) != FOUND_LINE_SOURCE:
        return None
    ds = getattr(dam, "_dpc_ds_dir", None)
    line = getattr(dam, "crest_line_xy", None)
    if ds is None or not line or len(line) < 2:
        return None
    a, b = np.asarray(line[0], float), np.asarray(line[-1], float)
    t = b - a
    n = math.hypot(t[0], t[1])
    if n <= 1e-9:
        return None
    nv = (float(-t[1] / n), float(t[0] / n))
    if nv[0] * ds[0] + nv[1] * ds[1] < 0:
        nv = (-nv[0], -nv[1])
    return nv, ("crest-line normal on the embankment's low side (pond to low ground, found by "
                "the dam point check)")


def found_line_geojson(rec, crs=None):
    """The found line as a GeoJSON FeatureCollection dict, or None."""
    e = (rec or {}).get("embankment") or {}
    if not e.get("line_xy"):
        return None
    props = {"kind": "embankment crest found on the DEM (dam point check)",
             "used": rec.get("outcome") == "moved", "outcome": rec.get("outcome"),
             "length_m": e.get("length_m"), "crest_ft": e.get("crest_ft"),
             "distance_from_point_m": e.get("distance_m"),
             "note": "accept it as DAM_LINES or CREST_LINE to make later runs use it as given"}
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "LineString", "coordinates": e["line_xy"]}}


# ======================================================================== the cache =======
_CACHE = OrderedDict()
CACHE_ENTRIES = 64
_LOCK = threading.Lock()          # a preview and a run may check in two Processing tasks


def dem_stamp(terrain):
    """What identifies the DEM a check read: the file (path, size, mtime, z factor, resample
    method) of the source under the wrappers, else its dpc_stamp attribute, else None (no
    caching). The resample method (review 2026-11-17): the replay and V read through the
    run's terrain read (GdalTerrain.resample), only the survey reads with average."""
    t = terrain
    rs = None
    for _i in range(6):
        if t is None:
            break
        st = getattr(t, "dpc_stamp", None)
        if st is not None:
            return st
        if rs is None and isinstance(getattr(t, "resample", None), str):
            rs = t.resample
        p = getattr(t, "dem_path", None)
        if isinstance(p, str) and p:
            try:
                s = os.stat(p)
                return (p, s.st_size, getattr(s, "st_mtime_ns", s.st_mtime),
                        float(getattr(t, "z_to_m", 1.0) or 1.0), rs)
            except OSError:
                return None
        t = getattr(t, "base", None)
    return None


def cache_key(dam, dam_xy, terrain, extra=()):
    stamp = dem_stamp(terrain)
    if stamp is None:
        return None
    return (str(getattr(dam, "dam_id", "")), round(float(dam_xy[0]), 2),
            round(float(dam_xy[1]), 2), stamp, getattr(dam, "dam_height_ft", None),
            getattr(dam, "surface_area_top_ac", None), getattr(dam, "crest_length_ft", None)) + \
        tuple(extra)


def cache_get(key):
    if key is None:
        return None
    with _LOCK:
        v = _CACHE.get(key)
        if v is not None:
            _CACHE.move_to_end(key)
        return v


def cache_put(key, value):
    if key is None:
        return
    with _LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_ENTRIES:
            _CACHE.popitem(last=False)


def cache_clear():
    with _LOCK:
        _CACHE.clear()
