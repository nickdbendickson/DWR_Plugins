# -*- coding: utf-8 -*-
"""Fill a dam's missing height and storage from the National Inventory of Dams (NID).

Only blanks are filled: a DWR value above 0 is never replaced. 0 counts as blank, because
the DWR inventory stores "not recorded" as 0.

Two sources:
* the NID layer in the suite data folder (hydro/NID_dams.gpkg in the offline pack). It is
  read-only and on disk, so the wizard uses it as soon as a dam is listed;
* the public NID web service (nid.sec.usace.army.mil). The wizard only asks it when the
  engineer clicks "Fill from NID (online)": no network call is made otherwise.

A dam is matched by its NID id (the DWR 'national_id'), else by the nearest NID dam within
MATCH_M metres of the DWR point. No Qt here, so this is tested without a display.
"""
import json
import math
import os
import sqlite3
import struct

MATCH_M = 100.0
NID_API = "https://nid.sec.usace.army.mil/api"
TIMEOUT_S = 15

# NID field names, best first. The layer uses the ArcGIS names, the web service camelCase.
HEIGHT_KEYS = ("NID_HEIGHT", "nidHeight", "DAM_HEIGHT", "damHeight")
STORAGE_KEYS = ("MAX_STORAGE", "maxStorage", "NID_STORAGE", "nidStorage",
                "NORMAL_STORAGE", "normalStorage")
API_PROPS = ("nidId", "name", "nidHeight", "damHeight", "maxStorage", "nidStorage",
             "normalStorage", "latitude", "longitude")


