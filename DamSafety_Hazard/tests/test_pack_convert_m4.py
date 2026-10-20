# -*- coding: utf-8 -*-
"""M4 regression test for damsuite/pack_convert.convert_pack.

An offline pack (format "nd-dam-hazard-offline-pack", v1) keeps each feature as
a zlib-compressed GeoJSON blob in a ``ds_<name>`` table. OGR cannot read those
blobs as geometry, so the old converter (``ogr.Open()`` + ``CopyLayer`` straight
on the pack) wrote opaque blobs, and with GDAL absent it merely copied the
sqlite - never decoding anything.

This builds a tiny real pack and checks that convert_pack now DECODES the blobs:

  * with no GDAL (this sandbox): one GeoJSON FeatureCollection per data set with
    the real geometry/properties - and the pack is NOT just copied through;
  * with a fake ``osgeo.ogr`` injected: OGR is handed the decoded ``.geojson``
    (never the ``.sqlite`` pack) and a GeoPackage is produced.

FAILS before the fix (no .geojson; sqlite copied / blobs OGR-copied), PASSES
after. Run standalone:  python3 DamSafety_Hazard/tests/test_pack_convert_m4.py

Real GDAL/QGIS are not importable here, so the OGR branch is exercised through a
minimal fake that records what OGR was asked to open (see risk notes).
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import types
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PACK_CONVERT = os.path.join(os.path.dirname(HERE), "damsuite", "pack_convert.py")

_POINT = [-100.5, 47.5]
_PROPS = {"name": "Test Dam", "nidid": "ND00123"}


def _load_module():
    """Load pack_convert.py in isolation (it is pure stdlib, no relative imports)."""
    spec = importlib.util.spec_from_file_location("pack_convert_under_test", PACK_CONVERT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_pack(path):
    """Write a minimal but faithful format-1 offline pack at `path`."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("format", "nd-dam-hazard-offline-pack"),
        ("format_version", "1"),
        ("bbox", json.dumps([-104.0489, 45.9351, -96.5543, 49.0])),
        ("created", "2026-01-01T00:00:00"),
    ])
    con.execute("CREATE TABLE datasets (name TEXT PRIMARY KEY, label TEXT, url TEXT, "
                "fetched TEXT, count INTEGER, status TEXT, note TEXT, maxw REAL, maxh REAL, "
                "geometry TEXT, gaps TEXT)")
    con.execute("INSERT INTO datasets (name, status, count) VALUES (?, ?, ?)",
                ("NID_dams", "complete", 1))
    con.execute("CREATE TABLE ds_NID_dams (fid INTEGER PRIMARY KEY, uid TEXT UNIQUE, "
                "minx REAL, miny REAL, maxx REAL, maxy REAL, feat BLOB)")
    feature = {"type": "Feature",
               "geometry": {"type": "Point", "coordinates": _POINT},
               "properties": dict(_PROPS)}
    blob = zlib.compress(json.dumps(feature, separators=(",", ":")).encode("utf-8"), 6)
    con.execute("INSERT INTO ds_NID_dams (uid, minx, miny, maxx, maxy, feat) VALUES "
                "(?, ?, ?, ?, ?, ?)", ("g:1", _POINT[0], _POINT[1], _POINT[0], _POINT[1], blob))
    con.commit()
    con.close()


# --------------------------------------------------------------- fake osgeo --
class _Recorder(object):
    def __init__(self):
        self.opened = []
        self.created = []
        self.copied = []


