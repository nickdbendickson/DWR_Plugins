"""USGS 3DEP 1 m lidar DEM tiles, native (Screening 0.17.29): the project tiles themselves, not
the resampled ImageServer images that Download DEM's "USGS 3DEP" source asks for.

    plan = make_plan(bbox_ll, tile_dir, http)        list the tiles: project, dates, sizes, disk
    fetch(plan, http, feedback)                      download (resume, retries), check, manifest
    build_vrt(entries, vrt, fallback, bounds_ll)     the per-dam VRT, coarser DEM underneath

Where the tiles come from: The National Map Access API (TNM),
    https://tnmaccess.nationalmap.gov/api/v1/products?datasets=Digital Elevation Model (DEM) 1 meter
        &bbox=west,south,east,north&prodFormats=GeoTIFF&max=..&offset=..
which answers {"total", "items", "errors", "messages"}; each item has the tile's downloadURL,
sizeInBytes, publicationDate and vendorMetaUrl (the tile's FGDC XML, whose begdate / enddate are
the collection dates). The tiles are 10 x 10 km GeoTIFFs at 1 m, NAD83 UTM in metres, about
275 MB each, named USGS_1M_<zone>_x<..>y<..>_<project>.tif.

Overlaps: two projects can publish the same 10 km cell (same zone, x, y). The newest collection
wins (collection end date from the project's metadata, then the publication date) and the
choice is reported; the older tile is not downloaded unless keep_older, when it is kept
underneath in the VRT. Within the VRT, sources are ordered oldest first, so newer data is on top
wherever two tiles overlap at all.

Files (tile_dir is <suite data folder>/dem/lidar_1m/ unless a folder is chosen; see
tile_folder): the tiles, lidar_1m_manifest.json (tile, project, dates, URL, sha256, bytes), and
<tile>.part while a download is incomplete (kept for resume). Nothing is ever deleted except a
.part that turned out to be corrupt. Pure Python and GDAL (imported only where it is needed); no
QGIS here.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request

from ..damsuite import gdalsafe as GS   # GDAL without switching its exception mode
from . import dem_zunit as ZU           # 0.17.50: the fallback DEM's unit, as the Runner reads it
from .warp_opts import no_vshift        # 0.17.50: warps keep the values as stored

TNM_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
DATASET = "Digital Elevation Model (DEM) 1 meter"
PAGE = 100
MANIFEST = "lidar_1m_manifest.json"
SUB = "lidar_1m"                        # under the suite data folder's dem/ folder
PACK_NAMES = ("nd_dam_suite_pack", "nd_offline_pack")
RESERVE_BYTES = 2 * 1024 ** 3           # free space left after the download
DEFAULT_MAX_BYTES = 10 * 1024 ** 3      # one run downloads at most this much unless raised
CHUNK = 4 * 1024 * 1024
RETRIES = 4
USER_AGENT = "DamSafety_Screening (3DEP 1 m tiles)"
NAME_RE = re.compile(r"USGS_1M_(\d+)_(x\d+y\d+)_(.+?)\.tif$", re.I)
TITLE_RE = re.compile(r"USGS 1 Meter (\d+) (x\d+y\d+) (\S+)", re.I)


class Cancelled(Exception):
    """The user cancelled; finished tiles and the .part of the current one are kept."""


class TileError(IOError):
    """A tile could not be downloaded or failed its checks."""


# ------------------------------------------------------------------------------- feedback


def _info(feedback, msg):
    if feedback is not None:
        feedback.pushInfo(msg)


def _warn(feedback, msg):
    if feedback is not None:
        getattr(feedback, "pushWarning", feedback.pushInfo)(msg)


def _progress(feedback, pct):
    if feedback is not None:
        feedback.setProgress(int(max(0, min(100, pct))))


def _check_cancel(feedback):
    if feedback is not None and feedback.isCanceled():
        raise Cancelled()


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "{:.0f} {}".format(n, unit) if unit == "bytes" else "{:.2f} {}".format(n, unit)
        n /= 1024.0


# ----------------------------------------------------------------------------------- HTTP


class UrlHttp:
    """urllib through QGIS's proxy (damsuite.netproxy) when it can be imported. Tests pass
    their own object with the same three methods."""

    def __init__(self, timeout=120):
        self.timeout = timeout

    def _open(self, url, headers=None):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            from ..damsuite import netproxy
            return netproxy.urlopen(req, timeout=self.timeout)
        except ImportError:
            return urllib.request.urlopen(req, timeout=self.timeout)

    def get_json(self, url):
        with self._open(url) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_text(self, url):
        with self._open(url) as r:
            return r.read().decode("utf-8", "replace")

    def open_range(self, url, start):
        """(status, headers, readable) for url from byte `start` (a Range request when > 0)."""
        r = self._open(url, {"Range": "bytes={}-".format(start)} if start > 0 else None)
        status = getattr(r, "status", None) or r.getcode()
        return status, {k.lower(): v for k, v in r.headers.items()}, r


# ------------------------------------------------------------------------------ TNM query


def tnm_url(bbox_ll, offset=0, page=PAGE):
    w, s, e, n = bbox_ll
    q = urllib.parse.urlencode({"datasets": DATASET, "bbox": "{:.6f},{:.6f},{:.6f},{:.6f}".format(w, s, e, n),
                                "prodFormats": "GeoTIFF", "max": page, "offset": offset})
    return TNM_URL + "?" + q


def query_tnm(bbox_ll, http, feedback=None, page=PAGE):
    """Every TNM 1 m item meeting the lon/lat box (all pages)."""
    items, offset, total = [], 0, None
    while True:
        _check_cancel(feedback)
        d = http.get_json(tnm_url(bbox_ll, offset, page))
        if isinstance(d, list):                        # a saved item list
            return d
        if d.get("errors"):
            raise IOError("TNM: {}".format(d["errors"]))
        got = d.get("items") or []
        items += got
        total = int(d.get("total") or len(items))
        offset += len(got)
        if not got or offset >= total:
            return items


def parse_item(item):
    """The facts of one TNM item that the tool uses."""
    url = item.get("downloadURL") or (item.get("urls") or {}).get("TIFF") or ""
    name = os.path.basename(urllib.parse.urlparse(url).path)
    m = NAME_RE.search(name)
    if m:
        zone, cell, project = m.group(1), m.group(2), m.group(3)
    else:
        t = TITLE_RE.search(item.get("title") or "")
        zone, cell, project = (t.group(1), t.group(2), t.group(3)) if t else ("", name, "")
    return {"tile": name, "url": url, "title": item.get("title") or "", "project": project,
            "zone": zone, "key": "{}_{}".format(zone, cell), "bytes": int(item.get("sizeInBytes") or 0),
            "publication_date": (item.get("publicationDate") or "")[:10],
            "last_updated": item.get("lastUpdated") or "", "meta_url": item.get("vendorMetaUrl") or "",
            "bbox": item.get("boundingBox") or {}, "collection_begin": "", "collection_end": ""}


def _iso(d8):
    d8 = (d8 or "").strip()
    if re.fullmatch(r"\d{8}", d8):
        return "{}-{}-{}".format(d8[:4], d8[4:6], d8[6:])
    return d8[:10]


def collection_dates(xml_text):
    """(begin, end) ISO dates from a tile's FGDC metadata (begdate / enddate), ("", "") if none."""
    b = re.findall(r"<begdate>\s*([^<]+?)\s*</begdate>", xml_text or "")
    e = re.findall(r"<enddate>\s*([^<]+?)\s*</enddate>", xml_text or "")
    return (_iso(min(b)) if b else "", _iso(max(e)) if e else "")


