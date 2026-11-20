"""BoannGrid delta / artifact audit layer: what the depression-fill changed, and a first-pass
triage of each filled depression.

The fill raises ground; this layer records every change so the conditioning is auditable, and
censuses each filled depression (area, volume, depth, the level it filled to) so a real pit is
not silently treated like a DEM glitch. It is pure Python on top of core/metalterrain.fill - no
GPU kernel - and uses no scipy (a numpy label-propagation fallback) so it runs in the cloud.

    from nd_breach_runner.core.metalterrain import audit
    delta = audit.fill_delta(dem, filled)                 # how much the fill raised each cell
    labels, items = audit.depression_census(dem, filled, cell_m=10.0)

Triage classes (first-pass, geometry only - the full A/B/C/D decision engine builds on this):
    "noise"     tiny and shallow: DEM roughness, fill silently
    "fill"      moderate, shallow, low volume: a fill artifact
    "pit"       deep or large: a real closed depression, keep and flag
    "crossing"  long and thin: likely a channel blocked by a road/rail fill -> reconnection (#3)
"""
import numpy as np

from . import fill as F

NODATA = F.NODATA
MIN_DELTA_M = 1e-3            # a change smaller than this is not a filled cell (float32 noise)


def fill_delta(dem, filled):
    """How much the fill raised each cell: filled - dem, clamped at 0, NaN where NoData."""
    a = np.asarray(dem, dtype=np.float64)
    f = np.asarray(filled, dtype=np.float64)
    valid = np.isfinite(a) & (a != NODATA)
    d = np.where(valid, np.maximum(f - a, 0.0), np.nan)
    return d


def _label4(mask):
    """4-connected connected components of a boolean grid. Uses scipy when it imports (production
    QGIS python), else a numpy label-propagation fallback (cloud, where scipy is often broken).
    Returns (labels int32 with 0 = background, n)."""
    mask = np.asarray(mask, dtype=bool)
    try:
        from scipy.ndimage import label
        lab, n = label(mask, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool))
        return lab.astype(np.int32), int(n)
    except Exception:                               # noqa: BLE001 - scipy absent/broken
        pass
    ny, nx = mask.shape
    lab = np.where(mask, np.arange(ny * nx, dtype=np.int64).reshape(ny, nx) + 1, 0)
    while True:
        cur = lab.copy()
        for sh, ax in ((1, 0), (-1, 0), (1, 1), (-1, 1)):
            nb = np.roll(lab, sh, axis=ax)
            if ax == 0:
                (nb[0, :] if sh == 1 else nb[-1, :]).fill(0)
            else:
                (nb[:, 0] if sh == 1 else nb[:, -1]).fill(0)
            take = mask & (nb > 0) & ((cur == 0) | (nb < cur))
            cur = np.where(take, nb, cur)
        if np.array_equal(cur, lab):
            break
        lab = cur
    ids = np.unique(lab[mask])
    remap = np.zeros(int(lab.max()) + 1, dtype=np.int32)
    remap[ids] = np.arange(1, ids.size + 1, dtype=np.int32)
    return remap[lab], int(ids.size)


def classify(area_cells, max_depth_m, aspect, cell_m,
             noise_cells=4, noise_depth=0.3, fill_depth=1.0, fill_vol_m3=None,
             pit_depth=2.0, pit_area_m2=2000.0, crossing_aspect=4.0):
    """First-pass triage of one depression from its geometry. `aspect` is the bounding-box long/
    short ratio. A long, thin, not-shallow depression reads as a channel blocked by a linear fill
    (a road/rail crossing); a deep or large one as a real pit; small shallow ones as artifacts."""
    area_m2 = area_cells * cell_m * cell_m
    if aspect >= crossing_aspect and max_depth_m >= noise_depth:
        return "crossing"
    if max_depth_m >= pit_depth or area_m2 >= pit_area_m2:
        return "pit"
    if area_cells <= noise_cells and max_depth_m < noise_depth:
        return "noise"
    return "fill"


def depression_census(dem, filled=None, cell_m=1.0, eps=1e-5, backend="auto"):
    """Census every filled depression on `dem` (NaN / -9999 = NoData). Returns (labels, items).
    `labels` is int32 (0 = not filled). Each item: id, n_cells, area_m2, volume_m3, max_depth_m,
    floor_elev_m (lowest original ground), spill_elev_m (the level it filled to), centroid (row,
    col) and `klass` (classify()). Pass `filled` to skip re-filling."""
    a = np.asarray(dem, dtype=np.float64)
    if filled is None:
        filled, _ = F.fill_dem(a, eps=eps, backend=backend)
    else:
        filled = np.asarray(filled, dtype=np.float64)
    delta = fill_delta(a, filled)
    mask = np.isfinite(delta) & (delta > MIN_DELTA_M)
    labels, n = _label4(mask)
    cell_area = cell_m * cell_m
    items = []
    for lid in range(1, n + 1):
        m = labels == lid
        rr, cc = np.nonzero(m)
        d = delta[m]
        items.append({
            "id": lid,
            "n_cells": int(m.sum()),
            "area_m2": float(m.sum() * cell_area),
            "volume_m3": float(d.sum() * cell_area),
            "max_depth_m": float(d.max()),
            "floor_elev_m": float(a[m].min()),
            "spill_elev_m": float(filled[m].max()),
            "centroid": (float(rr.mean()), float(cc.mean())),
        })
        h = rr.max() - rr.min() + 1
        w = cc.max() - cc.min() + 1
        aspect = max(h, w) / max(1, min(h, w))
        items[-1]["klass"] = classify(items[-1]["n_cells"], items[-1]["max_depth_m"], aspect,
                                      cell_m)
    return labels, items
