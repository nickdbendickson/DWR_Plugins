#!/usr/bin/env python3
"""NickReview dams on a Mac, one dam at a time, the same way as the box reruns (Oct 2026):

  pack extracts -> 3DHP streams -> ndhazard:runnerculverts -> culvert pre-check -> Screening 7b
  record (dam.json) -> ndbreach:rundam (LISFLOOD-FP pre-pass, then forced ANUGA with gmsh)
  -> ndbreach:ndhazardclass -> ndhazard:batch

Started by run_nickreview.sh (which sets the environment); run with the damsafety-qgis python.
The data folder (--data) is only read. Everything is written under --out.

Dams with a finished box preparation (DWR-2801 with the full-native v2 DEM and the invfix2
structures, DWR-2865, DWR-3034) reuse it ("kit" mode); the others are prepared here from the
NickReview screening record (Dams/NickReview/dams/<ID>/dam.json) with the same settings.

rundam starts only after the Screening 7b record (dams/<ID>/dam.json, from
Dams/NickReview/screen_maxstor) is written, as on the box: without it the Runner models the
dam with its default crest length instead of the record's dam_length_ft. --allow-no-record
runs it anyway (and says so in the log and the manifest).

Finished stages are skipped. --redo runs again, in the same command, the stages in --stages
that apply to the dam and every later stage that reads their results (READS below: precheck
is a report no later stage reads); the other stages keep their results. prepare and
structures do not apply to the kit dams (unless --no-kits), so '--redo --stages prepare'
changes nothing for them. A --redo command is a session (<out>/redo_sessions/): run the same
command again after a stop and it goes on where it stopped, without redoing the dams it
already redid; a plain run while one is unfinished says so and prints its command.
status/*.ok records the mode (kit or prepare) a stage ran in: a dam whose finished stages ran
in the other mode is not run (the two would mix) unless --redo is given, which then runs every
stage of the dam again.

On the box (no installer), --list-data runs without the solvers and plugins, with the box's
QGIS python (any python with GDAL):
  <the box's QGIS python> nickreview_mac.py --list-data data_files_nickreview.tsv \\
      --data /home/box/DamSafety --out /tmp/nr_list --dams-file nickreview_dams.txt
(add --dam DWR-2801 and absaraka/data_files_DWR-2801.tsv for that list).
"""
import argparse
import csv
import datetime
import glob
import hashlib
import json
import os
import platform
import random
import re
import shlex
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

MILE = 1609.344
# Box path -> folder name inside --data (the copy_to column of the data lists)
BOX_PREFIXES = [
    ("/home/box/DamSafety", ""),
    ("/workspace/nickreview_rerun_20261023", "nickreview_rerun_20261023"),
    ("/workspace/culvert_sources", "culvert_sources"),
    ("/workspace/culvert_precheck_20261024", "culvert_precheck_20261024"),
    ("/workspace/offline_packs_20261021", "offline_packs_20261021"),
]
# Finished box preparations (rundam.json + hazclass.json), relative to --data
# (all paths below are the box paths; Ctx.map_path finds them under --data)
NR = "/workspace/nickreview_rerun_20261023/"
KITS = {
    "DWR-2801": NR + "dwr2801_maincc5892e",  # full_native_v2 DEM + invfix2 structures
    "DWR-2865": NR + "dwr2865",
    "DWR-3034": NR + "dwr3034",
}
PACK_SQLITE = "/workspace/offline_packs_20261021/hazard_pack/nd_offline_pack_20261021/nd_offline_pack.sqlite"
SUITE = "/home/box/DamSafety/ND_Dam_Suite_Pack"
SUITE_LAYERS = {  # pack name -> suite pack file (used when no offline pack sqlite is given)
    "NSI_structures": "structures/NSI_structures.gpkg",
    "ND_roads_NG911": "roads/ND_roads_NG911.gpkg",
    "ND_county_roads": "roads/ND_county_roads.gpkg",
    "HPMS_ND_2024": "roads/HPMS_ND_2024.gpkg",
    "NDDOT_traffic_counts": "roads/NDDOT_traffic_counts.gpkg",
    "NARN_rail_lines": "railroads/NARN_rail_lines.gpkg",
    "NBI_bridges": "roads/NBI_bridges.gpkg",
    "NID_dams": "hydro/NID_dams.gpkg",
    "OSM_lifelines": "lifelines/OSM_lifelines.gpkg",
    "lifeline_nd_basic_care": "lifelines/lifeline_nd_basic_care.gpkg",
    "NHD_flowlines": "hydro/NHD_flowlines.gpkg",
}
EXTRACTS = ["NSI_structures", "ND_roads_NG911", "ND_county_roads", "HPMS_ND_2024",
            "NDDOT_traffic_counts", "NARN_rail_lines", "NBI_bridges", "NID_dams", "OSM_crossings",
            "OSM_lifelines", "lifeline_nd_basic_care"]
CULVERT_SOURCES = ["/workspace/culvert_sources/research/nddot_centerline_pipes.gpkg",
                   "/workspace/culvert_sources/nddot_bridge_inventory.gpkg",
                   "/workspace/culvert_sources/ntad_nbi.gpkg"]
DEFAULT_DEM = "/home/box/DamSafety/DEM/NickReview_runner_1m/NickReview_runner_1m.vrt"  # 3DEP 1 m averaged to 5 m
SCREEN_DAMS = "/home/box/DamSafety/Dams/NickReview/dams"
WATER_MASK = SUITE + "/hydro/NHD_waterbodies.gpkg"
DWR_DAMS = SUITE + "/dwr/DWR_dams.gpkg"
SCREEN_MAXSTOR = "/home/box/DamSafety/Dams/NickReview/screen_maxstor"


def screen_7b_files(dam):
    """The NickReview max-storage screening files the Screening 7b record reads (box paths):
    (needed, optional). The stage-storage curve is optional (no curve: no surface area sent)."""
    return ([SCREEN_MAXSTOR + "/8_hazard.gpkg", SCREEN_MAXSTOR + "/4_breach_points.gpkg"],
            [SCREEN_MAXSTOR + "/3_stage_storage/curve_{}.csv".format(dam)])


# Section 2 of the NickReview rerun plan (same for every dam)
RUNDAM_FIXED = {
    "METHOD": 0, "SCENARIO": 0, "BASELINE": True, "STOP_RULE": False,
    "S_KIND": "kind", "S_WIDTH": "span_ft", "S_HEIGHT": "rise_ft", "S_BARRELS": "barrels",
    "S_SHAPE": "shape", "S_INV_UP": "invert_up", "S_INV_DN": "invert_dn", "S_NAME": "name",
    "CULVERT_MATCH_M": 75, "CULVERT_BURN_MATCHED": False, "DEM_Z": 3, "TIER": 1,
    "FORCE_ANUGA": True, "CELL": 30, "MANNING": 0.06, "ARRIVAL_FT": 0.1, "EDGE_TOL_FT": 1,
    "LENGTH_MI": 20, "HALF_WIDTH_MI": 6, "STEP_MI": 3, "MAX_EXTENT_MI": 20, "MAX_RUNS": 1,
    "DURATION_HR": 0, "DURATION_MAX_HR": 72, "WORK_CRS": "EPSG:5070", "ANUGA_BUFFER": 2000,
    "KEEP_FILES": True, "PRODUCTS": True,
}
CULVERT_ARGS = ["STREAM_REVERSED=false", "SEARCH_FT=492.1", "ROAD_WIDTH_FT=32.8",
                "RAIL_WIDTH_FT=19.7", "MARGIN_FT=32.8", "USE_NBI_WIDTH=true",
                "ALLOW_NO_EMBANKMENT=false", "PASS_ELEVATIONS=true"]
HAZBATCH_ARGS = ["DOWNLOADS=true", "REPORT=true", "EAP=false", "WORKERS=1", "TIMEOUT_MIN=90"]
HAZBATCH_CSV = "Batch_Hazard_Classification.csv"   # ndhazard:batch's table (one row per dam)
STAGES = ["prepare", "structures", "precheck", "record", "rundam", "hazclass", "hazbatch"]
KIT_SKIPS = ("prepare", "structures")               # the box preparation replaces these
# the earlier stages whose results each stage reads: --redo runs a stage again when one of
# them runs again. precheck is a report no later stage reads; record reads only --data.
READS = {"prepare": (), "structures": ("prepare",), "precheck": ("structures",), "record": (),
         "rundam": ("prepare", "structures", "record"), "hazclass": ("prepare", "rundam"),
         "hazbatch": ("rundam", "hazclass")}
NONFATAL = ("precheck",)                            # a report: when it fails, the rest still runs
SESSIONS = "redo_sessions"                          # under --out: one file per open --redo command
KEEP_CLOSED = 20                                    # closed sessions kept in redo_sessions/closed/
STREAMS_MARK = "streams_source.json"                # which streams file prepare made, and why
SOURCES_MARK = "prepare_sources.json"               # the files prepare's extracts were cut from
# the data lists (--list-data): header and the "source" column
LIST_HEADER = "bytes\tcopy_to (under ~/dwr-dam-suite/NickReview_data/)\tsource\n"
LIDAR_SOURCE = "live download possible (USGS 3DEP 1 m, The National Map) or copy"


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- output that survives a closed terminal / tee (Ctrl-C under run_nickreview.sh) -----------
_STDOUT = {"ok": True}


def out(msg=""):
    """print() that never raises: once stdout is gone (tee killed by Ctrl-C) the run goes on
    quietly, still writing its own logs and the manifest."""
    if not _STDOUT["ok"]:
        return
    try:
        print(msg, flush=True)
    except (BrokenPipeError, OSError):
        _STDOUT["ok"] = False
        try:                     # so Python's own flush at exit cannot raise either
            fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(fd, sys.stdout.fileno())
        except OSError:
            pass


def write_json(path, obj, **kw):
    """Write JSON through a temporary file and a rename: a cut run never leaves half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, **kw)
    os.replace(tmp, path)


def _remove(path):
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


_HASHES = {}   # path -> (size, mtime_ns, sha256): every file is read once per run


def sha256(path):
    if not os.path.isfile(path):
        return None
    st = os.stat(path)
    hit = _HASHES.get(path)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
        return hit[2]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    _HASHES[path] = (st.st_size, st.st_mtime_ns, h.hexdigest())
    return h.hexdigest()


def files_of(v):
    """The existing files named by an input value ('a;;b' lists, 'path|layername=x')."""
    if not isinstance(v, str):
        return []
    found = []
    for part in v.split(";;"):
        f = part.split("|")[0].strip()
        if f.startswith("/") and os.path.isfile(f):
            found.append(f)
    return found


class Ctx(object):
    def __init__(self, a):
        self.a = a
        self.data = os.path.abspath(os.path.expanduser(a.data))
        self.out = os.path.abspath(os.path.expanduser(a.out))
        self.dry = a.dry_run
        self.problems = []
        self.warnings = []
        self.used = set()
        self.session = None      # the RedoSession of this --redo command
        self.pending_redo = {}   # dam -> lines about an unfinished --redo that has not redone it

    # -- paths ---------------------------------------------------------------------------
    def d(self, box_path):
        """A box path (constants above) -> where it is under --data."""
        return self.map_path(box_path)

    def map_path(self, p):
        """A box path in a kit file -> the same file under --data (falls back to the box path
        itself when that exists, so the script also runs on the box)."""
        if not isinstance(p, str) or not p.startswith("/"):
            return p
        for old, sub in self.a.maps + BOX_PREFIXES:
            if p == old or p.startswith(old + "/"):
                rest = p[len(old):].lstrip("/")
                cand = os.path.join(self.data, sub, rest) if not sub.startswith("/") else \
                    os.path.join(sub, rest)
                if os.path.exists(cand.split("|")[0]):
                    return cand
                if os.path.exists(p.split("|")[0]):
                    return p
                return cand
        return p

    def map_value(self, v):
        if isinstance(v, str) and ";;" in v:
            return ";;".join(self.map_path(x) for x in v.split(";;"))
        return self.map_path(v)

    def rel_of(self, p):
        """A box path -> its place under --data (inverse of map_path)."""
        for old, sub in BOX_PREFIXES:
            if p == old or p.startswith(old + "/"):
                return os.path.join(sub, p[len(old):].lstrip("/"))
        return "OTHER" + p

    def need(self, path, what):
        if not os.path.exists(path.split("|")[0]):
            self.problems.append("{}: missing {}".format(what, path))
            return False
        return True


# -- DEM VRTs ----------------------------------------------------------------------------------
def _sources(root):
    """(source element, its SourceFilename / SourceDataset element) of a parsed VRT."""
    for el in root.iter():
        for tag in ("SourceFilename", "SourceDataset"):
            child = el.find(tag)
            if child is not None and (child.text or "").strip():
                yield el, child


def _vrt_geo(root):
    """(geotransform, SRS text) of a parsed VRT; None where it has none."""
    gt, srs = None, None
    g = root.find("GeoTransform")
    if g is not None and g.text:
        try:
            gt = [float(v) for v in g.text.split(",")]
        except ValueError:
            gt = None
    s = root.find("SRS")
    if s is not None and (s.text or "").strip():
        srs = s.text.strip()
    return gt, srs


def _footprint(src_el, gt):
    """(x0, y0, x1, y1) of one VRT source in the VRT's CRS (its DstRect), or None."""
    dr = src_el.find("DstRect")
    if dr is None or gt is None or len(gt) != 6 or gt[2] or gt[4]:
        return None
    try:
        xo, yo, xs, ys = (float(dr.get(k)) for k in ("xOff", "yOff", "xSize", "ySize"))
    except (TypeError, ValueError):
        return None
    xa, xb = gt[0] + gt[1] * xo, gt[0] + gt[1] * (xo + xs)
    ya, yb = gt[3] + gt[5] * yo, gt[3] + gt[5] * (yo + ys)
    return (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb))


