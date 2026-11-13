"""Drainage traced from the DEM itself, for the draws no stream layer has (Finder 0.1).

Pure numpy / scipy. The flow directions come from a priority flood (Barnes, Lehman & Mulla 2014):
cells are taken from the outlets (the grid edge and the edge of no-data) inwards, lowest first;
each cell drains to the cell it was reached from, so a pit drains out over its lowest rim (a dam's
pool drains out over its spill point) and a flat drains towards where it was entered. The filled
surface of the same pass gives each cell's depth below its spill level (a pond, a dugout, a closed
depression).

The lines are routed over the DEM with its narrow raised features opened away (open_ridges): a
dam or a road fill across a draw is narrower than the valley, so the line runs straight through
it, as a mapped stream does, instead of round it by the spillway.

Every draw whose drainage area reaches `min_area_m2` becomes a line, traced from its head down to
where it meets a line already traced or the grid edge. The fill finder walks these lines exactly as
it walks a stream layer, so a stock dam on a coulee no one mapped is found too. A drainage area
near the grid edge is a minimum (the grid does not hold the whole basin): the plugins read the
DEM with a pad round each tile.
"""
import heapq
import math

import numpy as np

SQ2 = math.sqrt(2.0)
_NB = ((-1, -1, SQ2), (-1, 0, 1.0), (-1, 1, SQ2), (0, -1, 1.0), (0, 1, 1.0), (1, -1, SQ2),
       (1, 0, 1.0), (1, 1, SQ2))


def flow(dem):
    """(receiver, order, filled) of a DEM (2-D, NaN = no data): receiver[i] is the flat index of
    the cell i drains to (-1 = an outlet: the grid edge or next to no data), `order` the flat
    indices from the outlets upstream (each cell after its receiver), `filled` the DEM with every
    pit filled to its spill level."""
    z = np.asarray(dem, float)
    h, w = z.shape
    n = h * w
    valid = np.isfinite(z)
    zl = np.where(valid, z, np.inf).ravel().tolist()
    vl = valid.ravel().tolist()
    filled = np.full(n, np.nan)
    rec = np.full(n, -1, dtype=np.int64)
    closed = bytearray(n)
    heap = []
    # seeds: valid cells on the grid edge or next to no data
    edge = np.zeros((h, w), bool)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    if not valid.all():
        from scipy import ndimage as ndi
        edge |= ndi.binary_dilation(~valid, structure=np.ones((3, 3), bool))
    seq = 0
    for i in np.flatnonzero((edge & valid).ravel()).tolist():
        closed[i] = 1
        heapq.heappush(heap, (zl[i], seq, i))
        seq += 1
    order = []
    fl = filled.tolist()
    rl = rec.tolist()
    push, pop = heapq.heappush, heapq.heappop
    offs = [(dr * w + dc, dr, dc) for dr, dc, _d in _NB]
    while heap:
        zc, _s, i = pop(heap)
        fl[i] = zc
        order.append(i)
        r, c = divmod(i, w)
        inner = 0 < r < h - 1 and 0 < c < w - 1
        for off, dr, dc in offs:
            if not inner:
                rr, cc = r + dr, c + dc
                if rr < 0 or rr >= h or cc < 0 or cc >= w:
                    continue
            j = i + off
            if closed[j] or not vl[j]:
                continue
            closed[j] = 1
            rl[j] = i
            zj = zl[j]
            seq += 1
            push(heap, (zj if zj > zc else zc, seq, j))
    return (np.asarray(rl, dtype=np.int64), np.asarray(order, dtype=np.int64),
            np.asarray(fl, float).reshape(h, w))


def accumulate(receiver, order, shape, cell, dem=None):
    """(drainage area m2, longest flow path m) of every cell, from flow(); with `dem`, also the
    ground elevation at the head of that longest path (for the path's average slope)."""
    h, w = shape
    n = h * w
    area = [cell * cell] * n
    length = [0.0] * n
    top = np.asarray(dem, float).ravel().tolist() if dem is not None else None
    rec = receiver.tolist()
    for i in reversed(order.tolist()):
        j = rec[i]
        if j < 0:
            continue
        area[j] += area[i]
        k = abs(i - j)
        d = cell if k == 1 or k == w else cell * SQ2
        if length[i] + d > length[j]:
            length[j] = length[i] + d
            if top is not None:
                top[j] = top[i]
    out = (np.asarray(area).reshape(shape), np.asarray(length).reshape(shape))
    if top is not None:
        out = out + (np.asarray(top).reshape(shape),)
    return out


