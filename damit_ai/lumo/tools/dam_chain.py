#!/usr/bin/env python3
# dam_chain.py v1.5 — run both dam-plugin sets headless from Terminal (macOS, conda), stdlib only.
#
#   dam_chain.py setup                       one-time: batch profiles that point at your QGIS
#                                            profiles "nd" and "damsafety", plugins enabled
#   dam_chain.py plugins --set nd|dwr        load every plugin of the set, check the chain's
#                                            algorithms are there, run the self-tests
#   dam_chain.py open --set nd|dwr           open QGIS (GUI) with that set's profile
#   dam_chain.py seed --dams-folder D --csv dams.csv
#                                            make <D>/<dam_id>/dam.json for each row (never overwrites)
#   dam_chain.py run --set nd  --dams-folder D --ids DWR-2801,DWR-4031
#                                            per dam: Get data -> Screening -> Runner -> Hazard
#                                            (ndhazard:getdamdata, then ndhazard:suitechain)
#                                            --hi-res downloads a 1 m DEM (DEM_CELL_M=1).
#                                            --triage asks the local Ollama model to explain a
#                                            failed step (writes TRIAGE.txt next to the run log).
#                                            Defaults can live in the environment (a --flag still wins):
#                                              DAMCHAIN_GETDATA_PARAM, DAMCHAIN_BREACH_PARAM,
#                                              DAMCHAIN_SCREEN_PARAM, DAMCHAIN_CLASSIFY_PARAM
#                                            each a space-separated list of KEY=VALUE, e.g.
#                                              export DAMCHAIN_GETDATA_PARAM="DEM_CELL_M=1 DEM_MI=15"
#   dam_chain.py run --set dwr --dams LAYER --ids 2801,4031 --out D --dem DEM --screen-work W
#                                            the DamSafety Runner's own list chain
#                                            (ndbreach:chainbatch: Screening -> Runner -> Hazard)
#   dam_chain.py research ask|research|search|ingest|chat|list ...
#                                            the literature research assistant (damresearch, a
#                                            separate model and library from lumo); same as typing
#                                            damresearch ...  (v1.4)
#   dam_chain.py audit [--repo R] [--zips Z] [--lumo] [--golden --results D] ...
#                                            local repo audit: release/packaging, security patterns,
#                                            network hosts, optional Lumo diff review, golden runs.
#                                            Report in ~/local-ai/reports (repo_audit.py, v1.5)
#   dam_chain.py crest --set nd|dwr          open the Crest desk wired to that set: its Report-step
#                                            buttons (dam folder, screening, flood model) run that
#                                            set's plugins, in ~/CrestDams/<set>
#
# Every run goes through qgis_process in the conda env (ndqgis), with the set's own QGIS
# profile, so ND and DamSafety never load together (they share Processing ids).
# --dry-run prints the commands without running them. Logs: <dams folder>/_chain_logs/.
import os, sys, re, csv, json, time, shutil, argparse, subprocess, threading, urllib.request, urllib.error
from datetime import datetime

QGIS_PROFILES = os.path.expanduser(os.environ.get("DAMCHAIN_QGIS_PROFILES",
                                   "~/Library/Application Support/QGIS/QGIS3/profiles"))
BATCH_ROOT = os.path.expanduser(os.environ.get("DAMCHAIN_BATCH_ROOT", "~/local-ai/qgis-batch"))
CREST_DIR = os.path.expanduser(os.environ.get("DAMCHAIN_CREST_DIR", "~/local-ai/crest-desktop"))

# Local Ollama triage (shared convention with suite_check.py / audit_plugin.py): optional, advisory.
LUMO_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
LUMO_MODEL = os.environ.get("LUMO_MODEL", "lumo")
LUMO_NUM_CTX = int(os.environ.get("LUMO_NUM_CTX", "16384"))
SETS = {
    "nd": {"profile": "nd", "runner": "nd_breach_runner",
           "plugins": ["ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening",
                       "nd_data_updater"],
           "needs": ["ndhazard:getdamdata", "ndhazard:suitechain", "nddam:runchain",
                     "ndbreach:rundam", "ndhazard:classify", "ndbreach:selftest", "nddam:selftest"]},
    "dwr": {"profile": "damsafety", "runner": "DamSafety_Runner",
            "plugins": ["DamSafety_Core", "DamSafety_Setup", "DamSafety_Data", "DamSafety_Screening",
                        "DamSafety_Runner", "DamSafety_Hazard", "DamSafety_HazardLite",
                        "DamSafety_PMP"],
            "needs": ["ndbreach:chainbatch", "nddam:runchain", "ndbreach:batch",
                      "ndhazard:classify", "ndbreach:selftest", "nddam:selftest"]},
}
DWR_HEC = {"run": 0, "wait": 1, "skip": 2}
DWR_STOP = {"end": 0, "screening": 1, "runner": 2}
DEM_UNITS = {"detect": 0, "feet": 1, "us-feet": 2, "meters": 3}


# ------------------------------------------------------------------ plumbing
def conda_exe():
    for c in (os.environ.get("CONDA_EXE"), shutil.which("conda"),
              os.path.expanduser("~/miniconda3/bin/conda"), os.path.expanduser("~/miniconda/bin/conda"),
              os.path.expanduser("~/miniforge3/bin/conda")):
        if c and os.path.exists(c):
            return c
    sys.exit("conda not found. Open a Terminal where `conda` works, or set CONDA_EXE.")