def _rect_hits(srs_text, rect, box):
    """Does rect (x0, y0, x1, y1 in srs_text) touch box (EPSG:5070)?"""
    from osgeo import osr
    src = osr.SpatialReference()
    src.SetFromUserInput(srs_text)
    dst = osr.SpatialReference(); dst.ImportFromEPSG(5070)
    for s_ in (src, dst):
        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    for i in range(5):
        for j in range(5):
            x, y, _ = ct.TransformPoint(rect[0] + (rect[2] - rect[0]) * i / 4,
                                        rect[1] + (rect[3] - rect[1]) * j / 4)
            xs.append(x); ys.append(y)
    return not (max(xs) < box[0] or min(xs) > box[2] or max(ys) < box[1] or min(ys) > box[3])


def tile_hits(path, box):
    """Does a raster's footprint touch box (EPSG:5070)? (only the tiles a dam reads are listed)"""
    from osgeo import gdal
    ds = gdal.Open(path)
    gt, w, h = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
    xa, xb, ya, yb = gt[0], gt[0] + gt[1] * w, gt[3], gt[3] + gt[5] * h
    return _rect_hits(ds.GetProjection(), (min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)), box)


def touches(path, rect, srs, box):
    """Does a DEM source touch the dam's box? The file's own footprint when it is here (the
    test --list-data used on the box), else its rectangle in the VRT; True when neither is
    known. relocate_vrt, --list-data and the manifest all use this one test."""
    if os.path.isfile(path) and not path.lower().endswith(".vrt"):
        try:
            return tile_hits(path, box)
        except Exception:
            pass
    if rect is not None and srs:
        try:
            return _rect_hits(srs, rect, box)
        except Exception:
            pass
    return True


def _source_path(ctx, vrt, el):
    t = el.text.strip()
    p = os.path.normpath(os.path.join(os.path.dirname(vrt), t)) if el.get("relativeToVRT") == "1" else t
    return ctx.map_path(p)


def relocate_vrt(ctx, src_vrt, dst_dir, box=None, seen=None):
    """Copy a VRT (and the VRTs it references) into dst_dir with every source path remapped to
    --data. Rasters are referenced in place (absolute), never copied. A missing tile is a
    problem (rundam is not started) when it touches box, the dam's area (the same box and test
    as --list-data); one outside it is never read and only counted. Returns the new path."""
    seen = {} if seen is None else seen
    src_vrt = os.path.abspath(src_vrt)
    if src_vrt in seen:
        return seen[src_vrt]
    stem, ext = os.path.splitext(os.path.basename(src_vrt))
    base, n = stem + ext, 1
    while base in {os.path.basename(v) for v in seen.values()}:   # same name, other folder
        base, n = "{}_{}{}".format(stem, n, ext), n + 1
    dst = os.path.join(dst_dir, base)
    seen[src_vrt] = dst
    tree = ET.parse(src_vrt)
    gt, srs = _vrt_geo(tree.getroot())
    for src_el, el in _sources(tree.getroot()):
        ref = _source_path(ctx, src_vrt, el)
        if ref.lower().endswith(".vrt") and os.path.isfile(ref):
            ref = relocate_vrt(ctx, ref, dst_dir, box, seen)
        elif not os.path.exists(ref):
            if box is None or touches(ref, _footprint(src_el, gt), srs, box):
                ctx.problems.append("DEM tile missing (it covers the dam's area): {}".format(ref))
            else:
                ctx.warnings.append("DEM tile outside the dam's area not here (never read): "
                                    "{}".format(ref))
        el.text = ref
        el.set("relativeToVRT", "0")
    if not ctx.dry:
        os.makedirs(dst_dir, exist_ok=True)
        tmp = dst + ".tmp"
        tree.write(tmp, encoding="UTF-8")
        os.replace(tmp, dst)
    return dst


def dem_files(ctx, vrt, box, acc=None):
    """The files a dam's DEM VRT chain reads inside box: every VRT of the chain (in order) and
    the tiles that touch box; existing files only (--list-data and the manifest's sha256)."""
    acc = [] if acc is None else acc
    root = ET.parse(vrt).getroot()
    gt, srs = _vrt_geo(root)
    for src_el, el in _sources(root):
        p = _source_path(ctx, vrt, el)
        if p in acc or not os.path.isfile(p):
            continue
        if p.lower().endswith(".vrt"):
            acc.append(p)
            dem_files(ctx, p, box, acc)
        elif touches(p, _footprint(src_el, gt), srs, box):
            acc.append(p)
    return acc


# -- the dam's own record ----------------------------------------------------------------------
try:
    from osgeo import gdal as _g, ogr as _o, osr as _s
    _g.UseExceptions(); _o.UseExceptions(); _s.UseExceptions()
except ImportError:  # run with the damsafety-qgis python
    pass


def screening_record(ctx, dam):
    p = ctx.d(os.path.join(SCREEN_DAMS, dam, "dam.json"))
    if not ctx.need(p, dam + " screening record"):
        return None
    return json.load(open(p))["fields"]


def dwr_max_pool_area(ctx, num):
    from osgeo import ogr
    ds = ogr.Open(ctx.d(DWR_DAMS))
    if ds is None:
        return None
    lyr = ds.GetLayer(0)
    lyr.SetAttributeFilter("structure_id = {}".format(int(num)))
    for f in lyr:
        v = f.GetField("max_pool_area")
        if v not in (None, "") and float(v) > 0:
            return float(v)
    return None


def kit_inputs(ctx, dam):
    kit = ctx.d(KITS[dam])
    rd = os.path.join(kit, "rundam.json")
    hz = os.path.join(kit, "hazclass.json")
    if not (ctx.need(rd, dam + " kit rundam.json") and ctx.need(hz, dam + " kit hazclass.json")):
        return None, None
    r = json.load(open(rd))["inputs"]
    h = json.load(open(hz))["inputs"]
    r = {k: ctx.map_value(v) for k, v in r.items()}
    h = {k: ctx.map_value(v) for k, v in h.items()}
    return r, h


def generated_inputs(ctx, dam, w):
    rec = screening_record(ctx, dam)
    if rec is None:
        return None, None
    num = dam.split("-")[1]
    area = dwr_max_pool_area(ctx, num)
    area_src = "DWR max_pool_area"
    if area is None:
        area, area_src = rec.get("surface_area_ac"), "screening surface_area_ac"
    r = {"DAM_ID": dam, "DAM_NAME": rec.get("name"), "NID_ID": rec.get("nid_id"),
         "DAM_POINT": "{:.3f},{:.3f} [EPSG:5070]".format(float(rec["x"]), float(rec["y"])),
         "HEIGHT_FT": rec.get("height_ft"), "STORAGE_ACFT": rec.get("storage_acft"),
         "AREA_AC": round(float(area), 3) if area else None,
         "TOP_FT": round(float(rec["top_of_dam_ft"]), 3) if rec.get("top_of_dam_ft") else None}
    r = {k: v for k, v in r.items() if v not in (None, "")}
    r.update(RUNDAM_FIXED)
    pk = lambda n: os.path.join(w, "pack_{}_{}.gpkg".format(n, num))
    r.update({
        "RECEPTOR_POINTS": pk("NSI_structures"), "RECEPTOR_ROADS": pk("ND_roads_NG911"),
        "RECEPTOR_RAIL": pk("NARN_rail_lines"),
        "STRUCTURES": os.path.join(w, "structures_{}.gpkg".format(num)) + "|layername=runner_culverts",
        "CULVERT_INVENTORY": ";;".join(ctx.d(x) for x in CULVERT_SOURCES),
        "DEM": ctx.d(DEFAULT_DEM), "WATER_MASK": ctx.d(WATER_MASK)})
    h = {"LETHALITY": 0, "STRUCTURES": pk("NSI_structures"), "S_ID": "fd_id", "S_OCC": "occtype",
         "ROADS": pk("ND_roads_NG911"), "ROADS_MORE": pk("ND_county_roads"),
         "AADT_LAYERS": pk("HPMS_ND_2024") + ";;" + pk("NDDOT_traffic_counts"),
         "RAIL": pk("NARN_rail_lines"), "T_PASSENGER": "PASSNGR", "PASSENGER_CODES": "A,B,C",
         "LIFELINES": pk("OSM_lifelines"), "LIFELINES_MORE": pk("lifeline_nd_basic_care"),
         "DOWNSTREAM_DAMS": os.path.join(w, "pack_NID_dams_{}_downstream.gpkg".format(num)),
         "D_ID": "NIDID", "D_CLASS": "HAZARD_POTENTIAL", "WET_FT": 0.1, "UNKNOWN_AADT": 1,
         "UNKNOWN_TRAINS": 1}
    notes = ["AREA_AC from " + area_src, "TOP_FT, height, storage and point from " +
             os.path.join(SCREEN_DAMS, dam, "dam.json")]
    return (r, notes), h


def area_box(r):
    x, y = [float(v) for v in r["DAM_POINT"].split("[")[0].split(",")]
    half = (float(r.get("LENGTH_MI", 20)) + float(r.get("HALF_WIDTH_MI", 6))) * MILE + 3000.0
    return (x - half, y - half, x + half, y + half)


