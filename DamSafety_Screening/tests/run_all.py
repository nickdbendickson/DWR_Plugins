"""Run every Screening test script (tests/test_*.py) in its own Python process and report
ok / SKIP / FAIL per script. No QGIS needed: the scripts use tests/qgis_mock.

    python3 DamSafety_Screening/tests/run_all.py            (from anywhere)

A script that prints a 'SKIP:' line and exits 0 is counted as skipped (an optional
dependency such as scipy is missing)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    names = sorted(n for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py"))
    counts = {"ok": 0, "SKIP": 0, "FAIL": 0}
    for name in names:
        p = subprocess.run([sys.executable, os.path.join(HERE, name)], cwd=HERE,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.stdout or ""
        state = "FAIL" if p.returncode else ("SKIP" if "SKIP:" in out and out.strip().startswith("SKIP:") else "ok")
        counts[state] += 1
        print("{:5} {}".format(state, name))
        if state == "FAIL":
            print("      " + "\n      ".join(out.strip().splitlines()[-6:]))
    print("\n{} scripts: {ok} ok, {SKIP} skipped, {FAIL} failed".format(len(names), **counts))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
