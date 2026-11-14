"""Finder runs on files (GDAL / OGR + numpy + scipy; no QGIS): a synthetic valley written as a
GeoTIFF with a dam, a dry embankment and a road fill, stream / road / known-structure layers in
another CRS, and the three runs end to end.

    python -m <plugin folder>.tests.test_finder
"""
import math
import os
import shutil
import tempfile
import unittest

import numpy as np

try:
    from osgeo import gdal, ogr, osr
except ImportError:                                   # pragma: no cover
    gdal = None

from . import test_core as tc

X0, Y0 = 100000.0, 2700000.0                          # EPSG:5070 origin of the synthetic grid


def srs(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    if hasattr(s, "SetAxisMappingStrategy"):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def write_dem(path, dem, feet=False, tag=True, srs_epsg=5070):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, dem.shape[1], dem.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, tc.CELL, 0.0, Y0 + dem.shape[0] * tc.CELL, 0.0, -tc.CELL))
    if srs_epsg is not None:
        ds.SetProjection(srs(srs_epsg).ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(np.where(np.isfinite(dem), dem / (0.3048 if feet else 1.0), -9999.0))
    if tag:
        b.SetUnitType("ft" if feet else "m")
    ds = None


def write_layer(path, name, geoms, epsg=4326, fields=()):
    """geoms: [(wkt in EPSG:5070, {field: value})]; written in `epsg`."""
    drv = ogr.GetDriverByName("GPKG")
    ds = ogr.Open(path, 1) if os.path.exists(path) else drv.CreateDataSource(path)
    lyr = ds.CreateLayer(name, srs(epsg), ogr.wkbUnknown)
    for f in fields:
        lyr.CreateField(ogr.FieldDefn(f, ogr.OFTString))
    ct = osr.CoordinateTransformation(srs(5070), srs(epsg))
    for wkt, attrs in geoms:
        g = ogr.CreateGeometryFromWkt(wkt)
        g.Transform(ct)
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(g)
        for k, v in attrs.items():
            f.SetField(k, v)
        lyr.CreateFeature(f)
    ds = None
    return "{}|layername={}".format(path, name)


def gx(col):
    return X0 + col


def gy(row_from_top_m):
    return Y0 + tc.N * tc.CELL - row_from_top_m


def row_y(row):
    return gy((row + 0.5) * tc.CELL)


@unittest.skipIf(gdal is None, "no GDAL (osgeo) in this Python")
class Runs(unittest.TestCase):
    def setUp(self):
        from .. import finder
        self.f = finder
        self.tmp = tempfile.mkdtemp(prefix="finder_")
        dem, _road = tc.scene()
        self.dem = os.path.join(self.tmp, "dem.tif")
        write_dem(self.dem, dem)
        self.vec = os.path.join(self.tmp, "in.gpkg")
        x = gx(tc.XS)
        self.streams = write_layer(self.vec, "streams", [
            ("LINESTRING ({} {}, {} {})".format(x, gy(1.0), x, gy(tc.N * tc.CELL - 1.0)), {})])
        self.roads = write_layer(self.vec, "roads", [
            ("LINESTRING ({} {}, {} {})".format(gx(0), row_y(320), gx(tc.N * tc.CELL), row_y(320)), {})],
            epsg=26914)
        # the dry embankment is on the DWR list (60 ft off), the dam (590 ft away) is not; an NBI
        # culvert at the road
        self.known = write_layer(self.vec, "dwr_dams", [
            ("POINT ({} {})".format(x + 18.0, row_y(260)), {"structure_id": "4031"})],
            fields=("structure_id",))
        self.culverts = write_layer(self.vec, "nbi", [
            ("POINT ({} {})".format(x + 10.0, row_y(320)), {"STRUCTURE_NUMBER_008": "9117181"})],
            fields=("STRUCTURE_NUMBER_008",))
        self.out = os.path.join(self.tmp, "out.gpkg")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def emb(self, **kw):
        return self.f.run_embankments(self.dem, self.streams, self.out, roads=self.roads,
                                      known=[(self.known, "DWR dams", None)], match_ft=200.0,
                                      log=lambda *_: None, **kw)

    def test_embankments(self):
        res = self.emb()
        by = {r["kind"]: r for r in res["rows"]}
        self.assertEqual(set(by), {"dam", "dry embankment", "road fill"})
        self.assertEqual(by["dam"]["status"], "new")
        self.assertEqual(by["dry embankment"]["status"], "listed")
        self.assertEqual(by["dry embankment"]["known_id"], "4031")
        self.assertAlmostEqual(by["dam"]["height_ft"], 4.0 / 0.3048, delta=1.0)
        self.assertEqual(by["road fill"]["on_road"], "yes")
        self.assertTrue(os.path.exists(res["csv"]))
        ds = ogr.Open(self.out)
        self.assertEqual(ds.GetLayerByName("embankments").GetFeatureCount(), 3)
        # Screening hand-off: the dam (new); never a road fill; the listed one is left out
        sd = ds.GetLayerByName("screening_dams")
        self.assertEqual(sd.GetFeatureCount(), 1)
        f = sd.GetNextFeature()
        self.assertEqual(f.GetField("status"), "Candidate")
        self.assertTrue(f.GetField("structure_id").startswith("FIND-"))
        self.assertGreater(f.GetField("max_pool_vol"), 50.0)
        info = {f.GetField("key"): f.GetField("value") for f in ds.GetLayerByName("run_info")}
        self.assertEqual(info["tool"], "embankments")
        self.assertEqual(info["finder_version"], self.f.VERSION)
        self.assertEqual(info["match_ft"], "200.0")
        self.assertEqual(info["cancelled"], "False")
        self.assertIn("min_height_m=", info["params"])
        self.assertTrue(any(k.startswith("sha256_") for k in info), info)
        f = ds.GetLayerByName("embankments").GetNextFeature()
        # rewind: at least one is_new field is yes or no
        ds2 = ogr.Open(self.out)
        vals = []
        lyr = ds2.GetLayerByName("embankments")
        for feat in lyr:
            vals.append(feat.GetField("is_new"))
        self.assertTrue(vals and all(v in ("yes", "no") for v in vals), vals)
        self.assertIn("yes", vals)
        hs = gdal.Open(res["hillshade"])
        self.assertEqual(hs.GetRasterBand(1).DataType, gdal.GDT_Byte)
        self.assertGreater(hs.GetRasterBand(1).ComputeRasterMinMax(False)[1], 0)

    def test_tiles_give_the_same_answer(self):
        one = sorted((r["find_id"], r["kind"]) for r in self.emb()["rows"])
        many = sorted((r["find_id"], r["kind"]) for r in self.emb(tile_m=300.0, hillshade=False)["rows"])
        self.assertEqual(one, many)

    def test_dem_in_feet(self):
        dem, _r = tc.scene()
        write_dem(self.dem, dem, feet=True)
        by = {r["kind"]: r for r in self.emb()["rows"]}
        self.assertAlmostEqual(by["dam"]["height_ft"], 4.0 / 0.3048, delta=1.0)

    def test_a_dem_with_no_unit_is_refused(self):
        dem, _road = tc.scene()
        # EPSG:5070, no Z tag: the horizontal CRS unit is metres (with a confirm warning)
        bare = os.path.join(self.tmp, "bare.tif")
        write_dem(bare, dem, tag=False)
        in_feet, how = self.f.dem_unit(bare)
        self.assertFalse(in_feet)
        self.assertIn("horizontal CRS", how)
        # the same values with the unit chosen: run, and the choice is logged
        logs = []
        self.f.run_embankments(bare, self.streams, self.out, z_feet=False, hillshade=False, log=logs.append)
        self.assertTrue(any("metres (your choice)" in str(m) for m in logs))
        # a USGS 3DEP file records no unit either but is metres
        usgs = os.path.join(self.tmp, "USGS_1M_14_x99y999_TEST.tif")
        write_dem(usgs, dem, tag=False)
        self.assertEqual(self.f.dem_unit(usgs), (False, "USGS 3DEP files (metres)"))
        # a wrong choice: ND ground (300 m) read as feet is 91 m, below any ND ground -> a warning
        nd = os.path.join(self.tmp, "nd.tif")
        write_dem(nd, np.full((20, 20), 300.0), tag=False)
        logs = []
        self.f.check_dem_unit(nd, True, logs.append)
        self.assertTrue(any("WARNING" in str(m) for m in logs), logs)
        logs = []
        self.f.check_dem_unit(nd, False, logs.append)
        self.assertFalse(any("WARNING" in str(m) for m in logs), logs)
        # no CRS and an ambiguous typical value (800 fits metres and feet): still refused
        none = os.path.join(self.tmp, "nogeo.tif")
        write_dem(none, np.full((20, 20), 1200.0), tag=False, srs_epsg=None)
        with self.assertRaises(RuntimeError) as cm:
            self.f.dem_unit(none)
        self.assertIn("DEM elevation unit", str(cm.exception))
        # value heuristic: 2,000 looks like feet
        ftish = os.path.join(self.tmp, "ftish.tif")
        write_dem(ftish, np.full((20, 20), 2000.0), tag=False, srs_epsg=None)
        in_feet, how = self.f.dem_unit(ftish)
        self.assertTrue(in_feet)
        self.assertIn("looks like feet", how)

    def test_cancel_stops_and_says_so(self):
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 1                   # after the run has started
        res = self.emb(cancelled=cancelled, tile_m=100.0, hillshade=False)
        self.assertTrue(res["cancelled"])
        res = self.f.run_crossings(self.roads, self.streams, os.path.join(self.tmp, "x.gpkg"), dem=self.dem,
                                   log=lambda *_: None, cancelled=lambda: True)
        self.assertTrue(res["cancelled"])
        self.assertTrue(all(r.get("pipe_in") is None for r in res["rows"]))

    def test_ponds(self):
        pool = tc.bed(200) + 3.0
        dem, _r = tc.scene()
        cols = np.where(np.abs(dem[150] - pool) < 1e-6)[0]          # the flat pool on row 150
        x0, x1 = gx(cols.min() * tc.CELL), gx((cols.max() + 1) * tc.CELL)
        y0, y1 = row_y(196) - 1.5, row_y(150)
        wkt = "POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(a=x0, b=x1, c=y0, d=y1)
        wb = write_layer(self.vec, "waterbodies", [(wkt, {"ftype": "LakePond"})], fields=("ftype",))
        res = self.f.run_ponds(wb, self.dem, self.out, known=[self.known], cell=3.0, max_ac=0,
                               type_field="ftype", log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1, res)
        r = res["rows"][0]
        self.assertEqual(r["kind"], "embankment pond")
        self.assertEqual(r["status"], "new")
        self.assertTrue(r["find_id"].startswith("POND-"))

    def test_ponds_pool_from_the_water_elev_mosaic(self):
        pool = tc.bed(200) + 3.0
        dem, _r = tc.scene()
        cols = np.where(np.abs(dem[150] - pool) < 1e-6)[0]
        x0, x1 = gx(cols.min() * tc.CELL), gx((cols.max() + 1) * tc.CELL)
        y0, y1 = row_y(196) - 1.5, row_y(150)
        wkt = "POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(a=x0, b=x1, c=y0, d=y1)
        wb = write_layer(self.vec, "waterbodies", [(wkt, {"ftype": "LakePond"})], fields=("ftype",))
        # a class-9 water-surface mosaic over the whole scene at a known elevation
        we = os.path.join(self.tmp, "water_elev.tif")
        from osgeo import gdal
        g = gdal.Open(self.dem)
        arr = np.full((g.RasterYSize, g.RasterXSize), pool + 0.4, np.float32)   # 0.4 m above the DEM pool
        drv = gdal.GetDriverByName("GTiff")
        ods = drv.Create(we, g.RasterXSize, g.RasterYSize, 1, gdal.GDT_Float32)
        ods.SetGeoTransform(g.GetGeoTransform()); ods.SetProjection(g.GetProjection())
        ob = ods.GetRasterBand(1); ob.WriteArray(arr); ob.SetNoDataValue(-999999.0); ods = None
        res = self.f.run_ponds(wb, self.dem, self.out, cell=3.0, max_ac=0, type_field="ftype",
                               water_elev=we, log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1, res)
        self.assertIn("measured LiDAR water surface", res["rows"][0]["pool_method"])

    def test_crossings_with_fills(self):
        self.emb()
        res = self.f.run_crossings(self.roads, self.streams, self.out, known=[self.culverts],
                                   fills=self.out + "|layername=embankments", log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1)
        r = res["rows"][0]
        self.assertEqual((r["status"], r["known_id"], r["fill_kind"]), ("listed", "9117181", "road fill"))
        res = self.f.run_crossings(self.roads, self.streams, self.out, log=lambda *_: None)
        self.assertEqual(res["rows"][0]["status"], "new")


    def test_fills_at_the_edge_of_the_terrain_data(self):
        # Finder 0.7: the whole scene first - the end of the DEM file is missing terrain too (600 m
        # east and west of the valley line, nearer for the two fills towards its south end), all
        # over 150 m off: the kinds are as ever and edge_ft says how far
        res = self.emb(hillshade=False)
        by = {r["kind"]: r["edge_ft"] for r in res["rows"]}
        self.assertEqual(sorted(by), ["dam", "dry embankment", "road fill"])
        self.assertAlmostEqual(by["dam"], 600.0 / 0.3048, delta=30.0)
        self.assertTrue(all(v > 150.0 / 0.3048 for v in by.values()), by)
        self.assertEqual(res["counts"]["data_edge"], 0)
        # the east part of the DEM is NoData from 90 m east of the valley line: every fill is kept, as
        # "at the edge of the terrain data", and none is a candidate
        from . import test_core as c
        rows = c.Score().rows()
        for i, r in enumerate(rows):
            r.update({"x": X0 + 100.0 * i, "y": Y0, "find_id": "FIND-{}".format(i), "kind": "dam", "lat": 0.0, "lon": 0.0})
        listed = os.path.join(self.tmp, "listed.gpkg")
        self.f.write_points(listed, "embankments", self.f.EMB_FIELDS, rows)
        model = self.f.run_score([listed + "|layername=embankments"], os.path.join(self.tmp, "s.gpkg"),
                                 log=lambda *_: None)
        dem, _road = tc.scene()
        dem[:, 230:] = np.nan
        write_dem(self.dem, dem)
        said = []
        res = self.f.run_embankments(self.dem, self.streams, self.out, roads=self.roads, model=model["model_path"],
                                     hillshade=False, log=said.append)
        self.assertEqual([r["kind"] for r in res["rows"]], [self.f.em.K_EDGE] * 3)
        self.assertEqual(res["counts"]["data_edge"], 3)
        self.assertTrue(any("3 of them at the edge of the terrain data" in m for m in said), said)
        for r in res["rows"]:
            self.assertAlmostEqual(r["edge_ft"], 90.0 / 0.3048, delta=12.0)
            self.assertTrue(r["notes"].startswith("at the edge of the terrain data: missing terrain acts as a wall here"))
            self.assertIsNone(r["score"])
            self.assertEqual(r["score_note"], "not scored: at the edge of the terrain data")
        self.assertEqual({r["notes"].split("it would otherwise read as: ")[1].split(";")[0] for r in res["rows"]},
                         {"dam", "dry embankment", "road fill"})
        ds = ogr.Open(self.out)
        lyr = ds.GetLayerByName("embankments")
        self.assertEqual(lyr.GetFeatureCount(), 3)
        self.assertGreaterEqual(lyr.GetLayerDefn().GetFieldIndex("edge_ft"), 0)
        self.assertEqual(ds.GetLayerByName("screening_dams").GetFeatureCount(), 0)     # nothing for Screening
        ds = None
        fills = self.out + "|layername=embankments"
        with self.assertRaises(RuntimeError):                                          # nothing for a review batch
            self.f.run_review_batch(fills, os.path.join(self.tmp, "rb.gpkg"), n=3, log=lambda *_: None)
        # the score tool does not learn from them and does not score them
        said = []
        sc = self.f.run_score([listed + "|layername=embankments", fills], os.path.join(self.tmp, "s2.gpkg"),
                              log=said.append)
        self.assertTrue(any("3 fill(s) at the edge of the terrain data: not learned from and not scored" in m for m in said))
        self.assertEqual(sc["model"]["n_listed"] + sc["model"]["n_other"], len(rows))
        self.assertEqual([r["score"] for r in sc["rows"][-3:]], [None] * 3)
        self.assertTrue(all(r["score"] is not None for r in sc["rows"][:-3]))
        # the distance is yours: 0 = the rule is off, and the kinds are as in 0.6
        res = self.f.run_embankments(self.dem, self.streams, self.out, roads=self.roads, hillshade=False,
                                     params=self.f.em.FinderParams(edge_m_max=0.0), log=lambda *_: None)
        self.assertEqual(sorted(r["kind"] for r in res["rows"]), ["dam", "dry embankment", "road fill"])
        self.assertTrue(all(r["edge_ft"] is None for r in res["rows"]))

    def test_a_window_under_the_edge_distance_is_warned_of(self):
        # the edge rule sees a fill's pool only inside the spill-test window: a script that sets
        # window_m under edge_m_max is told so (the defaults, 1200 m and 150 m, are not)
        said = []
        self.f.run_embankments(self.dem, self.streams, self.out, hillshade=False, log=said.append,
                               params=self.f.em.FinderParams(window_m=100.0))
        self.assertEqual(sum(1 for m in said if "is under edge_m_max" in m), 1)
        said = []
        self.f.run_embankments(self.dem, self.streams, self.out, hillshade=False, log=said.append)
        self.assertFalse(any("is under edge_m_max" in m for m in said))

    def test_known_ids_of_a_sarp_layer(self):
        # a SARP barrier layer has SARPID on every row, NIDID on some and a Name on most: a row with
        # an NID id gets it (what people look a dam up by), every other row its SARPID (field names
        # in any case), never the name; other layers are as before
        path = os.path.join(self.tmp, "known.gpkg")
        x, y = gx(100.0), gy(100.0)
        sarp = write_layer(path, "sarp", [
            ("POINT ({} {})".format(x, y), {"Name": "Wetland 2", "SARPID": "ND55127"}),
            ("POINT ({} {})".format(x + 50, y), {"Name": "Big Dam", "SARPID": "ND00012", "NIDID": "ND00345"}),
            ("POINT ({} {})".format(x + 100, y), {"Name": "No id here"})], fields=("Name", "SARPID", "NIDID"))
        lower = write_layer(path, "sarp_lower", [("POINT ({} {})".format(x, y), {"name": "Pond", "sarpid": "ND7"})],
                            fields=("name", "sarpid"))
        nid = write_layer(path, "nid", [("POINT ({} {})".format(x, y), {"NAME": "Big Dam", "NIDID": "ND00345"})],
                          fields=("NAME", "NIDID"))
        ids = lambda src, idf=None: [p[2] for p in self.f.read_known([(src, "layer", idf)]).pts]
        self.assertEqual(ids(sarp), ["ND55127", "ND00345", "No id here"])
        self.assertEqual(ids(lower), ["ND7"])
        self.assertEqual(ids(nid), ["ND00345"])
        self.assertEqual(ids(sarp, "SARPID")[1], "ND00012")         # the field you name comes first
        # two SARP-estimated dams with the same name and no NID id: two ids, not one
        est = write_layer(path, "sarp_est", [
            ("POINT ({} {})".format(x, y), {"Name": "Wetland 2", "SARPID": "ND90001"}),
            ("POINT ({} {})".format(x + 50, y), {"Name": "Wetland 2", "SARPID": "ND90002"})],
            fields=("Name", "SARPID", "NIDID"))
        self.assertEqual(ids(est), ["ND90001", "ND90002"])

    def test_dem_drainage_alone_finds_the_fills(self):
        res = self.f.run_embankments(self.dem, None, self.out, roads=self.roads,
                                     known=[(self.known, "DWR dams", None)], match_ft=200.0,
                                     drainage_ac=2.0, drain_cell=6.0, hillshade=False, log=lambda *_: None)
        kinds = sorted(r["kind"] for r in res["rows"])
        self.assertEqual(kinds, ["dam", "dry embankment", "road fill"])
        self.assertTrue(all(r["line_source"] == "DEM drainage" for r in res["rows"]))
        ds = ogr.Open(self.out)
        self.assertEqual(ds.GetLayerByName("spillways").GetFeatureCount(), 3)

    def test_pool_lines_find_a_pond_no_draw_reaches(self):
        # a draw threshold far above anything on the grid: no draw at all, but the dam's pool has
        # its own line out through its outlet (Finder 0.6); the dry embankment and the road fill
        # hold no pool, so nothing finds them
        # the scene with its pool 2 m deep at the dam, so the pool ends on the valley floor inside
        # the grid (a closed depression; the scene's 3 m pool runs off the top of the grid)
        dem, y = tc.valley()
        dem = tc.add_fill(dem, y, 200, 4.0)
        dem = tc.add_fill(dem, y, 260, 2.5)
        dem = tc.add_fill(dem, y, 320, 2.5, half=2)
        lvl = tc.bed(200) + 2.0
        dem = np.where((y < 197) & (dem < lvl), lvl, dem)
        self.dem = os.path.join(self.tmp, "dem_closed.tif")
        write_dem(self.dem, dem)
        kw = dict(roads=self.roads, known=[(self.known, "DWR dams", None)], match_ft=200.0,
                  drainage_ac=1e6, drain_cell=6.0, hillshade=False, log=lambda *_: None)
        none = self.f.run_embankments(self.dem, None, self.out, pool_lines=False, **kw)
        self.assertEqual(none["rows"], [])
        res = self.f.run_embankments(self.dem, None, self.out, pool_max_drain_ac=None, **kw)
        self.assertEqual([r["kind"] for r in res["rows"]], ["dam"])
        r = res["rows"][0]
        self.assertEqual(r["line_source"], "DEM pool outlet")
        self.assertEqual(r["rescued"], "no")
        self.assertEqual(res["counts"]["pool_outlet"], 1)
        self.assertIn("line out of the pool", r["notes"])
        # the 50 ac cap: this pool drains the whole valley above it, so the draws are left to it
        capped = self.f.run_embankments(self.dem, None, self.out, **kw)
        self.assertEqual(capped["rows"], [])
        # with the draws on, the draw's reading of the dam is kept, not the pool line's
        both = self.f.run_embankments(self.dem, None, self.out, roads=self.roads,
                                      known=[(self.known, "DWR dams", None)], match_ft=200.0,
                                      drainage_ac=2.0, drain_cell=6.0, hillshade=False,
                                      pool_max_drain_ac=None, log=lambda *_: None)
        self.assertEqual(sorted(r["kind"] for r in both["rows"]), ["dam", "dry embankment", "road fill"])
        self.assertTrue(all(r["line_source"] == "DEM drainage" for r in both["rows"]))

    def test_a_small_pond_is_found_wherever_the_routing_cells_fall(self):
        # 324 m2 of flat water on a hillside behind its own fill, no draw near it (Finder 0.7).
        # It is a flat water surface and a closed depression at once; on most placements both seeds
        # fall in one 10 m routing cell and only the depression starts a line - which still comes
        # out of the flat water. Found on every placement; not with the 0.6 seed size.
        z, _wat, _lvl = tc.hill_pond(6)
        kw = dict(drainage_ac=1e6, hillshade=False, log=lambda *_: None)
        global X0
        keep = X0
        try:
            for dx in (0.0, 3.0, 6.0, 9.0, 12.0):
                X0 = keep + dx                                # the DEM moved over the 10 m cells
                dem = os.path.join(self.tmp, "pond{:.0f}.tif".format(dx))
                write_dem(dem, z)
                rows = self.f.run_embankments(dem, None, self.out, **kw)["rows"]
                self.assertEqual([(r["kind"], r["line_source"]) for r in rows], [("dam", "DEM pool outlet")], dx)
                self.assertLess(rows[0]["area_ac"] * 4046.86, 400.0)
                self.assertIn("small pond", rows[0]["notes"])
                old = self.f.run_embankments(dem, None, self.out, pool_opts={"flat_min_m2": 300.0}, **kw)["rows"]
                self.assertEqual(old, [], dx)
        finally:
            X0 = keep

    def test_near10_is_the_points_cell(self):
        # the fill's position on the 10 m routing grid: the cell holding it, radius in cells
        g = (1000.0, 10.0, 0.0, 2000.0, 0.0, -10.0)
        self.assertEqual(self.f._near10((1015.0, 1985.0, 65.0), g), (1.0, 1.0, 6.5))
        self.assertEqual(self.f._near10((1035.0, 1985.0, 65.0), g), (1.0, 3.0, 6.5))
        self.assertIsNone(self.f._near10(None, g))
        self.assertEqual(self.f._cells10([1015.0], [1985.0], g, (5, 5)), [6])
        # with a share: a 10 m cell a pool's 3 m cells only brush (1 of ~11) is left out, 5 is kept
        xs = [1015.0] * 5 + [1025.0]
        ys = [1985.0] * 5 + [1985.0]
        self.assertEqual(self.f._cells10(xs, ys, g, (5, 5)), [6, 7])
        self.assertEqual(self.f._cells10(xs, ys, g, (5, 5), 0.4, 3.0), [6])

    def test_a_held_pool_counts_only_the_water_leaving_by_the_fill(self):
        # the shipped chain (_cells10 + _near10 + pool_inflow) on a 3 x 7 grid: a pool on row 1,
        # cols 1-5, whose water leaves by col 1 (5 m2 over it) and by col 5 (7 m2); a fill at col 5
        # gets 7, one at col 1 gets 5, no fill (or a wide radius) gets both
        g = (1000.0, 10.0, 0.0, 2030.0, 0.0, -10.0)
        rec = -np.ones(21, np.int64)
        rec[8], rec[9], rec[10], rec[11], rec[12] = 1, 8, 11, 12, 19
        area = np.zeros((3, 7))
        area[1, 1], area[1, 5] = 5.0, 7.0
        xs = [1000.0 + (c + 0.5) * 10.0 for c in range(1, 6) for _k in range(5)]
        ys = [2015.0] * len(xs)
        held = lambda near: self.f._held_inflow(xs, ys, near, rec, area, g, 3.0)
        self.assertEqual(held((1055.0, 2015.0, 5.0)), 7.0)
        self.assertEqual(held((1015.0, 2015.0, 5.0)), 5.0)
        self.assertEqual(held(None), 12.0)
        self.assertEqual(held((1035.0, 2015.0, 100.0)), 12.0)
        self.assertEqual(held((1035.0, 2015.0, 5.0)), 0.0)

    def test_a_pond_beside_a_big_draw_keeps_its_own_drainage(self):
        # a hillside pond (2.3 ac of water) whose west berm stands 16 m from the valley's draw (80 ac),
        # which runs beside it and never crosses it: only its pool line finds it, and the 50 ac cap
        # reads the pool line's own path and the ground draining into the pool, not the draw beside it
        dem, y = tc.valley()
        x = np.mgrid[0:tc.N, 0:tc.N][1]
        lvl = tc.bed(165) + 1.0
        dem = np.where((y >= 150) & (y <= 180) & (x >= 207) & (x <= 240), lvl, dem)
        berm = ((y >= 148) & (y <= 182) & (x >= 205) & (x <= 206)) | \
            ((((y >= 148) & (y <= 149)) | ((y >= 181) & (y <= 182))) & (x >= 205) & (x <= 240))
        dem = np.where(berm, np.maximum(dem, lvl + 1.2), dem)
        path = os.path.join(self.tmp, "dem_hillside.tif")
        write_dem(path, dem)
        res = self.f.run_embankments(path, None, self.out, drainage_ac=20.0, hillshade=False, log=lambda *_: None)
        self.assertEqual([(r["kind"], r["line_source"]) for r in res["rows"]], [("dam", "DEM pool outlet")])
        d = res["rows"][0]["drain_ac"]
        self.assertGreater(d, 31 * 34 * 9.0 / 4046.86)                 # at least the pond itself
        self.assertLess(d, 50.0)
        # a scour hole in the draw's bed beside the berm is a pool of its own (the draws walk it): the
        # pond's drainage is still its own, not the draw's
        hole = (y >= 160) & (y <= 170) & (x >= 197) & (x <= 204)
        write_dem(path, np.where(hole, tc.bed(y) - 0.3, dem))
        res = self.f.run_embankments(path, None, self.out, drainage_ac=20.0, hillshade=False, log=lambda *_: None)
        self.assertEqual([(r["kind"], r["line_source"]) for r in res["rows"]], [("dam", "DEM pool outlet")])
        self.assertAlmostEqual(res["rows"][0]["drain_ac"], d, delta=1.0)

    def test_ridges_and_dugouts_layers(self):
        res = self.f.run_embankments(self.dem, self.streams, self.out, roads=self.roads, ridges=True,
                                     dugouts=True, hillshade=False, log=lambda *_: None)
        ds = ogr.Open(self.out)
        self.assertIsNotNone(ds.GetLayerByName("ridges"))
        self.assertIsNotNone(ds.GetLayerByName("dugouts"))
        # the three fills are fills, not also ridges
        self.assertEqual(len(res["ridges"]), 0, res["ridges"])

    def test_crossing_pipe_size(self):
        res = self.f.run_crossings(self.roads, self.streams, self.out, dem=self.dem, drain_cell=6.0,
                                   log=lambda *_: None)
        r = res["rows"][0]
        self.assertGreater(r["drainage_ac"], 10.0)
        self.assertAlmostEqual(r["q_cfs"], 0.35 * 2.0 * r["drainage_ac"], delta=0.2)
        self.assertTrue(r["pipe_size"].endswith("in round") or r["pipe_size"].startswith("box"))

    def test_nwi_codes(self):
        self.assertEqual(self.f.nwi_modifiers("PUBHx"), "x")
        self.assertEqual(self.f.nwi_modifiers("L1UBHh"), "h")
        self.assertEqual(self.f.nwi_modifiers("PEM1/SS1Cd"), "d")
        self.assertEqual(self.f.nwi_modifiers("PEM1C"), "")
        self.assertEqual(self.f.nwi_modifiers("excavated"), "")

    def test_osm_structures(self):
        osm = os.path.join(self.tmp, "t.osm")
        with open(osm, "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">'
                     '<node id="1" lat="47.0" lon="-97.4" version="1"><tag k="waterway" v="dam"/>'
                     '<tag k="name" v="Test Dam"/></node>'
                     '<node id="2" lat="47.001" lon="-97.401" version="1"/>'
                     '<node id="3" lat="47.002" lon="-97.401" version="1"/>'
                     '<node id="4" lat="47.03" lon="-97.42" version="1"><tag k="amenity" v="school"/></node>'
                     '<way id="10" version="1"><nd ref="2"/><nd ref="3"/><tag k="tunnel" v="culvert"/>'
                     '<tag k="waterway" v="stream"/></way></osm>')
        src = self.f.osm_structures(osm, self.out, log=lambda *_: None)
        rows = self.f.read_rows(src)
        self.assertEqual(sorted(r["kind"] for r in rows), ["culvert", "dam"])
        kn = self.f.read_known([src])
        self.assertEqual(len(kn), 2)
        # a dam list with a status field: each point's status rides along (known_state)
        dams = write_layer(os.path.join(self.tmp, "dams.gpkg"), "dams",
                           [("POINT ({} {})".format(X0, Y0), {"structure_id": "20061", "status": "Breached"}),
                            ("POINT ({} {})".format(X0 + 900.0, Y0), {"structure_id": "20071", "status": "Intact"})],
                           fields=("structure_id", "status"))
        kn = self.f.read_known([(dams, "DWR dams", None)])
        self.assertEqual(sorted(zip(kn.state, (p[2] for p in kn.pts))), [("Breached", "20061"), ("Intact", "20071")])

    def test_score_run(self):
        from . import test_core as c
        rows = c.Score().rows()
        for i, r in enumerate(rows):
            r.update({"x": X0 + 100.0 * i, "y": Y0, "find_id": "FIND-{}".format(i), "kind": "dam",
                      "lat": 0.0, "lon": 0.0})
        self.f.write_points(self.out, "embankments", self.f.EMB_FIELDS, rows)
        res = self.f.run_score([self.out + "|layername=embankments"], os.path.join(self.tmp, "s.gpkg"),
                               log=lambda *_: None)
        self.assertGreater(res["cv_auc"], 0.8)
        self.assertTrue(os.path.exists(res["model_path"]))
        res2 = self.f.run_embankments(self.dem, self.streams, self.out, model=res["model_path"],
                                      hillshade=False, log=lambda *_: None)
        self.assertTrue(all(0.0 <= r["score"] <= 1.0 for r in res2["rows"]))
        self.assertTrue(all("score_note" in r for r in res2["rows"]))
        # the 0.5 kinds, with reviewers' marks as trusted labels (self.out now holds the run above)
        self.f.write_points(self.out, "embankments", self.f.EMB_FIELDS, rows)
        marks = os.path.join(self.tmp, "marks.csv")
        with open(marks, "w", encoding="utf-8-sig") as fh:          # a spreadsheet's BOM
            fh.write("find_id,mark\nFIND-0,no\nFIND-1,yes\nFIND-2,unsure\nFIND-9999,yes\n")
        for kind in ("0.5", "0.5-terrain", "0.5-trees"):
            r3 = self.f.run_score([self.out + "|layername=embankments"], os.path.join(self.tmp, kind + ".gpkg"),
                                  kind=kind, marks=marks, log=lambda *_: None)
            # the unknown id is left out
            self.assertEqual(r3["model"]["trusted_labels"], 3)
            self.assertIn("ag_head", r3["model"]["features"])
            self.assertGreater(r3["cv_auc"], 0.7)
        with self.assertRaises(RuntimeError):
            self.f.run_score([self.out + "|layername=embankments"], os.path.join(self.tmp, "x.gpkg"), kind="v9",
                             log=lambda *_: None)
        # a marks file with no matching card says so; one without the columns, or missing, raises
        said = []
        none_ = os.path.join(self.tmp, "none.csv")
        with open(none_, "w") as fh:
            fh.write("find_id,mark\nFIND-9999,yes\n")
        self.f.run_score([self.out + "|layername=embankments"], os.path.join(self.tmp, "n.gpkg"), kind="0.5",
                         marks=none_, log=said.append)
        self.assertTrue(any("none of the 1 mark" in m for m in said))
        bad = os.path.join(self.tmp, "bad.csv")
        with open(bad, "w") as fh:
            fh.write("id,verdict\nFIND-1,yes\n")
        for m in (bad, os.path.join(self.tmp, "nowhere.csv")):
            with self.assertRaises(RuntimeError):
                self.f.run_score([self.out + "|layername=embankments"], os.path.join(self.tmp, "b.gpkg"),
                                 kind="0.5", marks=m, log=lambda *_: None)

    # -------------------------------------------------------------------------------- water --
    def write_image(self, path, epsg=5070):
        g, nir, _pond = tc.image()
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(path, g.shape[1], g.shape[0], 4, gdal.GDT_Byte)
        ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + g.shape[0], 0.0, -1.0))
        ds.SetProjection(srs(epsg).ExportToWkt())
        for b, a in zip((1, 2, 3, 4), (g, g, g, nir)):
            ds.GetRasterBand(b).WriteArray(a.astype(np.uint8))
        ds = None
        return path

    def pond_layer(self, half=False):
        # a mapped water body over the north half of the pond (half=True) or all of it
        cx, cy = X0 + 150.0, Y0 + 150.0
        y0 = cy if half else cy - 45.0
        wkt = "POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(
            a=cx - 45.0, b=cx + 45.0, c=y0, d=cy + 45.0)
        return write_layer(self.vec, "wb_half" if half else "wb", [(wkt, {})])

    def test_water_from_a_file(self):
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        logs = []
        res = self.f.run_water(img, self.out, cell=1.0, green_band=2, nir_band=4, log=logs.append)
        if len(res["rows"]) != 1:                     # say why (GDAL / numpy versions differ)
            box = self.f._image_box(img)
            bands, gt = self.f.read_bands(img, box, 1.0, (2, 4))
            from ..core import water as wt
            idx = wt.ndwi(bands[2], bands[4])
            logs.append("box {} gt {} shape {} green {} nir {} ndwi>=0.15 {} dark {} patches {}".format(
                box, gt, idx.shape, np.nanpercentile(bands[2], [1, 50, 99]),
                np.nanpercentile(bands[4], [1, 50, 99]), int(np.nansum(idx >= 0.15)),
                int(wt.dark_in_nir(bands[4], idx).sum()),
                wt.water_patches(idx, 0.15, 1, 50, 10.0, wt.dark_in_nir(bands[4], idx))[1]))
        self.assertEqual(len(res["rows"]), 1, "\n".join(str(x) for x in logs))
        r = res["rows"][0]
        self.assertAlmostEqual(r["area_ac"], math.pi * 40 ** 2 / 4046.8564224, delta=0.06)
        self.assertEqual(r["mapped"], "no")
        self.assertTrue(r["water_id"].startswith("WATER-"))
        ds = ogr.Open(self.out)
        self.assertEqual(ds.GetLayerByName("water").GetFeatureCount(), 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "out_water.csv")))

    def test_water_from_a_las_tile(self):
        # class-9 (water) points over a 40 m square at the pond centre, class-2 ground elsewhere,
        # written at the grid's EPSG:5070 coords; run_water should find exactly that one patch
        cx, cy = X0 + 150.0, Y0 + 150.0
        wx, wy = np.meshgrid(np.arange(cx - 20, cx + 20, 1.0), np.arange(cy - 20, cy + 20, 1.0))
        gx = np.linspace(X0 + 10, X0 + 290, 80)
        gy = np.full(80, Y0 + 280.0)
        x = np.concatenate([wx.ravel(), gx])
        y = np.concatenate([wy.ravel(), gy])
        cls = np.concatenate([np.full(wx.size, 9, np.uint8), np.full(80, 2, np.uint8)])
        path = os.path.join(self.tmp, "tile.las")
        tc.write_las(path, x, y, cls)
        res = self.f.run_water(path, self.out, cell=1.0, min_ac=0.02, min_width_m=3.0,
                               las_src_epsg=5070, log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1)
        self.assertAlmostEqual(res["rows"][0]["area_ac"], 40 * 40 / 4046.8564224, delta=0.05)
        ds = ogr.Open(self.out)
        self.assertEqual(ds.GetLayerByName("water").GetFeatureCount(), 1)

    def test_water_from_a_count_raster(self):
        # a prebuilt class-9 water-count raster (band 1): a 40 m block of count 3, nodata elsewhere
        path = os.path.join(self.tmp, "wcount.tif")
        counts = np.zeros((300, 300), np.float32)
        counts[120:160, 120:160] = 3.0                    # 40 x 40 m of water, count 3
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(path, 300, 300, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + 300, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        b = ds.GetRasterBand(1); b.WriteArray(counts); b.SetNoDataValue(0.0); ds = None
        res = self.f.run_water(None, self.out, water_raster=path, cell=1.0, min_ac=0.02,
                               min_width_m=3.0, log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1)
        self.assertAlmostEqual(res["rows"][0]["area_ac"], 40 * 40 / 4046.8564224, delta=0.05)
        # count_min above the raster's value finds nothing
        res = self.f.run_water(None, self.out, water_raster=path, water_count_min=5, cell=1.0,
                               min_ac=0.02, min_width_m=3.0, log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 0)

    def test_build_water_mosaic_round_trip(self):
        # two LAS tiles, each with a 40 m water block at a known z; build the mosaic, then read it
        # back through run_water's WATER_RASTER path - the builder's output must feed the reader.
        mdir = os.path.join(self.tmp, "mosaic")
        tiles = []
        for i, (cx, cy, zz) in enumerate([(X0 + 150, Y0 + 150, 300.0), (X0 + 450, Y0 + 150, 310.0)]):
            # points at cell centres (+0.5) so each lands unambiguously in one 1 m cell
            wx, wy = np.meshgrid(np.arange(cx - 19.5, cx + 20, 1.0), np.arange(cy - 19.5, cy + 20, 1.0))
            gx = np.linspace(cx - 140, cx + 140, 60)
            gy = np.full(60, cy - 140.0)
            x = np.concatenate([wx.ravel(), gx])
            y = np.concatenate([wy.ravel(), gy])
            z = np.concatenate([np.full(wx.size, zz), np.full(60, 999.0)])
            cls = np.concatenate([np.full(wx.size, 9, np.uint8), np.full(60, 2, np.uint8)])
            p = os.path.join(self.tmp, "m{}.las".format(i))
            tc.write_las(p, x, y, cls, z=z)
            tiles.append(p)
        res = self.f.build_water_mosaic(tiles, mdir, cell=1.0, mosaic_epsg=5070, src_epsg=5070,
                                        log=lambda *_: None)
        self.assertEqual(res["n_wet_tiles"], 2)
        self.assertTrue(os.path.exists(res["count_vrt"]))
        self.assertTrue(os.path.exists(res["elev_vrt"]))
        # the count mosaic holds the water cells (count 1 each)
        cds = gdal.Open(res["count_vrt"])
        ca = cds.GetRasterBand(1).ReadAsArray()
        self.assertEqual(int((ca == 1).sum()), 2 * 40 * 40)   # both 40 x 40 blocks
        # the elevation mosaic carries the mean water z (nodata -999999 off the water)
        eds = gdal.Open(res["elev_vrt"])
        ea = eds.GetRasterBand(1).ReadAsArray()
        wet = ea[ea > -999998]
        self.assertTrue(np.all((wet >= 299.9) & (wet <= 310.1)))
        cds = eds = None
        # the reader finds both ponds from the count mosaic
        r2 = self.f.run_water(None, self.out, water_raster=res["count_vrt"], cell=1.0, min_ac=0.02,
                              min_width_m=3.0, log=lambda *_: None)
        self.assertEqual(len(r2["rows"]), 2)
        self.assertIsNone(res["points_vrt"])
        # with the return counts: every point counted (water and ground), the mosaic unchanged
        r3 = self.f.build_water_mosaic(tiles, os.path.join(self.tmp, "m3"), cell=1.0, mosaic_epsg=5070,
                                       src_epsg=5070, point_counts=True, log=lambda *_: None)
        pds = gdal.Open(r3["points_vrt"])
        pa = pds.GetRasterBand(1).ReadAsArray()
        self.assertEqual(int(pa.sum()), 2 * (40 * 40 + 60))
        cds3 = gdal.Open(r3["count_vrt"])
        ca3 = cds3.GetRasterBand(1).ReadAsArray()
        pds = cds3 = None
        self.assertEqual(int((ca3 == 1).sum()), 2 * 40 * 40)

    def test_water_mapped_and_small_tiles(self):
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        res = self.f.run_water(img, self.out, cell=1.0, mapped=self.pond_layer(), tile_m=100.0,
                               log=lambda *_: None)
        self.assertEqual([r["mapped"] for r in res["rows"]], ["yes"])  # once, across 9 tiles
        res = self.f.run_water(img, self.out, cell=1.0, mapped=self.pond_layer(half=True),
                               log=lambda *_: None)
        self.assertAlmostEqual(res["rows"][0]["mapped_share"], 0.5, delta=0.06)

    def test_water_from_an_image_service(self):
        import urllib.parse
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        asked = []

        def opener(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            asked.append(url)
            self.assertTrue(url.startswith("https://example.test/ImageServer/exportImage?"))
            x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
            w, h = (int(v) for v in q["size"][0].split(","))
            mem = "/vsimem/tile_{}.tif".format(len(asked))
            gdal.Translate(mem, img, projWin=[x0, y1, x1, y0], width=w, height=h)
            f = gdal.VSIFOpenL(mem, "rb")
            gdal.VSIFSeekL(f, 0, 2)
            size = gdal.VSIFTellL(f)
            gdal.VSIFSeekL(f, 0, 0)
            data = gdal.VSIFReadL(1, size, f)
            gdal.VSIFCloseL(f)
            gdal.Unlink(mem)
            return data

        box = (X0, Y0, X0 + 300.0, Y0 + 300.0)
        url = "https://example.test/ImageServer"
        with self.assertRaises(RuntimeError):
            self.f.run_water(url, self.out, opener=opener, log=lambda *_: None)   # no extent
        res = self.f.run_water(url, self.out, bbox=box, cell=1.0, opener=opener, log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1)
        n = len(asked)
        self.assertGreater(n, 0)
        self.f.run_water(url, self.out, bbox=box, cell=1.0, opener=opener, log=lambda *_: None)
        self.assertEqual(len(asked), n)            # the tiles are kept and reused
        with self.assertRaises(RuntimeError):
            self.f.fetch_imageserver(url, box, 1.0, os.path.join(self.tmp, "bad"),
                                     opener=lambda u: b"<html>error</html>", log=lambda *_: None)

    def test_water_several_years(self):
        a = self.write_image(os.path.join(self.tmp, "a.tif"))
        b = os.path.join(self.tmp, "b.tif")
        self.write_image(b)
        ds = gdal.Open(b, 1)                                  # year b: a flooded field as well
        for band, v in ((2, 60), (4, 20)):
            arr = ds.GetRasterBand(band).ReadAsArray()
            arr[200:260, 150:280] = v
            ds.GetRasterBand(band).WriteArray(arr)
        ds = None
        one = self.f.run_water(b, self.out, log=lambda *_: None)
        self.assertEqual(len(one["rows"]), 2)
        res = self.f.run_water([(a, 2, 4), (b, 2, 4), (a, 2, 4)], self.out, log=lambda *_: None)
        self.assertEqual(len(res["rows"]), 1)
        self.assertEqual(res["rows"][0]["images"], 3)
        self.assertAlmostEqual(res["rows"][0]["wet_share"], 1.0, delta=0.05)

    def test_water_needs_a_nir_band(self):
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        with self.assertRaises(RuntimeError):
            self.f.run_water(img, self.out, nir_band=5, log=lambda *_: None)

    def test_evidence(self):
        res = self.emb()
        dam = [r for r in res["rows"] if r["kind"] == "dam"][0]
        for k in ("ag_head_ft", "below_frac", "outlet_dist_ft", "drain_ac", "relief_ft", "pool_lat"):
            self.assertIsNotNone(dam.get(k), k)
        self.assertGreater(dam["ag_head_ft"], 3.0)
        self.assertGreater(dam["drain_ac"], 1.0)
        # imagery at the DEM's grid: open water on the dam's pool in 2018, none in 2014
        dem, _r = tc.scene()
        pool = np.isclose(dem, tc.bed(200) + 3.0) & (np.arange(tc.N)[:, None] < 197)
        imgs = []
        for year, wet in ((2014, False), (2018, True)):
            path = os.path.join(self.tmp, "img{}.tif".format(year))
            ds = gdal.GetDriverByName("GTiff").Create(path, tc.N, tc.N, 4, gdal.GDT_Byte)
            ds.SetGeoTransform((X0, tc.CELL, 0.0, Y0 + tc.N * tc.CELL, 0.0, -tc.CELL))
            ds.SetProjection(srs(5070).ExportToWkt())
            nir = np.where(pool & wet, 20, 140)
            for b, a in ((1, np.full(pool.shape, 70)), (2, np.full(pool.shape, 80)), (3, np.full(pool.shape, 60)), (4, nir)):
                ds.GetRasterBand(b).WriteArray(a.astype(np.uint8))
            ds = None
            imgs.append({"year": year, "src": path, "green": 2, "nir": 4, "red": 1, "blue": 3})
        x = gx(tc.XS)
        nwi = write_layer(self.vec, "nwi", [("POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(
            a=x - 300.0, b=x + 300.0, c=row_y(197), d=row_y(0)), {"ATTRIBUTE": "PUBHh"})], fields=("ATTRIBUTE",))
        blds = write_layer(self.vec, "buildings", [("POINT ({} {})".format(x + 9.0, row_y(360)), {})])
        out = os.path.join(self.tmp, "ev.gpkg")
        ev = self.f.run_evidence(self.out + "|layername=embankments", out, images=imgs, nwi=nwi, dem=self.dem,
                                 buildings=blds, roads=self.roads, log=lambda *_: None)
        d = [r for r in ev["rows"] if r["kind"] == "dam"][0]
        self.assertEqual((d["img_years"], d["wet_years"], d["first_wet"]), (2, 1, 2018))
        self.assertEqual((d["nwi_code"], d["nwi_h"], d["nwi_x"], d["nwi_regime"]), ("PUBHh", "yes", "no", "H"))
        self.assertGreaterEqual(d["bld_1mi"], 1)
        self.assertGreaterEqual(d["roads_1mi"], 1)
        self.assertGreater(d["conseq"], 0)
        # the dam, and the dry embankment below it whose pool (about 100 m round) reaches the
        # polygon's edge; the road fill further down does not
        self.assertEqual(ev["counts"]["nwi_h"], 2)
        self.assertEqual([r["nwi_h"] for r in ev["rows"] if r["kind"] == "road fill"], ["no"])
        back = self.f.read_rows(out + "|layername=evidence")
        self.assertEqual(len(back), len(ev["rows"]))
        self.assertIn("wet_years", back[0])
        # the permit check is from the storage alone: blank when the storage is unknown, and the
        # consequence pass does not overwrite it
        fl = self.evid_fills([{"x": x, "y": row_y(200), "find_id": "NOSTORE", "kind": "dam", "area_ac": 1.0},
                              {"x": x, "y": row_y(210), "find_id": "BIG", "kind": "dam", "area_ac": 1.0,
                               "storage_af": 40.0}])
        ev2 = self.f.run_evidence(fl, os.path.join(self.tmp, "ev2.gpkg"), dem=self.dem, roads=self.roads,
                                  log=lambda *_: None)
        got = {r["find_id"]: r for r in ev2["rows"]}
        self.assertIsNone(got["NOSTORE"].get("permit_check"))
        self.assertEqual(got["BIG"]["permit_check"], "yes")
        self.assertIn("conseq", got["NOSTORE"])
        # a numeric field blank in every row is still written as a number
        self.assertIn(("storage_af", "float"), self.f._fields_of([{"find_id": "A", "storage_af": None}]))
        self.assertIn(("nwi_h", "str"), self.f._fields_of([{"nwi_h": None}]))
        txt = "2018 | /x/img.tif | green=3 nir=1 red=2\n# a comment\n2020|svc|nir=1, green=3"
        self.assertEqual(self.f.parse_images(txt), [{"year": 2018, "src": "/x/img.tif", "green": 3, "nir": 1, "red": 2},
                                                    {"year": 2020, "src": "svc", "nir": 1, "green": 3}])
        with self.assertRaises(ValueError):
            self.f.parse_images("2018 | x | red=1")
        # the ND GIS Hub colour-infrared years are NIR, green, blue: green is band 2, no red
        from .. import brand
        for d in self.f.parse_images(brand.EVIDENCE_IMAGES):
            if "_CIR" in d["src"]:
                self.assertEqual((d["nir"], d["green"], d.get("red")), (1, 2, None))
        for _lab, src, g, n in brand.IMAGE_SERVICES:
            if "_CIR" in src:
                self.assertEqual((g, n), (2, 1))

    def evid_fills(self, rows):
        path = os.path.join(self.tmp, "fills.gpkg")
        self.f.write_points(path, "embankments", [("find_id", "str"), ("kind", "str"), ("area_ac", "float"),
                                                  ("storage_af", "float"), ("note", "str"), ("height_ft", "float")],
                            rows)
        return path + "|layername=embankments"

    def test_review_batch_reviewed_file_must_exist(self):
        fills = self.evid_fills([{"x": X0 + 100.0 * i, "y": Y0, "find_id": "FIND-{:08X}".format(i), "kind": "dam",
                                  "area_ac": 1.0, "height_ft": float(i)} for i in range(30)])
        out = os.path.join(self.tmp, "rb.gpkg")
        with self.assertRaises(RuntimeError):
            self.f.run_review_batch(fills, out, n=10, reviewed=os.path.join(self.tmp, "nowhere.txt"),
                                    log=lambda *_: None)
        said = []
        top = "FIND-0000001D"                           # height 29: rank 1
        res = self.f.run_review_batch(fills, out, n=10, per_tile_cap=10, log=said.append)
        self.assertIn(top, [r["find_id"] for r in res["rows"]])     # so the checks below can fail
        # a reviewed card is left out of the batch (a list, then a marks file)
        res = self.f.run_review_batch(fills, out, n=10, reviewed=[top], per_tile_cap=10, log=said.append)
        self.assertEqual(len(res["rows"]), 10)
        self.assertNotIn(top, [r["find_id"] for r in res["rows"]])
        self.assertTrue(all(r["pool_n"] == 29 for r in res["rows"]))
        path = os.path.join(self.tmp, "reviewed.csv")
        with open(path, "w", encoding="utf-8-sig") as fh:
            fh.write("find_id,mark\n{},yes\n".format(top))
        res = self.f.run_review_batch(fills, out, n=10, reviewed=path, per_tile_cap=10, log=said.append)
        self.assertNotIn(top, [r["find_id"] for r in res["rows"]])
        # one 5 km tile, at most 3 near the cut: a short batch, and it says so
        res = self.f.run_review_batch(fills, out, n=10, log=said.append)
        self.assertEqual(len(res["rows"]), 9)
        self.assertTrue(any("the batch has 9 card(s), not 10" in m for m in said))

    def test_evidence_lidar_needs_coverage(self):
        # a return-count tile 6 returns a cell over x0..x0+400 (0 stored as nodata, as the
        # mosaic builder writes it): a fill 5 km away has no LiDAR evidence, and a fill whose pool
        # straddles the tile edge is read on its covered cells only (dry land, nothing empty)
        tif = os.path.join(self.tmp, "points_count_t.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, 400, 400, 1, gdal.GDT_Int32)
        ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + 400.0, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(0)
        ds.GetRasterBand(1).WriteArray(np.full((400, 400), 6, np.int32))
        ds = None
        vrt = os.path.join(self.tmp, "point_count.vrt")
        gdal.BuildVRT(vrt, [tif])
        wide = os.path.join(self.tmp, "wide.vrt")                    # a VRT wider than its tiles
        gdal.BuildVRT(wide, [tif], outputBounds=(X0 - 2000.0, Y0 - 2000.0, X0 + 6000.0, Y0 + 2000.0))
        fills = self.evid_fills([
            {"x": X0 + 5400.0, "y": Y0 + 200.0, "find_id": "FAR", "kind": "dam", "area_ac": 1.0},
            {"x": X0 + 380.0, "y": Y0 + 200.0, "find_id": "EDGE", "kind": "dam", "area_ac": 1.0}])
        for src in (vrt, wide):
            ev = self.f.run_evidence(fills, os.path.join(self.tmp, "lc.gpkg"), lidar_points=src, log=lambda *_: None)
            got = {r["find_id"]: r for r in ev["rows"]}
            for k in ("lidar_pool_empty", "lidar_ring_empty", "lidar_contrast"):
                self.assertIsNone(got["FAR"].get(k), (src, k))
            self.assertEqual(got["EDGE"]["lidar_pool_empty"], 0.0)
            self.assertEqual(got["EDGE"]["lidar_ring_empty"], 0.0)

    def count_tile(self, name, x0, y0, arr):
        """A return-count GeoTIFF (EPSG:5070, 1 m, 0 = nodata) with its top-left at (x0, y0 + rows)."""
        tif = os.path.join(self.tmp, name)
        ds = gdal.GetDriverByName("GTiff").Create(tif, arr.shape[1], arr.shape[0], 1, gdal.GDT_Int32)
        ds.SetGeoTransform((x0, 1.0, 0.0, y0 + arr.shape[0], 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(0)
        ds.GetRasterBand(1).WriteArray(arr.astype(np.int32))
        ds = None
        return tif

    def lidar_of(self, fills, src):
        ev = self.f.run_evidence(fills, os.path.join(self.tmp, "lc.gpkg"), lidar_points=src, log=lambda *_: None)
        return {r["find_id"]: r for r in ev["rows"]}

    def test_evidence_lidar_partial_tile_and_pond(self):
        # a tile cut by a diagonal project edge (returns where x + y < 420 m, the rest nodata in
        # the tile's box), with a pond void (no returns) inside the survey: a pool in the empty
        # corner has no evidence, a pool 20 m inside the edge reads dry, and the pond reads empty
        yy, xx = np.mgrid[0:600, 0:600]
        x, y = xx + 0.5, 600 - yy - 0.5
        a = np.where(x + y < 420.0, 6, 0)
        a[(x - 120.0) ** 2 + (y - 120.0) ** 2 < 30.0 ** 2] = 0          # the pond
        vrt = os.path.join(self.tmp, "cut.vrt")
        gdal.BuildVRT(vrt, [self.count_tile("cut.tif", X0, Y0, a)])
        fills = self.evid_fills([
            {"x": X0 + 450.0, "y": Y0 + 450.0, "find_id": "CORNER", "kind": "dam", "area_ac": 1.0},
            {"x": X0 + 196.0, "y": Y0 + 196.0, "find_id": "EDGE", "kind": "dam", "area_ac": 1.0},
            {"x": X0 + 120.0, "y": Y0 + 120.0, "find_id": "POND", "kind": "dam", "area_ac": 0.7}])
        got = self.lidar_of(fills, vrt)
        for k in ("lidar_pool_empty", "lidar_ring_empty", "lidar_contrast"):
            self.assertIsNone(got["CORNER"].get(k), k)
        self.assertEqual(got["EDGE"]["lidar_pool_empty"], 0.0)
        self.assertEqual(got["EDGE"]["lidar_ring_empty"], 0.0)
        self.assertGreater(got["POND"]["lidar_pool_empty"], 0.9)
        self.assertLess(got["POND"]["lidar_ring_empty"], 0.2)

    def test_evidence_lidar_nested_vrt_and_a_rebuilt_one(self):
        # a VRT of a per-project VRT keeps the gap between the project's tiles; and a VRT
        # rebuilt at the same path between two runs is read afresh
        ta = self.count_tile("na.tif", X0, Y0, np.full((400, 400), 6))
        tb = self.count_tile("nb.tif", X0 + 1000.0, Y0, np.full((400, 400), 6))
        inner = os.path.join(self.tmp, "proj", "point_count.vrt")
        os.makedirs(os.path.dirname(inner))
        gdal.BuildVRT(inner, [ta, tb])
        outer = os.path.join(self.tmp, "state.vrt")
        gdal.BuildVRT(outer, [inner])
        fills = self.evid_fills([
            {"x": X0 + 700.0, "y": Y0 + 200.0, "find_id": "GAP", "kind": "dam", "area_ac": 1.0},
            {"x": X0 + 1200.0, "y": Y0 + 200.0, "find_id": "IN", "kind": "dam", "area_ac": 1.0}])
        got = self.lidar_of(fills, outer)
        self.assertIsNone(got["GAP"].get("lidar_pool_empty"))
        self.assertEqual(got["IN"]["lidar_pool_empty"], 0.0)
        one = os.path.join(self.tmp, "redo.vrt")
        gdal.BuildVRT(one, [ta])
        self.assertIsNone(self.lidar_of(fills, one)["IN"].get("lidar_pool_empty"))
        gdal.BuildVRT(one, [ta, tb])                    # the same path, now over both tiles
        self.assertEqual(self.lidar_of(fills, one)["IN"]["lidar_pool_empty"], 0.0)

    def test_evidence_lidar_gap_between_two_surveys(self):
        # project P1's tile box runs to x0+600 but its survey stops at x0+500; project P2 starts at
        # x0+700. Returns of P2 must not close P1's unsurveyed strip: a pool over the strip reads
        # dry on its surveyed cells, not 'empty'
        x = np.arange(600)[None, :] + 0.5
        a = np.where(np.broadcast_to(x, (600, 600)) < 500.0, 6, 0)
        p1 = os.path.join(self.tmp, "P1", "point_count.vrt")
        p2 = os.path.join(self.tmp, "P2", "point_count.vrt")
        for d in (p1, p2):
            os.makedirs(os.path.dirname(d))
        gdal.BuildVRT(p1, [self.count_tile("P1/t.tif", X0, Y0, a)])
        gdal.BuildVRT(p2, [self.count_tile("P2/t.tif", X0 + 700.0, Y0, np.full((600, 600), 6))])
        state = os.path.join(self.tmp, "state_gap.vrt")
        gdal.BuildVRT(state, [p1, p2])
        fills = self.evid_fills([{"x": X0 + 545.0, "y": Y0 + 300.0, "find_id": "GAP", "kind": "dam", "area_ac": 5.0}])
        got = self.lidar_of(fills, state)["GAP"]
        for k in ("lidar_pool_empty", "lidar_ring_empty", "lidar_contrast"):
            self.assertIn(got.get(k), (None, 0.0), k)

    def test_evidence_lidar_pond_on_a_tile_seam_and_odd_vrts(self):
        # a pond void on the seam of two tiles still reads empty; a warped VRT and a VRT source
        # with no DstRect still count as surveyed; a missing project VRT costs only its own fills
        a = np.full((400, 800), 6)
        yy, xx = np.mgrid[0:400, 0:800]
        a[((xx + 0.5) - 400.0) ** 2 + ((400 - yy - 0.5) - 200.0) ** 2 < 30.0 ** 2] = 0
        ta = self.count_tile("seam_a.tif", X0, Y0, a[:, :400])
        tb = self.count_tile("seam_b.tif", X0 + 400.0, Y0, a[:, 400:])
        seam = os.path.join(self.tmp, "seam.vrt")
        gdal.BuildVRT(seam, [ta, tb])
        fills = self.evid_fills([{"x": X0 + 400.0, "y": Y0 + 200.0, "find_id": "POND", "kind": "dam", "area_ac": 0.7},
                                 {"x": X0 + 150.0, "y": Y0 + 200.0, "find_id": "DRY", "kind": "dam", "area_ac": 1.0}])
        got = self.lidar_of(fills, seam)
        self.assertGreater(got["POND"]["lidar_pool_empty"], 0.8)
        self.assertLess(got["POND"]["lidar_ring_empty"], 0.2)
        warped = os.path.join(self.tmp, "warped.vrt")
        gdal.Warp(warped, ta, format="VRT")
        hand = os.path.join(self.tmp, "hand.vrt")
        with open(hand, "w") as fh:
            fh.write('<VRTDataset rasterXSize="400" rasterYSize="400"><SRS>{}</SRS>'
                     '<GeoTransform>{}, 1, 0, {}, 0, -1</GeoTransform>'
                     '<VRTRasterBand dataType="Int32" band="1"><NoDataValue>0</NoDataValue><SimpleSource>'
                     '<SourceFilename relativeToVRT="0">{}</SourceFilename><SourceBand>1</SourceBand>'
                     '</SimpleSource></VRTRasterBand></VRTDataset>'.format(srs(5070).ExportToWkt().replace('"', "&quot;"),
                                                                           X0, Y0 + 400.0, ta))
        for src in (warped, hand):
            self.assertEqual(self.lidar_of(fills, src)["DRY"]["lidar_pool_empty"], 0.0, src)
        pa = os.path.join(self.tmp, "PA", "point_count.vrt")
        pb = os.path.join(self.tmp, "PB", "point_count.vrt")
        for d in (pa, pb):
            os.makedirs(os.path.dirname(d))
        gdal.BuildVRT(pa, [ta])
        gdal.BuildVRT(pb, [self.count_tile("PB/far.tif", X0 + 3000.0, Y0, np.full((400, 400), 6))])
        state = os.path.join(self.tmp, "state_missing.vrt")
        gdal.BuildVRT(state, [pa, pb])
        import shutil
        shutil.rmtree(os.path.dirname(pb))
        said = []
        ev = self.f.run_evidence(fills, os.path.join(self.tmp, "lm.gpkg"), lidar_points=state, log=said.append)
        got = {r["find_id"]: r for r in ev["rows"]}
        self.assertEqual(got["DRY"]["lidar_pool_empty"], 0.0)
        self.assertTrue(any("could not be read" in m and "PB" in m for m in said), said)

    def test_evidence_two_services_one_year(self):
        # the CIR and the 4-band of one year: each its own tiles, and water in either counts
        from unittest import mock
        import urllib.parse
        asked = []

        def image(url, bands):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
            w, h = (int(v) for v in q["size"][0].split(","))
            mem = "/vsimem/svc_{}.tif".format(len(asked))
            ds = gdal.GetDriverByName("GTiff").Create(mem, w, h, len(bands), gdal.GDT_Byte)
            ds.SetGeoTransform((x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h))
            ds.SetProjection(srs(5070).ExportToWkt())
            for k, v in enumerate(bands):
                ds.GetRasterBand(k + 1).WriteArray(np.full((h, w), v, np.uint8))
            ds = None
            f = gdal.VSIFOpenL(mem, "rb")
            gdal.VSIFSeekL(f, 0, 2)
            n = gdal.VSIFTellL(f)
            gdal.VSIFSeekL(f, 0, 0)
            data = gdal.VSIFReadL(1, n, f)
            gdal.VSIFCloseL(f)
            gdal.Unlink(mem)
            return data

        class Resp:
            def __init__(self, data):
                self.data = data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.data

        def fake(url, timeout=None):
            asked.append(url)
            if "/cir/" in url:
                return Resp(image(url, (20, 90, 60)))               # NIR, green, blue: water
            return Resp(image(url, (60, 90, 50, 20)))               # R, G, B, NIR: water
        imgs = "2018 | https://svc.test/cir/ImageServer | nir=1 green=2\n" \
               "2018 | https://svc.test/four/ImageServer | red=1 green=2 blue=3 nir=4"
        fills = self.evid_fills([{"x": X0 + 200.0, "y": Y0 + 200.0, "find_id": "F1", "kind": "dam", "area_ac": 1.0}])
        with mock.patch("urllib.request.urlopen", fake):
            ev = self.f.run_evidence(fills, os.path.join(self.tmp, "svc.gpkg"), images=imgs, log=lambda *_: None)
        self.assertTrue(any("/cir/" in u for u in asked) and any("/four/" in u for u in asked))
        r = ev["rows"][0]
        self.assertEqual((r["img_years"], r["wet_years"]), (1, 1))

    def test_evidence_permit_bare_and_fields(self):
        # permit_check needs only the storage; a later image with no data keeps the bare share;
        # a field blank in row 1 keeps its number type; a polygon edge 20 m off counts as near
        a = os.path.join(self.tmp, "a.tif")
        ds = gdal.GetDriverByName("GTiff").Create(a, 400, 400, 4, gdal.GDT_Byte)
        ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + 400.0, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        yy, xx = np.mgrid[0:400, 0:400]
        bare = np.hypot(xx + 0.5 - 200.0, yy + 0.5 - 200.0) < 40.0      # bright bare ground at F1, grass round it
        for b, v, g in ((1, 200, 60), (2, 190, 90), (3, 180, 50), (4, 205, 160)):
            ds.GetRasterBand(b).WriteArray(np.where(bare, v, g).astype(np.uint8))
        ds = None
        b_ = os.path.join(self.tmp, "b.tif")                              # 2020: somewhere else
        ds = gdal.GetDriverByName("GTiff").Create(b_, 50, 50, 4, gdal.GDT_Byte)
        ds.SetGeoTransform((X0 + 9000.0, 1.0, 0.0, Y0 + 9050.0, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds = None
        imgs = [{"year": 2018, "src": a, "red": 1, "green": 2, "blue": 3, "nir": 4},
                {"year": 2020, "src": b_, "red": 1, "green": 2, "blue": 3, "nir": 4}]
        q = write_layer(self.vec, "quarry", [("POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(
            a=X0 - 200.0, b=X0 + 200.0, c=Y0 + 220.0, d=Y0 + 620.0), {})], epsg=5070)
        fills = self.evid_fills([
            {"x": X0 + 200.0, "y": Y0 + 200.0, "find_id": "F1", "kind": "dam", "area_ac": 0.5, "storage_af": 40.0},
            {"x": X0 + 100.0, "y": Y0 + 100.0, "find_id": "F2", "kind": "dam", "area_ac": 0.5, "storage_af": 3.0,
             "height_ft": 7.5}])
        ev = self.f.run_evidence(fills, os.path.join(self.tmp, "pb.gpkg"), images=imgs, quarries=q,
                                 log=lambda *_: None)
        got = {r["find_id"]: r for r in ev["rows"]}
        self.assertEqual((got["F1"]["permit_check"], got["F2"]["permit_check"]), ("yes", "no"))
        self.assertEqual(ev["counts"]["permit_check"], 1)
        self.assertGreater(got["F1"]["bare_share"], 0.5)
        self.assertEqual(got["F1"]["in_quarry"], "yes")                   # 20 m below the south edge
        self.assertEqual(dict(self.f._fields_of(ev["rows"]))["height_ft"], "float")
        back = self.f.read_rows(os.path.join(self.tmp, "pb.gpkg") + "|layername=evidence")
        self.assertIsInstance([r for r in back if r["find_id"] == "F2"][0]["height_ft"], float)


def country(size=3600, seed=3):
    """(DEM at 1 m cells, number of fills built) of rolling ground falling south with seven draws
    that wander, an earth fill across each every 410 m (3.0 to 4.2 m high, 2.5:1 faces, a 6 m top)
    and a flat pond behind every other one. The ground is rough at the scale of a 3 m cell, so
    where the cells fall changes every reading (as on real LiDAR)."""
    from scipy import ndimage as ndi
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:size, 0:size].astype(float)
    rough = ndi.zoom(ndi.gaussian_filter(rng.standard_normal((size // 8, size // 8)), 3.0), 8, order=1)[:size, :size]
    z = 300.0 - 0.012 * y + 6.0 * rough
    n = 0
    for k, xc in enumerate((330.3, 800.6, 1290.2, 1760.9, 2240.4, 2700.5, 3200.8)):
        wig = xc + 60.0 * np.sin(y / 310.0 + k)
        z = z + np.minimum(np.abs(x - wig) * 0.035, 9.0) - 9.0
        for j, yr in enumerate(np.arange(380.4 + 37 * k, size - 300, 410.3)):
            h = 3.0 + 0.6 * ((k + j) % 3)
            xw = xc + 60.0 * np.sin(yr / 310.0 + k)
            base = 300.0 - 0.012 * yr - 9.0 + 6.0 * rough[int(yr), int(xw)]
            face = np.clip(1.0 - (np.abs(y - yr) - 3.0) / (2.5 * h), 0.0, 1.0)
            z = np.where(face > 0, np.maximum(z, base + h * face), z)
            n += 1
            if (k + j) % 2 == 0:
                lvl = base + h - 0.9
                z = np.where((y < yr - 3.0) & (y > yr - 400) & (np.abs(x - wig) < 200) & (z < lvl), lvl, z)
    return z, n


@unittest.skipIf(gdal is None, "no GDAL (osgeo) in this Python")
class FixedLattice(unittest.TestCase):
    """Finder 0.7: a result does not depend on where the run's box is."""

    def setUp(self):
        from .. import finder
        self.f = finder
        self.tmp = tempfile.mkdtemp(prefix="finder_lat_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_lattice(self):
        f = self.f
        self.assertEqual(f.grid_lattice(3.0, 10.0), 30.0)
        self.assertEqual(f.grid_lattice(2.0, 10.0), 10.0)
        self.assertEqual(f.grid_lattice(2.5, 10.0), 10.0)
        self.assertEqual(f.grid_lattice(1.5, 10.0), 30.0)
        self.assertEqual(f.grid_lattice(3.0, 3.0), 3.0)
        self.assertIsNone(f.grid_lattice(3.3, 10.0))         # they meet only every 330 m
        self.assertIsNone(f.grid_lattice(math.pi, 10.0))     # not a whole millimetre
        self.assertEqual(f.snap_out((100001.0, 2700007.0, 100061.0, 2700059.5), 30.0),
                         (99990.0, 2700000.0, 100080.0, 2700060.0))
        on = (99990.0, 2699970.0, 100080.0, 2700060.0)
        self.assertEqual(f.snap_out(on, 30.0), on)           # a box on the lattice stays
        self.assertEqual(f.snap_out((5.0, 5.0, 5.0, 5.0), 3.0), (3.0, 3.0, 6.0, 6.0))
        self.assertEqual(f.snap_out((-7.0, -7.0, -1.0, 2.0), 3.0), (-9.0, -9.0, 0.0, 3.0))
        sq = f.lattice_squares((4999.0, -1.0, 5001.0, 1.0), 5000.0)
        self.assertEqual(sq, [(0.0, -5000.0, 5000.0, 0.0), (0.0, 0.0, 5000.0, 5000.0),
                              (5000.0, -5000.0, 10000.0, 0.0), (5000.0, 0.0, 10000.0, 5000.0)])
        self.assertEqual(f.lattice_squares((5000.0, 0.0, 10000.0, 5000.0), 5000.0),
                         [(5000.0, 0.0, 10000.0, 5000.0)])    # a box that is a square is that square

    def test_the_dem_cells_do_not_move_with_the_box(self):
        dem, _n = country(600)
        tif = os.path.join(self.tmp, "c.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, 600, 600, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((X0 + 0.4, 1.0, 0.0, Y0 + 600.7, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).SetUnitType("m")
        ds.GetRasterBand(1).WriteArray(dem.astype(np.float32))
        ds = None
        box = (X0 + 101.0, Y0 + 100.0, X0 + 400.0, Y0 + 421.0)
        za, ga = self.f.warp_dem(tif, box, 3.0)
        for dx, dy in ((1.0, 1.0), (7.0, -2.0), (0.3, 100.1)):
            zb, gb = self.f.warp_dem(tif, (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy), 3.0)
            for g in (ga, gb):                               # cell edges at whole multiples of 3 m
                self.assertEqual((g[0] % 3.0, g[3] % 3.0, g[1], g[5]), (0.0, 0.0, 3.0, -3.0))
            c, r = int(round((gb[0] - ga[0]) / 3.0)), int(round((ga[3] - gb[3]) / 3.0))
            a = za[max(r, 0):, max(c, 0):]
            b = zb[max(-r, 0):, max(-c, 0):]
            h, w = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
            self.assertGreater(h * w, 5000)
            self.assertTrue(np.array_equal(a[:h, :w], b[:h, :w]))     # the same cells, to the bit

    def test_a_moved_box_gives_the_same_fills(self):
        # draws, pool lines and stream lines over rough ground; a box moved by 1 m, 7 m and 1,234 m
        # (the last touches other squares of the lattice): every fill inside both boxes is the same
        # fill with the same id and the same readings. On 0.6 (squares and cells laid from the box's
        # corner) no id survived a 1 m move on this ground.
        dem, built = country()
        self.assertEqual(built, 51)
        tif = os.path.join(self.tmp, "country.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, dem.shape[1], dem.shape[0], 1, gdal.GDT_Float32)
        x0, y0 = X0 + 0.4, Y0 + 0.7                          # the DEM's own corner is on no lattice
        ds.SetGeoTransform((x0, 1.0, 0.0, y0 + dem.shape[0], 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).SetUnitType("m")
        ds.GetRasterBand(1).WriteArray(dem.astype(np.float32))
        ds = None
        vec = os.path.join(self.tmp, "in.gpkg")              # a mapped stream down the third draw
        ys = np.arange(40.0, 3560.0, 25.0)
        xs = 1290.2 + 60.0 * np.sin(ys / 310.0 + 2)
        streams = write_layer(vec, "streams", [("LINESTRING ({})".format(", ".join(
            "{} {}".format(x0 + a, y0 + 3600.0 - b) for a, b in zip(xs, ys))), {})])
        from ..core import embankments as em
        p = em.FinderParams(window_m=400.0, ridge_half_m=200.0, terrain_m=200.0)
        base = (x0 + 100.0, y0 + 100.0, x0 + 2300.0, y0 + 2300.0)

        def run(d):
            b = tuple(v + d for v in base)
            res = self.f.run_embankments(tif, streams, os.path.join(self.tmp, "o.gpkg"), bbox=b, drainage_ac=3.0,
                                         tile_m=1000.0, hillshade=False, dikes=False, params=p,
                                         pool_margin_m=90.0, log=lambda *_: None)
            for r in res["rows"]:
                self.assertTrue(b[0] <= r["x"] < b[2] and b[1] <= r["y"] < b[3])   # only the box's own
            return b, res["rows"]

        b0, r0 = run(0.0)
        self.assertGreater(len(r0), 40)
        self.assertGreater(len({r["line_source"] for r in r0}), 1)
        self.assertEqual(len({r["find_id"] for r in r0}), len(r0))
        for d, least in ((1.0, 40), (7.0, 40), (1234.0, 10)):
            b1, r1 = run(d)
            both = lambda r: all(b[0] <= r["x"] < b[2] and b[1] <= r["y"] < b[3] for b in (b0, b1))   # noqa: E731
            a = {r["find_id"]: r for r in r0 if both(r)}
            c = {r["find_id"]: r for r in r1 if both(r)}
            self.assertGreaterEqual(len(a), least)
            self.assertEqual(sorted(a), sorted(c), "box moved by {} m".format(d))
            for k in a:
                self.assertEqual({f: v for f, v in a[k].items() if f != "known_ft"},
                                 {f: v for f, v in c[k].items() if f != "known_ft"})

    def test_open_water_does_not_move_with_the_box(self):
        g, nir, _pond = tc.image()
        img = os.path.join(self.tmp, "img.tif")
        ds = gdal.GetDriverByName("GTiff").Create(img, g.shape[1], g.shape[0], 4, gdal.GDT_Byte)
        ds.SetGeoTransform((X0 + 0.37, 1.0, 0.0, Y0 + g.shape[0] + 0.61, 0.0, -1.0))   # pixels on no lattice
        ds.SetProjection(srs(5070).ExportToWkt())
        for b, a in zip((1, 2, 3, 4), (g, g, g, nir)):
            ds.GetRasterBand(b).WriteArray(a.astype(np.uint8))
        ds = None
        got = []
        for dx, dy in ((0.0, 0.0), (0.45, 0.2), (7.0, -3.3)):
            box = (X0 + 20.0 + dx, Y0 + 20.0 + dy, X0 + 280.0 + dx, Y0 + 280.0 + dy)
            res = self.f.run_water(img, os.path.join(self.tmp, "w.gpkg"), bbox=box, cell=1.0, tile_m=200.0,
                                   log=lambda *_: None)
            self.assertEqual(len(res["rows"]), 1)
            got.append((res["rows"][0]["water_id"], res["rows"][0]["area_ac"], res["rows"][0]["ndwi"]))
        self.assertEqual(len(set(got)), 1, got)

    def test_open_water_cells_are_on_the_lattice_at_a_cell_that_does_not_divide_the_square(self):
        # cell 0.6 m: 200 m and 300 m squares are not whole numbers of cells. Each window is grown
        # to whole multiples of 0.6 m, so the cells fall on the same ground for any square size
        # and any box: the same patch, and its outline on the 0.6 m lattice
        g, nir, _pond = tc.image()
        img = os.path.join(self.tmp, "img.tif")
        ds = gdal.GetDriverByName("GTiff").Create(img, g.shape[1], g.shape[0], 4, gdal.GDT_Byte)
        ds.SetGeoTransform((X0 + 0.37, 1.0, 0.0, Y0 + g.shape[0] + 0.61, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        for b, a in zip((1, 2, 3, 4), (g, g, g, nir)):
            ds.GetRasterBand(b).WriteArray(a.astype(np.uint8))
        ds = None
        got = []
        for tile_m, (dx, dy) in ((200.0, (0.0, 0.0)), (300.0, (0.0, 0.0)), (200.0, (0.45, 0.2)), (300.0, (7.0, -3.3))):
            box = (X0 + 20.0 + dx, Y0 + 20.0 + dy, X0 + 280.0 + dx, Y0 + 280.0 + dy)
            out = os.path.join(self.tmp, "w{}.gpkg".format(len(got)))
            res = self.f.run_water(img, out, bbox=box, cell=0.6, tile_m=tile_m, log=lambda *_: None)
            self.assertEqual(len(res["rows"]), 1)
            got.append((res["rows"][0]["water_id"], res["rows"][0]["area_ac"], res["rows"][0]["ndwi"]))
            ds = ogr.Open(out)
            feat = next(iter(ds.GetLayerByName("water")))
            geom = feat.GetGeometryRef()
            pts = []
            for k in range(geom.GetGeometryCount()):
                part = geom.GetGeometryRef(k)
                for m in range(part.GetGeometryCount()):
                    ring = part.GetGeometryRef(m)
                    pts += [ring.GetPoint_2D(q) for q in range(ring.GetPointCount())]
            ds = None
            off = max(abs(v / 0.6 - round(v / 0.6)) for xy in pts for v in xy)
            self.assertLess(off, 1e-4, "tile_m {}: an outline vertex is not on the 0.6 m lattice".format(tile_m))
        self.assertEqual(len(set(got)), 1, got)

    def test_crossings_and_flow_paths_are_routed_on_lattice_squares(self):
        # the crossings tool (drainage area) and the evidence tool (what is below a fill) route
        # each point in the window of the lattice square that holds it (grown by the tool's pad),
        # whatever other points are in the run
        from unittest import mock
        dem, _n = country(600)
        tif = os.path.join(self.tmp, "c.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, 600, 600, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((X0 + 0.4, 1.0, 0.0, Y0 + 600.7, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).SetUnitType("m")
        ds.GetRasterBand(1).WriteArray(dem.astype(np.float32))
        ds = None
        pts = [(X0 + 130.0, Y0 + 140.0), (X0 + 330.0, Y0 + 90.0), (X0 + 450.0, Y0 + 420.0), (X0 + 90.0, Y0 + 510.0)]
        far = [(X0 - 5130.0, Y0 - 7270.0), (X0 + 9870.0, Y0 + 333.0)]   # off the DEM: they move a box laid from the points
        sq = 200.0

        def squares(pad, points):
            return sorted({(math.floor(x / sq) * sq - pad, math.floor(y / sq) * sq - pad,
                            math.floor(x / sq) * sq + sq + pad, math.floor(y / sq) * sq + sq + pad) for x, y in points})
        roads = write_layer(os.path.join(self.tmp, "roads.gpkg"), "roads", [
            ("LINESTRING ({} {}, {} {})".format(X0, Y0 + 60.0 + 110.0 * k, X0 + 600.0, Y0 + 75.0 + 110.0 * k), {})
            for k in range(5)], epsg=5070)

        def run(extra):
            boxes = []
            real = self.f.warp_dem

            def spy(dem_, box, cell, *a, **k):
                boxes.append(tuple(round(v, 6) for v in box))
                return real(dem_, box, cell, *a, **k)
            cr = [{"x": x, "y": y} for x, y in pts + extra]
            fl = [{"x": x, "y": y, "height_ft": 8.0, "storage_af": 3.0} for x, y in pts + extra]
            with mock.patch.object(self.f, "warp_dem", spy):
                self.f._size_crossings(cr, tif, False, 0.35, 2.0, 8.0, 10.0, sq, lambda *_: None)
                nc = len(boxes)
                self.f._consequences(fl, tif, False, None, roads, 500.0, 10.0, lambda *_: None, lambda: False,
                                     tile_m=sq)
            got = ([(r.get("drainage_ac"), r.get("flow_len_ft")) for r in cr[:len(pts)]],
                   [tuple(sorted((k, v) for k, v in r.items() if k not in ("x", "y"))) for r in fl[:len(pts)]])
            return sorted(boxes[:nc]), sorted(boxes[nc:]), got
        cb, fb, alone = run([])
        self.assertEqual(cb, squares(4000.0, pts))
        self.assertEqual(fb, squares(500.0, pts))
        self.assertTrue(all(d is not None for d, _f in alone[0]))
        self.assertTrue(all(dict(c).get("roads_1mi") is not None for c in alone[1]))
        cb2, fb2, more = run(far)
        self.assertEqual(cb2, squares(4000.0, pts + far))
        self.assertEqual(fb2, squares(500.0, pts + far))
        self.assertEqual(more, alone)

    def test_a_square_under_the_curved_edge_of_a_geographic_dem_is_read(self):
        # a DEM in degrees (2 x 1 degree across the -96 meridian, a few cells: only its outline
        # matters): in EPSG:5070 its south edge bulges about 400 m south of the box of its four
        # corners. A lattice square holding only that strip is still read
        tif = os.path.join(self.tmp, "geo.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, 20, 10, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((-97.0, 0.1, 0.0, 47.0, 0.0, -0.1))
        ds.SetProjection(srs(4269).ExportToWkt())
        ds = None
        ct = osr.CoordinateTransformation(srs(4269), srs(5070))
        corners = [ct.TransformPoint(x, y)[:2] for x in (-97.0, -95.0) for y in (46.0, 47.0)]
        south = min(c[1] for c in corners)
        px, py = ct.TransformPoint(-96.0, 46.0)[:2]                  # the south edge at the meridian
        self.assertLess(py, south - 300.0)
        sq = (px - 100.0, py - 100.0, px + 100.0, py + 50.0)       # holds only the strip beyond the corners' box
        self.assertLess(sq[3], south - 100.0)
        self.assertEqual(self.f._squares_on(tif, [sq]), [sq])
        far = (px - 100.0, py - 400.0, px + 100.0, py - 200.0)      # beyond the outline and its 100 m: left out
        self.assertEqual(self.f._squares_on(tif, [far]), [])
        box = self.f._outline_box(tif)
        self.assertAlmostEqual(box[1], py, delta=1.0)

    def test_image_tiles_are_asked_on_the_cell_lattice_and_an_old_tile_is_not_reused(self):
        import urllib.parse
        asked = []

        def opener(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
            w, h = (int(v) for v in q["size"][0].split(","))
            asked.append((x0, y0, x1, y1))
            return image_bytes(x0, y0, x1, y1, w, h)
        url = "https://example.test/ImageServer"

        def fetch(box, cell, name, snap):
            vrt = self.f.fetch_imageserver(url, box, cell, os.path.join(self.tmp, name), tile_px=100,
                                           log=lambda *_: None, opener=opener, snap=snap)
            ds = gdal.Open(vrt)
            gt = ds.GetGeoTransform()
            ds = None
            return gt

        def on(v, cell):
            return abs(round(v / cell) * cell - v) < 1e-6
        # snap=True (the open-water tool): every request's corners on whole multiples of the
        # cell, a cell (0.6 m) that does not divide the 100 m of a tile
        box = (X0 + 100.37, Y0 + 50.21, X0 + 190.0, Y0 + 131.1)
        gt = fetch(box, 0.6, "snap", True)
        self.assertEqual(len(asked), 4)                      # 2 x 2 tiles of 100 px
        self.assertTrue(all(on(v, 0.6) for a in asked for v in a), asked)
        self.assertTrue(on(gt[0], 0.6) and on(gt[3], 0.6), gt)
        # a tile kept from an older run that was not snapped, whose rounded file name is the one
        # this run asks for (corner 0.4 m and 0.3 m off the lattice: both round down), is
        # downloaded again, not read as if it were on the lattice
        del asked[:]
        old = (X0 + 0.4, Y0 + 0.3, X0 + 80.4, Y0 + 80.3)
        fetch(old, 1.0, "old", False)
        self.assertEqual(len(asked), 1)
        gt = fetch(old, 1.0, "old", True)
        self.assertEqual(len(asked), 2)
        self.assertEqual((gt[0], gt[3]), (X0, Y0 + 81.0))
        fetch(old, 1.0, "old", True)                         # the lattice tile is now kept: reused
        self.assertEqual(len(asked), 2)
        # snap=False (the evidence tool's windows, read with read_bands(snap=False)): its own
        # tiles are reused as they are
        fetch(old, 1.0, "own", False)
        gt = fetch(old, 1.0, "own", False)
        self.assertEqual(len(asked), 3)
        self.assertEqual((round(gt[0], 6), round(gt[3], 6)), (X0 + 0.4, Y0 + 80.3))
        # a kept tile that cannot be opened is downloaded again
        tif = [n for n in os.listdir(os.path.join(self.tmp, "own")) if n.endswith(".tif")]
        self.assertEqual(len(tif), 1)
        with open(os.path.join(self.tmp, "own", tif[0]), "wb") as fh:
            fh.write(b"II*\x00 cut short")
        fetch(old, 1.0, "own", False)
        self.assertEqual(len(asked), 4)


def image_bytes(x0, y0, x1, y1, w, h):
    """The bytes of a 4-band GeoTIFF over (x0, y0, x1, y1) of w x h pixels: an image service's answer."""
    mem = "/vsimem/answer_{}_{}.tif".format(x0, y0)
    ds = gdal.GetDriverByName("GTiff").Create(mem, w, h, 4, gdal.GDT_Byte)
    ds.SetGeoTransform((x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h))
    ds.SetProjection(srs(5070).ExportToWkt())
    for b in range(1, 5):
        ds.GetRasterBand(b).WriteArray(np.full((h, w), 50 * b, np.uint8))
    ds = None
    fh = gdal.VSIFOpenL(mem, "rb")
    gdal.VSIFSeekL(fh, 0, 2)
    n = gdal.VSIFTellL(fh)
    gdal.VSIFSeekL(fh, 0, 0)
    data = gdal.VSIFReadL(1, n, fh)
    gdal.VSIFCloseL(fh)
    gdal.Unlink(mem)
    return bytes(data)


# ------------------------------------------------------------------ evidence on the review cards --
N_CARDS = 30
QUIET = lambda *_: None                                         # noqa: E731


def card_x(i):
    return X0 + 100.0 * i


class Resp:
    """What urllib.request.urlopen answers with, for a faked image service."""

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.data


def service_tiff(url, wet_every):
    """A 4-band GeoTIFF (R, G, B, NIR) for an exportImage request: open water (low NIR) over the
    whole tile when the card under it (cards are 100 m apart) has an index divisible by wet_every."""
    import urllib.parse
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
    w, h = (int(v) for v in q["size"][0].split(","))
    wet = int(round(((x0 + x1) / 2.0 - X0) / 100.0)) % wet_every == 0
    mem = "/vsimem/card_svc_{}.tif".format(abs(hash(url)))
    ds = gdal.GetDriverByName("GTiff").Create(mem, w, h, 4, gdal.GDT_Byte)
    ds.SetGeoTransform((x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h))
    ds.SetProjection(srs(5070).ExportToWkt())
    for k, v in enumerate((60, 90, 50, 20 if wet else 150)):
        ds.GetRasterBand(k + 1).WriteArray(np.full((h, w), v, np.uint8))
    ds = None
    fh = gdal.VSIFOpenL(mem, "rb")
    gdal.VSIFSeekL(fh, 0, 2)
    n = gdal.VSIFTellL(fh)
    gdal.VSIFSeekL(fh, 0, 0)
    data = gdal.VSIFReadL(1, n, fh)
    gdal.VSIFCloseL(fh)
    gdal.Unlink(mem)
    return bytes(data)


@unittest.skipIf(gdal is None, "no GDAL (osgeo) in this Python")
class Cards(unittest.TestCase):
    """The water evidence on the review cards, and review_order from the evidence tool. Every
    test is offline: imagery is a local GeoTIFF or a faked urllib.request.urlopen."""

    def setUp(self):
        from .. import finder
        self.f = finder
        self.tmp = tempfile.mkdtemp(prefix="cards_")
        self.out = os.path.join(self.tmp, "rb.gpkg")
        self.rows = [{"x": card_x(i), "y": Y0, "find_id": "FIND-{:08X}".format(i), "kind": "dam", "status": "new",
                      "area_ac": 0.5, "height_ft": 5.0 + i, "storage_af": 2.0, "score": round(0.02 + 0.03 * i, 3),
                      "length_ft": 120.0} for i in range(N_CARDS)]
        self.fields = [("find_id", "str"), ("kind", "str"), ("status", "str"), ("area_ac", "float"),
                       ("height_ft", "float"), ("storage_af", "float"), ("score", "float"), ("length_ft", "float")]
        self.fills = self.layer("fills.gpkg", self.rows, self.fields)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def layer(self, name, rows, fields):
        path = os.path.join(self.tmp, name)
        self.f.write_points(path, "embankments", fields, rows)
        return path + "|layername=embankments"

    def image(self, name, wet_every):
        """A 4-band 1 m image over every card: open water within 30 m of each wet_every-th card."""
        path = os.path.join(self.tmp, name)
        nx, ny = 100 * N_CARDS + 200, 400
        ds = gdal.GetDriverByName("GTiff").Create(path, nx, ny, 4, gdal.GDT_Byte)
        ds.SetGeoTransform((X0 - 100.0, 1.0, 0.0, Y0 + 200.0, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        yy, xx = np.mgrid[0:ny, 0:nx]
        x, y = X0 - 100.0 + xx + 0.5, Y0 + 200.0 - yy - 0.5
        wet = np.zeros((ny, nx), bool)
        for i in range(0, N_CARDS, wet_every):
            wet |= (x - card_x(i)) ** 2 + (y - Y0) ** 2 < 30.0 ** 2
        for b, a in ((1, np.full(wet.shape, 60)), (2, np.full(wet.shape, 90)), (3, np.full(wet.shape, 50)),
                     (4, np.where(wet, 20, 150))):
            ds.GetRasterBand(b).WriteArray(a.astype(np.uint8))
        ds = None
        return path

    def two_years(self):
        return "2014 | {} | red=1 green=2 blue=3 nir=4\n2018 | {} | red=1 green=2 blue=3 nir=4".format(
            self.image("y2014.tif", 2), self.image("y2018.tif", 3))

    def nwi_layer(self, epsg=4326):
        return write_layer(os.path.join(self.tmp, "nwi{}.gpkg".format(epsg)), "nwi", [
            ("POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(
                a=card_x(i) - 20.0, b=card_x(i) + 20.0, c=Y0 - 20.0, d=Y0 + 20.0), {"ATTRIBUTE": "PABFh"})
            for i in range(0, N_CARDS, 3)], epsg=epsg, fields=("ATTRIBUTE",))

    def batch(self, **kw):
        kw.setdefault("log", QUIET)
        kw.setdefault("n", 10)
        kw.setdefault("per_tile_cap", 10)
        return self.f.run_review_batch(self.fills, self.out, **kw)

    def on_disk(self, path=None):
        ds = ogr.Open(path or self.out)
        lyr = ds.GetLayerByName("review_batch")
        d = lyr.GetLayerDefn()
        names = [d.GetFieldDefn(j).GetName() for j in range(d.GetFieldCount())]
        return names, [f.items() for f in lyr]

    def run_info(self, path=None):
        ds = ogr.Open(path or self.out)
        return {f.GetField("key"): f.GetField("value") for f in ds.GetLayerByName("run_info")}

    def pick(self, rows):
        return [tuple(r.get(k) for k in self.f.PICK_FIELDS) for r in rows]

    # ------------------------------------------------------------------ what the card carries --
    def test_cards_carry_water_years_nwi_and_lidar(self):
        import csv
        nwi = self.nwi_layer()
        a = np.full((400, 100 * N_CARDS + 200), 6, np.int32)                # 6 returns a cell ...
        yy, xx = np.mgrid[0:a.shape[0], 0:a.shape[1]]
        for i in range(0, N_CARDS, 4):                                      # ... and a pond void
            a[(X0 - 100.0 + xx + 0.5 - card_x(i)) ** 2 + (200.0 - yy - 0.5) ** 2 < 22.0 ** 2] = 0
        tif = os.path.join(self.tmp, "points.tif")
        ds = gdal.GetDriverByName("GTiff").Create(tif, a.shape[1], a.shape[0], 1, gdal.GDT_Int32)
        ds.SetGeoTransform((X0 - 100.0, 1.0, 0.0, Y0 + 200.0, 0.0, -1.0))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).SetNoDataValue(0)
        ds.GetRasterBand(1).WriteArray(a)
        ds = None
        vrt = os.path.join(self.tmp, "point_count.vrt")
        gdal.BuildVRT(vrt, [tif])
        res = self.batch(images=self.two_years(), nwi=nwi, lidar_points=vrt)
        self.assertEqual(len(res["rows"]), 10)
        self.assertTrue(any(r["nwi_code"] for r in res["rows"]))             # so the checks below can fail
        self.assertTrue(any(r["lidar_pool_empty"] for r in res["rows"]))
        for r in res["rows"]:
            i = int(r["find_id"][-8:], 16)
            want = int(i % 2 == 0) + int(i % 3 == 0)
            self.assertEqual(r["water_years"], "{} of 2".format(want), r["find_id"])
            self.assertEqual((r["wet_years"], r["img_years"]), (want, 2))
            self.assertEqual(r["nwi_code"], "PABFh" if i % 3 == 0 else "")
            self.assertEqual(r["nwi_h"], "yes" if i % 3 == 0 else "no")
            if i % 4 == 0:
                self.assertGreater(r["lidar_pool_empty"], 0.5)
            else:
                self.assertEqual(r["lidar_pool_empty"], 0.0)
            self.assertEqual(r["evid_note"], "")
            # the card shows CARD_FIELDS, not every field the evidence tool writes
            for k in ("first_wet", "bare_share", "nwi_regime", "lidar_ring_empty", "lidar_class9", "permit_check"):
                self.assertNotIn(k, r)
        self.assertEqual(res["evidence"]["cards"], 10)
        self.assertEqual(res["evidence"]["requests"], 0)                    # local files: nothing asked
        # on disk: the evidence right after the batch's own fields, in the layer and in the CSV
        names, feats = self.on_disk()
        k = len(self.f.REVIEW_FIELDS)
        self.assertEqual(names[:k], [n for n, _t in self.f.REVIEW_FIELDS])
        self.assertEqual(names[k:k + len(self.f.CARD_FIELDS)], [n for n, _t in self.f.CARD_FIELDS])
        self.assertEqual(len(feats), 10)
        with open(res["csv"], newline="", encoding="utf-8") as fh:
            back = list(csv.DictReader(fh))
        self.assertEqual([r["water_years"] for r in back], [r["water_years"] for r in res["rows"]])
        self.assertIn("length_ft", back[0])                                 # the fill's own fields still ride along
        # run_info says this batch showed the evidence (its marks are not a blind test of it)
        self.assertEqual(self.run_info()["evidence"], "True")
        # the card's reading is the evidence tool's, by the same reader: same water, NWI and LiDAR
        ev = self.f.run_evidence(self.fills, os.path.join(self.tmp, "ev.gpkg"), images=self.two_years(), nwi=nwi,
                                 lidar_points=vrt, log=QUIET)
        tool = {r["find_id"]: r for r in ev["rows"]}
        for r in res["rows"]:
            t = tool[r["find_id"]]
            for key in ("wet_years", "img_years", "wet_share", "wet_list", "nwi_code", "nwi_h", "nwi_x",
                        "lidar_pool_empty", "lidar_contrast"):
                self.assertEqual(r[key], t[key], (r["find_id"], key))
        # an NWI layer in another CRS (UTM) is asked the same boxes
        utm = self.batch(nwi=self.nwi_layer(26914))["rows"]
        self.assertEqual([r["nwi_code"] for r in utm], [r["nwi_code"] for r in res["rows"]])
        self.assertTrue(any(r["nwi_code"] for r in utm))

    def test_an_nwi_layer_that_cannot_be_read_is_a_note(self):
        said = []
        res = self.batch(images=self.two_years(), nwi=os.path.join(self.tmp, "nowhere.gpkg"), log=said.append)
        self.assertTrue(all(r["water_years"] for r in res["rows"]))
        self.assertTrue(all("the NWI layer could not be read" in r["evid_note"] for r in res["rows"]))
        self.assertTrue(all(r.get("nwi_code") is None for r in res["rows"]))
        self.assertEqual(sum(1 for m in said if "the NWI layer could not be read" in m), 1)

    # ---------------------------------------------------------------- evidence never picks --
    def test_evidence_never_moves_the_pick(self):
        plain = self.batch(evidence=False)["rows"]
        with_ev = self.batch(images=self.two_years())["rows"]
        self.assertTrue(any(r.get("water_years") for r in with_ev))         # so the check below can fail
        self.assertEqual(self.pick(plain), self.pick(with_ev))
        # fills that already carry evidence (an 'evidence' layer as FILLS): water and buildings
        # on the lowest-scored cards change no card, rank or chance - with the evidence shown or not
        loaded = [dict(r, wet_share=1.0 if i < 5 else 0.0, wet_years=10 if i < 5 else 0, img_years=10,
                       nwi_code="PABFh" if i < 5 else "", nwi_h="yes" if i < 5 else "no",
                       lidar_contrast=0.9 if i < 5 else 0.0, conseq=50.0 if i < 5 else 0.0, bld_1mi=9 if i < 5 else 0,
                       review_order=i + 1)
                  for i, r in enumerate(self.rows)]
        self.fills = self.layer("with_evidence.gpkg", loaded, self.fields + [
            ("wet_share", "float"), ("wet_years", "int"), ("img_years", "int"), ("nwi_code", "str"), ("nwi_h", "str"),
            ("lidar_contrast", "float"), ("conseq", "float"), ("bld_1mi", "int"), ("review_order", "int")])
        self.assertEqual(self.pick(plain), self.pick(self.batch(evidence=False)["rows"]))
        self.assertEqual(self.pick(plain), self.pick(self.batch()["rows"]))

    def test_the_score_reads_no_evidence_field(self):
        from ..core import score as sc
        seen = set()

        class Row(dict):
            def get(self, k, d=None):
                seen.add(k)
                return dict.get(self, k, d)

            def __getitem__(self, k):
                seen.add(k)
                return dict.__getitem__(self, k)
        evid = ({n for n, _t in self.f.EVID_FIELDS} | {n for n, _t in self.f.CARD_FIELDS}
                | {n for n, _t in self.f.BELOW_FIELDS} | {self.f.ORDER_FIELD[0]})
        base = dict(self.rows[7], pool="yes", crest_height_ft=9.0, head_ft=6.0, relief_ft=30.0, drain_ac=40.0)
        loud = dict(base, wet_share=1.0, wet_years=10, img_years=10, wet_list="2009,2010", water_years="10 of 10",
                    nwi_code="PUBHh", nwi_h="yes", nwi_x="yes", lidar_pool_empty=1.0, lidar_contrast=1.0,
                    lidar_class9=1.0, bare_share=1.0, conseq=99.0, bld_1mi=50, roads_1mi=4, review_order=1,
                    evid_note="x")
        folder = os.path.dirname(os.path.abspath(self.f.__file__))
        for name in sorted(os.listdir(os.path.join(folder, "models"))):
            model = sc.load(os.path.join(folder, "models", name))
            seen.clear()
            a = sc.score_detail(model, [Row(base)])
            b = sc.score_detail(model, [Row(loud)])
            self.assertEqual(a, b, name)
            self.assertTrue(seen, name)
            self.assertEqual(seen & evid, set(), name)
        # every feature the score could be trained on, whether or not a shipped model uses it
        seen.clear()
        for name, fn in sc._FN.items():
            fn(Row(loud))
            self.assertEqual(seen & evid, set(), name)
        sc.terrain_group(Row(loud))
        self.assertEqual(seen & evid, set())

    def test_a_changed_pick_is_refused(self):
        from unittest import mock

        def meddle(rows, **_k):
            rows[0]["incl_prob"] = 1.0
            rows[0]["rank"] = 1
            return {}
        with mock.patch.object(self.f, "_fill_evidence", meddle):
            with self.assertRaises(RuntimeError):
                self.batch(images=self.two_years())
        names, feats = self.on_disk()                                       # the batch as picked is on disk
        self.assertEqual(len(feats), 10)
        self.assertNotIn("water_years", names)

    def test_the_pick_fields_are_pinned_and_each_is_checked(self):
        from unittest import mock
        self.assertEqual(self.f.PICK_FIELDS, ("n", "find_id", "stratum", "rank", "incl_prob", "second", "score",
                                              "list_n", "pool_n"))
        for k, v in (("incl_prob", 0.5), ("second", "flip"), ("stratum", "other"), ("n", 99)):
            def meddle(rows, k=k, v=v, **_k):
                rows[-1][k] = v                                          # this field alone
                return {}
            with mock.patch.object(self.f, "_fill_evidence", meddle):
                with self.assertRaises(RuntimeError, msg=k):
                    self.batch(images=self.two_years())

    def test_the_cards_read_the_image_on_the_pool_window_itself(self):
        # the image tiles of a card are laid out from its pool window's own corner, so they are
        # read on that window (read_bands snap=False), not moved to the cell lattice and resampled
        from unittest import mock
        seen = []
        real = self.f.read_bands

        def spy(image, ext, cell, bands, snap=True):
            seen.append(snap)
            return real(image, ext, cell, bands, snap=snap)
        with mock.patch.object(self.f, "read_bands", spy):
            with mock.patch("urllib.request.urlopen", lambda url, timeout=None: Resp(service_tiff(url, 2))):
                self.batch(images=self.hub_lines(years=(2014,)) + "\n2018 | {} | red=1 green=2 blue=3 nir=4".format(
                    self.image("y2018.tif", 3)))
        self.assertEqual(len(seen), 2 * 10)
        self.assertEqual(set(seen), {False})

    def test_no_image_seen_is_blank_on_a_card_from_an_evidence_layer(self):
        # fills from an 'evidence' layer made when no image could be read (img_years 0,
        # wet_years 0): with no imagery given now, the card's wet_years is blank, not 0
        loaded = [dict(r, wet_years=0, img_years=0, wet_share=None) for r in self.rows]
        self.fills = self.layer("ev0.gpkg", loaded, self.fields + [("wet_years", "int"), ("img_years", "int"),
                                                                  ("wet_share", "float")])
        res = self.batch()
        self.assertTrue(all(r.get("wet_years") is None and r.get("water_years") is None for r in res["rows"]))
        names, feats = self.on_disk()
        self.assertTrue(all(f["wet_years"] is None and f["water_years"] is None for f in feats))
        self.assertTrue(all("no imagery given" in f["evid_note"] for f in feats))

    def test_a_fill_at_the_edge_of_the_terrain_data_is_never_picked(self):
        from ..core import embankments as em
        rows = [dict(r, kind=em.K_EDGE, score=0.99) if i % 3 == 0 else r for i, r in enumerate(self.rows)]
        self.fills = self.layer("edge.gpkg", rows, self.fields)
        for kinds in ((), (em.K_DAM, em.K_EDGE), (em.K_DAM, em.K_DRY)):
            res = self.f.run_review_batch(self.fills, self.out, n=15, per_tile_cap=30, kinds=kinds, evidence=False,
                                          log=QUIET)
            self.assertEqual(len(res["rows"]), 15)
            self.assertFalse(any(r["kind"] == em.K_EDGE for r in res["rows"]), kinds)

    # ------------------------------------------------------------------- offline / failures --
    def hub_lines(self, hosts=("hub.test",), years=(2009, 2010, 2012, 2014, 2015)):
        return "\n".join("{} | https://{}/svc{}/ImageServer | red=1 green=2 blue=3 nir=4".format(y, h, y)
                         for h in hosts for y in years)

    def test_offline_the_batch_is_still_written(self):
        from unittest import mock
        import urllib.error
        asked, said = [], []

        def dead(url, timeout=None):
            asked.append((url, timeout))
            raise urllib.error.URLError("no network")
        with mock.patch("urllib.request.urlopen", dead):
            res = self.batch(images=self.hub_lines(), image_timeout=7.0, log=said.append)
        names, feats = self.on_disk()
        self.assertEqual(len(feats), 10)
        self.assertEqual([f["find_id"] for f in feats], [r["find_id"] for r in res["rows"]])
        self.assertTrue(all(f["water_years"] is None and f["wet_years"] is None for f in feats))   # not '0 of 0'
        self.assertTrue(all("no image year could be read" in f["evid_note"] for f in feats))
        # the host is given up on after the first card, each of its 5 years asked twice: 10
        # requests, not 5 x 10 cards
        self.assertEqual(len(asked), 10)
        self.assertEqual(res["evidence"]["requests"], 10)
        self.assertEqual({t for _u, t in asked}, {7.0})                     # the batch's own short time-out
        self.assertEqual(sum(1 for m in said if "not asked again this run" in m), 1)
        self.assertEqual(sum(1 for m in said if "no image" in m), 5)        # the first card's years, no more
        self.assertEqual(res["evidence"]["dead_services"], ["hub.test"])
        # the pick is the pick of a batch with the evidence off
        self.assertEqual(self.pick(res["rows"]), self.pick(self.batch(evidence=False)["rows"]))

    def test_an_error_page_is_no_image_and_one_dead_service_leaves_the_other(self):
        # page.test answers every request with an error page (an answer: no image for that fill,
        # asked again for the next); dead.test gives no answer at all (left alone)
        from unittest import mock
        import urllib.error
        asked = []

        def half(url, timeout=None):
            asked.append(url)
            if "page.test" in url:
                return Resp(b"<html>Error 500</html>")
            if "dead.test" in url:
                raise urllib.error.URLError("refused")
            return Resp(service_tiff(url, 2))
        with mock.patch("urllib.request.urlopen", half):
            res = self.batch(images=self.hub_lines(hosts=("page.test", "dead.test", "live.test"),
                                                   years=(2014, 2015, 2016)))
        for r in res["rows"]:
            i = int(r["find_id"][-8:], 16)
            self.assertEqual(r["water_years"], "{} of 3".format(3 if i % 2 == 0 else 0))
        self.assertEqual(res["evidence"]["dead_services"], ["dead.test"])
        self.assertEqual(sum(1 for u in asked if "page.test" in u), 3 * 10)       # every card, as before
        self.assertEqual(sum(1 for u in asked if "dead.test" in u), 3 * 2)        # first card, asked twice

    def test_a_broken_year_is_left_alone_and_never_stops_the_other_years(self):
        from unittest import mock
        asked, said = [], []
        down = ("/svc2012/", "/svc2014/", "/svc2015/")                      # three years in a row are down

        import urllib.error

        def svc(url, timeout=None):
            asked.append(url)
            if any(d in url for d in down):                                 # 'service unavailable'
                raise urllib.error.HTTPError(url, 503, "Service Unavailable", None, None)
            return Resp(service_tiff(url, 2))
        with mock.patch("urllib.request.urlopen", svc):
            res = self.batch(images=self.hub_lines(), log=said.append)
        notes = []
        for r in res["rows"]:                                               # the host goes on answering
            i = int(r["find_id"][-8:], 16)
            self.assertEqual(r["water_years"], "{} of 2".format(2 if i % 2 == 0 else 0))
            notes.append(r["evid_note"])
        # the 3 cards that asked say the years could not be read; the 7 after them also say why
        # they were not asked
        plain = "3 of 5 image years could not be read; no NWI layer given; no LiDAR count raster given"
        self.assertEqual(notes[:3], [plain] * 3)
        self.assertEqual(notes[3:], ["3 of 5 image years could not be read; image year 2012, 2014, 2015 not "
                                     "asked (its service gave no answer earlier in this run); no NWI layer "
                                     "given; no LiDAR count raster given"] * 7)
        # each broken year is asked for 3 cards, then left alone: one line each, not one a card
        self.assertEqual(sum(1 for u in asked if any(d in u for d in down)), 3 * 3)
        self.assertEqual(len(asked), 2 * 10 + 3 * 3)
        self.assertEqual(sum(1 for m in said if "not asked again this run" in m), 3)
        self.assertEqual(res["evidence"]["dead_services"],
                         ["https://hub.test/svc{}/ImageServer".format(y) for y in (2012, 2014, 2015)])

    def test_a_gap_in_one_year_never_takes_the_year_from_the_fills_beyond_it(self):
        # a year whose service answers HTTP 500 over a stretch of the cards (a gap in that year's
        # imagery, 4 neighbouring cards): only those cards lack the year; every card beyond the
        # gap is still asked and read (as Finder 0.6.0 did), and no card says 'not asked'
        from unittest import mock
        import urllib.error
        asked = []

        def svc(url, timeout=None):
            asked.append(url)
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            x0, _y0, x1, _y1 = (float(v) for v in q["bbox"][0].split(","))
            i = int(round(((x0 + x1) / 2.0 - X0) / 100.0))
            if "/svc2012/" in url and 5 <= i <= 12:
                raise urllib.error.HTTPError(url, 500, "Internal Server Error", None, None)
            return Resp(service_tiff(url, 2))
        rows = [dict(r) for r in self.rows[:20]]
        with mock.patch("urllib.request.urlopen", svc):
            c = self.f._fill_evidence(rows, images=self.hub_lines(), cache=os.path.join(self.tmp, "gap"), log=QUIET)
        got = [(int(r["find_id"][-8:], 16), r["img_years"]) for r in rows]
        self.assertEqual(got, [(i, 4 if 5 <= i <= 12 else 5) for i in range(20)])
        self.assertEqual(c["dead_services"], [])
        self.assertEqual(len(asked), 5 * 20)
        self.assertFalse(any("img_skipped" in r for r in rows))

    def test_the_evidence_tool_says_which_year_was_not_asked(self):
        # a year whose service gives no answer: left alone after 3 fills, and each fill after them
        # carries img_skipped (the years); a run where nothing was left alone has no such field
        from unittest import mock
        import urllib.error

        def svc(url, timeout=None):
            if "/svc2012/" in url:
                raise urllib.error.URLError("timed out")
            return Resp(service_tiff(url, 2))
        fills = self.layer("ev_in.gpkg", self.rows[:6], self.fields)
        out = os.path.join(self.tmp, "ev.gpkg")
        with mock.patch("urllib.request.urlopen", svc):
            res = self.f.run_evidence(fills, out, images=self.hub_lines(), log=QUIET)
        self.assertEqual([r.get("img_skipped") for r in res["rows"]], [None] * 3 + ["2012"] * 3)
        self.assertEqual([r["img_years"] for r in res["rows"]], [4] * 6)
        ds = ogr.Open(out)
        d = ds.GetLayerByName("evidence").GetLayerDefn()
        names = [d.GetFieldDefn(j).GetName() for j in range(d.GetFieldCount())]
        ds = None
        self.assertEqual(names[-2:], ["img_skipped", "review_order"])
        with mock.patch("urllib.request.urlopen", lambda url, timeout=None: Resp(service_tiff(url, 2))):
            res = self.f.run_evidence(out + "|layername=evidence", os.path.join(self.tmp, "ev2.gpkg"),
                                      images=self.hub_lines(), log=QUIET)
        self.assertFalse(any(r.get("img_skipped") for r in res["rows"]))   # an old note is not carried
        ds = ogr.Open(os.path.join(self.tmp, "ev2.gpkg"))
        d = ds.GetLayerByName("evidence").GetLayerDefn()
        self.assertLess(d.GetFieldIndex("img_skipped"), 0)
        ds = None

    def test_a_blank_answer_is_not_kept_as_a_tile(self):
        # a service under load can answer with an image that holds no data: the card says a year
        # is missing, and the next run asks for that tile again instead of reading the blank one
        from unittest import mock
        import urllib.parse
        state = {"blank": True, "asked": []}

        def blank(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
            w, h = (int(v) for v in q["size"][0].split(","))
            mem = "/vsimem/blank_{}.tif".format(len(state["asked"]))
            ds = gdal.GetDriverByName("GTiff").Create(mem, w, h, 4, gdal.GDT_Byte)      # all zeros
            ds.SetGeoTransform((x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h))
            ds.SetProjection(srs(5070).ExportToWkt())
            ds = None
            fh = gdal.VSIFOpenL(mem, "rb")
            gdal.VSIFSeekL(fh, 0, 2)
            n = gdal.VSIFTellL(fh)
            gdal.VSIFSeekL(fh, 0, 0)
            data = gdal.VSIFReadL(1, n, fh)
            gdal.VSIFCloseL(fh)
            gdal.Unlink(mem)
            return bytes(data)

        def svc(url, timeout=None):
            state["asked"].append(url)
            if state["blank"] and "/svc2012/" in url:
                return Resp(blank(url))
            return Resp(service_tiff(url, 2))
        with mock.patch("urllib.request.urlopen", svc):
            first = self.batch(images=self.hub_lines())
            self.assertEqual(len(state["asked"]), 50)
            self.assertTrue(all(r["img_years"] == 4 for r in first["rows"]))
            self.assertTrue(all(r["evid_note"].startswith("1 of 5 image years could not be read")
                                for r in first["rows"]))
            self.assertEqual(first["evidence"]["dead_services"], [])        # an answer, if an empty one
            state["blank"] = False
            again = self.batch(images=self.hub_lines())
        self.assertEqual(len(state["asked"]), 50 + 10)                      # only the blank tiles again
        self.assertTrue(all(r["img_years"] == 5 for r in again["rows"]))
        self.assertTrue(all(not r["evid_note"].startswith("1 of 5") for r in again["rows"]))

    def test_a_tile_cut_short_is_asked_for_again(self):
        # a tile whose header opens and whose pixels do not (a download cut short): the card says
        # the year could not be read, and the next run asks for that tile again
        from unittest import mock
        state = {"cut": True, "asked": []}

        def svc(url, timeout=None):
            state["asked"].append(url)
            data = service_tiff(url, 2)
            if state["cut"] and "/svc2012/" in url:
                data = data[:len(data) // 2]
            return Resp(data)
        with mock.patch("urllib.request.urlopen", svc):
            first = self.batch(images=self.hub_lines())
            self.assertEqual(len(state["asked"]), 50)
            self.assertTrue(all(r["img_years"] == 4 for r in first["rows"]))
            self.assertTrue(all(r["evid_note"].startswith("1 of 5 image years could not be read")
                                for r in first["rows"]))
            state["cut"] = False
            again = self.batch(images=self.hub_lines())
        self.assertEqual(len(state["asked"]), 50 + 10)                      # only the broken tiles again
        self.assertTrue(all(r["img_years"] == 5 for r in again["rows"]))

    def test_a_long_run_asks_a_silent_host_again_after_a_while(self):
        # the evidence tool's rule (retry_s): the host is down for the first two fills, then back
        from unittest import mock
        import urllib.error
        state = {"down": True, "asked": 0}

        def svc(url, timeout=None):
            state["asked"] += 1
            if state["down"]:
                raise urllib.error.URLError("no network")
            return Resp(service_tiff(url, 2))

        def read(retry_s, name):
            rows = [dict(r) for r in self.rows[:6]]
            state.update(down=True, asked=0)
            said = []

            def progress(pct):
                if pct >= 100.0 * 2 / 6 - 1e-6:                             # two fills read: the network is back
                    state["down"] = False
            with mock.patch("urllib.request.urlopen", svc):
                c = self.f._fill_evidence(rows, images=self.hub_lines(), cache=os.path.join(self.tmp, name),
                                          retry_s=retry_s, log=said.append, progress=progress)
            return rows, c, said
        rows, c, said = read(None, "never")                                 # the batch: not asked again this run
        self.assertEqual([r["img_years"] for r in rows], [0] * 6)
        self.assertEqual((state["asked"], c["dead_services"]), (10, ["hub.test"]))
        rows, c, said = read(0.0, "again")                                  # asked again once the wait is over
        self.assertEqual([r["img_years"] for r in rows], [0, 0, 5, 5, 5, 5])
        self.assertEqual(c["dead_services"], [])
        self.assertEqual(sum(1 for m in said if "not asked again" in m), 1)
        self.assertEqual(sum(1 for m in said if "answers again" in m), 1)
        rows, c, said = read(3600.0, "later")                               # the wait is not over: left alone
        self.assertEqual([r["img_years"] for r in rows], [0] * 6)
        self.assertEqual(state["asked"], 10)

    def test_a_rerun_reads_the_tiles_it_has(self):
        from unittest import mock
        asked = []

        def svc(url, timeout=None):
            asked.append(url)
            return Resp(service_tiff(url, 2))
        with mock.patch("urllib.request.urlopen", svc):
            first = self.batch(images=self.hub_lines())
            n1 = len(asked)
            again = self.batch(images=self.hub_lines())
        self.assertEqual(n1, 5 * 10)
        self.assertEqual(first["evidence"]["requests"], 50)
        self.assertEqual(len(asked), n1)                                    # nothing downloaded twice
        self.assertEqual(again["evidence"]["requests"], 0)
        self.assertEqual([r["water_years"] for r in first["rows"]], [r["water_years"] for r in again["rows"]])
        self.assertTrue(os.path.isdir(os.path.splitext(self.out)[0] + "_imagery"))     # kept beside the output

    def test_evidence_off_is_todays_batch_and_asks_nothing(self):
        from unittest import mock

        def never(url, timeout=None):
            raise AssertionError("the network was asked: {}".format(url))
        with mock.patch("urllib.request.urlopen", never):
            self.batch(evidence=False, images=self.hub_lines())
            names, feats = self.on_disk()
            self.assertEqual(self.run_info()["evidence"], "False")
            # and with no source at all, the default of the finder function (the brand's image list is
            # the default of the QGIS tool, not of finder.run_review_batch: scripts and tests stay offline)
            res = self.batch()
            names2, _f = self.on_disk()
        self.assertIsNone(res["evidence"])
        # the batch of Finder 0.6: its own fields, then the fill's
        want = [n for n, _t in self.f.REVIEW_FIELDS] + ["length_ft"]
        self.assertEqual(names, want)
        self.assertEqual(names2, want)
        self.assertEqual(len(feats), 10)
        import inspect
        sig = inspect.signature(self.f.run_review_batch).parameters
        self.assertEqual((sig["evidence"].default, sig["images"].default, sig["image_timeout"].default),
                         (True, None, 30.0))

    def test_a_crash_in_the_evidence_step_leaves_the_batch(self):
        from unittest import mock
        said = []
        with mock.patch.object(self.f, "_fill_evidence", side_effect=MemoryError("boom")):
            res = self.batch(images=self.two_years(), log=said.append)
        names, feats = self.on_disk()
        self.assertEqual(len(feats), 10)
        self.assertNotIn("water_years", names)
        self.assertTrue(any("the batch is written without it" in m for m in said))
        self.assertIsNone(res["evidence"])
        self.assertNotIn("water_years", res["rows"][0])
        # an image line that cannot be read is the same: a warning, the batch as picked
        said = []
        res = self.batch(images="2018 | x.tif | red=1", log=said.append)
        self.assertTrue(any("the batch is written without it" in m and "green= and nir=" in m for m in said))
        self.assertEqual(len(self.on_disk()[1]), 10)

    def test_a_cancel_during_the_evidence_keeps_every_card(self):
        calls = []

        def cancelled():
            calls.append(1)
            return len(calls) > 3
        res = self.batch(images=self.two_years(), cancelled=cancelled)
        names, feats = self.on_disk()
        self.assertEqual(len(feats), 10)
        self.assertEqual(sum(1 for f in feats if f["water_years"]), 3)
        self.assertEqual(res["evidence"]["cards"], 3)
        self.assertTrue(res["cancelled"])
        self.assertEqual(self.run_info()["cancelled"], "True")

    def test_a_card_with_a_water_history_keeps_it(self):
        from unittest import mock
        loaded = [dict(r, wet_years=7, img_years=10, wet_share=0.7, wet_list="2009,2010") for r in self.rows]
        self.fills = self.layer("ev.gpkg", loaded, self.fields + [("wet_years", "int"), ("img_years", "int"),
                                                                  ("wet_share", "float"), ("wet_list", "str")])

        def never(url, timeout=None):
            raise AssertionError("asked for an image the card already has")
        with mock.patch("urllib.request.urlopen", never):
            res = self.batch(images=self.hub_lines())
            self.assertTrue(all(r["water_years"] == "7 of 10" for r in res["rows"]))
            # and with no source given at all: what the fills carry is still put on the card
            res = self.batch()
        self.assertTrue(all(r["water_years"] == "7 of 10" for r in res["rows"]))
        names, _f = self.on_disk()
        k = len(self.f.REVIEW_FIELDS)
        self.assertEqual(names[k:k + len(self.f.CARD_FIELDS)], [n for n, _t in self.f.CARD_FIELDS])

    def test_the_brands_default_image_list_parses(self):
        from .. import brand
        got = self.f.parse_images(brand.EVIDENCE_IMAGES)
        self.assertTrue(got)
        self.assertTrue(all(self.f.is_service(d["src"]) and "green" in d and "nir" in d for d in got))

    # ------------------------------------------------------- what is below: shown, never used --
    def below_fills(self):
        """Fills as the evidence tool leaves them when it had a DEM and buildings: the LOWEST scores
        have the most below them."""
        loaded = [dict(r, bld_1mi=N_CARDS - i, bld_3mi=0, roads_1mi=i % 3, conseq=3.0 * (N_CARDS - i) + i % 3)
                  for i, r in enumerate(self.rows)]
        return self.layer("below.gpkg", loaded, self.fields + [("bld_1mi", "int"), ("bld_3mi", "int"),
                                                                ("roads_1mi", "int"), ("conseq", "float")])

    def test_what_is_below_is_shown_on_the_card_and_never_picks(self):
        from unittest import mock
        plain = self.batch(evidence=False)["rows"]
        self.fills = self.below_fills()
        with mock.patch.object(self.f, "_consequences", side_effect=AssertionError("the batch computed what is below")):
            res = self.batch(images=self.two_years())
        self.assertEqual(self.pick(res["rows"]), self.pick(plain))          # same cards, ranks and chances
        names, feats = self.on_disk()
        k = len(self.f.REVIEW_FIELDS) + len(self.f.CARD_FIELDS)
        self.assertEqual(names[k:k + 3], ["bld_1mi", "roads_1mi", "conseq"])
        self.assertIn("bld_3mi", names[k + 3:])                             # the rest ride along as before
        for f in feats:
            i = int(f["find_id"][-8:], 16)
            self.assertEqual((f["bld_1mi"], f["roads_1mi"], f["conseq"]), (N_CARDS - i, i % 3, 3.0 * (N_CARDS - i) + i % 3))
        # the cards are not in the order of what is below them (the batch is shuffled)
        self.assertNotEqual([f["conseq"] for f in feats], sorted((f["conseq"] for f in feats), reverse=True))
        # fills with nothing below computed: the three fields are not on the card
        self.fills = self.layer("fills2.gpkg", self.rows, self.fields)
        self.batch(images=self.two_years())
        self.assertNotIn("conseq", self.on_disk()[0])

    def test_evidence_tool_writes_review_order(self):
        import csv
        # scores in three bands; inside a band the fill with more below it comes first
        rows = []
        for i, (score, conseq) in enumerate(((0.95, 0.0), (0.91, 6.0), (0.84, 40.0), (0.31, 2.0), (0.39, 0.0),
                                             (0.30, 9.0), (None, 99.0))):
            rows.append(dict(self.rows[i], score=score, conseq=conseq, bld_1mi=int(conseq // 3)))
        fills = self.layer("scored.gpkg", rows, self.fields + [("conseq", "float"), ("bld_1mi", "int")])
        said = []
        out = os.path.join(self.tmp, "ev.gpkg")
        ev = self.f.run_evidence(fills, out, log=said.append)
        self.assertEqual([r["review_order"] for r in ev["rows"]], [2, 1, 3, 5, 6, 4, 7])
        self.assertTrue(any("review_order" in m and "the layer keeps the fills' own order" in m for m in said))
        # the layer keeps the fills' order; the CSV is in review order
        ds = ogr.Open(out)
        lyr = ds.GetLayerByName("evidence")
        d = lyr.GetLayerDefn()
        self.assertEqual(d.GetFieldDefn(d.GetFieldCount() - 1).GetName(), "review_order")
        self.assertEqual([f["find_id"] for f in lyr], [r["find_id"] for r in rows])
        ds = None
        with open(ev["csv"], newline="", encoding="utf-8") as fh:
            back = list(csv.DictReader(fh))
        self.assertEqual([int(r["review_order"]) for r in back], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([r["find_id"] for r in back], [rows[i]["find_id"] for i in (1, 0, 2, 5, 3, 4, 6)])
        # the evidence layer back in: the order is worked out again, not carried, and a review
        # batch from it is the batch of the plain fills
        ev2 = self.f.run_evidence(out + "|layername=evidence", os.path.join(self.tmp, "ev2.gpkg"), log=QUIET)
        self.assertEqual([r["review_order"] for r in ev2["rows"]], [2, 1, 3, 5, 6, 4, 7])
        # fills with no score: no order, the CSV as the layer
        noscore = self.layer("plain.gpkg", [dict(r, score=None) for r in rows], self.fields + [("conseq", "float")])
        ev3 = self.f.run_evidence(noscore, os.path.join(self.tmp, "ev3.gpkg"), log=QUIET)
        self.assertNotIn("review_order", ev3["rows"][0])
        with open(ev3["csv"], newline="", encoding="utf-8") as fh:
            back = list(csv.DictReader(fh))
        self.assertNotIn("review_order", back[0])
        self.assertEqual([r["find_id"] for r in back], [r["find_id"] for r in rows])

    def test_layers_are_read_through_their_index(self):
        # the buildings and roads of the consequence pass: the same features as a whole read
        pts = [("POINT ({} {})".format(card_x(i), Y0 + 37.0 * (i % 5)), {}) for i in range(N_CARDS)]
        foot = [("POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(
            a=card_x(i) + 40.0, b=card_x(i) + 52.0, c=Y0 + 300.0, d=Y0 + 310.0), {}) for i in range(N_CARDS)]
        lines = [("LINESTRING ({} {}, {} {})".format(card_x(i), Y0 - 500.0, card_x(i) + 30.0, Y0 + 500.0), {})
                 for i in range(N_CARDS)]
        for epsg in (4326, 26914):
            vec = os.path.join(self.tmp, "idx{}.gpkg".format(epsg))
            b = write_layer(vec, "b", pts + foot, epsg=epsg)
            r = write_layer(vec, "r", lines, epsg=epsg)
            box = (card_x(4) - 5.0, Y0 - 50.0, card_x(11) + 5.0, Y0 + 400.0)
            got = self.f._points_of(b, box)
            self.assertEqual(len(got), 8 + 7)                               # points 4..11, outlines 4..10
            self.assertEqual(self.f._lines_in(r, box), self.f.read_lines(r, box))
            self.assertEqual(len(self.f._lines_in(r, box)), 8)              # roads 4..11 meet the box
            self.assertEqual(self.f._lines_in(r, (X0 - 9000.0, Y0, X0 - 8000.0, Y0 + 10.0)), [])

    def test_buildings_and_roads_default_to_the_data_pack(self):
        import json
        from unittest import mock
        from .. import brand
        if not hasattr(brand, "pack_layer"):
            self.skipTest("this brand has no offline data pack")
        try:
            from DamSafety_Core.damsuite import offline_pack, suitedata  # noqa: F401
        except ImportError:
            self.skipTest("DamSafety Core is not beside this plugin")
        pack = os.path.join(self.tmp, "ND_Dam_Suite_Pack")
        os.makedirs(os.path.join(pack, "roads"))
        os.makedirs(os.path.join(pack, "structures"))
        write_layer(os.path.join(pack, "roads", "ND_county_roads.gpkg"), "ND_county_roads",
                    [("LINESTRING ({} {}, {} {})".format(X0, Y0, X0 + 50.0, Y0), {})])
        write_layer(os.path.join(pack, "structures", "USA_Structures.gpkg"), "USA_Structures",
                    [("POINT ({} {})".format(X0, Y0), {})])
        with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"format": offline_pack.PACK_FORMAT, "format_version": 2, "layout": "gpkg", "datasets": {
                "ND_county_roads": {"file": "roads/ND_county_roads.gpkg"},
                "USA_Structures": {"file": "structures/USA_Structures.gpkg"},
                "NSI_structures": {"file": "structures/NSI_structures.gpkg"}}}, fh)     # listed, but not there
        with mock.patch.dict(os.environ, {"NDSUITE_PACK": pack, "NDSUITE_SOURCE": "pack"}):
            # the first data set of the brand's list that the pack really holds
            self.assertEqual(brand.pack_layer(brand.PACK_BUILDINGS), os.path.join(pack, "structures", "USA_Structures.gpkg"))
            self.assertEqual(brand.pack_layer(brand.PACK_ROADS), os.path.join(pack, "roads", "ND_county_roads.gpkg"))
            self.assertEqual(brand.pack_layer(("no_such_layer",)), "")
        with mock.patch.dict(os.environ, {"NDSUITE_PACK": os.path.join(self.tmp, "nowhere"), "NDSUITE_SOURCE": "pack",
                                          "NDSUITE_DATA": os.path.join(self.tmp, "nowhere")}):
            self.assertEqual(brand.pack_layer(brand.PACK_BUILDINGS), "")    # no pack: no default, no error


if __name__ == "__main__":
    unittest.main()