def drainage_lines(receiver, area, gt, min_area_m2, min_cells=5):
    """Polylines [(x, y), ...] (downstream order) of every draw draining at least min_area_m2,
    from its head to the cell where it meets a line already traced (included, so the lines join)
    or to an outlet. `gt` is the GDAL geotransform of the grid."""
    h, w = area.shape
    ch = (area >= min_area_m2).ravel()
    rec = receiver
    donor = np.zeros(h * w, bool)
    idx = np.flatnonzero(ch)
    tgt = rec[idx]
    ok = tgt >= 0
    donor[tgt[ok][ch[tgt[ok]]]] = True
    heads = idx[~donor[idx]]
    heads = heads[np.argsort(-area.ravel()[heads], kind="stable")]       # big draws first
    seen = np.zeros(h * w, bool)
    recl = rec.tolist()
    chl = ch.tolist()
    lines = []
    for hd in heads.tolist():
        path = [hd]
        seen[hd] = True
        cur = hd
        while True:
            nxt = recl[cur]
            if nxt < 0 or not chl[nxt]:
                break
            path.append(nxt)
            if seen[nxt]:
                break
            seen[nxt] = True
            cur = nxt
        if len(path) >= min_cells:
            rr, cc = np.divmod(np.asarray(path), w)
            lines.append(list(zip((gt[0] + (cc + 0.5) * gt[1]).tolist(),
                                  (gt[3] + (rr + 0.5) * gt[5]).tolist())))
    return lines


def open_ridges(dem, cell, width_m):
    """The DEM with everything raised and narrower than width_m removed (grey opening): fills,
    embankments, roads. Valleys are kept. NaN stays NaN."""
    from scipy import ndimage as ndi
    z = np.asarray(dem, float)
    fin = np.isfinite(z)
    if not fin.any():
        return z.copy()
    k = max(3, int(round(width_m / cell)) | 1)
    o = ndi.grey_opening(np.where(fin, z, np.nanmax(z)), size=(k, k))
    o[~fin] = np.nan
    return o


def depth(dem, filled):
    """Depth of each cell below its spill level (0 on ground that drains freely)."""
    d = np.asarray(filled, float) - np.asarray(dem, float)
    d[~np.isfinite(d)] = 0.0
    return np.maximum(d, 0.0)


# ----------------------------------------------------------------------------- pool outlets --
# A pond on a hillside, on a terrace or at the head of a swale can drain less than the draw
# threshold, so no draw crosses its dam however the draws are routed (Iowa's pond-dam census: at
# 10 ac most of the missed dams had no draw within 30 m). Every POOL the DEM shows - a closed
# depression, or a flat water surface whose rim is all higher (hydro-flattened water, or the near-
# flat TIN a pond with no lidar returns becomes) - gets its own short line instead: from inside the
# pool down the opened-ridge routing (the receivers the draws use), out through the pool's outlet
# and on below it, so the fill that holds the pool is profiled whatever drains to it.


def flat_pools(dem, cell, tol_m=0.15, min_m2=300.0, ring_tol_m=0.10, max_lower=0.10):
    """(label grid, [label, ...]) of the flat water surfaces of a DEM: connected (8-way) cells
    whose 3x3 neighbourhood spans at most tol_m, at least min_m2 in all, and whose 1-cell ring is
    (almost) all higher: at most max_lower of the ring more than ring_tol_m below the flat's median
    (a field or a terrace bench has lower ground along one side)."""
    from scipy import ndimage as ndi
    z = np.asarray(dem, float)
    fin = np.isfinite(z)
    mx = ndi.maximum_filter(np.where(fin, z, 1e9), size=3)
    mn = ndi.minimum_filter(np.where(fin, z, -1e9), size=3)
    lab, n = ndi.label(fin & (mx - mn <= tol_m), structure=np.ones((3, 3), bool))
    if not n:
        return lab, []
    cnt = np.bincount(lab.ravel(), minlength=n + 1)
    keep = []
    for k, sl in enumerate(ndi.find_objects(lab), start=1):
        if sl is None or cnt[k] * cell * cell < min_m2:
            continue
        r0, r1 = max(0, sl[0].start - 1), min(z.shape[0], sl[0].stop + 1)
        c0, c1 = max(0, sl[1].start - 1), min(z.shape[1], sl[1].stop + 1)
        m = lab[r0:r1, c0:c1] == k
        sub = z[r0:r1, c0:c1]
        lev = float(np.median(sub[m]))
        ring = ndi.binary_dilation(m, structure=np.ones((3, 3), bool)) & ~m & np.isfinite(sub)
        if ring.any() and float((sub[ring] < lev - ring_tol_m).mean()) <= max_lower:
            keep.append(k)
    return lab, keep


