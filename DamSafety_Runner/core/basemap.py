"""Aerial base map under the QA maps (3.11.0; Docs & QA spec SPEC_aerial_basemap_outputs, Nick).

qa_inflow_map.png and qa_depth_map.png are drawn over a recent aerial photo with street and town
labels, a scale bar (m and ft), a true-north arrow and a credit line naming the imagery, its
flight date(s), the CRS and the plugin version. No QGIS import: urllib + GDAL + numpy, so the
command-line runner and the worker thread draw the same maps.

Imagery (SOURCES, tried in SOURCE_ORDER until one answers with a picture of the map):
  nd_naip_2025        ND DWR WMS aerial.dwr.nd.gov/dwrwms, layer NorthDakota_NAIP_2025_col_air
                      (USDA-FSA NAIP 2025, 0.6 m, flown Jul-Sep 2025), GetMap 1.1.1 asked
                      directly in the map's CRS (EPSG:5070 for the suite) - no warp.
  esri_world_imagery  Esri World Imagery XYZ tiles (EPSG:3857), mosaicked and warped onto the map.
  usgs_imagery        USGS National Map imagery XYZ tiles (the Hazard AERIAL_IMAGERY_URL).
A source that times out, sends an HTML / XML page (a login or error page) instead of an image,
or sends a blank picture (outside its coverage: the ND WMS outside North Dakota) is skipped.
Labels: Esri Reference/World_Transportation + Reference/World_Boundaries_and_Places tiles at one
zoom level coarser than the imagery (readable text), at most 17, drawn as an RGBA image ABOVE
the depth.
Flight dates: ND GIS Hub Imagery/Dates_Imagery layer 41 (the NAIP 2025 seamlines, IDATE) for the
NAIP; the World Imagery 'identify' (DATE (YYYYMMDD)) for Esri; USGS publishes none.

Offline (ND_DAM_OFFLINE / NDBREACH_BASEMAP_OFFLINE / ND_DAM_HAZARD_OFFLINE_ONLY true, or
ND_DAM_SUITE set - the suite's no-internet rule; or every source failed): a GeoTIFF / MBTiles in
<pack>/imagery/ that covers the map (pack_dirs()), else the DEM hillshade, and the credit says
which ("Imagery not available (offline): hillshade of <DEM>").

Time: every map has a NETWORK budget (BUDGET_S, 30 s); each request's timeout is what is left
of it; when it is spent the next sources are not asked. The whole thing sits inside
qa_maps.write_qa_maps()'s never-fail try.

Cache: answers are kept under the user's cache folder (cache_dir(): %LOCALAPPDATA% /
~/Library/Caches / $XDG_CACHE_HOME, then DamSafety/basemap_cache; NDBREACH_BASEMAP_CACHE sets
another folder, "off" none), file names = md5 of the request URL (ASCII), 90 days, the oldest
dropped past 200 MB (NDBREACH_BASEMAP_CACHE_MB). Never inside the run or input folders
(Cache.usable): a cache folder under one is not used. GDAL reads with GDAL_PAM_ENABLED=NO so no
.aux.xml is written anywhere.

Network: the proxy set in QGIS (damsuite.netproxy, Settings > Options > Network), else the
system's; certificates are checked against the system store plus certifi when it is installed
(macOS python.org builds have no system store); never turned off.

Overrides (no new plugin version needed): NDBREACH_BASEMAP_ORDER = comma-separated source ids;
NDBREACH_BASEMAP_SOURCES = a JSON file {"sources": [...], "order": [...], "labels": [...]}
(entries like SOURCES, same id replaces a built-in one).
"""
import hashlib
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
import uuid

import numpy as np

MODES = ("auto", "imagery", "hillshade")
MODE_LABELS = ("Auto (aerial imagery; hillshade when offline)", "Aerial imagery",
               "Hillshade only")
DEFAULT_MODE = "auto"

NAIP_2025_DATES_URL = ("https://ndgishub.nd.gov/arcgis/rest/services/Imagery/Dates_Imagery/"
                       "MapServer/41")
ESRI_IMAGERY_IDENTIFY = ("https://services.arcgisonline.com/arcgis/rest/services/World_Imagery/"
                         "MapServer/identify")
SOURCES = [
    {"id": "nd_naip_2025", "kind": "wms", "url": "https://aerial.dwr.nd.gov/dwrwms",
     "layer": "NorthDakota_NAIP_2025_col_air", "format": "image/jpeg",
     "name": "USDA-FSA NAIP 2025 natural colour (ND DWR WMS)", "short": "NAIP 2025",
     "res": "0.6 m", "credit": "USDA-FSA NAIP 2025 via ND DWR (aerial.dwr.nd.gov)",
     "date_service": "naip:" + NAIP_2025_DATES_URL},
    {"id": "esri_world_imagery", "kind": "xyz",
     "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/"
            "{z}/{y}/{x}", "zmax": 19,
     "name": "Esri World Imagery", "short": "Esri World Imagery", "res": "0.3-1 m",
     "credit": "Esri, Maxar, Earthstar Geographics, and the GIS User Community",
     "date_service": "esri:" + ESRI_IMAGERY_IDENTIFY},
    {"id": "usgs_imagery", "kind": "xyz",
     "url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/"
            "tile/{z}/{y}/{x}", "zmax": 16,
     "name": "USGS National Map imagery", "short": "USGS imagery", "res": "about 1 m",
     "credit": "USGS The National Map", "date_service": ""},
]
SOURCE_ORDER = ("nd_naip_2025", "esri_world_imagery", "usgs_imagery")
LABEL_URLS = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/"
    "MapServer/tile/{z}/{y}/{x}",
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/"
    "World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
)
LABEL_CREDIT = "Esri reference (Esri, HERE, Garmin, OSM contributors)"
LABEL_ZMAX = 17
LABEL_ALPHA = 0.95

