#!/usr/bin/env python3
# suite_check.py v1.0 — run the dam-suite tests and runs locally in your conda envs,
# then (optionally) have the local model explain each failure. Stdlib only.
#
#   suite_check.py nd     --repo ~/Documents/DSHazard/nd_hazard          CI fast-checks + plugin suites
#   suite_check.py nd     --repo ... --models                            + LISFLOOD-FP/ANUGA model tests
#   suite_check.py dwr    --repo ~/Documents/DWR_Plugins                  same discovery on the DamSafety repo
#   suite_check.py runner --repo ... selftest|check|run CONFIG.json        Breach Runner without QGIS (ndbreach_cli)
#   suite_check.py crest  --repo ~/Documents/DSHazard/crest              npm ci, typecheck, lint, test
#   add --triage to send each failing step's log (and the code it points at) to the local model.
#
# The tests are the source of truth. The model only explains a failure you already have.
import os, sys, re, json, shutil, argparse, subprocess, time, urllib.request, urllib.error
from datetime import datetime

CHAT_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
MODEL = os.environ.get("HEIM_MODEL", "heim")
NUM_CTX = int(os.environ.get("HEIM_NUM_CTX", "16384"))
REPORT_DIR = os.path.expanduser("~/local-ai/reports")
DEFAULT_LISFLOOD = os.path.expanduser("~/nd-dam-suite/bin/lisflood")   # built by the Mac installer

ENV_HINTS = {
    "ndqgis": "conda create -n ndqgis -c conda-forge --override-channels --strict-channel-priority "
              "python=3.12 qgis=3.34 draco=1.5.7 'numpy<2' scipy",
    "ndanuga": "conda create -n ndanuga -c conda-forge --override-channels --strict-channel-priority "
               "python=3.12 anuga=4.0.1 gmsh python-gmsh scipy",
    "ndnode": "conda create -n ndnode -c conda-forge --override-channels nodejs=22 python=3.12",
}


# ---------------------------------------------------------------- conda glue
def conda_exe():
    for c in (os.environ.get("CONDA_EXE"), shutil.which("conda"),
              os.path.expanduser("~/miniconda3/bin/conda"), os.path.expanduser("~/miniconda/bin/conda"),
              os.path.expanduser("~/miniforge3/bin/conda")):
        if c and os.path.exists(c):
            return c
    sys.exit("conda not found. Open a terminal where `conda` works, or set CONDA_EXE.")


def env_prefix(conda, name):
    out = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True)
    try:
        envs = json.loads(out.stdout)["envs"]
    except (ValueError, KeyError):
        envs = []
    for p in envs:
        if os.path.basename(p) == name:
            return p
    hint = ENV_HINTS.get(name, "conda create -n %s ..." % name)
    sys.exit("conda env '%s' not found. Create it once:\n  %s" % (name, hint))


def in_env(conda, env, argv):
    """argv run inside a conda env with its activation scripts (GDAL_DATA, PROJ, Qt plugins)."""
    return [conda, "run", "--no-capture-output", "-n", env] + argv


