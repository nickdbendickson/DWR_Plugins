"""v3.10.22: 2D breaklines for the HEC-RAS model, written AND built into the computation points.

HEC-RAS lines its cell faces up with a breakline only when it lays out the computation points
itself (RAS Mapper "Enforce breaklines"). It keeps a point layout it is given exactly (Muncie
gmsh test 2026-10-18: 10,824 seeds, all 10,824 cell centres within 6e-11 ft) and does not
enforce the breaklines in it: there the faces followed 2 % of 'Road 1' and 0 % of
'HighGround 1', against 89 % and 97 % with HEC's own points. The Runner writes its points, so
it enforces the breaklines the way HEC-RAS does: the points within one spacing of a line are
removed and a pair of points is put across the line, half a spacing each side, every spacing
along it, so that the Voronoi face between each pair lies on the line. The 'BreakLine' blocks
(and the dam connection's 'Conn CellSize') are written too, so RAS Mapper shows them and
re-enforcing them in RAS Mapper gives the same layout.

Lines (model units, the 2D area downstream of the dam line, which is on its perimeter):
* dam: the dam connection is on the perimeter, where HEC-RAS makes the faces itself; one row of
  points half a spacing inside it, every spacing along it, gives even faces along the weir.
* breach: the two sides of the breach opening cut into the terrain (the trench walls), from the
  dam line to the end of the cut.
* channel: the two banks of a one-cell-wide row along the run's flow path, from the end of the
  breach cut to two cells from the edge of the 2D area. A breakline down the middle of the
  channel would put a face on the thalweg; the banks put the thalweg in a row of cells.

Only numpy. Keywords as in HEC's example projects (BaldEagleDamBrk.g01-g03, Muncie.g04).
"""
import math

import numpy as np

# spacing along the lines, as a fraction of the cell size, when the points are the grid
GRID_NEAR_FRAC = 0.5
MIN_PAIR_FRAC = 0.45        # a new point closer than this x its spacing to another goes
PERIM_FRAC = 0.3            # ... or closer than this x its spacing to the perimeter
CHANNEL_END_CELLS = 2.0     # the channel lines stop this many cells from the perimeter
SIMPLIFY_CELLS = 0.5        # flow path simplified to this x the cell size, then smoothed
SMOOTH_PASSES = 5           # Chaikin passes: bends of about a cell's radius or more


