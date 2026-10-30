# -*- coding: utf-8 -*-
"""v3.13.0 (Nick: "downstream slope data is in dwr data"): the dam's downstream slope from its
ND DWR dam record, for the breach inflow's toe band (workflow._toe_kwargs ->
terrain.toe_band_for_dam: the band runs out to H x (slope + 1)).

The DWR_dams layer has `downstream_slope` (text): H of H:1V as typed, mostly "3", "2.5",
"3.0", sometimes "2:1", "2.5;3", "3,3.5,", "4 STEP", "3`", "varies", "0" or a stray number
("37.5", "80"). parse_ds_slope() reads it; anything it cannot read as a slope of 1:1 to 10:1
is "not known" (the toe band then uses 3:1 and the run warns).

Order (workflow._toe_kwargs): RunSettings.inflow_toe_ds_slope_h, then the dam's own
DS_SLOPE / F_DS (DamData.ds_slope_h), then this DWR value (DamData.dwr_ds_slope_h, set by
apply_to_dam), else 3:1 with a warning. Only the toe band uses the DWR value: the breach
model and the HEC-RAS embankment keep DS_SLOPE / F_DS as before.

The record is looked up by the dam's DWR structure number (dam.json dwr_structure_id, else the
digits of a "DWR-2801" dam ID) in, first, the dam folder's DWR dams (Data's "Get data for this
dam": data/dwr_dams.gpkg in dam.json's data section), then the suite's DWR dams
(damsuite.suitedata.dwr_layers: the dwr/ drop-in, else the offline pack). Read-only; never
raises."""
import math
import os
import re
import sqlite3

SLOPE_FIELDS = ("downstream_slope", "downstream_slope_h", "ds_slope", "ds_slope_h",
                "downstream")                     # "downstream" = a shapefile's 10 characters
ID_FIELDS = ("structure_id", "structure_", "structureid", "struct_id", "dwr_id")
SLOPE_MIN, SLOPE_MAX = 1.0, 10.0                  # H:1V a dam's downstream face can have
_NUM = r"(?:\d+(?:\.\d*)?|\.\d+)"


def parse_ds_slope(raw):
    """(H of H:1V or None, note) from a DWR downstream_slope value. "3", "3.0", 3.0, "2:1",
    "3`" -> 3 / 2; "2.5;3", "3,3.5," (several faces / berms) -> the flattest (largest), so the
    band reaches the toe; None / "" / "0" / "varies" / outside 1-10 -> None."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, "empty"
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        vals = [float(raw)]
    else:
        text = str(raw).strip()
        vals = []
        for a, b, single in re.findall(r"({0})\s*:\s*({0})|({0})".format(_NUM), text):
            try:
                if single:
                    vals.append(float(single))
                elif float(b) > 0:
                    vals.append(float(a) / float(b))
            except ValueError:
                pass
        if not vals:
            return None, "{!r} is not a number".format(text)
    ok = [v for v in vals if math.isfinite(v) and SLOPE_MIN <= v <= SLOPE_MAX]
    if not ok:
        return None, "{!r} is not a slope of {:g}:1 to {:g}:1".format(
            raw, SLOPE_MIN, SLOPE_MAX)
    z = max(ok)
    if len(vals) > 1:
        return z, "{!r}: the flattest, {:g}:1".format(raw, z)
    return z, "{!r} = {:g}:1".format(raw, z)


def dwr_ids(dam_id, rec=None):
    """The DWR structure numbers of the dam (as algorithms._dwr_ids): dam.json's
    dwr_structure_id and the digits of a 'DWR-2801' dam ID, without leading zeros."""
    out = set()
    v = None
    try:
        v = rec.get("dwr_structure_id") if rec is not None else None
    except Exception:            # noqa: BLE001
        v = None
    if v not in (None, ""):
        out.add(str(v).strip().split(".")[0].lstrip("0") or "0")
    m = re.match(r"^\s*DWR[-_ ]?0*(\d+)\s*$", str(dam_id or ""), re.I)
    if m:
        out.add(m.group(1).lstrip("0") or "0")
    return out


def _norm(v):
    return str(v).strip().split(".")[0].lstrip("0") or "0"


def _gpkg_rows(path, layer=""):
    """[{lower-case field: value}] of a GeoPackage's DWR dams table (no geometry)."""
    con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM gpkg_contents WHERE data_type='features'")]
        low = {t.lower(): t for t in tables}
        table = low.get((layer or "").lower()) or low.get("dwr_dams") or (
            tables[0] if len(tables) == 1 else None)
        if table is None:
            return []
        cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(table))]
        idc = next((c for c in cols if c.lower() in ID_FIELDS), None)
        if idc is None:
            return []
        want = [c for c in cols if c.lower() in ID_FIELDS + SLOPE_FIELDS]
        sql = 'SELECT {} FROM "{}"'.format(", ".join('"{}"'.format(c) for c in want), table)
        return [dict((c.lower(), v) for c, v in zip(want, row)) for row in con.execute(sql)]
    finally:
        con.close()


