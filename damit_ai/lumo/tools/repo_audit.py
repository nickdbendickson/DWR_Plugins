#!/usr/bin/env python3
# repo_audit.py v1.0 — one local audit report for a dam-suite repo (DWR_Plugins, nd_hazard, crest,
# damit). Standard library only; runs offline in a few minutes. Used by `damchain audit`.
#
#   repo_audit.py --repo ~/src/DWR_Plugins                       # checks 1-4
#   repo_audit.py --repo ~/src/DWR_Plugins --zips ~/dam-suites-mac-2026-11-10/plugins/dwr
#   repo_audit.py --repo ~/src/DWR_Plugins --lumo                # + Lumo reviews the diff vs main
#   repo_audit.py --golden-record --results ~/DamSafety/Dams --ids DWR-2801,DWR-2865,DWR-4031
#   repo_audit.py --golden        --results ~/DamSafety/Dams     # compare with that baseline
#
# Checks (each finding has a severity: HIGH fails the run, MED/LOW/INFO are listed):
#   1. release   the repo's own fast gate (tools/ci_checks.py, when the repo has one), then what it
#                does not cover: every plugin folder named in every release tool / installer list
#                that names most of them (the bug where DamSafety_Core was left out of the release
#                zips), and with --zips the built zips themselves (one top folder, version in the
#                file name = metadata.txt, no .pyc, every plugin present)
#   2. security  bandit + ruff when they are installed (never installed by this script), plus
#                built-in patterns: download-then-run in shell scripts, pickle/marshal loads,
#                eval/exec, shell=True / os.system, yaml.load without SafeLoader, tar extractall
#                without filter, TLS verification turned off, plain ftp://, hard-coded secrets,
#                downloads saved with no checksum anywhere in the file
#   3. network   every host the code names, with the files (an allowlist for IT; with
#                --allow-hosts FILE, hosts not on the list are MED)
#   4. lumo      (--lumo) the local model reviews the changed lines vs --base; TRIAGE ONLY: a
#                finding is real only when a failing test shows it
#   5. golden    (--golden-record / --golden) Runner results of dams you trust vs a later run:
#                peak breach cfs, flooded acres, max depth, max velocity, storage at failure,
#                latest arrival, LISFLOOD-FP volume error. Run the dams first, e.g.
#                damchain run --set dwr ... --out ~/DamSafety/Dams --ids 2801,2865,4031
#
# Report: ~/local-ai/reports/audit_<repo>_<time>.md and .json. Exit status 1 when anything is HIGH.
import argparse, ast, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.request, zipfile
from datetime import datetime

REPORTS = os.path.expanduser(os.environ.get("DAMAUDIT_REPORTS", "~/local-ai/reports"))
GOLDEN_DIR = os.path.expanduser(os.environ.get("DAMAUDIT_GOLDEN", "~/local-ai/golden"))
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LUMO_MODEL = os.environ.get("LUMO_MODEL", "lumo")
SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "build", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "offline", "lisflood-src"}
CODE_EXT = (".py", ".sh", ".zsh", ".command", ".bash")
WEB_EXT = (".ts", ".tsx", ".js", ".mjs", ".cjs")
SEV_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2, "INFO": 3}


class Report:
    def __init__(self):
        self.items = []          # dicts: sev, check, where, msg
        self.notes = {}          # check -> list of lines (summary text)

    def add(self, sev, check, where, msg):
        self.items.append({"sev": sev, "check": check, "where": where, "msg": msg})

    def note(self, check, line):
        self.notes.setdefault(check, []).append(line)


def say(msg):
    print(msg, flush=True)


def walk_files(root, exts):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for f in files:
            if f.endswith(exts):
                yield os.path.join(d, f)


def rel(root, p):
    return os.path.relpath(p, root)


def read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def is_test(path):
    parts = path.replace("\\", "/").split("/")
    return any(x in ("tests", "test", "__tests__") for x in parts) or os.path.basename(path).startswith("test_") \
        or re.search(r"\.test\.(m?[jt]sx?)$", path) is not None


