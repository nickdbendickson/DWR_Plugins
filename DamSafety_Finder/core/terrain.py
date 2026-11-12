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
