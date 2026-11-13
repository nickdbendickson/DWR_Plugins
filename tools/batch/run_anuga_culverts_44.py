#!/usr/bin/env python3
"""Run the 44 dams in dams_44.gpkg through the DamSafety Runner only, with ANUGA for the final
results and culverts in the terrain (DamSafety Runner 3.16.0, release 1.29, profile 'damsafety').

Why the DamSafety Runner: it finds the culverts itself. Every place a road crosses a channel
gets a culvert ("guess:"); a guess within 75 m of an inventory culvert takes that culvert's
real size (NDDOT bridge inventory, NDDOT Center Line Pipes, NBI). Matched and guessed culverts
are both cut into the DEM here. The ND Runner only takes culverts you draw yourself.

Per dam (one ndbreach:chainbatch call, dams one after another; Screening and Hazard skipped):
  LISFLOOD-FP (ACC) finds the flood extent, then ANUGA runs the final results on a mesh over it.

Settings passed to the Runner (ndbreach.batch.*):
  SOLVER=1 CELL=10 CREST_BURN=true GRID_REFINE=true GRID_CHECK_CELL=0 REFINE_NEAR=false
  CULVERT_BURN_GUESSES=true   cut unmatched road crossings in as 1.2 x 0.9 m boxes (UNVERIFIED)
  CULVERT_BURN_MATCHED=true   cut inventory-matched culverts in at their inventory size
  CULVERT_INVENTORY=...       the GeoPackages given with --culvert-inventory (or --get-clp)
  DEM units meters (the statewide 1 m VRT is NAVD88 metres)

ANUGA is much slower than LISFLOOD-FP: plan on hours per dam. It needs the 'ndanuga' conda env
(install_all.zsh sets it up). Use --parallel N to run N groups side by side (each group gets
its own part folder under --out, because each chainbatch writes its own summary).

Usage:
  python3 run_anuga_culverts_44.py --get-clp              # download NDDOT Center Line Pipes once
  python3 run_anuga_culverts_44.py                        # all 44, one after another
  python3 run_anuga_culverts_44.py --only DWR-2801 --watch
  python3 run_anuga_culverts_44.py --check-only           # preflight only
  python3 run_anuga_culverts_44.py --dry-run
"""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from batch_common import conda_exe, find_damchain, get_clp, need_data, say, start_watcher   # noqa: E402

RUNNER_PARAMS = ["SOLVER=1", "CELL=10", "CREST_BURN=true", "GRID_REFINE=true", "GRID_CHECK_CELL=0",
                 "REFINE_NEAR=false", "CULVERT_BURN_GUESSES=true", "CULVERT_BURN_MATCHED=true"]


