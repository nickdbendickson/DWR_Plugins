# -*- coding: utf-8 -*-
"""2.18.54 review 3 (Nick, 2026-10-21): what the lone-911 300 ft no-building rule checks
a point against.

* USDA NAIP 2025 flight dates (ND GIS Hub Imagery/Dates_Imagery seamlines, layer 41): the
  'imagery date' of the rule at each 911 point - one query by the run's extent
  (download job "naip", outputs/_downloads, never the offline data pack), then a spatial
  index (ImageryDates), never one request per point.
* Possibly misplaced 911 points: the point's own street in the NG9-1-1 road centerlines
  (the run's road layer, already downloaded once per run) - St_Name / St_PosTyp /
  St_PosDir and the address range holding Add_Number with its parity - and the location
  interpolated along that segment (StreetIndex).
* Small NAIP 2025 clips (about 600 m square, red marker at the 911 point, yellow ring at
  the expected address location) for the review items: aerial.dwr.nd.gov WMS 1.1.1,
  capped per run, with a download timeout; a failure only leaves the clip out.
"""
import datetime
import math
import os
import json
import re
import urllib.parse

NAIP_DATES_URLS = {
    2025: "https://ndgishub.nd.gov/arcgis/rest/services/Imagery/Dates_Imagery/MapServer/41",
    2024: "https://ndgishub.nd.gov/arcgis/rest/services/Imagery/Dates_Imagery/MapServer/39",
}
NAIP_DATES_NAME = "NAIP2025_flight_dates"
NAIP_LABEL = "USDA NAIP 2025"
NAIP_WMS_URL = "https://aerial.dwr.nd.gov/dwrwms?"
NAIP_WMS_LAYERS = {2025: "NorthDakota_NAIP_2025_col_air", 2024: "NorthDakota_NAIP_2024_col_air",
                   "2023_15cm": "NorthDakota_2023_15cm_col_air"}
CLIP_SIZE_M = 600.0
CLIP_PX = 512
CLIP_MAX = 25                   # clips per run (setting naip_clip_max)
CLIP_TIMEOUT_S = 20.0           # per clip request
CLIP_STOP_AFTER_FAILS = 3       # consecutive failures: the service is down, stop trying
MISPLACED_911_M = 150.0         # setting misplaced_911_m
CLIP_SUBDIR = "imagery"
FALLBACK_PREFIX = "Imagery dates: NAIP 2025 flight dates"         # outputs/QA/imagery/<struct id>_naip2025.jpg


# ------------------------------------------------------------- flight dates --
def parse_flight_date(idate, sdate=None):
    """IDATE '2025-07-13' (or SDATE '07/13/2025 09:28') -> datetime.date, or None."""
    for v in (idate, sdate):
        v = ("" if v is None else str(v)).strip()
        if not v or v.upper() == "NULL":
            continue
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", v)
        if m:
            try:
                return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", v)
        if m:
            try:
                return datetime.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                pass
    return None


def fetch_naip_dates(extent_4326, output_folder, progress_cb=None):
    """The NAIP 2025 seamline polygons (flight dates) intersecting the run's extent - one
    paged query per run, cached in outputs/_downloads. Returns (layer, count, path)."""
    from .download import fetch_arcgis_layer
    return fetch_arcgis_layer(NAIP_DATES_URLS[2025], extent_4326, output_folder,
                              NAIP_DATES_NAME, progress_cb=progress_cb, pack_fallback=False)


DATES_TIMEOUT_S = 10.0


def imagery_dates(extent_4326, year=2025, get=None, timeout_s=DATES_TIMEOUT_S):
    """2.19.0 (shared with the report / map book credit): the NAIP `year` flight dates
    (sorted datetime.date list, no repeats) of the seamline polygons intersecting
    extent_4326 = (xmin, ymin, xmax, ymax) in lon / lat - one envelope query, no geometry.
    Returns [] when no seamline covers the extent; raises on a failed request (the caller
    says "date not read"). get(url, timeout_s) -> bytes (default: QGIS network stack)."""
    base = NAIP_DATES_URLS.get(year)
    if not base:
        return []
    x0, y0, x1, y1 = [float(v) for v in extent_4326]
    q = {"geometry": "{:.6f},{:.6f},{:.6f},{:.6f}".format(x0, y0, x1, y1),
         "geometryType": "esriGeometryEnvelope", "inSR": "4326",
         "spatialRel": "esriSpatialRelIntersects", "outFields": "IDATE,SDATE",
         "returnGeometry": "false", "f": "json"}
    raw = (get or _http_get)(base.rstrip("/") + "/query?" + urllib.parse.urlencode(q),
                             timeout_s)
    data = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    if not isinstance(data, dict) or "error" in data:
        raise ValueError("flight-date service error: {}".format(
            (data or {}).get("error") if isinstance(data, dict) else data))
    out = set()
    for f in data.get("features") or []:
        a = {str(k).upper(): v for k, v in (f.get("attributes") or {}).items()}
        d = parse_flight_date(a.get("IDATE"), a.get("SDATE"))
        if d is not None:
            out.add(d)
    return sorted(out)


