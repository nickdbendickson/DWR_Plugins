#!/usr/bin/env python3
"""run_results.py 1.0.0 - dam-breach run results in a database Heim can answer from.

    heim runs add FOLDER ...           read every run below FOLDER into ~/local-ai/runs.db
    heim runs list                     the runs that are in it
    heim runs show DAM                 one dam's numbers: peak flow, breach, flood, hazard
    heim runs at DAM PLACE             depth / velocity / arrival at a structure, road or address
    heim runs at DAM 46.98,-97.39      ... or at a point (reads the rasters; needs GDAL)
    heim runs hydrograph DAM           the breach hydrograph, its peak and when it peaks
    heim runs ask "question"           Heim writes the query, the code runs it, Heim phrases it
    heim runs sql "SELECT ..."         your own query (read-only)
    heim runs schema                   the tables and what each column means
    heim runs forget DAM | --all       take runs out again

Why a database and not the document index: the document index finds passages that read like the
question and hands them to the model, which then copies a number out of text. That is fine for
"why was the run cut off" and wrong for "what is the depth at 4512 Main St": a passage search can
return the neighbour's row, and a model can round or swap a number. Here every number is a column
in a table, a question becomes one SELECT, and the rows printed are the answer. The model only
writes the query and the sentence; the code checks that every number in the sentence is in the
rows, and says so when one is not.

What is read (nothing is written next to the run; the files are opened read-only):
  run_report.json            DamSafety Runner (_breach_model/) and DamIT Runner (run folder):
                             peak breach flow, time to peak, breach width / formation time, starting
                             WSE, storage, flooded acres, max depth / velocity, latest arrival, every
                             other number of the report as a fact, and its warnings
  run_report.md              when there is no run_report.json (a copied results folder)
  breach_hydrograph.csv      and no_breach_hydrograph.csv, gauge_*.csv
  receptors_flooded.csv      the Runner's receptor points: depth, velocity, arrival at each
  Hazard outputs             Classification/Habitable_Structures, Roads_Inundated, Rail_Inundated,
                             Access_Cut_Off, Dams_In_Series, Lifeline_Facilities,
                             Bridges_Culverts_Crossings (.gpkg, else .csv), the classification
                             summary; DamIT Hazard classify/damit_hazard_receptors.gpkg and its
                             report
  Consequences (*).csv       the Runner's consequences tool
  the four result rasters    Depth (Max), Velocity (Max), Arrival Time, WSE (Max): their paths, for
                             `at` with a point

Stdlib only. `at` with a point uses GDAL (osgeo) when this Python has it, else the QGIS conda
environment's Python (ndqgis) when one is found. `ask` needs Ollama; nothing else does.
"""
import argparse
import csv
import datetime
import glob
import json
import math
import os
import re
import sqlite3
import struct
import subprocess  # nosec B404 - only to re-run this file under the QGIS Python for GDAL
import sys
import urllib.error
import urllib.request

VERSION = "1.0.0"
SCHEMA_VERSION = 1
DB = os.path.expanduser(os.environ.get("HEIM_RUNS_DB", "~/local-ai/runs.db"))
BASE = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
CHAT_MODEL = os.environ.get("HEIM_MODEL", "heim")
NUM_CTX = int(os.environ.get("HEIM_NUM_CTX", "16384"))
MAX_ROWS = 200                      # rows a query may return; the rest are counted, not shown
QUERY_SECONDS = 10.0                # a query that runs longer is stopped
WALK_DEPTH = 6                      # how deep `add` looks below a folder for run reports

FINAL_RASTERS = (("depth", "Depth (Max).tif", "ft"), ("velocity", "Velocity (Max).tif", "ft/s"),
                 ("arrival", "Arrival Time.tif", "h after breach"), ("wse", "WSE (Max).tif", "ft"))
# Hazard's result tables (DamSafety Hazard submittal._TABLE_BASES) and what each one holds
HAZARD_TABLES = (("Habitable_Structures", "structure"), ("Roads_Inundated", "road"),
                 ("Rail_Inundated", "rail"), ("Access_Cut_Off", "access cut off"),
                 ("Dams_In_Series", "dam"), ("Lifeline_Facilities", "lifeline"),
                 ("Bridges_Culverts_Crossings", "crossing"),
                 ("Future_Development_Parcels", "parcel"))
HAZARD_SUMMARY = ("Hazard_Classification_Summary.json", "Hazard_Classification_Summary.txt")
CONSEQUENCE_FILES = (("Consequences (Structures).csv", "structure"), ("Consequences (Roads).csv", "road"))
SKIP_DIRS = {"lisflood", "anuga", "hecras", "results", "model", "_downloads", "_reprojected", "_smoke",
             "pylibs", "node_modules", ".git", "__pycache__"}

# Columns of the feature tables, by meaning (first match wins, case-insensitive)
COL_ID = ("struct_id", "rid", "facility_id", "source_fid", "id", "fid", "nsi_fd_id")
COL_NAME = ("name", "facility_name", "facility", "road_name", "railroad", "dam_name", "struct_name",
            "label_name")
COL_ADDRESS = ("address", "full_address", "site_address")
COL_CITY = ("city", "place")
COL_DEPTH = ("max_depth_ft", "depth_ft", "depth")
COL_VEL = ("max_vel_fps", "vel_fps", "velocity_fps", "velocity")
COL_ARR = ("first_arrival_hr", "arrival_hr", "arrival")
COL_KIND = ("kind",)

FT = 0.3048


# ----------------------------------------------------------------------------- the database
DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY,
  run_key TEXT UNIQUE,
  dam_id TEXT, dam_name TEXT, scenario TEXT, breach_method TEXT, final_model TEXT, tool TEXT,
  run_started TEXT, run_finished TEXT,
  peak_breach_cfs REAL, time_to_peak_hr REAL, breach_volume_acft REAL, storage_at_failure_acft REAL,
  starting_wse_ft REAL, breach_bottom_ft REAL, top_of_dam_ft REAL, dam_height_ft REAL,
  avg_breach_width_ft REAL, formation_time_hr REAL,
  flooded_acres REAL, max_depth_ft REAL, max_velocity_fps REAL, velocity_p99_fps REAL,
  latest_arrival_hr REAL, cell_m REAL, manning_n REAL, cut_off TEXT,
  hazard_class TEXT, tm11_class TEXT, flooded_structures INTEGER, hazard_folder TEXT,
  hazard_older_than_run INTEGER,
  folder TEXT, dam_folder TEXT, source TEXT, ingested TEXT);
CREATE TABLE IF NOT EXISTS facts (run_id INTEGER, key TEXT, value_num REAL, value_text TEXT);
CREATE TABLE IF NOT EXISTS hydrograph (run_id INTEGER, series TEXT, time_hr REAL, flow_cfs REAL);
CREATE TABLE IF NOT EXISTS features (
  run_id INTEGER, layer TEXT, kind TEXT, feature_id TEXT, name TEXT, address TEXT,
  depth_ft REAL, velocity_fps REAL, arrival_hr REAL, x REAL, y REAL, crs TEXT,
  source_file TEXT, attrs TEXT);
CREATE TABLE IF NOT EXISTS warnings (run_id INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS rasters (run_id INTEGER, role TEXT, path TEXT, units TEXT);
CREATE INDEX IF NOT EXISTS facts_run ON facts(run_id);
CREATE INDEX IF NOT EXISTS hydro_run ON hydrograph(run_id);
CREATE INDEX IF NOT EXISTS feat_run ON features(run_id);
CREATE INDEX IF NOT EXISTS feat_name ON features(name);
"""

# What each column means: shown by `schema`, and given to the model with every `ask`
SCHEMA_DOC = """\
Tables (SQLite). Units are in the column names: _ft feet (elevations NAVD88 unless the run says
otherwise), _fps feet per second, _cfs cubic feet per second, _acft acre-feet, _hr hours after
breach initiation, _m metres.

runs: one row per model run.
  run_id, dam_id (e.g. DWR-2801, or an NID id such as ND00199), dam_name, scenario (sunny / pmf /
  ...), breach_method, final_model (LISFLOOD-FP, ANUGA, HEC-RAS), tool (Runner name + version),
  run_started, run_finished,
  peak_breach_cfs (peak outflow of the breach hydrograph), time_to_peak_hr, breach_volume_acft,
  storage_at_failure_acft, starting_wse_ft (pool at failure), breach_bottom_ft, top_of_dam_ft,
  dam_height_ft, avg_breach_width_ft, formation_time_hr,
  flooded_acres, max_depth_ft (deepest cell), max_velocity_fps (the fastest single cell, often a
  spike at the breach; velocity_p99_fps is the representative figure), latest_arrival_hr,
  cell_m (model cell size), manning_n, cut_off (why the run stopped early, empty if it did not),
  hazard_class (ND hazard class from Hazard), tm11_class, flooded_structures (habitable structures
  Hazard counted as flooded), hazard_older_than_run (1 = the Hazard outputs are older than this run:
  re-classify before trusting them), folder, dam_folder.
facts: every other number or text of a run's report. run_id, key (dotted path such as
  hydrograph.storage_exponent_m, settings.manning_n, summary.flooded_acres_ge_1ft,
  lisflood_runs.0.size_mi), value_num, value_text.
