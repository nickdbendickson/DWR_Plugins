# -*- coding: utf-8 -*-
"""2.18.66: the ND DWR export files (dams, dam lines, unknown structures, dugouts, dugout
polygons) as offline-pack data sets - build_nd_pack.py --dwr-folder PATH.

The files are the GeoPackages ND DWR hands out (DWR_Dams.gpkg with its two layers,
DWR_Dam_Lines.gpkg, DWR_Unknown_Structures.gpkg, DWR_Dugouts.gpkg, DWR_Dugouts_Polygons.gpkg;
any names the suite folder's auto-fill takes). The layer of each data set is picked by the same
rules as the suite folder (damsuite.suite_fill.scan). Every feature keeps all its attributes; its
geometry is reprojected to longitude / latitude (the pack's CRS) here, in pure Python:

  EPSG:4326 / 4269             longitude / latitude (as they are)
  EPSG:2265 / 2266             NAD83 / North Dakota North / South (ft) - Lambert conformal conic
  EPSG:32120 / 32121           the same in metres
  EPSG:26913 / 26914           NAD83 / UTM zone 13N / 14N (also 6342 / 6343 / 6344, 32613 / 32614)
  EPSG:5070                    NAD83 / Conus Albers

The linear unit is the one in the file's own CRS definition (gpkg_spatial_ref_sys). EPSG:2265 /
2266 are in international feet (UNIT["foot",0.3048], false easting 1968503.937 ft = 600 km - North
Dakota's state plane is not in US survey feet); a file whose definition says US survey feet
(1200 / 3937 m) is read in those. With no unit in the file the EPSG default is used. NAD83 is taken
as WGS 84 (about 1 m in North Dakota, as everywhere in the pack).
A feature whose geometry is empty (NaN) is left out by the pack writer and counted in its note.
Another CRS is an error naming it (convert the file to one of these in QGIS first).

No QGIS, no GDAL: sqlite3, struct, math (the builder runs outside QGIS).
"""
import datetime
import math
import os
import re
import sqlite3

if __package__:
    from .damsuite import suite_fill as SF, suitegpkg as G
else:                                     # build_nd_pack.py run as a script: the plugin folder
    from damsuite import suite_fill as SF, suitegpkg as G     # noqa: E402

GROUP = "dwr"
# pack data set, suite_fill slot, label, what it is (the manifest's note)
DATASETS = (
    ("DWR_dams", "dwr", "ND DWR dams inventory", "ND State Water Commission / DWR dam inventory"),
    ("DWR_dam_lines", "dam_lines", "ND DWR dam lines", "ND DWR dam lines (linear features)"),
    ("DWR_unknown_structures", "unknown", "ND DWR unknown structures",
     "ND DWR structures with no dam record"),
    ("DWR_dugouts", "dugouts", "ND DWR dugouts", "ND DWR dugout points"),
    ("DWR_dugout_polygons", "dugout_polygons", "ND DWR dugout polygons", "ND DWR dugout polygons"),
)
US_FT = 1200.0 / 3937.0
_A, _INVF = G.GRS80_A, G.GRS80_INVF


class DwrFileError(Exception):
    pass


