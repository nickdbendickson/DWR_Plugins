#!/usr/bin/env python3
"""Do the model settings change the hazard class? The 44 dams, run more than once.

Each dam is run through the whole DamSafety chain (Screening, Runner, Hazard classification;
profile 'damsafety') once per setting, each setting in its own folder, and the classes are put
side by side. The question it answers: for how many of these dams does the class depend on the
cell size or on the engine, and which ones.

The settings (--variants; the names are the folder names):
  lf10   LISFLOOD-FP, 10 m cell. The recommended settings: every other one is compared to it.
  lf20   LISFLOOD-FP, 20 m cell.
  an10   ANUGA for the final results on LISFLOOD-FP's extent, 10 m cell, triangles up to
         100 m2 near the dam and 1,000 m2 elsewhere (the Runner's defaults).
  an20   ANUGA the same way, 20 m cell, triangles up to 400 m2 and 4,000 m2.
At 20 m the automatic finer grid is off (GRID_REFINE=false): with it on, a 20 m run that
over-spreads is re-run at 10 m and the comparison is gone.

Default: lf10,lf20 (hours). ANUGA takes hours per dam, so the two ANUGA settings are days for
44 dams: ask for them with --variants all, or try a few dams first with --only.

Usage:
  python3 run_settings_study_44.py --dry-run                 # print the commands, run nothing
  python3 run_settings_study_44.py --only DWR-2801,DWR-3034  # two dams, the two LISFLOOD-FP settings
  python3 run_settings_study_44.py --parallel 4              # all 44, 4 groups side by side
  python3 run_settings_study_44.py --variants all --parallel 4
  python3 run_settings_study_44.py --report-only             # rebuild the report from what is there

A dam that finished under a setting is skipped the next time (--redo runs it again), so the
study can be stopped and started again, and the ANUGA settings can be added later.

Results, in the study folder (default ~/DamSafety/Settings_Study_44):
  Settings_Study.html   one page: how many dams keep their class, the dams that do not
  Settings_Study.csv    one row per dam, every setting's class and counts
  <setting>/<dam>/      each run, complete, as the chain leaves it

A class that changes with the settings is not an error in either run. It marks a dam whose
class rests on something the model does not pin down, and that dam's class needs an engineer's
look. Nothing here decides a class.
"""
import argparse
import csv
import glob
import html
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from batch_common import find_damchain, need_data, say                      # noqa: E402

COMMON = ["SOLVER=1", "CREST_BURN=true", "GRID_CHECK_CELL=0", "REFINE_NEAR=false"]
# name -> (label, ANUGA?, Runner parameters on top of COMMON)
VARIANTS = {
    "lf10": ("LISFLOOD-FP, 10 m", False, ["CELL=10", "GRID_REFINE=true"]),
    "lf20": ("LISFLOOD-FP, 20 m", False, ["CELL=20", "GRID_REFINE=false"]),
    "an10": ("ANUGA, 10 m", True, ["CELL=10", "GRID_REFINE=true", "ANUGA_FINE=100", "ANUGA_COARSE=1000"]),
    "an20": ("ANUGA, 20 m", True, ["CELL=20", "GRID_REFINE=false", "ANUGA_FINE=400", "ANUGA_COARSE=4000"]),
}
ORDER = ("lf10", "lf20", "an10", "an20")
DEFAULT = "lf10,lf20"
REFERENCE = "lf10"
# What each pair isolates (the report counts the dams whose class differs in each)
PAIRS = (("lf10", "lf20", "cell size, LISFLOOD-FP (10 m against 20 m)"),
         ("an10", "an20", "cell size, ANUGA (10 m against 20 m)"),
         ("lf10", "an10", "engine at 10 m (LISFLOOD-FP against ANUGA)"),
         ("lf20", "an20", "engine at 20 m (LISFLOOD-FP against ANUGA)"))
RANK = {"low": 1, "medium": 2, "significant": 2, "high": 3}
SUMMARY_NAME = "Chain_Batch_Summary.csv"                 # DamSafety_Runner/core/chain.SUMMARY_NAME
SNAPSHOT = os.path.join("outputs", "QA", "Last_Run_Snapshot.json")   # Hazard's record of a run
CSV_NAME, HTML_NAME = "Settings_Study.csv", "Settings_Study.html"


