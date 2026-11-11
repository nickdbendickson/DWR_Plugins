"""The three Finder tools through processing.run in a real QGIS (offscreen), on the synthetic
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
        self.assertEqual(names, ["crossings", "embankments", "ponds"])

    def test_embankments_tool(self):
        from osgeo import ogr
        self.run_alg("embankments", {"DEM": self.dem, "STREAMS": self.streams, "ROADS": self.roads,
                                     "KNOWN": [self.known], "MATCH_FT": 200.0, "OUT": self.out,
                                     "HILLSHADE": False})
        ds = ogr.Open(self.out)
        kinds = sorted(f.GetField("kind") for f in ds.GetLayerByName("embankments"))
        self.assertEqual(kinds, ["dam", "dry embankment", "road fill"])

    def test_crossings_tool(self):
        from osgeo import ogr
        self.run_alg("crossings", {"ROADS": self.roads, "STREAMS": self.streams, "KNOWN": [self.culverts],
                                   "OUT": self.out})
        ds = ogr.Open(self.out)
        f = ds.GetLayerByName("crossings").GetNextFeature()
        self.assertEqual(f.GetField("status"), "listed")

    def test_memory_layer_is_refused(self):
        from qgis.core import QgsProcessingException, QgsVectorLayer
        mem = QgsVectorLayer("LineString?crs=EPSG:5070", "scratch", "memory")
        with self.assertRaises(QgsProcessingException):
            self.run_alg("crossings", {"ROADS": mem, "STREAMS": self.streams, "OUT": self.out})


if __name__ == "__main__":
    unittest.main()