def gpkg_ids(path):
    """dam_id values of the dams layer, read with the ndqgis env's GDAL (or this Python's)."""
    code = ("from osgeo import ogr; d=ogr.Open(%r); l=d.GetLayer(0); "
            "print(','.join(f.GetField('dam_id') for f in l))" % path)
    for cmd in ([sys.executable, "-c", code],
                [conda_exe(), "run", "-n", "ndqgis", "python", "-c", code]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().split(",")
        except Exception:                                                    # noqa
            pass
    sys.exit("Could not read %s (needs GDAL: run from the ndqgis env or give --only)" % path)


def run_group(n, ids, args, py, tool, inventory):
    out = args.out if args.parallel <= 1 else os.path.join(args.out, "part%d" % n)
    cmd = [py, tool, "--threads", "1"] + (["--quiet"] if args.parallel > 1 else []) + \
          (["--dry-run"] if args.dry_run else []) + [
        "run", "--set", "dwr", "--dams", args.dams, "--id-field", "dam_id", "--ids", ",".join(ids),
        "--out", out, "--no-screen", "--anuga", "--stop-after", "runner", "--hec", "skip",
        "--dem-units", "meters"]
    if args.dem:
        cmd += ["--dem", args.dem]
    params = list(RUNNER_PARAMS)
    if inventory:
        params.append("CULVERT_INVENTORY=" + ";;".join(inventory))
    for p in params:
        cmd += ["--runner-param", p]
    if args.redo:
        cmd.append("--redo")
    if args.check_only:
        cmd.append("--check-only")
    if args.triage:
        cmd.append("--triage")
    say("group %d: %d dam(s) -> %s" % (n, len(ids), out))
    say("  " + " ".join(cmd))
    if args.dry_run:
        return 0
    return subprocess.run(cmd, env=dict(os.environ, OMP_NUM_THREADS="1")).returncode


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dams", default="",
                    help="the dams layer (default: dams_44.gpkg next to this script or in the "
                         "batch-44-dams folder; it is not in the repository)")
    ap.add_argument("--out", default="~/DamSafety/Dams44_ANUGA", help="one folder per dam is written here")
    ap.add_argument("--dem", default="",
                    help="DEM or VRT covering every dam (default: the statewide VRT in the suite data "
                         "folder, dem/ND_1m_suite.vrt, when it exists; else the Runner's own DEM source)")
    ap.add_argument("--culvert-inventory", action="append", default=[],
                    help="inventory GeoPackage that sizes culverts (repeatable): NDDOT bridge inventory, "
                         "NDDOT Center Line Pipes, NTAD NBI")
    ap.add_argument("--get-clp", action="store_true",
                    help="download NDDOT Center Line Pipes into <suite data>/culverts/ and use it")
    ap.add_argument("--only", default="", help="comma separated dam ids")
    ap.add_argument("--parallel", type=int, default=1, help="groups side by side (default 1)")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="preflight only")
    ap.add_argument("--triage", action="store_true", help="on a failed step ask the local model why")
    ap.add_argument("--watch", action="store_true", help="start batch_watch.py on the output folder")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    data = os.path.expanduser(os.environ.get("NDSUITE_DATA", "~/ND_Suite_Data"))
    args.out = os.path.abspath(os.path.expanduser(args.out))
    args.dams = os.path.abspath(os.path.expanduser(args.dams)) if args.dams else need_data(
        "dams_44.gpkg", "The ANUGA batch reads the dams from a GeoPackage layer with a dam_id field.")
    if not args.dem:
        vrt = os.path.join(data, "dem", "ND_1m_suite.vrt")
        args.dem = vrt if os.path.exists(vrt) else ""
    else:
        args.dem = os.path.abspath(os.path.expanduser(args.dem))
    if args.dem:
        say("DEM: %s (keep the external SSD mounted if the VRT points to it)" % args.dem)
    inventory = [os.path.abspath(os.path.expanduser(p)) for p in args.culvert_inventory]
    clp = os.path.join(data, "culverts", "nddot_clp.gpkg")
    if args.get_clp:
        inventory.append(get_clp(os.path.join(data, "culverts")))
    elif os.path.exists(clp) and clp not in inventory:
        inventory.append(clp)
    say("culvert inventory: " + (", ".join(inventory) if inventory else
                                 "none (road crossings are cut in as guessed boxes; --get-clp adds sizes)"))
    py, tool = find_damchain()
    ids = [s.strip() for s in args.only.split(",") if s.strip()] or gpkg_ids(args.dams)
    watcher = None
    if not args.dry_run:                    # a dry run prints; it creates nothing
        os.makedirs(args.out, exist_ok=True)
    if args.watch and not args.dry_run:
        watcher = start_watcher(args.out)
    k = max(1, min(args.parallel, len(ids)))
    groups = [ids[i::k] for i in range(k)]
    with ThreadPoolExecutor(max_workers=k) as pool:
        rcs = list(pool.map(lambda a: run_group(a[0] + 1, a[1], args, py, tool, inventory),
                            enumerate(groups)))
    bad = sum(1 for r in rcs if r)
    say("finished: %d group(s), %d with a failed dam. Per-dam results: Chain_Batch_Summary.csv in %s%s"
        % (len(rcs), bad, args.out, " (one per part folder)" if k > 1 else ""))
    if watcher:
        say("the watcher keeps running for a final pass; stop it with Ctrl-C or: kill %d" % watcher.pid)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
