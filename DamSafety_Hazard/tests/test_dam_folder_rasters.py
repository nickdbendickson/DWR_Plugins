"""Hazard 2.18.35: the dam folder must be ONE dam's folder, and the rasters must belong to
it (GUI test H-1 / H-2 on 8b0601c). Qt-free (damfolder + autodetect) and real-Qt
(offscreen) dialog tests, with the exact repro of the GUI test report:

  H-1  dam folder 2801 -> 9999 kept 2801's depth / velocity / arrival rasters
  H-2  the Dams folder ("Dams", "Dams/") and "//" were accepted as the dam folder

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_dam_folder_rasters.py
"""
import json
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
from qgis.PyQt.QtWidgets import QMessageBox  # noqa: E402

from DamSafety_Hazard import checks as CK  # noqa: E402
from DamSafety_Hazard import damfolder as DF  # noqa: E402
from DamSafety_Hazard import dialog as DLG  # noqa: E402
from DamSafety_Hazard.autodetect import find_rasters  # noqa: E402

CK.check_services = lambda *a, **k: []          # Check inputs without the network
RASTERS = ("Depth (Max).tif", "Velocity (Max).tif", "Arrival Time.tif")


def _pump(n=6):
    for _ in range(n):
        _app.processEvents()


def _raster(path, value):
    arr = np.full((30, 30), value, "float32")
    ds = gdal.GetDriverByName("GTiff").Create(path, 30, 30, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000, 10, 0, 5200000, 0, -10))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(arr)
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds = None


def _dams():
    """Dams/2801 (dam.json, like the Absaraka copy) and the decoy Dams/9999 (rasters only),
    the layout of the GUI test."""
    dams = os.path.join(tempfile.mkdtemp(prefix="hz_dams_"), "Dams")
    for dam, rec in (("2801", True), ("9999", False)):
        d = os.path.join(dams, dam)
        os.makedirs(os.path.join(d, "outputs"))
        if rec:
            with open(os.path.join(d, "dam.json"), "w") as fh:
                json.dump({"fields": {"dam_id": dam, "name": "Absaraka"}}, fh)
        for i, name in enumerate(RASTERS):
            _raster(os.path.join(d, name), 1.0 + i)
    return dams


class _NoPopups(object):
    def __enter__(self):
        self.calls, self._old = [], {}
        for name in ("warning", "information", "critical"):
            self._old[name] = getattr(QMessageBox, name)
            setattr(QMessageBox, name, staticmethod(
                lambda *a, _n=name, **k: self.calls.append((_n, a[1:3] if len(a) > 2 else a))))
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


def _boxes(d):
    return [d.depth_edit.text(), d.vel_edit.text(), d.arr_edit.text()]


# ---------------------------------------------------------------- Qt-free rules

def test_normalize_and_root_refused():
    assert DF.normalize("//") == os.path.abspath("/")
    assert DF.normalize("/a//b///c/") == "/a/b/c"
    for bad in ("/", "//", "///", " / "):
        assert "root" in DF.dam_folder_problem(bad.strip()), bad


def test_dams_root_and_parent_refused_dam_folder_accepted():
    dams = _dams()
    for bad in (dams, dams + "/", dams + "//", os.path.dirname(dams)):
        p = DF.dam_folder_problem(bad, dams_roots=[dams])
        assert p, bad
    # without the suite setting: refused because it holds dam folders
    p = DF.dam_folder_problem(dams + "/")
    assert "2801" in p and "9999" in p, p
    for ok in ("2801", "9999", "2801/", "9999//"):
        assert DF.dam_folder_problem(os.path.join(dams, ok), dams_roots=[dams]) == "", ok
    assert "does not exist" in DF.dam_folder_problem(os.path.join(dams, "1234"))
    assert DF.dam_id(os.path.join(dams, "2801")) == "2801"


def test_raster_problems():
    dams = _dams()
    d28, d99 = os.path.join(dams, "2801"), os.path.join(dams, "9999")
    own = {"depth": os.path.join(d99, RASTERS[0])}
    assert DF.raster_problems(d99, own) == []
    other = {"depth": os.path.join(d28, RASTERS[0]), "vel": os.path.join(d99, RASTERS[1])}
    probs = DF.raster_problems(d99, other)
    assert len(probs) == 1 and "outside the dam folder" in probs[0], probs
    # a raster of another dam copied into this folder (other dam ID in the name)
    alien = os.path.join(d28, "DWR2900_Depth.tif")
    _raster(alien, 1.0)
    probs = DF.raster_problems(d28, {"depth": alien})
    assert probs and "2900" in probs[0], probs


