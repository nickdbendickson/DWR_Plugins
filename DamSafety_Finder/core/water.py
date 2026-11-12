"""Open water in aerial imagery: the pond outlines no hydrography layer has (Finder 0.1).

Pure numpy / scipy. The water index is NDWI (McFeeters 1996) from the green and near-infrared
bands of 4-band or colour-infrared imagery (NAIP, the ND statewide orthos):

    NDWI = (green - NIR) / (green + NIR)

Open water absorbs near-infrared and comes out positive; crops, grass and bare soil negative;
stubble and wet soil near 0. A cell is water when NDWI >= `threshold` (YOUR value; 0.15 is a
starting value for NAIP in late summer - check it on a known pond) and it is dark in the
near-infrared (at most `nir_frac` of the land's median: metal roofs, gravel, rail cars and
shadows can have a positive NDWI but are not that dark). The mask is opened by
`open_px` cells (drops field edges, roads and roof shadows narrower than about 2 x open_px + 1
cells) and closed again, then every connected patch of at least `min_px` cells that is somewhere
at least `min_width_px` wide is one water body (a ditch along a road or railroad is long but
narrow).

Imagery shows what was wet on the day it was flown: a dry year misses ponds, a wet year adds
flooded fields and potholes. Emergent vegetation (cattails) hides part of a marshy pool. The
outlines are for the pond tool (core/ponds.py), which then asks the LiDAR whether the water is
held above the ground.
"""
import numpy as np

try:  # scipy ships with the QGIS bundles
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None


def ndwi(green, nir):
    """NDWI of two bands (any numeric type); NaN where both are 0 or either is NaN."""
    g = np.asarray(green, float)
    n = np.asarray(nir, float)
    s = g + n
    with np.errstate(divide="ignore", invalid="ignore"):
        out = (g - n) / s
    out[~np.isfinite(out) | (s == 0)] = np.nan
    return out


def dark_in_nir(nir, index, frac=0.25):
    """Cells whose near-infrared is at most `frac` x the median near-infrared of the land (NDWI
    below 0) of the grid. Open water is dark in NIR (NAIP: about 30 of 255 on a pond against
    about 200 on crops); a metal roof, a gravel road, rail cars or a shadow can have a positive
    NDWI but are not that dark. All True when there is no land to compare with."""
    n = np.asarray(nir, float)
    land = np.isfinite(n) & np.isfinite(index) & (index < 0)
    if land.sum() < 100:
        return np.ones(n.shape, bool)
    return np.isfinite(n) & (n <= frac * float(np.median(n[land])))


def water_patches(index, threshold=0.15, open_px=1, min_px=50, min_width_px=0.0, dark=None):
    """(labels, n): 4-connected water patches of an NDWI grid (NaN = no data, never water),
    opened and closed by `open_px` cells, patches under `min_px` cells removed, and patches
    nowhere wider than `min_width_px` cells (twice the largest distance to their edge: a road or
    railroad ditch, a shadow strip along a shelterbelt) removed; labels 1..n. `dark` (bool grid,
    dark_in_nir) must also hold for a cell to be water."""
    if ndi is None:
        raise RuntimeError("scipy is required for the water outlines")
    idx = np.asarray(index, float)
    wet = np.isfinite(idx) & (idx >= threshold)
    if dark is not None:
        wet &= dark
    if open_px > 0:
        st = ndi.generate_binary_structure(2, 1)
        wet = ndi.binary_opening(wet, structure=st, iterations=open_px)
        wet = ndi.binary_closing(wet, structure=st, iterations=open_px) & np.isfinite(idx)
    lab, n = ndi.label(wet)
    if n == 0:
        return lab, 0
    sizes = np.bincount(lab.ravel())
    keep = sizes >= max(1, int(min_px))
    keep[0] = False
    if min_width_px > 0:
        dist = ndi.distance_transform_edt(lab > 0)
        widest = np.zeros(n + 1)
        np.maximum.at(widest, lab.ravel(), dist.ravel())
        keep &= 2.0 * widest >= min_width_px
    remap = np.zeros(n + 1, dtype=np.int32)
    remap[keep] = np.arange(1, int(keep.sum()) + 1, dtype=np.int32)
    return remap[lab], int(keep.sum())


def persistence(reads, threshold=0.15, nir_frac=0.25):
    """(mean NDWI, wet share, geotransform) of several images of one grid: reads = [({band:
    array}, gt, green band, NIR band)]. The wet share of a cell is the share of the images with
    data there in which it is water (NDWI >= threshold and dark in NIR); NaN where none has data."""
    wet = cnt = idx_sum = None
    gt = None
    for bands, gtk, gb, nb in reads:
        gt = gtk
        idx = ndwi(bands[gb], bands[nb])
        ok = np.isfinite(idx)
        w = ok & (idx >= threshold)
        if nir_frac:
            w &= dark_in_nir(bands[nb], idx, nir_frac)
        if wet is None:
            wet = np.zeros(idx.shape)
            cnt = np.zeros(idx.shape)
            idx_sum = np.zeros(idx.shape)
        wet += w
        cnt += ok
        idx_sum += np.where(ok, idx, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        share = np.where(cnt > 0, wet / np.maximum(cnt, 1), np.nan)
        mean = np.where(cnt > 0, idx_sum / np.maximum(cnt, 1), np.nan)
    return mean, share, gt


def patch_stats(labels, n, index, mapped=None):
    """Per patch 1..n: (cells, mean NDWI, share of its cells on a mapped water body) as arrays
    indexed by label (index 0 unused). `mapped` is a bool grid of the mapped water bodies."""
    idx = np.where(np.isfinite(index), index, 0.0)
    cells = np.bincount(labels.ravel(), minlength=n + 1).astype(float)
    sums = np.bincount(labels.ravel(), weights=idx.ravel(), minlength=n + 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        mean = np.where(cells > 0, sums / np.maximum(cells, 1), np.nan)
    if mapped is None:
        share = np.zeros(n + 1)
    else:
        on = np.bincount(labels.ravel(), weights=np.asarray(mapped, float).ravel(), minlength=n + 1)
        share = np.where(cells > 0, on / np.maximum(cells, 1), 0.0)
    return cells, mean, share
