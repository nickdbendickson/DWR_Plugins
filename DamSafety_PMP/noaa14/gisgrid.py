"""NOAA Atlas 14 precipitation-frequency GIS grids from the PFDS "PF in GIS format" data source.

Source (the only one): https://hdsc.nws.noaa.gov/pfds/pfds_gis.html . The page's download form builds
this file name, which is served over plain HTTPS (checked 2026-11-12)::

    https://hdsc.nws.noaa.gov/pub/hdsc/data/{reg}/{reg}{ARI}yr{dur}{type}{series}.zip

    reg     Atlas 14 volume folder: mw, orb, sw, tx, se, ne, inw, ak, hi, pr, sa, ... (REGIONS)
    ARI     1 2 5 10 25 50 100 200 500 1000 (PDS) | 2 5 10 25 50 100 200 500 1000 (AMS, = 1/AEP)
    dur     05m 10m 15m 30m 60m 02h 03h 06h 12h 24h 48h 03d 04d 07d 10d 20d 30d 45d 60d (DUR_CODES)
    type    a = estimate, al = lower 90% bound, au = upper 90% bound
    series  empty = partial-duration series, _ams = annual-maximum series (no 1-year file)

e.g. .../mw/mw100yr24ha.zip is the Volume 8 100-year, 24-hour estimate. The zip holds one ESRI ASCII grid
(.asc; the old Volume 1 'sa' zip holds a .ver4 text grid), a .prj and an .xml metadata file. The grid is
geographic NAD83 (EPSG:4269), cell 30 arc-seconds / 0.0083333 deg, integer thousandths of an inch (scale
0.001 -> inches), NODATA -9 (-999 in the 'sa' file). Only the zip is cached; the grid is read in place with
GDAL (/vsizip/) and clipped straight to a Float32 GeoTIFF in inches.

Region of a location: the PFDS point query (atlas14.fetch) answers with the volume folder code (``reg``), so
the same code names the GIS folder. Note PFDS serves Volume 1 (Semiarid Southwest, version 5) and Volume 6
(California) under ``sw``; the ``sw`` GIS grids cover both (the Phoenix AZ cell, 3406, equals the PFDS Volume 1
point value 3.41 in). The ``sa`` folder is the older Volume 1 version 4 grid and is never chosen automatically.

NOAA Atlas 14 is the only data source; Atlas 14 stops at the 1000-year ARI and this computes no PMP.
"""
import datetime
import hashlib
import json
import os
import re
import zipfile

from . import atlas14 as A

GIS_BASE = "https://hdsc.nws.noaa.gov/pub/hdsc/data/"
GIS_PAGE = "https://hdsc.nws.noaa.gov/pfds/pfds_gis.html"
SCALE_TO_INCHES = 0.001
NODATA_OUT = -9999.0

# PFDS duration label -> GIS file-name code (2-day is 48h on the server)
DUR_CODES = {"5-min": "05m", "10-min": "10m", "15-min": "15m", "30-min": "30m", "60-min": "60m", "2-hr": "02h",
             "3-hr": "03h", "6-hr": "06h", "12-hr": "12h", "24-hr": "24h", "2-day": "48h", "3-day": "03d",
             "4-day": "04d", "7-day": "07d", "10-day": "10d", "20-day": "20d", "30-day": "30d", "45-day": "45d",
             "60-day": "60d"}
DURATION_LABELS = A.DURATION_LABELS                  # the one shared list (atlas14.py)
assert set(DUR_CODES) == set(DURATION_LABELS)
BOUNDS = (("estimate", "a"), ("lower90", "al"), ("upper90", "au"))
# (code, Atlas 14 volume, name). 'auto' is not here: the region comes from the point.
REGIONS = (("sw", "6 (and 1 v5)", "Southwest: California, Arizona, Nevada, Utah, New Mexico (current grids)"),
           ("mw", "8", "Midwestern States (includes North Dakota)"),
           ("orb", "2", "Ohio River Basin and surrounding states"),
           ("se", "9", "Southeastern States"),
           ("ne", "10", "Northeastern States"),
           ("tx", "11", "Texas"),
           ("inw", "12", "Interior Northwest"),
           ("ak", "7", "Alaska"),
           ("hi", "4", "Hawaiian Islands"),
           ("pr", "3", "Puerto Rico and the U.S. Virgin Islands"),
           ("sa", "1 (v4, legacy)", "Semiarid Southwest, older version 4 grid (PFDS serves v5 under sw)"))
REGION_CODES = [r[0] for r in REGIONS]
PACIFIC = ("nmi", "ch", "ko", "pp", "nk", "pg", "yp", "wl", "ul", "pl", "rm", "as", "gu", "wki")
ALL_REGIONS = REGION_CODES + list(PACIFIC)


class NotPublished(A.Atlas14Error):
    """NOAA has no such GIS file (e.g. an AMS grid for a volume without AMS, a 1-year AEP)."""


