#!/usr/bin/env python3
# audit_plugin.py v1.3 — whole-folder Python error auditor (stdlib only)
#
# Two passes per file:
#   1. STATIC (exact, no model): syntax errors, sys.exit/os._exit/exit/quit,
#      bare or swallowing except blocks, gdal.DontUseExceptions(), `is` against
#      a literal. Line numbers are real. Run alone with --static-only.
#   2. MODEL (advisory): the local model reads the file with line numbers, in
#      windows split at top-level def/class boundaries so nothing is truncated.
#
# v1.3 fixes (vs v1.2):
#   - v1.2 rebuilt files by concatenating overlapping chunks, so the model saw
#     200 duplicated characters at every 1,300-character boundary and reported
#     phantom syntax errors. Files are now read from disk (the indexed folder or
#     --path); the DB fallback strips the overlap.
#   - Files over 60,000 chars were cut off; they are now audited in windows.
#   - num_ctx is sent per request and the window is sized to fit, so Ollama
#     never silently trims the front of the prompt (the instructions).
#   - The verdict is parsed from the "FILE VERDICT:" line after any <think>
#     block, not by looking for "CLEAN" anywhere in the first 200 chars (the
#     echoed format line "CLEAN | ISSUES FOUND" counted as clean).
#   - Only .py files are audited (v1.2 sent README.md and JSON to a Python audit).
#   - Sampling follows Qwen3's thinking-mode advice (temperature 0.1 makes
#     thinking models loop until the 30-minute timeout).
#   - Ctrl+C writes the partial report instead of losing it.
#   - --changed REF audits only files changed since a git ref.
import os, sys, re, ast, json, sqlite3, argparse, subprocess, urllib.request, urllib.error
from datetime import datetime

DB = os.path.expanduser(os.environ.get("PIERRE_DB", "~/local-ai/knowledge.db"))
CHAT_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/chat"
MODEL = os.environ.get("PIERRE_MODEL", "pierre")
NUM_CTX = int(os.environ.get("PIERRE_NUM_CTX", "16384"))
OUT_RESERVE_TOKENS = 6144       # thinking + answer
PROMPT_TOKENS = 900             # the audit instructions themselves
CHARS_PER_TOKEN = 3.0           # conservative for Python source
MAX_WINDOW_CHARS = 60000
INDEX_OVERLAP = 200             # must equal OVERLAP in rag_index.py
REPORT_DIR = os.path.expanduser("~/local-ai/reports")
SKIP_DIRS = {"__MACOSX", ".git", "node_modules", "__pycache__", ".venv", "releases"}
SCRIPT_DIRS = {"tests", "test", "scripts", "tools"}

AUDIT_PROMPT = """You are performing a focused Python error audit on part of one file from the
{collection} codebase (QGIS plugin, dam-hazard domain; runs in QGIS 3.34 / Python 3.12
from conda-forge on macOS, and CI also runs it on QGIS 4 / Qt6).

This is lines {first}-{last} of {total}. Other parts of the file are audited separately,
so a name defined outside these lines is NOT an error. Each line starts with its real
line number and "|"; cite those numbers.

Audit for:
1. NoneType operands where strings or numbers are expected (TypeError-prone lines)
2. Filename-matching logic that is too broad (e.g. single-letter stem matches
   misclassifying units) or mishandles ._*, .DS_Store, __MACOSX
3. Silent failures: swallowed exceptions, or paths that write empty output without logging
4. sys.exit() / os._exit() reachable from code running inside QGIS
5. GDAL exception-handling misuse
6. Logic bugs: wrong comparison operators, off-by-one, unreachable code,
   wrong units (ft vs m, cfs vs m3/s), wrong variable used
Syntax errors are checked separately by a compiler; do not report them.

Format your answer EXACTLY as:
FILE VERDICT: <CLEAN or ISSUES FOUND>
ISSUES:
- [SEVERITY: low/med/high] LINE <n>: description
(if CLEAN, write "ISSUES: none")

Report real defects only, no style. If unsure, prefix the item with UNSURE.

=== FILE: {path} (lines {first}-{last} of {total}) ===
{code}
"""

VERDICT_RE = re.compile(r"FILE VERDICT:\s*\**\s*(CLEAN|ISSUES FOUND)", re.I)
THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


