"""Runs DamSafety Core's unit-system tests with the Hazard suite, which CI runs in real QGIS
(Core has no test job of its own): the pure-Python tests of damsuite.display_units, and the
real-QGIS check that a Processing tool leaves SI copies of its result tables (Core 1.4.0).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_core_si_copies.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPTS = (os.path.join(ROOT, "DamSafety_Core", "tests", "test_display_units.py"),
           os.path.join(ROOT, "DamSafety_Core", "tests", "qgis_si_copies_check.py"))


def main():
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    failed = 0
    for script in SCRIPTS:
        proc = subprocess.run([sys.executable, script], env=env, cwd=ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
        text = proc.stdout.decode("utf-8", "replace")
        ok = proc.returncode == 0
        print(("ok    " if ok else "FAIL  ") + os.path.relpath(script, ROOT))
        if not ok:
            failed += 1
            print(text[-4000:])
    print("{} failed".format(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
