# -*- coding: utf-8 -*-
"""Hazard 2.18.66: the ND DWR GeoPackages in the offline pack (build_nd_pack --dwr-folder).

Tiny synthetic DWR files with ND DWR's own export names (DWR_Dams.gpkg with its two layers,
DWR_Dam_Lines.gpkg, DWR_Unknown_Structures.gpkg, DWR_Dugouts.gpkg, DWR_Dugouts_Polygons.gpkg) in
EPSG:2266 - whose definition says international feet (UNIT["foot",0.3048]), as DWR's files do -
and one in EPSG:26914 metres, written with sqlite3 in a temp folder. Checks:

  * without dwr_folder the data-set list is the same as before (no DWR_*, no "local_gpkg");
  * the five DWR_* data sets come from the files the suite folder's rules pick (DWR_Dams, not the
    duplicate "dams" layer), every attribute kept, geometry in lon / lat to < 1 mm, a row with no
    geometry counted and named in the note, the source file and date in the note;
  * a CRS the builder cannot reproject is a clear error; no file -> a clear error;
  * the pack converted to format 2: the plugins' fill (suite_fill.layers_for_settings / scan) and
    suitedata.dwr_layers read the DWR layers from the pack when the suite folder has none, and the
    engineer's own file always wins.

Nothing outside the temp folder is read or written; no network.
    python3 DamSafety_Hazard/tests/test_dwr_pack_files.py
"""
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
for k in ("NDSUITE_SETTINGS", "NDSUITE_PACK", "NDSUITE_DATA", "NDSUITE_SOURCE", "NDSUITE_DAMS"):
    os.environ.pop(k, None)

from DamSafety_Hazard import build_nd_pack as B             # noqa: E402
from DamSafety_Hazard import dwr_files as DF                # noqa: E402
from DamSafety_Hazard.damsuite import gpkgpack as GP        # noqa: E402
from DamSafety_Hazard.damsuite import suitegpkg as G        # noqa: E402
from DamSafety_Hazard.damsuite import suite_fill as SF      # noqa: E402
from DamSafety_Hazard.damsuite import suitedata as SD       # noqa: E402

WKT_2266 = (
    'PROJCS["NAD83 / North Dakota South (ft)",GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
    'SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","6269"]],'
    'PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
    'AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4269"]],PROJECTION["Lambert_Conformal_Conic_2SP"],'
    'PARAMETER["latitude_of_origin",45.6666666666667],PARAMETER["central_meridian",-100.5],'
    'PARAMETER["standard_parallel_1",47.4833333333333],PARAMETER["standard_parallel_2",46.1833333333333],'
    'PARAMETER["false_easting",1968503.937],PARAMETER["false_northing",0],'
    'UNIT["foot",0.3048,AUTHORITY["EPSG","9002"]],AXIS["Easting",EAST],AXIS["Northing",NORTH],'
    'AUTHORITY["EPSG","2266"]]')
WKT_26914 = ('PROJCS["NAD83 / UTM zone 14N",GEOGCS["NAD83",DATUM["North_American_Datum_1983",'
             'SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],'
             'UNIT["degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
             'PARAMETER["latitude_of_origin",0],PARAMETER["central_meridian",-99],'
             'PARAMETER["scale_factor",0.9996],PARAMETER["false_easting",500000],'
             'PARAMETER["false_northing",0],UNIT["metre",1],AUTHORITY["EPSG","26914"]]')
WKT_3857 = 'PROJCS["WGS 84 / Pseudo-Mercator",UNIT["metre",1],AUTHORITY["EPSG","3857"]]'
SRS = {2266: ("NAD83 / North Dakota South (ft)", WKT_2266), 26914: ("NAD83 / UTM zone 14N", WKT_26914),
       3857: ("WGS 84 / Pseudo-Mercator", WKT_3857)}

# ND DWR's LCC South (EPSG:2266 / 32121) forward, metres - only to place the test points
_A, _F = 6378137.0, 1 / 298.257222101
_E2 = _F * (2 - _F)
_E = math.sqrt(_E2)


def _t(p):
    s = math.sin(p)
    return math.tan(math.pi / 4 - p / 2) / ((1 - _E * s) / (1 + _E * s)) ** (_E / 2)


def _m(p):
    s = math.sin(p)
    return math.cos(p) / math.sqrt(1 - _E2 * s * s)


