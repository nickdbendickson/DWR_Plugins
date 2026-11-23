# -*- coding: utf-8 -*-
"""Hazard 2.26.0: flood depth on the UNCONDITIONED ground, when the model ran on a conditioned DEM.

A terrain conditioner (the Runner's BoannGrid reconnect, a channel burn, a hand edit) lowers
ground cells so water can pass a road fill. The flood model then reports depth = water surface -
conditioned ground, so in a carved notch the "depth" is the notch's own depth, not water on the
real ground. Absaraka (DWR-2801, 30 m, November 2026): notches up to 19.5 m deep next to Casselton
houses gave them 8 ft median "depth" while the peak water surface was 7.8 ft BELOW the real grade;
107 houses went high-danger and TM-11 went High, all from the conditioning.

This module puts the depth back on the real ground. With the DEM the model ran on (`run_dem`) and
the ground before conditioning (`raw_dem`), lowering = raw - run where positive, and on every
lowered cell

    depth_raw = depth - lowering        (the same water surface, measured from the real ground)

dried (0) where that is not above `wet_min`; velocity is set to 0 on a cell that becomes dry, so
"peak velocity over the wet cells" (Nick's option C) reads only cells that are wet on the real
ground. Cells that were not lowered are copied unchanged, so a run on an unconditioned DEM is
untouched. Raised cells (a levee raise) are left as modelled: the water there really stood on the
raised crest.

    from DamSafety_Hazard import rawground as RG
    stats = RG.write_raw_ground(depth_tif, run_dem_tif, raw_dem_tif, out_folder, vel_path=vel_tif)
    # -> classify on stats["depth"] / stats["vel"]

The model gate (modelgate.py, check "terrain") WARNs when wet cells sit on lowered ground and
points here. numpy for the arithmetic; GDAL (osgeo) only for the file helpers. No QGIS.
"""
import os

import numpy as np

FT_PER_M = 1.0 / 0.3048
DEPTH_NAME = "Depth (Max) raw ground.tif"
VEL_NAME = "Velocity (Max) raw ground.tif"
DEFAULT_MIN_LOWERING_FT = 0.5     # lowering above this at a wet cell / receptor -> warn


def ground_lowering(run_dem, raw_dem):
    """raw - run where both are finite and the run ground is lower; 0 elsewhere (same units)."""
    run = np.asarray(run_dem, dtype=np.float64)
    raw = np.asarray(raw_dem, dtype=np.float64)
    if run.shape != raw.shape:
        raise ValueError("run and raw DEMs differ in shape: {} vs {}".format(run.shape, raw.shape))
    low = np.where(np.isfinite(run) & np.isfinite(raw), raw - run, 0.0)
    return np.maximum(low, 0.0)


def depth_on_raw_ground(depth, lowering, vel=None, dem_to_depth=FT_PER_M, wet_min=0.0):
    """(depth_raw, vel_raw, stats) for a depth grid and the ground lowering on the same grid.
    `dem_to_depth` converts the DEM's vertical unit to the depth unit (default metres -> feet).
    Unlowered cells are returned unchanged (NaN stays NaN)."""
    d = np.asarray(depth, dtype=np.float64)
    low = np.asarray(lowering, dtype=np.float64) * float(dem_to_depth)
    if d.shape != low.shape:
        raise ValueError("depth and lowering differ in shape: {} vs {}".format(d.shape, low.shape))
    hit = (low > 0) & np.isfinite(d)
    wet0 = hit & (d > wet_min)
    dr = d.copy()
    dr[hit] = d[hit] - low[hit]
    dried = hit & ~(dr > wet_min)
    dr[dried] = 0.0
    vr = None
    if vel is not None:
        vr = np.array(vel, dtype=np.float64, copy=True)
        if vr.shape != d.shape:
            raise ValueError("velocity and depth differ in shape")
        vr[dried & np.isfinite(vr)] = 0.0
    stats = {"cells_lowered": int((low > 0).sum()),
             "wet_cells_on_lowered_ground": int(wet0.sum()),
             "cells_dried": int((dried & wet0).sum()),
             "max_lowering": float(low.max()) if low.size else 0.0,
             "max_lowering_wet": float(low[wet0].max()) if wet0.any() else 0.0,
             "max_depth_before": float(d[wet0].max()) if wet0.any() else 0.0,
             "max_depth_after": float(dr[wet0].max()) if wet0.any() else 0.0}
    return dr, vr, stats


