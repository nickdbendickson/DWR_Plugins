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

Far-field sizing (3.10.69). Optional spec keys that make the mesh coarser away from the
refinement lines. Each one is absent by default, and then the mesh and the gmsh size fields
are exactly as before. The Runner sets them from its anuga_gmsh setting or the
NDBREACH_ANUGA_GMSH_* environment variables (anuga_model.gmsh_env_options):

    grade_dist_m       NDBREACH_ANUGA_GMSH_GRADE_DIST_M      default unset (= 1500 m)
        Distance from the refinement lines over which the size grows from the line size to
        the far-field size (max_area_m2): the lines' Threshold field DistMax. The channel and
        point fields scale with it (channel 2 x, points 0.6 x; at the defaults these are
        1500 / 2500 / 600 m). An explicit line_dist_max_m, channel_dist_max_m or
        point_dist_max_m still wins. The benchmark's "grade 500" is grade_dist_m = 500.
    line_min_area_m2   NDBREACH_ANUGA_GMSH_LINE_MIN_AREA_M2  default unset (= min_area_m2, 100)
        Smallest triangle area on the refinement lines: replaces min_area_m2, which also sets
        the declutter gap, vertex spacing and the quality gate. Culvert ends keep
        culvert_area_m2 when it is set (the Runner sets 25 m2 when there are culverts).
    max_area_m2        NDBREACH_ANUGA_GMSH_MAX_AREA_M2       default 20000
        The far-field (largest) triangle area, as before. The variable is new.
    refine_footprint   NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT  default unset (= everywhere)
        A ring [[x, y]...] or a list of rings. The refinement lines and the flow path are
        cut to the inside of it, so outside it the mesh grades to the far-field size.
        Culvert ends and the inflow point are always kept. The Runner's value "wet" uses the
        LISFLOOD-FP wet area, padded by refine_footprint_pad_m
        (NDBREACH_ANUGA_GMSH_REFINE_PAD_M, default 200 m).

Bad values (not a positive number, a grading distance that does not reach past DistMin, or
a ring with fewer than 3 points) raise MeshError with the key's name. When any of these keys
(grade_dist_m, line_min_area_m2, refine_footprint) is set, the resolved values go into the
mesh stats as "sizing" (run_report anuga_mesh.sizing and anuga_mesh.gmsh), with max_area_m2
checked too.
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


CREST_ANGLE_DEG = 30.0      # an embedded edge this close to parallel to a crest line is on it


def crest_edges(nodes, edges, kinds, crest_lines, hmin, max_angle_deg=CREST_ANGLE_DEG):
    """Boolean per edge (3.10.91): a "line" edge whose midpoint is within 0.75 x hmin + 1 m
    (node_lines' snap, plus a metre) of a crest line and that runs within max_angle_deg of
    parallel to it. The edges of crossing lines (culverts, a REFINE_LINES stream) meet a
    crest at an angle and are left out. All False with no crest lines."""
    out = np.zeros(len(edges), bool)
    cl = [np.asarray(c, float).reshape(-1, 2) for c in crest_lines or []]
    cl = [c for c in cl if len(c) >= 2]
    if not cl or not len(edges):
        return out
    tol = 0.75 * float(hmin) + 1.0
    idx = _SegIndex(max(2.0 * tol, 50.0))
    for k, c in enumerate(cl):
        idx.add(c, k)
    cos_max = math.cos(math.radians(max_angle_deg))
    for k, ((a, b), kd) in enumerate(zip(edges, kinds)):
        if kd != "line":
            continue
        pa, pb = np.asarray(nodes[a], float), np.asarray(nodes[b], float)
        ab = pb - pa
        L = float(np.hypot(*ab))
        if L <= 0:
            continue
        r = idx.near(0.5 * (pa + pb))
        out[k] = r is not None and r[0] <= tol and abs(float(np.dot(ab / L, r[2]))) > cos_max
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


