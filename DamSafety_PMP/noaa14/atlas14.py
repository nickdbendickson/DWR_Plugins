"""NOAA Atlas 14 precipitation-frequency estimates from the NOAA/NWS Precipitation Frequency Data
Server (PFDS, https://hdsc.nws.noaa.gov/pfds/). Pure Python.

This module (with the GIS grids of gisgrid.py) is the Atlas 14 source of the Atlas 14 tools. It reads the PFDS point query
(cgi_readH5.py), which returns, for a latitude / longitude, the Atlas 14 estimates for 19
durations (5 min - 60 days) and 10 average recurrence intervals (partial-duration series,
1 - 1000 years) or 9 annual exceedance probabilities (annual-maximum series, 1/2 - 1/1000), with
the 90% confidence bounds, and the Atlas 14 volume, version, region and authors. Locations
Atlas 14 does not cover (e.g. Oregon) raise :class:`CoverageError`.

No PMP (probable maximum precipitation) value comes from here: Atlas 14 publishes frequency
estimates up to the 1000-year ARI only.
"""
import contextlib
import datetime
import hashlib
import json
import math
import os
import re
import time
import urllib.parse

PFDS_URL = "https://hdsc.nws.noaa.gov/cgi-bin/new/cgi_readH5.py"
PFDS_PAGE = "https://hdsc.nws.noaa.gov/pfds/"
DURATIONS = (("5-min", 5), ("10-min", 10), ("15-min", 15), ("30-min", 30), ("60-min", 60), ("2-hr", 120),
             ("3-hr", 180), ("6-hr", 360), ("12-hr", 720), ("24-hr", 1440), ("2-day", 2880), ("3-day", 4320),
             ("4-day", 5760), ("7-day", 10080), ("10-day", 14400), ("20-day", 28800), ("30-day", 43200),
             ("45-day", 64800), ("60-day", 86400))
# THE Atlas 14 duration list: every tool that offers a duration (fetchgrid / gridbasin DURATION, hyetograph /
# raingrid STORM) builds its choice from this one list, so the same index is the same duration everywhere
# (0 = 5-min ... 9 = 24-hr ... 18 = 60-day). Do not reorder it: it is also the row order of PFDS tables.
DURATION_LABELS = [d for d, _ in DURATIONS]
DEFAULT_DURATION = "24-hr"
DEFAULT_DURATION_INDEX = DURATION_LABELS.index(DEFAULT_DURATION)
MAX_STORM_STEPS = 20000
PDS_ARI = (1, 2, 5, 10, 25, 50, 100, 200, 500, 1000)          # average recurrence interval, years
AMS_AEP_YEARS = (2, 5, 10, 25, 50, 100, 200, 500, 1000)       # 1 / annual exceedance probability
UNITS = {("depth", "english"): "in", ("depth", "metric"): "mm",
         ("intensity", "english"): "in/hr", ("intensity", "metric"): "mm/hr"}


class Atlas14Error(ValueError):
    pass


class CoverageError(Atlas14Error):
    """PFDS has no NOAA Atlas 14 estimate for the location."""


def query_url(lat, lon, data="depth", units="english", series="pds"):
    lat, lon = float(lat), float(lon)
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise Atlas14Error("latitude / longitude out of range: {}, {}".format(lat, lon))
    if data not in ("depth", "intensity") or units not in ("english", "metric") or series not in ("pds", "ams"):
        raise Atlas14Error("data must be depth|intensity, units english|metric, series pds|ams")
    q = urllib.parse.urlencode({"lat": "{:.4f}".format(lat), "lon": "{:.4f}".format(lon), "type": "pf",
                                "data": data, "units": units, "series": series})
    return PFDS_URL + "?" + q


_ARRAY = re.compile(r"^\s*(quantiles|upper|lower)\s*=\s*(\[.*?\]);\s*$", re.M | re.S)
_SCALAR = re.compile(r"^\s*(\w+)\s*=\s*'([^']*)'\s*;", re.M)