def add_collection_dates(tiles, http, feedback=None):
    """Fill collection_begin / collection_end, reading one metadata XML per project."""
    by_project = {}
    for t in tiles:
        by_project.setdefault(t["project"], []).append(t)
    for project, group in sorted(by_project.items()):
        _check_cancel(feedback)
        dates = ("", "")
        for t in group[:2]:
            if not t["meta_url"]:
                continue
            try:
                dates = collection_dates(http.get_text(t["meta_url"]))
            except Exception as e:                      # noqa - the publication date stands in
                _info(feedback, "{}: metadata not read ({}); using the publication date".format(project, e))
            if dates[1]:
                break
        for t in group:
            t["collection_begin"], t["collection_end"] = dates
    return tiles


def _age_key(t):
    return (t.get("collection_end") or "", t.get("publication_date") or "", t.get("last_updated") or "")


def select_tiles(tiles, keep_older=False):
    """(chosen, dropped, notes). One tile per 10 km cell: the newest collection, then the newest
    publication. With keep_older the older ones are kept too (underneath in the VRT). chosen is
    ordered oldest first, the order the VRT stacks them."""
    by_key = {}
    for t in tiles:
        by_key.setdefault(t["key"], []).append(t)
    chosen, dropped, notes = [], [], []
    for key in sorted(by_key):
        group = sorted(by_key[key], key=_age_key, reverse=True)
        best, rest = group[0], group[1:]
        chosen.append(best)
        if rest:
            older = [r for r in rest if r["tile"] != best["tile"]]
            if older:
                notes.append("{}: {} ({}) is the newest collection; {} {}".format(
                    key, best["project"], best.get("collection_end") or best["publication_date"],
                    "kept underneath: " if keep_older else "not used: ",
                    ", ".join("{} ({})".format(r["project"], r.get("collection_end") or r["publication_date"])
                              for r in older)))
            (chosen if keep_older else dropped).extend(older)
    chosen.sort(key=_age_key)
    return chosen, dropped, notes