def pool_seeds(dem, gt, dep=None, flat_tol_m=0.15, flat_min_m2=300.0, dep_min_m=0.15, dep_min_m2=100.0):
    """(seeds [(x, y, area m2, kind)], pool mask) of the pools of a DEM (grid gt): the most interior
    cell of every flat water surface (flat_pools; kind 'flat') and the deepest cell of every closed
    depression at least dep_min_m deep over at least dep_min_m2 (cells more than 2 cm below their
    spill level; kind 'depression'). dep: depth below the spill level (depth() of flow()), computed
    when None."""
    from scipy import ndimage as ndi
    z = np.asarray(dem, float)
    cell = abs(gt[1])
    seeds = []
    lab, keep = flat_pools(z, cell, flat_tol_m, flat_min_m2)
    mask = np.isin(lab, keep) if keep else np.zeros(z.shape, bool)
    objs = ndi.find_objects(lab)
    for k in keep:
        sl = objs[k - 1]
        m = lab[sl] == k
        dt = ndi.distance_transform_edt(np.pad(m, 1))[1:-1, 1:-1]
        r, c = np.unravel_index(int(np.argmax(dt)), dt.shape)
        r, c = r + sl[0].start, c + sl[1].start
        seeds.append((gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5], float(m.sum() * cell * cell), "flat"))
    if dep is None:
        _r, _o, filled = flow(z)
        dep = depth(z, filled)
    dl, n = ndi.label(dep > 0.02, structure=np.ones((3, 3), bool))
    if n:
        idx = np.arange(1, n + 1)
        mx = np.asarray(ndi.maximum(dep, dl, idx))
        cnt = np.bincount(dl.ravel(), minlength=n + 1)[1:]
        pos = ndi.maximum_position(dep, dl, idx)
        ok = (mx >= dep_min_m) & (cnt * cell * cell >= dep_min_m2)
        for k in np.flatnonzero(ok).tolist():
            r, c = pos[k]
            seeds.append((gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5], float(cnt[k] * cell * cell),
                          "depression"))
        mask |= np.isin(dl, np.flatnonzero(ok) + 1)
    return seeds, mask


def pool_lines(seeds, receiver, gt, shape, pool=None, down_m=300.0, max_m=1500.0, min_cells=5):
    """(lines, exits): polylines [(x, y), ...] in downstream order from each seed along `receiver`
    (flow() of the opened DEM; grid gt / shape), from the seed's cell to down_m beyond where the
    path leaves its pool (pool: bool grid on gt; None = at the seed's own cell), at most max_m in
    all; and for each line the distance along it (m) at which it leaves its pool. A seed in a cell
    another seed already started from is skipped (put the biggest pools first)."""
    h, w = shape
    cell = abs(gt[1])
    lines, exits, seen = [], [], set()
    for x, y, *_ in seeds:
        c = int(math.floor((x - gt[0]) / gt[1]))
        r = int(math.floor((y - gt[3]) / gt[5]))
        if not (0 <= r < h and 0 <= c < w):
            continue
        i = r * w + c
        if i in seen:
            continue
        seen.add(i)
        path, tot = [i], 0.0
        out_m = None if (pool is not None and pool[r, c]) else 0.0
        while True:
            j = int(receiver[i])
            if j < 0:
                break
            tot += cell if abs(i - j) in (1, w) else cell * SQ2
            path.append(j)
            i = j
            if out_m is None and not pool[j // w, j % w]:
                out_m = tot
            if (out_m is not None and tot - out_m >= down_m) or tot >= max_m:
                break
        if len(path) >= min_cells:
            rr, cc = np.divmod(np.asarray(path), w)
            lines.append(list(zip((gt[0] + (cc + 0.5) * gt[1]).tolist(),
                                  (gt[3] + (rr + 0.5) * gt[5]).tolist())))
            exits.append(out_m if out_m is not None else tot)
    return lines, exits