def traced_box(ctx, r, log):
    """The Runner's own downstream trace (as the box preview did): steepest descent on the
    filled DEM for LENGTH_MI from the dam, buffered by HALF_WIDTH_MI, plus 3 km for extracts."""
    import importlib.util
    import numpy as np
    from osgeo import gdal
    core = os.path.join(ctx.a.plugins, "DamSafety_Runner", "core")

    def load(n):
        sp = importlib.util.spec_from_file_location("nr_" + n, os.path.join(core, n + ".py"))
        m = importlib.util.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m
    G, terrain = load("grid"), load("terrain")
    x, y = [float(v) for v in r["DAM_POINT"].split("[")[0].split(",")]
    cs = float(r.get("CELL", 30))
    L = float(r.get("LENGTH_MI", 20)) * MILE
    W = float(r.get("HALF_WIDTH_MI", 6)) * MILE
    half = L + W + 2 * cs
    b = (x - half, y - half, x + half, y + half)
    n = int(round(2 * half / cs))
    o = gdal.Warp("", r["DEM"], options=gdal.WarpOptions(
        format="MEM", outputBounds=(b[0], b[3] - n * cs, b[0] + n * cs, b[3]), width=n, height=n,
        resampleAlg="average", dstNodata=-9999, outputType=gdal.GDT_Float32, dstSRS="EPSG:5070"))
    a = o.GetRasterBand(1).ReadAsArray().astype(float)
    a[a == -9999] = np.nan
    res = terrain.trace_downstream(G.Grid(a, b[0], b[3], cs), (x, y), L)
    path = res[0] if isinstance(res, tuple) else res
    pb = terrain.path_bounds(list(path) + [(x, y)], W, snap=cs)
    log("  traced {} points; domain {:.1f} x {:.1f} mi".format(
        len(path), (pb[2] - pb[0]) / MILE, (pb[3] - pb[1]) / MILE))
    return (pb[0] - 3000.0, pb[1] - 3000.0, pb[2] + 3000.0, pb[3] + 3000.0)


# -- extracts ----------------------------------------------------------------------------------
def lonlat_box(b):
    from osgeo import osr
    s = osr.SpatialReference(); s.ImportFromEPSG(5070)
    g = osr.SpatialReference(); g.ImportFromEPSG(4326)
    for r in (s, g):
        r.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    t = osr.CoordinateTransformation(s, g)
    pts = [t.TransformPoint(x, y)[:2] for x in (b[0], b[2]) for y in (b[1], b[3])]
    return (min(p[0] for p in pts) - .02, min(p[1] for p in pts) - .02,
            max(p[0] for p in pts) + .02, max(p[1] for p in pts) + .02)


def vector_to(dst, src, **kw):
    """gdal.VectorTranslate into a temporary GeoPackage, then renamed onto dst: a cut run never
    leaves a half-written dst, and every prepare run replaces it."""
    from osgeo import gdal
    gdal.UseExceptions()
    tmp = (dst[:-5] if dst.endswith(".gpkg") else dst) + ".part.gpkg"
    _remove(tmp)
    ds = gdal.VectorTranslate(tmp, src, format="GPKG", **kw)
    if ds is None:
        raise RuntimeError("could not write {}".format(tmp))
    ds = None  # noqa: F841 - closes the GeoPackage before the rename
    _remove(dst)
    os.replace(tmp, dst)


def feature_count(path):
    from osgeo import ogr
    ds = ogr.Open(path)
    if ds is None:
        return 0
    return sum(ds.GetLayer(i).GetFeatureCount() for i in range(ds.GetLayerCount()))


def layer_names(path):
    from osgeo import ogr
    try:
        ds = ogr.Open(path)
    except Exception:
        return []
    return [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())] if ds is not None else []


def write_features(feats, dst, name):
    tmp = dst + ".geojson"
    with open(tmp, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    try:
        vector_to(dst, tmp, layerName=name)
    finally:
        os.remove(tmp)


def extract(ctx, name, bbox, dst, pack_reader, log, read=None):
    """dst = the layer clipped to bbox (offline pack, else suite pack). Returns the feature
    count, or None when there is no source (then an older dst is removed, never reused). The
    file it was cut from is added to the list read (for the manifest's sha256)."""
    read = [] if read is None else read
    if pack_reader is not None:
        try:
            feats = pack_reader.query(name, lonlat_box(bbox))
            write_features(feats, dst, name)
            read.append(ctx.a.pack)
            log("  {} {} features (offline pack)".format(name, len(feats)))
            return len(feats)
        except Exception as e:  # not in the pack: fall back to the suite pack
            log("  {}: not from the offline pack ({}); trying the suite pack".format(name, e))
    rel = SUITE_LAYERS.get(name)
    if rel is None or not os.path.isfile(ctx.d(os.path.join(SUITE, rel))):
        _remove(dst)
        log("  {}: no source (left empty)".format(name))
        return None
    vector_to(dst, ctx.d(os.path.join(SUITE, rel)), layerName=name,
              spatFilter=list(bbox), spatSRS="EPSG:5070")
    read.append(ctx.d(os.path.join(SUITE, rel)))
    n = feature_count(dst)
    log("  {} {} features (suite pack, clipped)".format(name, n))
    return n


def downstream_nid(src, dst, nid):
    where = "NIDID <> '{}'".format(nid.replace("'", "")) if nid else None
    vector_to(dst, src, layerName="NID_dams", where=where)


def fetch_3dhp(ctx, bbox, dst, log):
    """3DHP flowlines (USGS REST) for the area, with the NHD field names added, as the box did.
    Raises on an ArcGIS error answer (HTTP 200 with {"error": ...}) and when the area has no
    flowlines, so the caller falls back to NHD instead of writing an empty streams layer."""
    sys.path.insert(0, os.path.join(ctx.a.plugins, "DamSafety_Runner"))
    from damsuite import hydro3dhp as h3
    ll = lonlat_box(bbox)
    feats, off = [], 0
    while True:
        q = {"where": "1=1", "geometry": ",".join(str(v) for v in ll),
             "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": 4326,
             "spatialRel": "esriSpatialRelIntersects", "outFields": "*", "f": "geojson",
             "resultOffset": off, "resultRecordCount": 1000}
        url = h3.FLOWLINE_URL + "/query?" + urllib.parse.urlencode(q)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=300) as r:
                    page = json.load(r)
                if not isinstance(page, dict):
                    raise RuntimeError("3DHP answered {!r}".format(page)[:300])
                if "error" in page:      # ArcGIS: HTTP 200 with an error body
                    raise RuntimeError("3DHP answered an error: {}".format(
                        json.dumps(page["error"])[:300]))
                if not isinstance(page.get("features"), list):
                    raise RuntimeError("3DHP answer has no feature list")
                break
            except Exception as e:
                if attempt == 3:
                    raise
                log("  3DHP retry ({})".format(e)); time.sleep(10 * (attempt + 1))
        got = page["features"]
        feats += got
        if len(got) < 1000 and not page.get("exceededTransferLimit"):
            break
        off += len(got)
    if not feats:
        raise RuntimeError("3DHP returned no flowlines for the area")
    feats = h3.normalise_flowlines(feats)
    write_features(feats, dst, "streams_3dhp")
    log("  3DHP flowlines {}".format(len(feats)))
    return len(feats)


def read_batch_row(hb, dam, since):
    """(Status, Message) of dam in the ndhazard:batch table written in hb since `since`."""
    cands = [p for p in glob.glob(os.path.join(hb, HAZBATCH_CSV[:-4] + "*.csv"))
             if os.path.getmtime(p) >= since - 2]
    if not cands:
        return "missing", "no {} written in {}".format(HAZBATCH_CSV, hb)
    path = max(cands, key=os.path.getmtime)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(line for line in f if not line.startswith("#")):
            if dam in (row.get("Dam"), os.path.basename(os.path.normpath(row.get("Folder") or ""))):
                return (row.get("Status") or "").strip() or "?", (row.get("Message") or "").strip()
    return "missing", "{} has no row for {}".format(os.path.basename(path), dam)


# -- running -----------------------------------------------------------------------------------
def stages_to_run(asked, kit, redo):
    """The stages one command runs for a dam, in run order: the stages asked for (--stages) that
    apply to it (prepare and structures do not apply to a dam with a box preparation); with
    --redo, also every later stage that applies and reads the results of one that runs again
    (READS; e.g. precheck: only precheck, record: record, rundam, hazclass, hazbatch). [] when
    none applies."""
    applies = [s for s in STAGES if not (kit and s in KIT_SKIPS)]
    run = {s for s in applies if s in asked}
    if redo:
        for s in applies:                       # STAGES order: a stage reads only earlier ones
            if any(r in run for r in READS[s]):
                run.add(s)
    return [s for s in applies if s in run]


def is_kit(dam, no_kits):
    return dam in KITS and not no_kits


def ok_mode(text):
    """The mode (kit / prepare) written in a status/<stage>.ok, or None (an older .ok)."""
    m = re.search(r"\bmode=(kit|prepare)\b", text or "")
    return m.group(1) if m else None


def read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def ok_stamp(path):
    """(when, exact) a status/<stage>.ok or .failed was written: its t= (seconds since the
    epoch), else its file time (an .ok written before t= was); (None, False) when not there."""
    m = re.search(r"\bt=(\d+(?:\.\d+)?)", read_text(path))
    if m:
        return float(m.group(1)), True
    try:
        return os.path.getmtime(path), False
    except OSError:
        return None, False


def written_since(path, since):
    """Was this status file written at or after `since` (seconds since the epoch)? A file
    time (an older .ok) gets one second for file systems that keep whole seconds."""
    t, exact = ok_stamp(path)
    return t is not None and t >= since - (0 if exact else 1)


def structures_mode(value, dam):
    """kit / prepare from a STRUCTURES input (None when empty): prepare mode makes
    work/<dam>/structures_<n>.gpkg; a box preparation names its own file."""
    if not isinstance(value, str) or not value.strip():
        return None
    p = os.path.normpath(value.split("|")[0].strip())
    made = os.path.join("work", dam, "structures_{}.gpkg".format(dam.split("-")[1]))
    return "prepare" if p.endswith(os.sep + made) else "kit"


def ok_modes(w, dam):
    """{stage: mode} of the finished stages of dam (work folder w). An .ok from before the mode
    was written: prepare and structures, and every stage of a dam without a box preparation,
    ran in prepare mode. For another stage of a kit dam, work/<dam>/rundam.json tells it (its
    STRUCTURES: the box preparation's file, or work/<dam>/structures_<n>.gpkg with --no-kits)
    when the run that wrote the .ok wrote it (each run writes rundam.json just after precheck
    and before record, rundam, hazclass and hazbatch). When a later run wrote rundam.json
    again, a prepare.ok at least as old as the stage means prepare mode, else kit mode."""
    status = os.path.join(w, "status")
    rj = os.path.join(w, "rundam.json")
    try:
        with open(rj) as f:
            jmode = structures_mode(json.load(f)["inputs"].get("STRUCTURES"), dam)
        jt = os.path.getmtime(rj)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        jmode, jt = None, None
    prep = os.path.join(status, "prepare.ok")
    modes = {}
    for s in STAGES:
        p = os.path.join(status, s + ".ok")
        if not os.path.isfile(p):
            continue
        m = ok_mode(read_text(p))
        if m is None:
            t = os.path.getmtime(p)
            if s in KIT_SKIPS or dam not in KITS:
                m = "prepare"
            elif jmode and (jt <= t or (s == "precheck" and jt - t <= 60)):
                m = jmode
            elif os.path.isfile(prep) and os.path.getmtime(prep) <= t:
                m = "prepare"
            else:
                m = "kit"
        modes[s] = m
    return modes


def stage_states(w, dam, mode, not_done=()):
    """{stage: None when finished, else why not} for the stages that apply to dam in mode (kit
    mode: not prepare and structures). Finished: its status/<stage>.ok is there, ran in mode,
    and is newer than the .ok of every stage it reads (READS) that applies, each of them
    finished too. A rundam run without the Screening 7b record (--allow-no-record) stays
    finished until a record is written. not_done: stages taken as not finished (--redo
    --dry-run shows them as run again)."""
    status = os.path.join(w, "status")
    modes = ok_modes(w, dam)
    res, when = {}, {}
    for s in STAGES:
        if mode == "kit" and s in KIT_SKIPS:
            continue
        p = os.path.join(status, s + ".ok")
        if s in not_done:
            res[s] = "it runs again"
            continue
        if s not in modes:
            res[s] = "status/{}.ok is not there".format(s)
            continue
        if modes[s] != mode:
            res[s] = "status/{}.ok is from a {} mode run".format(s, modes[s])
            continue
        when[s] = ok_stamp(p)[0]
        res[s] = None
        no_record = s == "rundam" and "no-record" in read_text(p)
        for r in READS[s]:
            if r not in res or (no_record and r == "record" and r not in modes):
                continue
            if res[r] is not None:
                res[s] = "it reads {}, which is not finished".format(r)
                break
            if when[r] > when[s]:
                res[s] = ("it ran without the Screening 7b record (--allow-no-record) and the "
                          "record is there now" if no_record and r == "record" else
                          "{} ran again after it (status/{}.ok is newer)".format(r, r))
                break
    return res


