"""1.5.0: "Build offline pack…" - download every layer of the ND dam suite's offline pack and
write it in BOTH formats from the same download:

  <output folder>/ND_Dam_Suite_Pack/      format 2: one GeoPackage per layer in theme folders
                                          (damsuite.gpkgpack), manifest.json, LAYERS.txt
  <output folder>/nd_offline_pack/        format 1: nd_offline_pack.sqlite (every vector layer)
                                          + manifest.json (the raster is not in format 1)
  <output folder>/ND_Dam_Suite_Pack_build/  the work file and downloads (resume; may be deleted
                                          once the pack is built)

The vector layers are fetched by the Hazard's pack engine (build_nd_pack.py, vendored here
byte-identical) into one resumable work database (format 1). That database is copied as the
format-1 pack and converted to format 2 with gpkgpack.convert_v1; the NLCD raster is added to
format 2 with gpkgpack.add_raster_file. Run again to resume: finished layers are kept, only
missing or failed ones are fetched (refresh=True fetches everything again).

Sources (PR #221 / #223): HIFLD only from user-supplied files (clipped to ND, left out without
a file - no other mirror; the reference pack's files are credited by checksum, others neutrally);
HPMS_ND_<year> kept next to NDDOT_traffic_counts for comparison; NHD_flowlines / NHD_waterbodies
filled from USGS 3DHP with NHD-style fields, read area by area (1.5.1: the NHD service fills
only the areas 3DHP cannot serve, hydro_src says which on every feature); the SARP Aquatic
Barrier Inventory as ABI_* (public API + national road-crossings download; removed / excluded
barriers kept and flagged in barrier_status); ND DEQ layers as NDDEQ_* (landfills, source water protection areas). What each
layer was built from (file / URL, sha256, data version, terms) is kept in the work folder's
provenance.json and copied into both manifests.

No QGIS import here (the dialog / task live in pack_dialog.py); GDAL is needed for the DWR and
local-file layers and for the raster. Command line (for testing):
    python3 -m DamSafety_Data.pack_build --out /tmp/x --bbox=-100.9,46.7,-100.6,46.9 --only ND_counties
"""
import datetime
import hashlib
import json
import math
import os
import shutil
import sqlite3
import time
import urllib.parse
import urllib.request
import zipfile

from . import build_nd_pack as BP
from . import constants as C
from . import dwr_files as DF
from . import offline_pack as OP
from .damsuite import gpkgpack as GP

PACK2 = GP.PACK_FOLDER                       # ND_Dam_Suite_Pack
PACK1 = "nd_offline_pack"
WORK = "ND_Dam_Suite_Pack_build"
MARK = "data_updater_build"                  # manifest key: this pack was built here (updatable)
PROV_NAME = "provenance.json"                # work folder: what each layer was built from
BUILDER = "DamSafety_Data pack_build 1.5.1"
ND_BBOX = OP.ND_BBOX
DEFAULT_PAD = OP.DEFAULT_PAD_DEG

ND_COUNTIES_URL = "https://ndgishub.nd.gov/arcgis/rest/services/All_GovtBoundaries/MapServer/20"
# HIFLD lifeline layers (1.5.0, PR #221): HIFLD Open is published on DataLumos / ICPSR (by the
# Data Rescue Project, https://portal.datarescueproject.org/) and on hsdl.org. DataLumos
# needs a (free) ICPSR login and its Terms of Use to download, so the builder cannot fetch them:
# download the file (GeoJSON or shapefile zip) and put it in the HIFLD folder of the build window.
# (name, label, DataLumos project, category, file-name keys, keys that rule a file out)
DATALUMOS = "https://www.datalumos.org/datalumos/project/{}"
HIFLD = (
    ("HIFLD_fire_ems_stations", "HIFLD fire and emergency medical service (EMS) stations",
     239161, "Fire station", ("fire", "ems"), ()),
    ("HIFLD_local_eoc", "HIFLD local emergency operations centers",
     239489, "Emergency operations center",
     ("eoc", "emergency_operations", "emergency-operations", "emergency operations"),
     ("state",)),
    ("HIFLD_local_law_enforcement", "HIFLD local law enforcement locations",
     239493, "Law enforcement", ("law", "police"), ()),
    # 239262 = "HIFLD OPEN Integrated Compliance Information System (ICIS) Wastewater Treatment
    # Plants" (the point layer; 240789 is a later copy of the same layer, also accepted).
    # 239290 / 239287 are its SIC / NAICS code tables (no points) - ruled out by name.
    ("HIFLD_wastewater_treatment_plants", "EPA ICIS wastewater treatment plants (HIFLD)",
     239262, "Wastewater treatment", ("wastewater",), ("sic", "naics")),
)
# 1.5.1: where a HIFLD file came from is known only for the files of the reference pack
# (ND_Dam_Suite_Pack/hifld, its manifest.json) - recognised by their sha256. Any other file is
# credited neutrally (the file and its checksum; the DataLumos project that publishes the layer
# as a pointer, not as a claim about where this copy came from).
_HSDL = "HIFLD (Homeland Infrastructure Foundation-Level Data) national shapefile from hsdl.org"
HIFLD_KNOWN = {
    "ef17c81b73a723daa9be38773aa6f8a35af2a8a17dba19ad15c295dd741c6a30": {
        "source": _HSDL + ", uploaded by Nick to the project (2026-10-06); clipped to North "
                          "Dakota + 0.1 degree (the reference pack's file)",
        "source_url": C.HIFLD_PORTAL_URL, "source_origin": "hsdl.org"},
    "9867bb5f83c654e65783e259cd2ed1837e0e9352fe3bd8f93d3025a70f59612d": {
        "source": _HSDL + ", uploaded by Nick to the project (2026-10-06); clipped to North "
                          "Dakota + 0.1 degree (the reference pack's file)",
        "source_url": C.HIFLD_PORTAL_URL, "source_origin": "hsdl.org"},
    "51f7c2114a2f0c51e288b162da00c3e5c91f8ead71cee19003f11926015db552": {
        "source": _HSDL + ", uploaded by Nick to the project (2026-10-06); clipped to North "
                          "Dakota + 0.1 degree (the reference pack's file)",
        "source_url": C.HIFLD_PORTAL_URL, "source_origin": "hsdl.org"},
    # the reference manifest says "DataLumos, project 239290, downloaded by Nick 2026-10-06";
    # DataLumos lists 239290 as the layer's SIC-code table (a 47 KB zip, no points) and 239262
    # as the point layer this file holds (375 ND points, NPDES_ID / CWP_* fields): flagged
    "a35916420f516b9f088c8b0b0c0779fe653a720cf7b04f55b9010d8ed4046e4b": {
        "source": "HIFLD Open ICIS wastewater treatment plants on DataLumos, downloaded by Nick "
                  "2026-10-06; 375 North Dakota points (NAME and CATEGORY fields added) (the "
                  "reference pack's file)",
        "source_url": DATALUMOS.format(239262), "source_origin": "DataLumos",
        "provenance_check": "the reference pack's manifest names DataLumos project 239290, "
                            "which DataLumos lists as the SIC-code table of this layer (no "
                            "points); the points are project 239262 - not confirmed which "
                            "download this file came from (sha256 recorded)"},
}
# ND Department of Environmental Quality layers published on the ND GIS Hub (public ArcGIS
# REST; DEQ's own ArcGIS Online services hold no facility points). DEQ does not publish
# wastewater / NDPDES facilities, public water system plants or intakes as GIS layers.
NDDEQ_HUB = "https://ndgishub.nd.gov/arcgis/rest/services/All_Locations/MapServer/"
NDDEQ = (
    ("NDDEQ_landfills", "ND DEQ permitted landfills (Division of Waste Management)", 12,
     "Selected landfills permitted in North Dakota; WasteType = the primary waste accepted"),
    ("NDDEQ_source_water_protection_community", "ND DEQ source water protection areas - "
     "community public water systems", 16, "Drinking-water source protection areas of "
     "community public water systems (PWS_NAME, PWS_TYPE, PWS_SOURCE); intakes not published"),
    ("NDDEQ_source_water_protection_noncommunity", "ND DEQ source water protection areas - "
     "non-community public water systems", 17, "Drinking-water source protection areas of "
     "non-community public water systems; intakes not published"),
)
# 3DHP (USGS 3D Hydrography Program) in place of NHD: the pack keeps the names NHD_flowlines /
# NHD_waterbodies (the Hazard format-1 reader, Setup's suite_fill and the Hazard / Runner
# culvert tools ask for those names); the features are 3DHP with the NHD-style fields added
# (damsuite.hydro3dhp). 1.5.1: read area by area (engine kind arcgis_tiled, 0.25 degree tiles,
# a tile that times out is split); only the areas 3DHP cannot serve are filled from the NHD
# service, every feature says which (HYDRO_SRC_FIELD), the manifest gives the counts per source
# and the areas filled / not read.
HYDRO = (
    ("NHD_flowlines", "USGS 3DHP flowlines (NHD fields added; NHD fills areas 3DHP cannot "
     "serve)", "nhd", "flowlines"),
    ("NHD_waterbodies", "USGS 3DHP waterbodies (NHD fields added; NHD fills areas 3DHP cannot "
     "serve)", "waterbodies", "waterbodies"),
)
HYDRO_SRC_FIELD = "hydro_src"                 # 1.5.1: "3DHP" or "NHD" on every hydro feature
HYDRO_TILE_DEG = 0.25
HYDRO_SPLIT_DEPTH = 4                         # 0.25 degree -> 1/64 degree at the deepest
HYDRO_CHUNK = 250                             # features per 3DHP request
# SARP / NACC National Aquatic Barrier Inventory (https://tool.aquaticbarriers.org): the public
# API (dams, surveyed small barriers: one state as CSV) and the national road-crossings download
# (unsurveyed road / stream crossings are not in the public API)
ABI_API = "https://tool.aquaticbarriers.org/api/v1/public"
ABI_ROAD_ZIP = "https://tool.aquaticbarriers.org/downloads/national/road_crossings.zip"
ABI_ROAD_MEMBER = "aquatic_barrier_ranks.csv"
ABI = (
    ("ABI_dams", "SARP Aquatic Barrier Inventory - dams (on- and off-network)",
     {"mode": "api", "kind": "dams"}),
    ("ABI_small_barriers_surveyed", "SARP Aquatic Barrier Inventory - surveyed road / stream "
     "crossings (small barriers)", {"mode": "api", "kind": "barriers"}),
    ("ABI_road_crossings_unsurveyed", "SARP Aquatic Barrier Inventory - unsurveyed road / "
     "stream crossings", {"mode": "zip", "kind": "road_crossings"}),
)
ABI_STATE, ABI_STATE_NAME = "ND", "North Dakota"
# every barrier is kept (removed / excluded ones too); barrier_status says which they are
ABI_NOTE = ("barrier_status = present / removed / excluded / removed+excluded (from the "
            "inventory's removed and excluded fields, both kept; removed: no longer a barrier, "
            "yearremoved gives the year; excluded: left out of SARP's network analysis). "
            "network = on-network / off-network.")
