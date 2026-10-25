# -*- coding: utf-8 -*-
"""Hazard 2.18.60 / damsuite 1.17.5: format-2 offline packs and the check against manifest.json.

A format-2 pack is a folder: manifest.json, LAYERS.txt and one GeoPackage per layer. This
builds small synthetic packs in a temp folder (GeoPackages written with GDAL/OGR when it is
importable, else with the suite's own stdlib writer gpkgpack.write_layer) plus a format-1
.sqlite pack, and checks:

  * format detection (folder / manifest.json / LAYERS.txt -> 2; .sqlite and its
    folder -> 1; anything else -> 0);
  * Hazard's offline_pack.open_pack reads a format-2 folder (same data set names, NHD_ alias
    of the 3DHP_ layers, query) and still reads format 1;
  * damsuite.packcheck: a valid pack has no warnings; a missing file, a count mismatch, a
    field, a geometry and a CRS mismatch are each named; a missing required layer is an
    error with a clear message; format 1 still checks OK;
  * Hazard's download.pack_check / offline_pack.check never raise.

Nothing outside the temp folder is read or written.
    python3 DamSafety_Hazard/tests/test_pack_format2.py
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Hazard import offline_pack as HOP          # noqa: E402
from DamSafety_Hazard.damsuite import packcheck as PC    # noqa: E402
from DamSafety_Hazard.damsuite import gpkgpack as GP     # noqa: E402

try:
    from osgeo import ogr, osr                             # noqa: E402
    ogr.UseExceptions()
    HAVE_OGR = True
except Exception:                                          # noqa: BLE001 - no GDAL here
    HAVE_OGR = False

FAILS = []


def expect(cond, what):
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


# ---------------------------------------------------------------- builders --
STRUCTS = [((-100.50, 47.50), {"fd_id": 1, "occtype": "RES1", "val_struct": 120000.5}),
           ((-100.51, 47.51), {"fd_id": 2, "occtype": "COM1", "val_struct": 90000.0}),
           ((-100.52, 47.52), {"fd_id": 3, "occtype": "RES1", "val_struct": 75000.0})]
FLOWS = [([(-100.6, 47.4), (-100.4, 47.6)], {"gnis_name": "Test Creek"})]
WATER = [([[(-100.55, 47.45), (-100.45, 47.45), (-100.45, 47.55), (-100.55, 47.45)]],
          {"gnis_name": "Test Lake"})]

FIELDS = {"NSI_structures": [["fd_id", "fd_id", "int"], ["occtype", "occtype", "text"],
                             ["val_struct", "val_struct", "real"]],
          "3DHP_flowlines": [["gnis_name", "gnis_name", "text"]],
          "3DHP_waterbodies": [["gnis_name", "gnis_name", "text"]]}
GEOMS = {"NSI_structures": "POINT", "3DHP_flowlines": "LINESTRING",
         "3DHP_waterbodies": "MULTIPOLYGON"}
FILES = {"NSI_structures": "structures/NSI_structures.gpkg",
         "3DHP_flowlines": "hydro/3DHP_flowlines.gpkg",
         "3DHP_waterbodies": "hydro/3DHP_waterbodies.gpkg"}
FEATS = {"NSI_structures": STRUCTS, "3DHP_flowlines": FLOWS, "3DHP_waterbodies": WATER}
OGR_KIND = {"int": "OFTInteger64", "real": "OFTReal", "text": "OFTString"}


def _geojson(name, geom):
    g = GEOMS[name]
    if g == "POINT":
        return {"type": "Point", "coordinates": list(geom)}
    if g == "LINESTRING":
        return {"type": "LineString", "coordinates": [list(p) for p in geom]}
    return {"type": "MultiPolygon", "coordinates": [[[list(p) for p in ring] for ring in geom]]}


def write_gpkg(path, name, epsg=4326, geometry=None, fields=None, feats=None):
    """One GeoPackage layer `name` (table = data set name)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = FIELDS[name] if fields is None else fields
    feats = FEATS[name] if feats is None else feats
    gname = geometry or GEOMS[name]
    if HAVE_OGR:
        drv = ogr.GetDriverByName("GPKG")
        ds = drv.CreateDataSource(path)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        gt = {"POINT": ogr.wkbPoint, "LINESTRING": ogr.wkbLineString,
              "MULTIPOLYGON": ogr.wkbMultiPolygon, "POLYGON": ogr.wkbPolygon}[gname]
        lyr = ds.CreateLayer(name, srs, gt, ["GEOMETRY_NAME=geom", "FID=fid", "SPATIAL_INDEX=NO"])
        for col, _p, kind in fields:
            lyr.CreateField(ogr.FieldDefn(col, getattr(ogr, OGR_KIND[kind])))
        for geom, props in feats:
            f = ogr.Feature(lyr.GetLayerDefn())
            g = _geojson(name, geom)
            if gname == "POINT" and g["type"] != "Point":
                g = {"type": "Point", "coordinates": list(geom[0][0]) if g["type"] ==
                     "MultiPolygon" else list(geom[0])}
            f.SetGeometry(ogr.CreateGeometryFromJson(json.dumps(g)))
            for col, prop, _k in fields:
                if prop in props:
                    f.SetField(col, props[prop])
            lyr.CreateFeature(f)
        ds = None
    else:
        rows = [{"type": "Feature", "geometry": _geojson(name, g), "properties": p}
                for g, p in feats]
        GP.write_layer(path, name, lambda: iter(rows),
                       fields=fields, geometry=gname)
        if epsg != 4326:
            con = sqlite3.connect(path)
            con.execute("INSERT OR IGNORE INTO gpkg_spatial_ref_sys (srs_name, srs_id, "
                        "organization, organization_coordsys_id, definition) VALUES "
                        "('NAD83 / Conus Albers', ?, 'EPSG', ?, 'undefined')", (epsg, epsg))
            con.execute("UPDATE gpkg_geometry_columns SET srs_id = ?", (epsg,))
            con.execute("UPDATE gpkg_contents SET srs_id = ?", (epsg,))
            con.commit()
            con.close()
    return path