# ------------------------------------------------------------------ 1. release / packaging
def plugin_folders(root):
    out = []
    for name in sorted(os.listdir(root)):
        md = os.path.join(root, name, "metadata.txt")
        if os.path.isfile(md):
            out.append(name)
    return out


def metadata(path):
    vals = {}
    for line in read(path).splitlines():
        m = re.match(r"^(\w+)\s*=\s*(.*)$", line)
        if m and m.group(1) not in vals:
            vals[m.group(1)] = m.group(2).strip()
    return vals


RELEASE_FILE = re.compile(r"(?i)release|build|package|install|bundle|zip|dist|publish|deploy")


def check_release(root, rep, zips_dir=None, run_gate=True):
    plugins = plugin_folders(root)
    gate = os.path.join(root, "tools", "ci_checks.py")
    if run_gate and os.path.isfile(gate):
        say("  running the repo's own fast gate (tools/ci_checks.py) ...")
        t0 = time.time()
        r = subprocess.run([sys.executable, gate], cwd=root, capture_output=True, text=True, timeout=1800)
        tail = (r.stdout + r.stderr).strip().splitlines()[-15:]
        if r.returncode:
            rep.add("HIGH", "release", "tools/ci_checks.py", "the repo's fast gate failed:\n" + "\n".join(tail))
        else:
            rep.note("release", "tools/ci_checks.py passed (%.0f s)" % (time.time() - t0))
    if not plugins:
        rep.note("release", "no QGIS plugin folders (no */metadata.txt): plugin checks skipped")
        return
    rep.note("release", "plugins: " + ", ".join(plugins))
    names = {p: metadata(os.path.join(root, p, "metadata.txt")) for p in plugins}
    core = [p for p in plugins if p.endswith("_Core")]
    for p, md in names.items():
        for key in ("name", "version", "qgisMinimumVersion"):
            if not md.get(key):
                rep.add("HIGH", "release", p + "/metadata.txt", "no %s=" % key)
        if core and p not in core:
            core_name = names[core[0]].get("name", core[0])
            if core_name not in (md.get("plugin_dependencies") or ""):
                rep.add("HIGH", "release", p + "/metadata.txt",
                        "does not declare plugin_dependencies=%s" % core_name)
    # every list in tools/ / installers that names most plugins must name all of them
    lists = [f for f in walk_files(os.path.join(root, "tools"), CODE_EXT + (".yml", ".yaml", ".md"))] \
        if os.path.isdir(os.path.join(root, "tools")) else []
    lists += [os.path.join(root, ".github", "workflows", f)
              for f in (os.listdir(os.path.join(root, ".github", "workflows"))
                        if os.path.isdir(os.path.join(root, ".github", "workflows")) else [])]
    for f in lists:
        if is_test(f) or not RELEASE_FILE.search(os.path.basename(f)):
            continue
        text = read(f)
        named = [p for p in plugins if re.search(r"\b%s\b" % re.escape(p), text)]
        if len(plugins) >= 3 and len(named) >= max(3, (len(plugins) + 1) // 2) and len(named) < len(plugins):
            missing = [p for p in plugins if p not in named]
            rep.add("MED", "release", rel(root, f),
                    "names %d of %d plugins but not %s (a release/install list that misses a plugin?)"
                    % (len(named), len(plugins), ", ".join(missing)))
    if zips_dir:
        check_zips(os.path.expanduser(zips_dir), plugins, names, rep)


def check_zips(zdir, plugins, names, rep):
    zips = sorted(f for f in os.listdir(zdir) if f.endswith(".zip")) if os.path.isdir(zdir) else []
    if not zips:
        rep.add("HIGH", "release", zdir, "no .zip files")
        return
    seen = set()
    for z in zips:
        path = os.path.join(zdir, z)
        try:
            with zipfile.ZipFile(path) as zf:
                members = zf.namelist()
                tops = {m.split("/", 1)[0] for m in members if m.strip("/")}
                if len(tops) != 1:
                    rep.add("HIGH", "release", z, "has %d top folders (%s); QGIS needs exactly one"
                            % (len(tops), ", ".join(sorted(tops))[:120]))
                    continue
                top = tops.pop()
                seen.add(top)
                if top not in plugins:
                    rep.add("MED", "release", z, "top folder %s is not a plugin folder of this repo" % top)
                mdname = top + "/metadata.txt"
                if mdname not in members:
                    rep.add("HIGH", "release", z, "no metadata.txt in %s/" % top)
                    continue
                ver = ""
                for line in zf.read(mdname).decode("utf-8", "replace").splitlines():
                    if line.startswith("version="):
                        ver = line.split("=", 1)[1].strip()
                        break
                if ver and ver not in z:
                    rep.add("HIGH", "release", z, "file name does not carry metadata version %s" % ver)
                repo_ver = names.get(top, {}).get("version")
                if repo_ver and ver and repo_ver != ver:
                    rep.add("LOW", "release", z, "zip is %s, the repo checkout is %s (an older build?)" % (ver, repo_ver))
                pyc = [m for m in members if m.endswith(".pyc") or "/__pycache__/" in m]
                if pyc:
                    rep.add("LOW", "release", z, "%d compiled .pyc files inside (rebuild without them)" % len(pyc))
                comment = zf.comment.decode("utf-8", "replace").strip()
                if comment:
                    rep.note("release", "%s: built from %s" % (z, comment[:12]))
        except zipfile.BadZipFile:
            rep.add("HIGH", "release", z, "not a readable zip")
    for p in plugins:
        if p not in seen:
            rep.add("HIGH", "release", zdir, "no zip for plugin %s" % p)


# ------------------------------------------------------------------ 2. security
# repo_audit: off  (the pattern table itself; lines between off/on are not scanned)
PY_PATTERNS = [
    ("HIGH", r"\bverify\s*=\s*False\b|_create_unverified_context|CERT_NONE|check_hostname\s*=\s*False",
     "TLS certificate checking turned off"),
    ("MED", r"\b(pickle|cPickle|dill|marshal)\.loads?\s*\(|\bpd\.read_pickle\s*\(|np\.load\([^)]*allow_pickle\s*=\s*True",
     "unpickling: a doctored file runs code; only for files this tool wrote in a folder only you can write"),
    ("MED", r"(?<![\w.])(eval|exec)\s*\((?!\s*compile\()", "eval/exec of a string"),
    ("LOW", r"(?<![\w.])exec\s*\(\s*compile\(", "exec of compiled source (a loader/shim: check where the source comes from)"),
    ("MED", r"shell\s*=\s*True", "subprocess with shell=True (quote every value, or pass a list)"),
    ("MED", r"\bos\.system\s*\(|\bos\.popen\s*\(", "os.system/os.popen runs a shell"),
    ("MED", r"\byaml\.load\s*\((?![^)]*SafeLoader)", "yaml.load without SafeLoader"),
    # zipfile.extractall already drops ".." and absolute names; tarfile does not
    ("MED", r"\b(tf|tar\w*|\w*_tar|\w*tarball)\.extractall\s*\((?![^)]*filter\s*=)",
     "tar extractall without filter='data' (path traversal / links)"),
    ("MED", r"['\"]ftp://", "plain FTP (no encryption, no integrity check)"),
    ("HIGH", r"(?i)\b(api[_-]?key|secret|token|passw(or)?d)\s*=\s*['\"][A-Za-z0-9_\-/+]{12,}['\"]",
     "looks like a hard-coded secret"),
]
SH_PATTERNS = [
    ("HIGH", r"(curl|wget)\b[^|\n]*\|\s*(sudo\s+)?(ba|z)?sh\b", "download piped straight into a shell"),
    ("HIGH", r"(?i)--insecure\b|curl\s+(-\w*k\w*\s)", "curl without certificate checking"),
    ("MED", r"(^|[;&|(`]|\bthen|\bdo|\belse)\s*sudo\s+\S", "sudo in a script (the suite installs are meant to need none)"),
    ("MED", r"(curl|wget)\b[^\n]*\.(sh|command|pkg|dmg)\b", "downloads a program (check it is hash-checked before it runs)"),
    ("MED", r"['\"]?ftp://", "plain FTP"),
]
WEB_PATTERNS = [
    ("MED", r"(?<![\w.])eval\s*\(|new\s+Function\s*\(", "eval / new Function"),
    ("LOW", r"\.innerHTML\s*=|dangerouslySetInnerHTML", "HTML from a string (check it is escaped)"),
]
DOWNLOAD = re.compile(r"urlopen|urlretrieve|requests\.(get|post)|httpx\.|curl\b|wget\b")
WRITES = re.compile(r"open\([^)]*['\"]wb['\"]|\.write_bytes\(|urlretrieve|-o\s+\S+|--output")
HASHED = re.compile(r"sha256|sha512|hashlib|shasum|checksum|md5", re.I)
# repo_audit: on


def strip_py_comments(text):
    out = []
    for line in text.splitlines():
        s = line.lstrip()
        out.append("" if s.startswith("#") else line)
    return out


def scan_security(root, rep):
    n = 0
    for f in walk_files(root, CODE_EXT + WEB_EXT):
        n += 1
        r = rel(root, f)
        text = read(f)
        test = is_test(f)
        lines = strip_py_comments(text) if f.endswith(CODE_EXT) else text.splitlines()
        pats = PY_PATTERNS if f.endswith(".py") else SH_PATTERNS if f.endswith(CODE_EXT) else WEB_PATTERNS
        off, raw = False, text.splitlines()
        for i, line in enumerate(lines, 1):
            mark = raw[i - 1].strip() if i <= len(raw) else ""
            if mark.startswith("# repo_audit: off"):
                off = True
            elif mark.startswith("# repo_audit: on"):
                off = False
            if off or ("noqa" in line and "audit" in line):   # "# noqa: audit" accepts a reviewed line
                continue
            code = re.sub(r"'[^']*'|\"[^\"]*\"", "''", line) if pats is SH_PATTERNS else line
            for sev, pat, msg in pats:
                if re.search(pat, code if "sudo" in pat else line):
                    rep.add("LOW" if test else sev, "security", "%s:%d" % (r, i),
                            msg + ("  (test code)" if test else "") + "\n    " + line.strip()[:160])
        if not test and DOWNLOAD.search(text) and WRITES.search(text) and not HASHED.search(text):
            rep.add("LOW", "security", r, "downloads and saves files, with no checksum anywhere in the file")
    rep.note("security", "%d code files scanned with the built-in patterns" % n)
    run_linters(root, rep)


def find_tool(name):
    p = shutil.which(name)
    if p:
        return [p]
    for env in ("ndlumo", "ndresearch", "ndqgis"):
        for base in ("~/miniconda3", "~/miniforge3", "~/miniconda", "~/anaconda3"):
            cand = os.path.expanduser("%s/envs/%s/bin/%s" % (base, env, name))
            if os.path.isfile(cand):
                return [cand]
    return None


# ruff rules worth a look (the full S/B sets flag every try/except/pass and local SQL string)
RUFF_RULES = "F821,F811,E9,B006,B008,B023,S102,S301,S307,S324,S501,S506,S602,S604,S605,S113"


def run_linters(root, rep):
    bandit = find_tool("bandit")
    if bandit:
        r = subprocess.run(bandit + ["-q", "-r", "-f", "json", "-x", ",".join(
            os.path.join(root, d) for d in ("tests", "node_modules", ".git")), root],
            capture_output=True, text=True, timeout=1800)
        try:
            res = json.loads(r.stdout or "{}").get("results", [])
        except ValueError:
            res = []
        for x in res:
            sev = {"HIGH": "MED", "MEDIUM": "LOW"}.get(x.get("issue_severity"), "INFO")
            if is_test(x.get("filename", "")):
                continue
            rep.add(sev, "security", "%s:%s" % (rel(root, x.get("filename", "")), x.get("line_number")),
                    "bandit %s: %s" % (x.get("test_id"), x.get("issue_text")))
        rep.note("security", "bandit: %d findings (high -> MED, medium -> LOW, low -> INFO)" % len(res))
    else:
        rep.note("security", "bandit not installed (optional; e.g. into the ndlumo env from the conda-forge "
                             "channel when you have one), built-in patterns only")
    ruff = find_tool("ruff")
    if ruff:
        r = subprocess.run(ruff + ["check", "--quiet", "--no-cache", "--select", RUFF_RULES,
                                   "--output-format", "json", root], capture_output=True, text=True, timeout=1800)
        try:
            res = json.loads(r.stdout or "[]")
        except ValueError:
            res = []
        res = [x for x in res if not is_test(x.get("filename", ""))]
        for x in res[:400]:
            code = x.get("code") or ""
            sev = "MED" if code.startswith(("F821", "E9")) else "LOW" if code.startswith(("B", "S")) else "INFO"
            rep.add(sev, "security" if code.startswith("S") else "lint",
                    "%s:%s" % (rel(root, x.get("filename", "")), (x.get("location") or {}).get("row")),
                    "ruff %s: %s" % (code, x.get("message")))
        rep.note("security", "ruff: %d findings (undefined names / syntax = MED)" % len(res))
    else:
        rep.note("security", "ruff not installed (optional), skipped")


# ------------------------------------------------------------------ 3. network inventory
URL = re.compile(r"\b(https?|ftp)://([A-Za-z0-9.-]+\.[A-Za-z]{2,})(:\d+)?")
NOT_HOSTS = {"example.com", "example.org", "localhost", "www.w3.org", "schemas.openxmlformats.org",
             "purl.org", "ns.adobe.com", "json-schema.org", "www.opengis.net", "www.qgis.org"}


def network_inventory(root, rep, allow_file=None, out_hosts=None):
    hosts = {}
    for f in walk_files(root, CODE_EXT + WEB_EXT + (".json", ".yml", ".yaml", ".txt")):
        if is_test(f) or os.path.getsize(f) > 3_000_000:
            continue
        for m in URL.finditer(read(f)):
            h = m.group(2).lower().rstrip(".")
            if h in NOT_HOSTS or h.endswith((".local", ".invalid")) or re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
                continue
            e = hosts.setdefault(h, {"schemes": set(), "files": set()})
            e["schemes"].add(m.group(1))
            e["files"].add(rel(root, f))
    allowed = set()
    if allow_file:
        for line in read(os.path.expanduser(allow_file)).splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if line:
                allowed.add(line)
    for h in sorted(hosts):
        e = hosts[h]
        files = sorted(e["files"])
        rep.note("network", "%-45s %-10s %3d file(s)  %s" % (h, "/".join(sorted(e["schemes"])), len(files),
                                                            ", ".join(files[:3]) + (" ..." if len(files) > 3 else "")))
        if allowed and not any(h == a or h.endswith("." + a) for a in allowed):
            rep.add("MED", "network", files[0], "host not on the allowlist: %s" % h)
        if "ftp" in e["schemes"]:
            rep.add("LOW", "network", files[0], "plain FTP host: %s" % h)
    if out_hosts:
        with open(out_hosts, "w", encoding="utf-8") as fh:
            fh.write("# hosts named in %s (one per line; use as --allow-hosts after review)\n" % root)
            fh.write("\n".join(sorted(hosts)) + "\n")
    return hosts


# ------------------------------------------------------------------ 4. lumo diff review
def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=3):
            return True
    except Exception:            # noqa: BLE001 - any failure = not reachable
        return False


