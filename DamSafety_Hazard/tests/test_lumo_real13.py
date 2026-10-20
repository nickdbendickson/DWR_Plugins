"""Lumo vReal.13 PF.1 / PF.2 / RP.1 / RP.3 (Hazard 2.18.24).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real13.py
"""
import csv
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

from DamSafety_Hazard import portfolio as PF  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import reproduce as RP  # noqa: E402
from DamSafety_Hazard import structures as STR  # noqa: E402


def _writer_text(cls="High", unverified=2, cut=True):
    nd = {"cls": cls, "reasons": ["economic loss x"], "counts": {"unverified": unverified},
          "criteria": None, "judgment": []}
    tm11 = {"cls": "High", "lij": 7.0, "lij_structures_high": 7, "lij_structures_judgment": 0,
            "judgment_included": True, "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
            "cls_without_judgment": "High", "notes": []}
    extra = {"cut_off": {"reason": "time cap"}} if cut else {}
    return "\n".join(CL.summary_lines(nd, tm11, [], [], [], "Dam", [], False, extra))


def test_pf2_nd_class_without_suffix():
    text = _writer_text()
    assert "  |  " in text                      # the writer really adds the suffix
    f = PF.summary_facts(PF.parse_summary(text))
    assert f["nd"] == "High", f["nd"]
    assert "unverified" in f["nd_flags"] and "incomplete" in f["nd_flags"]
    f2 = PF.summary_facts(PF.parse_summary(_writer_text("Low", 0, False)))
    assert f2["nd"] == "Low" and "nd_flags" not in f2


def _csv(rows):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "Habitable_Structures.csv")
    keys = ["ND_Outcome", "Habitable", "Notes", "Reviewed"]
    with open(p, "w", newline="") as fh:
        w = csv.DictWriter(fh, keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in keys})
    return p


def test_pf1_mobile_notes_and_reviewed():
    notes = [STR.structure_notes(dict(category="Habitable", persons=2, sources=[], **kw))
             for kw in ({"mobile_rule": "shape"}, {"mobile_rule": "park_lot"},
                        {"mobile_occupied": True})]
    assert all(n for n in notes)
    rows = [{"ND_Outcome": "Probable loss of life", "Habitable": "Yes",
             "Notes": "; ".join(n) if isinstance(n, list) else str(n)} for n in notes]
    rows.append({"ND_Outcome": "Not inundated", "Habitable": "Yes", "Notes": "",
                 "Reviewed": "2026-10-15 engineer"})      # reviewed, no reason left
    assert PF.open_review_from_csv(_csv(rows)) == (3, 1)


def test_rp3_shared_guard():
    d = tempfile.mkdtemp()
    assert RP.work_folder_problem(d) is None
    assert RP.work_folder_problem(os.path.join(d, "new")) is None
    open(os.path.join(d, "x.txt"), "w").close()
    assert "not empty" in RP.work_folder_problem(d)
    try:
        RP._check_work_folder(d)
        raise AssertionError("not refused")
    except ValueError:
        pass
    open(os.path.join(d, RP.WORK_MARKER), "w").close()
    assert RP.work_folder_problem(d) is None


def test_rp1_unknown_settings_reported():
    from DamSafety_Hazard.pipeline import DEFAULTS
    k = next(iter(DEFAULTS))
    orig = RP._find_settings
    RP._find_settings = lambda f: ({"settings": {k: DEFAULTS[k], "zz_future_setting": 1},
                                    "scenarios": []}, None, False)
    try:
        try:
            plan = RP.plan_from_package(tempfile.mkdtemp(), tempfile.mkdtemp())
        except Exception as e:                       # a later step may refuse an empty plan
            raise AssertionError("plan failed: {}".format(e))
    finally:
        RP._find_settings = orig
    assert "zz_future_setting" not in plan["cfg"]
    assert any("zz_future_setting" in n for n in plan["notes"]), plan["notes"]


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
    sys.stdout.flush()
    if fails:
        sys.exit(1)
    # all passed; os._exit(0) before the real-QgsApplication offscreen teardown segfaults at shutdown
    os._exit(0)