# ------------------------------------------------------------ projections --
class _LCC(object):
    """Lambert conformal conic, two standard parallels, GRS 80 (Snyder 1987, eqs. 15-1 to 15-11);
    x / y in metres (false easting / northing in metres)."""

    def __init__(self, lat1, lat2, lat0, lon0, x0, y0=0.0):
        f = 1.0 / _INVF
        self.e2 = f * (2.0 - f)
        self.e = math.sqrt(self.e2)
        self.lon0, self.x0, self.y0 = math.radians(lon0), x0, y0
        p1, p2, p0 = (math.radians(v) for v in (lat1, lat2, lat0))
        m1, m2 = self._m(p1), self._m(p2)
        t1, t2, t0 = self._t(p1), self._t(p2), self._t(p0)
        self.n = (math.log(m1) - math.log(m2)) / (math.log(t1) - math.log(t2))
        self.F = m1 / (self.n * t1 ** self.n)
        self.rho0 = _A * self.F * t0 ** self.n

    def _m(self, phi):
        s = math.sin(phi)
        return math.cos(phi) / math.sqrt(1.0 - self.e2 * s * s)

    def _t(self, phi):
        s = math.sin(phi)
        return math.tan(math.pi / 4.0 - phi / 2.0) / ((1.0 - self.e * s) / (1.0 + self.e * s)) ** (
            self.e / 2.0)

    def forward(self, lon, lat):
        rho = _A * self.F * self._t(math.radians(lat)) ** self.n
        th = self.n * (math.radians(lon) - self.lon0)
        return self.x0 + rho * math.sin(th), self.y0 + self.rho0 - rho * math.cos(th)

    def inverse(self, x, y):
        dx, dy = x - self.x0, self.rho0 - (y - self.y0)
        rho = math.copysign(math.hypot(dx, dy), self.n)
        th = math.atan2(dx, dy) if self.n > 0 else math.atan2(-dx, -dy)
        t = (rho / (_A * self.F)) ** (1.0 / self.n)
        phi = math.pi / 2.0 - 2.0 * math.atan(t)
        for _i in range(15):
            s = math.sin(phi)
            nxt = math.pi / 2.0 - 2.0 * math.atan(t * ((1.0 - self.e * s) / (1.0 + self.e * s)) ** (
                self.e / 2.0))
            if abs(nxt - phi) < 1e-13:
                phi = nxt
                break
            phi = nxt
        return math.degrees(th / self.n + self.lon0), math.degrees(phi)