hydrograph: run_id, series ('breach', 'no breach', 'gauge <name>'), time_hr, flow_cfs.
features: one row per structure, road, rail line, facility or receptor the flood was checked
  against. run_id, layer (source table, e.g. Habitable_Structures, Roads_Inundated,
  runner_receptors), kind (structure / road / rail / dam / lifeline / crossing / access cut off /
  receptor), feature_id (Struct_ID and the like), name (road name, facility name, railroad),
  address (street address and city where Hazard had one), depth_ft (maximum depth at it),
  velocity_fps, arrival_hr (first arrival), x, y (in crs), crs, source_file, attrs (JSON of every
  column of the source row; read one with json_extract(attrs, '$.ACER_Zone_Label')).
warnings: run_id, text (the run report's "check these" lines).
rasters: run_id, role (depth / velocity / arrival / wse), path, units.
"""


def connect(path=None, write=False):
    path = path or DB
    if write:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        db = sqlite3.connect(path)
        db.executescript(DDL)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        return db
    if not os.path.exists(path):
        return None
    return sqlite3.connect("file:%s?mode=ro" % _uri_path(path), uri=True)


def _uri_path(path):
    from urllib.parse import quote
    return quote(os.path.abspath(path))


# ----------------------------------------------------------------------------- small helpers
def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x) if math.isfinite(float(x)) else None
    m = re.match(r"^\s*([-+]?[\d,]*\.?\d+(?:[eE][-+]?\d+)?)", str(x))
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _get(d, *paths):
    """The first of the dotted paths that is present and not empty."""
    for p in paths:
        cur = d
        for part in p.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur not in (None, "", [], {}):
            return cur
    return None


def flatten(obj, prefix=""):
    """(dotted key, value) for every scalar of a JSON document; lists are indexed (.0, .1)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, "%s.%s" % (prefix, k) if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:200]):
            yield from flatten(v, "%s.%d" % (prefix, i))
    else:
        yield prefix, obj


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _say(msg=""):
    print(msg)


