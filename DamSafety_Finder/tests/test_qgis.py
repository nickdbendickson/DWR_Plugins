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
        self.assertEqual(names, ["crossings", "embankments", "osm", "ponds", "score", "water"])

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


if __name__ == "__main__":
    unittest.main()