def check_choice(region, years, duration, bound="estimate", series="pds"):
    if region not in ALL_REGIONS:
        raise A.Atlas14Error("unknown Atlas 14 region {!r}; use one of {}".format(region, ", ".join(ALL_REGIONS)))
    if duration not in DUR_CODES:
        raise A.Atlas14Error("unknown duration {!r}; Atlas 14 durations: {}".format(duration, ", ".join(DURATION_LABELS)))
    if bound not in dict(BOUNDS):
        raise A.Atlas14Error("bound must be one of estimate, lower90, upper90")
    if series not in ("pds", "ams"):
        raise A.Atlas14Error("series must be pds or ams")
    allowed = A.PDS_ARI if series == "pds" else A.AMS_AEP_YEARS
    if int(years) not in allowed:
        raise A.Atlas14Error("{}-year is not an Atlas 14 {} frequency for the GIS grids; use one of {}".format(
            years, series.upper(), ", ".join(str(f) for f in allowed)))


def zip_name(region, years, duration, bound="estimate", series="pds"):
    check_choice(region, years, duration, bound, series)
    return "{reg}{ari}yr{dur}{typ}{ser}.zip".format(reg=region, ari=int(years), dur=DUR_CODES[duration],
                                                    typ=dict(BOUNDS)[bound], ser="_ams" if series == "ams" else "")


def grid_url(region, years, duration, bound="estimate", series="pds"):
    return "{}{}/{}".format(GIS_BASE, region, zip_name(region, years, duration, bound, series))


_NAME = re.compile(r"^(?P<reg>[a-z]+)(?P<ari>\d+)yr(?P<dur>\d\d)(?P<u>[mhd])(?P<typ>a[lu]?)(?P<ams>_ams)?\.zip$")


def parse_zip_name(name):
    """Inverse of zip_name (None if it is not an Atlas 14 GIS file name)."""
    m = _NAME.match(os.path.basename(name))
    if not m:
        return None
    code = m.group("dur") + m.group("u")
    label = {v: k for k, v in DUR_CODES.items()}.get(code)
    typ = {v: k for k, v in BOUNDS}[m.group("typ")]
    return {"region": m.group("reg"), "frequency_years": int(m.group("ari")), "duration": label, "bound": typ,
            "series": "ams" if m.group("ams") else "pds"} if label else None


def region_from_pfds(pf):
    """The GIS folder for a parsed PFDS point answer (its ``reg`` code), or an Atlas14Error."""
    code = (pf.get("region_code") or "").lower()
    if code not in ALL_REGIONS:
        raise A.Atlas14Error("PFDS answered with region {!r} ({}); no Atlas 14 GIS folder is known for it. "
                             "Choose the region by hand.".format(code, pf.get("region")))
    return code


def cache_zip_path(cache_dir, region, name):
    return os.path.join(cache_dir, "atlas14_gis", region, name)


