"""The screening's entries in the suite's dam record, `<Dams folder>/<dam ID>/dam.json`
(damsuite.dam_record; tool name "ndscreen", version = metadata.txt). Pure Python, no QGIS.

Written by
  * tool 7b for every dam it sends to ND Dam Breach Runner, BEFORE the Runner runs: the values
    it sends (dam_id, name, nid_id, dwr_structure_id, height_ft, storage_acft, surface_area_ac,
    the breach point as lon / lat and x / y / crs, existing_hazard_class), the dam length
    (dam_length_ft, 0.17.43) and a "screened" status. surface_area_ac or dam_length_ft
    the screening does not have is written as null with the reason in its source note
    (missing_notes, 0.17.43), so the record says why it is empty instead of leaving it out;
  * tool 8, when given the Dams folder, for every dam that ALREADY has a folder there (no folder
    is created): the dam's identity, height and existing class and a "screened" status.

surface_area_ac is the area at the top of dam (what the Runner reads for AREA_AC): only the
area tool 7b sends - from the tool-3 stage-storage curve, or (0.17.46) when that curve is too
shallow or missing (0.17.49: or no curve folder was given), the DWR inventory's maximum-pool area
(max_pool_area, recorded at the top of dam). Its source note says which (surface_area_source = stage_storage / inventory_max_pool_area).
The inventory's normal-pool area
(inv_area_m2: DWR normal_area / NID SURFACE_AREA) is NOT written there, since the Runner would
take it for the top-of-dam area and its storage-area exponent; it is quoted in the null's note.
dam_length_ft is the length of the mapped dam line (tool 4's crest_len_m), else the
inventory's dam length (tool 1's crest_len_m: DWR dam_length / NID DAM_LENGTH; 0 = not
recorded) - the order tool 5 uses. Tool 8 does not write it (it has no breach points, and a
second source would flip the value between runs).

Rules (damsuite.dam_record): values are set with set_many - a value another tool (or the
engineer) put there is never overwritten; the refusal is recorded in the file's "conflicts"
and reported back here for the log. The "screened" status result is
    {"triage": tri_result of tool 6 ("clear" / "needs 2D") or "not run",
     "hz_class": tool 8's class or None, "border": hz_border, "gaps": hz_gaps,
     "triage_reason": tri_reason, "by": "tool 7b" / "tool 8"}
so damsuite.dam_record.next_action says "model" unless the triage cleared the dam. A status
whose substance (triage, hz_class, border, gaps) equals the newest "screened" entry of this
tool is not added again - whichever tool (7b or 8) wrote it. Numbers are rounded to 3
decimals, as tool 7b sends them to the Runner (round3). A dam.json that
cannot be read (broken, foreign, newer format) is never replaced: the dam is reported and
skipped.

lon / lat of the breach point are computed from EPSG:5070 with the Albers equal-area inverse
(Snyder 1987) on GRS 1980 - NAD83 degrees, used as WGS 84 (they differ by about 1 m here).
"""
import configparser
import datetime
import math
import os
import re

try:
    from ..damsuite import dam_record as dr
except ImportError:                                   # core/ itself on sys.path (tests)
    import importlib
    dr = importlib.import_module("damsuite.dam_record")

TOOL = "ndscreen"
FT = 0.3048
AF_M3 = 1233.4818375
ACRE_M2 = 4046.8564224
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plugin_version(folder=_PLUGIN_DIR):
    """version= of the plugin's metadata.txt ("" when it cannot be read)."""
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        with open(os.path.join(folder, "metadata.txt"), encoding="utf-8-sig") as fh:
            cp.read_file(fh)
        return cp.get("general", "version", fallback="").strip()
    except (OSError, configparser.Error):
        return ""


# ------------------------------------------------------------------ EPSG:5070 inverse
_A, _INVF = 6378137.0, 298.257222101
_E2 = 2.0 / _INVF - 1.0 / _INVF ** 2
_E = math.sqrt(_E2)


