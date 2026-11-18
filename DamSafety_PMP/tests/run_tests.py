"""Run the light tests (no AWA data, no HEC-HMS, no network). Data-dependent scripts exit 77 (SKIP)
when their inputs are missing; run them from a working folder that holds those inputs.
test_atlas14_qgis.py runs the Atlas 14 tools in real QGIS when there is one, else it is a SKIP."""
import os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
LIGHT = ["test_light.py", "test_vector_round.py", "test_qt6.py", "test_streamstats.py",
         "test_atlas14_shared.py", "test_atlas14_core.py", "test_atlas14_grid.py", "test_atlas14_chain.py",
         "test_atlas14_qgis.py"]
DATA = ["validate_pothole.py", "validate_hydro.py", "test_cascade.py", "verify.py", "run_core.py",
        "test_sd_snow.py", "test_hms.py", "e2e_damsafety.py"]
todo = LIGHT + (DATA if "--all" in sys.argv else [])
env = dict(os.environ, PYTHONUNBUFFERED="1")      # a script that hangs still shows how far it got
_proj = os.path.join(sys.prefix, "share", "proj")      # conda python run without 'conda activate'
if os.path.isdir(_proj):
    env.setdefault("PROJ_DATA", _proj); env.setdefault("PROJ_LIB", _proj)
bad = 0
LIMIT_S = 300                      # one script; a hang is named as a FAIL instead of stalling the CI job


def run_one(path):
    """(returncode or None on a hang, stdout, stderr). The script runs in its own process group, so a hang
    kills it and anything it started (a child left holding the output pipe would block the read)."""
    kw = {"start_new_session": True} if os.name == "posix" else {}
    p = subprocess.Popen([sys.executable, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         env=env, **kw)
    try:
        out, err = p.communicate(timeout=LIMIT_S)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            import signal
            os.killpg(p.pid, signal.SIGKILL)
        else:
            p.kill()
        try:
            out, err = p.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", "(no output could be read after the kill)"
        return None, out or "", err or ""


for t in todo:
    rc, out, err = run_one(os.path.join(HERE, t))
    tag = "ok" if rc == 0 else ("SKIP" if rc == 77 else "FAIL")
    bad += tag == "FAIL"
    print("%-5s %s%s" % (tag, t, " (no result after %d s)" % LIMIT_S if rc is None else ""), flush=True)
    if tag == "FAIL":
        print(out[-1500:], err[-2500:], flush=True)
print("%d scripts, %d failed" % (len(todo), bad))
sys.exit(1 if bad else 0)