BUDGET_S = 30.0               # network seconds per map (all sources, labels and dates)
REQUEST_TIMEOUT_S = 15.0      # one request at most (less when the budget is nearly spent)
MAX_TILES = 64                # per XYZ mosaic (the zoom is lowered until it fits)
MAX_PX = 1400                 # longest side of the picture asked for
MAX_BYTES = 25 * 1024 * 1024  # one answer
CACHE_MB = 200.0
CACHE_TTL_DAYS = 90.0
USER_AGENT = "ND-Dam-Suite/DamSafety-Runner (QA maps; dam safety)"
OFFLINE_ENVS = ("ND_DAM_OFFLINE", "NDBREACH_BASEMAP_OFFLINE", "ND_DAM_HAZARD_OFFLINE_ONLY")
SUITE_ENVS = ("ND_DAM_SUITE",)
PACK_EXTS = (".tif", ".tiff", ".mbtiles")
R_EARTH = 6378137.0
ORIGIN = math.pi * R_EARTH
_IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"II*\x00", b"MM\x00*")


class BasemapError(Exception):
    """A source did not give a usable picture (the next one is tried)."""


def normalize_mode(v):
    """A qa_basemap value (name, label, index, None) -> one of MODES."""
    if v is None or v == "":
        return DEFAULT_MODE
    if isinstance(v, (int, np.integer)) and not isinstance(v, bool):
        if 0 <= int(v) < len(MODES):
            return MODES[int(v)]
        raise ValueError("qa_basemap index {} is not one of 0-{}".format(v, len(MODES) - 1))
    m = str(v).strip().lower()
    for name, label in zip(MODES, MODE_LABELS):
        if m in (name, label.lower()):
            return name
    if m in ("aerial", "on", "online"):
        return "imagery"
    if m in ("off", "none", "no", "dem", "terrain"):
        return "hillshade"
    raise ValueError("qa_basemap must be one of {} (got {!r})".format(", ".join(MODES), v))


