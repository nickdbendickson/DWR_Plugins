"""Optional gmsh mesher for the ANUGA tier (ANUGA_MESHER = "gmsh").

Small triangles only along breaklines (roads/rail embankments, structures,
culverts, the breach and dam) and the flow path; large ones elsewhere. The
mesh goes to the ANUGA worker as plain arrays (mesh_points / mesh_triangles /
mesh_boundary) and is built there with anuga.Domain(points, vertices, boundary).

This file is also a stand-alone script, run in the ANUGA Python environment
(gmsh is GPL-2+, an optional runtime dependency: install it there with
`pip install gmsh`; it is never bundled with or imported by the plugin in QGIS):

    python gmsh_mesher.py mesh_in.json mesh_out.npz

Only numpy (and gmsh for build_mesh) are needed. All coordinates are model CRS
metres. The default ANUGA mesh does not use anything in this file.
"""
import json
import math
import sys
import time

import numpy as np

TAG_OUTFLOW = "outflow"
# Quality gate: the smallest triangle may be this fraction of the smallest size the
# mesher was asked for (min_area_m2, or culvert_area_m2 when that is smaller). gmsh
# scatters triangle sizes about +-30 % around the size field; at Absaraka (20 x 20 mi,
# 100 m2 lines) a good mesh (min angle 31 deg) had its smallest triangle at 49.9 m2,
# 0.4993 x 100 m2, so the old 0.5 rejected it. Slivers are caught by min_angle_deg.
MIN_AREA_TOL = 0.45


class MeshError(Exception):
    """The gmsh mesh could not be made or failed the quality gate."""


def h_of_area(area_m2):
    """Edge of an equilateral triangle with this area."""
    return math.sqrt(4.0 * float(area_m2) / math.sqrt(3.0))


# ------------------------------------------------------------------ line clean-up
def _thin(line, step):
    """Drop interior vertices closer than `step` to the last kept one (the
    Runner densifies breaklines for ANUGA's mesher; a vertex every few metres
    would force tiny triangles here)."""
    p = _dp(np.asarray(line, float).reshape(-1, 2), 0.5)
    keep = [p[0]]
    for q in p[1:-1]:
        if np.hypot(*(q - keep[-1])) >= step:
            keep.append(q)
    if len(keep) > 1 and np.hypot(*(p[-1] - keep[-1])) < 0.5 * step:
        keep.pop()
    keep.append(p[-1])
    return np.array(keep).tolist()


