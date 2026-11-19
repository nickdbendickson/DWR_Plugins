"""Every terrain read needs a CRS: a terrain/DEM source with no coordinate system is refused at
ingest, never silently reprojected or defaulted to EPSG:5070.

Background: an ESRI ASCII (.asc) grid carries no CRS. In the Edenville-Sanford 2020 storm runs an
untagged refill box went out in EPSG:5070, 3DEP answered 0 m everywhere, and a sea-level pit at the
terrain's west edge stopped flood growth ~1 km below Midland. PR #397 refused that 0 m tile on the
first 3DEP refill; this extends the discipline to the terrain read itself (GdalTerrain) and to every
application of a saved refill tile (dem_refill.fill_grid), not only the first download.

A tagged, valid DEM must be unaffected.

Runnable standalone (needs GDAL / osgeo; skips without it):
    python3 nd_breach_runner/tests/test_terrain_crs_required.py
or as part of the suite through the registry.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))          # the checkout root (holds nd_breach_runner)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from nd_breach_runner.tests.registry import test as register, need_osgeo, Skip  # noqa: E402

EPSG = 26914                                           # UTM 14N, metres (a North Dakota run CRS)
# a 10 x 10 box at 10 m in that CRS, and the window a run reads back from it
X0, YTOP, CELL, N = 600000.0, 5200100.0, 10.0, 10
BOUNDS = (X0, YTOP - N * CELL, X0 + N * CELL, YTOP)


def _tif(path, arr, nodata=-9999.0, tagged=True):
    """An N x N GeoTIFF of `arr` on the box above; `tagged` controls whether it carries a CRS."""
    from osgeo import gdal, osr
    import numpy as np
    a = np.asarray(arr, dtype="float32")
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, CELL, 0.0, YTOP, 0.0, -CELL))
    if tagged:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(EPSG)
        ds.SetProjection(srs.ExportToWkt())            # an untagged source leaves this empty
    b = ds.GetRasterBand(1)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    b.WriteArray(a)
    ds = None
    return path


@register
def terrain_without_a_crs_is_refused_and_a_tagged_one_passes():
    need_osgeo()
    import numpy as np
    from nd_breach_runner.core import gdal_io as G, dem_refill
    from nd_breach_runner.core.grid import Grid

    d = tempfile.mkdtemp(prefix="terrain_crs_")
    try:
        ground = np.full((N, N), 200.0, dtype="float32")

        # 1. an untagged terrain source is refused at ingest (never warped as if in work_srs,
        #    never defaulted to EPSG:5070)
        untagged = _tif(os.path.join(d, "untagged.tif"), ground, tagged=False)
        try:
            G.GdalTerrain(untagged, "EPSG:{}".format(EPSG), G.Z_METERS)
        except ValueError as e:
            msg = str(e)
            assert "no coordinate system" in msg, msg
            assert "EPSG:5070" in msg and "gdal_edit" in msg, msg     # actionable: tag the CRS
        else:
            raise AssertionError("an untagged terrain source was accepted")

        # 2. a tagged, valid DEM is unaffected - it reads its elevations as before
        tagged = _tif(os.path.join(d, "tagged.tif"), ground, tagged=True)
        g = G.GdalTerrain(tagged, "EPSG:{}".format(EPSG), G.Z_METERS).read(BOUNDS, CELL)
        vals = np.asarray(getattr(g, "data", g), dtype=float)
        assert np.isfinite(vals).any() and abs(float(np.nanmedian(vals)) - 200.0) < 0.01, vals

        # 3. fill_grid refuses a saved refill tile with no ground / one flat value (the 0 m pit),
        #    extending #397's refusal beyond the first download, and fills a hole from real ground
        hole = ground.astype("float64").copy()
        hole[5, 5] = np.nan                                # a one-cell no-data hole to refill
        grid = Grid(hole, X0, YTOP, CELL,
                    crs_wkt=G.GdalTerrain(tagged, "EPSG:{}".format(EPSG), G.Z_METERS)
                    .read(BOUNDS, CELL).crs_wkt)
        pit = _tif(os.path.join(d, "pit.tif"), np.zeros((N, N)), nodata=None)   # all 0 m, no ground
        try:
            dem_refill.fill_grid(grid, pit)
        except IOError as e:
            assert "ground" in str(e), e
        else:
            raise AssertionError("a saved 0 m pit tile was filled in on re-application")
        # a real tile (varied ground) fills the hole and leaves real cells alone
        real = ground.astype("float64").copy()
        real += np.linspace(-5.0, 5.0, N)[None, :]          # not one flat value: real relief
        out = dem_refill.fill_grid(grid, _tif(os.path.join(d, "real.tif"), real, nodata=-9999.0))
        assert np.isfinite(out.data[5, 5]), "the hole was not filled from real ground"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _run():
    tests = [terrain_without_a_crs_is_refused_and_a_tagged_one_passes]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "->", e)
        except Exception as e:                           # noqa: BLE001
            failed += 1
            print("FAIL  ", fn.__name__, "->", repr(e))
            import traceback
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(tests), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
