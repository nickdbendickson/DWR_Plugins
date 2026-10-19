"""Hazard 2.18.32: the severity-ranked "Check inputs" panel (ui_check_panel.py).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_check_panel.py
    (or pytest). SHOTS=<folder> also saves screenshots of the three states.
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
from qgis.PyQt.QtWidgets import QPushButton, QLabel, QWidget, QVBoxLayout, QHBoxLayout  # noqa: E402

from DamSafety_Hazard import checks as CK  # noqa: E402
from DamSafety_Hazard.ui_check_panel import CheckPanel, Collapsible  # noqa: E402

ERRS = [(CK.ERROR, "depth values look like water-surface ELEVATIONS (median 1,512) - give the depth raster"),
        (CK.ERROR, "no velocity raster: ACER zones use max depth with max velocity")]
WARNS = [(CK.WARNING, "no arrival raster - no warning times"),
         (CK.WARNING, "maximum depth 180 ft - check the units")]
OKS = [(CK.OK, "depth: 1,000 wet cells"), (CK.OK, "velocity: fine"), (CK.OK, "outlines found")]


def _make():
    host = QWidget()
    host.resize(640, 10)
    v = QVBoxLayout(host)
    row = QHBoxLayout()
    chk, prim = QPushButton("Check inputs"), QPushButton("Classify")
    prim.setToolTip("base tip")
    row.addWidget(chk)
    row.addWidget(prim)
    row.addStretch()
    v.addLayout(row)
    panel = CheckPanel(chk, prim, action="classifying")
    v.addWidget(panel)
    v.addStretch()
    got = []
    panel.errors_fixed.connect(got.append)
    return host, panel, chk, prim, got


def _texts(w):
    return [lbl.text() for lbl in w.findChildren(QLabel)]


def _shot(host, name):
    folder = os.environ.get("SHOTS")
    if folder:
        host.show()
        host.adjustSize()
        _app.processEvents()
        host.grab().save(os.path.join(folder, name))


def test_errors_block():
    host, panel, chk, prim, got = _make()
    panel.set_results(ERRS + WARNS + OKS)
    assert panel.blocked and panel.n_errors == 2 and panel.n_warnings == 2
    assert chk.text() == "Check inputs \u26a0 2"
    assert not prim.isEnabled() and "Blocked" in prim.toolTip()
    t = _texts(panel)
    assert "2 errors must be fixed before classifying" in t
    assert any("ELEVATIONS" in x for x in t)
    assert got == [False]
    _shot(host, "check_panel_error.png")


def test_warnings_only():
    host, panel, chk, prim, got = _make()
    panel.set_results(WARNS + OKS)
    assert prim.isEnabled() and chk.text() == "Check inputs \u25d0 2"
    assert "2 warnings" in prim.toolTip()
    assert _texts(panel).count("WARN") == 2
    boxes = panel.findChildren(Collapsible)
    titles = [b.title() for b in boxes]
    assert "2 warnings \u2014 the run proceeds, check them" in titles
    assert "3 checks passed" in titles
    assert got == [True]
    _shot(host, "check_panel_warning.png")


def test_all_ok_and_reset():
    host, panel, chk, prim, got = _make()
    panel.set_results(ERRS)
    panel.set_results(OKS)                  # re-call resets everything
    assert prim.isEnabled() and prim.toolTip() == "base tip"
    assert chk.text() == "Check inputs" and not panel.blocked
    assert "WARN" not in _texts(panel) and not any("must be fixed" in x for x in _texts(panel))
    (box,) = panel.findChildren(Collapsible)
    assert not box.is_expanded() and box.header.text() == "\u25b8 3 checks passed"
    _shot(host, "check_panel_ok.png")
    for _ in range(3):                      # header text stays right across toggles
        box.header.toggle()
    assert box.is_expanded() and box.header.text() == "\u25be 3 checks passed"
    box.header.toggle()
    assert box.header.text() == "\u25b8 3 checks passed"
    assert got == [False, True]
    panel.set_results(ERRS)
    panel.clear()
    assert prim.isEnabled() and chk.text() == "Check inputs" and panel.isHidden()
    assert got == [False, True, False, True]


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


def test_smoke_wse_raster_as_depth():
    """A water-surface-elevation raster given as depth fires the red banner."""
    d = tempfile.mkdtemp()
    dep, vel = os.path.join(d, "wse.tif"), os.path.join(d, "vel.tif")
    _raster(dep, 1500 + np.random.RandomState(1).rand(50, 50).astype("float32") * 20)
    _raster(vel, np.full((50, 50), 3.0, "float32"))
    res = CK.check_all({"depth": dep, "vel": vel}, cfg={})
    assert any(l == CK.ERROR and "ELEVATIONS" in t for l, t in res), res
    host, panel, chk, prim, got = _make()
    panel.set_results(res)
    assert panel.blocked and not prim.isEnabled() and chk.text().startswith("Check inputs \u26a0")
    assert any("ELEVATIONS" in x for x in _texts(panel))
    _shot(host, "check_panel_smoke_wse.png")


def test_dialog_wiring():
    from DamSafety_Hazard.dialog import NDDamHazardDialog
    dlg = NDDamHazardDialog()
    assert dlg.check_panel.check_button is dlg.check_btn
    assert dlg.check_panel.primary_button is dlg.classify_btn
    dlg.check_panel.set_results(ERRS)
    assert not dlg.classify_btn.isEnabled()
    dlg.depth_edit.setText(dlg.depth_edit.text() + "x")    # an input change clears it
    assert dlg.classify_btn.isEnabled() and dlg.check_btn.text() == "Check inputs"
    dlg.close()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("{} passed".format(n))
    # All checks ran and passed (a failure raises before here). This builds a real QgsApplication,
    # whose offscreen teardown at interpreter shutdown can return non-zero / segfault on CI and make
    # the runner report FAIL despite the passing checks. Exit 0 explicitly before that teardown.
    sys.stdout.flush()
    os._exit(0)
