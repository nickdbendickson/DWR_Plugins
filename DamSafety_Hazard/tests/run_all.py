"""Run every Hazard test script (tests/test_*.py) in its own Python process and report
ok / FAIL per script, then exit non-zero if any failed.

    python3 DamSafety_Hazard/tests/run_all.py            (from the repo root or anywhere)

One process per script so a real QgsApplication built by one test cannot disturb the next.
This replaces the inline CI shell loop: a single Python entry point whose own exit status is
the job's result matches the Screening/Runner steps and sidesteps a micromamba `bash -el`
quirk where an explicit `exit $fail` in the step did not propagate even when every script
exited 0 (the loop reported [rc=0] for all 27 scripts yet the step still exited 1)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    names = sorted(n for n in os.listdir(HERE) if n.startswith("test_") and n.endswith(".py"))
    failed = []
    for name in names:
        env = dict(os.environ)
        env.pop("NDSUITE_SETTINGS", None)          # never the engineer's real settings file
        p = subprocess.run([sys.executable, os.path.join(HERE, name)], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out = p.stdout or ""
        state = "FAIL" if p.returncode else "ok"
        print("{:4} {}  (rc={})".format(state, name, p.returncode), flush=True)
        if p.returncode:
            failed.append(name)
            print("      " + "\n      ".join(out.strip().splitlines()[-12:]), flush=True)
    print("\n{} scripts: {} ok, {} failed".format(len(names), len(names) - len(failed), len(failed)),
          flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
