"""Screening 0.17.50 (Nick, Absaraka DWR-2801, 2026-10-26).

1. 'Download DEM' with USGS 3DEP at 1 m over a dam's 10 mi buffer said it would fetch 729
   pieces. They are ImageServer export requests of 1200 x 1200 px (1.2 km at 1 m) over a
   32 km square, 27 x 27 = 729, not lidar tiles. imageserver_chunks / chunk_note count them
   and say so; nothing changes at 10 m (3 x 3 = 9).
2. The local GeoTIFFs had no band unit (the ImageServer tiles and BuildVRT carry none), so the
   Runner's 'DEM elevation units' = 'Detect from the raster' could not read metres.
   write_local_dem and warp_to now tag band 1 with the unit the source records (build_vrts tags
   its VRTs; the USGS 3DEP ImageServer serves metres), never blindly 'm'.
Needs GDAL (the QGIS Python); prints SKIP otherwise. No network."""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _optional  # noqa: E402
_optional.need("osgeo", "writes and reads small GeoTIFFs")
import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from nd_dam_screening.core import dem_download as dd  # noqa: E402

gdal.UseExceptions()
MI = 1609.344


def test_chunks():
    side = 2 * 10 * MI                                  # the dialog's 10 mi buffer, a square
    n, km = dd.imageserver_chunks((0.0, 0.0, side, side), 1)
    assert (n, km) == (729, 1.2), (n, km)
    n10, km10 = dd.imageserver_chunks((0.0, 0.0, side, side), 10)
    assert (n10, km10) == (9, 12.0), (n10, km10)
    assert dd.imageserver_chunks((0.0, 0.0, 1200.0, 1200.0), 1)[0] == 1
    note = dd.chunk_note(n, 1, km, side / 1000.0, side / 1000.0)
    for s in ("729 export requests", "1.2 km", "not lidar tiles", "32 x 32 km box",
              "USGS 3DEP 1 m lidar tiles (native, TNM)", "smaller buffer"):
        assert s in note, (s, note)
    assert dd.chunk_note(n10, 10, km10) == "" and dd.chunk_note(36, 1, 1.2) == ""
    print("OK chunks")


def _src(work):
    s = osr.SpatialReference()
    s.ImportFromEPSG(5070)
    p = os.path.join(work, "src.tif")
    ds = gdal.GetDriverByName("GTiff").Create(p, 60, 60, 1, gdal.GDT_Float32)
    ds.SetProjection(s.ExportToWkt())
    ds.SetGeoTransform((0.0, 1.0, 0.0, 1500060.0, 0.0, -1.0))
    ds.GetRasterBand(1).WriteArray(np.full((60, 60), 450.0, dtype="float32"))
    ds = None
    return p


def _unit(p):
    ds = gdal.Open(p)
    try:
        return ds.GetRasterBand(1).GetUnitType()
    finally:
        ds = None


def test_units():
    work = tempfile.mkdtemp(prefix="scr_dem_units_")
    try:
        _units(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _units(work):
    src = _src(work)
    assert _unit(src) == ""
    # gdal.Warp alone does not carry a unit: why the tag is written explicitly
    plain = os.path.join(work, "plain.tif")
    gdal.Warp(plain, src, xRes=5, yRes=5)
    assert _unit(plain) == ""
    assert dd.set_metres(plain) and _unit(plain) == "m"
    assert dd.set_metres(os.path.join(work, "missing.tif")) is False
    info = {"source": "local", "crs": "EPSG:5070", "units": "metre", "file_type": "GTiff",
            "cell_m": 5.0, "native_source": "", "native_crs": "", "native_units": "",
            "native_file_type": ""}
    # the USGS 3DEP ImageServer mosaic records no unit; the service serves metres: 'm'
    got = os.path.join(work, "out", "dem_5m.tif")
    dd.write_local_dem(src, got, 5.0, dd.product_info("usgs", 5))
    assert _unit(got) == "m", _unit(got)
    # any other source that records no unit: left untagged (not a blind 'm')
    other = os.path.join(work, "out", "other_5m.tif")
    dd.write_local_dem(src, other, 5.0, info)
    assert _unit(other) == "", _unit(other)
    # a source that records its unit gives that unit (build_vrts' metre VRTs: 'm')
    tagged = os.path.join(work, "tagged.tif")
    gdal.Translate(tagged, src)
    assert dd.set_metres(tagged) and _unit(tagged) == "m"
    dd.write_local_dem(tagged, os.path.join(work, "out", "t_5m.tif"), 5.0, info)
    assert _unit(os.path.join(work, "out", "t_5m.tif")) == "m"
    # warp_to carries the source's unit (the local GeoTIFF's 'm'); an untagged source stays so
    w = dd.warp_to(got, os.path.join(work, "w26914.tif"), 26914)
    assert _unit(w) == "m", _unit(w)
    w = dd.warp_to(src, os.path.join(work, "w2_26914.tif"), 26914)
    assert _unit(w) == "", _unit(w)
    print("OK units")


if __name__ == "__main__":
    test_chunks()
    test_units()
    print("ALL OK")