def test_find_rasters_never_enters_other_dam_folders():
    dams = _dams()
    d28 = os.path.join(dams, "2801")
    # a sibling dam nested below 2801 (copied by mistake): skipped, not searched
    nested = os.path.join(d28, "DWR-1234")
    os.makedirs(nested)
    _raster(os.path.join(nested, "Depth (Max).tif"), 9.0)
    for name in RASTERS:
        os.remove(os.path.join(d28, name))
    found = find_rasters(d28)
    assert not found["depth"], found
    assert any("other dam folder" in n for n in found["notes"]), found["notes"]


# ---------------------------------------------------------------- the dialog (repro)

def test_h1_switch_2801_to_9999_replaces_rasters():
    dams = _dams()
    d = _make()
    try:
        d.dam_folder_edit.setText(os.path.join(dams, "2801"))
        _pump()
        assert all(p.startswith(os.path.join(dams, "2801") + os.sep) for p in _boxes(d)), _boxes(d)
        d.dam_folder_edit.setText(os.path.join(dams, "9999"))
        _pump()
        d.raster_group.setChecked(True)
        _pump()
        assert all(p.startswith(os.path.join(dams, "9999") + os.sep) for p in _boxes(d)), _boxes(d)
        assert d.output_folder_edit.text() == os.path.join(dams, "9999", "outputs")
        assert d._raster_ownership_problems() == []
        assert d.classify_btn.isEnabled()
    finally:
        _done(d)


def test_h1_typed_raster_outside_new_folder_cleared_with_notice():
    dams = _dams()
    d = _make()
    try:
        d.dam_folder_edit.setText(os.path.join(dams, "9999"))
        _pump()
        d.raster_group.setChecked(True)
        typed = os.path.join(dams, "9999", "Velocity (Max).tif")
        d.vel_edit.setText(typed)                 # typed by hand, inside 9999
        d.dam_folder_edit.setText(os.path.join(dams, "2801"))
        _pump()
        assert d.vel_edit.text().startswith(os.path.join(dams, "2801") + os.sep), d.vel_edit.text()
        assert "cleared" in d.status_label.text(), d.status_label.text()
    finally:
        _done(d)


def test_check_inputs_fails_and_classify_off_on_outside_raster():
    dams = _dams()
    d = _make()
    try:
        d.dam_folder_edit.setText(os.path.join(dams, "9999"))
        _pump()
        d.raster_group.setChecked(True)
        d.depth_edit.setText(os.path.join(dams, "2801", "Depth (Max).tif"))   # another dam's
        _pump()
        assert not d.classify_btn.isEnabled()
        assert "outside the dam folder" in d.classify_btn.toolTip()
        with _NoPopups() as np_:
            d._on_check_inputs()
        assert not np_.calls, np_.calls
        assert d.check_panel.blocked and not d.classify_btn.isEnabled()
        ok, errors = d.validate_all_inputs()
        assert not ok and any("outside the dam folder" in e for e in errors), errors
        # put the right raster back: Classify returns
        d.depth_edit.setText(os.path.join(dams, "9999", "Depth (Max).tif"))
        _pump()
        assert d._raster_ownership_problems() == []
    finally:
        _done(d)


def test_h2_dams_root_and_double_slash_refused():
    dams = _dams()
    d = _make()
    try:
        for bad in (dams, dams + "/", "//"):
            for e in (d.depth_edit, d.vel_edit, d.arr_edit, d.dv_edit):
                e.setText("")
            d.dam_folder_edit.setText("")
            _pump()
            d.dam_folder_edit.setText(bad)
            _pump()
            assert d.config_dam_folder == "", bad
            assert d.output_folder_edit.text() == "", bad
            assert _boxes(d) == ["", "", ""], (bad, _boxes(d))
            assert "refused" in d.status_label.text(), d.status_label.text()
            assert d.dam_folder_edit.text() == bad     # what was typed stays (no wipe)
            assert not d.classify_btn.isEnabled()
            assert d._raster_ownership_problems()[0].startswith("Dam folder refused")
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