def _ogr_rows(path, layer=""):
    from osgeo import ogr                         # a shapefile / other drop-in
    ds = ogr.Open(path)
    if ds is None:
        return []
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    out = []
    for f in lyr or ():
        out.append(dict((k.lower(), v) for k, v in f.items().items()))
    return out


def find_record(paths, ids):
    """({lower-case field: value}, path) of the first DWR dams record whose structure id is one
    of `ids`, or (None, why not)."""
    if not ids:
        return None, "the dam has no DWR structure number"
    tried = []
    for p in paths or ():
        if not p:
            continue
        path, _, lay = str(p).partition("|")
        lay = lay.split("=", 1)[1] if lay.startswith("layername=") else ""
        if not os.path.isfile(path):
            continue
        tried.append(path)
        try:
            rows = _gpkg_rows(path, lay) if path.lower().endswith(".gpkg") else \
                _ogr_rows(path, lay)
        except Exception:        # noqa: BLE001 - an unreadable file: the next one
            continue
        for r in rows:
            if any(_norm(r.get(k)) in ids for k in ID_FIELDS if r.get(k) not in (None, "")):
                return r, path
    if not tried:
        return None, "no DWR dams layer (dam folder data / suite dwr / offline pack)"
    return None, "DWR structure {} not in {}".format(
        "/".join(sorted(ids)), ", ".join(os.path.basename(t) for t in tried))


def default_paths(folder=None, rec=None):
    """The DWR dams layers to look in: the dam folder's (dam.json data section), then the
    suite's (dwr/ drop-in, else the active offline pack)."""
    out = []
    try:
        from ..damsuite import damdata
        p = damdata.dam_data(folder).get("dwr_dams") if folder else None
        if p:
            out.append(p)
    except Exception:            # noqa: BLE001 - outside the plugin
        pass
    try:
        from ..damsuite import suitedata
        p = suitedata.dwr_layers().get("dwr_dams")
        if p and p not in out:
            out.append(p)
    except Exception:            # noqa: BLE001
        pass
    return out


def ds_slope_of(dam_id, rec=None, folder=None, paths=None):
    """(H of H:1V or None, note) of the dam's DWR record downstream_slope."""
    rows, where = find_record(default_paths(folder, rec) if paths is None else paths,
                              dwr_ids(dam_id, rec))
    if rows is None:
        return None, where
    raw = next((rows[k] for k in SLOPE_FIELDS if k in rows), None)
    z, note = parse_ds_slope(raw)
    return z, "DWR record downstream_slope {} ({})".format(note, os.path.basename(where))


def apply_to_dam(dam, folder=None, rec=None, paths=None):
    """Set dam.dwr_ds_slope_h / dam.dwr_ds_slope_note from the DWR record when the dam has no
    DS_SLOPE / F_DS. Returns the log line ("" when DS_SLOPE / F_DS is given)."""
    if getattr(dam, "ds_slope_h", None) not in (None, ""):
        return ""
    try:
        z, note = ds_slope_of(getattr(dam, "dam_id", ""), rec, folder, paths)
    except Exception as e:       # noqa: BLE001 - never stops a run
        z, note = None, "DWR record not read ({})".format(e)
    dam.dwr_ds_slope_h, dam.dwr_ds_slope_note = z, note
    return "Downstream slope for the inflow toe band: {}".format(
        note if z is not None else "{}; 3:1 is used".format(note))