def dates_text(dates):
    """'2025-07-13' / '2025-07-13 to 2025-08-02' ('' without)."""
    dates = sorted(dates or [])
    if not dates:
        return ""
    lo, hi = dates[0].isoformat(), dates[-1].isoformat()
    return lo if lo == hi else "{} to {}".format(lo, hi)


class ImageryDates(object):
    """Flight date at a point (analysis CRS) from the seamline polygons; built once."""

    def __init__(self, layer, crs, transform_ctx, label=NAIP_LABEL):
        from qgis.core import (QgsCoordinateTransform, QgsCoordinateReferenceSystem,
                               QgsSpatialIndex, QgsFeature)
        self.label = label
        self.polys = []
        self.index = QgsSpatialIndex()
        xf = QgsCoordinateTransform(layer.crs() if layer.crs().isValid() else
                                    QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                                    transform_ctx)
        names = [f.name().lower() for f in layer.fields()]
        ii = names.index("idate") if "idate" in names else None
        si = names.index("sdate") if "sdate" in names else None
        for f in layer.getFeatures():
            a = f.attributes()
            d = parse_flight_date(a[ii] if ii is not None else None,
                                  a[si] if si is not None else None)
            g = f.geometry()
            if d is None or g is None or g.isEmpty():
                continue
            g = type(g)(g)
            try:
                g.transform(xf)
            except Exception:                       # noqa - a polygon off the CRS
                continue
            k = len(self.polys)
            self.polys.append((g, d))
            ft = QgsFeature(k)
            ft.setGeometry(g)
            self.index.addFeature(ft)
        ds = [d for _g, d in self.polys]
        self.range = (min(ds), max(ds)) if ds else (None, None)

    def __len__(self):
        return len(self.polys)

    def at(self, x, y):
        """(date, text) of the earliest flight over (x, y), or None outside the seamlines.
        The earliest: a 911 record after it may be newer than the imagery (counted)."""
        from qgis.core import QgsGeometry, QgsPointXY, QgsRectangle
        pg = QgsGeometry.fromPointXY(QgsPointXY(x, y))
        best = None
        for k in self.index.intersects(QgsRectangle(x, y, x, y)):
            g, d = self.polys[k]
            if g.intersects(pg) and (best is None or d < best):
                best = d
        if best is None:
            return None
        return best, "{} flown {}".format(self.label, best.isoformat())


def naip_source_line(dates, path=None):
    """DATA SOURCES line of the flight dates ('' without)."""
    if dates is None or not len(dates):
        return ""
    lo, hi = dates.range
    return ("{} flight dates (ND GIS Hub Imagery/Dates_Imagery seamlines, layer 41): {} "
            "polygon(s) over the study area, flown {} to {}{} - the imagery date of the lone "
            "911 300 ft rule at each point".format(
                NAIP_LABEL, len(dates), lo, hi,
                " ({})".format(os.path.basename(path)) if path else ""))


# ---------------------------------------------------------- misplaced 911 --
def _norm(v):
    v = "" if v is None else str(v)
    return "" if v.strip().upper() in ("", "NULL", "NONE") else re.sub(r"\s+", " ",
                                                                       v.strip().upper())


def street_key(name, postyp="", posdir=""):
    return (_norm(name), _norm(postyp), _norm(posdir))


def street_label(key):
    return " ".join(p for p in key if p)


def address_number(text):
    m = re.match(r"^\s*(\d+)", "" if text is None else str(text))
    return int(m.group(1)) if m else None


def _in_range(num, lo, hi, parity=None):
    """num in the range lo..hi of one side; parity 'O' / 'E' / 'B' (both) when given, else
    the parity of the range's ends."""
    try:
        lo, hi = int(lo), int(hi)
    except (TypeError, ValueError):
        return False
    if lo == 0 and hi == 0:
        return False
    if not (min(lo, hi) <= num <= max(lo, hi)):
        return False
    p = _norm(parity)[:1]
    if p == "B" or p == "Z":
        return True
    if p == "O":
        return num % 2 == 1
    if p == "E":
        return num % 2 == 0
    return num % 2 == lo % 2


