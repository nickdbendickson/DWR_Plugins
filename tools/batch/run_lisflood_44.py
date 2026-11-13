#!/usr/bin/env python3
"""Run the 44 dams in dams_44.csv through the ND chain headless, with the recommended
LISFLOOD-FP settings (ND Runner 3.29.0, release 1.29).

What it does, per dam (DWR-<structure id>):
  1. seeds <dams-folder>/<dam_id>/dam.json from dams_44.csv (an existing dam.json is kept)
  2. damchain run --set nd: Get data -> Screening -> Runner (LISFLOOD-FP) -> Hazard classification
  3. writes batch44_lisflood_summary.csv in the dams folder

Recommended LISFLOOD-FP settings (all are the Runner's defaults; they are passed explicitly so
a saved QGIS default or a DAMCHAIN_BREACH_PARAM in ~/.zshrc cannot change them):
  SOLVER=1          ACC (local inertial). FV2 / MWDG2 velocities are not validated yet.
  TIER=0            LISFLOOD-FP only
  CELL=10           10 m model cell (do not go coarser on flat plains)
  CREST_BURN=true   road / rail embankments kept from the 1 m DEM (statewide VRT, else 3DEP 1 m)
  GRID_REFINE=true  a coarse run that shows over-spread is re-run at 10 m automatically
  GRID_CHECK_CELL=0 no 5 m check run (hours per dam; opt in with --check-5m)
  REFINE_NEAR=false near-dam FV1 refine off
  THREADS=1         one LISFLOOD-FP thread per dam; dams run side by side instead (--parallel)
  The ACC stability re-run (theta 0.7 / CFL 0.5 when a run's volume error is over 1 %) is on.

Usage (in Terminal, after install_all.zsh; any Python 3 works):
  python3 run_lisflood_44.py                      # all 44, 2 at a time
  python3 run_lisflood_44.py --parallel 3 --watch # 3 at a time + the rule watcher (batch_watch.py)
  python3 run_lisflood_44.py --only DWR-2801,DWR-3034
  python3 run_lisflood_44.py --dry-run            # print the commands, run nothing
A dam that finished before is skipped (damchain keeps a state file); --redo runs it again.

When the data folder has no offline pack, one is built first for the area of the dams about
to run (get_offline_pack.py: no QGIS window; minutes for a few dams, hours for all 44, which
take in the whole State). --no-get-pack leaves that out. Without a pack Get data downloads
most layers from the servers, but Screening then stops at its check: the DWR dam lines come
only from the pack or from your own DWR files in the data folder's dwr/.
"""
import argparse
import csv
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from batch_common import chain_status, find_damchain, say, start_watcher     # noqa: E402
from get_offline_pack import ensure_pack                                     # noqa: E402

SEED_COLS = ["dam_id", "name", "lat", "lon", "height_ft", "storage_acft", "dam_length_ft",
             "dwr_structure_id", "nid_id", "existing_hazard_class"]
BREACH_PARAMS = ["SOLVER=1", "TIER=0", "CELL=10", "CREST_BURN=true", "GRID_REFINE=true",
                 "GRID_CHECK_CELL=0", "REFINE_NEAR=false", "THREADS=1"]


def read_dams(path, only):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if only:
        want = {s.strip() for s in only.split(",") if s.strip()}
        rows = [r for r in rows if r["dam_id"] in want]
        missing = want - {r["dam_id"] for r in rows}
        if missing:
            sys.exit("Not in %s: %s" % (os.path.basename(path), ", ".join(sorted(missing))))
    return rows


