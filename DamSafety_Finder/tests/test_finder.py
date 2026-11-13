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


    def test_dem_drainage_alone_finds_the_fills(self):
        res = self.f.run_embankments(self.dem, None, self.out, roads=self.roads,
                                     known=[(self.known, "DWR dams", None)], match_ft=200.0,
                                     drainage_ac=2.0, drain_cell=6.0, hillshade=False, log=lambda *_: None)
        kinds = sorted(r["kind"] for r in res["rows"])
        self.assertEqual(kinds, ["dam", "dry embankment", "road fill"])
        self.assertTrue(all(r["line_source"] == "DEM drainage" for r in res["rows"]))
        ds = ogr.Open(self.out)
        self.assertEqual(ds.GetLayerByName("spillways").GetFeatureCount(), 3)

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


if __name__ == "__main__":
    unittest.main()
