"""Temporal distributions of a design storm: NOAA Atlas 14 quartile curves (HDSC) and the NRCS 24-hour types.

NOAA Atlas 14 stays the ONLY source of the storm DEPTH (the Atlas 14 point / basin tables). This module only supplies a
SHAPE: how that depth is spread in time.

NOAA Atlas 14 temporal distributions (Volume 8, Midwestern States)
  Published by NOAA/NWS HDSC as comma-delimited files, one per temporal distribution area (1-4) and duration
  (6, 12, 24, 96 hours), https://hdsc.nws.noaa.gov/pub/hdsc/data/mw/mw_<area>_<hours>h_temporal.csv, page
  https://hdsc.nws.noaa.gov/pfds/pfds_temporal.html (area map and shapefile
  https://hdsc.nws.noaa.gov/pfds/shapefiles/Vol8_Temporals_Shapefiles.zip). Each file holds cumulative percentages of
  the storm total every 0.5 h for the first-, second-, third- and fourth-quartile cases (the quartile of the storm
  that holds the most rain) and for all cases, at 90%, 80% ... 10% "percent of occurrence" (the 50% column is the
  median curve). The distributions are for precipitation exceeding the 2-year ARI estimate (Perica et al. 2013,
  NOAA Atlas 14 Vol. 8 v2, appendix A.5).

NRCS 24-hour types I, IA, II, III
  Cumulative fraction of the 24-hour depth (USDA SCS TR-55 (1986) appendix B; NRCS NEH 630 chapter 4). Carried here at
  hourly ordinates (Type II also at the TR-55 half-hour points around its peak), linearly interpolated: a shape option
  for comparison with legacy practice, not an Atlas 14 product. NRCS NEH 630 ch. 4 (2019) prefers the Atlas 14
  distributions where they exist. For regulatory work check these ordinates against the 0.1-hour WinTR-55 tables.
"""
import csv
import datetime
import hashlib
import io
import json
import os

import numpy as np

from . import atlas14 as A

HDSC_DATA = "https://hdsc.nws.noaa.gov/pub/hdsc/data"
TEMPORAL_PAGE = "https://hdsc.nws.noaa.gov/pfds/pfds_temporal.html"
SHAPEFILE_URL = {"mw": "https://hdsc.nws.noaa.gov/pfds/shapefiles/Vol8_Temporals_Shapefiles.zip"}
SHAPEFILE_NAME = {"mw": "Vol_8_Temporal_Regions.shp"}
VOLUMES = {"mw": "Volume 8 (Midwestern States)"}     # the volumes whose temporal files this plugin has been checked against
DURATIONS_H = (6, 12, 24, 96)
QUARTILES = ("first", "second", "third", "fourth", "all")
QUARTILE_LABELS = ["First quartile", "Second quartile", "Third quartile", "Fourth quartile", "All cases"]
PERCENTS = (90, 80, 70, 60, 50, 40, 30, 20, 10)
DEFAULT_QUARTILE = "second"
DEFAULT_PERCENT = 50

# pattern ids in the order of the PATTERN enum (alternating block first = the default and the 0.2.0 / 0.3.0 behaviour)
PATTERNS = ("alternating", "atlas14", "nrcs1", "nrcs1a", "nrcs2", "nrcs3")
PATTERN_LABELS = ["Alternating block (Atlas 14 depth-duration curve; default)",
                  "NOAA Atlas 14 temporal distribution (Volume 8; 6, 12, 24 or 96 h storms)",
                  "NRCS Type I (24-hr storm only)", "NRCS Type IA (24-hr storm only)",
                  "NRCS Type II (24-hr storm only)", "NRCS Type III (24-hr storm only)"]
NRCS_NAMES = {"nrcs1": "I", "nrcs1a": "IA", "nrcs2": "II", "nrcs3": "III"}
NRCS_SOURCE = ("USDA Soil Conservation Service, Urban Hydrology for Small Watersheds, Technical Release 55, 2nd ed., "
               "1986, appendix B; USDA NRCS National Engineering Handbook Part 630 Hydrology, chapter 4")

