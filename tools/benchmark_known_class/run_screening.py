"""Run the benchmark dams end to end through DamSafety Screening and capture the fixtures.

For each batch of dams this runs the headless chain (nddam:runchain: steps 1-8 and 10, with the
quick screen 6, as a county screen would) in its own work folder, with DAMSAFETY_RECEPTOR_DUMP
pointing at fixtures/, so tool 8 writes each dam's receptors there. Then it reads each work
folder's 8_hazard.gpkg into screening_results.csv (the end-to-end class per dam).

    python3 run_screening.py --pack <ND_Dam_Suite_Pack> --work <folder> [--tier cloud] [--batch 6]
                             [--only DWR-6122,DWR-2642] [--settings extra.json]

Needs qgis_process with DamSafety Core + Screening enabled (QGIS's Python; QT_QPA_PLATFORM is
set to offscreen) and network access to USGS 3DEP. A batch whose work folder already has
8_hazard.gpkg is not run again (delete the folder to redo it). Long batches: the built-in engine
took about 20 to 40 minutes per small dam in the cloud (4 cores); big reservoirs (tier mac) take
hours - run those on a workstation.

The screening settings are the window's defaults (USGS 3DEP 10 m terrain, built-in engine,
20 m grid, ACER TM 11 charts, 12 h clock up to 96 h) - what an engineer gets without changing
anything - so the end-to-end classes measure the suite as shipped.
"""
import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
DAMS_CSV = os.path.join(HERE, "dams.csv")
OUT_CSV = os.path.join(HERE, "screening_results.csv")
FIELDS = ("dam_id", "hz_class", "hz_vs_existing", "hz_reason", "n_bldg_wet", "n_bldg_lethal", "n_road_wet",
          "n_rail_wet", "hz_conf", "hz_border", "sim_status", "tri_result", "edge_flag", "work")

BASE_SETTINGS = {"terrain_source": 0, "engine": 2, "crit": 0, "chain": ["6"], "dem_cell": 10,
                 "grid_m": 20, "simh": 12, "simmax": 96}


def settings(pack, extra=None):
    s = dict(BASE_SETTINGS)
    s.update({"suite_folder": pack, "dwr": os.path.join(pack, "dwr", "DWR_dams.gpkg"),
              "dam_lines": os.path.join(pack, "dwr", "DWR_dam_lines.gpkg")})
    s.update(extra or {})
    return s


def read_hazard(gpkg):
    """Rows of tool 8's 8_hazard.gpkg (sqlite; no GDAL needed)."""
    con = sqlite3.connect("file:{}?mode=ro".format(gpkg), uri=True)
    try:
        tab = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()[0]
        cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(tab))]
        rows = [dict(zip(cols, r)) for r in con.execute('SELECT * FROM "{}"'.format(tab))]
    finally:
        con.close()
    return rows


# The qgis_process command. On a Mac the suite runs inside a conda env, so set e.g.
#   QGIS_PROCESS="conda run -n ndqgis qgis_process"
# (any command and its arguments, space-separated). Default is a bare qgis_process on PATH.
QGIS_PROCESS = (os.environ.get("QGIS_PROCESS") or "qgis_process").split()


def run_batch(ids, work, sett_path, log):
    os.makedirs(work, exist_ok=True)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", DAMSAFETY_RECEPTOR_DUMP=FIXTURES)
    cmd = QGIS_PROCESS + ["run", "nddam:runchain", "--", "WORK=" + work, "SETTINGS=" + sett_path,
           "DAM_ID=" + ",".join(ids), "INCLUDE=1", "LOG=" + os.path.join(work, "chain.txt")]
    log("run {} dam(s) in {}: {}".format(len(ids), work, ", ".join(ids)))
    with open(os.path.join(work, "qgis_process.txt"), "w", encoding="utf-8") as fh:
        p = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT)  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args -- fixed argv list, no shell; QGIS_PROCESS and the dam IDs/work paths are the benchmark operator's own CLI args
    log("  exit {}".format(p.returncode))
    return p.returncode


