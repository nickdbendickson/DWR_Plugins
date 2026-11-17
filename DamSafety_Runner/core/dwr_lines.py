# -*- coding: utf-8 -*-
"""3.20.13 (Nick, 2026-11-17: "dam point should be at the center of the embankment in most all
cases"; he chose "DWR line centre, else found centre"): a batch dam's DWR dam line.

Nick's 41 placed points of the batch of 44 lie on DWR's dam line (a median 1.8 m off it), and
all 44 dams have one in the pack (two are refused by the guard below: DWR-2659, 7.2 km from its
point, and DWR-4031, whose 55 m line is 506 m away). The single-dam tool could use a DWR line
(DAM_LINES), but the batch
tools (QGIS "batch", ndbreach_cli.py batch, and the chain, which runs the QGIS batch for each
dam) never did. Now each batch dam takes its DWR line, matched by its DWR structure id, from

1. a DAM_LINES layer when one is given (the QGIS batch; the CLI's layers.dam_lines), else
2. the engineer's own DWR dam lines file in the suite data folder's dwr/ (damsuite.suitedata
   .dwr_layers, as core/dwr_slope does for the DWR dams), else
3. the active offline pack's DWR_dam_lines data set, found through the pack's manifest
   (suitedata.pack_file: never a hard-coded file name), else
4. the pack named by ND_DAM_HAZARD_OFFLINE_PACK (the Hazard plugin's pack setting, which the
   batch kit exports), through its manifest too.

The line becomes the dam line exactly like a DAM_LINES line (dam.crest_line_xy: the mid-crest
point at its length midpoint, the crest raise, the toe-band inflow with the 3.20.12 side rules)
and the point goes to its centre. A user crest line (the batch's CREST_LINE) still wins; the
switch is USE_DWR_LINES / batch.use_dwr_lines, and per dam a Use_DWR_Line field / column of 0.

The structure id of a dam: an explicit structure id field, a structure_id-like field of the row,
the digits of a "DWR-2645" dam ID, dam.json's dwr_structure_id, else a NID (national_id,
NIDID ...) looked up in the DWR dams (national_id -> structure_id).

Guard: the line is refused (and the run warns) when its nearest point is more than NEAR_M from
the inventory point, its centre more than CENTRE_M, or it is degenerate (fewer than 2 distinct
vertices or shorter than MIN_LEN_M). DWR inventory points are often hundreds of metres off the
dam (median 160 m on the 44); DWR-2899's correct line has its centre 569 m from the point but
passes 426 m from it, DWR-2659's wrong line is 7.2 km away.

The centre is the line's length midpoint (place_midcrest). Tested against Nick's 41 marked dams
before coding (2026-11-17; median / 80th percentile distance to the mark, m): (a) the length
midpoint 28.3 / 73.1, (b) over the deepest section 26.6 / 80.1 (its toe variant 13.6 / 81.1),
(c) the middle of the longest straight part 23.5 / 108.7. (b) and (c) improve the median but
worsen the 80th percentile, and their median gain comes from the 'move' marks (the screen's own
suggested points, which favour depth rules); on the 16 points Nick placed himself (a) is best
(23.0 / 70.9). So (a) is kept. A variant outside the three - the middle of the longest stretch
at least half as deep as the deepest (17.7 / 61.0 on the 41, 25.6 / 66.1 on his 16) - is left
for a later decision.

Pure Python (sqlite3 for a GeoPackage, GDAL's ogr only for another file format): the CLI uses
it without QGIS. Coordinates come back in the file's CRS (LINE_CRS for the DWR lines, which are
labelled EPSG:4326 but are NAD83 longitude / latitude); the caller converts them."""
import math
import os
import pathlib
import re
import sqlite3
import threading

LINE_CRS = "EPSG:4269"     # the DWR lines are NAD83 lon/lat, labelled 4326 (holdout.py)
PACK_DATASET = "DWR_dam_lines"
DAMS_DATASET = "DWR_dams"
ENV_HAZARD_PACK = "ND_DAM_HAZARD_OFFLINE_PACK"
ENV = "NDBREACH_DWR_LINES"   # off / 0 / no / false: no DWR lines in any batch (a kill switch)

NEAR_M = 500.0             # the line's nearest point at most this far from the inventory point
CENTRE_M = 1000.0          # ... and its centre at most this far
MIN_LEN_M = 10.0           # shorter (or fewer than 2 distinct vertices): degenerate
JOIN_M = 2.0               # parts of one structure whose ends meet this close are one line
LAYER_RADIUS_M = 2000.0    # a DAM_LINES layer is read this far round the dam