# ----------------------------------------------------------------------------- finding runs
def find_reports(path, depth=WALK_DEPTH):
    """Every run below `path`: run_report.json files, and run_report.md in folders that have
    no run_report.json (a copied results folder). A file given directly counts as one."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        return [path] if os.path.basename(path) in ("run_report.json", "run_report.md") else []
    found = []
    base = path.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(path):
        if root.count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = sorted(d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith("pass_"))
        if "run_report.json" in files:
            found.append(os.path.join(root, "run_report.json"))
        elif "run_report.md" in files:
            found.append(os.path.join(root, "run_report.md"))
    # a run_report.md beside (or below) a run_report.json of the same run is not another run
    jdirs = {os.path.dirname(p) for p in found if p.endswith(".json")}
    return [p for p in found if p.endswith(".json") or os.path.dirname(p) not in jdirs]


def has_runs(path):
    return bool(find_reports(path, depth=4))


def run_folders(report_path):
    """(run folder, model folder, dam folder) of a report. DamSafety: <dam>/_breach_model/
    run_report.json, results in <dam>/. DamIT: <dam>/runs/<id>/run_<time>/run_report.json,
    results in outputs/ below it."""
    model = os.path.dirname(os.path.abspath(report_path))
    run = os.path.dirname(model) if os.path.basename(model) == "_breach_model" else model
    dam = run
    parts = run.split(os.sep)
    if len(parts) >= 3 and parts[-3] == "runs":          # DamIT <dam>/runs/<id>/run_x
        dam = os.sep.join(parts[:-3])
    for cand in (dam, os.path.dirname(dam)):
        if os.path.isfile(os.path.join(cand, "dam.json")) and not os.path.isfile(os.path.join(dam, "dam.json")):
            dam = cand
    return run, model, dam


def _first_file(names, folders):
    for f in folders:
        for n in names:
            p = os.path.join(f, n)
            if os.path.isfile(p):
                return p
    return None


# ----------------------------------------------------------------------------- run_report.md
_MD_RESULT = {
    "scenario": "scenario", "final rasters from": "final_model", "peak breach outflow": "peak_breach_cfs",
    "storage at failure": "storage_at_failure_acft", "flooded area": "flooded_acres",
    "latest arrival": "latest_arrival_hr",
}


def report_from_md(path):
    """A run report dict from run_report.md (the Runner renders it from the JSON, so the same
    names): the Result table and the bullet lists. Used only when there is no JSON."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    rep = {"summary": {}, "hydrograph": {}, "warnings": [], "_from_md": True}
    m = re.search(r"^#\s+Breach model run:\s*(\S+)", text, re.M)
    if m:
        rep["summary"]["dam_id"] = m.group(1)
    m = re.search(r"^(ND Dam Breach Runner|DamSafety Runner|DamIT Runner)[^·\n]*", text, re.M)
    if m:
        rep["tool"] = m.group(0).strip()
    m = re.search(r"finished (\d{4}-\d\d-\d\dT[\d:]+)", text)
    if m:
        rep["run_finished"] = m.group(1)
    section = None
    for line in text.splitlines():
        h = re.match(r"^##\s+(.*)", line)
        if h:
            section = h.group(1).strip().lower()
            continue
        row = re.match(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", line)
        if row and section == "result":
            k, v = row.group(1).strip().lower(), row.group(2).strip()
            if k in _MD_RESULT:
                key = _MD_RESULT[k]
                rep["summary"][key] = v if key in ("scenario", "final_model") else _num(v)
                if key == "peak_breach_cfs":
                    mm = re.search(r"\(([^)]+)\)", v)
                    if mm:
                        rep["summary"]["breach_method"] = mm.group(1)
            elif k.startswith("maximum depth"):
                nums = re.findall(r"[-+]?[\d,]*\.?\d+", v)
                if nums:
                    rep["summary"]["max_depth_ft"] = _num(nums[0])
                if len(nums) > 1:
                    rep["summary"]["max_velocity_fps"] = _num(nums[1])
            continue
        b = re.match(r"^-\s+([a-z0-9 ]+?):\s+(.+)$", line)
        if b and section and section.startswith("breach"):
            key = b.group(1).strip().replace(" ", "_")
            val = b.group(2).strip()
            n = _num(val)
            rep["hydrograph"][key] = n if n is not None and re.match(r"^[-+\d.,]+$", val) else val
        elif line.startswith("- ") and section == "check these":
            rep["warnings"].append(line[2:].strip())
        if section and section.startswith("results cut off") and line.strip().startswith("The run ended") \
                and "Reason:" in line:
            rep["cut_off"] = {"reason": line.split("Reason:", 1)[1].strip().rstrip(".")}
    return rep


# ----------------------------------------------------------------------------- one run
def _scenario_of_rule(rule):
    """DamIT writes the scenario only in its hydrograph rule ("sunny day, no outlet ...")."""
    if not rule:
        return None
    head = str(rule).split(",")[0].strip().lower()
    return head if head in ("sunny day", "pmf", "flood", "rainy day") or head.startswith("pmf") else None


def run_row(rep, extra=None):
    """The `runs` columns from a run report (DamSafety or DamIT Runner, or one parsed from .md)."""
    extra = extra or {}
    s = {
        "dam_id": _get(rep, "summary.dam_id", "dam.dam_id", "dam_id") or extra.get("dam_id"),
        "dam_name": _get(rep, "dam.name") or extra.get("dam_name"),
        "scenario": _get(rep, "summary.scenario", "scenario", "settings.scenario") or _scenario_of_rule(
            _get(rep, "hydrograph.rule")),
        "breach_method": _get(rep, "hydrograph.breach_parameters", "hydrograph.label",
                              "summary.breach_method", "hydrograph.method"),
        "final_model": _get(rep, "summary.final_model", "flood_engine_short") or re.sub(
            r"\s*\(.*\)\s*$", "", str(_get(rep, "engine", "tier") or "")) or None,
        "tool": _get(rep, "tool") or extra.get("tool"),
        "run_started": _get(rep, "run_started"),
        "run_finished": _get(rep, "run_finished") or extra.get("run_finished"),
        "peak_breach_cfs": _num(_get(rep, "summary.peak_breach_cfs", "hydrograph.peak_cfs")),
        "time_to_peak_hr": _num(_get(rep, "hydrograph.time_to_peak_hr")),
        "breach_volume_acft": _num(_get(rep, "hydrograph.volume_acft", "hydrograph.released_acft")),
        "storage_at_failure_acft": _num(_get(rep, "summary.storage_at_failure_acft",
                                             "hydrograph.storage_at_failure_acft", "dam.storage_top_acft"))
        or extra.get("storage_acft"),
        "starting_wse_ft": _num(_get(rep, "hydrograph.starting_wse_ft")),
        "breach_bottom_ft": _num(_get(rep, "hydrograph.breach_bottom_ft")),
        "top_of_dam_ft": _num(_get(rep, "dam.top_of_dam_ft")) or extra.get("top_of_dam_ft"),
        "dam_height_ft": _num(_get(rep, "dam.dam_height_ft", "hydrograph.hw_ft")) or extra.get("dam_height_ft"),
        "avg_breach_width_ft": _num(_get(rep, "hydrograph.avg_breach_width_ft")),
        "formation_time_hr": _num(_get(rep, "hydrograph.formation_time_hr")),
        "flooded_acres": _num(_get(rep, "summary.flooded_acres", "stats.flooded_acres")),
        "max_depth_ft": _num(_get(rep, "summary.max_depth_ft", "stats.max_depth_ft")),
        "max_velocity_fps": _num(_get(rep, "summary.max_velocity_fps")),
        "velocity_p99_fps": _num(_get(rep, "summary.velocity_p99_fps")),
        "latest_arrival_hr": _num(_get(rep, "summary.latest_arrival_hr")),
        "cell_m": _num(_get(rep, "settings.cell_size_m", "record_cell_m", "terrain_grid.cell_m")),
        "manning_n": _num(_get(rep, "settings.manning_n")),
        "cut_off": _get(rep, "cut_off.reason", "stop_reason"),
    }
    for k in ("dam_id", "dam_name", "scenario", "breach_method", "final_model", "tool", "cut_off"):
        if s[k] is not None:
            s[k] = str(s[k])
    return s


def read_hydrographs(folders):
    """[(series, [(t, q)...], path)] for the hydrograph CSVs in the folders (first one wins)."""
    out, seen = [], set()
    for f in folders:
        for p in sorted(glob.glob(os.path.join(glob.escape(f), "*.csv"))):
            n = os.path.basename(p).lower()
            if n == "breach_hydrograph.csv":
                series = "breach"
            elif n == "no_breach_hydrograph.csv":
                series = "no breach"
            elif n.startswith("gauge_"):
                series = "gauge " + os.path.splitext(os.path.basename(p))[0][6:]
            else:
                continue
            if series in seen:
                continue
            pts = read_series(p)
            if pts:
                seen.add(series)
                out.append((series, pts, p))
    return out


def read_series(path):
    pts = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            head = next(rd, None) or []
            low = [h.strip().lower() for h in head]
            ti = next((i for i, h in enumerate(low) if h.startswith("time")), 0)
            qi = next((i for i, h in enumerate(low) if "cfs" in h or "discharge" in h or "flow" in h), 1)
            cms = "cms" in (low[qi] if qi < len(low) else "") or "m3" in (low[qi] if qi < len(low) else "")
            for row in rd:
                if len(row) <= max(ti, qi):
                    continue
                t, q = _num(row[ti]), _num(row[qi])
                if t is None or q is None:
                    continue
                pts.append((t, q / 0.028316846592 if cms else q))
    except OSError:
        return []
    return pts


# ----------------------------------------------------------------------------- features
def _col(cols, names):
    low = {c.lower(): c for c in cols}
    for n in names:
        if n in low:
            return low[n]
    return None


def normalise_feature(row, layer, kind_default):
    """(kind, feature_id, name, address, depth, velocity, arrival) from one row of any of the
    feature tables, by column meaning (COL_*)."""
    cols = list(row.keys())

    def val(names):
        c = _col(cols, names)
        v = row.get(c) if c else None
        return None if v in ("", None) else v
    kind = val(COL_KIND) or kind_default
    fid = val(COL_ID)
    name = val(COL_NAME)
    addr = val(COL_ADDRESS)
    city = val(COL_CITY)
    if addr and city:
        addr = "%s, %s" % (addr, city)
    if isinstance(fid, float) and fid.is_integer():
        fid = int(fid)
    return (str(kind) if kind is not None else None, None if fid is None else str(fid),
            None if name is None else str(name), None if addr is None else str(addr),
            _num(val(COL_DEPTH)), _num(val(COL_VEL)), _num(val(COL_ARR)))


def wkt_center(text):
    """Centre of the bounding box of a WKT geometry (POINT, LINESTRING, ... with or without Z/M)."""
    if not text:
        return None
    m = re.match(r"^\s*([A-Za-z]+)\s*(ZM|Z|M)?\s*\(", text)
    if not m:
        return None
    dims = {"ZM": 4, "Z": 3, "M": 3}.get((m.group(2) or "").upper(), 2)
    body = text[m.end() - 1:]
    nums = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", body)]
    if dims == 2:
        # a coordinate list can still carry a third value without the Z tag (QGIS writes "POINT Z"
        # but other tools do not): use the commas to find how many numbers a vertex has
        first = re.search(r"\(+\s*([^,()]+)", body)
        if first:
            dims = max(2, len(first.group(1).split()))
    xs, ys = nums[0::dims], nums[1::dims]
    if not xs or not ys:
        return None
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def _wkb_coords(buf, off=0):
    """Every (x, y) of a WKB geometry and the offset after it (ISO and EWKB-less Z/M codes)."""
    endian = "<" if buf[off] == 1 else ">"
    gtype = struct.unpack_from(endian + "I", buf, off + 1)[0]
    off += 5
    base = gtype % 1000
    flag = gtype // 1000
    dims = 2 + (1 if flag in (1, 2) else 2 if flag == 3 else 0)
    if gtype & 0x80000000:                 # EWKB Z flag
        base, dims = gtype & 0xFF, 3
    pts = []

    def read_pts(n, o):
        for _ in range(n):
            x, y = struct.unpack_from(endian + "dd", buf, o)
            pts.append((x, y))
            o += 8 * dims
        return o
    if base == 1:
        off = read_pts(1, off)
    elif base == 2:
        n = struct.unpack_from(endian + "I", buf, off)[0]
        off = read_pts(n, off + 4)
    elif base == 3:
        nr = struct.unpack_from(endian + "I", buf, off)[0]
        off += 4
        for _ in range(nr):
            n = struct.unpack_from(endian + "I", buf, off)[0]
            off = read_pts(n, off + 4)
    elif base in (4, 5, 6, 7):
        n = struct.unpack_from(endian + "I", buf, off)[0]
        off += 4
        for _ in range(n):
            sub, off = _wkb_coords(buf, off)
            pts.extend(sub)
    return pts, off


def gpkg_center(blob):
    """Centre of a GeoPackage geometry blob: the envelope when it has one, else its vertices."""
    if not blob or len(blob) < 8 or blob[:2] != b"GP":
        return None
    flags = blob[3]
    endian = "<" if flags & 1 else ">"
    env = (flags >> 1) & 7
    size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env, 0)
    if flags & 0x10:                       # empty geometry
        return None
    if size:
        minx, maxx, miny, maxy = struct.unpack_from(endian + "dddd", blob, 8)
        return ((minx + maxx) / 2.0, (miny + maxy) / 2.0)
    try:
        pts, _ = _wkb_coords(bytes(blob), 8)
    except (struct.error, IndexError):
        return None
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def read_gpkg(path):
    """{layer name: (crs, [row dict with '_xy']) } of a GeoPackage, opened read-only and immutable
    (nothing about the file changes: QGIS's GeoPackage writer otherwise bumps its change counter)."""
    out = {}
    try:
        db = sqlite3.connect("file:%s?mode=ro&immutable=1" % _uri_path(path), uri=True)
    except sqlite3.Error:
        return out
    try:
        layers = db.execute("SELECT c.table_name, g.column_name, s.organization, s.organization_coordsys_id "
                            "FROM gpkg_contents c JOIN gpkg_geometry_columns g ON g.table_name=c.table_name "
                            "LEFT JOIN gpkg_spatial_ref_sys s ON s.srs_id=c.srs_id "
                            "WHERE c.data_type='features'").fetchall()
        for table, gcol, org, code in layers:
            crs = "%s:%s" % (org.upper(), code) if org and code is not None else None
            cols = [r[1] for r in db.execute('PRAGMA table_info("%s")' % table.replace('"', '""'))]
            rows = []
            q = 'SELECT %s FROM "%s"' % (", ".join('"%s"' % c.replace('"', '""') for c in cols),
                                         table.replace('"', '""'))
            for r in db.execute(q):        # nosemgrep - names come from the file's own catalog, quoted
                d = dict(zip(cols, r))
                g = d.pop(gcol, None)
                d["_xy"] = gpkg_center(g) if isinstance(g, (bytes, bytearray, memoryview)) else None
                rows.append(d)
            out[table] = (crs, rows)
    except sqlite3.Error:
        pass
    finally:
        db.close()
    return out