_P1, _P2, _P0, _L0 = map(math.radians, (47 + 29 / 60.0, 46 + 11 / 60.0, 45 + 40 / 60.0, -100.5))
_N = (math.log(_m(_P1)) - math.log(_m(_P2))) / (math.log(_t(_P1)) - math.log(_t(_P2)))
_FF = _m(_P1) / (_N * _t(_P1) ** _N)
_R0 = _A * _FF * _t(_P0) ** _N


def lcc_ft(lon, lat):
    r = _A * _FF * _t(math.radians(lat)) ** _N
    th = _N * (math.radians(lon) - _L0)
    return (600000.0 + r * math.sin(th)) / 0.3048, (_R0 - r * math.cos(th)) / 0.3048


def write_gpkg(path, layers):
    """layers: {name: (geometry type, srs_id, [(field, sqlite type)], [(geom or None, {field: v})])}"""
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
  organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL,
  description TEXT);
CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
  identifier TEXT, description TEXT DEFAULT '', last_change DATETIME, min_x DOUBLE, min_y DOUBLE,
  max_x DOUBLE, max_y DOUBLE, srs_id INTEGER);
CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL,
  geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL);
""")
    con.execute("PRAGMA application_id=1196444487")
    for name, (gtype, srs, fields, rows) in layers.items():
        con.execute("INSERT OR IGNORE INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
                    (SRS[srs][0], srs, "EPSG", srs, SRS[srs][1], ""))
        con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) VALUES "
                    "(?,?,?,?)", (name, "features", name, srs))
        con.execute("INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,0,0)", (name, "geom", gtype, srs))
        cols = ", ".join('"{}" {}'.format(f, t) for f, t in fields)
        con.execute('CREATE TABLE "{}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom {}, {})'.format(
            name, gtype, cols))
        for g, props in rows:
            names = ["geom"] + [f for f, _ in fields]
            vals = [G.geometry_blob(g, srs) if g else None] + [props.get(f) for f, _ in fields]
            con.execute('INSERT INTO "{}" ({}) VALUES ({})'.format(
                name, ", ".join('"{}"'.format(n) for n in names), ", ".join("?" * len(names))), vals)
    con.commit()
    con.close()


def pt(lon, lat):
    return {"type": "Point", "coordinates": list(lcc_ft(lon, lat))}


def square(lon, lat, d=0.0005):
    ring = [list(lcc_ft(lon + a, lat + b)) for a, b in ((-d, -d), (d, -d), (d, d), (-d, d), (-d, -d))]
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def dwr_folder(d):
    os.makedirs(d)
    DAMF = [("structure_id", "INTEGER"), ("dam_name", "TEXT"), ("hazard_class", "TEXT"),
            ("national_id", "TEXT"), ("longitude", "REAL"), ("latitude", "REAL"), ("owner_note", "TEXT")]
    dams = [(pt(-100.2, 46.9), {"structure_id": 11, "dam_name": "Alpha Dam", "hazard_class": "Low",
                                "national_id": "ND00011", "longitude": -100.2, "latitude": 46.9,
                                "owner_note": "kept as is"}),
            (pt(-100.3, 46.8), {"structure_id": 12, "dam_name": "Beta Dam", "hazard_class": "Undetermined"}),
            (None, {"structure_id": 13, "dam_name": "No Geometry Dam"})]
    write_gpkg(os.path.join(d, "DWR_Dams.gpkg"), {
        "dams": ("POINT", 26914, DAMF, []),                       # DWR's duplicate layer (UTM)
        "DWR_Dams": ("POINT", 2266, DAMF, dams)})
    write_gpkg(os.path.join(d, "DWR_Dam_Lines.gpkg"), {"dams_linear_features": (
        "MULTILINESTRING", 2266, [("feature_id", "INTEGER"), ("structure_id", "INTEGER")],
        [({"type": "MultiLineString", "coordinates": [[list(lcc_ft(-100.2, 46.9)),
                                                       list(lcc_ft(-100.201, 46.9))]]},
          {"feature_id": 1, "structure_id": 11})])})
    write_gpkg(os.path.join(d, "DWR_Unknown_Structures.gpkg"), {"DWR_Unknown_Structures": (
        "POINT", 2266, [("structure_id", "INTEGER"), ("dam_name", "TEXT")],
        [(pt(-100.4, 46.7), {"structure_id": 21, "dam_name": "Gamma"})])})
    write_gpkg(os.path.join(d, "DWR_Dugouts.gpkg"), {"DWR_Dugouts": (
        "POINT", 2266, [("structure_id", "INTEGER"), ("longitude", "REAL"), ("latitude", "REAL"),
                        ("storage_above_grade_acft", "REAL"), ("structure_status", "TEXT")],
        [(pt(-100.25, 46.85), {"structure_id": 501, "longitude": -100.25, "latitude": 46.85,
                               "storage_above_grade_acft": 0.0, "structure_status": "Intact"}),
         (pt(-100.26, 46.86), {"structure_id": 502, "longitude": -100.26, "latitude": 46.86,
                               "storage_above_grade_acft": 2.5, "structure_status": "Intact"})])})
    write_gpkg(os.path.join(d, "DWR_Dugouts_Polygons.gpkg"), {"DWR_Dugouts_Polygons": (
        "MULTIPOLYGON", 2266, [("feature_id", "INTEGER"), ("structure_id", "INTEGER")],
        [(square(-100.25, 46.85), {"feature_id": 1, "structure_id": 501}),
         (square(-100.2501, 46.8501), {"feature_id": 2, "structure_id": 501}),     # 2 rows, 1 id
         (square(-83.0, 5.0), {"feature_id": 3, "structure_id": 777})])})           # far outside ND


work = tempfile.mkdtemp(prefix="dwr_pack_test_")
os.environ["HOME"] = work                     # no ~/.ndsuite/config.json of the box
SD.CONFIG = os.path.join(work, ".ndsuite", "config.json")
try:
    # ---- 1. without --dwr-folder nothing changes
    base = B.datasets()
    assert not any(t[0].startswith("DWR_") or t[3] == "local_gpkg" for t in base), base
    assert B.datasets(None) == base

    src = os.path.join(work, "DWR files")
    dwr_folder(src)
    table = B.datasets(src)
    assert table[:len(base)] == base
    extra = table[len(base):]
    names = [t[0] for t in extra]
    assert names == ["DWR_dams", "DWR_dam_lines", "DWR_unknown_structures", "DWR_dugouts",
                     "DWR_dugout_polygons"], names
    assert all(t[2] == "dwr" and t[3] == "local_gpkg" for t in extra), extra
    spec = {t[0]: t[4] for t in extra}
    assert spec["DWR_dams"]["layer"] == "DWR_Dams", spec["DWR_dams"]                # not "dams"
    assert spec["DWR_dam_lines"]["layer"] == "dams_linear_features"
    assert spec["DWR_dugout_polygons"]["layer"] == "DWR_Dugouts_Polygons"
    try:
        B.datasets(os.path.join(work, "nowhere"))
        raise AssertionError("no error for a folder with no DWR file")
    except B.BuildError as e:
        assert "DWR" in str(e), e
    print("OK (no option = same list; five DWR_* data sets from the suite folder rules)")

    # ---- 2. reading: CRS from the file (international feet), all attributes, < 1 mm
    feats, info = DF.read_layer(os.path.join(src, "DWR_Dams.gpkg"), "DWR_Dams")
    assert info["epsg"] == 2266 and abs(info["unit_m"] - 0.3048) < 1e-12, info
    assert info["rows"] == 3 and info["empty"] == 1, info
    a = feats[0]
    lon, lat = a["geometry"]["coordinates"][:2]
    err = math.hypot((lon + 100.2) * 111320 * math.cos(math.radians(46.9)), (lat - 46.9) * 110540)
    assert err < 0.001, (lon, lat, err)
    assert a["properties"]["owner_note"] == "kept as is" and a["properties"]["national_id"] == "ND00011"
    bad = os.path.join(work, "bad")
    os.makedirs(bad)
    write_gpkg(os.path.join(bad, "DWR_Dugouts.gpkg"), {"DWR_Dugouts": (
        "POINT", 3857, [("structure_id", "INTEGER"), ("longitude", "REAL")],
        [({"type": "Point", "coordinates": [0.0, 0.0]}, {"structure_id": 1})])})
    try:
        DF.read_layer(os.path.join(bad, "DWR_Dugouts.gpkg"), "DWR_Dugouts")
        raise AssertionError("no error for EPSG:3857")
    except DF.DwrFileError as e:
        assert "EPSG:3857" in str(e) and "EPSG:2266" in str(e), e
    print("OK (EPSG:2266 international feet from the file, attributes kept, unknown CRS named)")

    # ---- 3. the build (dwr group only - no network), then format 2
    out = os.path.join(work, "packs", "nd_offline_pack_dwr.zip")
    os.makedirs(os.path.dirname(out))
    logs = []
    B.build(out, only="dwr", dwr_folder=src, log=logs.append, workers=1)
    assert os.path.isfile(out), logs
    pack = os.path.join(work, "packs", "ND_Dam_Suite_Pack")
    m = GP.convert_v1(out, pack, only=names)
    want = {"DWR_dams": 2, "DWR_dam_lines": 1, "DWR_unknown_structures": 1, "DWR_dugouts": 2,
            "DWR_dugout_polygons": 3}
    for n, c in want.items():
        e = m["datasets"][n]
        assert e["count"] == c and e["status"] == "complete", (n, e)
        assert e["file"] == "dwr/{}.gpkg".format(n), e
        assert "file dated" in e["note"] and "EPSG:2266" in e["note"], e["note"]
        assert os.path.basename(spec[n]["path"]) in e["note"], e["note"]
    assert "3 rows)" in m["datasets"]["DWR_dams"]["note"] and "without geometry" in m["datasets"]["DWR_dams"]["note"], m["datasets"]["DWR_dams"]
    assert "outside" not in m["datasets"]["DWR_dugouts"]["note"]
    assert "1 wholly outside North Dakota (kept)" in m["datasets"]["DWR_dugout_polygons"]["note"], \
        m["datasets"]["DWR_dugout_polygons"]["note"]
    rows = G.read_features(os.path.join(pack, "dwr", "DWR_dugouts.gpkg"), "DWR_dugouts")
    assert sorted(p["structure_id"] for _, p in rows) == [501, 502]
    assert {p["storage_above_grade_acft"] for _, p in rows} == {0.0, 2.5}
    assert all(abs(g["coordinates"][0] + 100.25) < 0.02 for g, _ in rows), rows
    print("OK (pack built: DWR_* counts, notes with file and date, format 2 conversion)")

    # ---- 4. the plugins read the DWR layers from the pack; the engineer's own file wins
    suite = os.path.join(work, "suite")
    os.makedirs(suite)
    got = SF.layers_for_settings({"suite_folder": suite, "pack": pack})
    for slot, n in (("dwr", "DWR_dams"), ("dam_lines", "DWR_dam_lines"), ("unknown", "DWR_unknown_structures"),
                    ("dugouts", "DWR_dugouts"), ("dugout_polygons", "DWR_dugout_polygons")):
        assert got.get(slot) == "{}|layername={}".format(os.path.join(pack, "dwr", n + ".gpkg"), n), (slot, got)
    os.environ["NDSUITE_SOURCE"] = "download"         # Download mode: no pack fallback
    SF._SCAN.clear()
    got_dl = SF.layers_for_settings({"suite_folder": suite})
    assert not got_dl.get("dugouts"), got_dl
    os.environ.pop("NDSUITE_SOURCE")
    # the pack inside the suite folder (Setup's fill scans it): pack layers, then a user file wins
    suite2 = os.path.join(work, "suite2")
    shutil.copytree(pack, os.path.join(suite2, "ND_Dam_Suite_Pack"))
    found, _ = SF.scan(suite2)
    assert found["dugouts"].startswith(os.path.join(suite2, "ND_Dam_Suite_Pack", "dwr", "DWR_dugouts.gpkg")), found
    shutil.copy(os.path.join(src, "DWR_Dugouts.gpkg"), os.path.join(suite2, "DWR_Dugouts.gpkg"))
    SF._SCAN.clear()
    found, _ = SF.scan(suite2)
    assert found["dugouts"] == "{}|layername=DWR_Dugouts".format(os.path.join(suite2, "DWR_Dugouts.gpkg")), found
    assert found["dugout_polygons"].startswith(os.path.join(suite2, "ND_Dam_Suite_Pack")), found
    # suitedata.dwr_layers: dwr/ drop-in first, the pack for the rest; pack=False = dwr/ only
    os.environ["NDSUITE_PACK"] = pack
    data = os.path.join(work, "data")
    os.makedirs(os.path.join(data, "dwr"))
    d = SD.dwr_layers(data)
    assert d["dwr_dugouts"] == os.path.join(pack, "dwr", "DWR_dugouts.gpkg"), d
    shutil.copy(os.path.join(src, "DWR_Dugouts.gpkg"), os.path.join(data, "dwr", "DWR_Dugouts.gpkg"))
    d = SD.dwr_layers(data)
    assert d["dwr_dugouts"] == os.path.join(data, "dwr", "DWR_Dugouts.gpkg"), d
    assert d["dwr_dams"] == os.path.join(pack, "dwr", "DWR_dams.gpkg"), d
    assert SD.dwr_layers(data, pack=False)["dwr_dams"] == "", SD.dwr_layers(data, pack=False)
    os.environ.pop("NDSUITE_PACK")
    print("OK (plugins fall back to the pack's DWR layers; the engineer's own file wins)")
finally:
    shutil.rmtree(work, ignore_errors=True)
