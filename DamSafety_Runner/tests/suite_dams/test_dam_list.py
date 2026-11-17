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


def test_dwr_record_fills_the_embankment_but_not_its_elevations(tmp_path):
    """3.20.8 (Nick, 2026-11-16): a DWR record's crest width, slopes and crest length fill the
    page; its elevations (NGVD 29 or a local datum) never do, so the top of dam and the breach
    bottom stay blank and come from the DEM. A text such as "2.5;3" (two slopes) is left blank."""
    folder = tmp_path / "suite"
    folder.mkdir()
    path = str(folder / "dwr_dams.gpkg")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('dwr_dams', 'features')")
    con.execute("INSERT INTO gpkg_geometry_columns VALUES ('dwr_dams', 'geom', 'POINT')")
    con.execute("CREATE TABLE dwr_dams (fid INTEGER PRIMARY KEY, geom BLOB, dam_name TEXT, "
                "structure_id TEXT, county TEXT, max_dam_height REAL, max_pool_vol REAL, "
                "max_pool_area REAL, max_pool_elev REAL, streambed_elev REAL, top_width REAL, "
                "upstream_slope TEXT, downstream_slope TEXT, dam_length REAL, "
                "vertical_datum TEXT, longitude REAL, latitude REAL)")
    con.executemany(
        "INSERT INTO dwr_dams (dam_name, structure_id, county, max_dam_height, max_pool_vol, "
        "max_pool_area, max_pool_elev, streambed_elev, top_width, upstream_slope, "
        "downstream_slope, dam_length, vertical_datum, longitude, latitude) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("Swan Buffalo Detention Dam 12", "2801", "Cass", 27.5, 1615.5, 154.5, 1070.0, 1042.5,
          12.0, "3.5", "2.5", 1700.0, "NGVD 29", -97.39113, 46.98337),
         ("Two Slopes", "2802", "Cass", 20.0, 100.0, 10.0, 130.0, 100.0, 0.0, "2.5;3", "",
          0.0, "Local", -97.4, 46.9)])
    con.commit()
    con.close()
    rows = {r["id"]: r for r in SD.dams(path + "|layername=dwr_dams", county="Cass")}
    r = rows["2801"]
    assert (r["crest_width"], r["us_slope"], r["ds_slope"], r["crest_length"]) == \
        (12.0, 3.5, 2.5, 1700.0)
    r = rows["2802"]
    assert (r["crest_width"], r["us_slope"], r["ds_slope"], r["crest_length"]) == \
        (None, None, None, None)

    w = WZ._classes()["Wizard"](None)
    w.answers["dams_folder"] = str(tmp_path / "Dams")
    w.found = {"dwr": path + "|layername=dwr_dams"}
    w.show()
    _pump()
    try:
        page = w.currentPage()
        page.county.setCurrentIndex(page.county.findData("Cass"))
        _pump()
        page.feature.setCurrentIndex([page.feature.itemText(i) for i in
                                      range(page.feature.count())].index(
                                          "Swan Buffalo Detention Dam 12 (2801)"))
        _pump()
        got = dict((p, page.value_edit[p].text()) for p in page.value_edit)
        assert (got["CREST_WIDTH"], got["US_SLOPE"], got["DS_SLOPE"], got["CREST_LENGTH"]) == \
            ("12", "3.5", "2.5", "1700")
        assert (got["HEIGHT_FT"], got["STORAGE_ACFT"], got["AREA_AC"]) == ("27.5", "1615.5", "154.5")
        assert got["TOP_FT"] == "" and got["BOTTOM_FT"] == "" and got["PMF_FT"] == ""
        assert page.mark["CREST_WIDTH"].text() == page.mark["HEIGHT_FT"].text()   # both "ok"
        assert page.validatePage()
        # another dam: nothing of the first dam's embankment is left behind
        page.feature.setCurrentIndex([page.feature.itemText(i) for i in
                                      range(page.feature.count())].index("Two Slopes (2802)"))
        _pump()
        assert page.value_edit["CREST_WIDTH"].text() == "" and page.value_edit["US_SLOPE"].text() == ""
    finally:
        w.hide()
        w.deleteLater()
        _pump()
