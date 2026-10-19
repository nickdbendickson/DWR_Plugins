"""Setup 1.0.8: the wizard's Next button follows the page (QWizardPage.isComplete +
completeChanged) and the reason it is off shows in red under the control; the folder scan
shows coloured marks, the dam page the number of matching dams, the DEM page where the
download is saved, and the Run page the values the Runner really gets.

Real Qt, offscreen, in the QGIS python (PyQt5), with a throw-away QGIS settings folder:
    QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_Setup/tests/test_wizard_live.py
"""
import os
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_CFG = tempfile.mkdtemp(prefix="setup_live_cfg_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = _CFG          # never the user's QGIS settings
os.environ.pop("NDSUITE_SETTINGS", None)

qgis_core = pytest.importorskip("qgis.core")
from qgis.PyQt.QtCore import QSettings  # noqa: E402
from qgis.PyQt.QtWidgets import QWizard  # noqa: E402

for _fmt in (QSettings.NativeFormat, QSettings.IniFormat):   # settings in _CFG only
    QSettings.setPath(_fmt, QSettings.UserScope, _CFG)

_app = qgis_core.QgsApplication.instance() or qgis_core.QgsApplication([], True)

from DamSafety_Setup import plugin as P  # noqa: E402
from DamSafety_Setup.damsuite import suite_config as SC  # noqa: E402


def _pump(n=4):
    for _ in range(n):
        _app.processEvents()


def _suite(dams):
    """A suite data folder with dwr_dams.gpkg (what suite_fill.scan and _dams read)."""
    folder = tempfile.mkdtemp(prefix="setup_live_suite_")
    path = os.path.join(folder, "dwr_dams.gpkg")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('dwr_dams', 'features')")
    con.execute("INSERT INTO gpkg_geometry_columns VALUES ('dwr_dams', 'geom', 'POINT')")
    con.execute("CREATE TABLE dwr_dams (fid INTEGER PRIMARY KEY, geom BLOB, dam_name TEXT, "
                "structure_id TEXT, county TEXT, max_dam_height REAL, max_pool_vol REAL, "
                "longitude REAL, latitude REAL)")
    con.executemany("INSERT INTO dwr_dams (dam_name, structure_id, county, max_dam_height, "
                    "max_pool_vol, longitude, latitude) VALUES (?,?,?,?,?,?,?)", dams)
    con.commit()
    con.close()
    return folder


DAMS = [("Absaraka", "2801", "Cass", 18.0, 410.0, -97.4, 46.9),
        ("Blank Record", "3161", "Cass", None, None, -97.5, 46.8),
        ("Other", "4000", "Barnes", 12.0, 90.0, -98.0, 46.9)]


@pytest.fixture
def wiz(tmp_path):
    SC.set("dams_folder", str(tmp_path / "Dams"))
    w = P.SetupWizard()
    w.show()
    _pump()
    yield w
    w.hide()
    w.deleteLater()
    _pump()
    for key in ("dams_folder", "data_folder"):     # no setting left for the other tests
        qgis_core.QgsSettings().remove(SC.PREFIX + key)


def _next_enabled(w):
    return w.button(QWizard.NextButton).isEnabled()


def _to_dam_page(w, county="Cass"):
    page = w.page(P.FOLDER_PAGE)
    page.path.setText(_suite(DAMS))
    page._scan()
    _pump()
    w.next()
    _pump()
    dam = w.page(P.DAM_PAGE)
    dam.county.setCurrentIndex(dam.county.findData(county))
    _pump()
    return dam


def test_folder_page_next_follows_the_scan(wiz):
    page = wiz.page(P.FOLDER_PAGE)
    assert not page.isComplete() and not _next_enabled(wiz)
    assert page.reason.isVisibleTo(page) and "Pick the suite data folder" in page.reason.text()
    page.path.setText("/no/such/folder")
    page._scan()
    assert not _next_enabled(wiz) and "not found" in page.reason.text()
    empty = os.path.join(tempfile.mkdtemp(), "parent", "empty")   # scan also reads the parent
    os.makedirs(empty)
    page.path.setText(empty)
    page._scan()
    assert not _next_enabled(wiz) and "No DWR dams" in page.reason.text()
    assert "\u25cb" in page.note.text() and P.RED in page.note.text()       # red circle: DWR
    suite = _suite(DAMS)
    page.path.setText(suite)
    assert not _next_enabled(wiz)                     # typed, not scanned yet
    page._timer.timeout.emit()                        # the debounce fires
    _pump()
    assert page.isComplete() and _next_enabled(wiz) and not page.reason.isVisibleTo(page)
    assert "\u2713" in page.note.text() and P.GREEN in page.note.text()
    assert SC.get("data_folder") == suite             # saved only once the dams are found


def test_folder_scan_marks():
    txt = P.scan_marks_html({"dwr": "x", "roads": "y"})
    assert txt.count("\u2713") == 2 and txt.count("\u25cb") == 3
    assert P.RED not in txt                           # DWR found: no red mark
    assert P.RED in P.scan_marks_html({})


def test_dam_page_blocks_zero_height_and_storage(wiz):
    dam = _to_dam_page(wiz)
    assert "2 dams match (Cass)" in dam.count.text()
    names = [dam.combo.itemText(i) for i in range(dam.combo.count())]
    assert names == ["Absaraka (Cass)", "Blank Record (Cass)"]
    assert dam.isComplete() and _next_enabled(wiz)    # Absaraka: 18 ft, 410 ac-ft
    dam.combo.setCurrentIndex(1)                      # no height / storage in the record
    _pump()
    assert not dam.isComplete() and not _next_enabled(wiz)
    assert "Height is 0 ft" in dam.reason.text() and dam.reason.isVisibleTo(dam)
    dam.height.setValue(9)
    assert not _next_enabled(wiz) and "storage" in dam.reason.text()
    dam.storage.setValue(55)
    assert _next_enabled(wiz) and not dam.reason.isVisibleTo(dam)
    dam.search.setText("zzz")
    dam._find()
    assert dam.count.text().startswith("No dam matches") and not _next_enabled(wiz)
    assert "Pick a dam" in dam.reason.text()


def test_match_count_text():
    rows = [{"name": "a"}] * 400
    assert "only the first 400" in P.match_count_text(rows, "Cass")
    assert P.match_count_text([{"name": "a"}], "", "lake") == '1 dam matches ("lake")'
    assert P.match_count_text([{"name": "x", "error": True}]).startswith("No dam matches")


def test_dem_page_next_and_save_path(wiz, tmp_path):
    _to_dam_page(wiz)
    wiz.next()
    _pump()
    dem = wiz.page(P.DEM_PAGE)
    assert wiz.currentId() == P.DEM_PAGE
    assert not _next_enabled(wiz) and "Choose the DEM" in dem.reason.text()
    folder = os.path.join(str(tmp_path / "Dams"), "terrain")
    assert dem.save_to.text() == "Saves to {}\nas 2801_10m_5070.tif".format(folder)
    dem.res.setCurrentIndex(1)
    assert dem.save_to.text().endswith("2801_1m_5070.tif")
    dem.dem.setText(str(tmp_path))
    assert not _next_enabled(wiz) and "a folder" in dem.reason.text()
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"x")
    dem.dem.setText(str(tif))
    assert _next_enabled(wiz) and not dem.reason.isVisibleTo(dem)


