# -*- coding: utf-8 -*-
"""M2 follow-up (finding [8]): classify_nd marks a run incomplete when a
structure / road / railroad layer was not evaluated (classification.classify_nd
-> nd["incomplete"]). history.make_snapshot must record it and portfolio.dam_row
must surface it in the batch / portfolio row, without changing the class value.

history.py and portfolio.py import without PyQGIS, so this runs in the sandbox:

    python3 DamSafety_Hazard/tests/test_m2_incomplete_surfaced.py

Fail-before / pass-after: before the fix the snapshot carried no "incomplete"
key and the portfolio row's Notes was empty for an incomplete run; the two
assertions below ("incomplete" in the snapshot, "INCOMPLETE (...)" in the row)
fail on the old code and pass on the new.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Hazard import history as HIS       # noqa: E402
from DamSafety_Hazard import portfolio as PF       # noqa: E402
from DamSafety_Hazard.constants import OUTPUT_FOLDER_NAME, SUBDIR_QA   # noqa: E402

_TM11 = {"cls": "Low", "lij": 0.0}


def _write_snapshot(incomplete):
    """A dam folder whose outputs/QA holds a Last_Run_Snapshot for an ND 'Low'
    run with the given incomplete list; returns the dam folder."""
    nd = {"cls": "Low", "reasons": ["r"], "incomplete": list(incomplete)}
    snap = HIS.make_snapshot(nd, _TM11, [], [], [], "EPSG:1", "MyDam", {}, {},
                             governing="PMF")
    dam = os.path.join(tempfile.mkdtemp(), "MyDam")
    qa = os.path.join(dam, OUTPUT_FOLDER_NAME, SUBDIR_QA)
    os.makedirs(qa)
    with open(os.path.join(qa, HIS.SNAPSHOT_FILE), "w", encoding="utf-8") as fh:
        json.dump(snap, fh)
    return dam, snap


def test_make_snapshot_records_incomplete():
    _dam, snap = _write_snapshot(["structures"])
    # the recorded run carries the incomplete layers (mirrors nd_hazard
    # history.make_entry) ...
    assert snap.get("incomplete") == ["structures"], snap.get("incomplete")
    # ... and the class value itself is untouched
    assert snap.get("nd") == "Low", snap.get("nd")


def test_make_snapshot_clean_run_is_empty():
    _dam, snap = _write_snapshot([])
    # a fully evaluated run records an empty list, never a marker
    assert snap.get("incomplete") == [], snap.get("incomplete")


def test_portfolio_row_shows_incomplete_marker():
    dam, _snap = _write_snapshot(["structures"])
    row = PF.dam_row(dam)
    # additive marker in the portfolio row, class value unchanged
    assert row["ND_Class"] == "Low", row["ND_Class"]
    assert "INCOMPLETE (structures)" in row["Notes"], row["Notes"]


def test_portfolio_row_clean_has_no_marker():
    dam, _snap = _write_snapshot([])
    row = PF.dam_row(dam)
    assert row["ND_Class"] == "Low", row["ND_Class"]
    assert "INCOMPLETE" not in row["Notes"], row["Notes"]


def test_portfolio_row_lists_several_layers():
    dam, _snap = _write_snapshot(["structures", "railroads"])
    row = PF.dam_row(dam)
    assert "INCOMPLETE (structures, railroads)" in row["Notes"], row["Notes"]


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS {}".format(t.__name__))
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print("FAIL {}: {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n{} passed, {} failed".format(len(tests) - failed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