# ------------------------------------------------------------------------------- folders


def in_pack(path):
    """True for an offline pack folder or anything inside it (ND_Dam_Suite_Pack, nd_offline_pack,
    or a folder whose manifest.json is a pack manifest)."""
    p = os.path.realpath(os.path.abspath(os.path.expanduser(str(path or ""))))
    for part in p.replace("\\", "/").split("/"):
        if part.lower().startswith(PACK_NAMES):
            return True
    q = p
    while True:
        try:
            with open(os.path.join(q, "manifest.json"), encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict) and str(m.get("format", "")).startswith("nd-dam-hazard-offline-pack"):
                return True
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(q)
        if parent == q:
            return False
        q = parent


def writable(path):
    p = os.path.abspath(os.path.expanduser(path))
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return False
        p = parent
    return os.path.isdir(p) and os.access(p, os.W_OK | os.X_OK)


def suite_dem_dir(suite):
    """The suite data folder's dem/ folder (an existing DEM/ or Dem/ is used as it is)."""
    try:
        for name in sorted(os.listdir(suite)):
            if name.lower() == "dem" and os.path.isdir(os.path.join(suite, name)):
                return os.path.join(suite, name)
    except OSError:
        pass
    return os.path.join(suite, "dem")


def tile_folder(explicit="", suite="", fallback=""):
    """(folder, how, notes): the folder chosen for the tiles first, then
    <suite data folder>/dem/lidar_1m/, then `fallback` (the tool's output folder); never an
    offline pack or a folder that can't be written."""
    notes = []
    for how, cand in (("chosen", (explicit or "").strip()),
                      ("suite", os.path.join(suite_dem_dir(suite.strip()), SUB) if (suite or "").strip() else ""),
                      ("output", os.path.join(fallback, SUB) if fallback else "")):
        if not cand:
            continue
        if in_pack(cand):
            notes.append("{} is inside the offline pack (read only); not used for tiles.".format(cand))
        elif not writable(cand):
            notes.append("Can't write to {}; not used for tiles.".format(cand))
        else:
            return cand, how, notes
    raise TileError("no folder for the lidar tiles: " + " ".join(notes or ["choose a tile folder"]))


def free_bytes(folder):
    p = os.path.abspath(folder)
    while not os.path.exists(p):
        p = os.path.dirname(p)
    return shutil.disk_usage(p).free


# ------------------------------------------------------------------------------ manifest


