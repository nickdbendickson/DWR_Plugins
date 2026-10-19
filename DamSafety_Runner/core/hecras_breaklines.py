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
* channel: the two banks of a one-cell-wide row (a full cell, v2c) along the run's flow path, from the end of the
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
# v3.10.22 (v2c, Absaraka v2b against v2, 2026-10-18):
# * the channel strip is one full cell wide (v2b: half a cell, 50 ft; its small cells carried
#   the main-channel velocity for 37,800 ft and held the time step at 6.25 s for 22 h of the
#   run instead of 12.5 s: 2,957 s against 1,628 s), and never narrower than this x cell;
CHANNEL_STRIP_FRAC = 1.0
# * the dam row is finer across the breach (v2b: 50 ft cells, so only 2 low cells fronted the
#   80.8 ft breach; HEC-RAS takes each connection segment's tailwater from the two cells at its
#   ends, and the dry embankment cells next to them held the tailwater up and the pool at
#   1,049.2 ft instead of draining to 1,046.9 ft), then growing by at most BREACH_GRADING a
#   cell to the dam spacing.
# v2d (Absaraka v2c / v2c_gmsh: 'weir breach lower than the cells they are connected to' at
#   station 809.6, volume errors 38.7 % / >= 12 %): v2c put a cell FACE on each breach bottom
#   corner (850 - 40.4 = 809.6, 20.2 ft cells from the centre), so the embankment cell next to
#   the corner, with no cut terrain in it, fronted the corner 2.8-3.4 ft above the invert. Now
#   the corners are cell CENTRES (cells of half / k, k whole, centred on the breach centre), so
#   the corner cell straddles the corner and the terrain cut reaches into it; and
#   BREACH_DAM_CELLS spans the bottom corner to corner (2: three cells of 40.4 ft, the
#   middle one inside the breach, instead of v2c's 20.2 ft cells, which held HEC-RAS's time step
#   at its floor, 6 s / 2^3 = 0.75 s, through the breach and at 1.5 s after it).
BREACH_DAM_CELLS = 2
ENFORCE_REACH = 1.01        # v2d: old points within this x the reach of a line go
BREACH_DAM_MIN_FRAC = 0.1   # ... but no finer than this x cell
BREACH_ROW_MIN_FRAC = 0.4   # ... nor than this x the dam spacing (v2d)
BREACH_GRADING = 1.3


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


def _poly_frame(line, xy):
    """v3.10.22: (station of xy, unit tangent there) on a polyline (2 points: the straight
    line, as before)."""
    p = np.asarray(line, float).reshape(-1, 2)
    if len(p) == 2:
        L = float(np.hypot(*(p[1] - p[0])))
        u = (p[1] - p[0]) / (L or 1.0)
        return float(np.dot(np.asarray(xy, float) - p[0], u)), u
    s = station_of(p, xy)
    c = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
    k = int(np.clip(np.searchsorted(c, s, side="right") - 1, 0, len(p) - 2))
    t = p[k + 1] - p[k]
    return s, t / (np.hypot(*t) or 1.0)