SID_FIELDS = ("structure_id", "structure_", "structureid", "struct_id", "dwr_structure_id",
              "dwr_struct", "dwr_structure", "dwr_id")
NID_FIELDS = ("nidid", "nid_id", "national_id", "national_i", "federal_id")
SWITCH_FIELDS = ("use_dwr_line", "use_dwr_li", "use_dwr_lines")
OFF_WORDS = ("0", "no", "n", "false", "f", "off", "none", "skip")
ON_WORDS = ("1", "yes", "y", "true", "t", "on")
_NID_RE = re.compile(r"^\s*[A-Z]{2}\d{3,6}\s*$")

# the chain's runner-stage fingerprint (core/chain.stage_inputs): a resume reruns the Runner
# once when the placement rules change (3.20.13: DWR lines in batches, dam point check move)
PLACEMENT_RULE = "3.20.13"

SOURCE_LAYER = "the DAM_LINES layer"
SOURCE_PACK = "the pack"


def env_off(environ=None):
    """True when NDBREACH_DWR_LINES says off (0 / no / false / off)."""
    v = (os.environ if environ is None else environ).get(ENV)
    return v is not None and str(v).strip().lower() in OFF_WORDS


# ======================================================================== ids ============
def norm_id(v):
    """'2645', 2645, 2645.0, '02645', ' 2645 ' -> '2645'; None / '' / NULL -> ''."""
    if v is None:
        return ""
    if hasattr(v, "isNull") and v.isNull():
        return ""
    if isinstance(v, float):
        if v != v:
            return ""
        v = int(v) if v == int(v) else v
    s = str(v).strip()
    if not s or s.upper() in ("NULL", "NONE", "NAN"):
        return ""
    s = s.split(".")[0] if re.match(r"^\d+(\.0*)?$", s) else s
    if s.isdigit():
        return s.lstrip("0") or "0"
    return s


def dwr_number(dam_id):
    """'DWR-2645' / 'dwr_2645' / 'DWR 02645' -> '2645', else ''."""
    m = re.match(r"^\s*DWR[-_ ]?0*(\d+)\s*$", str(dam_id or ""), re.I)
    return (m.group(1).lstrip("0") or "0") if m else ""


def sid_key(v):
    """The structure id key of a value, the same on the dam side (structure_ids) and on the
    line side (LineIndex, find_in_lines, the batch's CREST_LINE, the NID table): 2645, '2645',
    2645.0, '02645', 'DWR-2645' -> '2645'; '' for a blank value or one that is no structure id
    (3.20.13 review: a lines layer whose id field holds 'DWR-3034' matches dam 3034)."""
    s = norm_id(v)
    return s if s.isdigit() else dwr_number(s)


def id_field_of(names):
    """The structure id field of a lines layer's field names: the first of SID_FIELDS (in that
    priority, case-insensitive) it has, else ''. One rule for every reader (the GeoPackage and
    OGR readers, the QGIS batch's DAM_LINES, the CLI's GeoJSON layers.dam_lines)."""
    low = {}
    for n in sorted((str(x) for x in names or ()), key=str):
        low.setdefault(n.lower(), n)
    return next((low[f] for f in SID_FIELDS if f in low), "")


def _get(attrs, names):
    """The first non-empty value of `names` (case-insensitive) in a dict of attributes."""
    if not attrs:
        return None, ""
    low = {}
    for k in attrs:
        low.setdefault(str(k).lower(), k)
    for n in names:
        k = low.get(n.lower())
        if k is None:
            continue
        v = attrs.get(k)
        if v is None or (hasattr(v, "isNull") and v.isNull()) or str(v).strip() in ("", "NULL"):
            continue
        return v, k
    return None, ""


def row_switch(attrs):
    """A dam's Use_DWR_Line value: False (0 / no / false / off), True (1 / yes / ...), None
    when the row has no such value."""
    v, _k = _get(attrs, SWITCH_FIELDS)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in OFF_WORDS:
        return False
    if s in ON_WORDS:
        return True
    try:
        return float(s) != 0.0
    except ValueError:
        return None