def default_sizes():
    """build_mesh's defaults for every size / option key a spec may override."""
    return dict(min_area_m2=100.0, channel_area_m2=250.0, max_area_m2=20000.0,
                line_dist_min_m=60.0, line_dist_max_m=1500.0, channel_dist_min_m=250.0,
                channel_dist_max_m=2500.0, point_dist_min_m=40.0, point_dist_max_m=600.0,
                min_angle_deg=20.0, min_area_tol=MIN_AREA_TOL, culvert_area_m2=None,
                declutter=True, threads=0, algorithm=5, random_seed=None,
                optimize=(("Laplace2D", 5),), smoothing=None)


# ------------------------------------------------------------------ far-field sizing (3.10.69)
# grade_dist_m sets each Threshold field's DistMax to ratio x grade_dist_m (the benchmark's
# "grade 500" variant: lines 500 m, channel 1000 m, points 300 m)
GRADE_RATIOS = (("line_dist_max_m", 1.0), ("channel_dist_max_m", 2.0), ("point_dist_max_m", 0.6))
# the keys new in 3.10.69 (max_area_m2 is older; set alone it is used as before, with no record)
SIZING_KEYS = ("grade_dist_m", "line_min_area_m2", "refine_footprint")


def positive_number(name, v):
    """float(v) when it is a finite number > 0; MeshError naming the key otherwise."""
    if isinstance(v, bool):
        raise MeshError("{} must be a positive number, not {!r}".format(name, v))
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise MeshError("{} must be a positive number, not {!r}".format(name, v))
    if not math.isfinite(x) or x <= 0.0:
        raise MeshError("{} must be a positive number, not {!r}".format(name, v))
    return x


def footprint_rings(fp):
    """refine_footprint (one ring [[x, y]...] or a list of rings) -> list of (N, 2) arrays
    (open rings); MeshError for anything else."""
    try:
        if isinstance(fp, (str, bytes)) or len(fp) == 0:
            raise TypeError(fp)
        one = np.ndim(fp[0][0]) == 0          # [[x, y], ...] rather than [[[x, y], ...], ...]
        rings = [np.asarray(fp, float)] if one else [np.asarray(r, float) for r in fp]
    except (TypeError, ValueError, IndexError):
        raise MeshError("refine_footprint must be a ring [[x, y], ...] or a list of rings")
    out = []
    for r in rings:
        if r.ndim != 2 or r.shape[1] != 2 or not np.isfinite(r).all():
            raise MeshError("refine_footprint must be a ring [[x, y], ...] or a list of rings")
        if len(r) > 1 and np.allclose(r[0], r[-1]):
            r = r[:-1]
        if len(r) < 3:
            raise MeshError("refine_footprint has a ring with fewer than 3 points")
        out.append(r)
    if not out:
        raise MeshError("refine_footprint has no ring")
    return out