def _dp(pts, tol):
    """Douglas-Peucker: straight runs become one segment."""
    if len(pts) < 3:
        return pts
    keep = np.zeros(len(pts), bool)
    keep[[0, -1]] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, ab = pts[i], pts[j] - pts[i]
        n = np.hypot(*ab)
        seg = pts[i + 1:j]
        d = (np.hypot(*(seg - a).T) if n == 0 else
             np.abs(ab[0] * (seg[:, 1] - a[1]) - ab[1] * (seg[:, 0] - a[0])) / n)
        k = int(np.argmax(d))
        if d[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return pts[keep]


def _densify(line, step):
    p = np.asarray(line, float).reshape(-1, 2)
    out = [p[0]]
    for a, b in zip(p[:-1], p[1:]):
        n = max(1, int(math.ceil(np.hypot(*(b - a)) / step)))
        out += [a + (b - a) * k / n for k in range(1, n + 1)]
    return np.array(out)


class _SegIndex:
    """Grid hash of line segments for 'what is within d of this point' queries."""

    def __init__(self, cell):
        self.cell = float(cell)
        self.cells = {}
        self.a, self.b, self.owner = [], [], []

    def add(self, line, owner):
        p = np.asarray(line, float)
        for a, b in zip(p[:-1], p[1:]):
            k = len(self.a)
            self.a.append(a)
            self.b.append(b)
            self.owner.append(owner)
            lo = np.floor(np.minimum(a, b) / self.cell).astype(int)
            hi = np.floor(np.maximum(a, b) / self.cell).astype(int)
            for gx in range(lo[0] - 1, hi[0] + 2):
                for gy in range(lo[1] - 1, hi[1] + 2):
                    self.cells.setdefault((gx, gy), []).append(k)

    def near(self, p):
        """(distance, projection, unit direction, owner) of the nearest segment, or None."""
        ks = self.cells.get((int(math.floor(p[0] / self.cell)), int(math.floor(p[1] / self.cell))))
        if not ks:
            return None
        a = np.array([self.a[k] for k in ks])
        b = np.array([self.b[k] for k in ks])
        ab = b - a
        L2 = np.maximum((ab ** 2).sum(1), 1e-12)
        t = np.clip(((p - a) * ab).sum(1) / L2, 0, 1)
        q = a + ab * t[:, None]
        d = np.hypot(*(p - q).T)
        i = int(np.argmin(d))
        return float(d[i]), q[i], ab[i] / math.sqrt(L2[i]), self.owner[ks[i]]


def declutter(lines, gap, max_angle_deg=30.0):
    """Make a set of breaklines safe to embed (real road and rail layers have them):
    - a stretch of a line running within `gap` of, and within max_angle_deg of parallel
      to, a line already kept (double tracks, sidings, a road beside the rail, divided
      highways, the same road twice) is dropped; it would make sliver triangles;
    - an end that stops within `gap` of another kept line (a T-junction drawn a few
      metres short) is moved onto that line and just past it, so node_lines joins them.
    Lines earlier in the list win. Crossings at larger angles are left to node_lines.
    Returns [(polyline, index of the input line it came from)]."""
    if not lines:
        return []
    cos_max = math.cos(math.radians(max_angle_deg))
    idx = _SegIndex(max(4.0 * gap, 50.0))
    runs, src = [], []
    for li, ln in enumerate(lines):
        p = _densify(ln, max(gap / 2.0, 1.0))
        if len(p) < 2:
            continue
        t = np.gradient(p, axis=0)
        t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-12)[:, None]
        cov = np.zeros(len(p), bool)
        for i, q in enumerate(p):
            r = idx.near(q)
            if r is not None and r[0] < gap and abs(float(np.dot(t[i], r[2]))) > cos_max:
                cov[i] = True
        new, i = [], 0
        while i < len(p):
            if cov[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(p) and not cov[j + 1]:
                j += 1
            seg = p[i:j + 1]
            if len(seg) >= 2 and np.hypot(*np.diff(seg, axis=0).T).sum() >= gap:
                new.append(seg)
            i = j + 1
        for seg in new:
            idx.add(seg, len(runs))
            runs.append(seg)
            src.append(li)
    # snap loose ends onto a nearby line
    out = []
    all_idx = _SegIndex(max(4.0 * gap, 50.0))
    for k, r in enumerate(runs):
        all_idx.add(r, k)
    for k, r in enumerate(runs):
        r = r.copy()
        for end, prev in ((0, 1), (-1, -2)):
            q = r[end]
            best = None
            ks = all_idx.cells.get((int(math.floor(q[0] / all_idx.cell)),
                                    int(math.floor(q[1] / all_idx.cell))), [])
            for s_ in ks:
                if all_idx.owner[s_] == k:
                    continue
                a, b = all_idx.a[s_], all_idx.b[s_]
                ab = b - a
                tt = float(np.clip(np.dot(q - a, ab) / max(np.dot(ab, ab), 1e-12), 0, 1))
                pr = a + ab * tt
                d = float(np.hypot(*(q - pr)))
                if d < gap and (best is None or d < best[0]):
                    best = (d, pr)
            if best is not None and best[0] > 1e-6:
                u = best[1] - q
                u = u / max(np.hypot(*u), 1e-12)
                r[end] = best[1] + 0.5 * u          # just across: a proper crossing
        out.append((r, src[k]))
    return out


def _seg_list(lines):
    segs = []
    for k, ln in enumerate(lines):
        p = np.asarray(ln, float).reshape(-1, 2)
        for i in range(len(p) - 1):
            if np.hypot(*(p[i + 1] - p[i])) > 0:
                segs.append((p[i], p[i + 1], k))
    return segs


def node_lines(lines, kinds, snap_m):
    """Join crossing lines at shared points and snap nearby points.

    lines: list of polylines; kinds: same-length list of class labels
    ("line" or "channel"). Returns (nodes (N, 2), edges (E, 2) int, edge_kind
    list). Every crossing becomes a shared node; nodes closer than snap_m are
    merged; zero-length and duplicate edges are dropped."""
    segs = _seg_list(lines)
    if not segs:
        return np.zeros((0, 2)), np.zeros((0, 2), int), []
    A = np.array([s[0] for s in segs])
    B = np.array([s[1] for s in segs])
    cuts = [[0.0, 1.0] for _ in segs]
    d = B - A
    for i in range(len(segs)):
        # proper intersections of segment i with every later segment
        j = np.arange(i + 1, len(segs))
        if not len(j):
            break
        den = d[i, 0] * d[j, 1] - d[i, 1] * d[j, 0]
        ok = np.abs(den) > 1e-12
        w = A[j] - A[i]
        t = np.where(ok, (w[:, 0] * d[j, 1] - w[:, 1] * d[j, 0]) / np.where(ok, den, 1), -1)
        u = np.where(ok, (w[:, 0] * d[i, 1] - w[:, 1] * d[i, 0]) / np.where(ok, den, 1), -1)
        hit = ok & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)
        for jj, tt, uu in zip(j[hit], t[hit], u[hit]):
            cuts[i].append(float(tt))
            cuts[jj].append(float(uu))
    pts, raw = [], []
    for (a, b, k), cs in zip(segs, cuts):
        cs = sorted(set(cs))
        q = [a + (b - a) * c for c in cs]
        for p0, p1 in zip(q[:-1], q[1:]):
            raw.append((len(pts), len(pts) + 1, kinds[k]))
            pts += [p0, p1]
    pts = np.array(pts)
    # snap: greedy merge of points within snap_m (grid hashing)
    cell = max(snap_m, 1e-6)
    keys = {}
    rep = np.empty(len(pts), int)
    nodes = []
    for i, p in enumerate(pts):
        gx, gy = int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))
        found = -1
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n in keys.get((gx + dx, gy + dy), ()):
                    if np.hypot(*(nodes[n] - p)) <= snap_m:
                        found = n
                        break
                if found >= 0:
                    break
            if found >= 0:
                break
        if found < 0:
            found = len(nodes)
            nodes.append(p)
            keys.setdefault((gx, gy), []).append(found)
        rep[i] = found
    edges, kind_out, seen = [], [], {}
    for i0, i1, kd in raw:
        a, b = rep[i0], rep[i1]
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        if key in seen:
            if kd == "line":                  # the finer class wins
                kind_out[seen[key]] = "line"
            continue
        seen[key] = len(edges)
        edges.append(key)
        kind_out.append(kd)
    nodes = np.array(nodes)
    edges, kind_out = _split_at_near_nodes(nodes, edges, kind_out, snap_m)
    return nodes, np.array(edges, int).reshape(-1, 2), kind_out


