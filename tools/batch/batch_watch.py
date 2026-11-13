#!/usr/bin/env python3
"""Watch a dams folder while a batch runs and report findings as they happen.

Every --every minutes it looks for what changed under the folder:
  * new lines in the chain logs (_chain_logs/, _batch44_logs/, _county_logs/) that say
    WARNING / ERROR / FAILED / Traceback,
  * Runner reports that appeared or changed (<dam>/**/run_report.json),
and checks each finished dam BY RULE (code, no model):
  * the run failed or wrote no report,
  * LISFLOOD-FP volume error over 1 % (the stability re-run should have fixed it),
  * the flood reached DEM gaps (NoData) or the domain cut-off with the edge still moving,
  * the grid-spread advisory (coarse-grid over-spread),
  * culverts cut in as unverified guesses,
  * zero flooded area, or a breach that did not occur.

The rule findings are the result. With --with-model the facts (never whole rasters) also go
to the local model (Ollama, model 'heim') for a short note on what to check; that note is
advisory and is OFF by default, because a model answering while the solver runs takes the
cores the run needs, and a local model used as a live watcher has timed out and got rules
backwards before. Ask it afterwards:  python3 batch_watch.py <folder> --once --with-model

Output: BATCH_FINDINGS.md in the watched folder (newest at the bottom), the same lines in this
Terminal, and a macOS notification for every dam with a finding (--no-notify turns it off).

  python3 batch_watch.py ~/ND_Dams                 # watch until Ctrl-C
  python3 batch_watch.py ~/ND_Dams --once          # one pass over what is there now
  python3 batch_watch.py ~/DamSafety/Dams44_ANUGA --every 5
Env: OLLAMA_URL (default http://127.0.0.1:11434), HEIM_MODEL (default heim).

This was lumo_watch.py in the batch zips. It asked a model called 'lumo', which has not
existed since the local AI was renamed, so its model note had been failing on every dam.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("HEIM_MODEL") or os.environ.get("LUMO_MODEL") or "heim"
LOG_HIT = re.compile(r"\b(WARNING|ERROR|FAILED|Traceback|Exception)\b")
GAP_HIT = re.compile(r"no ?data|gap in the dem|dem gap|touches .*gap", re.I)
EDGE_HIT = re.compile(r"cut-?off|edge (is )?still moving|run limit|max(imum)? iterations", re.I)


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class State:
    def __init__(self, path):
        self.path = path
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:                                                    # noqa
            d = {}
        self.log_pos = d.get("log_pos", {})
        self.reports = d.get("reports", {})

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"log_pos": self.log_pos, "reports": self.reports}, fh)
        os.replace(tmp, self.path)


def new_log_lines(root, st):
    """Lines matching LOG_HIT added to any chain / batch log since the last pass."""
    hits = []
    # _county_logs was missing: run_county.py writes its console logs there, so a county run's
    # failures never reached the findings file
    pats = ["_chain_logs/**/*.log", "_batch44_logs/**/*.log", "_county_logs/**/*.log",
            "*/_chain/**/*.log", "part*/_chain_logs/**/*.log"]
    files = set()
    for p in pats:
        files.update(glob.glob(os.path.join(root, p), recursive=True))
    for f in sorted(files):
        try:
            size = os.path.getsize(f)
        except OSError:
            continue
        pos = st.log_pos.get(f, 0)
        if size < pos:
            pos = 0
        if size == pos:
            continue
        with open(f, encoding="utf-8", errors="replace") as fh:
            fh.seek(pos)
            chunk = fh.read()
            st.log_pos[f] = fh.tell()
        for ln in chunk.splitlines():
            if LOG_HIT.search(ln):
                hits.append((os.path.relpath(f, root), ln.strip()[:300]))
    return hits


def walk(obj, key):
    """Every value stored under `key` anywhere in a nested report."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            out += walk(v, key)
    elif isinstance(obj, list):
        for v in obj:
            out += walk(v, key)
    return out


def check_report(path):
    """(dam, facts dict, rule findings list) from one run_report.json."""
    with open(path, encoding="utf-8") as fh:
        rep = json.load(fh)
    summ = rep.get("summary") or {}
    dam = summ.get("dam_id") or rep.get("dam_id") or path.split(os.sep)[-3]
    warns = [str(w) for w in (rep.get("warnings") or [])]
    find = []
    errs = [float(v) for v in walk(rep, "max_volume_error_pct") if isinstance(v, (int, float))]
    if errs and max(abs(e) for e in errs) > 1.0:
        find.append("LISFLOOD-FP volume error up to %.1f %% (over 1 %%)" % max(errs, key=abs))
    if summ.get("breach_occurs") is False:
        find.append("no breach occurred (check the dam data: height, storage, top of dam)")
    acres = summ.get("flooded_acres")
    if isinstance(acres, (int, float)) and acres <= 0:
        find.append("flooded area is 0 ac")
    if rep.get("grid_spread_check", {}).get("flagged") if isinstance(rep.get("grid_spread_check"), dict) else False:
        find.append("grid-spread advisory: likely coarse-grid over-spread")
    for w in warns:
        if GAP_HIT.search(w):
            find.append("DEM gap: " + w[:200])
        elif EDGE_HIT.search(w):
            find.append("domain: " + w[:200])
    n_guess = len(rep.get("culvert_guesses_cut") or [])        # DamSafety Runner
    if n_guess:
        find.append("%d culvert(s) cut in as UNVERIFIED guesses (check sizes near receptors)" % n_guess)
    n_match = len(rep.get("culvert_matches") or [])
    culv = {"guessed_cut": n_guess, "inventory_matches": n_match,
            "anuga_culvert_inverts": bool(rep.get("anuga_culvert_inverts"))}
    facts = {"dam": dam, "report": path, "summary": summ, "warnings": warns[:25],
             "volume_error_pct": errs[-5:], "stability": rep.get("lisflood_stability"),
             "grid_refine": rep.get("grid_refine"), "crest_burn": summ.get("crest_burn"),
             "culverts": culv}
    return dam, facts, find


