"""Screening 0.17.32 (#109): the Settings window fits laptop screens, with the real Qt of QGIS.

    QT_QPA_PLATFORM=offscreen <QGIS python> DamSafety_Screening/tests/test_settings_window_fits.py

Before 0.17.32 the Run options tab (24 rows and the DEM box) set the Settings window's minimum
size: 1080 x 1223 in QGIS on the test box (1452 x 1498 offscreen here), so asking for 1280 x 800
or 1024 x 700 left Close / Save / Reset below a laptop screen. Now that tab scrolls, its
drop-downs no longer set the width, and the window is fitted to its screen (ui_fit.fit_window).

Prints SKIP when no real QGIS Python is available (run_all.py counts it as skipped). Fake
screens of each size stand in for the monitor (QApplication.screenAt / primaryScreen and the
main window's screen() patched)."""
PKG = "DamSafety_Screening"
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
ROOT = tempfile.mkdtemp(prefix="setfit_")
os.symlink(PLUGIN_DIR, os.path.join(ROOT, PKG))      # import the plugin by its folder name only
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["HOME"] = tempfile.mkdtemp(prefix="setfit_home_")     # no real QGIS profile or settings
os.environ.pop("NDSUITE_SETTINGS", None)

try:
    from qgis.core import QgsApplication
except ImportError:
    print("SKIP: no real QGIS Python here (run it with the QGIS Python)")
    sys.exit(0)
_app = QgsApplication.instance() or QgsApplication([], True)

from qgis.PyQt.QtCore import QRect, QCoreApplication, QEvent  # noqa: E402
from qgis.PyQt.QtWidgets import (QApplication, QWidget, QScrollArea, QAbstractButton,  # noqa: E402
                                 QLineEdit, QComboBox, QAbstractSpinBox, QPushButton)

import importlib  # noqa: E402
DS = importlib.import_module(PKG + ".damsuite.settings")
DS.set_current_path("", remember=False)
D = importlib.import_module(PKG + ".dialog")
LAPTOPS = ((1280, 800), (1366, 768), (1024, 700), (1920, 1080))


class FakeScreen(object):
    def __init__(self, w, h):
        self._r = QRect(0, 0, w, h)

    def availableGeometry(self):
        return QRect(self._r)

    def geometry(self):
        return QRect(self._r)


class Screen(object):
    """Every window opens on one screen of this size."""

    def __init__(self, w, h):
        self.scr = FakeScreen(w, h)

    def __enter__(self):
        scr = self.scr
        self._old = (QApplication.screenAt, QApplication.primaryScreen)
        QApplication.screenAt = staticmethod(lambda pos: scr)
        QApplication.primaryScreen = staticmethod(lambda: scr)
        D.NDDamScreeningDialog.screen = lambda self: scr        # the Settings window's parent
        return scr

    def __exit__(self, *exc):
        QApplication.screenAt, QApplication.primaryScreen = self._old
        del D.NDDamScreeningDialog.screen


def _settle():
    for _ in range(4):
        QCoreApplication.processEvents()


def _open():
    d = D.NDDamScreeningDialog()
    d.show()
    _settle()
    d.show_settings()
    _settle()
    return d, d.settings_window


def _close(d):
    d.settings_window.close()
    d.close()
    d.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _close_button(sw):
    return [b for b in sw.findChildren(QPushButton) if b.text() == "Close"][0]


def _inside(sw, w):
    tl = w.mapTo(sw, w.rect().topLeft())
    br = w.mapTo(sw, w.rect().bottomRight())
    return 0 <= tl.x() and 0 <= tl.y() and br.x() < sw.width() and br.y() < sw.height()


def test_minimum_size_fits_every_laptop_screen():
    for w, h in LAPTOPS:
        with Screen(w, h):
            d, sw = _open()
            try:
                assert sw.minimumHeight() <= h and sw.minimumWidth() <= w, (w, h, sw.minimumSize())
                assert sw.width() <= w and sw.height() <= h, (w, h, sw.size())
                assert sw.maximumWidth() <= w and sw.maximumHeight() <= h
            finally:
                _close(d)


def test_the_issue_sizes_are_honoured_and_close_stays_on_screen():
    for (sw_w, sw_h), ask in (((1280, 800), (1280, 800)), ((1024, 700), (1024, 700)),
                              ((1366, 768), (1280, 760))):
        with Screen(sw_w, sw_h):
            d, sw = _open()
            try:
                sw.resize(*ask)
                _settle()
                assert (sw.width(), sw.height()) == ask, (ask, sw.size())
                close = _close_button(sw)
                assert close.isVisible() and _inside(sw, close), (ask, close.geometry())
                for name in ("Save", "Reset all"):
                    btn = [b for b in sw.findChildren(QPushButton) if b.text() == name]
                    assert btn and _inside(sw, btn[0]), (ask, name)
            finally:
                _close(d)


def test_run_options_scroll_and_keep_their_row_height():
    with Screen(1024, 700):
        d, sw = _open()
        try:
            sw.resize(1024, 700)
            _settle()
            page = d.grid.tabs.widget(0)
            assert d.grid.tabs.tabText(0) == "Run options"
            assert isinstance(page, QScrollArea), type(page).__name__
            assert page.verticalScrollBar().maximum() > 0          # it does scroll at 700 px
            short = []
            for x in page.widget().findChildren(QWidget):
                if not isinstance(x, (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox)):
                    continue
                if isinstance(x.parent(), QAbstractSpinBox):
                    continue
                if x.height() < x.minimumSizeHint().height():
                    short.append((type(x).__name__, x.height(), x.minimumSizeHint().height()))
            assert not short, short
            assert page.horizontalScrollBar().maximum() == 0        # nothing wider than 1024
            assert hasattr(d, "local_dem") and d.local_dem.window() is sw   # same widgets, moved
        finally:
            _close(d)


def test_long_drop_downs_do_not_set_the_width_and_keep_their_text():
    with Screen(1280, 800):
        d, sw = _open()
        try:
            page = d.grid.tabs.widget(0).widget()
            for c in page.findChildren(QComboBox):
                assert c.minimumSizeHint().width() < 700, (c.currentText(), c.minimumSizeHint().width())
                assert c.toolTip(), c.currentText()
        finally:
            _close(d)


def _teardown():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QgsApplication.exitQgis()


if __name__ == "__main__":
    n = 0
    try:
        for _name, _fn in sorted(globals().items()):
            if _name.startswith("test_") and callable(_fn):
                _fn()
                n += 1
                print("ok", _name)
    finally:
        _teardown()
    print("{}: all {} checks passed".format(os.path.basename(__file__), n))