def _split_at_near_nodes(nodes, edges, kinds, snap_m, rounds=4):
    """A node within snap_m of an edge it is not on (two lines that nearly cross, a line
    passing just by another's vertex) would make a sliver: split the edge at that node."""
    if not len(edges) or snap_m <= 0:
        return edges, kinds
    cell = max(4.0 * snap_m, 1.0)
    for _ in range(rounds):
        hashed = {}
        for i, p in enumerate(nodes):
            hashed.setdefault((int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))),
                              []).append(i)
        out_e, out_k, changed = [], [], False
        for (a, b), kd in zip(edges, kinds):
            pa, pb = nodes[a], nodes[b]
            lo = np.floor((np.minimum(pa, pb) - snap_m) / cell).astype(int)
            hi = np.floor((np.maximum(pa, pb) + snap_m) / cell).astype(int)
            ab = pb - pa
            L2 = float(np.dot(ab, ab))
            hits = []
            for gx in range(lo[0], hi[0] + 1):
                for gy in range(lo[1], hi[1] + 1):
                    for n in hashed.get((gx, gy), ()):
                        if n in (a, b):
                            continue
                        t = float(np.dot(nodes[n] - pa, ab)) / max(L2, 1e-12)
                        if 0.0 < t < 1.0 and np.hypot(*(nodes[n] - (pa + ab * t))) < snap_m:
                            hits.append((t, n))
            if not hits:
                out_e.append((a, b))
                out_k.append(kd)
                continue
            changed = True
            chain = [a] + [n for _t, n in sorted(hits)] + [b]
            for u, v in zip(chain[:-1], chain[1:]):
                if u != v:
                    out_e.append((u, v))
                    out_k.append(kd)
        # drop duplicates (the finer class wins)
        seen, edges, kinds = {}, [], []
        for (a, b), kd in zip(out_e, out_k):
            key = (min(a, b), max(a, b))
            if key in seen:
                if kd == "line":
                    kinds[seen[key]] = "line"
                continue
            seen[key] = len(edges)
            edges.append(key)
            kinds.append(kd)
        if not changed:
            break
    return edges, kinds