class RedoSession(object):
    """One --redo command (<out>/redo_sessions/<id>.json): its dams and stages, and for each
    dam when this command began its redo (its stages invalidated) and finished it. The same
    --redo command run again continues the session: a dam it began is not invalidated again
    (its stages finished since then are skipped) and the dams it has not reached are redone.
    A dam is finished when every stage the session redoes for it is finished (stage_states:
    in its mode and newer than what it reads) and was written after the session began that
    dam (any run may write it, e.g. a plain resume); a precheck (a report) that ran and
    failed since then counts as finished, with that problem. The session is closed when all
    its dams are finished: its file moves to redo_sessions/closed/ (the newest KEEP_CLOSED
    are kept), and the same command then starts a new redo."""

    def __init__(self, path, rec):
        self.path, self.rec = path, rec

    @property
    def sid(self):
        return self.rec.get("id", "?")

    @staticmethod
    def key(a, dams):
        """What makes two --redo commands the same command."""
        return {"dams": list(dams), "stages": [s for s in STAGES if s in a.stages],
                "no_kits": bool(a.no_kits), "offline": bool(a.offline)}

    @classmethod
    def open_ones(cls, out_dir, tidy=True):
        """The unfinished sessions in out_dir, oldest first (only the files directly in
        redo_sessions/; a damaged file is skipped). tidy: a closed one still there (closed by
        an older version of this script) is moved to closed/."""
        found = []
        for p in sorted(glob.glob(os.path.join(out_dir, SESSIONS, "*.json"))):
            try:
                with open(p) as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and isinstance(rec.get("key"), dict):
                    s = cls(p, rec)
                    if not rec.get("finished"):
                        found.append(s)
                    elif tidy:
                        s.close()
            except (OSError, ValueError):
                out("WARNING: {} could not be read; ignored".format(p))
        return sorted(found, key=lambda s: s.rec.get("started_epoch") or 0)

    def close(self):
        """Move the file of this finished session to redo_sessions/closed/; keep the newest
        KEEP_CLOSED there (the file names start with the start time)."""
        if os.path.basename(os.path.dirname(self.path)) == "closed":
            return
        cdir = os.path.join(os.path.dirname(self.path), "closed")
        os.makedirs(cdir, exist_ok=True)
        dst = os.path.join(cdir, os.path.basename(self.path))
        os.replace(self.path, dst)
        self.path = dst
        for old in sorted(glob.glob(os.path.join(cdir, "*.json")))[:-KEEP_CLOSED]:
            try:
                os.remove(old)
            except OSError:
                pass

    @classmethod
    def new(cls, out_dir, key, command):
        t = now()
        sid = "{}_{:04x}".format(t.replace(":", "").replace("-", ""), random.getrandbits(16))
        rec = {"id": sid, "started": t, "started_epoch": time.time(), "command": command,
               "key": key, "dams": {}, "finished": None}
        return cls(os.path.join(out_dir, SESSIONS, sid + ".json"), rec)

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        write_json(self.path, self.rec, indent=1)

    def dam(self, dam):
        return self.rec.setdefault("dams", {}).setdefault(dam, {})

    def began(self, dam):
        return (self.rec.get("dams") or {}).get(dam, {}).get("began")

    def begin(self, dam, stages):
        d = self.dam(dam)
        d.update(began=now(), began_epoch=time.time(), stages=list(stages))
        d.pop("finished", None)
        self.save()

    def stages_of(self, dam):
        d = (self.rec.get("dams") or {}).get(dam) or {}
        if "stages" in d:
            return d["stages"]
        k = self.rec["key"]
        return stages_to_run(k["stages"], is_kit(dam, k["no_kits"]), True)

    def progress(self, out_dir, dam):
        """(finished, problems) of dam in this session: finished when every stage the session
        redoes for it is finished (stage_states, in its mode) and was written since the session
        began the dam (for a dam it has not begun: since the session started). A NONFATAL
        stage (precheck, a report no stage reads) that ran and failed since then counts as
        finished, with that problem."""
        d = (self.rec.get("dams") or {}).get(dam) or {}
        since = d.get("began_epoch") or self.rec.get("started_epoch") or 0
        mode = "kit" if is_kit(dam, self.rec["key"]["no_kits"]) else "prepare"
        w = os.path.join(out_dir, "work", dam)
        st = stage_states(w, dam, mode)
        problems = []
        for s in self.stages_of(dam):
            if s in st and st[s] is None and \
                    written_since(os.path.join(w, "status", s + ".ok"), since):
                continue
            fail = os.path.join(w, "status", s + ".failed")
            if s in NONFATAL and ok_mode(read_text(fail)) == mode and written_since(fail, since):
                problems.append("{} failed: {}".format(s, re.sub(
                    r"\s*\b(mode|t)=\S+", "", read_text(fail)).strip()))
                continue
            return False, []
        return True, problems

    def refresh(self, out_dir, save=True):
        """Mark the dams that are redone as finished (with their problems), and the session
        when all are (its file then moves to closed/). Returns True when it is finished."""
        changed = False
        for dam in self.rec["key"]["dams"]:
            d = self.dam(dam)
            if not d.get("finished"):
                fin, problems = self.progress(out_dir, dam)
                if fin:
                    d["finished"] = now()
                    if problems:
                        d["problems"] = problems
                    changed = True
        if not self.rec.get("finished") and \
                all(self.dam(x).get("finished") for x in self.rec["key"]["dams"]):
            self.rec["finished"] = now()
            changed = True
        if changed and save:
            self.save()
            if self.rec.get("finished"):
                self.close()
        return bool(self.rec.get("finished"))

    def problems(self):
        """{dam: [problems]} of the dams finished with a problem (a failed precheck)."""
        return {d: v["problems"] for d, v in (self.rec.get("dams") or {}).items()
                if v.get("problems")}

    def pending(self):
        """(dams not begun, dams begun and not finished)."""
        ds = self.rec.get("dams") or {}
        todo = [d for d in self.rec["key"]["dams"] if not (ds.get(d) or {}).get("finished")]
        return ([d for d in todo if not (ds.get(d) or {}).get("began")],
                [d for d in todo if (ds.get(d) or {}).get("began")])

    def describe(self):
        """The lines a run prints about this unfinished session."""
        new, part = self.pending()
        k = self.rec["key"]
        lines = ["an earlier --redo run did not finish (session {}, started {}; --redo of {} "
                 "for {} dam(s)).".format(self.sid, self.rec.get("started"),
                                          ",".join(k["stages"]), len(k["dams"]))]
        if new:
            lines.append("not redone yet (they keep their results from before that --redo): "
                         + " ".join(new))
        if part:
            lines.append("redo begun, not finished (their remaining stages run in any run of "
                         "them): " + " ".join(part))
        lines.append("to finish that --redo, run its command again (the dams it already redid "
                     "are not redone): " + self.rec.get("command", "?"))
        lines.append("(to drop it instead, delete its file: rm {})".format(shlex.quote(self.path)))
        return lines


def run_command(a, ctx, redo=True, stages=None, dams=None, no_kits=None, offline=None):
    """The run_nickreview.sh command with this run's options (to continue a --redo session,
    and the NHD fallback's hint); stages, dams, --no-kits and --offline as given, else this
    run's."""
    here = os.path.dirname(os.path.abspath(__file__))
    work = os.path.abspath(os.path.expanduser(os.environ.get("DAMSAFETY_WORK")
                                              or "~/dwr-dam-suite"))
    c = ["bash", os.path.join(here, "run_nickreview.sh")] + (["--redo"] if redo else [])
    stages = [s for s in STAGES if s in (a.stages if stages is None else stages)]
    if stages != STAGES:
        c += ["--stages", ",".join(stages)]
    flags = {"no_kits": a.no_kits if no_kits is None else no_kits,
             "offline": a.offline if offline is None else offline,
             "allow_no_record": a.allow_no_record}
    c += ["--" + f.replace("_", "-") for f in ("no_kits", "offline", "allow_no_record") if flags[f]]
    if a.threads != 8:
        c += ["--threads", str(a.threads)]
    for d in (a.dam if dams is None else dams):
        c += ["--dam", d]
    if os.path.abspath(a.dams_file) != os.path.join(here, "nickreview_dams.txt"):
        c += ["--dams", os.path.abspath(a.dams_file)]
    if ctx.data != os.path.join(work, "NickReview_data"):
        c += ["--data", ctx.data]
    if ctx.out != os.path.join(work, "Dams", "NickReview_mac"):
        c += ["--out", ctx.out]
    if a.pack_given:
        c += ["--pack", a.pack_given]
    for m in a.map:
        c += ["--map", m]
    return " ".join(shlex.quote(x) for x in c)


