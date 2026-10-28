"""1.5.0: "Download imagery for these dams…" - NAIP aerial imagery around each dam of a dams
layer (or the chosen ones), one JPEG-compressed Cloud Optimized GeoTIFF per dam:

    <output folder>/imagery/<dam_id>_naip_<year>.tif     (YCbCr JPEG, internal overviews)
    <output folder>/imagery/manifest.json                (URL, year, resolution, box, sha256)
    <output folder>/imagery/.tiles/<dam_id>/...          (the downloaded pieces; resume only)

Source: the USGS National Map NAIP Imagery service (SERVICE, an ArcGIS ImageServer; NAIP from
USDA FPAC, 0.6 m in North Dakota). Each dam's box (2 x radius, in the dam's NAD83 UTM zone) is
fetched with exportImage in pieces of at most TILE_PX x TILE_PX pixels (the server allows
4000), only the newest NAIP year on the box (mosaicRule Year = <year>), bands 1-3 (natural
colour; NIR left out). The pieces are mosaicked and written with GDAL's COG driver.

Never written inside a pack (ND_Dam_Suite_Pack / nd_offline_pack) and nothing goes into the
sqlite. Run again to resume: a dam whose file is in the manifest with the same sha256 and
settings is skipped; pieces already downloaded are kept.

No QGIS import (the window lives in imagery_dialog.py). Command line (testing):
    python3 -m DamSafety_Data.imagery --dams dams.gpkg --out /tmp/x [--ids A,B] [--id-field F]
        [--radius 2500] [--res 0.6] [--estimate]
"""
import datetime
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = ("https://imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/"
           "ImageServer")
SOURCE = "USGS The National Map - NAIP Imagery (USDA FPAC NAIP), ArcGIS ImageServer exportImage"
FOLDER = "imagery"
MANIFEST = "manifest.json"
TILES = ".tiles"
TILE_PX = 2000                       # pieces of at most 2000 x 2000 px (server max 4000)
RESOLUTIONS = (0.6, 1.0, 2.0)        # metres; 0.6 = native NAIP in North Dakota
DEFAULT_RADIUS_M = 2500.0            # 5 km box
QUALITY = 85                         # JPEG quality of the output
TRANSFERS = {"jpeg": ("JPEG", 90), "lossless": ("LZ77", None)}
# bytes per pixel, measured on Bismarck-area NAIP 2023, 0.6 m (download pieces / finished COG
# with overviews: 60.1 MB / 24.2 MB for 8334 x 8334 px); for the size estimate only
BPP_DOWNLOAD = {"jpeg": 0.87, "lossless": 2.8}
BPP_OUTPUT = 0.35
TIMEOUT_S = 180
TRIES = 4
ID_FIELDS = ("dam_id", "damid", "nidid", "nid_id", "structure_id", "dam_name", "name", "id")
PACK_NAMES = ("ND_Dam_Suite_Pack", "nd_offline_pack")
UA = "DamSafety_Data/1.5.0 (+https://github.com/nickdbendickson/DWR_Plugins)"


class ImageryError(Exception):
    pass


class Cancelled(ImageryError):
    pass


# ----------------------------------------------------------------- the dams --
def safe_id(v):
    """A dam id usable in a file name."""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(v).strip()).strip("._")
    return s[:80] or "dam"


def pick_id_field(names):
    low = {n.lower(): n for n in names}
    for k in ID_FIELDS:
        if k in low:
            return low[k]
    return None


