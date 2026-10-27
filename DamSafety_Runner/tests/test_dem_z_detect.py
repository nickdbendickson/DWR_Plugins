"""Runner 3.10.88: 'DEM elevation units' = 'Detect from the raster' (DEM_Z 0, the default).

Before, the run tools refused it for every DEM ("Set the DEM elevation unit (feet or metres)"),
even one that records its unit - so a DEM fresh from Screening's Download DEM tool could not run
with the default. Now (core/gdal_io.resolve_dem_z) the unit the raster records - band unit type,
else a compound CRS's vertical unit - is used; a DEM that records none still stops, and the
message names the setting and which sources are metres / feet. Never a guess from the
horizontal CRS or the values. Needs GDAL (the QGIS Python); prints SKIP otherwise."""
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.tests.registry import test

try:
    from osgeo import gdal, osr
except ImportError:                                      # pragma: no cover - plain python3
    gdal = None


def _dem(folder, name, unit="", value=400.0):
    """A 20 x 20 EPSG:26914 GeoTIFF (UTM 14N metres) with band unit `unit`."""
    path = os.path.join(folder, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, 20, 20, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000.0, 10.0, 0.0, 5200000.0, 0.0, -10.0))
    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(np.full((20, 20), value, dtype="float32"))
    b.SetNoDataValue(-9999.0)
    if unit:
        b.SetUnitType(unit)
    b = None
    ds = None
    return path


def _vrt(folder, name, srcs):
    path = os.path.join(folder, name)
    v = gdal.BuildVRT(path, srcs)
    v = None
    return path


