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


def write_dem(path, dem, feet=False, tag=True):
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, dem.shape[1], dem.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, tc.CELL, 0.0, Y0 + dem.shape[0] * tc.CELL, 0.0, -tc.CELL))
    ds.SetProjection(srs(5070).ExportToWkt())
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
        bare = os.path.join(self.tmp, "bare.tif")
        write_dem(bare, dem, tag=False)
        with self.assertRaises(RuntimeError) as cm:
            self.f.run_embankments(bare, self.streams, self.out, log=lambda *_: None)
        self.assertIn("DEM elevation unit", str(cm.exception))
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


if __name__ == "__main__":
    unittest.main()