def _poly_points(line, st):
    """v3.10.22: points at stations st along a polyline and the unit tangents there."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]
    st = np.asarray(st, float)
    if len(p) == 2:
        u = (p[1] - p[0]) / (c[-1] or 1.0)
        return p[0][None, :] + u[None, :] * st[:, None], np.repeat(u[None, :], len(st), 0)
    k = np.clip(np.searchsorted(c, st, side="right") - 1, 0, len(p) - 2)
    t = p[k + 1] - p[k]
    t /= np.maximum(np.hypot(*t.T), 1e-12)[:, None]
    return p[k] + t * (st - c[k])[:, None], t


def plan(perimeter, cell, dam_line, downstream, breach=None, path=None, spacing=None):
    """The breaklines of the model. dam_line (A, B), or a polyline from A to B (v3.10.22, the
    crest): the dam connection; downstream: unit vector into the 2D area at the breach;
    breach: (centre on the dam line, half width of the cut, cut
    length[, half the breach bottom width, default the cut's]) or None; path: the run's flow path or None; spacing: {'dam', 'breach', 'channel'}
    near spacings (default GRID_NEAR_FRAC x cell). Returns {'dam': {...} or None, 'lines':
    [{'name', 'line', 'near', 'far', 'kind'}], 'skipped': [why]}."""
    cell = float(cell)
    sp = {k: GRID_NEAR_FRAC * cell for k in ("dam", "breach", "channel")}
    sp.update({k: float(v) for k, v in (spacing or {}).items() if v})
    dline = np.asarray(dam_line, float).reshape(-1, 2)
    A, B = dline[0], dline[-1]
    d = np.asarray(downstream, float)
    d = d / (np.hypot(*d) or 1.0)
    out = {"dam": {"line": dline.tolist(), "near": sp["dam"], "far": cell},
           "lines": [], "skipped": []}
    ab = (B - A) / (np.hypot(*(B - A)) or 1.0)
    cut_len = 0.0
    if breach is not None:
        C, cw, cut_len = np.asarray(breach[0], float), float(breach[1]), float(breach[2])
        if len(dline) > 2:
            ab = _poly_frame(dline, C)[1]           # the crest's own direction at the breach
        # v2d: the dam row is centred on the breach's own bottom corners (breach[3], half the
        # bottom width), which can be inside the cut when the cut is widened to a terrain cell
        rh = float(breach[3]) if len(breach) > 3 and breach[3] else cw
        s = min(sp["breach"], cw)               # the pairs of the two sides must not meet
        # v2d: a dam row whose cells are centred on the breach bottom corners (and on the
        # centre), BREACH_DAM_CELLS cells from corner to corner, never coarser than the dam
        # spacing; the breach lines start where that row ends
        sb = breach_row_spacing(rh, sp["dam"], cell)
        out["dam"]["breach"] = {"centre": C.tolist(), "half": rh, "near": sb}
        # the cut's sides cross the row in the corner cell (sb deep) or, when the cut is wider
        # than the bottom, in a coarser one (at most the dam spacing deep)
        t0 = sb if cw <= rh + 0.5 * sb + 1e-9 else max(sb, sp["dam"])
        if cut_len - t0 >= 2 * s:
            for nm, side in (("Breach left", -1.0), ("Breach right", 1.0)):
                ln = [C + ab * side * cw + d * t0, C + ab * side * cw + d * cut_len]
                out["lines"].append({"name": nm, "line": [list(map(float, q)) for q in ln],
                                     "near": s, "far": cell, "kind": "breach"})
        else:
            out["skipped"].append("breach: the cut is too short for breaklines")
    if path is not None and len(path) >= 2:
        h = max(sp["channel"], CHANNEL_STRIP_FRAC * cell)
        c = channel_centre(path, perimeter, dline, max(cut_len, 0.0) + h, cell, h)
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


def breach_row_spacing(half, dam_spacing, cell, n=None):
    """The dam row's cell width across the breach: half / k with k whole (so both bottom
    corners are cell centres), about 2 x half / n (n = BREACH_DAM_CELLS), at most the dam
    spacing and at least max(BREACH_DAM_MIN_FRAC x cell, BREACH_ROW_MIN_FRAC x the dam
    spacing). A bottom narrower than that floor gets one cell (k = 0) that holds both corners,
    each at least half the floor from its faces."""
    n = int(n or BREACH_DAM_CELLS)
    half = float(half)
    ds = float(dam_spacing)
    floor = min(ds, max(BREACH_DAM_MIN_FRAC * cell, BREACH_ROW_MIN_FRAC * ds))
    if half <= 0:
        return ds
    if half < floor - 1e-9:
        return min(max(floor, 2.0 * half + floor), max(ds, 2.0 * half + floor))
    target = min(ds, max(2.0 * half / max(n, 1), floor))
    k = max(1, min(int(math.ceil(half / target - 1e-9)), int(math.floor(half / floor + 1e-9))))
    return half / k


def dam_stations(L, s, tc, half, sb, grading=BREACH_GRADING):
    """Stations (cell centres) and cell widths of the dam row, 0..L along the dam: width sb
    over the breach, with a cell centred on the breach centre tc and on each bottom corner
    tc +- half (when half is a whole number of sb, see breach_row_spacing), so that no face
    lies on a corner (v2d; v2c put faces on the corners); then growing by at most `grading`
    a cell from sb to s, and the rest evenly at about s. Returns (centres, widths)."""
    k = max(0, int(round(float(half) / float(sb))))
    faces = [tc - 0.5 * sb, tc + 0.5 * sb] if 0.5 * sb < min(tc, L - tc) else [tc]
    for sign in (1.0, -1.0):
        f = tc + sign * 0.5 * sb
        side = []
        end = L if sign > 0 else 0.0
        ok = (lambda v: v < L - 1e-9) if sign > 0 else (lambda v: v > 1e-9)
        if not ok(f):
            continue
        w = sb
        for _ in range(k):                         # the fine cells out to the corner cell
            nxt = f + sign * sb
            if not ok(nxt):
                break
            side.append(nxt)
            f = nxt
        while w < s - 1e-9:                         # grading up to the dam spacing
            w = min(s, w * grading)
            nxt = f + sign * w
            if not ok(nxt):
                break
            side.append(nxt)
            f = nxt
        rem = abs(end - f)
        m = max(1, int(round(rem / s))) if rem > 1e-9 else 0
        if m and rem / m < 0.5 * w:                # a sliver at the end: join it
            m = max(1, m - 1)
        for j in range(1, m):
            side.append(f + sign * rem * j / m)
        if sign > 0:
            faces = faces + side
        else:
            faces = side[::-1] + faces
    faces = np.r_[0.0, np.asarray(faces, float), float(L)]
    faces = np.unique(np.clip(faces, 0.0, float(L)))
    faces = faces[np.r_[True, np.diff(faces) > 1e-9]]
    centres = 0.5 * (faces[1:] + faces[:-1])
    return centres, np.diff(faces)


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
        dl = np.asarray(dam["line"], float).reshape(-1, 2)
        A, B = dl[0], dl[-1]
        s = float(dam["near"])
        L = float(length(dl)) if len(dl) > 2 else float(np.hypot(*(B - A)))
        n = max(1, int(round(L / s)))
        u = (B - A) / (float(np.hypot(*(B - A))) or 1.0)
        # the 2D area's side of the dam line: the normal pointing inside
        sgn = 1.0
        mid, um = _poly_points(dl, [0.5 * L])
        mid, um = mid[0], um[0]
        nrm = np.array([-um[1], um[0]])
        if not inside(np.array([mid[0] + nrm[0] * s]), np.array([mid[1] + nrm[1] * s]),
                      perimeter)[0]:
            sgn = -1.0
        if dam.get("breach"):
            tt, ss = dam_stations(L, s, _poly_frame(dl, dam["breach"]["centre"])[0],
                                  float(dam["breach"]["half"]), float(dam["breach"]["near"]))
        else:
            tt = (np.arange(n) + 0.5) * (L / n)
            ss = np.full(n, s)
        # v3.10.22: along a polyline (the crest), each point half its cell inside, on the normal
        # of its own segment
        q, tq = _poly_points(dl, tt)
        nq = sgn * np.column_stack([-tq[:, 1], tq[:, 0]])
        row = q + nq * 0.5 * ss[:, None]
        items.append((dl, s, float(ss.min()), row))
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
        # v2d: 1 % past r too (gmsh lays a row of nodes exactly 2 spacings off the dam line,
        # which is r for a 50 ft dam row: Absaraka v2d_gmsh kept them 25 ft from the row)
        idx = _near_line(pts, keep, line, ENFORCE_REACH * r)
        if len(idx):
            dd = poly_dist(pts[idx, 0], pts[idx, 1], line)
            keep[idx[dd < ENFORCE_REACH * r]] = False
        new.append(add)
        new_s.append(np.full(len(add), s))
    # v2d: nothing but the breach pairs inside the breach cut (Absaraka v2d_gmsh: a gmsh node on
    # the breach centre line, exactly one spacing from both sides, survived 20 ft from the
    # pairs in the fastest flow there is)
    br = [ln for ln in bl.get("lines", []) if ln.get("kind") == "breach"]
    if len(br) == 2:
        a_, b_ = (np.asarray(ln["line"], float) for ln in br)
        quad = np.vstack([a_[0], a_[-1], b_[-1], b_[0]])
        dam_side = dam["line"] if dam else None
        if dam_side is not None:
            # from the dam line itself, so the cut's first metres are covered too
            ds_ = np.asarray(dam_side, float).reshape(-1, 2)
            if len(ds_) == 2:
                A_, B_ = ds_
                u_ = (B_ - A_) / (np.hypot(*(B_ - A_)) or 1.0)
                pa = A_ + u_ * float(np.dot(a_[0] - A_, u_))
                pb = A_ + u_ * float(np.dot(b_[0] - A_, u_))
            else:
                # v3.10.22: the crest is straight across the breach: its segment's frame there
                s_, u_ = _poly_frame(ds_, 0.5 * (a_[0] + b_[0]))
                c_ = _poly_points(ds_, [s_])[0][0]
                pa = c_ + u_ * float(np.dot(a_[0] - c_, u_))
                pb = c_ + u_ * float(np.dot(b_[0] - c_, u_))
            quad = np.vstack([pa, a_[-1], b_[-1], pb])
        inq = keep & inside(pts[:, 0], pts[:, 1], quad)
        keep[inq] = False
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


def voronoi_precheck(points, perimeter, max_faces=MAX_FACES, clip=None, max_cells_per_fp=8,
                     perimeter_tol=None):
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
    pieces = np.ones(len(pts), np.int64)
    pface = np.full(len(pts), np.inf)
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
            c = clip(vor.vertices[reg])
            if isinstance(c, dict):
                sides[i] = int(c.get("sides") or 0)
                pieces[i] = int(c.get("pieces") or 0)
                pf = c.get("perimeter_faces") or []
                if pf:
                    pface[i] = float(min(pf))
            else:
                sides[i] = int(c or 0)
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
    per_chk = perimeter_check(pts, per, pieces, pface, full, perimeter_tol)
    return {"cells": int(len(pts)), "interior": int(full.sum()),
            "boundary": int(len(pts) - full.sum()), "max_sides": int(sides.max()) if len(pts)
            else 0, "over": over, "over_interior": int(full[over].sum()),
            "over_boundary": int((~full[over]).sum()), "facepoints_over": fp_over,
            "max_cells_per_facepoint": fp_max, **per_chk}


# v3.10.22 (Absaraka v2b_gmsh, 2026-10-18: "2D Flow Area: Downstream - Error generating
# Mesh ... 1 face errors detected on the perimeter of the mesh"): HEC-RAS also refuses a
# perimeter cell that the perimeter cuts in two, and chokes on a perimeter face (the piece of
# the perimeter inside one cell) of a fraction of a foot, which comes from a perimeter corner
# that sits almost on a cell edge. Tolerances, as fractions of the local point spacing with a
# floor in model units (hecras_model passes 5 ft / 0.5 ft in the model's units).
PERIM_SHORT_FRAC = 0.02      # a perimeter face shorter than 2 % of the spacing ...
PERIM_SHORT_MIN = 5.0        # ... or than 5 ft (HEC-RAS's own example meshes: 6.2 ft at least)
PERIM_VERTEX_FRAC = 0.005    # a perimeter corner within 0.5 % of the spacing of a cell edge
PERIM_VERTEX_MIN = 0.5       # ... or within 0.5 ft


def _local_spacing(pts):
    from scipy.spatial import cKDTree
    if len(pts) < 2:
        return np.ones(len(pts))
    d, _ = cKDTree(pts).query(pts, k=2)
    return d[:, 1]


def vertex_edge_distance(pts, perimeter, k=4):
    """For each perimeter corner: the seed whose cell holds it, the neighbour whose shared
    cell edge is nearest, and the distance from the corner to that edge (the bisector of the
    two seeds). Returns (dist, own, other)."""
    from scipy.spatial import cKDTree
    per = np.asarray(perimeter, float)
    k = int(min(k, len(pts)))
    if k < 2:
        n = len(per)
        return np.full(n, np.inf), np.zeros(n, np.int64), np.zeros(n, np.int64)
    d, j = cKDTree(pts).query(per, k=k)
    p1 = pts[j[:, 0]]
    best = np.full(len(per), np.inf)
    other = j[:, 1].copy()
    for m in range(1, k):
        pj = pts[j[:, m]]
        sep = np.hypot(*(pj - p1).T)
        with np.errstate(divide="ignore", invalid="ignore"):
            bd = (d[:, m] ** 2 - d[:, 0] ** 2) / (2.0 * sep)
        bd = np.where(sep > 0, bd, np.inf)
        better = bd < best
        best = np.where(better, bd, best)
        other = np.where(better, j[:, m], other)
    return best, j[:, 0], other


def perimeter_check(pts, perimeter, pieces=None, pface=None, full=None, tol=None):
    """The perimeter part of voronoi_precheck: cells split in two by the perimeter
    ('split'), cells with a perimeter face shorter than max(PERIM_SHORT_FRAC x spacing,
    PERIM_SHORT_MIN) ('short'), and perimeter corners within max(PERIM_VERTEX_FRAC x spacing,
    PERIM_VERTEX_MIN) of a cell edge ('near_vertex': rows of corner index, seed, neighbour,
    distance). tol = (short_min, vertex_min) in model units; default 5 and 0.5 (feet)."""
    pts = np.asarray(pts, float).reshape(-1, 2)
    per = np.asarray(perimeter, float)
    n = len(pts)
    smin, vmin = tol if tol is not None else (PERIM_SHORT_MIN, PERIM_VERTEX_MIN)
    sp = _local_spacing(pts)
    pieces = np.ones(n, np.int64) if pieces is None else pieces
    pface = np.full(n, np.inf) if pface is None else pface
    split = np.nonzero(pieces > 1)[0]
    stol = np.maximum(PERIM_SHORT_FRAC * sp, smin)
    short = np.nonzero(pface < stol)[0]
    vd, own, oth = vertex_edge_distance(pts, per)
    vtol = np.maximum(PERIM_VERTEX_FRAC * sp[own], vmin) if n else np.zeros(0)
    nv = np.nonzero(vd < vtol)[0]
    fin = pface[np.isfinite(pface)]
    return {"split": split, "short": short,
            "near_vertex": np.c_[nv, own[nv], oth[nv]].astype(np.int64) if len(nv)
            else np.zeros((0, 3), np.int64),
            "near_vertex_dist": vd[nv],
            "split_cells": int(len(split)), "short_perimeter_faces": int(len(short)),
            "perimeter_corners_near_edge": int(len(nv)),
            "min_perimeter_face": float(fin.min()) if len(fin) else None,
            "min_corner_to_edge": float(vd.min()) if len(vd) else None}


_ARRAY_KEYS = ("over", "split", "short", "near_vertex", "near_vertex_dist")


def precheck_summary(chk):
    """voronoi_precheck without its index arrays (for reports and JSON)."""
    out = {k: v for k, v in chk.items() if k not in _ARRAY_KEYS}
    for k in ("min_perimeter_face", "min_corner_to_edge"):
        if out.get(k) is not None:
            out[k] = round(float(out[k]), 3)
    return out


def perimeter_bad(chk):
    return chk["split_cells"] + chk["short_perimeter_faces"] + chk["perimeter_corners_near_edge"]


def fix_faces(points, perimeter, protected=None, clip=None, max_faces=MAX_FACES, passes=12,
              nudge=True, perimeter_tol=None):
    """Make the layout pass voronoi_precheck: every cell at most max_faces sides, and (v2c)
    no perimeter cell split in two, no perimeter face shorter than the tolerance, and no
    perimeter corner almost on a cell edge.
    Over-sided cells: first a nudge (each bad point that is not a breakline pair moves to the
    middle of its own cell, one Lloyd step, which evens out its neighbours), then the bad
    point goes (its neighbours share its area); a protected point stays and its busiest
    unprotected neighbour goes instead.
    Perimeter: a split cell or one with a sliver perimeter face loses its point (its
    neighbours take the strip of perimeter over; a protected point keeps its place and its
    nearest unprotected neighbour goes); a corner within the tolerance of a cell edge moves
    that edge off it by nudging the corner's own point (or, if that one is protected, the
    neighbour) 3 tolerances along the line to the corner.
    Returns (points, protected, {'nudged', 'removed', 'left', 'facepoints_over',
    'perimeter_nudged', 'perimeter_removed', 'perimeter_left', 'perimeter_found', 'check'})
    where 'check' is the final voronoi_precheck without its index arrays and
    'perimeter_found' the first check's perimeter counts."""
    from scipy.spatial import Voronoi
    pts = np.asarray(points, float).reshape(-1, 2).copy()
    per = np.asarray(perimeter, float)
    prot = np.zeros(len(pts), bool) if protected is None else np.asarray(protected, bool).copy()
    nudged = removed = pnudged = premoved = 0
    found = None
    smin, vmin = perimeter_tol if perimeter_tol is not None else (PERIM_SHORT_MIN,
                                                                   PERIM_VERTEX_MIN)
    for k in range(passes):
        chk = voronoi_precheck(pts, per, max_faces, clip, perimeter_tol=perimeter_tol)
        if found is None:
            found = {key: chk[key] for key in ("split_cells", "short_perimeter_faces",
                                                "perimeter_corners_near_edge")}
        bad = chk["over"]
        pbad = np.union1d(chk["split"], chk["short"]).astype(np.int64)
        nv = chk["near_vertex"]
        if not len(bad) and not len(pbad) and not len(nv):
            break
        # perimeter corners near an edge: move the edge (cheap, never removes a point)
        if len(nv):
            sp = _local_spacing(pts)
            for vi, a, b in nv:
                tol = max(PERIM_VERTEX_FRAC * sp[a], vmin)
                # v3.10.22: when both are protected (two dam-row points meeting at a crest
                # vertex of a polyline dam line), the corner's own point moves anyway: 3
                # tolerances is a small fraction of a row cell, and a corner on a cell edge
                # stops HEC-RAS ('face errors detected on the perimeter of the mesh')
                both = bool(prot[a] and prot[b])
                for q, sign in ((a, 1.0), (b, -1.0)):
                    if prot[q] and not (both and q == a):
                        continue
                    dv = per[vi] - pts[q]
                    L = float(np.hypot(*dv))
                    if L <= 0:
                        continue
                    step = min(3.0 * tol, 0.25 * L)
                    new = pts[q] + sign * dv / L * step
                    if inside(np.array([new[0]]), np.array([new[1]]), per)[0]:
                        pts[q] = new
                        pnudged += 1
                        break
        drop = set()
        if len(pbad):
            from scipy.spatial import cKDTree
            tree = cKDTree(pts)
            for i in pbad:
                if not prot[i]:
                    drop.add(int(i))
                    continue
                _, nb = tree.query(pts[i], k=min(7, len(pts)))
                nb = [int(j) for j in np.atleast_1d(nb)[1:] if not prot[j]]
                if nb:
                    drop.add(nb[0])
            premoved += len(drop)
        if len(bad):
            if nudge and k == 0 and not drop:
                vor = Voronoi(pts)
                nn = 0
                for i in bad:
                    reg = vor.regions[vor.point_region[i]]
                    if prot[i] or not reg or -1 in reg:
                        continue
                    c = vor.vertices[reg].mean(axis=0)
                    if inside(np.array([c[0]]), np.array([c[1]]), per)[0]:
                        pts[i] = c
                        nn += 1
                nudged += nn
                if nn:
                    continue
            vor = Voronoi(pts)
            rp = vor.ridge_points
            cnt = np.bincount(rp.ravel(), minlength=len(pts))
            ob = set(int(i) for i in bad if not prot[i])
            for i in bad:
                if prot[i]:
                    nb = np.r_[rp[rp[:, 0] == i, 1], rp[rp[:, 1] == i, 0]]
                    nb = nb[~prot[nb]]
                    if len(nb):
                        ob.add(int(nb[np.argmax(cnt[nb])]))
            removed += len(ob - drop)
            drop |= ob
        if len(pts) - len(drop) < 4:                     # a degenerate layout: stop
            break
        if not drop and not len(nv):
            break
        if drop:
            keep = np.ones(len(pts), bool)
            keep[list(drop)] = False
            pts, prot = pts[keep], prot[keep]
    chk = voronoi_precheck(pts, per, max_faces, clip, perimeter_tol=perimeter_tol)
    left = int(len(chk["over"]))
    pleft = int(len(chk["split"]) + len(chk["short"]))
    summ = precheck_summary(chk)
    return pts, prot, {"nudged": int(nudged), "removed": int(removed), "left": left,
                       "facepoints_over": summ["facepoints_over"],
                       "perimeter_nudged": int(pnudged), "perimeter_removed": int(premoved),
                       "perimeter_left": pleft,
                       "perimeter_corners_left": int(len(chk["near_vertex"])),
                       "perimeter_found": found or {}, "check": summ}