def git(root, *args):
    r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True)
    return r.returncode, r.stdout


def lumo_review(root, rep, base, max_files=25):
    rc, _ = git(root, "rev-parse", "--verify", base)
    if rc:
        base = "HEAD~1"
        rep.note("lumo", "base not found; reviewing the last commit (HEAD~1..HEAD)")
    rc, names = git(root, "diff", "--name-only", "%s...HEAD" % base)
    if rc:
        rc, names = git(root, "diff", "--name-only", base)
    rc2, wt = git(root, "diff", "--name-only")
    files = [f for f in dict.fromkeys((names + wt).split()) if f.endswith((".py",) + WEB_EXT)
             and not is_test(f) and os.path.isfile(os.path.join(root, f))]
    if not files:
        rep.note("lumo", "no changed source files vs %s" % base)
        return
    if not ollama_up():
        rep.note("lumo", "Ollama is not running at %s (lumo-start), review skipped" % OLLAMA)
        return
    for f in files[:max_files]:
        rc, diff = git(root, "diff", "-U12", base, "--", f)
        if not diff.strip():
            continue
        prompt = ("Review this change to %s from a QGIS dam-safety plugin suite. List only REAL bugs the "
                  "change introduces (wrong results, crashes, data loss, security). For each give: line, "
                  "the problem, and a unit test that would fail because of it. Reply as JSON: "
                  "{\"findings\":[{\"line\":N,\"problem\":\"...\",\"test\":\"...\"}]}. If nothing is wrong "
                  "reply {\"findings\":[]}. Do not invent code that is not in the diff.\n\n%s"
                  % (f, diff[:14000]))
        body = json.dumps({"model": LUMO_MODEL, "messages": [{"role": "user", "content": prompt}],
                           "stream": False, "think": False, "format": "json",
                           "options": {"num_ctx": int(os.environ.get("LUMO_NUM_CTX", "16384"))}}).encode()
        try:
            req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=600) as r:
                txt = json.load(r).get("message", {}).get("content", "")
            found = json.loads(re.sub(r"<think>.*?</think>", "", txt, flags=re.S)).get("findings", [])
        except Exception as e:   # noqa: BLE001
            rep.note("lumo", "%s: model call failed (%s)" % (f, e))
            continue
        for x in found[:8]:
            rep.add("INFO", "lumo", "%s:%s" % (f, x.get("line", "?")),
                    "UNVERIFIED (model): %s\n    test that would show it: %s"
                    % (str(x.get("problem", ""))[:400], str(x.get("test", ""))[:300]))
        rep.note("lumo", "%s: %d finding(s)" % (f, len(found)))
    if len(files) > max_files:
        rep.note("lumo", "%d more changed files not reviewed (--lumo-max)" % (len(files) - max_files))