def parse(text):
    """The PFDS answer as a dict. Raises CoverageError for result = 'none'."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    sc = dict(_SCALAR.findall(text))
    if sc.get("result") != "values":
        m = re.search(r"ErrorMsg\s*=\s*'([^']*)'", text)
        msg = m.group(1).strip() if m else "no Atlas 14 values in the answer"
        if sc.get("result") == "none":
            raise CoverageError("NOAA Atlas 14 does not cover this location (PFDS: {}). There is no Atlas 14 "
                                "estimate for it.".format(msg))
        raise Atlas14Error("unexpected PFDS answer: {}".format(msg if m else text[:200]))
    arrays = {}
    for name, body in _ARRAY.findall(text):
        try:
            rows = json.loads(body.replace("'", '"'))
            arrays[name] = [[float(v) for v in row] for row in rows]
        except (ValueError, TypeError) as e:
            raise Atlas14Error("cannot read {} from the PFDS answer: {}".format(name, e))
    q = arrays.get("quantiles")
    series = sc.get("ser", "pds")
    cols = PDS_ARI if series == "pds" else AMS_AEP_YEARS
    if not q or len(q) != len(DURATIONS) or any(len(r) != len(cols) for r in q):
        raise Atlas14Error("PFDS answer has {} durations x {} frequencies; expected {} x {}".format(
            len(q or []), len(q[0]) if q else 0, len(DURATIONS), len(cols)))
    datatype, unit = sc.get("datatype", "depth"), sc.get("unit", "english")
    return {"quantiles": q, "upper": arrays.get("upper"), "lower": arrays.get("lower"),
            "lat": float(sc.get("lat", "nan")), "lon": float(sc.get("lon", "nan")), "series": series,
            "datatype": datatype, "units": unit, "unit_label": UNITS.get((datatype, unit), unit),
            "frequencies": list(cols), "durations": [d for d, _ in DURATIONS],
            "region": sc.get("region", ""), "region_code": sc.get("reg", ""), "volume": sc.get("volume", ""),
            "version": sc.get("version", ""), "authors": sc.get("authors", ""), "file": sc.get("file", "")}


def citation(pf):
    """The PFDS-recommended reference form for the volume that answered."""
    return ("{authors}, NOAA Atlas 14 Volume {vol} Version {ver}, Precipitation-Frequency Atlas of the "
            "United States, {region}. NOAA, National Weather Service, Silver Spring, MD. Estimates from the "
            "Precipitation Frequency Data Server, {page}".format(authors=pf["authors"] or "Perica, S. et al.",
                                                                 vol=pf["volume"], ver=pf["version"],
                                                                 region=pf["region"], page=PFDS_PAGE))


def source_record(pf, url, fetched=None, cached=False):
    return {"source": "NOAA Atlas 14 (PFDS)", "pfds_url": url, "volume": pf["volume"], "version": pf["version"],
            "region": pf["region"], "series": pf["series"], "datatype": pf["datatype"], "units": pf["unit_label"],
            "lat": pf["lat"], "lon": pf["lon"], "fetched": fetched, "from_cache": cached, "citation": citation(pf),
            "note": "NOAA Atlas 14 precipitation-frequency estimates only; no PMP value and no other PMP source."}


def _cache_path(cache_dir, url):
    return os.path.join(cache_dir, "pfds_" + hashlib.sha256(url.encode()).hexdigest()[:20] + ".txt")


def fetch(lat, lon, http, cache_dir=None, data="depth", units="english", series="pds", offline=False):
    """(parsed, source_record). Uses the cache first (a cached answer never expires: Atlas 14
    volumes change only with a new version, which the record shows). ``offline`` refuses the
    network. A location outside Atlas 14 raises CoverageError (and is not cached)."""
    url = query_url(lat, lon, data, units, series)
    if cache_dir:
        p = _cache_path(cache_dir, url)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                text = fh.read()
            meta = {}
            if os.path.exists(p + ".json"):
                with open(p + ".json", encoding="utf-8") as fh:
                    meta = json.load(fh)
            pf = parse(text)
            return pf, source_record(pf, url, meta.get("fetched"), True)
    if offline:
        raise Atlas14Error("offline: no cached PFDS answer for {:.4f}, {:.4f}".format(float(lat), float(lon)))
    text = http.get_bytes(url, timeout=60).decode("utf-8", "replace")
    pf = parse(text)
    fetched = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        p = _cache_path(cache_dir, url)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(p + ".json", "w", encoding="utf-8") as fh:
            json.dump({"url": url, "fetched": fetched}, fh)
    return pf, source_record(pf, url, fetched, False)


def fetch_many(points, http, cache_dir=None, pause_s=1.0, sleep=time.sleep, **kw):
    """fetch() for several (lat, lon); waits ``pause_s`` between live requests (be polite to NOAA)."""
    out = []
    live = 0
    for lat, lon in points:
        if live and pause_s:
            sleep(pause_s)
        pf, src = fetch(lat, lon, http, cache_dir, **kw)
        live += 0 if src["from_cache"] else 1
        out.append((pf, src))
    return out


def freq_index(pf, years):
    try:
        return pf["frequencies"].index(int(years))
    except ValueError:
        raise Atlas14Error("{}-year is not an Atlas 14 {} frequency; use one of {}".format(
            years, pf["series"].upper(), ", ".join(str(f) for f in pf["frequencies"])))


def duration_index(label):
    for i, (d, _) in enumerate(DURATIONS):
        if d == label:
            return i
    raise Atlas14Error("unknown duration {!r}; Atlas 14 durations: {}".format(label, ", ".join(d for d, _ in DURATIONS)))


def table_rows(pf, which="quantiles"):
    rows = []
    for i, (dlabel, minutes) in enumerate(DURATIONS):
        for j, f in enumerate(pf["frequencies"]):
            r = {"duration": dlabel, "minutes": minutes, "frequency_years": f, "series": pf["series"],
                 "value": pf["quantiles"][i][j], "units": pf["unit_label"]}
            if pf.get("lower"):
                r["lower90"] = pf["lower"][i][j]
            if pf.get("upper"):
                r["upper90"] = pf["upper"][i][j]
            rows.append(r)
    return rows


def combine(pfs, how="mean"):
    """Cell-by-cell mean or max of several answers (same series / units)."""
    if not pfs:
        raise Atlas14Error("no Atlas 14 answers to combine")
    keys = {(p["series"], p["datatype"], p["units"]) for p in pfs}
    if len(keys) != 1:
        raise Atlas14Error("cannot combine answers of different series / units: {}".format(sorted(keys)))
    out = dict(pfs[0])
    for name in ("quantiles", "upper", "lower"):
        if any(p.get(name) is None for p in pfs):
            out[name] = None
            continue
        grid = []
        for i in range(len(DURATIONS)):
            row = []
            for j in range(len(out["frequencies"])):
                vals = [p[name][i][j] for p in pfs]
                row.append(round(sum(vals) / len(vals), 4) if how == "mean" else max(vals))
            grid.append(row)
        out[name] = grid
    out["volumes"] = sorted({"{} v{}".format(p["volume"], p["version"]) for p in pfs})
    out["points"] = len(pfs)
    return out


def depth_at(pf, minutes, years):
    """Depth for any duration between 5 min and 60 days, log-log interpolated between the published
    Atlas 14 durations (exact at published durations; no extrapolation)."""
    if pf["datatype"] != "depth":
        raise Atlas14Error("hyetographs need depth estimates (data=depth)")
    j = freq_index(pf, years)
    mins = [m for _, m in DURATIONS]
    if minutes < mins[0] or minutes > mins[-1]:
        raise Atlas14Error("{} min is outside the Atlas 14 durations ({} min - {} days)".format(
            minutes, mins[0], mins[-1] // 1440))
    for i, m in enumerate(mins):
        if minutes == m:
            return pf["quantiles"][i][j]
        if minutes < m:
            m0, m1 = mins[i - 1], m
            d0, d1 = pf["quantiles"][i - 1][j], pf["quantiles"][i][j]
            if d0 <= 0 or d1 <= 0:
                return d0 + (d1 - d0) * (minutes - m0) / float(m1 - m0)
            t = (math.log(minutes) - math.log(m0)) / (math.log(m1) - math.log(m0))
            return math.exp(math.log(d0) + t * (math.log(d1) - math.log(d0)))
    return pf["quantiles"][-1][j]


def duration_minutes(label):
    return DURATIONS[duration_index(label)][1]


def check_storm(storm_minutes, step_minutes, label=None):
    """A storm of this duration can only be built from whole time steps, with a step of at least the shortest
    Atlas 14 duration; otherwise Atlas14Error says what to change (never silently dropped from the choice)."""
    name = "{} ({} min)".format(label, storm_minutes) if label else "{} min".format(storm_minutes)
    if step_minutes < DURATIONS[0][1]:
        raise Atlas14Error("the time step must be at least {} min (the shortest Atlas 14 duration)".format(DURATIONS[0][1]))
    if storm_minutes < step_minutes:
        ok = [m for m in (5, 10, 15, 30, 60) if m <= storm_minutes and storm_minutes % m == 0]
        raise Atlas14Error("the storm duration {} is shorter than the time step of {} min: set the time step to {} min or "
                           "less{}".format(name, step_minutes, storm_minutes, " (e.g. {})".format(
                               ", ".join(str(m) for m in ok)) if ok else ""))
    if storm_minutes % step_minutes:
        ok = [m for m in (5, 10, 15, 30, 60, 120, 180, 360, 720, 1440) if m <= storm_minutes and storm_minutes % m == 0]
        raise Atlas14Error("the storm duration {} is not a whole number of {}-min time steps: choose a time step that "
                           "divides {} min{}".format(name, step_minutes, storm_minutes, " (e.g. {})".format(
                               ", ".join(str(m) for m in ok)) if ok else ""))
    if storm_minutes // step_minutes > MAX_STORM_STEPS:
        raise Atlas14Error("the storm {} with a {}-min time step is {} blocks (at most {}): use a longer time step".format(
            name, step_minutes, storm_minutes // step_minutes, MAX_STORM_STEPS))


def alternating_block(pf, years, storm_minutes, step_minutes):
    """Design hyetograph by the alternating block method (Chow, Maidment and Mays 1988, sec. 14.4)
    from the Atlas 14 depth-duration curve at one frequency: the storm total equals the Atlas 14
    depth for the storm duration and every centred window of k steps holds the Atlas 14 depth for
    that duration. This is not an Atlas 14 temporal distribution (those are published separately
    as quartile curves). Returns [(t_end_min, incremental, cumulative)]."""
    if step_minutes <= 0 or storm_minutes <= 0:
        raise Atlas14Error("the storm duration and the time step must be positive")
    check_storm(storm_minutes, step_minutes)
    n = int(storm_minutes // step_minutes)
    cum = [depth_at(pf, step_minutes * (k + 1), years) for k in range(n)]
    inc = [cum[0]] + [max(cum[k] - cum[k - 1], 0.0) for k in range(1, n)]
    blocks = sorted(inc, reverse=True)
    order = [None] * n
    centre = (n - 1) // 2
    for k, b in enumerate(blocks):
        off = (k + 1) // 2
        pos = centre + off if k % 2 else centre - off
        if k == 0:
            pos = centre
        if not (0 <= pos < n) or order[pos] is not None:
            pos = next(i for i in range(n) if order[i] is None)
        order[pos] = b
    out, c = [], 0.0
    for k, v in enumerate(order):
        c += v
        out.append((step_minutes * (k + 1), round(v, 4), round(c, 4)))
    return out


# ------------------------------------------------------------------ GDAL error mode (gisgrid, gridstats, temporal)
# audit 2026-11-20: gdal.UseExceptions() / ogr.UseExceptions() are process-wide in the GDAL bindings QGIS ships.
# gridstats called them at import and gisgrid / temporal inside functions, never switching back, so loading or
# running an Atlas 14 tool changed GDAL for QGIS and every other plugin (code written for the default mode checks
# `if ds is None` and meets an exception instead). The Atlas 14 core now asks for exceptions only inside
# gdal_raising(), which puts the previous mode back on the way out. osgeo is imported only when it is entered, so
# this module stays pure Python.
def _uses_exceptions(module):
    try:
        return bool(module.GetUseExceptions())
    except Exception:                                            # noqa - an old binding without the getter
        return False


@contextlib.contextmanager
def gdal_raising(*names):
    """GDAL Python exceptions on for the enclosed block only, for the osgeo modules named ("gdal", "ogr", "osr";
    default gdal and ogr); each one switched on here is switched off again on exit (in reverse order: GDAL >= 3.7
    keeps the error handlers on a stack), so the mode QGIS and the other plugins see never changes. Also a
    decorator: ``@A.gdal_raising("gdal")`` runs the whole function this way."""
    import importlib
    switched = []
    try:
        for name in names or ("gdal", "ogr"):
            module = importlib.import_module("osgeo." + name)
            if not _uses_exceptions(module):
                module.UseExceptions()
                switched.append(module)
        yield
    finally:
        for module in reversed(switched):
            try:
                module.DontUseExceptions()
            except Exception:                                    # noqa - never fails a run
                pass