def build_v2(folder):
    """A valid format-2 pack folder."""
    os.makedirs(folder, exist_ok=True)
    datasets = {}
    for name in ("NSI_structures", "3DHP_flowlines", "3DHP_waterbodies"):
        write_gpkg(os.path.join(folder, *FILES[name].split("/")), name)
        datasets[name] = {"file": FILES[name], "layer": name, "count": len(FEATS[name]),
                          "fields": FIELDS[name], "geometry": GEOMS[name], "status": "complete",
                          "label": name, "group": FILES[name].split("/")[0]}
    m = {"format": "nd-dam-hazard-offline-pack", "format_version": 2, "layout": "gpkg",
         "area": "test", "bbox": [-104.0489, 45.9351, -96.5543, 49.0],
         "created": "2026-10-25T00:00:00", "datasets": datasets, "rasters": {},
         "left_out": {"Terrain (DEM)": "too large"}}
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1)
    with open(os.path.join(folder, "LAYERS.txt"), "w", encoding="utf-8") as fh:
        fh.write("test pack\nVectors: EPSG:4326\n")
    return folder


def edit_manifest(folder, fn):
    p = os.path.join(folder, "manifest.json")
    with open(p, encoding="utf-8") as fh:
        m = json.load(fh)
    fn(m)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=1)


def build_v1(folder):
    """A minimal format-1 pack: nd_offline_pack.sqlite (+ manifest.json) in `folder`."""
    os.makedirs(folder, exist_ok=True)
    db = os.path.join(folder, "nd_offline_pack.sqlite")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("format", "nd-dam-hazard-offline-pack"), ("format_version", "1"),
        ("bbox", json.dumps([-104.0489, 45.9351, -96.5543, 49.0])),
        ("created", "2026-10-14T00:00:00")])
    con.execute("CREATE TABLE datasets (name TEXT PRIMARY KEY, label TEXT, url TEXT, "
                "fetched TEXT, count INTEGER, status TEXT, note TEXT, maxw REAL, maxh REAL, "
                "geometry TEXT, gaps TEXT)")
    con.execute("INSERT INTO datasets (name, label, count, status, geometry, gaps) VALUES "
                "(?, ?, ?, ?, ?, ?)", ("NSI_structures", "NSI", len(STRUCTS), "complete",
                                       "Point", "[]"))
    con.execute("CREATE TABLE ds_NSI_structures (fid INTEGER PRIMARY KEY, uid TEXT UNIQUE, "
                "minx REAL, miny REAL, maxx REAL, maxy REAL, feat BLOB)")
    for i, (pt, props) in enumerate(STRUCTS):
        feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": list(pt)},
                "properties": props}
        blob = zlib.compress(json.dumps(feat).encode("utf-8"), 6)
        con.execute("INSERT INTO ds_NSI_structures (uid, minx, miny, maxx, maxy, feat) VALUES "
                    "(?, ?, ?, ?, ?, ?)", ("s:{}".format(i), pt[0], pt[1], pt[0], pt[1], blob))
    con.commit()
    con.close()
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump({"format": "nd-dam-hazard-offline-pack", "format_version": 1,
                   "datasets": {"NSI_structures": {"count": len(STRUCTS),
                                                   "status": "complete"}}}, fh)
    return db


