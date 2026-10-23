"""ndbreach:chainbatch with the real QGIS (offscreen): the Processing parameters, the dam
point GeoPackage, the preflight-only run and a whole batch with the stages mocked (no model,
no HEC-RAS, no Wine).

    QT_QPA_PLATFORM=offscreen <QGIS python> DamSafety_Runner/tests/realqt_chain.py

Not named test_*: run_tests.py imports every test_* module into its fake-Qt process."""
import csv
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.dirname(HERE)
ROOT = tempfile.mkdtemp()
os.symlink(RUNNER, os.path.join(ROOT, "DamSafety_Runner"))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)
os.environ["HOME"] = tempfile.mkdtemp(prefix="realqt_chain_home_")

from qgis.core import (QgsApplication, QgsFeature, QgsGeometry, QgsPointXY,  # noqa: E402
                       QgsProcessingContext, QgsProcessingFeedback, QgsVectorLayer)
_app = QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", sys.prefix), True)
_app.initQgis()

from DamSafety_Runner import algorithms  # noqa: E402,F401  (before its sub-modules)
from DamSafety_Runner import algorithms_chain as AC  # noqa: E402
from DamSafety_Runner.core import chain as CH  # noqa: E402


class FB(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.lines = []

    def pushInfo(self, m):
        self.lines.append(m)

    def reportError(self, m, fatal=False):
        self.lines.append("ERR " + m)


def layer():
    lyr = QgsVectorLayer("Point?crs=EPSG:26914&field=NIDID:string&field=Name:string&"
                         "field=Height:double", "dams", "memory")
    feats = []
    for i, (nid, x) in enumerate((("ND001", 500000), ("ND 002", 501000), ("ND003", 502000))):
        f = QgsFeature(lyr.fields())
        f.setAttributes([nid, "Dam %d" % i, 20.0 + i])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, 5200000)))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    return lyr


def main():
    out = tempfile.mkdtemp(prefix="realqt_chain_out_")
    dem = os.path.join(out, "dem.tif")
    open(dem, "w").close()
    alg = AC.ChainBatchAlgorithm()
    alg.initAlgorithm({})
    names = {p.name() for p in alg.parameterDefinitions()}
    assert {k.key for k in CH.KEYS} <= names and "DAMS" in names, names
    lyr = layer()
    ctx = QgsProcessingContext()
    # dam point GeoPackage for the Runner's batch
    feat = next(lyr.getFeatures())
    gp = AC.write_point((lyr.fields(), lyr.crs(), feat), os.path.join(out, "p", "dam.gpkg"))
    g = QgsVectorLayer(gp, "g", "ogr")
    assert g.isValid() and g.featureCount() == 1, gp
    gf = next(g.getFeatures())
    assert gf["NIDID"] == "ND001" and g.crs().authid() == "EPSG:26914"
    # preflight only: the other plugins' tools are missing here -> errors listed, nothing run
    params = {"DAMS": lyr, "DAM_IDS": "ND001, ND 002, NOPE", "OUT_FOLDER": out, "DEM": dem,
              "HEC_MODE": 2, "CHECK_ONLY": True}
    fb = FB()
    res = alg.processAlgorithm(params, ctx, fb)
    text = "\n".join(fb.lines)
    assert "NOPE" in text and "nddam:runchain" in text, text
    assert res["COUNTS"].startswith("preflight"), res
    # a whole batch with the stages mocked
    calls = []

    def fake_run(self, stage, dam):
        calls.append((dam.dam_id, stage, dam.mode))
        if dam.dam_id == "ND 002" and stage == "runner":
            raise CH.ChainError("no DEM coverage")
        f = os.path.join(dam.folder, stage + ".txt")
        os.makedirs(dam.folder, exist_ok=True)
        open(f, "w").close()
        o = {CH.REQUIRED_OUTPUT[stage]: f}
        if stage == "classify":
            o.update(nd_class="Low", tm11_class="Low", project=f)
        return o
    AC.QgisOps.run = fake_run
    AC.registry_algs = lambda: set(CH.ALGS.values())
    ov = os.path.join(out, "ov.csv")
    with open(ov, "w") as f:
        f.write("dam_id,HEC_MODE\nND003,1\n")
    params.update(CHECK_ONLY=False, DAM_IDS="", OVERRIDES_CSV=ov, SCREENING=False)
    fb = FB()
    res = alg.processAlgorithm(params, ctx, fb)
    with open(res["SUMMARY"], newline="") as f:
        rows = {r["Dam"]: r for r in csv.DictReader(f)}
    assert set(rows) == {"ND001", "ND 002", "ND003"}, rows
    assert rows["ND001"]["Status"] == "done" and rows["ND001"]["HEC_Mode"] == "skip"
    assert rows["ND 002"]["Status"] == "failed"
    assert rows["ND003"]["HEC_Mode"] == "wait", rows["ND003"]
    assert [s for d, s, _m in calls if d == "ND001"] == ["runner", "classify"]
    assert [s for d, s, _m in calls if d == "ND003"] == ["runner", "hec_build", "hec_run",
                                                        "hazard_import", "classify"]
    used = json.load(open(os.path.join(out, "ND003", "_chain", CH.USED_NAME)))
    assert used["overrides_applied"], used
    man = json.load(open(res["MANIFEST"]))
    assert man["counts"] == {"done": 2, "failed": 1}, man
    # the window: values round trip, the check, the Processing parameters
    from DamSafety_Runner.chain_dialog import ChainDialog
    from qgis.core import QgsProject
    QgsProject.instance().addMapLayer(lyr)
    d = ChainDialog(None)
    cv, _ = CH.chain_values({})
    cv.update(OUT_FOLDER=out, DEM=dem, HEC_MODE=2, SCREENING=False, HEC_WAIT_MIN=15.0)
    d.set_chain_values(cv)
    got = d.chain_values()
    assert got["HEC_MODE"] == 2 and got["OUT_FOLDER"] == out and not got["SCREENING"], got
    assert not d.wait_min.isEnabled()
    d.layer.setLayer(lyr)
    d.id_field.setField("NIDID")
    d.selected_only.setChecked(False)
    assert d.dam_ids() == (["ND001", "ND 002", "ND003"], [])
    d.ids.setText("ND003, XX")
    assert d.dam_ids() == (["ND003"], ["XX"])
    rows = d.run_check()
    assert [t for lv, t in rows if lv == CH.ERROR] == ["XX: dam ID XX is not in the dams "
                                                        "layer"], rows
    assert not d.run_btn.isEnabled()
    d.ids.setText("ND003")
    rows = d.run_check()
    assert not [t for lv, t in rows if lv == CH.ERROR], rows
    assert d.run_btn.isEnabled()
    p = d.params()
    assert p["DAMS"] == lyr.id() and p["DAM_ID_FIELD"] == "NIDID" and p["HEC_MODE"] == 2
    sf = os.path.join(out, "suite.csv")
    d.settings_file.setText(sf)
    assert d.save_to_settings() and os.path.exists(sf)
    d.hec_radios[1].setChecked(True)
    d.fill_from_settings()
    assert d.chain_values()["HEC_MODE"] == 2           # back to the saved value
    shutil.rmtree(out, ignore_errors=True)
    print("realqt_chain: OK")


if __name__ == "__main__":
    code = 1
    try:
        main()
        code = 0
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)                  # offscreen QGIS can crash on teardown; the result stands