@test
def dem_z_detect_reads_the_recorded_unit():
    if gdal is None:
        print("SKIP dem_z_detect (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_")
    try:
        assert G.resolve_dem_z(_dem(d, "m.tif", "m"), "auto")[0] == G.Z_METERS
        assert G.resolve_dem_z(_dem(d, "metre.tif", "metre"), "auto")[0] == G.Z_METERS
        assert G.resolve_dem_z(_dem(d, "ft.tif", "ft", 1300.0), "auto")[0] == G.Z_FEET
        assert G.resolve_dem_z(_dem(d, "usft.tif", "US survey foot", 1300.0), "auto")[0] == G.Z_US_FEET
        # an explicit choice is used as given, whatever the raster says
        assert G.resolve_dem_z(_dem(d, "none.tif"), "meters") == (G.Z_METERS, "chosen")
        assert G.resolve_dem_z(os.path.join(d, "m.tif"), "feet")[0] == G.Z_FEET
        # no unit recorded (a plain GeoTIFF, a BuildVRT mosaic - BuildVRT drops the tag): refused,
        # naming the setting; a horizontal metre CRS is not taken as the elevation unit
        for path in (os.path.join(d, "none.tif"), _vrt(d, "mosaic.vrt", [os.path.join(d, "m.tif")])):
            try:
                G.resolve_dem_z(path, "auto")
                raise AssertionError("no unit recorded must not pass: " + path)
            except ValueError as e:
                msg = str(e)
                assert "'DEM elevation units'" in msg and "Meters or Feet" in msg, msg
                assert "no elevation unit is recorded" in msg, msg
        # a unit that is neither feet nor metres is named
        try:
            G.resolve_dem_z(_dem(d, "odd.tif", "furlong"), "auto")
            raise AssertionError("an unknown unit must not pass")
        except ValueError as e:
            assert "'furlong'" in str(e), str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _gis(alg_mod, dem_path, z_index):
    """RunDamAlgorithm.gis() with the fake QGIS: the DEM layer's CRS is left unset so the
    model CRS is WORK_CRS (EPSG:26914), as for a DEM QGIS cannot read the CRS of."""
    from nd_breach_runner.tests import fake_qgis
    a = alg_mod.RunDamAlgorithm()
    a.initAlgorithm()
    dem = types.SimpleNamespace(source=lambda: dem_path,
                                crs=lambda: fake_qgis.QgsCoordinateReferenceSystem(""))
    params = {"DEM": dem, "DEM_Z": z_index,
              "WORK_CRS": fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914")}
    return a.gis(params, fake_qgis.QgsProcessingContext(),
                 fake_qgis.QgsCoordinateReferenceSystem("EPSG:26914"), (600100.0, 5199900.0))


@test
def rundam_dem_z_detect_runs_a_tagged_dem_and_names_the_setting_otherwise():
    if gdal is None:
        print("SKIP rundam_dem_z_detect (no GDAL)")
        return
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    assert [z[0] for z in algs.ZUNITS][0] == "auto"
    d = tempfile.mkdtemp(prefix="demz_gis_")
    try:
        tagged = _dem(d, "dl_10m_5070.tif", "m")
        untagged = _dem(d, "plain.tif")
        src = _gis(algs, tagged, 0)[0]                   # 'Detect from the raster' + band unit m
        # 3.10.89: the units note (run log, report.json, docx) says how, not "(chosen)"
        assert abs(src.z_to_m - 1.0) < 1e-12, src.z_to_m
        assert src.z_note == "metres (detected: raster unit 'm')", src.z_note
        assert _gis(algs, tagged, 3)[0].z_note == "metres (chosen)"
        src = _gis(algs, _dem(d, "ft_tagged.tif", "ft", 1300.0), 0)[0]
        assert abs(src.z_to_m - 0.3048) < 1e-12, src.z_to_m
        _gis(algs, untagged, 3)                          # Meters chosen: runs as before
        try:
            _gis(algs, untagged, 0)
            raise AssertionError("an untagged DEM with 'Detect' must stop")
        except fake_qgis.QgsProcessingException as e:
            assert "'DEM elevation units'" in str(e) and "plain.tif" in str(e), str(e)
        # the ND ground-range check still runs on the detected unit: a metre tag on feet values
        try:
            _gis(algs, _dem(d, "feet_values_tagged_m.tif", "m", 3000.0), 0)
            raise AssertionError("3000 'm' is outside North Dakota and must stop")
        except fake_qgis.QgsProcessingException as e:
            assert "outside the North Dakota" in str(e), str(e)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------- the Runner window's 'Download a DEM' box
@test
def dem_facts_count_imageserver_chunks_and_offer_the_native_1m_tiles():
    from nd_breach_runner.tests.test_popup import qt_fakes
    with qt_fakes() as q:
        D = q.dialog
        # Nick's Absaraka case: USGS 3DEP, Dam, 10 mi, 1 m = 27 x 27 = 729 ImageServer chunks
        t = D.dem_facts_text(0, 1, 10.0, 0)
        assert "729 export requests (27 x 27) of 1.2 km" in t and "not lidar tiles" in t, t
        assert D.DEM_SOURCE_TNM in t, t
        t10 = D.dem_facts_text(0, 10, 10.0, 0)
        assert "9 export requests (3 x 3) of 12.0 km" in t10 and D.DEM_SOURCE_TNM not in t10, t10
        assert "export requests" not in D.dem_facts_text(1, 1, 10.0, 0)       # ND DWR LiDAR
        assert "export requests" not in D.dem_facts_text(0, 1, 10.0, 1)       # a county
        tn = D.dem_facts_text(2, 1, 10.0, 0)
        assert "10 km" in tn and "download the listed tiles" in tn and "metres" in tn, tn
        # 3.10.89: ND DWR DEM_Imagine is UTM metres (EPSG:3721 is UTM 14N), as build_vrts reads
        # it; only ASCII_Grids_ft is feet - the facts line said 'QL2 feet ... native units feet'
        nd = D.dem_facts_text(1, 10, 10.0, 0)
        assert "native units feet" not in nd and "QL2 feet" not in nd, nd
        assert "DEM_Imagine: Erdas Imagine .img, UTM metres" in nd and "converted x 0.3048" in nd, nd
        # the native tiles are not offered for the state (the tool refuses it)
        st = D.dem_facts_text(2, 1, 10.0, 2)
        assert "not offered for the whole state" in st and "10 km GeoTIFFs" not in st, st
        dlg = D.NDBreachRunnerDialog()
        # 3.10.89: vars(), not hasattr() - the fake QWidget answers every attribute name
        if "dem_source" not in vars(dlg):
            print("SKIP the window part (DamSafety Screening not importable here)")
            return
        items = [dlg.dem_source.itemText(i) for i in range(dlg.dem_source.count())]
        assert items == ["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)", D.DEM_SOURCE_TNM], items
        dlg.dem_source.setCurrentIndex(0)
        dlg.dem_res.setCurrentIndex(1)
        assert "729 export requests" in dlg.dem_facts.text(), dlg.dem_facts.text()
        dlg.dem_source.setCurrentIndex(2)
        assert "10 km GeoTIFFs" in dlg.dem_facts.text(), dlg.dem_facts.text()
        d = tempfile.mkdtemp(prefix="demdl_")
        try:
            dlg.dem_out.setText(d)
            dlg.dem_point.setText("-97.390918,46.984382")
            dlg._open_dem_download()
            alg, p = q.proc.DIALOGS[-1]
            assert alg == "nddam:downloaddem" and p["SOURCE"] == 2 and p["BUFFER_MI"] == 10.0, p
            # 3.10.89: COUNTY is the enum index (a name became index 0, Adams, in the tool dialog)
            names = [dlg.dem_county.itemText(i) for i in range(dlg.dem_county.count())]
            assert "Stark" in names and names[0] != "Stark", names[:3]
            dlg.dem_scope.setCurrentIndex(1)
            dlg.dem_county.setCurrentIndex(names.index("Stark"))
            dlg._open_dem_download()
            p = q.proc.DIALOGS[-1][1]
            assert isinstance(p["COUNTY"], int) and names[p["COUNTY"]] == "Stark", p["COUNTY"]
            # SOURCE 2 for the whole state: refused here, before the tool is opened
            n = len(q.proc.DIALOGS)
            dlg.dem_scope.setCurrentIndex(2)
            dlg._open_dem_download()
            assert len(q.proc.DIALOGS) == n, q.proc.DIALOGS[-1]
        finally:
            shutil.rmtree(d, ignore_errors=True)