def read_csv_table(path):
    rows = []
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                d = {k: v for k, v in r.items() if k is not None}
                wkt_col = _col(d.keys(), ("wkt", "geometry", "geom"))
                xy = wkt_center(d.pop(wkt_col)) if wkt_col else None
                if xy is None:
                    x, y = _num(d.get(_col(d.keys(), ("x",)) or "")), _num(d.get(_col(d.keys(), ("y",)) or ""))
                    xy = (x, y) if x is not None and y is not None else None
                d["_xy"] = xy
                rows.append(d)
    except OSError:
        return []
    return rows


def hazard_folders(dam_folder):
    """Where Hazard wrote this dam's outputs: the folder the record's last classification names,
    else <dam>/outputs; DamIT Hazard: <dam>/classify."""
    out = []
    rec = _load_json(os.path.join(dam_folder, "dam.json")) or {}
    for s in reversed(rec.get("status") or []):
        res = s.get("result") if isinstance(s, dict) else None
        if isinstance(s, dict) and s.get("step") == "classified" and isinstance(res, dict) \
                and res.get("output_folder"):
            p = str(res["output_folder"])
            out.append(p if os.path.isabs(p) else os.path.join(dam_folder, p))
            break
    out += [os.path.join(dam_folder, "outputs"), os.path.join(dam_folder, "classify")]
    seen, res = set(), []
    for p in out:
        rp = os.path.realpath(p)
        if rp not in seen and os.path.isdir(p):
            seen.add(rp)
            res.append(p)
    return res


def hazard_tables(folder):
    """[(layer, kind, crs, rows, path)] of one Hazard output folder. A table written as .gpkg,
    .csv and .xlsx is read once, from the .gpkg."""
    out = []
    cls = os.path.join(folder, "Classification")
    search = [cls, folder] if os.path.isdir(cls) else [folder]
    for base, kind in HAZARD_TABLES:
        for d in search:
            gpkgs = sorted(glob.glob(os.path.join(glob.escape(d), base + "*.gpkg")))
            stems = set()
            for p in gpkgs:
                stem = os.path.splitext(os.path.basename(p))[0]
                for layer, (crs, rows) in read_gpkg(p).items():
                    out.append((stem if layer.lower() == base.lower() else layer, kind, crs, rows, p))
                stems.add(stem)
            for p in sorted(glob.glob(os.path.join(glob.escape(d), base + "*.csv"))):
                stem = os.path.splitext(os.path.basename(p))[0]
                if stem not in stems:
                    out.append((stem, kind, None, read_csv_table(p), p))
    p = os.path.join(folder, "damit_hazard_receptors.gpkg")
    if os.path.isfile(p):
        for layer, (crs, rows) in read_gpkg(p).items():
            out.append(("damit_" + layer, None, crs, rows, p))
    return out