def structure_ids(dam_id, attrs=None, rec=None, field=None, nid_lookup=None):
    """[(structure id, how)] of a batch dam, in the order they are tried: the chosen field,
    a structure id field of the row, the digits of a DWR-NNNN dam ID, dam.json's
    dwr_structure_id, then a NID (row field, dam.json nid_id or a NID-like dam ID) looked up
    with nid_lookup(nid) -> structure id or ''."""
    out, seen = [], set()

    def add(v, how):
        s = sid_key(v)                      # a structure id field holding 'DWR-3034' too
        if s and s not in seen:
            seen.add(s)
            out.append((s, how))
    attrs = attrs or {}
    if field:
        v, _k = _get(attrs, (field,))
        add(v, "field {}".format(field))
    v, k = _get(attrs, SID_FIELDS)
    add(v, "field {}".format(k))
    add(dwr_number(dam_id), "dam ID {}".format(dam_id))
    if rec is not None:
        try:
            add(rec.get("dwr_structure_id"), "dam.json dwr_structure_id")
        except Exception:                                     # noqa: BLE001
            pass
    if nid_lookup is not None and not out:
        nids = []
        v, k = _get(attrs, NID_FIELDS)
        if v is not None:
            nids.append((str(v).strip(), "field {}".format(k)))
        if rec is not None:
            try:
                if rec.get("nid_id"):
                    nids.append((str(rec.get("nid_id")).strip(), "dam.json nid_id"))
            except Exception:                                 # noqa: BLE001
                pass
        if _NID_RE.match(str(dam_id or "")):
            nids.append((str(dam_id).strip(), "dam ID"))
        for nid, how in nids:
            try:
                sid = nid_lookup(nid)
            except Exception:                                 # noqa: BLE001
                sid = ""
            add(sid, "NID {} ({}) in the DWR dams".format(nid, how))
    return out


# ======================================================================== sources ========
def _suitedata():
    try:
        from ..damsuite import suitedata
        return suitedata
    except Exception:                                         # noqa: BLE001 - outside a plugin
        return None


def _hazard_pack_file(dataset, environ=None):
    """The data set's file in the pack ND_DAM_HAZARD_OFFLINE_PACK names (through the pack's
    manifest), or ''."""
    env = os.environ if environ is None else environ
    loc = (env.get(ENV_HAZARD_PACK) or "").strip()
    SD = _suitedata()
    if not loc or SD is None:
        return ""
    loc = os.path.abspath(os.path.expanduser(loc))
    if not os.path.exists(loc):
        return ""
    try:
        from ..damsuite import gpkgpack as GP
        folder = GP.pack_folder_of(loc) or loc
    except Exception:                                         # noqa: BLE001
        folder = loc
    try:
        return SD.pack_file(dataset, folder) or ""
    except Exception:                                         # noqa: BLE001
        return ""


def pack_candidates(environ=None):
    """[(path, label)] of the DWR dam lines a batch may use when no DAM_LINES layer is given, in
    order: the engineer's dwr/ file, the active pack's DWR_dam_lines (its manifest), the
    ND_DAM_HAZARD_OFFLINE_PACK pack's."""
    out = []
    SD = _suitedata()
    if SD is not None:
        try:
            p = SD.dwr_layers(pack=False).get("dwr_dam_lines")
            if p and os.path.exists(p.split("|")[0]):
                out.append((p, "your DWR file {}".format(os.path.basename(p.split("|")[0]))))
        except Exception:                                     # noqa: BLE001
            pass
        try:
            pk = SD.active_pack()
            p = SD.pack_file(PACK_DATASET, pk) if pk else ""
            if p:
                out.append((p, SOURCE_PACK))
        except Exception:                                     # noqa: BLE001
            pass
    p = _hazard_pack_file(PACK_DATASET, environ)
    if p and p not in [c[0] for c in out]:
        out.append((p, SOURCE_PACK))
    return out


def pack_source(environ=None):
    """(path, label) of the first of pack_candidates that gives lines (a damaged, empty or
    unreadable file - a zero-byte copy, a .zip drop-in, a .shp without GDAL - is passed over
    for the next); ('', why not) when none does."""
    skipped = []
    for path, label in pack_candidates(environ):
        idx = line_index(path, label)
        if idx is not None and idx.count:
            return path, label
        why = getattr(idx, "error", "") if idx is not None else ""
        skipped.append("{} gave no lines{}".format(
            os.path.basename(str(path).split("|")[0]), " ({})".format(why) if why else ""))
    if skipped:
        return "", "no DWR dam lines: " + "; ".join(skipped)
    return "", "no DWR dam lines: no offline pack (or its DWR_dam_lines) and no dwr/ file"


def open_lines(environ=None):
    """(LineIndex or None, path, label or why not) of the batch's DWR dam lines when no
    DAM_LINES layer is given (pack_source). Never raises."""
    try:
        path, label = pack_source(environ)
    except Exception as e:                                    # noqa: BLE001 - never stop a batch
        return None, "", "no DWR dam lines: {}: {}".format(type(e).__name__, e)
    if not path:
        return None, "", label
    idx = line_index(path, label)
    if idx is None or not idx.count:
        return None, path, "no lines read from {}".format(path)
    return idx, path, label


