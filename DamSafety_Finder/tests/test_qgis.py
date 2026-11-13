"""The Finder tools through processing.run in a real QGIS (offscreen), on the synthetic
valley of test_finder.py. Skips without QGIS.

    QT_QPA_PLATFORM=offscreen python -m <plugin folder>.tests.test_qgis
"""
import os
import sys
import tempfile
import unittest

try:
    from qgis.core import QgsApplication
    HAVE_QGIS = True
except ImportError:                                   # pragma: no cover
    HAVE_QGIS = False

from . import test_finder as tf

_APP = None


def qgis_app():
    global _APP
    if QgsApplication.instance() is not None:
        return QgsApplication.instance()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    profile = os.environ.get("QGIS_CUSTOM_CONFIG_PATH") or tempfile.mkdtemp(prefix="finder_qgis_")
    _APP = QgsApplication([], False, profile)
    _APP.initQgis()
    plug = os.path.join(QgsApplication.pkgDataPath(), "python", "plugins")
    if plug not in sys.path:
        sys.path.append(plug)
    from processing.core.Processing import Processing
    Processing.initialize()
    return _APP


@unittest.skipUnless(HAVE_QGIS and tf.gdal is not None, "no QGIS / GDAL in this Python")
class Tools(tf.Runs):
    @classmethod
    def setUpClass(cls):
        qgis_app()
        from .. import brand, plugin
        reg = QgsApplication.processingRegistry()
        if reg.providerById(brand.PROVIDER_ID) is None:
            cls.provider = plugin.Provider()
            reg.addProvider(cls.provider)
        cls.pid = brand.PROVIDER_ID

    def run_alg(self, name, params):
        import processing
        return processing.run("{}:{}".format(self.pid, name), params)

    def test_registered(self):
        reg = QgsApplication.processingRegistry()
        names = sorted(a.name() for a in reg.providerById(self.pid).algorithms())
        self.assertEqual(names, ["crossings", "embankments", "evidence", "osm", "ponds", "review_batch",
                                 "score", "water", "water_mosaic"])

    def test_embankments_tool(self):
        from osgeo import ogr
        self.run_alg("embankments", {"DEM": self.dem, "STREAMS": self.streams, "ROADS": self.roads,
                                     "KNOWN": [self.known], "MATCH_FT": 200.0, "OUT": self.out,
                                     "HILLSHADE": False})
        ds = ogr.Open(self.out)
        kinds = sorted(f.GetField("kind") for f in ds.GetLayerByName("embankments"))
        self.assertEqual(kinds, ["dam", "dry embankment", "road fill"])

    def test_a_cancelled_run_fails(self):
        import processing
        from qgis.core import QgsProcessingException, QgsProcessingFeedback
        fb = QgsProcessingFeedback()
        fb.cancel()
        for name, params in (("embankments", {"DEM": self.dem, "STREAMS": self.streams, "OUT": self.out,
                                              "HILLSHADE": False}),
                             ("crossings", {"ROADS": self.roads, "STREAMS": self.streams, "DEM": self.dem,
                                            "OUT": self.out})):
            with self.assertRaises(QgsProcessingException) as cm:
                processing.run("{}:{}".format(self.pid, name), params, feedback=fb)
            self.assertIn("Cancelled", str(cm.exception))

    def test_crossings_tool(self):
        from osgeo import ogr
        self.run_alg("crossings", {"ROADS": self.roads, "STREAMS": self.streams, "KNOWN": [self.culverts],
                                   "OUT": self.out})
        ds = ogr.Open(self.out)
        f = ds.GetLayerByName("crossings").GetNextFeature()
        self.assertEqual(f.GetField("status"), "listed")

    def test_water_tool(self):
        from osgeo import ogr
        from .. import brand
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        self.run_alg("water", {"SOURCE": [len(brand.IMAGE_SERVICES)], "IMAGE": img, "GREEN": 2, "NIR": 4,
                               "MAPPED": self.pond_layer(), "OUT": self.out})
        ds = ogr.Open(self.out)
        f = ds.GetLayerByName("water").GetNextFeature()
        self.assertEqual(f.GetField("mapped"), "yes")

    def test_water_from_a_count_raster_tool(self):
        import numpy as np
        from osgeo import gdal, ogr
        path = os.path.join(self.tmp, "wcount.tif")
        counts = np.zeros((300, 300), np.float32)
        counts[120:160, 120:160] = 2.0
        ds = gdal.GetDriverByName("GTiff").Create(path, 300, 300, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((tf.X0, 1.0, 0.0, tf.Y0 + 300, 0.0, -1.0))
        ds.SetProjection(tf.srs(5070).ExportToWkt())
        b = ds.GetRasterBand(1); b.WriteArray(counts); b.SetNoDataValue(0.0); ds = None
        self.run_alg("water", {"WATER_RASTER": path, "WATER_BAND": 1, "WATER_COUNT_MIN": 1.0,
                               "CELL": 1.0, "MIN_AC": 0.02, "MIN_WIDTH": 3.0, "OUT": self.out})
        d = ogr.Open(self.out)
        self.assertEqual(d.GetLayerByName("water").GetFeatureCount(), 1)

    def test_build_water_mosaic_tool(self):
        import numpy as np
        from osgeo import gdal, ogr
        cx, cy = tf.X0 + 150.0, tf.Y0 + 150.0
        wx, wy = np.meshgrid(np.arange(cx - 20, cx + 20, 1.0), np.arange(cy - 20, cy + 20, 1.0))
        x = wx.ravel(); y = wy.ravel()
        z = np.full(x.size, 305.0)
        cls = np.full(x.size, 9, np.uint8)
        las = os.path.join(self.tmp, "mt.las")
        tf.tc.write_las(las, x, y, cls, z=z)
        mdir = os.path.join(self.tmp, "qmosaic")
        res = self.run_alg("water_mosaic", {"LAS": las, "LAS_CLASSES": "9", "LAS_EPSG": "5070",
                                            "CELL": 1.0, "OUT": mdir})
        cvrt = res["COUNT_VRT"]
        self.assertTrue(os.path.exists(cvrt))
        # the mosaic feeds the open-water tool
        self.run_alg("water", {"WATER_RASTER": cvrt, "WATER_BAND": 1, "WATER_COUNT_MIN": 1.0,
                               "CELL": 1.0, "MIN_AC": 0.02, "MIN_WIDTH": 3.0, "OUT": self.out})
        d = ogr.Open(self.out)
        self.assertEqual(d.GetLayerByName("water").GetFeatureCount(), 1)

    def test_water_service_needs_an_extent(self):
        from qgis.core import QgsProcessingException
        with self.assertRaises(QgsProcessingException):
            self.run_alg("water", {"SOURCE": [0], "OUT": self.out})

    def test_embankments_tool_dem_only(self):
        from osgeo import ogr
        self.run_alg("embankments", {"DEM": self.dem, "ROADS": self.roads, "DRAIN_AC": 2.0,
                                     "DUGOUTS": True, "RIDGES": True, "OUT": self.out, "HILLSHADE": False})
        ds = ogr.Open(self.out)
        self.assertEqual(ds.GetLayerByName("embankments").GetFeatureCount(), 3)

    def test_crossings_tool_with_dem(self):
        from osgeo import ogr
        self.run_alg("crossings", {"ROADS": self.roads, "STREAMS": self.streams, "DEM": self.dem,
                                   "OUT": self.out})
        ds = ogr.Open(self.out)
        f = ds.GetLayerByName("crossings").GetNextFeature()
        self.assertGreater(f.GetField("drainage_ac"), 0)

    def test_memory_layer_is_refused(self):
        from qgis.core import QgsProcessingException, QgsVectorLayer
        mem = QgsVectorLayer("LineString?crs=EPSG:5070", "scratch", "memory")
        with self.assertRaises(QgsProcessingException):
            self.run_alg("crossings", {"ROADS": mem, "STREAMS": self.streams, "OUT": self.out})

    def test_evidence_and_review_batch_tools(self):
        from osgeo import ogr
        self.run_alg("embankments", {"DEM": self.dem, "STREAMS": self.streams, "ROADS": self.roads,
                                     "OUT": self.out, "HILLSHADE": False})
        fills = self.out + "|layername=embankments"
        ev = os.path.join(self.tmp, "ev.gpkg")
        self.run_alg("evidence", {"FILLS": fills, "IMAGES": "", "DEM": self.dem, "ROADS": self.roads, "OUT": ev})
        ds = ogr.Open(ev)
        lyr = ds.GetLayerByName("evidence")
        self.assertEqual(lyr.GetFeatureCount(), 3)
        self.assertGreaterEqual(lyr.GetLayerDefn().GetFieldIndex("roads_1mi"), 0)
        ds = None
        rb = os.path.join(self.tmp, "rb.gpkg")
        # EVIDENCE off: the tool's default image list is the brand's services, and no test may
        # reach the network
        with self.no_network():
            self.run_alg("review_batch", {"FILLS": fills, "N": 5, "EVIDENCE": False, "OUT": rb})
        ds = ogr.Open(rb)
        lyr = ds.GetLayerByName("review_batch")
        self.assertEqual(lyr.GetFeatureCount(), 2)   # the dam + the dry fill
        f = lyr.GetNextFeature()                     # a sheet needs the fill's own fields too
        self.assertEqual(f.GetField("list_n"), 2)
        self.assertGreater(f.GetField("length_ft"), 0)
        self.assertLess(lyr.GetLayerDefn().GetFieldIndex("water_years"), 0)

    def no_network(self):
        from unittest import mock

        def never(url, timeout=None):
            raise AssertionError("a test asked the network: {}".format(url))
        return mock.patch("urllib.request.urlopen", never)

    def card_fills(self):
        """Six fills inside the image of write_image, the first on its pond."""
        cx, cy = tf.X0 + 150.0, tf.Y0 + 150.0
        spots = [(cx, cy)] + [(tf.X0 + 40.0 + 44.0 * i, tf.Y0 + 75.0) for i in range(5)]     # clear of the roof
        return self.evid_fills([{"x": x, "y": y, "find_id": "FIND-{:08X}".format(i), "kind": "dam", "area_ac": 0.3,
                                 "height_ft": 9.0 - i} for i, (x, y) in enumerate(spots)])

    def test_review_batch_tool_shows_evidence(self):
        from osgeo import ogr
        from .. import brand, finder
        reg = QgsApplication.processingRegistry()
        alg = reg.algorithmById("{}:review_batch".format(self.pid))
        par = {q.name(): q for q in alg.parameterDefinitions()}
        # on by default, with the brand's image list; the NWI and LiDAR layers are the user's
        self.assertIs(par["EVIDENCE"].defaultValue(), True)
        self.assertEqual(par["IMAGES"].defaultValue(), brand.EVIDENCE_IMAGES)
        self.assertTrue(finder.parse_images(par["IMAGES"].defaultValue()))
        self.assertIsNone(par["NWI"].defaultValue())
        self.assertIsNone(par["LIDAR_POINTS"].defaultValue())
        # the same words as the evidence tool for the same inputs
        ev = {q.name(): q for q in reg.algorithmById("{}:evidence".format(self.pid)).parameterDefinitions()}
        for name in ("IMAGES", "NWI", "LIDAR_POINTS"):
            self.assertEqual(par[name].description(), ev[name].description())
            self.assertTrue(par[name].flags() & par[name].FlagOptional)
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        nwi = tf.write_layer(self.vec, "nwi_cards", [("POLYGON (({a} {c}, {b} {c}, {b} {d}, {a} {d}, {a} {c}))".format(
            a=tf.X0 + 120.0, b=tf.X0 + 180.0, c=tf.Y0 + 120.0, d=tf.Y0 + 180.0), {"ATTRIBUTE": "PABFh"})],
            fields=("ATTRIBUTE",))
        rb = os.path.join(self.tmp, "cards.gpkg")
        with self.no_network():
            self.run_alg("review_batch", {"FILLS": self.card_fills(), "N": 5, "CAP": 5, "NWI": nwi,
                                          "IMAGES": "2018 | {} | green=2 nir=4".format(img), "OUT": rb})
        ds = ogr.Open(rb)
        lyr = ds.GetLayerByName("review_batch")
        d = lyr.GetLayerDefn()
        names = [d.GetFieldDefn(j).GetName() for j in range(d.GetFieldCount())]
        k = len(finder.REVIEW_FIELDS)
        self.assertEqual(names[k:k + len(finder.CARD_FIELDS)], [n for n, _t in finder.CARD_FIELDS])
        got = {f.GetField("find_id"): (f.GetField("water_years"), f.GetField("nwi_code"), f.GetField("evid_note"))
               for f in lyr}
        self.assertEqual(len(got), 5)
        self.assertEqual(got["FIND-00000000"], ("1 of 1", "PABFh", "no LiDAR count raster given"))
        self.assertEqual(sorted(set(v for k_, v in got.items() if k_ != "FIND-00000000")),
                         [("0 of 1", "", "no LiDAR count raster given")])
        info = {f.GetField("key"): f.GetField("value") for f in ds.GetLayerByName("run_info")}
        self.assertEqual((info["tool"], info["evidence"]), ("review", "True"))
        ds = None
        # the same cards, ranks and chances as with the evidence off
        off = os.path.join(self.tmp, "cards_off.gpkg")
        with self.no_network():
            self.run_alg("review_batch", {"FILLS": self.card_fills(), "N": 5, "CAP": 5, "EVIDENCE": False, "OUT": off})

        def pick(path):
            return [tuple(r.get(q) for q in finder.PICK_FIELDS)
                    for r in finder.read_rows(path + "|layername=review_batch")]
        self.assertEqual(pick(rb), pick(off))

    def test_a_cancelled_review_batch_keeps_the_batch(self):
        import processing
        from osgeo import ogr
        from qgis.core import QgsProcessingException, QgsProcessingFeedback
        img = self.write_image(os.path.join(self.tmp, "img.tif"))
        fb = QgsProcessingFeedback()
        fb.cancel()
        rb = os.path.join(self.tmp, "cancelled.gpkg")
        with self.no_network(), self.assertRaises(QgsProcessingException) as cm:
            processing.run("{}:review_batch".format(self.pid), {
                "FILLS": self.card_fills(), "N": 5, "CAP": 5, "IMAGES": "2018 | {} | green=2 nir=4".format(img),
                "OUT": rb}, feedback=fb)
        self.assertIn("holds the whole batch as picked", str(cm.exception))
        ds = ogr.Open(rb)
        self.assertEqual(ds.GetLayerByName("review_batch").GetFeatureCount(), 5)

    def test_evidence_tool_buildings_and_roads_start_from_the_data_pack(self):
        import json
        from unittest import mock
        from .. import brand, plugin

        def defaults():
            alg = plugin.AddEvidence()
            alg.initAlgorithm()
            return {q.name(): q.defaultValue() for q in alg.parameterDefinitions()}
        nowhere = os.path.join(self.tmp, "nowhere")
        with mock.patch.dict(os.environ, {"NDSUITE_PACK": nowhere, "NDSUITE_DATA": nowhere, "NDSUITE_SOURCE": "pack"}):
            d = defaults()
        self.assertEqual((d["BUILDINGS"], d["ROADS"], d["DEM"]), (None, None, None))    # no pack: optional, empty
        if not hasattr(brand, "pack_layer"):
            self.skipTest("this brand has no offline data pack")
        try:
            from DamSafety_Core.damsuite import offline_pack
        except ImportError:
            self.skipTest("DamSafety Core is not beside this plugin")
        pack = os.path.join(self.tmp, "ND_Dam_Suite_Pack")
        os.makedirs(os.path.join(pack, "structures"))
        os.makedirs(os.path.join(pack, "roads"))
        nsi = os.path.join(pack, "structures", "NSI_structures.gpkg")
        roads = os.path.join(pack, "roads", "ND_roads_NG911.gpkg")
        x = tf.gx(tf.tc.XS)
        tf.write_layer(nsi, "NSI_structures", [("POINT ({} {})".format(x + 9.0, tf.row_y(360)), {})])
        tf.write_layer(roads, "ND_roads_NG911", [("LINESTRING ({} {}, {} {})".format(
            tf.gx(0), tf.row_y(320), tf.gx(tf.tc.N * tf.tc.CELL), tf.row_y(320)), {})])
        with open(os.path.join(pack, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump({"format": offline_pack.PACK_FORMAT, "format_version": 2, "layout": "gpkg", "datasets": {
                "NSI_structures": {"file": "structures/NSI_structures.gpkg"},
                "ND_roads_NG911": {"file": "roads/ND_roads_NG911.gpkg"}}}, fh)
        with mock.patch.dict(os.environ, {"NDSUITE_PACK": pack, "NDSUITE_SOURCE": "pack"}):
            d = defaults()
            self.assertEqual((d["BUILDINGS"], d["ROADS"]), (nsi, roads))
            # with a DEM and nothing else said, the tool counts what is below from the pack's layers
            self.run_alg("embankments", {"DEM": self.dem, "STREAMS": self.streams, "ROADS": self.roads,
                                         "OUT": self.out, "HILLSHADE": False})
            ev = os.path.join(self.tmp, "ev_pack.gpkg")
            alg = plugin.AddEvidence()
            alg.initAlgorithm()
            import processing
            from osgeo import ogr
            processing.run(alg, {"FILLS": self.out + "|layername=embankments", "IMAGES": "", "DEM": self.dem,
                                 "OUT": ev})
        ds = ogr.Open(ev)
        got = {f.GetField("kind"): (f.GetField("bld_1mi"), f.GetField("roads_1mi")) for f in ds.GetLayerByName("evidence")}
        self.assertGreaterEqual(got["dam"][0], 1)
        self.assertGreaterEqual(got["dam"][1], 1)


if __name__ == "__main__":
    unittest.main()
