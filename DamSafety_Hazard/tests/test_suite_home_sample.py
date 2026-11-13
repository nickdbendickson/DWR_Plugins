"""Core 1.2.0: "Try the sample dam" in the Dam suite home (DamSafety_Core/ui/suite_home.py),
with the real Qt of QGIS and the real Runner window.

The button puts the suite's sample dam (damsuite.sample_dam) in the Dams folder: from the
offline pack when the pack has it, else its terrain is downloaded (asked first, in the
background, the same button cancels). Then the Runner opens with the Dams folder, the dam, its
terrain file and, when the user picks the quick first try, a 30 m cell.

The elevation service is a double; nothing is downloaded.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_suite_home_sample.py
"""
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()
_app.setOrganizationName("DamSafetyTests")       # never the engineer's own QGIS settings
_app.setApplicationName("suite_home_sample")

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402

from DamSafety_Hazard import suite_home as SHW  # noqa: E402
from DamSafety_Core.damsuite import dam_record as DR  # noqa: E402
from DamSafety_Core.damsuite import sample_dam as SD  # noqa: E402

TINY = "T-SAMPLE"
SD.SAMPLES[TINY] = SD.Sample(
    dam_id=TINY, name="Tiny Test Dam",
    fields={"dam_id": TINY, "name": "Tiny Test Dam", "lon": -100.0, "lat": 47.0, "x": 1200.0,
            "y": 5700.0, "crs": "EPSG:5070", "height_ft": 20.0, "storage_acft": 100.0},
    box=(1000.0, 5000.0, 3500.0, 6300.0), z_range_m=(200.0, 500.0),
    about="a test dam on a test plain.", note="test sample")
SD.DEFAULT = TINY
SD.TILE_PX, SD.RETRY_WAIT_S, SD.MIN_DEM_BYTES = 100, 0.0, 1000
CALLS = []


