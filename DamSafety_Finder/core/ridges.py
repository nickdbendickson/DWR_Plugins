"""Raised ridges and dug-out pits found anywhere in a DEM, with or without a draw (Finder 0.1).

Pure numpy / scipy.

RIDGES - levees, dikes, off-channel storage embankments, a dam on a draw with no line.
  The ground's rise above its surroundings is the DEM minus its grey opening by `width_m` (a
  white top-hat): anything narrower than width_m that stands up is kept, broad hills are not. A
  ridge is a patch of rise >= `min_height_m` that is long (major axis >= `min_len_m`), thin (axis
  ratio >= `min_ratio`) and straight (it fills at least `min_rect` of its smallest rectangle: the
  spur between two meander bends does not), away from the road / rail mask (the caller widens it
  to the base of a road embankment). It holds water when a
  closed depression at least `min_head_m` deep (the depth below the spill level from
  core/terrain.py) lies along it.

DUGOUTS - a stock-water pit dug into the ground, wet or dry.
  A pit (depth below the DEM's grey closing by `dug_close_m`: any hollow narrower than that is
  filled to its rim) at least `dug_min_depth_m` deep, between
  `dug_min_m2` and `dug_max_m2`, whose outline fills at least `dug_rect` of its smallest enclosing
  rectangle (dug pits are rectangles, potholes are round or ragged), no more than `dug_max_ratio`
  times as long as wide, with spoil: the upper quarter of the ground within 8 m of the pit at least
  `spoil_m` higher than the ground 20-40 m out (the dug material is piled along some sides).

Every threshold is the engineer's value; the defaults are starting values. A natural ridge (an
esker, a beach ridge, a moraine) can pass the ridge test; check each one on the hillshade.
"""
import math
from dataclasses import dataclass

import numpy as np

try:  # scipy ships with the QGIS bundles
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None

K_LEVEE = "dike / levee (holds water)"
K_RIDGE = "raised ridge"


@dataclass
class RidgeParams:
    width_m: float = 60.0          # widest thing the top-hat keeps (an embankment's base)
    min_height_m: float = 1.0      # rise above the surroundings
    min_len_m: float = 80.0
    min_ratio: float = 5.0         # length / width
    min_rect: float = 0.6          # share of its smallest rectangle it fills (straight, not a bend)
    min_head_m: float = 0.3        # depth of a closed depression along it = holds water
    road_share: float = 0.3        # more of its cells on the road / rail mask = a road, dropped
    dug_min_depth_m: float = 1.0
    dug_min_m2: float = 150.0
    dug_max_m2: float = 20000.0
    dug_rect: float = 0.85
    dug_max_ratio: float = 8.0
    spoil_m: float = 0.3
    dug_close_m: float = 120.0     # widest pit looked for


def _axes(rows, cols, cell):
    """(major length m, minor length m, angle rad) of a cell set: 4 x the standard deviation along
    the principal axes (a filled rectangle of sides a, b gives about a x 1.15 and b x 1.15)."""
    if len(rows) < 3:
        return len(rows) * cell, cell, 0.0
    x = cols.astype(float) * cell
    y = -rows.astype(float) * cell
    cov = np.cov(np.vstack([x - x.mean(), y - y.mean()]))
    ev, evec = np.linalg.eigh(cov)
    ev = np.maximum(ev, (cell * cell) / 12.0)
    major = 2.0 * math.sqrt(3.0 * ev[1])          # a uniform bar of length L has var L^2 / 12
    minor = 2.0 * math.sqrt(3.0 * ev[0])
    return major, minor, math.atan2(evec[1, 1], evec[0, 1])


def _hull(pts):
    """Convex hull (Andrew's monotone chain) of [(x, y)]."""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lo, up = [], []
    for p in pts:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], p) <= 0:
            lo.pop()
        lo.append(p)
    for p in reversed(pts):
        while len(up) >= 2 and cross(up[-2], up[-1], p) <= 0:
            up.pop()
        up.append(p)
    return lo[:-1] + up[:-1]


