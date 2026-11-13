#!/usr/bin/env python3
"""Run every DWR dam or dugout in one North Dakota county (or several) headless (release 1.29).

The structures come from nd_dams_dugouts.csv / .gpkg (DWR Dams and Dugouts layers downloaded
2026-11-06; IDs DWR-<structure id> for dams, DUG-<structure id> for dugouts).

Two modes:
  --mode lisflood (default)  the full ND chain per structure: Get data -> Screening -> Runner
        (LISFLOOD-FP) -> Hazard classification, with the recommended LISFLOOD-FP settings
        (SOLVER=1 ACC, TIER=0, CELL=10, CREST_BURN, GRID_REFINE, GRID_CHECK_CELL=0,
        REFINE_NEAR=false, THREADS=1; the ACC stability re-run is on). Screening fills a
        missing height / storage from the DEM (marked 'estimated (DEM)'), so structures
        without them run too (--need-hv to skip them).
  --mode anuga               DamSafety Runner only, LISFLOOD-FP for the extent then ANUGA for the
        final results, with culverts cut into the terrain (road crossings as guessed boxes,
        inventory-matched ones at their real size). Needs height AND storage: structures
        without them are skipped and listed.

Which structures (--kind):
  dams      DWR Dams with status Intact (change with --status "Intact,Under Construction")
  dugouts   DWR Dugouts
  both

Examples:
  python3 run_county.py --list                                  # counts per county
  python3 run_county.py --county Cass --kind dams --dry-run
  python3 run_county.py --county Cass --kind dams --parallel 3 --watch
  python3 run_county.py --county "Grand Forks,Walsh" --kind dugouts
  python3 run_county.py --county Cass --kind dams --mode anuga --get-clp --watch
  python3 run_county.py --county all --kind both                # the whole state (days)
Output folders: <root>/<County>/<kind>/<ID>/ for lisflood, <root>/<County>/<kind>_ANUGA/ for anuga
(root ~/ND_County_Runs). Finished structures are skipped next time; --redo runs them again.
"""
import argparse
import csv
import os
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from batch_common import chain_status, find_damchain, get_clp, need_data, say, start_watcher   # noqa: E402
from get_offline_pack import ensure_pack                                     # noqa: E402

SEED_COLS = ["dam_id", "name", "lat", "lon", "height_ft", "storage_acft", "dam_length_ft",
             "dwr_structure_id", "nid_id", "existing_hazard_class"]
LISFLOOD = ["SOLVER=1", "TIER=0", "CELL=10", "CREST_BURN=true", "GRID_REFINE=true",
            "GRID_CHECK_CELL=0", "REFINE_NEAR=false", "THREADS=1"]
ANUGA_EXTRA = ["CULVERT_BURN_GUESSES=true", "CULVERT_BURN_MATCHED=true"]


def load_rows(path=""):
    path = path or need_data("nd_dams_dugouts.csv", "It lists every DWR dam and dugout by county "
                                                  "(the county-batch package has it).")
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def select(rows, counties, kind, statuses, need_hv):
    kinds = {"dams": {"dam"}, "dugouts": {"dugout"}, "both": {"dam", "dugout"}}[kind]
    out = []
    for r in rows:
        if r["kind"] not in kinds or (counties and r["county"].lower() not in counties):
            continue
        if r["kind"] == "dam" and statuses and r["status"].lower() not in statuses:
            continue
        if need_hv and r["has_height_storage"] != "yes":
            continue
        out.append(r)
    return out


def county_slug(name):
    return name.replace(" ", "_")