class StreetIndex(object):
    """NG9-1-1 road centerline segments by street (St_Name, St_PosTyp, St_PosDir), in the
    analysis CRS; built once per run. ok is False when the layer lacks the address fields
    (a project road layer of another kind)."""
    FIELDS = ("st_name", "st_postyp", "st_posdir", "fromaddr_l", "toaddr_l", "fromaddr_r",
              "toaddr_r")

    def __init__(self, layer, crs, transform_ctx):
        from qgis.core import QgsCoordinateTransform, QgsCoordinateReferenceSystem
        self.by_key = {}
        names = [f.name().lower() for f in layer.fields()]
        self.ok = all(n in names for n in self.FIELDS)
        self.to_4326 = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:4326"),
                                              transform_ctx)
        if not self.ok:
            return
        ix = {n: names.index(n) for n in self.FIELDS}
        for opt in ("parity_l", "parity_r"):
            ix[opt] = names.index(opt) if opt in names else None
        xf = QgsCoordinateTransform(layer.crs() if layer.crs().isValid() else
                                    QgsCoordinateReferenceSystem("EPSG:4326"), crs,
                                    transform_ctx)
        for f in layer.getFeatures():
            a = f.attributes()
            key = street_key(a[ix["st_name"]], a[ix["st_postyp"]], a[ix["st_posdir"]])
            g = f.geometry()
            if not key[0] or g is None or g.isEmpty():
                continue
            g = type(g)(g)
            try:
                g.transform(xf)
            except Exception:                       # noqa
                continue
            sides = []
            for s in ("l", "r"):
                p = ix["parity_" + s]
                sides.append((a[ix["fromaddr_" + s]], a[ix["toaddr_" + s]],
                              a[p] if p is not None else None))
            self.by_key.setdefault(key, []).append((g, sides))

    def __len__(self):
        return sum(len(v) for v in self.by_key.values())

    def expected(self, key, num, near=None):
        """(QgsPointXY in the analysis CRS, street label) of address `num` on street `key`,
        interpolated along the segment whose range holds it (the one nearest `near` when
        several do), or None."""
        from qgis.core import QgsGeometry
        best = None
        for g, sides in self.by_key.get(key, ()):
            for lo, hi, par in sides:
                if not _in_range(num, lo, hi, par):
                    continue
                lo_i, hi_i = int(lo), int(hi)
                frac = 0.5 if hi_i == lo_i else (num - lo_i) / float(hi_i - lo_i)
                try:
                    pt = g.interpolate(max(0.0, min(1.0, frac)) * g.length()).asPoint()
                except Exception:                   # noqa
                    continue
                d = 0.0 if near is None else math.hypot(pt.x() - near.x(), pt.y() - near.y())
                if best is None or d < best[0]:
                    best = (d, pt)
                break
        if best is None:
            return None
        return best[1], street_label(key)

    def lonlat(self, pt):
        try:
            q = self.to_4326.transform(pt)
            return q.x(), q.y()
        except Exception:                           # noqa
            return None


def misplaced_text(m):
    return "possibly misplaced: {:.0f} m from its address on {}; expected location near " \
        "{:.5f},{:.5f}".format(m["dist_m"], m["street"], m["lat"], m["lon"])


# ------------------------------------------------------------------- clips --
def clip_bbox(lon, lat, size_m=CLIP_SIZE_M):
    half = size_m / 2.0
    dlat = half / 111320.0
    dlon = half / (111320.0 * max(0.05, math.cos(math.radians(lat))))
    return lon - dlon, lat - dlat, lon + dlon, lat + dlat


def clip_url(lon, lat, layer=NAIP_WMS_LAYERS[2025], size_m=CLIP_SIZE_M, px=CLIP_PX):
    x0, y0, x1, y1 = clip_bbox(lon, lat, size_m)
    q = {"SERVICE": "WMS", "VERSION": "1.1.1", "REQUEST": "GetMap", "LAYERS": layer,
         "STYLES": "", "SRS": "EPSG:4326",
         "BBOX": "{:.7f},{:.7f},{:.7f},{:.7f}".format(x0, y0, x1, y1),
         "WIDTH": str(px), "HEIGHT": str(px), "FORMAT": "image/jpeg"}
    base = NAIP_WMS_URL if NAIP_WMS_URL.endswith(("?", "&")) else NAIP_WMS_URL + "?"
    return base + urllib.parse.urlencode(q)


def _http_get(url, timeout_s):
    """GET with a timeout (patched in the tests)."""
    from . import download as DL
    return DL.http_get_bytes(url, DL.HTTP_USER_AGENT, timeout_s=timeout_s)


