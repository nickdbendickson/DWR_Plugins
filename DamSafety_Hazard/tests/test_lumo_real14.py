"""Lumo vReal.14 FF.1/FF.2/FF.4, PH.1, EX.8, TP.1 (Hazard 2.18.25).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real14.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import formfill as FF  # noqa: E402
from DamSafety_Hazard import portfolio as PF  # noqa: E402
from DamSafety_Hazard import parcelhints as PH  # noqa: E402
from DamSafety_Hazard import temporal as TP  # noqa: E402
from DamSafety_Hazard.constants import SUBDIR_QA  # noqa: E402

GOV = "1 habitable structure(s) in the ACER high danger zone [5.2.1.1]"


def _folder(provisional, snapshot=False):
    reasons = [GOV]
    if provisional:
        reasons.insert(0, CL.PROVISIONAL_PREFIX + "HIGH is provisional: the most-exposed ...")
    nd = {"cls": "High", "reasons": reasons, "counts": {}, "criteria": None, "judgment": []}
    tm11 = {"cls": "High", "lij": 7.0, "lij_structures_high": 7, "lij_structures_judgment": 0,
            "judgment_included": True, "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
            "cls_without_judgment": "High", "notes": []}
    out = tempfile.mkdtemp()
    with open(os.path.join(out, "Hazard_Classification_Summary.txt"), "w") as fh:
        fh.write("\n".join(CL.summary_lines(nd, tm11, [], [], [], "Dam", [], False, {})))
    if snapshot:
        os.makedirs(os.path.join(out, SUBDIR_QA))
        with open(os.path.join(out, SUBDIR_QA, "Last_Run_Snapshot.json"), "w") as fh:
            json.dump({"nd": "High", "nd_reasons": reasons, "tm11": "High", "lij": 7.0}, fh)
    return out


def test_ff1_governing_skips_provisional():
    for prov in (True, False):
        for snap in (False, True):
            v = FF.available_values(_folder(prov, snap))
            assert v["ND_GOVERNING_CRITERION"] == GOV, (prov, snap, v["ND_GOVERNING_CRITERION"])
            assert v["ND_GOVERNING_SECTION"] == "5.2.1.1"
            assert ("PROVISIONAL" in v["ND_REASONS"]) == prov


def test_ff4_clean_nid():
    for bad in ("", None, "none", "None", "''", '""', "  "):
        assert PF.clean_nid(bad) is None, bad
    assert PF.clean_nid(" ND00123 ") == "ND00123"


def test_ph1_nonnumeric_counted():
    c = {"ok": True, "hinted": 0, "on_parcel": 10, "no_building": 0, "non_residential": 0,
         "no_parcel": 0, "parcels_matched": 10, "improvement_empty": 3,
         "improvement_nonnumeric": 7, "fields": {"improvement": "IMPR"}}
    txt = "\n".join(PH.summary_lines(c))
    assert "not a number (7" in txt and "10 of the 10" in txt, txt
    c.update(improvement_empty=0, improvement_nonnumeric=2)
    assert "2 improvement value(s) are not numbers" in "\n".join(PH.summary_lines(c))
    assert PH._number("N/A") != PH._number("N/A")      # NaN


def test_tp1_nominal_only_when_used():
    from qgis.core import QgsVectorLayer, QgsFeature
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=Arrival_hr:double", "EAP", "memory")
    f = QgsFeature(lyr.fields())
    f.setAttributes([1.5])
    lyr.dataProvider().addFeatures([f])
    ok, msg = TP.apply_arrival_animation(lyr, "Arrival_hr", None)
    assert ok and "(nominal)" in msg, msg
    ok, msg = TP.apply_arrival_animation(lyr, "Arrival_hr", "2026-05-01 06:00")
    assert ok and "(nominal)" not in msg, msg


def test_ex8_bad_start_warns():
    from qgis.core import QgsVectorLayer, QgsFeature
    from DamSafety_Hazard import pipeline_extras as PX
    base, field, _u = TP.ANIMATED_LAYERS[0]
    d = tempfile.mkdtemp()
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field={}:double".format(field), base, "memory")
    f = QgsFeature(lyr.fields())
    f.setAttributes([1.0])
    lyr.dataProvider().addFeatures([f])
    key = os.path.normcase(os.path.abspath(os.path.join(d, base + ".gpkg")))
    for start, warn in (("yesterday noon", True), ("2026-05-01 06:00", False), ("", False)):
        logs = []

        class _Run:
            cfg = {"temporal_animation": True, "add_to_project": True,
                   "temporal_breach_start": start}
            added = {key: lyr}

            def log(self, m):
                logs.append(m)
        lines = PX.temporal_step(_Run(), None, "", d)
        got = any("not understood" in m for m in logs)
        assert got == warn, (start, logs)
        nominal = any("nominal" in x for x in lines)
        assert nominal == (start != "2026-05-01 06:00"), (start, lines)


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", n, e)
    sys.exit(1 if fails else 0)