def load_manifest(folder):
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("tiles"), dict) else {"tiles": {}}
    except (OSError, ValueError):
        return {"tiles": {}}


def save_manifest(folder, manifest):
    os.makedirs(folder, exist_ok=True)
    manifest["format"] = "damsuite/lidar_1m_manifest"
    manifest["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    path = os.path.join(folder, MANIFEST)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------- plan


def present(tile, folder, manifest):
    """True when the tile is on disk with the expected size and recorded in the manifest."""
    path = os.path.join(folder, tile["tile"])
    entry = manifest.get("tiles", {}).get(tile["tile"])
    if not os.path.isfile(path) or not entry or not entry.get("sha256"):
        return False
    size = os.path.getsize(path)
    return size == int(entry.get("bytes") or -1) and (not tile["bytes"] or size == tile["bytes"])


def make_plan(bbox_ll, folder, http, feedback=None, keep_older=False, items=None):
    """Ask TNM (unless `items` is given) and work out what a download would do."""
    raw = items if items is not None else query_tnm(bbox_ll, http, feedback)
    tiles = [parse_item(i) for i in raw]
    tiles = [t for t in tiles if t["url"] and t["tile"].lower().endswith(".tif")]
    add_collection_dates(tiles, http, feedback)
    chosen, dropped, notes = select_tiles(tiles, keep_older)
    manifest = load_manifest(folder)
    for t in chosen:
        t["present"] = present(t, folder, manifest)
        part = os.path.join(folder, t["tile"] + ".part")
        t["partial_bytes"] = os.path.getsize(part) if (not t["present"] and os.path.isfile(part)) else 0
    need = sum(max(0, t["bytes"] - t["partial_bytes"]) for t in chosen if not t["present"])
    return {"bbox_ll": list(bbox_ll), "folder": folder, "tiles": chosen, "dropped": dropped,
            "notes": notes, "total_bytes": sum(t["bytes"] for t in chosen),
            "need_bytes": need, "free_bytes": free_bytes(folder),
            "projects": sorted({t["project"] for t in chosen})}


def disk_check(plan, max_bytes=DEFAULT_MAX_BYTES, reserve=RESERVE_BYTES):
    """(ok, message): the download fits under max_bytes and leaves `reserve` free."""
    need, free = plan["need_bytes"], plan["free_bytes"]
    if max_bytes and need > max_bytes:
        return False, "{} to download is over the {} limit for one run (raise 'Largest download').".format(
            fmt_bytes(need), fmt_bytes(max_bytes))
    if need + reserve > free:
        return False, "{} to download but only {} free in {} ({} must stay free).".format(
            fmt_bytes(need), fmt_bytes(free), plan["folder"], fmt_bytes(reserve))
    return True, "{} to download, {} free in {}.".format(fmt_bytes(need), fmt_bytes(free), plan["folder"])


def describe_plan(plan):
    """Lines for the log: one per tile, then per project, then the totals."""
    lines = []
    for t in plan["tiles"]:
        lines.append("{:<48} {:>10}  project {}  collected {} to {}  published {}{}".format(
            t["tile"], fmt_bytes(t["bytes"]), t["project"], t.get("collection_begin") or "?",
            t.get("collection_end") or "?", t["publication_date"] or "?",
            "  (already here)" if t.get("present") else (
                "  (resume from {})".format(fmt_bytes(t["partial_bytes"])) if t.get("partial_bytes") else "")))
    for p in plan["projects"]:
        group = [t for t in plan["tiles"] if t["project"] == p]
        lines.append("project {}: {} tiles, {}".format(p, len(group), fmt_bytes(sum(t["bytes"] for t in group))))
    lines += plan["notes"]
    lines.append("{} tiles, {} in all; {} still to download; {} free in {}".format(
        len(plan["tiles"]), fmt_bytes(plan["total_bytes"]), fmt_bytes(plan["need_bytes"]),
        fmt_bytes(plan["free_bytes"]), plan["folder"]))
    return lines


# ------------------------------------------------------------------------------ download


def download_tile(tile, folder, http, feedback=None, retries=RETRIES, sleep=time.sleep, on_bytes=None):
    """Download one tile into folder through <tile>.part, resuming a .part left by an earlier
    run or attempt (HTTP Range), with retries. Returns the final path."""
    dest = os.path.join(folder, tile["tile"])
    part = dest + ".part"
    expected = int(tile["bytes"] or 0)
    err = None
    for attempt in range(retries):
        _check_cancel(feedback)
        start = os.path.getsize(part) if os.path.isfile(part) else 0
        if expected and start > expected:
            os.remove(part)
            start = 0
        if expected and start == expected:
            os.replace(part, dest)
            return dest
        try:
            status, headers, body = http.open_range(tile["url"], start)
            try:
                if start and status != 206:              # the server ignored the Range: start over
                    start = 0
                mode = "ab" if start else "wb"
                with open(part, mode) as f:
                    while True:
                        _check_cancel(feedback)
                        chunk = body.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        if on_bytes:
                            on_bytes(len(chunk))
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
            got = os.path.getsize(part)
            if expected and got != expected:
                raise TileError("{}: {} of {} bytes".format(tile["tile"], got, expected))
            os.replace(part, dest)
            return dest
        except Cancelled:
            raise
        except Exception as e:                           # noqa - retried, then reported
            err = e
            _info(feedback, "{}: attempt {} of {} failed ({}){}".format(
                tile["tile"], attempt + 1, retries, e, "; retrying" if attempt + 1 < retries else ""))
            if attempt + 1 < retries:
                sleep(min(60, 5 * 2 ** attempt))
    raise TileError("{}: download failed after {} attempts ({})".format(tile["tile"], retries, err))


def verify_tile(path, expected_bytes=0, cell_tol=0.05):
    """Raise TileError unless the file has the expected size and GDAL opens it as a 1 m grid with
    a CRS. Returns {"epsg", "cell_m", "size"}."""
    size = os.path.getsize(path)
    if expected_bytes and size != int(expected_bytes):
        raise TileError("{}: {} bytes, expected {}".format(os.path.basename(path), size, expected_bytes))
    from osgeo import osr
    try:
        ds = GS.open_raster(path)
    except RuntimeError as e:
        raise TileError("{}: GDAL can't open it ({})".format(os.path.basename(path), e))
    gt = ds.GetGeoTransform()
    wkt = ds.GetProjection()
    ds = None
    cx, cy = abs(gt[1]), abs(gt[5])
    if abs(cx - 1.0) > cell_tol or abs(cy - 1.0) > cell_tol:
        raise TileError("{}: cell {:.3f} x {:.3f} m, not 1 m".format(os.path.basename(path), cx, cy))
    if not wkt:
        raise TileError("{}: no CRS".format(os.path.basename(path)))
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.AutoIdentifyEPSG()
    return {"epsg": srs.GetAuthorityCode(None) or "", "cell_m": round(cx, 4), "size": size}


def fetch(plan, http, feedback=None, verify=verify_tile, sleep=time.sleep, pct=(5, 90)):
    """Download every tile of the plan that is not already here, check it, and record it in the
    manifest (after each tile, so a cancel or a failure keeps what finished). Returns
    (entries, failed): manifest entries of the plan's tiles that are here, and messages."""
    folder = plan["folder"]
    os.makedirs(folder, exist_ok=True)
    manifest = load_manifest(folder)
    todo = [t for t in plan["tiles"] if not t.get("present")]
    need = max(1, plan["need_bytes"])
    done = [0]
    lo, hi = pct

    def tick(n):
        done[0] += n
        _progress(feedback, lo + (hi - lo) * min(1.0, done[0] / need))

    failed = []
    for i, t in enumerate(plan["tiles"]):
        _check_cancel(feedback)
        if t.get("present"):
            _info(feedback, "{}: already here, skipped".format(t["tile"]))
            continue
        _info(feedback, "{} ({} of {}, {})".format(t["tile"], todo.index(t) + 1, len(todo), fmt_bytes(t["bytes"])))
        try:
            path = download_tile(t, folder, http, feedback, sleep=sleep, on_bytes=tick)
            facts = verify(path, t["bytes"])
        except Cancelled:
            save_manifest(folder, manifest)
            raise
        except TileError as e:
            failed.append(str(e))
            _info(feedback, "!!! {}".format(e))
            bad = os.path.join(folder, t["tile"])
            if os.path.isfile(bad) and "download failed" not in str(e):
                os.replace(bad, bad + ".bad")              # kept for a look, never used
            continue
        manifest["tiles"][t["tile"]] = {
            "tile": t["tile"], "project": t["project"], "key": t["key"], "url": t["url"],
            "publication_date": t["publication_date"], "collection_begin": t.get("collection_begin", ""),
            "collection_end": t.get("collection_end", ""), "bytes": os.path.getsize(path),
            "sha256": sha256(path), "epsg": facts.get("epsg", ""),
            "downloaded": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
        save_manifest(folder, manifest)
    entries = [dict(manifest["tiles"][t["tile"]], path=os.path.join(folder, t["tile"]))
               for t in plan["tiles"] if t["tile"] in manifest["tiles"]
               and os.path.isfile(os.path.join(folder, t["tile"]))]
    order = {t["tile"]: i for i, t in enumerate(plan["tiles"])}
    entries.sort(key=lambda e: order.get(e["tile"], 0))
    return entries, failed


# ----------------------------------------------------------------------------------- VRT


def _epsg_of(path):
    from osgeo import osr
    ds = GS.open_raster(path)
    wkt = ds.GetProjection()
    ds = None
    if not wkt:
        return ""
    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.AutoIdentifyEPSG()
    code = srs.GetAuthorityCode(None)
    return "EPSG:{}".format(code) if code else wkt


def _bounds_in(bounds_ll, dst):
    from osgeo import osr
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    d = osr.SpatialReference()
    d.SetFromUserInput(dst)
    d.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, d)
    w, s, e, n = bounds_ll
    pts = [ct.TransformPoint(w + (e - w) * i / 4.0, s + (n - s) * j / 4.0)[:2]
           for i in range(5) for j in range(5)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def fallback_unit(path):
    """(choice, how) of the elevation unit a fallback DEM records (core/dem_zunit.recorded, the
    Runner's 'Detect from the raster' rule), or ("", why)."""
    try:
        ds = GS.open_raster(path)
    except RuntimeError as e:
        return "", str(e)
    try:
        return ZU.recorded(ds, path)
    finally:
        ds = None


def _exact_scale(vrt_path, f):
    """Write the scale VRT's ScaleRatio with all its digits: GDAL writes 6 significant digits
    (0.304801 for 1200/3937, 1.3 ppm off)."""
    with open(vrt_path, encoding="utf-8") as fh:
        xml = fh.read()
    fixed = re.sub(r"<ScaleRatio>[^<]*</ScaleRatio>",
                   "<ScaleRatio>{!r}</ScaleRatio>".format(float(f)), xml)
    if fixed != xml:
        with open(vrt_path, "w", encoding="utf-8") as fh:
            fh.write(fixed)


def build_vrt(entries, vrt_path, fallback="", bounds_ll=None, meta=None, feedback=None):
    """The per-dam VRT: the coarser `fallback` DEM (if given) at the bottom, then the tiles oldest
    collection first, so the newest lidar is on top and the fallback shows only where no tile has
    data. Everything is in the CRS most tiles have (other UTM zones and the fallback are warped
    into it as VRTs beside this one), at 1 m. bounds_ll clips the VRT to the dam's box. Writes
    <vrt>.json with the sources. Returns (vrt_path, json_path).

    The VRT is tagged metres (band unit 'm'), so the fallback must be metres too (0.17.50): one
    in feet or US survey feet (its band unit, vertical CRS or Download DEM record says so) is
    converted (x 0.3048 / x 1200/3937) before it goes under the tiles; one that records no unit is
    not used (warning; json 'fallback_refused'). Before, a feet fallback showed through the
    VRT as metres and the Runner's 'Detect from the raster' read it so."""
    from osgeo import gdal
    if not entries:
        raise TileError("no lidar tiles to build the VRT from")
    out_dir = os.path.dirname(os.path.abspath(vrt_path))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(vrt_path))[0]
    crs_of = {e["path"]: _epsg_of(e["path"]) for e in entries}
    counts = {}
    for c in crs_of.values():
        counts[c] = counts.get(c, 0) + 1
    target = max(sorted(counts), key=lambda c: counts[c])
    sources, warped = [], []
    fb_units, refused = "", ""
    if fallback:
        fb_got, fb_how = fallback_unit(fallback)
        if not fb_got:
            refused = "{}: {}".format(fallback, fb_how)
            _warn(feedback, "The fallback DEM {} is not used: {}. The 1 m VRT is tagged metres, so "
                            "the DEM underneath must record its unit (band unit, vertical CRS, or "
                            "a USGS 3DEP Download DEM GeoTIFF); tag it (gdal_edit -units m, or ft) "
                            "or give the Download DEM 10 m GeoTIFF. Where no lidar tile has data "
                            "the VRT has holes.".format(os.path.basename(fallback), fb_how))
            fallback = ""
    if fallback:
        fb = os.path.join(out_dir, "{}_fallback.vrt".format(stem))
        # the values as stored (-novshift): the conversion below is the only one. GDAL's default
        # vertical shift already turned a feet fallback into metres when its CRS or the tiles'
        # is compound, and the scale VRT then converted it again (951 ft read as 88.4 m)
        GS.checked(gdal.Warp(fb, fallback, **no_vshift(gdal, dict(
            format="VRT", dstSRS=target, resampleAlg="bilinear", outputType=gdal.GDT_Float32,
            dstNodata=-999999))), "warping " + fallback)
        warped.append(fb)
        if fb_got == ZU.METERS:
            fb_units = "metres ({})".format(fb_how)
        else:
            f = ZU.TO_M[fb_got]
            fbm = os.path.join(out_dir, "{}_fallback_m.vrt".format(stem))
            out = GS.checked(gdal.Translate(fbm, fb, format="VRT", outputType=gdal.GDT_Float32,
                                            scaleParams=[[0.0, 1.0, 0.0, f]]),
                             "converting " + fallback)
            out = None
            _exact_scale(fbm, f)
            warped.append(fbm)
            fb = fbm
            fb_units = "{} ({}), converted x {:.10g} to metres".format(ZU.NAMES[fb_got], fb_how, f)
            _warn(feedback, "The fallback DEM {} is in {} ({}): converted to metres (x {:.10g}) "
                            "under the 1 m tiles.".format(os.path.basename(fallback),
                                                          ZU.NAMES[fb_got], fb_how, f))
        sources.append(fb)
    for e in entries:
        p = e["path"]
        if crs_of[p] != target:
            w = os.path.join(out_dir, "{}_{}_warped.vrt".format(stem, os.path.splitext(e["tile"])[0]))
            GS.checked(gdal.Warp(w, p, **no_vshift(gdal, dict(       # metres stay metres
                format="VRT", dstSRS=target, xRes=1.0, yRes=1.0, resampleAlg="bilinear",
                dstNodata=-999999))), "warping " + p)
            sources.append(w)
            warped.append(w)
            _info(feedback, "{} is in {}; warped to {} for the VRT".format(e["tile"], crs_of[p], target))
        else:
            sources.append(p)
    # the 1 m grid on whole metres, as the tiles are: a fallback or a box with another origin
    # must not shift the tiles by part of a cell (that would resample every lidar cell)
    opts = {"resolution": "user", "xRes": 1.0, "yRes": 1.0, "targetAlignedPixels": True,
            "resampleAlg": "bilinear", "VRTNodata": -999999}
    if bounds_ll:
        opts["outputBounds"] = _bounds_in(bounds_ll, target)
    try:
        ds = GS.checked(gdal.BuildVRT(vrt_path, sources, options=gdal.BuildVRTOptions(**opts)),
                        "building " + vrt_path)
    except RuntimeError as e:
        raise TileError(str(e))
    ds.GetRasterBand(1).SetUnitType("m")
    ds = None
    projects = []
    for e in entries:
        if e["project"] not in projects:
            projects.append(e["project"])
    info = {"format": "damsuite/lidar_1m_vrt", "vrt": vrt_path, "crs": target, "units": "metres",
            "vertical_datum": "NAVD88", "cell_m": 1.0,
            "source": "USGS 3DEP 1 m lidar DEM tiles (native, The National Map)",
            "projects": [{"project": p,
                          "collection_begin": min((e.get("collection_begin") or "") for e in entries if e["project"] == p),
                          "collection_end": max((e.get("collection_end") or "") for e in entries if e["project"] == p),
                          "publication_date": max((e.get("publication_date") or "") for e in entries if e["project"] == p),
                          "tiles": sum(1 for e in entries if e["project"] == p)} for p in projects],
            "tiles": [{k: e.get(k, "") for k in ("tile", "project", "collection_begin", "collection_end",
                                                  "publication_date", "url", "sha256", "bytes", "path")}
                      for e in entries],
            "order": "bottom to top: fallback, then tiles oldest collection first (newest on top)",
            "fallback": fallback or "", "fallback_units": fb_units, "fallback_refused": refused,
            "warped_helpers": warped, "bbox_ll": list(bounds_ll or []),
            "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    info.update(meta or {})
    js = os.path.splitext(vrt_path)[0] + ".json"
    with open(js, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    return vrt_path, js


# ----------------------------------------------------------------------------- extents


def domain_of_dam_folder(folder, pad_m=500.0, buffer_mi=None):
    """(bbox_ll, dam_id, how) for a dam folder: the Runner's final model bounds
    (_breach_model/state.json final_bounds, EPSG:5070) padded by pad_m, else dam.json's lon/lat
    with buffer_mi. Read only."""
    dam_id, lon, lat = "", None, None
    try:
        with open(os.path.join(folder, "dam.json"), encoding="utf-8") as f:
            fields = (json.load(f) or {}).get("fields") or {}
        dam_id = str(fields.get("dam_id") or "")
        lon, lat = float(fields["lon"]), float(fields["lat"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        with open(os.path.join(folder, "_breach_model", "state.json"), encoding="utf-8") as f:
            b = (json.load(f) or {}).get("final_bounds")
        if b and len(b) == 4:
            x0, y0, x1, y1 = [float(v) for v in b]
            return (box_5070_to_ll((x0 - pad_m, y0 - pad_m, x1 + pad_m, y1 + pad_m)), dam_id,
                    "Runner model bounds + {:.0f} m".format(pad_m))
    except (OSError, ValueError, TypeError):
        pass
    if lon is not None and buffer_mi:
        return point_box_ll(lon, lat, buffer_mi), dam_id, "dam point + {} mi".format(buffer_mi)
    raise ValueError("{}: no Runner model bounds and no dam point in dam.json".format(folder))


def box_5070_to_ll(box):
    from osgeo import osr
    src = osr.SpatialReference()
    src.ImportFromEPSG(5070)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    x0, y0, x1, y1 = box
    pts = [ct.TransformPoint(x, y)[:2] for x in (x0, (x0 + x1) / 2, x1) for y in (y0, (y0 + y1) / 2, y1)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def point_box_ll(lon, lat, buffer_mi):
    import math
    dlat = float(buffer_mi) / 69.0
    dlon = float(buffer_mi) / (69.0 * max(0.2, math.cos(math.radians(float(lat)))))
    return (float(lon) - dlon, float(lat) - dlat, float(lon) + dlon, float(lat) + dlat)


def parse_bbox(text):
    """'west,south,east,north' in degrees -> tuple, or None for empty text."""
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        w, s, e, n = [float(v) for v in raw.replace(" ", "").split(",")[:4]]
    except ValueError:
        raise ValueError("Box must be west,south,east,north in degrees")
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise ValueError("Box must be west,south,east,north in degrees (west < east, south < north)")
    return (w, s, e, n)