def fetch_zip(region, years, duration, bound, series, cache_dir, download, offline=False, now=None):
    """(zip path, record). ``download(url, dest)`` streams a URL to a file and raises IOError (an
    HttpError with ``status``). Cached zips never expire (an Atlas 14 volume changes only with a new
    version, which the zip's metadata shows). HTTP 404 -> NotPublished; other failures -> IOError."""
    url = grid_url(region, years, duration, bound, series)
    name = zip_name(region, years, duration, bound, series)
    path = cache_zip_path(cache_dir, region, name)
    meta_path = path + ".json"
    if os.path.exists(path) and zipfile.is_zipfile(path):
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        return path, {"url": url, "fetched": meta.get("fetched"), "from_cache": True, "zip": path,
                      "bytes": os.path.getsize(path), "sha256": meta.get("sha256")}
    if offline:
        raise A.Atlas14Error("offline: {} is not in the cache ({})".format(name, os.path.dirname(path)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        download(url, path)
    except IOError as e:
        if getattr(e, "status", None) == 404:
            raise NotPublished("NOAA has not published {} (HTTP 404: {}). AMS grids exist only for some volumes "
                               "and start at the 2-year AEP; check {}".format(name, url, GIS_PAGE))
        raise
    if not zipfile.is_zipfile(path):
        os.remove(path)
        raise A.Atlas14Error("the download from {} is not a zip file".format(url))
    fetched = (now or datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0).isoformat()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump({"url": url, "fetched": fetched, "sha256": h.hexdigest()}, fh)
    return path, {"url": url, "fetched": fetched, "from_cache": False, "zip": path,
                  "bytes": os.path.getsize(path), "sha256": h.hexdigest()}


def _members(zpath):
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
    grid = [n for n in names if os.path.splitext(n)[1].lower() in (".asc", ".ver4", ".tif", ".txt")]
    if not grid:
        raise A.Atlas14Error("no grid found in {} ({})".format(os.path.basename(zpath), ", ".join(names)))
    xml = [n for n in names if n.lower().endswith(".xml")]
    return grid[0], (xml[0] if xml else None)


def zip_metadata(zpath):
    """Title and publication date from the zip's own XML metadata (empty when absent)."""
    _, xml = _members(zpath)
    out = {}
    if xml:
        with zipfile.ZipFile(zpath) as z:
            text = z.read(xml).decode("utf-8", "replace")
        t = re.search(r"<title>([^<]*)</title>", text)
        d = re.search(r"<pubdate>([^<]*)</pubdate>", text)
        out = {"title": t.group(1).strip() if t else None, "pubdate": d.group(1).strip() if d else None}
    return out


@A.gdal_raising("gdal", "ogr")             # audit 2026-11-20: not process-wide any more
def read_clip(zpath, window=None, mask_wkt=None, mask_wkt_crs=None):
    """Read the Atlas 14 grid in a zip. ``window`` = (xmin, ymin, xmax, ymax) in the grid's geographic
    degrees (None = the whole grid); ``mask_wkt`` (geographic degrees) blanks the cells it does not
    touch: every cell the polygon touches is kept (GDAL ALL_TOUCHED, the same rule as
    gridstats.basin_stats, so a fetched grid and a grid file give the same basin cells).
    Returns (array in inches, float32, NODATA_OUT where empty; geotransform; EPSG-wkt)."""
    import numpy as np
    from osgeo import gdal, ogr, osr
    member, _ = _members(zpath)
    ds = gdal.Open("/vsizip/{}/{}".format(zpath, member))
    band = ds.GetRasterBand(1)
    gt = ds.GetGeoTransform()
    nodata = band.GetNoDataValue()
    nx, ny = ds.RasterXSize, ds.RasterYSize
    if window is None:
        c0, r0, w, h = 0, 0, nx, ny
    else:
        xmin, ymin, xmax, ymax = window
        c0 = int(np.floor((xmin - gt[0]) / gt[1]))
        c1 = int(np.ceil((xmax - gt[0]) / gt[1]))
        r0 = int(np.floor((ymax - gt[3]) / gt[5]))
        r1 = int(np.ceil((ymin - gt[3]) / gt[5]))
        if c1 <= 0 or r1 <= 0 or c0 >= nx or r0 >= ny:
            raise A.Atlas14Error("the area is outside this Atlas 14 grid (lon {:.3f}..{:.3f}, lat {:.3f}..{:.3f})".format(
                gt[0], gt[0] + gt[1] * nx, gt[3] + gt[5] * ny, gt[3]))
        c0, r0 = max(c0, 0), max(r0, 0)
        w, h = min(c1, nx) - c0, min(r1, ny) - r0
    raw = band.ReadAsArray(c0, r0, w, h).astype("float64")
    valid = np.isfinite(raw) & (raw >= 0)
    if nodata is not None:
        valid &= raw != nodata
    out_gt = (gt[0] + c0 * gt[1], gt[1], 0.0, gt[3] + r0 * gt[5], 0.0, gt[5])
    if mask_wkt:
        mem = gdal.GetDriverByName("MEM").Create("", w, h, 1, gdal.GDT_Byte)
        mem.SetGeoTransform(out_gt)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4269)
        mem.SetProjection(srs.ExportToWkt())
        vds = ogr.GetDriverByName("Memory").CreateDataSource("m")
        lyr = vds.CreateLayer("m", srs, ogr.wkbUnknown)
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(ogr.CreateGeometryFromWkt(mask_wkt))
        lyr.CreateFeature(f)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        valid &= mem.GetRasterBand(1).ReadAsArray() == 1
    if not valid.any():
        raise A.CoverageError("no Atlas 14 grid cells with values in the area (outside Atlas 14 coverage?)")
    arr = np.where(valid, raw * SCALE_TO_INCHES, NODATA_OUT).astype("float32")
    return arr, out_gt, 4269


@A.gdal_raising("gdal")
def write_geotiff(path, arr, gt, epsg=4269):
    from osgeo import gdal, osr
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32,
                                              ["COMPRESS=DEFLATE", "PREDICTOR=3", "TILED=YES"])
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(NODATA_OUT)
    b.WriteArray(arr)
    b.FlushCache()
    ds = None


def array_stats(arr):
    import numpy as np
    v = arr[arr != NODATA_OUT]
    return {"cells": int(v.size), "mean": round(float(v.mean()), 4), "min": round(float(v.min()), 4),
            "max": round(float(v.max()), 4)}


def value_at(arr, gt, lon, lat):
    """Cell value (inches) containing lon / lat, or None when outside the array or empty."""
    c = int((lon - gt[0]) // gt[1])
    r = int((lat - gt[3]) // gt[5])
    if not (0 <= r < arr.shape[0] and 0 <= c < arr.shape[1]) or arr[r, c] == NODATA_OUT:
        return None
    return round(float(arr[r, c]), 4)


def sidecar_path(tif_path):
    return tif_path + ".json"


def read_sidecar(tif_path):
    p = sidecar_path(tif_path)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    return None


def citation(volume_note, pubdate=None):
    return ("NOAA Atlas 14 precipitation-frequency estimates in GIS format, NOAA/NWS Hydrometeorological Design "
            "Studies Center (Volume {}{}). {}".format(volume_note, ", published {}".format(pubdate) if pubdate else "",
                                                       GIS_PAGE))