# ---------------------------------------------------------------- the runs
def pick_variants(text):
    names = list(ORDER) if text.strip().lower() == "all" else [s.strip() for s in text.split(",") if s.strip()]
    bad = [n for n in names if n not in VARIANTS]
    if bad or not names:
        sys.exit("--variants: %s is not one of %s (or 'all')" % (", ".join(bad) or "nothing", ", ".join(ORDER)))
    return [n for n in ORDER if n in names]


def read_ids(path, only):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if only:
        want = [s.strip() for s in only.split(",") if s.strip()]
        missing = sorted(set(want) - {r["dam_id"] for r in rows})
        if missing:
            sys.exit("Not in %s: %s" % (os.path.basename(path), ", ".join(missing)))
        rows = [r for r in rows if r["dam_id"] in want]
    return rows


def command(py, tool, variant, ids, out, args):
    _label, anuga, extra = VARIANTS[variant]
    cmd = [py, tool, "--threads", "1"] + (["--quiet"] if args.parallel > 1 else []) + \
          (["--dry-run"] if args.dry_run else []) + [
        "run", "--set", "dwr", "--dams", args.dams, "--id-field", "dam_id", "--ids", ",".join(ids),
        "--out", out, "--hec", "skip", "--dem-units", args.dem_units]
    if anuga:
        cmd.append("--anuga")
    if args.dem:
        cmd += ["--dem", args.dem]
    if args.no_screen:
        cmd.append("--no-screen")
    for p in COMMON + extra:
        cmd += ["--runner-param", p]
    if args.redo:
        cmd.append("--redo")
    return cmd