def _fetch(url, timeout=0):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
    w, h = (int(v) for v in q["size"][0].split(","))
    CALLS.append(url)
    name = "/vsimem/home_sample_{}.tif".format(len(CALLS))
    ds = gdal.GetDriverByName("GTiff").Create(name, w, h, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(5070)
    ds.SetProjection(sr.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(np.full((h, w), 300.0, "float32"))
    ds = None
    f = gdal.VSIFOpenL(name, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    n = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    raw = bytes(gdal.VSIFReadL(1, n, f))
    gdal.VSIFCloseL(f)
    gdal.Unlink(name)
    return raw


SD._fetch = _fetch
TMP = tempfile.mkdtemp(prefix="home_sample_")
PACK = os.path.join(TMP, "data", "ND_Dam_Suite_Pack")
ROOTS = []
SD.pack_roots = lambda: list(ROOTS)


def _pump(n=6):
    for _ in range(n):
        _app.processEvents()


class _Plugin:
    """The Runner plugin as the home sees it: run_plugin() and the window."""

    def __init__(self, dialog):
        self.dialog, self.opened = dialog, 0

    def run_plugin(self):
        self.opened += 1


class _Window:
    """A Runner window double (the fields the hand-off fills); the real window is checked in
    test_the_real_runner_window_takes_the_terrain_and_the_cell."""

    def __init__(self):
        from qgis.PyQt.QtWidgets import QComboBox, QDoubleSpinBox, QLineEdit
        self.dams_edit, self.dam_edit = QLineEdit(), QLineEdit()
        self.dem_combo, self.grid_m = QComboBox(), QDoubleSpinBox()
        self.grid_m.setRange(0.5, 500.0)
        self.grid_m.setValue(10.0)
        self.dem_combo.addItem("(choose in the tool)", "")
        self.refreshed = 0

    def refresh(self):
        self.refreshed += 1


def _home(dams, answers):
    """The home window on a Dams folder; `answers` are the replies to its questions, in order."""
    h = SHW.SuiteHomeDialog(opened_from="ndhazard")
    h.dams_edit.setText(dams)
    h._save = lambda: None                        # nothing remembered between tests
    h.asked, h.told, h.plug = [], [], _Plugin(_Window())
    replies = list(answers)

    def ask(text, yes="Yes", no="No"):
        h.asked.append((text, yes, no))
        assert replies, "an unexpected question: " + text
        return replies.pop(0)
    h._ask = ask
    h._tell = h.told.append
    h._plugin_object = lambda key: h.plug if key == "ndbreach" else None
    h.refresh()
    return h


def _finish(h, timeout=60):
    """Let the background download end and the window notice."""
    job = h._sample_job
    assert job is not None and job.wait(timeout)
    out = h._sample_poll()
    _pump()
    return out


def _dams(name):
    d = os.path.join(TMP, name)
    os.makedirs(d)
    return d


def test_the_button_is_there_and_the_window_says_what_it_writes():
    h = _home(_dams("d0"), [])
    assert h.sample_btn.text() == "Try the sample dam…" and h.sample_btn.isEnabled()
    labels = [w.text() for w in h.findChildren(type(h.summary_label))]
    assert any("Try the sample dam" in t and "adds the sample dam" in t for t in labels), labels
    h.close()


def test_without_a_pack_sample_the_download_is_asked_first_and_a_no_writes_nothing():
    dams = _dams("d1")
    del CALLS[:]
    h = _home(dams, [False])
    assert h.try_sample() == ""
    text, yes, no = h.asked[0]
    assert "Tiny Test Dam" in text and "USGS" in text and "MB" in text and dams in text
    assert (yes, no) == ("Download", "Cancel")
    assert os.listdir(dams) == [] and not CALLS and h._sample_job is None
    assert h.plug.opened == 0
    h.close()


def test_the_download_runs_in_the_background_and_then_the_runner_opens_on_the_dam():
    dams = _dams("d2")
    del CALLS[:]
    h = _home(dams, [True, True])                 # download; quick first try
    assert h.try_sample() == "downloading"
    assert "cancel" in h.sample_btn.text() and h._sample_timer.isActive()
    assert _finish(h) == "opened"
    target = os.path.join(dams, TINY)
    dem = os.path.join(target, SD.dem_name())
    assert len(CALLS) == 6 and SD.installed(dams) == target
    assert h.sample_btn.text() == "Try the sample dam…" and h._sample_job is None
    assert h._sample_timer is None
    # the dam is in the table, selected
    assert [r["dam_id"] for r in h.rows] == [TINY]
    assert h.row_at(h.table.currentRow())["folder"] == target
    # the question before the Runner opens: what it is, that it is a sample, the quick try
    text, yes, no = h.asked[1]
    assert target in text and "not a hazard determination" in text and "downloaded" in text
    assert yes == "Quick first try: 30 m cell" and no == "Keep the Runner's cell"
    w = h.plug.dialog
    assert h.plug.opened == 1 and w.refreshed == 1
    assert (w.dams_edit.text(), w.dam_edit.text()) == (dams, TINY)
    assert w.dem_combo.currentData() == dem and w.grid_m.value() == 30.0
    assert len(h.told) == 1 and "set it back to 10 m" in h.told[0]
    # again: nothing is downloaded or copied; this time the cell is kept
    h2 = _home(dams, [False])
    assert h2.try_sample() == "opened" and len(CALLS) == 6
    assert "already in the Dams folder" in h2.asked[0][0]
    assert h2.plug.dialog.grid_m.value() == 10.0 and h2.told == []
    assert h2.plug.dialog.dem_combo.currentData() == dem
    h.close()
    h2.close()


def test_the_pack_sample_is_copied_and_the_pack_is_left_alone():
    del CALLS[:]
    src = SD.build(PACK, fetch=_fetch)
    made = len(CALLS)
    before = sorted(os.listdir(src))
    ROOTS[:] = [os.path.join(TMP, "nothing"), PACK]
    try:
        dams = _dams("d3")
        h = _home(dams, [False])                  # only the cell question: no download asked
        assert h.try_sample() == "opened" and len(CALLS) == made
        assert "Copied from the offline pack" in h.asked[0][0]
        target = os.path.join(dams, TINY)
        assert SD.installed(dams) == target and sorted(os.listdir(src)) == before
        assert DR.load(target).get("name") == "Tiny Test Dam"
        assert h.plug.dialog.dem_combo.currentData() == os.path.join(target, SD.dem_name())
        h.close()
    finally:
        ROOTS[:] = []


def test_a_click_while_downloading_cancels_and_the_next_click_goes_on():
    dams = _dams("d4")
    del CALLS[:]
    entered, release = threading.Event(), threading.Event()

    def slow(url, timeout=0):
        entered.set()
        release.wait(30)
        return _fetch(url)
    SD._fetch = slow
    try:
        h = _home(dams, [True])
        assert h.try_sample() == "downloading" and entered.wait(30)
        assert h.try_sample() == "cancelling" and not h.sample_btn.isEnabled()
        release.set()
        assert _finish(h) == ""
        assert h.sample_btn.isEnabled() and h.sample_btn.text() == "Try the sample dam…"
        assert len(h.told) == 1 and "cancelled" in h.told[0] and h.plug.opened == 0
        assert SD.installed(dams) == "" and h.rows == [], "a half-made dam is not listed"
    finally:
        SD._fetch = _fetch
    got = len(CALLS)
    assert got == 1, got
    h = _home(dams, [True, False])
    assert h.try_sample() == "downloading" and _finish(h) == "opened"
    assert len(CALLS) == 6, "the parts already downloaded are kept"
    h.close()


def test_a_failed_download_is_said_in_words_and_nothing_opens():
    dams = _dams("d5")

    def down(url, timeout=0):
        raise OSError("network is unreachable")
    SD._fetch = down
    try:
        h = _home(dams, [True])
        assert h.try_sample() == "downloading" and _finish(h) == ""
        assert len(h.told) == 1 and "network is unreachable" in h.told[0]
        assert "internet" in h.told[0] and h.plug.opened == 0
        assert any("Sample dam" in e for e in SHW.RECENT_ERRORS)
        h.close()
    finally:
        SD._fetch = _fetch


def test_a_dams_folder_in_the_pack_is_refused_and_no_folder_offers_the_default():
    inside = os.path.join(TMP, "ND_Dam_Suite_Pack", "Dams")
    h = _home(inside, [])
    assert h.try_sample() == "" and "offline pack" in h.told[0]
    assert not os.path.exists(inside)
    h.close()
    h = _home("", [False])
    assert h.try_sample() == ""
    text, yes, no = h.asked[0]
    assert "No Dams folder" in text and (yes, no) == ("Use this folder", "Cancel")
    assert h.dams_edit.text() == ""
    h.close()


def test_the_real_runner_window_takes_the_terrain_and_the_cell():
    from qgis.core import QgsProject
    from DamSafety_Runner import dialog as RD
    dams = _dams("d6")
    src = SD.build(os.path.join(TMP, "pack6"), fetch=_fetch)
    done = SD.install(src, dams)
    QgsProject.instance().removeEntry(RD.SCOPE, RD.ENTRY)
    w = RD.NDBreachRunnerDialog()
    w.show()
    _pump()
    try:
        row = {"folder": done["folder"], "dem": done["dem"], "grid_m": 30.0}
        assert SHW.point_window_at(w, "ndbreach", dams, row) == [
            "dams_folder", "dam_id", "dem", "grid_m"]
        _pump()
        s = w.settings()
        assert (s["dams_folder"], s["dam_id"]) == (dams, TINY)
        assert s["dem"] == done["dem"] and s["grid_m"] == 30.0
        # an ordinary row of the table (no terrain, no cell) leaves both as they are
        assert SHW.point_window_at(w, "ndbreach", dams, {"folder": done["folder"]}) == [
            "dams_folder", "dam_id"]
        _pump()
        assert w.settings()["dem"] == done["dem"] and w.settings()["grid_m"] == 30.0
    finally:
        w.close()
        w.deleteLater()
        _pump()


if __name__ == "__main__":
    failed = 0
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print("ok    " + name)
                except Exception:                                # noqa: BLE001
                    import traceback
                    failed += 1
                    print("FAIL  " + name)
                    traceback.print_exc()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("{} failed".format(failed))
    sys.stdout.flush()
    os._exit(1 if failed else 0)
