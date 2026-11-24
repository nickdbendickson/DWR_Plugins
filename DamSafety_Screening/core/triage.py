"""Quick screen (triage): a cheap, deliberately generous flood envelope.

Purpose: decide which dams obviously cannot reach any structure, road or
railroad, so the full 2D model is only run where it can change the answer.

Method (numpy only)
  1. Flow path: steepest descent from the breach toe.  Where the path gets
     stuck in a pit (a pothole or a road fill), a local priority search finds
     the lowest spill point and the path continues through it.  Very large
     closed basins end the path ("terminal basin").
  2. Stage along the path: Manning normal depth for the UNATTENUATED breach
     peak in a cross-section cut perpendicular to the path, using a local
     bed slope (never flatter than `min_slope`).  A safety margin is added:
     30% of the flow depth, at least 0.15 m (a fixed large margin floods
     half the prairie around a stock pond and clears nothing).
  3. Reach: the path is followed until the valley below that stage would hold
     `volume_factor` x the released volume (default 2x), or the terrain ends.
  4. Envelope: cells that DRAIN to the path (D8 on the pit-filled surface),
     below the stage of the path cell they drain to and not lower than its bed
     (height above nearest drainage).  Cells whose water goes elsewhere are
     never included, so the envelope does not spread along contour lines.
  5. Corridor (0.52.0): the reach in 3 is a cheap estimate that can stop far short of the
     real flood, so the tool also looks along the whole path, the stream lines below it and
     the waterbody shores they touch out to a straight-line distance (corridor_axis), and a
     closed basin can be proven where they end (closed_basin).

Every simplification errs towards a LARGER envelope (no attenuation, peak
stage everywhere at once, margin, volume factor), because a dam is only
cleared ("no 2D needed") when nothing lies inside it.  It is still a screening
shortcut: the triage tool can send a random audit sample of cleared dams to
the full model to check it.
"""
import heapq
import math
import numpy as np

from .gridutil import NODATA, inside

N8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
N4 = [(-1, 0), (1, 0), (0, -1), (0, 1)]


def trace_path(dem, start_rc, max_steps=200000, max_pit_cells=40000):
    """Steepest-descent path with pit escape.  Returns (cells, end_reason)."""
    shape = dem.shape
    valid = dem > NODATA + 1
    r, c = start_rc
    if not inside(shape, r, c) or not valid[r, c]:
        return [], "start outside terrain"
    path = [(r, c)]
    seen = {(r, c)}
    for _ in range(max_steps):
        z = dem[r, c]
        best, bz = None, z
        for dr, dc in N8:
            rr, cc = r + dr, c + dc
            if not inside(shape, rr, cc) or not valid[rr, cc]:
                return path, "edge"
            dz = (dem[rr, cc] - z) / (1.4142 if dr and dc else 1.0)
            if dem[rr, cc] < bz and (best is None or dz < best[2]):
                best = (rr, cc, dz)
        if best is not None:
            nxt = [(best[0], best[1])]
        else:                                    # pit or flat: priority search for a spill
            nxt = _escape(dem, valid, r, c, seen, max_pit_cells)
            if nxt is None:
                return path, "terminal basin"
            if nxt == "edge":
                return path, "edge"
        added = False
        for rc in nxt:
            if rc in seen:
                continue
            seen.add(rc); path.append(rc); added = True
        if not added:
            return path, "stuck"
        r, c = path[-1]
    return path, "max steps"


def _escape(dem, valid, r0, c0, seen, max_cells):
    z0 = dem[r0, c0]
    heap = [(z0, r0, c0)]
    parent = {(r0, c0): None}
    while heap:
        z, r, c = heapq.heappop(heap)
        if len(parent) > max_cells:
            return None
        for dr, dc in N4:
            rr, cc = r + dr, c + dc
            if (rr, cc) in parent:
                continue
            if not inside(dem.shape, rr, cc) or not valid[rr, cc]:
                return "edge"
            parent[(rr, cc)] = (r, c)
            if dem[rr, cc] < z0 - 1e-6:          # lower than the pit bottom: a true outlet
                chain, k = [], (rr, cc)
                while k is not None and k != (r0, c0):
                    chain.append(k); k = parent[k]
                return chain[::-1]
            heapq.heappush(heap, (dem[rr, cc], rr, cc))
    return None