AADT_NOTE = ("AADT: both HPMS_ND_<year> (FHWA HPMS) and NDDOT_traffic_counts are in the pack, "
             "for comparison")
# ND DWR MapService downloads (qlay = the layer NAME; SpatiaLite keeps the full field names)
DWR_URL = "https://mapservice.dwr.nd.gov/php/downloadShape.php"
DWR_MAPFILE = "/Volumes/Data/WebServer/newswcprivate/mapfiles/swc.map"
DWR_EXTENT = "1026879.6641881 54774.288373765 2999091.3458676 1331032.7378794"
DWR_LAYERS = (
    ("DWR_dams", "Dams", "ND DWR dam inventory (DWR MapService)"),
    ("DWR_dam_lines", "Damsline", "ND DWR dam lines (DWR MapService)"),
    ("DWR_dugouts", "Dugouts", "ND DWR dugouts (DWR MapService)"),
    ("DWR_dugout_polygons", "DugoutsPoly", "ND DWR dugout polygons (DWR MapService)"),
)
NLCD_NAME = "NLCD_2021_landcover"
NLCD_LABEL = "NLCD 2021 land cover, North Dakota, 30 m (EPSG:5070)"
NLCD_WCS = "https://www.mrlc.gov/geoserver/mrlc_download/wcs"
NLCD_WCS_COVERAGE = "mrlc_download:NLCD_2021_Land_Cover_L48"
NLCD_ESRI = ("https://di-nlcd.img.arcgis.com/arcgis/rest/services/USA_NLCD_Annual_LandCover/"
             "ImageServer/exportImage")
NLCD_YEAR_MS = 1609459200000                 # 2021-01-01 (the Annual NLCD time axis: Year)
NLCD_TILE = 4000                             # pixels per side of one request
NLCD_ORIGIN = 15.0                           # the NLCD grid: x, y = 15 + 30 k (EPSG:5070)


class PackBuildError(Exception):
    pass


# ------------------------------------------------------------------ registry --
def _e(name, label, group, kind, spec, **info):
    return {"name": name, "label": label, "group": group, "kind": kind, "spec": spec,
            "info": info}


def registry(dwr_folder=None, hifld_folder=None, nlcd_file=None):
    """Every layer of the pack: [{name, label, group, kind, spec, info}]. kind: the engine's
    arcgis / nsi / osm / local_gpkg, or hydro (3DHP, NHD in the areas 3DHP cannot serve), abi
    (Aquatic Barrier Inventory CSV), ogr (a local vector file), dwr_download, nlcd (the raster), none (cannot
    be fetched: listed as left out). Same names as the reference ND_Dam_Suite_Pack, plus ABI_*
    and NDDEQ_*. Both AADT sources are kept: HPMS_ND_<year> (FHWA HPMS, newest year that
    answers) and NDDOT_traffic_counts, for comparison. A HIFLD layer without a file is left
    out (DataLumos page named)."""
    from .damsuite import hydro3dhp as H
    hydro = {h[0]: h for h in HYDRO}
    out = []
    for n, l, g, k, s in BP.datasets(None):
        if k == "first_of" and n == "HPMS_ND":
            out.append(_e(n, l, g, k, s, note=AADT_NOTE))
            continue
        if n == "NDDOT_traffic_counts":
            out.append(_e(n, l, g, k, s, note=AADT_NOTE))
            continue
        if n in hydro:
            _n, label, group, which = hydro[n]
            flow = which == "flowlines"
            out.append(_e(n, label, group, "hydro", {
                "url": H.FLOWLINE_URL if flow else H.WATERBODY_URL,
                "fields": H.FLOW_FIELDS if flow else H.WB_FIELDS, "precision": 6,
                "which": which, "fallback": s}, hydro_3dhp_name=H.ALIASES.get(n),
                source="USGS 3D Hydrography Program (3DHP_all MapServer layer {}), NHD "
                       "fields added".format(H.FLOWLINE_LAYER if flow else H.WATERBODY_LAYER)))
            continue
        out.append(_e(n, l, g, k, s))
    out.append(_e("ND_counties", "County boundaries (ND GIS Hub counties)", "boundaries",
                  "arcgis", {"url": ND_COUNTIES_URL}, source="ND GIS Hub county boundaries"))
    files = _hifld_files(hifld_folder)
    for name, label, proj, cat, _keys, _no in HIFLD:
        dl = DATALUMOS.format(proj)
        if name in files:
            # 1.5.1: no claim about where the file came from (see HIFLD_KNOWN, _file_prov)
            out.append(_e(name, label, "hifld", "ogr", {"path": files[name], "state": "ND"},
                          category=cat, source_page=dl,
                          source="HIFLD file supplied for the build: {} (where this copy came "
                                 "from is not recorded; HIFLD Open publishes the layer on "
                                 "DataLumos as project {})".format(os.path.basename(
                                     files[name].replace("/vsizip/", "")), proj)))
        else:
            out.append(_e(name, label, "hifld", "none", {}, category=cat, why=(
                "HIFLD Open is kept by the Data Rescue Project on DataLumos, which needs a free "
                "ICPSR login and its Terms of Use to download: download the GeoJSON or "
                "shapefile zip from {} and put it in the HIFLD folder of the build "
                "window".format(dl))))
    for name, label, lid, note in NDDEQ:
        out.append(_e(name, label, "nddeq", "arcgis", {"url": NDDEQ_HUB + str(lid)}, note=note,
                      source="ND Department of Environmental Quality, via the ND GIS Hub "
                             "(All_Locations layer {})".format(lid)))
    for name, label, spec in ABI:
        out.append(_e(name, label, "barriers", "abi", dict(spec), category="aquatic barrier",
                      note=ABI_NOTE,
                      source="SARP / NACC National Aquatic Barrier Inventory ({})".format(
                          "public API" if spec["mode"] == "api" else
                          "national road-crossings download")))
    local = {}
    if dwr_folder:
        try:
            local = DF.find_layers(os.path.abspath(dwr_folder))
        except DF.DwrFileError as e:
            raise PackBuildError(str(e))
    for name, slot, label, what in DF.DATASETS:
        if name in local:
            path, layer = local[name]
            out.append(_e(name, label, "dwr", "local_gpkg", {"path": path, "layer": layer,
                          "slot": slot, "what": what}, source="ND DWR file " +
                          os.path.basename(path)))
            continue
        dl = [d for d in DWR_LAYERS if d[0] == name]
        if dl:
            out.append(_e(name, dl[0][2], "dwr", "dwr_download", {"qlay": dl[0][1]},
                          source="ND DWR MapService download ({})".format(dl[0][1])))
        else:
            out.append(_e(name, label, "dwr", "none", {}, why=(
                "not published by the DWR MapService - put DWR_Unknown_Structures.gpkg in "
                "the DWR folder of the build window")))
    out.append(_e(NLCD_NAME, NLCD_LABEL, GP.RASTER_GROUP, "nlcd",
                  {"path": nlcd_file} if nlcd_file else {}))
    return out