def number(value):
    """value as a float above 0, else None (None, '', 0, negative and text are blank)."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return None
    return val if val > 0 and not math.isnan(val) else None


def nid_values(rec):
    """(height_ft, height_field, storage_acft, storage_field) from one NID record."""
    rec = rec or {}
    low = {str(k).lower(): k for k in rec}

    def first(keys):
        for key in keys:
            k = low.get(key.lower())
            if k is not None and number(rec.get(k)) is not None:
                return number(rec.get(k)), key.upper() if "_" in key else key
        return None, ""
    h, hf = first(HEIGHT_KEYS)
    s, sf = first(STORAGE_KEYS)
    return h, hf, s, sf


def _nid_id(rec):
    for key in ("NIDID", "nidId", "nid_id"):
        if rec.get(key):
            return str(rec[key]).strip()
    return ""


def metres(lon1, lat1, lon2, lat2):
    """Ground distance in metres (equirectangular: fine at 100 m)."""
    k = math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot((lon2 - lon1) * k, lat2 - lat1) * 111320.0


def parse_point(text):
    """(lon, lat) from the wizard's 'lon,lat [EPSG:4326]', or None. Other CRSs are not
    degrees, so they are not matched by distance."""
    raw = str(text or "")
    crs = raw.split("[", 1)[1].strip(" ]").upper() if "[" in raw else "EPSG:4326"
    if crs not in ("EPSG:4326", "OGC:CRS84", ""):
        return None
    try:
        lon, lat = [float(p) for p in raw.split("[")[0].split(",")[:2]]
    except ValueError:
        return None
    if not (-180 <= lon <= 180 and -90 <= lat <= 90) or (lon == 0 and lat == 0):
        return None
    return lon, lat


def gpkg_point(blob):
    """(x, y) of a GeoPackage point geometry blob, or None."""
    try:
        blob = bytes(blob)
        if blob[:2] != b"GP":
            return None
        flags = blob[3]
        env = (flags >> 1) & 7
        start = 8 + {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env, 0)
        wkb = blob[start:]
        end = "<" if wkb[0] == 1 else ">"
        gtype = struct.unpack(end + "I", wkb[1:5])[0] % 1000
        if gtype != 1:
            return None
        return struct.unpack(end + "dd", wkb[5:21])
    except Exception:
        return None


_CACHE = {}


def load_layer(uri):
    """The NID layer's records [{field: value, '_lon', '_lat'}] (cached by file and time).
    [] when the file is missing or unreadable. Opened read-only."""
    path, layer = str(uri or ""), None
    if "|layername=" in path:
        path, layer = path.split("|layername=", 1)
    path = path.split("|", 1)[0]
    if not path or not os.path.isfile(path):
        return []
    key = (path, layer, os.path.getmtime(path))
    if key in _CACHE:
        return _CACHE[key]
    recs = []
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT table_name FROM gpkg_contents WHERE data_type = 'features'")]
            table = layer if layer in tables else next(
                (t for t in tables if "nid" in t.lower()), tables[0] if tables else None)
            if table:
                gcol = con.execute("SELECT column_name FROM gpkg_geometry_columns "
                                   "WHERE table_name = ?", (table,)).fetchone()
                gcol = gcol[0] if gcol else None
                cur = con.execute('SELECT * FROM "{}"'.format(table))
                cols = [d[0] for d in cur.description]
                for rec in cur:
                    data = dict(zip(cols, rec))
                    low = {c.lower(): c for c in cols}
                    lon = data.get(low.get("longitude", ""), None)
                    lat = data.get(low.get("latitude", ""), None)
                    if (lon is None or lat is None) and gcol:
                        xy = gpkg_point(data.get(gcol))
                        if xy:
                            lon, lat = xy
                    data.pop(gcol, None)
                    try:
                        data["_lon"], data["_lat"] = float(lon), float(lat)
                    except (TypeError, ValueError):
                        data["_lon"] = data["_lat"] = None
                    recs.append(data)
        finally:
            con.close()
    except sqlite3.Error:
        recs = []
    _CACHE.clear()
    _CACHE[key] = recs
    return recs


def match(records, nid_id="", point=None, max_m=MATCH_M):
    """(record, how) for a dam: by NID id, else the nearest within max_m metres.
    how is 'NID id' or 'nearest NID dam, 42 m'. (None, '') when nothing matches."""
    nid_id = str(nid_id or "").strip().upper()
    if nid_id:
        for rec in records:
            if _nid_id(rec).upper() == nid_id:
                return rec, "NID id"
    if point:
        lon, lat = point
        best, dist = None, None
        for rec in records:
            if rec.get("_lon") is None:
                continue
            d = metres(lon, lat, rec["_lon"], rec["_lat"])
            if dist is None or d < dist:
                best, dist = rec, d
        if best is not None and dist <= max_m:
            return best, "nearest NID dam, {:.0f} m".format(dist)
    return None, ""


def apply(row, rec, how, where):
    """Fill row['height'] / row['storage'] from an NID record where they are blank.
    Sets row['height_source'] / row['storage_source'] for what it filled.
    Returns the names filled, e.g. ['storage']."""
    if not rec:
        return []
    h, hf, s, sf = nid_values(rec)
    nid = _nid_id(rec) or "?"
    done = []
    for key, val, field in (("height", h, hf), ("storage", s, sf)):
        if val is None or number(row.get(key)) is not None:
            continue
        row[key] = val
        row[key + "_source"] = "NID {} {} ({}, {})".format(nid, field, where, how)
        done.append(key)
    if done:
        row["nid_match"] = nid
        if not row.get("nid"):
            row["nid"] = nid
    return done


def needs(row):
    """True when the dam still has no height or no storage."""
    return number((row or {}).get("height")) is None or number((row or {}).get("storage")) is None


NORMAL_POOL_SOURCE = ("DWR normal pool volume - NOT max storage, under-estimates the breach "
                      "(no DWR max pool volume and no NID storage on record)")


def from_normal_pool(row):
    """True when the row's storage is only the DWR normal pool fallback (see fill_rows)."""
    return bool((row or {}).get("storage_normal_pool"))


def mark_dwr(row):
    """Source labels for the values the DWR record itself supplied (a DWR value always wins).
    A blank storage is left blank here: the NID max storage is tried first (fill_rows)."""
    for key in ("height", "storage"):
        if number(row.get(key)) is not None and not row.get(key + "_source"):
            row[key + "_source"] = "DWR"