def normal_depth_stage(xs_z, q, slope, n, cell, dmax=40.0):
    """Water-surface elevation carrying q in a cross-section (strip method).

    xs_z: elevations across the valley, centre index = channel point.
    Only strips connected to the centre (not past a higher bank) convey flow.
    Returns (wse, wet_width_m, area_m2).
    """
    xs = np.asarray(xs_z, float)
    ci = len(xs) // 2
    zmin = float(xs[ci])
    s = math.sqrt(max(slope, 1e-6))

    def conv(wse):
        lo = ci
        while lo > 0 and xs[lo - 1] < wse and xs[lo - 1] > NODATA + 1:
            lo -= 1
        hi = ci
        while hi < len(xs) - 1 and xs[hi + 1] < wse and xs[hi + 1] > NODATA + 1:
            hi += 1
        d = np.clip(wse - xs[lo:hi + 1], 0, None)
        return s / n * float(np.sum(d ** (5.0 / 3.0))) * cell, (hi - lo + 1) * cell, \
            float(d.sum()) * cell, lo == 0 or hi == len(xs) - 1

    a, b = zmin, zmin + dmax
    if conv(b)[0] < q:
        qq, w, area, spill = conv(b)
        return b, w, area
    for _ in range(50):
        m = 0.5 * (a + b)
        if conv(m)[0] < q:
            a = m
        else:
            b = m
    _, w, area, _ = conv(b)
    return b, w, area


def envelope(dem, gt, start_rc, q_peak, volume_m3, n=0.05, min_slope=5e-4,
             margin_m=0.15, margin_frac=0.3, volume_factor=2.0, half_width_m=1500.0, slope_len_m=150.0,
             min_reach_m=500.0, max_lateral_m=None, hand_tol_m=0.5):
    """Triage envelope.  Returns dict(mask, depth, path, stage, reach_m, end, edge, trace_end)."""
    cell = abs(gt[1])
    path, end = trace_path(dem, start_rc)
    # 0.52.0: "end" becomes "volume" when the volume budget stops the reach; the corridor check
    # needs why the PATH ended (edge / terminal basin / stuck / max steps), kept as trace_end
    res = {"mask": np.zeros(dem.shape, bool), "depth": np.zeros(dem.shape),
           "stage_grid": np.full(dem.shape, np.nan), "untraced": False,
           "path": path, "stage": [], "reach_m": 0.0, "end": end, "edge": end == "edge",
           "trace_end": end}
    if len(path) < 2:
        # v0.11.1: no flow path (the breach toe is on the terrain edge, in a pit, outside the
        # terrain, or on flat ground). The envelope is empty, which must NOT read as "nothing
        # inside the envelope": the caller sees untraced=True and keeps the dam for 2D.
        res["untraced"] = True
        return res
    pr = np.array([p[0] for p in path]); pc = np.array([p[1] for p in path])
    steps = np.hypot(np.diff(pr), np.diff(pc)) * cell
    dist = np.concatenate(([0.0], np.cumsum(steps)))
    zp = dem[pr, pc]
    k = max(int(slope_len_m / cell), 2)
    hw = int(half_width_m / cell)
    stage = np.full(len(path), np.nan)
    stored = 0.0
    last = len(path) - 1
    for i in range(len(path)):
        i0, i1 = max(i - k, 0), min(i + k, len(path) - 1)
        ddist = max(dist[i1] - dist[i0], cell)
        slope = max((zp[i0] - zp[i1]) / ddist, min_slope)
        dy, dx = pr[i1] - pr[i0], pc[i1] - pc[i0]
        L = math.hypot(dx, dy) or 1.0
        py, px = dx / L, -dy / L                      # perpendicular (row, col)
        offs = np.arange(-hw, hw + 1)
        rr = np.clip(np.round(pr[i] + py * offs).astype(int), 0, dem.shape[0] - 1)
        cc = np.clip(np.round(pc[i] + px * offs).astype(int), 0, dem.shape[1] - 1)
        xs = dem[rr, cc].copy()
        xs[hw] = zp[i]
        wse, width, area = normal_depth_stage(xs, q_peak, slope, n, cell)
        depth_i = wse - float(xs[hw])
        stage[i] = wse + max(margin_m, margin_frac * depth_i)
        seg = steps[i - 1] if i > 0 else 0.0
        stored += area * seg
        if dist[i] >= min_reach_m and stored >= volume_factor * volume_m3:
            last = i
            break
    res["reach_m"] = float(dist[last])
    res["stage"] = stage[:last + 1].tolist()
    if last == len(path) - 1 and end == "edge":
        res["edge"] = True
    elif last < len(path) - 1:
        res["end"] = "volume"
        res["edge"] = False

    # ---- lateral extent by drainage (height above nearest drainage, HAND):
    # each cell is compared with the stage of the path cell its own flow reaches
    lab = drainage_labels(dem, path[:last + 1])
    lim_c = int((max_lateral_m or half_width_m) / cell)
    valid = dem > NODATA + 1
    li = np.where(lab >= 0, lab, 0)
    st_c = np.where(lab >= 0, stage[li], np.nan)
    zp_c = zp[li]
    pr_c, pc_c = pr[li], pc[li]
    rr, cc = np.mgrid[0:dem.shape[0], 0:dem.shape[1]]
    near = (np.abs(rr - pr_c) <= lim_c) & (np.abs(cc - pc_c) <= lim_c)
    wet = (lab >= 0) & valid & near & (dem < st_c) & (dem >= zp_c - hand_tol_m)
    for i in range(last + 1):                       # path cells themselves
        wet[pr[i], pc[i]] = True
    if wet[0, :].any() or wet[-1, :].any() or wet[:, 0].any() or wet[:, -1].any():
        res["edge"] = True
    ring = np.zeros(dem.shape, bool)                # next to missing terrain
    ring[1:, :] |= ~valid[:-1, :]; ring[:-1, :] |= ~valid[1:, :]
    ring[:, 1:] |= ~valid[:, :-1]; ring[:, :-1] |= ~valid[:, 1:]
    if (wet & ring).any():
        res["edge"] = True
    res["mask"] = wet
    res["stage_grid"] = np.where(wet, st_c, np.nan)
    res["depth"] = np.where(wet, np.nan_to_num(st_c) - dem, 0.0).clip(min=0)
    return res


