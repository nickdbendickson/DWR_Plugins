# -*- coding: utf-8 -*-
"""audit R2 (2026-11-07): a structure whose evaluation raises is not a dry structure.

Before: structures.evaluate_structures wrapped each structure in `except Exception as e:
problems.log(...)`; the record kept max_depth None, so apply_type / structure_outcome made it
OUT_DRY ("Not inundated") - out of the ND / TM 11 counts, no line in the report, and the only
trace was a QGIS-log warning nobody reads in a batch. A LOW could rest on a house the plugin
never looked at.

After: the record carries rec["eval_failed"] (the error), its ND outcome is OUT_FAILED
("Evaluation failed - verify"), its Notes start with "EVALUATION FAILED - verify: ...",
structures.evaluation_failures() counts them, and classification.classify_nd(...,
structs_failed=N) marks the class INCOMPLETE ("structures (N could not be evaluated)"), puts a
PROVISIONAL reason line first and a judgment item in, so no run with a failure presents a
clean LOW / MEDIUM. summary_lines shows the INCOMPLETE marker on the headline class line.

Runs without QGIS: the stand-in qgis module of test_parcel_site_m3 is installed when real
PyQGIS is not importable; the depth sampler and the geometries are small fakes (the structure
whose sampling raises is the one under test). With real PyQGIS present the same fakes are used
(evaluate_structures only needs .buffer / .boundingBox / .centroid of the geometry and
.cells_in_geometry / .values_at of the samplers), so the test runs in both places.

    python3 DamSafety_Hazard/tests/test_eval_failed_not_dry.py
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)
ROOT = os.path.dirname(HAZARD)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from test_parcel_site_m3 import MODE  # noqa: E402  (installs the stand-in qgis when needed)
from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import scenarios as SC  # noqa: E402

WET = (ST.OUT_PROBABLE, ST.OUT_JUDGMENT, ST.OUT_ECONOMIC)      # a counted, flooded outcome


# ------------------------------------------------------------------ fakes --
class _Point(object):
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _BBox(object):
    def __init__(self, x0, y0, x1, y1):
        self.b = (x0, y0, x1, y1)

    def xMinimum(self):
        return self.b[0]

    def yMinimum(self):
        return self.b[1]

    def xMaximum(self):
        return self.b[2]

    def yMaximum(self):
        return self.b[3]


class _Geom(object):
    """A square footprint: just what evaluate_structures asks of a QgsGeometry."""

    def __init__(self, x0, y0, x1, y1):
        self.b = (x0, y0, x1, y1)

    def buffer(self, d, _segs=8):
        x0, y0, x1, y1 = self.b
        return _Geom(x0 - d, y0 - d, x1 + d, y1 + d)

    def boundingBox(self):
        return _BBox(*self.b)

    def isNull(self):
        return False

    def isEmpty(self):
        return False

    def isMultipart(self):
        return False

    def centroid(self):
        x0, y0, x1, y1 = self.b
        return _Centroid(0.5 * (x0 + x1), 0.5 * (y0 + y1))


class _Centroid(object):
    def __init__(self, x, y):
        self.p = _Point(x, y)

    def asPoint(self):
        return self.p


class _Sampler(object):
    """A depth / velocity / arrival reader over a flat 1 ft grid: every cell wet at `value`.
    `boom` is a predicate on the region's bbox: when it is true the read raises (the
    structure under test)."""

    def __init__(self, value, boom=None, ok=True):
        self.ok, self.backend, self.crs, self.layer = ok, "numpy", None, None
        self.gt = (0.0, 1.0, 0.0, 100.0, 0.0, -1.0)
        self.value, self.boom = float(value), boom

    def cells_in_geometry(self, geom):
        bb = geom.boundingBox()
        if self.boom is not None and self.boom(bb):
            raise RuntimeError("synthetic sampler failure (bad geometry)")
        xs = np.arange(np.floor(bb.xMinimum()) + 0.5, np.ceil(bb.xMaximum()), 1.0)
        ys = np.arange(np.floor(bb.yMinimum()) + 0.5, np.ceil(bb.yMaximum()), 1.0)
        X, Y = np.meshgrid(xs, ys)
        return X.ravel(), Y.ravel(), np.full(X.size, self.value)

    def values_at(self, xs, ys):
        return np.full(np.shape(xs), self.value)


def _rec(sid, x0, y0, habitable="Yes", persons=3):
    """A habitable structure record as build_inventory leaves one (the keys
    evaluate_structures, apply_type, structure_notes and classify_nd read)."""
    return {"id": sid, "geom": _Geom(x0, y0, x0 + 10.0, y0 + 10.0), "footprint": True,
            "habitable": habitable, "category": ST.CAT_DWELLING, "acer_type": ST.TYPE_HOUSE,
            "persons": persons, "families": {"FEMA_STRUCTURES"}, "sources": [],
            "type_conflict": False, "lifeline": False, "sqft": None, "occupancy": "",
            "type_basis": "", "persons_basis": "", "address_note": "", "site_note": "",
            "fema_category": "", "fd_id": None, "area_ft2": 1000.0}


def _evaluate(structs, boom):
    samplers = {"depth": _Sampler(4.0, boom=boom), "vel": _Sampler(1.0, ok=False),
                "arr": _Sampler(0.5, ok=False)}
    ST.evaluate_structures(structs, samplers, analysis_crs=None, transform_ctx=None,
                           lat=None, sample_area=ST.SAMPLE_AREA)
    return structs


# ------------------------------------------------------------------ tests --
def test_failed_structure_is_marked_not_dry():
    # two houses, 4 ft deep everywhere; the second one's read raises
    structs = [_rec(1, 10.0, 10.0), _rec(2, 60.0, 60.0)]
    _evaluate(structs, boom=lambda bb: bb.xMinimum() > 50.0)
    good, bad = structs
    assert good.get("eval_failed") is None and good["max_depth"] == 4.0, good
    assert good["outcome"] in WET, good["outcome"]   # 4 ft deep: wet, counted
    assert bad.get("eval_failed"), bad.get("eval_failed")
    assert "synthetic sampler failure" in bad["eval_failed"], bad["eval_failed"]
    assert bad["max_depth"] is None
    # the whole point: never OUT_DRY
    assert bad["outcome"] == ST.OUT_FAILED, bad["outcome"]
    assert bad["outcome"] != ST.OUT_DRY
    # the count the pipeline reads
    failed = ST.evaluation_failures(structs)
    assert [r["id"] for r in failed] == [2], failed
    # the Notes text that reaches the structures layer / spreadsheet, first
    notes = ST.structure_notes(bad)
    assert notes and notes[0].startswith("EVALUATION FAILED - verify: "), notes
    assert "synthetic sampler failure" in notes[0], notes
    assert not any(n.startswith("EVALUATION FAILED") for n in ST.structure_notes(good))


def test_apply_type_keeps_the_failed_outcome():
    """Stage B re-applies the type / settings from the snapshot without the rasters: the
    marker must survive the snapshot and apply_type must keep OUT_FAILED."""
    structs = [_rec(1, 10.0, 10.0), _rec(2, 60.0, 60.0)]
    _evaluate(structs, boom=lambda bb: bb.xMinimum() > 50.0)
    assert "eval_failed" in SC.EVAL_KEYS and "eval_failed" in SC.SNAP_KEYS
    snap = SC.snapshot(structs)
    assert snap[2]["eval_failed"], snap[2]
    fresh = [_rec(1, 10.0, 10.0), _rec(2, 60.0, 60.0)]
    SC.restore(fresh, snap)
    for rec in fresh:
        ST.apply_type(rec, False, 1.0, False)
    assert fresh[1]["outcome"] == ST.OUT_FAILED, fresh[1]["outcome"]
    assert fresh[0]["outcome"] in WET, fresh[0]["outcome"]
    # a re-evaluation that succeeds clears the marker (the _RESET)
    _evaluate(fresh, boom=None)
    assert fresh[1].get("eval_failed") is None and fresh[1]["outcome"] in WET


def test_classify_nd_marks_the_class_incomplete_and_provisional():
    structs = [_rec(1, 10.0, 10.0), _rec(2, 60.0, 60.0)]
    _evaluate(structs, boom=lambda bb: bb.xMinimum() > 50.0)
    nd = CL.classify_nd(structs, [], [], structs_failed=len(ST.evaluation_failures(structs)))
    # the good house (4 ft, judgment zone at velocity 0) makes it MEDIUM by the ND rule; the
    # failed one does not raise the class, it marks it incomplete
    assert nd["cls"] == "Medium", nd["cls"]
    assert nd["structs_failed"] == 1, nd
    assert "structures (1 could not be evaluated)" in nd["incomplete"], nd["incomplete"]
    assert nd["reasons"][0].startswith(CL.PROVISIONAL_PREFIX), nd["reasons"]
    assert "could not be evaluated" in nd["reasons"][0]
    # governing_reason skips the provisional line (formfill reads a criterion)
    assert CL.governing_reason(nd["reasons"]).startswith("MEDIUM -"), nd["reasons"]
    assert any("could not be evaluated" in j and "NOT counted as dry" in j
               for j in nd["judgment"]), nd["judgment"]
    rows = {c["criterion"]: c["found"] for c in nd["criteria"]}
    assert rows["Habitable structures inundated (economic loss)"].endswith(
        "; 1 not evaluated"), rows
    assert rows["Probable loss of life - habitable structures in the ACER high danger zone"] \
        .endswith("; 1 not evaluated"), rows
    # the headline line of the summary never reads as a clean class
    lines = CL.summary_lines(nd, {"cls": "Low", "lij": 0.0, "lij_structures_high": 0,
                                  "lij_structures_judgment": 0, "judgment_included": False,
                                  "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
                                  "notes": []}, structs, [], [], "Dam", [], False)
    head = [x for x in lines if x.startswith("ND HAZARD CLASSIFICATION")][0]
    assert "INCOMPLETE (structures (1 could not be evaluated))" in head, head


def test_classify_nd_a_lone_failed_structure_is_not_a_clean_low():
    """One structure, its evaluation raised: before the fix this was a clean LOW."""
    structs = [_rec(2, 60.0, 60.0)]
    _evaluate(structs, boom=lambda bb: True)
    nd = CL.classify_nd(structs, [], [])          # the records carry the marker themselves
    assert nd["cls"] == "Low", nd["cls"]
    assert nd["structs_failed"] == 1 and nd["incomplete"], nd
    assert nd["reasons"][0].startswith(CL.PROVISIONAL_PREFIX), nd["reasons"]
    assert any(r.startswith("LOW -") for r in nd["reasons"]), nd["reasons"]


def test_classify_nd_clean_run_is_unchanged():
    structs = [_rec(1, 10.0, 10.0), _rec(2, 60.0, 60.0)]
    _evaluate(structs, boom=None)
    nd = CL.classify_nd(structs, [], [], structs_failed=0)
    assert nd["structs_failed"] == 0 and nd["incomplete"] == [], nd
    assert not any(r.startswith(CL.PROVISIONAL_PREFIX) for r in nd["reasons"]), nd["reasons"]
    assert not any("could not be evaluated" in j for j in nd["judgment"]), nd["judgment"]
    rows = {c["criterion"]: c["found"] for c in nd["criteria"]}
    assert "not evaluated" not in rows["Habitable structures inundated (economic loss)"], rows


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok     {}".format(t.__name__))
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print("FAIL   {}: {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n{} tests, {} failed (qgis: {})".format(len(tests), failed, MODE))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
