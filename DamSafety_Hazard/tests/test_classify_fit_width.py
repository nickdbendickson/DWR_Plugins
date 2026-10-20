"""Hazard 2.18.42 (#108): the Classify view never scrolls sideways.

Until 2.18.41 the long one-line check boxes (the lifeline-facilities one is 1 250 px) set the
minimum width of the whole scrolled Classify page: with its sections open the page was
1 530 px wide, so in a 1280 x 800 or 1024 x 640 window the Browse…, Zip…, Import, Read plan
and 'Use this plan for the classification' buttons sat past the right edge. Built in a real
Qt (offscreen) - no project, no network.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_classify_fit_width.py
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

from qgis.PyQt.QtCore import QPoint, Qt  # noqa: E402
from qgis.PyQt.QtTest import QTest  # noqa: E402
from qgis.PyQt.QtWidgets import (QAbstractButton, QAbstractSpinBox, QCheckBox, QComboBox,  # noqa: E402
                                 QLineEdit, QWidget)

from DamSafety_Hazard import checks as CK  # noqa: E402
from DamSafety_Hazard import dialog as DLG  # noqa: E402
from DamSafety_Hazard.ui_widgets import Collapsible  # noqa: E402

CK.check_services = lambda *a, **k: []          # no network

SIZES = ((1280, 800), (1024, 640))
# (Under Xvfb with the QGIS fonts the page needs 591 px and also fits the 640 x 420 minimum
# window; the offscreen platform's fallback font is about a quarter wider, so the test keeps to
# the two screen sizes of the issue.)


def _pump(n=8):
    for _ in range(n):
        _app.processEvents()


def _make():
    from qgis.core import QgsProject
    from DamSafety_Hazard import settings as ST
    QgsProject.instance().removeEntry(ST.SCOPE, ST.KEY)     # no settings of an earlier test
    d = DLG.NDDamHazardDialog()
    d.setMaximumSize(8000, 8000)
    d.show()
    _pump()
    d.tab_widget.setCurrentIndex(d._view_index("Classify"))
    d.raster_group.setChecked(True)
    for sec in d.findChildren(Collapsible):          # every section open (worst case)
        if sec is not d.log_section:
            sec.set_expanded(True)
    _pump()
    return d


def _done(d):
    d._busy = False
    d.close()
    d.deleteLater()
    _pump()


def _cut_at_right(d):
    """Visible leaf widgets / inputs / buttons on the Classify page that end past the right edge
    of the scroll view (what the GUI harness reported as clipped_by_scroll_h)."""
    sa = d.classify_scroll
    vp = sa.viewport()
    right = vp.rect().right() + 1
    cut = []
    for w in sa.widget().findChildren(QWidget):
        if not w.isVisible() or w.width() <= 0:
            continue
        leaf = not w.findChildren(QWidget) or isinstance(
            w, (QAbstractButton, QLineEdit, QComboBox, QAbstractSpinBox))
        if not leaf:
            continue
        x = w.mapTo(vp, QPoint(0, 0)).x()
        if x + w.width() > right:
            cut.append("{} {!r} ends at {} > {}".format(
                type(w).__name__, str(getattr(w, "text", lambda: "")())[:50], x + w.width(), right))
    return cut


def test_classify_page_fits_without_sideways_scrolling():
    d = _make()
    try:
        sa = d.classify_scroll
        for w, h in SIZES:
            d.resize(w, h)
            _pump()
            page, vp = sa.widget(), sa.viewport()
            need = page.minimumSizeHint().expandedTo(page.minimumSize()).width()
            assert need <= vp.width(), (
                "Classify page needs {} px, the view at {}x{} is {} px".format(need, w, h, vp.width()))
            hbar = sa.horizontalScrollBar()
            assert hbar.maximum() == 0 and not hbar.isVisible(), (w, h, hbar.maximum())
            cut = _cut_at_right(d)
            assert not cut, "{}x{}: {} widgets cut off at the right, e.g. {}".format(
                w, h, len(cut), cut[:5])
        # the buttons named in #108 are inside the view at 1024 x 640
        d.resize(1024, 640)
        _pump()
        right = sa.viewport().width()
        for btn in (d.dam_browse_btn, d.find_rasters_btn, d.depth_browse_btn):
            assert btn.mapTo(sa.viewport(), QPoint(0, 0)).x() + btn.width() <= right, btn.text()
    finally:
        _done(d)


def test_no_long_one_line_check_box_on_classify():
    """A new long QCheckBox (it cannot wrap) would widen the page again: Classify check boxes
    longer than a narrow window are WrapCheckBox."""
    from DamSafety_Hazard.ui_wrap import WrapCheckBox
    d = _make()
    try:
        long_plain = [cb.text()[:60] for cb in d.classify_scroll.widget().findChildren(QCheckBox)
                      if not isinstance(cb, WrapCheckBox) and cb.minimumSizeHint().width() > 400]
        assert not long_plain, long_plain
        # the settings / signals still see check boxes
        assert isinstance(d.lifelines_download_check, QCheckBox)
    finally:
        _done(d)


def test_wrap_check_box_wraps_and_toggles():
    from DamSafety_Hazard.ui_wrap import WrapCheckBox
    text = ("Lifeline facilities - hospitals, care homes, schools, fire / EMS / police, utilities "
            "(ND GIS Hub, EIA, OpenStreetMap) (internet)")
    cb = WrapCheckBox(text)
    one = QCheckBox(text)
    assert cb.sizeHint() == one.sizeHint()                       # same one-line size
    assert cb.minimumSizeHint().width() < one.sizeHint().width() // 2
    assert cb.hasHeightForWidth()
    assert cb.heightForWidth(one.sizeHint().width()) == one.sizeHint().height()
    assert cb.heightForWidth(220) > 2 * one.sizeHint().height()  # wraps onto more lines
    cb.resize(220, cb.heightForWidth(220))
    cb.show()
    _pump()
    assert not cb.isChecked()
    # a click on the wrapped text (last line, right part) toggles it, as on a QCheckBox label
    QTest.mouseClick(cb, Qt.LeftButton, Qt.NoModifier, QPoint(180, cb.height() - 4))
    assert cb.isChecked()
    cb.setText("short")
    assert cb.heightForWidth(220) == QCheckBox("short").sizeHint().height()
    cb.hide()
    cb.deleteLater()
    _pump()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("{} passed".format(n))
    sys.stdout.flush()
    os._exit(0)                     # skip the offscreen QgsApplication teardown (see run_all)
