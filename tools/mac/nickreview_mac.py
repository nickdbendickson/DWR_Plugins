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
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

MILE = 1609.344
# Box path -> folder name inside --data (where make_nickreview_data_kit.sh puts it)
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
STAGES = ["prepare", "structures", "precheck", "record", "rundam", "hazclass", "hazbatch"]


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(path, limit=512 * 1024 * 1024):
    if not os.path.isfile(path):
        return None
    if os.path.getsize(path) > limit:
        return "skipped (>512 MB)"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Ctx(object):
    def __init__(self, a):
        self.a = a
        self.data = os.path.abspath(os.path.expanduser(a.data))
        self.out = os.path.abspath(os.path.expanduser(a.out))
        self.dry = a.dry_run
        self.problems = []
        self.used = set()

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


# -- VRT relocation ----------------------------------------------------------------------------
def relocate_vrt(ctx, src_vrt, dst_dir, seen=None):
    """Copy a VRT (and the VRTs it references) into dst_dir with every source path remapped to
    --data. Rasters are referenced in place (absolute), never copied. Returns the new path."""
    seen = {} if seen is None else seen
    src_vrt = os.path.abspath(src_vrt)
    if src_vrt in seen:
        return seen[src_vrt]
    base = os.path.basename(src_vrt)
    dst = os.path.join(dst_dir, base)
    seen[src_vrt] = dst
    tree = ET.parse(src_vrt)
    for el in tree.iter():
        if el.tag not in ("SourceFilename", "SourceDataset") or not (el.text or "").strip():
            continue
        txt = el.text.strip()
        if el.get("relativeToVRT") == "1":
            ref = os.path.normpath(os.path.join(os.path.dirname(src_vrt), txt))
        else:
            ref = txt
        ref = ctx.map_path(ref)
        if ref.lower().endswith(".vrt") and os.path.isfile(ref):
            ref = relocate_vrt(ctx, ref, dst_dir, seen)
        elif not os.path.exists(ref):
            ctx.problems.append("DEM tile missing: {}".format(ref))
        el.text = ref
        el.set("relativeToVRT", "0")
    if not ctx.dry:
        os.makedirs(dst_dir, exist_ok=True)
        tree.write(dst, encoding="UTF-8")
    return dst


def tile_hits(path, box):
    """Does a raster's footprint touch box (EPSG:5070)? (only the tiles a dam reads are listed)"""
    from osgeo import gdal, osr
    ds = gdal.Open(path)
    gt, w, h = ds.GetGeoTransform(), ds.RasterXSize, ds.RasterYSize
    src = osr.SpatialReference(wkt=ds.GetProjection())
    dst = osr.SpatialReference(); dst.ImportFromEPSG(5070)
    for s_ in (src, dst):
        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    xs, ys = [], []
    for i in range(5):
        for j in range(5):
            x, y, _ = ct.TransformPoint(gt[0] + gt[1] * w * i / 4, gt[3] + gt[5] * h * j / 4)
            xs.append(x); ys.append(y)
    return not (max(xs) < box[0] or min(xs) > box[2] or max(ys) < box[1] or min(ys) > box[3])


def vrt_tiles(ctx, vrt, acc=None):
    """All files a VRT needs (for the manifest and the data-kit list)."""
    acc = set() if acc is None else acc
    for el in ET.parse(vrt).iter():
        if el.tag in ("SourceFilename", "SourceDataset") and (el.text or "").strip():
            t = el.text.strip()
            p = os.path.normpath(os.path.join(os.path.dirname(vrt), t)) \
                if el.get("relativeToVRT") == "1" else t
            p = ctx.map_path(p)
            if p not in acc:
                acc.add(p)
                if p.lower().endswith(".vrt") and os.path.isfile(p):
                    vrt_tiles(ctx, p, acc)
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


def write_features(feats, dst, name):
    from osgeo import gdal
    gdal.UseExceptions()
    tmp = dst + ".geojson"
    with open(tmp, "w") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f)
    gdal.VectorTranslate(dst, tmp, format="GPKG", layerName=name)
    os.remove(tmp)


