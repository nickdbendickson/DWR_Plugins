"""Hazard 2.22.0 (Nick, 10/30/2026): "any house in the judgement zone should be
automatically labeled as medium hazard". A habitable structure in the ACER judgment zone
makes the dam ND MEDIUM by rule - not probable loss of life, not a provisional High.
Houses in the high danger zone still make it High. TM 11 is not changed. The old
setting (judgment zone counted as probable loss of life, High) stays as an option that
is off by default. Synthetic structures only (depth / velocity of the two review houses,
no DWR data, no rasters).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_judgment_zone_medium_rule.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import pipeline as PL  # noqa: E402
from DamSafety_Hazard import robustness as ROB  # noqa: E402
from DamSafety_Hazard.damsuite import tm11_curves as TC  # noqa: E402
from DamSafety_Hazard.constants import (CLASS_HIGH, CLASS_JUDGMENT, ND_HIGH,  # noqa: E402
                                        ND_MEDIUM)
from DamSafety_Hazard.structures import OUT_JUDGMENT, OUT_PROBABLE  # noqa: E402

HOUSE = "House on foundation"


def _house(sid, depth, vel, persons=3):
    """A habitable house at (depth ft, velocity ft/s), zone and margins from the ACER
    house chart the plugin uses."""
    z = TC.zone("house", depth, vel)
    lo, hi = TC.thresholds("house", vel)
    rec = {"id": sid, "habitable": "Yes", "acer_type": HOUSE, "persons": persons,
           "max_depth": depth, "max_vel": vel, "margin_judg": lo - depth,
           "margin_high": hi - depth}
    if z == TC.HIGH:
        rec.update(zone=CLASS_HIGH, outcome=OUT_PROBABLE)
    elif z == TC.JUDGMENT:
        rec.update(zone=CLASS_JUDGMENT, outcome=OUT_JUDGMENT)
    else:
        raise AssertionError("not wet in a High/Judgment zone: {}".format(rec))
    return rec


# the two review houses (depth / velocity only)
H2801 = (4010, 3.10, 0.11)     # DWR-2801 Absaraka r4, 82 15th Ave N
H4031 = (16775, 2.11, 6.95)    # DWR-4031 Washek toe run


def _result_row(nd):
    return [c for c in nd["criteria"] if c["section"] == "Result"][0]["result"]


def _jz_row(nd):
    return [c for c in nd["criteria"]
            if c["criterion"] == "Habitable structures in the ACER judgment zone"][0]


def _run(structs, **kw):
    nd = CL.classify_nd(structs, [], [], **kw)
    CL.extra_criteria(nd, structs, {}, {})
    return nd


def test_review_houses_are_in_the_judgment_zone():
    for sid, d, v in (H2801, H4031):
        assert TC.zone("house", d, v) == TC.JUDGMENT, (sid, d, v, TC.thresholds("house", v))
    print("ok 4010 (3.10 ft, 0.11 ft/s) and 16775 (2.11 ft, 6.95 ft/s) are judgment zone")


def test_default_is_medium_by_rule():
    for sid, d, v in (H2801, H4031):
        s = [_house(sid, d, v)]
        nd = _run(s)
        assert nd["cls"] == ND_MEDIUM, (sid, nd["cls"], nd["reasons"])
        assert not nd.get("provisional"), nd
        assert _result_row(nd) == "MEDIUM", _result_row(nd)
        assert _jz_row(nd)["result"] == ND_MEDIUM + " (by rule)", _jz_row(nd)
        assert nd["judgment_rule"] == "medium"
        text = "\n".join(nd["reasons"] + nd["judgment"])
        assert "classed MEDIUM by rule" in text, text
        assert "HIGH" not in "\n".join(nd["reasons"]), nd["reasons"]
        assert "provisional" not in text.lower(), text
        assert "would make the dam HIGH" not in text, text
        assert "5.3(f)" not in text, text            # 0 borderline: no 5.3(f) citation
        print("ok {} -> ND {} ({})".format(sid, nd["cls"].upper(), nd["reasons"][0]))


def test_tm11_unchanged():
    s = [_house(*H2801)]
    tm = CL.classify_tm11(s, [])                      # the TM 11 default is unchanged
    assert tm["judgment_included"] is True
    assert tm["lij_structures_judgment"] == 3, tm
    assert str(tm["cls"]).lower() == "significant", tm["cls"]
    print("ok TM 11 still counts judgment-zone persons: {} (LIJ {})".format(tm["cls"],
                                                                       tm["lij"]))


def test_high_zone_still_high():
    s = [_house(1, 6.0, 6.0), _house(*H2801)]
    assert s[0]["zone"] == CLASS_HIGH
    nd = _run(s)
    assert nd["cls"] == ND_HIGH and not nd.get("provisional"), nd
    assert nd["reasons"][0].startswith("HIGH - 1 habitable structure(s) with lethal"), nd
    print("ok high danger zone house still makes the dam HIGH")


def test_old_option_still_available():
    s = [_house(*H2801)]
    nd = _run(s, judgment_as_probable=True)
    assert nd["cls"] == ND_HIGH and nd.get("provisional"), nd
    assert _result_row(nd) == "HIGH (provisional)"
    assert nd["judgment_rule"] == "probable"
    print("ok old option (judgment zone = probable loss of life) still gives HIGH "
          "(provisional)")


def test_defaults_and_options():
    assert PL.DEFAULTS["nd_judgment_as_probable"] is False
    assert PL.DEFAULTS["judgment_as_probable"] is True        # TM 11 only
    cfg = PL.config()
    assert cfg["nd_judgment_as_probable"] is False
    # robustness measures to the ACER high-danger boundary by default
    s = [_house(*H2801)]
    rob = ROB.analyse(s, _run(s))
    assert any("high-danger boundary" in x for x in rob["lines"]) or rob["up"], rob
    print("ok defaults: ND judgment zone MEDIUM, TM 11 includes judgment-zone persons")


def test_summary_line():
    s = [_house(*H2801)]
    nd = _run(s)
    tm = CL.classify_tm11(s, [])
    L = CL.summary_lines(nd, tm, s, [], [], "synthetic", [], False)
    blob = "\n".join(L)
    assert "classed MEDIUM by rule" in blob, blob
    assert "HIGH is provisional" not in blob and "HIGH (provisional)" not in blob, blob
    print("ok summary says MEDIUM by rule, no provisional HIGH")


if __name__ == "__main__":
    test_review_houses_are_in_the_judgment_zone()
    test_default_is_medium_by_rule()
    test_tm11_unchanged()
    test_high_zone_still_high()
    test_old_option_still_available()
    test_defaults_and_options()
    test_summary_line()
    print("all ok")