# cumulative fraction of the 24-hour depth, hours 0..24
_H = list(range(25))
_NRCS = {
    "I": dict(zip(_H, (0.000, 0.017, 0.035, 0.054, 0.076, 0.100, 0.125, 0.156, 0.194, 0.254, 0.515, 0.623, 0.684, 0.732,
                       0.770, 0.802, 0.832, 0.860, 0.886, 0.910, 0.932, 0.952, 0.970, 0.986, 1.000))),
    "IA": dict(zip(_H, (0.000, 0.020, 0.050, 0.082, 0.116, 0.156, 0.206, 0.268, 0.425, 0.520, 0.577, 0.624, 0.664, 0.701,
                        0.736, 0.769, 0.801, 0.831, 0.860, 0.887, 0.913, 0.937, 0.959, 0.980, 1.000))),
    "II": {0: 0.000, 1: 0.011, 2: 0.022, 3: 0.035, 4: 0.048, 5: 0.063, 6: 0.080, 7: 0.098, 8: 0.120, 8.5: 0.133, 9: 0.147,
           9.5: 0.163, 10: 0.181, 10.5: 0.204, 11: 0.235, 11.5: 0.283, 11.75: 0.357, 12: 0.663, 12.5: 0.735, 13: 0.772,
           13.5: 0.799, 14: 0.820, 15: 0.854, 16: 0.880, 17: 0.903, 18: 0.922, 19: 0.938, 20: 0.952, 21: 0.965,
           22: 0.977, 23: 0.989, 24: 1.000},
    "III": dict(zip(_H, (0.000, 0.010, 0.020, 0.031, 0.043, 0.057, 0.072, 0.090, 0.114, 0.146, 0.189, 0.250, 0.500, 0.750,
                         0.811, 0.854, 0.886, 0.909, 0.928, 0.943, 0.957, 0.969, 0.981, 0.991, 1.000))),
}


class TemporalError(A.Atlas14Error):
    pass


def pattern_index(pid):
    return PATTERNS.index(pid)


def check_pattern(pattern, storm_minutes, label=None):
    """The curves exist only for certain storm durations; anything else fails with the list (never silently falls back)."""
    name = "{} ({} min)".format(label, storm_minutes) if label else "{} min".format(storm_minutes)
    if pattern == "alternating":
        return
    if pattern == "atlas14":
        ok = [h * 60 for h in DURATIONS_H]
        if storm_minutes not in ok:
            raise TemporalError("NOAA Atlas 14 temporal distributions exist for 6-, 12-, 24- and 96-hour storms only; the "
                                "storm duration is {}: choose STORM 6-hr, 12-hr, 24-hr or 4-day, or another pattern".format(name))
    elif pattern in NRCS_NAMES:
        if storm_minutes != 1440:
            raise TemporalError("the NRCS Type {} distribution is a 24-hour curve; the storm duration is {}: choose 24-hr "
                                "or another pattern".format(NRCS_NAMES[pattern], name))
    else:
        raise TemporalError("unknown temporal pattern {!r}".format(pattern))


# ------------------------------------------------------------------ NRCS
def nrcs_curve(kind):
    """(hours, cumulative fraction) of NRCS Type 'I' | 'IA' | 'II' | 'III'."""
    t = _NRCS[kind]
    hs = sorted(t)
    return np.array(hs, dtype=float), np.array([t[h] for h in hs], dtype=float)


# ------------------------------------------------------------------ Atlas 14 temporal files
def temporal_url(code, area, hours):
    if code not in VOLUMES:
        raise TemporalError("Atlas 14 temporal distributions are wired for {} only (the PFDS region here is {!r}); use "
                            "the alternating-block or an NRCS pattern".format(", ".join(VOLUMES.values()), code))
    if hours not in DURATIONS_H:
        raise TemporalError("no Atlas 14 temporal distribution for {} hours".format(hours))
    return "{}/{}/{}_{}_{}h_temporal.csv".format(HDSC_DATA, code, code, int(area), int(hours))