def seed(py, tool, rows, dams_folder, dry):
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SEED_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        tmp = fh.name
    cmd = [py, tool] + (["--dry-run"] if dry else []) + ["seed", "--dams-folder", dams_folder,
                                                          "--csv", tmp]
    say("seed: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    os.unlink(tmp)


def run_one(py, tool, row, args, logdir):
    did = row["dam_id"]
    params = list(BREACH_PARAMS)
    if args.check_5m:
        params = [p for p in params if not p.startswith("GRID_CHECK_CELL")] + ["GRID_CHECK_CELL=5"]
    if args.cell != 10:
        params = [p for p in params if not p.startswith("CELL=")] + ["CELL=%g" % args.cell]
    cmd = [py, tool, "--threads", "1", "--quiet"] + (["--dry-run"] if args.dry_run else []) + [
        "run", "--set", "nd", "--dams-folder", args.dams_folder, "--ids", did,
        "--data-folder", args.data_folder, "--timeout-min", str(args.timeout_min)]
    if args.dem:
        # The ND set has no --dem of its own (dam_chain's --dem belongs to the dwr set), so the
        # DEM goes in as a Runner parameter: suitechain passes BREACH_PARAMETERS straight
        # through to ndbreach:rundam, and data_aware fills rundam's DEM from the dam folder ONLY
        # when the value it is given is empty. Naming it here therefore bypasses Get data's
        # windowing entirely and models on the VRT itself.
        #
        # OVERWRITE=true is not optional. damdata.exists() reports a role already recorded in
        # dam.json as present and Get data skips it ("already there (overwrite to replace)"), so
        # any dam folder left over from an earlier 3DEP run would keep its old data/dem.vrt and
        # model the old terrain for hours while the log still says "get data: ok".
        #
        # Both go in as argv. dam_chain's parse_kv splits each on the FIRST "=" only, so a path
        # containing spaces survives. The DAMCHAIN_*_PARAM environment variables do NOT: _env_kv
        # splits those on whitespace. Never put this path in one.
        params = [q for q in params if not q.startswith("DEM=")] + ["DEM=%s" % args.dem]
        cmd += ["--getdata-param", "OVERWRITE=true"]
    for p in params:
        cmd += ["--breach-param", p]
    if args.redo:
        cmd.append("--redo")
    if args.triage:
        cmd.append("--triage")
    env = dict(os.environ, OMP_NUM_THREADS="1")
    out_path = os.path.join(logdir, "%s.console.log" % did)
    say("start %s  %s (%s)" % (did, row.get("name", ""), row.get("county", "")))
    if row.get("dwr_status", "Intact") != "Intact":
        say("  note: DWR lists %s as %s; the breach run still goes ahead" % (did, row["dwr_status"]))
    t0 = time.time()
    if args.dry_run:
        say("  " + " ".join(cmd))
        return did, "dry-run", 0.0, out_path
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n\n")
        fh.flush()
        rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env).returncode
    mins = (time.time() - t0) / 60.0
    status = chain_status(os.path.expanduser(args.dams_folder), did, rc)
    say("done  %s  %s  %.0f min  (%s)" % (did, status, mins, out_path))
    return did, status, mins, out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=os.path.join(HERE, "dams_44.csv"))
    ap.add_argument("--dams-folder", default="~/ND_Dams")
    ap.add_argument("--data-folder", default=os.environ.get("NDSUITE_DATA", "~/ND_Suite_Data"),
                    help="suite data folder with the offline pack (default $NDSUITE_DATA or ~/ND_Suite_Data)")
    ap.add_argument("--parallel", type=int, default=2,
                    help="dams at a time, 1 LISFLOOD-FP thread each (default 2; up to your core count)")
    ap.add_argument("--only", default="", help="comma separated dam ids from the CSV")
    ap.add_argument("--cell", type=float, default=10.0, help="model cell in m (default 10)")
    ap.add_argument("--check-5m", action="store_true", help="also run the 5 m grid check (slow)")
    ap.add_argument("--timeout-min", type=int, default=720, help="per step (default 720)")
    ap.add_argument("--redo", action="store_true", help="run dams again that finished before")
    ap.add_argument("--triage", action="store_true", help="on a failed step ask the local model why (TRIAGE.txt)")
    ap.add_argument("--watch", action="store_true", help="start batch_watch.py on the dams folder")
    ap.add_argument("--dem", default="",
                    help="model on this DEM/VRT instead of the terrain Get data downloads, e.g. a "
                         "statewide 1 m VRT on an external drive. Passed as the Runner's DEM and "
                         "with OVERWRITE=true so an earlier dam folder re-cuts its terrain. NOTE: "
                         "the model grid becomes the DEM's CRS. use_local_terrain.sh checks the "
                         "VRT first and explains the consequences.")
    ap.add_argument("--no-get-pack", action="store_true",
                    help="when there is no offline pack, do not build one first (Screening then "
                         "stops at its check unless your DWR files are in the data folder's dwr/)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.dams_folder = os.path.abspath(os.path.expanduser(args.dams_folder))
    args.data_folder = os.path.abspath(os.path.expanduser(args.data_folder))
    if args.dem:
        args.dem = os.path.abspath(os.path.expanduser(args.dem))
        if not os.path.exists(args.dem):
            sys.exit("--dem %s does not exist (is the drive mounted?)" % args.dem)
        say("DEM: %s (keep the drive mounted for the whole run)" % args.dem)
    py, tool = find_damchain()
    rows = read_dams(args.csv, args.only)
    ensure_pack(args.data_folder, rows, say=say, get=not args.no_get_pack, dry_run=args.dry_run)
    logdir = os.path.join(args.dams_folder, "_batch44_logs", time.strftime("%Y%m%d_%H%M%S"))
    if not args.dry_run:                    # a dry run prints; it creates nothing
        os.makedirs(logdir, exist_ok=True)
    seed(py, tool, rows, args.dams_folder, args.dry_run)
    watcher = None
    if args.watch and not args.dry_run:
        watcher = start_watcher(args.dams_folder)
    say("%d dam(s), %d at a time; LISFLOOD-FP: %s" % (len(rows), args.parallel, " ".join(BREACH_PARAMS)))
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futs = [pool.submit(run_one, py, tool, r, args, logdir) for r in rows]
        for f in as_completed(futs):
            results.append(f.result())
    names = {r["dam_id"]: r.get("name", "") for r in rows}
    summ = os.path.join(args.dams_folder, "batch44_lisflood_summary.csv")
    if not args.dry_run:
        with open(summ, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["dam_id", "name", "status", "minutes", "console_log"])
            for did, st, mins, log in sorted(results):
                w.writerow([did, names.get(did, ""), st, "%.0f" % mins, log])
    bad = [r for r in results if not (r[1].startswith("ok") or r[1] == "dry-run")]
    say("finished: %d ok, %d failed. Summary %s (classes: Chain_Summary_ND.csv)" % (
        len(results) - len(bad), len(bad), summ))
    if watcher:
        say("the watcher keeps running for a final pass; stop it with Ctrl-C or: kill %d" % watcher.pid)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
