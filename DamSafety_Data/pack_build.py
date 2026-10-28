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

No QGIS import here (the dialog / task live in pack_dialog.py); GDAL is needed for the DWR and
local-file layers and for the raster. Command line (for testing):
    python3 -m DamSafety_Data.pack_build --out /tmp/x --bbox -100.9,46.7,-100.6,46.9 --only ND_counties
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
BUILDER = "DamSafety_Data pack_build 1.5.0"
ND_BBOX = OP.ND_BBOX
DEFAULT_PAD = OP.DEFAULT_PAD_DEG

ND_COUNTIES_URL = "https://ndgishub.nd.gov/arcgis/rest/services/All_GovtBoundaries/MapServer/20"
# HIFLD lifeline layers: public FEMA / Esri mirrors of the HIFLD layers of the reference pack
HIFLD = (
    ("HIFLD_fire_ems_stations", "HIFLD fire and emergency medical service (EMS) stations",
     "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
     "Structures_Medical_Emergency_Response_v1/FeatureServer/2", "Fire station",
     ("fire", "ems")),
    ("HIFLD_local_eoc", "HIFLD local emergency operations centers",
     "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
     "Critical_Infrastructure_Map_Service/FeatureServer/13", "Emergency operations center",
     ("eoc", "emergency_operations", "emergency operations")),
    ("HIFLD_local_law_enforcement", "HIFLD local law enforcement locations",
     "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
     "Structures_Law_Enforcement_v1/FeatureServer/0", "Law enforcement",
     ("law", "police")),
    ("HIFLD_wastewater_treatment_plants", "EPA wastewater treatment plants (HIFLD)",
     "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
     "wastewater_treatment_plants_epa_frs/FeatureServer/0", "Wastewater treatment",
     ("wastewater",)),
)
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
    arcgis / first_of / nsi / osm / local_gpkg, or ogr (a local vector file), dwr_download,
    nlcd (the raster), none (cannot be fetched: listed as left out). Same names as the
    reference ND_Dam_Suite_Pack."""
    out = [_e(n, l, g, k, s) for n, l, g, k, s in BP.datasets(None)]
    out.append(_e("ND_counties", "County boundaries (ND GIS Hub counties)", "boundaries",
                  "arcgis", {"url": ND_COUNTIES_URL}, source="ND GIS Hub county boundaries"))
    files = _hifld_files(hifld_folder)
    for name, label, url, cat, _keys in HIFLD:
        if name in files:
            out.append(_e(name, label, "hifld", "ogr", {"path": files[name]}, category=cat,
                          source="HIFLD file {}".format(os.path.basename(files[name]))))
        else:
            out.append(_e(name, label, "hifld", "arcgis", {"url": url}, category=cat,
                          source="HIFLD layer (public mirror) " + url.split("/rest/")[0]))
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
    """{HIFLD name: file} for the vector files of `folder` whose name says what they are."""
    if not folder or not os.path.isdir(folder):
        return {}
    exts = (".gpkg", ".shp", ".geojson", ".json", ".zip", ".sqlite")
    out = {}
    for fn in sorted(os.listdir(folder)):
        low = fn.lower()
        if not low.endswith(exts):
            continue
        for name, _l, _u, _c, keys in HIFLD:
            if name not in out and any(k in low for k in keys):
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
                else:
                    got = BP._run_one(ctx, name, label, kind, t["spec"], bbox)
                if name in TRANSFORMS:
                    _rewrite(writer.con, name, TRANSFORMS[name])
                log("  done: {:,} features in {:.0f} s".format(got, time.time() - t0))
            except (OP.PackError, BP.BuildCancelled):
                raise
            except BP._NET_ERRORS + (PackBuildError, RuntimeError) as e:
                failed[name] = str(e)
                log("  FAILED: {}".format(e))
        man1 = writer.manifest()
    finally:
        writer.close(finalize=False)
    for t in todo:
        if t["kind"] == "none":
            left[t["name"]] = t["info"].get("why") or "not available"
    infos = {t["name"]: dict(t["info"]) for t in table}
    for t in table:
        infos[t["name"]].pop("why", None)
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
    w = ctx.writer
    w.begin(name, label, url, resume=False, signature="file|{}".format(path))
    try:
        n, skipped = 0, 0
        batch = []
        for f in read_vector(path, bbox):
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


def _uid(f):
    return DF.uid_of(f) or None


def read_vector(path, bbox):
    """GeoJSON features (lon / lat, every attribute) of the first layer of a vector file,
    those touching bbox."""
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
    manifest.json (the raster is named as not included)."""
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
        for key in ("category", "source"):
            if (infos.get(k) or {}).get(key):
                v[key] = infos[k][key]
    man[MARK] = BUILDER
    man["left_out"] = dict(left)
    man["rasters_not_included"] = {
        NLCD_NAME: "a format-1 pack holds vector layers only; the NLCD raster is in "
                   "{}/landcover/{}.gpkg".format(PACK2, NLCD_NAME)}
    man["sha256"] = _sha256(dst)
    with open(os.path.join(p1, OP.MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2, sort_keys=True)
    log("Format 1: {} ({:.1f} MB)".format(dst, os.path.getsize(dst) / 1048576.0))
    return man


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
    extra = {k: {kk: vv for kk, vv in (infos.get(k) or {}).items() if vv} for k in infos}
    man2 = GP.convert_v1(src_db, p2, only=only, extra_info=extra, log=log,
                         cancel=cancel)
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
    offline_pack.open_pack, and the same layers with the same counts (the raster is format 2
    only). Returns (ok, lines)."""
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
    a = ap.parse_args(argv)
    if a.list:
        for t in registry(a.dwr_folder, a.hifld_folder, a.nlcd_file):
            print("{:36s} {:11s} {:13s} {}".format(t["name"], t["group"], t["kind"], t["label"]))
        return 0
    if a.verify:
        ok, lines = verify(os.path.abspath(a.out))
        print("\n".join(lines))
        return 0 if ok else 1
    bbox = [float(v) for v in a.bbox.split(",")] if a.bbox else None
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    r = build(a.out, only=only, bbox=bbox, refresh=a.refresh, dwr_folder=a.dwr_folder,
              hifld_folder=a.hifld_folder, nlcd_file=a.nlcd_file)
    ok, lines = verify(os.path.abspath(a.out))
    print("\n".join(lines))
    return 1 if r["failed"] or not ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
