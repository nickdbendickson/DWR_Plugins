"""The dam wizard's list of the suite's DWR dams (moved from DamSafety Setup 1.0.13's dam
page): the county lists its dams with the number that match, a record without a height or
storage is marked and stops Next with the reason on the page, and a name search narrows it.

    QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_Runner/tests/suite_dams
"""
import os
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_CFG = tempfile.mkdtemp(prefix="runner_list_cfg_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = _CFG
os.environ.pop("NDSUITE_SETTINGS", None)

qgis_core = pytest.importorskip("qgis.core")
from qgis.PyQt.QtCore import QSettings  # noqa: E402

for _fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
    QSettings.setPath(_fmt, QSettings.Scope.UserScope, _CFG)

_app = qgis_core.QgsApplication.instance() or qgis_core.QgsApplication([], True)

from DamSafety_Runner import suite_dams as SD  # noqa: E402
from DamSafety_Runner import wizard as WZ  # noqa: E402

DAMS = [("Absaraka", "2801", "Cass", 18.0, 410.0, -97.4, 46.9),
        ("Blank Record", "3161", "Cass", None, None, -97.5, 46.8),
        ("Other", "4000", "Barnes", 12.0, 90.0, -98.0, 46.9)]


def _pump(n=4):
    for _ in range(n):
        _app.processEvents()


def _suite(dams):
    """dwr_dams.gpkg as suite_dams.dams() reads it."""
    folder = tempfile.mkdtemp(prefix="runner_list_suite_")
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
    return path + "|layername=dwr_dams"


@pytest.fixture
def dam(tmp_path):
    w = WZ._classes()["Wizard"](None)
    w.answers["dams_folder"] = str(tmp_path / "Dams")
    w.found = {"dwr": _suite(DAMS)}
    w.show()
    _pump()
    page = w.currentPage()
    page.county.setCurrentIndex(page.county.findData("Cass"))
    _pump()
    yield page
    w.hide()
    w.deleteLater()
    _pump()


def test_county_lists_its_dams_and_a_blank_record_stops_next(dam):
    assert "2 dams match (Cass)" in dam.note.text()
    names = [dam.feature.itemText(i) for i in range(dam.feature.count())]
    assert names == ["Absaraka (2801)", "Blank Record (3161)"]
    assert dam.validatePage()                          # Absaraka: 18 ft, 410 ac-ft
    dam.feature.setCurrentIndex(1)                     # no height / storage in the record
    _pump()
    assert dam.mark["HEIGHT_FT"].text() == "✕" and dam.mark["STORAGE_ACFT"].text() == "✕"
    assert not dam.validatePage()
    assert dam.problems.isVisible() and "Dam height" in dam.problems.text()
    dam.value_edit["HEIGHT_FT"].setText("9")
    dam.value_edit["STORAGE_ACFT"].setText("55")
    assert dam.validatePage() and not dam.problems.isVisible()
    dam.search.setText("zzz")
    dam.fill_features()
    assert dam.note.text().startswith("No dam matches") and dam.feature.count() == 0


def test_match_count_text():
    rows = [{"name": "a"}] * 400
    assert "only the first 400" in SD.match_count_text(rows, "Cass")
    assert SD.match_count_text([{"name": "a"}], "", "lake") == '1 dam matches ("lake")'
    assert SD.match_count_text([{"name": "x", "error": True}]).startswith("No dam matches")