# ------------------------------------------------------------------ stepping
class Steps:
    def __init__(self, label):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = os.path.join(REPORT_DIR, "suite_%s_%s" % (label, stamp))
        os.makedirs(self.dir, exist_ok=True)
        self.results = []          # (name, rc, seconds, log)

    def run(self, name, argv, cwd, env=None, timeout=3600):
        log = os.path.join(self.dir, re.sub(r"[^A-Za-z0-9_.-]+", "_", name) + ".log")
        print("== %s" % name, flush=True)
        t0 = time.time()
        e = dict(os.environ)
        e.update(env or {})
        e.pop("PYTHONPATH", None)      # a shell PYTHONPATH has broken the ND tools before
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("$ (cd %s && %s)\n\n" % (cwd, " ".join(argv)))
            fh.flush()
            try:
                p = subprocess.Popen(argv, cwd=cwd, env=e, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace")
                for line in p.stdout:
                    fh.write(line)
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                rc = 124
                fh.write("\n[TIMEOUT after %ds]\n" % timeout)
            except FileNotFoundError as ex:
                rc = 127
                fh.write("\n[not found: %s]\n" % ex)
        dt = time.time() - t0
        print("   %s  (%.0fs)  %s" % ("PASS" if rc == 0 else "FAIL rc=%d" % rc, dt, log), flush=True)
        self.results.append((name, rc, dt, log))
        return rc

    def summary(self):
        bad = [r for r in self.results if r[1] != 0]
        print("\n" + "=" * 70)
        for name, rc, dt, _ in self.results:
            print("  %-4s %-48s %6.0fs" % ("ok" if rc == 0 else "FAIL", name[:48], dt))
        print("%d steps, %d failed. Logs: %s" % (len(self.results), len(bad), self.dir))
        return bad


# ------------------------------------------------------------------- targets
def discover_python_suites(repo):
    """Mirror the nd_hazard CI layout: <pkg>/tests/run_tests.py run as modules from the repo root,
    and packages with only tests/test_*.py run file by file."""
    mods, files = [], []
    for d in sorted(os.listdir(repo)):
        tdir = os.path.join(repo, d, "tests")
        if d.startswith(".") or d in ("releases", "docs", "tools", "shared") or not os.path.isdir(tdir):
            continue
        if os.path.isfile(os.path.join(tdir, "run_tests.py")):
            mods.append(d)
        else:
            files += [os.path.join(d, "tests", f) for f in sorted(os.listdir(tdir))
                      if f.startswith("test_") and f.endswith(".py")]
    return mods, files


def target_plugins(args, conda, steps):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    env_prefix(conda, args.qgis_env)
    py = in_env(conda, args.qgis_env, ["python"])
    qt = {"QT_QPA_PLATFORM": "offscreen"}
    for f in ("tools/ci_checks.py", "tools/test_ci_checks.py"):
        if os.path.isfile(os.path.join(repo, f)):
            steps.run("fast: " + f, py + [f], repo, qt, timeout=900)
    mods, files = discover_python_suites(repo)
    if args.only:
        mods = [m for m in mods if args.only in m]
        files = [f for f in files if args.only in f]
    for m in mods:
        steps.run("suite: %s" % m, py + ["-m", m + ".tests.run_tests"], repo, qt)
    for f in files:
        steps.run("test: %s" % f, py + [f], repo, qt, timeout=1800)
    if args.gui and os.path.isfile(os.path.join(repo, "tools/gui_lint.py")):
        steps.run("gui_lint", py + ["-X", "faulthandler", "tools/gui_lint.py"], repo, qt)
    if args.models:
        runner = next((m for m in ("nd_breach_runner",) if os.path.isdir(os.path.join(repo, m))), None)
        if not runner:
            print("   (no nd_breach_runner package here; --models skipped)")
            return
        menv = dict(qt)
        lis = args.lisflood or DEFAULT_LISFLOOD
        if os.path.isfile(lis):
            menv["NDBREACH_LISFLOOD"] = lis
        else:
            print("   ! no LISFLOOD-FP at %s — model tests that need it will skip or fail" % lis)
        anuga = os.path.join(env_prefix(conda, args.anuga_env), "bin", "python")
        menv["NDBREACH_ANUGA_PYTHON"] = anuga
        menv["OMP_NUM_THREADS"] = str(args.threads)
        steps.run("models: %s --models" % runner, py + ["-m", runner + ".tests.run_tests", "--models"],
                  repo, menv, timeout=7200)


def target_runner(args, conda, steps):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    cli = None
    for cand in ("nd_breach_runner/scripts/ndbreach_cli.py", "DamSafety_Runner/scripts/ndbreach_cli.py"):
        if os.path.isfile(os.path.join(repo, cand)):
            cli = cand
            break
    if not cli:
        sys.exit("ndbreach_cli.py not found under %s" % repo)
    env_prefix(conda, args.qgis_env)
    py = in_env(conda, args.qgis_env, ["python"])
    extra = []
    lis = args.lisflood or DEFAULT_LISFLOOD
    if args.action in ("check", "run", "estimate") and os.path.isfile(lis):
        extra += ["--lisflood", lis]
    if args.action in ("check", "run", "estimate"):
        extra += ["--anuga-python", os.path.join(env_prefix(conda, args.anuga_env), "bin", "python"),
                  "--threads", str(args.threads)]
    argv = py + [cli, args.action] + ([args.config] if args.config else []) + extra
    steps.run("runner: %s" % args.action, argv, repo, {"OMP_NUM_THREADS": str(args.threads)}, timeout=4 * 3600)


def target_crest(args, conda, steps):
    repo = os.path.abspath(os.path.expanduser(args.repo))
    env_prefix(conda, args.node_env)
    run = lambda *a: in_env(conda, args.node_env, list(a))
    nm, lock = os.path.join(repo, "node_modules"), os.path.join(repo, "package-lock.json")
    if not os.path.isdir(nm) or os.path.getmtime(lock) > os.path.getmtime(nm):
        steps.run("npm ci", run("npm", "ci"), repo, timeout=1800)
    for s in ("typecheck", "lint", "test"):
        steps.run("npm run " + s, run("npm", "run", s), repo, timeout=1800)
    if args.desktop:
        steps.run("npm run test:desktop", run("npm", "run", "test:desktop"), repo)
    if args.validate:
        dam, flood = args.validate
        steps.run("validate:crest", run("npm", "run", "validate:crest", "--", "--runner", dam, "--crest", flood),
                  repo)


# -------------------------------------------------------------------- triage
TRACE_RE = re.compile(r'File "([^"]+)", line (\d+)')
TS_RE = re.compile(r"((?:src|scripts|server)/[\w./-]+\.(?:ts|tsx|mjs|js))[(:](\d+)")


def code_context(repo, log_text, radius=30):
    """The deepest traceback frame inside the repo, with line numbers."""
    hits = [(f, int(n)) for f, n in TRACE_RE.findall(log_text)] + \
           [(f, int(n)) for f, n in TS_RE.findall(log_text)]
    for f, n in reversed(hits):
        full = f if os.path.isabs(f) else os.path.join(repo, f)
        full = os.path.realpath(full)
        if full.startswith(os.path.realpath(repo) + os.sep) and os.path.isfile(full):
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
            a, b = max(1, n - radius), min(len(lines), n + radius)
            body = "\n".join("%5d| %s" % (i, lines[i - 1]) for i in range(a, b + 1))
            return "%s (line %d)\n%s" % (os.path.relpath(full, repo), n, body)
    return "(no frame inside the repository found in the log)"


def chat(prompt):
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False,
               "think": True, "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                                          "num_ctx": NUM_CTX, "num_predict": 4096}}
    req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 400:
            payload.pop("think")
            req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1800) as r:
                out = json.loads(r.read())
        else:
            raise
    txt = (out.get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def triage(repo, failures, out_dir):
    path = os.path.join(out_dir, "TRIAGE.txt")
    lines = ["FAILURE TRIAGE — model %s (advisory; the logs are the facts)" % MODEL, "=" * 70]
    for name, rc, _, log in failures:
        with open(log, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        tail = "\n".join(text.splitlines()[-150:])
        prompt = ("A test step failed. Say in a few lines (1) whether this is an ENVIRONMENT problem "
                  "(missing module, wrong numpy/GDAL/Qt version, missing LISFLOOD-FP/ANUGA, network) "
                  "or a CODE bug, (2) the most likely cause, citing log lines and code line numbers, "
                  "(3) the smallest next check to confirm it. Do not invent code you cannot see; say "
                  "UNSURE when unsure.\n\nSTEP: %s (exit %d)\n\n=== LOG TAIL ===\n%s\n\n=== CODE ===\n%s"
                  % (name, rc, tail, code_context(repo, text)))
        print("triage: %s ..." % name, flush=True)
        try:
            ans = chat(prompt)
        except Exception as e:
            ans = "TRIAGE CALL FAILED: %s" % e
        lines += ["", "-" * 70, "STEP: %s (exit %d)  log: %s" % (name, rc, log), ans]
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    print("Triage: %s" % path)


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Run the dam-suite tests/runs in conda envs.")
    ap.add_argument("target", choices=["nd", "dwr", "runner", "crest"])
    ap.add_argument("action", nargs="?", default=None, help="runner: selftest | check | run | estimate | hydrograph")
    ap.add_argument("config", nargs="?", default=None, help="runner: config JSON for run/estimate/hydrograph")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--qgis-env", default="ndqgis")
    ap.add_argument("--anuga-env", default="ndanuga")
    ap.add_argument("--node-env", default="ndnode")
    ap.add_argument("--lisflood", default="")
    ap.add_argument("--threads", type=int, default=1, help="OpenMP threads (1 is the efficient choice)")
    ap.add_argument("--only", default="", help="nd/dwr: only suites/tests whose name contains this")
    ap.add_argument("--models", action="store_true", help="nd/dwr: also run the model tests")
    ap.add_argument("--gui", action="store_true", help="nd/dwr: also run tools/gui_lint.py")
    ap.add_argument("--desktop", action="store_true", help="crest: also the desktop smoke test")
    ap.add_argument("--validate", nargs=2, metavar=("DAM_FOLDER", "FLOOD_GEOJSON"),
                    help="crest: score a Crest layer against a Runner run")
    ap.add_argument("--triage", action="store_true", help="ask the local model to explain failures")
    args = ap.parse_args()

    conda = conda_exe()
    steps = Steps(args.target)
    if args.target in ("nd", "dwr"):
        target_plugins(args, conda, steps)
    elif args.target == "runner":
        if args.action not in ("selftest", "check", "run", "estimate", "hydrograph"):
            sys.exit("runner needs an action: selftest | check | run CONFIG | estimate CONFIG | hydrograph CONFIG")
        if args.action in ("run", "estimate", "hydrograph") and not args.config:
            sys.exit("runner %s needs a config JSON (make one: runner template)" % args.action)
        if args.config:
            args.config = os.path.abspath(os.path.expanduser(args.config))
        target_runner(args, conda, steps)
    else:
        target_crest(args, conda, steps)
    bad = steps.summary()
    if bad and args.triage:
        triage(os.path.abspath(os.path.expanduser(args.repo)), bad, steps.dir)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