def _truthy(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def offline():
    """(True, why) when the maps must not ask the internet."""
    for k in OFFLINE_ENVS:
        if _truthy(os.environ.get(k)):
            return True, "offline mode ({})".format(k)
    for k in SUITE_ENVS:
        if str(os.environ.get(k) or "").strip():
            return True, "offline mode (suite: {} set)".format(k)
    return False, ""


# ----------------------------------------------------------------------------- sources --
def sources():
    """(sources by id, order, label urls) with the overrides applied."""
    by_id = {s["id"]: dict(s) for s in SOURCES}
    order = list(SOURCE_ORDER)
    labels = list(LABEL_URLS)
    path = os.environ.get("NDBREACH_BASEMAP_SOURCES", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for s in d.get("sources") or []:
                if isinstance(s, dict) and s.get("id") and s.get("url"):
                    by_id[str(s["id"])] = dict(by_id.get(str(s["id"]), {}), **s)
            if d.get("order"):
                order = [str(i) for i in d["order"]]
            if d.get("labels") is not None:
                labels = [str(u) for u in d["labels"]]
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    env = os.environ.get("NDBREACH_BASEMAP_ORDER", "").strip()
    if env:
        order = [i.strip() for i in env.split(",") if i.strip()]
    return by_id, [i for i in order if i in by_id], labels


# ------------------------------------------------------------------------------- cache --
def cache_dir():
    """The user cache folder of the base map tiles ('' = no cache)."""
    v = os.environ.get("NDBREACH_BASEMAP_CACHE", "").strip()
    if v.lower() in ("off", "0", "none", "no"):
        return ""
    if v:
        return v
    if sys.platform.startswith("win"):
        root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"),
                                                              "AppData", "Local")
    elif sys.platform == "darwin":
        root = os.path.join(os.path.expanduser("~"), "Library", "Caches")
    else:
        root = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(root, "DamSafety", "basemap_cache")


def _inside(path, folder):
    try:
        p = os.path.normcase(os.path.realpath(path))
        f = os.path.normcase(os.path.realpath(folder))
    except (OSError, ValueError):
        return False
    return p == f or p.startswith(f.rstrip(os.sep) + os.sep)


class Cache(object):
    """md5(url) -> bytes on disk; TTL and an LRU size cap (trim())."""

    def __init__(self, root=None, cap_mb=None, ttl_days=CACHE_TTL_DAYS, avoid=()):
        self.root = cache_dir() if root is None else root
        if cap_mb is None:
            try:
                cap_mb = float(os.environ.get("NDBREACH_BASEMAP_CACHE_MB", CACHE_MB))
            except ValueError:
                cap_mb = CACHE_MB
        self.cap = max(0.0, cap_mb) * 1024 * 1024
        self.ttl = ttl_days * 86400.0
        self.hits = 0
        self.writes = 0
        if self.root and (self.cap <= 0 or any(a and _inside(self.root, a) for a in avoid)):
            self.root = ""                  # never a cache inside a run / input folder

    def _path(self, url):
        h = hashlib.md5(url.encode("utf-8")).hexdigest()
        return os.path.join(self.root, h[:2], h + ".bin")

    def get(self, url):
        if not self.root:
            return None
        p = self._path(url)
        try:
            st = os.stat(p)
            if time.time() - st.st_mtime > self.ttl or st.st_size == 0:
                return None
            with open(p, "rb") as f:
                data = f.read()
            os.utime(p, None)                 # recently used (LRU by mtime)
        except OSError:
            return None
        self.hits += 1
        return data

    def put(self, url, data):
        if not self.root or not data:
            return
        p = self._path(url)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = "{}.{}.tmp".format(p, uuid.uuid4().hex[:8])
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            self.writes += 1
        except OSError:
            pass

    def trim(self):
        """Drop expired files, then the least recently used past the cap. Never raises."""
        if not self.root or not os.path.isdir(self.root):
            return 0
        files, total, now, gone = [], 0, time.time(), 0
        try:
            for d in os.scandir(self.root):
                if not d.is_dir():
                    continue
                for f in os.scandir(d.path):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if now - st.st_mtime > self.ttl or f.name.endswith(".tmp") and \
                            now - st.st_mtime > 3600:
                        try:
                            os.remove(f.path)
                            gone += 1
                        except OSError:
                            pass
                        continue
                    files.append((st.st_mtime, st.st_size, f.path))
                    total += st.st_size
        except OSError:
            return gone
        if total > self.cap:
            for _t, size, p in sorted(files):
                try:
                    os.remove(p)
                    gone += 1
                    total -= size
                except OSError:
                    pass
                if total <= 0.9 * self.cap:
                    break
        return gone


# ----------------------------------------------------------------------------- network --
def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:                              # noqa: BLE001 - certifi is optional
        pass
    return ctx


def _open(url, timeout):
    """urllib through QGIS's proxy when one is set (damsuite.netproxy), certificates checked."""
    proxy = None
    try:
        from ..damsuite import netproxy
        proxy = netproxy.proxy_for(url)
    except Exception:                              # noqa: BLE001 - no damsuite / QGIS
        proxy = None
    handlers = [urllib.request.HTTPSHandler(context=_ssl_context())]
    if proxy is not None:
        handlers.append(urllib.request.ProxyHandler({} if proxy == "" else
                                                    {"http": proxy, "https": proxy}))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


class Net(object):
    """Requests of one map: the cache, the time budget, counts. get() raises BasemapError."""

    def __init__(self, budget_s=BUDGET_S, cache=None, opener=None):
        self.deadline = time.time() + float(budget_s)
        self.cache = cache if cache is not None else Cache(root="")
        self.opener = opener or _open
        self.requests = 0
        self.failures = []

    def left(self):
        return self.deadline - time.time()

    def get(self, url, want="image"):
        data = self.cache.get(url)
        if data is not None:
            return data
        left = self.left()
        if left < 0.5:
            raise BasemapError("network time budget spent")
        self.requests += 1
        try:
            with self.opener(url, min(REQUEST_TIMEOUT_S, left)) as r:
                data = r.read(MAX_BYTES + 1)
        except Exception as e:                     # noqa: BLE001 - timeouts, HTTP, SSL, ...
            raise BasemapError("{}: {}".format(type(e).__name__, e))
        if len(data) > MAX_BYTES:
            raise BasemapError("answer too large")
        head = data[:512].lstrip().lower()
        if want == "image":
            if not data.startswith(_IMAGE_MAGIC):
                what = "an HTML page (login / error?)" if head.startswith((b"<!doctype", b"<html")) \
                    else "an XML error" if head.startswith(b"<") else "no image"
                raise BasemapError("the service sent {}".format(what))
        elif want == "json":
            try:
                j = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise BasemapError("the service did not send JSON")
            if isinstance(j, dict) and "error" in j:
                raise BasemapError("the service answered an error: {}".format(j.get("error")))
        self.cache.put(url, data)
        return data


# ------------------------------------------------------------------------- GDAL helpers --
class _NoPam(object):
    """GDAL_PAM_ENABLED=NO (no .aux.xml anywhere) for the reads inside."""

    def __enter__(self):
        from osgeo import gdal
        self.old = gdal.GetConfigOption("GDAL_PAM_ENABLED")
        gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")
        return self

    def __exit__(self, *a):
        from osgeo import gdal
        gdal.SetConfigOption("GDAL_PAM_ENABLED", self.old)
        return False


def _srs(wkt_or_epsg):
    from osgeo import osr
    s = osr.SpatialReference()
    if isinstance(wkt_or_epsg, int):
        s.ImportFromEPSG(wkt_or_epsg)
    else:
        s.ImportFromWkt(str(wkt_or_epsg))
    try:
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return s


def epsg_of(crs_wkt):
    """EPSG code of a WKT (None when it has none)."""
    try:
        s = _srs(crs_wkt)
        s.AutoIdentifyEPSG()
        code = s.GetAuthorityCode(None)
        return int(code) if code else None
    except Exception:                              # noqa: BLE001
        return None


def crs_label(crs_wkt):
    code = epsg_of(crs_wkt) if crs_wkt else None
    if code:
        return "EPSG:{}".format(code)
    try:
        return _srs(crs_wkt).GetName() or "CRS of the run"
    except Exception:                              # noqa: BLE001
        return "CRS of the run"


def metres_per_unit(crs_wkt):
    try:
        v = float(_srs(crs_wkt).GetLinearUnits())
        return v if v > 0 else 1.0
    except Exception:                              # noqa: BLE001
        return 1.0


def _transform(pts, src_wkt, dst):
    from osgeo import osr
    ct = osr.CoordinateTransformation(_srs(src_wkt), _srs(dst))
    return np.array([ct.TransformPoint(float(x), float(y))[:2] for x, y in pts])


def _edge_points(bbox, n=8):
    x0, y0, x1, y1 = bbox
    t = np.linspace(0, 1, n)
    xs = np.concatenate([x0 + (x1 - x0) * t, np.full(n, x1), x0 + (x1 - x0) * t, np.full(n, x0)])
    ys = np.concatenate([np.full(n, y0), y0 + (y1 - y0) * t, np.full(n, y1), y0 + (y1 - y0) * t])
    return list(zip(xs, ys))


def decode(data):
    """Image bytes (JPEG / PNG / GIF / TIFF) -> RGBA uint8 (h, w, 4) with GDAL /vsimem/."""
    from osgeo import gdal
    name = "/vsimem/ndb_basemap_{}".format(uuid.uuid4().hex)
    gdal.FileFromMemBuffer(name, bytes(data))
    try:
        with _NoPam():
            ds = gdal.Open(name)
        if ds is None:
            raise BasemapError("the picture cannot be read")
        return _rgba(ds)
    finally:
        ds = None
        gdal.Unlink(name)


def _rgba(ds):
    n = ds.RasterCount
    b1 = ds.GetRasterBand(1)
    ct = b1.GetColorTable()
    if n == 1 and ct is not None:
        lut = np.array([ct.GetColorEntry(i) for i in range(ct.GetCount())], dtype=np.uint8)
        if lut.shape[1] == 3:
            lut = np.concatenate([lut, np.full((len(lut), 1), 255, np.uint8)], axis=1)
        a = b1.ReadAsArray()
        return lut[np.clip(a, 0, len(lut) - 1)]
    bands = [ds.GetRasterBand(i + 1).ReadAsArray().astype(np.uint8) for i in range(min(n, 4))]
    if n == 1:
        bands = bands * 3 + [np.full_like(bands[0], 255)]
    elif n == 2:
        bands = [bands[0]] * 3 + [bands[1]]
    elif n == 3:
        bands = bands + [np.full_like(bands[0], 255)]
    return np.dstack(bands)


def _mem(rgba, gt, wkt):
    from osgeo import gdal
    h, w = rgba.shape[:2]
    ds = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    ds.SetGeoTransform(gt)
    ds.SetProjection(wkt)
    for i in range(4):
        ds.GetRasterBand(i + 1).WriteArray(rgba[:, :, i])
    return ds


def warp(rgba, gt, src_wkt, bbox, w, h, dst_wkt):
    """RGBA on (gt, src_wkt) -> RGBA (h, w, 4) on the map grid `bbox` in dst_wkt (bilinear)."""
    from osgeo import gdal
    src = _mem(rgba, gt, src_wkt)
    dst = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    dst.SetGeoTransform(map_gt(bbox, w, h))
    dst.SetProjection(dst_wkt)
    gdal.ReprojectImage(src, dst, src_wkt, dst_wkt, gdal.GRA_Bilinear)
    out = np.dstack([dst.GetRasterBand(i + 1).ReadAsArray() for i in range(4)]).astype(np.uint8)
    src = dst = None
    return out


def map_gt(bbox, w, h):
    x0, y0, x1, y1 = bbox
    return (x0, (x1 - x0) / float(w), 0.0, y1, 0.0, -(y1 - y0) / float(h))


def _blank(rgba):
    """True for a picture with nothing on it (outside the service's coverage)."""
    a = rgba[:, :, 3] > 0
    if a.mean() < 0.5:
        return True
    rgb = rgba[:, :, :3][a].astype(float)
    return float(rgb.std(axis=0).max()) < 2.0


# ------------------------------------------------------------------------------- fetch --
def wms(src, bbox, w, h, crs_wkt, net):
    """A WMS 1.1.1 GetMap of the map, asked in its own CRS when it has an EPSG code (else in
    EPSG:3857 and warped)."""
    code = epsg_of(crs_wkt)
    if code:
        req_bbox, srs = bbox, code
    else:
        p = _transform(_edge_points(bbox), crs_wkt, 3857)
        req_bbox, srs = (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()), 3857
    q = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": src.get("layer", ""),
         "STYLES": "", "SRS": "EPSG:{}".format(srs),
         "BBOX": ",".join("{:.3f}".format(v) for v in req_bbox), "WIDTH": int(w),
         "HEIGHT": int(h), "FORMAT": src.get("format") or "image/jpeg"}
    base = src["url"]
    url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode(q, safe=",:/")
    rgba = decode(net.get(url))
    if rgba.shape[:2] != (int(h), int(w)):
        raise BasemapError("the WMS sent {}x{} px, not {}x{}".format(
            rgba.shape[1], rgba.shape[0], int(w), int(h)))
    if srs != code:
        rgba = warp(rgba, map_gt(req_bbox, w, h), _srs(3857).ExportToWkt(), bbox, w, h, crs_wkt)
    return rgba