def dams_source(environ=None):
    """The DWR dams file for the NID -> structure id lookup, or ''."""
    SD = _suitedata()
    if SD is not None:
        try:
            p = SD.dwr_layers().get("dwr_dams")
            if p:
                return p
        except Exception:                                     # noqa: BLE001
            pass
    return _hazard_pack_file(DAMS_DATASET, environ)


# ======================================================================== reading ========
def _connect_ro(path):
    """A read-only SQLite connection to a file, its path encoded as a URI (offline_pack
    .sqlite_ro_uri: '#', '?', '%' in a folder name, a Windows share //server/share)."""
    try:
        from ..damsuite.offline_pack import sqlite_ro_uri
    except Exception:                                         # noqa: BLE001 - outside a plugin
        def sqlite_ro_uri(p):
            uri = p.as_uri()
            if uri.startswith("file://") and not uri.startswith("file:///"):
                uri = "file:////" + uri[len("file://"):]
            return uri + "?mode=ro"
    return sqlite3.connect(sqlite_ro_uri(pathlib.Path(os.path.abspath(path))), uri=True)


def _gpkg_table(con, layer, want_lines):
    rows = con.execute("SELECT c.table_name, g.column_name, g.geometry_type_name, g.srs_id "
                       "FROM gpkg_contents c JOIN gpkg_geometry_columns g "
                       "ON c.table_name = g.table_name WHERE c.data_type = 'features'").fetchall()
    if not rows:
        return None
    if layer:
        for r in rows:
            if r[0].lower() == layer.lower():
                return r
    if want_lines:
        ln = [r for r in rows if "LINE" in str(r[2]).upper()]
        rows = ln or rows
        named = [r for r in rows if "line" in r[0].lower()]
        rows = named or rows
    else:
        named = [r for r in rows if "dwr_dams" == r[0].lower()]
        rows = named or rows
    return rows[0]


def _crs_of(srs_id, org=None, org_id=None, definition=None):
    """The CRS text of a GeoPackage srs_id with its gpkg_spatial_ref_sys row (organization,
    organization_coordsys_id, definition): 'EPSG:n', another authority's 'ESRI:102039', else
    the WKT definition (a custom CRS); lon/lat 4326 / 4269 and the undefined 0 / -1 are
    LINE_CRS. 3.20.13 review: an srs_id is not always an EPSG code (ESRI:102039 is srs_id
    102039 with organization ESRI; GDAL numbers a custom CRS 100000 and up)."""
    try:
        s = int(srs_id)
    except (TypeError, ValueError):
        return LINE_CRS
    if s in (0, -1):
        return LINE_CRS
    o = str(org or "").strip().upper()
    try:
        code = int(org_id)
    except (TypeError, ValueError):
        code = None
    wkt = str(definition or "").strip()
    if wkt.lower() == "undefined":
        wkt = ""
    if o == "EPSG" and code is not None:
        return LINE_CRS if code in (4326, 4269) else "EPSG:{}".format(code)
    if o and o != "NONE" and code is not None:
        return "{}:{}".format(o, code)
    if wkt:
        return wkt
    if org is None and org_id is None:            # no gpkg_spatial_ref_sys row: as before
        return LINE_CRS if s in (4326, 4269) else "EPSG:{}".format(s)
    return ""                                      # no usable definition: unknown


def _crs_of_ogr(sr):
    """The CRS text of an OGR SpatialReference: 'AUTHORITY:code' (lon/lat 4326 / 4269:
    LINE_CRS), else its WKT (an ESRI .prj without an authority), else LINE_CRS."""
    if sr is None:
        return LINE_CRS
    try:
        sr.AutoIdentifyEPSG()
    except Exception:                                         # noqa: BLE001
        pass
    name, code = sr.GetAuthorityName(None), sr.GetAuthorityCode(None)
    if name and code:
        if str(name).upper() == "EPSG" and str(code) in ("4326", "4269"):
            return LINE_CRS
        return "{}:{}".format(str(name).upper(), code)
    try:
        wkt = sr.ExportToWkt()
    except Exception:                                         # noqa: BLE001
        wkt = ""
    return wkt or LINE_CRS