def env_prefix(conda, name):
    out = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True)
    try:
        for p in json.loads(out.stdout)["envs"]:
            if os.path.basename(p) == name:
                return p
    except (ValueError, KeyError):
        pass
    sys.exit("conda env '%s' not found — run the suite installer first." % name)


def find_qgis_process(prefix, override=""):
    if override:
        return override
    for c in (os.path.join(prefix, "bin", "qgis_process"),
              os.path.join(prefix, "QGIS.app", "Contents", "MacOS", "bin", "qgis_process"),
              os.path.join(prefix, "QGIS.app", "Contents", "MacOS", "qgis_process")):
        if os.path.isfile(c):
            return c
    return "qgis_process"          # on the env's PATH after activation


class QP:
    """qgis_process inside the conda env, with one set's batch profile."""

    def __init__(self, args, set_name):
        self.dry = args.dry_run
        self.quiet = getattr(args, "quiet", False)
        if self.dry:
            self.conda, self.prefix = shutil.which("conda") or "conda", "<%s>" % args.env
        else:
            self.conda = conda_exe()
            self.prefix = env_prefix(self.conda, args.env)
        self.qp = find_qgis_process(self.prefix, args.qgis_process)
        self.base = os.path.join(BATCH_ROOT, set_name)
        self.env = dict(os.environ)
        self.env.pop("PYTHONPATH", None)
        self.env.update({"QGIS_CUSTOM_CONFIG_PATH": self.base, "QT_QPA_PLATFORM": "offscreen",
                         "OMP_NUM_THREADS": str(args.threads), "NDBREACH_ANUGA_MESHER": "gmsh"})
        self.envname = args.env
        lis = os.path.expanduser(args.lisflood or "")
        if lis and os.path.isfile(lis):
            # Also read by tools that get no EXE parameter (DamSafety Screening's self-test before 0.18.5).
            self.env["NDBREACH_LISFLOOD"] = lis

    def argv(self, *qp_args):
        return [self.conda, "run", "--no-capture-output", "-n", self.envname, self.qp] + list(qp_args)

    @staticmethod
    def _is_noise(t):
        return (not t) or ("XDG_RUNTIME_DIR" in t) or t.startswith("Warning 1:") \
            or t.startswith("QStandardPaths") or t.startswith("Warning:")

    def run(self, alg, params, log_path, timeout=None, label=""):
        """Returns (ok, results dict, last error lines). Streams the plugin's own progress lines
        to the terminal (unless --quiet) with a heartbeat, so a long step never looks frozen."""
        cmd = self.argv("--json", "run", alg, "--") + ["%s=%s" % (k, v) for k, v in params.items()]
        shown = " ".join(_q(c) for c in cmd)
        if self.dry:
            print("  DRY  QGIS_CUSTOM_CONFIG_PATH=%s %s" % (_q(self.base), shown))
            return True, {}, ""
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        tag = label or alg
        out_chunks, start, last, in_json = [], time.time(), [time.time()], [False]
        with open(log_path, "w", encoding="utf-8") as log:
            log.write("$ %s\n\n" % shown)
            log.flush()
            try:
                p = subprocess.Popen(cmd, env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
            except OSError as e:
                log.write("\n[LAUNCH FAILED] %s\n" % e)
                return False, {}, str(e)

            def pump_out():
                for line in p.stdout:
                    out_chunks.append(line)
                    last[0] = time.time()
                    t = line.strip()
                    if t.startswith("{"):
                        in_json[0] = True           # the --json result block begins; stop echoing
                    if not self.quiet and not in_json[0] and not self._is_noise(t):
                        sys.stdout.write("      %s\n" % t)
                        sys.stdout.flush()

            def pump_err():
                for line in p.stderr:
                    log.write(line)
                    log.flush()

            stop = threading.Event()

            def beat():
                while not stop.wait(20):
                    if not self.quiet and time.time() - last[0] >= 18:
                        sys.stdout.write("      ... %s running (%ds elapsed)\n"
                                         % (tag, int(time.time() - start)))
                        sys.stdout.flush()

            threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err, beat)]
            for th in threads:
                th.start()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                stop.set()
                threads[0].join(2); threads[1].join(2)
                log.write("\n[TIMEOUT]\n")
                return False, {}, "timeout"
            stop.set()
            threads[0].join(2); threads[1].join(2)
            out = "".join(out_chunks)
            log.write("\n--- stdout ---\n" + (out or ""))
        res = _json_results(out)
        if p.returncode == 0 and res.get("OK") is not False:
            return True, res, ""
        if p.returncode == 0:      # the algorithm finished but reports a failure (self-tests)
            return False, res, "failed step %s, see %s" % (res.get("FAILED_STEP", "?"),
                                                           res.get("REPORT") or log_path)
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = []
            for l in fh.read().split("\n--- stdout ---")[0].splitlines():
                l = l.strip()
                if l and not self._is_noise(l) and (not tail or tail[-1] != l):
                    tail.append(l)
        return False, res, " | ".join(tail[-2:])

    def plain(self, *qp_args):
        cmd = self.argv(*qp_args)
        if self.dry:
            print("  DRY  %s" % " ".join(_q(c) for c in cmd))
            return 0, ""
        p = subprocess.run(cmd, env=self.env, capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr


def _json_results(out):
    """qgis_process --json results. Plugins print log lines to stdout before the JSON, so parse
    from the first line that is just "{"."""
    out = out or ""
    for start in [0] + [m.start() + 1 for m in re.finditer(r"\n\{", out)]:
        try:
            return json.loads(out[start:]).get("results") or {}
        except (ValueError, AttributeError):
            continue
    return {}


def _q(s):
    s = str(s)
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


def _lumo_chat(prompt, timeout=1800):
    """Ask the local Ollama model once; return its text (thinking stripped). Raises on transport error."""
    payload = {"model": LUMO_MODEL, "messages": [{"role": "user", "content": prompt}],
               "stream": False, "think": True,
               "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                           "num_ctx": LUMO_NUM_CTX, "num_predict": 4096}}
    def _post(pl):
        req = urllib.request.Request(LUMO_URL, data=json.dumps(pl).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    try:
        out = _post(payload)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        payload.pop("think")                       # some models reject the think flag
        out = _post(payload)
    txt = (out.get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def triage_failure(step_name, err, log_path, out_dir):
    """Send a failed step's log tail to the local model and append an advisory note to TRIAGE.txt.
    Advisory only: the log is the fact. Never raises (a triage problem must not fail the run)."""
    tail = ""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = "\n".join(fh.read().splitlines()[-150:])
    except Exception:                                                        # noqa
        tail = "(log not readable: %s)" % log_path
    prompt = ("A dam-suite step failed while running headless in QGIS (qgis_process). In a few lines, "
              "say (1) whether this is an ENVIRONMENT/DATA problem (no offline pack or chosen layers, "
              "missing DEM, DEM no-data at the flood edge, LISFLOOD-FP/ANUGA missing, wrong DEM units, "
              "network/proxy) or a CODE bug, (2) the most likely cause citing log lines, (3) the "
              "smallest next check or fix. Do not invent anything you cannot see in the log; say UNSURE "
              "when unsure.\n\nSTEP: %s\nERROR: %s\n\n=== LOG TAIL ===\n%s"
              % (step_name, err or "(none)", tail))
    print("   triage (%s): asking %s ..." % (step_name, LUMO_MODEL), flush=True)
    try:
        ans = _lumo_chat(prompt)
    except Exception as e:                                                   # noqa
        ans = ("TRIAGE UNAVAILABLE: could not reach Ollama at %s (%s). Start Ollama and pull the "
               "model, or set OLLAMA_URL / LUMO_MODEL." % (LUMO_URL, e))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "TRIAGE.txt")
    block = ["", "=" * 72, "%s  STEP: %s" % (_stamp(), step_name),
             "error: %s" % (err or "(none)"), "log:   %s" % log_path,
             "model: %s (advisory; the log is the fact)" % LUMO_MODEL, "-" * 72, ans]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    print("   triage -> %s" % path, flush=True)
    for ln in ans.splitlines()[:6]:
        print("      | " + ln, flush=True)
    return path


def parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            sys.exit("--param needs KEY=VALUE, got: %s" % it)
        k, v = it.split("=", 1)
        out[k.strip()] = v
    return out


def _env_kv(env_name):
    """KEY=VALUE defaults from an environment variable, split on whitespace.
    e.g.  export DAMCHAIN_BREACH_PARAM="CELL=10 MANNING=0.06"
    """
    raw = (os.environ.get(env_name) or "").strip()
    return [tok for tok in re.split(r"\s+", raw) if tok] if raw else []


def kv(items, env_name=None):
    """parse_kv with environment defaults applied first; a --flag value overrides the env."""
    out = {}
    if env_name:
        out.update(parse_kv(_env_kv(env_name)))
    out.update(parse_kv(items))
    return out


def read_ids(args):
    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()]
    if args.ids_csv:
        with open(os.path.expanduser(args.ids_csv), newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                v = (row.get("dam_id") or "").strip()
                if v and v not in ids:
                    ids.append(v)
    return ids


def write_atomic(path, text):
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- commands
def cmd_setup(args):
    """~/local-ai/qgis-batch/<set>/profiles/default -> your real QGIS profile, plugins enabled."""
    for set_name, spec in SETS.items():
        prof = getattr(args, "%s_profile" % set_name) or spec["profile"]
        real = os.path.join(QGIS_PROFILES, prof)
        link_dir = os.path.join(BATCH_ROOT, set_name, "profiles")
        link = os.path.join(link_dir, "default")
        if not os.path.isdir(real):
            print("! %s: no QGIS profile '%s' at %s — install that set first (skipped)" % (set_name, prof, real))
            continue
        os.makedirs(link_dir, exist_ok=True)
        if os.path.islink(link):
            os.remove(link)
        elif os.path.exists(link):
            sys.exit("%s exists and is not a link; move it away first" % link)
        os.symlink(real, link)
        print("%s: batch profile -> %s" % (set_name, real))
        qp = QP(args, set_name)
        for p in spec["plugins"]:
            if os.path.isdir(os.path.join(real, "python", "plugins", p)):
                qp.plain("plugins", "enable", p)
        print("   plugins enabled for qgis_process")


def cmd_plugins(args):
    spec = SETS[args.set]
    qp = QP(args, args.set)
    rc, out = qp.plain("list")
    if not args.dry_run:
        have = set(re.findall(r"\b(\w+:\w+)\b", out))
        # v1.5: `qgis_process list` leaves out tools hidden from the toolbox (the DamSafety
        # self-tests, Screening's run-chain): ask for each one by id before calling it missing
        for alg in spec["needs"]:
            if alg not in have:
                rc_h, _ = qp.plain("help", alg)
                if rc_h == 0:
                    have.add(alg)
        for alg in spec["needs"]:
            print("  %-4s %s" % ("ok" if alg in have else "MISSING", alg))
        providers = sorted({a.split(":")[0] for a in have if a.split(":")[0].startswith("nd")})
        print("  providers loaded: %s" % ", ".join(providers))
        missing = [a for a in spec["needs"] if a not in have]
        if missing:
            print("  -> run `dam_chain.py setup`, and check the plugins are installed in profile '%s'"
                  % spec["profile"])
    if args.selftest:
        logs = os.path.join(os.path.expanduser(args.log_dir), "selftest_%s_%s" % (args.set, _stamp()))
        lis = os.path.expanduser(args.lisflood)
        # ND Screening takes the step-7 cell in feet (CELL_FT), DamSafety Screening in metres (CELL).
        cell = {"CELL_FT": 100} if args.set == "nd" else {"CELL": 30}
        scr = dict(cell, ENGINE=2, SIMH=2, REPORT=os.path.join(logs, "nddam_selftest.txt"))
        run = {"RUN_MODEL": "true", "THREADS": args.threads}
        if os.path.isfile(lis):                  # test the real engine too, not only the built-in one
            scr.update(ENGINE=0, EXE=lis)        # both sets take EXE (DamSafety Screening from 0.18.5)
            run["LISFLOOD_EXE"] = lis
        for alg, params in (("ndbreach:selftest", run), ("nddam:selftest", scr)):
            ok, res, err = qp.run(alg, params, os.path.join(logs, alg.replace(":", "_") + ".log"),
                                  timeout=1800)
            print("  %-4s %s  %s" % ("PASS" if ok else "FAIL", alg,
                                     res.get("RESULT") or res.get("SUMMARY") or err))
        if not args.dry_run:
            print("  logs: %s" % logs)


def cmd_open(args):
    spec = SETS[args.set]
    conda = conda_exe()
    prefix = env_prefix(conda, args.env)
    app = os.path.join(prefix, "QGIS.app", "Contents", "MacOS", "QGIS")
    exe = app if os.path.isfile(app) else "qgis"
    cmd = [conda, "run", "-n", args.env, "env", "NDBREACH_ANUGA_MESHER=gmsh", exe,
           "--profile", getattr(args, "profile", None) or spec["profile"]]
    print(" ".join(_q(c) for c in cmd))
    if not args.dry_run:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        subprocess.Popen(cmd, env=env, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def cmd_pack(args):
    """Build / refresh the offline data pack with the ND Data Updater plugin, into the data folder.
    The pack build (structures, roads, rail, 3DHP, ABI barriers, NLCD, counties ...) is a statewide, multi-
    gigabyte download; it has no headless processing algorithm, so this opens QGIS to the Updater,
    which creates the pack in the folder you point it at and tells the dam plugins to read it there."""
    folder = os.path.expanduser(args.data_folder) if args.data_folder else "~/ND_Suite_Data"
    bar = "=" * 72
    print(bar)
    print("Build the offline pack into:  %s" % folder)
    print(bar)
    print("QGIS is opening with the '%s' profile. In QGIS:" % (getattr(args, "profile", None) or SETS[args.set]["profile"]))
    print("  1. Plugins menu > ND Data Updater > 'Settings and status...'.")
    print("  2. Local data > 'Choose...' > %s  (the three dam plugins follow it)." % folder)
    print("  3. Keep 'Offline pack for the ND dam plugins (all layers)' and")
    print("     'Make the ND dam plugins read this pack' ticked; 3DHP on 'auto'.")
    print("  4. Click 'Save and Check All Now'. Statewide, several GB, an hour or more the")
    print("     first time; 'Rebuild Only Missing Layers' resumes an interrupted build.")
    print("  The pack lands at %s/ND_Dam_Suite_Pack/; 'Offline pack and data folder...'" % folder)
    print("  in the same menu shows it layer by layer. Every later run reads it offline.")
    print(bar)
    if getattr(args, "print_only", False):
        return
    cmd_open(args)


def cmd_seed(args):
    """dam.json per CSV row. Columns: dam_id (required), name, lat, lon, height_ft, storage_acft,
    dam_length_ft, dwr_structure_id, nid_id, existing_hazard_class. Existing dam.json is kept."""
    dams = os.path.expanduser(args.dams_folder)
    num = {"lat", "lon", "height_ft", "storage_acft", "dam_length_ft"}
    made = kept = 0
    if not os.path.isfile(os.path.expanduser(args.csv)):
        sys.exit("No CSV at %s. Make one with a header line, e.g.\n"
                 "  dam_id,name,lat,lon,height_ft,storage_acft,dwr_structure_id,nid_id\n"
                 "  DWR-2801,Swan Buffalo 12,46.984382,-97.390918,27.5,1615.344,2801,ND00199"
                 % os.path.expanduser(args.csv))
    with open(os.path.expanduser(args.csv), newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            did = (row.get("dam_id") or "").strip()
            if not did:
                continue
            folder = os.path.join(dams, did)
            path = os.path.join(folder, "dam.json")
            if os.path.exists(path):
                kept += 1
                continue
            fields = {"dam_id": did}
            for k, v in row.items():
                if k == "dam_id" or v is None or not str(v).strip():
                    continue
                k = k.strip()
                try:
                    fields[k] = float(v) if k in num else str(v).strip()
                except ValueError:
                    sys.exit("%s: %s is not a number: %r" % (did, k, v))
            if "lat" not in fields or "lon" not in fields:
                print("! %s: no lat/lon — skipped" % did)
                continue
            rec = {"format": "nd_dam_suite/dam_record", "format_version": 1, "fields": fields,
                   "sources": {"_seed": {"tool": "dam_chain.py seed", "when": _stamp()[:8],
                                         "note": "from %s" % os.path.basename(args.csv)}},
                   "status": []}
            if args.dry_run:
                print("  DRY  %s" % path)
                continue
            os.makedirs(folder, exist_ok=True)
            write_atomic(path, json.dumps(rec, indent=1) + "\n")
            made += 1
    print("dam.json written: %d, already there (kept): %d" % (made, kept))


def run_nd(args):
    dams = os.path.abspath(os.path.expanduser(args.dams_folder))
    ids = read_ids(args) or sorted(d for d in os.listdir(dams)
                                   if os.path.isfile(os.path.join(dams, d, "dam.json")))
    if not ids:
        sys.exit("No dams: give --ids / --ids-csv, or seed dam folders first.")
    qp = QP(args, "nd")
    logs = os.path.join(dams, "_chain_logs", _stamp())
    summary = os.path.join(dams, "Chain_Summary_ND.csv")
    rows = []
    flags = {"SCREEN": str(not args.no_screen).lower(), "BREACH": str(not args.no_breach).lower(),
             "CLASSIFY": str(not args.no_classify).lower()}
    _env = {"SCREEN_PARAMETERS": "DAMCHAIN_SCREEN_PARAM",
            "BREACH_PARAMETERS": "DAMCHAIN_BREACH_PARAM",
            "CLASSIFY_PARAMETERS": "DAMCHAIN_CLASSIFY_PARAM"}
    for k, items in (("SCREEN_PARAMETERS", args.screen_param), ("BREACH_PARAMETERS", args.breach_param),
                     ("CLASSIFY_PARAMETERS", args.classify_param)):
        kvv = kv(items, _env[k])
        if kvv:
            flags[k] = json.dumps(kvv)
    for n, did in enumerate(ids, 1):
        folder = os.path.join(dams, did)
        state_path = os.path.join(folder, "_chain_nd", "state.json")
        t0 = time.time()
        print("[%d/%d] %s" % (n, len(ids), did), flush=True)
        if not os.path.isfile(os.path.join(folder, "dam.json")):
            rows.append([did, "no dam.json", "", "0", "", "seed it first (dam_chain.py seed)"])
            print("   no dam.json — skipped")
            continue
        if not args.redo and os.path.isfile(state_path):
            with open(state_path, encoding="utf-8") as fh:
                st = json.load(fh)
            if st.get("status") == "done":
                rows.append([did, "done (earlier run)", st.get("nd_class", ""), "0", st.get("log", ""), ""])
                print("   done earlier (class %s) — skipped; --redo to run again" % st.get("nd_class", "?"))
                continue
        status, nd_class, err, log = "done", "", "", ""
        if not args.no_getdata:
            gp = {"DAM_FOLDER": folder}
            if args.data_folder:
                gp["DATA_FOLDER"] = os.path.expanduser(args.data_folder)
            gp.update(kv(args.getdata_param, "DAMCHAIN_GETDATA_PARAM"))
            if getattr(args, "hi_res", False) and "DEM_CELL_M" not in gp:
                gp["DEM_CELL_M"] = "1"
            log = os.path.join(logs, "%s_1_getdamdata.log" % did)
            ok, _r, err = qp.run("ndhazard:getdamdata", gp, log, timeout=args.timeout_min * 60,
                                  label="get data for %s" % did)
            print("   get data: %s" % ("ok" if ok else "FAILED " + err))
            if not ok:
                status = "failed: get data"
                if getattr(args, "triage", False):
                    triage_failure("get data (%s)" % did, err, log, logs)
        if status == "done":
            cp = dict(flags, DAM_FOLDER=folder)
            cp.update(parse_kv(args.param))
            log = os.path.join(logs, "%s_2_suitechain.log" % did)
            ok, res, err = qp.run("ndhazard:suitechain", cp, log, timeout=args.timeout_min * 60,
                                   label="screening/runner/hazard for %s" % did)
            nd_class = str(res.get("ND_CLASS") or "")
            print("   chain: %s" % (("ok, ND class %s" % (nd_class or "?")) if ok else "FAILED " + err))
            if not ok:
                status = "failed: chain"
                if getattr(args, "triage", False):
                    triage_failure("chain (%s)" % did, err, log, logs)
        mins = "%.1f" % ((time.time() - t0) / 60)
        rows.append([did, status, nd_class, mins, log, err])
        if not args.dry_run:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            write_atomic(state_path, json.dumps({"status": "done" if status == "done" else "failed",
                                                 "nd_class": nd_class, "log": log, "error": err,
                                                 "when": _stamp()}, indent=1))
            _write_summary(summary, rows)
        if status != "done" and args.stop_on_error:
            break
    if not args.dry_run:
        print("\nSummary: %s" % summary)


def _write_summary(path, rows):
    """One row per dam: this run's rows replace earlier rows of the same dam, others are kept."""
    import io
    head = ["dam_id", "status", "nd_class", "minutes", "log", "error"]
    merged = {}
    if os.path.isfile(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.reader(fh):
                if r and r[0] != "dam_id":
                    merged[r[0]] = r
    for r in rows:
        merged[r[0]] = r
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(head)
    w.writerows(merged[k] for k in sorted(merged))
    write_atomic(path, buf.getvalue())


def run_dwr(args):
    if not args.dams:
        sys.exit("--set dwr needs --dams (a points layer: .gpkg, .shp, .geojson, or a 'path|layername=x')")
    p = {"DAMS": os.path.expanduser(args.dams), "OUT_FOLDER": os.path.expanduser(args.out or ""),
         "HEC_MODE": DWR_HEC[args.hec], "STOP_AFTER": DWR_STOP[args.stop_after],
         "RUNNER_MODELS": 1 if args.anuga else 0, "SCREENING": str(not args.no_screen).lower(),
         "KEEP_GOING": str(not args.stop_on_error).lower(), "SKIP_DONE": str(not args.redo).lower(),
         "CHECK_ONLY": str(args.check_only).lower()}
    if not p["OUT_FOLDER"]:
        sys.exit("--set dwr needs --out (the dams folder the chain writes into)")
    for key, val in (("DAM_ID_FIELD", args.id_field), ("DAM_IDS", args.ids),
                     ("IDS_CSV", args.ids_csv), ("DEM", args.dem), ("SCREEN_WORK", args.screen_work),
                     ("SETTINGS_FILE", args.settings_file), ("OVERRIDES_CSV", args.overrides_csv)):
        if val:
            p[key] = os.path.expanduser(val) if key not in ("DAM_IDS", "DAM_ID_FIELD") else val
    p.update(parse_kv(args.param))
    # Per-tool settings (e.g. the Runner's DEM units) reach chainbatch only through the
    # settings file or an overrides CSV; write the overrides CSV for the listed dams.
    tool_kv = {"ndbreach.batch.%s" % k: v for k, v in parse_kv(args.runner_param).items()}
    tool_kv.update({"ndhazard.classify.%s" % k: v for k, v in parse_kv(args.classify_param).items()})
    if args.dem_units:
        tool_kv["ndbreach.batch.DEM_Z"] = DEM_UNITS[args.dem_units]
    logs = os.path.join(p["OUT_FOLDER"], "_chain_logs")
    if tool_kv:
        ids = read_ids(args)
        if not ids:
            sys.exit("--dem-units / --runner-param / --classify-param need the dams named (--ids or --ids-csv)")
        if args.overrides_csv:
            sys.exit("give either --overrides-csv or --dem-units/--runner-param/--classify-param, not both")
        ov = os.path.join(logs, "overrides_%s.csv" % _stamp())
        cols = sorted(tool_kv)
        lines = [",".join(["dam_id"] + cols)] + [",".join([i] + [str(tool_kv[c]) for c in cols]) for i in ids]
        if not args.dry_run:
            os.makedirs(logs, exist_ok=True)
            write_atomic(ov, "\n".join(lines) + "\n")
        p["OVERRIDES_CSV"] = ov
    qp = QP(args, "dwr")
    log = os.path.join(logs, "chainbatch_%s.log" % _stamp())
    ok, res, err = qp.run("ndbreach:chainbatch", p, log, label="DamSafety chain")
    if args.dry_run:
        return
    counts = str(res.get("COUNTS") or "")
    m = re.search(r"(\d+)\s+failed", counts)
    if ok and (m and int(m.group(1)) > 0 or "error" in counts):
        ok = False                       # the chain finished, but some dams (or the preflight) failed
    print("%s  %s" % ("OK" if ok else "FAILED", counts or err))
    print("log: %s" % log)
    if res.get("SUMMARY"):
        print("summary: %s" % res["SUMMARY"])
    if not ok and getattr(args, "triage", False):
        triage_failure("DamSafety chain", err or counts, log, os.path.dirname(log))
    sys.exit(0 if ok else 1)


def _stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _wrapper(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n# written by dam_chain.py crest; safe to delete (it is rewritten)\n")
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)


def crest_env(args, set_name):
    """Wrapper scripts + environment that point the Crest desk at one plugin set.
    Crest calls `qgis_process` (QGIS_PROCESS) and the Runner's ndbreach_cli.py (CREST_NDBREACH_CLI,
    run with CREST_PYTHON); both wrappers run inside the conda env with that set's batch profile."""
    spec = SETS[set_name]
    qp = QP(args, set_name)
    base = qp.base
    real = os.path.realpath(os.path.join(base, "profiles", "default"))
    if not os.path.isdir(real):
        sys.exit("No batch profile for '%s' at %s: run `dam_chain.py setup` first." % (set_name, base))
    cli = os.path.join(real, "python", "plugins", spec["runner"], "scripts", "ndbreach_cli.py")
    if not os.path.isfile(cli):
        sys.exit("%s not found: is the %s set installed in profile '%s'?" % (cli, set_name, spec["profile"]))
    exports = ["export QGIS_CUSTOM_CONFIG_PATH=%s" % _q(base), "export QT_QPA_PLATFORM=offscreen",
               "export NDBREACH_ANUGA_MESHER=gmsh", "export OMP_NUM_THREADS=%d" % args.threads,
               "unset PYTHONPATH"]
    if qp.env.get("NDBREACH_LISFLOOD"):
        exports.append("export NDBREACH_LISFLOOD=%s" % _q(qp.env["NDBREACH_LISFLOOD"]))
    bindir = os.path.join(base, "bin")
    run = "exec %s run --no-capture-output -n %s" % (_q(qp.conda), _q(qp.envname))
    _wrapper(os.path.join(bindir, "qgis_process"), exports + ['%s %s "$@"' % (run, _q(qp.qp))])
    _wrapper(os.path.join(bindir, "python3"), exports + ['%s python "$@"' % run])
    work = os.path.expanduser(args.work or os.path.join("~/CrestDams", set_name))
    os.makedirs(work, exist_ok=True)
    env = {"QGIS_PROCESS": os.path.join(bindir, "qgis_process"),
           "CREST_PYTHON": os.path.join(bindir, "python3"),
           "CREST_NDBREACH_CLI": cli,
           "CREST_WORK": work,
           "CREST_WORK_ROOTS": os.pathsep.join([os.path.expanduser("~/CrestDams"), work])}
    if args.data_folder:
        env["NDSUITE_DATA"] = os.path.expanduser(args.data_folder)
    return env


def cmd_crest(args):
    env = crest_env(args, args.set)
    for k in sorted(env):
        print("export %s=%s" % (k, _q(env[k])))
    if args.print_env:
        return
    crest = os.path.expanduser(args.crest_dir)
    run_py = os.path.join(crest, "run.py")
    if not os.path.isfile(run_py):
        sys.exit("Crest desk not found at %s (run.py). Install it with install_all.zsh or pass --crest-dir." % crest)
    py = args.crest_python
    if not py:
        try:
            py = os.path.join(env_prefix(conda_exe(), "ndcrest"), "bin", "python")
        except SystemExit:
            py = sys.executable              # no ndcrest env: the desk opens in the browser instead
    full = dict(os.environ)
    full.pop("PYTHONPATH", None)
    full.update(env)
    print("Crest desk -> %s plugins (profile '%s'), work folder %s. Leave this Terminal open."
          % ("ND" if args.set == "nd" else "DamSafety", SETS[args.set]["profile"], env["CREST_WORK"]))
    if args.dry_run:
        print("  DRY  %s %s" % (_q(py), _q(run_py)))
        return
    sys.exit(subprocess.call([py, run_py], cwd=crest, env=full))


def research_passthrough(argv):
    """`damchain research ...` -> the damresearch tool, with its own Python (ndresearch env)."""
    tool = os.path.expanduser(os.environ.get("DAMRESEARCH_TOOL", "~/local-ai/research/damresearch.py"))
    py = os.path.expanduser(os.environ.get("DAMRESEARCH_PY", "") or "~/local-ai/research/python")
    if not os.path.isfile(tool):
        sys.exit("damresearch is not installed. Run: zsh setup-dam-research.zsh (from the dam-research folder)")
    if not os.path.exists(py):
        py = sys.executable
    os.execv(py, [py, tool] + argv)


def audit_passthrough(argv):
    """`damchain audit ...` -> repo_audit.py next to this file (or in ~/local-ai)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for tool in (os.path.join(here, "repo_audit.py"), os.path.expanduser("~/local-ai/repo_audit.py")):
        if os.path.isfile(tool):
            os.execv(sys.executable, [sys.executable, tool] + argv)
    sys.exit("repo_audit.py is not installed next to dam_chain.py; rerun setup-local-lumo.sh")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "research":
        research_passthrough(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "audit":
        audit_passthrough(sys.argv[2:])
    ap = argparse.ArgumentParser(description="Run the ND and DamSafety dam plugins headless. "
                                             "`research ...` runs the literature assistant (damresearch); "
                                             "`audit ...` the local repo audit (audit --help).")
    ap.add_argument("--env", default="ndqgis", help="conda env with QGIS (default ndqgis)")
    ap.add_argument("--qgis-process", default="", help="path to qgis_process (default: found in the env)")
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--lisflood", default="~/nd-dam-suite/bin/lisflood",
                    help="LISFLOOD-FP binary (missing = the plugins' own choice / built-in engine)")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run nothing")
    ap.add_argument("--quiet", action="store_true", help="don't stream each step's progress lines to the terminal (the full log is still written)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup")
    s.add_argument("--nd-profile", default="")
    s.add_argument("--dwr-profile", default="")

    s = sub.add_parser("plugins")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--selftest", action="store_true")
    s.add_argument("--log-dir", default="~/local-ai/reports")

    s = sub.add_parser("open")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--profile", default="")

    s = sub.add_parser("pack", help="build/refresh the offline data pack into the data folder (opens the Updater)")
    s.add_argument("--set", choices=SETS, default="nd")
    s.add_argument("--data-folder", default="", help="where the pack is built (default ~/ND_Suite_Data)")
    s.add_argument("--profile", default="")
    s.add_argument("--print-only", action="store_true", help="print the steps, do not open QGIS")

    s = sub.add_parser("crest", help="open the Crest desk wired to one plugin set")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--work", default="", help="Crest work folder (default ~/CrestDams/<set>)")
    s.add_argument("--data-folder", default="", help="ND offline pack / suite data folder (NDSUITE_DATA)")
    s.add_argument("--crest-dir", default=CREST_DIR, help="the crest-desktop folder (default ~/local-ai/crest-desktop)")
    s.add_argument("--crest-python", default="", help="Python for the desk (default: the ndcrest env)")
    s.add_argument("--print-env", action="store_true", help="write the wrappers, print the exports, do not open")

    s = sub.add_parser("seed")
    s.add_argument("--dams-folder", required=True)
    s.add_argument("--csv", required=True)

    s = sub.add_parser("run")
    s.add_argument("--set", choices=SETS, required=True)
    s.add_argument("--ids", default="", help="comma separated dam ids")
    s.add_argument("--ids-csv", default="", help="CSV with a dam_id column")
    s.add_argument("--no-screen", action="store_true")
    s.add_argument("--redo", action="store_true", help="run dams again that finished before")
    s.add_argument("--stop-on-error", action="store_true")
    s.add_argument("--param", action="append", help="extra KEY=VALUE for the chain algorithm")
    s.add_argument("--triage", action="store_true",
                   help="on a failed step, send the log tail to the local Ollama model and write "
                        "an advisory TRIAGE.txt next to the run log")
    s.add_argument("--lumo-model", default="", help="Ollama model for --triage (default: $LUMO_MODEL or 'lumo')")
    g = s.add_argument_group("nd set (ndhazard:suitechain per dam folder)")
    g.add_argument("--dams-folder", default="", help="folder of <dam_id>/dam.json folders")
    g.add_argument("--data-folder", default="", help="suite data folder / offline pack for Get data")
    g.add_argument("--no-getdata", action="store_true")
    g.add_argument("--hi-res", action="store_true",
                   help="download a 1 m 3DEP DEM (DEM_CELL_M=1) unless --getdata-param sets it; "
                        "pair with DEM_MI to bound the download")
    g.add_argument("--no-breach", action="store_true")
    g.add_argument("--no-classify", action="store_true")
    g.add_argument("--getdata-param", action="append",
                   help="KEY=VALUE for ndhazard:getdamdata, e.g. DEM_CELL_M=5, DEM_MI=10, OVERWRITE=true")
    g.add_argument("--screen-param", action="append", help="KEY=VALUE for nddam:runchain")
    g.add_argument("--breach-param", action="append", help="KEY=VALUE for ndbreach:rundam")
    g.add_argument("--classify-param", action="append", help="KEY=VALUE for ndhazard:classify")
    g.add_argument("--timeout-min", type=int, default=480)
    g = s.add_argument_group("dwr set (ndbreach:chainbatch)")
    g.add_argument("--dams", default="", help="dams points layer")
    g.add_argument("--id-field", default="", help="dam id field of the layer")
    g.add_argument("--out", default="", help="output folder, one folder per dam")
    g.add_argument("--dem", default="", help="DEM or VRT covering every dam")
    g.add_argument("--screen-work", default="", help="Screening work folder (statewide run + data package)")
    g.add_argument("--hec", choices=DWR_HEC, default="skip",
                   help="HEC-RAS 2D stage (default skip: it cannot run on a Mac)")
    g.add_argument("--anuga", action="store_true", help="LISFLOOD-FP then ANUGA")
    g.add_argument("--stop-after", choices=DWR_STOP, default="end")
    g.add_argument("--settings-file", default="")
    g.add_argument("--overrides-csv", default="")
    g.add_argument("--check-only", action="store_true", help="preflight only")
    g.add_argument("--dem-units", choices=DEM_UNITS, default="",
                   help="the Runner's DEM elevation units (needed when the DEM records none, e.g. a VRT)")
    g.add_argument("--runner-param", action="append", help="KEY=VALUE for the Runner (ndbreach:batch)")

    args = ap.parse_args()
    if getattr(args, "lumo_model", ""):
        globals()["LUMO_MODEL"] = args.lumo_model
    if args.cmd == "setup":
        cmd_setup(args)
    elif args.cmd == "plugins":
        cmd_plugins(args)
    elif args.cmd == "open":
        cmd_open(args)
    elif args.cmd == "pack":
        cmd_pack(args)
    elif args.cmd == "seed":
        cmd_seed(args)
    elif args.cmd == "crest":
        cmd_crest(args)
    elif args.set == "nd":
        if not args.dams_folder:
            sys.exit("--set nd needs --dams-folder")
        run_nd(args)
    else:
        run_dwr(args)


if __name__ == "__main__":
    main()