def hazard_summary(folders):
    """(class, tm11 class, flooded structures, path) from Hazard's summary, or Nones."""
    for f in folders:
        for name in HAZARD_SUMMARY:
            p = os.path.join(f, name)
            if not os.path.isfile(p):
                continue
            if name.endswith(".json"):
                d = _load_json(p) or {}
                return (d.get("nd_class"), d.get("tm11_class"), d.get("flooded_structures"), p)
            with open(p, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            nd = re.search(r"^ND HAZARD CLASSIFICATION[^:]*:\s*([A-Za-z]+)", text, re.M)
            tm = re.search(r"^ACER TM 11 CLASSIFICATION:\s*([A-Za-z]+)", text, re.M)
            fl = re.search(r"(\d+) habitable structure\(s\) inundated", text)
            return (nd.group(1).title() if nd else None, tm.group(1).title() if tm else None,
                    int(fl.group(1)) if fl else None, p)
        p = os.path.join(f, "damit_hazard_report.json")
        if os.path.isfile(p):
            d = _load_json(p) or {}
            return (d.get("suggested_label"), None, None, p)
    return (None, None, None, None)


def find_named(folder, names, depth=3):
    out = []
    base = folder.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(folder):
        if root.count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
        for n in names:
            if n in files:
                out.append(os.path.join(root, n))
    return out


# ----------------------------------------------------------------------------- ingest
def ingest_report(db, report_path, log=_say):
    """Read one run into the database (replacing an earlier read of the same report)."""
    report_path = os.path.abspath(report_path)
    run_dir, model_dir, dam_dir = run_folders(report_path)
    if report_path.endswith(".md"):
        rep = report_from_md(report_path) or {}
        src = "run_report.md"
    else:
        rep = _load_json(report_path)
        if not isinstance(rep, dict):
            log("  skipped %s: not a JSON report" % report_path)
            return None
        src = "run_report.json"
    extra = {}

    def put(k, v):
        if extra.get(k) is None and v not in (None, ""):
            extra[k] = v
    # a Crest / copied folder: <dam>_record.json beside the report has the dam's facts
    for p in glob.glob(os.path.join(glob.escape(run_dir), "*_record.json")):
        d = (_load_json(p) or {}).get("dam") or {}
        for k, v in (("dam_id", d.get("id")), ("dam_name", d.get("name")),
                     ("top_of_dam_ft", _num(d.get("crestFt"))), ("dam_height_ft", _num(d.get("heightFt")))):
            put(k, v)
    dj = _load_json(os.path.join(dam_dir, "dam.json")) or {}
    fields = dj.get("fields") if isinstance(dj.get("fields"), dict) else {}
    put("dam_name", fields.get("dam_name") or fields.get("name"))
    put("dam_id", fields.get("dam_id"))
    rs = _load_json(os.path.join(run_dir, "run_setup.json")) or {}
    if isinstance(rs, dict) and rs:
        for k, path in (("dam_name", "dam.name"), ("dam_id", "dam.dam_id"), ("dam_height_ft", "dam.height_ft"),
                        ("storage_acft", "dam.storage_acft")):
            put(k, _get(rs, path))
        if _get(rs, "created_by.plugin"):
            put("tool", ("%s %s" % (_get(rs, "created_by.plugin"), _get(rs, "created_by.version") or "")).strip())
    if not rep.get("run_finished"):
        mt = _mtime(report_path)
        if mt:
            extra["run_finished"] = datetime.datetime.fromtimestamp(mt).isoformat(timespec="seconds")
    row = run_row(rep, extra)
    if not row["dam_id"]:
        row["dam_id"] = os.path.basename(dam_dir)
    folders = [model_dir, run_dir, os.path.join(run_dir, "outputs")]
    hyd = read_hydrographs(folders)
    if row["peak_breach_cfs"] is None:
        for series, pts, _p in hyd:
            if series == "breach":
                row["peak_breach_cfs"] = max(q for _, q in pts)
    if row["time_to_peak_hr"] is None:
        for series, pts, _p in hyd:
            if series == "breach":
                row["time_to_peak_hr"] = max(pts, key=lambda tq: tq[1])[0]
    hz_dirs = hazard_folders(dam_dir)
    hz_class, tm11, flooded, hz_path = hazard_summary(hz_dirs)
    run_t = _mtime(report_path)
    hz_t = _mtime(hz_path) if hz_path else None
    row.update({"hazard_class": hz_class, "tm11_class": tm11, "flooded_structures": flooded,
                "hazard_folder": os.path.dirname(hz_path) if hz_path else None,
                "hazard_older_than_run": (None if hz_t is None or run_t is None else int(hz_t < run_t - 1.0)),
                "folder": run_dir, "dam_folder": dam_dir, "source": src,
                "ingested": datetime.datetime.now().isoformat(timespec="seconds")})
    key = os.path.realpath(report_path)
    old = db.execute("SELECT run_id FROM runs WHERE run_key=?", (key,)).fetchone()
    if old:
        delete_runs(db, [old[0]])
    cols = ["run_key"] + list(row.keys())
    cur = db.execute("INSERT INTO runs (%s) VALUES (%s)" % (", ".join(cols), ", ".join("?" * len(cols))),
                     [key] + list(row.values()))
    rid = cur.lastrowid
    # every other number and text of the report
    facts = []
    for k, v in flatten({k: v for k, v in rep.items() if k not in ("warnings", "_from_md")}):
        if isinstance(v, bool):
            facts.append((rid, k, float(v), str(v)))
        elif isinstance(v, (int, float)):
            facts.append((rid, k, float(v) if math.isfinite(float(v)) else None, None))
        elif v is not None:
            facts.append((rid, k, None, str(v)[:2000]))
    db.executemany("INSERT INTO facts VALUES (?,?,?,?)", facts)
    db.executemany("INSERT INTO warnings VALUES (?,?)",
                   [(rid, str(w)) for w in (rep.get("warnings") or []) if w])
    for series, pts, _p in hyd:
        db.executemany("INSERT INTO hydrograph VALUES (?,?,?,?)", [(rid, series, t, q) for t, q in pts])
    # rasters: the report's own paths when they still exist, else by name in the run's folders
    finals = _get(rep, "outputs.final") or {}
    if not isinstance(finals, dict):
        finals = {}
    for role, name, units in FINAL_RASTERS:
        p = finals.get(role) or _get(rep, "outputs." + role)
        if not (isinstance(p, str) and os.path.isfile(p)):
            p = _first_file([name], [run_dir, os.path.join(run_dir, "outputs"), dam_dir])
        if p:
            db.execute("INSERT INTO rasters VALUES (?,?,?,?)", (rid, role, os.path.abspath(p), units))
    # features: the Runner's receptors, Hazard's tables, the consequences tool
    n_feat = {}
    tables = []
    rp = _first_file(["receptors_flooded.csv"], [model_dir, run_dir])
    if rp:
        tables.append(("runner_receptors", "receptor", _get(rep, "work_crs"), read_csv_table(rp), rp))
    for d in hz_dirs:
        tables += hazard_tables(d)
    for p in find_named(dam_dir, [n for n, _k in CONSEQUENCE_FILES]):
        kind = dict(CONSEQUENCE_FILES)[os.path.basename(p)]
        tables.append((os.path.splitext(os.path.basename(p))[0], kind, None, read_csv_table(p), p))
    for layer, kind_default, crs, rows, path in tables:
        batch = []
        for r in rows:
            xy = r.pop("_xy", None)
            kind, fid, name, addr, dep, vel, arr = normalise_feature(r, layer, kind_default)
            attrs = json.dumps({k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
                                for k, v in r.items()}, default=str)
            batch.append((rid, layer, kind, fid, name, addr, dep, vel, arr,
                          xy[0] if xy else None, xy[1] if xy else None, crs, path, attrs))
        db.executemany("INSERT INTO features VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        n_feat[layer] = n_feat.get(layer, 0) + len(batch)
    log("  %s  %s%s  peak %s cfs, max depth %s ft%s%s" % (
        row["dam_id"], row["scenario"] or "?", (" " + row["final_model"]) if row["final_model"] else "",
        _fmt(row["peak_breach_cfs"], 0), _fmt(row["max_depth_ft"], 2),
        ("; " + ", ".join("%s %d" % kv for kv in sorted(n_feat.items()))) if n_feat else "; no structure tables",
        "  (Hazard outputs are OLDER than this run)" if row["hazard_older_than_run"] else ""))
    return rid


def add(paths, db_path=None, log=_say):
    """Read every run below each path. Returns the number of runs read."""
    db = connect(db_path, write=True)
    n = 0
    try:
        for p in paths:
            reps = find_reports(p)
            if not reps:
                log("no run_report.json (or run_report.md) found in %s" % p)
                continue
            log("%s: %d run(s)" % (p, len(reps)))
            for r in reps:
                with db:
                    if ingest_report(db, r, log=log) is not None:
                        n += 1
    finally:
        db.close()
    log("%d run(s) in %s" % (n, db_path or DB))
    return n


def delete_runs(db, ids):
    for t in ("facts", "hydrograph", "features", "warnings", "rasters", "runs"):
        db.executemany("DELETE FROM %s WHERE run_id=?" % t, [(i,) for i in ids])  # nosec B608 - fixed table names


def dam_ids(db):
    return [r[0] for r in db.execute("SELECT DISTINCT dam_id FROM runs WHERE dam_id IS NOT NULL")]


def match_runs(db, dam):
    """Runs of a dam named by id (DWR-2801), its number (2801), NID (ND00199) or part of its
    name; newest first."""
    dam = (dam or "").strip()
    if not dam:
        return []
    sq = re.sub(r"[^a-z0-9]", "", dam.lower())
    rows = db.execute("SELECT run_id, dam_id, dam_name, scenario, final_model, run_finished FROM runs "
                      "ORDER BY run_finished DESC, run_id DESC").fetchall()
    exact = [r for r in rows if re.sub(r"[^a-z0-9]", "", (r[1] or "").lower()) == sq]
    if exact:
        return exact
    num = [r for r in rows if sq.isdigit() and re.sub(r"\D", "", r[1] or "") == sq.lstrip("0")]
    num = num or [r for r in rows if sq.isdigit() and re.sub(r"\D", "", r[1] or "") == sq]
    if num:
        return num
    return [r for r in rows if len(sq) >= 4 and sq in re.sub(r"[^a-z0-9]", "", (r[2] or "").lower())]


def dams_named_in(text, db_path=None):
    """The dam ids of the database that a question names (id, number or name). [] when none,
    or when there is no database yet."""
    db = connect(db_path)
    if db is None:
        return []
    try:
        rows = db.execute("SELECT DISTINCT dam_id, dam_name FROM runs").fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    low = text.lower()
    toks = set(re.findall(r"[a-z0-9]+(?:[-_ ]?[a-z0-9]+)?", low))
    flat = {re.sub(r"[^a-z0-9]", "", t) for t in toks}
    words = set(re.findall(r"[a-z0-9]+", low))
    out = []
    for did, name in rows:
        if not did:
            continue
        d = re.sub(r"[^a-z0-9]", "", did.lower())
        digits = re.sub(r"\D", "", did)
        if d in flat or (len(digits) >= 3 and digits in words) or \
                (name and len(name) >= 6 and name.lower() in low):
            out.append(did)
    return out


# ----------------------------------------------------------------------------- output
def _fmt(x, nd=2):
    if x is None:
        return "-"
    if isinstance(x, float):
        if nd == 0:
            return "{:,.0f}".format(x)
        t = ("{:,.%df}" % nd).format(x)
        return t.rstrip("0").rstrip(".") if "." in t else t
    return str(x)


def table(cols, rows, limit=MAX_ROWS):
    """A plain text table (numbers as stored, nothing rounded away)."""
    if not rows:
        return "(no rows)"
    shown = rows[:limit]
    cells = [[("" if v is None else repr(v) if isinstance(v, float) and not v.is_integer() else
               str(int(v)) if isinstance(v, float) else str(v)) for v in r] for r in shown]
    cells = [[c if len(c) <= 60 else c[:57] + "..." for c in r] for r in cells]
    w = [max([len(str(c))] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    out = ["  ".join(str(c).ljust(w[i]) for i, c in enumerate(cols)),
           "  ".join("-" * w[i] for i in range(len(cols)))]
    out += ["  ".join(r[i].ljust(w[i]) for i in range(len(cols))) for r in cells]
    if len(rows) > limit:
        out.append("... %d more row(s)" % (len(rows) - limit))
    return "\n".join(out)


def cmd_list(db):
    rows = db.execute("SELECT dam_id, dam_name, scenario, final_model, peak_breach_cfs, max_depth_ft, "
                      "flooded_acres, hazard_class, run_finished, "
                      "(SELECT COUNT(*) FROM features f WHERE f.run_id=runs.run_id) "
                      "FROM runs ORDER BY dam_id, run_finished").fetchall()
    _say(table(["dam", "name", "scenario", "model", "peak_cfs", "max_depth_ft", "flooded_ac",
                "hazard", "finished", "features"], rows))


def describe_run(db, run_id):
    r = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    cols = [d[0] for d in db.execute("SELECT * FROM runs LIMIT 0").description]
    d = dict(zip(cols, r))
    lines = ["%s%s  %s run, %s (%s)" % (d["dam_id"], " " + d["dam_name"] if d["dam_name"] else "",
                                         d["scenario"] or "?", d["final_model"] or "?", d["tool"] or "?"),
             "  finished %s   folder %s" % (d["run_finished"] or "?", d["folder"])]
    groups = [
        ("Breach", [("peak breach outflow", "peak_breach_cfs", "cfs"), ("time to peak", "time_to_peak_hr", "h"),
                    ("breach method", "breach_method", ""), ("average breach width", "avg_breach_width_ft", "ft"),
                    ("formation time", "formation_time_hr", "h"),
                    ("starting WSE (pool at failure)", "starting_wse_ft", "ft"),
                    ("breach bottom", "breach_bottom_ft", "ft"), ("top of dam", "top_of_dam_ft", "ft"),
                    ("dam height", "dam_height_ft", "ft"), ("storage at failure", "storage_at_failure_acft", "ac-ft"),
                    ("breach volume", "breach_volume_acft", "ac-ft")]),
        ("Flood", [("flooded area", "flooded_acres", "ac"), ("maximum depth", "max_depth_ft", "ft"),
                   ("maximum velocity (one cell)", "max_velocity_fps", "ft/s"),
                   ("velocity P99", "velocity_p99_fps", "ft/s"), ("latest arrival", "latest_arrival_hr", "h"),
                   ("model cell", "cell_m", "m"), ("Manning n", "manning_n", ""), ("cut off", "cut_off", "")]),
        ("Hazard", [("ND class", "hazard_class", ""), ("ACER TM 11 class", "tm11_class", ""),
                    ("flooded habitable structures", "flooded_structures", "")]),
    ]
    for title, items in groups:
        got = [(label, d.get(col), unit) for label, col, unit in items if d.get(col) not in (None, "")]
        if not got:
            continue
        lines.append("  " + title)
        for label, v, unit in got:
            lines.append("    %-32s %s%s" % (label, v if not isinstance(v, float) else _fmt(v, 3),
                                             (" " + unit) if unit else ""))
    if d.get("hazard_older_than_run"):
        lines.append("    NOTE: the Hazard outputs are older than this run; re-classify before using them")
    n = db.execute("SELECT kind, COUNT(*), SUM(depth_ft > 0) FROM features WHERE run_id=? GROUP BY kind",
                   (run_id,)).fetchall()
    if n:
        lines.append("  Features: " + ", ".join("%s %d (%d wet)" % (k or "?", c, w or 0) for k, c, w in n))
    w = db.execute("SELECT COUNT(*) FROM warnings WHERE run_id=?", (run_id,)).fetchone()[0]
    if w:
        lines.append("  %d thing(s) to check in the run report (heim runs sql \"SELECT text FROM warnings "
                     "WHERE run_id=%d\")" % (w, run_id))
    return "\n".join(lines)


def cmd_show(db, dam):
    runs = match_runs(db, dam)
    if not runs:
        sys.exit("no run of '%s' in %s (heim runs list)" % (dam, DB))
    for r in runs:
        _say(describe_run(db, r[0]))
        _say("")


def cmd_hydrograph(db, dam, every=None):
    runs = match_runs(db, dam)
    if not runs:
        sys.exit("no run of '%s' in %s" % (dam, DB))
    for r in runs:
        pts = db.execute("SELECT series, time_hr, flow_cfs FROM hydrograph WHERE run_id=? ORDER BY series, time_hr",
                         (r[0],)).fetchall()
        _say("%s %s %s" % (r[1], r[3] or "", r[4] or ""))
        if not pts:
            _say("  no hydrograph CSV was found with this run")
            continue
        series = sorted({p[0] for p in pts})
        for s in series:
            sp = [(t, q) for ss, t, q in pts if ss == s]
            t_pk, q_pk = max(sp, key=lambda tq: tq[1])
            _say("  %s: %d points, peak %s cfs at %s h, ends at %s h" % (
                s, len(sp), _fmt(q_pk, 1), _fmt(t_pk, 3), _fmt(sp[-1][0], 3)))
            if every:
                step = max(1, int(every))
                _say(table(["time_hr", "flow_cfs"], sp[::step]))


# ----------------------------------------------------------------------------- `at`
_LATLON = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*[, ]\s*([-+]?\d+(?:\.\d+)?)\s*$")


def parse_point(text):
    """(x, y, is_lonlat) from 'lat,lon' (degrees; either order is recognised by range) or 'x,y'
    (projected, in the raster's CRS). None when the text is not a point."""
    m = _LATLON.match(text or "")
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    if abs(a) <= 180 and abs(b) <= 180 and min(abs(a), abs(b)) <= 90:
        if abs(b) > 90 or (a > 0 > b):          # "lat, lon" (North America: lon negative)
            return (b, a, True)
        if abs(a) > 90 or (b > 0 > a):          # "lon, lat"
            return (a, b, True)
        return (b, a, True)                     # both small and same sign: read as "lat, lon"
    return (a, b, False)


def sample_rasters(rasters, x, y, lonlat):
    """{role: value} at a point, with GDAL. Raises ImportError when GDAL is not importable."""
    from osgeo import gdal, osr
    gdal.UseExceptions()
    osr.UseExceptions()
    out = {}
    for role, path, units in rasters:
        try:
            ds = gdal.Open(path)
        except RuntimeError:
            ds = None
        if ds is None:
            out[role] = ("cannot open %s" % path, units)
            continue
        px, py = x, y
        if lonlat:
            src = osr.SpatialReference()
            src.ImportFromEPSG(4326)
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst = osr.SpatialReference(wkt=ds.GetProjection())
            dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            px, py = osr.CoordinateTransformation(src, dst).TransformPoint(x, y)[:2]
        gt = ds.GetGeoTransform()
        col = int(math.floor((px - gt[0]) / gt[1]))
        row = int(math.floor((py - gt[3]) / gt[5]))
        if not (0 <= col < ds.RasterXSize and 0 <= row < ds.RasterYSize):
            out[role] = ("outside the model area", units)
            continue
        b = ds.GetRasterBand(1)
        v = b.ReadAsArray(col, row, 1, 1)[0][0]
        nd = b.GetNoDataValue()
        if nd is not None and float(v) == float(nd) or not math.isfinite(float(v)):
            out[role] = ("dry (no data)", units)
        else:
            out[role] = (float(v), units)
        out["_xy"] = (px, py)
    return out


def qgis_python():
    """The QGIS conda environment's Python (it has GDAL), or None."""
    env = os.environ.get("NDQGIS_PYTHON")
    cands = [env] if env else []
    for root in ("miniforge3", "miniconda3", "mambaforge", "anaconda3", "opt/miniforge3"):
        cands.append(os.path.expanduser("~/%s/envs/ndqgis/bin/python" % root))
    for c in cands:
        if c and os.path.isfile(c) and os.path.abspath(c) != os.path.abspath(sys.executable):
            return c
    return None


def cmd_at(db, dam, place, near_m=100.0):
    runs = match_runs(db, dam)
    if not runs:
        sys.exit("no run of '%s' in %s (heim runs list)" % (dam, DB))
    ids = [r[0] for r in runs]
    pt = parse_point(place)
    if pt:
        x, y, lonlat = pt
        for r in runs:
            rasters = db.execute("SELECT role, path, units FROM rasters WHERE run_id=?", (r[0],)).fetchall()
            _say("%s %s %s: at %s %s" % (r[1], r[3] or "", r[4] or "", place, "(lat, lon)" if lonlat else "(x, y)"))
            if not rasters:
                _say("  this run has no result rasters on record")
                continue
            try:
                vals = sample_rasters(rasters, x, y, lonlat)
            except ImportError:
                py = qgis_python()
                if py and not os.environ.get("HEIM_RUNS_NO_REEXEC"):
                    _say("  (GDAL is not in this Python; reading the rasters with %s)" % py)
                    env = dict(os.environ, HEIM_RUNS_NO_REEXEC="1", HEIM_RUNS_DB=DB)
                    subprocess.call([py, os.path.abspath(__file__), "at", dam, place], env=env)  # nosec B603
                    return
                _say("  GDAL is not importable in this Python, so the rasters cannot be read at a point.\n"
                     "  Use the QGIS environment's Python:  NDQGIS_PYTHON=/path/to/ndqgis/bin/python heim runs at ...\n"
                     "  or name a structure / road / address instead of a point.")
                return
            pxy = vals.pop("_xy", None)
            for role in ("depth", "velocity", "arrival", "wse"):
                if role in vals:
                    v, u = vals[role]
                    _say("  %-9s %s %s" % (role, _fmt(v, 3) if isinstance(v, float) else v,
                                            u if isinstance(v, float) else ""))
            if pxy:
                near = db.execute("SELECT kind, feature_id, COALESCE(name, address), depth_ft, velocity_fps, "
                                  "arrival_hr, ((x-?)*(x-?)+(y-?)*(y-?)) AS d2 FROM features "
                                  "WHERE run_id=? AND x IS NOT NULL ORDER BY d2 LIMIT 5",
                                  (pxy[0], pxy[0], pxy[1], pxy[1], r[0])).fetchall()
                near = [n for n in near if n[6] is not None and math.sqrt(n[6]) <= near_m]
                if near:
                    _say("  features within %g m:" % near_m)
                    _say(table(["kind", "id", "name/address", "depth_ft", "vel_fps", "arrival_hr", "dist_m"],
                               [n[:6] + (round(math.sqrt(n[6]), 1),) for n in near]))
        return
    like = "%" + place.strip() + "%"
    q = ("SELECT r.dam_id, r.scenario, r.final_model, f.layer, f.kind, f.feature_id, f.name, f.address, "
         "f.depth_ft, f.velocity_fps, f.arrival_hr FROM features f JOIN runs r ON r.run_id=f.run_id "
         "WHERE f.run_id IN (%s) AND (f.feature_id = ? OR f.name LIKE ? OR f.address LIKE ? "
         "OR f.attrs LIKE ?) ORDER BY f.depth_ft DESC" % ",".join("?" * len(ids)))  # nosec B608 - placeholders only
    rows = db.execute(q, ids + [place.strip(), like, like, like]).fetchall()
    if not rows:
        _say("nothing named '%s' in the structure / road tables of %s. `heim runs sql \"SELECT DISTINCT "
             "name, address FROM features\"` lists what is there." % (place, dam))
        return
    _say(table(["dam", "scenario", "model", "layer", "kind", "id", "name", "address", "depth_ft",
                "velocity_fps", "arrival_hr"], rows))


# ----------------------------------------------------------------------------- SQL (read-only)
_FORBIDDEN = re.compile(r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|replace|vacuum|"
                        r"reindex|analyze|begin|commit|rollback|savepoint|release)\b", re.I)


def check_sql(sql):
    """The query, cleaned, or raises ValueError: one SELECT (or WITH ... SELECT) statement."""
    s = (sql or "").strip().strip("`").strip()
    s = re.sub(r"^sql\s*", "", s, flags=re.I).strip()
    s = s.rstrip(";").strip()
    if not s:
        raise ValueError("empty query")
    if ";" in re.sub(r"'[^']*'", "''", s):
        raise ValueError("one statement only")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("only SELECT queries are run")
    if _FORBIDDEN.search(re.sub(r"'[^']*'", "''", s)):
        raise ValueError("only SELECT queries are run")
    return s


def run_sql(sql, db_path=None, limit=MAX_ROWS):
    """(columns, rows, total) of a read-only query. The connection is read-only, so a query
    that got past check_sql still cannot change anything; a slow one is stopped."""
    s = check_sql(sql)
    db = connect(db_path)
    if db is None:
        raise ValueError("no run database yet at %s: heim runs add FOLDER" % (db_path or DB))
    import time
    t0 = time.time()
    db.set_progress_handler(lambda: 1 if time.time() - t0 > QUERY_SECONDS else 0, 20000)
    try:
        cur = db.execute(s)  # nosemgrep - read-only connection, one SELECT checked above (by design)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(limit + 1)
        total = len(rows)
        if total > limit:
            total += len(cur.fetchall())
        return cols, rows[:limit], total
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise ValueError("the query ran longer than %g s and was stopped" % QUERY_SECONDS)
        raise ValueError(str(e))
    finally:
        db.close()


# ----------------------------------------------------------------------------- `ask`
def catalog(db_path=None):
    """What is in the database, in a few lines, for the model: the runs and the layers."""
    db = connect(db_path)
    if db is None:
        return "(empty)"
    try:
        runs = db.execute("SELECT run_id, dam_id, dam_name, scenario, final_model, run_finished FROM runs "
                          "ORDER BY dam_id, run_finished").fetchall()
        layers = db.execute("SELECT run_id, layer, kind, COUNT(*), SUM(depth_ft > 0) FROM features "
                            "GROUP BY run_id, layer, kind").fetchall()
        series = db.execute("SELECT run_id, series, COUNT(*) FROM hydrograph GROUP BY run_id, series").fetchall()
    finally:
        db.close()
    lines = ["Runs (run_id | dam_id | dam_name | scenario | final_model | finished):"]
    lines += ["  %s | %s | %s | %s | %s | %s" % r for r in runs[:80]]
    if len(runs) > 80:
        lines.append("  ... %d more" % (len(runs) - 80))
    if layers:
        lines.append("Feature layers (run_id | layer | kind | rows | wet):")
        lines += ["  %s | %s | %s | %d | %d" % (a, b, c, d, e or 0) for a, b, c, d, e in layers[:120]]
    if series:
        lines.append("Hydrograph series (run_id | series | points):")
        lines += ["  %s | %s | %d" % s for s in series[:80]]
    return "\n".join(lines)


SQL_RULES = """\
Write ONE SQLite SELECT that answers the question from the tables below. Rules:
- Return JSON only: {"sql": "SELECT ..."}.
- Match dams with dam_id LIKE '%2801%' (the user may give only the number) and names, roads and
  addresses with LIKE '%...%' (case does not matter in LIKE). Never compare text with = unless it
  is an id you can see in the catalog.
- Always select dam_id and scenario (from runs) so the answer can say which run it is, and the
  column names with their units.
- A question about one place (a structure, address, road, facility) is answered from features
  (depth_ft, velocity_fps, arrival_hr). Peak flow / breach / flood totals come from runs.
  A number not in runs is in facts (key LIKE '%...%').
- When a dam has several runs, return all of them unless the question picks one.
- Add LIMIT 50.
"""


def ollama_chat(messages, fmt=None, timeout=900):
    payload = {"model": CHAT_MODEL, "messages": messages, "stream": False, "think": False,
               "options": {"num_ctx": NUM_CTX, "temperature": 0}}
    if fmt:
        payload["format"] = fmt
    for attempt in (1, 2):
        req = urllib.request.Request(BASE + "/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 - local Ollama URL
                out = json.loads(r.read())
            return ((out.get("message") or {}).get("content") or "").strip()
        except urllib.error.HTTPError as e:
            if attempt == 1 and e.code == 400 and "think" in payload:   # an Ollama without "think"
                payload.pop("think")
                continue
            raise
    return ""


def sql_from_reply(text):
    t = (text or "").strip()
    try:
        d = json.loads(t)
        if isinstance(d, dict) and d.get("sql"):
            return str(d["sql"])
    except ValueError:
        pass
    m = re.search(r"```(?:sql)?\s*(.*?)```", t, re.S | re.I)
    if m:
        return m.group(1)
    m = re.search(r"((?:select|with)\b.*)", t, re.S | re.I)
    return m.group(1) if m else t


def numbers_in(text):
    return [m.group(0) for m in re.finditer(r"(?<![A-Za-z_-])[-+]?\d[\d,]*\.?\d*", text or "")]


def unsupported_numbers(answer, rows, question=""):
    """Numbers in the answer that are not in the query result (rounding allowed), nor in the
    question. A number the model wrote that the table does not hold is how a wrong figure would
    get through; the reader is told which ones."""
    have = []
    for r in rows:
        for v in r:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                have.append(float(v))
            elif isinstance(v, str):
                have += [_num(n) for n in numbers_in(v) if _num(n) is not None]
    have += [_num(n) for n in numbers_in(question) if _num(n) is not None]
    bad = []
    for n in numbers_in(answer):
        v = _num(n)
        if v is None:
            continue
        dec = len(n.split(".")[1]) if "." in n else 0
        tol = 0.5 * 10 ** (-dec) + 1e-9
        if any(abs(v - h) <= tol or abs(v - round(h, dec)) <= 1e-9 for h in have):
            continue
        if v.is_integer() and 0 <= v <= 10:         # "1 structure", "two runs", list numbers
            continue
        bad.append(n)
    return bad


def ask(question, db_path=None, out=_say, chat=None):
    """Answer a question about the runs: the model writes one SELECT, the code runs it read-only
    and prints the rows, the model phrases the answer from the rows only, and the code checks the
    numbers in that sentence against the rows. Returns the answer text ('' when only rows)."""
    chat = chat or ollama_chat
    probe = connect(db_path)
    if probe is None:
        out("No run results yet. Read some in first:  heim runs add ~/DamSafety/Dams")
        return ""
    probe.close()
    sys_msg = SQL_RULES + "\n" + SCHEMA_DOC + "\nWhat is in the database now:\n" + catalog(db_path)
    msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": question}]
    cols = rows = sql = None
    total = 0
    for attempt in (1, 2):
        try:
            reply = chat(msgs, fmt="json")
        except Exception as e:                       # Ollama not running, model missing
            out("could not reach the model (%s). Is Ollama running? heim-start\n"
                "Without the model:  heim runs show DAM,  heim runs at DAM PLACE,  heim runs sql \"SELECT ...\""
                % e)
            return ""
        sql = sql_from_reply(reply)
        try:
            cols, rows, total = run_sql(sql, db_path)
        except ValueError as e:
            if attempt == 2:
                out("Query: %s\nIt failed: %s" % (sql, e))
                return ""
            msgs += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": "That query failed: %s. Write a corrected one. JSON only." % e}]
            continue
        if not rows and attempt == 1:
            msgs += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": "That query returned no rows. Check the names against the "
                                                 "catalog and use LIKE '%...%'. If it really is not there, "
                                                 "return the same query. JSON only."}]
            continue
        break
    out("Query: %s" % check_sql(sql))
    out(table(cols, rows))
    if total > len(rows):
        out("(%d rows in all; %d shown)" % (total, len(rows)))
    if not rows:
        out("Nothing in the run results matches that. `heim runs list` shows what is there.")
        return ""
    data = "\n".join([" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r)
                                           for r in rows[:50]])
    try:
        ans = chat([{"role": "system", "content":
                     "Answer the question in one to three plain sentences using ONLY the rows below. "
                     "Copy numbers exactly as they appear (you may round to fewer decimals) with their "
                     "units from the column names. Name the dam and scenario. If the rows do not answer "
                     "the question, say so. Do not add anything that is not in the rows."},
                    {"role": "user", "content": "Question: %s\n\nRows:\n%s" % (question, data)}])
    except Exception as e:
        out("(the rows above are the answer; the model could not phrase it: %s)" % e)
        return ""
    if not ans:
        out("(the rows above are the answer; the model returned nothing)")
        return ""
    out("-" * 70)
    out(ans)
    bad = unsupported_numbers(ans, rows, question)
    if bad:
        out("CHECK: %s in that answer %s not in the query result; trust the table above." % (
            ", ".join(bad), "is" if len(bad) == 1 else "are"))
    return ans


# ----------------------------------------------------------------------------- main
def main(argv=None):
    global DB
    ap = argparse.ArgumentParser(
        prog="heim runs",
        description="Dam-breach run results (peak flow, depth / velocity / arrival at each structure) in a "
                    "database Heim answers from with exact numbers.")
    ap.add_argument("--db", default=None, help="the run database (default ~/local-ai/runs.db, or HEIM_RUNS_DB)")
    ap.add_argument("--version", action="version", version="run_results " + VERSION)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("add", help="read every run below the folders (dam folders, a Dams folder, a run folder)")
    p.add_argument("paths", nargs="+")
    sub.add_parser("list", help="the runs in the database")
    p = sub.add_parser("show", help="one dam's numbers: breach, flood, hazard")
    p.add_argument("dam", help="DWR-2801, 2801, ND00199 or part of the name")
    p = sub.add_parser("at", help="depth / velocity / arrival at a structure, road, address or point")
    p.add_argument("dam")
    p.add_argument("place", nargs="+", help="a structure id, a name or address (part of it), or 'lat,lon' / 'x,y'")
    p.add_argument("--near-m", type=float, default=100.0, help="with a point: list features this close")
    p = sub.add_parser("hydrograph", help="the hydrograph(s) of a dam's runs: peak and time to peak")
    p.add_argument("dam")
    p.add_argument("--every", type=int, default=None, help="also print every Nth point")
    p = sub.add_parser("ask", help="a question in words; Heim writes the query, the code runs it")
    p.add_argument("question", nargs="+")
    p = sub.add_parser("sql", help="your own SELECT (read-only)")
    p.add_argument("query", nargs="+")
    sub.add_parser("schema", help="the tables and what each column means")
    p = sub.add_parser("forget", help="take a dam's runs out of the database")
    p.add_argument("dam", nargs="?")
    p.add_argument("--all", action="store_true", help="empty the database")
    args = ap.parse_args(argv)
    if args.db:
        DB = os.path.abspath(os.path.expanduser(args.db))
    if not args.cmd:
        ap.print_help()
        return 0
    if args.cmd == "add":
        return 0 if add(args.paths) else 1
    if args.cmd == "schema":
        _say(SCHEMA_DOC)
        return 0
    if args.cmd == "ask":
        ask(" ".join(args.question))
        return 0
    if args.cmd == "sql":
        try:
            cols, rows, total = run_sql(" ".join(args.query))
        except ValueError as e:
            sys.exit("query refused or failed: %s" % e)
        _say(table(cols, rows))
        if total > len(rows):
            _say("(%d rows in all; %d shown)" % (total, len(rows)))
        return 0
    if args.cmd == "forget":
        db = connect(write=True)
        with db:
            if args.all:
                ids = [r[0] for r in db.execute("SELECT run_id FROM runs")]
            elif args.dam:
                ids = [r[0] for r in match_runs(db, args.dam)]
            else:
                sys.exit("forget what? a dam id, or --all")
            delete_runs(db, ids)
        db.close()
        _say("%d run(s) taken out" % len(ids))
        return 0
    db = connect()
    if db is None:
        sys.exit("No run results yet at %s. Read some in first:  heim runs add ~/DamSafety/Dams" % DB)
    try:
        if args.cmd == "list":
            cmd_list(db)
        elif args.cmd == "show":
            cmd_show(db, args.dam)
        elif args.cmd == "at":
            cmd_at(db, args.dam, " ".join(args.place), args.near_m)
        elif args.cmd == "hydrograph":
            cmd_hydrograph(db, args.dam, args.every)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