# v3.10.22 (v2d; Absaraka v2c and v2c_gmsh, 2026-10-18: HEC-RAS 'SA 2D Connection at Dam has a
# weir breach lower than the cells they are connected to', weir station 809.6 at 1,046.43 ft
# against cell 62974 at 1,049.26 ft, and volume errors of 38.7 % / >= 12 %, against 0.5 % in
# v2 and v2b): every cell that fronts the breach bottom on the dam connection must reach the
# final breach invert. v2c's fine dam row put a cell face exactly on each breach bottom corner
# (station 809.6 = 850 - 40.4), so the embankment cell next to it fronted the corner and its
# ground (no cut pixel in it) was 2.8 ft above the invert.
BREACH_CELL_TOL = 0.01      # model units: a cell may sit this much above the invert
BREACH_STATION_TOL = 0.5    # a face end this close to a bottom corner counts as fronting it


def connection_cells(points, perimeter, conn_line, step=0.1):
    """The cells that front a connection on the 2D area's perimeter: the line sampled every
    `step` (stations from its first point), each sample moved a hair into the 2D area, and the
    nearest point (Voronoi). Returns [(cell index, station from, station to)] in order."""
    from scipy.spatial import cKDTree
    pts = np.asarray(points, float).reshape(-1, 2)
    ln = np.asarray(conn_line, float).reshape(-1, 2)
    per = np.asarray(perimeter, float)
    seg = np.hypot(*np.diff(ln, axis=0).T)
    along = np.r_[0.0, np.cumsum(seg)]
    if along[-1] <= 0:
        return []
    st = np.arange(0.0, along[-1] + 1e-9, step)
    if st[-1] < along[-1] - 1e-9:
        st = np.r_[st, along[-1]]
    xy = np.column_stack([np.interp(st, along, ln[:, 0]), np.interp(st, along, ln[:, 1])])
    k = np.clip(np.searchsorted(along, st, side="right") - 1, 0, len(seg) - 1)
    t = (ln[k + 1] - ln[k]) / np.maximum(seg[k], 1e-12)[:, None]
    n = np.column_stack([-t[:, 1], t[:, 0]])
    eps = 1e-3 * max(step, 1e-6)
    mid = len(st) // 2
    probe = xy[mid] + n[mid] * 10 * eps
    if not inside(np.array([probe[0]]), np.array([probe[1]]), per)[0]:
        n = -n
    own = cKDTree(pts).query(xy + n * eps)[1]
    out = []
    i0 = 0
    for i in range(1, len(st) + 1):
        if i == len(st) or own[i] != own[i0]:
            # the face end lies between the last sample of one cell and the first of the next
            a = st[i0] if i0 == 0 else 0.5 * (st[i0 - 1] + st[i0])
            b = st[-1] if i == len(st) else 0.5 * (st[i - 1] + st[i])
            out.append((int(own[i0]), float(a), float(b)))
            i0 = i
    return out


