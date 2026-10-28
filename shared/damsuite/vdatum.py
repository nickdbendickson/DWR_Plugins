"""NGVD29 -> NAVD88 for the dam elevations the plugins compare with the LiDAR DEM (1.18.0).

The ND LiDAR DEMs are NAVD88. DWR and NID inventory elevations (top of dam, normal pool, max
pool, spillway crest) and values an engineer types are often NGVD29 (the "1929 datum" of the
original plans), about 0.6 - 2.1 ft lower in North Dakota. A value whose datum is not stated
as NAVD88 is taken as NGVD29 and shifted with NOAA's VERTCON at the dam (Nick, 2026-10-28:
"convert the 1071.15 NGVD29 to NAVD88 ... for all dams that do not state the elevations as
NAVD88"); one stated NAVD88 (or "DEM", read from the DEM itself) is used as it is.

Offline: the shift comes from the bundled VERTCON grid clipped to North Dakota (vertcon_nd.py,
public-domain NOAA data, bilinear between the 0.05 deg nodes). Outside the clip, pyproj with
PROJ's us_noaa_vertconc.tif is tried when both are installed (no network request: PROJ's
network access is left as it is set); else the values are used unconverted, with a warning.
No web service (NCAT) is called.

Local datums (1.18.1, Nick 2026-10-28: "if elevation is 100 or similar, they would be local
and would have to take actual elevations from dem"): plans often use an assumed datum (top of
dam 100.0 ft). local_check() flags a top of dam (or, with no top, a pool) as local when
  * it lies outside ND_MIN_FT .. ND_MAX_FT (500 - 3,600 ft; ND ground is about 750 ft at the
    Red River to 3,506 ft at White Butte, so no ND elevation on NGVD29 / NAVD88 is outside it),
  * it is more than DEM_TOL_FT (50 ft) from the DEM crest near the dam after the NGVD29 shift
    (the NGVD29 / NAVD88 difference in ND is under 2.1 ft; the inventory heights are rarely
    off by more than a few feet), or
  * it is a round value (a multiple of 100 ft: 100, 1000, 1100 ...) more than ROUND_TOL_FT
    (10 ft) from the DEM crest.
A local value is not datum-converted: the top of dam becomes the DEM crest and every other
dam elevation keeps its height relative to the local top (DEM top - (local top - value)).
The record then has source_datum "local (assumed)" (or "local" when stated / chosen), method
"DEM", the original values in fields and a warning. MODE "local" (the window's "Local (use
DEM)") forces it; an explicit NAVD88 / NGVD29 choice turns the detection off.

    rec = plan(lon, lat, stated="", override="auto")
    rec["shift_ft"], rec["applied"], rec["warnings"]
    new = convert_ft(1071.15, rec, "top_of_dam_ft")    # 1072.239 at DWR-2801 (NCAT: 1072.24)

plan() records what was done: {"target": "NAVD88", "source_datum", "how" ("stated" /
"assumed" / "override"), "stated", "override", "lon", "lat", "shift_m", "shift_ft", "method",
"applied", "fields": {name: {"source": ft, "navd88": ft}}, "warnings": [...]}. That dict is
what the Runner writes into run_report.json and dam.json (extra "vertical_datum") and what
ND Dam Hazard prints in its summary (summary_lines).

Pure Python standard library."""
import base64
import math
import struct
import zlib

NAVD88 = "NAVD88"
NGVD29 = "NGVD29"
LOCAL = "local"
TARGET = NAVD88
AUTO = "auto"
MODES = (AUTO, NAVD88, NGVD29, LOCAL)
LOCAL_ASSUMED = "local (assumed)"   # source_datum of a value detected as local
METHOD_DEM = "DEM"
# local_check thresholds (see the module text)
ND_MIN_FT = 500.0
ND_MAX_FT = 3600.0
DEM_TOL_FT = 50.0
ROUND_TOL_FT = 10.0
ROUND_STEP_FT = 100.0
NAVD88_TAG = "NAVD88 ft"          # dam.json top_of_dam_datum of a converted (or NAVD88) value
FT = 0.3048
METHOD_GRID = "VERTCON grid (NOAA vertconc.gtx 2018, bundled ND clip, bilinear)"
METHOD_PYPROJ = "VERTCON grid via pyproj vgridshift (PROJ us_noaa_vertconc.tif)"
METHOD_NONE = "none"