def poly_dist(px, py, line, chunk=4096):
    """Distance from points (arrays) to an open polyline [(x, y)...]."""
    s = np.asarray(line, float).reshape(-1, 2)
    px = np.asarray(px, float)
    py = np.asarray(py, float)
    shape = px.shape
    px, py = px.ravel(), py.ravel()
    if len(s) == 1:
        return np.hypot(px - s[0, 0], py - s[0, 1]).reshape(shape)
    a, b = s[:-1], s[1:]
    dx, dy = b[:, 0] - a[:, 0], b[:, 1] - a[:, 1]
    L2 = dx * dx + dy * dy
    ok = L2 > 0
    if not ok.any():
        return np.hypot(px - s[0, 0], py - s[0, 1]).reshape(shape)
    a, dx, dy, L2 = a[ok], dx[ok], dy[ok], L2[ok]
    out = np.empty(len(px))
    step = max(1, int(chunk * 64 // max(1, len(a))))
    for i in range(0, len(px), step):
        X = px[i:i + step, None] - a[None, :, 0]
        Y = py[i:i + step, None] - a[None, :, 1]
        t = np.clip((X * dx + Y * dy) / L2, 0.0, 1.0)
        out[i:i + step] = np.hypot(X - t * dx, Y - t * dy).min(axis=1)
    return out.reshape(shape)


def ring_dist(px, py, ring):
    r = np.asarray(ring, float).reshape(-1, 2)
    return poly_dist(px, py, np.vstack([r, r[:1]]))


def inside(px, py, ring):
    """Even-odd point in polygon, vectorised."""
    s = np.asarray(ring, float).reshape(-1, 2)
    px = np.asarray(px, float)
    py = np.asarray(py, float)
    x, y = s[:, 0], s[:, 1]
    xj, yj = np.roll(x, 1), np.roll(y, 1)
    ins = np.zeros(px.shape, bool)
    for a, b, c, e in zip(x, y, xj, yj):
        cond = (b > py) != (e > py)
        with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
            xi = (c - a) * (py - b) / np.where(e - b == 0, 1e-300, e - b) + a
        ins ^= cond & (px < xi)
    return ins


def length(line):
    s = np.asarray(line, float).reshape(-1, 2)
    return float(np.hypot(*np.diff(s, axis=0).T).sum()) if len(s) > 1 else 0.0


def resample(line, step):
    """Points every `step` along a polyline (ends kept)."""
    s = np.asarray(line, float).reshape(-1, 2)
    if len(s) < 2:
        return s
    seg = np.hypot(*np.diff(s, axis=0).T)
    keep = np.r_[True, seg > 1e-9]
    s = s[keep]
    along = np.r_[0.0, np.cumsum(np.hypot(*np.diff(s, axis=0).T))]
    if along[-1] <= 0:
        return s[:1]
    n = max(1, int(math.ceil(along[-1] / step)))
    t = np.linspace(0.0, along[-1], n + 1)
    return np.column_stack([np.interp(t, along, s[:, 0]), np.interp(t, along, s[:, 1])])


def simplify(line, tol):
    """Douglas-Peucker."""
    s = np.asarray(line, float).reshape(-1, 2)
    if len(s) < 3:
        return s
    keep = np.zeros(len(s), bool)
    keep[[0, -1]] = True
    stack = [(0, len(s) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        d = poly_dist(s[i + 1:j, 0], s[i + 1:j, 1], s[[i, j]])
        k = int(np.argmax(d))
        if d[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return s[keep]


def _normals(pts):
    """Left normals at each vertex (averaged tangents)."""
    t = np.zeros_like(pts)
    t[1:-1] = pts[2:] - pts[:-2]
    t[0] = pts[1] - pts[0]
    t[-1] = pts[-1] - pts[-2]
    t /= np.maximum(np.hypot(*t.T), 1e-12)[:, None]
    return np.column_stack([-t[:, 1], t[:, 0]]), t


def offset_line(centre, off):
    """The centre line moved `off` to its left (negative: right); vertices that a tight bend
    would pull closer than 0.8 x |off| to the centre line, or that step backwards, are dropped."""
    c = np.asarray(centre, float).reshape(-1, 2)
    if len(c) < 2:
        return np.zeros((0, 2))
    n, t = _normals(c)
    o = c + off * n
    ok = poly_dist(o[:, 0], o[:, 1], c) >= 0.8 * abs(off)
    o, t = o[ok], t[ok]
    if len(o) < 2:
        return np.zeros((0, 2))
    keep = [0]
    for i in range(1, len(o)):
        if np.dot(o[i] - o[keep[-1]], t[i]) > 0.25 * abs(off):
            keep.append(i)
    return o[keep]


def chaikin(line):
    """One pass of Chaikin's corner cutting (ends kept)."""
    c = np.asarray(line, float).reshape(-1, 2)
    if len(c) < 3:
        return c
    q = 0.75 * c[:-1] + 0.25 * c[1:]
    r = 0.25 * c[:-1] + 0.75 * c[1:]
    mid = np.empty((2 * (len(c) - 1), 2))
    mid[0::2], mid[1::2] = q, r
    return np.vstack([c[:1], mid[1:-1], c[-1:]])


def channel_centre(path, perimeter, dam_line, start_dist, cell, step):
    """The run's flow path inside the 2D area from `start_dist` below the dam line to
    CHANNEL_END_CELLS cells from the perimeter, simplified and resampled every `step`."""
    p = np.asarray(path, float).reshape(-1, 2)
    if len(p) < 2:
        return np.zeros((0, 2))
    p = resample(p, 0.5 * step)
    dd = poly_dist(p[:, 0], p[:, 1], dam_line)
    ins = inside(p[:, 0], p[:, 1], perimeter)
    rd = ring_dist(p[:, 0], p[:, 1], perimeter)
    ok = ins & (rd >= CHANNEL_END_CELLS * cell)
    go = np.nonzero(ok & (dd >= start_dist))[0]
    if not len(go):
        return np.zeros((0, 2))
    i0 = int(go[0])
    i1 = i0
    while i1 + 1 < len(p) and ok[i1 + 1]:
        i1 += 1
    c = p[i0:i1 + 1]
    if length(c) < 4 * step:
        return np.zeros((0, 2))
    # the run's path follows the DEM cells (right angles); round its corners to about a cell
    c = simplify(c, SIMPLIFY_CELLS * cell)
    for _ in range(SMOOTH_PASSES):
        c = chaikin(c)
    return resample(c, step)


def strip_rows(c):
    """Mid-points of the centre line's segments and their left normals."""
    c = np.asarray(c, float)
    mid = 0.5 * (c[1:] + c[:-1])
    t = c[1:] - c[:-1]
    t /= np.maximum(np.hypot(*t.T), 1e-12)[:, None]
    return mid, np.column_stack([-t[:, 1], t[:, 0]])


def _forward(ln, nrm, h):
    """Drops vertices of an offset line that step backwards (inside a bend)."""
    if len(ln) < 2:
        return ln
    tang = np.column_stack([nrm[:, 1], -nrm[:, 0]])
    keep = [0]
    for i in range(1, len(ln)):
        if np.dot(ln[i] - ln[keep[-1]], tang[i]) > 0.25 * h:
            keep.append(i)
    return ln[keep]


def plan(perimeter, cell, dam_line, downstream, breach=None, path=None, spacing=None):
    """The breaklines of the model. dam_line (A, B): the dam connection; downstream: unit
    vector into the 2D area; breach: (centre on the dam line, half width of the cut, cut
    length) or None; path: the run's flow path or None; spacing: {'dam', 'breach', 'channel'}
    near spacings (default GRID_NEAR_FRAC x cell). Returns {'dam': {...} or None, 'lines':
    [{'name', 'line', 'near', 'far', 'kind'}], 'skipped': [why]}."""
    cell = float(cell)
    sp = {k: GRID_NEAR_FRAC * cell for k in ("dam", "breach", "channel")}
    sp.update({k: float(v) for k, v in (spacing or {}).items() if v})
    A, B = (np.asarray(v, float) for v in dam_line)
    d = np.asarray(downstream, float)
    d = d / (np.hypot(*d) or 1.0)
    out = {"dam": {"line": [A.tolist(), B.tolist()], "near": sp["dam"], "far": cell},
           "lines": [], "skipped": []}
    ab = (B - A) / (np.hypot(*(B - A)) or 1.0)
    cut_len = 0.0
    if breach is not None:
        C, cw, cut_len = np.asarray(breach[0], float), float(breach[1]), float(breach[2])
        s = min(sp["breach"], cw)               # the pairs of the two sides must not meet
        t0 = 0.5 * sp["dam"] + 0.5 * s
        if cut_len - t0 >= 2 * s:
            for nm, side in (("Breach left", -1.0), ("Breach right", 1.0)):
                ln = [C + ab * side * cw + d * t0, C + ab * side * cw + d * cut_len]
                out["lines"].append({"name": nm, "line": [list(map(float, q)) for q in ln],
                                     "near": s, "far": cell, "kind": "breach"})
        else:
            out["skipped"].append("breach: the cut is too short for breaklines")
    if path is not None and len(path) >= 2:
        h = sp["channel"]
        c = channel_centre(path, perimeter, [A, B], max(cut_len, 0.0) + h, cell, h)
        if len(c) >= 2:
            mid, nrm = strip_rows(c)
            for nm, off in (("Channel left", 0.5 * h), ("Channel right", -0.5 * h)):
                ln = mid + off * nrm
                ok = inside(ln[:, 0], ln[:, 1], perimeter)
                ok &= poly_dist(ln[:, 0], ln[:, 1], c) >= 0.4 * h     # a tight bend
                ln = _forward(ln[ok], nrm[ok], h)
                if len(ln) >= 2:
                    out["lines"].append({"name": nm, "line": ln.tolist(), "near": h, "far": cell,
                                         "kind": "channel"})
            out["strip"] = {"centre": c.tolist(), "h": h}
            out["channel_length"] = round(length(c), 1)
        else:
            out["skipped"].append("channel: the flow path is not inside the 2D area below the "
                                  "breach")
    else:
        out["skipped"].append("channel: the run has no flow path")
    return out


def _pairs(line, s):
    """HEC-RAS's enforcement: points half a spacing each side of the line, every spacing."""
    c = resample(line, s)
    if len(c) < 2:
        return np.zeros((0, 2))
    mid = 0.5 * (c[1:] + c[:-1])
    t = c[1:] - c[:-1]
    t /= np.maximum(np.hypot(*t.T), 1e-12)[:, None]
    n = np.column_stack([-t[:, 1], t[:, 0]])
    return np.vstack([mid + 0.5 * s * n, mid - 0.5 * s * n])


def _near_line(pts, keep, line, s):
    """Indexes of kept points that may be within s of the line (candidates)."""
    dense = resample(line, 0.5 * s)
    try:
        from scipy.spatial import cKDTree
    except ImportError:                                # pragma: no cover - scipy is optional
        lo = dense.min(axis=0) - 2 * s
        hi = dense.max(axis=0) + 2 * s
        box = keep & (pts[:, 0] >= lo[0]) & (pts[:, 0] <= hi[0]) & (pts[:, 1] >= lo[1]) & \
            (pts[:, 1] <= hi[1])
        return np.nonzero(box)[0]
    hits = cKDTree(pts).query_ball_point(dense, 1.01 * s)
    idx = np.unique(np.concatenate([np.asarray(h, int) for h in hits] + [np.zeros(0, int)]))
    return idx[keep[idx]]


def enforce(points, perimeter, bl):
    """Computation points with the breaklines of plan() built in. Returns (points, stats)."""
    pts = np.asarray(points, float).reshape(-1, 2)
    keep = np.ones(len(pts), bool)
    new, new_s = [], []
    dam = bl.get("dam")
    items = []
    if dam:
        A, B = (np.asarray(v, float) for v in dam["line"])
        s = float(dam["near"])
        L = float(np.hypot(*(B - A)))
        n = max(1, int(round(L / s)))
        u = (B - A) / (L or 1.0)
        # the 2D area's side of the dam line: the normal pointing inside
        nrm = np.array([-u[1], u[0]])
        mid = A + 0.5 * (B - A)
        if not inside(np.array([mid[0] + nrm[0] * s]), np.array([mid[1] + nrm[1] * s]),
                      perimeter)[0]:
            nrm = -nrm
        tt = (np.arange(n) + 0.5) * (L / n)
        row = A[None, :] + u[None, :] * tt[:, None] + nrm[None, :] * 0.5 * s
        items.append(([A, B], s, s, row))
    for ln in bl.get("lines", []):
        if ln.get("kind") == "channel" and bl.get("strip"):
            continue
        items.append((ln["line"], float(ln["near"]), float(ln["near"]),
                      _pairs(ln["line"], float(ln["near"]))))
    if bl.get("strip"):
        # one row of cells along the channel: points on the centre line and one spacing each
        # side, so the faces between the rows lie on the two bank lines
        c, h = np.asarray(bl["strip"]["centre"], float), float(bl["strip"]["h"])
        mid, nrm = strip_rows(c)
        rows = np.vstack([mid, mid + h * nrm, mid - h * nrm])
        # in a bend, a row point of one segment can fall on the bank line of the next
        for ln in bl.get("lines", []):
            if ln.get("kind") == "channel" and len(rows):
                rows = rows[poly_dist(rows[:, 0], rows[:, 1], ln["line"]) >= 0.4 * h]
        items.append((c, 1.5 * h, h, rows))
    for line, r, s, add in items:
        line = np.asarray(line, float)
        idx = _near_line(pts, keep, line, r)
        if len(idx):
            dd = poly_dist(pts[idx, 0], pts[idx, 1], line)
            keep[idx[dd < r]] = False
        new.append(add)
        new_s.append(np.full(len(add), s))
    removed = int((~keep).sum())
    if new:
        add = np.vstack(new)
        add_s = np.concatenate(new_s)
    else:
        add, add_s = np.zeros((0, 2)), np.zeros(0)
    ok = inside(add[:, 0], add[:, 1], perimeter) if len(add) else np.zeros(0, bool)
    if len(add):
        ok &= ring_dist(add[:, 0], add[:, 1], perimeter) >= PERIM_FRAC * add_s
    # earlier lines win where the points of two lines meet
    grid = {}
    g = max(float(add_s.min()) if len(add_s) else 1.0, 1e-6)
    kept_new = []
    for i in np.nonzero(ok)[0]:
        x, y = add[i]
        r = MIN_PAIR_FRAC * add_s[i]
        gi, gj = int(math.floor(x / g)), int(math.floor(y / g))
        close = False
        for a in range(gi - 2, gi + 3):
            for b in range(gj - 2, gj + 3):
                for q in grid.get((a, b), ()):
                    if math.hypot(q[0] - x, q[1] - y) < r:
                        close = True
                        break
                if close:
                    break
            if close:
                break
        if not close:
            grid.setdefault((gi, gj), []).append((x, y))
            kept_new.append(i)
    add = add[kept_new]
    # old points too close to a new one (different spacings meeting) go too
    rest = pts[keep]
    if len(add) and len(rest):
        try:
            from scipy.spatial import cKDTree
            dmin = cKDTree(add).query(rest)[0]
        except ImportError:                            # pragma: no cover - scipy is optional
            dmin = np.array([np.hypot(*(add - p).T).min() for p in rest])
        far = dmin >= MIN_PAIR_FRAC * float(add_s.min())
        removed += int((~far).sum())
        rest = rest[far]
    out = np.vstack([rest, add]) if len(add) else rest
    return out, {"points_removed": removed, "points_added": int(len(add)),
                 "points": int(len(out)), "_protected": np.r_[np.zeros(len(rest), bool),
                                                              np.ones(len(add), bool)]}


def share_on_faces(points, line, step=None, tol=0.1):
    """The share of a line (sampled every `step`) that lies on a cell face: a sample is on a
    face when it is within tol x the spacing of its two nearest points from the face between
    them (their perpendicular bisector)."""
    from scipy.spatial import cKDTree
    pts = np.asarray(points, float)
    line = np.asarray(line, float)
    step = step or max(length(line) / 2000.0, 1e-6)
    x = resample(line, step)
    d, i = cKDTree(pts).query(x, k=2)
    sep = np.hypot(*(pts[i[:, 1]] - pts[i[:, 0]]).T)
    off = (d[:, 1] ** 2 - d[:, 0] ** 2) / np.maximum(2.0 * sep, 1e-12)
    return float(np.mean(off <= tol * sep))


MAX_FACES = 8               # HEC-RAS: at most 8 faces to a cell (8 facepoint columns)


def limit_faces(points, perimeter, cell, protected=None, max_faces=MAX_FACES, passes=12):
    """Points whose Voronoi cell would have more than max_faces faces (HEC-RAS refuses them):
    a coarse point among fine ones (where the breakline points meet the grid) or a gmsh node of
    valence 9 or more. The point goes (its neighbours share its area); a protected point
    (a breakline pair) stays and its busiest unprotected neighbour goes instead; a protected
    point among protected ones only (a tight bend of the channel rows) is left and counted.
    Cells within 0.75 cell of the perimeter are left to hecras_model.mesh_check (HEC-RAS clips
    them to the perimeter, which changes their faces). Returns (points, removed, left over)."""
    try:
        from scipy.spatial import Voronoi
    except ImportError:                                # pragma: no cover - scipy is optional
        return np.asarray(points, float), 0, 0
    pts = np.asarray(points, float).reshape(-1, 2)
    prot = np.zeros(len(pts), bool) if protected is None else np.asarray(protected, bool).copy()
    inner = ring_dist(pts[:, 0], pts[:, 1], perimeter) > 0.75 * float(cell)
    removed = 0
    left = 0
    for _ in range(passes):
        vor = Voronoi(pts)
        rp = vor.ridge_points
        cnt = np.bincount(rp.ravel(), minlength=len(pts))
        bad = np.nonzero(inner & (cnt > max_faces))[0]
        if not len(bad):
            break
        drop = set(int(i) for i in bad if not prot[i])
        for i in bad:
            if prot[i]:
                nb = np.r_[rp[rp[:, 0] == i, 1], rp[rp[:, 1] == i, 0]]
                nb = nb[~prot[nb]]
                if len(nb):
                    drop.add(int(nb[np.argmax(cnt[nb])]))
        if not drop:
            break
        keep = np.ones(len(pts), bool)
        keep[list(drop)] = False
        pts, prot, inner = pts[keep], prot[keep], inner[keep]
        removed += len(drop)
    vor = Voronoi(pts)
    cnt = np.bincount(vor.ridge_points.ravel(), minlength=len(pts))
    left = int((inner & (cnt > max_faces)).sum())
    return pts, removed, left


def voronoi_precheck(points, perimeter, max_faces=MAX_FACES, clip=None, max_cells_per_fp=8):
    """HEC-RAS's own mesh limits checked before HEC-RAS sees the points (v2_gmsh, Absaraka
    2026-10-18: "48 cell(s) with more than 8 sides" in the property tables).
    The Voronoi diagram of the points; a cell whose corners are all inside the perimeter
    keeps its sides; a cell that reaches the perimeter is clipped to it by `clip(corners)`
    -> number of sides (hecras_model gives the OGR clip; without one its unclipped sides are
    used, which can only over-count). Also the face points (Voronoi corners) shared by more
    than `max_cells_per_fp` cells. On Absaraka v2_gmsh this flags 58 interior cells with 9
    sides (HEC-RAS counted 48: it never reports more than this check).
    Returns {'cells', 'interior', 'boundary', 'max_sides', 'over' (indices), 'over_interior',
    'over_boundary', 'facepoints_over', 'max_cells_per_facepoint'}."""
    from scipy.spatial import Voronoi
    pts = np.asarray(points, float).reshape(-1, 2)
    per = np.asarray(perimeter, float)
    lo, hi = per.min(axis=0), per.max(axis=0)
    pad = 10.0 * float(max(hi - lo))
    frame = np.array([[lo[0] - pad, lo[1] - pad], [hi[0] + pad, lo[1] - pad],
                      [hi[0] + pad, hi[1] + pad], [lo[0] - pad, hi[1] + pad]])
    vor = Voronoi(np.vstack([pts, frame]))
    vin = inside(vor.vertices[:, 0], vor.vertices[:, 1], per)
    sides = np.zeros(len(pts), np.int64)
    full = np.zeros(len(pts), bool)
    fp_count = {}
    for i in range(len(pts)):
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        if all(vin[r] for r in reg):
            full[i] = True
            sides[i] = len(reg)
            for r in reg:
                fp_count[r] = fp_count.get(r, 0) + 1
        elif clip is not None:
            sides[i] = int(clip(vor.vertices[reg]) or 0)
        else:
            sides[i] = len(reg)
    # qhull splits a corner shared by many cells into near-duplicates: merge them
    fp_over, fp_max = 0, 0
    if fp_count:
        ids = np.fromiter(fp_count.keys(), np.int64)
        key = np.round(vor.vertices[ids] / 1e-3).astype(np.int64)
        _, inv = np.unique(key, axis=0, return_inverse=True)
        merged = np.zeros(inv.max() + 1, np.int64)
        np.add.at(merged, inv.ravel(), np.fromiter(fp_count.values(), np.int64))
        # a corner of n cells is counted once per cell around it in each split copy
        groups = np.bincount(inv.ravel())
        cells_at = np.where(groups > 1, merged - 2 * (groups - 1), merged)
        fp_max = int(cells_at.max())
        fp_over = int((cells_at > max_cells_per_fp).sum())
    over = np.nonzero(sides > max_faces)[0]
    return {"cells": int(len(pts)), "interior": int(full.sum()),
            "boundary": int(len(pts) - full.sum()), "max_sides": int(sides.max()) if len(pts)
            else 0, "over": over, "over_interior": int(full[over].sum()),
            "over_boundary": int((~full[over]).sum()), "facepoints_over": fp_over,
            "max_cells_per_facepoint": fp_max}


def fix_faces(points, perimeter, protected=None, clip=None, max_faces=MAX_FACES, passes=12,
              nudge=True):
    """Make every cell have at most max_faces sides (voronoi_precheck): first a nudge (each
    bad point that is not a breakline pair moves to the middle of its own cell, one Lloyd
    step, which evens out its neighbours), then the bad point goes (its neighbours share its
    area); a protected point stays and its busiest unprotected neighbour goes instead.
    Returns (points, protected, {'nudged', 'removed', 'left', 'facepoints_over', 'check'})
    where 'check' is the final voronoi_precheck without the index array."""
    from scipy.spatial import Voronoi
    pts = np.asarray(points, float).reshape(-1, 2).copy()
    prot = np.zeros(len(pts), bool) if protected is None else np.asarray(protected, bool).copy()
    nudged = removed = 0
    for k in range(passes):
        chk = voronoi_precheck(pts, perimeter, max_faces, clip)
        bad = chk["over"]
        if not len(bad):
            break
        if nudge and k == 0:
            vor = Voronoi(pts)
            for i in bad:
                reg = vor.regions[vor.point_region[i]]
                if prot[i] or not reg or -1 in reg:
                    continue
                c = vor.vertices[reg].mean(axis=0)
                if inside(np.array([c[0]]), np.array([c[1]]), perimeter)[0]:
                    pts[i] = c
                    nudged += 1
            if nudged:
                continue
        vor = Voronoi(pts)
        rp = vor.ridge_points
        cnt = np.bincount(rp.ravel(), minlength=len(pts))
        drop = set(int(i) for i in bad if not prot[i])
        for i in bad:
            if prot[i]:
                nb = np.r_[rp[rp[:, 0] == i, 1], rp[rp[:, 1] == i, 0]]
                nb = nb[~prot[nb]]
                if len(nb):
                    drop.add(int(nb[np.argmax(cnt[nb])]))
        if not drop:
            break
        keep = np.ones(len(pts), bool)
        keep[list(drop)] = False
        pts, prot = pts[keep], prot[keep]
        removed += len(drop)
    chk = voronoi_precheck(pts, perimeter, max_faces, clip)
    left = int(len(chk["over"]))
    chk = {k: v for k, v in chk.items() if k != "over"}
    return pts, prot, {"nudged": int(nudged), "removed": int(removed), "left": left,
                       "facepoints_over": chk["facepoints_over"], "check": chk}
