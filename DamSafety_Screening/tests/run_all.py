"""Run every Screening test script (tests/test_*.py) in its own Python process and report
ok / SKIP / FAIL per script. No QGIS needed: the scripts use tests/qgis_mock.

    python3 DamSafety_Screening/tests/run_all.py            (from anywhere)

A script that prints a 'SKIP:' line and exits 0 is counted as skipped (an optional
dependency such as scipy is missing). A script that passes with a SKIP line in the middle of
its output is shown as "ok*" with that line. The Screening <-> Runner parity checks
(tests/_runner.py) FAIL instead of skipping when the Runner is missing in CI."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    names = sorted(n for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py"))
    counts = {"ok": 0, "SKIP": 0, "FAIL": 0}
    partly = []
    for name in names:
        env = dict(os.environ)
        env.pop("NDSUITE_SETTINGS", None)          # never the engineer's real settings file
        p = subprocess.run([sys.executable, os.path.join(HERE, name)], cwd=HERE, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.stdout or ""
        state = "FAIL" if p.returncode else ("SKIP" if "SKIP:" in out and out.strip().startswith("SKIP:") else "ok")
        counts[state] += 1
        # audit F-02: a part skipped mid-script is shown (it used to read as a plain "ok")
        skipped = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("SKIP")]
        if state == "ok" and skipped:
            partly.append(name)
        print("{:5} {}".format(state + ("*" if state == "ok" and skipped else ""), name))
        if state == "ok" and skipped:
            print("      " + "\n      ".join(skipped[:3]))
        if state == "FAIL":
            # Full output on failure: a short tail can hide the traceback entirely.
            print("      " + "\n      ".join(out.strip().splitlines()))
    print("\n{} scripts: {ok} ok, {SKIP} skipped, {FAIL} failed".format(len(names), **counts) +
          ("; {} ok* with a part skipped: {}".format(len(partly), ", ".join(partly))
           if partly else ""))
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