_GRID = None


def classify(text):
    """NAVD88 / NGVD29 / "local" / "" (not stated or not recognised) for a datum text:
    'NAVD88 ft', 'NAVD 88', 'DEM', 'DEM estimate', 'LiDAR' -> NAVD88; 'NGVD29', 'NGVD 1929',
    'MSL', '1929' -> NGVD29; 'local', 'assumed', 'arbitrary' -> local."""
    t = str(text or "").strip().lower()
    if not t:
        return ""
    navd = "navd" in t or "1988" in t or t == "dem" or t.startswith(("dem ", "dem(", "lidar"))
    ngvd = "ngvd" in t or "1929" in t or "msl" in t or "mean sea level" in t
    if navd and ngvd:
        return ""                 # 'NGVD29 / NAVD88': ambiguous - treated as not stated
    if navd:
        return NAVD88
    if ngvd:
        return NGVD29
    if "local" in t or "assumed" in t or "arbitrary" in t:
        return LOCAL
    return ""


def mode(text):
    """The override setting from a text or an index (0 auto, 1 NAVD88, 2 NGVD29, 3 local)."""
    if isinstance(text, int) and not isinstance(text, bool):
        return MODES[text] if 0 <= text < len(MODES) else AUTO
    c = classify(text)
    return c if c in (NAVD88, NGVD29, LOCAL) else AUTO


def _grid():
    global _GRID
    if _GRID is None:
        from . import vertcon_nd as V
        raw = zlib.decompress(base64.b64decode("".join(V.DATA)))
        n = V.NCOLS * V.NROWS
        _GRID = (V, struct.unpack("<{}h".format(n), raw))
    return _GRID


def grid_shift_m(lon, lat):
    """NAVD88 - NGVD29 (m) from the bundled ND grid, or None outside it."""
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    V, vals = _grid()
    fi = (lon - V.LON0) / V.STEP
    fj = (V.LAT_TOP - lat) / V.STEP
    eps = 1e-9
    if fi < -eps or fj < -eps or fi > V.NCOLS - 1 + eps or fj > V.NROWS - 1 + eps:
        return None
    fi = min(max(fi, 0.0), V.NCOLS - 1.0)
    fj = min(max(fj, 0.0), V.NROWS - 1.0)
    i0, j0 = min(int(fi), V.NCOLS - 2), min(int(fj), V.NROWS - 2)
    tx, ty = fi - i0, fj - j0

    def at(j, i):
        return vals[j * V.NCOLS + i] / float(V.SCALE)
    top = at(j0, i0) * (1 - tx) + at(j0, i0 + 1) * tx
    bot = at(j0 + 1, i0) * (1 - tx) + at(j0 + 1, i0 + 1) * tx
    return top * (1 - ty) + bot * ty


