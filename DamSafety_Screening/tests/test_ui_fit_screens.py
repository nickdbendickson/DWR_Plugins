"""Screening 0.17.25: monitor-aware window sizing (ui_fit.py) with the real Qt of QGIS, offscreen.

    QT_QPA_PLATFORM=offscreen <QGIS python> DamSafety_Screening/tests/test_ui_fit_screens.py

Prints SKIP when no real QGIS Python is available (run_all.py counts it as skipped). Fake
screens stand in for a second monitor (QApplication.screenAt / primaryScreen patched, a parent
with its own screen)."""
PKG = "DamSafety_Screening"
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
ROOT = tempfile.mkdtemp(prefix="uifit_")
os.symlink(PLUGIN_DIR, os.path.join(ROOT, PKG))      # import the plugin by its folder name only
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from qgis.core import QgsApplication
except ImportError:
    print("SKIP: no real QGIS Python here (run it with the QGIS Python)")
    sys.exit(0)
_app = QgsApplication.instance() or QgsApplication([], True)

from qgis.PyQt.QtCore import QRect  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402

import importlib  # noqa: E402
UF = importlib.import_module(PKG + ".ui_fit")


class FakeScreen(object):
    """Stands in for a QScreen: only availableGeometry() is asked."""

    def __init__(self, x, y, w, h):
        self._r = QRect(x, y, w, h)

    def availableGeometry(self):
        return QRect(self._r)


class Patched(object):
    """QApplication.screenAt / primaryScreen replaced for one test."""

    def __init__(self, at=None, primary=None):
        self.at, self.primary = at, primary

    def __enter__(self):
        self._old = (QApplication.screenAt, QApplication.primaryScreen)
        at, primary = self.at, self.primary
        QApplication.screenAt = staticmethod(lambda pos: at)
        if primary is not None:
            QApplication.primaryScreen = staticmethod(lambda: primary)
        return self

    def __exit__(self, *exc):
        QApplication.screenAt, QApplication.primaryScreen = self._old


def _inside(dlg, scr):
    a = scr.availableGeometry()
    return (a.left() <= dlg.x() and dlg.x() + dlg.width() <= a.left() + a.width()
            and a.top() <= dlg.y() and dlg.y() + dlg.height() <= a.top() + a.height())


def test_fit_size_never_larger_than_the_screen():
    assert UF.fit_size((1000, 720), QRect(0, 0, 1920, 1040)) == (1000, 720, 640, 420)
    for w, h in ((800, 560), (480, 320), (300, 200)):          # smaller than the old 520 x 400 floor
        fw, fh, mw, mh = UF.fit_size((1000, 720), QRect(0, 0, w, h))
        assert fw <= w and fh <= h and mw <= fw and mh <= fh, (w, h, fw, fh, mw, mh)


def test_second_monitor_under_the_mouse_not_the_primary():
    primary = FakeScreen(0, 0, 2560, 1400)                       # big primary on the left
    laptop = FakeScreen(2560, 200, 1024, 600)                    # small monitor under the mouse
    with Patched(at=laptop, primary=primary):
        dlg = QDialog()
        UF.fit_window(dlg, (1000, 720))
        assert dlg.width() <= 1024 and dlg.height() <= 600, (dlg.width(), dlg.height())
        assert dlg.maximumWidth() <= 1024 and dlg.maximumHeight() <= 600
        assert _inside(dlg, laptop), (dlg.x(), dlg.y(), dlg.width(), dlg.height())
        dlg.deleteLater()


def test_parent_screen_wins_over_the_mouse():
    big = FakeScreen(-1920, 0, 1920, 1080)                       # QGIS on a monitor left of 0,0

    class Win(QWidget):
        def screen(self):
            return big
    parent = Win()
    parent.setGeometry(-1800, 100, 1600, 900)
    with Patched(at=FakeScreen(0, 0, 640, 480)):
        dlg = QDialog(parent)
        assert UF._screen_for(dlg) is big
        UF.fit_window(dlg, (1000, 720))
        assert (dlg.width(), dlg.height()) == (1000, 720)
        assert dlg.maximumWidth() == 1920 and dlg.maximumHeight() == 1080
        assert _inside(dlg, big), (dlg.x(), dlg.y())
        dlg.deleteLater()
    parent.deleteLater()


def test_small_parent_screen_caps_the_window():
    small = FakeScreen(0, 0, 800, 500)

    class Win(QWidget):
        def screen(self):
            return small
    parent = Win()
    parent.setGeometry(0, 0, 800, 500)
    with Patched(at=FakeScreen(0, 0, 3840, 2160)):
        dlg = QDialog(parent)
        UF.fit_window(dlg, (1000, 720))
        assert dlg.width() <= 800 and dlg.height() <= 500 and _inside(dlg, small)
        assert dlg.minimumWidth() <= 800 and dlg.minimumHeight() <= 500
        dlg.deleteLater()
    parent.deleteLater()


def test_cap_follows_the_window_to_another_monitor():
    dlg = QDialog()
    UF.fit_window(dlg, (1000, 720))
    handle = dlg.windowHandle()
    assert handle is not None and getattr(dlg, "_fit_follows_screen", False)
    real = QApplication.primaryScreen()
    a = real.availableGeometry()
    dlg.setMaximumSize(10000, 10000)
    dlg.resize(a.width() + 400, a.height() + 300)                # as if it came from a bigger monitor
    handle.screenChanged.emit(real)
    assert dlg.maximumWidth() == a.width() and dlg.maximumHeight() == a.height()
    assert dlg.width() <= a.width() and dlg.height() <= a.height()
    dlg.deleteLater()


def test_no_screen_uses_the_preferred_size():
    with Patched(at=None):
        old = QApplication.primaryScreen
        QApplication.primaryScreen = staticmethod(lambda: None)
        try:
            dlg = QDialog()
            UF.fit_window(dlg, (700, 500))
            assert (dlg.width(), dlg.height()) == (700, 500)
            dlg.deleteLater()
        finally:
            QApplication.primaryScreen = old


if __name__ == "__main__":
    n = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            n += 1
            print("ok", _name)
    print("{}: all {} checks passed".format(os.path.basename(__file__), n))
    # Exit without Python's interpreter teardown: some QGIS builds (conda-forge on GitHub
    # Actions) crash while destroying the QgsApplication and its widgets at exit, which
    # turned a passing run into a failure. Every check has already run and been reported.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
