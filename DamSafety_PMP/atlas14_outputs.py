"""The files the NOAA Atlas 14 tools write (pure Python + numpy, no QGIS; DWR only).

* write_outputs: the Atlas 14 table (CSV) and its JSON record (schema damit.pmp.atlas14, the same
  file DamIT PMP's pointpf / basinpf write, so either plugin reads the other's).
* make_storms: Atlas 14 frequency storms as nd_pmp hyetograph CSVs, the format core/hydro.py
  read_hyetograph reads, so they feed ndpmp:inflow_hydrograph and then the Runner's
  ndbreach:spillwayadequacy unchanged:

      # storm_id: atlas14_pds_0100yr_24hr_alternating
      # storm_type: atlas14
      # duration_hr: 24
      ...                                   ('# key: value' header lines)
      time_hr,incr_depth_in,cum_depth_in
      1,0.0884,0.0884                       (time at the END of each uniform step, no t = 0 row)

  GROSS rain in inches: losses belong to ndpmp:inflow_hydrograph. The depth is the Atlas 14
  depth (a point value) times the areal factor the user gives (1 = none); this module invents
  no areal reduction. Plus a summary CSV (one row per storm) and a JSON record whose storms are
  DamIT-style damit.pmp.hyetograph records. A rerun into the same folder removes the Atlas 14
  hyetographs of the earlier run that it does not write again (inflow_hydrograph routes every CSV).

The Atlas 14 numbers come only from the shared core in noaa14/ (byte-identical with DamIT PMP).
GDAL work in the core (the temporal-area shapefile lookup) runs inside gdalsafe.gdal_exceptions(),
so the GDAL exception mode QGIS and the other plugins see is left as it was (audit R3). The
downloads run before that block: the mode is process-wide, so it is on only while OGR reads.
"""
import csv
import glob
import json
import os
import re
import zipfile

from .core import gdalsafe as GS
from .noaa14 import atlas14 as A
from .noaa14 import gisgrid as G
from .noaa14 import storm as S
from .noaa14 import temporal as T

MM_PER_IN = 25.4
NOTE = ("Source: NOAA Atlas 14 only (NOAA/NWS Precipitation Frequency Data Server, https://hdsc.nws.noaa.gov/pfds/). "
        "Atlas 14 gives precipitation-frequency estimates up to the 1000-year ARI; it is not a PMP study and these "
        "tools compute no PMP (ndpmp:pmp does, from the ND PMP study data). A location outside Atlas 14 coverage fails "
        "with a message.")
HYETO_SUBDIR = "hyetographs"
STORM_PREFIX = "atlas14_"                 # every storm id (and so every hyetograph file name) starts with it
SUMMARY_NAME = "atlas14_storms_summary.csv"
RECORD_NAME = "atlas14_storms.json"
SUMMARY_COLS = ["storm_id", "frequency_years", "series", "duration", "duration_hr", "step_min", "pattern",
                "temporal_distribution", "atlas14_depth_in", "areal_factor", "depth_in", "peak_incr_in", "area_mi2",
                "file"]
QUARTILE_TAG = {"first": "q1", "second": "q2", "third": "q3", "fourth": "q4", "all": "qall"}
NRCS_TAG = {"nrcs1": "nrcs_type1", "nrcs1a": "nrcs_type1a", "nrcs2": "nrcs_type2", "nrcs3": "nrcs_type3"}