class Runner(object):
    def __init__(self, ctx, dam):
        self.ctx, self.dam = ctx, dam
        self.num = dam.split("-")[1]
        self.w = os.path.join(ctx.out, "work", dam)
        self.dams_folder = os.path.join(ctx.out, "dams")
        self.df = os.path.join(self.dams_folder, dam)
        self.hb = os.path.join(ctx.out, "hazbatch", dam)
        self.status = os.path.join(self.w, "status")
        self.logf = None
        self.record = {"dam": dam, "started": now(), "stages": {}}
        self.box = None
        self.dem_src = None
        self.inputs = {}         # group -> [files] read by this dam's stages (for the sha256)
        self.redo_dry = set()    # --redo --dry-run: the stages shown as run again
        self.mode = "kit" if is_kit(dam, ctx.a.no_kits) else "prepare"

    def log(self, msg):
        line = "[{}] {} {}".format(now(), self.dam, msg)
        out(line)
        if self.logf:
            self.logf.write(line + "\n"); self.logf.flush()

    def note(self, text):
        self.record.setdefault("notes", []).append(text)

    def states(self):
        """{stage: None when finished, else why not} (stage_states; --redo --dry-run: the
        stages it would run again count as not finished)."""
        return stage_states(self.w, self.dam, self.mode, self.redo_dry)

    def done(self, stage):
        return self.states().get(stage, "") is None

    def due(self, stage, run):
        """Does stage run now: it is one of this command's stages (run) and is not finished,
        e.g. because a stage it reads has just run again (then the log says so)."""
        if stage not in run:
            return False
        why = self.states().get(stage, "")
        if why is None:
            return False
        if stage not in self.redo_dry and not self.ctx.a.list_data and \
                os.path.isfile(os.path.join(self.status, stage + ".ok")):
            self.log("{}: runs again: {}".format(stage, why))
        return True

    def needed(self, want, st):
        """(the stages to run, in order; the ones of them added): want plus every unfinished
        stage that an unfinished stage of it reads (READS), so that no stage runs on results
        that are missing or out of date (e.g. --stages hazclass after a stop in rundam)."""
        run, first = set(want), set()
        for s in reversed(STAGES):
            if s in run and st.get(s) is not None:
                for r in READS[s]:
                    if r in st and st[r] is not None and r not in run:
                        run.add(r)
                        first.add(r)
        return [s for s in STAGES if s in run], [s for s in STAGES if s in first]

    def mark(self, stage, info=""):
        """status/<stage>.ok: when (also as t=, seconds since the epoch, which the checks of what
        is finished compare), the mode the stage ran in, and a note (e.g. no-record)."""
        _remove(os.path.join(self.status, stage + ".failed"))
        with open(os.path.join(self.status, stage + ".ok"), "w") as f:
            f.write("{} mode={} t={:.6f}{}\n".format(now(), self.mode, time.time(),
                                                     " " + info if info else ""))

    def mark_failed(self, stage, rc, log):
        """status/<stage>.failed: a NONFATAL stage ran and failed (the --redo session counts it
        as finished, with this problem)."""
        with open(os.path.join(self.status, stage + ".failed"), "w") as f:
            f.write("{} mode={} t={:.6f} exit {}, see {}\n".format(now(), self.mode, time.time(),
                                                               rc, log))

    def mark_info(self, stage):
        return read_text(os.path.join(self.status, stage + ".ok"))

    def invalidate(self, stages):
        for s in stages:
            p = os.path.join(self.status, s + ".ok")
            if os.path.exists(p):
                os.rename(p, p + ".redo_" + now().replace(":", ""))
            _remove(os.path.join(self.status, s + ".failed"))

    def show(self, cmd, stdin=None, log=None):
        s = " ".join(shlex.quote(c) for c in cmd)
        if stdin:
            s += " < " + shlex.quote(stdin)
        if log:
            s += " > " + shlex.quote(log) + " 2>&1"
        return s

    def run(self, stage, cmd, stdin=None, logname=None):
        logp = os.path.join(self.w, (logname or stage) + ".log")
        self.log("{}: {}".format(stage, self.show(cmd, stdin, logp)))
        if self.ctx.dry:
            return 0
        t0 = time.time()
        with open(logp, "a") as lf:
            lf.write("# {} {}\n".format(now(), self.show(cmd, stdin))); lf.flush()
            fin = open(stdin) if stdin else subprocess.DEVNULL
            try:
                rc = subprocess.call(cmd, stdin=fin, stdout=lf, stderr=subprocess.STDOUT,
                                     cwd=self.w)
            finally:
                if stdin:
                    fin.close()
        self.record["stages"][stage] = {"rc": rc, "minutes": round((time.time() - t0) / 60, 1),
                                        "log": logp}
        self.log("{}: exit {} after {:.1f} min".format(stage, rc, (time.time() - t0) / 60))
        return rc

    def qp(self):
        return [self.ctx.a.qgis_process or "qgis_process"]   # empty only with --list-data

    # -- streams: 3DHP, or NHD (--offline / fallback), recorded in work/<dam>/streams_source.json
    def streams_file(self, source):
        return os.path.join(self.w, "streams_{}_{}.gpkg".format(source, self.num))

    def streams_info(self):
        try:
            with open(os.path.join(self.w, STREAMS_MARK)) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
        old = self.streams_file("3dhp")    # made before the marker existed: the layer tells
        if os.path.isfile(old):
            nhd = "NHD_flowlines" in layer_names(old)
            return {"source": "nhd" if nhd else "3dhp", "file": old,
                    "why": "an earlier run of this script" + (" (3DHP fallback)" if nhd else "")}
        return None

    def refetch_hint(self):
        """How to get the 3DHP streams for this dam (the NHD fallback's log lines): the command
        in this run's mode (--no-kits for a kit dam prepared here) and folders, without
        --offline."""
        later = ", ".join(stages_to_run(["prepare"], False, True)[1:])
        cmd = run_command(self.ctx.a, self.ctx, stages=["prepare"], dams=[self.dam],
                          no_kits=self.ctx.a.no_kits and self.dam in KITS, offline=False)
        return "'{}' fetches 3DHP again{}; every later stage of this dam ({}) then runs " \
            "again".format(cmd, " (with --offline it uses NHD again)" if self.ctx.a.offline
                           else "", later)

    def report_streams(self):
        """Every run says which streams the structures were built on (and the manifest too)."""
        info = self.streams_info()
        if not info:
            return
        if info.get("source") == "nhd":
            self.log("  streams: NHD flowlines ({}{}), NOT the 3DHP streams the box runs used; "
                     "{}".format(info.get("why", ""), ", " + info["when"] if info.get("when")
                                 else "", self.refetch_hint()))
            self.note("streams: NHD flowlines ({}) - differs from the box runs (3DHP)".format(
                info.get("why", "")))
        else:
            self.note("streams: 3DHP ({})".format(info.get("when", "")))

    def prepare(self, ctx, r, bbox):
        """The extracts, NDDOT pipes and streams for this dam, each replaced (never reused):
        returns 0, or 8 when the streams are empty (prepare is then not marked done)."""
        reader = None
        if ctx.a.pack and os.path.isfile(ctx.a.pack):
            sys.path.insert(0, os.path.join(ctx.a.plugins, "DamSafety_Runner"))
            from damsuite import offline_pack as op
            reader = op.PackReader(ctx.a.pack)
        smark = os.path.join(self.w, SOURCES_MARK)
        _remove(smark)
        read = [ctx.d(CULVERT_SOURCES[0])]
        for n in EXTRACTS:
            extract(ctx, n, bbox, os.path.join(self.w, "pack_{}_{}.gpkg".format(n, self.num)),
                    reader, self.log, read)
        ds = os.path.join(self.w, "pack_NID_dams_{}_downstream.gpkg".format(self.num))
        src = os.path.join(self.w, "pack_NID_dams_{}.gpkg".format(self.num))
        if os.path.exists(src):
            downstream_nid(src, ds, r.get("NID_ID"))
        else:
            _remove(ds)
        vector_to(os.path.join(self.w, "nddot_pipes_{}.gpkg".format(self.num)),
                  ctx.d(CULVERT_SOURCES[0]), spatFilter=list(bbox), spatSRS="EPSG:5070")
        mark = os.path.join(self.w, STREAMS_MARK)
        _remove(mark)
        info = {"when": now()}
        if ctx.a.offline:
            info.update(source="nhd", why="--offline")
        else:
            try:
                fetch_3dhp(ctx, bbox, self.streams_file("3dhp"), self.log)
                info.update(source="3dhp")
                _remove(self.streams_file("nhd"))       # an older fallback is not used again
            except Exception as e:  # USGS service down: NHD from the suite pack
                self.log("  3DHP failed ({}); using NHD flowlines from the suite pack "
                         "(differs from the box runs; later, {})".format(e, self.refetch_hint()))
                info.update(source="nhd", why="3DHP failed: {}".format(e)[:300])
        if info["source"] == "nhd":
            _remove(self.streams_file("3dhp"))
            extract(ctx, "NHD_flowlines", bbox, self.streams_file("nhd"), None, self.log, read)
        info["file"] = self.streams_file(info["source"])
        info["features"] = feature_count(info["file"]) if os.path.isfile(info["file"]) else 0
        if not info["features"]:
            self.log("prepare: no stream lines for the area ({}); prepare NOT done - fix the "
                     "streams source and run again".format(info["file"]))
            return 8
        write_json(mark, info, indent=1)
        write_json(smark, {"when": now(), "files": sorted(set(read))}, indent=1)
        return 0

    def prepare_sources(self):
        """The files the extracts, NDDOT pipes and NHD streams were cut from (prepare_sources.json);
        for a work folder made before that file existed, every source prepare could have read."""
        try:
            with open(os.path.join(self.w, SOURCES_MARK)) as f:
                return list(json.load(f)["files"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        ctx = self.ctx
        return [ctx.d(CULVERT_SOURCES[0])] + ([ctx.a.pack] if ctx.a.pack else []) + \
            [ctx.d(os.path.join(SUITE, f)) for f in SUITE_LAYERS.values()]

    def go(self):
        ctx = self.ctx
        kit = is_kit(self.dam, ctx.a.no_kits)
        mode = self.mode
        self.record["mode"] = mode
        if not ctx.dry:
            os.makedirs(self.status, exist_ok=True)
            self.logf = open(os.path.join(self.w, "run_{}.log".format(self.dam)), "a")
        for line in ctx.pending_redo.get(self.dam, []):     # an unfinished --redo of this dam
            self.log("NOTE: " + line)
            self.note(line)
        want = stages_to_run(ctx.a.stages, kit, ctx.a.redo)
        if not want and not ctx.a.list_data:
            # e.g. --stages prepare for a dam with a box preparation: nothing applies, so
            # nothing is invalidated and nothing runs
            self.log("{}: not used for this dam ({} mode: the box preparation replaces {}); "
                     "nothing to do".format(",".join(ctx.a.stages), mode, " and ".join(KIT_SKIPS)))
            self.record["skipped"] = True
            return 0
        # status/*.ok of the other mode (e.g. --no-kits for a dam finished with its box
        # preparation): never mix the two; --redo runs every stage again in this mode. (A kit
        # run never reads what prepare and structures made, so their .ok do not count for it.)
        other = {} if ctx.a.list_data else \
            {s: m for s, m in ok_modes(self.w, self.dam).items()
             if m != mode and not (kit and s in KIT_SKIPS)}
        if other:
            was = sorted(set(other.values()))[0]
            how = ("--no-kits: prepared here" if mode == "prepare" else
                   "the box preparation; the earlier run had --no-kits")
            self.record["mode_change"] = {"from": was, "to": mode, "stages": sorted(other)}
            if not ctx.a.redo:
                msg = ("finished stages ({}) ran in {} mode, and this command runs the dam in {} "
                       "mode ({}). Not run, so the two are not mixed: add --redo to run every "
                       "stage of it again in {} mode{}".format(
                           ", ".join(s for s in STAGES if s in other), was, mode, how, mode,
                           "" if mode == "prepare" else ", or add --no-kits to keep prepare mode"))
                self.log(msg)
                ctx.problems.append(msg)
                return 2
            want = stages_to_run(STAGES, kit, True)
            self.log("mode change: finished stages ran in {} mode; --redo runs every stage "
                     "again in {} mode ({})".format(was, mode, how))
        sess = ctx.session
        if ctx.a.redo and sess is not None and sess.began(self.dam) and not other:
            # this --redo command ran before and stopped: the dam's redo began then (its stages
            # were invalidated once); the stages finished since are skipped, the rest run
            self.record["redo_session"] = {"id": sess.sid, "began": sess.began(self.dam)}
            want = sess.stages_of(self.dam) or want
            self.log("--redo session {}: this dam's redo began {}; its stages finished since then "
                     "are skipped".format(sess.sid, sess.began(self.dam)))
        elif ctx.a.redo:
            # --redo: the stages asked for that apply to this dam and every later stage that
            # reads their results run again in this command; the others keep their results
            if ctx.dry:
                self.redo_dry = set(want)
            else:
                self.invalidate(STAGES if other else want)
                if sess is not None:          # after the invalidation: a stop in between redoes
                    sess.begin(self.dam, want)    # the invalidation, never skips it
            if sess is not None:
                self.record["redo_session"] = {"id": sess.sid, "began": now()}
            self.log("--redo{}: {} {}".format(
                " (session {})".format(sess.sid) if sess is not None else "", ", ".join(want),
                "would run again (dry run: no status is changed)" if ctx.dry else "run again"))
        # a stage runs only on finished results of the stages it reads (READS: its .ok newer
        # than theirs, stage_states): an unfinished stage it reads runs first, in this command
        st = self.states()
        run, first = self.needed(want, st)
        for r in first:
            self.log("{}: not finished ({}); it runs first, since {} read(s) its results".format(
                r, st[r], ", ".join(s for s in run if r in READS[s] and st.get(s) is not None)))
        if first:
            self.record["ran_first"] = first
        if not self.redo_dry and not ctx.a.list_data and all(st[s] is None for s in run):
            self.log("finished before (status/*.ok); skipped. {}".format(
                "Its redo in this session is done" if ctx.a.redo else "--redo to run again"))
            self.record["skipped"] = True
            if not kit:
                self.report_streams()
            return 0
        self.log("mode: {} | work {} | results {}".format(mode, self.w, self.df))
        notes = []
        if kit:
            r, h = kit_inputs(ctx, self.dam)
            if r is None:
                return 2
            notes.append("box preparation " + self.ctx.d(KITS[self.dam]))
        else:
            rn, h = generated_inputs(ctx, self.dam, self.w)
            if rn is None:
                return 2
            r, notes = rn
        self.record.setdefault("notes", []).extend(notes)
        # common overrides: this Mac's solvers, threads and output folder
        self.box = area_box(r)
        self.dem_src = r["DEM"]
        ctx.need(self.dem_src, self.dam + " DEM")
        if r["DEM"].lower().endswith(".vrt") and os.path.isfile(r["DEM"]):
            r["DEM"] = relocate_vrt(ctx, r["DEM"], os.path.join(self.w, "dem"), self.box)
            self.note("DEM {} (VRT copied with its paths mapped)".format(self.dem_src))
            if ctx.warnings:
                self.log("  DEM: {} tile(s) outside the dam's area are not here; they are never "
                         "read (e.g. {})".format(len(ctx.warnings), ctx.warnings[0].split(": ", 1)[-1]))
                self.record["dem_tiles_not_here_outside_area"] = len(ctx.warnings)
        r.update({"THREADS": ctx.a.threads, "LISFLOOD_EXE": ctx.a.lisflood,
                  "ANUGA_PYTHON": ctx.a.anuga_python, "DAMS_FOLDER": self.dams_folder})
        h["DAM_FOLDER"] = self.df
        self.record.update({"rundam_inputs": r, "hazclass_inputs": h})
        if ctx.a.list_data:
            for v in list(r.values()) + list(h.values()):
                for f in files_of(v):
                    if not f.startswith(ctx.out) and f not in (
                            ctx.a.lisflood, ctx.a.anuga_python, ctx.a.qgis_process):
                        ctx.used.add(f)
            if self.dem_src.lower().endswith(".vrt") and os.path.isfile(self.dem_src):
                ctx.used.add(self.dem_src)
                ctx.used.update(dem_files(ctx, self.dem_src, self.box))
            if kit:
                for j in ("rundam.json", "hazclass.json"):
                    ctx.used.add(os.path.join(ctx.d(KITS[self.dam]), j))
            need7b, opt7b = screen_7b_files(self.dam)       # the record stage's inputs
            ctx.used.update(f for f in (ctx.d(x) for x in need7b + opt7b) if os.path.isfile(f))
        bbox = self.box
        prep_now = not kit and self.due("prepare", run)
        # 1. extracts (prepare mode only)
        bbox_file = os.path.join(self.w, "extract_box.json")
        if not kit and not ctx.dry:
            if os.path.isfile(bbox_file) and not prep_now:
                bbox = tuple(json.load(open(bbox_file))["bbox"])
            else:
                try:
                    bbox = traced_box(ctx, r, self.log)
                    how = "Runner trace + 3 km"
                except Exception as e:
                    how = "square (trace failed: {})".format(e)
                    self.log("  trace failed ({}); using the square box".format(e))
                write_json(bbox_file, {"bbox": bbox, "how": how, "crs": "EPSG:5070"})
        if prep_now:
            self.log("prepare: extracts for box {} (EPSG:5070, {:.1f} x {:.1f} km){}".format(
                tuple(int(v) for v in bbox), (bbox[2] - bbox[0]) / 1000, (bbox[3] - bbox[1]) / 1000,
                " - dry run shows the square upper bound; the real run traces the path first"
                if ctx.dry else ""))
            if not ctx.dry:
                rc = self.prepare(ctx, r, bbox)
                if rc:
                    return rc
                self.mark("prepare")
        if not kit:
            self.report_streams()
        # 2. structures (ndhazard:runnerculverts)
        pk = lambda n: os.path.join(self.w, "pack_{}_{}.gpkg".format(n, self.num))
        sinfo = self.streams_info() or {}
        streams = sinfo.get("file") or self.streams_file("3dhp")
        s_inputs = {"NBI": pk("NBI_bridges"), "OSM": pk("OSM_crossings"),
                    "NDDOT": ctx.d(CULVERT_SOURCES[1]),
                    "NDDOT_PIPES": os.path.join(self.w, "nddot_pipes_{}.gpkg".format(self.num)),
                    "STREAMS": streams, "ROADS": pk("ND_roads_NG911"), "RAIL": pk("NARN_rail_lines")}
        if not kit:
            self.inputs["structures"] = list(s_inputs.values()) + ([ctx.a.pack] if ctx.a.pack else [])
            # what the extracts were cut from, and the DWR dams layer (AREA_AC)
            self.inputs["prepare"] = self.prepare_sources() + [ctx.d(DWR_DAMS)]
        if not kit and self.due("structures", run):
            args = ["{}={}".format(k, v) for k, v in s_inputs.items()]
            args += ["EXTENT={},{},{},{} [EPSG:5070]".format(bbox[0], bbox[2], bbox[1], bbox[3]),
                     "OUTPUT=" + r["STRUCTURES"].split("|")[0],
                     "CSV=" + os.path.join(self.w, "structures_{}_not_placed.csv".format(self.num))]
            args += CULVERT_ARGS
            if ctx.a.pack:
                args.append("PACK=" + ctx.a.pack)
            if self.run("structures", self.qp() + ["run", "ndhazard:runnerculverts", "--"] + args):
                return 3
            if not ctx.dry:
                self.mark("structures")
        # 3. culvert pre-check (report only, never changes the structures)
        if self.due("precheck", run):
            cli = os.path.join(ctx.a.plugins, "DamSafety_Runner", "scripts", "ndbreach_cli.py")
            cmd = [sys.executable, cli, "culvert-precheck", "--structures",
                   r["STRUCTURES"].split("|")[0], "--layer", "runner_culverts", "--fine-dem",
                   r["DEM"], "--dam", self.dam, "--dem-z", "meters", "--model-cell",
                   str(r.get("CELL", 30)), "--out", os.path.join(self.w, "culvert_precheck")]
            rc = self.run("precheck", cmd, logname="culvert_precheck")
            if rc == 0 and not ctx.dry:
                self.mark("precheck")
            elif not ctx.dry:      # NONFATAL: a report no later stage reads; the rest still runs
                logp = os.path.join(self.w, "culvert_precheck.log")
                self.mark_failed("precheck", rc, logp)
                self.log("WARNING: precheck (the culvert pre-check, a report) failed: exit {}, see "
                         "{}; the later stages still run, and the next run of it tries it "
                         "again".format(rc, logp))
                self.record.setdefault("nonfatal", []).append(
                    "precheck failed: exit {} (see {})".format(rc, logp))
        # 4. the inputs rundam reads (the water mask and the culvert inventory are never made
        #    here; the receptors and structures are, in prepare mode, and the DEM VRT copy is
        #    written only by a real run - its source was checked above) + the two JSON files
        rj = os.path.join(self.w, "rundam.json")
        hj = os.path.join(self.w, "hazclass.json")
        made = ("RECEPTOR_POINTS", "RECEPTOR_ROADS", "RECEPTOR_RAIL", "STRUCTURES")
        keys = ("WATER_MASK", "CULVERT_INVENTORY") + (made if kit or not ctx.dry else ()) + \
            (("DEM",) if not ctx.dry else ())
        for k in keys:
            for x in str(r.get(k) or "").split(";;"):
                if x:
                    ctx.need(x, self.dam + " " + k)
        for k in ("LISFLOOD_EXE", "ANUGA_PYTHON"):
            if r[k] or not ctx.a.list_data:          # --list-data may run without the solvers
                ctx.need(r[k], "solver " + k)
        need7b, _opt7b = screen_7b_files(self.dam)
        if not self.done("record"):
            for x in need7b:
                if ctx.a.allow_no_record:
                    if not os.path.exists(ctx.d(x)):
                        self.log("  screening 7b input not here (--allow-no-record): " + ctx.d(x))
                else:
                    ctx.need(ctx.d(x), "screening 7b record input")
        if not ctx.dry:
            write_json(rj, {"inputs": r}, indent=1)
            write_json(hj, {"inputs": h}, indent=1)
        else:
            if kit:
                for k, v in h.items():
                    if isinstance(v, str) and v.startswith("/") and k != "DAM_FOLDER":
                        for x in v.split(";;"):
                            ctx.need(x, self.dam + " hazclass " + k)
            else:
                if ctx.a.pack:
                    ctx.need(ctx.a.pack, "offline pack")
                for n in EXTRACTS:
                    if not ctx.a.pack and n in SUITE_LAYERS:
                        ctx.need(ctx.d(os.path.join(SUITE, SUITE_LAYERS[n])), "suite layer")
            self.log("would write {} :\n{}".format(rj, json.dumps({"inputs": r}, indent=1)))
            self.log("would write {} ({} inputs)".format(hj, len(h)))
        # 5. Screening 7b record (dams/<ID>/dam.json), before rundam as on the box
        record_err = None
        if self.due("record", run):
            self.log("record: Screening 7b dam.json -> " + os.path.join(self.df, "dam.json"))
            if not ctx.dry:
                try:
                    write_7b(ctx, self.dam, r, self.df)
                    self.mark("record")
                except Exception as e:
                    record_err = "{}: {}".format(type(e).__name__, e)
                    self.log("record: NOT written ({})".format(record_err))
                    self.record["record_error"] = record_err
                    if not ctx.a.allow_no_record:     # rundam needs it (see the docstring)
                        ctx.problems.append("Screening 7b record not written ({}); copy "
                                            "Dams/NickReview/screen_maxstor (see the data list) "
                                            "or run with --allow-no-record".format(record_err))
                        self.log("record failed: rundam and the stages after it are not started")
                        return 4
        # 6-8 models and hazard
        # (a rundam run without the record (--allow-no-record) is not finished once a record is
        #  there: stage_states; it runs again, and hazclass and hazbatch after it)
        if self.due("rundam", run):
            if not ctx.dry and not self.done("record"):
                why = record_err or "status/record.ok is missing (run '--stages record' first)"
                if ctx.a.allow_no_record:
                    self.log("WARNING: rundam runs WITHOUT the Screening 7b record ({}) because "
                             "of --allow-no-record: the crest length is the Runner's default, "
                             "not the record's dam_length_ft, so results can differ from the box "
                             "runs".format(why))
                    self.note("rundam without the Screening 7b record (--allow-no-record): " + why)
                else:
                    ctx.problems.append(
                        "no Screening 7b record ({}). rundam would model the dam with the "
                        "Runner's default crest length instead of the record's dam_length_ft; "
                        "copy Dams/NickReview/screen_maxstor (see the data list) or run with "
                        "--allow-no-record".format(why))
            if ctx.problems and not ctx.dry:
                self.log("inputs missing, rundam not started:\n  " + "\n  ".join(ctx.problems))
                return 4
            if not ctx.dry:      # the record rundam reads (the Runner adds to it while it runs)
                dj = os.path.join(self.df, "dam.json")
                self.record["dam_json_read_by_rundam"] = {dj: sha256(dj)}
            if self.run("rundam", self.qp() + ["run", "ndbreach:rundam", "-"], stdin=rj):
                return 5
            if not ctx.dry:
                self.mark("rundam", "" if self.done("record") else "no-record")
        if self.due("hazclass", run):
            if self.run("hazclass", self.qp() + ["run", "ndbreach:ndhazardclass", "-"], stdin=hj):
                return 6
            if not ctx.dry:
                self.mark("hazclass")
        if self.due("hazbatch", run):
            if not ctx.dry:
                os.makedirs(self.hb, exist_ok=True)
                link = os.path.join(self.hb, self.dam)
                if not os.path.lexists(link):
                    os.symlink(self.df, link)
            else:
                self.log("hazbatch: parent {} with a link {} -> {}".format(self.hb, self.dam, self.df))
            t0 = time.time()
            if self.run("hazbatch", self.qp() + ["run", "ndhazard:batch", "--", "PARENT=" + self.hb]
                        + HAZBATCH_ARGS):
                return 7
            if not ctx.dry:
                # ndhazard:batch exits 0 also when this dam failed or was skipped: its row says
                row, msg = read_batch_row(self.hb, self.dam, t0)
                self.record["stages"]["hazbatch"]["row_status"] = row
                if row != "done":
                    self.log("hazbatch: {} is '{}' in {} ({}); not marked done".format(
                        self.dam, row, HAZBATCH_CSV, msg or "no message"))
                    return 7
                self.mark("hazbatch")
        if not ctx.dry:
            # stages left out of this command (--stages) that read one that ran again
            stale = {s: why for s, why in self.states().items() if why is not None and
                     s not in run and os.path.isfile(os.path.join(self.status, s + ".ok"))}
            if stale:
                self.log("out of date now, not run by this command (--stages): {}; a run with "
                         "them (e.g. without --stages) runs them again".format("; ".join(
                             "{} ({})".format(s, why) for s, why in stale.items())))
                self.record["out_of_date"] = stale
        self.record["finished"] = now()
        return 0

    def checksums(self):
        """sha256 of every file this dam's stages read: each rundam and hazclass input (every
        file of a ';;' list), the DEM VRTs and the DEM tiles that touch the dam's area, the
        structures inputs (streams, extracts, NDDOT, offline pack; prepare mode), the files the
        extracts were cut from (suite pack layers or the offline pack, NHD flowlines, NDDOT
        pipes) and the DWR dams layer (prepare mode), the Screening 7b inputs, the screening
        record, the box kit's JSON files (kit mode), the suite settings CSV the plugins read
        (NDSUITE_SETTINGS: the --out copy) and the two JSON files written here; the
        LISFLOOD-FP binary and the ANUGA python too (they are rundam inputs). The dam's own
        dam.json is hashed when rundam starts (record["dam_json_read_by_rundam"]), since the
        Runner and the hazard tools add to it afterwards."""
        ctx, files = self.ctx, []
        for grp in ("rundam_inputs", "hazclass_inputs"):
            for k, v in sorted((self.record.get(grp) or {}).items()):
                if k not in ("DAMS_FOLDER", "DAM_FOLDER"):
                    files += files_of(v)
        if self.dem_src and self.box and self.dem_src.lower().endswith(".vrt") \
                and os.path.isfile(self.dem_src):
            files += [self.dem_src] + dem_files(ctx, self.dem_src, self.box)
        if self.record.get("mode") == "kit":
            files += [os.path.join(ctx.d(KITS[self.dam]), j) for j in ("rundam.json", "hazclass.json")]
        for v in self.inputs.values():
            files += [f for f in v if f and os.path.isfile(f)]
        need7b, opt7b = screen_7b_files(self.dam)
        files += [ctx.d(x) for x in need7b + opt7b + [os.path.join(SCREEN_DAMS, self.dam, "dam.json")]]
        files += [os.path.join(self.w, j) for j in ("rundam.json", "hazclass.json")]
        if os.environ.get("NDSUITE_SETTINGS"):
            files.append(os.path.abspath(os.environ["NDSUITE_SETTINGS"]))
        seen, sums = set(), {}
        for f in files:
            if f not in seen and os.path.isfile(f):
                seen.add(f)
                sums[f] = sha256(f)
        return sums


def write_7b(ctx, dam, r, folder):
    """The dam.json write of Screening tool 7b (handoff.rundam_params + damrecord_io), from the
    NickReview max-storage screening, as screening7b_record.py did on the box."""
    sys.path.insert(0, ctx.a.plugins)
    from osgeo import ogr, osr
    from DamSafety_Screening.core import handoff as ho
    from DamSafety_Screening.core import damrecord_io as dri
    w = ctx.d(SCREEN_MAXSTOR)
    ds = ogr.Open(os.path.join(w, "8_hazard.gpkg"))
    lyr = ds.GetLayerByName("8_hazard")
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())]
    lyr.SetAttributeFilter("dam_id = '{}'".format(dam))
    f = lyr.GetNextFeature()
    if f is None:
        raise RuntimeError("not in 8_hazard")
    attrs = {n: f.GetField(n) for n in names}
    bds = ogr.Open(os.path.join(w, "4_breach_points.gpkg"))
    bl = bds.GetLayer(0)
    bl.SetAttributeFilter("dam_id = '{}'".format(dam))
    b = bl.GetNextFeature()
    if b is None:
        raise RuntimeError("not in 4_breach_points")
    g = b.GetGeometryRef().Clone()
    alb = osr.SpatialReference(); alb.ImportFromEPSG(5070)
    alb.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src = bl.GetSpatialRef().Clone(); src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    g.Transform(osr.CoordinateTransformation(src, alb))
    c = g.Centroid(); bxy = (c.GetX(), c.GetY())
    bnames = [bl.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(bl.GetLayerDefn().GetFieldCount())]
    blen = b.GetField("crest_len_m") if "crest_len_m" in bnames else None
    area, anote = ho.area_for_runner(os.path.join(w, "3_stage_storage", "curve_{}.csv".format(
        dam)), attrs.get("est_crest_m"), attrs.get("height_m"))
    p, notes = ho.rundam_params(attrs, bxy, "EPSG:5070", r["DEM"], os.path.dirname(folder), None,
                                None, area)
    if anote:
        notes = list(notes) + [anote]
    d_class = "hazard_cla" if "hazard_cla" in names else None
    jv = dri.handoff_values(attrs, p, bxy, "EPSG:5070", d_class, blen)
    miss = dri.missing_notes(jv, attrs, notes)
    os.makedirs(folder, exist_ok=True)
    dri.write(folder, jv, dri.screening_result(attrs, "tool 7b"),
              dri.plugin_version(os.path.join(ctx.a.plugins, "DamSafety_Screening")),
              field_notes=dri.source_notes(attrs, notes, blen), missing=miss)