def rect_of(points):
    """(area m2, long side m, short side m) of the smallest rectangle round points [(x, y)]."""
    hull = np.asarray(_hull([(float(x), float(y)) for x, y in points]), float)
    if len(hull) < 3:
        return 0.0, 0.0, 0.0
    best = None
    for k in range(len(hull)):
        e = hull[(k + 1) % len(hull)] - hull[k]
        t = math.atan2(e[1], e[0])
        ca, sa = math.cos(t), math.sin(t)
        u = hull[:, 0] * ca + hull[:, 1] * sa
        v = -hull[:, 0] * sa + hull[:, 1] * ca
        a, b = u.max() - u.min(), v.max() - v.min()
        if best is None or a * b < best[0]:
            best = (a * b, max(a, b), min(a, b))
    return best


def min_rect(rows, cols, cell):
    """(area m2, long side m, short side m) of the smallest rectangle round the cells (their
    corners), by rotating calipers over the hull edges."""
    corners = []
    for r, c in zip(rows.tolist(), cols.tolist()):
        corners += [(c * cell, r * cell), ((c + 1) * cell, r * cell), (c * cell, (r + 1) * cell),
                    ((c + 1) * cell, (r + 1) * cell)]
    best = rect_of(corners)
    if best[0] <= 0:
        return float(len(rows) * cell * cell), cell, cell
    return best


def find_ridges(dem, gt, depth=None, road_mask=None, params=None, keep=None, exclude=()):
    """Ridges of a DEM (2-D m, NaN = no data; gt a north-up geotransform). `depth`: the depth of
    each cell below its spill level (terrain.depth), same grid; without it no ridge is said to hold
    water. `exclude`: (x, y) points (fills already found): a ridge within 3 cells of one is that
    fill and is left out. Returns [dict(x, y, kind, height_m, length_m, width_m, head_m, cells)],
    highest first."""
    p = params or RidgeParams()
    z = np.asarray(dem, float)
    cell = abs(gt[1])
    fin = np.isfinite(z)
    if not fin.any():
        return []
    g = np.where(fin, z, np.nanmin(z))
    k = max(3, int(round(p.width_m / cell)) | 1)
    rise = g - ndi.grey_opening(g, size=(k, k))
    rise[~fin] = 0.0
    e = k // 2 + 1                                  # the opening is not valid at the grid edge
    rise[:e, :] = rise[-e:, :] = rise[:, :e] = rise[:, -e:] = 0.0
    lab, n = ndi.label(rise >= p.min_height_m, structure=np.ones((3, 3), bool))
    out = []
    if n == 0:
        return out
    if exclude:
        near = ndi.binary_dilation(lab > 0, iterations=3)
        drop = set()
        big = ndi.maximum_filter(lab, size=7)
        for ex, ey in exclude:
            c = int(math.floor((ex - gt[0]) / gt[1]))
            r = int(math.floor((ey - gt[3]) / gt[5]))
            if 0 <= r < lab.shape[0] and 0 <= c < lab.shape[1] and near[r, c] and big[r, c]:
                drop.add(int(big[r, c]))
    else:
        drop = set()
    objs = ndi.find_objects(lab)
    for i, sl in enumerate(objs, start=1):
        if sl is None or i in drop:
            continue
        m = lab[sl] == i
        rr, cc = np.nonzero(m)
        if len(rr) * cell * cell < p.min_len_m * cell:          # too small to be min_len long
            continue
        major, minor, _ang = _axes(rr, cc, cell)
        if major < p.min_len_m or major / max(minor, cell) < p.min_ratio:
            continue
        if len(rr) * cell * cell / max(min_rect(rr, cc, cell)[0], 1e-9) < p.min_rect:
            continue
        rows, cols = rr + sl[0].start, cc + sl[1].start
        if road_mask is not None and road_mask[rows, cols].mean() > p.road_share:
            continue
        top = int(np.argmax(rise[rows, cols]))
        x = gt[0] + (cols[top] + 0.5) * gt[1]
        y = gt[3] + (rows[top] + 0.5) * gt[5]
        if keep is not None and not keep(x, y):
            continue
        head = 0.0
        if depth is not None:
            r0, r1 = max(0, sl[0].start - 4), min(z.shape[0], sl[0].stop + 4)
            c0, c1 = max(0, sl[1].start - 4), min(z.shape[1], sl[1].stop + 4)
            mm = np.zeros((r1 - r0, c1 - c0), bool)
            mm[rows - r0, cols - c0] = True
            ring = ndi.binary_dilation(mm, iterations=3) & ~mm
            dd = depth[r0:r1, c0:c1][ring]
            head = float(np.percentile(dd, 90)) if dd.size else 0.0
        out.append({"x": x, "y": y, "kind": K_LEVEE if head >= p.min_head_m else K_RIDGE,
                    "height_m": float(rise[rows, cols].max()), "length_m": major,
                    "width_m": len(rows) * cell * cell / max(major, cell), "head_m": head,
                    "cells": len(rows)})
    out.sort(key=lambda d: -d["height_m"])
    return out