def ask_model(facts, find, timeout):
    prompt = ("You are reviewing a dam-breach flood run from the ND dam-safety suite, running "
              "headless. Below are FACTS from the Runner's report and RULE FINDINGS from a checker. "
              "In at most 6 short lines: (1) is anything wrong or suspicious, citing the facts; "
              "(2) what the engineer should check next; (3) what can be ignored. Do not invent "
              "numbers that are not in the facts; say UNSURE when unsure.\n\nRULE FINDINGS:\n%s\n\n"
              "FACTS:\n%s" % ("\n".join("- " + f for f in find) or "- none",
                              json.dumps(facts, indent=1, default=str)[:12000]))
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "think": False,
                       "options": {"num_ctx": int(os.environ.get("HEIM_NUM_CTX")
                                                    or os.environ.get("LUMO_NUM_CTX") or "16384")}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # nosec B310 - the local Ollama server
            txt = json.load(r).get("message", {}).get("content", "")
        return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip() or \
            "(the model returned nothing; rule findings above still stand)"
    except Exception as e:                                                   # noqa
        return "(model unavailable at %s: %s; rule findings above still stand)" % (OLLAMA, e)


def notify(title, text):
    if sys.platform != "darwin":
        return
    t = text.replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", 'display notification "%s" with title "%s"' % (t, title)],
                   capture_output=True)


def emit(out, lines):
    block = "\n".join(lines) + "\n"
    print(block, flush=True)
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(block + "\n")


def one_pass(root, st, out, args):
    hits = new_log_lines(root, st)
    if hits:
        lines = ["## %s  log lines" % now()]
        lines += ["- `%s`: %s" % (f, ln) for f, ln in hits[-40:]]
        if len(hits) > 40:
            lines.append("- (%d more)" % (len(hits) - 40))
        emit(out, lines)
    for path in sorted(glob.glob(os.path.join(root, "**", "run_report.json"), recursive=True)):
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        if st.reports.get(path) == mt:
            continue
        try:
            dam, facts, find = check_report(path)
        except Exception as e:                                               # noqa
            emit(out, ["## %s  %s" % (now(), path), "- report not readable yet: %s" % e])
            continue
        st.reports[path] = mt
        s = facts["summary"]
        lines = ["## %s  %s  (%s)" % (now(), dam, os.path.relpath(path, root)),
                 "- result: %s ac flooded, max depth %s ft, peak %s cfs, cell %s m, model %s" % (
                     s.get("flooded_acres"), s.get("max_depth_ft"), s.get("peak_breach_cfs"),
                     s.get("cell_m"), s.get("final_model") or s.get("lisflood_solver"))]
        lines += ["- RULE: " + f for f in find] or ["- RULE: nothing flagged"]
        if args.with_model and (find or args.model_every_dam):
            lines += ["- heim (advisory):"] + ["    " + ln for ln in ask_model(facts, find, args.timeout).splitlines()]
        emit(out, lines)
        if find and not args.no_notify:
            notify("Dam run: " + str(dam), find[0])
    st.save()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--every", type=float, default=2.0, help="minutes between passes (default 2)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--with-model", action="store_true",
                    help="also ask the local model for a note on each flagged dam (advisory; off by default)")
    ap.add_argument("--no-model", action="store_true", help="accepted for old command lines; it is the default")
    ap.add_argument("--model-every-dam", action="store_true",
                    help="ask the model about every finished dam, not only flagged ones")
    ap.add_argument("--timeout", type=int, default=600, help="seconds for one model answer")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()
    root = os.path.abspath(os.path.expanduser(args.folder))
    os.makedirs(root, exist_ok=True)
    out = os.path.join(root, "BATCH_FINDINGS.md")
    state = os.path.join(root, ".batch_watch_state.json")
    legacy = os.path.join(root, ".lumo_watch_state.json")
    if not os.path.exists(state) and os.path.exists(legacy):
        # a batch started under the old watcher: carry on from where it had read to, so every
        # finished dam is not reported a second time
        st = State(legacy)
        st.path = state
    else:
        st = State(state)
    if args.no_model:
        args.with_model = False
    emit(out, ["# %s  watching %s (%s)" % (now(), root, ("rules, and model %s at %s" % (MODEL, OLLAMA))
                                           if args.with_model else "rules only")])
    try:
        while True:
            one_pass(root, st, out, args)
            if args.once:
                break
            time.sleep(max(10.0, args.every * 60.0))
    except KeyboardInterrupt:
        one_pass(root, st, out, args)
        emit(out, ["# %s  stopped" % now()])


if __name__ == "__main__":
    main()