def bilinear(raster, xy):
    """Bilinear sample of a north-up raster (array with NaN, x0, ytop, pixel) at points, the
    terrain cell centres as the nodes (NaN outside; a NaN corner falls back to the nearest)."""
    a, x0, y0, pix = raster
    a = np.asarray(a, float)
    xy = np.asarray(xy, float).reshape(-1, 2)
    fx = (xy[:, 0] - x0) / pix - 0.5
    fy = (y0 - xy[:, 1]) / pix - 0.5
    ny, nx = a.shape
    c0 = np.clip(np.floor(fx).astype(int), 0, max(nx - 2, 0))
    r0 = np.clip(np.floor(fy).astype(int), 0, max(ny - 2, 0))
    c1, r1 = np.minimum(c0 + 1, nx - 1), np.minimum(r0 + 1, ny - 1)
    tx, ty = np.clip(fx - c0, 0, 1), np.clip(fy - r0, 0, 1)
    v = (a[r0, c0] * (1 - tx) * (1 - ty) + a[r0, c1] * tx * (1 - ty) +
         a[r1, c0] * (1 - tx) * ty + a[r1, c1] * tx * ty)
    bad = ~np.isfinite(v)
    if bad.any():
        rr = np.clip(np.round(fy).astype(int), 0, ny - 1)
        cc = np.clip(np.round(fx).astype(int), 0, nx - 1)
        v[bad] = a[rr[bad], cc[bad]]
    v[(fx < -0.5) | (fy < -0.5) | (fx > nx - 0.5) | (fy > ny - 0.5)] = np.nan
    return v