def _threshold(d, smin, dmin, dmax, hmax):
    """gmsh's Threshold field (linear between DistMin and DistMax)."""
    if d <= dmin:
        return smin
    if d >= dmax:
        return hmax
    return smin + (hmax - smin) * (d - dmin) / max(dmax - dmin, 1e-9)


def short_edge_counts(nodes, edges, kinds, pts, g, hmin, hch, hpt, hmax):
    """{edge index: number of pieces} for embedded edges shorter than 3 x the local mesh
    size. gmsh would cut such an edge into floor(L / h) + 1 pieces (L = 1.01 h gives two
    of 0.5 h); round(L / h) keeps every piece at 0.75 h or more. The local size is the
    size field (build_mesh's Threshold fields) at the edge's midpoint. Longer edges are left
    to gmsh: floor(L / h) + 1 pieces of L >= 3 h are at least 0.75 h long."""
    if not len(edges):
        return {}
    a, b = nodes[edges[:, 0]], nodes[edges[:, 1]]
    is_line = np.array([kd == "line" for kd in kinds], bool)
    pts = np.asarray(pts, float).reshape(-1, 2)
    out = {}
    for k in range(len(edges)):
        L = float(np.hypot(*(b[k] - a[k])))
        if L <= 0:
            continue
        m = 0.5 * (a[k] + b[k])
        h = hmax
        if is_line.any():
            d = 0.0 if is_line[k] else _dist_to_segments(m, a[is_line], b[is_line])
            h = min(h, _threshold(d, hmin, g["line_dist_min_m"], g["line_dist_max_m"], hmax))
        if (~is_line).any():
            d = 0.0 if not is_line[k] else _dist_to_segments(m, a[~is_line], b[~is_line])
            h = min(h, _threshold(d, hch, g["channel_dist_min_m"], g["channel_dist_max_m"], hmax))
        if len(pts):
            d = float(np.min(np.hypot(*(pts - m).T)))
            h = min(h, _threshold(d, hpt, g["point_dist_min_m"], g["point_dist_max_m"], hmax))
        h = max(h, min(hmin, hpt))                   # Mesh.MeshSizeMin
        if L < 3.0 * h:
            out[k] = max(1, int(round(L / h)))
    return out


def _dist_to_ring(p, ring):
    r = np.asarray(ring, float)
    a, b = r, np.roll(r, -1, axis=0)
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-12)
    t = np.clip(((p - a) * ab).sum(1) / L2, 0, 1)
    q = a + ab * t[:, None]
    return float(np.min(np.hypot(*(p - q).T)))


def _dist_to_segments(p, a, b):
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-12)
    t = np.clip(((p - a) * ab).sum(1) / L2, 0, 1)
    return float(np.min(np.hypot(*(p - (a + ab * t[:, None])).T)))


