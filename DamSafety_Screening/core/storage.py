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
    if crest is not None and known_height_m and crest - pool > known_height_m + 0.3:
        # restored from the ND copy (0.19.0): the top of dam cannot be more than the dam height
        # above the pool; a higher DEM value near the point is a road, rail or hill
        notes.append("DEM crest {:.1f} ft above the pool, more than the dam height: crest = pool + "
                     "height".format((crest - pool) / 0.3048))
        crest = pool + known_height_m
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
    if spill is not None and known_height_m and crest > spill + 0.01 and len(curve_above) >= 2:
        # restored from the ND copy (0.19.0, nd_dam_screening/core/storage.py): a dam of known
        # height holds water to its crest; above the spill level the fill has left the reservoir,
        # so the curve goes on from the spill to the crest with the reservoir area at the spill
        # level. Without this the stage-storage curve truncates at the spill, understating storage.
        (e1, v1), (e0, v0) = curve_above[-1], curve_above[-2]
        a_spill = (v1 - v0) / (e1 - e0) if e1 > e0 else 0.0
        ext = np.linspace(spill, crest, steps + 1)[1:]
        curve_above = list(curve_above) + [(float(e), v1 + a_spill * float(e - spill)) for e in ext]
        leak_note = leak_note.replace("storage held at the spill level",
                                      "storage leak at the spill level") + \
            ("; above it the curve goes on to the crest {:.1f} ft with the reservoir area at the "
             "spill level ({:,.1f} ac)".format(crest / 0.3048, a_spill / 4046.8564224))
    elif spill is not None:                  # water runs out above this level; crest held at spill
        crest = min(crest, spill)
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


# 0.17.27: tool 3's STORAGE_CHOICE (the window's "Storage" choice; same labels and values)
STORAGE_CHOICES = ("Max storage", "Normal storage", "Larger of the two")
STORAGE_MAX, STORAGE_NORMAL, STORAGE_LARGER = 0, 1, 2