def _install_fake_ogr(rec):
    """Inject a minimal osgeo.ogr that records every path handed to it."""
    ogr = types.ModuleType("osgeo.ogr")

    class _InDS(object):
        def __init__(self, path):
            self._n = 0
            try:
                with open(path, encoding="utf-8") as fh:
                    self._n = len(json.load(fh).get("features", []))
            except Exception:
                self._n = 0

        def GetLayerCount(self):
            return 1

        def GetLayerByIndex(self, i):
            layer = types.SimpleNamespace()
            layer.GetName = lambda: "NID_dams"
            return layer

    class _OutDS(object):
        def __init__(self, path):
            self._path = path

        def CopyLayer(self, layer, name):
            rec.copied.append(name)

    class _Driver(object):
        def DeleteDataSource(self, out):
            try:
                os.remove(out)
            except OSError:
                pass

        def CreateDataSource(self, out):
            rec.created.append(out)
            with open(out, "wb") as fh:            # a real GPKG would appear on disk
                fh.write(b"GPKG-fake")
            return _OutDS(out)

    def _open(path):
        rec.opened.append(path)
        return _InDS(path)

    ogr.Open = _open
    ogr.GetDriverByName = lambda name: _Driver()
    pkg = types.ModuleType("osgeo")
    pkg.ogr = ogr
    sys.modules["osgeo"] = pkg
    sys.modules["osgeo.ogr"] = ogr


# ------------------------------------------------------------------- tests --
def test_decodes_blobs_without_gdal():
    # None in sys.modules makes "from osgeo import ogr" raise ImportError, so the no-GDAL path
    # is exercised even on a machine (QGIS) that has GDAL installed. Restored at the end.
    saved = {k: sys.modules.get(k) for k in ("osgeo", "osgeo.ogr")}
    sys.modules["osgeo"] = None
    sys.modules["osgeo.ogr"] = None
    try:
        _decodes_blobs_without_gdal()
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _decodes_blobs_without_gdal():
    mod = _load_module()
    tmp = tempfile.mkdtemp(prefix="m4pack_")
    pack = os.path.join(tmp, "nd_offline_pack.sqlite")
    dest = os.path.join(tmp, "out")
    _build_pack(pack)

    note = mod.convert_pack(pack, dest)

    gj = os.path.join(dest, "NID_dams.geojson")
    assert os.path.isfile(gj), "expected a decoded GeoJSON, got: " + repr(sorted(os.listdir(dest)))
    fc = json.load(open(gj, encoding="utf-8"))
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1, fc
    feat = fc["features"][0]
    assert feat["geometry"]["coordinates"] == _POINT, feat
    assert feat["properties"]["name"] == "Test Dam", feat
    # The old code copied the opaque sqlite instead of decoding it:
    assert not os.path.isfile(os.path.join(dest, "nd_offline_pack.sqlite")), \
        "pack was copied through, not decoded"
    layers = open(os.path.join(dest, "LAYERS.txt"), encoding="utf-8").read()
    assert "NID_dams.geojson" in layers, layers
    assert "Decoded" in note, note
    print("OK no-GDAL decode:", note)


def test_ogr_gets_decoded_geojson_not_the_pack():
    rec = _Recorder()
    _install_fake_ogr(rec)
    try:
        mod = _load_module()
        tmp = tempfile.mkdtemp(prefix="m4pack_ogr_")
        pack = os.path.join(tmp, "nd_offline_pack.sqlite")
        dest = os.path.join(tmp, "out")
        _build_pack(pack)

        note = mod.convert_pack(pack, dest)

        # The whole point of M4: OGR must see the DECODED GeoJSON, never the
        # sqlite pack full of zlib blobs.
        assert rec.opened, "OGR.Open was never called"
        assert all(p.endswith(".geojson") for p in rec.opened), rec.opened
        assert not any(p.endswith(".sqlite") for p in rec.opened), rec.opened
        assert rec.copied == ["NID_dams"], rec.copied
        gpkg = os.path.join(dest, "NID_dams.gpkg")
        assert os.path.isfile(gpkg), sorted(os.listdir(dest))
        assert not os.path.isfile(os.path.join(dest, "NID_dams.geojson")), \
            "temporary GeoJSON should be removed once the GeoPackage is written"
        layers = open(os.path.join(dest, "LAYERS.txt"), encoding="utf-8").read()
        assert "NID_dams.gpkg" in layers, layers
        print("OK OGR path:", note, "| opened:", rec.opened)
    finally:
        sys.modules.pop("osgeo", None)
        sys.modules.pop("osgeo.ogr", None)


if __name__ == "__main__":
    test_decodes_blobs_without_gdal()
    test_ogr_gets_decoded_geojson_not_the_pack()
    print("all M4 pack_convert tests passed")