def _inside(p, ring):
    x, y = p
    r = np.asarray(ring, float)
    x0, y0 = r[:, 0], r[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    c = ((y0 > y) != (y1 > y)) & (x < (x1 - x0) * (y - y0) / np.where(y1 != y0, y1 - y0, 1) + x0)
    return bool(np.count_nonzero(c) % 2)


def keep_inside(nodes, edges, kinds, ring, clearance_m):
    """Drop edges with a node outside the ring or closer than clearance_m to it
    (embedded lines touching the outer boundary make slivers or stop gmsh)."""
    ok = np.array([_inside(p, ring) and _dist_to_ring(p, ring) > clearance_m for p in nodes],
                  bool) if len(nodes) else np.zeros(0, bool)
    keep = [i for i, (a, b) in enumerate(edges) if ok[a] and ok[b]]
    return edges[keep] if len(keep) else np.zeros((0, 2), int), [kinds[i] for i in keep]


# ------------------------------------------------------------------ gmsh
def build_mesh(spec):
    """spec (dict): outer [[x, y]...], lines [[...]...] (embankments, structures,
    culverts, breach, dam), channel [[...]...] (flow path), points [[x, y]...]
    (culvert ends, inflow), and sizes: min_area_m2, channel_area_m2,
    max_area_m2, culvert_area_m2 (the points; None = min_area_m2), line_dist_min_m,
    line_dist_max_m, channel_dist_min_m, channel_dist_max_m, min_angle_deg,
    min_area_tol (gate, see MIN_AREA_TOL), threads.
    Returns dict(points, triangles (CCW), boundary [[tri, edge]...], stats)."""
    g = dict(min_area_m2=100.0, channel_area_m2=250.0, max_area_m2=20000.0,
             line_dist_min_m=60.0, line_dist_max_m=1500.0, channel_dist_min_m=250.0,
             channel_dist_max_m=2500.0, point_dist_min_m=40.0, point_dist_max_m=600.0,
             min_angle_deg=20.0, min_area_tol=MIN_AREA_TOL, culvert_area_m2=None, declutter=True,
             threads=0)
    g.update({k: v for k, v in spec.items() if k in g})
    if g["culvert_area_m2"] is None:
        g["culvert_area_m2"] = g["min_area_m2"]
    ring = np.asarray(spec["outer"], float)
    if np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    if len(ring) < 3:
        raise MeshError("outer polygon has fewer than 3 points")
    # checked before gmsh is imported, so a bad spec is reported as such even where gmsh
    # is missing (that is reported by make_gmsh_mesh as the ModuleNotFoundError line)
    import gmsh
    # gmsh works in local coordinates: with UTM-sized numbers its tolerances
    # break down and meshing can hang
    origin = ring.min(axis=0)
    ring = ring - origin
    shift = lambda ls: [(np.asarray(l, float).reshape(-1, 2) - origin).tolist() for l in ls]
    spec = dict(spec, lines=shift(spec.get("lines", [])), channel=shift(spec.get("channel", [])),
                points=(np.asarray(spec.get("points", []), float).reshape(-1, 2) - origin).tolist())
    # gmsh triangles scatter about +-30 % around the size field, so the floor edge is
    # 1.3 x the equilateral edge of min_area: the smallest triangles then stay near min_area
    hmin = 1.3 * h_of_area(g["min_area_m2"])
    hpt = 1.3 * h_of_area(g["culvert_area_m2"])      # culvert ends and the inflow point
    hch = max(hmin, h_of_area(g["channel_area_m2"]))
    hmax = max(hch, h_of_area(g["max_area_m2"]))
    # vertices at least 2 x the local size apart, so gmsh never splits a segment
    # into pieces shorter than the size
    # parallel near-duplicates and loose ends (road / rail layers) would make slivers
    raw_l = [l for l in spec.get("lines", []) if len(l) >= 2]
    raw_c = [l for l in spec.get("channel", []) if len(l) >= 2]
    if g["declutter"]:
        kept = declutter(raw_l + raw_c, hmin)
        n = len(raw_l)
        raw_l = [r for r, i in kept if i < n]
        raw_c = [r for r, i in kept if i >= n]
    lines = [_thin(l, 2.0 * hmin) for l in raw_l]
    chans = [_thin(l, 2.0 * hch) for l in raw_c]
    nodes, edges, kinds = node_lines(lines + chans, ["line"] * len(lines) + ["channel"] * len(chans),
                                     snap_m=0.75 * hmin)
    edges, kinds = keep_inside(nodes, edges, kinds, ring, hmin)
    pts_in = [p for p in np.asarray(spec.get("points", []), float).reshape(-1, 2)
              if _inside(p, ring) and _dist_to_ring(p, ring) > hmin]

    t0 = time.time()
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        if g["threads"]:
            gmsh.option.setNumber("General.NumThreads", int(g["threads"]))
        gmsh.model.add("anuga")
        geo = gmsh.model.geo
        bp = [geo.addPoint(float(x), float(y), 0.0) for x, y in ring]
        bl = [geo.addLine(bp[i], bp[(i + 1) % len(bp)]) for i in range(len(bp))]
        surf = geo.addPlaneSurface([geo.addCurveLoop(bl)])
        used = sorted(set(edges.ravel().tolist())) if len(edges) else []
        gp = {n: geo.addPoint(float(nodes[n][0]), float(nodes[n][1]), 0.0) for n in used}
        line_c, chan_c, curve_of = [], [], []
        for (a, b), kd in zip(edges, kinds):
            curve_of.append(geo.addLine(gp[a], gp[b]))
            (line_c if kd == "line" else chan_c).append(curve_of[-1])
        free = []
        for p in pts_in:      # a point on or near an embedded line would stop gmsh
            if not len(edges) or _dist_to_segments(p, nodes[edges[:, 0]], nodes[edges[:, 1]]) > hmin / 2.0:
                free.append(geo.addPoint(float(p[0]), float(p[1]), 0.0))
        # 3.10.26: gmsh cuts an embedded curve of length L into floor(L / h) + 1 pieces, so an
        # edge just longer than the local size h (an embankment piece's last segment, a piece
        # between a crossing and a vertex) became two pieces of ~h / 2 and the triangles there
        # fell below the gate (DWR-4441: a 20.0 m end segment at h = 19.8 m, 44 m2 < 45 m2).
        # Short edges get a fixed node count instead: round(L / h) pieces, never under 0.75 h.
        short = short_edge_counts(nodes, edges, kinds, pts_in, g, hmin, hch, hpt, hmax)
        for k, n_seg in short.items():
            geo.mesh.setTransfiniteCurve(curve_of[k], n_seg + 1)
        geo.synchronize()
        if line_c or chan_c:
            gmsh.model.mesh.embed(1, line_c + chan_c, 2, surf)
        if free:
            gmsh.model.mesh.embed(0, free, 2, surf)
        f = gmsh.model.mesh.field
        fields = []

        def threshold(dist_args, smin, dmin, dmax):
            fd = f.add("Distance")
            for k, v in dist_args.items():
                f.setNumbers(fd, k, v)
            f.setNumber(fd, "Sampling", 20)
            ft = f.add("Threshold")
            f.setNumber(ft, "InField", fd)
            f.setNumber(ft, "SizeMin", smin)
            f.setNumber(ft, "SizeMax", hmax)
            f.setNumber(ft, "DistMin", dmin)
            f.setNumber(ft, "DistMax", dmax)
            fields.append(ft)

        if line_c:
            threshold({"CurvesList": line_c}, hmin, g["line_dist_min_m"], g["line_dist_max_m"])
        if chan_c:
            threshold({"CurvesList": chan_c}, hch, g["channel_dist_min_m"], g["channel_dist_max_m"])
        ptags = free + [gp[n] for n in used if any(
            np.hypot(*(nodes[n] - p)) <= hmin / 2.0 for p in pts_in)]
        if ptags:
            threshold({"PointsList": ptags}, hpt, g["point_dist_min_m"], g["point_dist_max_m"])
        fc = f.add("MathEval")
        f.setString(fc, "F", repr(hmax))
        fields.append(fc)
        fm = f.add("Min")
        f.setNumbers(fm, "FieldsList", fields)
        f.setAsBackgroundMesh(fm)
        for k in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
            gmsh.option.setNumber("Mesh." + k, 0)
        gmsh.option.setNumber("Mesh.MeshSizeMin", min(hmin, hpt) if ptags else hmin)
        gmsh.option.setNumber("Mesh.MeshSizeMax", hmax)
        gmsh.option.setNumber("Mesh.Algorithm", 5)
        gmsh.model.mesh.generate(2)
        gmsh.model.mesh.optimize("Laplace2D", niter=5)
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        etypes, _, enodes = gmsh.model.mesh.getElements(2)
        tri_nodes = None
        for et, en in zip(etypes, enodes):
            if et == 2:
                tri_nodes = np.asarray(en, np.int64).reshape(-1, 3)
        if tri_nodes is None or not len(tri_nodes):
            raise MeshError("gmsh made no triangles")
    finally:
        gmsh.finalize()
    mesh_s = time.time() - t0
    return finish(np.asarray(tags, np.int64), np.asarray(xyz).reshape(-1, 3)[:, :2] + origin,
                  tri_nodes,
                  g, mesh_s, n_lines=len(line_c), n_channel=len(chan_c), n_points=len(ptags),
                  short_edges_fixed=len(short))


def finish(tags, xy, tri_nodes, g, mesh_s=0.0, **extra):
    """Compact the gmsh arrays, orient triangles CCW, tag the outer edges and
    apply the quality gate (min angle, min area)."""
    used = np.unique(tri_nodes)
    pos = {int(t): i for i, t in enumerate(tags)}
    remap = {int(t): k for k, t in enumerate(used)}
    pts = xy[[pos[int(t)] for t in used]]
    tri = np.vectorize(remap.get)(tri_nodes).astype(np.int64)
    p0, p1, p2 = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    cr = (p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0])
    cw = cr < 0
    tri[cw] = tri[cw][:, [0, 2, 1]]
    area = 0.5 * np.abs(cr)
    boundary = boundary_edges(tri)
    q = quality(pts, tri)
    stats = dict(mesher="gmsh", triangles=int(len(tri)), nodes=int(len(pts)),
                 mesh_s=round(mesh_s, 2), min_area_m2=round(float(area.min()), 2),
                 min_angle_deg=round(q["min_angle"], 2), p1_angle_deg=round(q["p1_angle"], 2),
                 min_inradius_m=round(q["min_inradius"], 3), boundary_edges=int(len(boundary)))
    intended, tol, gate = gate_area(g)
    stats.update(gate_min_area_m2=round(gate, 2), gate_intended_area_m2=round(intended, 2),
                 min_area_tol=tol)
    stats.update(extra)
    if q["min_angle"] < g["min_angle_deg"]:
        raise MeshError("minimum angle {:.1f} deg is below {:.0f} deg".format(
            q["min_angle"], g["min_angle_deg"]))
    if area.min() < gate:
        raise MeshError("smallest triangle {:.2f} m2 is below the {:.1f} m2 gate ({:g} x the "
                        "smallest intended {:g} m2)".format(area.min(), gate, tol, intended))
    return dict(points=pts, triangles=tri, boundary=boundary, stats=stats)