def _q(phi):
    s = math.sin(phi)
    return (1 - _E2) * (s / (1 - _E2 * s * s) - (1 / (2 * _E)) * math.log((1 - _E * s) / (1 + _E * s)))


def _m(phi):
    s = math.sin(phi)
    return math.cos(phi) / math.sqrt(1 - _E2 * s * s)


_P1, _P2, _P0, _L0 = (math.radians(v) for v in (29.5, 45.5, 23.0, -96.0))
_N = (_m(_P1) ** 2 - _m(_P2) ** 2) / (_q(_P2) - _q(_P1))
_C = _m(_P1) ** 2 + _N * _q(_P1)
_RHO0 = _A * math.sqrt(_C - _N * _q(_P0)) / _N


def lonlat_from_5070(x, y):
    """(lon, lat) degrees of an EPSG:5070 point."""
    dy = _RHO0 - y
    rho = math.hypot(x, dy)
    theta = math.atan2(x, dy)
    q = (_C - (rho * _N / _A) ** 2) / _N
    phi = math.asin(max(-1.0, min(1.0, q / 2)))
    for _i in range(25):
        s, c = math.sin(phi), math.cos(phi)
        nxt = phi + (1 - _E2 * s * s) ** 2 / (2 * c) * (
            q / (1 - _E2) - s / (1 - _E2 * s * s) + (1 / (2 * _E)) * math.log((1 - _E * s) / (1 + _E * s)))
        if abs(nxt - phi) < 1e-13:
            phi = nxt
            break
        phi = nxt
    return math.degrees(_L0 + theta / _N), math.degrees(phi)