def _parts_of(geom):
    """[[(x, y), ...]] of a GeoJSON-like line geometry."""
    if not geom:
        return []
    t = str(geom.get("type", "")).lower()
    c = geom.get("coordinates") or []
    if t == "linestring":
        return [[(float(p[0]), float(p[1])) for p in c]]
    if t == "multilinestring":
        return [[(float(p[0]), float(p[1])) for p in part] for part in c]
    if t == "geometrycollection":
        out = []
        for g in geom.get("geometries") or []:
            out += _parts_of(g)
        return out
    return []


class LineIndex(object):
    """The DWR dam lines of one file by structure id: parts(sid) -> [[(x, y)]] in self.crs
    (geometries parsed only when asked for)."""

    def __init__(self, path, label=SOURCE_PACK):
        self.path, self.label = path, label
        self.crs = LINE_CRS
        self.id_field = ""
        self._blobs = {}
        self._parsed = {}
        self.count = 0
        self.error = ""
        file_, _, lay = str(path).partition("|")
        lay = lay.split("=", 1)[1] if lay.startswith("layername=") else ""
        self.file = file_
        if file_.lower().endswith(".gpkg"):
            self._read_gpkg(file_, lay)
        else:
            self._read_ogr(file_, lay)

    def _read_gpkg(self, path, layer):
        con = _connect_ro(path)
        try:
            t = _gpkg_table(con, layer, True)
            if t is None:
                self.error = "no feature table"
                return
            table, gcol, _gt, srs = t
            try:
                row = con.execute("SELECT organization, organization_coordsys_id, definition "
                                  "FROM gpkg_spatial_ref_sys WHERE srs_id = ?",
                                  (srs,)).fetchone()
            except sqlite3.Error:
                row = None
            self.crs = _crs_of(srs, *(row or (None, None, None)))
            cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(
                table.replace('"', '""')))]
            idc = id_field_of(cols) or None
            if idc is None:
                self.error = "no structure id field"
                return
            self.id_field = idc
            for sid, blob in con.execute('SELECT "{}", "{}" FROM "{}"'.format(
                    idc.replace('"', '""'), gcol.replace('"', '""'), table.replace('"', '""'))):
                s = sid_key(sid)
                if s and blob is not None:
                    self._blobs.setdefault(s, []).append(blob)
                    self.count += 1
        finally:
            con.close()

    def _read_ogr(self, path, layer):
        try:
            from osgeo import ogr
        except ImportError:
            self.error = "GDAL is needed to read this file format"
            return
        ds = ogr.Open(path)
        if ds is None:
            self.error = "GDAL could not open it"
            return
        lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
        if lyr is None:
            return
        self.crs = _crs_of_ogr(lyr.GetSpatialRef())
        defn = lyr.GetLayerDefn()
        names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
        idc = id_field_of(names) or None
        if idc is None:
            self.error = "no structure id field"
            return
        self.id_field = idc
        for f in lyr:
            s = sid_key(f.GetField(idc))
            g = f.GetGeometryRef()
            if not s or g is None:
                continue
            parts = []
            if g.GetGeometryCount() > 0:
                for k in range(g.GetGeometryCount()):
                    sg = g.GetGeometryRef(k)
                    parts.append([(sg.GetX(i), sg.GetY(i)) for i in range(sg.GetPointCount())])
            else:
                parts.append([(g.GetX(i), g.GetY(i)) for i in range(g.GetPointCount())])
            self._parsed.setdefault(s, []).extend(parts)
            self.count += 1

    @classmethod
    def empty(cls, path, label, error):
        """An index with no lines (the file could not be read: `error`)."""
        idx = cls.__new__(cls)
        idx.path, idx.label, idx.crs, idx.id_field = path, label, LINE_CRS, ""
        idx._blobs, idx._parsed, idx.count = {}, {}, 0
        idx.file = str(path).split("|")[0]
        idx.error = error
        return idx

    def has(self, sid):
        return sid in self._blobs or sid in self._parsed

    def parts(self, sid):
        if sid in self._parsed:
            return self._parsed[sid]
        out = []
        if sid in self._blobs:
            from ..damsuite import suitegpkg as G
            for b in self._blobs[sid]:
                try:
                    geom, _srs = G.parse_blob(b)
                except Exception:                             # noqa: BLE001 - a bad blob
                    continue
                out += _parts_of(geom)
        self._parsed[sid] = out
        return out


_INDEX = {}
_LOCK = threading.Lock()


def line_index(path, label=SOURCE_PACK):
    """LineIndex of a file, cached by path and modification time (read once per batch)."""
    file_ = str(path).split("|")[0]
    try:
        stamp = os.path.getmtime(file_)
    except OSError:
        return None
    with _LOCK:
        hit = _INDEX.get(path)
        if hit is not None and hit[0] == stamp:
            hit[1].label = label
            return hit[1]
    try:
        idx = LineIndex(path, label)
    except Exception as e:                                    # noqa: BLE001 - damaged file
        idx = LineIndex.empty(path, label, "{}: {}".format(type(e).__name__, e))
    with _LOCK:
        _INDEX[path] = (stamp, idx)
    return idx