def gate_area(g):
    """(smallest intended triangle area, tolerance, gate area) for the sizes in g.

    The gate is relative to the smallest size the mesher was asked to make, not the
    base min_area_m2: a smaller culvert_area_m2 makes small triangles on purpose, and
    they must not fail the gate; what it has to catch is triangles much smaller than
    anything requested (slivers at unnoded crossings, which also set ANUGA's timestep)."""
    tol = float(g.get("min_area_tol", MIN_AREA_TOL))
    intended = float(g["min_area_m2"])
    ca = g.get("culvert_area_m2")
    if ca is not None:
        intended = min(intended, float(ca))
    return intended, tol, intended * tol


def boundary_edges(tri):
    """[[triangle, edge]] for edges used by one triangle only; ANUGA's edge i is
    opposite vertex i."""
    e = np.vstack([tri[:, [1, 2]], tri[:, [2, 0]], tri[:, [0, 1]]])
    owner = np.concatenate([np.arange(len(tri))] * 3)
    eid = np.repeat([0, 1, 2], len(tri))
    key = np.sort(e, axis=1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    one = cnt[inv.ravel()] == 1
    out = np.column_stack([owner[one], eid[one]]).astype(np.int64)
    return out[np.lexsort((out[:, 1], out[:, 0]))]


def quality(pts, tri):
    a = np.hypot(*(pts[tri[:, 1]] - pts[tri[:, 2]]).T)
    b = np.hypot(*(pts[tri[:, 0]] - pts[tri[:, 2]]).T)
    c = np.hypot(*(pts[tri[:, 0]] - pts[tri[:, 1]]).T)
    A = np.degrees(np.arccos(np.clip((b * b + c * c - a * a) / (2 * b * c), -1, 1)))
    B = np.degrees(np.arccos(np.clip((a * a + c * c - b * b) / (2 * a * c), -1, 1)))
    m = np.minimum(np.minimum(A, B), 180.0 - A - B)
    s = 0.5 * (a + b + c)
    area = np.sqrt(np.clip(s * (s - a) * (s - b) * (s - c), 0, None))
    return dict(min_angle=float(m.min()), p1_angle=float(np.percentile(m, 1)),
                min_inradius=float((area / s).min()))


def main(argv):
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    try:
        m = build_mesh(spec)
    except MeshError as e:
        print("MESHERROR " + str(e), flush=True)
        return 3
    np.savez(argv[2], mesh_points=m["points"], mesh_triangles=m["triangles"],
             mesh_boundary=m["boundary"], mesh_stats=json.dumps(m["stats"]))
    print("MESHOK " + json.dumps(m["stats"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
