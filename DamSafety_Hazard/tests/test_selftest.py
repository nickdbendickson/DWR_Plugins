"""The in-plugin self-test (gear menu > Run self-test: the real pipeline on synthetic data)
must pass; audit 2026-10-19 H1 / M10 found it failing on main and not run in CI.

Real QGIS only (it builds layers, rasters and a print layout):
    QT_QPA_PLATFORM=offscreen python DamSafety_Hazard/tests/test_selftest.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)                     # never the engineer's settings
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = tempfile.mkdtemp(prefix="hazard_selftest_cfg_")


def main():
    try:
        from qgis.core import QgsApplication
    except ImportError:
        if os.environ.get("CI"):
            print("FAIL: no PyQGIS in CI - the self-test must run there")
            return 1
        print("SKIP: no PyQGIS (the self-test needs real QGIS)")
        return 0
    app = QgsApplication([], False)
    app.initQgis()
    from DamSafety_Hazard.selftest import run_self_test
    ok, lines = run_self_test()
    fails = [ln for ln in lines if ln.startswith("FAIL")]
    passes = [ln for ln in lines if ln.startswith("PASS")]
    for ln in fails:
        print(ln)
    print("self-test: {} passed, {} failed -> {}".format(len(passes), len(fails),
                                                        "OK" if ok else "FAILED"))
    return 0 if ok and not fails else 1


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(rc)              # skip QGIS teardown (it can crash on exit in CI)
