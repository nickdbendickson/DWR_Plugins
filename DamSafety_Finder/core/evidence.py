"""Evidence beyond the bare-earth DEM for each fill (Finder 0.5).

Pure numpy (scipy only for the flow path), no QGIS, no GDAL: the finder reads the rasters and
layers and hands this module grids and points. Each function answers one question a reviewer
asks of a candidate, from a source the DEM cannot see:

  WATER, YEAR AFTER YEAR (imagery with a near-infrared band: NAIP colour-infrared 2009-2020 on the
  ND GIS Hub, the 2018 4-band 30 cm): is there open water in the pool, in how many years, since
  when? A stock pond holds water most years; a field approach, a road fill or a natural hump does
  not. (A dug pond holds water too - this does not tell a dam from a dugout.)
  BARE GROUND round it (same imagery): a gravel pit or a quarry is bright bare ground.
  THE NWI CODE of the wetland it sits in: 'h' = diked / impounded, 'x' = excavated, and the water
  regime (A temporary ... H permanent). Mapped mostly from 1970s-80s photos: a weak label.
  LIDAR WATER (a LiDAR point-count raster and the class-9 water-count mosaic): water swallows the
  pulses, so a pond is a patch of empty cells; ponds under 2 acres are neither flattened nor
  classed water in 3DEP, so the empty-cell share inside the pool, against a ring round it, is a
  water test small ponds pass. A contrast within each candidate, never an absolute cut-off.
  WHAT IS BELOW IT (DEM, buildings, roads): the flow path from the fill downhill, the buildings
  beside it low enough to flood (Montana's small-dam screen: flood depth 75 % of the dam height,
  little damage past 5 miles) and the roads it crosses, in distance bands. A second sort for
  the review list, not a dam probability.

Nothing here is a determination; each is a field on the candidate for the reviewer and the score.
"""
import math
import re

import numpy as np

MILE = 1609.344
CONSEQ_BINS_M = (1.0 * MILE, 3.0 * MILE, 5.0 * MILE)      # 0-1, 1-3, 3-5 miles
PERMIT_AF = 12.5            # ND: a water permit is needed to store more than 12.5 acre-feet


def disk(shape, gt, x, y, r):
    """Bool grid of the cells (centres) within r metres of (x, y); gt top-left origin."""
    rows = gt[3] + (np.arange(shape[0]) + 0.5) * gt[5]
    cols = gt[0] + (np.arange(shape[1]) + 0.5) * gt[1]
    return (cols[None, :] - x) ** 2 + (rows[:, None] - y) ** 2 <= r * r


def ring(shape, gt, x, y, r_in, r_out):
    return disk(shape, gt, x, y, r_out) & ~disk(shape, gt, x, y, r_in)


