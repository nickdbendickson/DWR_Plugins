"""Hazard 2.18.59: a dam's window state saved as its own project (damstate.py,
ndhazard:savedamstate) - what the Runner's batch chain writes after each dam so the dam can be
reopened in the Hazard window and given more scenarios. Tiny GeoTIFF / GeoPackage fixtures.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_damstate.py
"""
import json
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsProject, QgsProcessingContext,  # noqa: E402
                       QgsProcessingFeedback)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, ogr, osr  # noqa: E402

from DamSafety_Hazard import damstate as DS  # noqa: E402
from DamSafety_Hazard import settings as S  # noqa: E402
from DamSafety_Hazard import processing_provider as PP  # noqa: E402


def _tif(path):
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 3, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((1000.0, 10.0, 0, 2000.0, 0, -10.0))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(2265)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.ones((3, 4), dtype="f4"))
    ds = None
    return path


def _gpkg(path):
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(2265)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("boundary", sr, ogr.wkbPolygon)
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetGeometry(ogr.CreateGeometryFromWkt("POLYGON((1000 1970,1040 1970,1040 2000,"
                                            "1000 2000,1000 1970))"))
    lyr.CreateFeature(f)
    ds = None
    return path


def _read_state(qgz):
    p = QgsProject()
    assert p.read(qgz), qgz
    v = p.readEntry(S.SCOPE, S.KEY, "")
    v = v[0] if isinstance(v, (tuple, list)) else v
    return p, json.loads(v)


def test_state_keys_are_window_keys():
    """Every key written is one the window restores (dialog._SETTING_WIDGETS + the table)."""
    src = open(os.path.join(ROOT, "DamSafety_Hazard", "dialog.py"), encoding="utf-8").read()
    block = src[src.index("_SETTING_WIDGETS = ("):]
    block = block[:block.index(")\n")]
    widgets = set(re.findall(r'"([a-z_]+)"', block)) | {"scenario_rows", "main_baseline"}
    st = DS.build_state("/d", "/d/a.tif", classify_options={k: True for k in
                                                            DS.CLASSIFY_TO_WIDGET})
    missing = sorted(k for k in st if k not in widgets)
    assert not missing, missing


def test_save_and_reopen(d):
    dam = os.path.join(d, "DAM_1")
    os.makedirs(dam)
    dep, vel, arr = (_tif(os.path.join(dam, n)) for n in ("dep.tif", "vel.tif", "arr.tif"))
    bnd = _gpkg(os.path.join(dam, "model_boundary_2d_area.gpkg"))
    prof = os.path.join(d, "p.ndprofile.json")
    S.save_profile(prof, {"keep_dry_check": False, "extent_spin": 1234.0,
                          "depth_edit": "/elsewhere.tif"}, "test")
    before = len(QgsProject.instance().mapLayers())
    r = DS.save_dam_state(dam, dep, vel, arr, name="HEC-RAS 2D", nid="ND01234",
                          boundary=bnd, profile=prof, classify_options={"REPORT": False},
                          dam_id="DAM 1")
    assert r["project"] == os.path.join(dam, "Hazard_DAM_1.qgz"), r["project"]
    assert os.path.isfile(r["project"]) and os.path.isfile(r["json"])
    assert len(QgsProject.instance().mapLayers()) == before        # the open project untouched
    p, st = _read_state(r["project"])
    assert st["dam_folder_edit"] == dam and st["depth_edit"] == dep
    assert st["vel_edit"] == vel and st["arr_edit"] == arr
    assert st["main_name_edit"] == "HEC-RAS 2D" and st["subject_nid_edit"] == "ND01234"
    assert st["scenario_rows"] == [] and st["main_baseline"] == ""
    assert st["keep_dry_check"] is False and st["extent_spin"] == 1234.0   # from the profile
    assert st["report_check"] is False                                     # classify options
    assert st["output_folder_edit"].startswith(dam)
    lyr = p.mapLayer(st["boundary_combo"])                     # the combo's data = layer id
    assert lyr is not None and lyr.source().startswith(bnd), st["boundary_combo"]
    assert sum(1 for x in p.mapLayers().values() if x.name().startswith("HEC-RAS 2D - ")) == 3
    rec = json.load(open(r["json"], encoding="utf-8"))
    assert rec["format"] == DS.STATE_FORMAT and rec["state"]["depth_edit"] == dep
    assert "Add scenario" in rec["reopen"]
    # no boundary -> the "(none)" choice
    r2 = DS.save_dam_state(dam, dep, project_path=os.path.join(dam, "b.qgz"))
    assert _read_state(r2["project"])[1]["boundary_combo"] == DS.NONE_TOKEN


def test_bad_inputs(d):
    for args in ((os.path.join(d, "nope"), os.path.join(d, "x.tif")),):
        try:
            DS.save_dam_state(*args)
        except ValueError as e:
            assert "dam folder not found" in str(e) and "depth raster not found" in str(e)
        else:
            raise AssertionError("bad inputs accepted")


def test_algorithm(d):
    dam = os.path.join(d, "DAM_2")
    os.makedirs(dam)
    dep = _tif(os.path.join(dam, "Depth (Max).tif"))
    alg = PP.SaveDamStateAlgorithm()
    alg.initAlgorithm()
    names = [p.name() for p in alg.parameterDefinitions()]
    for k in ("DAM_FOLDER", "DEPTH", "BOUNDARY", "PROFILE", "CLASSIFY_OPTIONS", "PROJECT_FILE"):
        assert k in names, k
    assert "savedamstate" in [c().name() for c in PP.Provider.original_classes()]
    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    res = alg.processAlgorithm({"DAM_FOLDER": dam, "DEPTH": dep, "DAM_ID": "D2",
                                "CLASSIFY_OPTIONS": '{"EAP": false}'}, ctx, fb)
    assert res["PROJECT"] == os.path.join(dam, "Hazard_D2.qgz") and os.path.isfile(res["PROJECT"])
    assert _read_state(res["PROJECT"])[1]["eap_check"] is False
    from qgis.core import QgsProcessingException
    try:
        alg.processAlgorithm({"DAM_FOLDER": dam, "DEPTH": dep, "CLASSIFY_OPTIONS": "[1"},
                             ctx, fb)
    except QgsProcessingException as e:
        assert "JSON" in str(e)
    else:
        raise AssertionError("bad JSON accepted")


def main():
    d = tempfile.mkdtemp(prefix="hz_state_")
    try:
        test_state_keys_are_window_keys()
        test_save_and_reopen(d)
        test_bad_inputs(d)
        test_algorithm(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok test_damstate")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