def run_group(py, tool, variant, n, ids, args):
    root = os.path.join(args.study_folder, variant)
    out = root if args.parallel <= 1 else os.path.join(root, "part%d" % n)
    cmd = command(py, tool, variant, ids, out, args)
    say("%s group %d: %d dam(s) -> %s" % (variant, n, len(ids), out))
    say("  " + " ".join(cmd))
    if args.dry_run:
        return 0
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "study_console.log"), "a", encoding="utf-8") as fh:
        fh.write("\n$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                              env=dict(os.environ, OMP_NUM_THREADS="1")).returncode


def run_variant(py, tool, variant, ids, args):
    """Every dam under one setting. Returns the number of groups whose chain exited non-zero
    (the report reads each dam's own row, so this is only for the exit status and the log)."""
    groups = [ids[i::args.parallel] for i in range(max(1, args.parallel))]
    groups = [g for g in groups if g]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        rcs = list(pool.map(lambda ng: run_group(py, tool, variant, ng[0], ng[1], args),
                            enumerate(groups, 1)))
    failed = sum(1 for rc in rcs if rc)
    if not args.dry_run:
        say("%s: finished in %.0f min, %d of %d group(s) reported a failure" % (
            variant, (time.time() - t0) / 60.0, failed, len(groups)))
    return failed


# ---------------------------------------------------------------- reading the results
def summary_rows(variant_folder):
    """dam id -> its newest row of the chain summaries under a setting's folder (the folder
    itself and its partN folders: each chain run writes its own summary)."""
    out = {}
    paths = sorted(glob.glob(os.path.join(variant_folder, SUMMARY_NAME)) +
                   glob.glob(os.path.join(variant_folder, "part*", SUMMARY_NAME)),
                   key=lambda p: os.path.getmtime(p))
    for p in paths:
        try:
            with open(p, newline="", encoding="utf-8-sig") as fh:
                for r in csv.DictReader(fh):
                    if r.get("Dam"):
                        out[r["Dam"]] = dict(r, _summary=p)
        except (OSError, csv.Error):
            continue
    return out


def snapshot_counts(dam_folder):
    """What Hazard recorded for the run in this dam folder: flooded habitable structures,
    people in them, the deepest of them, the life-loss index, the first reason. {} if none."""
    try:
        with open(os.path.join(dam_folder, SNAPSHOT), encoding="utf-8") as fh:
            snap = json.load(fh)
    except (OSError, ValueError):
        return {}
    wet = [s for s in snap.get("structures") or []
           if s.get("outcome") not in (None, "", "Not inundated")]
    depths = [float(s["depth"]) for s in wet if s.get("depth") is not None]
    reasons = snap.get("nd_reasons") or []
    return {"structures": len(wet), "persons": sum(int(s.get("persons") or 0) for s in wet),
            "max_depth": max(depths) if depths else None, "lij": snap.get("lij"),
            "roads": len(snap.get("roads") or []), "reason": str(reasons[0]) if reasons else "",
            "incomplete": bool(snap.get("incomplete"))}


def _result(row):
    """One dam under one setting, from its summary row (None: not run):
    {"status": "done" | "failed" | "not run", "nd": class, ...}."""
    if not row:
        return {"status": "not run", "nd": "", "tm11": "", "message": ""}
    done = (row.get("Status") or "").strip().lower() == "done"
    res = {"status": "done" if done else "failed", "nd": (row.get("ND_Class") or "").strip() if done else "",
           "tm11": (row.get("TM11_Class") or "").strip() if done else "",
           "minutes": row.get("Minutes") or "", "message": (row.get("Message") or "")[:300]}
    if done and not res["nd"]:
        res["status"] = "failed"                       # a finished chain with no class is no result
        res["message"] = res["message"] or "the chain finished without a class"
    folder = row.get("Folder") or ""
    if done and folder:
        res.update(snapshot_counts(folder))
    return res


def collect(study_folder, variants, dams):
    """[{dam_id, name, existing, results: {variant: result}}] for the dams of the study."""
    per_variant = {v: summary_rows(os.path.join(study_folder, v)) for v in variants}
    out = []
    for d in dams:
        out.append({"dam_id": d["dam_id"], "name": d.get("name", ""),
                    "existing": d.get("existing_hazard_class", ""),
                    "results": {v: _result(per_variant[v].get(d["dam_id"])) for v in variants}})
    return out


def rank(cls):
    return RANK.get(str(cls or "").strip().lower(), 0)


def verdict(results, variants):
    """'same', 'changes', or 'incomplete' (a setting failed or was not run, and the ones that
    did finish agree: nothing can be said yet). A dam whose finished settings already disagree
    is 'changes' even while others are missing."""
    classes = [results[v]["nd"] for v in variants if results[v]["status"] == "done"]
    if len(set(c.lower() for c in classes)) > 1:
        return "changes"
    if len(classes) < len(variants) or len(classes) < 2:
        return "incomplete"
    return "same"


def pair_counts(rows, variants):
    """[(text, compared, differ, up, down)]: per pair of settings both run, how many dams were
    done under both, how many differ, and how many are higher / lower under the second."""
    out = []
    for a, b, text in PAIRS:
        if a not in variants or b not in variants:
            continue
        both = [(r["results"][a]["nd"], r["results"][b]["nd"]) for r in rows
                if r["results"][a]["status"] == "done" and r["results"][b]["status"] == "done"]
        diff = [(x, y) for x, y in both if x.lower() != y.lower()]
        up = sum(1 for x, y in diff if rank(y) > rank(x) > 0)
        down = sum(1 for x, y in diff if 0 < rank(y) < rank(x))
        out.append((text, len(both), len(diff), up, down))
    return out


# ---------------------------------------------------------------- the report
def write_csv(path, rows, variants):
    cols = ["dam_id", "name", "dwr_listed_class", "verdict"]
    for v in variants:
        cols += [v + "_" + k for k in ("status", "nd_class", "tm11_class", "structures", "persons",
                                       "max_depth_ft", "life_loss_index", "minutes", "message")]
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            line = [r["dam_id"], r["name"], r["existing"], verdict(r["results"], variants)]
            for v in variants:
                x = r["results"][v]
                line += [x["status"], x["nd"], x.get("tm11", ""), _show(x.get("structures")),
                         _show(x.get("persons")), _show(x.get("max_depth")), _show(x.get("lij")),
                         x.get("minutes", ""), x.get("message", "") if x["status"] != "done" else ""]
            w.writerow(line)
    os.replace(tmp, path)


def _show(v):
    return "" if v is None else ("%g" % v if isinstance(v, float) else str(v))


def _cell(x, ref):
    """One setting's cell in the table: the class, and what it rests on."""
    if x["status"] == "not run":
        return '<td class="na">not run</td>'
    if x["status"] != "done":
        return '<td class="fail">failed</td>'
    bits = []
    if x.get("structures") is not None:
        bits.append("%d structure%s" % (x["structures"], "" if x["structures"] == 1 else "s"))
    if x.get("max_depth") is not None:
        bits.append("deepest %.1f ft" % x["max_depth"])
    mark = ' class="diff"' if ref and x["nd"].lower() != ref.lower() else ""
    return "<td%s><b>%s</b>%s</td>" % (mark, html.escape(x["nd"]),
                                       "<br><small>%s</small>" % html.escape(", ".join(bits)) if bits else "")


def report_html(rows, variants, day, study_folder=""):
    v_all = [verdict(r["results"], variants) for r in rows]
    n = len(rows)
    same, changes, inc = v_all.count("same"), v_all.count("changes"), v_all.count("incomplete")
    e = html.escape
    head = "".join("<th>%s</th>" % e(VARIANTS[v][0]) for v in variants)
    out = ["<!doctype html><html><head><meta charset='utf-8'><title>Do the settings change the class?</title>",
           "<style>body{font:14px/1.45 -apple-system,Segoe UI,Arial,sans-serif;margin:24px;color:#111}"
           "h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 6px}"
           "table{border-collapse:collapse;margin:6px 0}th,td{border:1px solid #bbb;padding:4px 8px;"
           "text-align:left;vertical-align:top}th{background:#eee}td.diff{background:#ffe2b8}"
           "td.fail{background:#f6c9c9}td.na{color:#777}small{color:#444}.big{font-size:16px}"
           "p.note{color:#333;max-width:60em}@media print{body{margin:8mm}}</style></head><body>",
           "<h1>Do the model settings change the hazard class?</h1>",
           "<p>%d dam%s, %s. Made %s.</p>" % (n, "" if n == 1 else "s",
                                             e(" · ".join(VARIANTS[v][0] for v in variants)), e(day))]
    done_all = same + changes
    out.append("<p class='big'><b>%d of %d</b> dams have a different class under at least one setting. "
               "<b>%d</b> keep the same class under every setting.%s</p>" % (
                   changes, n, same,
                   " <b>%d</b> cannot be judged yet: a setting failed or has not been run." % inc if inc else ""))
    if len(variants) < 2:
        out.append("<p class='note'>Only one setting was run, so there is nothing to compare. "
                   "Run a second one (see --variants).</p>")
    pc = pair_counts(rows, variants)
    if pc:
        out.append("<h2>What changes it</h2><table><tr><th>Compared</th><th>Dams run both ways</th>"
                   "<th>Class differs</th><th>Higher under the second</th><th>Lower under the second</th></tr>")
        for text, both, diff, up, down in pc:
            out.append("<tr><td>%s</td><td>%d</td><td><b>%d</b></td><td>%d</td><td>%d</td></tr>" % (
                e(text), both, diff, up, down))
        out.append("</table>")

    def table(title, pick, note=""):
        sel = [r for r, v in zip(rows, v_all) if v == pick]
        if not sel:
            return
        out.append("<h2>%s (%d)</h2>%s" % (e(title), len(sel), "<p class='note'>%s</p>" % e(note) if note else ""))
        out.append("<table><tr><th>Dam</th><th>DWR lists it as</th>%s</tr>" % head)
        for r in sel:
            ref = next((r["results"][v]["nd"] for v in variants if r["results"][v]["status"] == "done"), "")
            out.append("<tr><td>%s<br><small>%s</small></td><td>%s</td>%s</tr>" % (
                e(r["dam_id"]), e(r["name"]), e(r["existing"]),
                "".join(_cell(r["results"][v], ref) for v in variants)))
        out.append("</table>")

    table("Dams whose class changes with the settings", "changes",
          "Shaded: a class different from the first setting that finished. These are the dams to "
          "look at: their class rests on something the model does not pin down.")
    table("Dams that cannot be judged yet", "incomplete",
          "A setting failed or has not been run, and the ones that finished agree so far.")
    table("Dams with the same class under every setting", "same")
    if done_all == 0 and n:
        out.append("<p class='note'>No dam has finished under two settings yet.</p>")
    out.append("<p class='note'>The classes are screening results of the DamSafety suite on these "
               "settings. A class that changes is not an error in either run, and nothing on this "
               "page decides a class. Every run is kept, complete, in its own folder%s. "
               "The same rows are in %s.</p>" % (
                   " under " + e(study_folder) if study_folder else "", CSV_NAME))
    out.append("</body></html>")
    return "\n".join(out)


def write_report(study_folder, variants, dams, day=None):
    rows = collect(study_folder, variants, dams)
    os.makedirs(study_folder, exist_ok=True)
    cpath, hpath = os.path.join(study_folder, CSV_NAME), os.path.join(study_folder, HTML_NAME)
    write_csv(cpath, rows, variants)
    tmp = hpath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(report_html(rows, variants, day or time.strftime("%Y-%m-%d %H:%M"), study_folder))
    os.replace(tmp, hpath)
    return rows, cpath, hpath


def variants_present(study_folder):
    """The settings that have a chain summary in the study folder (for --report-only)."""
    return [v for v in ORDER if summary_rows(os.path.join(study_folder, v))]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variants", default=DEFAULT,
                    help="settings to run, comma separated, or 'all' (default %s)" % DEFAULT)
    ap.add_argument("--csv", default=os.path.join(HERE, "dams_44.csv"), help="the list of dams")
    ap.add_argument("--dams", default="",
                    help="the dams layer (default: dams_44.gpkg next to this script or in the "
                         "batch-44-dams folder; it is not in the repository)")
    ap.add_argument("--study-folder", default="~/DamSafety/Settings_Study_44",
                    help="one folder per setting is written here")
    ap.add_argument("--dem", default="",
                    help="DEM or VRT covering every dam (default: the statewide VRT in the suite "
                         "data folder, dem/ND_1m_suite.vrt, when it exists; else the chain's own source)")
    ap.add_argument("--dem-units", default="meters", choices=("detect", "feet", "us-feet", "meters"),
                    help="the DEM's elevation units (default meters: the statewide VRT)")
    ap.add_argument("--only", default="", help="comma separated dam ids from the CSV")
    ap.add_argument("--parallel", type=int, default=1,
                    help="groups of dams side by side within a setting (default 1)")
    ap.add_argument("--no-screen", action="store_true", help="skip the Screening stage")
    ap.add_argument("--redo", action="store_true", help="run dams again that finished before")
    ap.add_argument("--report-only", action="store_true",
                    help="run nothing: rebuild the report from the runs in the study folder")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, run and write nothing")
    args = ap.parse_args(argv)
    args.study_folder = os.path.abspath(os.path.expanduser(args.study_folder))
    args.parallel = max(1, args.parallel)
    dams = read_ids(args.csv, args.only)
    if args.report_only:
        variants = variants_present(args.study_folder)
        if not variants:
            sys.exit("No finished run under %s yet (looked for %s in %s)." % (
                args.study_folder, SUMMARY_NAME, ", ".join(ORDER)))
    else:
        variants = pick_variants(args.variants)
        args.dams = os.path.abspath(os.path.expanduser(args.dams)) if args.dams else need_data(
            "dams_44.gpkg", "It holds the 44 dams as points with their DWR record.")
        data = os.path.expanduser(os.environ.get("NDSUITE_DATA", "~/ND_Suite_Data"))
        if not args.dem and os.path.exists(os.path.join(data, "dem", "ND_1m_suite.vrt")):
            args.dem = os.path.join(data, "dem", "ND_1m_suite.vrt")
        if args.dem:
            args.dem = os.path.abspath(os.path.expanduser(args.dem))
            if not os.path.exists(args.dem):
                sys.exit("--dem %s does not exist (is the drive mounted?)" % args.dem)
        py, tool = find_damchain()
        ids = [d["dam_id"] for d in dams]
        say("%d dam(s), %d setting(s): %s" % (len(ids), len(variants), ", ".join(variants)))
        if any(VARIANTS[v][1] for v in variants):
            say("ANUGA settings are in the list: hours per dam. --only tries a few dams first.")
        for v in variants:
            run_variant(py, tool, v, ids, args)
        if args.dry_run:
            return 0
        # an earlier run's settings stay in the report (the study is built up over days)
        variants = [v for v in ORDER if v in variants or v in variants_present(args.study_folder)]
    rows, cpath, hpath = write_report(args.study_folder, variants, dams)
    verdicts = [verdict(r["results"], variants) for r in rows]
    say("%d dam(s): %d change class with the settings, %d keep it, %d cannot be judged yet" % (
        len(rows), verdicts.count("changes"), verdicts.count("same"), verdicts.count("incomplete")))
    say("report: %s" % hpath)
    say("table:  %s" % cpath)
    # exit 1 while a dam is missing a result: a script that waits for the study sees it is not whole
    return 1 if "incomplete" in verdicts else 0


if __name__ == "__main__":
    sys.exit(main())