def layer_names(table=None):
    return [t["name"] for t in (table or registry())]


def _hifld_files(folder):
    """{HIFLD name: file} for the vector files of `folder` whose name says what they are (the
    DataLumos downloads: <layer>-geojson.zip / -shapefile.zip, or an unpacked file)."""
    if not folder or not os.path.isdir(folder):
        return {}
    exts = (".gpkg", ".shp", ".geojson", ".json", ".zip", ".sqlite")
    out = {}
    for fn in sorted(os.listdir(folder)):
        low = fn.lower()
        if not low.endswith(exts) or low == "metadata.json":
            continue
        for name, _l, _p, _c, keys, no in HIFLD:
            if name not in out and any(k in low for k in keys) and \
                    not any(k in low for k in no):
                p = os.path.join(folder, fn)
                out[name] = ("/vsizip/" + p) if low.endswith(".zip") else p
    return out


def wastewater_fields(f):
    """The reference pack's NAME / CATEGORY on the EPA wastewater points (the HIFLD lifeline
    reader takes the name from NAME)."""
    p = f.setdefault("properties", {})
    if not p.get("NAME"):
        p["NAME"] = p.get("CWP_NAME") or p.get("FAC_NAME") or p.get("name") or ""
    p.setdefault("CATEGORY", "Wastewater treatment")
    return f


TRANSFORMS = {"HIFLD_wastewater_treatment_plants": wastewater_fields}


# ------------------------------------------------------------- safe target --
def _norm(p):
    return os.path.normcase(os.path.realpath(os.path.expanduser(p)))


def _inside(a, b):
    a, b = _norm(a), _norm(b)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def _is_pack_folder(p):
    """settings.in_pack, without importing QGIS."""
    from .settings import in_pack
    return in_pack(p)


def check_target(out_parent, active=()):
    """Refuse an output folder that is (inside) a pack, or that holds a pack this builder did
    not make, or a pack the plugins are reading now (`active`: pack paths). Returns the three
    folders (pack2, pack1, work)."""
    if not out_parent:
        raise PackBuildError("Choose an output folder first.")
    out_parent = os.path.abspath(os.path.expanduser(out_parent))
    if _is_pack_folder(out_parent):
        raise PackBuildError("{} is an offline pack (or inside one). Choose an ordinary folder: "
                             "a new {} folder is made in it.".format(out_parent, PACK2))
    p2, p1, wk = (os.path.join(out_parent, x) for x in (PACK2, PACK1, WORK))
    for p in (p2, p1):
        if os.path.exists(p):
            m = _read_json(os.path.join(p, GP.MANIFEST_NAME))
            if not (isinstance(m, dict) and m.get(MARK)):
                raise PackBuildError("{} already holds a pack that was not built here. Choose "
                                     "another (empty) output folder - an existing pack is "
                                     "never written into.".format(p))
    for a in active or ():
        if a and (_inside(a, p2) or _inside(a, p1)):
            raise PackBuildError("The plugins are reading the pack in {} now. Choose another "
                                 "output folder, or point the plugins at another pack "
                                 "first.".format(out_parent))
    return p2, p1, wk


def _read_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def nd_bbox(pad=DEFAULT_PAD):
    return (ND_BBOX[0] - pad, ND_BBOX[1] - pad, ND_BBOX[2] + pad, ND_BBOX[3] + pad)


# -------------------------------------------------------------------- build --
def build(out_parent, only=None, bbox=None, refresh=False, dwr_folder=None, hifld_folder=None,
          nlcd_file=None, active=(), workers=None, progress=None, log=None, cancel=None,
          area="North Dakota"):
    """Build or resume the pack in out_parent (see the module text). only: layer names (None =
    all pack layers). Returns a summary dict {pack2, pack1, manifest2, manifest1, failed,
    left_out}."""
    log = log or print
    progress = progress or (lambda p, m="": None)
    cancel = cancel or (lambda: False)
    p2, p1, wk = check_target(out_parent, active)
    bbox = tuple(float(v) for v in (bbox or nd_bbox()))
    table = registry(dwr_folder, hifld_folder, nlcd_file)
    known = set(t["name"] for t in table)
    want = set(only) if only else None
    if want:
        bad = want - known
        if bad:
            raise PackBuildError("unknown layer(s): " + ", ".join(sorted(bad)))
    todo = [t for t in table if want is None or t["name"] in want]
    os.makedirs(wk, exist_ok=True)
    db = os.path.join(wk, OP.DB_NAME)
    prov = _read_json(os.path.join(wk, PROV_NAME)) or {}
    log("Offline pack for {} - area {:.4f}, {:.4f} to {:.4f}, {:.4f} (lon / lat)".format(
        area, *bbox))
    log("Output: {} and {}".format(p2, p1))
    try:
        writer = OP.PackWriter(db, bbox, area=area, created_by="{} / engine {}".format(
            BUILDER, BP.BUILDER_VERSION))
    except OP.PackError as e:
        raise PackBuildError("{} (delete the {} folder to start again)".format(e, WORK))
    workers = max(1, int(workers or C.DOWNLOAD_WORKERS))
    failed, left = {}, {}
    vec = [t for t in todo if t["kind"] not in ("nlcd", "none")]
    n = max(1, len(todo))
    BP._CANCEL.clear()
    try:
        for i, t in enumerate(vec):
            if cancel():
                raise BP.BuildCancelled("cancelled")
            lo, hi = 90.0 * i / n, 90.0 * (i + 1) / n
            name, label, kind = t["name"], t["label"], t["kind"]
            names = [c[0] for c in t["spec"]["choices"]] if kind == "first_of" else [name]
            if kind != "local_gpkg" and not refresh and any(writer.done(x) for x in names):
                log("[{}/{}] {}: already downloaded (kept)".format(i + 1, len(vec), label))
                continue
            log("[{}/{}] {}".format(i + 1, len(vec), label))

            def sub(p, m="", lo=lo, hi=hi, label=label):
                if cancel():
                    BP._CANCEL.set()
                    raise BP.BuildCancelled("cancelled")
                progress(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0,
                         "{}: {}".format(label, m) if m else label)

            def beat(text, lo=lo, label=label):
                if cancel():
                    BP._CANCEL.set()
                progress(lo, "{}: {}".format(label, text))
            ctx = BP._Ctx(writer, workers, log, sub, beat, refresh)
            t0 = time.time()
            try:
                if kind in ("ogr", "dwr_download"):
                    got = _run_file(ctx, t, bbox, wk, refresh)
                    prov[name] = _file_prov(t, getattr(ctx, "file_path", ""))
                elif kind == "hydro":
                    got, prov[name] = _run_hydro(ctx, t, bbox)
                elif kind == "abi":
                    got, prov[name] = _run_abi(ctx, t, bbox, wk, refresh)
                else:
                    got = BP._run_one(ctx, name, label, kind, t["spec"], bbox)
                    if kind == "local_gpkg" and getattr(ctx, "source_date", None):
                        # 1.5.1: the file's own date (not the day it was copied here)
                        prov[name] = {"source_date": ctx.source_date[0],
                                      "source_date_from": ctx.source_date[1]}
                if name in TRANSFORMS:
                    _rewrite(writer.con, name, TRANSFORMS[name])
                log("  done: {:,} features in {:.0f} s".format(got, time.time() - t0))
            except (OP.PackError, BP.BuildCancelled):
                raise
            except BP._NET_ERRORS + (PackBuildError, RuntimeError, ValueError) as e:
                failed[name] = str(e)
                log("  FAILED: {}".format(e))
            finally:
                _write_json(os.path.join(wk, PROV_NAME), prov)
        man1 = writer.manifest()
    finally:
        writer.close(finalize=False)
    for t in todo:
        if t["kind"] == "none":
            left[t["name"]] = t["info"].get("why") or "not available"
    infos = {t["name"]: dict(t["info"]) for t in table}
    for t in table:
        infos[t["name"]].pop("why", None)
        infos[t["name"]].update(prov.get(t["name"]) or {})
        if t["kind"] == "first_of":                     # HPMS_ND -> HPMS_ND_<year>
            for c in t["spec"]["choices"]:
                infos.setdefault(c[0], dict(infos[t["name"]]))
    if cancel():
        raise BP.BuildCancelled("cancelled")
    progress(90.0, "Writing nd_offline_pack (format 1)\u2026")
    man1 = write_format1(db, p1, man1, infos, left, log)
    progress(93.0, "Writing ND_Dam_Suite_Pack (format 2)\u2026")
    man2 = write_format2(os.path.join(p1, OP.DB_NAME), p2, man1, infos, left, refresh, log,
                         cancel)
    nl = [t for t in todo if t["kind"] == "nlcd"]
    if nl:
        progress(96.0, "Land cover raster\u2026")
        try:
            _nlcd(p2, man2, nl[0], bbox, wk, refresh, log, cancel)
        except BP.BuildCancelled:
            raise
        except Exception as e:                                   # noqa: BLE001 - reported
            failed[NLCD_NAME] = str(e)
            if NLCD_NAME not in man2.get("rasters", {}):
                man2.setdefault("left_out", {})[NLCD_NAME] = "download failed: {}".format(e)
            log("  NLCD FAILED: {}".format(e))
        _finish2(p2, man2)
    man1 = nlcd_format1(p1, p2, man1, man2, log)      # 1.5.2: the raster in format 1 too
    progress(100.0, "Offline pack written")
    summary(man2, log)
    return {"pack2": p2, "pack1": p1, "manifest2": man2, "manifest1": man1, "failed": failed,
            "left_out": dict(man2.get("left_out") or {})}


