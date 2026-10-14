"""Reservoir storage estimation from a DEM plus a water-body mask.

Pure numpy/scipy (no QGIS imports) so it can be unit-tested outside QGIS.

Why two parts: LiDAR DEMs are hydro-flattened, so the DEM says nothing about
the bed below the water surface.  Storage is therefore
  (a) ABOVE normal pool  -> measured directly from the DEM, and
  (b) BELOW normal pool  -> estimated by projecting the shoreline slope
      inward (depth = distance-from-shore * tan(slope)), capped at a
      maximum depth.
All units here are metres / square metres / cubic metres, in an equal-area CRS.
"""
from dataclasses import dataclass, field

import numpy as np

try:  # scipy ships with the QGIS bundles, but degrade gracefully if absent
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None


@dataclass
class StorageResult:
    pool_elev: float
    crest_elev: float
    surface_area_m2: float
    below_pool_m3: float
    above_pool_m3: float          # pool -> crest, from the DEM
    total_m3: float
    max_depth_m: float
    shore_slope_deg: float
    confidence: str
    curve: list = field(default_factory=list)   # [(elev_m, storage_m3), ...]
    notes: list = field(default_factory=list)


def pool_elevation(dem, water_mask):
    """Hydro-flattened water is (near) constant; use the median."""
    vals = dem[water_mask & np.isfinite(dem)]
    if vals.size == 0:
        raise ValueError("water mask does not overlap valid DEM cells")
    return float(np.median(vals))


def shore_slope_deg(dem, water_mask, cell, ring_m=30.0):
    """Mean slope of the ground in a ring just outside the water edge."""
    if ndi is None:
        return None
    dist = ndi.distance_transform_edt(~water_mask) * cell
    ring = (dist > 0) & (dist <= ring_m)
    if ring.sum() < 10:
        return None
    filled = np.where(np.isfinite(dem), dem, np.nanmedian(dem))
    gy, gx = np.gradient(filled, cell)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    return float(np.median(slope[ring]))



LEAK_JUMP = 8.0
LEAK_AREA = 3.0
LEAK_MIN_STEP = 2


def _pool_cells(d, water_mask, stage):
    below = d < stage
    lab, _ = ndi.label(below)
    ids = np.unique(lab[water_mask & below])
    ids = ids[ids > 0]
    return np.isin(lab, ids)


def _touches_edge(mask):
    return bool(mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any())


def above_pool_curve_guarded(dem, water_mask, pool, crest, cell, steps=20, guard=True):
    """(curve, spill elevation or None, note). Stops when the pool runs out over a
    spillway or low saddle (Absaraka 28,701 ac-ft vs DWR 1,616)."""
    if ndi is None:
        raise RuntimeError("scipy is required for the above-pool volume")
    area = cell * cell
    d = np.where(np.isfinite(dem), dem, np.inf)
    d = np.where(water_mask, np.minimum(d, pool), d)

    def at(stage):
        keep = _pool_cells(d, water_mask, stage)
        return float(((stage - d[keep]).sum()) * area), int(keep.sum()), keep

    stages = np.linspace(pool, crest, steps + 1)
    if guard:
        _v0, n0, keep0 = at(pool)
        nw = int(np.asarray(water_mask, bool).sum())
        if nw > 0 and (n0 > LEAK_AREA * nw or (_touches_edge(keep0) and not _touches_edge(water_mask))):
            v_pool = float(((pool - d[water_mask]).clip(min=0)).sum() * area)
            curve = [(float(st), v_pool + nw * area * float(st - pool)) for st in stages]
            note = ("storage leak at the pool level ({:.1f} ft): above-pool volume is the "
                    "pond area times the rise".format(pool / 0.3048))
            return curve, None, note
    curve, cells = [], []
    bad = None
    for k, stage in enumerate(stages):
        vol, n, keep = at(stage)
        if guard and k >= 1:
            edge = _touches_edge(keep) and not _touches_edge(_pool_cells(d, water_mask, stages[k - 1]))
            jump = False
            if k >= LEAK_MIN_STEP and len(curve) >= 2:
                dv_prev = curve[-1][1] - curve[-2][1]
                dv = vol - curve[-1][1]
                jump = dv_prev > 0 and dv > LEAK_JUMP * dv_prev and cells[-1] > 0 and n > LEAK_AREA * cells[-1]
            if edge or jump:
                bad = k
                break
        curve.append((float(stage), vol))
        cells.append(n)
    if bad is None:
        return curve, None, ""
    lo, hi = float(stages[bad - 1]), float(stages[bad])
    n_lo = cells[-1]
    for _i in range(10):
        mid = 0.5 * (lo + hi)
        _v, n, keep = at(mid)
        if _touches_edge(keep) or n > LEAK_AREA * max(n_lo, 1):
            hi = mid
        else:
            lo = mid
    spill = lo
    curve = [(float(st), at(st)[0]) for st in np.linspace(pool, spill, steps + 1)]
    note = ("storage held at the spill level {:.1f} ft (DEM storage leak guard)".format(spill / 0.3048))
    return curve, spill, note


