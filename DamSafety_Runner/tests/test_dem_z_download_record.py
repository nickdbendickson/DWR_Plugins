"""Runner 3.10.89: Nick's one-dam run on a DEM from 'Download DEM for this dam…' (Absaraka
DWR-2801, USGS 3DEP, ground about 320 m).

1. 'Detect from the raster' (the default) stopped on a download of Screening 0.17.49 and earlier:
   those GeoTIFFs have no band unit. The Download DEM tool has always written its own record - the
   dataset metadata SOURCE + UNITS=metres (write_local_dem) and the <stem>_dem.txt facts file -
   and gdal_io.recorded_z_unit now reads it.
2. 'Feet' on that metre DEM stopped with "DEM median 320.2 with units 'feet' is outside the North
   Dakota ground range (about 250-600 m). Check the elevation unit." without saying which unit
   fits; a slightly higher dam (ground above 328 m) was not stopped at all. median_unit_problem
   names the unit that fits and stops a feet reading below 200 m when metres fit.
Needs GDAL (the QGIS Python); prints SKIP otherwise."""
import os
import shutil
import tempfile

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_dem_z_detect import _dem, _gis

try:
    from osgeo import gdal
except ImportError:                                      # pragma: no cover - plain python3
    gdal = None


def _old_download(folder, name, value=320.2, metadata=True, sidecar=True):
    """A Download DEM GeoTIFF as Screening 0.17.49 wrote it: no band unit, the dataset metadata
    and the facts file of write_local_dem / write_sidecar."""
    path = _dem(folder, name, "", value)
    if metadata:
        ds = gdal.Open(path, gdal.GA_Update)
        ds.SetMetadataItem("SOURCE", "USGS 3DEP ImageServer (elevation.nationalmap.gov)")
        ds.SetMetadataItem("CRS", "EPSG:5070")
        ds.SetMetadataItem("UNITS", "metres")
        ds = None
    if sidecar:
        with open(os.path.splitext(path)[0] + "_dem.txt", "w", encoding="utf-8") as fh:
            fh.write("source: USGS 3DEP ImageServer (elevation.nationalmap.gov)\ncrs: EPSG:5070\n"
                     "units: metres\nfile_type: GeoTIFF float32, tiled, DEFLATE\ncell_m: 10\n")
    return path


@test
def dem_z_detect_reads_the_download_dem_record():
    if gdal is None:
        print("SKIP dem_z_download_record (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_dl_")
    try:
        both = _old_download(d, "DWR-2801_10m_5070.tif")
        got, how = G.resolve_dem_z(both, "auto")
        assert got == G.Z_METERS and "UNITS=metres" in how, (got, how)
        md_only = _old_download(d, "md_10m_5070.tif", sidecar=False)
        assert G.resolve_dem_z(md_only, "auto")[0] == G.Z_METERS
        side_only = _old_download(d, "side_10m_5070.tif", metadata=False)
        got, how = G.resolve_dem_z(side_only, "auto")
        assert got == G.Z_METERS and "side_10m_5070_dem.txt" in how, (got, how)
        # a warped copy (the tool's extra CRS copy) keeps the dataset metadata
        warped = os.path.join(d, "dem_26914.tif")
        gdal.Warp(warped, md_only, dstSRS="EPSG:32614")
        assert G.resolve_dem_z(warped, "auto")[0] == G.Z_METERS
        # UNITS alone (no SOURCE: not the tool's record) is not trusted; a band unit wins
        ds = gdal.Open(_dem(d, "units_only.tif"), gdal.GA_Update)
        ds.SetMetadataItem("UNITS", "metres")
        ds = None
        try:
            G.resolve_dem_z(os.path.join(d, "units_only.tif"), "auto")
            raise AssertionError("UNITS without SOURCE must not pass")
        except ValueError as e:
            assert "no Download DEM record" in str(e), str(e)
        ft = _old_download(d, "tagged_ft.tif", 1050.0)
        ds = gdal.Open(ft, gdal.GA_Update)
        ds.GetRasterBand(1).SetUnitType("ft")
        ds = None
        assert G.resolve_dem_z(ft, "auto")[0] == G.Z_FEET
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def median_unit_problem_names_the_unit_that_fits():
    from nd_breach_runner.core import gdal_io as G
    msg = G.median_unit_problem(320.2, G.Z_FEET)       # Absaraka 3DEP run as Feet
    assert msg and "outside the North Dakota" in msg and "with units 'Feet'" in msg, msg
    assert "set 'DEM elevation units' to Meters" in msg, msg
    assert "set 'DEM elevation units' to Meters" in G.median_unit_problem(320.2, G.Z_US_FEET)
    # ground above 328 m read as feet (100-200 m) passed before; metres fit, so it stops now
    msg = G.median_unit_problem(350.0, G.Z_FEET)
    assert msg and "to Meters" in msg, msg
    # plausible readings pass
    assert G.median_unit_problem(320.2, G.Z_METERS) is None
    assert G.median_unit_problem(1050.0, G.Z_FEET) is None          # 320 m
    assert G.median_unit_problem(2500.0, G.Z_FEET) is None          # 762 m: west ND
    assert G.median_unit_problem(150.0, G.Z_METERS) is None         # as before (>= 100 m)
    # feet values run as metres: Feet fits
    msg = G.median_unit_problem(3000.0, G.Z_METERS)
    assert msg and "to Feet" in msg, msg
    # neither fits
    msg = G.median_unit_problem(-9.0, G.Z_METERS)
    assert msg and "Neither feet nor metres" in msg, msg


@test
def rundam_runs_an_old_download_with_detect_and_says_meters_for_feet():
    if gdal is None:
        print("SKIP rundam_old_download (no GDAL)")
        return
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    d = tempfile.mkdtemp(prefix="demz_dl_gis_")
    try:
        dem = _old_download(d, "DWR-2801_10m_5070.tif")
        src = _gis(algs, dem, 0)[0]                      # 'Detect from the raster'
        assert abs(src.z_to_m - 1.0) < 1e-12, src.z_to_m
        _gis(algs, dem, 3)                               # Meters
        for z in (1, 2):                                 # Feet, US survey feet
            try:
                _gis(algs, dem, z)
                raise AssertionError("a metre DEM run as feet must stop")
            except fake_qgis.QgsProcessingException as e:
                assert "set 'DEM elevation units' to Meters" in str(e), str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)