# ---------------------------------------------------------- local / DWR files --
def _run_file(ctx, t, bbox, wk, refresh):
    """A vector file (ogr) or a DWR MapService download into the work database."""
    name, label, spec = t["name"], t["label"], t["spec"]
    if t["kind"] == "dwr_download":
        path, url = dwr_download(spec["qlay"], os.path.join(wk, "downloads"), refresh,
                                 ctx.log)
    else:
        path, url = spec["path"], spec["path"]
    ctx.file_path = path
    w = ctx.writer
    w.begin(name, label, url, resume=False, signature="file|{}".format(path))
    try:
        n, skipped = 0, 0
        batch = []
        for f in read_vector(path, bbox, state=spec.get("state")):
            batch.append(f)
            if len(batch) >= 2000:
                w.add(batch, _uid)
                n += len(batch)
                batch = []
                ctx.sub(50, "{:,} features".format(n))
        if batch:
            w.add(batch, _uid)
    except Exception as e:
        w.fail(name, str(e))
        raise PackBuildError("{}: {}".format(os.path.basename(path), e))
    return w.finish("from {}".format(os.path.basename(path.replace("/vsizip/", ""))))


def _write_json(p, obj):
    try:
        with open(p + ".part", "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
        os.replace(p + ".part", p)
    except OSError:
        pass


def _real_file(path):
    """The file on disk behind a GDAL path (/vsizip/a.zip/inner -> a.zip)."""
    p = path.replace("/vsizip/", "", 1) if path.startswith("/vsizip/") else path
    while p and not os.path.isfile(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return p if os.path.isfile(p) else ""


def _file_prov(t, path):
    """Manifest note of a layer read from a file: the file, its date and sha256 (and the
    DataLumos page of a HIFLD file). 1.5.1: the date is the file's own (GeoPackage last
    change; else its modified time - source_date_from says which), and a HIFLD file of the
    reference pack (HIFLD_KNOWN, by sha256) gets its recorded source."""
    f = _real_file(path or t["spec"].get("path") or "")
    out = {}
    if t["info"].get("source_page"):
        out["source_url"] = t["info"]["source_page"]
    if f:
        sha = _sha256(f)
        day, how = DF.source_date(f) if f.lower().endswith(".gpkg") else (
            datetime.date.fromtimestamp(os.path.getmtime(f)).isoformat(), "file modified")
        out.update(source_file=os.path.basename(f), source_sha256=sha, source_date=day,
                   source_date_from=how)
        if t.get("group") == "hifld":
            known = HIFLD_KNOWN.get(sha)
            if known:
                out.update(known)
            else:
                out["source_origin"] = "not recorded"
    return out


# --------------------------------------------------------------- 3DHP / NHD --
def _hydro_spec(spec):
    """1.5.1: the engine spec (arcgis_tiled) of a hydro layer: 3DHP, filled from NHD."""
    fb = spec["fallback"]
    return {"url": spec["url"], "fields": spec["fields"], "precision": spec.get("precision"),
            "tag": "3DHP", "source_field": HYDRO_SRC_FIELD, "tile_deg": HYDRO_TILE_DEG,
            "split_depth": HYDRO_SPLIT_DEPTH, "chunk": HYDRO_CHUNK,
            "fill": {"url": fb["url"], "fields": fb.get("fields", "*"),
                     "precision": fb.get("precision"), "tag": "NHD",
                     "label": "NHD fill ({})".format(spec.get("which", "hydro"))}}


def _run_hydro(ctx, t, bbox):
    """3DHP flowlines / waterbodies under the NHD name (NHD fields added). 1.5.1: read area by
    area - the 3DHP features read are kept and only the areas 3DHP cannot serve (ids not listed
    after splitting the tile, HTTP 504 / time-outs; features not read) are filled from the NHD
    service (one source per place, tile edges not doubled). Returns (count, note)."""
    from .damsuite import hydro3dhp as H
    name, label, spec = t["name"], t["label"], t["spec"]
    norm = H.normalise_flowline if spec["which"] == "flowlines" else H.normalise_waterbody
    ctx.tiled = None
    n = BP._run_one(ctx, name, label, "arcgis_tiled", _hydro_spec(spec), bbox)
    return n, _hydro_note(ctx.writer.con, name, norm, spec["url"],
                          getattr(ctx, "tiled", None), spec["fallback"]["url"])


def _hydro_note(con, name, norm, url, rep=None, nhd_url=None):
    """Adds the NHD fields to the stored 3DHP features (not to the NHD ones filling areas) and
    says where the features come from: per source (hydro_src) and, for 3DHP, per work unit
    ('NHD' = migrated from NHD, else elevation-derived hydrography). rep: the engine's per-area
    report (arcgis_tiled)."""
    units = {}
    per = {"3DHP": 0, "NHD": 0}

    def fn(f):
        p = f.get("properties") or {}
        if p.get(HYDRO_SRC_FIELD) == "NHD":
            per["NHD"] += 1
            return f
        f = norm(f)
        p = f.get("properties")
        if isinstance(p, dict):
            p[HYDRO_SRC_FIELD] = "3DHP"
        else:
            p = f["properties"] = {HYDRO_SRC_FIELD: "3DHP"}
        per["3DHP"] += 1
        u = str(p.get("workunitid") or p.get("WORKUNITID") or "unknown")
        units[u] = units.get(u, 0) + 1
        return f
    _rewrite(con, name, fn)
    nhd = units.get("NHD", 0)
    rep = rep or {}
    src = "3DHP" if not per["NHD"] else ("NHD" if not per["3DHP"] else "3DHP+NHD")
    fill = rep.get("fill_areas") or []
    gaps = rep.get("gap_areas") or []
    out = {"hydro_source": src, "source_url": url, HYDRO_SRC_FIELD + "_field":
           "{} = 3DHP or NHD on every feature".format(HYDRO_SRC_FIELD),
           "count_3dhp": per["3DHP"], "count_nhd": per["NHD"],
           "coverage_3dhp": {"features_by_workunit": units, "nhd_migrated": nhd,
                             "elevation_derived": sum(units.values()) - nhd},
           "fields_mapped": "gnis_name <- gnisidlabel, gnis_id <- gnisid, ftype / fcode <- "
                            "featuretypelabel (fcode = ftype x 100), featuredate as a date; "
                            "the 3DHP fields are kept (id3dhp, mainstemid, workunitid ...); "
                            "no reachcode / permanent_identifier in 3DHP"}
    if rep:
        out["areas"] = {"tiles": rep.get("tiles"), "tile_deg": HYDRO_TILE_DEG,
                        "not_from_3dhp": len(fill), "filled_from_nhd": rep.get(
                            "fill_areas_read", 0), "not_read": len(gaps),
                        "failed_3dhp_areas": fill, "gap_areas": gaps}
    if fill:
        why = "3DHP: {}".format(rep.get("why") or "{} area(s) not read".format(len(fill)))
        out["fallback_reason"] = "{} - {} of {} area(s) filled from NHD{}".format(
            why, rep.get("fill_areas_read", 0), len(fill),
            "; {} area(s) read by neither service".format(len(gaps)) if gaps else "")
        out["nhd_url"] = nhd_url
        if src == "NHD":
            out["source"] = "USGS NHD ({}) - 3DHP not available in any area".format(nhd_url)
        else:
            out["source"] = ("USGS 3DHP ({:,} features, NHD fields added) with USGS NHD in the "
                             "{} area(s) 3DHP could not serve ({:,} features)".format(
                                 per["3DHP"], len(fill), per["NHD"]))
    return out


# --------------------------------------------------- Aquatic Barrier Inventory --
def _download(url, dest, refresh, log, max_age=86400):
    """url into the file dest (kept for max_age seconds; again with refresh). Returns
    (dest, downloaded now)."""
    if not refresh and os.path.isfile(dest) and time.time() - os.path.getmtime(dest) < max_age:
        return dest, False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": BP._ua()})
    n = 0
    with urllib.request.urlopen(req, timeout=BP.TIMEOUT_S) as r, open(dest + ".part", "wb") as f:
        for b in iter(lambda: r.read(1 << 20), b""):
            f.write(b)
            n += len(b)
    os.replace(dest + ".part", dest)
    if log:
        log("  {}: {:,} bytes".format(url.split("?")[0], n))
    return dest, True


def _abi_meta(kind, dest, refresh, log):
    """The public API's metadata (data_version, data_publish_date, citation, terms_of_use)."""
    try:
        p, _new = _download("{}/{}/metadata".format(ABI_API, kind),
                            os.path.join(dest, "{}_metadata.json".format(kind)), refresh, log)
        m = _read_json(p) or {}
    except BP._NET_ERRORS + (OSError, ValueError) as e:
        if log:
            log("  ABI metadata not read ({})".format(e))
        m = {}
    return {k: m.get(k) for k in ("data_version", "data_publish_date", "citation",
                                  "terms_of_use") if m.get(k)}


def _readme_meta(text):
    out = {}
    for line in text.splitlines():
        if line.startswith("Data version:"):
            out["data_version"] = line.split(":", 1)[1].strip()
        elif line.startswith("Data published:"):
            out["data_publish_date"] = line.split(":", 1)[1].strip()
    return out


def abi_rows(t, dest, refresh=False, log=None):
    """(header, rows, provenance) of one Aquatic Barrier Inventory layer for North Dakota.
    api: <ABI_API>/<kind>/state?id=ND (CSV). zip: the national road-crossings download, its
    North Dakota rows kept (cached as a small CSV) and only the unsurveyed ones (Surveyed =
    'not likely'; 'yes' / 'likely' are the surveyed small barriers)."""
    import csv
    import io
    spec = t["spec"]
    kind = spec["kind"]
    if spec["mode"] == "api":
        url = "{}/{}/state?id={}".format(ABI_API, kind, ABI_STATE)
        p, _new = _download(url, os.path.join(dest, "{}_{}.csv".format(kind, ABI_STATE)),
                            refresh, log)
        with open(p, encoding="utf-8", newline="") as f:
            rd = csv.reader(f)
            head = [h.strip().lower() for h in next(rd)]
            rows = [r for r in rd if r]
        prov = {"source_url": url, "source_sha256": _sha256(p),
                "source_date": datetime.date.fromtimestamp(os.path.getmtime(p)).isoformat(),
                "filter": "state = {} (public API)".format(ABI_STATE)}
        prov.update(_abi_meta(kind, dest, refresh, log))
        return head, rows, prov
    zp, new = _download(ABI_ROAD_ZIP, os.path.join(dest, "road_crossings.zip"), refresh, log,
                        max_age=7 * 86400)
    cache = os.path.join(dest, "road_crossings_{}.csv".format(ABI_STATE))
    with zipfile.ZipFile(zp) as z:
        if new or refresh or not os.path.isfile(cache) or \
                os.path.getmtime(cache) < os.path.getmtime(zp):
            with z.open(ABI_ROAD_MEMBER) as raw, open(cache + ".part", "w", encoding="utf-8",
                                                      newline="") as out:
                lines = io.TextIOWrapper(raw, encoding="utf-8", newline="")
                wr = csv.writer(out)
                head = next(csv.reader([next(lines)]))
                wr.writerow(head)
                si = head.index("State")
                key = '"{}"'.format(ABI_STATE_NAME)
                # most of the 6 GB file is other states: a cheap text test first
                for line in lines:
                    if key in line:
                        r = next(csv.reader([line]))
                        if len(r) == len(head) and r[si] == ABI_STATE_NAME:
                            wr.writerow(r)
            os.replace(cache + ".part", cache)
        meta = _readme_meta(z.read("README.txt").decode("utf-8", "replace"))
        terms = z.read("TERMS_OF_USE.txt").decode("utf-8", "replace") \
            if "TERMS_OF_USE.txt" in z.namelist() else ""
    with open(cache, encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        head = [h.strip().lower() for h in next(rd)]
        si = head.index("surveyed")
        rows = [r for r in rd if r and r[si] == "not likely"]
    prov = {"source_url": ABI_ROAD_ZIP, "source_sha256": _sha256(zp),
            "source_file": "{} / {}".format(os.path.basename(zp), ABI_ROAD_MEMBER),
            "source_date": datetime.date.fromtimestamp(os.path.getmtime(zp)).isoformat(),
            "filter": "State = {}, Surveyed = 'not likely' (unsurveyed)".format(ABI_STATE_NAME)}
    prov.update(meta)
    if terms:
        prov["terms_of_use"] = terms.split("By downloading", 1)[-1].split("For your", 1)[0]\
            .strip()
        prov["terms_of_use"] = "By downloading " + prov["terms_of_use"]
        if "Citation:" in terms:
            prov["citation"] = terms.split("Citation:", 1)[1].strip("-\n ").strip()
    return head, rows, prov


def _numeric_columns(head, rows):
    """Columns whose every value is a number (no leading zero, so HUC codes stay text)."""
    import re
    num = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$")
    out = set()
    for i, h in enumerate(head):
        vals = [r[i] for r in rows if i < len(r) and r[i] != ""]
        if vals and all(num.match(v) for v in vals):
            out.add(i)
    return out


def _yes(v):
    return str(v).strip().lower() in ("yes", "true", "1", "y")


def barrier_status(props):
    """present / removed / excluded / removed+excluded from the inventory's removed and
    excluded fields (any case; missing = no)."""
    low = {k.lower(): v for k, v in props.items()}
    flags = [k for k in ("removed", "excluded") if _yes(low.get(k))]
    return "+".join(flags) if flags else "present"


def abi_features(head, rows, bbox):
    """Points (lon / lat) of the rows inside bbox, every column kept (numbers as numbers),
    plus network = on-network / off-network (from hasnetwork / onnetwork) and barrier_status
    (present / removed / excluded / removed+excluded; no row is dropped)."""
    la, lo = head.index("lat"), head.index("lon")
    nk = head.index("hasnetwork") if "hasnetwork" in head else \
        head.index("onnetwork") if "onnetwork" in head else -1
    nums = _numeric_columns(head, rows)
    feats = []
    for r in rows:
        try:
            x, y = float(r[lo]), float(r[la])
        except (ValueError, IndexError):
            continue
        if not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
            continue
        props = {}
        for i, h in enumerate(head):
            if i in (la, lo) or i >= len(r):
                continue
            v = r[i]
            if i in nums and v != "":
                v = float(v) if any(c in v for c in ".eE") else int(v)
            elif v == "":
                v = None
            props[h] = v
        if nk >= 0:
            on = str(r[nk]).strip().lower() in ("yes", "true", "1")
            props["network"] = "on-network" if on else "off-network"
        props["barrier_status"] = barrier_status(props)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [
            round(x, 6), round(y, 6)]}, "properties": props})
    return feats


