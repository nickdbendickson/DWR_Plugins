"""H5 (QGIS 3.34 worker-thread CRS guard) unit test for download.parallel_map.

    python3 DamSafety_Hazard/tests/test_transform_guard_h5.py

Real PyQGIS is NOT needed here: the test injects minimal fake ``qgis`` and
sibling (compat/constants/helpers/osm_parse) modules into sys.modules and loads
DamSafety_Hazard/download.py in isolation, then exercises the ported helpers:

  * _adopt_results re-creates each worker-thread QgsVectorLayer in the CALLING
    thread (the H5 fix) and parks the original in _ORPHANS;
  * _adopt_value recurses into tuples/lists and passes non-layers through;
  * _repair_transforms clears the transform cache (invalidateCache);
  * _warm_transforms and _prime_ellipsoids run without raising.

This FAILS before the fix (download has no _adopt_results / _ORPHANS ->
AttributeError) and PASSES after. Full end-to-end verification of the invalid
short-circuited transform still needs real QGIS 3.34 / PROJ (see risk notes).
"""
import os
import sys
import types
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)


# ---------------------------------------------------------------- fake qgis --
class _Provider(object):
    def __init__(self, layer):
        self._layer = layer

    def addAttributes(self, fields):
        self._layer._fields = list(self._layer._fields) + list(fields)

    def addFeatures(self, feats):
        self._layer._features = list(self._layer._features) + list(feats)


class QgsVectorLayer(object):
    def __init__(self, uri="", name="", provider="ogr"):
        self._uri = uri
        self._name = name
        self._provider = provider
        self._fields = []
        self._features = []
        self._valid = True
        self._wkb = 1
        self._crs = QgsCoordinateReferenceSystem("EPSG:4326")
        self.create_thread = threading.get_ident()

    def isValid(self):
        return self._valid

    def providerType(self):
        return self._provider

    def name(self):
        return self._name

    def crs(self):
        return self._crs

    def setCrs(self, crs):
        self._crs = crs

    def wkbType(self):
        return self._wkb

    def fields(self):
        return list(self._fields)

    def getFeatures(self):
        return list(self._features)

    def dataProvider(self):
        return _Provider(self)

    def updateFields(self):
        pass

    def updateExtents(self):
        pass

    def source(self):
        return self._uri


class QgsCoordinateReferenceSystem(object):
    def __init__(self, authid="EPSG:4326"):
        self._authid = authid

    def authid(self):
        return self._authid

    def isValid(self):
        return bool(self._authid)

    def isGeographic(self):
        return True


class QgsWkbTypes(object):
    @staticmethod
    def displayString(_w):
        return "Point"


class QgsCoordinateTransformContext(object):
    pass


class QgsCoordinateTransform(object):
    invalidated = [0]

    def __init__(self, *a, **kw):
        self._a = a

    def isValid(self):
        return True

    def isShortCircuited(self):
        return False

    @staticmethod
    def invalidateCache(*a, **kw):
        QgsCoordinateTransform.invalidated[0] += 1


class QgsEllipsoidUtils(object):
    calls = []

    @staticmethod
    def ellipsoidParameters(a):
        QgsEllipsoidUtils.calls.append(a)
        return {}


class QgsBlockingNetworkRequest(object):
    pass


class QgsSettings(object):
    pass


def _install_fakes():
    qgis = types.ModuleType("qgis")
    core = types.ModuleType("qgis.core")
    for nm in ("QgsVectorLayer", "QgsCoordinateReferenceSystem", "QgsWkbTypes",
               "QgsCoordinateTransform", "QgsCoordinateTransformContext",
               "QgsEllipsoidUtils", "QgsBlockingNetworkRequest", "QgsSettings"):
        setattr(core, nm, globals()[nm])
    pyqt = types.ModuleType("qgis.PyQt")
    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtcore.QUrl = type("QUrl", (object,), {})
    qtnet = types.ModuleType("qgis.PyQt.QtNetwork")
    qtnet.QNetworkRequest = type("QNetworkRequest", (object,), {})
    qgis.core = core
    qgis.PyQt = pyqt
    pyqt.QtCore = qtcore
    pyqt.QtNetwork = qtnet
    sys.modules.update({
        "qgis": qgis, "qgis.core": core, "qgis.PyQt": pyqt,
        "qgis.PyQt.QtCore": qtcore, "qgis.PyQt.QtNetwork": qtnet,
    })

    # fake parent package so the real download.py loads WITHOUT running the
    # real __init__ (which would try to install the transform guard)
    pkg = types.ModuleType("DamSafety_Hazard")
    pkg.__path__ = [HAZARD]
    sys.modules["DamSafety_Hazard"] = pkg

    compat = types.ModuleType("DamSafety_Hazard.compat")
    compat.NET_NO_ERROR = 0
    compat.MSG_WARNING = 1
    compat.MSG_INFO = 0
    compat.enum_value = lambda x: x
    sys.modules["DamSafety_Hazard.compat"] = compat

    constants = types.ModuleType("DamSafety_Hazard.constants")
    for nm, val in (("NSI_API_URL", "http://x"), ("DOWNLOAD_TILE_DEG", 0.2),
                    ("ARCGIS_PAGE_SIZE", 1000), ("ARCGIS_MAX_PAGES", 50),
                    ("DOWNLOAD_FOLDER_NAME", "_downloads"),
                    ("ND_ADDRESS_POINTS_URL", "http://x"), ("USA_STRUCTURES_URL", "http://x"),
                    ("NID_URL", "http://x"), ("NID_MIRROR_URL", "http://x"),
                    ("OVERPASS_URL", "http://x"), ("HTTP_USER_AGENT", "ua"),
                    ("NBI_URL", "http://x"), ("DOWNLOAD_WORKERS", 4), ("OVERPASS_WORKERS", 2)):
        setattr(constants, nm, val)
    sys.modules["DamSafety_Hazard.constants"] = constants

    helpers = types.ModuleType("DamSafety_Hazard.helpers")
    helpers.log_message = lambda *a, **kw: None
    helpers.on_main_thread = lambda: threading.current_thread() is threading.main_thread()

    class CancelledByUser(Exception):
        pass
    helpers.CancelledByUser = CancelledByUser
    # note: NO prime_ellipsoids -> download._prime_ellipsoids must use its local body
    sys.modules["DamSafety_Hazard.helpers"] = helpers

    osm = types.ModuleType("DamSafety_Hazard.osm_parse")
    for nm in ("overpass_query", "overpass_to_features", "crossing_category", "camp_features",
               "camps_query", "place_features", "crossings_query", "crossing_features",
               "places_query", "_circle_ll", "_DEAD_RAIL", "trailer_parks_query",
               "trailer_park_features"):
        setattr(osm, nm, None)
    sys.modules["DamSafety_Hazard.osm_parse"] = osm


