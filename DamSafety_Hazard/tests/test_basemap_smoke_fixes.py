"""2.19.1: QGIS Dev's smoke test of main 7feee1f (PR #218).

- A label service is used only when one of its tiles came back: with the label host down the
  maps carry no label layer, the credit does not say "labels:" and the DATA SOURCES line lists
  the label service once under "not used" (7feee1f: 122.8 s per export, labels still credited).
- A check that ran to its timeout says it timed out; the check cache key carries the WMS layer.
- ND_DAM_OFFLINE=0 / false is not offline (the Runner's _truthy rule).
- The hillshade z factor follows the DEM's CRS.
All HTTP is mocked (test_aerial_basemap.Env); no web layer is opened.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_basemap_smoke_fixes.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_aerial_basemap as A  # noqa: E402  (inits the headless QgsApplication)

from qgis.core import QgsCoordinateReferenceSystem, QgsRasterLayer  # noqa: E402

from DamSafety_Hazard import basemap as BM  # noqa: E402
from DamSafety_Hazard import constants as C  # noqa: E402
from DamSafety_Hazard import pipeline as P  # noqa: E402
from DamSafety_Hazard import report as R  # noqa: E402


def test_labels_down_no_label_layers_no_credit():
    depth = A._depth()
    with A.Env(down=("World_Transportation", "World_Boundaries")) as e:
        info = {}
        layers, _s, used, cap, _n = R.layout_imagery(True, [depth], False, A.RECT, A.CRS,
                                                     info=info)
    assert used and "NAIP 2025" in cap, cap
    assert "labels:" not in cap, cap
    assert not any("World_" in A.OPENED.get(x.id(), ("", ""))[0] for x in layers)
    assert not info.get("labels") and info["label_urls"] == []
    # one host: the second label service is not asked once the first failed
    label_calls = [c for c in e.calls if "World_" in str(c)]
    hosts = {BM.urlparse(u).netloc for u in C.AERIAL_LABEL_URLS}
    assert len(label_calls) == len(hosts), label_calls
    line = BM.source_line(info)
    assert "; labels:" not in line, line
    assert line.count("not used:") == 1 and line.count("labels World_Transportation") == 1, line
    assert line.count("labels World_Boundaries") == 1, line
    # one label service up, the other down: only the one that answered is used and credited
    with A.Env(down=("World_Boundaries",)):
        info = {}
        layers, _s, used, cap, _n = R.layout_imagery(True, [A._depth()], False, A.RECT, A.CRS,
                                                     info=info)
    labs = [x for x in layers if "World_" in A.OPENED.get(x.id(), ("", ""))[0]]
    assert len(labs) == 1 and "World_Transportation" in A.OPENED[labs[0].id()][0]
    assert "labels:" in cap and info["label_urls"] == [
        u for u in C.AERIAL_LABEL_URLS if "World_Transportation" in u]
    line = BM.source_line(info)
    assert "World_Boundaries" in line.split("not used:")[1], line


def test_label_check_timeout_is_short():
    seen = []

    def probe(url=None, lonlat=None, timeout_ms=0, request_url=None):
        seen.append(timeout_ms)
        return True, "ok"
    saved = BM.probe
    BM.probe = probe
    try:
        out = BM.label_checks((A.LON, A.LAT))
    finally:
        BM.probe = saved
    assert [ok for _u, ok, _t in out] == [True] * len(C.AERIAL_LABEL_URLS)
    assert seen and set(seen) == {BM.LABEL_PROBE_TIMEOUT_MS}
    assert BM.LABEL_PROBE_TIMEOUT_MS < BM.PROBE_TIMEOUT_MS


def test_hillshade_lists_failed_sources_once():
    with A.Env(down=("dwrwms", "World_Imagery", "nationalmap")):
        info = {}
        R.layout_imagery(True, [A._depth()], False, A.RECT, A.CRS, dem_path=A.DEM, info=info)
    assert info["kind"] == "hillshade", info.get("kind")
    line = BM.source_line(info)
    assert line.count("nd_naip_2025") == 1 and line.count("esri_world_imagery") == 1, line
    assert "(no aerial imagery)" in line, line
    off = BM.source_line({"kind": "hillshade", "path": "dem.tif",
                          "note": "offline mode (no internet requests)"})
    assert "offline mode" in off and "not used" not in off, off


def test_probe_error_says_timed_out():
    t = BM.probe_error_text("Operation canceled", 15.02, 15000)
    assert "within 15 s" in t and "timed out" in t, t
    t = BM.probe_error_text("", 3.0, 3000)
    assert "within 3 s (timed out)" in t, t
    t = BM.probe_error_text("Host aerial.dwr.nd.gov not found", 0.05, 15000)
    assert "timed out" not in t and "not found" in t, t


def test_probe_key_has_the_wms_layer():
    url = "https://aerial.dwr.nd.gov/dwrwms"
    a = url + "?SERVICE=WMS&LAYERS=NAIP_2025&BBOX=1,2,3,4&WIDTH=256&HEIGHT=256&CRS=EPSG:3857"
    b = url + "?service=WMS&layers=NAIP_2025&bbox=5,6,7,8&width=512&height=512&crs=EPSG:3857"
    c = a.replace("NAIP_2025", "NAIP_2024")
    assert BM.probe_key(url, a) == BM.probe_key(url, b)        # map position ignored
    assert BM.probe_key(url, a) != BM.probe_key(url, c)        # another layer: own check
    assert BM.probe_key(url) == url


def test_nd_dam_offline_zero_is_online():
    keys = ("ND_DAM_SUITE", "ND_DAM_OFFLINE")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.pop("ND_DAM_SUITE", None)
        for v, off in (("0", False), ("false", False), ("off", False), ("", False),
                       ("1", True), ("true", True), ("YES", True)):
            os.environ["ND_DAM_OFFLINE"] = v
            assert BM.suite_offline() is off, (v, off)
            assert bool(P.downloads_disabled()) is off, (v, off)
        os.environ.pop("ND_DAM_OFFLINE", None)
        os.environ["ND_DAM_SUITE"] = "/tmp/suite"
        assert BM.suite_offline() and P.downloads_disabled()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_hillshade_z_factor_follows_crs():
    proj = QgsRasterLayer(A.DEM, "dem", "gdal")
    assert abs(BM.hillshade_z_factor(proj) - BM.HILLSHADE_EXAGGERATION) < 1e-12
    geo = A._tif(os.path.join(A.TMP, "dem_geo.tif"), epsg=4326, x0=A.LON - 0.01,
                 y0=A.LAT + 0.01, size=0.02, n=20)
    g = QgsRasterLayer(geo, "dem_geo", "gdal")
    assert g.crs() == QgsCoordinateReferenceSystem("EPSG:4326")
    z = BM.hillshade_z_factor(g)
    import math
    want = BM.HILLSHADE_EXAGGERATION / (111320.0 * math.cos(math.radians(A.LAT)))
    assert abs(z - want) / want < 1e-3, (z, want)
    assert abs(BM.hillshade_z_factor(proj, 1.0) - 1.0) < 1e-12


def test_one_plugin_version():
    src = open(C.__file__, encoding="utf-8").read()
    assert src.count("\nPLUGIN_VERSION =") == 1
    meta = open(os.path.join(os.path.dirname(C.__file__), "metadata.txt"),
                encoding="utf-8").read()
    assert "\nversion={}\n".format(C.PLUGIN_VERSION) in meta


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