# ----------------------------------------------------------------- static pass
def _main_guard_ranges(tree):
    out = []
    for node in tree.body:
        if isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                    and t.left.id == "__name__"):
                out.append((node.lineno, getattr(node, "end_lineno", node.lineno)))
    return out


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def static_findings(src, path):
    """Exact checks. Returns (findings, tree or None)."""
    found = []
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        found.append(("high", e.lineno or 0, "SyntaxError: %s" % e.msg))
        return found, None
    in_script = bool(SCRIPT_DIRS & set(path.replace("\\", "/").split("/")[:-1]))
    guards = _main_guard_ranges(tree)

    def guarded(line):
        return any(a <= line <= b for a, b in guards)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in ("sys.exit", "os._exit", "exit", "quit"):
                if in_script or guarded(node.lineno):
                    sev, note = "low", " (script/test or __main__ block; fine unless imported by the plugin)"
                else:
                    sev, note = "high", " — terminates QGIS if reached from plugin code"
                found.append((sev, node.lineno, "%s() call%s" % (name, note)))
            elif name and name.endswith("DontUseExceptions"):
                found.append(("med", node.lineno, "%s() — GDAL errors become silent None returns" % name))
        elif isinstance(node, ast.ExceptHandler):
            body = node.body
            swallow = all(isinstance(b, (ast.Pass, ast.Continue)) or
                          (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))
                          for b in body)
            if node.type is None:
                found.append(("med", node.lineno, "bare except: (also catches KeyboardInterrupt/SystemExit)"
                              + ("; body swallows it silently" if swallow else "")))
            elif swallow:
                found.append(("med", node.lineno, "except %s: swallowed silently (no log)"
                              % (ast.unparse(node.type) if hasattr(ast, "unparse") else "...")))
        elif isinstance(node, ast.Compare):
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.Is, ast.IsNot)) and isinstance(right, ast.Constant) \
                        and right.value is not None and not isinstance(right.value, bool) \
                        and right.value is not Ellipsis:
                    found.append(("med", node.lineno, "`is` compared with literal %r (identity, not equality)"
                                  % (right.value,)))
    # Swallowed excepts are often deliberate (best-effort cleanup), so they are
    # collapsed into one low line per file instead of drowning the real findings.
    sw = [f for f in found if f[2].startswith("except ") and "swallowed silently" in f[2]]
    found = [f for f in found if f not in sw]
    if sw:
        found.append(("low", sw[0][1], "%d except blocks swallow errors without logging, lines %s "
                      "(check the ones whose failure would leave a wrong or empty result)"
                      % (len(sw), ", ".join(str(f[1]) for f in sorted(sw, key=lambda f: f[1])))))
    found.sort(key=lambda f: f[1])
    return found, tree


# ------------------------------------------------------------- model windows
def window_chars():
    budget = (NUM_CTX - OUT_RESERVE_TOKENS - PROMPT_TOKENS) * CHARS_PER_TOKEN
    return int(max(8000, min(MAX_WINDOW_CHARS, budget)))


def split_windows(src, tree, limit):
    """Yield (first_line, last_line, numbered_text). Splits at top-level statements."""
    lines = src.splitlines()
    total = len(lines)
    if total == 0:
        return
    numbered = ["%5d| %s" % (i + 1, l) for i, l in enumerate(lines)]
    starts = [1]
    if tree is not None:
        for node in tree.body:
            ln = node.lineno
            decos = getattr(node, "decorator_list", None) or []
            if decos:
                ln = min([ln] + [d.lineno for d in decos])
            if ln > 1:
                starts.append(ln)
    starts = sorted(set(starts))
    blocks = []                                  # (first, last) 1-based inclusive
    for i, s in enumerate(starts):
        e = (starts[i + 1] - 1) if i + 1 < len(starts) else total
        blocks.append((s, e))
    win_first, win_len = None, 0
    cur_last = 0
    for s, e in blocks:
        size = sum(len(numbered[k]) + 1 for k in range(s - 1, e))
        if size > limit:                         # one huge block: flush, then hard-split it
            if win_first is not None:
                yield win_first, cur_last, "\n".join(numbered[win_first - 1:cur_last])
                win_first, win_len = None, 0
            a, acc = s, 0
            for k in range(s - 1, e):
                acc += len(numbered[k]) + 1
                if acc > limit and k >= a:
                    yield a, k, "\n".join(numbered[a - 1:k])
                    a, acc = k + 1, len(numbered[k]) + 1
            yield a, e, "\n".join(numbered[a - 1:e])
            continue
        if win_first is not None and win_len + size > limit:
            yield win_first, cur_last, "\n".join(numbered[win_first - 1:cur_last])
            win_first, win_len = None, 0
        if win_first is None:
            win_first = s
        win_len += size
        cur_last = e
    if win_first is not None:
        yield win_first, cur_last, "\n".join(numbered[win_first - 1:cur_last])


