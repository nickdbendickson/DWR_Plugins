"""Basin statistics of a local NOAA Atlas 14 GIS grid (GDAL + numpy).

The PFDS GIS data page publishes Atlas 14 grids (ASCII grid / GeoTIFF) per volume, frequency and
duration, with values in thousandths of an inch (scale 0.001). The user downloads the grid once;
this module reads it, masks it with the basin, and returns mean / min / max in inches.
"""
import json
import os
import re

import numpy as np
from osgeo import gdal, ogr, osr

from . import atlas14 as A

# audit 2026-11-20: no gdal / ogr.UseExceptions() at import (process-wide, it changed GDAL for QGIS and every other
# plugin); the two GDAL readers below raise inside A.gdal_raising() only.

# e.g. mw100yr24ha.asc, orb_100yr24ha.tif, sw1000yr60da_ams.asc, mw100yr48hau.asc (upper 90% bound)
_NAME = re.compile(r"^(?P<region>[a-z]+)_?(?P<freq>\d+)yr(?P<dur>\d+)(?P<unit>[mhd])a(?P<ulm>[lu])?(?P<ams>_ams)?", re.I)
_UNIT = {"m": "min", "h": "hr", "d": "day"}


def duration_label(n, unit):
    """Atlas 14 duration label of a file-name duration (60m -> 60-min, 48h -> 2-day, 24h -> 24-hr)."""
    unit = unit.lower()
    n = int(n)
    if unit == "h" and n == 48:
        return "2-day"
    return "{}-{}".format(n, _UNIT[unit])


def parse_name(path):
    m = _NAME.match(os.path.basename(path))
    if not m:
        return {}
    out = {"region_code": m.group("region").lower(), "frequency_years": int(m.group("freq")),
           "duration": duration_label(m.group("dur"), m.group("unit")),
           "series": "ams" if m.group("ams") else "pds"}
    if m.group("ulm"):
        out["bound"] = "lower90" if m.group("ulm").lower() == "l" else "upper90"
    return out


def grid_scale(path, default=0.001):
    """Inches per grid unit: 1.0 for a grid written by an Atlas 14 fetchgrid tool (its JSON sidecar says units in;
    the sidecar schema id damit.pmp.atlas14_gis is a file-format name shared by both plugins), else ``default``
    (NOAA's own grids are thousandths of an inch)."""
    side = path + ".json"
    if os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as fh:
                doc = json.load(fh)
            if doc.get("schema") == "damit.pmp.atlas14_gis" and doc.get("units") == "in":
                return 1.0
        except (OSError, ValueError):
            pass
    return default


@A.gdal_raising("gdal", "ogr")
def point_value(raster_path, x, y, crs_wkt, scale=0.001):
    """Value (in inches) of the grid cell containing x, y (a point in the CRS ``crs_wkt``); None if outside / empty."""
    ds = gdal.Open(raster_path)
    if ds is None:
        return None            # audit 2026-11-20: unreadable raster, not a crash (also releases cleanly)
    try:
        return _point_value_body(ds, x, y, crs_wkt, scale)
    finally:
        ds = None              # audit 2026-11-20: release the handle before GC (Windows file locking)


def _point_value_body(ds, x, y, crs_wkt, scale):
    """The read logic, split out so point_value can guarantee the ds release."""
    band = ds.GetRasterBand(1)
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection()) if ds.GetProjection() else srs.ImportFromEPSG(4326)
    psrs = osr.SpatialReference()
    psrs.ImportFromWkt(crs_wkt)
    for s in (srs, psrs):
        try:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:                                   # pragma: no cover
            pass
    if not srs.IsSame(psrs):
        x, y, _ = osr.CoordinateTransformation(psrs, srs).TransformPoint(x, y)
    gt = ds.GetGeoTransform()
    c, r = int((x - gt[0]) // gt[1]), int((y - gt[3]) // gt[5])
    if not (0 <= c < ds.RasterXSize and 0 <= r < ds.RasterYSize):
        return None
    v = float(band.ReadAsArray(c, r, 1, 1)[0, 0])
    nd = band.GetNoDataValue()
    if (nd is not None and v == nd) or v < 0 or v != v:
        return None
    return round(v * scale, 4)


@A.gdal_raising("gdal", "ogr")
def basin_stats(raster_path, basin_wkt, basin_epsg_wkt, scale=0.001):
    ds = gdal.Open(raster_path)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    srs = osr.SpatialReference()
    if ds.GetProjection():
        srs.ImportFromWkt(ds.GetProjection())
    else:
        srs.ImportFromEPSG(4326)                                 # PFDS ASCII grids are geographic (NAD83)
    bsrs = osr.SpatialReference()
    bsrs.ImportFromWkt(basin_epsg_wkt)
    for s in (srs, bsrs):
        try:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:                                   # pragma: no cover - GDAL < 3
            pass
    geom = ogr.CreateGeometryFromWkt(basin_wkt)
    if not srs.IsSame(bsrs):
        geom.Transform(osr.CoordinateTransformation(bsrs, srs))
    gt = ds.GetGeoTransform()
    minx, maxx, miny, maxy = geom.GetEnvelope()
    c0 = max(int((minx - gt[0]) / gt[1]), 0)
    c1 = min(int((maxx - gt[0]) / gt[1]) + 1, ds.RasterXSize)
    r0 = max(int((maxy - gt[3]) / gt[5]), 0)
    r1 = min(int((miny - gt[3]) / gt[5]) + 1, ds.RasterYSize)
    if c1 <= c0 or r1 <= r0:
        raise ValueError("the basin is outside the Atlas 14 grid {}".format(os.path.basename(raster_path)))
    w, h = c1 - c0, r1 - r0
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
    mem.SetGeoTransform((gt[0] + c0 * gt[1], gt[1], 0, gt[3] + r0 * gt[5], 0, gt[5]))
    mem.SetProjection(srs.ExportToWkt())
    vds = ogr.GetDriverByName("Memory").CreateDataSource("")
    lyr = vds.CreateLayer("b", srs, ogr.wkbUnknown)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(geom)
    lyr.CreateFeature(f)
    gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
    mask = mem.GetRasterBand(1).ReadAsArray() == 1
    a = band.ReadAsArray(c0, r0, w, h).astype("float64")
    valid = mask & np.isfinite(a)
    if nodata is not None:
        valid &= a != nodata
    valid &= a >= 0
    if not valid.any():
        raise ValueError("no Atlas 14 grid cells with values inside the basin (outside Atlas 14 coverage?)")
    v = a[valid] * scale
    return {"cells": int(valid.sum()), "mean": round(float(v.mean()), 4), "min": round(float(v.min()), 4),
            "max": round(float(v.max()), 4)}