def parse_temporal(text):
    """Parse an HDSC temporal CSV -> {'title', 'hours': [..], 'cases': {quartile: {percent: [cum %..]}}}."""
    lines = [ln.rstrip("\r\n") for ln in text.splitlines()]
    title = lines[0].strip() if lines else ""
    cases, cur, hours, pcts = {}, None, None, None
    names = {"FIRST-QUARTILE": "first", "SECOND-QUARTILE": "second", "THIRD-QUARTILE": "third",
             "FOURTH-QUARTILE": "fourth", "ALL CASES": "all"}
    for ln in lines:
        up = ln.upper()
        if up.startswith("CUMULATIVE PERCENTAGES OF TOTAL PRECIPITATION FOR"):
            cur = next((v for k, v in names.items() if k in up), None)
            if cur is None:
                raise TemporalError("unrecognised case in the temporal file: " + ln)
            cases[cur] = {"hours": [], "cols": []}
            continue
        if cur is None or not ln.strip() or ln.lower().startswith("time,"):
            continue
        row = [c.strip() for c in ln.split(",")]
        if row[0].lower() == "hours":
            pcts = [int(c.rstrip("%")) for c in row[1:]]
            cases[cur]["pcts"] = pcts
            continue
        try:
            vals = [float(c) for c in row]
        except ValueError:
            continue
        cases[cur]["hours"].append(vals[0])
        cases[cur]["cols"].append(vals[1:])
    if not cases:
        raise TemporalError("not an HDSC temporal distribution file")
    out = {"title": title, "cases": {}}
    for q, c in cases.items():
        arr = np.array(c["cols"], dtype=float)
        if arr.shape[1] != len(c.get("pcts", [])):
            raise TemporalError("the temporal file's columns do not match its percent header")
        out["cases"][q] = {"hours": c["hours"], "pcts": c["pcts"], "cum": arr}
    return out


def curve(parsed, quartile, percent):
    """(hours array, cumulative fraction 0..1 array) for a quartile case at a percent of occurrence (monotone)."""
    if quartile not in parsed["cases"]:
        raise TemporalError("the temporal file has no {} case".format(quartile))
    c = parsed["cases"][quartile]
    if percent not in c["pcts"]:
        raise TemporalError("percent of occurrence {} is not in the file (one of {})".format(
            percent, ", ".join(str(p) for p in c["pcts"])))
    col = c["cum"][:, c["pcts"].index(percent)] / 100.0
    h = np.array(c["hours"], dtype=float)
    if abs(col[0]) > 1e-9 or abs(col[-1] - 1.0) > 1e-6 or (np.diff(col) < -1e-9).any():
        raise TemporalError("the temporal curve is not a cumulative 0..100 % curve")
    return h, np.maximum.accumulate(col)


