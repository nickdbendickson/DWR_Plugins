"""Hazard 2.18.41 (audit H2): depth rasters in metres and velocity rasters in m/s are
detected (band unit / 'units' metadata, the HEC-RAS project's SI Units, the file name) and
flagged by Check inputs and the model gate's units row; feet rasters are not.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_metric_units_h2.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402

from DamSafety_Hazard import checks as CK  # noqa: E402

TMP = tempfile.mkdtemp(prefix="hazard_h2_")


def _tif(name, value, epsg=2266, unit="", meta=None, folder=TMP):
    """A 20 x 20 raster: a wet square of `value`, NoData around it."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, 20, 20, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000.0, 10.0, 0, 200000.0, 0, -10.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    a = np.full((20, 20), -9999.0, dtype=np.float32)
    a[5:15, 5:15] = value
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(a)
    if unit:
        b.SetUnitType(unit)
    if meta:
        ds.SetMetadata(meta)
    ds = None
    return path


def _levels(res, word):
    return [lvl for lvl, text in res if word in text and "looks like" in text]


def test_unit_words():
    assert CK._unit_word("m") == "metric" and CK._unit_word("metres") == "metric"
    assert CK._unit_word("m/s") == "metric" and CK._unit_word("Meters per second") == "metric"
    assert CK._unit_word("ft") == "us" and CK._unit_word("US survey foot") == "us"
    assert CK._unit_word("ft/s") == "us" and CK._unit_word("") is None
    assert CK._unit_word("hours") is None


def test_file_name_hints():
    assert CK._name_units("/x/depth_m.tif")[0] == "metric"
    assert CK._name_units("/x/Depth (m).tif")[0] == "metric"
    assert CK._name_units("/x/Velocity_mps.tif")[0] == "metric"
    assert CK._name_units("/x/vel_m_s.tif")[0] == "metric"
    assert CK._name_units("/x/Depth_ft.tif")[0] == "us"
    assert CK._name_units("/x/Velocity_ftps.tif")[0] == "us"
    for plain in ("Depth (Max).tif", "depth.tif", "Depth (Max).Terrain.dem_10m.tif",
                  "max_depth.tif", "D_M3.tif"):
        assert CK._name_units("/x/" + plain)[0] is None, plain


def test_band_unit_metres_is_an_error():
    d = _tif("depth_bandunit.tif", 4.0, unit="m")
    v = _tif("vel_bandunit.tif", 2.0, unit="m/s")
    res = CK.check_rasters(d, v, label="Breach")
    assert _levels(res, "depth raster") == [CK.ERROR], res
    assert _levels(res, "velocity raster") == [CK.ERROR], res
    txt = [t for _l, t in res if "depth raster looks like metres" in t][0]
    assert "band unit 'm'" in txt and "13.1 feet" in txt, txt       # 4 m = 13.1 ft


def test_units_metadata_item():
    d = _tif("depth_meta.tif", 2.0, meta={"UNITS": "meters"})
    res = CK.check_rasters(d)
    assert _levels(res, "depth raster") == [CK.ERROR], res


def test_hec_ras_si_project():
    proj = os.path.join(TMP, "ras_si")
    os.makedirs(os.path.join(proj, "Plan 01"), exist_ok=True)
    with open(os.path.join(proj, "Dam.prj"), "w") as fh:
        fh.write("Proj Title=Dam\nCurrent Plan=p01\nSI Units\n")
    with open(os.path.join(proj, "Plan 01", "Projection.prj"), "w") as fh:
        fh.write('PROJCS["NAD83 / UTM zone 14N"]')                       # ESRI .prj: ignored
    d = _tif("Depth (Max).tif", 3.0, epsg=26914, folder=os.path.join(proj, "Plan 01"))
    res = CK.check_rasters(d)
    assert _levels(res, "depth raster") == [CK.ERROR], res
    assert any("SI Units" in t for _l, t in res), res
    eng = os.path.join(TMP, "ras_us")
    os.makedirs(eng, exist_ok=True)
    with open(os.path.join(eng, "Dam.prj"), "w") as fh:
        fh.write("Proj Title=Dam\nEnglish Units\n")
    d2 = _tif("depth_m.tif", 3.0, epsg=26914, folder=eng)                # project wins
    assert not _levels(CK.check_rasters(d2), "depth raster")


def test_name_only_is_a_warning_and_metric_crs_makes_it_an_error():
    d = _tif("depth_m.tif", 3.0, epsg=2266)              # ND State Plane North, US feet
    assert _levels(CK.check_rasters(d), "depth raster") == [CK.WARNING]
    d = _tif("depth_m_utm.tif", 3.0, epsg=26914)          # UTM 14N, metres
    assert _levels(CK.check_rasters(d), "depth raster") == [CK.ERROR]


def test_feet_rasters_pass():
    for kw in ({}, {"unit": "ft"}, {"meta": {"units": "feet"}}):
        d = _tif("depth_feet_{}.tif".format(len(kw) and list(kw)[0]), 4.0, epsg=26914, **kw)
        v = _tif("Velocity_ftps.tif", 3.0, epsg=26914, **kw)
        res = CK.check_rasters(d, v)
        assert not [r for r in res if "looks like" in r[1]], res
        assert not [r for r in res if r[0] == CK.ERROR], res


def test_model_gate_units_row_fails_on_metres():
    from DamSafety_Hazard import modelgate as MG
    d = _tif("gate_depth.tif", 4.0, unit="metre")
    rep = MG.gate({"depth": d}, {})
    units = [r for r in rep["rows"] if r.get("Check") == "units" and "metres" in r.get("Reason", "")]
    assert units and units[0]["Status"] == MG.FAIL, rep["rows"]


def test_pipeline_guard_stops_metric_rasters():
    from qgis.core import QgsRasterLayer
    from DamSafety_Hazard import pipeline as PL

    class Run(object):
        def __init__(self):
            self.lines = []

        def log(self, msg):
            self.lines.append(msg)

    class Scen(object):
        name = "Breach"

    def layers(path):
        return {"Breach": {"depth": QgsRasterLayer(path, "d"), "vel": None}}
    run = Run()
    PL._metric_units_guard(run, [Scen()], layers(_tif("pipe_ft.tif", 4.0, unit="ft")))
    assert run.lines == []
    PL._metric_units_guard(run, [Scen()], layers(_tif("pipe_m.tif", 4.0, epsg=2266)))
    assert run.lines and "looks metric" in run.lines[0]                # name only: logged
    try:
        PL._metric_units_guard(run, [Scen()], layers(_tif("pipe_band.tif", 4.0, unit="m")))
    except RuntimeError as e:
        assert "expects feet" in str(e)
    else:
        raise AssertionError("a raster with band unit 'm' must stop the run")


def main():
    tests = [test_unit_words, test_file_name_hints, test_band_unit_metres_is_an_error,
             test_units_metadata_item, test_hec_ras_si_project,
             test_name_only_is_a_warning_and_metric_crs_makes_it_an_error,
             test_feet_rasters_pass, test_model_gate_units_row_fails_on_metres,
             test_pipeline_guard_stops_metric_rasters]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL ", fn.__name__, ":", e)
    print("\n{} tests, {} failed".format(len(tests), failed))
    shutil.rmtree(TMP, ignore_errors=True)
    return failed


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(1 if rc else 0)
