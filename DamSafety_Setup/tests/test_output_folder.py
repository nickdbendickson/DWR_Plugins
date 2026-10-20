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
    """suite_config.py is shared: every plugin's copy carries the same pack guard."""
    paths = [os.path.join(ROOT, p, "damsuite", "suite_config.py")
             for p in ("DamSafety_Setup", "DamSafety_Runner", "DamSafety_Screening",
                       "DamSafety_Hazard")]
    paths.append(os.path.join(ROOT, "DamSafety_Setup", "suite_config.py"))
    texts = {open(p).read() for p in paths if os.path.isfile(p)}
    assert len(texts) == 1 and "def in_suite_pack" in texts.pop()