def cell_min_elevations(points, cells, raster, perimeter=None, sub=None):
    """The lowest ground in each of `cells` (indices into points): the terrain sampled
    bilinearly every `sub` (default the smaller of 1/16 of a terrain cell and 1/40 of the
    cell's spacing to its nearest neighbour) over the cell (the part of the
    Voronoi cell inside the perimeter). On Absaraka v2c / v2c_gmsh this gives 1,049.74 /
    1,050.30 ft for the cells HEC-RAS reported at 1,049.26 / 1,049.78 ft (a terrain-cell
    minimum gives 1,046.43, which HEC-RAS does not use), so it errs on the high (strict)
    side. Returns {cell: z}."""
    from scipy.spatial import cKDTree
    pts = np.asarray(points, float).reshape(-1, 2)
    pix = float(raster[3])
    tree = cKDTree(pts)
    out = {}
    for c in sorted(set(int(v) for v in cells)):
        k = int(min(9, len(pts)))
        d, _ = tree.query(pts[c], k=k)
        d = np.atleast_1d(d)
        R = float(np.max(d)) if k > 1 else pix
        h = float(sub) if sub else min(pix / 16.0, (d[1] if k > 1 else pix) / 40.0)
        g = np.arange(-R, R + 1e-9, max(h, 1e-6))
        X, Y = np.meshgrid(pts[c, 0] + g, pts[c, 1] + g)
        xy = np.column_stack([X.ravel(), Y.ravel()])
        m = tree.query(xy)[1] == c
        if perimeter is not None:
            m &= inside(xy[:, 0], xy[:, 1], perimeter)
        xy = xy[m] if m.any() else pts[c:c + 1]
        z = bilinear(raster, xy)
        out[c] = float(np.nanmin(z)) if np.isfinite(z).any() else float("nan")
    return out


