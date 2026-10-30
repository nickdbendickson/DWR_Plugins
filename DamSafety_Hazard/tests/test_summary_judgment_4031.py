"""Hazard 2.21.1 (DWR-4031 review): the summary's "Without judgment-zone items" line
gives the ND class as well as TM 11's, and the 5.3(f) borderline rule is cited only
when the borderline count is above 0. Synthetic structures, no rasters.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_summary_judgment_4031.py
"""
import copy
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

from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import formfill as FF  # noqa: E402
from DamSafety_Hazard.constants import CLASS_JUDGMENT, ND_HIGH, ND_MEDIUM  # noqa: E402
from DamSafety_Hazard.structures import OUT_JUDGMENT, OUT_ECONOMIC, OUT_DRY  # noqa: E402

HOUSE = "House on foundation"


def _structs(near=None):
    """Like DWR-4031: one judgment-zone house 0.053 ft past the ACER line, one more wet
    house (5.2.2) and one dry house cut off (5.2.1.2)."""
    j = {"id": 16775, "habitable": "Yes", "acer_type": HOUSE, "persons": 3,
         "zone": CLASS_JUDGMENT, "outcome": OUT_JUDGMENT, "margin_judg": -0.053,
         "damage_usd": 60000.0, "max_depth": 2.11}
    if near:
        j["near"] = near
    e = {"id": 2, "habitable": "Yes", "acer_type": HOUSE, "persons": 3, "zone": 3,
         "outcome": OUT_ECONOMIC, "damage_usd": 43090.0, "max_depth": 0.8}
    c = {"id": 7937, "habitable": "Yes", "acer_type": HOUSE, "persons": 3, "zone": None,
         "outcome": OUT_DRY, "cut_off": True}
    return [j, e, c]


def _classify(structs):
    nd = CL.classify_nd(structs, [], [], judgment_as_probable=True)
    tm = CL.classify_tm11(structs, [], judgment_as_probable=True)
    CL.extra_criteria(nd, structs, {}, {})
    return nd, tm


def _text(nd):
    return "\n".join(nd["reasons"] + nd["judgment"])


def test_no_borderline_no_5_3f():
    structs = _structs()
    nd, tm = _classify(structs)
    assert nd["cls"] == ND_HIGH and nd.get("provisional"), nd
    row = [c for c in nd["criteria"] if c["section"] == "5.3(f)"][0]
    assert row["found"] == "0" and row["result"] == "not met", row
    assert "5.3(f)" not in _text(nd), _text(nd)
    assert "judgment zone is counted as probable loss of life" in nd["reasons"][0]
    assert "(conservative judgment-zone setting)" in _text(nd)
    print("ok no borderline -> no 5.3(f) citation")


def test_borderline_cites_5_3f():
    nd, _tm = _classify(_structs(near="High"))
    row = [c for c in nd["criteria"] if c["section"] == "5.3(f)"][0]
    assert row["found"] == "1", row
    t = _text(nd)
    assert "borderline goes to the higher class, 5.3(f): 1 borderline structure(s)" in t, t
    assert "(conservative judgment-zone setting; borderline rule 5.3(f))" in t, t
    print("ok borderline count 1 -> 5.3(f) cited")


def test_nd_without_judgment_is_medium():
    structs = _structs()
    before = copy.deepcopy(structs)
    ndw = CL.nd_class_without_judgment(structs, [], [], {}, {})
    assert ndw == ND_MEDIUM, ndw
    assert structs == before                        # nothing passed in is changed
    nd, tm = _classify(structs)
    assert str(tm["cls_without_judgment"]).lower() == "low", tm
    nd["cls_without_judgment"] = ndw
    L = CL.summary_lines(nd, tm, structs, [], [], "D4031", [], False)
    line = [x for x in L if x.strip().startswith("Without judgment-zone items:")]
    assert line == ["  Without judgment-zone items: {} (TM 11 Table 1); ND class: MEDIUM".format(
        tm["cls_without_judgment"])], line
    # without the helper's value (older callers) only the TM 11 class, labelled
    nd2 = dict(nd)
    nd2.pop("cls_without_judgment")
    assert CL.without_judgment_text(nd2, tm) == "{} (TM 11 Table 1)".format(
        tm["cls_without_judgment"])
    # no judgment zone at all: the ND class is the class
    assert CL.nd_class_without_judgment(structs[1:], [], [], {}, {}) == ND_MEDIUM
    print("ok ND without judgment-zone items = Medium, TM 11 = Low, both labelled")
    return L


def test_formfill_reads_both(lines):
    d = tempfile.mkdtemp(prefix="hz4031_")
    try:
        with open(os.path.join(d, "Hazard_Classification_Summary.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        v = FF.available_values(d)
        assert v["TM11_WITHOUT_JUDGMENT"].lower() == "low", v["TM11_WITHOUT_JUDGMENT"]
        assert v["ND_WITHOUT_JUDGMENT"] == "Medium", v["ND_WITHOUT_JUDGMENT"]
        with open(os.path.join(d, "Hazard_Classification_Summary.txt"), "w") as f:
            f.write("\n".join(x.replace(" (TM 11 Table 1); ND class: MEDIUM", "")
                              for x in lines) + "\n")     # a 2.21.0 summary
        v = FF.available_values(d)
        assert v["TM11_WITHOUT_JUDGMENT"].lower() == "low" and v["ND_WITHOUT_JUDGMENT"] is None
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok formfill: TM11_WITHOUT_JUDGMENT and ND_WITHOUT_JUDGMENT")


if __name__ == "__main__":
    test_no_borderline_no_5_3f()
    test_borderline_cites_5_3f()
    L = test_nd_without_judgment_is_medium()
    test_formfill_reads_both(L)
    print("all ok")