def _run_abi(ctx, t, bbox, wk, refresh):
    name, label = t["name"], t["label"]
    head, rows, prov = abi_rows(t, os.path.join(wk, "downloads", "abi"), refresh, ctx.log)
    feats = abi_features(head, rows, bbox)
    w = ctx.writer
    w.begin(name, label, prov["source_url"], resume=False,
            signature="abi|{}|{}".format(prov.get("source_sha256"), prov.get("filter")))
    try:
        for i in range(0, len(feats), 2000):
            w.add(feats[i:i + 2000], lambda f: "sarpid:{}".format(
                f["properties"].get("sarpid") or id(f)))
            ctx.sub(50, "{:,} features".format(min(len(feats), i + 2000)))
    except Exception as e:
        w.fail(name, str(e))
        raise PackBuildError("{}: {}".format(name, e))
    counts = {}
    for f in feats:
        p = f["properties"]
        for k in ("network", "barrier_status"):
            if k in p:
                key = "{}={}".format(k, p[k])
                counts[key] = counts.get(key, 0) + 1
    prov["counts"] = counts
    prov["network_field"] = ("network: on-network / off-network (from the inventory's "
                             "hasnetwork / onnetwork); one layer, not two")
    prov["flag_field"] = ("barrier_status: present / removed / excluded / removed+excluded "
                          "(removed and excluded barriers are kept, not dropped)")
    n = w.finish("{} (data version {})".format(prov.get("filter"), prov.get("data_version")))
    return n, prov