_NIDS = {}


def nid_lookup_for(path):
    """A function NID -> structure id ('' when not found) on a DWR dams file (GeoPackage)."""
    if not path:
        return None
    file_ = str(path).split("|")[0]
    if not file_.lower().endswith(".gpkg") or not os.path.isfile(file_):
        return None
    try:
        stamp = os.path.getmtime(file_)
    except OSError:
        return None
    hit = _NIDS.get(file_)
    if hit is None or hit[0] != stamp:
        table = {}
        try:
            con = _connect_ro(file_)
            try:
                t = _gpkg_table(con, "", False)
                if t is not None:
                    cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(
                        t[0].replace('"', '""')))]
                    idc = next((c for n in SID_FIELDS for c in cols if c.lower() == n), None)
                    nc = next((c for n in NID_FIELDS for c in cols if c.lower() == n), None)
                    if idc and nc:
                        for sid, nid in con.execute('SELECT "{}", "{}" FROM "{}"'.format(
                                idc.replace('"', '""'), nc.replace('"', '""'),
                            t[0].replace('"', '""'))):
                            if nid and sid_key(sid):
                                table.setdefault(str(nid).strip().upper(), sid_key(sid))
            finally:
                con.close()
        except sqlite3.Error:
            table = {}
        hit = (stamp, table)
        _NIDS[file_] = hit
    return lambda nid: hit[1].get(str(nid or "").strip().upper(), "")


def unknown_crs_text(path, crs):
    """The batch's one message for a DWR lines file whose CRS cannot be used (3.20.13 review:
    said once, not as a distance refusal for every dam)."""
    c = str(crs or "")
    c = c if len(c) <= 60 else c[:57] + "..."
    return "no DWR dam lines: {} is in a coordinate system that cannot be read ({})".format(
        os.path.basename(str(path or "").split("|")[0]) or "the DWR dam lines file",
        c or "none given")


# ======================================================================== geometry =======
def _dedupe(line):
    out = []
    for p in line:
        q = (float(p[0]), float(p[1]))
        if not (math.isfinite(q[0]) and math.isfinite(q[1])):
            continue
        if not out or math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) > 1e-6:
            out.append(q)
    return out


def length(line):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(line[:-1], line[1:]))


def midpoint(line):
    """The point at half the line's length."""
    half = 0.5 * length(line)
    acc = 0.0
    for a, b in zip(line[:-1], line[1:]):
        s = math.hypot(b[0] - a[0], b[1] - a[1])
        if acc + s >= half and s > 0:
            t = (half - acc) / s
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        acc += s
    return tuple(line[-1])