def kinds(res, layer):
    return sorted({i.kind for i in res.issues if i.layer == layer and i.level != PC.NOTE})


# ------------------------------------------------------------------- tests --
def main():
    print("GeoPackages written with {}".format("GDAL/OGR" if HAVE_OGR else
                                                "gpkgpack.write_layer (no GDAL here)"))
    tmp = tempfile.mkdtemp(prefix="pack_format2_")
    try:
        good = build_v2(os.path.join(tmp, "good"))
        v1 = build_v1(os.path.join(tmp, "v1"))
        other = os.path.join(tmp, "plain")
        os.makedirs(other)

        # -- detection
        expect(PC.detect_format(good) == 2, "detect: format-2 folder -> 2")
        expect(PC.detect_format(os.path.join(good, "manifest.json")) == 2,
               "detect: its manifest.json -> 2")
        expect(PC.detect_format(os.path.join(good, "LAYERS.txt")) == 2,
               "detect: its LAYERS.txt -> 2")
        expect(PC.detect_format(os.path.join(good, "hydro")) == 0,
               "detect: a subfolder of the pack -> 0 (not a pack itself)")
        expect(PC.detect_format(v1) == 1, "detect: nd_offline_pack.sqlite -> 1")
        expect(PC.detect_format(os.path.dirname(v1)) == 1, "detect: its folder -> 1")
        expect(PC.detect_format(other) == 0, "detect: a plain folder -> 0")
        expect(HOP.pack_format(good) == 2 and HOP.pack_format(v1) == 1,
               "Hazard offline_pack.pack_format agrees")

        # -- loading through Hazard's open_pack
        r = HOP.open_pack(good)
        expect(r.has("NSI_structures") and r.has("3DHP_flowlines"),
               "open_pack(format 2): data sets by their pack names")
        expect(r.has("NHD_flowlines") and r.has("NHD_waterbodies"),
               "open_pack(format 2): NHD_ names answer to the 3DHP_ layers")
        feats = r.query("NSI_structures", (-101.0, 47.0, -100.0, 48.0))
        expect(len(feats) == 3 and sorted(f["properties"]["fd_id"] for f in feats) == [1, 2, 3],
               "open_pack(format 2): query returns the 3 structures with their fields")
        expect(r.created == "2026-10-25", "open_pack(format 2): created from manifest.json")
        r1 = HOP.open_pack(v1)
        expect(isinstance(r1, HOP.PackReader) and r1.has("NSI_structures") and
               len(r1.query("NSI_structures", (-101.0, 47.0, -100.0, 48.0))) == 3,
               "open_pack(format 1): unchanged PackReader, 3 structures")
        try:
            HOP.open_pack(other)
            expect(False, "open_pack(plain folder) raises PackError")
        except HOP.PackError:
            expect(True, "open_pack(plain folder) raises PackError")

        # -- a valid pack
        res = PC.check_pack(good, required=("NSI_structures", "NHD_waterbodies"))
        expect(res.ok and not res.warnings and not res.errors and res.format == 2,
               "valid format-2 pack: no warnings ({})".format(res.summary()))
        expect(res.summary().endswith("OK - every layer matches manifest.json"),
               "valid pack: summary says OK")
        expect(any(i.kind == "left_out" for i in res.issues), "left_out listed as a note")
        expect(HOP.check(good, required=("NSI_structures",)).ok,
               "Hazard offline_pack.check: valid pack OK")
        expect(HOP.describe(good).find("CHECK:") < 0, "describe(): no CHECK on a valid pack")

        # -- a missing layer file
        miss = shutil.copytree(good, os.path.join(tmp, "missing"))
        os.remove(os.path.join(miss, "hydro", "3DHP_waterbodies.gpkg"))
        res = PC.check_pack(miss)
        expect(not res.ok and kinds(res, "3DHP_waterbodies") == ["missing"] and
               "missing: 3DHP_waterbodies" in res.summary(),
               "missing file: named ({})".format(res.summary()))
        expect(not res.errors, "missing optional layer: a warning, not an error")
        expect("CHECK:" in HOP.describe(miss), "describe(): CHECK on a pack with a missing layer")

        # -- a required layer missing
        req = shutil.copytree(good, os.path.join(tmp, "required"))
        os.remove(os.path.join(req, "structures", "NSI_structures.gpkg"))
        res = PC.check_pack(req, required=("NSI_structures",))
        expect(res.missing_required == ["NSI_structures"] and res.errors,
               "required layer missing: an error")
        msg = res.required_message("The classification")
        expect("NSI_structures" in msg and "not found" in msg,
               "required layer missing: clear message ({})".format(msg))
        try:
            res.raise_if_required_missing("Hazard")
            expect(False, "raise_if_required_missing raises")
        except PC.PackCheckError:
            expect(True, "raise_if_required_missing raises PackCheckError")
        try:
            from DamSafety_Hazard import download as DL       # needs qgis (CI: real QGIS)
        except ImportError:
            DL = None
            print("skip Hazard download.pack_check (no QGIS here)")
        if DL is not None:
            hres = DL.pack_check(req)
            expect(hres is not None and hres.missing_required == ["NSI_structures"],
                   "Hazard download.pack_check: NSI_structures required, does not raise")
        hres = HOP.check(req, required=("NSI_structures",))
        expect(hres is not None and hres.missing_required == ["NSI_structures"],
               "Hazard offline_pack.check: required layer missing, does not raise")
        unl = PC.check_pack(good, required=("USA_Structures",))
        expect(unl.missing_required == ["USA_Structures"],
               "required layer not in manifest.json: an error")

        # -- a count mismatch
        cnt = shutil.copytree(good, os.path.join(tmp, "count"))
        edit_manifest(cnt, lambda m: m["datasets"]["NSI_structures"].update(count=5))
        res = PC.check_pack(cnt)
        expect(kinds(res, "NSI_structures") == ["count"] and
               "not as in manifest.json: NSI_structures" in res.summary(),
               "count mismatch: named ({})".format(res.lines(notes=False)[-1].strip()))
        expect(PC.check_pack(cnt, deep=False).ok, "quick check (deep=False) skips counts")

        # -- a field mismatch
        fld = shutil.copytree(good, os.path.join(tmp, "fields"))
        edit_manifest(fld, lambda m: m["datasets"]["NSI_structures"]["fields"].append(
            ["num_story", "num_story", "int"]))
        res = PC.check_pack(fld)
        expect(kinds(res, "NSI_structures") == ["fields"] and
               any("num_story" in i.text for i in res.issues),
               "field missing from the file: named")
        typ = shutil.copytree(good, os.path.join(tmp, "ftype"))
        edit_manifest(typ, lambda m: m["datasets"]["NSI_structures"].update(
            fields=[["fd_id", "fd_id", "text"], ["occtype", "occtype", "text"],
                    ["val_struct", "val_struct", "real"]]))
        res = PC.check_pack(typ)
        expect(kinds(res, "NSI_structures") == ["fields"] and
               any("type(s) differ" in i.text for i in res.issues),
               "field type differs from the manifest kind: named")

        # -- a geometry mismatch
        geo = shutil.copytree(good, os.path.join(tmp, "geom"))
        edit_manifest(geo, lambda m: m["datasets"]["3DHP_flowlines"].update(geometry="POINT"))
        res = PC.check_pack(geo)
        expect(kinds(res, "3DHP_flowlines") == ["geometry"],
               "geometry mismatch: named ({})".format(res.summary()))

        # -- a CRS mismatch (a layer written in EPSG:5070)
        crs = shutil.copytree(good, os.path.join(tmp, "crs"))
        p = os.path.join(crs, "hydro", "3DHP_flowlines.gpkg")
        os.remove(p)
        write_gpkg(p, "3DHP_flowlines", epsg=5070)
        res = PC.check_pack(crs)
        expect(kinds(res, "3DHP_flowlines") == ["crs"] and
               any("5070" in i.text for i in res.issues if i.kind == "crs"),
               "CRS mismatch: named ({})".format(res.summary()))

        # -- format 1 still works
        res = PC.check_pack(v1, required=("NSI_structures",))
        expect(res.format == 1 and res.ok, "format 1: checks OK ({})".format(res.summary()))
        expect(HOP.check(v1, required=("NSI_structures",)).ok,
               "Hazard offline_pack.check: format 1 OK")

        # -- never raises
        HOP.check(os.path.join(tmp, "nowhere"))
        expect(True, "Hazard offline_pack.check on a missing path does not raise")
        expect(not PC.check_pack(other).ok or PC.check_pack(other).format == 0,
               "check_pack on a plain folder: not a pack")

        # -- read-only: the good pack's files are unchanged by all of the above
        expect(not os.path.exists(os.path.join(good, "manifest.json-journal")),
               "nothing written into the pack")
    finally:
        PC.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
