"""Hazard 2.18.58: the HEC-RAS 2D flow area as the model boundary.

'Import HEC-RAS plan results' (rasimport / ndhazard:hecrasplan) writes the 2D flow area
perimeter(s) to model_boundary_2d_area.gpkg next to the rasters, and ndhazard:batch BOUNDARY
uses each dam folder's copy for the model edge check (a dam without it: automatic check).
Tiny h5py fixture, no HEC-RAS.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_batch_boundary.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402

from DamSafety_Hazard import pipeline as PL  # noqa: E402
from DamSafety_Hazard import processing_provider as PP  # noqa: E402
from DamSafety_Hazard import rasimport as RI  # noqa: E402


def _plan(path):
    import h5py
    with h5py.File(path, "w") as f:
        g = f.create_group("Geometry/2D Flow Areas")
        g["Polygon Points"] = np.array([[0, 0], [100, 0], [100, 50], [0, 50],
                                        [200, 0], [260, 0], [230, 40]], dtype="f8")
        g["Polygon Info"] = np.array([[0, 4, 0, 0], [4, 3, 0, 0]], dtype="i4")
        g["Attributes"] = np.array([(b"Main",), (b"",)], dtype=[("Name", "S16")])


def test_perimeters_to_gpkg(d):
    from osgeo import ogr
    hdf = os.path.join(d, "x.p01.hdf")
    _plan(hdf)
    rings = RI.area_perimeters(hdf)
    assert [n for n, _ in rings] == ["Main", "Area 2"], rings
    assert rings[0][1][2] == (100.0, 50.0)
    out = RI.write_area_boundary(hdf, os.path.join(d, RI.BOUNDARY_FILE))
    assert out and os.path.isfile(out)
    ds = ogr.Open(out)
    lyr = ds.GetLayerByName(RI.BOUNDARY_LAYER)
    assert lyr.GetFeatureCount() == 2
    areas = sorted(round(f.GetGeometryRef().GetArea()) for f in lyr)
    assert areas == [1200, 5000], areas
    ds = None
    # a plan without a perimeter: no file, no error
    import h5py
    empty = os.path.join(d, "empty.p01.hdf")
    with h5py.File(empty, "w") as f:
        f.create_group("Geometry")
    assert RI.area_perimeters(empty) == []
    assert RI.write_area_boundary(empty, os.path.join(d, "none.gpkg")) is None
    assert not os.path.exists(os.path.join(d, "none.gpkg"))
    return out


def test_resolve_boundary_file(d, gpkg):
    dam = os.path.join(d, "dam1")
    os.makedirs(dam)
    shutil.copy(gpkg, os.path.join(dam, RI.BOUNDARY_FILE))
    logs = []
    cfg = {"dam_folder": dam, "model_boundary_layer": None,
           "model_boundary_file": RI.BOUNDARY_FILE}
    got = PL.resolve_boundary_file(cfg, logs.append)
    lyr = got["model_boundary_layer"]
    assert lyr is not None and lyr.isValid() and lyr.featureCount() == 2
    assert cfg["model_boundary_layer"] is None                  # the caller's cfg is kept
    assert any("Model boundary:" in m for m in logs)
    # missing file: automatic check, logged
    logs[:] = []
    cfg2 = dict(cfg, dam_folder=os.path.join(d, "nodam"))
    assert PL.resolve_boundary_file(cfg2, logs.append) is cfg2
    assert any("not found" in m for m in logs)
    # unreadable file: automatic check, warned
    bad = os.path.join(d, "bad.gpkg")
    open(bad, "w").write("not a geopackage")
    logs[:] = []
    cfg3 = dict(cfg, model_boundary_file=bad)
    assert PL.resolve_boundary_file(cfg3, logs.append)["model_boundary_layer"] is None
    assert any("WARNING" in m for m in logs)
    # not set, or a layer given: untouched
    assert PL.resolve_boundary_file({"model_boundary_file": ""}) == {"model_boundary_file": ""}
    given = dict(cfg, model_boundary_layer="L")
    assert PL.resolve_boundary_file(given) is given
    assert PL.DEFAULTS["model_boundary_file"] == ""
    # reaches the parallel workers (a plain string setting, unlike the layer)
    assert "model_boundary_file" in PP.worker_setting_keys()
    assert "model_boundary_layer" not in PP.worker_setting_keys()


def test_batch_parameter():
    alg = PP.BatchAlgorithm()
    alg.initAlgorithm({})
    p = alg.parameterDefinition("BOUNDARY")
    assert p is not None and p.defaultValue() == 0 and len(p.options()) == 2
    f = alg.parameterDefinition("BOUNDARY_FILE")
    assert f.defaultValue() == RI.BOUNDARY_FILE == PP.BOUNDARY_FILE_DEFAULT
    plan = PP.RasPlanAlgorithm()
    plan.initAlgorithm({})
    assert "BOUNDARY" in [o.name() for o in plan.outputDefinitions()]
    src = open(os.path.join(ROOT, "DamSafety_Hazard", "rasimport.py"), encoding="utf-8").read()
    body = src.split("def import_plan(", 1)[1].split("\ndef ", 1)[0]
    assert "write_area_boundary(hdf_path, os.path.join(out_folder, BOUNDARY_FILE), wkt)" in body
    psrc = open(os.path.join(ROOT, "DamSafety_Hazard", "pipeline.py"), encoding="utf-8").read()
    run = psrc.split("def run_classification(", 1)[1].split("\ndef ", 1)[0]
    assert run.index("resolve_boundary_file(cfg, log)") < run.index("Run(cfg")


def test_batch_passes_the_file_name():
    """processAlgorithm with BOUNDARY = 1 gives batch_classify model_boundary_file."""
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    seen = {}
    old = PP.batch_classify

    def fake(parent, over, **k):
        seen.update(over)
        return [], os.path.join(parent, PP.BATCH_CSV)
    PP.batch_classify = fake
    d = tempfile.mkdtemp(prefix="hz_bnd_alg_")
    try:
        alg = PP.BatchAlgorithm()
        alg.initAlgorithm({})
        ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
        for b, want in ((0, None), (1, RI.BOUNDARY_FILE)):
            seen.clear()
            try:
                alg.processAlgorithm({"FOLDER": d, "PARENT": d, "BOUNDARY": b}, ctx, fb)
            except Exception:
                pass                                     # only the settings matter here
            assert seen.get("model_boundary_file") == want, (b, seen)
    finally:
        PP.batch_classify = old
        shutil.rmtree(d, ignore_errors=True)


def main():
    d = tempfile.mkdtemp(prefix="hz_bnd_")
    try:
        gpkg = test_perimeters_to_gpkg(d)
        test_resolve_boundary_file(d, gpkg)
        test_batch_parameter()
        test_batch_passes_the_file_name()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok test_batch_boundary")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