def _ring_area(r):
    x, y = r[:, 0], r[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def resolve_sizing(spec, g):
    """Apply the far-field sizing keys of spec (see the module docstring) to the size
    dict g in place, after checking them. Returns the record for the mesh stats: {} when
    none is set, so the default mesh and its stats stay exactly as before."""
    if not any(spec.get(k) is not None for k in SIZING_KEYS):
        return {}
    rec = {}
    for k in ("min_area_m2", "channel_area_m2", "max_area_m2"):
        g[k] = positive_number(k, g[k])
    if spec.get("line_min_area_m2") is not None:
        g["min_area_m2"] = rec["line_min_area_m2"] = positive_number(
            "line_min_area_m2", spec["line_min_area_m2"])
    if spec.get("grade_dist_m") is not None:
        G = rec["grade_dist_m"] = positive_number("grade_dist_m", spec["grade_dist_m"])
        for key, ratio in GRADE_RATIOS:
            if spec.get(key) is None:
                g[key] = ratio * G
    for key, _ in GRADE_RATIOS:
        lo = key.replace("_max_", "_min_")
        g[key] = positive_number(key, g[key])
        if g[key] <= float(g[lo]):
            raise MeshError(
                "{} = {:g} m must be more than {} = {:g} m (the size grows from the line size "
                "to the far-field size between them){}".format(
                    key, g[key], lo, float(g[lo]),
                    "; grade_dist_m = {:g} m is too short".format(rec["grade_dist_m"])
                    if "grade_dist_m" in rec and spec.get(key) is None else ""))
    rec.update(min_area_m2=g["min_area_m2"], channel_area_m2=g["channel_area_m2"],
               max_area_m2=g["max_area_m2"], line_dist_max_m=g["line_dist_max_m"],
               channel_dist_max_m=g["channel_dist_max_m"], point_dist_max_m=g["point_dist_max_m"])
    if spec.get("refine_footprint") is not None:
        rings = footprint_rings(spec["refine_footprint"])
        rec["refine_footprint_rings"] = len(rings)
        rec["refine_footprint_km2"] = round(sum(_ring_area(r) for r in rings) / 1e6, 3)
    return rec


def _seg_ring_ts(a, b, ring):
    """Parameters t (0..1) where segment a-b crosses the ring's edges."""
    c, d = ring, np.roll(ring, -1, axis=0)
    r, s = b - a, d - c
    den = r[0] * s[:, 1] - r[1] * s[:, 0]
    ok = np.abs(den) > 1e-12
    den = np.where(ok, den, 1.0)
    ca = c - a
    t = (ca[:, 0] * s[:, 1] - ca[:, 1] * s[:, 0]) / den
    u = (ca[:, 0] * r[1] - ca[:, 1] * r[0]) / den
    hit = ok & (t > 0.0) & (t < 1.0) & (u >= 0.0) & (u <= 1.0)
    return t[hit].tolist()


def clip_to_rings(line, rings):
    """The pieces of a polyline inside any of the rings (each segment is cut where it
    crosses a ring edge and a piece is kept when its midpoint is inside)."""
    p = np.asarray(line, float).reshape(-1, 2)
    pieces, cur = [], []
    for a, b in zip(p[:-1], p[1:]):
        ts = [0.0, 1.0]
        for r in rings:
            ts += _seg_ring_ts(a, b, r)
        ts = sorted(set(ts))
        for t0, t1 in zip(ts[:-1], ts[1:]):
            if t1 - t0 < 1e-12:
                continue
            q0, q1 = a + (b - a) * t0, a + (b - a) * t1
            if any(_inside(0.5 * (q0 + q1), r) for r in rings):
                if cur and np.allclose(cur[-1], q0):
                    cur.append(q1)
                else:
                    if len(cur) >= 2:
                        pieces.append(cur)
                    cur = [q0, q1]
            else:
                if len(cur) >= 2:
                    pieces.append(cur)
                cur = []
    if len(cur) >= 2:
        pieces.append(cur)
    return [np.array(c).tolist() for c in pieces]


def _length(line):
    p = np.asarray(line, float).reshape(-1, 2)
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0


# ------------------------------------------------------------------ gmsh
def size_lines_of(spec):
    """spec["size_lines"] checked: [{'line' [[x, y]...] (2+ points), 'size' > 0, 'dist_min'
    >= 0, 'dist_max' > dist_min}...] (spec units); [] when absent (ANUGA meshes as before)."""
    out = []
    for i, sl in enumerate(spec.get("size_lines") or []):
        try:
            ln = np.asarray(sl["line"], float).reshape(-1, 2)
        except (KeyError, TypeError, ValueError):
            raise MeshError("size_lines[{}] needs a line [[x, y]...]".format(i))
        if len(ln) < 2 or not np.isfinite(ln).all():
            raise MeshError("size_lines[{}] needs a line of 2 or more points".format(i))
        size = positive_number("size_lines[{}] size".format(i), sl.get("size"))
        dmin = float(sl.get("dist_min", 2.0 * size))
        dmax = positive_number("size_lines[{}] dist_max".format(i),
                               sl.get("dist_max", dmin + 10.0 * size))
        if not (0.0 <= dmin < dmax):
            raise MeshError("size_lines[{}]: dist_min must be >= 0 and below dist_max".format(i))
        out.append({"line": ln, "size": size, "dist_min": dmin, "dist_max": dmax})
    return out


def _densify(line, step):
    """The line's vertices plus points every <= step along each segment."""
    p = np.asarray(line, float).reshape(-1, 2)
    out = [p[0]]
    for a, b in zip(p[:-1], p[1:]):
        n = max(1, int(math.ceil(float(np.hypot(*(b - a))) / max(step, 1e-9))))
        out += [a + (b - a) * k / n for k in range(1, n + 1)]
    return np.asarray(out)


def build_mesh(spec):
    """spec (dict): outer [[x, y]...], lines [[...]...] (embankments, structures,
    culverts, breach, dam), channel [[...]...] (flow path), points [[x, y]...]
    (culvert ends, inflow), and sizes: min_area_m2, channel_area_m2,
    max_area_m2, culvert_area_m2 (the points; None = min_area_m2), line_dist_min_m,
    line_dist_max_m, channel_dist_min_m, channel_dist_max_m, min_angle_deg,
    min_area_tol (gate, see MIN_AREA_TOL), threads; v3.10.22 (HEC-RAS seeds): algorithm
    (gmsh Mesh.Algorithm, default 5), random_seed (Mesh.RandomSeed, for the same points
    every run), optimize [[method, niter]...] (default Laplace2D x 5), smoothing
    (Mesh.Smoothing passes); v3.10.69 far-field sizing (module docstring): grade_dist_m,
    line_min_area_m2, refine_footprint; 3.10.75 (HEC-RAS seeds): size_lines [{line, size,
    dist_min, dist_max}...], lines that only set the size (edge length `size` within dist_min
    of the line, growing to the far field by dist_max) and are not embedded (no nodes on
    them; see size_lines_of); 3.10.91 (ANUGA crest hold, core/anuga_crest.py): crest_lines
    [[...]...], road / rail crest centrelines embedded and sized as the lines (no triangle
    straddles them), their mesh nodes returned as crest_nodes (indices into points).
    Returns dict(points, triangles (CCW), boundary [[tri, edge]...], stats[, crest_nodes])."""
    g = default_sizes()
    g.update({k: v for k, v in spec.items() if k in g})
    sizing = resolve_sizing(spec, g)               # {} (and g untouched) when none is set
    if g["culvert_area_m2"] is None:
        g["culvert_area_m2"] = g["min_area_m2"]
    ring = np.asarray(spec["outer"], float)
    if np.allclose(ring[0], ring[-1]):
        ring = ring[:-1]
    if len(ring) < 3:
        raise MeshError("outer polygon has fewer than 3 points")
    size_lines = size_lines_of(spec)
    if size_lines:
        g["size_line_min_m"] = min(sl["size"] for sl in size_lines)   # 3.10.76: the gate
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
    rings = (footprint_rings(spec["refine_footprint"]) if spec.get("refine_footprint") is not None
             else None)
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
    if rings is not None:
        # refine only inside the footprint: outside it the size grows to the far field
        rings = [r - origin for r in rings]
        before = sum(_length(l) for l in raw_l + raw_c)
        raw_l = [q for l in raw_l for q in clip_to_rings(l, rings) if _length(q) >= hmin]
        raw_c = [q for l in raw_c for q in clip_to_rings(l, rings) if _length(q) >= hmin]
        sizing["refine_lines_km_outside"] = round(
            (before - sum(_length(l) for l in raw_l + raw_c)) / 1000.0, 3)
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
    # 3.10.91: which embedded line edges lie along a crest line (spec "crest_lines", the
    # Runner's road / rail crest centrelines, also given in "lines"); the nodes on them are
    # returned as crest_nodes so the Runner can hold them at the 1 m crest elevation.
    # Classification only: the mesh is the same with or without the key.
    crest_edge = crest_edges(nodes, edges, kinds, shift(spec.get("crest_lines", [])), hmin)
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
        line_c, chan_c, curve_of, crest_c = [], [], [], []
        for k, ((a, b), kd) in enumerate(zip(edges, kinds)):
            curve_of.append(geo.addLine(gp[a], gp[b]))
            (line_c if kd == "line" else chan_c).append(curve_of[-1])
            if crest_edge[k]:
                crest_c.append(curve_of[-1])
        free = []
        for p in pts_in:      # a point on or near an embedded line would stop gmsh
            if not len(edges) or _dist_to_segments(p, nodes[edges[:, 0]], nodes[edges[:, 1]]) > hmin / 2.0:
                free.append(geo.addPoint(float(p[0]), float(p[1]), 0.0))
        # 3.10.31: gmsh cuts an embedded curve of length L into floor(L / h) + 1 pieces, so an
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
        # 3.10.75: size-only lines (HEC-RAS structure connections): points along the line,
        # not embedded, so they set the size field only (their own nodes are in no triangle
        # and finish() drops them)
        sl_tags = [[geo.addPoint(float(x), float(y), 0.0) for x, y in _densify(
            np.asarray(sl["line"], float) - origin, 0.5 * sl["size"])] for sl in size_lines]
        if size_lines:
            geo.synchronize()
        for sl, tags_ in zip(size_lines, sl_tags):
            threshold({"PointsList": tags_}, sl["size"], sl["dist_min"], sl["dist_max"])
        fc = f.add("MathEval")
        f.setString(fc, "F", repr(hmax))
        fields.append(fc)
        fm = f.add("Min")
        f.setNumbers(fm, "FieldsList", fields)
        f.setAsBackgroundMesh(fm)
        for k in ("MeshSizeExtendFromBoundary", "MeshSizeFromPoints", "MeshSizeFromCurvature"):
            gmsh.option.setNumber("Mesh." + k, 0)
        gmsh.option.setNumber("Mesh.MeshSizeMin", min(
            [min(hmin, hpt) if ptags else hmin] + [sl["size"] for sl in size_lines]))
        gmsh.option.setNumber("Mesh.MeshSizeMax", hmax)
        gmsh.option.setNumber("Mesh.Algorithm", int(g["algorithm"]))
        if g["random_seed"] is not None:
            gmsh.option.setNumber("Mesh.RandomSeed", int(g["random_seed"]))
        if g["smoothing"] is not None:
            gmsh.option.setNumber("Mesh.Smoothing", int(g["smoothing"]))
        gmsh.model.mesh.generate(2)
        for meth, nit in (g["optimize"] or ()):
            gmsh.model.mesh.optimize(str(meth), niter=int(nit))
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        crest_tags = set()
        for c in crest_c:            # nodes on the crest curves, their end points included
            ct, _x, _p = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            crest_tags.update(int(t) for t in ct)
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
                  short_edges_fixed=len(short), crest_tags=crest_tags if crest_c else None,
                  **dict({"sizing": sizing} if sizing else {},
                         **({"size_lines": len(size_lines)} if size_lines else {})))