def ndwi(green, nir):
    """(G - NIR) / (G + NIR); NaN where both are 0 or either is missing."""
    g = np.asarray(green, float)
    n = np.asarray(nir, float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (g - n) / (g + n)
    out[~np.isfinite(out)] = np.nan
    return out


def ndvi(red, nir):
    r = np.asarray(red, float)
    n = np.asarray(nir, float)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (n - r) / (n + r)
    out[~np.isfinite(out)] = np.nan
    return out


def wet(index, mask, threshold=0.15, min_share=0.10):
    """(water seen: True / False / None when the image has no data there, share of the masked
    cells over the NDWI threshold)."""
    v = np.asarray(index, float)[mask]
    v = v[np.isfinite(v)]
    if len(v) < max(4, 0.5 * int(mask.sum())):
        return None, float("nan")
    share = float((v > threshold).mean())
    return share >= min_share, share


def water_history(seen):
    """{year: True / False / None} -> {'years', 'wet_years', 'wet_share', 'first_wet', 'last_wet',
    'wet_list'}: how many imaged years show water in the pool."""
    yrs = sorted(y for y, v in seen.items() if v is not None)
    wy = [y for y in yrs if seen[y]]
    return {"years": len(yrs), "wet_years": len(wy),
            "wet_share": (len(wy) / float(len(yrs))) if yrs else float("nan"),
            "first_wet": wy[0] if wy else None, "last_wet": wy[-1] if wy else None,
            "wet_list": ",".join(str(y) for y in wy)}


def bare_share(red, green, blue, nir, mask, ndvi_max=0.15):
    """Share of the masked cells that are bright bare ground: no vegetation (NDVI under ndvi_max)
    and brighter than the median of the whole image window (pits, quarries, fresh spoil; open
    water is dark and is not counted)."""
    rgb = (np.asarray(red, float) + np.asarray(green, float) + np.asarray(blue, float)) / 3.0
    v = ndvi(red, nir)
    ok = mask & np.isfinite(v) & np.isfinite(rgb)
    if ok.sum() < 4:
        return float("nan")
    bright = rgb > np.nanmedian(rgb)
    return float(((v < ndvi_max) & bright)[ok].mean())


_REGIME = re.compile(r"([A-HJKLMNPRSTUVWYZ])[a-z]*$")


def nwi_parse(code):
    """{'code', 'system' (P, L, R, ...), 'regime' (A-H, K-Z water-regime letter, '' when none),
    'h' (diked / impounded), 'x' (excavated)} of an NWI attribute code like 'PUBHh' or 'PABFx'."""
    c = str(code or "").strip()
    m = re.match(r"^([MERLP])[0-9A-Z/]*?([A-Z]?)([a-z]*)$", c)
    if not m:
        return {"code": c, "system": "", "regime": "", "h": False, "x": False}
    mods = m.group(3)
    return {"code": c, "system": m.group(1), "regime": m.group(2), "h": "h" in mods, "x": "x" in mods}


def dropout(points, pool, ring_mask, water=None):
    """LiDAR evidence for water in a pool. `points`: per-cell count of all returns (0 = no
    return); `water`: per-cell count of class-9 (water) points, optional. {'pool_empty' (share of
    pool cells with no return), 'ring_empty', 'contrast' (pool - ring: water swallows pulses),
    'class9' (share of pool cells with a water point)}. A contrast within one candidate: occlusion
    and scan angle also thin the returns, so never read it against a fixed cut-off."""
    p = np.asarray(points, float)
    ok = np.isfinite(p)
    out = {"pool_empty": float("nan"), "ring_empty": float("nan"), "contrast": float("nan"),
           "class9": float("nan")}
    a, b = pool & ok, ring_mask & ok
    if a.sum() >= 4:
        out["pool_empty"] = float((p[a] <= 0).mean())
    if b.sum() >= 4:
        out["ring_empty"] = float((p[b] <= 0).mean())
    if math.isfinite(out["pool_empty"]) and math.isfinite(out["ring_empty"]):
        out["contrast"] = out["pool_empty"] - out["ring_empty"]
    if water is not None:
        w = np.asarray(water, float)
        m = pool & np.isfinite(w)
        if m.sum() >= 4:
            out["class9"] = float((w[m] > 0).mean())
    return out


def flow_path(receiver, shape, start, max_steps=100000):
    """Cells (flat indices) from `start` down the receivers of terrain.flow() to an outlet."""
    path = [int(start)]
    rec = receiver
    seen = {int(start)}
    while len(path) < max_steps:
        j = int(rec[path[-1]])
        if j < 0 or j in seen:
            break
        path.append(j)
        seen.add(j)
    return path


def path_xy(path, shape, gt):
    """(xy (n, 2), cumulative distance m (n,)) of a flow path's cells."""
    r, c = np.divmod(np.asarray(path, np.int64), shape[1])
    xy = np.c_[gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5]]
    d = np.r_[0.0, np.cumsum(np.hypot(np.diff(xy[:, 0]), np.diff(xy[:, 1])))]
    return xy, d


