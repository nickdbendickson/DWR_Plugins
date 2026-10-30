"""Runner 3.10.72: culvert-precheck (core/culvert_precheck.py, `ndbreach_cli.py culvert-precheck`).

A flat 1 m DEM at 250 m with a no-data block on the east side, and a structures GeoPackage in feet
like the Runner's layer. Expected, from the 3.10.71 rule (lowest fine ground within
max(apron, 5 m), cap at ground + 0.3 m):
  * A: given inverts sit 0.584 / 0.554 m above a 250 m bed (820 ft, above the
    0-500 ft local band; the old 100 m bed is 328 ft and is now inside it).
    HIGH at both ends; the Runner caps them to ground + 0.3 m;
  * B: no inverts and both ends in the no-data block gives FALLBACK (the Runner uses the 30 m DEM);
  * C: no inverts gives fine_grid, with the suggested invert = ground (no flag);
  * D: 3 m long (< 2 x width) gives SKIPPED; a bridge is not a culvert and is not listed.
The checks never write next to the inputs."""
import csv
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

EPSG = 5070
FT = 0.3048
# 250 m = 820 ft, above the 0-500 ft local band. The old 100 m bed is 328 ft,
# which reconcile_inverts now treats as a local datum. The foot inverts keep
# the same offset above the bed that 330.0 / 329.9 ft had over 100 m.
GROUND_M = 250.0
INV_UP_FT = (GROUND_M + (330.0 * FT - 100.0)) / FT
INV_DN_FT = (GROUND_M + (329.9 * FT - 100.0)) / FT


def _gdal():
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ogr.UseExceptions()
    return gdal, ogr, osr


def _dem(path, ground=GROUND_M, nodata_from_col=300, x0=0.0, y0=1000.0, n=400):
    gdal, _ogr, osr = _gdal()
    s = osr.SpatialReference()
    s.ImportFromEPSG(EPSG)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetProjection(s.ExportToWkt())
    ds.SetGeoTransform((x0, 1.0, 0.0, y0, 0.0, -1.0))
    a = np.full((n, n), ground, "float32")
    a[:, nodata_from_col:] = -9999.0
    if n > 200:
        a[200, 100] = ground - 0.5      # a low cell 1.6 m from C's upstream end
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(a)
    ds = None


def _structures(path):
    _gdal_, ogr, osr = _gdal()
    s = osr.SpatialReference()
    s.ImportFromEPSG(EPSG)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("runner_culverts", s, ogr.wkbLineString)
    for n, t in (("name", ogr.OFTString), ("kind", ogr.OFTString), ("span_ft", ogr.OFTReal),
                 ("rise_ft", ogr.OFTReal), ("barrels", ogr.OFTInteger), ("shape", ogr.OFTString),
                 ("invert_up", ogr.OFTReal), ("invert_dn", ogr.OFTReal), ("src_id", ogr.OFTString)):
        lyr.CreateField(ogr.FieldDefn(n, t))
    rows = [("A", "culvert", 4.0, 4.0, 1, "box", INV_UP_FT, INV_DN_FT, "idA", [(50, 900), (50, 860)]),
            ("B", "culvert", 3.0, 3.0, 1, "pipe", None, None, "idB", [(350, 900), (350, 860)]),
            ("C", "culvert", 6.0, 4.0, 2, "arch", None, None, "idC", [(102, 800), (102, 760)]),
            ("D", "culvert", 6.0, 4.0, 1, "box", None, None, "idD", [(150, 700), (150, 697)]),
            ("E", "bridge", 40.0, 10.0, 1, "", None, None, "idE", [(200, 700), (200, 650)])]
    for name, kind, w, h, nb, sh, iu, idn, sid, pts in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        for k, v in (("name", name), ("kind", kind), ("span_ft", w), ("rise_ft", h),
                     ("barrels", nb), ("shape", sh), ("invert_up", iu), ("invert_dn", idn),
                     ("src_id", sid)):
            if v is not None:
                f.SetField(k, v)
        g = ogr.Geometry(ogr.wkbLineString)
        for x, y in pts:
            g.AddPoint_2D(float(x), float(y))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds = None