def nearest(line, xy):
    """The distance from xy to the polyline."""
    best = float("inf")
    if len(line) == 1:
        return math.hypot(xy[0] - line[0][0], xy[1] - line[0][1])
    for a, b in zip(line[:-1], line[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 <= 0 else max(0.0, min(1.0, ((xy[0] - a[0]) * dx + (xy[1] - a[1]) * dy)
                                             / L2))
        d = math.hypot(xy[0] - a[0] - t * dx, xy[1] - a[1] - t * dy)
        best = min(best, d)
    return best


def join_parts(parts, tol=JOIN_M):
    """Parts whose ends meet within `tol` joined into one line (a dam line digitized in
    pieces); the others as they are."""
    parts = [list(p) for p in parts if len(p) >= 2]
    changed = True
    while changed and len(parts) > 1:
        changed = False
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                a, b = parts[i], parts[j]
                d = lambda p, q: math.hypot(p[0] - q[0], p[1] - q[1])
                if d(a[-1], b[0]) <= tol:
                    new = a + b[1:]
                elif d(a[-1], b[-1]) <= tol:
                    new = a + b[::-1][1:]
                elif d(a[0], b[-1]) <= tol:
                    new = b + a[1:]
                elif d(a[0], b[0]) <= tol:
                    new = b[::-1] + a[1:]
                else:
                    continue
                parts[i] = new
                del parts[j]
                changed = True
                break
            if changed:
                break
    return parts


def choose(parts_work, inv_xy):
    """(line, info) of a dam's DWR line parts in the work CRS: parts that meet are joined; of
    the parts that pass the guard, the one nearest the inventory point is the line (the longer
    one on a tie; DWR-2665's two lines: the one 15 m from the point, which Nick kept, not the
    longer one 258 m away). info: length_m, nearest_m, centre_m, parts; line None with
    info["refused"] (the reason) when none passes."""
    clean = [_dedupe(p) for p in parts_work]
    clean = join_parts([p for p in clean if len(p) >= 2])
    info = {"parts": len(clean)}
    if not clean:
        info["refused"] = "the line is degenerate (fewer than 2 distinct vertices)"
        return None, info
    rows = []
    for p in clean:
        L = length(p)
        c = midpoint(p)
        rows.append((L, p, nearest(p, inv_xy), math.hypot(c[0] - inv_xy[0], c[1] - inv_xy[1])))
    rows.sort(key=lambda r: (round(r[2], 1), -r[0]))
    why = None
    for L, p, dn, dc in rows:
        r = _refusal(L, dn, dc)
        if r is None:
            info.update(length_m=round(L, 1), nearest_m=round(dn, 1), centre_m=round(dc, 1))
            return p, info
        if why is None:
            why = r
            info.update(length_m=round(L, 1), nearest_m=round(dn, 1), centre_m=round(dc, 1))
    info["refused"] = why
    return None, info


def guard(line, inv_xy):
    """(reason or None, info) of one line (in the work CRS) against the guard of choose():
    None when it may be used as this dam's line. The batch's CREST_LINE matched by structure
    id is held to the same guard (3.20.13)."""
    p = _dedupe(line)
    if len(p) < 2:
        return "the line is degenerate (fewer than 2 distinct vertices)", {}
    L, c = length(p), midpoint(p)
    dn, dc = nearest(p, inv_xy), math.hypot(c[0] - inv_xy[0], c[1] - inv_xy[1])
    info = {"length_m": round(L, 1), "nearest_m": round(dn, 1), "centre_m": round(dc, 1)}
    return _refusal(L, dn, dc), info


def _refusal(L, dn, dc):
    return ("the line is degenerate ({:.1f} m long)".format(L) if L < MIN_LEN_M else
            "the line is {:.0f} m from the inventory point (more than {:.0f} m)".format(
                dn, NEAR_M) if dn > NEAR_M else
            "the line's centre is {:.0f} m from the inventory point (more than {:.0f} m)"
            .format(dc, CENTRE_M) if dc > CENTRE_M else None)


def dam_point_check_env(environ=None):
    """The dam point check's environment override as the check reads it
    (dam_point_check.env_mode: NDBREACH_DAM_POINT_CHECK, else DAM_POINT_CHECK): 'off', 'warn' or
    'move', '' when unset or not understood (the setting then applies)."""
    from . import dam_point_check
    return dam_point_check.env_mode(os.environ if environ is None else environ) or ""


def rule_state(environ=None):
    """The chain's runner-stage fingerprint term (core/chain.stage_inputs): the placement rule
    version, the DWR line switch and the dam point check override. Cheap (reads no file)."""
    env = os.environ if environ is None else environ
    return {"placement": PLACEMENT_RULE, "dwr_lines": "off" if env_off(env) else "on",
            "dam_point_check_env": dam_point_check_env(env)}


def find(ids, index, to_work, inv_xy, label=None):
    """(line in the work CRS or None, info) of the structure ids of `ids` the index has, in
    order: the first whose line passes the guard; when none does, the first refusal. info:
    source, structure_id, matched_by, used, text (and refused when the line is not used); None
    when no id has a line."""
    first = None
    for sid, how in ids or ():
        if index is None or not index.has(sid):
            continue
        src = index.parts(sid)
        try:
            work = [list(map(tuple, to_work(p))) for p in src if len(p) >= 2]
        except Exception as e:                                # noqa: BLE001
            line, info = None, {"refused": "the line could not be converted to the model CRS "
                                           "({}: {})".format(type(e).__name__, e)}
        else:
            line, info = choose(work, inv_xy)
        info.update(source=label or index.label, structure_id=sid, matched_by=how)
        info["used"] = line is not None
        info["text"] = describe(info)
        if line is not None:
            return line, info
        if first is None:
            first = info
    return None, first


def find_in_lines(lines_attrs, id_field, ids, inv_xy, label=SOURCE_LAYER):
    """The same for lines already in the work CRS [(line, {field: value})] (a DAM_LINES layer
    read round the dam): matched by structure id when the layer has an id field, else the
    line nearest the point (within NEAR_M)."""
    if not lines_attrs:
        return None, None
    if id_field:
        first = None
        for sid, how in ids or ():
            hit = [ln for ln, at in lines_attrs if sid_key(at.get(id_field)) == sid]
            if hit:
                line, info = choose([[tuple(map(float, q)) for q in h] for h in hit], inv_xy)
                info.update(source=label, structure_id=sid, matched_by=how,
                            used=line is not None)
                info["text"] = describe(info)
                if line is not None:
                    return line, info
                first = first or info
        return None, first
    best = min(lines_attrs, key=lambda la: nearest([tuple(q) for q in la[0]], inv_xy))
    line, info = choose([[tuple(map(float, q)) for q in best[0]]], inv_xy)
    info.update(source=label, structure_id="", matched_by="the nearest line (the layer has "
                "no structure id field)", used=line is not None)
    info["text"] = describe(info)
    return line, info


def describe(info):
    """'DWR dam line from the pack, structure 2645 (461 m, centre 266 m from the inventory
    point)' or 'DWR dam line from the pack, structure 2659, not used: ...'."""
    sid = info.get("structure_id")
    head = "{} from {}{}".format(info.get("kind") or "DWR dam line",
                                 info.get("source") or SOURCE_PACK,
                                 ", structure {}".format(sid) if sid else "")
    if info.get("refused"):
        return "{}, not used: {}".format(head, info["refused"])
    return "{} ({:.0f} m long, centre {:.0f} m from the inventory point)".format(
        head, info.get("length_m") or 0.0, info.get("centre_m") or 0.0)


def batch_text(info):
    """The batch CSV's Dam_Line column."""
    if not info:
        return ""
    sid = info.get("structure_id")
    src = info.get("source") or SOURCE_PACK
    if info.get("refused"):
        return "not used ({}{}): {}".format(src, ", {}".format(sid) if sid else "",
                                            info["refused"])
    if info.get("used") is False and info.get("why"):
        return info["why"]
    return "DWR line ({}{}), {:.0f} m, centre {:.0f} m from the point".format(
        src, ", structure {}".format(sid) if sid else "", info.get("length_m") or 0.0,
        info.get("centre_m") or 0.0)


def apply(dam, xy, line, info):
    """The line as the dam line (exactly as DAM_LINES in the single-dam tool): the point as
    given kept as dam._point_original, dam.crest_line_xy = the line, the source recorded.
    Returns the line's length midpoint (place_midcrest then puts the point at mid-crest)."""
    dam._point_original = (float(xy[0]), float(xy[1]))
    dam.crest_line_xy = [(float(p[0]), float(p[1])) for p in line]
    dam._crest_line_source = info.get("text") or describe(info)
    dam._dam_line_info = dict(info)
    return midpoint(dam.crest_line_xy)


def note_only(dam, info):
    """A DWR line that was found but not used (or switched off): recorded for the report."""
    if info:
        dam._dam_line_info = dict(info)


def warning_text(info):
    """The run warning for a refused line."""
    return "{}. The dam point is used as given (the dam point check may still move it).".format(
        describe(info))


def not_used(dam, why):
    """Record why a dam has no DWR line (the Dam_Line column): switched off, a user crest line,
    no structure id, no line for it, no source."""
    info = {"used": False, "why": why}
    dam._dam_line_info = info
    return info


def resolve(dam, xy, ids, index=None, to_work=None, layer=None, source_why=""):
    """A batch dam's DWR line: from `layer` ((lines in the work CRS with attributes, id field)
    of a DAM_LINES layer read round the dam) first, else from `index` (a LineIndex, its parts
    converted with to_work). Returns (xy, info, warning): xy is the line's length midpoint when
    the line is used (dam.crest_line_xy is set, see apply), else the point as given; warning is
    the run warning of a refused line, else None. Never raises."""
    try:
        line = info = None
        if layer is not None:
            line, info = find_in_lines(layer[0], layer[1], ids, xy)
        if info is None and index is not None and to_work is not None:
            line, info = find(ids, index, to_work, xy)
    except Exception as e:                                    # noqa: BLE001 - never stop a run
        info = {"used": False, "why": "DWR dam line not read ({}: {})".format(
            type(e).__name__, e)}
        dam._dam_line_info = info
        return xy, info, None
    if info is None:
        why = ("no structure id (dam ID, structure_id field or dam.json)" if not ids else
               "no DWR line for structure {}".format("/".join(s for s, _h in ids))
               if (index is not None or layer is not None) else
               source_why or "no DWR dam lines")
        return xy, not_used(dam, why), None
    if line is None:
        note_only(dam, info)
        return xy, info, warning_text(info)
    return apply(dam, xy, line, info), info, None