def fill_eps(dem, eps=1e-3):
    """Priority-flood pit filling with a tiny gradient so every cell drains out."""
    valid = dem > NODATA + 1
    z = np.where(valid, dem, np.inf).astype(float)
    done = ~valid.copy()
    heap = []
    ny, nx = dem.shape
    edge = np.zeros(dem.shape, bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    nb = np.zeros(dem.shape, bool)
    nb[1:, :] |= ~valid[:-1, :]; nb[:-1, :] |= ~valid[1:, :]
    nb[:, 1:] |= ~valid[:, :-1]; nb[:, :-1] |= ~valid[:, 1:]
    for r, c in zip(*np.where(valid & (edge | nb))):
        heapq.heappush(heap, (z[r, c], r, c)); done[r, c] = True
    while heap:
        zz, r, c = heapq.heappop(heap)
        for dr, dc in N8:
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not done[rr, cc]:
                done[rr, cc] = True
                if z[rr, cc] <= zz:
                    z[rr, cc] = zz + eps
                heapq.heappush(heap, (z[rr, cc], rr, cc))
    return np.where(valid, z, NODATA)


def drainage_labels(dem, path):
    """Index of the path cell each grid cell drains to (D8 on the pit-filled
    surface), -1 if it drains elsewhere."""
    zf = fill_eps(dem)
    ny, nx = dem.shape
    big = np.where(zf > NODATA + 1, zf, np.inf)
    pad = np.pad(big, 1, constant_values=np.inf)
    best = np.full(dem.shape, 0.0)
    recv = np.arange(ny * nx).reshape(ny, nx)
    idx = recv.copy()
    for dr, dc in N8:
        nbz = pad[1 + dr:1 + dr + ny, 1 + dc:1 + dc + nx]
        drop = (big - nbz) / (1.4142 if dr and dc else 1.0)
        take = drop > best
        best = np.where(take, drop, best)
        rr = np.clip(np.arange(ny)[:, None] + dr, 0, ny - 1)
        cc = np.clip(np.arange(nx)[None, :] + dc, 0, nx - 1)
        recv = np.where(take, rr * nx + cc, recv)
    recv = recv.ravel()
    lab = np.full(ny * nx, -1)
    for i, (r, c) in enumerate(path):
        if lab[r * nx + c] < 0:
            lab[r * nx + c] = i
    # pointer jumping: follow receivers until a labelled cell (or a sink) is met
    for _ in range(int(np.ceil(np.log2(ny * nx))) + 2):
        need = lab < 0
        if not need.any():
            break
        lab = np.where(need, lab[recv], lab)
        recv = recv[recv]
    del idx
    return lab.reshape(ny, nx)


def coarsen(dem, factor):
    """Block mean; a block with any no-data cell becomes no-data."""
    if factor <= 1:
        return dem
    ny, nx = (dem.shape[0] // factor) * factor, (dem.shape[1] // factor) * factor
    b = dem[:ny, :nx].reshape(ny // factor, factor, nx // factor, factor)
    bad = (b <= NODATA + 1).any(axis=(1, 3))
    m = b.mean(axis=(1, 3))
    return np.where(bad, NODATA, m)


def screen(dem, gt, start_rc, q_peak, volume_m3, target_cell=10.0, **kw):
    """Envelope computed on a coarsened grid (default ~10 m), returned at full resolution.

    Screening does not need LiDAR resolution, and the coarse grid makes it
    ~100x faster.  Stage is carried back to the fine grid, so fine-grid depth
    = coarse stage - fine ground.  The whole traced path (not cut at reach_m) comes back at
    full resolution, with trace_end, for the corridor check.
    """
    cell = abs(gt[1])
    k = max(int(round(target_cell / cell)), 1)
    dc = coarsen(dem, k)
    gtc = (gt[0], cell * k, 0.0, gt[3], 0.0, -cell * k)
    res = envelope(dc, gtc, (start_rc[0] // k, start_rc[1] // k), q_peak, volume_m3, **kw)
    st = np.repeat(np.repeat(res["stage_grid"], k, 0), k, 1)
    full = np.full(dem.shape, np.nan)
    full[:st.shape[0], :st.shape[1]] = st
    wet = np.isfinite(full) & (dem < full) & (dem > NODATA + 1)
    res["mask"] = wet
    res["depth"] = np.where(wet, full - dem, 0.0)
    res["path"] = [(r * k + k // 2, c * k + k // 2) for r, c in res["path"]]
    res["factor"] = k
    return res


# ------------------------------------------------------------------ downstream corridor (0.52.0)
# The envelope's reach is cut by a 2x-volume budget, so it can stop far short of a real breach
# flood (DWR-2636: 446 m envelope, 4.9 km 2D flood). A dam the envelope would clear is still
# sent to 2D when anything High-capable lies within W of its flow path, of the stream lines
# below it (every branch) or of the shores of the waterbodies they touch, out to a STRAIGHT-LINE
# distance d_max from the breach toe - or when that corridor cannot be completed. Numpy only:
# the Processing tool passes plain EPSG:5070 arrays in and gets arrays / strings back.
# beyond_m: how far past d_max (straight line) a branch is still followed, so a reach that leaves
# the D circle and comes back inside it is checked; the Processing tool reads the stream lines
# that far out
CORRIDOR_DEFAULTS = dict(d_max=15000.0, along_cap=5.0, walk_budget=25.0, seed_tol=60.0, end_r=300.0,
                         snap=5.0, gap=100.0, dens=10.0, wb_touch=30.0, state_m=300.0,
                         outside_skip_m=5000.0, beyond_m=1500.0)
PART_NAMES = {"p": "flow path", "n": "stream", "w": "shore"}


def densify(xy, step):
    """Polyline vertices with extra points so no gap exceeds `step` (first point kept)."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    if len(xy) < 2:
        return xy.copy()
    seg = np.hypot(*np.diff(xy, axis=0).T)
    n = np.maximum(np.ceil(seg / step).astype(int), 1)
    k = np.repeat(np.arange(len(seg)), n)
    t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 1) / np.repeat(n, n)
    return np.vstack([xy[:1], xy[k] + t[:, None] * (xy[k + 1] - xy[k])])


class PointIndex:
    """Nearest-point distances to a fixed point set: a numpy bucket grid (no scipy), exact."""

    def __init__(self, xy, bucket):
        self.xy = np.asarray(xy, float).reshape(-1, 2)
        self.b = float(bucket)
        self.cells = {}
        if len(self.xy):
            k = np.floor(self.xy / self.b).astype(np.int64)
            order = np.lexsort((k[:, 1], k[:, 0]))
            ks = k[order]
            brk = np.nonzero(np.any(np.diff(ks, axis=0) != 0, axis=1))[0] + 1
            for s, e in zip(np.concatenate(([0], brk)), np.concatenate((brk, [len(ks)]))):
                self.cells[(int(ks[s, 0]), int(ks[s, 1]))] = order[s:e]

    def nearest(self, pts, max_d):
        """(distance, index) of the nearest indexed point within max_d of each of pts;
        inf / -1 where none is that close."""
        pts = np.asarray(pts, float).reshape(-1, 2)
        dist = np.full(len(pts), np.inf)
        idx = np.full(len(pts), -1, dtype=np.int64)
        if not self.cells or not len(pts):
            return dist, idx
        reach = int(math.ceil(max_d / self.b))
        k = np.floor(pts / self.b).astype(np.int64)
        groups = {}
        for i, kk in enumerate(map(tuple, k.tolist())):
            groups.setdefault(kk, []).append(i)
        for (kx, ky), ii in groups.items():
            cand = [self.cells[c] for c in ((kx + dx, ky + dy) for dx in range(-reach, reach + 1)
                                            for dy in range(-reach, reach + 1)) if c in self.cells]
            if not cand:
                continue
            c = np.concatenate(cand)
            ii = np.asarray(ii)
            d = np.hypot(pts[ii, 0][:, None] - self.xy[c, 0][None, :], pts[ii, 1][:, None] - self.xy[c, 1][None, :])
            j = np.argmin(d, axis=1)
            dm = d[np.arange(len(ii)), j]
            ok = dm <= max_d
            dist[ii[ok]] = dm[ok]
            idx[ii[ok]] = c[j[ok]]
        return dist, idx

    def within(self, p, r):
        """Indices of the indexed points within r of the single point p."""
        reach = int(math.ceil(r / self.b))
        kx, ky = int(math.floor(p[0] / self.b)), int(math.floor(p[1] / self.b))
        cand = [self.cells[c] for c in ((kx + dx, ky + dy) for dx in range(-reach, reach + 1)
                                        for dy in range(-reach, reach + 1)) if c in self.cells]
        if not cand:
            return np.zeros(0, dtype=np.int64)
        c = np.concatenate(cand)
        return c[np.hypot(self.xy[c, 0] - p[0], self.xy[c, 1] - p[1]) <= r]


class StreamNet:
    """Stream lines (EPSG:5070 arrays, digitised downstream) for an endpoint-matched walk.
    both_ways[i] True (3DHP flowdirection 0 = unknown): the line is also walked reversed."""

    def __init__(self, lines, both_ways=None, bucket=100.0, step=10.0):
        L = []
        for i, c in enumerate(lines):
            c = np.asarray(c, float).reshape(-1, 2)
            if len(c) < 2:
                continue
            c = densify(c, step)                         # vertex tests then mean distance to the line
            L.append(c)
            if both_ways is not None and both_ways[i]:
                L.append(c[::-1].copy())
        self.lines = L
        self.vl = np.concatenate([np.full(len(c), i) for i, c in enumerate(L)]) if L else np.zeros(0, int)
        self.vi = np.concatenate([np.arange(len(c)) for c in L]) if L else np.zeros(0, int)
        self.vxy = np.concatenate(L) if L else np.zeros((0, 2))
        self.last = np.array([len(c) - 1 for c in L], int)
        # length of line left below each vertex (a gap jump needs somewhere to go)
        self.rem = np.concatenate([np.concatenate((np.cumsum(np.hypot(*np.diff(c, axis=0).T)[::-1])[::-1], [0.0]))
                                   for c in L]) if L else np.zeros(0)
        self.index = PointIndex(self.vxy, bucket)
        st = np.nonzero(self.vi == 0)[0]
        self.start_v = st
        self.starts = PointIndex(self.vxy[st], bucket)

    def twin(self, a, b):
        """b is a's reversed copy (or the same line digitised the other way)."""
        return np.array_equal(self.lines[a][0], self.lines[b][-1]) and np.array_equal(self.lines[a][-1], self.lines[b][0])


def _in_ring(pts, ring):
    """Even-odd point-in-polygon for many points against one closed ring (numpy)."""
    x, y = pts[:, 0][:, None], pts[:, 1][:, None]
    x1, y1 = ring[:-1, 0][None, :], ring[:-1, 1][None, :]
    x2, y2 = ring[1:, 0][None, :], ring[1:, 1][None, :]
    cross = ((y1 > y) != (y2 > y)) & (x < (x2 - x1) * (y - y1) / np.where(y2 != y1, y2 - y1, 1e-12) + x1)
    return (cross.sum(axis=1) % 2) == 1


def _walk(net, seeds, toe, o, visited, flags, ends, pieces, stats):
    """Downstream walk in along-path order. A branch is followed on past d_max (straight line
    from the toe), because a meander, or a creek that joins a river turning back, can leave the D
    circle and come back inside it (corridor_axis keeps only the points within d_max); it is cut
    where it first lies more than d_max + beyond_m from the toe, and a line end beyond d_max is
    complete. A branch still within d_max after along_cap x d_max of channel, or a walk longer
    than walk_budget x d_max, is flagged (the dam then needs 2D)."""
    D = o["d_max"]
    R = D + o["beyond_m"]
    cap = o["along_cap"] * D
    heap = [(float(a), int(l), int(v)) for l, v, a in seeds]
    heapq.heapify(heap)
    while heap:
        d0, l, v = heapq.heappop(heap)
        if any(v2 <= v and a2 <= d0 + 1e-6 for v2, a2 in visited.get(l, ())):
            continue
        visited.setdefault(l, []).append((v, d0))
        c = net.lines[l][v:]
        seg = np.hypot(*np.diff(c, axis=0).T) if len(c) > 1 else np.zeros(0)
        cum = d0 + np.concatenate(([0.0], np.cumsum(seg)))
        rr = np.hypot(c[:, 0] - toe[0], c[:, 1] - toe[1])
        out = np.nonzero(rr > R)[0]
        over = np.nonzero(cum > cap)[0]
        if len(over) and (not len(out) or over[0] < out[0]):   # along-path cap first
            k = int(over[0])
            pieces.append(c[:k + 1]); stats["walked_m"] += cum[k] - cum[0]
            if rr[k] <= D:                                 # the rest inside d_max is unchecked: fail safe
                flags.append("{:.0f} km of channel followed and still within {:.0f} km of the dam"
                             .format(cap / 1000, D / 1000))
            continue
        if len(out):                                       # past d_max + beyond_m: complete
            k = int(out[0])
            pieces.append(c[:k + 1]); stats["walked_m"] += cum[k] - cum[0]; stats["reached"] += 1
            continue
        pieces.append(c)
        stats["walked_m"] += cum[-1] - cum[0]
        if stats["walked_m"] > o["walk_budget"] * D:
            flags.append("more than {:.0f} km of stream lines below the dam".format(o["walk_budget"] * D / 1000))
            return
        end = net.lines[l][-1]
        nxt = [int(net.start_v[j]) for j in net.starts.within(end, o["snap"])]
        nxt = [j for j in nxt if net.vl[j] != l and not net.twin(l, int(net.vl[j]))]
        if nxt:                                            # confluence / divergence: every branch
            for j in nxt:
                heapq.heappush(heap, (cum[-1] + float(np.hypot(*(net.vxy[j] - end))), int(net.vl[j]), 0))
            continue
        best = None                                        # digitising gap: nearest vertex of another line
        for j in net.index.within(end, o["gap"]):
            lj = int(net.vl[j])
            if lj == l or net.rem[j] < o["gap"] or net.twin(l, lj):
                continue                                   # (a stub's last metres would loop)
            dj = float(np.hypot(*(net.vxy[j] - end)))
            if best is None or dj < best[0]:
                best = (dj, lj, int(net.vi[j]))
        if best is not None:
            stats["gaps"] += 1
            heapq.heappush(heap, (cum[-1] + best[0], best[1], best[2]))
        elif rr[-1] <= D:
            ends.append((end.copy(), float(rr[-1])))
        else:                                              # ends beyond d_max: complete
            stats["reached"] += 1


def _runs(xy, keep):
    """The runs of consecutive kept points of a polyline with at least 2 points (output lines)."""
    out, s = [], None
    for i, k in enumerate(list(keep) + [False]):
        if k and s is None:
            s = i
        elif not k and s is not None:
            if i - s >= 2:
                out.append(xy[s:i])
            s = None
    return out


def corridor_axis(path_xy, trace_end, net, waterbodies=None, **kw):
    """Downstream corridor of one dam.

    path_xy:      tool 6's FULL flow path (cell centres, EPSG:5070), toe first.
    trace_end:    why the path ended (trace_path's end: edge / terminal basin / stuck / ...).
    net:          StreamNet of the stream lines within d_max + 1.5 km of the toe.
    waterbodies:  callable(bbox) -> [(key, exterior ring (n,2) EPSG:5070)], or None.
    Returns dict(axis, src ('p' path / 'n' stream / 'w' shore), line_m (straight-line distance
    from the toe), parts [(xy, src)] (the axis as lines, for the output layer), flags (reasons
    the corridor is incomplete: needs 2D), open_ends [(xy, line_m, 'path'|'stream')] that need a
    closed-basin proof, stats, toe).
    """
    o = dict(CORRIDOR_DEFAULTS); o.update(kw)
    D = o["d_max"]
    flags, ends, pieces, visited = [], [], [], {}
    stats = dict(walked_m=0.0, reached=0, gaps=0, waterbodies=0, outlets=0, lines=len(net.lines))
    pxy = np.asarray(path_xy, float).reshape(-1, 2)
    if len(pxy) < 2:
        stats["reach_m"] = 0.0
        return dict(axis=pxy, src=np.full(len(pxy), "p"), line_m=np.zeros(len(pxy)), parts=[],
                    flags=["no flow path from the breach toe"], open_ends=[], stats=stats,
                    toe=pxy[0].copy() if len(pxy) else None)
    toe = pxy[0].copy()
    palong = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(pxy, axis=0).T))))
    # ---- seeds: each stream line the path touches, at its most upstream touched vertex
    seeds = {}
    pidx = PointIndex(pxy, max(o["seed_tol"], 50.0))
    for k in range(len(pxy)):                            # stream vertices within seed_tol of the path
        for j in net.index.within(pxy[k], o["seed_tol"]):
            l, v = int(net.vl[j]), int(net.vi[j])
            if l not in seeds or v < seeds[l][0]:
                seeds[l] = (v, float(palong[k]))
    seeds = [(l, v, a) for l, (v, a) in seeds.items()]
    open_ends = []
    de, je = net.index.nearest(pxy[-1:], o["end_r"])
    linked = bool(np.isfinite(de[0]))
    if linked:                                           # the stream the path ends at carries on
        j = int(je[0])
        seeds.append((int(net.vl[j]), int(net.vi[j]), float(palong[-1] + de[0])))
    pline_end = float(np.hypot(*(pxy[-1] - toe)))
    if trace_end == "edge":
        if not linked:
            flags.append("the flow path leaves the terrain clip {:.1f} km from the dam with no stream "
                         "line within {:.0f} m".format(pline_end / 1000, o["end_r"]))
    elif trace_end == "terminal basin":
        if not linked:
            open_ends.append((pxy[-1].copy(), pline_end, "path"))
    else:
        flags.append("the flow path ends '{}' {:.1f} km from the dam".format(trace_end, pline_end / 1000))
    if seeds:
        _walk(net, seeds, toe, o, visited, flags, ends, pieces, stats)
    # ---- waterbody shores (and the lines leaving them), until nothing new is touched
    shores, seen, lakes, shore_lines = [], set(), [], []
    for _ in range(6):
        if waterbodies is None:
            break
        walked = [densify(c, o["dens"]) for c in pieces]
        # the streams touch a waterbody out to d_max only (a branch is walked on past it)
        walked = [w[np.hypot(w[:, 0] - toe[0], w[:, 1] - toe[1]) <= D + o["dens"]] for w in walked]
        axis = np.concatenate([pxy] + walked + shores)
        aidx = PointIndex(axis, 100.0)
        cand = {}
        for c0 in range(0, len(axis), 300):              # small boxes along the axis, not one big box
            ch = axis[c0:c0 + 300]
            lo, hi = ch.min(0) - o["wb_touch"] - 20.0, ch.max(0) + o["wb_touch"] + 20.0
            for key, ring in waterbodies((lo[0], lo[1], hi[0], hi[1])):
                if key not in seen:
                    cand[key] = ring
        new = []
        for key, ring in cand.items():
            r = densify(ring, o["dens"])
            touched = np.isfinite(aidx.nearest(r, o["wb_touch"] + 1.0)[0]).any()
            if not touched:                              # axis inside a wide river / lake polygon
                s = axis[np.linspace(0, len(axis) - 1, min(len(axis), 200)).astype(int)]
                inb = (s[:, 0] >= r[:, 0].min()) & (s[:, 0] <= r[:, 0].max()) & \
                      (s[:, 1] >= r[:, 1].min()) & (s[:, 1] <= r[:, 1].max())
                touched = bool(inb.any() and _in_ring(s[inb], ring).any())
            if not touched:
                continue
            seen.add(key)
            inr = np.hypot(r[:, 0] - toe[0], r[:, 1] - toe[1]) <= D
            keep = r[inr]
            if len(keep):
                shores.append(keep)
                shore_lines += _runs(r, inr)                 # the output layer: no jump across a cut
            new.append(r)
        if not new:
            break
        stats["waterbodies"] += len(new)
        out = []
        for r in new:                                    # lines that START on the shore: outlets
            d, _ = PointIndex(r, 100.0).nearest(net.vxy[net.start_v], o["wb_touch"])
            starts = [int(j) for j, x in zip(net.start_v, d) if np.isfinite(x)
                      and math.hypot(net.vxy[j, 0] - toe[0], net.vxy[j, 1] - toe[1]) <= D]
            ridx = PointIndex(r, 100.0)
            leaves = False                               # an OUTLET ends outside the waterbody
            for j in starts:
                e = net.lines[int(net.vl[j])][-1][None, :]
                if not _in_ring(e, r)[0] and not np.isfinite(ridx.nearest(e, o["wb_touch"])[0][0]):
                    leaves = True
                    break
            lakes.append((r, leaves))
            out += [(int(net.vl[j]), 0, 0.0) for j in starts if int(net.vl[j]) not in visited]
        if out:
            stats["outlets"] += len(out)
            _walk(net, out, toe, o, visited, flags, ends, pieces, stats)
    parts = [(pxy, "p")] + [(densify(c, o["dens"]), "n") for c in pieces] + [(s, "w") for s in shores]
    axis = np.concatenate([p for p, _ in parts])
    src = np.concatenate([np.full(len(p), k) for p, k in parts])
    line = np.hypot(axis[:, 0] - toe[0], axis[:, 1] - toe[1])
    keep = line <= D + 1e-6
    axis, src, line = axis[keep], src[keep], line[keep]
    lines = [(run, k) for p, k in parts if k != "w"
             for run in _runs(p, np.hypot(p[:, 0] - toe[0], p[:, 1] - toe[1]) <= D + 1e-6)]
    lines += [(run, "w") for run in shore_lines]
    # ---- stream ends before d_max: resolved when the DEM path carries on from there
    done = []
    for e_xy, e_r in ends:
        if any(math.hypot(*(e_xy - q)) <= o["wb_touch"] for q in done):
            continue                                     # the same end reached twice
        done.append(e_xy)
        dp, ip = pidx.nearest(e_xy[None, :], o["seed_tol"])
        if np.isfinite(dp[0]) and palong[-1] - palong[int(ip[0])] > o["end_r"]:
            continue                                     # the DEM path carries on from here
        if any(out and (np.isfinite(PointIndex(r, 100.0).nearest(e_xy[None, :], o["wb_touch"])[0][0])
                        or _in_ring(e_xy[None, :], r)[0]) for r, out in lakes):
            continue                                     # into a lake that has an outlet (walked)
        open_ends.append((e_xy, e_r, "stream"))
    stats["reach_m"] = float(line.max()) if len(line) else 0.0
    return dict(axis=axis, src=src, line_m=line, parts=lines, flags=flags, open_ends=open_ends,
                stats=stats, toe=toe)


def closed_basin(dem, rc, need_m3, cell):
    """Closed-depression proof at grid cell rc: the depression system there (8-connected cells
    the eps-0 pit fill raises by more than 1 cm) must not touch the window edge or missing
    terrain, and must store at least need_m3 below its spill level.  Returns
    dict(holds, volume_m3, area_km2, why)."""
    zf = fill_eps(dem, eps=0.0)
    valid = dem > NODATA + 1
    dep = valid & ((zf - dem) > 0.01)
    ny, nx = dem.shape
    r0, c0 = rc
    seeds = [(r, c) for r in range(r0 - 2, r0 + 3) for c in range(c0 - 2, c0 + 3)
             if 0 <= r < ny and 0 <= c < nx and dep[r, c]]
    if not seeds:
        return dict(holds=False, volume_m3=0.0, area_km2=0.0, why="no closed depression there")
    comp = np.zeros(dem.shape, bool)
    stack = list(seeds)
    for s in seeds:
        comp[s] = True
    while stack:
        r, c = stack.pop()
        for dr, dc in N8:
            a, b = r + dr, c + dc
            if 0 <= a < ny and 0 <= b < nx and dep[a, b] and not comp[a, b]:
                comp[a, b] = True
                stack.append((a, b))
    edge = np.zeros(dem.shape, bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    grow = comp.copy()
    grow[1:, :] |= comp[:-1, :]; grow[:-1, :] |= comp[1:, :]
    grow[:, 1:] |= grow[:, :-1].copy(); grow[:, :-1] |= grow[:, 1:].copy()
    vol = float(((zf - dem) * comp).sum()) * cell * cell
    area = float(comp.sum()) * cell * cell / 1e6
    if (grow & (edge | ~valid)).any():
        return dict(holds=False, volume_m3=vol, area_km2=area,
                    why="the depression runs off the terrain window")
    if vol < need_m3:
        return dict(holds=False, volume_m3=vol, area_km2=area,
                    why="the closed basin holds {:,.0f} m3, less than {:,.0f} m3".format(vol, need_m3))
    return dict(holds=True, volume_m3=vol, area_km2=area,
                why="closed basin of {:.2f} km2 holds {:,.0f} m3".format(area, vol))


def state_margin(xy, ring):
    """Signed distance (m) of EPSG:5070 points to the state outline ring: > 0 inside, < 0
    outside.  Only outline segments near the points' box are tested."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    ring = np.asarray(ring, float)
    lo, hi = xy.min(0) - 5000.0, xy.max(0) + 5000.0
    a, b = ring[:-1], ring[1:]
    near = ~((np.maximum(a[:, 0], b[:, 0]) < lo[0]) | (np.minimum(a[:, 0], b[:, 0]) > hi[0]) |
             (np.maximum(a[:, 1], b[:, 1]) < lo[1]) | (np.minimum(a[:, 1], b[:, 1]) > hi[1]))
    inside = _in_ring(xy, ring)
    if not near.any():
        return np.where(inside, 5000.0, -5000.0)
    a, b = a[near], b[near]
    ab = b - a
    L2 = np.maximum((ab ** 2).sum(1), 1e-12)
    t = np.clip(((xy[:, None, :] - a[None]) * ab[None]).sum(2) / L2[None], 0, 1)
    p = a[None] + t[..., None] * ab[None]
    d = np.hypot(xy[:, None, 0] - p[..., 0], xy[:, None, 1] - p[..., 1]).min(1)
    return np.where(inside, d, -d)