# ------------------------------------------------------------------------------ the Atlas 14 table
def write_outputs(csv_path, json_path, pf, sources, extra=None):
    """The table CSV and the damit.pmp.atlas14 JSON (as DamIT PMP's pointpf / basinpf write them)."""
    for p in (csv_path, json_path):
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    rows = A.table_rows(pf)
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        cols = ["duration", "minutes", "frequency_years", "series", "value", "lower90", "upper90", "units"]
        w = csv.DictWriter(fh, cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        fh.write("# {}\n".format(A.citation(pf)))
    doc = {"schema": "damit.pmp.atlas14", "schema_version": 1, "atlas14": pf, "sources": sources,
           "citation": A.citation(pf), "note": NOTE}
    doc.update(extra or {})
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    return doc


def read_atlas14_json(path):
    """(pf, doc) from a damit.pmp.atlas14 JSON (atlas14_point / atlas14_basin, or DamIT's pointpf / basinpf)."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc["atlas14"], doc
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise A.Atlas14Error("Not an Atlas 14 estimates JSON (from atlas14_point / atlas14_basin) ({}): {}".format(path, e))


# ------------------------------------------------------------------------------ storms
def parse_frequencies(text, series="pds"):
    """'100, 500,1000' -> [100, 500, 1000]: whole years, each an Atlas 14 frequency of the series."""
    allowed = A.PDS_ARI if series == "pds" else A.AMS_AEP_YEARS
    out = []
    for tok in re.split(r"[,;\s]+", str(text or "").strip()):
        if not tok:
            continue
        try:
            f = float(tok)
        except ValueError:
            raise A.Atlas14Error("frequency {!r} is not a number; give whole years separated by commas, e.g. "
                                 "100,500,1000".format(tok))
        if f != int(f):
            raise A.Atlas14Error("frequency {} is not a whole number of years".format(tok))
        f = int(f)
        if f not in allowed:
            raise A.Atlas14Error("{}-year is not an Atlas 14 {} frequency; use one of {}".format(
                f, series.upper(), ", ".join(str(x) for x in allowed)))
        if f not in out:
            out.append(f)
    if not out:
        raise A.Atlas14Error("give at least one frequency in years, e.g. 100,500,1000")
    return out


def duration_tag(label):
    """'24-hr' -> '24hr', '60-min' -> '60min', '2-day' -> '2day'."""
    return re.sub(r"[^0-9a-z]+", "", label.lower())


def pattern_tag(pattern, quartile=None, percent=None):
    if pattern == "atlas14":
        return "atlas14temporal_{}_{}pct".format(QUARTILE_TAG.get(quartile, quartile), int(percent))
    return NRCS_TAG.get(pattern, pattern)


def storm_id(series, years, label, pattern, quartile=None, percent=None):
    """File-name safe and unique per storm: atlas14_pds_0100yr_24hr_alternating."""
    sid = "atlas14_{}_{:04d}yr_{}_{}".format(series, int(years), duration_tag(label), pattern_tag(pattern, quartile, percent))
    return re.sub(r"[^A-Za-z0-9_]+", "_", sid)


def temporal_text(pattern, rec=None):
    if pattern == "alternating":
        return "alternating block (Atlas 14 depth-duration curve)"
    if pattern == "atlas14":
        return "NOAA Atlas 14 temporal distribution, {} area {}, {} quartile, {}% of occurrence".format(
            rec["volume"], rec["area"], rec["quartile"], rec["percent_of_occurrence"]) if rec else "NOAA Atlas 14 temporal distribution"
    return "NRCS Type {} 24-hour distribution".format(T.NRCS_NAMES[pattern])


def inch_factor(pf):
    """Inches per unit of the Atlas 14 answer (depth only)."""
    if pf.get("datatype", "depth") != "depth":
        raise A.Atlas14Error("hyetographs need depth estimates (data=depth); the JSON holds {}".format(pf.get("datatype")))
    u = pf.get("unit_label", "in")
    if u == "in":
        return 1.0
    if u == "mm":
        return 1.0 / MM_PER_IN
    raise A.Atlas14Error("unknown Atlas 14 depth unit {!r}".format(u))


def _fmt(v, nd=6):
    """Numbers as the nd_pmp writer writes them (core/temporal.py _fmt)."""
    if v is None:
        return ""
    f = float(v)
    return ("%.*f" % (nd, f)).rstrip("0").rstrip(".") if f != int(f) else str(int(f))


def _ascii(v):
    """One header line, plain ASCII (read_hyetograph opens the file in the platform's encoding)."""
    return " ".join(str(v).split()).encode("ascii", "replace").decode("ascii")


def write_hyetograph(folder, sid, rows_in, header):
    """<folder>/<sid>.csv in the nd_pmp hyetograph format. ``rows_in`` [(t_end_min, incr_in, cum_in)];
    ``header`` [(key, value)] in order."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, sid + ".csv")
    with open(path, "w", newline="", encoding="ascii") as fh:
        for k, v in header:
            fh.write("# {}: {}\n".format(k, _ascii(v)))
        w = csv.writer(fh)
        w.writerow(["time_hr", "incr_depth_in", "cum_depth_in"])
        for t_min, inc, cum in rows_in:
            w.writerow([_fmt(t_min / 60.0), _fmt(inc), _fmt(cum)])
    return path


def scale_rows(rows, factor):
    """[(t_end_min, inc, cum)] times ``factor``, the increments taken as differences of the scaled cumulative
    column, so they add up to the storm total (the core rounds both columns to 4 decimals on their own)."""
    out, prev = [], 0.0
    for t, _, cum in rows:
        c = round(float(cum) * factor, 6)
        out.append((t, round(c - prev, 6), c))
        prev = c
    return out


def folder_hyetographs(folder):
    """The CSV file names in ``folder``: what ndpmp:inflow_hydrograph routes with its default PATTERN (*.csv)."""
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(folder, "*.csv")))


def _is_atlas14_hyetograph(path):
    """True for a hyetograph this module wrote (its header says storm_type: atlas14)."""
    try:
        with open(path, encoding="ascii", errors="replace") as fh:
            for _ in range(3):
                if fh.readline().strip() == "# storm_type: atlas14":
                    return True
    except OSError:
        pass
    return False


def remove_earlier_storms(folder, keep):
    """Remove the Atlas 14 hyetographs in ``folder`` that are not in ``keep`` (file names): storms of an earlier run
    that inflow_hydrograph would otherwise route with this run's. Other CSVs are left alone. Returns the names removed."""
    gone = []
    for name in folder_hyetographs(folder):
        path = os.path.join(folder, name)
        if name.startswith(STORM_PREFIX) and name not in keep and _is_atlas14_hyetograph(path):
            os.remove(path)
            gone.append(name)
    return gone


def fetch_temporal_files(lon, lat, code, hours, area, cache_dir, download, offline=False):
    """Download (or find in the cache) the files T.atlas14_curve reads: the temporal-area shapefile (when ``area`` is
    None) and the temporal CSV of the area. The downloads (up to 120 s each) run OUTSIDE gdal_exceptions(): GDAL's
    exception mode is process-wide, so only the OGR read of the cached shapefile runs inside it. atlas14_curve then
    reads the cache only."""
    if code not in T.VOLUMES:
        return                                                  # atlas14_curve gives the message
    if area is None:
        zp, _ = T.fetch_file(T.SHAPEFILE_URL[code], cache_dir, download, offline)
        d = os.path.join(cache_dir, "temporal_areas_" + code)   # where atlas14_curve unzips it
        shp = os.path.join(d, T.SHAPEFILE_NAME[code])
        if not os.path.exists(shp):
            with zipfile.ZipFile(zp) as z:
                z.extractall(d)
        with GS.gdal_exceptions():
            area = T.locate_area(lon, lat, shp)
    T.fetch_file(T.temporal_url(code, area, hours), cache_dir, download, offline)


def make_storms(pf, frequencies, durations, patterns, step_min, out_folder, area_mi2=0.0, areal_factor=1.0,
                quartile=T.DEFAULT_QUARTILE, percent=T.DEFAULT_PERCENT, temporal_area=None, cache_dir=None,
                download=None, offline=False, sources=None, location=None, log=None, warn=None):
    """Write one nd_pmp hyetograph per (duration, pattern, frequency) into <out_folder>/hyetographs/, the summary
    CSV and the JSON record. A duration / pattern pair that cannot be built (a pattern with no curve for that
    duration, or a duration that is not a whole number of steps) is skipped with a warning and listed in the record;
    when no storm can be built, Atlas14Error lists why. Atlas 14 hyetographs of an earlier run in that folder that
    this run does not write are removed (with a warning). Returns the record (dict)."""
    log = log or (lambda m: None)
    warn = warn or log
    if not (areal_factor > 0):
        raise A.Atlas14Error("the areal factor must be greater than 0 (1 = no areal reduction)")
    if area_mi2 is None or area_mi2 < 0:
        raise A.Atlas14Error("the drainage area cannot be negative")
    to_in = inch_factor(pf)
    for f in frequencies:
        A.freq_index(pf, f)                                  # every frequency is an Atlas 14 one, or a message
    folder = os.path.join(out_folder, HYETO_SUBDIR)
    cite = A.citation(pf)
    storms, skipped, curves = [], [], {}
    for label in durations:
        storm_min = A.duration_minutes(label)
        for pattern in patterns:
            try:
                A.check_storm(storm_min, step_min, label)
                T.check_pattern(pattern, storm_min, label)
            except A.Atlas14Error as e:
                skipped.append({"duration": label, "pattern": pattern, "reason": str(e)})
                warn("Skipped {} {}: {}".format(label, pattern, e))
                continue
            curve, rec = None, None
            if pattern == "atlas14":
                key = ("atlas14", storm_min)
                if key not in curves:
                    if download is None and not offline:
                        raise A.Atlas14Error("the Atlas 14 temporal distribution needs a downloader or OFFLINE")
                    code = G.region_from_pfds(pf)
                    fetch_temporal_files(pf["lon"], pf["lat"], code, storm_min // 60, temporal_area, cache_dir,
                                         download, offline)          # the network, outside the GDAL block
                    with GS.gdal_exceptions():                # the area lookup reads the cached shapefile with OGR
                        h, fr, rec = T.atlas14_curve(pf["lon"], pf["lat"], code, storm_min // 60,
                                                     quartile, percent, cache_dir, download, temporal_area, offline)
                    curves[key] = ((h, fr), rec)
                    log("Atlas 14 temporal distribution: {} area {}, {} quartile, {}% of occurrence ({})".format(
                        rec["volume"], rec["area"], quartile, percent, rec["file"]["url"]))
                curve, rec = curves[key]
            elif pattern in T.NRCS_NAMES:
                kind = T.NRCS_NAMES[pattern]
                curve, rec = T.nrcs_curve(kind), T.nrcs_record(kind)
            for years in frequencies:
                rows = S.build(pf, years, storm_min, step_min, pattern, label, curve)
                point_in = A.depth_at(pf, storm_min, years) * to_in
                rows_in = scale_rows(rows, to_in * areal_factor)
                total = rows_in[-1][2]
                sid = storm_id(pf["series"], years, label, pattern, quartile, percent)
                ttext = temporal_text(pattern, rec)
                header = [("storm_id", sid), ("storm_type", "atlas14"), ("duration_hr", _fmt(storm_min / 60.0)),
                          ("timestep_hr", _fmt(step_min / 60.0)), ("area_mi2", _fmt(area_mi2)),
                          ("frequency_years", years), ("series", pf["series"])]
                if pf["series"] == "ams":
                    header.append(("aep_percent", _fmt(100.0 / years)))
                header += [("temporal_distribution", ttext), ("depth_in", _fmt(total)),
                          ("atlas14_depth_in", _fmt(round(point_in, 6))), ("areal_factor", _fmt(areal_factor)),
                          ("rain", "gross (no losses; ndpmp:inflow_hydrograph applies them)"),
                          ("atlas14", "Volume {} Version {}, {}; lat {:.5f}, lon {:.5f}".format(
                              pf["volume"], pf["version"], pf["region"], pf["lat"], pf["lon"])),
                          ("source", "NOAA Atlas 14 precipitation-frequency estimate (point value) x areal_factor; "
                                     "written by ndpmp:atlas14_hyetograph; not a PMP"),
                          ("citation", cite)]
                if rec:
                    header.append(("temporal_source", rec.get("citation") or rec.get("source")))
                path = write_hyetograph(folder, sid, rows_in, header)
                peak = max(r[1] for r in rows_in)
                storms.append({
                    "schema": "damit.pmp.hyetograph", "schema_version": 1, "storm_id": sid, "file": path,
                    "frequency_years": years, "series": pf["series"], "storm": label, "storm_min": storm_min,
                    "step_min": step_min, "pattern": pattern, "pattern_label": T.PATTERN_LABELS[T.PATTERNS.index(pattern)],
                    "temporal_distribution": ttext, "temporal": rec, "depth_unit": "in",
                    "atlas14_depth_in": round(point_in, 4), "areal_factor": areal_factor, "gross_total": total,
                    "peak_incr_in": peak, "losses": None, "area_mi2": area_mi2, "citation": cite,
                    "note": "NOAA Atlas 14 is the only depth source; a temporal pattern is a shape only. Gross rain: "
                            "ndpmp:inflow_hydrograph applies the losses."})
                log("{}: {} in ({} x {}){}".format(sid, _fmt(total, 4), _fmt(round(point_in, 4)), _fmt(areal_factor),
                                                    "" if pattern == "alternating" else ", " + ttext))
    if not storms:
        raise A.Atlas14Error("no storm could be built: " + "; ".join(
            "{} {}: {}".format(s["duration"], s["pattern"], s["reason"]) for s in skipped))
    removed = remove_earlier_storms(folder, {os.path.basename(s["file"]) for s in storms})
    if removed:
        warn("Removed {} Atlas 14 hyetograph(s) of an earlier run from {} (this run does not write them): {}".format(
            len(removed), folder, ", ".join(removed)))
    summary = os.path.join(out_folder, SUMMARY_NAME)
    with open(summary, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SUMMARY_COLS)
        for s in storms:
            w.writerow([s["storm_id"], s["frequency_years"], s["series"], s["storm"], _fmt(s["storm_min"] / 60.0),
                        s["step_min"], s["pattern"], s["temporal_distribution"], _fmt(s["atlas14_depth_in"]),
                        _fmt(s["areal_factor"]), _fmt(s["gross_total"]), _fmt(s["peak_incr_in"]), _fmt(area_mi2),
                        os.path.relpath(s["file"], out_folder)])
    record = {"schema": "ndpmp.atlas14_hyetographs", "schema_version": 1, "tool": "ndpmp:atlas14_hyetograph",
              "hyetograph_folder": folder, "summary_csv": summary, "area_mi2": area_mi2, "areal_factor": areal_factor,
              "areal_factor_note": ("Atlas 14 values are point values. areal_factor multiplies every depth; it is the "
                                    "user's areal reduction factor (1 = none). This tool computes no areal reduction."),
              "frequencies": list(frequencies), "durations": list(durations), "patterns": list(patterns),
              "step_min": step_min, "quartile": quartile, "percent_of_occurrence": percent,
              "atlas14": {k: pf.get(k) for k in ("volume", "version", "region", "region_code", "series", "lat", "lon",
                                                 "volumes", "points")},
              "location": location, "sources": sources, "storms": storms, "skipped": skipped,
              "removed_earlier_storms": removed, "citation": cite,
              "note": NOTE}
    path = os.path.join(out_folder, RECORD_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1, default=str)
    record["record_json"] = path
    return record