def list_counts(rows):
    import collections
    c = collections.Counter()
    for r in rows:
        key = "dam" if r["kind"] == "dam" and r["status"] == "Intact" else (
            "dugout" if r["kind"] == "dugout" else None)
        if key:
            c[(r["county"], key)] += 1
            if r["has_height_storage"] == "yes":
                c[(r["county"], key + "_hv")] += 1
    print("%-15s %12s %12s   (with height+storage)" % ("county", "dams(Intact)", "dugouts"))
    for county in sorted({k[0] for k in c}):
        print("%-15s %6d (%4d) %6d (%4d)" % (county, c[(county, "dam")], c[(county, "dam_hv")],
                                             c[(county, "dugout")], c[(county, "dugout_hv")]))
    print("%-15s %6d (%4d) %6d (%4d)" % ("TOTAL", sum(v for k, v in c.items() if k[1] == "dam"),
                                         sum(v for k, v in c.items() if k[1] == "dam_hv"),
                                         sum(v for k, v in c.items() if k[1] == "dugout"),
                                         sum(v for k, v in c.items() if k[1] == "dugout_hv")))


# ------------------------------------------------------------------ lisflood (ND chain)
def seed(py, tool, rows, folder, dry):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SEED_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        tmp = fh.name
    cmd = [py, tool] + (["--dry-run"] if dry else []) + ["seed", "--dams-folder", folder, "--csv", tmp]
    out = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(tmp)
    if out.returncode:
        sys.exit("seed failed:\n" + out.stdout + out.stderr)
    say("seed %s: %s" % (folder, (out.stdout.strip().splitlines() or [""])[-1]))


def run_nd_one(py, tool, row, folder, args, logdir):
    did = row["dam_id"]
    params = [p for p in LISFLOOD if not p.startswith("CELL=")] + ["CELL=%g" % args.cell]
    cmd = [py, tool, "--threads", "1", "--quiet"] + (["--dry-run"] if args.dry_run else []) + [
        "run", "--set", "nd", "--dams-folder", folder, "--ids", did,
        "--data-folder", args.data_folder, "--timeout-min", str(args.timeout_min)]
    for p in params:
        cmd += ["--breach-param", p]
    cmd += ["--redo"] if args.redo else []
    cmd += ["--triage"] if args.triage else []
    if args.dry_run:
        say("  " + " ".join(cmd))
        return did, "dry-run", 0.0, ""
    log = os.path.join(logdir, "%s.console.log" % did)
    say("start %s  %s (%s)%s" % (did, row["name"], row["county"],
                                 "" if row["has_height_storage"] == "yes" else
                                 "  [height/storage from the DEM]"))
    t0 = time.time()
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                            env=dict(os.environ, OMP_NUM_THREADS="1")).returncode
    mins = (time.time() - t0) / 60.0
    st = chain_status(folder, did, rc)
    say("done  %s  %s  %.0f min" % (did, st, mins))
    return did, st, mins, log


def run_lisflood(py, tool, sel, folder, args):
    logdir = os.path.join(folder, "_county_logs", time.strftime("%Y%m%d_%H%M%S"))
    if not args.dry_run:                    # a dry run prints; it creates nothing
        os.makedirs(logdir, exist_ok=True)
    seed(py, tool, sel, folder, args.dry_run)
    res = []
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs = [pool.submit(run_nd_one, py, tool, r, folder, args, logdir) for r in sel]
        for f in as_completed(futs):
            res.append(f.result())
    names = {r["dam_id"]: r["name"] for r in sel}
    summ = os.path.join(folder, "county_lisflood_summary.csv")
    old = {}
    if os.path.exists(summ):
        with open(summ, newline="", encoding="utf-8") as fh:
            old = {r["dam_id"]: r for r in csv.DictReader(fh)}
    for did, st, mins, log in res:
        old[did] = {"dam_id": did, "name": names.get(did, ""), "status": st,
                    "minutes": "%.0f" % mins, "console_log": log}
    if not args.dry_run:
        with open(summ, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["dam_id", "name", "status", "minutes", "console_log"])
            w.writeheader()
            w.writerows(old[k] for k in sorted(old))
    return sum(1 for r in res if not (r[1].startswith("ok") or r[1] == "dry-run"))


