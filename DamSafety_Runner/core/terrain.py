"""Terrain helpers: where to put the breach inflow, which way the flood goes,
and how big the model domain has to be."""
import heapq
import math

import numpy as np

from .grid import Grid

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


def pool_mask(grid, dam_height_m):
    """Cells of a closed depression on `grid` (the reservoir behind an embankment): filled
    (priority flood from the grid edges) minus ground deeper than max(0.5 m, 15 % of the dam
    height). A downstream toe or channel drains off the grid and is not in it. The grid must
    reach well past the reservoir (POOL_HALF_M) or the basin drains through the grid edge."""
    z = grid.masked()
    fill = priority_flood(z) - z
    return np.isfinite(fill) & (fill > max(0.5, 0.15 * float(dam_height_m)))


def downstream_direction(pool_grid, dam_xy, dam_height_m, crest_line=None, crest_xy=None,
                         near_m=600.0):
    """Unit vector from the crest towards downstream, and how it was found, or (None, why).

      1. the crest line's normal, the side away from the pool (else the side the trace takes);
      2. no crest line: away from the pool (the centroid of the depression within near_m);
      3. no pool: the steepest-descent trace from the dam point;
      4. else the trace from place_from_crest's crest (`crest_xy`).
    The trace alone is the weakest: on a dry or hydro-flattened basin it descends into the
    reservoir (Absaraka DWR-2801), which is why the pool comes first."""
    def unit(v):
        n = math.hypot(v[0], v[1])
        return (v[0] / n, v[1] / n) if n > 1e-9 else None

    away = None
    if pool_grid is not None:
        pool = pool_mask(pool_grid, dam_height_m)
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
                      pool=None):
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
    pt, note = _pick_inflow(X, Y, d, z, need, top_m, radius_m, ok)
    if pt is None and constrained:
        pt, note2 = _pick_inflow(X, Y, d, z, need, top_m, radius_m, np.ones(z.shape, dtype=bool))
        if pt is not None:
            return pt, "{}; used {} (check it)".format(NO_DOWNSTREAM, note2)
    return pt, note


def _pick_inflow(X, Y, d, z, need, top_m, radius_m, ok):
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


def streambed_inflow(grid, crest_xy, dam_height_m, top_m, radius_m=150.0):
    """Lowest cell on the downstream trace, not the nearest low ground.

    Follow steepest descent from the crest. Prefer the first cell at least half
    the dam height below the crest. If none, take the lowest cell on that path
    within radius_m. A cell off the trace is not the streambed.
    Returns ((x, y), note) or (None, reason)."""
    try:
        path, _length = trace_downstream(grid, crest_xy, radius_m)
    except Exception as e:
        return None, "downstream trace failed: {}".format(e)
    if len(path) < 2:
        return None, "downstream trace did not leave the crest"
    z = grid.masked()
    need = top_m - 0.5 * dam_height_m
    best = None
    lowest = None
    for x, y in path[1:]:
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


def place_from_crest(grid, dam_xy, dam_height_m, top_m, radius_m=400.0):
    """A label point is not always the embankment. Find the crest of this dam
    (`embankment_crest`: ground at the top of dam, never a higher hill), then the nearest toe
    at least half the dam height below the TOP OF DAM.
    Returns (inflow_xy, crest_xy, note) or (None, crest_xy or None, reason)."""
    crest, crest_z, cnote = embankment_crest(grid, dam_xy, dam_height_m, top_m, radius_m)
    if crest is None:
        return None, None, cnote
    xs = grid.x_centers()
    ys = grid.y_centers()
    X, Y = np.meshgrid(xs, ys)
    z = grid.masked()
    moved = math.hypot(crest[0] - dam_xy[0], crest[1] - dam_xy[1])
    inflow, why = streambed_inflow(grid, crest, dam_height_m, top_m, radius_m)
    if inflow is not None:
        r, c = grid.rc(*inflow)
        if not (grid.inside(r, c) and np.isfinite(z[r, c]) and z[r, c] < top_m):
            inflow, why = None, "the downstream trace did not go below the top of dam"
    if inflow is None:
        inflow, why = find_inflow_point(grid, crest, dam_height_m, top_m, radius_m)
    if inflow is None:
        # Half the height is the normal toe. A label point can miss it by a foot on a
        # coarse DEM, so the fallback accepts 8 ft or 40 percent of the height.
        need = min(top_m - 8.0 * 0.3048, top_m - 0.4 * dam_height_m)
        d2 = np.hypot(X - crest[0], Y - crest[1])
        sel = (d2 <= max(radius_m, 800.0)) & np.isfinite(z) & (z <= need)
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