def above_pool_curve(dem, water_mask, pool, crest, cell, steps=20):
    """Storage between pool and crest: sum (stage - dem) over cells that are
    below the stage AND connected to the reservoir."""
    if ndi is None:
        raise RuntimeError("scipy is required for the above-pool volume")
    area = cell * cell
    d = np.where(np.isfinite(dem), dem, np.inf)
    d = np.where(water_mask, np.minimum(d, pool), d)  # water surface = pool
    curve = []
    for stage in np.linspace(pool, crest, steps + 1):
        below = d < stage
        lab, _ = ndi.label(below)                    # 4-connectivity
        ids = np.unique(lab[water_mask & below])
        ids = ids[ids > 0]
        keep = np.isin(lab, ids)
        vol = float(((stage - d[keep]).sum()) * area)
        curve.append((float(stage), vol))
    return curve


def below_pool_volume(dem, water_mask, cell, slope_deg, max_depth, shape_factor=1.0):
    """Bathymetry proxy: depth = distance-to-shore * tan(slope), capped."""
    if ndi is None:
        raise RuntimeError("scipy is required for the below-pool volume")
    dist = ndi.distance_transform_edt(water_mask) * cell
    tan = np.tan(np.radians(max(slope_deg, 0.5)))
    depth = np.minimum(dist * tan, max_depth) * shape_factor
    return float(depth[water_mask].sum() * cell * cell), float(depth.max())


def estimate_storage(dem, water_mask, cell, dam_xy_idx=None, crest_radius_m=50.0,
                     known_height_m=None, max_depth_m=None, steps=20,
                     shape_factor=1.0):
    """Main entry.  dem/water_mask are 2-D arrays on the same grid.

    dam_xy_idx: (row, col) of the dam point, used to find the crest.
    known_height_m: NID/state dam height, used to cap depth and crest.
    """
    notes = []
    # v0.11.1: a -9999 fill value the raster does not declare as no-data is no-data too (no real
    # ground is below -9998 m); it used to give a "valid" pool at -9999 m
    dem = np.asarray(dem, float)
    if np.any(dem <= -9998.0):
        dem = np.where(dem <= -9998.0, np.nan, dem)
    water_mask = water_mask.astype(bool) & np.isfinite(dem)
    pool = pool_elevation(dem, water_mask)
    area_m2 = float(water_mask.sum() * cell * cell)

    # --- crest elevation -------------------------------------------------
    crest = None
    if dam_xy_idx is not None:
        r, c = dam_xy_idx
        n = int(round(crest_radius_m / cell))
        win = dem[max(r - n, 0): r + n + 1, max(c - n, 0): c + n + 1]
        win = win[np.isfinite(win) & (win > pool)]
        if win.size:
            crest = float(np.percentile(win, 98))
            notes.append("crest from DEM near dam point")
    if crest is None or crest - pool < 0.3:
        if known_height_m:
            crest = pool + 0.8 * known_height_m
            notes.append("crest = pool + 0.8*height (DEM crest not found)")
        else:
            crest = pool + 1.0
            notes.append("crest defaulted to pool + 1 m")

    # --- slope / depth ----------------------------------------------------
    slope = shore_slope_deg(dem, water_mask, cell)
    if slope is None:
        slope = 8.0
        notes.append("shore slope defaulted to 8 deg")
    if max_depth_m is None:
        max_depth_m = 0.85 * known_height_m if known_height_m else 4.0
        notes.append("max depth from dam height" if known_height_m
                     else "max depth defaulted to 4 m")

    below, dmax = below_pool_volume(dem, water_mask, cell, slope, max_depth_m,
                                    shape_factor)
    curve_above, spill, leak_note = above_pool_curve_guarded(
        dem, water_mask, pool, crest, cell, steps)
    if leak_note:
        notes.append(leak_note)
        conf_leak = True
    else:
        conf_leak = False
    above = curve_above[-1][1]
    curve = [(e, v + below) for e, v in curve_above]

    conf = "medium"
    if any("defaulted" in s or "not found" in s or "storage leak" in s for s in notes):
        conf = "low"
    elif known_height_m:
        conf = "medium"
    return StorageResult(pool, crest, area_m2, below, above, below + above,
                         dmax, slope, conf, curve, notes)