def _uid(f):
    return DF.uid_of(f) or None


STATE_FIELDS = ("state", "state_abbr", "stusps", "st", "state_code", "fac_state", "statename",
                "state_name")


def _other_state(props, state):
    """True when the feature's own state field names another state than `state` (two
    letters; the full name of ND is also understood). No state field: False."""
    names = {state.upper(), {"ND": "NORTH DAKOTA"}.get(state.upper(), state.upper())}
    for k, v in props.items():
        if k.lower() in STATE_FIELDS and isinstance(v, str) and v.strip():
            return v.strip().upper() not in names
    return False


def read_vector(path, bbox, state=None):
    """GeoJSON features (lon / lat, every attribute) of the first layer of a vector file,
    those touching bbox (and, with state, those whose state field - if any - is that state:
    a national HIFLD file clipped to North Dakota)."""
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ds = ogr.Open(path)
    if ds is None:
        raise PackBuildError("{} could not be opened".format(path))
    lyr = ds.GetLayer(0)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    srs = lyr.GetSpatialRef()
    tr = None
    if srs is not None:
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not srs.IsSame(dst):
            tr = osr.CoordinateTransformation(srs, dst)
    defn = lyr.GetLayerDefn()
    fnames = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    for feat in lyr:
        g = feat.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone()
        g.FlattenTo2D()
        if tr is not None:
            g.Transform(tr)
        geom = json.loads(g.ExportToJson())
        if not OP.intersects_rect(geom, bbox):
            continue
        props = {"fid": feat.GetFID()}
        for i, nm in enumerate(fnames):
            v = feat.GetField(i)
            if isinstance(v, (datetime.date, datetime.datetime)):
                v = v.isoformat()
            props[nm] = v
        if state and _other_state(props, state):
            continue
        yield {"type": "Feature", "geometry": geom, "properties": props}


def dwr_download(qlay, dest, refresh=False, log=None):
    """One ND DWR MapService layer as SpatiaLite (full field names), statewide, NAD83. Kept
    in dest (downloaded again with refresh, or when older than a day). Returns (path, url)."""
    os.makedirs(dest, exist_ok=True)
    zp = os.path.join(dest, "DWR_{}.zip".format(qlay))
    fresh = os.path.isfile(zp) and time.time() - os.path.getmtime(zp) < 86400
    if refresh or not fresh:
        data = urllib.parse.urlencode({
            "whichextent": "all", "srid": "4269", "downloadformat": "spat", "qlay": qlay,
            "mapext": DWR_EXTENT, "map": DWR_MAPFILE}).encode()
        req = urllib.request.Request(DWR_URL, data=data, headers={"User-Agent": BP._ua()})
        with urllib.request.urlopen(req, timeout=BP.TIMEOUT_S) as r:
            raw = r.read()
        if not raw.startswith(b"PK"):
            raise PackBuildError("the DWR MapService sent no file for {} ({} bytes)".format(
                qlay, len(raw)))
        with open(zp + ".part", "wb") as f:
            f.write(raw)
        os.replace(zp + ".part", zp)
        if log:
            log("  DWR {}: {:,} bytes".format(qlay, len(raw)))
    with zipfile.ZipFile(zp) as z:
        inner = next((n for n in z.namelist() if n.lower().endswith((".sqlite", ".shp"))), None)
    if not inner:
        raise PackBuildError("{} holds no SpatiaLite / shapefile".format(zp))
    return "/vsizip/{}/{}".format(zp, inner), "{} (qlay={})".format(DWR_URL, qlay)


def _rewrite(con, name, fn):
    """Apply fn(feature) -> feature to every row of data set `name` of the work db."""
    import zlib
    tbl = OP._table(name)
    rows = con.execute("SELECT fid, feat FROM {}".format(tbl)).fetchall()
    con.executemany("UPDATE {} SET feat=? WHERE fid=?".format(tbl), [
        (zlib.compress(json.dumps(fn(json.loads(zlib.decompress(b).decode("utf-8"))),
                                  separators=(",", ":")).encode("utf-8"), 6), fid)
        for fid, b in rows])
    con.commit()


