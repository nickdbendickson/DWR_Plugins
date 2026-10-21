"""Hazard 2.18.40: the suite hand-off into this window (the equivalent of ND Hazard 2.30.0's
test_v2300 'the_window_opens_on_setup_and_run_and_takes_a_hand_off').

The Dam suite home's "Classify" (suite_home.point_window_at) fills dam_folder_edit with the dam
the Runner wrote Depth (Max).tif / Velocity (Max).tif / Arrival Time.tif into, then calls the
window's refresh hook. The rasters must be found, Classify (the one-click run: find, check,
classify) must be on, and nothing may start by itself. Before 2.18.40 the window had no refresh
hook, so pointing it again at the dam it already showed (opened before the Runner finished, or
restored from the last session) fired no textChanged and left the raster boxes empty.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_suite_handoff.py
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

from DamSafety_Hazard import checks as CK  # noqa: E402
from DamSafety_Hazard import dialog as DLG  # noqa: E402
from DamSafety_Hazard import suite_home as SH  # noqa: E402

CK.check_services = lambda *a, **k: []          # no network
RASTERS = ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif")


def _pump(n=6):
    for _ in range(n):
        _app.processEvents()


def _raster(path, value):
    ds = gdal.GetDriverByName("GTiff").Create(path, 30, 30, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000, 10, 0, 5200000, 0, -10))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((30, 30), value, "float32"))
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds = None


def _runner_outputs(dam):
    for i, name in enumerate(RASTERS):
        _raster(os.path.join(dam, name), 1.0 + i)


def _dams(*ids):
    dams = os.path.join(tempfile.mkdtemp(prefix="hz_handoff_"), "Dams")
    for dam in ids:
        os.makedirs(os.path.join(dams, dam))
    return dams


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


def _names(d):
    return [os.path.basename(e.text()) for e in (d.depth_edit, d.vel_edit, d.arr_edit)]


def _point(d, dams, dam):
    done = SH.point_window_at(d, "ndhazard", dams, {"folder": os.path.join(dams, dam)})
    _pump()
    return done


def test_hand_off_fills_the_rasters_and_offers_classify_without_running():
    dams = _dams("2801")
    _runner_outputs(os.path.join(dams, "2801"))
    d = _make()
    started = []
    d._on_run_all = lambda *a: started.append(1)
    try:
        assert hasattr(d, "refresh"), "the suite home's hand-off hook"
        assert _point(d, dams, "2801") == ["dam_folder"]
        assert d.config_dam_folder == os.path.join(dams, "2801")
        assert _names(d) == list(RASTERS), _names(d)
        assert d.classify_btn.isEnabled() and d._inputs_valid_for_main()
        assert "3 raster(s) found" in d.status_label.text(), d.status_label.text()
        assert started == [] and not d._busy             # an offer, never a run by itself
        d.classify_btn.click()                            # the one click
        _pump()
        assert started == [1]
    finally:
        _done(d)


def test_hand_off_to_the_dam_already_shown_finds_the_new_rasters():
    """The window was opened on the dam before the Runner finished (or restored with it);
    the suite home then hands the same dam over again."""
    dams = _dams("2801")
    dam = os.path.join(dams, "2801")
    d = _make()
    try:
        _point(d, dams, "2801")
        assert _names(d) == ["", "", ""]
        _runner_outputs(dam)                              # the Runner writes its rasters
        _point(d, dams, "2801")                           # same text: no textChanged
        assert _names(d) == list(RASTERS), _names(d)
        assert "3 raster(s) found" in d.status_label.text(), d.status_label.text()
        assert d.classify_btn.isEnabled()
    finally:
        _done(d)


def test_hand_off_keeps_hand_chosen_rasters_and_switches_dams_cleanly():
    dams = _dams("2801", "2802")
    for dam in ("2801", "2802"):
        _runner_outputs(os.path.join(dams, dam))
    d = _make()
    try:
        _point(d, dams, "2801")
        mine = os.path.join(dams, "2801", "Depth (Max) mine.tif")
        _raster(mine, 7.0)
        d.depth_edit.setText(mine)                        # the engineer's own choice
        _point(d, dams, "2801")
        assert d.depth_edit.text() == mine                # refresh fills empty boxes only
        _point(d, dams, "2802")                           # another dam: its own rasters
        assert all(os.sep + "2802" + os.sep in e.text()
                   for e in (d.depth_edit, d.vel_edit, d.arr_edit)), _names(d)
        assert not d._raster_ownership_problems()
        d._busy = True                                    # a run in progress: hands off
        d.depth_edit.setText("")
        assert d.refresh() == 0 and d.depth_edit.text() == ""
    finally:
        _done(d)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name, flush=True)
    print("{} passed".format(n), flush=True)
    sys.stdout.flush()
    os._exit(0)      # skip the offscreen QgsApplication teardown (see test_dam_folder_rasters)
