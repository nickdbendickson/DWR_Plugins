"""2.19.0 (spec SPEC_aerial_basemap_outputs, acceptance test 4): the report / map book base
map. The imagery is the LAST layer of each map item (ND DWR NAIP 2025 WMS first), the Esri
label tiles are ABOVE the flood, the credit says "NAIP 2025" and the flight date
2025-07-13; a source that fails its check (or has no flight-date seamline there) hands over
to the next; offline: pack GeoTIFF imagery, else a hillshade of the DEM, with no request and
no .aux.xml. All HTTP is mocked; web layers are not opened (no capabilities request).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_aerial_basemap.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_restored_911 as T  # noqa: E402  (inits the headless QgsApplication)

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (QgsCoordinateReferenceSystem, QgsPrintLayout, QgsProject,  # noqa: E402
                       QgsRasterLayer, QgsRectangle)

from DamSafety_Hazard import basemap as BM  # noqa: E402
from DamSafety_Hazard import constants as C  # noqa: E402
from DamSafety_Hazard import report as R  # noqa: E402
from DamSafety_Hazard import pipeline as P  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")
LON, LAT = -97.2125, 46.9005          # Casselton (NAIP 2025 flown 2025-07-13)
TMP = tempfile.mkdtemp(prefix="hz_basemap_")
NAIP_JSON = json.dumps({"features": [{"attributes": {"IDATE": "2025-07-13",
                                                     "SDATE": "07/13/2025 09:28"}}]})
ESRI_JSON = json.dumps({"results": [{"attributes": {"SRC_DATE2": "20240922",
                                                    "NICE_NAME": "Maxar"}}]})


def _xy():
    s = osr.SpatialReference()
    s.ImportFromEPSG(4326)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    t = osr.SpatialReference()
    t.ImportFromEPSG(5070)
    x, y, _z = osr.CoordinateTransformation(s, t).TransformPoint(LON, LAT)
    return x, y


X, Y = _xy()
RECT = QgsRectangle(X - 1500, Y - 1500, X + 1500, Y + 1500)


def _tif(path, epsg=5070, x0=None, y0=None, size=3000.0, n=60, bands=1, date=None):
    x0 = X - size / 2 if x0 is None else x0
    y0 = Y + size / 2 if y0 is None else y0
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, bands, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, size / n, 0, y0, 0, -size / n))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(epsg)
    ds.SetProjection(sr.ExportToWkt())
    a = np.fromfunction(lambda i, j: (i + j) % 7, (n, n)).astype("float32")
    for b in range(bands):
        ds.GetRasterBand(b + 1).WriteArray(a)
    if date:
        ds.SetMetadataItem("ACQUISITION_DATE", date)
    ds = None
    return path


DEPTH = _tif(os.path.join(TMP, "depth.tif"))
DEM = _tif(os.path.join(TMP, "dem.tif"))
OPENED = {}                          # layer id -> (uri, provider) of every web layer opened


def _fake_open(uri, name, provider):
    lyr = QgsRasterLayer(DEPTH, name, "gdal")      # a valid stand-in; nothing is fetched
    OPENED[lyr.id()] = (uri, provider)
    return lyr


class Env(object):
    """offline_only / probe / http_get mocked; `down`: url parts whose check fails."""

    def __init__(self, down=(), naip=NAIP_JSON, offline=False, env=None):
        self.down, self.naip, self.offline, self.env = down, naip, offline, env or {}
        self.calls = []

    def probe(self, url=None, lonlat=None, timeout_ms=0, request_url=None):
        self.calls.append(request_url or url)
        if self.offline:
            raise AssertionError("a request was made offline: {}".format(url))
        if any(d in str(url) for d in self.down):
            return False, "the imagery service did not answer (mock)"
        return True, "imagery service answered"

    def get(self, url, timeout_s):
        self.calls.append(url)
        if self.offline:
            raise AssertionError("a request was made offline: {}".format(url))
        if "Dates_Imagery" in url:
            return self.naip.encode()
        if "identify" in url:
            return ESRI_JSON.encode()
        raise AssertionError("unexpected request " + url)

    def __enter__(self):
        self.saved = (BM.offline_only, BM.probe, BM.http_get, BM._open_layer,
                      {k: os.environ.get(k) for k in self.env})
        BM.offline_only = (lambda: True) if self.offline else (lambda: False)
        BM.probe = self.probe
        BM.http_get = self.get
        BM._open_layer = _fake_open
        BM.forget_probe()
        for k, v in self.env.items():
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        BM.offline_only, BM.probe, BM.http_get, BM._open_layer, env = self.saved
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _depth():
    return QgsRasterLayer(DEPTH, "Max depth", "gdal")


def test_report_maps_naip_last_labels_above_credit():
    depth = _depth()
    with Env() as e:
        info = {}
        layers, styles, used, cap, note = R.layout_imagery(True, [depth], False, RECT, CRS,
                                                           info=info)
    assert used and note == "", (used, note)
    img = layers[-1]
    uri, prov = OPENED[img.id()]
    assert prov == "wms" and "layers=NorthDakota_NAIP_2025_col_air" in uri, uri
    assert "url=https://aerial.dwr.nd.gov/dwrwms" in uri and "crs=EPSG:5070" in uri, uri
    # labels above the flood, the flood above the imagery
    labs = [lyr for lyr in layers if "World_" in OPENED.get(lyr.id(), ("", ""))[0]]
    assert len(labs) == 2, [OPENED.get(lyr.id()) for lyr in layers]
    assert layers.index(depth) > max(layers.index(x) for x in labs)
    assert layers.index(depth) < layers.index(img)
    assert all("tilePixelRatio=2" in OPENED[x.id()][0] and "zmax=17" in OPENED[x.id()][0]
               for x in labs)
    assert "NAIP 2025" in cap and "2025-07-13" in cap and "labels: Esri" in cap, cap
    assert "EPSG:5070" in cap and C.PLUGIN_VERSION in cap, cap
    assert styles and depth.id() in styles                    # flood see-through
    assert any("Dates_Imagery/MapServer/41/query" in c for c in e.calls), e.calls
    assert info["kind"] == "imagery" and info["source"]["id"] == "nd_naip_2025"
    line = BM.source_line(info)
    assert "nd_naip_2025" in line and "2025-07-13" in line and "World_Transportation" in line
    # in the print layout: the map item keeps that order; the credit is under the map
    layout = QgsPrintLayout(QgsProject.instance())
    layout.initializeDefaults()
    m = R._map(layout, CRS, RECT, layers, 10, 22, 180, 150, 0, styles)
    assert m.layers()[-1].id() == img.id()
    assert [x.id() for x in m.layers()].index(depth.id()) > 1
    lab = R.imagery_label(layout, used, cap, note, 100, 173, 90, 0)
    assert "2025-07-13" in lab.text()


def test_fallback_to_esri_then_usgs():
    with Env(down=("dwrwms",)):
        _l, _s, used, cap, _n = R.layout_imagery(True, [_depth()], False, RECT, CRS,
                                                 labels=False)
    assert used and "Esri World Imagery" in cap and "2024-09-22" in cap, cap
    assert "labels:" not in cap
    with Env(down=("dwrwms", "World_Imagery")):
        info = {}
        layers, _s, used, cap, _n = R.layout_imagery(True, [_depth()], False, RECT, CRS,
                                                     info=info)
    assert used and "USGS" in cap and "date not published" in cap, cap
    assert "usgsimagery" in OPENED[layers[-1].id()][0].lower()
    assert len(info["tried"]) == 2, info["tried"]
    # no NAIP seamline over the map (outside ND): the next source is used
    with Env(naip=json.dumps({"features": []})):
        _l, _s, used, cap, _n = R.layout_imagery(True, [_depth()], False, RECT, CRS)
    assert used and "Esri" in cap, cap


def test_all_down_no_dem_says_why():
    with Env(down=("dwrwms", "World_Imagery", "nationalmap")):
        layers, styles, used, cap, note = R.layout_imagery(True, [_depth()], False, RECT, CRS)
    assert not used and styles is None and len(layers) == 1
    assert "did not answer" in note, note


def test_offline_pack_then_hillshade_no_requests_no_aux():
    pack = os.path.join(TMP, "pack")
    os.makedirs(os.path.join(pack, "imagery"))
    before = set(os.listdir(TMP))
    # hillshade of the DEM (no pack imagery yet)
    with Env(offline=True, env={"ND_DAM_SUITE": pack}) as e:
        layers, _s, used, cap, note = R.layout_imagery(True, [_depth()], False, RECT, CRS,
                                                       dem_path=DEM)
    assert e.calls == [], e.calls
    assert used and cap.startswith("Imagery not available (offline): hillshade of dem.tif"), cap
    assert layers[-1].renderer().type() == "hillshade"
    assert note == ""
    # pack GeoTIFF (another CRS) covering the map
    _tif(os.path.join(pack, "imagery", "naip_casselton.tif"), epsg=5070, size=8000.0,
         x0=X - 4000, y0=Y + 4000, bands=3, date="2025-07-13")
    _tif(os.path.join(pack, "imagery", "aa_elsewhere.tif"), x0=X + 50000)   # not covering
    with Env(offline=True, env={"ND_DAM_SUITE": pack}) as e:
        info = {}
        layers, _s, used, cap, _n = R.layout_imagery(True, [_depth()], False, RECT, CRS,
                                                     dem_path=DEM, info=info)
    assert e.calls == []
    assert used and "offline pack naip_casselton.tif" in cap and "2025-07-13" in cap, cap
    assert "labels:" not in cap                             # no label tiles offline
    assert info["kind"] == "pack"
    aux = [n for n in os.listdir(os.path.join(pack, "imagery")) if n.endswith(".aux.xml")]
    assert not aux, aux
    assert set(os.listdir(TMP)) - before == set(), set(os.listdir(TMP)) - before


def test_sources_overrides():
    saved = ([dict(x) for x in C.AERIAL_SOURCES], list(C.AERIAL_SOURCE_ORDER),
             C.AERIAL_IMAGERY_URL)
    path = os.path.join(TMP, "sources.json")
    try:
        with open(path, "w") as fh:
            json.dump({"AERIAL_SOURCES": [{"id": "nd_naip_2025",
                                           "layer": "NorthDakota_NAIP_2024_col_air"},
                                          {"id": "bad", "kind": "xyz", "url": "ftp://x"}],
                       "AERIAL_SOURCE_ORDER": ["esri_world_imagery", "nd_naip_2025"]}, fh)
        C.apply_source_overrides(path)
        ids = [s["id"] for s in BM.sources()]
        assert ids == ["esri_world_imagery", "nd_naip_2025"], ids
        assert BM.sources()[1]["layer"] == "NorthDakota_NAIP_2024_col_air"
        assert [s["id"] for s in BM.sources("usgs_imagery")] == ["usgs_imagery"]
        with open(path, "w") as fh:
            json.dump({"AERIAL_IMAGERY_URL": "https://tiles.example/{z}/{x}/{y}.png"}, fh)
        C.AERIAL_SOURCES[:] = saved[0]
        C.apply_source_overrides(path)
        src = BM.sources()
        assert len(src) == 1 and src[0]["url"].startswith("https://tiles.example"), src
    finally:
        C.AERIAL_SOURCES = saved[0]
        C.AERIAL_SOURCE_ORDER = saved[1]
        C.AERIAL_IMAGERY_URL = saved[2]
        C.SOURCE_OVERRIDES.clear()


def test_in_project_layers_hidden_and_reused():
    with Env():
        bm = BM.layout_basemap(True, RECT, CRS)
        bm2 = BM.layout_basemap(True, RECT, CRS)
    proj = QgsProject.instance()
    assert bm["layer"].id() in proj.mapLayers() and bm2["layer"].id() == bm["layer"].id()
    node = proj.layerTreeRoot().findLayer(bm["layer"].id())
    assert node is not None and not node.itemVisibilityChecked()
    assert str(bm["layer"].customProperty(BM.HIDDEN_PROP, False)).lower() in ("true", "1")
    assert len(bm["labels"]) == 2 and all(x.id() in proj.mapLayers() for x in bm["labels"])


def test_settings_defaults_and_fast_run():
    d = P.DEFAULTS
    assert d["report_imagery"] is True and d["basemap_labels"] is True
    assert d["basemap_source"] == "auto" and abs(d["basemap_opacity_flood"] - 0.65) < 1e-9
    assert P.fast_run_config(dict(d, fast_run=True))["report_imagery"] is False
    from DamSafety_Hazard import ensemble as EN
    src = open(EN.__file__, encoding="utf-8").read()
    assert '"report_imagery": False' in src
    o = P.basemap_options({"basemap_labels": False, "basemap_source": "esri_world_imagery",
                           "basemap_opacity_flood": "2"}, None)
    assert o == {"labels": False, "source": "esri_world_imagery", "opacity": 1.0,
                 "dem_path": None}, o
    txt = os.path.join(TMP, "Hazard_Classification_Summary.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("X\n\nDATA SOURCES\n  - a\n  - b\n\nThis is a screening result.\n")
    assert P.add_summary_source(txt, "Report maps - Map base: NAIP")
    rows = open(txt, encoding="utf-8").read().split("\n")
    assert rows[3:6] == ["  - a", "  - b", "  - Report maps - Map base: NAIP"], rows


def test_imagery_dates_shared():
    from DamSafety_Hazard import lone911 as LN
    seen = []

    def get(url, timeout_s):
        seen.append(url)
        return NAIP_JSON.encode()
    d = LN.imagery_dates((LON - 0.01, LAT - 0.01, LON + 0.01, LAT + 0.01), 2025, get=get)
    assert [x.isoformat() for x in d] == ["2025-07-13"] and LN.dates_text(d) == "2025-07-13"
    assert "esriGeometryEnvelope" in seen[0] and "returnGeometry=false" in seen[0]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn) and fn.__module__ == __name__:
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name)
    sys.stdout.flush()
    os._exit(1 if fails else 0)