# ------------------------------------------------------------------ anuga (DamSafety Runner)
def run_anuga(py, tool, sel, layer, out, args, inventory):
    if not args.dry_run:
        os.makedirs(out, exist_ok=True)
    ids_csv = os.path.join(out, "_ids_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    if not args.dry_run:
        with open(ids_csv, "w", newline="", encoding="utf-8") as fh:
            fh.write("dam_id\n" + "\n".join(r["dam_id"] for r in sel) + "\n")
    cmd = [py, tool, "--threads", "1"] + (["--dry-run"] if args.dry_run else []) + [
        "run", "--set", "dwr", "--dams", "%s|layername=%s" % (args.gpkg, layer),
        "--id-field", "dam_id", "--ids-csv", ids_csv, "--out", out, "--no-screen", "--anuga",
        "--stop-after", "runner", "--hec", "skip", "--dem-units", "meters"]
    if args.dem:
        cmd += ["--dem", args.dem]
    params = [p for p in LISFLOOD if not p.startswith(("CELL=", "TIER=", "THREADS="))] + \
        ["CELL=%g" % args.cell] + ANUGA_EXTRA
    if inventory:
        params.append("CULVERT_INVENTORY=" + ";;".join(inventory))
    for p in params:
        cmd += ["--runner-param", p]
    cmd += ["--redo"] if args.redo else []
    cmd += ["--triage"] if args.triage else []
    say("  " + " ".join(cmd))
    if args.dry_run:
        return 0
    return 1 if subprocess.run(cmd, env=dict(os.environ, OMP_NUM_THREADS="1")).returncode else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", default="", help="county name, comma separated names, or 'all'")
    ap.add_argument("--kind", choices=("dams", "dugouts", "both"), default="dams")
    ap.add_argument("--mode", choices=("lisflood", "anuga"), default="lisflood")
    ap.add_argument("--status", default="Intact", help="dam statuses to run (comma separated; '' = all)")
    ap.add_argument("--need-hv", action="store_true", help="only structures with height AND storage")
    ap.add_argument("--only", default="", help="comma separated IDs within the selection")
    ap.add_argument("--limit", type=int, default=0, help="first N of the selection (a trial run)")
    ap.add_argument("--root", default="~/ND_County_Runs")
    ap.add_argument("--data-folder", default=os.environ.get("NDSUITE_DATA", "~/ND_Suite_Data"))
    ap.add_argument("--dem", default="", help="anuga mode: DEM/VRT (default the suite's dem/ND_1m_suite.vrt)")
    ap.add_argument("--culvert-inventory", action="append", default=[])
    ap.add_argument("--get-clp", action="store_true", help="anuga mode: download NDDOT Center Line Pipes")
    ap.add_argument("--parallel", type=int, default=2, help="lisflood mode: structures at a time (default 2)")
    ap.add_argument("--cell", type=float, default=10.0)
    ap.add_argument("--timeout-min", type=int, default=720)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--triage", action="store_true")
    ap.add_argument("--watch", action="store_true", help="start batch_watch.py on each county folder")
    ap.add_argument("--data", default="",
                    help="nd_dams_dugouts.csv (default: next to this script or in the county-batch "
                         "folder; it is not in the repository). anuga mode reads the .gpkg beside it")
    ap.add_argument("--no-get-pack", action="store_true",
                    help="lisflood mode: when there is no offline pack, do not build one first for "
                         "the counties' area (Screening then stops at its check unless your DWR "
                         "files are in the data folder's dwr/)")
    ap.add_argument("--list", action="store_true", help="print counts per county and stop")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rows = load_rows(os.path.abspath(os.path.expanduser(args.data)) if args.data else "")
    if args.list:
        list_counts(rows)
        return
    if not args.county:
        sys.exit("Give --county NAME (or 'all'); --list shows the counties.")
    known = sorted({r["county"] for r in rows if r["county"]})
    if args.county.strip().lower() == "all":
        counties = known
    else:
        want = [c.strip() for c in args.county.split(",") if c.strip()]
        bad = [c for c in want if c.lower() not in {k.lower() for k in known}]
        if bad:
            sys.exit("Unknown county: %s. Counties: %s" % (", ".join(bad), ", ".join(known)))
        counties = [k for k in known if k.lower() in {c.lower() for c in want}]
    statuses = {s.strip().lower() for s in args.status.split(",") if s.strip()}
    need_hv = args.need_hv or args.mode == "anuga"
    args.root = os.path.abspath(os.path.expanduser(args.root))
    args.data_folder = os.path.abspath(os.path.expanduser(args.data_folder))
    py, tool = find_damchain()
    inventory = []
    if args.mode == "anuga":
        beside = (os.path.splitext(os.path.abspath(os.path.expanduser(args.data)))[0] + ".gpkg") if args.data else ""
        args.gpkg = beside if os.path.isfile(beside) else need_data(
            "nd_dams_dugouts.gpkg", "anuga mode reads the structures from this GeoPackage (layers "
                                    "dams and dugouts).")
        data = args.data_folder
        if not args.dem:
            vrt = os.path.join(data, "dem", "ND_1m_suite.vrt")
            args.dem = vrt if os.path.exists(vrt) else ""
        inventory = [os.path.abspath(os.path.expanduser(p)) for p in args.culvert_inventory]
        clp = os.path.join(data, "culverts", "nddot_clp.gpkg")
        if args.get_clp and not args.dry_run:
            inventory.append(get_clp(os.path.join(data, "culverts")))
        elif os.path.exists(clp):
            inventory.append(clp)
    else:
        chosen = {c.lower() for c in counties}
        ids = {s.strip() for s in args.only.split(",") if s.strip()}
        area = [r for r in rows if r["county"].lower() in chosen and (not ids or r["dam_id"] in ids)]
        ensure_pack(args.data_folder, area, say=say, get=not args.no_get_pack, dry_run=args.dry_run)
    failed = 0
    for county in counties:
        kinds = ("dams", "dugouts") if args.kind == "both" else (args.kind,)
        for kind in kinds:
            sel = select(rows, {county.lower()}, kind, statuses, need_hv)
            if args.only:
                ids = {s.strip() for s in args.only.split(",")}
                sel = [r for r in sel if r["dam_id"] in ids]
            if args.limit:
                sel = sel[:args.limit]
            all_n = len(select(rows, {county.lower()}, kind, statuses, False))
            skipped = all_n - len(select(rows, {county.lower()}, kind, statuses, need_hv))
            if not sel:
                say("%s %s: nothing to run%s" % (county, kind, " (%d without height/storage skipped)" % skipped if skipped else ""))
                continue
            sub = kind if args.mode == "lisflood" else kind + "_ANUGA"
            folder = os.path.join(args.root, county_slug(county), sub)
            say("== %s %s: %d to run (%s mode)%s -> %s" % (
                county, kind, len(sel), args.mode,
                "; %d without height/storage skipped" % skipped if skipped else "", folder))
            if skipped and not args.dry_run:
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, "skipped_no_height_storage.csv"), "w",
                          newline="", encoding="utf-8") as fh:
                    w = csv.writer(fh)
                    w.writerow(["dam_id", "name", "height_ft", "storage_acft"])
                    for r in select(rows, {county.lower()}, kind, statuses, False):
                        if r["has_height_storage"] != "yes":
                            w.writerow([r["dam_id"], r["name"], r["height_ft"], r["storage_acft"]])
            watcher = None
            if args.watch and not args.dry_run:
                os.makedirs(folder, exist_ok=True)
                watcher = start_watcher(folder)
            try:
                if args.mode == "lisflood":
                    failed += run_lisflood(py, tool, sel, folder, args)
                else:
                    failed += run_anuga(py, tool, sel, "dams" if kind == "dams" else "dugouts",
                                        folder, args, inventory)
            finally:
                if watcher:
                    time.sleep(5)
                    watcher.send_signal(signal.SIGINT)      # one last pass, then it stops
                    try:
                        watcher.wait(timeout=900)
                    except subprocess.TimeoutExpired:
                        watcher.kill()
    say("finished: %s" % ("all ok" if not failed else "%d failed (see the summaries)" % failed))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