def resolve_storage_choice(inv_norm_m3, inv_max_m3, choice=STORAGE_MAX):
    """The inventory storage tool 3 uses for `choice` (0 Max, the default; 1 Normal; 2 Larger
    of the two). A value counts as given when it is over 1 m3 (missing, empty, zero or
    unreadable is not given).

    Returns dict(volume_m3 (None when neither value is given: the DEM estimate is kept),
    kind ("maximum" / "normal" / "none": which inventory value it is), choice (the int used),
    label (the choice's label), fallback ("" or the sentence saying what replaced a missing
    value), text (one line for the notes and the log)).

      Max:     maximum; missing -> normal (fallback); both missing -> none.
      Normal:  normal; missing -> maximum (fallback); both missing -> none.
      Larger:  the larger of the two; one missing -> the other (fallback); both -> none.
    An unknown choice is treated as Max and says so."""
    def given(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v and v > 1.0 else None
    n, m = given(inv_norm_m3), given(inv_max_m3)
    try:
        c = int(choice or 0)
    except (TypeError, ValueError):
        c = -1
    fallback = ""
    if c not in (STORAGE_MAX, STORAGE_NORMAL, STORAGE_LARGER):
        fallback = "unknown storage choice {!r}, used Max storage; ".format(choice)
        c = STORAGE_MAX
    if c == STORAGE_NORMAL:
        order = (("normal", n), ("maximum", m))
    elif c == STORAGE_LARGER and n is not None and m is not None:
        order = (("normal", n), ("maximum", m)) if n > m else (("maximum", m), ("normal", n))
    else:
        order = (("maximum", m), ("normal", n))
    vol, kind = None, "none"
    for k, v in order:
        if v is not None:
            vol, kind = v, k
            break
    wanted = {STORAGE_MAX: "maximum", STORAGE_NORMAL: "normal"}.get(c)
    if vol is None:
        fallback += "no inventory normal or maximum storage, so the DEM estimate is kept"
    elif c == STORAGE_LARGER and (n is None or m is None):
        fallback += "{} storage missing or zero, so the larger of the two is the {} storage".format(
            "normal" if n is None else "maximum", kind)
    elif wanted and kind != wanted:
        fallback += "{} storage missing or zero, so {} storage was used".format(wanted, kind)
    fallback = fallback.rstrip("; ")
    text = "storage choice {}: {}".format(
        STORAGE_CHOICES[c], "{} storage {:.0f} m3".format(kind, vol) if vol is not None
        else "no inventory storage")
    if fallback:
        text += " ({})".format(fallback)
    return {"volume_m3": vol, "kind": kind, "choice": c, "label": STORAGE_CHOICES[c],
            "fallback": fallback, "text": text}


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


# 0.17.47: each inventory storage at its own elevation (DWR-2865 / DWR-3034).
# Tool 3 used to calibrate the curve so that the storage AT THE LIDAR WATER SURFACE (the normal
# pool) is the inventory "normal" storage and then add the DEM fill above it. When the record's
# normal storage is really its maximum (NickReview's "maxstor" field map put DWR max_pool_vol in
# the normal-storage box), the maximum (167 ac-ft, DWR at 1874 ft) sat at the 1870 ft pool and
# the fill to the 1875.9 ft spill came on top: 237.6 ac-ft. Now the maximum storage is placed at
# the max-pool elevation, the normal storage at the normal pool, and only the DEM fill ABOVE the
# max-pool elevation is added on top of the maximum.
ANCHOR_SAME = 0.005          # normal and maximum within 0.5 % of each other are "the same value"


def _interp_curve(curve, elev):
    """Storage of `curve` [(elev, storage)] at `elev` (linear; held flat outside the curve)."""
    es = [float(e) for e, _v in curve]
    vs = [float(v) for _e, v in curve]
    return float(np.interp(float(elev), es, vs))


def max_pool_on_dem(pool_m, norm_elev_m=None, max_elev_m=None):
    """(elevation on the DEM of the inventory max pool, how it was found, flag or "").

    The lidar water surface is the normal pool (the DEM is hydro-flattened at it), so the max
    pool is (inventory max-pool elevation - inventory normal-pool elevation) above the DEM pool.
    Only the DIFFERENCE of the two inventory elevations is used: DWR records are in NAVD88 /
    NGVD29 feet or in a local datum (DWR-3034: normal 125.7, max 130.7, streambed 100), so their
    absolute values cannot be compared with the DEM. (None, ..., flag) when either elevation is
    missing or the max pool is below the normal pool."""
    def num(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v else None
    n, m = num(norm_elev_m), num(max_elev_m)
    if n is None or m is None:
        miss = " and ".join(w for w, v in (("normal-pool", n), ("max-pool", m)) if v is None)
        return None, "", "MAXPOOL-ELEV-MISSING: no inventory {} elevation".format(miss)
    if m < n - 0.01:
        return None, "", ("MAXPOOL-ELEV-BAD: inventory max-pool elevation {:.1f} ft is below the "
                          "normal pool {:.1f} ft".format(m / FT_M, n / FT_M))
    rise = max(m - n, 0.0)
    return (float(pool_m) + rise,
            "max pool {:.1f} ft above the normal pool (inventory {:.1f} -> {:.1f} ft)".format(
                rise / FT_M, n / FT_M, m / FT_M), "")


def anchor_inventory_storage(curve, below_est_m3, pool_m, inv_norm_m3, inv_max_m3, kind,
                             norm_elev_m=None, max_elev_m=None, crest_m=None):
    """The stage-storage curve with each inventory storage at its own elevation.

    curve        the DEM curve from estimate_storage, [(elev_m, storage_m3)] from the pool up,
                 storage = estimated below-pool volume + DEM fill above the pool
    below_est_m3 its estimated below-pool part (StorageResult.below_pool_m3)
    pool_m       the DEM pool (lidar water surface) = the inventory normal pool
    kind         resolve_storage_choice()["kind"]: "maximum", "normal" or "none"
    crest_m      the top of dam when it is above the end of the curve (as ensure_top_of_dam);
                 else the top of dam is the end of the curve (tool 3's est_crest_m)

    kind "normal" / "none": the normal storage (when given) at the pool and the DEM fill above
    it (calibrate_below_pool; the behaviour before 0.17.47).

    kind "maximum", with the max-pool elevation known (max_pool_on_dem):
      * storage at the max pool = inventory maximum;
      * storage at the pool = inventory normal storage when it is given and below the maximum;
        between the two the DEM fill is scaled to join them;
      * without a usable normal storage (missing, or not below the maximum - the 2865 pattern,
        where the "normal" box held the maximum) the storage at the pool is the maximum minus
        the DEM fill from the pool to the max pool;
      * above the max pool only the DEM fill above the max-pool elevation is added;
      * a max pool above the top of dam is placed AT the top of dam, flagged MAXPOOL-ABOVE-CREST:
        the storage at the top of dam is never below the maximum (as ensure_top_of_dam gave
        before 0.17.47) and no point above it carries the maximum; on a curve that reaches the
        top of dam the fill from the pool is scaled so the curve reaches the maximum there;
      * a max pool above the end of a curve that stops short of a given crest_m is added after
        the last DEM point, storage = maximum (CURVE-ABOVE-MAX when the curve is already above);
      * the DEM fill from the pool to the max pool larger than the maximum (FILL-EXCEEDS-MAX):
        the DEM curve is kept, its below-pool estimate at the pool, as before 0.17.47 without a
        normal storage (never 0 at the pool, which tool 5's sunny-day scenario cannot use).
    kind "maximum" without a usable max-pool elevation (NID-only records, a DWR record without
    normal_elev / max_pool_elev; a max pool below the normal pool, or at it with a maximum above
    the normal storage: MAXPOOL-ELEV-BAD): FALLBACK, flagged - the maximum is placed at the top
    of dam as before 0.17.47 (ensure_top_of_dam: the top is raised to the maximum, never
    lowered). Only a normal storage equal to the maximum (NORMAL-EQUALS-MAX) is then not used
    below the pool; one above the maximum stays at the pool as before, flagged
    NORMAL-NOT-BELOW-MAX.

    Returns (curve, scale, info) with info = {"anchor": short text for stor_anchor,
    "anchor_m": the elevation the used storage is anchored at, "flags": [...], "notes": [...]}."""
    flags, notes = [], []
    curve = [(float(e), float(v)) for e, v in (curve or [])]
    if not curve:
        return [], 1.0, {"anchor": "none", "anchor_m": None, "flags": flags, "notes": notes}

    def given(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if v == v and v > 1.0 else None
    n, m = given(inv_norm_m3), given(inv_max_m3)
    same = n is not None and m is not None and abs(n - m) <= ANCHOR_SAME * max(n, m)
    if same:
        flags.append("NORMAL-EQUALS-MAX: inventory normal storage {:.0f} m3 = maximum storage {:.0f} m3 "
                     "(both probably read from the max-storage field)".format(n, m))
    if kind != "maximum" or m is None:
        new, scale, cnote = calibrate_below_pool(curve, below_est_m3, n)
        if cnote:
            notes.append(cnote)
        anchor = "normal storage at the normal pool" if n is not None else "DEM estimate"
        return new, scale, {"anchor": anchor, "anchor_m": float(pool_m) if n is not None else None,
                            "flags": flags, "notes": notes}

    pool = float(pool_m)
    fill = [(e, v - float(below_est_m3 or 0.0)) for e, v in curve]   # DEM fill above the pool
    e_max, how, eflag = max_pool_on_dem(pool, norm_elev_m, max_elev_m)
    if (e_max is not None and e_max - pool <= 0.01 * FT_M and n is not None and not same
            and n < m - 1.0):
        # the max pool at the normal pool with a maximum above the normal storage: the maximum
        # would sit at the pool with the whole DEM fill added on top (the double count again)
        e_max, eflag = None, ("MAXPOOL-ELEV-BAD: inventory max-pool elevation {:.1f} ft is the normal "
                              "pool {:.1f} ft, but the maximum storage {:.0f} m3 is more than the normal "
                              "storage {:.0f} m3".format(float(max_elev_m) / FT_M,
                                                         float(norm_elev_m) / FT_M, m, n))
    if e_max is None:
        flags.append(eflag + "; FALLBACK: maximum storage placed at the top of dam")
        # only the 2865 field-map pattern (normal = maximum) drops the normal storage; one above
        # the maximum keeps the placement of before 0.17.47 (at the pool), flagged
        use_n = None if same else n
        if n is not None and not same and n > m:
            flags.append("NORMAL-NOT-BELOW-MAX: normal storage {:.0f} m3 is above the maximum {:.0f} m3: "
                         "kept at the normal pool (fallback, as before 0.17.47)".format(n, m))
        new, scale, cnote = calibrate_below_pool(curve, below_est_m3, use_n)
        if cnote:
            notes.append(cnote)
        if same:
            notes.append("normal storage not used below the pool (equal to the maximum)")
        new, tnote = ensure_top_of_dam(new, m, crest_m)
        if tnote:
            notes.append(tnote)
        return new, scale, {"anchor": "maximum storage at the top of dam (fallback)",
                            "anchor_m": float(new[-1][0]), "flags": flags, "notes": notes}

    notes.append(how)
    end_e = fill[-1][0]
    # the top of dam: the end of the DEM curve (tool 3's est_crest_m), or a given crest above it
    top_e = end_e
    try:
        if crest_m not in (None, "") and float(crest_m) > end_e + 0.05:
            top_e = float(crest_m)
    except (TypeError, ValueError):
        pass
    # a max pool above the top of dam: the maximum is placed AT the top of dam, never above it
    # (the storage there is at least the maximum, as ensure_top_of_dam gave before 0.17.47), and
    # the fill below it is scaled to reach it
    at_top = e_max > top_e + 0.01
    if at_top:
        flags.append("MAXPOOL-ABOVE-CREST: the inventory max pool is {:.1f} ft above the normal pool "
                     "but the crest only {:.1f} ft: maximum storage placed at the crest".format(
                         (e_max - pool) / FT_M, (top_e - pool) / FT_M))
    e_max = min(e_max, top_e)
    beyond = e_max > end_e + 0.01
    f_max = _interp_curve(fill, min(e_max, end_e))
    use_n = n if (n is not None and n < m - 1.0) else None
    if n is not None and use_n is None:
        flags.append("NORMAL-NOT-BELOW-MAX: normal storage {:.0f} m3 is not below the maximum {:.0f} m3: "
                     "not used at the normal pool".format(n, m))
    if use_n is not None:
        s_pool = use_n
    else:
        s_pool = m - f_max
        if s_pool < 0.0:
            # the maximum cannot sit at the max pool with any storage below the pool: the DEM
            # curve is kept (a storage of 0 at the pool failed tool 5's sunny-day scenario)
            flags.append("FILL-EXCEEDS-MAX: the DEM fill from the pool to the max pool ({:.0f} m3) is more "
                         "than the maximum storage {:.0f} m3: DEM curve kept, below-pool estimate {:.0f} m3 "
                         "at the normal pool".format(f_max, m, float(below_est_m3 or 0.0)))
            new, tnote = ensure_top_of_dam(curve, m, crest_m)
            if tnote:
                notes.append(tnote)
            return new, 1.0, {"anchor": "DEM estimate (maximum below the DEM fill)", "anchor_m": None,
                              "flags": flags, "notes": notes}
    out = []
    for e, f in fill:
        if e <= e_max + 1e-9:
            if beyond:                 # the whole curve is below the max pool
                v = s_pool + f
            elif f_max > 0.0:
                v = s_pool + (m - s_pool) * (f / f_max)
            else:
                v = s_pool + (m - s_pool) * ((e - pool) / (e_max - pool) if e_max > pool else 1.0)
        else:
            v = m + (f - f_max)
        out.append((e, float(v)))
    if beyond:
        top = out[-1][1]
        if top > m + 1.0:
            flags.append("CURVE-ABOVE-MAX: the curve reaches {:.0f} m3 below the max pool, more than the "
                         "maximum {:.0f} m3".format(top, m))
        out.append((float(e_max), max(m, top)))
        notes.append("max pool {:.1f} ft is above the end of the DEM curve {:.1f} ft: maximum storage "
                     "added as the last point".format(e_max / FT_M, end_e / FT_M))
    if e_max < end_e - 0.01 and not any(abs(e - e_max) < 1e-6 for e, _v in out):
        out.append((float(e_max), float(m)))
        out.sort(key=lambda p: p[0])
    # the curve must not fall with the stage
    for i in range(1, len(out)):
        if out[i][1] < out[i - 1][1]:
            out[i] = (out[i][0], out[i - 1][1])
    scale = (s_pool / float(below_est_m3)) if below_est_m3 else 1.0
    where = "top of dam" if at_top else "max pool"
    notes.append("maximum storage {:.0f} m3 at the {} {:.1f} ft; {} at the normal pool "
                 "{:.1f} ft".format(m, where, e_max / FT_M,
                                    "normal storage {:.0f} m3".format(use_n) if use_n is not None
                                    else "{:.0f} m3 (maximum minus the DEM fill)".format(s_pool),
                                    pool / FT_M))
    return out, float(scale), {"anchor": "maximum storage at the " + where + (
        " (max pool above the crest)" if at_top else ""), "anchor_m": float(e_max),
        "flags": flags, "notes": notes}