def _mark(raw, path, lon, lat, expected=None, size_m=CLIP_SIZE_M):
    """Red marker at the centre (the 911 point), a yellow ring at `expected` (lon, lat) when
    it is in the clip; written as JPEG. Without Qt painting the image is saved as is."""
    try:
        from qgis.PyQt.QtGui import QImage, QPainter, QPen, QColor
        from qgis.PyQt.QtCore import QPointF
        img = QImage.fromData(raw)
        if img.isNull():
            raise ValueError("not an image")
        img = img.convertToFormat(QImage.Format_RGB32) if hasattr(QImage, "Format_RGB32") \
            else img
        w, h = img.width(), img.height()
        p = QPainter(img)
        try:
            aa = getattr(QPainter, "Antialiasing", None)        # Qt6 scopes the enum
            if aa is None:
                aa = QPainter.RenderHint.Antialiasing
            p.setRenderHint(aa, True)
            pen = QPen(QColor(255, 0, 0))
            pen.setWidth(3)
            p.setPen(pen)
            c = QPointF(w / 2.0, h / 2.0)
            p.drawEllipse(c, 10.0, 10.0)
            p.drawLine(QPointF(c.x() - 18, c.y()), QPointF(c.x() + 18, c.y()))
            p.drawLine(QPointF(c.x(), c.y() - 18), QPointF(c.x(), c.y() + 18))
            if expected:
                x0, y0, x1, y1 = clip_bbox(lon, lat, size_m)
                ex, ey = expected
                if x0 <= ex <= x1 and y0 <= ey <= y1:
                    pen = QPen(QColor(255, 220, 0))
                    pen.setWidth(3)
                    p.setPen(pen)
                    p.drawEllipse(QPointF((ex - x0) / (x1 - x0) * w,
                                          (y1 - ey) / (y1 - y0) * h), 12.0, 12.0)
        finally:
            p.end()
        if not img.save(path, "JPG", 85):
            raise OSError("could not write " + path)
        return True
    except ImportError:
        with open(path, "wb") as fh:
            fh.write(raw)
        return True


def fetch_clip(lon, lat, path, expected=None, timeout_s=CLIP_TIMEOUT_S,
               layer=NAIP_WMS_LAYERS[2025]):
    """One NAIP clip around (lon, lat) written to `path`. Raises on failure."""
    raw = _http_get(clip_url(lon, lat, layer), timeout_s)
    if not raw or raw[:2] != b"\xff\xd8":
        raise ValueError("the imagery service did not return a JPEG ({})".format(
            (raw or b"")[:80].decode("utf-8", "replace").strip()))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part.jpg"
    _mark(raw, tmp, lon, lat, expected)
    os.replace(tmp, path)
    return path


class ClipMaker(object):
    """Per-run clip cache: clip(rec) -> path relative to the outputs folder, or '' (capped,
    switched off, offline, or the download failed - the item is written without it)."""

    def __init__(self, out, qa_dir, enabled=True, cap=CLIP_MAX, timeout_s=CLIP_TIMEOUT_S,
                 log=None):
        self.out, self.dir = out, os.path.join(qa_dir, CLIP_SUBDIR)
        self.enabled, self.cap, self.timeout_s = bool(enabled), int(cap), float(timeout_s)
        self.log = log or (lambda m: None)
        self.done, self.tried, self.fails, self.skipped = {}, 0, 0, 0

    def clip(self, rec):
        sid = rec.get("id")
        if sid in self.done:
            return self.done[sid]
        ll = rec.get("lonlat")
        if not self.enabled or ll is None or self.fails >= CLIP_STOP_AFTER_FAILS:
            self.done[sid] = ""
            return ""
        if self.tried >= self.cap:
            self.skipped += 1
            self.done[sid] = ""
            return ""
        self.tried += 1
        path = os.path.join(self.dir, "{}_naip2025.jpg".format(sid))
        mis = rec.get("misplaced") or {}
        exp = (mis["lon"], mis["lat"]) if mis.get("lon") is not None else None
        try:
            fetch_clip(ll[0], ll[1], path, expected=exp, timeout_s=self.timeout_s)
            self.fails = 0
            rel = os.path.relpath(path, self.out).replace(os.sep, "/")
        except Exception as e:                      # noqa - fail softly
            self.fails += 1
            self.log("NAIP 2025 clip for structure {} not saved: {}".format(sid, e))
            rel = ""
        self.done[sid] = rel
        return rel

    def summary(self):
        n = sum(1 for v in self.done.values() if v)
        if not self.tried:
            return ""
        return "{} NAIP 2025 clip(s) in {}{}{}".format(
            n, os.path.relpath(self.dir, self.out).replace(os.sep, "/"),
            "; {} not saved (download failed)".format(self.tried - n) if self.tried > n else "",
            "; {} more not clipped (cap {} per run, setting naip_clip_max)".format(
                self.skipped, self.cap) if self.skipped else "")