def calibrate_below_pool(curve, below_est_m3, inv_normal_m3, lo=0.1, hi=10.0):
    """Use inventory NORMAL-pool storage to correct the estimated below-pool volume.

    Like is compared with like: inventory normal storage vs the estimate AT pool.
    The above-pool part of the curve comes straight from the DEM and is kept as is.
    Returns (new_curve, scale, note).  scale is clamped to [lo, hi]; a clamped value
    means the inventory and the terrain disagree badly and is noted for review.
    """
    if not inv_normal_m3 or inv_normal_m3 <= 0 or not below_est_m3 or below_est_m3 <= 0:
        return list(curve), 1.0, ""
    raw = inv_normal_m3 / below_est_m3
    scale = min(max(raw, lo), hi)
    note = "" if scale == raw else f"inventory/estimate storage ratio {raw:.2f} clamped to {scale:.2f}"
    new = [(e, (v - below_est_m3) + below_est_m3 * scale) for e, v in curve]
    return new, float(scale), note


def ensure_top_of_dam(curve, inv_max_m3, crest_m=None):
    """Keep the below-pool calibration. Make storage at the crest at least inv_max_m3.

    If the DEM curve stops at a spill level below the crest, the inventory maximum
    is placed at the crest and the spill point is kept. Returns (curve, note).
    """
    if not curve or not inv_max_m3 or inv_max_m3 <= 1.0:
        return list(curve or []), ""
    elev, vol = curve[-1]
    target = float(inv_max_m3)
    crest = None
    try:
        if crest_m not in (None, ""):
            crest = float(crest_m)
    except (TypeError, ValueError):
        crest = None
    if crest is not None and crest > elev + 0.05:
        top_vol = max(float(vol), target)
        note = ""
        if top_vol > float(vol) + 1.0:
            note = ("assumption: storage at top of dam raised from {:.0f} m3 to inventory "
                    "maximum {:.0f} m3".format(vol, top_vol))
        return list(curve) + [(crest, top_vol)], note
    if float(vol) + 1.0 >= target:
        return list(curve), ""
    out = list(curve[:-1]) + [(elev, target)]
    return out, ("assumption: storage at top of dam raised from {:.0f} m3 to inventory "
                 "maximum {:.0f} m3".format(vol, target))


def storage_choice_volume(inv_norm_m3, inv_max_m3, choice=0):
    """choice: 0 max (default), 1 normal, 2 larger of the two."""
    n = float(inv_norm_m3 or 0)
    m = float(inv_max_m3 or 0)
    if int(choice or 0) == 1:
        return n if n > 1.0 else m, "normal storage"
    if int(choice or 0) == 2:
        return max(n, m), "larger of normal and maximum"
    return m if m > 1.0 else n, "maximum storage"


# Shallow-basin guard (ported from nd_ Screening 0.28.3, nd_dam_screening/algorithms/
# estimate_storage.py SHALLOW_BASIN): a basin whose curve spans less than this fraction of the
# dam height, bed to crest, is not the reservoir of the dam.
SHALLOW_BASIN = 0.5
SHALLOW_BASIN_WHY = ("the basin found is only {:.1f} ft deep against a dam height of {:.1f} ft, "
                     "so it is not the reservoir of this dam")
FT_M = 0.3048


def curve_span_m(res):
    """Depth of a StorageResult's basin, bed to crest: the estimated depth below the pool plus
    the crest above it (m). None when the result lacks the numbers."""
    try:
        span = float(res.crest_elev) - float(res.pool_elev) + max(float(res.max_depth_m or 0.0), 0.0)
    except (TypeError, ValueError, AttributeError):
        return None
    return span if span == span else None


def is_shallow_basin(span_m, height_m, fraction=SHALLOW_BASIN):
    """True when a basin `span_m` deep is far shallower than a dam `height_m` high."""
    try:
        h = float(height_m)
    except (TypeError, ValueError):
        return False
    return span_m is not None and h > 0.5 and span_m < fraction * h