def extract(ctx, name, bbox, dst, pack_reader, log):
    from osgeo import gdal
    gdal.UseExceptions()
    if pack_reader is not None:
        try:
            feats = pack_reader.query(name, lonlat_box(bbox))
            write_features(feats, dst, name)
            log("  {} {} features (offline pack)".format(name, len(feats)))
            return True
        except Exception as e:  # not in the pack: fall back to the suite pack
            log("  {}: not from the offline pack ({}); trying the suite pack".format(name, e))
    rel = SUITE_LAYERS.get(name)
    if rel is None or not os.path.isfile(ctx.d(os.path.join(SUITE, rel))):
        log("  {}: no source (left empty)".format(name))
        return False
    gdal.VectorTranslate(dst, ctx.d(os.path.join(SUITE, rel)), format="GPKG", layerName=name,
                         spatFilter=list(bbox), spatSRS="EPSG:5070")
    log("  {} (suite pack, clipped)".format(name))
    return True


def downstream_nid(src, dst, nid):
    from osgeo import gdal
    gdal.UseExceptions()
    where = "NIDID <> '{}'".format(nid.replace("'", "")) if nid else None
    gdal.VectorTranslate(dst, src, format="GPKG", layerName="NID_dams", where=where)


def fetch_3dhp(ctx, bbox, dst, log):
    """3DHP flowlines (USGS REST) for the area, with the NHD field names added, as the box did."""
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
                break
            except Exception as e:
                if attempt == 3:
                    raise
                log("  3DHP retry ({})".format(e)); time.sleep(10 * (attempt + 1))
        got = page.get("features") or []
        feats += got
        if len(got) < 1000 and not page.get("exceededTransferLimit"):
            break
        off += len(got)
    feats = h3.normalise_flowlines(feats)
    write_features(feats, dst, "streams_3dhp")
    log("  3DHP flowlines {}".format(len(feats)))