class _UTM(object):
    """Transverse Mercator (UTM north), GRS 80 (Snyder 1987, eqs. 8-12 to 8-25)."""

    def __init__(self, zone):
        f = 1.0 / _INVF
        self.e2 = f * (2.0 - f)
        self.lon0 = math.radians(-183.0 + 6.0 * zone)
        self.k0, self.x0 = 0.9996, 500000.0

    def forward(self, lon, lat):
        e2, k0 = self.e2, self.k0
        ep2 = e2 / (1.0 - e2)
        phi = math.radians(lat)
        s, c = math.sin(phi), math.cos(phi)
        N = _A / math.sqrt(1.0 - e2 * s * s)
        T, C = math.tan(phi) ** 2, ep2 * c * c
        A_ = (math.radians(lon) - self.lon0) * c
        M = _A * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * phi
                  - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * phi)
                  + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * phi)
                  - (35 * e2 ** 3 / 3072) * math.sin(6 * phi))
        x = self.x0 + k0 * N * (A_ + (1 - T + C) * A_ ** 3 / 6
                                + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A_ ** 5 / 120)
        y = k0 * (M + N * math.tan(phi) * (A_ * A_ / 2 + (5 - T + 9 * C + 4 * C * C) * A_ ** 4 / 24
                                           + (61 - 58 * T + T * T + 600 * C - 330 * ep2)
                                           * A_ ** 6 / 720))
        return x, y

    def inverse(self, x, y):
        e2, k0 = self.e2, self.k0
        ep2 = e2 / (1.0 - e2)
        M = y / k0
        mu = M / (_A * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
        e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
        p1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
              + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
              + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
        s, c = math.sin(p1), math.cos(p1)
        T, C = math.tan(p1) ** 2, ep2 * c * c
        N1 = _A / math.sqrt(1 - e2 * s * s)
        R1 = _A * (1 - e2) / (1 - e2 * s * s) ** 1.5
        D = (x - self.x0) / (N1 * k0)
        lat = p1 - (N1 * math.tan(p1) / R1) * (
            D * D / 2 - (5 + 3 * T + 10 * C - 4 * C * C - 9 * ep2) * D ** 4 / 24
            + (61 + 90 * T + 298 * C + 45 * T * T - 252 * ep2 - 3 * C * C) * D ** 6 / 720)
        lon = self.lon0 + (D - (1 + 2 * T + C) * D ** 3 / 6
                           + (5 - 2 * C + 28 * T - 3 * C * C + 8 * ep2 + 24 * T * T) * D ** 5 / 120) / c
        return math.degrees(lon), math.degrees(lat)


_ND_NORTH = (47.0 + 26.0 / 60, 48.0 + 44.0 / 60, 47.0, -100.5, 600000.0)
_ND_SOUTH = (46.0 + 11.0 / 60, 47.0 + 29.0 / 60, 45.0 + 40.0 / 60, -100.5, 600000.0)
# EPSG code -> (projection or None for longitude / latitude, linear unit in metres)
_KNOWN = {
    4326: (None, 1.0), 4269: (None, 1.0), 4152: (None, 1.0), 6318: (None, 1.0),
    2265: (_LCC(*_ND_NORTH), 0.3048), 2266: (_LCC(*_ND_SOUTH), 0.3048),
    32120: (_LCC(*_ND_NORTH), 1.0), 32121: (_LCC(*_ND_SOUTH), 1.0),
    26913: (_UTM(13), 1.0), 26914: (_UTM(14), 1.0), 6342: (_UTM(13), 1.0), 6343: (_UTM(14), 1.0),
    32613: (_UTM(13), 1.0), 32614: (_UTM(14), 1.0),
    5070: (G.ALBERS_5070, 1.0),
}


def _wkt_epsg_and_unit(con, srs_id):
    """(EPSG code, linear unit in metres or None) from the file's gpkg_spatial_ref_sys row."""
    try:
        row = con.execute("SELECT organization, organization_coordsys_id, definition FROM "
                          "gpkg_spatial_ref_sys WHERE srs_id=?", (srs_id,)).fetchone()
    except sqlite3.Error:
        row = None
    if not row:
        return srs_id, None
    org, code, wkt = row
    wkt = wkt or ""
    m = re.findall(r'AUTHORITY\["EPSG",\s*"?(\d+)"?\]\]\s*$', wkt.strip())
    epsg = int(m[-1]) if m else (int(code) if str(org or "").upper() == "EPSG" and code else srs_id)
    unit = None
    if wkt.startswith(("PROJCS", "PROJCRS")):
        # the projected CRS's own UNIT is the last top-level one (after the PROJECTION / PARAMETERs)
        u = re.findall(r'(?:UNIT|LENGTHUNIT)\["[^"]*",\s*([0-9.eE+-]+)', wkt)
        if u:
            try:
                unit = float(u[-1])
            except ValueError:
                unit = None
    return epsg, unit


def to_lonlat(con, srs_id):
    """(EPSG code, unit in metres, fn(x, y) -> (lon, lat)) for the layer's CRS; raises
    DwrFileError for another CRS."""
    epsg, unit = _wkt_epsg_and_unit(con, srs_id)
    if epsg not in _KNOWN:
        raise DwrFileError("CRS EPSG:{} is not one this builder reprojects ({}) - save the layer "
                           "in one of those (QGIS: Export > Save Features As)".format(
                               epsg, ", ".join("EPSG:{}".format(k) for k in sorted(_KNOWN))))
    proj, k = _KNOWN[epsg]
    if proj is None:
        return epsg, 1.0, (lambda x, y: (x, y))
    if unit is not None and unit > 0:
        k = unit                                  # the file's own unit wins
    return epsg, k, (lambda x, y: proj.inverse(x * k, y * k))


def _finite_geom(g):
    def ok(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return all(math.isfinite(v) for v in c[:2])
        return all(ok(p) for p in c) if isinstance(c, (list, tuple)) else True
    if g is None:
        return False
    if g.get("type") == "GeometryCollection":
        return all(_finite_geom(p) for p in g.get("geometries") or [])
    return ok(g.get("coordinates"))


def _project(c, fn, nd=7):
    if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
        lon, lat = fn(float(c[0]), float(c[1]))
        return [round(lon, nd), round(lat, nd)]
    return [_project(p, fn, nd) for p in c]


def _value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None                           # a BLOB attribute is not kept (JSON can not hold it)
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def find_layers(folder):
    """{pack data set: (file, layer)} of the DWR files in `folder` (the suite-folder rules)."""
    if not folder or not os.path.isdir(folder):
        raise DwrFileError("DWR folder not found: {}".format(folder))
    found = SF.scan(folder)[0]
    out = {}
    for name, slot, _label, _what in DATASETS:
        uri = found.get(slot)
        if uri and "|layername=" in uri:
            path, layer = uri.split("|layername=", 1)
            out[name] = (path, layer)
    return out


def file_date(path):
    return datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def source_date(path, layer=None):
    """(date, how) of a GeoPackage's own content - Hazard 2.20.2 / Data 1.5.1: the file's
    modified time is when it was COPIED to this computer, so the pack notes said "file dated" the
    copy day. The date is gpkg_contents.last_change of `layer` (or the newest of the file),
    how = "GeoPackage last change"; a file without it (not a GeoPackage, empty column): the
    modified time, how = "file modified"."""
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(
            path.replace("?", "%3f").replace("#", "%23")), uri=True)
        try:
            if layer:
                row = con.execute("SELECT last_change FROM gpkg_contents WHERE table_name=?",
                                  (layer,)).fetchone()
            else:
                row = con.execute("SELECT MAX(last_change) FROM gpkg_contents").fetchone()
        finally:
            con.close()
        txt = str(row[0] or "") if row else ""
        if re.match(r"^\d{4}-\d{2}-\d{2}", txt):
            return txt[:10], "GeoPackage last change"
    except sqlite3.Error:
        pass
    return file_date(path), "file modified"


def read_layer(path, layer):
    """(features as GeoJSON dicts in lon / lat with every attribute, info) of one layer.
    info: {"epsg", "unit_m", "rows", "empty"} - empty: rows with no / an empty geometry (the pack writer
    leaves those out and says so)."""
    con = sqlite3.connect("file:{}?mode=ro".format(path.replace("?", "%3f").replace("#", "%23")),
                          uri=True)
    try:
        row = con.execute("SELECT column_name, srs_id FROM gpkg_geometry_columns WHERE "
                          "table_name=?", (layer,)).fetchone()
        if not row:
            raise DwrFileError("{}: no layer {} with a geometry".format(path, layer))
        gcol, srs = row
        epsg, unit, fn = to_lonlat(con, srs)
        cols = [r[1] for r in con.execute('PRAGMA table_info("{}")'.format(layer.replace('"', '""')))]
        feats, empty = [], 0
        q = 'SELECT {} FROM "{}"'.format(", ".join('"{}"'.format(c.replace('"', '""')) for c in cols),
                                         layer.replace('"', '""'))
        for r in con.execute(q):
            d = dict(zip(cols, r))
            blob = d.pop(gcol)
            props = {k: _value(v) for k, v in d.items()}
            g = None
            if blob is not None:
                try:
                    g = G.parse_blob(blob, lists=True)[0]
                except (G.GpkgError, ValueError, IndexError, Exception):     # noqa
                    g = None
            if g is not None and _finite_geom(g):
                if g.get("type") == "GeometryCollection":
                    g = {"type": g["type"], "geometries": [
                        {"type": p["type"], "coordinates": _project(p["coordinates"], fn)}
                        for p in g["geometries"]]}
                else:
                    g = {"type": g["type"], "coordinates": _project(g["coordinates"], fn)}
            else:
                g = None
                empty += 1
            feats.append({"type": "Feature", "geometry": g, "properties": props})
        return feats, {"epsg": epsg, "unit_m": unit, "rows": len(feats), "empty": empty}
    finally:
        con.close()


def bbox_of(g):
    """(min lon, min lat, max lon, max lat) of a GeoJSON geometry, or None."""
    xs, ys = [], []

    def walk(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        elif isinstance(c, (list, tuple)):
            for p in c:
                walk(p)
    if not g:
        return None
    if g.get("type") == "GeometryCollection":
        for p in g.get("geometries") or ():
            walk(p.get("coordinates"))
    else:
        walk(g.get("coordinates"))
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def uid_of(f):
    """The file's fid: one pack row per row of the file, as it is (structure_id is not unique -
    DWR_Dugouts_Polygons has 1897 rows for 1748 ids)."""
    p = f.get("properties") or {}
    for k in ("fid", "FID", "OBJECTID", "objectid"):
        if p.get(k) not in (None, ""):
            return "{}:{}".format(k, p[k])
    return None