def read_dams(path, layer=None, id_field=None, only_ids=None):
    """[(dam_id, lon, lat)] of a vector file (point, or the centre of a line / polygon), in
    WGS 84. only_ids: keep those ids (the ONLY_IDS list)."""
    from osgeo import ogr, osr
    ds = ogr.Open(path)
    if ds is None:
        raise ImageryError("cannot open {}".format(path))
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    if lyr is None:
        raise ImageryError("no layer {} in {}".format(layer, path))
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    fld = id_field or pick_id_field(names)
    if id_field and id_field not in names:
        raise ImageryError("no field {} (fields: {})".format(id_field, ", ".join(names)))
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    src = lyr.GetSpatialRef()
    tr = None
    if src is not None:
        src = src.Clone()
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not src.IsSame(wgs):
            tr = osr.CoordinateTransformation(src, wgs)
    want = {str(i).strip() for i in only_ids} if only_ids else None
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        did = f.GetField(fld) if fld else f.GetFID()
        if did in (None, ""):
            did = f.GetFID()
        if want is not None and str(did).strip() not in want:
            continue
        c = g.Centroid() if g.GetGeometryType() not in (ogr.wkbPoint, ogr.wkbPoint25D) else \
            g.Clone()
        if tr is not None:
            c.Transform(tr)
        out.append((str(did), c.GetX(), c.GetY()))
    return out


# ----------------------------------------------------------------- the plan --
def utm_epsg(lon, lat):
    """NAD83 / UTM zone of a point (EPSG 269xx; North Dakota is zones 13 and 14)."""
    zone = int(math.floor((lon + 180.0) / 6.0)) + 1
    return 26900 + zone if lat >= 0 else 32700 + zone


def to_utm(lon, lat, epsg):
    from osgeo import osr
    a = osr.SpatialReference()
    a.ImportFromEPSG(4326)
    a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    b = osr.SpatialReference()
    b.ImportFromEPSG(epsg)
    b.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    x, y, _z = osr.CoordinateTransformation(a, b).TransformPoint(lon, lat)
    return x, y


def dam_plan(dam_id, lon, lat, radius_m=DEFAULT_RADIUS_M, res_m=0.6):
    """Box, size and exportImage pieces of one dam (UTM metres, snapped to the pixel grid)."""
    epsg = utm_epsg(lon, lat)
    x, y = to_utm(lon, lat, epsg)
    n = int(math.ceil(2 * radius_m / res_m))
    xmin = math.floor((x - radius_m) / res_m) * res_m
    ymin = math.floor((y - radius_m) / res_m) * res_m
    xmax, ymax = xmin + n * res_m, ymin + n * res_m
    tiles = []
    for r0 in range(0, n, TILE_PX):
        for c0 in range(0, n, TILE_PX):
            w, h = min(TILE_PX, n - c0), min(TILE_PX, n - r0)
            tx = xmin + c0 * res_m
            ty = ymax - (r0 + h) * res_m
            tiles.append({"row": r0 // TILE_PX, "col": c0 // TILE_PX, "w": w, "h": h,
                          "bbox": [round(tx, 3), round(ty, 3), round(tx + w * res_m, 3),
                                   round(ty + h * res_m, 3)]})
    return {"dam_id": str(dam_id), "file_id": safe_id(dam_id), "lon": lon, "lat": lat,
            "epsg": epsg, "res_m": res_m, "radius_m": radius_m, "pixels": n,
            "bbox": [round(xmin, 3), round(ymin, 3), round(xmax, 3), round(ymax, 3)],
            "tiles": tiles}


def estimate(plans, transfer="jpeg"):
    """(download bytes, output bytes) of the plans, roughly (BPP_* constants)."""
    px = sum(p["pixels"] ** 2 for p in plans)
    return int(px * BPP_DOWNLOAD[transfer]), int(px * BPP_OUTPUT)


def human(n):
    for u in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("{:.0f} {}" if u == "bytes" else "{:.1f} {}").format(n, u)
        n /= 1024.0