# ------------------------------------------------------------------ format 1 --
def write_format1(db, p1, man, infos, left, log):
    """nd_offline_pack/nd_offline_pack.sqlite (a clean, indexed copy of the work file) and its
    manifest.json (1.5.2: the NLCD raster is added beside it by nlcd_format1)."""
    os.makedirs(p1, exist_ok=True)
    dst = os.path.join(p1, OP.DB_NAME)
    tmp = dst + ".part"
    OP.write_clean_copy(db, tmp)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    con = sqlite3.connect(tmp)
    try:
        OP._index_all(con)
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('created', ?)", (stamp,))
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    os.replace(tmp, dst)
    man = dict(man)
    man["created"] = stamp
    for k, v in man["datasets"].items():
        for key, val in (infos.get(k) or {}).items():
            if val not in (None, "", {}, []):
                v[key] = val
    man[MARK] = BUILDER
    man["left_out"] = dict(left)
    man.pop("rasters_not_included", None)
    old = _read_json(os.path.join(p1, OP.MANIFEST_NAME)) or {}
    man["rasters"] = {k: e for k, e in (old.get("rasters") or {}).items()   # kept till re-copied
                      if os.path.isfile(os.path.join(p1, *str(e.get("file", "")).split("/")))}
    man["sha256"] = _sha256(dst)
    with open(os.path.join(p1, OP.MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2, sort_keys=True)
    log("Format 1: {} ({:.1f} MB)".format(dst, os.path.getsize(dst) / 1048576.0))
    return man


def nlcd_format1(p1, p2, man1, man2, log=None):
    """1.5.2: the format-2 pack's NLCD land cover GeoPackage copied into the format-1 pack
    (nd_offline_pack/landcover/NLCD_2021_landcover.gpkg, the same file) and listed in its
    manifest "rasters", so the format-1 pack holds every layer the plugins read. A copy with
    the same size and sha256 is kept. Without the raster in format 2 the format-1 manifest
    names it as not included. Returns the format-1 manifest (rewritten)."""
    log = log or (lambda m: None)
    man1 = dict(man1)
    e2 = (man2.get("rasters") or {}).get(NLCD_NAME) or {}
    src = os.path.join(p2, *e2["file"].split("/")) if e2.get("file") else ""
    rasters = dict(man1.get("rasters") or {})
    if src and os.path.isfile(src):
        dst = os.path.join(p1, *e2["file"].split("/"))
        e1 = {k: v for k, v in e2.items() if not k.startswith("_")}
        size = os.path.getsize(src)
        sha = e2.get("sha256") if e2.get("_sha_size") == size else None
        sha = sha or _sha256(src)
        if not (os.path.isfile(dst) and os.path.getsize(dst) == size and _sha256(dst) == sha):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst + ".part")
            os.replace(dst + ".part", dst)
            log("Format 1: {} copied ({:.1f} MB)".format(e2["file"], size / 1048576.0))
        e1.update(file_size=size, sha256=sha)
        rasters[NLCD_NAME] = e1
        man1.pop("rasters_not_included", None)
    else:
        rasters.pop(NLCD_NAME, None)
        man1["rasters_not_included"] = {
            NLCD_NAME: "the NLCD raster is not in {} either".format(PACK2)}
    man1["rasters"] = rasters
    with open(os.path.join(p1, OP.MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(man1, f, indent=2, sort_keys=True)
    return man1


def zip_format1(p1, out_zip, log=None):
    """1.5.2: the format-1 pack as one zip (nd_offline_pack/ with the sqlite, manifest.json,
    README.txt and the rasters its manifest lists), for the plugins' Browse… (unpacked once
    by offline_pack, rasters included). Returns out_zip."""
    log = log or (lambda m: None)
    man = _read_json(os.path.join(p1, OP.MANIFEST_NAME))
    db = os.path.join(p1, OP.DB_NAME)
    if not man or not os.path.isfile(db):
        raise PackBuildError("{} is not a built format-1 pack".format(p1))
    files = [OP.DB_NAME]
    if os.path.isfile(os.path.join(p1, "README.txt")):
        files.append("README.txt")
    for k, e in sorted((man.get("rasters") or {}).items()):
        rel = str((e or {}).get("file") or "")
        if not rel or not os.path.isfile(os.path.join(p1, *rel.split("/"))):
            raise PackBuildError("{}: raster {} missing ({})".format(p1, k, rel))
        files.append(rel)
    out_zip = os.path.abspath(out_zip)
    if _inside(out_zip, p1):
        raise PackBuildError("the zip cannot go inside the pack folder")
    tmp = out_zip + ".part"
    top = os.path.basename(os.path.normpath(p1))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for rel in files:
            z.write(os.path.join(p1, *rel.split("/")), top + "/" + rel,
                    zipfile.ZIP_STORED if rel.endswith(".gpkg") else zipfile.ZIP_DEFLATED)
        z.write(os.path.join(p1, OP.MANIFEST_NAME), top + "/" + OP.MANIFEST_NAME)  # last
    os.replace(tmp, out_zip)
    log("Format 1 zip: {} ({:.1f} MB)".format(out_zip, os.path.getsize(out_zip) / 1048576.0))
    return out_zip


# ------------------------------------------------------------------ format 2 --
def write_format2(src_db, p2, man1, infos, left, refresh, log, cancel):
    """ND_Dam_Suite_Pack from the format-1 file (gpkgpack.convert_v1); a layer already written
    with the same date and count is kept."""
    old = GP.read_manifest(p2) if os.path.isdir(p2) else None
    old = old if GP.is_gpkg_manifest(old) else {"datasets": {}}
    only = set()
    for k, v in man1["datasets"].items():
        o = old["datasets"].get(k) or {}
        same = (not refresh and o.get("fetched") == v.get("fetched") and
                o.get("count") == v.get("count") and o.get("file") and
                os.path.isfile(os.path.join(p2, *o["file"].split("/"))))
        if not same:
            only.add(k)
    # 1.5.1: zero counts kept (count_3dhp 0), as in format 1
    extra = {k: {kk: vv for kk, vv in (infos.get(k) or {}).items()
                 if vv not in (None, "", {}, [])} for k in infos}
    man2 = GP.convert_v1(src_db, p2, only=only, extra_info=extra, log=log,
                         cancel=cancel)
    for k, e in (man2.get("datasets") or {}).items():           # provenance of kept layers too
        e.update(extra.get(k) or {})
    for k, why in left.items():
        if k not in man2["datasets"]:
            man2.setdefault("left_out", {})[k] = why
    _finish2(p2, man2)
    return man2


def _finish2(p2, man2):
    for sect in ("datasets", "rasters"):
        for k, e in (man2.get(sect) or {}).items():
            p = os.path.join(p2, *e.get("file", "").split("/")) if e.get("file") else ""
            if p and os.path.isfile(p) and (e.get("sha256") is None or
                                            e.get("_sha_size") != os.path.getsize(p)):
                e["sha256"] = _sha256(p)
                e["_sha_size"] = os.path.getsize(p)
    man2[MARK] = BUILDER
    man2["created_by"] = man2.get("created_by") or BUILDER
    man2["format1_copy"] = "../{}/{}".format(PACK1, OP.DB_NAME)
    GP.write_manifest(p2, man2)


# --------------------------------------------------------------------- NLCD --
def _nlcd(p2, man2, t, bbox, wk, refresh, log, cancel):
    old = (man2.get("rasters") or {}).get(NLCD_NAME) or {}
    if old and not refresh and old.get("bbox") == list(bbox) and os.path.isfile(
            os.path.join(p2, *old["file"].split("/"))) and not t["spec"].get("path"):
        log("NLCD 2021 land cover: already in the pack (kept)")
        return
    info = {"label": NLCD_LABEL, "fetched": datetime.date.today().isoformat(),
            "bbox": list(bbox), "category": "land cover"}
    if t["spec"].get("path"):
        src = t["spec"]["path"]
        info.update(url=src, source="local NLCD file {}".format(os.path.basename(src)))
    else:
        src, url, source = nlcd_download(bbox, os.path.join(wk, "nlcd"), log, cancel)
        info.update(url=url, source=source)
    GP.add_raster_file(p2, man2, NLCD_NAME, src, info, log)
    man2.setdefault("left_out", {}).pop(NLCD_NAME, None)


def nlcd_grid(bbox):
    """(x0, y0, x1, y1) in EPSG:5070 on the NLCD 30 m grid covering the lon / lat bbox."""
    from osgeo import osr
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromEPSG(4326)
    b.ImportFromEPSG(5070)
    for s in (a, b):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(a, b)
    x0, y0, x1, y1 = bbox
    pts = [tr.TransformPoint(x0 + (x1 - x0) * i / 20.0, y)[:2] for i in range(21)
           for y in (y0, y1)] + [tr.TransformPoint(x, y0 + (y1 - y0) * j / 20.0)[:2]
                                 for j in range(21) for x in (x0, x1)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]

    def lo(v):
        return math.floor((v - NLCD_ORIGIN) / 30.0) * 30.0 + NLCD_ORIGIN

    def hi(v):
        return math.ceil((v - NLCD_ORIGIN) / 30.0) * 30.0 + NLCD_ORIGIN
    return lo(min(xs)), lo(min(ys)), hi(max(xs)), hi(max(ys))


def nlcd_download(bbox, dest, log, cancel):
    """NLCD 2021 for bbox as one GeoTIFF (EPSG:5070, 30 m, NLCD class values): the MRLC WCS
    first, else the Esri Annual NLCD image service (year 2021). Returns (tif, url, source)."""
    from osgeo import gdal
    gdal.UseExceptions()
    os.makedirs(dest, exist_ok=True)
    x0, y0, x1, y1 = nlcd_grid(bbox)
    step = NLCD_TILE * 30.0
    tiles = [(x, y, min(x + step, x1), min(y + step, y1))
             for x in _frange(x0, x1, step) for y in _frange(y0, y1, step)]
    errors = []
    for which in ("mrlc", "esri"):
        files = []
        try:
            for k, (a, b, c, d) in enumerate(tiles):
                if cancel():
                    raise BP.BuildCancelled("cancelled")
                fn = os.path.join(dest, "{}_{:03d}.tif".format(which, k))
                if not os.path.isfile(fn):
                    _nlcd_tile(which, (a, b, c, d), fn)
                files.append(fn)
                log("  NLCD tile {}/{} ({})".format(k + 1, len(tiles), which))
        except BP.BuildCancelled:
            raise
        except Exception as e:                                   # noqa: BLE001 - next source
            errors.append("{}: {}".format(which, e))
            log("  NLCD from {} failed ({})".format(which, e))
            if which == "mrlc":
                continue
            raise PackBuildError("NLCD download failed - " + "; ".join(errors))
        out = os.path.join(dest, "{}_nlcd2021.tif".format(which))
        vrt = gdal.BuildVRT(out + ".vrt", files)
        vrt = None
        gdal.Translate(out, out + ".vrt", projWin=(x0, y1, x1, y0),
                       noData=0, creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
        if which == "mrlc":
            return out, NLCD_WCS, "USGS / MRLC National Land Cover Database 2021 (MRLC WCS)"
        return out, NLCD_ESRI.rsplit("/", 1)[0], (
            "USGS Annual NLCD (Collection 1) land cover, year 2021, from the Esri image "
            "service (the MRLC WCS did not answer: {}); same class codes as NLCD 2021".format(
                errors[0] if errors else "-"))


def _frange(a, b, s):
    v = a
    while v < b - 1e-6:
        yield v
        v += s


def _nlcd_tile(which, box, fn):
    a, b, c, d = box
    w, h = int(round((c - a) / 30.0)), int(round((d - b) / 30.0))
    if which == "mrlc":
        q = {"service": "WCS", "version": "1.0.0", "request": "GetCoverage",
             "coverage": NLCD_WCS_COVERAGE, "crs": "EPSG:5070", "response_crs": "EPSG:5070",
             "bbox": "{},{},{},{}".format(a, b, c, d), "width": w, "height": h,
             "format": "GeoTIFF"}
        url, timeout = NLCD_WCS + "?" + urllib.parse.urlencode(q), 60
    else:
        q = {"bbox": "{},{},{},{}".format(a, b, c, d), "bboxSR": 5070, "imageSR": 5070,
             "size": "{},{}".format(w, h), "format": "tiff", "pixelType": "U8",
             "interpolation": "RSP_NearestNeighbor", "time": NLCD_YEAR_MS, "f": "image"}
        url, timeout = NLCD_ESRI + "?" + urllib.parse.urlencode(q), BP.TIMEOUT_S
    req = urllib.request.Request(url, headers={"User-Agent": BP._ua()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if not (raw[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00")):
        raise PackBuildError("not a GeoTIFF ({} bytes: {!r})".format(len(raw), raw[:80]))
    with open(fn + ".part", "wb") as f:
        f.write(raw)
    from osgeo import gdal
    ds = gdal.Open(fn + ".part")
    if ds is None or (ds.RasterXSize, ds.RasterYSize) != (w, h):
        raise PackBuildError("unexpected tile size")
    ds = None
    os.replace(fn + ".part", fn)


# ------------------------------------------------------------------ report --
def summary(man2, log):
    ds = man2.get("datasets") or {}
    log("")
    log("Pack: {} layers, {:,} features, {} raster(s)".format(
        len(ds), sum(int(v.get("count") or 0) for v in ds.values()),
        len(man2.get("rasters") or {})))
    for k, v in sorted((man2.get("left_out") or {}).items()):
        log("  NOT in the pack: {} - {}".format(k, v))


def verify(out_parent, deep=True):
    """Both packs of out_parent checked: packcheck (manifest vs data), opened by
    offline_pack.open_pack, and the same layers with the same counts; 1.5.2: the same rasters
    (format 1 lists the NLCD copy). Returns (ok, lines)."""
    from .damsuite import packcheck as PC
    p2, p1 = os.path.join(out_parent, PACK2), os.path.join(out_parent, PACK1)
    lines, ok, counts = [], True, []
    for p in (p1, p2):
        r = PC.check_pack(p, deep=deep)
        ok = ok and r.ok and not r.failed
        lines.append(r.summary())
        try:
            rd = OP.open_pack(p)
            ds = {k: int((v or {}).get("count") or 0) for k, v in rd.datasets.items()
                  if (v or {}).get("status") in OP.USABLE_STATUS}
            counts.append(ds)
            lines.append("open_pack({}): {} layer(s), {:,} features".format(
                os.path.basename(p), len(ds), sum(ds.values())))
        except Exception as e:                                   # noqa: BLE001 - reported
            ok = False
            counts.append({})
            lines.append("open_pack({}) FAILED: {}".format(os.path.basename(p), e))
    if counts[0] != counts[1]:
        ok = False
        diff = sorted(k for k in set(counts[0]) | set(counts[1])
                      if counts[0].get(k) != counts[1].get(k))
        lines.append("format 1 and format 2 differ: " + ", ".join(
            "{} {}/{}".format(k, counts[0].get(k), counts[1].get(k)) for k in diff))
    else:
        lines.append("format 1 and format 2: the same {} layer(s) and counts".format(
            len(counts[0])))
    r1 = (_read_json(os.path.join(p1, OP.MANIFEST_NAME)) or {}).get("rasters") or {}
    r2 = (GP.read_manifest(p2) or {}).get("rasters") or {} if os.path.isdir(p2) else {}
    bad = []
    for k, e in r2.items():
        e1 = r1.get(k) or {}
        f1 = os.path.join(p1, *str(e1.get("file", "")).split("/")) if e1.get("file") else ""
        if not f1 or not os.path.isfile(f1) or (
                e.get("sha256") and e1.get("sha256") and e.get("sha256") != e1.get("sha256")):
            bad.append(k)
        elif deep and e1.get("sha256") and _sha256(f1) != e1.get("sha256"):
            bad.append(k)
    if bad:
        ok = False
        lines.append("raster(s) not in format 1 (or different): " + ", ".join(sorted(bad)))
    else:
        lines.append("format 1 and format 2: the same {} raster(s)".format(len(r2)))
    return ok, lines


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--bbox", default="")
    ap.add_argument("--dwr-folder")
    ap.add_argument("--hifld-folder")
    ap.add_argument("--nlcd-file")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--verify", action="store_true", help="only check the built packs")
    ap.add_argument("--zip1", metavar="ZIP",
                    help="also write the format-1 pack (with its NLCD raster) as one zip")
    a = ap.parse_args(argv)
    if a.list:
        for t in registry(a.dwr_folder, a.hifld_folder, a.nlcd_file):
            print("{:36s} {:11s} {:13s} {}".format(t["name"], t["group"], t["kind"], t["label"]))
        return 0
    if a.verify:
        ok, lines = verify(os.path.abspath(a.out))
        print("\n".join(lines))
        if ok and a.zip1:
            zip_format1(os.path.join(os.path.abspath(a.out), PACK1), a.zip1, print)
        return 0 if ok else 1
    bbox = [float(v) for v in a.bbox.split(",")] if a.bbox else None
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    r = build(a.out, only=only, bbox=bbox, refresh=a.refresh, dwr_folder=a.dwr_folder,
              hifld_folder=a.hifld_folder, nlcd_file=a.nlcd_file)
    ok, lines = verify(os.path.abspath(a.out))
    print("\n".join(lines))
    if ok and a.zip1:
        zip_format1(r["pack1"], a.zip1, print)
    return 1 if r["failed"] or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