def versions(a):
    v = {"when": now(), "host": platform.node(), "platform": platform.platform(),
         "machine": platform.machine(), "python": sys.version.split()[0]}
    try:
        res = subprocess.run([a.qgis_process, "--version"], capture_output=True, text=True,
                             timeout=120).stdout
        v["qgis_process"] = [x for x in res.splitlines() if x.strip()][:3]
    except Exception as e:
        v["qgis_process"] = "error: {}".format(e)
    v["plugins"] = {}
    for p in sorted(os.listdir(a.plugins)) if os.path.isdir(a.plugins) else []:
        m = os.path.join(a.plugins, p, "metadata.txt")
        if p.startswith("DamSafety_") and os.path.isfile(m):
            for line in open(m, encoding="utf-8", errors="replace"):
                if line.startswith("version="):
                    v["plugins"][p] = line.split("=", 1)[1].strip()
    try:
        res = subprocess.run([a.anuga_python, "-c", "import anuga,gmsh;print(anuga.__version__,"
                              "gmsh.__version__)"], capture_output=True, text=True, timeout=300)
        v["anuga_gmsh"] = res.stdout.strip() or res.stderr.strip()[-300:]
    except Exception as e:
        v["anuga_gmsh"] = "error: {}".format(e)
    v["lisflood"] = {"path": a.lisflood, "sha256": sha256(a.lisflood),
                     "runner_patch": os.path.isfile(a.lisflood) and b"inittm_depth" in
                     open(a.lisflood, "rb").read()}
    return v


