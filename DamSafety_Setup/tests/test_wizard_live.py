"""Setup's one page (1.0.14): Continue… follows the page (QWizardPage.isComplete +
completeChanged), the reason it is off shows in red under the control, the folder scan shows
coloured marks, a missing DamSafety Runner keeps Continue… off with a red line, and Continue…
hands the scan to the Runner's dam wizard. The dam list itself is tested with the Runner's
wizard (DamSafety_Runner/tests/suite_dams).

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
    w = P.SetupWizard(runner=object())          # DamSafety Runner installed
    w.show()
    _pump()
    yield w
    w.hide()
    w.deleteLater()
    _pump()
    for key in ("dams_folder", "data_folder"):     # no setting left for the other tests
        qgis_core.QgsSettings().remove(SC.PREFIX + key)


def _continue_enabled(w):
    return w.button(QWizard.FinishButton).isEnabled()


def test_folder_page_next_follows_the_scan(wiz):
    _next_enabled = _continue_enabled                 # one page: Continue… is the Finish button
    assert wiz.buttonText(QWizard.FinishButton) == "Continue\u2026"
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


def test_no_runner_keeps_continue_off_and_says_why(tmp_path):
    w = P.SetupWizard(runner=None)
    w.show()
    _pump()
    try:
        page = w.page(P.FOLDER_PAGE)
        page.path.setText(_suite(DAMS))
        page._scan()
        _pump()
        assert not page.problem()                     # the folder is fine ...
        assert not page.isComplete() and not _continue_enabled(w)    # ... Runner is missing
        assert page.runner_line.isVisibleTo(page) and page.runner_line.text() == P.NO_RUNNER
        assert "Start here\u2026 again" in P.NO_RUNNER
        assert not page.next_banner.isVisibleTo(page)
    finally:
        w.hide()
        w.deleteLater()
        _pump()
        qgis_core.QgsSettings().remove(SC.PREFIX + "data_folder")


def test_continue_hands_the_scan_to_the_runner_wizard(monkeypatch):
    calls = []

    class Runner(object):
        @staticmethod
        def run_wizard(iface, found=None, data_folder=""):
            calls.append((found, data_folder))

    class Wiz(object):
        found = {"dwr": "dams.gpkg|layername=dwr_dams"}

        def __init__(self, parent=None, runner=None):
            assert runner is Runner
            calls.append("shown")

        def exec_(self):
            return 1

        def data_folder(self):
            return "/suite"

    class Iface(object):
        def mainWindow(self):
            return None

    monkeypatch.setattr(P, "runner_wizard_module", lambda: Runner)
    monkeypatch.setattr(P, "SetupWizard", Wiz)
    P.SetupPlugin(Iface()).run()
    assert calls == ["shown", ({"dwr": "dams.gpkg|layername=dwr_dams"}, "/suite")]


def test_folder_scan_marks():
    txt = P.scan_marks_html({"dwr": "x", "roads": "y"})
    assert txt.count("\u2713") == 2 and txt.count("\u25cb") == 3
    assert P.RED not in txt                           # DWR found: no red mark
    assert P.RED in P.scan_marks_html({})
