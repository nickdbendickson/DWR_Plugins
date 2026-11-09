"""The suite's pack guard (suite_config.in_suite_pack) is the same in every plugin's copy.
Setup 1.0.14 hands over to the Runner's wizard, whose own guard is in
DamSafety_Runner/tests/test_suite_pack_guard.py. Runs without QGIS:
    python3 -m pytest DamSafety_Setup/tests"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)



def test_the_damsuite_copies_are_identical():
    """suite_config.py is shared: Core 1.0.0 makes every plugin's copy the one module."""
    import importlib
    mods = {id(importlib.import_module(name)) for name in (
        "DamSafety_Setup.damsuite.suite_config", "DamSafety_Runner.damsuite.suite_config",
        "DamSafety_Screening.damsuite.suite_config", "DamSafety_Hazard.damsuite.suite_config",
        "DamSafety_Setup.suite_config", "DamSafety_Core.damsuite.suite_config")}
    assert len(mods) == 1, mods
    assert hasattr(importlib.import_module("DamSafety_Setup.suite_config"), "in_suite_pack")
