"""damsuite 1.18.3 (Screening 0.17.57): the suite's lookups find the DWR dams, NID and the NLCD
land cover in BOTH offline pack formats.

Tiny synthetic packs in a temp folder: a format-1 pack (nd_offline_pack/nd_offline_pack.sqlite
written by offline_pack.PackWriter, its manifest.json listing landcover/NLCD_2021_landcover.gpkg
under "rasters") and the same data converted to format 2 (gpkgpack.convert_v1, ND_Dam_Suite_Pack).
Checks, for Screening's and the Runner's vendored damsuite:

  * suitedata.pack_file / pack_entries / dwr_layers / nlcd_raster / find_pack answer for the
    format-1 pack (the .sqlite or its folder) as for format 2; a format-1 data set is written
    once to a GeoPackage in NDSUITE_PACK_CACHE (a real GeoPackage with every feature), never
    inside the pack (the pack's files are unchanged);
  * suite_fill.scan / _pack_dwr / layers_for_settings fill the DWR dams and the NID from format 1;
    format 2 is unchanged (_pack_dwr: the pack's DWR layers only, as before);
  * RASTER_EXT has .gpkg; damdata.PACK_SETS["flood_zones"] claims no pack layer;
  * offline_pack unpacks a zipped format-1 pack with the rasters its manifest lists.

No QGIS, no network; nothing outside the temp folder is written.
    python3 DamSafety_Screening/tests/test_pack_formats.py
"""
import hashlib
import importlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
ROOT = os.path.dirname(PLUGIN)
sys.path.insert(0, ROOT)
for k in ("NDSUITE_SETTINGS", "NDSUITE_PACK", "NDSUITE_DATA", "NDSUITE_SOURCE", "NDSUITE_DAMS"):
    os.environ.pop(k, None)

NLCD = "NLCD_2021_landcover"
BBOX = (-104.1, 45.9, -96.5, 49.1)


def _pt(i, x, y, **p):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [x, y]},
            "properties": dict(id=i, **p)}


SETS = {"DWR_dams": [_pt(1, -100.0, 47.0, DAM_NAME="A"), _pt(2, -99.0, 47.5, DAM_NAME="B")],
        "NID_dams": [_pt(3, -100.1, 47.1, NAME="N")],
        "HIFLD_fire_ems_stations": [_pt(4, -100.0, 47.0, FCODE=74026)]}


def make_packs(root, ds):
    """(format-1 folder, format-2 folder) with SETS and a stand-in NLCD file."""
    OP, GP = ds.offline_pack, ds.gpkgpack
    p1 = os.path.join(root, "nd_offline_pack")
    os.makedirs(os.path.join(p1, "landcover"))
    db = os.path.join(p1, OP.DB_NAME)
    w = OP.PackWriter(db, BBOX)
    for name, feats in SETS.items():
        w.begin(name, name, "https://example.invalid/" + name)
        w.add(feats, lambda f: f["properties"]["id"])
        w.finish()
    man = w.manifest()
    w.close(finalize=True)
    nl = os.path.join(p1, "landcover", NLCD + ".gpkg")
    with open(nl, "wb") as fh:
        fh.write(b"NLCD stand-in " * 50)
    man["rasters"] = {NLCD: {"file": "landcover/{}.gpkg".format(NLCD), "group": "landcover",
                             "label": "NLCD 2021 land cover"}}
    with open(os.path.join(p1, OP.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(man, fh)
    p2 = os.path.join(root, "ND_Dam_Suite_Pack")
    m2 = GP.convert_v1(db, p2)
    GP.add_raster_file(p2, m2, NLCD, nl, {"label": "NLCD 2021 land cover"})
    GP.write_manifest(p2, m2)
    return p1, p2


def _digest(folder):
    out = {}
    for r, _d, files in os.walk(folder):
        for f in files:
            p = os.path.join(r, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, folder)] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _gpkg_count(path, layer):
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM gpkg_contents WHERE table_name=?",
                           (layer,)).fetchone()[0] == 1
        return con.execute('SELECT COUNT(*) FROM "{}"'.format(layer)).fetchone()[0]
    finally:
        con.close()


class _NS(object):
    pass