# ------------------------------------------------------------------ values
def _txt(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return s if s and s.upper() not in ("NULL", "NONE") else None


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def identity(attrs, class_field=None):
    """dam_id, name, nid_id, dwr_structure_id and existing_hazard_class of a screening dam
    record (tool 1 names: dam_id 'DWR-5289' -> DWR structure 5289; 'NID-ND00123' has no DWR
    number)."""
    dam_id = _txt(attrs.get("dam_id"))
    out = {"dam_id": dam_id, "name": _txt(attrs.get("name")),
           "nid_id": (_txt(attrs.get("nid_id")) or "").upper() or None}
    m = re.match(r"^DWR-(.+)$", dam_id or "", re.I)
    if m:
        out["dwr_structure_id"] = m.group(1).strip()
    elif _txt(attrs.get("structure_id")):
        out["dwr_structure_id"] = _txt(attrs.get("structure_id"))
    if out["nid_id"] is None:
        m = re.match(r"^NID-(.+)$", dam_id or "", re.I)
        if m:
            out["nid_id"] = m.group(1).strip().upper()
    for f in (class_field, "haz_existing", "hazard_cla"):
        if f and _txt(attrs.get(f)):
            out["existing_hazard_class"] = _txt(attrs.get(f))
            break
    return {k: v for k, v in out.items() if v not in (None, "")}


def round3(x):
    """Every number the screening writes to dam.json is rounded like the values it sends to the
    Runner (handoff.rundam_params: 3 decimals), so tool 7b and tool 8 never write two values of
    one quantity that differ in the last digits (a later write would record a conflict)."""
    return None if x is None else round(float(x), 3)


def _pos_num(v):
    x = _num(v)
    return x if x is not None and x > 0 else None


def dam_length(attrs, line_len_m=None):
    """(dam length in ft rounded to 3 decimals, where it came from) or (None, ""): the length
    of the mapped dam line (tool 4's crest_len_m on the breach point), else the inventory's
    dam length (tool 1's crest_len_m: DWR dam_length / NID DAM_LENGTH) - the order tool 5
    uses for its overtopping crest."""
    m = _pos_num(line_len_m)
    if m is not None:
        return round3(m / FT), "length of the mapped dam line (tool 4)"
    m = _pos_num((attrs or {}).get("crest_len_m"))
    if m is not None:
        src = _txt((attrs or {}).get("source")) or "DWR / NID"
        return round3(m / FT), "dam inventory ({}: DWR dam_length / NID DAM_LENGTH)".format(src)
    return None, ""


def handoff_values(attrs, rundam_params, point_xy, crs="EPSG:5070", class_field=None,
                   line_len_m=None):
    """The values tool 7b sends to 'ndbreach:rundam', as dam record fields, and the dam
    length (dam_length; line_len_m = tool 4's crest_len_m of the dam's breach point)."""
    v = identity(attrs, class_field)
    p = rundam_params or {}
    for key, field in (("HEIGHT_FT", "height_ft"), ("STORAGE_ACFT", "storage_acft"),
                       ("AREA_AC", "surface_area_ac")):
        if _num(p.get(key)) is not None:
            v[field] = round3(_num(p.get(key)))
    length_ft, _src = dam_length(attrs, line_len_m)
    if length_ft is not None:
        v["dam_length_ft"] = length_ft
    if point_xy is not None:
        x, y = float(point_xy[0]), float(point_xy[1])
        v.update({"x": round3(x), "y": round3(y), "crs": crs})
        if crs.upper() == "EPSG:5070":
            lon, lat = lonlat_from_5070(x, y)
            v.update({"lon": round(lon, 7), "lat": round(lat, 7)})
    return v


def tool8_values(attrs, class_field=None):
    v = identity(attrs, class_field)
    h = _num(attrs.get("height_m"))
    if h is not None and h > 0:
        v["height_ft"] = round3(h / FT)          # the same rounding as tool 7b's HEIGHT_FT
    return v


def missing_notes(values, attrs=None, rundam_notes=None):
    """{field: reason} for surface_area_ac / dam_length_ft that tool 7b has no value for; write()
    records them as null with that reason (0.17.43: they were left out, so a missing value
    could not be told from a field the screening never fills)."""
    values, attrs = values or {}, attrs or {}
    out = {}
    if values.get("surface_area_ac") is None:
        # the most specific reason: the shallow-curve note (handoff.area_for_runner) is added
        # after rundam_params' generic "no surface area at the top of dam ..." line
        hits = [str(n) for n in (rundam_notes or []) if str(n).startswith("no surface area")]
        why = hits[-1] if hits else "no surface area at the top of dam from a tool-3 stage-storage curve"
        inv = _pos_num(attrs.get("inv_area_m2"))
        if inv is not None:
            why += ("; the inventory's normal-pool surface area is {:,.1f} ac (not written: the "
                    "Runner would read it as the area at the top of dam)".format(inv / ACRE_M2))
        out["surface_area_ac"] = why
    if values.get("dam_length_ft") is None:
        out["dam_length_ft"] = ("no mapped dam line (tool 4) and no dam length in the "
                                "inventory (DWR dam_length / NID DAM_LENGTH blank or 0)")
    return out


def screening_result(attrs, by):
    tri = _txt(attrs.get("tri_result"))
    res = {"triage": tri or "not run", "hz_class": _txt(attrs.get("hz_class")),
           "border": _txt(attrs.get("hz_border")), "gaps": _txt(attrs.get("hz_gaps")),
           "by": by}
    if _txt(attrs.get("tri_reason")):
        res["triage_reason"] = _txt(attrs.get("tri_reason"))
    if _txt(attrs.get("tri_audit")) == "yes":
        res["triage_audit"] = "yes (sent to the 2D model anyway)"
    return res


SUBSTANCE = ("triage", "hz_class", "border", "gaps")   # what next_action / the dashboards read


def same_substance(a, b):
    """True when two "screened" results agree on triage, class, borderline flag and data gaps
    (who wrote them - "by" - and the free-text reasons do not count)."""
    a, b = a or {}, b or {}
    norm = lambda v: None if v in (None, "") else str(v).strip()
    return all(norm(a.get(k)) == norm(b.get(k)) for k in SUBSTANCE)


def source_notes(attrs, rundam_notes=None, line_len_m=None):
    """dam.json notes for a height or storage the screening estimated, not a DWR value, and
    where the dam length came from."""
    attrs = attrs or {}
    low = " ".join(rundam_notes or []).lower()
    notes = {}
    stor = str(attrs.get("stor_method") or "").lower()
    if stor == "estimated" or "estimated storage" in low:
        notes["storage_acft"] = "estimated from the DEM (no DWR inventory storage)"
    if "no inventory height" in low or "estimat" in str(attrs.get("h_src") or "").lower():
        notes["height_ft"] = "estimated from the DEM (no DWR inventory height)"
    length_ft, src = dam_length(attrs, line_len_m)
    if length_ft is not None:
        notes["dam_length_ft"] = src
    area_src = surface_area_source(rundam_notes)
    if area_src:
        notes["surface_area_ac"] = AREA_SOURCE_NOTES[area_src]
    return notes


# 0.17.46: where the surface_area_ac tool 7b sent came from (handoff.surface_area_choice)
AREA_SOURCE_NOTES = {
    "stage_storage": "surface_area_source = stage_storage: the tool-3 stage-storage curve near the "
                     "top of dam",
    "inventory_max_pool_area": "surface_area_source = inventory_max_pool_area: the DWR inventory's "
                               "maximum-pool area (max_pool_area, acres; recorded at the top of dam), "
                               "because the stage-storage curve could not give the area or no curve "
                               "folder was given"}


def surface_area_source(rundam_notes):
    """'stage_storage' / 'inventory_max_pool_area' from handoff.rundam_params' area note, or None
    (no area sent)."""
    for n in rundam_notes or []:
        n = str(n)
        if n.startswith("surface area ") and "from the inventory's maximum-pool area" in n:
            return "inventory_max_pool_area"
        if n.startswith("surface area ") and "from the tool-3 curve" in n:
            return "stage_storage"
    return None


def write(folder, values, result, version=None, create=True, field_notes=None, missing=None):
    """Update <folder>/dam.json. Returns (path or None, [log lines]). create=False: nothing
    happens when the folder does not exist. missing: {field: reason} (missing_notes) written as
    null with the reason as the source note - only into a field that holds no value, so a value
    another tool (or a later run) recorded is never replaced by a null."""
    version = plugin_version() if version is None else version
    if not os.path.isdir(folder) and not create:
        return None, []
    try:
        rec = dr.load(folder, create=True)
    except ValueError as e:
        return None, ["dam.json not updated (the file is kept as it is): {}".format(e)]
    log = []
    field_notes = field_notes or {}
    plain = {k: v for k, v in (values or {}).items() if k not in field_notes}
    refused = list(rec.set_many(plain, TOOL, version, note="screening"))
    for name, note in field_notes.items():
        if name not in (values or {}):
            continue
        if not rec.set(name, values[name], TOOL, version, note=note):
            if values[name] not in (None, ""):
                refused.append(name)
    when = datetime.datetime.now().isoformat(timespec="seconds")
    for name, why in (missing or {}).items():
        if name in dr.FIELDS and rec.fields.get(name) is None and \
                (rec.sources.get(name) or {}).get("tool") in (None, "", TOOL):
            rec.fields[name] = None
            rec.sources[name] = {"tool": TOOL, "version": str(version or ""), "when": when,
                                 "note": "not known to the screening: " + str(why)}
    for name in refused:
        c = next((c for c in reversed(rec.conflicts) if c.get("field") == name), {})
        src = (c.get("old_source") or {}).get("tool") or "the engineer / unknown"
        log.append("dam.json: {} kept at {!r} (from {}); the screening's {!r} was not written "
                   "(recorded as a conflict)".format(name, c.get("old"), src, c.get("new")))
    last = rec.latest("screened")
    if not (last and last.get("tool") == TOOL and
            same_substance(last.get("result"), result)):
        rec.add_status("screened", TOOL, version, result)
    path = rec.save()
    return path, log