def station_of(line, xy):
    """Station along a polyline of the point's projection on it."""
    s = np.asarray(line, float).reshape(-1, 2)
    p = np.asarray(xy, float)
    best, st, acc = np.inf, 0.0, 0.0
    for a, b in zip(s[:-1], s[1:]):
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 <= 0 else float(np.clip((p - a) @ ab / L2, 0.0, 1.0))
        dd = float(np.hypot(*(a + t * ab - p)))
        if dd < best:
            best, st = dd, acc + t * math.sqrt(L2)
        acc += math.sqrt(L2)
    return st


def breach_cell_check(points, perimeter, conn_line, bottom_lo, bottom_hi, invert, raster,
                      top=None, side_slope=None, tol=BREACH_CELL_TOL,
                      station_tol=BREACH_STATION_TOL, step=0.1):
    """HEC-RAS's 'weir breach lower than the cells they are connected to', before HEC-RAS: the
    cells whose connection face overlaps the final breach bottom (stations bottom_lo..bottom_hi,
    a face that ends within station_tol of a corner counts too, as HEC-RAS may give the corner to
    either cell) and their lowest ground against the final invert. With top and side_slope, the
    cells along the breach sides (up to the top of dam) are listed as well, against the lowest
    breach elevation over their face, as a warning only.
    Returns {'ok', 'invert', 'reach', 'cells': [{'cell', 'from', 'to', 'min_elev', 'breach_z',
    'above', 'part'}], 'bad' (bottom cells above the invert), 'worst_above', 'side_warn'}."""
    lo, hi = float(min(bottom_lo, bottom_hi)), float(max(bottom_lo, bottom_hi))
    runs = connection_cells(points, perimeter, conn_line, step)
    slo, shi = lo, hi
    if top is not None and side_slope:
        w = float(side_slope) * max(0.0, float(top) - float(invert))
        slo, shi = lo - w, hi + w
    pick = [(c, a, b) for c, a, b in runs if b > slo - station_tol and a < shi + station_tol]
    zmin = cell_min_elevations(points, [c for c, _, _ in pick], raster, perimeter)
    rows = []
    for c, a, b in pick:
        bottom = b >= lo - station_tol and a <= hi + station_tol
        if bottom:
            bz = float(invert)
        elif not side_slope:
            continue
        else:
            # the lowest breach elevation over the face: at its end nearer the bottom
            near = b if b < lo else a
            bz = float(invert) + abs(near - (lo if b < lo else hi)) / float(side_slope)
        z = zmin[c]
        rows.append({"cell": int(c), "from": round(a, 2), "to": round(b, 2),
                     "min_elev": round(z, 3), "breach_z": round(bz, 3),
                     "above": round(z - bz, 3), "part": "bottom" if bottom else "side"})
    bad = [r for r in rows if r["part"] == "bottom" and not r["above"] <= tol]
    side = [r for r in rows if r["part"] == "side" and not r["above"] <= tol]
    bot = [r["above"] for r in rows if r["part"] == "bottom"]
    return {"ok": not bad, "invert": round(float(invert), 3), "reach": [round(lo, 2), round(hi, 2)],
            "tol": tol, "station_tol": station_tol, "cells": rows, "bad": len(bad),
            "bad_cells": bad, "worst_above": round(max(bot), 3) if bot else None,
            "bottom_cells": len(bot), "side_warn": len(side)}
