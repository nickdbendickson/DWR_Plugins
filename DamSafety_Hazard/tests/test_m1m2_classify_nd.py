# -*- coding: utf-8 -*-
"""M1 / M2 targeted tests for DamSafety_Hazard.classification.classify_nd.

Real PyQGIS / scipy are not importable in this sandbox, so this loads the real
classification.py with the heavy dependencies (qgis, constants, helpers,
structures, compat) stubbed. It exercises only classify_nd / _raise, which are
pure Python.

    python3 DamSafety_Hazard/tests/test_m1m2_classify_nd.py

M1: an empty inventory classifies as "Not classified" (not a key in ND_RANK);
    _raise must not KeyError on it, and the early return must carry "incomplete".
M2: classify_nd(..., structs_evaluated=False) must mark the habitable-structure
    rows "not evaluated", add the "class is a minimum and may be higher" judgment
    line, flag the LOW reason as "LOW SO FAR ...", and return incomplete=["structures"].
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)


def _load_classification():
    """Load the real classification.py with heavy deps stubbed."""
    # ---- fake qgis.core (QgsField/QgsFeature/QgsCoordinateReferenceSystem are
    #      imported at module top but unused by classify_nd) ----
    qgis = types.ModuleType("qgis")
    qgis.__path__ = []
    core = types.ModuleType("qgis.core")

    class _Dummy(object):
        def __init__(self, *a, **k):
            pass

    core.QgsField = _Dummy
    core.QgsFeature = _Dummy
    core.QgsCoordinateReferenceSystem = _Dummy
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = core

    # ---- stub the DamSafety_Hazard package so the real __init__.py (transform
    #      guard) does not run, but relative imports still resolve ----
    pkg = types.ModuleType("DamSafety_Hazard")
    pkg.__path__ = [HAZARD]
    sys.modules["DamSafety_Hazard"] = pkg

    compat = types.ModuleType("DamSafety_Hazard.compat")
    compat.WKB_NOGEOMETRY = 100
    compat.FT_STRING = "String"
    sys.modules["DamSafety_Hazard.compat"] = compat

    # constants: ND_RANK deliberately lacks "Not classified" (the real value),
    # which is exactly the M1 condition.
    constants = types.ModuleType("DamSafety_Hazard.constants")
    constants.ND_POLICY_REF = "ND REG_05.2026 Section 5.2"
    constants.TM11_REF = "USBR ACER TM 11 (1988)"
    constants.ND_HIGH = "High"
    constants.ND_MEDIUM = "Medium"
    constants.ND_LOW = "Low"
    constants.ND_RANK = {"Low": 0, "Medium": 1, "High": 2}
    constants.TM11_SIGNIFICANT_MAX_LIJ = 6
    constants.PLUGIN_NAME = "DamSafety Hazard"
    constants.PLUGIN_VERSION = "test"
    constants.CLASS_HIGH = 1
    constants.ND_AADT_LOW_BELOW = 500
    constants.CLASS_JUDGMENT = 2
    constants.vehicle_curve_available = lambda: False
    sys.modules["DamSafety_Hazard.constants"] = constants

    helpers = types.ModuleType("DamSafety_Hazard.helpers")
    helpers.create_memory_layer = lambda *a, **k: None
    helpers.export_layer_to_formats = lambda *a, **k: (True, None, {})
    helpers.prepare_output_path = lambda p: p
    sys.modules["DamSafety_Hazard.helpers"] = helpers

    structures = types.ModuleType("DamSafety_Hazard.structures")
    structures.OUT_PROBABLE = "Probable loss of life"
    structures.OUT_JUDGMENT = "ACER judgment zone"
    structures.OUT_ECONOMIC = "Economic loss"
    structures.OUT_DRY = "Dry"
    structures.OUT_TRACE = "Trace"
    structures.TYPE_MOBILE = "Mobile home"
    sys.modules["DamSafety_Hazard.structures"] = structures

    spec = importlib.util.spec_from_file_location(
        "DamSafety_Hazard.classification", os.path.join(HAZARD, "classification.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["DamSafety_Hazard.classification"] = mod
    spec.loader.exec_module(mod)
    return mod


CL = _load_classification()


def _road(nd_class="Medium", **kw):
    r = {"nd_class": nd_class, "nd_judgment": False, "name": "Main St",
         "aadt": 1000, "aadt_year": 2020, "road_class": "US", "length_ft": 100.0,
         "bridge": False}
    r.update(kw)
    return r


# ---------------------------------------------------------------- M1 ----------
def test_m1_raise_tolerates_not_classified():
    # the direct failure the bug caused: _raise with an unknown class must not
    # KeyError, and must still raise to the real class
    assert CL._raise("Not classified", "Medium") == "Medium"
    assert CL._raise("Not classified", "High") == "High"
    # a known class still wins over the unknown "floor"
    assert CL._raise("High", "Medium") == "High"
    assert CL._raise("Low", "Medium") == "Medium"


def test_m1_empty_inventory_returns_not_classified_with_incomplete():
    nd = CL.classify_nd([], [], [])
    assert nd["cls"] == "Not classified"
    # schema parity with the main path: every return now carries "incomplete"
    assert "incomplete" in nd
    # feeding the result back through _raise (as facility_criteria does when a
    # lifeline is flooded) no longer crashes
    assert CL._raise(nd["cls"], "Medium") == "Medium"


# ---------------------------------------------------------------- M2 ----------
def test_m2_structs_not_evaluated_marks_minimum_and_incomplete():
    # structures missing / failed, but a road was evaluated -> not the empty path
    nd = CL.classify_nd([], [_road()], [], structs_evaluated=False)
    # the safety-transparency judgment line
    assert any("the class is a minimum and may be higher" in j for j in nd["judgment"]), \
        nd["judgment"]
    # incomplete list reports structures (roads/rails were evaluated)
    assert nd["incomplete"] == ["structures"], nd["incomplete"]
    # the five habitable-structure criteria rows read "not evaluated" / "?"
    struct_rows = [c for c in nd["criteria"]
                   if c["criterion"].startswith("Probable loss of life")
                   or c["criterion"].startswith("Habitable structures")
                   or c["criterion"].startswith("Possible lifeline")
                   or c["criterion"].startswith("Unverified structures")]
    assert len(struct_rows) == 5, struct_rows
    for c in struct_rows:
        assert c["found"] == "not evaluated", c
        assert c["result"] == "?", c


def test_m2_low_so_far_reason_when_structs_not_evaluated():
    # no roads/rails impact and no structures evaluated -> "LOW SO FAR" wording
    nd = CL.classify_nd([], [_road(nd_class="Low", aadt=100, road_class="local")], [],
                        structs_evaluated=False)
    low = [r for r in nd["reasons"] if r.startswith("LOW")]
    assert low and "LOW SO FAR" in low[0] and "NOT evaluated" in low[0], nd["reasons"]


def test_m2_default_true_preserves_old_behaviour():
    # a Low road (no class impact) so the LOW reason is emitted; structs default-evaluated
    nd = CL.classify_nd([], [_road(nd_class="Low", aadt=100, road_class="local")], [])
    assert nd["incomplete"] == []                        # nothing missing
    assert not any("habitable-structure criteria" in j for j in nd["judgment"])
    # the structure rows now show real counts, not "not evaluated"
    prob = next(c for c in nd["criteria"]
                if c["criterion"].startswith("Probable loss of life"))
    assert prob["found"] == "0 (0 persons)", prob
    assert prob["result"] == "not met", prob
    low = [r for r in nd["reasons"] if r.startswith("LOW")]
    assert low and "LOW SO FAR" not in low[0], nd["reasons"]


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("PASS {}".format(t.__name__))
        except Exception as e:  # noqa
            failed += 1
            import traceback
            print("FAIL {}: {}".format(t.__name__, e))
            traceback.print_exc()
    print("\n{} passed, {} failed".format(len(tests) - failed, failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