def chat(prompt, think=True, timeout=1800):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        # Qwen3 thinking-mode sampling; greedy/low temperature makes it repeat itself.
        "options": {"temperature": 0.6, "top_p": 0.95, "top_k": 20,
                    "num_ctx": NUM_CTX, "num_predict": OUT_RESERVE_TOKENS},
    }
    if think:
        payload["think"] = True
    req = urllib.request.Request(CHAT_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if think and e.code == 400:              # Ollama older than 0.9: no "think" field
            return chat(prompt, think=False, timeout=timeout)
        raise
    content = (out.get("message") or {}).get("content") or ""
    content = THINK_RE.sub("", content).strip()
    if out.get("done_reason") == "length":
        content += "\n[model hit num_predict before finishing — verdict may be incomplete]"
    pe = out.get("prompt_eval_count")
    if isinstance(pe, int) and pe >= NUM_CTX - 64:
        content += "\n[WARNING: prompt filled the whole context; Ollama may have trimmed it]"
    return content


def parse_verdict(text):
    m = VERDICT_RE.search(text)
    if not m:
        return "UNPARSED"
    return "CLEAN" if m.group(1).upper() == "CLEAN" else "ISSUES"


# ---------------------------------------------------------------- file source
def files_from_disk(root, changed_ref=None):
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        sys.exit("Not a directory: %s" % root)
    if changed_ref:
        try:
            top = subprocess.run(["git", "-C", root, "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            names = subprocess.run(["git", "-C", root, "diff", "--name-only", "--diff-filter=AMR",
                                    changed_ref, "--", "."], capture_output=True, text=True,
                                   check=True).stdout.split()
            names += subprocess.run(["git", "-C", root, "ls-files", "--others", "--exclude-standard", "--", "."],
                                    capture_output=True, text=True, check=True).stdout.split()
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            sys.exit("--changed needs a git checkout and a valid ref (%s)" % e)
        out = {}
        for n in sorted(set(names)):
            full = os.path.join(top, n)
            if n.endswith(".py") and os.path.isfile(full) and full.startswith(root + os.sep):
                out[os.path.relpath(full, root)] = full
        return out
    out = {}
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS)
        for fn in sorted(fns):
            if fn.endswith(".py") and not fn.startswith("._"):
                full = os.path.join(dp, fn)
                out[os.path.relpath(full, root)] = full
    return out