# -- the data lists (--list-data, on the box) ------------------------------------------------
def source_of(rel):
    """The data lists' 'source' column for a file under --data."""
    parts = rel.split("/")
    if rel.startswith("DEM/lidar_1m_native/USGS_1M_"):
        return LIDAR_SOURCE
    if parts[0] == "DEM":
        return "copy (box-made DEM: 3DEP averaged/clipped)"
    if parts[0] == "nickreview_rerun_20261023":
        if len(parts) > 1 and parts[1] == "tiles":
            return "copy (box-made 1 m tiles)"
        if parts[-1].startswith("pack_"):
            return "copy (offline-pack extract made on the box)"
        return "copy (box run kit: settings / DEM VRT)"
    if parts[0] == "offline_packs_20261021":
        return "offline pack 20261021 (copy; read only, Hazard offline_pack setting stays empty)"
    if parts[0] == "ND_Dam_Suite_Pack":
        return "ND_Dam_Suite_Pack (copy, or point --map at your own copy)"
    if parts[0] == "culvert_sources":
        return "copy (box-made from NDDOT / NTAD downloads)"
    if parts[0] == "culvert_precheck_20261024":
        return "copy (box culvert fix: invfix2 + NBI 9122221)"
    if rel.startswith("Dams/NickReview/screen_maxstor/"):
        return "copy (NickReview max-storage screening: Screening 7b record input)"
    if rel.startswith("Dams/NickReview/"):
        return "copy (NickReview screening record)"
    return "copy"


def list_group(rel):
    """The folder a data-list file is counted under in DATA_TO_COPY*.md."""
    parts = rel.split("/")
    n = 3 if len(parts) > 3 and (parts[1] == "tiles" or parts[2] == "screen_maxstor") else 2
    return "/".join(parts[:n]) if len(parts) > n else rel