def locate_area(lon, lat, shp_path):
    """The temporal distribution area (1-4) containing a point (degrees, NAD83 ~ WGS84), from the HDSC shapefile."""
    from osgeo import ogr
    ogr.UseExceptions()
    ds = ogr.Open(shp_path)
    lyr = ds.GetLayer()
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint(float(lon), float(lat))
    hits = [int(f.GetField("Region")) for f in lyr if f.GetGeometryRef() is not None and f.GetGeometryRef().Contains(pt)]
    if not hits:
        raise TemporalError("the point ({:.4f}, {:.4f}) is outside the Atlas 14 Volume 8 temporal distribution areas".format(
            lat, lon))
    return hits[0]


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch_file(url, cache_dir, download, offline=False, now=None):
    """Cached download of one HDSC file -> (path, record {url, fetched, from_cache, sha256, bytes})."""
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, os.path.basename(url))
    meta = dest + ".json"
    if os.path.exists(dest) and os.path.exists(meta):
        with open(meta, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["from_cache"] = True
        return dest, rec
    if offline:
        raise TemporalError("offline: {} is not in the cache {}".format(os.path.basename(url), cache_dir))
    download(url, dest)
    stamp = (now or datetime.datetime.now(datetime.timezone.utc)).replace(microsecond=0).isoformat()
    rec = {"url": url, "fetched": stamp, "sha256": _sha256(dest), "bytes": os.path.getsize(dest), "from_cache": False}
    with open(meta, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    return dest, rec


_ZIP_MAX_TOTAL = 400 * 1024 * 1024  # audit 2026-11-19: cap uncompressed bytes


def _clear_torn_cache(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def _safe_extract_zip(z, dest):
    """Validated, size-capped extract (audit 2026-11-19): no absolute
    paths, no '..' traversal, no drive letters, total-size cap; the caller
    clears a torn cache first so a partial extract self-heals on rerun."""
    import os, re
    total = sum(i.file_size for i in z.infolist())
    if total > _ZIP_MAX_TOTAL:
        raise RuntimeError("temporal shapefile zip holds %.0f MB uncompressed (cap %d MB)"
                           % (total / 1e6, _ZIP_MAX_TOTAL // (1024 * 1024)))
    for n in z.namelist():
        parts = n.replace("\\", "/").split("/")
        if n.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", n):
            raise RuntimeError("unsafe path in temporal shapefile zip: " + n)
    os.makedirs(dest, exist_ok=True)
    z.extractall(dest)


def atlas14_curve(lon, lat, code, hours, quartile, percent, cache_dir, download, area=None, offline=False):
    """Fetch (or reuse) the HDSC file for the point and return (hours, cum fraction, record). ``area`` None = find it from the
    HDSC area shapefile (also cached). The record holds everything needed to cite and reproduce the curve."""
    if code not in VOLUMES:
        temporal_url(code, 1, hours)          # raises the clear message
    shp_rec = None
    if area is None:
        zp, shp_rec = fetch_file(SHAPEFILE_URL[code], cache_dir, download, offline)
        d = os.path.join(cache_dir, "temporal_areas_" + code)
        if not os.path.exists(os.path.join(d, SHAPEFILE_NAME[code])):
            import zipfile
            _clear_torn_cache(d)   # audit 2026-11-19: a half-extracted cache self-heals on rerun
            with zipfile.ZipFile(zp) as z:
                _safe_extract_zip(z, d)
        area = locate_area(lon, lat, os.path.join(d, SHAPEFILE_NAME[code]))
    url = temporal_url(code, area, hours)
    path, rec = fetch_file(url, cache_dir, download, offline)
    with open(path, encoding="utf-8", errors="replace") as fh:
        parsed = parse_temporal(fh.read())
    h, f = curve(parsed, quartile, percent)
    record = {"source": "NOAA Atlas 14 temporal distribution (HDSC)", "volume": VOLUMES[code], "area": int(area),
              "area_from": "HDSC shapefile point-in-polygon" if shp_rec else "user choice", "duration_hr": hours,
              "quartile": quartile, "percent_of_occurrence": percent, "file": rec, "file_title": parsed["title"],
              "page": TEMPORAL_PAGE, "area_shapefile": shp_rec,
              "citation": "Perica, S., et al., NOAA Atlas 14 Volume 8 Version 2.0 (2013), Midwestern States, appendix A.5 "
                          "temporal distributions; NOAA/NWS Hydrometeorological Design Studies Center, " + TEMPORAL_PAGE,
              "note": "Shape only (cumulative % of the storm total). The depth comes from the Atlas 14 estimate. The curves "
                      "describe storms exceeding the 2-year ARI estimate."}
    return h, f, record


def nrcs_record(kind):
    return {"source": "NRCS 24-hour rainfall distribution Type {}".format(kind), "type": kind, "duration_hr": 24,
            "citation": NRCS_SOURCE,
            "note": "A shape option only (hourly ordinates, linear interpolation); NOAA Atlas 14 remains the depth source. "
                    "Check against the WinTR-55 0.1-hour tables for regulatory work."}


def rows_from_curve(hours, frac, total, storm_min, step_min):
    """[(t_end_min, incremental, cumulative)] of a storm of depth ``total``: the curve (time in hours from the start of the
    storm, as it is, NOT stretched) read at every step boundary, differenced. A curve whose length is not the storm duration
    is an error (the caller checks the duration first)."""
    if abs(hours[-1] * 60.0 - storm_min) > 1e-6:
        raise TemporalError("the curve is {:g} h long but the storm is {:g} min".format(hours[-1], storm_min))
    n = int(storm_min // step_min)
    t = np.arange(0, n + 1) * step_min / 60.0
    cum = np.interp(t, hours, frac) * total
    cum[-1] = total
    cum = np.round(cum, 4)                    # whole-depth running totals, so the last row is exactly the storm total
    out, prev = [], 0.0
    for k in range(n):
        out.append((step_min * (k + 1), round(float(cum[k + 1] - prev), 4), float(cum[k + 1])))
        prev = float(cum[k + 1])
    return out