def collection_root(db, collection):
    try:
        row = db.execute("SELECT roots FROM roots WHERE collection=?", (collection,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    roots = json.loads(row[0])
    if len(roots) == 1 and os.path.isdir(roots[0]):
        return roots[0]
    return None


def files_from_db(db, collection):
    """Fallback when the indexed folder is gone: rebuild each file, removing the chunk overlap."""
    rows = db.execute("SELECT path, idx, txt FROM chunks WHERE collection = ? ORDER BY path, idx",
                      (collection,)).fetchall()
    files = {}
    for path, idx, txt in rows:
        if path.endswith(".py"):
            files.setdefault(path, []).append((idx, txt))
    out = {}
    for path, chunks in files.items():
        chunks.sort()
        idxs = [i for i, _ in chunks]
        if idxs != list(range(len(idxs))):
            print("  ! %s: chunk gap in index (indexed by rag_index < 2.4?) — reindex for an exact copy" % path)
        parts = [chunks[0][1]] + [t[INDEX_OVERLAP:] for _, t in chunks[1:]]
        out[path] = "".join(parts)
    return out


# ---------------------------------------------------------------------- main
def write_report(report_path, lines):
    tmp = report_path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, report_path)                 # atomic — no half-written reports


def main():
    global MODEL, NUM_CTX
    ap = argparse.ArgumentParser(description="Audit Python files: exact static checks + local-model review.")
    ap.add_argument("-c", "--collection", default=None,
                    help="indexed collection (reads its folder from disk when it still exists)")
    ap.add_argument("-p", "--path", default=None, help="audit this folder directly (no index needed)")
    ap.add_argument("-f", "--file", default=None, help="only files whose path contains this text")
    ap.add_argument("--changed", metavar="REF", default=None,
                    help="only .py files changed since this git ref (e.g. origin/main), plus untracked")
    ap.add_argument("--static-only", action="store_true", help="skip the model; exact checks only")
    ap.add_argument("--model", default=None)
    ap.add_argument("--num-ctx", type=int, default=None)
    args = ap.parse_args()
    if args.model:
        MODEL = args.model
    if args.num_ctx:
        NUM_CTX = args.num_ctx
    if not args.collection and not args.path:
        sys.exit("give -p <folder> or -c <collection>")

    label = args.collection or os.path.basename(os.path.normpath(os.path.expanduser(args.path)))
    sources = {}                                 # rel path -> (full path or None, text or None)
    if args.path:
        for rel, full in files_from_disk(args.path, args.changed).items():
            sources[rel] = (full, None)
    else:
        if not os.path.exists(DB):
            sys.exit("No index at %s. Run rag_index.py first, or use -p <folder>." % DB)
        db = sqlite3.connect(DB)
        root = collection_root(db, args.collection)
        if root:
            print("Reading files from disk: %s" % root)
            for rel, full in files_from_disk(root, args.changed).items():
                sources[rel] = (full, None)
        else:
            if args.changed:
                sys.exit("--changed needs the folder on disk; use -p <folder>.")
            print("Indexed folder not found; rebuilding files from the index.")
            for rel, txt in files_from_db(db, args.collection).items():
                sources[rel] = (None, txt)
        db.close()
    if args.file:
        sources = {p: v for p, v in sources.items() if args.file in p}
    if not sources:
        sys.exit("No .py files to audit for '%s'." % label)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(REPORT_DIR, exist_ok=True)
    report_path = os.path.join(REPORT_DIR, "audit_%s_%s.txt" % (label, stamp))
    limit = window_chars()
    lines = ["PYTHON ERROR AUDIT — %s" % label,
             "Model: %s (local Ollama), num_ctx %d, window %d chars   Run: %s"
             % ("none (--static-only)" if args.static_only else MODEL, NUM_CTX, limit, stamp),
             "Files: %d%s" % (len(sources), ("   changed since %s" % args.changed) if args.changed else ""),
             "=" * 70]
    n = {"issues": 0, "clean": 0, "failed": 0, "unparsed": 0, "static": 0}
    interrupted = False
    try:
        for i, rel in enumerate(sorted(sources), 1):
            full, text = sources[rel]
            if text is None:
                try:
                    with open(full, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError as e:
                    lines += ["", "-" * 70, "FILE: %s" % rel, "UNREADABLE: %s" % e]
                    n["failed"] += 1
                    continue
            print("[%d/%d] %s ... " % (i, len(sources), rel), end="", flush=True)
            sf, tree = static_findings(text, rel)
            block = ["", "-" * 70, "FILE: %s" % rel]
            if sf:
                n["static"] += 1
                block.append("STATIC (exact):")
                block += ["- [SEVERITY: %s] LINE %d: %s" % (s, ln, msg) for s, ln, msg in sf]
            verdicts = []
            if not args.static_only:
                wins = list(split_windows(text, tree, limit))
                for first, last, code in wins:
                    prompt = AUDIT_PROMPT.format(collection=label, path=rel, first=first, last=last,
                                                 total=len(text.splitlines()), code=code)
                    try:
                        ans = chat(prompt)
                    except Exception as e:
                        verdicts.append("FAILED")
                        block.append("MODEL lines %d-%d: AUDIT CALL FAILED: %s" % (first, last, e))
                        continue
                    if not ans.strip():
                        verdicts.append("FAILED")
                        block.append("MODEL lines %d-%d: EMPTY MODEL RESPONSE" % (first, last))
                        continue
                    v = parse_verdict(ans)
                    verdicts.append(v)
                    hdr = "MODEL (advisory)" + (" lines %d-%d" % (first, last) if len(wins) > 1 else "") + ":"
                    block += [hdr, ans]
            if "FAILED" in verdicts:
                n["failed"] += 1
                status = "CALL FAILED"
            elif "ISSUES" in verdicts:
                n["issues"] += 1
                status = "ISSUES FOUND"
            elif "UNPARSED" in verdicts:
                n["unparsed"] += 1
                status = "UNPARSED (read it)"
            else:
                n["clean"] += 1
                status = "clean" if not args.static_only else "done"
            if sf:
                status += " + %d static" % len(sf)
            print(status)
            lines += block
            write_report(report_path + ".inprogress", lines)   # survives a crash
    except KeyboardInterrupt:
        interrupted = True
        print("\n[interrupted — writing partial report]")

    if args.static_only:
        n["clean"] = 0
    lines += ["", "=" * 70,
              "SUMMARY: %d files — %d model ISSUES, %d clean, %d unparsed, %d call failures; "
              "%d files with static findings%s"
              % (len(sources), n["issues"], n["clean"], n["unparsed"], n["failed"], n["static"],
                 "   (INTERRUPTED — partial)" if interrupted else ""),
              "STATIC findings are exact. MODEL findings are advisory: confirm each one",
              "against the code (ideally with a failing test) before changing anything."]
    write_report(report_path, lines)
    try:
        os.remove(report_path + ".inprogress")
    except OSError:
        pass
    print("\nReport: %s" % report_path)
    print("Summary: %d model issues, %d clean, %d unparsed, %d failures, %d with static findings"
          % (n["issues"], n["clean"], n["unparsed"], n["failed"], n["static"]))


if __name__ == "__main__":
    main()