def _seg_cross(p1, p2, q1, q2):
    def orient(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    d1, d2 = orient(q1, q2, p1), orient(q1, q2, p2)
    d3, d4 = orient(p1, p2, q1), orient(p1, p2, q2)
    if d1 == d2 == 0:                                   # collinear: not a crossing
        return False
    return (d1 * d2 <= 0) and (d3 * d4 <= 0)            # an end on the other line counts


def road_crossings(xy, dist, roads, dedupe_m=50.0):
    """Distances (m along the path) where the flow path crosses a road / rail line (lists of
    (x, y)); crossings closer than dedupe_m are one (a divided road)."""
    if len(xy) < 2:
        return []
    x0, y0 = xy.min(axis=0)
    x1, y1 = xy.max(axis=0)
    hits = []
    for ln in roads:
        a = np.asarray(ln, float)
        if len(a) < 2 or a[:, 0].max() < x0 or a[:, 0].min() > x1 or a[:, 1].max() < y0 or a[:, 1].min() > y1:
            continue
        for k in range(len(a) - 1):
            q1, q2 = a[k], a[k + 1]
            sx0, sx1 = min(q1[0], q2[0]), max(q1[0], q2[0])
            sy0, sy1 = min(q1[1], q2[1]), max(q1[1], q2[1])
            near = np.flatnonzero((np.maximum(xy[:-1, 0], xy[1:, 0]) >= sx0) & (np.minimum(xy[:-1, 0], xy[1:, 0]) <= sx1)
                                  & (np.maximum(xy[:-1, 1], xy[1:, 1]) >= sy0) & (np.minimum(xy[:-1, 1], xy[1:, 1]) <= sy1))
            for i in near:
                if _seg_cross(xy[i], xy[i + 1], q1, q2):
                    hits.append(float(dist[i]))
    hits.sort()
    out = []
    for h in hits:
        if not out or h - out[-1] > dedupe_m:
            out.append(h)
    return out


def flooded_buildings(xy, dist, zpath, buildings, height_m, reach_m=5.0 * MILE, side_m=300.0,
                      depth_frac=0.75):
    """Buildings [(x, y, ground z)] beside the flow path (within side_m of it, within reach_m
    along it) whose ground is below the path's ground there plus depth_frac x the dam height
    (Montana DNRC small-dam screen). Returns the distance along the path of each."""
    out = []
    if not len(xy) or not len(buildings):
        return out
    keep = dist <= reach_m
    pxy, pd, pz = xy[keep], dist[keep], zpath[keep]
    depth = depth_frac * max(height_m, 0.0)
    for bx, by, bz in buildings:
        d2 = (pxy[:, 0] - bx) ** 2 + (pxy[:, 1] - by) ** 2
        k = int(np.argmin(d2))
        if d2[k] > side_m * side_m:
            continue
        if math.isfinite(bz) and math.isfinite(pz[k]) and bz > pz[k] + depth:
            continue
        out.append(float(pd[k]))
    return out


def consequence(build_d, road_d, storage_af, bins=CONSEQ_BINS_M):
    """{'bld_<n>mi' counts per band, 'roads_<n>mi', 'conseq' (a sort key: buildings weigh 3 / 2 /
    1 by band, a road crossing in the first band 1), 'permit_check' (storage over 12.5 ac-ft:
    a North Dakota water permit should exist)}."""
    out = {}
    lo = 0.0
    score = 0.0
    weights = (3.0, 2.0, 1.0)
    for i, hi in enumerate(bins):
        tag = "{:g}mi".format(hi / MILE)
        nb = sum(1 for d in build_d if lo <= d < hi)
        nr = sum(1 for d in road_d if lo <= d < hi)
        out["bld_" + tag] = nb
        out["roads_" + tag] = nr
        score += weights[min(i, len(weights) - 1)] * nb + (1.0 if i == 0 else 0.0) * nr
        lo = hi
    out["conseq"] = score
    out["permit_check"] = bool(storage_af is not None and math.isfinite(storage_af) and storage_af > PERMIT_AF)
    return out


def share_of_classes(grid, mask, classes):
    """Share of the masked cells whose value is one of `classes` (e.g. CDL pasture / grass)."""
    v = np.asarray(grid)[mask]
    v = v[np.isfinite(v.astype(float))]
    if not len(v):
        return float("nan")
    return float(np.isin(v.astype(int), list(classes)).mean())