def pit_depth(dem, cell, close_m):
    """Depth of every cell below the DEM's grey closing by close_m (NaN = no data -> 0)."""
    z = np.asarray(dem, float)
    fin = np.isfinite(z)
    if not fin.any():
        return np.zeros(z.shape)
    g = np.where(fin, z, np.nanmax(z))
    k = max(3, int(round(close_m / cell)) | 1)
    d = ndi.grey_closing(g, size=(k, k)) - g
    d[~fin] = 0.0
    return d


def find_dugouts(dem, gt, depth=None, params=None, keep=None):
    """Dug pits of a DEM (see the module notes); `depth` defaults to pit_depth (the depth is
    below the rim, the spoil tops where there are piles).
    Returns [dict(x, y, depth_m, area_m2, length_m, width_m, rect, spoil_m, volume_m3)]."""
    p = params or RidgeParams()
    z = np.asarray(dem, float)
    cell = abs(gt[1])
    if depth is None:
        depth = pit_depth(z, cell, p.dug_close_m)
    lab, n = ndi.label(depth >= 0.25)
    out = []
    if n == 0:
        return out
    for i, sl in enumerate(ndi.find_objects(lab), start=1):
        m = lab[sl] == i
        area = float(m.sum()) * cell * cell
        if area < p.dug_min_m2 or area > p.dug_max_m2:
            continue
        dsub = depth[sl]
        dmax = float(dsub[m].max())
        if dmax < p.dug_min_depth_m:
            continue
        rr, cc = np.nonzero(m)
        rect_a, long_s, short_s = min_rect(rr, cc, cell)
        rect = area / rect_a if rect_a > 0 else 0.0
        if rect < p.dug_rect or long_s / max(short_s, cell) > p.dug_max_ratio:
            continue
        rows, cols = rr + sl[0].start, cc + sl[1].start
        pad = int(math.ceil(40.0 / cell)) + 1
        r0, r1 = max(0, sl[0].start - pad), min(z.shape[0], sl[0].stop + pad)
        c0, c1 = max(0, sl[1].start - pad), min(z.shape[1], sl[1].stop + pad)
        mm = np.zeros((r1 - r0, c1 - c0), bool)
        mm[rows - r0, cols - c0] = True
        near = ndi.binary_dilation(mm, iterations=max(1, int(round(8.0 / cell)))) & ~mm
        inner = ndi.binary_dilation(mm, iterations=max(2, int(round(20.0 / cell))))
        far = ndi.binary_dilation(mm, iterations=max(3, int(round(40.0 / cell)))) & ~inner
        zs = z[r0:r1, c0:c1]
        a, b = zs[near & np.isfinite(zs)], zs[far & np.isfinite(zs)]
        if a.size == 0 or b.size == 0:
            continue
        spoil = float(np.percentile(a, 75) - np.median(b))     # piles need not ring the whole pit
        if spoil < p.spoil_m:
            continue
        cy, cx = rows.mean(), cols.mean()
        x = gt[0] + (cx + 0.5) * gt[1]
        y = gt[3] + (cy + 0.5) * gt[5]
        if keep is not None and not keep(x, y):
            continue
        out.append({"x": x, "y": y, "depth_m": dmax, "area_m2": area, "length_m": long_s,
                    "width_m": short_s, "rect": rect, "spoil_m": spoil,
                    "volume_m3": float(dsub[m].sum()) * cell * cell})
    out.sort(key=lambda d: -d["volume_m3"])
    return out