def test_dem_target_names():
    assert P.dem_target("/d", "DWR-2801", "", 0) == ("/d", "DWR-2801_10m_5070.tif")
    assert P.dem_target("/d", "", "-97.4,46.9 [EPSG:4326]", 1) == ("/d", "dam_-97.4_46.9_1m_5070.tif")
    assert P.dem_target("/d", "", "", 0)[1] == "(pick a dam first)"


def test_run_page_summary_states_the_values_used(wiz, tmp_path):
    """Finding: the Advanced values were read whether or not the box is ticked (it only
    greys them). Kept that way (the Runner's Advanced section also only folds the values
    away); the summary now states them instead of 'unless Advanced is opened'."""
    _to_dam_page(wiz)
    wiz.next()
    _pump()
    tif = tmp_path / "dem.tif"
    tif.write_bytes(b"x")
    wiz.page(P.DEM_PAGE).dem.setText(str(tif))
    wiz.next()
    _pump()
    run = wiz.page(P.RUN_PAGE)
    assert wiz.currentId() == P.RUN_PAGE
    txt = run.summary.text()
    assert txt.startswith("Absaraka\n") and "Built-in engine, 10 m grid, 24 h run, no EAP report" in txt
    assert "tick Advanced settings to change them" in txt
    run.advanced.setChecked(True)
    run.cell.setValue(30)
    run.eap.setChecked(True)
    run.advanced.setChecked(False)                    # unticked: the values stay
    txt = run.summary.text()
    assert "30 m grid" in txt and "EAP report" in txt and "no EAP" not in txt
    assert run.validatePage()
    params = wiz.params()
    assert params["CELL"] == 30 and params["PRODUCTS"] is True
    assert params["HEIGHT_FT"] == 18.0 and params["STORAGE_ACFT"] == 410.0
    assert params["DAM_ID"] == "2801"


def test_selected_dam_is_shared(wiz):
    dam = _to_dam_page(wiz)
    dam.height.setValue(20)
    assert wiz.selected_dam()["height"] == 20 and wiz.selected_dam()["id"] == "2801"
    dam.combo.clear()
    assert wiz.selected_dam() == {}