def finish(tags, xy, tri_nodes, g, mesh_s=0.0, crest_tags=None, **extra):
    """Compact the gmsh arrays, orient triangles CCW, tag the outer edges and
    apply the quality gate (min angle, min area). crest_tags (3.10.91): gmsh node tags on
    crest curves; returned as crest_nodes, indices into points (only when given)."""
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
    out = dict(points=pts, triangles=tri, boundary=boundary, stats=stats)
    if crest_tags is not None:
        cn = sorted(remap[int(t)] for t in crest_tags if int(t) in remap)
        out["crest_nodes"] = np.asarray(cn, np.int64)
        stats["crest_nodes"] = len(cn)
    return out


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
    sl = g.get("size_line_min_m")
    if sl:
        # 3.10.76: size_lines (the ANUGA line band, HEC-RAS seed lines) make small triangles
        # on purpose: the area of the smallest of their sizes, as gmsh makes it
        intended = min(intended, math.sqrt(3.0) / 4.0 * (float(sl) / 1.3) ** 2)
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
    extra = {"mesh_crest_nodes": m["crest_nodes"]} if "crest_nodes" in m else {}
    np.savez(argv[2], mesh_points=m["points"], mesh_triangles=m["triangles"],
             mesh_boundary=m["boundary"], mesh_stats=json.dumps(m["stats"]), **extra)
    print("MESHOK " + json.dumps(m["stats"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