# ------------------------------------------------------------------- HTTP --
def _get(url, params, timeout=TIMEOUT_S, cancel=None, log=None):
    """GET url?params (bytes); retried on 5xx / network errors."""
    full = url + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(TRIES):
        if cancel and cancel():
            raise Cancelled("stopped")
        try:
            req = urllib.request.Request(full, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise ImageryError("HTTP {} from {}".format(e.code, url))
            last = "HTTP {}".format(e.code)
        except (urllib.error.URLError, OSError) as e:
            last = str(getattr(e, "reason", e))
        if i + 1 < TRIES:
            wait = 5 * (i + 1) ** 2
            if log:
                log("  {} - again in {} s (try {} of {})".format(last, wait, i + 2, TRIES))
            time.sleep(wait)
    raise ImageryError("{}: {}".format(url.rsplit("/", 1)[-1], last))


def naip_years(plan, cancel=None, log=None):
    """{year: footprints} of the full-resolution NAIP images on the dam's box, newest first."""
    b = plan["bbox"]
    raw = _get(SERVICE + "/query", {
        "where": "Category = 1", "geometry": "{},{},{},{}".format(*b),
        "geometryType": "esriGeometryEnvelope", "inSR": plan["epsg"],
        "spatialRel": "esriSpatialRelIntersects", "outFields": "Year,Name,resolution_value",
        "returnGeometry": "false", "f": "json"}, timeout=120, cancel=cancel, log=log)
    try:
        d = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ImageryError("the NAIP service did not answer the image query")
    if "error" in d:
        raise ImageryError("NAIP query: {}".format(d["error"].get("message")))
    years = {}
    for f in d.get("features", []):
        a = f.get("attributes") or {}
        if a.get("Year"):
            years.setdefault(int(a["Year"]), []).append(a.get("Name"))
    return dict(sorted(years.items(), reverse=True))


def export_params(plan, tile, year, transfer="jpeg"):
    comp, q = TRANSFERS[transfer]
    p = {"bbox": "{},{},{},{}".format(*tile["bbox"]), "bboxSR": plan["epsg"],
         "imageSR": plan["epsg"], "size": "{},{}".format(tile["w"], tile["h"]),
         "format": "tiff", "pixelType": "U8", "bandIds": "0,1,2",
         "interpolation": "RSP_BilinearInterpolation", "compression": comp,
         "mosaicRule": json.dumps({"where": "Year = {}".format(int(year))},
                                 separators=(",", ":")), "f": "image"}
    if q:
        p["compressionQuality"] = q
    return p


def _valid_tiff(path, w=None, h=None, bands=3):
    if not os.path.isfile(path):
        return False
    try:
        from osgeo import gdal
        ds = gdal.Open(path)
        ok = ds is not None and ds.RasterCount >= bands and \
            (w is None or (ds.RasterXSize == w and ds.RasterYSize == h))
        ds = None
        return bool(ok)
    except Exception:                                            # noqa: BLE001 - not valid
        return False


def _georef(path, tile, plan):
    """Pin the piece to the box asked for (the server's own georeferencing is replaced)."""
    from osgeo import gdal, osr
    ds = gdal.Open(path, gdal.GA_Update)
    b, r = tile["bbox"], plan["res_m"]
    ds.SetGeoTransform((b[0], r, 0.0, b[3], 0.0, -r))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(plan["epsg"])
    ds.SetProjection(sr.ExportToWkt())
    ds = None


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------- the output --
def imagery_folder(out_parent):
    """<out_parent>/imagery; refused inside (or as) a pack folder."""
    if not out_parent:
        raise ImageryError("Choose an output folder.")
    p = os.path.abspath(out_parent)
    parts = [x.lower() for x in p.replace("\\", "/").split("/")]
    if any(x.startswith(n.lower()) for x in parts for n in PACK_NAMES):
        raise ImageryError("{} is (inside) a pack folder - the imagery is never written into a "
                           "pack. Choose another folder.".format(out_parent))
    return os.path.join(p, FOLDER)


def _read_manifest(folder):
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_manifest(folder, man):
    p = os.path.join(folder, MANIFEST)
    with open(p + ".part", "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1, sort_keys=True)
    os.replace(p + ".part", p)


def _settings_key(plan, transfer):
    return "{}|{}|{}|{}".format(plan["res_m"], plan["radius_m"], transfer, QUALITY)


def write_cog(pieces, out_path, epsg):
    """The pieces mosaicked into one JPEG (YCbCr) COG with overviews."""
    from osgeo import gdal
    vrt = out_path + ".vrt"
    v = gdal.BuildVRT(vrt, pieces)
    if v is None:
        raise ImageryError("could not mosaic the pieces")
    v = None
    tmp = out_path + ".part.tif"
    ds = gdal.Translate(tmp, vrt, format="COG", outputSRS="EPSG:{}".format(epsg),
                        creationOptions=["COMPRESS=JPEG", "QUALITY={}".format(QUALITY),
                                         "BLOCKSIZE=512", "OVERVIEW_RESAMPLING=AVERAGE",
                                         "BIGTIFF=IF_SAFER", "NUM_THREADS=1"])
    if ds is None:
        raise ImageryError("could not write {}".format(out_path))
    ds = None
    os.remove(vrt)
    os.replace(tmp, out_path)


def download(dams, out_parent, radius_m=DEFAULT_RADIUS_M, res_m=0.6, transfer="jpeg",
             refresh=False, log=None, progress=None, cancel=None, keep_tiles=False):
    """NAIP around each dam [(dam_id, lon, lat)] into <out_parent>/imagery. Returns
    {"folder", "done": {dam_id: entry}, "skipped": [...], "failed": {dam_id: why}}."""
    log = log or (lambda s: None)
    if res_m not in RESOLUTIONS:
        raise ImageryError("resolution {} m: one of {}".format(res_m, RESOLUTIONS))
    if transfer not in TRANSFERS:
        raise ImageryError("transfer: one of {}".format(", ".join(TRANSFERS)))
    folder = imagery_folder(out_parent)
    os.makedirs(folder, exist_ok=True)
    man = _read_manifest(folder)
    man.update(source=SOURCE, service=SERVICE, folder_note="one file per dam: "
               "<dam_id>_naip_<year>.tif, JPEG (YCbCr) COG with overviews")
    entries = man.setdefault("dams", {})
    plans = [dam_plan(d, x, y, radius_m, res_m) for d, x, y in dams]
    ids = [p["file_id"] for p in plans]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ImageryError("dam ids repeat (or clash once made file-safe): {}".format(
            ", ".join(sorted(dup))))
    dl, outb = estimate(plans, transfer)
    log("{} dam(s), {} x {} px each at {} m: about {} to download, {} on disk".format(
        len(plans), plans[0]["pixels"] if plans else 0, plans[0]["pixels"] if plans else 0,
        res_m, human(dl), human(outb)))
    res = {"folder": folder, "done": {}, "skipped": [], "failed": {}}
    total_tiles = max(1, sum(len(p["tiles"]) for p in plans))
    tiles_done = 0
    for k, plan in enumerate(plans):
        if cancel and cancel():
            raise Cancelled("stopped")
        did = plan["dam_id"]
        old = entries.get(did) or {}
        okey = _settings_key(plan, transfer)
        path = os.path.join(folder, old.get("file", "")) if old.get("file") else ""
        if not refresh and old.get("settings") == okey and path and os.path.isfile(path) and \
                sha256(path) == old.get("sha256") and _valid_tiff(path):
            log("[{}/{}] {}: kept ({})".format(k + 1, len(plans), did, old["file"]))
            res["skipped"].append(did)
            tiles_done += len(plan["tiles"])
            continue
        log("[{}/{}] {} ({:.5f}, {:.5f})".format(k + 1, len(plans), did, plan["lon"],
                                                 plan["lat"]))
        try:
            years = naip_years(plan, cancel, log)
            if not years:
                raise ImageryError("no NAIP image on the box")
            year = next(iter(years))
            tdir = os.path.join(folder, TILES, plan["file_id"], "{}_{}_{}".format(
                year, res_m, transfer))
            os.makedirs(tdir, exist_ok=True)
            pieces, got = [], 0
            for t in plan["tiles"]:
                p = os.path.join(tdir, "r{}_c{}.tif".format(t["row"], t["col"]))
                if refresh or not _valid_tiff(p, t["w"], t["h"]):
                    data = _get(SERVICE + "/exportImage", export_params(plan, t, year, transfer),
                                cancel=cancel, log=log)
                    with open(p + ".part", "wb") as f:
                        f.write(data)
                    if not _valid_tiff(p + ".part", t["w"], t["h"]):
                        head = data[:200].decode("utf-8", "replace")
                        raise ImageryError("piece r{} c{}: not an image ({})".format(
                            t["row"], t["col"], head))
                    _georef(p + ".part", t, plan)
                    os.replace(p + ".part", p)
                    got += os.path.getsize(p)
                pieces.append(p)
                tiles_done += 1
                if progress:
                    progress(100.0 * tiles_done / total_tiles)
            name = "{}_naip_{}.tif".format(plan["file_id"], year)
            out = os.path.join(folder, name)
            write_cog(pieces, out, plan["epsg"])
            if old.get("file") and old["file"] != name and \
                    os.path.isfile(os.path.join(folder, old["file"])):
                log("  note: the earlier file {} is left in place".format(old["file"]))
            e = {"file": name, "dam_id": did, "lon": plan["lon"], "lat": plan["lat"],
                 "year": year, "years_on_box": sorted(years), "naip_images": years[year],
                 "resolution_m": res_m, "radius_m": radius_m, "pixels": plan["pixels"],
                 "crs": "EPSG:{}".format(plan["epsg"]), "bbox": plan["bbox"],
                 "url": SERVICE + "/exportImage", "request": {
                     k2: v for k2, v in export_params(plan, plan["tiles"][0], year,
                                                      transfer).items() if k2 not in ("bbox",
                                                                                      "size")},
                 "pieces": len(pieces), "transfer": transfer, "downloaded_bytes": got,
                 "bytes": os.path.getsize(out), "sha256": sha256(out),
                 "fetched": datetime.datetime.now().isoformat(timespec="seconds"),
                 "settings": okey}
            entries[did] = e
            _write_manifest(folder, man)
            res["done"][did] = e
            log("  {}: {} ({} pieces, {} downloaded)".format(name, human(e["bytes"]),
                                                              len(pieces), human(got)))
            if not keep_tiles:
                for p in pieces:
                    os.remove(p)
                try:
                    os.rmdir(tdir)
                    os.rmdir(os.path.dirname(tdir))
                except OSError:
                    pass
        except Cancelled:
            _write_manifest(folder, man)
            raise
        except ImageryError as e:
            res["failed"][did] = str(e)
            log("  FAILED: {}".format(e))
    _write_manifest(folder, man)
    try:
        os.rmdir(os.path.join(folder, TILES))
    except OSError:
        pass
    return res


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dams", required=True, help="a vector file of dams")
    ap.add_argument("--layer")
    ap.add_argument("--id-field")
    ap.add_argument("--ids", help="only these dam ids (comma separated)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--radius", type=float, default=DEFAULT_RADIUS_M)
    ap.add_argument("--res", type=float, default=0.6)
    ap.add_argument("--transfer", choices=sorted(TRANSFERS), default="jpeg")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--estimate", action="store_true", help="size estimate only")
    a = ap.parse_args(argv)
    ids = [s for s in (a.ids or "").split(",") if s.strip()] or None
    dams = read_dams(a.dams, a.layer, a.id_field, ids)
    if not dams:
        print("no dams")
        return 1
    if a.estimate:
        plans = [dam_plan(d, x, y, a.radius, a.res) for d, x, y in dams]
        dl, out = estimate(plans, a.transfer)
        print("{} dam(s): about {} to download, {} on disk".format(len(plans), human(dl),
                                                                    human(out)))
        return 0
    r = download(dams, a.out, a.radius, a.res, a.transfer, a.refresh,
                 log=lambda s: print(s, flush=True))
    return 1 if r["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