def _setup():
    d = tempfile.mkdtemp(prefix="cpc_")
    inp = os.path.join(d, "dam")
    os.makedirs(inp)
    _dem(os.path.join(inp, "fine_1m.tif"))
    _structures(os.path.join(inp, "structures_T.gpkg"))
    return d, inp


def _by(rows):
    return {(r["name"], r["end"]): r for r in rows}


@test
def culvert_precheck_flags_high_fallback_and_skipped():
    try:
        _gdal()
    except ImportError:
        return
    from nd_breach_runner.core import culvert_precheck as cp
    d, inp = _setup()
    try:
        st = os.path.join(inp, "structures_T.gpkg")
        before = cp.sha256(st)
        rows, s = cp.check(st, os.path.join(inp, "fine_1m.tif"), dam="T", z_choice="meters")
        r = _by(rows)
        assert s["culverts"] == 4 and s["culverts_skipped_by_runner"] == 1, s
        assert not any(k[0] == "E" for k in r), "bridges are not culverts"
        a = r[("A", "upstream")]
        assert a["flag"] == cp.FLAG_HIGH, a
        assert abs(a["given_invert_m"] - INV_UP_FT * FT) < 1e-3 and abs(a["fine_ground_m"] - GROUND_M) < 1e-6
        assert abs(a["delta_m"] - (INV_UP_FT * FT - GROUND_M)) < 1e-3
        assert a["runner_source"] == "given_capped" and abs(a["runner_invert_m"] - (GROUND_M + 0.3)) < 1e-6
        assert abs(a["suggested_invert_m"] - GROUND_M) < 1e-6 and a["fallback"] == "no"
        assert "within 3.0 ft" in a["reconcile_note"], a["reconcile_note"]
        assert r[("A", "downstream")]["flag"] == cp.FLAG_HIGH
        for e in ("upstream", "downstream"):
            b = r[("B", e)]
            assert b["flag"] == cp.FLAG_FALLBACK and b["fallback"] == "yes", b
            assert b["runner_source"] == "model_dem" and b["runner_invert_m"] is None
            assert b["fine_ground_m"] is None and b["suggested_invert_m"] is None
        c = r[("C", "upstream")]
        # apron = max(2, 6 ft) = 2 m -> radius 5 m: the low cell 1.6 m away counts
        assert c["flag"] == "" and c["runner_source"] == "fine_grid", c
        assert abs(c["fine_ground_m"] - (GROUND_M - 0.5)) < 1e-6 and abs(c["suggested_invert_m"] - (GROUND_M - 0.5)) < 1e-6
        assert c["shape"] == "box" and c["barrels"] == 2 and c["search_m"] == 5.0
        assert r[("D", "upstream")]["flag"] == cp.FLAG_SKIPPED
        assert s["ends_high"] == 2 and s["culverts_high"] == 1
        assert s["ends_fallback"] == 2 and s["culverts_fallback"] == 1
        # no reconcile: same flags (A within 3 ft)
        rows2, _s2 = cp.check(st, os.path.join(inp, "fine_1m.tif"), z_choice="meters",
                              reconcile=False)
        assert _by(rows2)[("A", "upstream")]["reconcile_note"] == ""
        assert cp.sha256(st) == before, "the structures layer must not change"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_precheck_matches_fine_ground_inverts():
    """The precheck's runner columns are what structures.fine_ground_inverts gives the run."""
    try:
        _gdal()
    except ImportError:
        return
    from nd_breach_runner.core import culvert_precheck as cp, structures as sm
    d, inp = _setup()
    try:
        st, dem = os.path.join(inp, "structures_T.gpkg"), os.path.join(inp, "fine_1m.tif")
        feats, wkt, _l = cp.read_structures(st)
        fr = cp.FineRaster(dem, wkt, "meters")
        a = [s for _f, _a, s in feats if s.name == "A"][0]
        specs, _n = sm.anuga_culverts([a], cp._NoModelDem())
        sm.fine_ground_inverts(specs, fr.lowest)
        rows, _s = cp.check(st, dem, z_choice="meters", reconcile=False)
        r = _by(rows)
        assert abs(r[("A", "upstream")]["runner_invert_m"] - specs[0]["invert_elevations"][0]) < 1e-3
        assert specs[0]["invert_source"] == ["given_capped", "given_capped"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_precheck_cli_dam_folder_and_composite_labels():
    try:
        gdal, _ogr, _osr = _gdal()
    except ImportError:
        return
    from nd_breach_runner.core import culvert_precheck as cp
    d, inp = _setup()
    try:
        # a composite VRT: a 5 m fill everywhere under the 1 m tile (the 1 m one listed last)
        fill = os.path.join(inp, "fill5m.tif")
        _dem(fill, ground=100.2, nodata_from_col=10 ** 6, n=80)
        ds = gdal.Open(fill, gdal.GA_Update)
        ds.SetGeoTransform((0.0, 5.0, 0.0, 1000.0, 0.0, -5.0))
        ds = None
        vrt = os.path.join(inp, "T_composite_1m.vrt")
        gdal.BuildVRT(vrt, [fill, os.path.join(inp, "fine_1m.tif")], resolution="highest",
                      srcNodata=-9999, VRTNodata=-9999)
        srcs = cp.vrt_sources(vrt)
        assert os.path.basename(srcs[0]) == "fine_1m.tif" and os.path.basename(srcs[1]) == "fill5m.tif"
        with open(os.path.join(inp, "rundam.json"), "w") as f:
            json.dump({"inputs": {"DAM_ID": "T-1", "DEM": vrt, "DEM_Z": 3, "CELL": 30,
                                  "STRUCTURES": os.path.join(inp, "structures_T.gpkg")
                                  + "|layername=runner_culverts"}}, f)
        j = cp.find_dam_inputs(inp)
        assert j["dam"] == "T-1" and j["z_choice"] == "meters" and j["layer"] == "runner_culverts"
        assert j["fine_dem"] == vrt and len(j["coverage"]) == 2
        out = os.path.join(d, "out")
        empty = os.path.join(d, "nostructs")
        os.makedirs(empty)
        assert cp.main(["--dam-folder", inp, empty, "--out", out]) == 0
        with open(os.path.join(out, "culvert_precheck_T-1.csv")) as f:
            rows = list(csv.DictReader(f))
        assert tuple(rows[0].keys()) == cp.CSV_COLUMNS
        r = {(x["name"], x["end"]): x for x in rows}
        # B's ends are on the fill now (no FALLBACK), labelled as not on the top source
        b = r[("B", "upstream")]
        assert b["flag"] == "" and b["fine_source"] == "fill5m.tif" and b["on_top_source"] == "no", b
        assert r[("A", "upstream")]["on_top_source"] == "yes"
        md = open(os.path.join(out, "culvert_precheck_T-1.md")).read()
        assert "HIGH" in md and "| culverts | 4 |" in md and "..." not in md
        idx = json.load(open(os.path.join(out, "culvert_precheck_index.json")))
        assert [i["status"] for i in idx] == ["checked", "no structures layer"]
        # refuses to write into the input folder
        assert cp.main(["--dam-folder", inp, "--out", inp]) == 2
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_precheck_cli_subcommand_runs_as_a_script():
    """`python ndbreach_cli.py culvert-precheck --structures ... --fine-dem ...` end to end."""
    import subprocess
    import sys
    try:
        _gdal()
    except ImportError:
        return
    cli = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts",
                       "ndbreach_cli.py")
    d, inp = _setup()
    try:
        out = os.path.join(d, "out")
        r = subprocess.run([sys.executable, cli, "culvert-precheck", "--structures",
                            os.path.join(inp, "structures_T.gpkg"), "--fine-dem",
                            os.path.join(inp, "fine_1m.tif"), "--dem-z", "meters", "--dam", "T",
                            "--out", out], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        assert r.returncode == 0, r.stderr
        assert "HIGH 2 ends (1 culverts); FALLBACK 2 ends (1 culverts); skipped 1" in r.stdout, r.stdout
        assert os.path.isfile(os.path.join(out, "culvert_precheck_T.csv"))
        r = subprocess.run([sys.executable, cli, "culvert-precheck", "--out", out],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        assert r.returncode == 2
        r = subprocess.run([sys.executable, cli, "hydrograph", "x.json", "--bogus"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        assert r.returncode == 2 and "unrecognized arguments" in r.stderr
    finally:
        shutil.rmtree(d, ignore_errors=True)