def tile_zoom(bbox, w, crs_wkt, zmax=19, coarser=0, zmin=1):
    """XYZ level whose pixel matches the map's (one finer than the next lower level)."""
    p = _transform(_edge_points(bbox), crs_wkt, 3857)
    mpp = (p[:, 0].max() - p[:, 0].min()) / float(w)
    z = int(round(math.log2(2 * ORIGIN / 256.0 / max(mpp, 1e-6)))) - int(coarser)
    return max(zmin, min(int(zmax), z)), p


def _tile_range(p, z):
    ts = 2 * ORIGIN / 2 ** z
    n = 2 ** z - 1
    tx0 = int((p[:, 0].min() + ORIGIN) // ts)
    tx1 = int((p[:, 0].max() + ORIGIN) // ts)
    ty0 = int((ORIGIN - p[:, 1].max()) // ts)
    ty1 = int((ORIGIN - p[:, 1].min()) // ts)
    return max(0, tx0), min(n, tx1), max(0, ty0), min(n, ty1), ts


def xyz(url, bbox, w, h, crs_wkt, net, zmax=19, coarser=0, zmin=1, need=0.5):
    """XYZ tiles (EPSG:3857) mosaicked and warped onto the map. Returns (rgba, z)."""
    z, p = tile_zoom(bbox, w, crs_wkt, zmax, coarser, zmin)
    while True:
        tx0, tx1, ty0, ty1, ts = _tile_range(p, z)
        if (tx1 - tx0 + 1) * (ty1 - ty0 + 1) <= MAX_TILES or z <= zmin:
            break
        z -= 1
    M = np.zeros(((ty1 - ty0 + 1) * 256, (tx1 - tx0 + 1) * 256, 4), np.uint8)
    got, tried, last = 0, 0, ""
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            tried += 1
            u = url.replace("{z}", str(z)).replace("{x}", str(tx)).replace("{y}", str(ty))
            try:
                t = decode(net.get(u))
            except BasemapError as e:
                last = str(e)
                if "budget" in last or (got == 0 and tried >= 2):
                    raise BasemapError(last)
                continue
            if t.shape[:2] != (256, 256):
                continue
            M[(ty - ty0) * 256:(ty - ty0 + 1) * 256, (tx - tx0) * 256:(tx - tx0 + 1) * 256] = t
            got += 1
    if got < need * tried:
        raise BasemapError("{} of {} tiles ({})".format(got, tried, last or "no tiles"))
    gt = (tx0 * ts - ORIGIN, ts / 256.0, 0.0, ORIGIN - ty0 * ts, 0.0, -ts / 256.0)
    return warp(M, gt, _srs(3857).ExportToWkt(), bbox, w, h, crs_wkt), z


def labels(bbox, w, h, crs_wkt, net, urls=None, zmax=LABEL_ZMAX):
    """Esri reference tiles one level coarser than the map, composited -> RGBA float (h, w, 4)
    or None."""
    out = np.zeros((h, w, 4), np.float32)
    got = False
    for u in (LABEL_URLS if urls is None else urls):
        try:
            rgba, _z = xyz(u, bbox, w, h, crs_wkt, net, zmax=zmax, coarser=1, zmin=5, need=0.3)
        except BasemapError as e:
            net.failures.append("labels: {}".format(e))
            continue
        A = rgba.astype(np.float32) / 255.0
        a = A[:, :, 3:4]
        out[:, :, :3] = A[:, :, :3] * a + out[:, :, :3] * (1 - a)
        out[:, :, 3:4] = a + out[:, :, 3:4] * (1 - a)
        got = True
    if not got:
        return None
    a = out[:, :, 3:4]
    out[:, :, :3] = np.where(a > 1e-6, out[:, :, :3] / np.maximum(a, 1e-6), 0.0)  # straight
    out[:, :, 3] *= LABEL_ALPHA
    return out


def _lonlat_bbox(bbox, crs_wkt):
    p = _transform(_edge_points(bbox), crs_wkt, 4326)
    return p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()


def parse_date(v):
    """'2025-07-13', '7/13/2025', '20250713' -> '2025-07-13' (None otherwise)."""
    v = "" if v is None else str(v).strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        return "{:04d}-{:02d}-{:02d}".format(*map(int, m.groups()))
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        return "{:04d}-{:02d}-{:02d}".format(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", v)
    if m:
        return "{}-{}-{}".format(*m.groups())
    return None


def imagery_dates(src, bbox, crs_wkt, net):
    """Sorted flight dates of `src` over the map ('YYYY-MM-DD'), [] when the service publishes
    none or does not answer. The NAIP query is the same as Hazard's lone911.imagery_dates."""
    ds = str(src.get("date_service") or "")
    try:
        lon0, lat0, lon1, lat1 = _lonlat_bbox(bbox, crs_wkt)
        if ds.startswith("naip:"):
            q = {"geometry": "{:.6f},{:.6f},{:.6f},{:.6f}".format(lon0, lat0, lon1, lat1),
                 "geometryType": "esriGeometryEnvelope", "inSR": "4326",
                 "spatialRel": "esriSpatialRelIntersects", "outFields": "IDATE",
                 "returnGeometry": "false", "f": "json"}
            j = json.loads(net.get(ds[5:].rstrip("/") + "/query?" + urllib.parse.urlencode(q),
                                   "json").decode("utf-8"))
            vals = [(f.get("attributes") or {}).get("IDATE") for f in j.get("features") or []]
        elif ds.startswith("esri:"):
            cx, cy = (lon0 + lon1) / 2.0, (lat0 + lat1) / 2.0
            q = {"geometry": "{:.6f},{:.6f}".format(cx, cy), "geometryType": "esriGeometryPoint",
                 "sr": "4326", "layers": "all", "tolerance": "1",
                 "mapExtent": "{:.6f},{:.6f},{:.6f},{:.6f}".format(lon0, lat0, lon1, lat1),
                 "imageDisplay": "800,600,96", "returnGeometry": "false", "f": "json"}
            j = json.loads(net.get(ds[5:] + "?" + urllib.parse.urlencode(q), "json")
                           .decode("utf-8"))
            vals = []
            for r in j.get("results") or []:
                a = r.get("attributes") or {}
                vals += [a.get(k) for k in ("DATE (YYYYMMDD)", "SRC_DATE2", "SRC_DATE")]
        else:
            return []
    except (BasemapError, ValueError, TypeError, AttributeError, UnicodeDecodeError) as e:
        net.failures.append("dates: {}".format(e))
        return []
    return sorted({d for d in (parse_date(v) for v in vals) if d})


def date_text(dates):
    if not dates:
        return "flight date not published by the service"
    if len(dates) <= 3:
        return ", ".join(dates)
    return "{} to {}".format(dates[0], dates[-1])


# -------------------------------------------------------------------------- pack imagery --
def pack_dirs():
    """Folders holding offline imagery: <pack>/imagery for each pack / suite folder known."""
    roots = []
    v = os.environ.get("NDBREACH_BASEMAP_PACK", "").strip()
    if v:
        roots.append(v if os.path.basename(v.rstrip("/\\")).lower() == "imagery" else
                     os.path.join(v, "imagery"))
    for k in ("ND_DAM_HAZARD_OFFLINE_PACK", "ND_DAM_SUITE", "NDSUITE_HOME"):
        v = os.environ.get(k, "").strip()
        if not v:
            continue
        base = v if os.path.isdir(v) else os.path.dirname(v)
        roots.append(os.path.join(base, "imagery"))
    out = []
    for r in roots:
        if r and os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def pack_imagery(bbox, w, h, crs_wkt, dirs=None):
    """(rgba, file name, date or '') from a pack GeoTIFF / MBTiles covering the map, or None."""
    from osgeo import gdal
    for d in (pack_dirs() if dirs is None else dirs):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.lower().endswith(PACK_EXTS):
                continue
            path = os.path.join(d, n)
            try:
                with _NoPam():
                    ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
                    if ds is None or not _covers(ds, bbox, crs_wkt):
                        continue
                    mem = gdal.Warp("", ds, format="MEM", outputBounds=tuple(bbox), width=int(w),
                                    height=int(h), dstSRS=crs_wkt, resampleAlg="bilinear",
                                    dstAlpha=ds.RasterCount in (1, 3))
                    rgba = _rgba(mem)
                    md = ds.GetMetadata() or {}
                    mem = ds = None
            except Exception:                       # noqa: BLE001 - a broken file is skipped
                continue
            if _blank(rgba):
                continue
            date = ""
            for k in ("ACQUISITION_DATE", "DATE", "TIFFTAG_DATETIME", "date", "flown"):
                if md.get(k):
                    date = parse_date(str(md[k]).replace(":", "-", 2)) or str(md[k])
                    break
            return rgba, n, date
    return None


def _covers(ds, bbox, crs_wkt, frac=0.9):
    gt = ds.GetGeoTransform()
    wkt = ds.GetProjection()
    if not wkt:
        return False
    x0, y1 = gt[0], gt[3]
    x1, y0 = gt[0] + gt[1] * ds.RasterXSize, gt[3] + gt[5] * ds.RasterYSize
    p = _transform(_edge_points(bbox), crs_wkt, wkt)
    bx0, by0, bx1, by1 = p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()
    ix = max(0.0, min(x1, bx1) - max(x0, bx0))
    iy = max(0.0, min(max(y0, y1), by1) - max(min(y0, y1), by0))
    area = (bx1 - bx0) * (by1 - by0)
    return area > 0 and ix * iy >= frac * area


# ------------------------------------------------------------------------------ render --
class Basemap(object):
    """What render() found: rgb (h, w, 3 uint8) or None (draw the hillshade), labels (RGBA float)
    or None, and the record (info) for run_report.json."""

    def __init__(self):
        self.rgb = None
        self.labels = None
        self.info = {"mode": DEFAULT_MODE, "source": "hillshade", "name": "", "res": "",
                     "dates": [], "labels": False, "fallback": "", "requests": 0,
                     "cache_hits": 0, "failures": [], "seconds": 0.0}

    @property
    def source(self):
        return self.info["source"]

    def credit(self, crs_wkt=None, version="", dem_label="the DEM"):
        return credit(self.info, crs_wkt, version, dem_label)


def render(bbox, crs_wkt, w, h, mode=DEFAULT_MODE, want_labels=True, budget_s=BUDGET_S,
           avoid=(), cache=None, opener=None, dirs=None):
    """The base map of a map `bbox` (x0, y0, x1, y1 in crs_wkt) at w x h px. Never raises."""
    t0 = time.time()
    bm = Basemap()
    info = bm.info
    try:
        mode = normalize_mode(mode)
    except ValueError as e:
        info["failures"].append(str(e))
        mode = DEFAULT_MODE
    info["mode"] = mode
    net = None
    try:
        if mode == "hillshade":
            info["fallback"] = "qa_basemap = hillshade"
            return bm
        if not crs_wkt:
            info["fallback"] = "the grid has no CRS"
            return bm
        w, h = int(w), int(h)
        off, why = offline()
        cache = cache if cache is not None else Cache(avoid=avoid)
        net = Net(budget_s, cache, opener)
        if not off:
            by_id, order, label_urls = sources()
            for sid in order:
                src = by_id[sid]
                try:
                    if src.get("kind") == "wms":
                        rgba = wms(src, bbox, w, h, crs_wkt, net)
                    else:
                        rgba, _z = xyz(src["url"], bbox, w, h, crs_wkt, net,
                                       zmax=int(src.get("zmax") or 19))
                    if _blank(rgba):
                        raise BasemapError("blank picture (outside its coverage?)")
                except BasemapError as e:
                    net.failures.append("{}: {}".format(sid, e))
                    continue
                except Exception as e:             # noqa: BLE001 - GDAL, a bad override
                    net.failures.append("{}: {}: {}".format(sid, type(e).__name__, e))
                    continue
                bm.rgb = rgba[:, :, :3]
                info.update(source=sid, name=src.get("name", sid), res=src.get("res", ""),
                            credit_of=src.get("credit", ""),
                            dates=imagery_dates(src, bbox, crs_wkt, net))
                break
            if bm.rgb is not None and want_labels and label_urls:
                bm.labels = labels(bbox, w, h, crs_wkt, net, label_urls)
                info["labels"] = bm.labels is not None
        if bm.rgb is None:
            got = pack_imagery(bbox, w, h, crs_wkt, dirs)
            if got is not None:
                rgba, name, date = got
                bm.rgb = rgba[:, :, :3]
                info.update(source="pack", name="offline pack imagery {}".format(name),
                            res="", dates=[date] if date else [])
                info["fallback"] = why or "imagery services did not answer"
            else:
                info["fallback"] = why or "imagery services did not answer"
    except Exception as e:                         # noqa: BLE001 - never fails a map
        info["failures"].append("{}: {}".format(type(e).__name__, e))
        bm.rgb, bm.labels = None, None
        info["source"] = "hillshade"
        info["fallback"] = info["fallback"] or "base map error"
    finally:
        if net is not None:
            info["requests"] = net.requests
            info["cache_hits"] = net.cache.hits
            info["failures"] += net.failures[:12]
            net.cache.trim()
        info["seconds"] = round(time.time() - t0, 2)
    return bm


def credit(info, crs_wkt=None, version="", dem_label="the DEM"):
    """'Imagery: <source> (<res>), flown <dates>; labels: Esri reference (...); <CRS>; <version>'."""
    src = info.get("source")
    if src == "hillshade":
        why = info.get("fallback") or ""
        if why.startswith("offline"):
            parts = ["Imagery not available (offline): hillshade of {}".format(dem_label)]
        elif why == "qa_basemap = hillshade":
            parts = ["Base map: hillshade of {} (qa_basemap = hillshade)".format(dem_label)]
        else:
            parts = ["Imagery not available ({}): hillshade of {}".format(why or "no answer",
                                                                         dem_label)]
    else:
        name = info.get("name") or src
        res = " ({})".format(info["res"]) if info.get("res") else ""
        if src == "pack":
            parts = ["Imagery: {}{}, flown {}{}".format(
                name, res, date_text(info.get("dates")) if info.get("dates") else
                "date not recorded in the file",
                " (offline)" if str(info.get("fallback", "")).startswith("offline") else "")]
        else:
            parts = ["Imagery: {}{}, flown {}".format(name, res, date_text(info.get("dates")))]
            if info.get("credit_of"):
                parts[0] += " ({})".format(info["credit_of"])
        if info.get("labels"):
            parts.append("labels: " + LABEL_CREDIT)
    if crs_wkt:
        parts.append(crs_label(crs_wkt))
    if version:
        parts.append(version)
    return "; ".join(parts)


# -------------------------------------------------------------------------- decorations --
def true_north_deg(x, y, crs_wkt):
    """Angle (deg, counter-clockwise from grid up) of true north at (x, y); 0 without a CRS."""
    if not crs_wkt:
        return 0.0
    try:
        lon, lat = _transform([(x, y)], crs_wkt, 4326)[0]
        x2, y2 = _transform([(lon, lat + 0.01)], 4326, crs_wkt)[0]
        return math.degrees(math.atan2(-(x2 - x), (y2 - y)))
    except Exception:                              # noqa: BLE001
        return 0.0


NICE_M = (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000)


def scale_length(width_m, frac=0.25):
    target = width_m * frac
    return max([v for v in NICE_M if v <= target] or [NICE_M[0]])


def scale_text(L):
    lab = ("{:g} km".format(L / 1000.0)) if L >= 1000 else ("{:g} m".format(L))
    ft = round(L / 0.3048, -1 if L < 1000 else -2)
    return "{} ({:,.0f} ft)".format(lab, ft)


def decorate(ax, bbox, crs_wkt=None, fs=7):
    """Scale bar (m / km and ft) at the lower right and the true-north arrow at the upper right
    of `ax` (map units of crs_wkt; metres assumed without one). Returns (arrow angle, bar m)."""
    import matplotlib.patches as mpatches
    import matplotlib.patheffects as pe
    x0, y0, x1, y1 = bbox
    W, H = x1 - x0, y1 - y0
    mpu = metres_per_unit(crs_wkt) if crs_wkt else 1.0
    L = scale_length(W * mpu)
    Lu = L / mpu
    bx = x1 - W * 0.04 - Lu
    by = y0 + H * 0.035
    st = [pe.withStroke(linewidth=2.5, foreground="black")]
    for k in range(4):
        ax.add_patch(mpatches.Rectangle((bx + k * Lu / 4, by), Lu / 4, H * 0.008,
                                        fc="white" if k % 2 == 0 else "black", ec="black",
                                        lw=0.6, zorder=20))
    ax.text(bx + Lu / 2, by + H * 0.014, scale_text(L), ha="center", va="bottom", fontsize=fs,
            color="white", path_effects=st, zorder=21)
    ang = true_north_deg((x0 + x1) / 2.0, (y0 + y1) / 2.0, crs_wkt)
    cx, cy, r = 0.94, 0.89, 0.05
    dx, dy = -math.sin(math.radians(ang)) * r, math.cos(math.radians(ang)) * r
    ax.annotate("", xy=(cx + dx, cy + dy), xytext=(cx - dx * 0.2, cy - dy * 0.2),
                xycoords="axes fraction",
                arrowprops=dict(facecolor="white", edgecolor="black", width=4, headwidth=11,
                                headlength=10), zorder=22)
    ax.text(cx + dx * 1.35, cy + dy * 1.35, "N" if crs_wkt else "N (grid)",
            transform=ax.transAxes, ha="center", va="center", fontsize=fs + 3, fontweight="bold",
            color="white", path_effects=st, zorder=22)
    return ang, L


def depth_cmap():
    """Blues from 0.35 to 1 (the pale end would vanish over a photo)."""
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap
    return ListedColormap(plt.cm.Blues(np.linspace(0.35, 1.0, 256)))


def footer(fig, text, width=130, fs=6.5, ax=None):
    """The credit line under the map (wrapped; under `ax`'s tick labels when given)."""
    import textwrap
    body = "\n".join(textwrap.wrap(text, width))
    if ax is not None:
        return ax.annotate(body, xy=(0.0, 0.0), xycoords="axes fraction", xytext=(0, -24),
                           textcoords="offset points", fontsize=fs, va="top", ha="left",
                           annotation_clip=False)
    return fig.text(0.01, 0.005, body, fontsize=fs, va="bottom", ha="left")