def triage_cleared(work):
    """Dams the quick screen (tool 6) cleared in this work folder: {dam_id: reason}. A cleared
    dam gets no flood and so no tool-8 class; the suite's end-to-end class for it is Low (nothing
    downstream is reached), exactly as tool 8 records a triage status - so the benchmark counts
    it Low. There is no receptor fixture for it (nothing flooded)."""
    g = os.path.join(work, "6_triage.gpkg")
    if not os.path.isfile(g):
        return {}
    out = {}
    con = sqlite3.connect("file:{}?mode=ro".format(g), uri=True)
    try:
        tab = con.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'").fetchone()[0]
        cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(tab))]
        for r in con.execute('SELECT * FROM "{}"'.format(tab)):
            d = dict(zip(cols, r))
            if str(d.get("tri_result") or "").lower() == "clear":
                out[str(d.get("dam_id"))] = str(d.get("tri_reason") or "cleared by the quick screen")
    except Exception:
        return out
    finally:
        con.close()
    return out


def collect(work_root):
    out = {}
    for name in sorted(os.listdir(work_root)):
        work = os.path.join(work_root, name)
        if not os.path.isdir(work):
            continue
        g = os.path.join(work, "8_hazard.gpkg")
        flooded = set()
        if os.path.isfile(g):
            for r in read_hazard(g):
                row = {k: r.get(k) for k in FIELDS if k != "work"}
                row["work"] = name
                out[str(r.get("dam_id"))] = row
                flooded.add(str(r.get("dam_id")))
        # dams the quick screen cleared (no tool-8 row): their end-to-end class is Low by triage
        for did, reason in triage_cleared(work).items():
            if did in flooded or did in out:
                continue
            out[did] = {k: "" for k in FIELDS}
            out[did].update({"dam_id": did, "hz_class": "Low", "hz_conf": "triage",
                             "hz_reason": "triage: " + reason, "sim_status": "triage",
                             "tri_result": "clear", "work": name})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pack", required=True, help="the offline pack folder (ND_Dam_Suite_Pack)")
    ap.add_argument("--work", required=True, help="a folder for the batch work folders")
    ap.add_argument("--tier", default="cloud", help="cloud, mac or all (dams.csv tier)")
    ap.add_argument("--only", default="", help="comma-separated dam IDs (DWR-####) instead of a tier")
    ap.add_argument("--batch", type=int, default=6)
    ap.add_argument("--settings", default="", help="a JSON file of settings that replace the defaults")
    ap.add_argument("--collect-only", action="store_true")
    a = ap.parse_args(argv)

    with open(DAMS_CSV, newline="", encoding="utf-8") as fh:
        dams = list(csv.DictReader(fh))
    if a.only:
        ids = [x.strip() for x in a.only.split(",") if x.strip()]
    else:
        ids = [d["dam_id"] for d in dams if a.tier == "all" or d["tier"] == a.tier]
    extra = {}
    if a.settings:
        with open(a.settings, encoding="utf-8") as fh:
            extra = json.load(fh)
    os.makedirs(a.work, exist_ok=True)
    sett_path = os.path.join(a.work, "benchmark_settings.json")
    with open(sett_path, "w", encoding="utf-8") as fh:
        json.dump(settings(a.pack, extra), fh, indent=1)

    def log(s):
        print(s, flush=True)

    if not a.collect_only:
        for i in range(0, len(ids), a.batch):
            chunk = ids[i:i + a.batch]
            work = os.path.join(a.work, "batch_{:02d}".format(i // a.batch + 1))
            if os.path.isfile(os.path.join(work, "8_hazard.gpkg")) and \
                    not os.path.isfile(os.path.join(work, "_logs", "step_8.running")):
                log("skip {} (8_hazard.gpkg there)".format(work))
                continue
            run_batch(chunk, work, sett_path, log)

    got = collect(a.work)
    old = {}
    if os.path.isfile(OUT_CSV):
        with open(OUT_CSV, newline="", encoding="utf-8") as fh:
            old = {r["dam_id"]: r for r in csv.DictReader(fh)}
    old.update(got)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        for k in sorted(old):
            w.writerow(old[k])
    log("{} end-to-end classes -> {}".format(len(old), OUT_CSV))
    missing = [i for i in ids if i not in old]
    if missing:
        log("no class yet for: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
