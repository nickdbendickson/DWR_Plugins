"""Hazard 2.18.36: Lumo's Hazard screens review - status line, badge, red box, ellipsis,
Review help, window fit. Real Qt (offscreen), no project, no network.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_ui_polish.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.PyQt.QtCore import QRect  # noqa: E402
from qgis.PyQt.QtWidgets import (QAbstractButton, QApplication, QComboBox, QDialog,  # noqa: E402
                                 QGroupBox, QLabel, QMessageBox, QWidget)

from DamSafety_Hazard import checks as CK  # noqa: E402
from DamSafety_Hazard import dialog as DLG  # noqa: E402
from DamSafety_Hazard import ui_fit as UF  # noqa: E402

CK.check_services = lambda *a, **k: []


def _pump(n=4):
    for _ in range(n):
        _app.processEvents()


def _raster(path, arr):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000, 10, 0, 5200000, 0, -10))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(arr)
    b.SetNoDataValue(-9999)
    ds = None


def _bad_dam():
    """Depth looks like water-surface elevations: one ERROR from Check inputs."""
    d = os.path.join(tempfile.mkdtemp(prefix="hz_polish_"), "DWR-7")
    os.makedirs(d)
    _raster(os.path.join(d, "Depth (Max).tif"),
            1500 + np.random.RandomState(1).rand(40, 40).astype("float32") * 20)
    _raster(os.path.join(d, "Velocity (Max).tif"), np.full((40, 40), 3.0, "float32"))
    _raster(os.path.join(d, "Arrival Time (Max).tif"), np.full((40, 40), 1.5, "float32"))
    return d


class _NoPopups(object):
    def __enter__(self):
        self.calls, self._old = [], {}
        for name in ("warning", "information", "critical"):
            self._old[name] = getattr(QMessageBox, name)
            setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, **k: self.calls.append(_n)))
        return self

    def __exit__(self, *exc):
        for name, fn in self._old.items():
            setattr(QMessageBox, name, fn)


def _make():
    from qgis.core import QgsProject
    from DamSafety_Hazard import settings as ST
    QgsProject.instance().removeEntry(ST.SCOPE, ST.KEY)
    d = DLG.NDDamHazardDialog()
    d.show()
    _pump()
    return d


def _done(d):
    d._busy = False
    d.close()
    d.deleteLater()
    _pump()


def test_check_state_shown_once_badge_is_warning_sign():
    d = _make()
    try:
        d.dam_folder_edit.setText(_bad_dam())
        with _NoPopups() as np_:
            d._on_check_inputs()
        _pump()
        assert not np_.calls
        assert d.check_panel.blocked and d.check_panel.isVisible()
        # item 1: the counts are in the red box and the button badge, not also in the status line
        assert d.status_label.text() == "" and not d.status_label.isVisible()
        # item 2: the badge is U+26A0 WARNING SIGN, never U+25B2 (black up-pointing triangle)
        assert d.check_btn.text() == "Check inputs \u26a0 1", repr(d.check_btn.text())
        assert "\u25b2" not in d.check_btn.text()
        # Classify (blocked) - the same: no status line repeating the red box
        d._update_status("something earlier", True)
        assert d.status_label.isVisible()
        with _NoPopups():
            d._on_run_all()
        assert d.status_label.text() == "" and not d.status_label.isVisible()
        # a real message still shows
        d._update_status("Input check FAILED: boom", False)
        assert d.status_label.isVisible() and "boom" in d.status_label.text()
    finally:
        _done(d)


def test_red_box_sits_with_check_and_classify_on_classify_only():
    d = _make()
    try:
        d.dam_folder_edit.setText(_bad_dam())
        with _NoPopups():
            d._on_check_inputs()
        _pump()
        panel = d.check_panel
        # item 3: in the action bar under the tabs (not inside a Classify-page section),
        # directly above Check inputs / Classify, which it badges and blocks
        assert panel.parentWidget() is d.action_area
        assert d.check_btn.parentWidget() is d.action_area
        assert d.classify_btn.parentWidget() is d.action_area
        lay = d.action_area.layout()
        assert lay.itemAt(0).widget() is panel
        assert panel.geometry().bottom() <= d.check_btn.geometry().top()
        assert not d.classify_scroll.isAncestorOf(panel)
        for view in ("Results", "Review"):
            d._show_view(view)
            _pump()
            assert not panel.isVisible() and not d.action_area.isVisible(), view
        d._show_view("Classify")
        _pump()
        assert panel.isVisible()
    finally:
        _done(d)


def _texts(host):
    out = []
    for w in host.findChildren(QAbstractButton):
        out.append(("button", w.text()))
    for w in host.findChildren(QLabel):
        out.append(("label", w.text()))
    for w in host.findChildren(QGroupBox):
        out.append(("group", w.title()))
    for w in host.findChildren(QComboBox):
        out.extend(("combo", w.itemText(i)) for i in range(w.count()))
    for w in host.findChildren(QWidget):
        out.extend(("action", a.text()) for a in w.actions())
        out.append(("tooltip", w.toolTip()))
    return out


def test_one_ellipsis_character_everywhere_in_the_window():
    d = _make()
    try:
        bad = []
        for host in (d, d.readme_dialog, d.suite_settings_dialog):
            if host is None:
                continue
            bad += [(k, t) for k, t in _texts(host) if "..." in (t or "")]
        bad += [("gear", a.text()) for a in d.gear_menu.actions() if "..." in a.text()]
        assert not bad, bad
        # the progress texts and plugin menu entry (source scan of the strings changed)
        here = os.path.dirname(HERE)
        for name, needle in (("plugin.py", '"Classify\\u2026"'),
                             ("dialog.py", '"Checking inputs and data services\\u2026"'),
                             ("pipeline.py", '"Classifying\\u2026"'),
                             ("facility_review_tab.py", 'not overtopped\\u2026)')):
            assert needle in open(os.path.join(here, name), encoding="utf-8").read(), name
    finally:
        _done(d)


def test_review_help_collapsed_by_default():
    d = _make()
    try:
        d._show_view("Review")
        _pump()
        for sec, word in ((d.review_help, "Set habitable"), (d.facrev_help, "AADT")):
            assert not sec.is_expanded() and not sec.inner().isVisible()
            assert word in sec.inner().text()
            sec.header.click()
            _pump()
            assert sec.is_expanded()
            sec.header.click()
            _pump()
            assert not sec.is_expanded()
        d.review_tabs.setCurrentIndex(0)
        _pump()
        assert d.review_help.isVisible() and d.review_help.header.text().startswith("\u25b8 ")
    finally:
        _done(d)


class _FakeScreen(object):
    def __init__(self, rect):
        self._r = rect

    def availableGeometry(self):
        return QRect(self._r)


def test_fit_size_never_larger_than_screen():
    assert UF.fit_size((1000, 720), QRect(0, 0, 1920, 1040)) == (1000, 720, 640, 420)
    w, h, mw, mh = UF.fit_size((1000, 720), QRect(0, 0, 800, 560))
    assert w <= 800 and h <= 560 and mw <= w and mh <= h
    w, h, mw, mh = UF.fit_size((1000, 720), QRect(0, 0, 480, 320))   # below the old 520x400 floor
    assert w <= 480 and h <= 320 and mw <= 480 and mh <= 320


def test_fit_window_uses_cursor_screen_not_primary():
    old = QApplication.screenAt
    second = _FakeScreen(QRect(1920, 0, 1024, 600))       # a small monitor right of the primary
    try:
        QApplication.screenAt = staticmethod(lambda pos: second)
        dlg = QDialog()                                    # no parent: the mouse's screen
        UF.fit_window(dlg, (1000, 720))
        assert dlg.width() <= 1024 and dlg.height() <= 600, (dlg.width(), dlg.height())
        assert dlg.maximumWidth() <= 1024 and dlg.maximumHeight() <= 600
        assert 1920 <= dlg.x() and dlg.x() + dlg.width() <= 1920 + 1024, dlg.x()
        assert 0 <= dlg.y() and dlg.y() + dlg.height() <= 600
        dlg.deleteLater()
    finally:
        QApplication.screenAt = old


def test_fit_window_uses_parent_screen():
    big = _FakeScreen(QRect(0, 0, 2560, 1400))

    class _Win(QWidget):
        def screen(self):
            return big
    parent = _Win()
    parent.setGeometry(100, 100, 1600, 1000)
    old = QApplication.screenAt
    try:
        QApplication.screenAt = staticmethod(lambda pos: _FakeScreen(QRect(0, 0, 640, 480)))
        dlg = QDialog(parent)
        assert UF._screen_for(dlg) is big
        UF.fit_window(dlg, (1000, 720))
        assert (dlg.width(), dlg.height()) == (1000, 720)
        assert dlg.maximumWidth() == 2560 and dlg.maximumHeight() == 1400
        dlg.deleteLater()
        parent.deleteLater()
    finally:
        QApplication.screenAt = old


def test_hazard_window_fits_its_screen():
    d = _make()
    try:
        scr = UF._screen_for(d)
        a = scr.availableGeometry()
        assert d.width() <= a.width() and d.height() <= a.height()
        assert a.contains(d.geometry().topLeft())
    finally:
        _done(d)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("{} passed".format(n))