# ------------------------------------------------------------------ 5. golden runs
METRICS = {  # key in run_report summary: (relative tolerance, absolute tolerance)
    "peak_breach_cfs": (0.05, 50.0),
    "flooded_acres": (0.05, 5.0),
    "max_depth_ft": (0.05, 0.5),
    "max_velocity_fps": (0.10, 0.5),
    "storage_at_failure_acft": (0.02, 1.0),
    "latest_arrival_hr": (0.10, 0.25),
}


def find_values(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_values(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_values(v, key)


def dam_metrics(results, dam):
    """The newest run_report.json under <results>/<dam>/ -> metrics dict (or None)."""
    folder = os.path.join(results, dam)
    reps = []
    for d, _dirs, files in os.walk(folder):
        if "run_report.json" in files:
            p = os.path.join(d, "run_report.json")
            reps.append((os.path.getmtime(p), p))
    if not reps:
        return None
    path = max(reps)[1]
    try:
        r = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    s = r.get("summary") or {}
    m = {k: s.get(k) for k in METRICS if isinstance(s.get(k), (int, float))}
    for k, v in s.items():
        if "class" in k.lower() and isinstance(v, str):
            m[k] = v
    errs = [abs(float(v)) for v in find_values(r, "max_volume_error_pct") if isinstance(v, (int, float))]
    if errs:
        m["max_volume_error_pct"] = max(errs)
    m["_report"] = path
    m["_when"] = datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")
    with open(path, "rb") as fh:
        m["_sha256"] = hashlib.sha256(fh.read()).hexdigest()
    return m


def golden(results, ids, baseline, record, rep):
    results = os.path.expanduser(results)
    if record:
        if not ids:
            ids = sorted(d for d in os.listdir(results) if os.path.isdir(os.path.join(results, d))
                         and not d.startswith(("_", ".")))
        data = {"recorded": datetime.now().isoformat(timespec="seconds"), "results": results, "dams": {}}
        for d in ids:
            m = dam_metrics(results, d)
            if m:
                data["dams"][d] = m
                rep.note("golden", "%s: recorded %s" % (d, ", ".join("%s=%s" % (k, v) for k, v in m.items()
                                                                       if not k.startswith("_"))))
            else:
                rep.add("MED", "golden", d, "no run_report.json under %s/%s (run the dam first)" % (results, d))
        os.makedirs(os.path.dirname(baseline), exist_ok=True)
        with open(baseline, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        rep.note("golden", "baseline written: %s (%d dams)" % (baseline, len(data["dams"])))
        return
    if not os.path.isfile(baseline):
        rep.add("HIGH", "golden", baseline, "no baseline; record one first with --golden-record")
        return
    base = json.load(open(baseline, encoding="utf-8"))
    rep.note("golden", "baseline %s (recorded %s)" % (baseline, base.get("recorded")))
    for d, want in sorted(base.get("dams", {}).items()):
        if ids and d not in ids:
            continue
        got = dam_metrics(results, d)
        if not got:
            rep.add("MED", "golden", d, "no run_report.json for this dam under %s" % results)
            continue
        if got.get("_sha256") and got.get("_sha256") == want.get("_sha256"):
            rep.add("LOW", "golden", d, "same report as the baseline (the dam was not run again)")
        bad = []
        for k, (rtol, atol) in METRICS.items():
            a, b = want.get(k), got.get(k)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                if abs(b - a) > max(atol, rtol * abs(a)):
                    bad.append("%s %s -> %s (%+.1f%%)" % (k, a, b, 100.0 * (b - a) / a if a else 0.0))
            elif isinstance(a, (int, float)) and b is None:
                bad.append("%s missing (was %s)" % (k, a))
        for k in [k for k in want if "class" in k.lower() and not k.startswith("_")]:
            if want.get(k) != got.get(k):
                bad.append("%s %s -> %s" % (k, want.get(k), got.get(k)))
        ve = got.get("max_volume_error_pct")
        if isinstance(ve, (int, float)) and ve > 1.0:
            rep.add("HIGH", "golden", d, "LISFLOOD-FP volume error %.1f %% (over 1 %%)" % ve)
        if bad:
            rep.add("HIGH", "golden", d, "differs from the baseline: " + "; ".join(bad))
        else:
            rep.note("golden", "%s: matches the baseline" % d)


# ------------------------------------------------------------------ report
def write_report(rep, root, args, started):
    os.makedirs(REPORTS, exist_ok=True)
    name = os.path.basename(os.path.abspath(root)) if root else "golden"
    stem = os.path.join(REPORTS, "audit_%s_%s" % (re.sub(r"[^\w.-]+", "_", name),
                                                    datetime.now().strftime("%Y-%m-%d_%H%M")))
    items = sorted(rep.items, key=lambda x: (SEV_ORDER[x["sev"]], x["check"], x["where"]))
    count = {s: sum(1 for x in items if x["sev"] == s) for s in SEV_ORDER}
    head = ""
    if root:
        rc, sha = git(root, "rev-parse", "--short", "HEAD")
        rc2, br = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        head = "%s @ %s (%s)" % (root, sha.strip(), br.strip()) if not rc else root
    L = ["# Repo audit: %s" % name, "",
         "%s. %s. HIGH %d, MED %d, LOW %d, INFO %d. %.1f min." % (
             datetime.now().strftime("%Y-%m-%d %H:%M"), head or "golden runs", count["HIGH"], count["MED"],
             count["LOW"], count["INFO"], (time.time() - started) / 60), "",
         "HIGH fails the audit. Lumo findings are UNVERIFIED until a failing test shows them.", ""]
    for check in ("release", "security", "lint", "network", "lumo", "golden"):
        sel = [x for x in items if x["check"] == check]
        if not sel and check not in rep.notes:
            continue
        L += ["## %s" % check, ""]
        for n in rep.notes.get(check, [])[:400] if check != "network" else []:
            L.append("- " + n)
        if check == "network" and rep.notes.get("network"):
            L += ["```"] + rep.notes["network"] + ["```"]
        if sel:
            L.append("")
            for x in sel[:600]:
                L.append("- **%s** `%s` %s" % (x["sev"], x["where"], x["msg"].replace("\n", "\n  ")))
            if len(sel) > 600:
                L.append("- ... %d more in the .json" % (len(sel) - 600))
        L.append("")
    with open(stem + ".md", "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    with open(stem + ".json", "w", encoding="utf-8") as fh:
        json.dump({"repo": root, "head": head, "when": datetime.now().isoformat(timespec="seconds"),
                   "counts": count, "notes": rep.notes, "findings": items}, fh, indent=1)
    return stem, count


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local audit report for a dam-suite repo.")
    ap.add_argument("--repo", default="", help="the repo checkout to audit")
    ap.add_argument("--only", default="", help="comma list of release,security,network,lumo")
    ap.add_argument("--zips", default="", help="folder of built plugin zips to check against the repo")
    ap.add_argument("--no-gate", action="store_true", help="do not run the repo's tools/ci_checks.py")
    ap.add_argument("--allow-hosts", default="", help="file of allowed hosts (one per line)")
    ap.add_argument("--lumo", action="store_true", help="have the local model review the diff (triage only)")
    ap.add_argument("--base", default="origin/main", help="diff base for --lumo (default origin/main)")
    ap.add_argument("--lumo-max", type=int, default=25, help="most changed files to send the model")
    g = ap.add_argument_group("golden runs")
    g.add_argument("--golden-record", action="store_true", help="record the results under --results as the baseline")
    g.add_argument("--golden", action="store_true", help="compare the results under --results with the baseline")
    g.add_argument("--results", default="", help="results folder, one folder per dam (e.g. ~/DamSafety/Dams)")
    g.add_argument("--ids", default="", help="comma list of dam folders (default: all in the baseline)")
    g.add_argument("--baseline", default="", help="baseline file (default ~/local-ai/golden/<results folder>.json)")
    a = ap.parse_args(argv)
    if not a.repo and not (a.golden or a.golden_record):
        ap.error("give --repo, or --golden / --golden-record with --results")
    started = time.time()
    rep = Report()
    root = os.path.abspath(os.path.expanduser(a.repo)) if a.repo else ""
    only = set(x.strip() for x in a.only.split(",") if x.strip()) or {"release", "security", "network"}
    if a.lumo:
        only.add("lumo")
    if root:
        if not os.path.isdir(root):
            sys.exit("no such folder: %s" % root)
        if "release" in only:
            say("1/4 release and packaging ...")
            check_release(root, rep, a.zips or None, run_gate=not a.no_gate)
        if "security" in only:
            say("2/4 security patterns%s ..." % (" + bandit/ruff" if find_tool("bandit") or find_tool("ruff") else ""))
            scan_security(root, rep)
        if "network" in only:
            say("3/4 network inventory ...")
            os.makedirs(REPORTS, exist_ok=True)
            hosts_file = os.path.join(REPORTS, "hosts_%s.txt" % os.path.basename(root))
            hosts = network_inventory(root, rep, a.allow_hosts or None, hosts_file)
            rep.note("release", "network: %d hosts named; list for IT: %s" % (len(hosts), hosts_file))
        if "lumo" in only:
            say("4/4 Lumo review of the changes vs %s ..." % a.base)
            lumo_review(root, rep, a.base, a.lumo_max)
    if a.golden or a.golden_record:
        if not a.results:
            ap.error("--golden/--golden-record need --results")
        base = a.baseline or os.path.join(GOLDEN_DIR, re.sub(r"[^\w.-]+", "_",
                                          os.path.basename(os.path.normpath(os.path.expanduser(a.results)))) + ".json")
        ids = [x.strip() for x in a.ids.split(",") if x.strip()]
        say("golden runs: %s ..." % ("recording the baseline" if a.golden_record else "comparing with the baseline"))
        golden(a.results, ids, base, a.golden_record, rep)
    stem, count = write_report(rep, root, a, started)
    say("\nHIGH %d  MED %d  LOW %d  INFO %d" % (count["HIGH"], count["MED"], count["LOW"], count["INFO"]))
    for x in sorted(rep.items, key=lambda x: SEV_ORDER[x["sev"]])[:12]:
        if x["sev"] in ("HIGH", "MED"):
            say("  %-4s %-8s %s  %s" % (x["sev"], x["check"], x["where"], x["msg"].splitlines()[0][:110]))
    say("Report: %s.md" % stem)
    return 1 if count["HIGH"] else 0


if __name__ == "__main__":
    sys.exit(main())