def receptor_report(rows_cols, lowering, depth=None, dem_to_depth=FT_PER_M, radius=1,
                    min_lowering=DEFAULT_MIN_LOWERING_FT):
    """For receptors at grid (row, col): the ones with ground lowered by more than `min_lowering`
    (depth units) within `radius` cells. Returns [{"index", "row", "col", "max_lowering",
    "depth", "depth_raw"}] - the receptors whose depth would be read off a carve."""
    low = np.asarray(lowering, dtype=np.float64) * float(dem_to_depth)
    ny, nx = low.shape
    d = None if depth is None else np.asarray(depth, dtype=np.float64)
    out = []
    for k, (r, c) in enumerate(rows_cols):
        r, c = int(r), int(c)
        r0, r1 = max(0, r - radius), min(ny, r + radius + 1)
        c0, c1 = max(0, c - radius), min(nx, c + radius + 1)
        if r0 >= r1 or c0 >= c1:
            continue
        win = low[r0:r1, c0:c1]
        m = float(win.max())
        if m <= min_lowering:
            continue
        rec = {"index": k, "row": r, "col": c, "max_lowering": m}
        if d is not None:
            dw = d[r0:r1, c0:c1]
            fin = np.isfinite(dw)
            rec["depth"] = float(dw[fin].max()) if fin.any() else float("nan")
            dr = np.where(fin, dw - win, np.nan)
            rec["depth_raw"] = float(max(0.0, np.nanmax(dr))) if fin.any() else float("nan")
        out.append(rec)
    return out


# ------------------------------------------------------------------------- GDAL file helpers
def _read(path):
    from osgeo import gdal
    ds = gdal.Open(str(path))
    if ds is None:
        raise IOError("cannot open {}".format(path))
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype(np.float64)
    nod = b.GetNoDataValue()
    if nod is not None:
        a[a == nod] = np.nan
    return ds, a


def _on_grid(path, like_ds):
    """`path` read on `like_ds`'s grid (nearest neighbour; same grid -> read as is)."""
    from osgeo import gdal
    ds, a = _read(path)
    if (ds.RasterXSize, ds.RasterYSize) == (like_ds.RasterXSize, like_ds.RasterYSize) and \
            np.allclose(ds.GetGeoTransform(), like_ds.GetGeoTransform()):
        return a
    gt = like_ds.GetGeoTransform()
    nx, ny = like_ds.RasterXSize, like_ds.RasterYSize
    bounds = (gt[0], gt[3] + ny * gt[5], gt[0] + nx * gt[1], gt[3])
    out = gdal.Warp("", ds, format="MEM", outputBounds=bounds, width=nx, height=ny,
                    dstSRS=like_ds.GetProjection() or None, resampleAlg="near",
                    dstNodata=-9999.0)
    b = out.GetRasterBand(1)
    a = b.ReadAsArray().astype(np.float64)
    a[a == -9999.0] = np.nan
    return a


def lowering_on_depth_grid(depth_path, run_dem_path, raw_dem_path):
    """(depth array, lowering array in DEM units) on the depth raster's grid."""
    dds, depth = _read(depth_path)
    low = ground_lowering(_on_grid(run_dem_path, dds), _on_grid(raw_dem_path, dds))
    return dds, depth, low


def _write(path, arr, like_ds):
    from osgeo import gdal
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), like_ds.RasterXSize, like_ds.RasterYSize, 1, gdal.GDT_Float32,
                    options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(like_ds.GetGeoTransform())
    ds.SetProjection(like_ds.GetProjection())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(np.where(np.isfinite(arr), arr, -9999.0).astype(np.float32))
    ds.FlushCache()
    ds = None
    return str(path)


def write_raw_ground(depth_path, run_dem_path, raw_dem_path, out_folder, vel_path=None,
                     dem_to_depth=FT_PER_M, wet_min=0.0):
    """Write DEPTH_NAME (and VEL_NAME with `vel_path`) in `out_folder`: the model's depth and
    velocity on the unconditioned ground. Returns stats plus "depth" / "vel" (the paths)."""
    dds, depth, low = lowering_on_depth_grid(depth_path, run_dem_path, raw_dem_path)
    vel = _on_grid(vel_path, dds) if vel_path else None
    dr, vr, stats = depth_on_raw_ground(depth, low, vel, dem_to_depth=dem_to_depth,
                                        wet_min=wet_min)
    os.makedirs(out_folder, exist_ok=True)
    stats["depth"] = _write(os.path.join(out_folder, DEPTH_NAME), dr, dds)
    stats["vel"] = _write(os.path.join(out_folder, VEL_NAME), vr, dds) if vr is not None else None
    return stats
