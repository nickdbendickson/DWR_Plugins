"""Runner 3.10.39 (#107): the "Download a DEM" rows keep their height when the section opens.

    QT_QPA_PLATFORM=offscreen <QGIS python> DamSafety_Runner/tests/realqt_dem_rows.py

The main window has an explicit minimum size (ui_fit.fit_window), so Qt does not raise it to the
layout's minimum: before 3.10.39, opening "Download a DEM" in a 1280x800 window squeezed its
combo boxes, line edits and buttons to 6-9 px and drew them on top of each other. Now the body of
the window scrolls and opening a section grows the window up to its screen.

Real Qt, a fake screen of each laptop size (QApplication.screenAt / primaryScreen patched).
Not named test_*: tests/test_dem_rows_real_qt.py runs it in its own process."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(PLUGIN_DIR)
ROOT = tempfile.mkdtemp(prefix="demrows_")
os.symlink(PLUGIN_DIR, os.path.join(ROOT, "DamSafety_Runner"))
# the DEM download code lives in DamSafety_Screening, next to the Runner. Look beside the path we
# were started from and beside the real (symlink-resolved) plugin folder: a nd_breach_runner ->
# DamSafety_Runner link outside the repo must still find it (3.10.83).
SCREENING = None
for _repo in (REPO, os.path.dirname(os.path.realpath(PLUGIN_DIR))):
    if os.path.isdir(os.path.join(_repo, "DamSafety_Screening")):
        SCREENING = os.path.join(_repo, "DamSafety_Screening")
        break
if SCREENING:
    os.symlink(SCREENING, os.path.join(ROOT, "DamSafety_Screening"))
elif os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
    print("FAIL: DamSafety_Screening is not next to DamSafety_Runner; the DEM rows cannot be built")
    sys.exit(1)
else:   # a Runner-only copy: the section is the "not available" stub, there are no rows to check
    print("SKIP: DamSafety_Screening is not next to DamSafety_Runner (Runner-only copy)")
    sys.exit(0)
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from qgis.core import QgsApplication
except ImportError:
    print("SKIP: no real QGIS Python here (run it with the QGIS Python)")
    sys.exit(0)
_app = QgsApplication.instance() or QgsApplication([], True)

from qgis.PyQt.QtCore import QRect, QCoreApplication, QEvent  # noqa: E402
from qgis.PyQt.QtWidgets import (QApplication, QWidget, QAbstractButton, QLineEdit,  # noqa: E402
                                 QComboBox, QAbstractSpinBox, QLabel)

import importlib  # noqa: E402
D = importlib.import_module("DamSafety_Runner.dialog")
SIZES = ((1280, 800), (1366, 768), (1024, 700))


class FakeScreen(object):
    def __init__(self, w, h):
        self._r = QRect(0, 0, w, h)

    def availableGeometry(self):
        return QRect(self._r)

    def geometry(self):
        return QRect(self._r)


class Screen(object):
    def __init__(self, w, h):
        self.scr = FakeScreen(w, h)

    def __enter__(self):
        self._old = (QApplication.screenAt, QApplication.primaryScreen)
        scr = self.scr
        QApplication.screenAt = staticmethod(lambda pos: scr)
        QApplication.primaryScreen = staticmethod(lambda: scr)
        return self.scr

    def __exit__(self, *exc):
        QApplication.screenAt, QApplication.primaryScreen = self._old


def _settle():
    for _ in range(4):
        QCoreApplication.processEvents()


def _open(expand=True, tab=0):
    d = D.NDBreachRunnerDialog()
    d.tabs.setCurrentIndex(tab)
    d.show()
    _settle()
    d.dem_section.set_expanded(expand)
    _settle()
    return d


def _close(d):
    d.close()
    d.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _rows(d):
    out = []
    for w in d.dem_section.body.findChildren(QWidget):
        if not w.isVisible() or isinstance(w.parent(), QAbstractSpinBox):   # a spin box's own editor
            continue
        if isinstance(w, (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox, QLabel)):
            out.append(w)
    return out


def _need(w):
    if isinstance(w, QLabel) and w.wordWrap():
        return w.heightForWidth(w.width())
    return w.minimumSizeHint().height()


def _name(w):
    t = w.text() if hasattr(w, "text") and callable(getattr(w, "text")) else ""
    return "{} {!r}".format(type(w).__name__, (t or "")[:30])


def test_dem_rows_keep_their_height_on_laptop_screens():
    for w, h in SIZES:
        for tab in (0, 1):                                   # Run and Tools tab
            with Screen(w, h):
                d = _open(True, tab)
                try:
                    assert hasattr(d, "dem_source"), "DEM download section is the 'not available' stub"
                    assert d.dem_section.body.isVisible()
                    short = [(_name(x), x.height(), _need(x)) for x in _rows(d) if x.height() < _need(x)]
                    assert not short, ((w, h, tab), short)
                    assert len(_rows(d)) >= 15, len(_rows(d))
                finally:
                    _close(d)


def test_dem_rows_never_overlap():
    with Screen(1280, 800):
        d = _open(True)
        try:
            body = d.dem_section.body
            rects = [(x, x.geometry()) for x in _rows(d) if x.parent() is body]
            for i, (a, ra) in enumerate(rects):
                for b, rb in rects[i + 1:]:
                    assert not ra.intersects(rb), (_name(a), ra, _name(b), rb)
        finally:
            _close(d)


def test_window_fits_the_screen_and_the_body_scrolls():
    for w, h in SIZES:
        with Screen(w, h) as scr:
            d = _open(True)
            try:
                a = scr.availableGeometry()
                assert d.width() <= a.width() and d.height() <= a.height(), (w, h, d.size())
                assert a.contains(d.frameGeometry().topLeft()), (w, h, d.frameGeometry())
                sb = d.body_scroll.verticalScrollBar()
                content = d.body_widget.minimumSizeHint().height()
                assert content <= d.body_scroll.viewport().height() or sb.maximum() > 0, (w, h)
                assert d.close_btn.isVisible()
                assert d.close_btn.mapTo(d, d.close_btn.rect().bottomLeft()).y() <= d.height()
            finally:
                _close(d)


def test_opening_the_section_grows_the_window_up_to_the_screen():
    with Screen(1920, 1200):
        d = _open(False)
        try:
            before = d.height()
            d.dem_section.set_expanded(True)
            _settle()
            assert d.height() >= before and d.height() <= 1200, (before, d.height())
            assert d.body_scroll.verticalScrollBar().maximum() == 0      # all of it fits now
        finally:
            _close(d)


def test_collapsed_window_has_no_scroll_and_tabs_get_the_room():
    with Screen(1280, 800):
        d = _open(False)
        try:
            assert d.body_scroll.verticalScrollBar().maximum() == 0
            assert d.tabs.height() >= 150
        finally:
            _close(d)


def _teardown():
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QgsApplication.exitQgis()


if __name__ == "__main__":
    n = 0
    try:
        for _name_, _fn in sorted(globals().items()):
            if _name_.startswith("test_") and callable(_fn):
                _fn()
                n += 1
                print("ok", _name_)
    finally:
        _teardown()
    print("{}: all {} checks passed".format(os.path.basename(__file__), n))