def pyproj_shift_m(lon, lat):
    """The same offset through pyproj + PROJ's us_noaa_vertconc.tif, or None (no pyproj, no
    grid file, outside the grid)."""
    try:
        from pyproj import Transformer
        t = Transformer.from_pipeline(
            "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
            "+step +proj=vgridshift +grids=us_noaa_vertconc.tif +multiplier=1 "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg")
        _x, _y, z = t.transform(float(lon), float(lat), 0.0)
        z = float(z)
        return z if math.isfinite(z) and abs(z) < 5.0 else None
    except Exception:                                    # noqa - optional path
        return None


def shift_m(lon, lat, use_pyproj=True):
    """(NAVD88 - NGVD29 in m, method) at lon / lat (WGS 84 / NAD 83 degrees), or (None, why)."""
    s = grid_shift_m(lon, lat)
    if s is not None:
        return s, METHOD_GRID
    if use_pyproj:
        s = pyproj_shift_m(lon, lat)
        if s is not None:
            return s, METHOD_PYPROJ
    return None, ("outside the bundled North Dakota VERTCON grid and no pyproj / "
                  "us_noaa_vertconc.tif to fall back on")


def resolve(stated="", override=AUTO):
    """(source datum, how): the override when it is NAVD88 / NGVD29 / local; else the stated datum
    when recognised; else (NGVD29, "assumed")."""
    o = mode(override)
    if o != AUTO:
        return o, "override"
    c = classify(stated)
    if c:
        return c, "stated"
    return NGVD29, "assumed"


def plan(lon, lat, stated="", override=AUTO, use_pyproj=True, what="the dam elevations"):
    """The conversion record for one dam (see the module text). Nothing is converted here;
    convert_ft() applies it to each value and lists it in rec["fields"]."""
    src, how = resolve(stated, override)
    rec = {"target": TARGET, "source_datum": src, "how": how, "stated": str(stated or ""),
           "override": mode(override), "lon": _r(lon, 6), "lat": _r(lat, 6),
           "shift_m": 0.0, "shift_ft": 0.0, "method": METHOD_NONE, "applied": False,
           "fields": {}, "warnings": []}
    if src == NAVD88:
        rec["method"] = "none (NAVD88 {}: used as given)".format(
            "set by the engineer" if how == "override" else "as stated")
        return rec
    if src == LOCAL:
        rec["method"] = METHOD_DEM
        rec["local"] = True
        rec["warnings"].append("The datum of {} is local ({}): they are not datum-converted; "
                               "the top of dam is taken from the DEM and the other elevations "
                               "keep their height below it.".format(
                                   what, "set by the engineer" if how == "override"
                                   else repr(stated)))
        return rec
    s, method = shift_m(lon, lat, use_pyproj)
    if s is None:
        rec["method"] = "none ({})".format(method)
        rec["warnings"].append(
            "{} {} NGVD29 but could not be converted to NAVD88 ({}); they are used as given, "
            "which may put them about 1-2 ft low against the DEM.".format(
                what[0].upper() + what[1:], "were taken as" if how == "assumed" else "are",
                method))
        return rec
    rec.update(shift_m=round(s, 4), shift_ft=round(s / FT, 3), method=method, applied=True)
    rec["_shift_m"] = s
    if how == "assumed":
        rec["warnings"].append(
            "The datum of {} is not stated{}: NGVD29 was assumed and they were converted to "
            "NAVD88 ({:+.2f} ft, VERTCON at {:.5f}, {:.5f}). Set the source datum to NAVD88 if "
            "they already are.".format(what, " ({!r})".format(stated) if stated else "",
                                       s / FT, float(lat), float(lon)))
    return rec


def local_check(value_ft, dem_crest_ft=None, shift_ft=0.0, what="top of dam"):
    """(is_local, reason) for one elevation (ft, as given) against ND's range and, when given,
    the DEM crest near the dam (ft NAVD88). shift_ft: the NGVD29 shift the value would get."""
    try:
        v = float(value_ft)
    except (TypeError, ValueError):
        return False, ""
    if not math.isfinite(v):
        return False, ""
    if v < ND_MIN_FT or v > ND_MAX_FT:
        return True, ("the {} {:.2f} ft is outside North Dakota's range of ground elevations "
                      "(about 750 - 3,510 ft; checked {:.0f} - {:.0f} ft)".format(
                          what, v, ND_MIN_FT, ND_MAX_FT))
    try:
        c = float(dem_crest_ft) if dem_crest_ft is not None else None
    except (TypeError, ValueError):
        c = None
    if c is None or not math.isfinite(c):
        return False, ""
    d = v + float(shift_ft or 0.0) - c
    if abs(d) > DEM_TOL_FT:
        return True, ("the {} {:.2f} ft is {:+.1f} ft from the DEM crest near the dam "
                      "({:.2f} ft NAVD88; more than {:.0f} ft)".format(what, v, d, c,
                                                                       DEM_TOL_FT))
    if abs(v / ROUND_STEP_FT - round(v / ROUND_STEP_FT)) < 1e-9 and abs(d) > ROUND_TOL_FT:
        return True, ("the {} {:.2f} ft is a round value {:+.1f} ft from the DEM crest near "
                      "the dam ({:.2f} ft NAVD88; more than {:.0f} ft)".format(
                          what, v, d, c, ROUND_TOL_FT))
    return False, ""


def convert_ft(value_ft, rec, field=None):
    """value_ft on NAVD88 under `rec` (plan()); None stays None. With `field` the value is
    listed in rec["fields"] (a field listed already is NOT converted again: the value
    recorded then is returned)."""
    if value_ft is None or rec is None:
        return value_ft
    try:
        v = float(value_ft)
    except (TypeError, ValueError):
        return value_ft
    if field and field in rec.get("fields", {}):
        return rec["fields"][field]["_navd88"]
    out = v + float(rec.get("_shift_m", rec.get("shift_m", 0.0))) / FT if rec.get("applied") \
        else v
    if field:
        rec.setdefault("fields", {})[field] = {"source": round(v, 3), "navd88": round(out, 3),
                                               "_navd88": out}
    return out


def convert_m(value_m, rec, field=None):
    """The same for a value in metres."""
    if value_m is None or rec is None:
        return value_m
    ft = convert_ft(float(value_m) / FT, rec, field)
    return ft * FT


def public(rec):
    """The record without its private keys (for JSON files)."""
    if not rec:
        return rec
    out = {k: v for k, v in rec.items() if not k.startswith("_")}
    out["fields"] = {k: {a: b for a, b in v.items() if not a.startswith("_")}
                     for k, v in (rec.get("fields") or {}).items()}
    return out


def summary_lines(rec):
    """Plain-text lines (no bullets) for a report or summary."""
    if not rec:
        return []
    how = {"stated": "stated", "assumed": "NOT STATED - assumed",
           "override": "set by the engineer",
           "detected": "DETECTED from the values: " + str(rec.get("local_reason") or "")}.get(rec.get("how"), rec.get("how") or "")
    lines = ["Source datum: {} ({}{}); target NAVD88 (the DEM's).".format(
        rec.get("source_datum"), how,
        ", given as {!r}".format(rec["stated"]) if rec.get("stated") else "")]
    if rec.get("local"):
        lines.append("Local datum: not converted; elevations taken from the DEM ({}{}).".format(
            rec.get("dem_crest_from") or "DEM crest",
            "; DEM top - local top = {:+.2f} ft".format(rec["local_offset_ft"])
            if rec.get("local_offset_ft") is not None else ""))
    elif rec.get("applied"):
        lines.append("Shift NGVD29 -> NAVD88: {:+.3f} ft ({:+.4f} m) at lat {}, lon {}; method: "
                     "{}.".format(rec.get("shift_ft", 0.0), rec.get("shift_m", 0.0),
                                  rec.get("lat"), rec.get("lon"), rec.get("method")))
    else:
        lines.append("No shift applied; method: {}.".format(rec.get("method")))
    for k, v in sorted((rec.get("fields") or {}).items()):
        if rec.get("local") and v.get("navd88") is None:
            lines.append("{}: {:.2f} local -> not used ({})".format(
                k, v["source"], v.get("note") or "no local top to place it"))
        elif rec.get("local"):
            lines.append("{}: {:.2f} local -> {:.2f} NAVD88 (DEM)".format(k, v["source"],
                                                                         v["navd88"]))
        elif rec.get("applied"):
            lines.append("{}: {:.2f} {} -> {:.2f} NAVD88".format(k, v["source"],
                                                                 rec.get("source_datum"),
                                                                 v["navd88"]))
        else:
            lines.append("{}: {:.2f} (as given)".format(k, v["source"]))
    for w in rec.get("warnings") or []:
        lines.append("WARNING: " + w)
    return lines


def _r(v, n):
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return None
