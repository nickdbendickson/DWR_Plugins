"""Offscreen smoke test of the 'Get basin data from StreamStats…' window (real QGIS, no network:
the StreamStats client is the real one in offline mode on the saved responses).

Skips without QGIS. Set PMP_SHOTS=<folder> to save screenshots of the window.
Run:  QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_PMP/tests/test_streamstats_dialog.py
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures_streamstats"
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)

qgis_core = pytest.importorskip("qgis.core")
from qgis.core import QgsApplication, QgsProject  # noqa: E402

_APP = QgsApplication.instance()
if _APP is None:
    os.environ["HOME"] = tempfile.mkdtemp(prefix="pmp_ss_home_")   # never the real profile
    _APP = QgsApplication([], True)
    _APP.initQgis()

from DamSafety_PMP import plugin as pmp_plugin  # noqa: E402
from DamSafety_PMP import streamstats_dialog as sd  # noqa: E402

ss = sd.ss
SHOTS = os.environ.get("PMP_SHOTS")


def _pump(n=5):
    for _ in range(n):
        _APP.processEvents()


def _shot(win, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        _pump()
        win.grab().save(os.path.join(SHOTS, name + ".png"))
        page = win.findChild(sd.FitScroll).widget()          # the whole scrolling page
        page.grab().save(os.path.join(SHOTS, name + "_page.png"))


def _factory(cache, task):
    def no_net(*a):
        raise AssertionError("network used in the dialog test")
    return ss.StreamStatsClient(cache, fetcher=no_net, offline=True, min_interval_s=0,
                                should_cancel=task.isCanceled, progress=task.report)


def _dialog(tmp_path, fixture, name, lat, lon):
    out = tmp_path / "out"
    shutil.copytree(FIX / fixture, out / "streamstats_cache")
    d = sd.StreamStatsDialog(None, None, client_factory=_factory, run_sync=True)
    d.folder.setText(str(out))
    d.name.setText(name)
    d.lat.setValue(lat)
    d.lon.setValue(lon)
    d.show()
    _pump()
    return d, out


def test_menu_action_label():
    class Iface:
        def __init__(self):
            from qgis.PyQt.QtWidgets import QWidget
            self.w, self.menu = QWidget(), []

        def mainWindow(self):
            return self.w

        def addPluginToMenu(self, menu, action):
            self.menu.append((menu, action))

        def removePluginMenu(self, menu, action):
            self.menu.remove((menu, action))

    iface = Iface()
    p = pmp_plugin.NDPMPPlugin(iface)
    p.initGui()
    try:
        assert [(m, a.text()) for m, a in iface.menu] == [
            ("DamSafety PMP", "Get basin data from StreamStats\u2026")]
        assert not p.action.icon().isNull()
    finally:
        p.unload()
    assert iface.menu == []


def test_dialog_fills_basin_inputs_dwr2801(tmp_path):
    d, out = _dialog(tmp_path, "both_dams", "DWR2801", 46.98337, -97.39113)
    assert d.windowTitle().startswith("DamSafety PMP")
    assert not d.open_hydro_btn.isEnabled()
    d.run_btn.click()
    _pump()
    assert d.area.value() == pytest.approx(16.6, abs=0.05)
    assert d.noncontrib.value() == pytest.approx(4.46, abs=0.01)
    assert "suggested" in d.noncontrib_note.text().lower()
    assert d.timing_method() in ("kirpich", "nrcs")
    d.method.setCurrentIndex(0)
    assert 5 < d.tc.value() < 15 and d.lag.value() == pytest.approx(0.6 * d.tc.value(), abs=0.02)
    assert "Kirpich" in d.timing_note.text()
    d.cn.setValue(80)
    d.method.setCurrentIndex(1)
    assert "NRCS" in d.timing_note.text() and d.lag.value() > 0
    assert d.table.rowCount() == 8
    assert d.table.item(6, 0).text() == "0.2%" and d.table.item(6, 1).text() == "1,300"
    assert "27,400" in d.table.item(7, 1).text()
    assert d.progress.value() == 100 and not d.checks.blocked
    lyr = QgsProject.instance().mapLayer(d.layer_id)
    assert lyr is not None and lyr.isValid() and lyr.featureCount() == 1
    for k in ("watershed", "basin", "provenance"):
        assert Path(d.saved[k]).is_file()
    hp = d.hydro_params()
    assert "CN" not in hp and hp["TRANSFORM"] == 0 and hp["AREA_MI2"] == pytest.approx(16.6, abs=0.05)
    basin = json.loads(Path(hp["STREAMSTATS_JSON"]).read_text())
    assert basin["timing"]["method"] == "nrcs"          # re-saved with the edited method
    assert d.pmp_params()["BASIN"] == d.layer_id
    assert d.open_hydro_btn.isEnabled() and d.open_pmp_btn.isEnabled()
    d.method.setCurrentIndex(0)
    _shot(d, "streamstats_dwr2801")
    QgsProject.instance().removeAllMapLayers()
    d.close()


def test_dialog_not_on_stream_blocks_until_point_moves(tmp_path):
    d, out = _dialog(tmp_path, "unsnapped_nid_2801", "DWR2801", 46.98438182, -97.39091754)
    d.run_btn.click()
    _pump()
    assert d.checks.blocked and not d.run_btn.isEnabled()
    assert "Move the point onto the channel" in d.log.toPlainText()
    assert d.result is None and not d.open_hydro_btn.isEnabled()
    _shot(d, "streamstats_not_on_stream")
    d.lat.setValue(46.98337)                         # moving the point clears the error
    _pump()
    assert not d.checks.blocked
    d.close()


def test_dialog_input_checks(tmp_path):
    d = sd.StreamStatsDialog(None, None, client_factory=_factory, run_sync=True)
    d.folder.setText("")
    d.lat.setValue(44.0)
    assert not d.check_inputs()
    assert d.checks.blocked
    d.close()