def storage_fallback(row):
    """After the NID: the conservative storage when the DWR has no max pool volume.
    * the NID filled it, but the DWR normal pool is larger -> the larger (DWR normal pool);
    * nothing filled it -> the DWR normal pool, flagged non-conservative
      (row['storage_normal_pool'] = True, so the wizard still offers the NID web lookup).
    A DWR max pool volume or a typed value is never touched. Returns True when it changed."""
    normal = number(row.get("normal_vol"))
    if normal is None or str(row.get("storage_source") or "") in ("DWR", "typed in"):
        return False
    store = number(row.get("storage"))
    if store is None:
        row["storage"] = normal
        row["storage_source"] = NORMAL_POOL_SOURCE
        row["storage_normal_pool"] = True
        return True
    if str(row.get("storage_source") or "").startswith("NID ") and normal > store:
        row["storage_source"] = ("DWR normal pool volume (larger than {}: the larger "
                                 "is used)".format(row["storage_source"]))
        row["storage"] = normal
        row.pop("storage_normal_pool", None)
        return True
    return False


def fill_rows(rows, nid_uri):
    """Fill blanks in the listed dams: DWR values first (mark_dwr), then the local NID layer
    (its MAX_STORAGE first), then the DWR normal pool as the last resort (storage_fallback).
    Returns how many dams got an NID value."""
    recs = load_layer(nid_uri) if nid_uri else []
    filled = 0
    for row in rows or []:
        if row.get("error"):
            continue
        mark_dwr(row)
        if recs and needs(row):
            rec, how = match(recs, row.get("nid"), parse_point(row.get("point")))
            if apply(row, rec, how, "local NID layer"):
                filled += 1
        storage_fallback(row)
    return filled


# ---- the public web service (only on the engineer's click)

def online_urls(row):
    """The NID web requests for a dam, best first: its inventory record by NID id, then the
    NID dams within MATCH_M of its point."""
    urls = []
    nid = str((row or {}).get("nid") or "").strip()
    if nid:
        urls.append(("id", "{}/dams/{}/inventory".format(NID_API, nid)))
    pt = parse_point((row or {}).get("point"))
    if pt:
        miles = MATCH_M / 1609.344 * 1.5
        urls.append(("near", "{}/query?nr={:.6f},{:.6f},{:.4f}&addProps={}".format(
            NID_API, pt[0], pt[1], miles, ",".join(API_PROPS))))
    return urls


def online_records(kind, text):
    """NID records from a web answer: the inventory object, or the list of a query."""
    data = json.loads(text) if text else None
    if kind == "id":
        return [data] if isinstance(data, dict) and _nid_id(data) else []
    recs = []
    for rec in data if isinstance(data, list) else []:
        if not isinstance(rec, dict):
            continue
        rec = dict(rec)
        try:
            rec["_lon"], rec["_lat"] = float(rec.get("longitude")), float(rec.get("latitude"))
        except (TypeError, ValueError):
            rec["_lon"] = rec["_lat"] = None
        recs.append(rec)
    return recs


def fill_online(row, answers):
    """Fill blanks from web answers [(kind, text)] in online_urls order. Returns
    (names filled, message for the page)."""
    for kind, text in answers:
        try:
            recs = online_records(kind, text)
        except ValueError:
            continue
        if kind == "id":
            rec, how = (recs[0], "NID id") if recs else (None, "")
        else:
            rec, how = match(recs, "", parse_point(row.get("point")))
        if rec:
            done = apply(row, rec, how, "NID web service")
            if "storage" in done:
                row.pop("storage_normal_pool", None)
                storage_fallback(row)
            nid = _nid_id(rec)
            if done:
                return done, "Filled {} from NID {} (online).".format(" and ".join(done), nid)
            return [], "NID {} has no {} either: type it in.".format(
                nid, " or ".join(k for k in ("height", "storage") if number(row.get(k)) is None))
    return [], "This dam is not in the NID (by id or within {:.0f} m): type the values in.".format(
        MATCH_M)