def write_data_lists(tsv, rows, dams):
    """The TSV (bytes, copy_to, source: the format get_lidar_tiles.sh reads) and, next to it,
    the DATA_TO_COPY markdown summary. rows: [(bytes, rel)]."""
    rows = sorted(rows, key=lambda x: x[1].encode())
    os.makedirs(os.path.dirname(os.path.abspath(tsv)), exist_ok=True)
    with open(tsv, "w") as fo:
        fo.write(LIST_HEADER)
        for n, rel in rows:
            fo.write("{}\t{}\t{}\n".format(n, rel, source_of(rel)))
    base = os.path.basename(tsv)
    stem = base[len("data_files_"):-4] if base.startswith("data_files_") and base.endswith(".tsv") \
        else os.path.splitext(base)[0]
    md = os.path.join(os.path.dirname(os.path.abspath(tsv)), "DATA_TO_COPY_{}.md".format(stem))
    total = sum(n for n, _ in rows)
    lidar = [n for n, rel in rows if source_of(rel) == LIDAR_SOURCE]
    groups = {}
    for n, rel in rows:
        g = groups.setdefault(list_group(rel), [0, 0, source_of(rel)])
        g[0] += 1
        g[1] += n
    size = lambda b: "{:.2f} GB".format(b / 1e9) if b >= 1e8 else "{:.1f} MB".format(b / 1e6)
    lines = ["# Data to copy for {}".format(dams[0] if len(dams) == 1 else
                                            "all {} NickReview dams".format(len(dams))), "",
             "Put every file at `~/dwr-dam-suite/NickReview_data/<copy_to>` (same sub-folders). "
             "The scripts only read this folder.", "",
             "- **{} files, {:.2f} GB** in total.".format(len(rows), total / 1e9)]
    if lidar:
        lines += ["- {:.2f} GB of that is USGS 3DEP 1 m LiDAR that can be downloaded instead of "
                  "copied; the rest ({:.2f} GB) must be copied from the box.".format(
                      sum(lidar) / 1e9, (total - sum(lidar)) / 1e9),
                  "  To download them: `bash get_lidar_tiles.sh <path of {}>` ({} tiles; it "
                  "checks the size of each).".format(base, len(lidar))]
    lines += ["- The full per-file list (size, path, source) is `{}`.".format(base), "",
              "| folder | files | size | source |", "|---|---:|---:|---|"]
    for g, (cnt, b, src) in sorted(groups.items(), key=lambda kv: (-kv[1][1], kv[0])):
        lines.append("| `{}` | {} | {} | {} |".format(g, cnt, size(b), src))
    with open(md, "w") as f:
        f.write("\n".join(lines) + "\n")
    return md, total


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--data", required=True, help="NickReview data folder (read only)")
    ap.add_argument("--out", required=True, help="results folder (written)")
    ap.add_argument("--dam", action="append", default=[], help="only this dam (repeatable)")
    ap.add_argument("--dams-file", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--redo", action="store_true", help="run finished stages again, in this "
                    "command: the stages in --stages that apply to the dam and every later stage "
                    "that reads their results (precheck: only precheck; prepare and structures "
                    "do not apply to DWR-2801/2865/3034: they use the box preparation unless "
                    "--no-kits). Stopped, the same command goes on where it stopped "
                    "(<out>/redo_sessions/)")
    ap.add_argument("--offline", action="store_true", help="NHD flowlines from the suite pack "
                    "instead of downloading 3DHP")
    ap.add_argument("--no-kits", action="store_true", help="prepare 2801/2865/3034 here too")
    ap.add_argument("--allow-no-record", action="store_true", help="run rundam even when the "
                    "Screening 7b record could not be written (results can differ from the box)")
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--pack", default="", help="offline pack .sqlite (default: from --data)")
    # required, except with --list-data (a dry run that reads only the data files)
    ap.add_argument("--plugins", default="", help="the profile's python/plugins folder")
    ap.add_argument("--qgis-process", default="")
    ap.add_argument("--anuga-python", default="")
    ap.add_argument("--lisflood", default="")
    ap.add_argument("--map", action="append", default=[], metavar="BOXPATH=MACPATH")
    ap.add_argument("--list-data", default="", metavar="TSV",
                    help="(on the box) write every data file these dams need, with sizes, the "
                         "path to copy it to under --data and its source (the data_files_*.tsv "
                         "format), and the DATA_TO_COPY_*.md summary next to it; implies --dry-run "
                         "and lists finished dams too. --plugins, --qgis-process, --anuga-python "
                         "and --lisflood may be left out (on the box: --data /home/box/DamSafety)")
    a = ap.parse_args()
    try:                                    # every mode reads the data with GDAL
        __import__("osgeo.gdal")
    except ImportError:
        ap.error("this python ({}) has no GDAL (osgeo): run it with the QGIS python (on the box "
                 "the box's QGIS python; on the Mac run_nickreview.sh)".format(sys.executable))
    a.pack_given = a.pack
    if a.list_data:
        a.dry_run = True
    else:
        lack = ["--" + k.replace("_", "-") for k in ("plugins", "qgis_process", "anuga_python",
                                                     "lisflood") if not getattr(a, k)]
        if lack:
            ap.error("the following arguments are required: " + ", ".join(lack))
    a.stages = [s for s in a.stages.split(",") if s]
    bad_maps = [m for m in a.map if "=" not in m or not m.split("=", 1)[0].startswith("/")]
    if bad_maps:
        ap.error("--map needs BOXPATH=MACPATH with an absolute box path: {}".format(bad_maps))
    a.maps = [tuple(m.split("=", 1)) for m in a.map]
    bad = [s for s in a.stages if s not in STAGES]
    if bad or not a.stages:
        ap.error("unknown stage(s) {}; use {}".format(bad, ",".join(STAGES)))
    ctx = Ctx(a)
    if not a.pack and os.path.isfile(ctx.d(PACK_SQLITE)):
        a.pack = ctx.d(PACK_SQLITE)
    dams = [x.split("#")[0].strip() for x in open(a.dams_file)]
    dams = [x for x in dams if x]
    if a.dam:
        unknown = [d for d in a.dam if d not in dams]
        if unknown:
            out("Not in {}: {} (running them anyway)".format(a.dams_file, unknown))
        dams = a.dam
    out("NickReview on {} | {} dam(s) | threads {} | data {} (read only) | out {}{}".format(
        platform.machine(), len(dams), a.threads, ctx.data, ctx.out,
        " | DRY RUN: nothing is run or written" if a.dry_run else ""))
    out("offline pack: {}".format(a.pack or "none (suite pack layers; OSM crossings from the "
                                            "Hazard pack setting/downloads)"))
    manifest = {"script": "nickreview_mac.py", "args": vars(a), "env": {
        k: os.environ.get(k) for k in ("NDBREACH_ANUGA_MESHER", "NDBREACH_DEM_RESAMPLE",
                                       "NDSUITE_SETTINGS", "QGIS_CUSTOM_CONFIG_PATH",
                                       "NDBREACH_LISFLOOD", "NDBREACH_ANUGA_PYTHON",
                                       "OMP_NUM_THREADS", "NDBREACH_ANUGA_THREADS")},
        "settings_rundam": RUNDAM_FIXED, "hazbatch": HAZBATCH_ARGS, "dams": []}
    st = os.environ.get("NDSUITE_SETTINGS")
    if st:
        manifest["settings_csv_sha256"] = {os.path.abspath(st): sha256(os.path.abspath(st))}
    mp = os.path.join(ctx.out, "manifest_{}.json".format(now().replace(":", "")))

    def save_manifest():
        if not a.dry_run:
            write_json(mp, manifest, indent=1, default=str)

    # --redo sessions: continue this command's unfinished one, or start one; say which other
    # --redo commands are unfinished (a plain run keeps the dams they have not reached as they are)
    others, continued = [], None
    if not a.list_data:
        opened = RedoSession.open_ones(ctx.out, tidy=not a.dry_run)
        for s in opened:     # dams finished by runs since (e.g. a plain resume): closes some
            s.refresh(ctx.out, save=not a.dry_run)
        opened = [s for s in opened if not s.rec.get("finished")]
        if a.redo:
            key = RedoSession.key(a, dams)
            same = [s for s in opened if s.rec.get("key") == key]
            if same:
                ctx.session = same[-1]
                new, part = ctx.session.pending()
                continued = [d for d in key["dams"] if ctx.session.dam(d).get("finished")]
                out("--redo: the same command as the unfinished --redo session {} (started {}), "
                    "so this run CONTINUES it instead of starting a new redo: the dams it already "
                    "redid are not redone again ({}); not redone yet: {}; begun: {}. To start a "
                    "new --redo of every dam instead, delete its file first: rm {}".format(
                        ctx.session.sid, ctx.session.rec.get("started"),
                        " ".join(continued) or "-", " ".join(new) or "-", " ".join(part) or "-",
                        shlex.quote(ctx.session.path)))
            elif not a.dry_run:
                ctx.session = RedoSession.new(ctx.out, key, run_command(a, ctx))
                ctx.session.save()
                out("--redo session {} ({}): run the same command again after a stop and it "
                    "goes on where it stopped".format(ctx.session.sid, ctx.session.path))
            else:
                out("--redo (dry run): a new --redo session would start")
        others = [s for s in opened if s is not ctx.session]
        for s in others:
            out("WARNING: " + "\n  ".join(s.describe()))
            new, _part = s.pending()
            for d in new:
                ctx.pending_redo.setdefault(d, []).append(
                    "the unfinished --redo session {} has not redone this dam yet (results from "
                    "before that --redo); to finish it: {}".format(s.sid, s.rec.get("command")))
        if ctx.session is not None:
            manifest["redo_session"] = {
                "id": ctx.session.sid, "path": ctx.session.path,
                "started": ctx.session.rec.get("started"), "continued": continued is not None,
                "redone_before_this_run": continued or []}
        if others:
            manifest["unfinished_redo_sessions"] = [s.sid for s in others]

    worst = 0
    try:
        if not a.dry_run:
            os.makedirs(ctx.out, exist_ok=True)
            manifest["versions"] = versions(a)
            save_manifest()
        for dam in dams:
            ctx.problems, ctx.warnings = [], []
            run = Runner(ctx, dam)
            manifest["dams"].append(run.record)
            try:
                rc = run.go()
            except KeyboardInterrupt:
                run.log("stopped by the user"); rc = 130
            except Exception as e:
                run.log("ERROR {}: {}".format(type(e).__name__, e)); rc = 9
            run.record["rc"] = rc
            run.record["problems"] = list(ctx.problems)
            if ctx.problems:
                run.log("PROBLEMS:\n  " + "\n  ".join(ctx.problems))
            if not a.dry_run and rc != 130 and not run.record.get("skipped"):
                try:
                    run.record["checksums"] = run.checksums()
                except KeyboardInterrupt:
                    run.record["checksums"] = "interrupted"; rc = run.record["rc"] = 130
                except Exception as e:
                    run.record["checksums"] = "error: {}".format(e)
            if ctx.session is not None and not a.dry_run and rc != 130:
                ctx.session.refresh(ctx.out)        # this dam's redo finished (or not yet)
            if run.logf:
                run.logf.close()
            save_manifest()                         # after every dam, so a stop keeps them
            worst = max(worst, rc)
            if rc == 130:
                break
    except KeyboardInterrupt:
        out("stopped by the user")
        worst = max(worst, 130)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)   # a second Ctrl-C must not lose the manifest
        sess = ctx.session
        if sess is not None and not a.dry_run:
            try:                                       # closed when every dam is redone
                manifest["redo_session"].update(
                    finished=sess.rec.get("finished") if sess.refresh(ctx.out) else None,
                    path=sess.path, problems=sess.problems())
            except Exception as e:
                out("WARNING: the --redo session file could not be updated ({})".format(e))
        save_manifest()
    if a.list_data:
        common = [ctx.d(PACK_SQLITE), ctx.d(DWR_DAMS), ctx.d(WATER_MASK)] + [ctx.d(c) for c in CULVERT_SOURCES]
        common += [ctx.d(os.path.join(SUITE, f)) for f in SUITE_LAYERS.values()]
        if any(d not in KITS or a.no_kits for d in dams):  # prepare-mode dams read these
            ctx.used.update(f for f in common if os.path.isfile(f))
        for dam in dams:
            for f in (os.path.join(SCREEN_DAMS, dam, "dam.json"),):
                if os.path.isfile(ctx.d(f)):
                    ctx.used.add(ctx.d(f))
        rows = []
        for f in ctx.used:
            rel = ctx.rel_of(f)
            if rel.startswith("OTHER/"):                 # a --data path, not a box path
                rel = os.path.relpath(f, ctx.data) if f.startswith(ctx.data + os.sep) else rel
            rows.append((os.path.getsize(f), rel))
        md, total = write_data_lists(a.list_data, rows, dams)
        out("data list: {} files, {:.2f} GB -> {} (and {})".format(len(rows), total / 1e9,
                                                                  a.list_data, md))
    if not a.dry_run:
        out("manifest: " + mp)
    out("\nsummary (dam, mode, exit, problems):")
    for d in manifest["dams"]:
        out("  {:10s} {:8s} {:3d} {}{}".format(
            d["dam"], d.get("mode", "-"), d.get("rc", 130), len(d.get("problems", [])),
            "  (not fatal: " + "; ".join(x.split(" (see ")[0] for x in d["nonfatal"]) + ")"
            if d.get("nonfatal") else ""))
    sess = ctx.session
    if sess is not None:
        if continued is not None:
            out("--redo: this run CONTINUED the --redo session {} started {} (an earlier run of "
                "the same command); the dams it had already redone were not redone again: "
                "{}".format(sess.sid, sess.rec.get("started"), " ".join(continued) or "none"))
        if a.dry_run:
            pass
        elif sess.rec.get("finished"):
            probs = sess.problems()
            out("--redo session {}: every dam of it is redone (closed; the same command now "
                "starts a new --redo){}".format(sess.sid, "; finished with problems: " + "; ".join(
                    "{}: {}".format(d, ", ".join(p)) for d, p in probs.items()) if probs else ""))
        else:
            new, part = sess.pending()
            out("--redo session {} is not finished (not redone yet: {}; begun: {}). Run the same "
                "command again to go on: {}".format(sess.sid, " ".join(new) or "-",
                                                    " ".join(part) or "-", sess.rec.get("command")))
            out("  To start a new --redo of every dam instead, delete the session's file and run "
                "the command again: rm {}".format(shlex.quote(sess.path)))
    for s in others:
        if a.dry_run or not s.refresh(ctx.out):
            out("WARNING: " + "\n  ".join(s.describe()))
    return 0 if worst == 0 else (130 if worst == 130 else 1)


if __name__ == "__main__":
    sys.exit(main())
