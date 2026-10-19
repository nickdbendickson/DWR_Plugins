"""Lumo vReal.12 ES.1 / ES.4 / SE.2 (Hazard 2.18.23).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real12.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import estimate as ES  # noqa: E402
from DamSafety_Hazard import settings as ST  # noqa: E402

AREA = {"extent_4326": (-100.8, 46.8, -100.7, 46.9)}


class _Lyr:
    def __init__(self, src, name="lyr"):
        self._s, self._n = src, name

    def source(self):
        return self._s

    def name(self):
        return self._n


def _cfg(**kw):
    c = {"point_sources": [], "osm_layer": _Lyr("/tmp/some_outlines.gpkg", "outlines"),
         "output_folder": tempfile.mkdtemp(), "dam_folder": "", "local_data": None,
         "download_usa_structures": True, "download_address": True}
    c.update(kw)
    return c


def _names(cfg):
    return [j[0] for j in ES.planned_downloads(cfg, AREA)]


def _clear_env():
    for k in ("ND_DAM_SUITE", "ND_DAM_OFFLINE"):
        os.environ.pop(k, None)


def test_es1a_env_no_downloads():
    _clear_env()
    assert _names(_cfg())
    for k in ("ND_DAM_SUITE", "ND_DAM_OFFLINE"):
        os.environ[k] = "1"
        try:
            assert ES.planned_downloads(_cfg(), AREA) == []
        finally:
            _clear_env()


def test_es1b_address_layer_skips_download():
    _clear_env()
    assert "NDGISHUB_address_points" in _names(_cfg())
    c = _cfg(point_sources=[("NDGISHUB", _Lyr("/tmp/addr.gpkg"))])
    assert "NDGISHUB_address_points" not in _names(c)


def test_es1c_usa_structures():
    _clear_env()
    assert "USA_Structures" in _names(_cfg())
    fema = _cfg(osm_layer=_Lyr("/tmp/x/FEMA_Structures.gpkg", "FEMA Structures"))
    assert "USA_Structures" not in _names(fema)
    none = _cfg(osm_layer=None)
    jobs = [j for j in ES.planned_downloads(none, AREA) if j[0] == "USA_Structures"]
    assert jobs and "outline file" in jobs[0][1]


def test_es1d_refresh_only_with_tile_cache():
    orig = ES._todays_download
    ES._todays_download = lambda *a: "/tmp/today.gpkg"
    try:
        job = ("NID_dams", "NID", "arcgis", "http://x", AREA["extent_4326"])
        row = ES._plan_one(job, None, None, False, 7, True, 0, "/tmp")
        assert row["source"] == "today's download", row
        d = tempfile.mkdtemp()
        row = ES._plan_one(job, d, None, False, 7, True, 0, "/tmp")
        assert row["source"] != "today's download", row
    finally:
        ES._todays_download = orig


def test_es4_eap_on_pipeline_defaults():
    from DamSafety_Hazard import pipeline as PL
    for cfg in ({}, {"eap": True}, {"eap": True, "eap_travel": True},
                {"eap": True, "eap_mapbook": True, "eap_list": False}, {"eap_travel": True}):
        for part in ("travel", "list", "mapbook"):
            assert ES._eap_on(cfg, part) == PL._eap_on(cfg, part), (cfg, part)


def test_se2_failed_keys():
    from qgis.PyQt.QtWidgets import QComboBox, QSpinBox, QCheckBox
    cb = QComboBox()
    cb.addItem("a", "a")
    cb.addItem("b", "b")
    assert ST.set_widget_value(cb, "b") is True and cb.currentIndex() == 1
    assert ST.set_widget_value(cb, "zzz") is False and cb.currentIndex() == 1
    sp = QSpinBox()
    assert ST.set_widget_value(sp, "abc") is False
    assert ST.set_widget_value(QCheckBox(), True) is True
    meta = {"warnings": []}
    ST.note_failed_keys(meta, ["mode_combo", "cell_spin"])
    assert "cell_spin, mode_combo" in meta["warnings"][0]
    assert ST.note_failed_keys({"warnings": []}, [])["warnings"] == []


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", n, e)
    sys.stdout.flush()
    if fails:
        sys.exit(1)
    # all passed; os._exit(0) before the real-QgsApplication offscreen teardown segfaults at shutdown
    os._exit(0)