def check(pkg):
    ds = _NS()
    for m in ("offline_pack", "gpkgpack", "suitedata", "suite_fill", "damdata"):
        setattr(ds, m, importlib.import_module(pkg + ".damsuite." + m))
    SD, SF = ds.suitedata, ds.suite_fill
    work = tempfile.mkdtemp(prefix="packfmt_")
    try:
        cache = os.path.join(work, "cache")
        os.environ[SD.ENV_PACK_CACHE] = cache
        p1, p2 = make_packs(os.path.join(work, "packs"), ds)
        db = os.path.join(p1, ds.offline_pack.DB_NAME)
        before = _digest(p1)
        assert ".gpkg" in SD.RASTER_EXT
        assert SD._v1_db(p1) == db and SD._v1_db(db) == db
        assert SD._v1_db(p2) == "" and SD._v1_db(os.path.join(work, "nothing")) == ""

        for pk in (p1, db, p2):
            dams = SD.pack_file("DWR_dams", pk)
            nid = SD.pack_file("NID_dams", pk)
            nl = SD.pack_file(NLCD, pk, "rasters")
            assert dams and _gpkg_count(dams, "DWR_dams") == 2, (pk, dams)
            assert nid and _gpkg_count(nid, "NID_dams") == 1, (pk, nid)
            assert nl and os.path.isfile(nl) and nl.endswith(NLCD + ".gpkg"), (pk, nl)
            assert SD.pack_file("NO_such_layer", pk) == ""
            ents = SD.pack_entries(pk)
            assert {"DWR_dams", "NID_dams", "HIFLD_fire_ems_stations"} <= set(ents), ents
            assert ents["DWR_dams"]["group"] == "dwr" and ents["NID_dams"]["count"] == 1
            assert NLCD in SD.pack_entries(pk, "rasters")
            if pk == p2:
                assert dams.startswith(p2) and nl.startswith(p2)
            else:
                assert dams.startswith(cache) and nid.startswith(cache)   # never in the pack
                assert nl == os.path.join(p1, "landcover", NLCD + ".gpkg")
        # written once: a second lookup is the same file, not rewritten
        f1 = SD.pack_file("DWR_dams", p1)
        t1 = os.path.getmtime(f1)
        assert SD.pack_file("DWR_dams", db) == f1 and os.path.getmtime(f1) == t1

        # the folder lookups: the format-1 folder is a pack, format 2 still wins beside it
        assert SD.find_pack(p1) == p1
        assert SD.find_pack(os.path.dirname(p1)) == p2
        assert SD.nlcd_raster(p1) == os.path.join(p1, "landcover", NLCD + ".gpkg")
        assert SD.nlcd_raster(p2) == os.path.join(p2, "landcover", NLCD + ".gpkg")
        d1 = SD.dwr_layers(p1)
        assert d1["dwr_dams"] == f1 and not d1["dwr_dam_lines"], d1
        assert SD.dwr_layers(p2)["dwr_dams"] == os.path.join(p2, "dwr", "DWR_dams.gpkg")

        # suite_fill (Screening / Runner auto-fill)
        found, notes = SF.scan(p1)
        assert found.get("pack") == db, found
        assert found.get("dwr") == f1 + "|layername=DWR_dams", found
        assert found.get("nid", "").endswith("NID_dams.gpkg|layername=NID_dams"), found
        assert any("format-1 pack" in n for n in notes), notes
        v1 = SF._pack_dwr({"pack": p1}, [])
        assert set(v1) == {"dwr", "nid"}, v1
        assert SF._pack_dwr({"pack": db}, []) == v1
        lay = SF.layers_for_settings({"pack": p1})
        assert lay.get("dwr") == v1["dwr"] and lay.get("nid") == v1["nid"], lay
        v2 = SF._pack_dwr({"pack": p2}, [])
        assert v2 == {"dwr": os.path.join(p2, "dwr", "DWR_dams.gpkg") + "|layername=DWR_dams"}, v2
        found2, _n = SF.scan(p2)          # format 2: its GeoPackages are read by the scan (OGR)
        assert found2.get("pack") == p2, found2
        assert not any(u.startswith(cache) for u in found2.values()), found2

        # damdata: no pack holds the FEMA flood zones
        assert tuple(ds.damdata.PACK_SETS["flood_zones"]) == ()

        # a zipped format-1 pack: unpacked once with its rasters
        zp = os.path.join(work, "nd_offline_pack.zip")
        with zipfile.ZipFile(zp, "w") as z:
            for rel in (ds.offline_pack.DB_NAME, "landcover/{}.gpkg".format(NLCD),
                        ds.offline_pack.MANIFEST_NAME):
                z.write(os.path.join(p1, *rel.split("/")), "nd_offline_pack/" + rel)
        got = ds.offline_pack._unzip(zp, lambda m: None)
        assert os.path.basename(got) == ds.offline_pack.DB_NAME, got
        assert os.path.isfile(os.path.join(os.path.dirname(got), "landcover", NLCD + ".gpkg"))
        assert SD.pack_file(NLCD, got, "rasters")

        assert _digest(p1) == before, "the format-1 pack was changed"
    finally:
        os.environ.pop("NDSUITE_PACK_CACHE", None)
        shutil.rmtree(work, ignore_errors=True)


for pkg in ("nd_dam_screening", "nd_breach_runner"):
    check(pkg)
    print("ok {}: DWR / NID / NLCD from format 1 and format 2".format(pkg))
print("test_pack_formats: all passed")
