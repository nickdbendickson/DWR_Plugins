"""Run the light tests (no AWA data, no HEC-HMS, no QGIS). Data-dependent scripts exit 77 (SKIP)
when their inputs are missing; run them from a working folder that holds those inputs."""
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
LIGHT = ["test_light.py", "test_qt6.py"]
DATA = ["validate_pothole.py", "validate_hydro.py", "test_cascade.py", "verify.py", "run_core.py",
        "test_sd_snow.py", "test_hms.py", "e2e_damsafety.py"]
todo = LIGHT + (DATA if "--all" in sys.argv else [])
env = dict(os.environ)
_proj = os.path.join(sys.prefix, "share", "proj")      # conda python run without 'conda activate'
if os.path.isdir(_proj):
    env.setdefault("PROJ_DATA", _proj); env.setdefault("PROJ_LIB", _proj)
bad = 0
for t in todo:
    r = subprocess.run([sys.executable, os.path.join(HERE, t)], capture_output=True, text=True, env=env)
    tag = "ok" if r.returncode == 0 else ("SKIP" if r.returncode == 77 else "FAIL")
    bad += tag == "FAIL"
    print("%-5s %s" % (tag, t))
    if tag == "FAIL":
        print(r.stdout[-1500:], r.stderr[-2500:])
print("%d scripts, %d failed" % (len(todo), bad))
sys.exit(1 if bad else 0)
