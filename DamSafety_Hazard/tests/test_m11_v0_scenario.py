# -*- coding: utf-8 -*-
"""M11: the 5.3(f) criteria row must say "not evaluated - no Vmax raster for this scenario"
(not "not met") for a 2D scenario that was zoned off the ACER curves at velocity 0, because
its borderline margins were never computed and the borderline rule is silently inert there.

Real PyQGIS / scipy are not importable in this sandbox, so this loads the real
classification.py with the heavy dependencies stubbed (same pattern as
test_m1m2_classify_nd.py) and exercises extra_criteria directly - it is pure Python.

    python3 DamSafety_Hazard/tests/test_m11_v0_scenario.py

Report-only: the fix changes only the 5.3(f) result string for v0 scenarios; zones, classes,
margins and counts are unchanged. A 2D scenario WITH velocity still reads "not met".
"""
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)


def _load_classification():
    qgis = types.ModuleType("qgis"); qgis.__path__ = []
    core = types.ModuleType("qgis.core")

    class _Dummy(object):
        def __init__(self, *a, **k):
            pass
    core.QgsField = core.QgsFeature = core.QgsCoordinateReferenceSystem = _Dummy
    sys.modules["qgis"] = qgis
    sys.modules["qgis.core"] = core

    pkg = types.ModuleType("DamSafety_Hazard"); pkg.__path__ = [HAZARD]
    sys.modules["DamSafety_Hazard"] = pkg

    compat = types.ModuleType("DamSafety_Hazard.compat")
    compat.WKB_NOGEOMETRY = 100
    compat.FT_STRING = "String"
    sys.modules["DamSafety_Hazard.compat"] = compat

    constants = types.ModuleType("DamSafety_Hazard.constants")
    constants.ND_POLICY_REF = "ND REG_05.2026 Section 5.2"
    constants.TM11_REF = "USBR ACER TM 11 (1988)"
    constants.ND_HIGH, constants.ND_MEDIUM, constants.ND_LOW = "High", "Medium", "Low"
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
    structures.OUT_FAILED = "Evaluation failed - verify"
    structures.TYPE_MOBILE = "Mobile home"
    structures.inventory_judgment = lambda s: []
    sys.modules["DamSafety_Hazard.structures"] = structures

    spec = importlib.util.spec_from_file_location(
        "DamSafety_Hazard.classification", os.path.join(HAZARD, "classification.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["DamSafety_Hazard.classification"] = mod
    spec.loader.exec_module(mod)
    return mod


CL = _load_classification()


def _struct(**kw):
    base = {"habitable": "Yes", "outcome": "Economic loss", "zone": 1, "max_vel": 3.0,
            "margin_high": 1.2, "margin_judg": 0.4, "near": "", "persons": 2}
    base.update(kw)
    return base


def _row(nd, section):
    return next(c for c in nd["criteria"] if c["section"] == section)


def _run(structs, cfg):
    nd = {"cls": "Low", "criteria": [], "reasons": [], "judgment": [], "struct_items": 0}
    CL.extra_criteria(nd, structs, None, cfg)
    return nd


def test_m11_v0_scenario_row_says_not_evaluated():
    # 2D run (one_d False), structures zoned off the chart at velocity 0: no Vmax -> max_vel None,
    # margins NULL, no borderline structure found.
    structs = [_struct(max_vel=None, margin_high=None, margin_judg=None, zone=1),
               _struct(max_vel=None, margin_high=None, margin_judg=None, zone=2)]
    nd = _run(structs, {"near_ft": 0.5, "borderline_higher": True})
    row = _row(nd, "5.3(f)")
    assert row["result"].startswith("not evaluated - no Vmax raster"), row
    # report-only: nothing was counted or moved
    assert row["found"] == "0", row
    assert nd.get("borderline_moved", 0) == 0, nd
    assert nd["struct_items"] == 0, nd


def test_m11_2d_with_velocity_still_not_met():
    # a real 2D scenario WITH velocity and no borderline structure still reads "not met"
    structs = [_struct(), _struct(zone=2, margin_high=0.9)]
    nd = _run(structs, {"near_ft": 0.5})
    assert _row(nd, "5.3(f)")["result"] == "not met", _row(nd, "5.3(f)")


def test_m11_1d_run_unaffected():
    # a depth-only (1D) run already applies 5.3(f) through its own branch; the v0 relabel must
    # not fire there even if max_vel is None.
    structs = [_struct(max_vel=None, margin_high=None, zone=1)]
    nd = _run(structs, {"near_ft": 0.5, "one_d": True})
    assert _row(nd, "5.3(f)")["result"] == "not met", _row(nd, "5.3(f)")


if __name__ == "__main__":
    test_m11_v0_scenario_row_says_not_evaluated()
    test_m11_2d_with_velocity_still_not_met()
    test_m11_1d_run_unaffected()
    print("test_m11_v0_scenario: all tests passed")