def _load_download():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "DamSafety_Hazard.download", os.path.join(HAZARD, "download.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["DamSafety_Hazard.download"] = mod
    spec.loader.exec_module(mod)
    return mod


_install_fakes()
DL = _load_download()


# --------------------------------------------------------------------- tests --
def _make_in_worker(uri, name, provider, fields, feats):
    """Create a fake layer on a worker thread (mimics a download job)."""
    box = {}

    def go():
        lyr = QgsVectorLayer(uri, name, provider)
        lyr._fields = list(fields)
        lyr._features = list(feats)
        box["lyr"] = lyr
    t = threading.Thread(target=go)
    t.start()
    t.join()
    return box["lyr"]


def test_adopt_results_memory_layer_recreated_in_calling_thread():
    orig = _make_in_worker("Point?crs=EPSG:4326", "roads", "memory",
                           ["f1", "f2"], [{"a": 1}, {"b": 2}, {"c": 3}])
    out = {"roads": (True, (orig, 3, "/tmp/roads.geojson"))}
    DL._adopt_results(out)
    new, count, path = out["roads"][1]
    assert new is not orig, "layer must be re-created, not reused"
    assert isinstance(new, QgsVectorLayer)
    assert new.name() == "roads"
    assert new.providerType() == "memory"
    assert list(new.fields()) == ["f1", "f2"], new.fields()
    assert len(new.getFeatures()) == 3, new.getFeatures()
    assert new.create_thread == threading.get_ident(), "re-created off the calling thread"
    assert orig.create_thread != threading.get_ident(), "orig should come from a worker"
    assert orig in DL._ORPHANS, "worker layer must be parked, not dropped"
    assert count == 3 and path == "/tmp/roads.geojson", "non-layer values pass through"


def test_adopt_results_ogr_layer_recreated_from_source():
    orig = _make_in_worker("/tmp/nid.geojson", "nid", "ogr", [], [])
    out = {"nid": (True, (orig, 0, "/tmp/nid.geojson"))}
    DL._adopt_results(out)
    new = out["nid"][1][0]
    assert new is not orig
    assert new.source() == "/tmp/nid.geojson"
    assert new.providerType() == "ogr"
    assert new.create_thread == threading.get_ident()


def test_adopt_value_recurses_and_passes_through():
    assert DL._adopt_value(5) == 5
    assert DL._adopt_value("x") == "x"
    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "a", "memory")
    nested = DL._adopt_value([lyr, 2, ("z", lyr)])
    assert nested[1] == 2 and nested[2][0] == "z"
    assert isinstance(nested[0], QgsVectorLayer) and nested[0] is not lyr


def test_adopt_results_noop_off_main_thread():
    orig = QgsVectorLayer("Point?crs=EPSG:4326", "x", "memory")
    out = {"x": (True, orig)}

    def go():
        DL._adopt_results(out)
    t = threading.Thread(target=go)
    t.start()
    t.join()
    assert out["x"][1] is orig, "off the main thread adoption must be a no-op"


def test_adopt_results_skips_failed_jobs():
    err = RuntimeError("boom")
    out = {"bad": (False, err)}
    DL._adopt_results(out)
    assert out["bad"] == (False, err)


def test_repair_transforms_clears_cache():
    QgsCoordinateTransform.invalidated[0] = 0
    DL._warm_transforms()                     # must not raise
    DL._repair_transforms()
    assert QgsCoordinateTransform.invalidated[0] >= 1, "invalidateCache was not called"


def test_prime_ellipsoids_local_fallback():
    QgsEllipsoidUtils.calls[:] = []
    DL._prime_ellipsoids()                    # helpers has no prime_ellipsoids -> local body
    assert "EPSG:7019" in QgsEllipsoidUtils.calls
    assert "EPSG:7030" in QgsEllipsoidUtils.calls


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print("ok  ", t.__name__)
        except Exception as e:                # noqa
            failed += 1
            print("FAIL", t.__name__, "->", repr(e))
    print("{} tests, {} failed".format(len(tests), failed))
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
