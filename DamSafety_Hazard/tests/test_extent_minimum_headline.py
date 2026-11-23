"""Absaraka audit (2026-11-22): a flood that left the model (the Runner's extent_is_minimum)
reaches the Hazard headline, not only a cut-off. A label only: no class changes.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_extent_minimum_headline.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import portfolio as PF  # noqa: E402


def _text(extra):
    nd = {"cls": "Low", "reasons": ["x"], "counts": {}, "criteria": None, "judgment": []}
    tm11 = {"cls": "Low", "lij": 0.0, "lij_structures_high": 0, "lij_structures_judgment": 0,
            "judgment_included": True, "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
            "cls_without_judgment": "Low", "notes": []}
    return "\n".join(CL.summary_lines(nd, tm11, [], [], [], "Dam", [], False, extra))


def test_minimum_extent_on_the_headline():
    t = _text({"extent_is_minimum": 37.2})
    head = [ln for ln in t.splitlines() if ln.startswith("ND HAZARD CLASSIFICATION")][0]
    assert "flood extent is a minimum (37% of the breach volume left the model)" in head, head
    f = PF.summary_facts(PF.parse_summary(t))
    assert f["nd"] == "Low" and "minimum" in f.get("nd_flags", ""), f
    assert "minimum" in [ln for ln in _text({"extent_is_minimum": True}).splitlines()
                         if ln.startswith("ND HAZARD")][0]
    # a cut-off already says incomplete; the minimum bit is not repeated beside it
    t = _text({"cut_off": {"reason": "time cap"}, "extent_is_minimum": 40.0})
    assert "incomplete: flood cut off (time cap)" in t and "is a minimum" not in t, t
    assert "minimum" not in _text({})


if __name__ == "__main__":
    test_minimum_extent_on_the_headline()
    print("ok")
    sys.stdout.flush()
    os._exit(0)          # skip the real-QgsApplication offscreen teardown (segfaults at shutdown)