# -- running -----------------------------------------------------------------------------------
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

    def log(self, msg):
        line = "[{}] {} {}".format(now(), self.dam, msg)
        print(line, flush=True)
        if self.logf:
            self.logf.write(line + "\n"); self.logf.flush()

    def done(self, stage):
        return os.path.isfile(os.path.join(self.status, stage + ".ok"))

    def mark(self, stage, info=""):
        with open(os.path.join(self.status, stage + ".ok"), "w") as f:
            f.write(now() + " " + info + "\n")

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
        return [self.ctx.a.qgis_process]

    def go(self):
        ctx = self.ctx
        kit = self.dam in KITS and not ctx.a.no_kits
        mode = "kit" if kit else "prepare"
        if not ctx.dry:
            os.makedirs(self.status, exist_ok=True)
            self.logf = open(os.path.join(self.w, "run_{}.log".format(self.dam)), "a")
        if ctx.a.redo and not ctx.dry:
            for s in STAGES:
                p = os.path.join(self.status, s + ".ok")
                if os.path.exists(p):
                    os.rename(p, p + ".redo_" + now().replace(":", ""))
        if all(self.done(s) for s in ctx.a.stages):
            self.log("finished before (status/*.ok); skipped. --redo to run again")
            self.record["skipped"] = True
            return 0
        self.log("mode: {} | work {} | results {}".format(mode, self.w, self.df))
        self.record["mode"] = mode
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
        # common overrides: this Mac's solvers, threads and output folder
        self.dem_src = r["DEM"]
        ctx.need(self.dem_src, self.dam + " DEM")
        if r["DEM"].lower().endswith(".vrt") and os.path.isfile(r["DEM"]):
            r["DEM"] = relocate_vrt(ctx, r["DEM"], os.path.join(self.w, "dem"))
            notes.append("DEM {} (VRT copied with its paths mapped)".format(self.dem_src))
        r.update({"THREADS": ctx.a.threads, "LISFLOOD_EXE": ctx.a.lisflood,
                  "ANUGA_PYTHON": ctx.a.anuga_python, "DAMS_FOLDER": self.dams_folder})
        h["DAM_FOLDER"] = self.df
        self.record.update({"notes": notes, "rundam_inputs": r, "hazclass_inputs": h})
        if ctx.a.list_data:
            for v in list(r.values()) + list(h.values()):
                if isinstance(v, str):
                    for part in v.split(";;"):
                        f = part.split("|")[0].strip()
                        if f.startswith("/") and os.path.isfile(f) and not f.startswith(ctx.out) \
                                and f not in (ctx.a.lisflood, ctx.a.anuga_python, ctx.a.qgis_process):
                            ctx.used.add(f)
            if self.dem_src.lower().endswith(".vrt"):
                ctx.used.add(self.dem_src)
                box = area_box(r)
                ctx.used.update(t for t in vrt_tiles(ctx, self.dem_src) if os.path.isfile(t)
                                and (t.lower().endswith(".vrt") or tile_hits(t, box)))
            if kit:
                for j in ("rundam.json", "hazclass.json"):
                    ctx.used.add(os.path.join(ctx.d(KITS[self.dam]), j))
        bbox = area_box(r)
        want = ctx.a.stages
        # 1. extracts (prepare mode only)
        bbox_file = os.path.join(self.w, "extract_box.json")
        if not kit and not ctx.dry:
            if os.path.isfile(bbox_file):
                bbox = tuple(json.load(open(bbox_file))["bbox"])
            else:
                try:
                    bbox = traced_box(ctx, r, self.log)
                    how = "Runner trace + 3 km"
                except Exception as e:
                    how = "square (trace failed: {})".format(e)
                    self.log("  trace failed ({}); using the square box".format(e))
                json.dump({"bbox": bbox, "how": how, "crs": "EPSG:5070"}, open(bbox_file, "w"))
        if not kit and "prepare" in want and not self.done("prepare"):
            self.log("prepare: extracts for box {} (EPSG:5070, {:.1f} x {:.1f} km){}".format(
                tuple(int(v) for v in bbox), (bbox[2] - bbox[0]) / 1000, (bbox[3] - bbox[1]) / 1000,
                " - dry run shows the square upper bound; the real run traces the path first"
                if ctx.dry else ""))
            if not ctx.dry:
                reader = None
                if ctx.a.pack and os.path.isfile(ctx.a.pack):
                    sys.path.insert(0, os.path.join(ctx.a.plugins, "DamSafety_Runner"))
                    from damsuite import offline_pack as op
                    reader = op.PackReader(ctx.a.pack)
                for n in EXTRACTS:
                    dst = os.path.join(self.w, "pack_{}_{}.gpkg".format(n, self.num))
                    if not os.path.exists(dst):
                        extract(ctx, n, bbox, dst, reader, self.log)
                ds = os.path.join(self.w, "pack_NID_dams_{}_downstream.gpkg".format(self.num))
                src = os.path.join(self.w, "pack_NID_dams_{}.gpkg".format(self.num))
                if os.path.exists(src) and not os.path.exists(ds):
                    downstream_nid(src, ds, r.get("NID_ID"))
                pipes = os.path.join(self.w, "nddot_pipes_{}.gpkg".format(self.num))
                if not os.path.exists(pipes):
                    from osgeo import gdal
                    gdal.UseExceptions()
                    gdal.VectorTranslate(pipes, ctx.d(CULVERT_SOURCES[0]), format="GPKG",
                                         spatFilter=list(bbox), spatSRS="EPSG:5070")
                streams = os.path.join(self.w, "streams_3dhp_{}.gpkg".format(self.num))
                if not os.path.exists(streams):
                    if ctx.a.offline:
                        extract(ctx, "NHD_flowlines", bbox, streams, None, self.log)
                    else:
                        try:
                            fetch_3dhp(ctx, bbox, streams, self.log)
                        except Exception as e:  # USGS service down: NHD from the suite pack
                            self.log("  3DHP failed ({}); using NHD flowlines from the suite pack "
                                     "(differs from the box runs; rerun prepare with --redo "
                                     "later for 3DHP)".format(e))
                            self.record.setdefault("notes", []).append("streams: NHD fallback")
                            extract(ctx, "NHD_flowlines", bbox, streams, None, self.log)
                self.mark("prepare")
        # 2. structures (ndhazard:runnerculverts)
        if not kit and "structures" in want and not self.done("structures"):
            pk = lambda n: os.path.join(self.w, "pack_{}_{}.gpkg".format(n, self.num))
            args = ["NBI=" + pk("NBI_bridges"), "OSM=" + pk("OSM_crossings"),
                    "NDDOT=" + ctx.d(CULVERT_SOURCES[1]),
                    "NDDOT_PIPES=" + os.path.join(self.w, "nddot_pipes_{}.gpkg".format(self.num)),
                    "STREAMS=" + os.path.join(self.w, "streams_3dhp_{}.gpkg".format(self.num)),
                    "ROADS=" + pk("ND_roads_NG911"), "RAIL=" + pk("NARN_rail_lines"),
                    "EXTENT={},{},{},{} [EPSG:5070]".format(bbox[0], bbox[2], bbox[1], bbox[3]),
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
        if "precheck" in want and not self.done("precheck"):
            cli = os.path.join(ctx.a.plugins, "DamSafety_Runner", "scripts", "ndbreach_cli.py")
            cmd = [sys.executable, cli, "culvert-precheck", "--structures",
                   r["STRUCTURES"].split("|")[0], "--layer", "runner_culverts", "--fine-dem",
                   r["DEM"], "--dam", self.dam, "--dem-z", "meters", "--model-cell",
                   str(r.get("CELL", 30)), "--out", os.path.join(self.w, "culvert_precheck")]
            if self.run("precheck", cmd, logname="culvert_precheck") == 0 and not ctx.dry:
                self.mark("precheck")
        # 4. Screening 7b record + the two JSON files
        rj = os.path.join(self.w, "rundam.json")
        hj = os.path.join(self.w, "hazclass.json")
        if not ctx.dry:
            for k in ("RECEPTOR_POINTS", "RECEPTOR_ROADS", "RECEPTOR_RAIL", "STRUCTURES", "DEM"):
                ctx.need(r[k], self.dam + " " + k)
            json.dump({"inputs": r}, open(rj, "w"), indent=1)
            json.dump({"inputs": h}, open(hj, "w"), indent=1)
        else:
            for k in ("WATER_MASK",) + (("RECEPTOR_POINTS", "RECEPTOR_ROADS", "RECEPTOR_RAIL",
                                          "STRUCTURES") if kit else ()):
                ctx.need(r[k], self.dam + " " + k)
            if kit:
                for k, v in h.items():
                    if isinstance(v, str) and v.startswith("/") and k != "DAM_FOLDER":
                        for x in v.split(";;"):
                            ctx.need(x, self.dam + " hazclass " + k)
            else:
                for x in CULVERT_SOURCES:
                    ctx.need(ctx.d(x), "culvert source")
                if ctx.a.pack:
                    ctx.need(ctx.a.pack, "offline pack")
                for n in EXTRACTS:
                    if not ctx.a.pack and n in SUITE_LAYERS:
                        ctx.need(ctx.d(os.path.join(SUITE, SUITE_LAYERS[n])), "suite layer")
                ctx.need(ctx.d(os.path.join(SCREEN_MAXSTOR, "8_hazard.gpkg")), "screening 7b")
            for k in ("LISFLOOD_EXE", "ANUGA_PYTHON"):
                ctx.need(r[k], "solver " + k)
            self.log("would write {} :\n{}".format(rj, json.dumps({"inputs": r}, indent=1)))
            self.log("would write {} ({} inputs)".format(hj, len(h)))
        if "record" in want and not self.done("record"):
            self.log("record: Screening 7b dam.json -> " + os.path.join(self.df, "dam.json"))
            if not ctx.dry:
                try:
                    write_7b(ctx, self.dam, r, self.df)
                    self.mark("record")
                except Exception as e:  # provenance only; the run does not need it
                    self.log("record: not written ({})".format(e))
        # 5-7 models and hazard
        if "rundam" in want and not self.done("rundam"):
            if self.ctx.problems and not ctx.dry:
                self.log("inputs missing, rundam not started:\n  " + "\n  ".join(ctx.problems))
                return 4
            if self.run("rundam", self.qp() + ["run", "ndbreach:rundam", "-"], stdin=rj):
                return 5
            if not ctx.dry:
                self.mark("rundam")
        if "hazclass" in want and not self.done("hazclass"):
            if self.run("hazclass", self.qp() + ["run", "ndbreach:ndhazardclass", "-"], stdin=hj):
                return 6
            if not ctx.dry:
                self.mark("hazclass")
        if "hazbatch" in want and not self.done("hazbatch"):
            if not ctx.dry:
                os.makedirs(self.hb, exist_ok=True)
                link = os.path.join(self.hb, self.dam)
                if not os.path.lexists(link):
                    os.symlink(self.df, link)
            else:
                self.log("hazbatch: parent {} with a link {} -> {}".format(self.hb, self.dam, self.df))
            if self.run("hazbatch", self.qp() + ["run", "ndhazard:batch", "--", "PARENT=" + self.hb]
                        + HAZBATCH_ARGS):
                return 7
            if not ctx.dry:
                self.mark("hazbatch")
        self.record["finished"] = now()
        return 0


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
        out = subprocess.run([a.qgis_process, "--version"], capture_output=True, text=True,
                             timeout=120).stdout
        v["qgis_process"] = [x for x in out.splitlines() if x.strip()][:3]
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
        out = subprocess.run([a.anuga_python, "-c", "import anuga,gmsh;print(anuga.__version__,"
                              "gmsh.__version__)"], capture_output=True, text=True, timeout=300)
        v["anuga_gmsh"] = out.stdout.strip() or out.stderr.strip()[-300:]
    except Exception as e:
        v["anuga_gmsh"] = "error: {}".format(e)
    v["lisflood"] = {"path": a.lisflood, "sha256": sha256(a.lisflood),
                     "runner_patch": os.path.isfile(a.lisflood) and b"inittm_depth" in
                     open(a.lisflood, "rb").read()}
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--data", required=True, help="NickReview data folder (read only)")
    ap.add_argument("--out", required=True, help="results folder (written)")
    ap.add_argument("--dam", action="append", default=[], help="only this dam (repeatable)")
    ap.add_argument("--dams-file", required=True)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--redo", action="store_true", help="run finished stages again")
    ap.add_argument("--offline", action="store_true", help="NHD flowlines from the suite pack "
                    "instead of downloading 3DHP")
    ap.add_argument("--no-kits", action="store_true", help="prepare 2801/2865/3034 here too")
    ap.add_argument("--stages", default=",".join(STAGES))
    ap.add_argument("--pack", default="", help="offline pack .sqlite (default: from --data)")
    ap.add_argument("--plugins", required=True, help="the profile's python/plugins folder")
    ap.add_argument("--qgis-process", required=True)
    ap.add_argument("--anuga-python", required=True)
    ap.add_argument("--lisflood", required=True)
    ap.add_argument("--map", action="append", default=[], metavar="BOXPATH=MACPATH")
    ap.add_argument("--list-data", default="", metavar="TSV",
                    help="(on the box) write every data file these dams need, with sizes and the "
                         "path to copy it to under --data; implies --dry-run")
    a = ap.parse_args()
    if a.list_data:
        a.dry_run = True
    a.stages = [s for s in a.stages.split(",") if s]
    a.maps = [tuple(m.split("=", 1)) for m in a.map]
    bad = [s for s in a.stages if s not in STAGES]
    if bad:
        ap.error("unknown stage(s) {}; use {}".format(bad, ",".join(STAGES)))
    ctx = Ctx(a)
    if not a.pack and os.path.isfile(ctx.d(PACK_SQLITE)):
        a.pack = ctx.d(PACK_SQLITE)
    dams = [x.split("#")[0].strip() for x in open(a.dams_file)]
    dams = [x for x in dams if x]
    if a.dam:
        unknown = [d for d in a.dam if d not in dams]
        if unknown:
            print("Not in {}: {} (running them anyway)".format(a.dams_file, unknown))
        dams = a.dam
    print("NickReview on {} | {} dam(s) | threads {} | data {} (read only) | out {}{}".format(
        platform.machine(), len(dams), a.threads, ctx.data, ctx.out,
        " | DRY RUN: nothing is run or written" if a.dry_run else ""))
    print("offline pack: {}".format(a.pack or "none (suite pack layers; OSM crossings from the "
                                             "Hazard pack setting/downloads)"))
    manifest = {"script": "nickreview_mac.py", "args": vars(a), "env": {
        k: os.environ.get(k) for k in ("NDBREACH_ANUGA_MESHER", "NDBREACH_DEM_RESAMPLE",
                                       "NDSUITE_SETTINGS", "QGIS_CUSTOM_CONFIG_PATH",
                                       "NDBREACH_LISFLOOD", "NDBREACH_ANUGA_PYTHON",
                                       "OMP_NUM_THREADS", "NDBREACH_ANUGA_THREADS")},
        "settings_rundam": RUNDAM_FIXED, "hazbatch": HAZBATCH_ARGS, "dams": []}
    if not a.dry_run:
        os.makedirs(ctx.out, exist_ok=True)
        manifest["versions"] = versions(a)
    worst = 0
    for dam in dams:
        ctx.problems = []
        run = Runner(ctx, dam)
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
        if not a.dry_run:
            ins = run.record.get("rundam_inputs") or {}
            run.record["checksums"] = {k: sha256(str(v).split("|")[0]) for k, v in ins.items()
                                       if isinstance(v, str) and os.path.isfile(v.split("|")[0])}
            for j in ("rundam.json", "hazclass.json"):
                run.record["checksums"][j] = sha256(os.path.join(run.w, j))
        manifest["dams"].append(run.record)
        worst = max(worst, rc)
        if rc == 130:
            break
    if a.list_data:
        common = [PACK_SQLITE, ctx.d(DWR_DAMS), ctx.d(WATER_MASK)] + [ctx.d(c) for c in CULVERT_SOURCES]
        common += [ctx.d(os.path.join(SUITE, f)) for f in SUITE_LAYERS.values()]
        if any(d not in KITS or a.no_kits for d in dams):  # prepare-mode dams read these
            ctx.used.update(f for f in common if os.path.isfile(f))
        for dam in dams:
            for f in (os.path.join(SCREEN_DAMS, dam, "dam.json"),):
                if os.path.isfile(ctx.d(f)):
                    ctx.used.add(ctx.d(f))
        total = 0
        with open(a.list_data, "w") as fo:
            fo.write("bytes\tbox_path\tcopy_to_under_data\n")
            for f in sorted(ctx.used):
                rel = ctx.rel_of(f)
                n = os.path.getsize(f)
                total += n
                fo.write("{}\t{}\t{}\n".format(n, f, rel))
        print("data list: {} files, {:.2f} GB -> {}".format(len(ctx.used), total / 1e9, a.list_data))
    if not a.dry_run:
        mp = os.path.join(ctx.out, "manifest_{}.json".format(now().replace(":", "")))
        json.dump(manifest, open(mp, "w"), indent=1, default=str)
        print("manifest: " + mp)
    summary = [(d["dam"], d.get("mode", "-"), d["rc"], len(d.get("problems", [])))
               for d in manifest["dams"]]
    print("\nsummary (dam, mode, exit, problems):")
    for s in summary:
        print("  {:10s} {:8s} {:3d} {}".format(*s))
    return 0 if worst == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
