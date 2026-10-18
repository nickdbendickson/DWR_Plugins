"""Setup 1.0.10: hide DWR records that are not a dam to model ("Show all records" brings
them back), and 'Estimate from DEM' for a blank height / storage, on a synthetic DEM with a
known reservoir volume and dam height.

    QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_Setup/tests/test_dem_estimate.py
"""
import os
import sqlite3
import sys
import tempfile
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_CFG = tempfile.mkdtemp(prefix="setup_est_cfg_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = _CFG
os.environ.pop("NDSUITE_SETTINGS", None)

qgis_core = pytest.importorskip("qgis.core")
np = pytest.importorskip("numpy")
from osgeo import gdal, ogr, osr  # noqa: E402
from qgis.PyQt.QtCore import QSettings  # noqa: E402

for _fmt in (QSettings.NativeFormat, QSettings.IniFormat):
    QSettings.setPath(_fmt, QSettings.UserScope, _CFG)

_app = qgis_core.QgsApplication.instance() or qgis_core.QgsApplication([], True)

from DamSafety_Setup import plugin as P  # noqa: E402
from DamSafety_Setup import dem_estimate as DE  # noqa: E402
from DamSafety_Setup import dam_filter as DF  # noqa: E402
from DamSafety_Setup import runner_params as RP  # noqa: E402
from DamSafety_Setup.damsuite import suite_config as SC  # noqa: E402

# Synthetic site in EPSG:5070 near Fargo. A plain at 110 m; the dam crest along y = Y0 from
# X0 - 100 to X0 + 100 (on the plain, 110 m); a reservoir pit at 102 m north of the dam,
# 200 m x 200 m; the valley floor south of the dam at 100 m.
X0, Y0 = -60000.0, 2632000.0
CELL = 5.0
CREST, POOL, TOE = 110.0, 102.0, 100.0
VOL_M3 = (CREST - POOL) * 200.0 * 200.0               # 320,000 m3
VOL_ACFT = VOL_M3 / 1233.48183754752                 # 259.4 ac-ft
HEIGHT_FT = (CREST - TOE) / 0.3048                   # 32.8 ft


def _wgs(x, y):
    a = osr.SpatialReference()
    a.ImportFromEPSG(5070)
    b = osr.SpatialReference()
    b.ImportFromEPSG(4326)
    for s in (a, b):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(a, b).TransformPoint(x, y)[:2]


def _dem(folder, leak=False, unit_ft=False, name="bowl.tif"):
    n = 800                                            # 4 km a side
    x0, y0 = X0 - n * CELL / 2, Y0 + n * CELL / 2
    X = x0 + (np.arange(n) + 0.5) * CELL
    Y = y0 - (np.arange(n) + 0.5) * CELL
    GX, GY = np.meshgrid(X, Y)
    z = np.full((n, n), CREST)
    z[(GY < Y0 - 10) & (np.abs(GX - X0) < 300)] = TOE                 # valley below the dam
    z[(GY > Y0 + 10) & (GY < Y0 + 210) & (np.abs(GX - X0) < 100)] = POOL
    if leak:                                           # a channel from the pit to the edge
        z[(GY > Y0 + 10) & (np.abs(GX - X0) < 20)] = POOL
    if unit_ft:
        z = z / (1200.0 / 3937.0)
    path = os.path.join(folder, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, CELL, 0, y0, 0, -CELL))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.WriteArray(z.astype(np.float32))
    band.SetNoDataValue(-9999)
    if unit_ft:
        band.SetUnitType("US survey foot")
    ds = None
    return path


LINE = [_wgs(X0 - 100, Y0), _wgs(X0 + 100, Y0)]
POINT = _wgs(X0, Y0)


def test_bowl_height_and_storage():
    d = tempfile.mkdtemp()
    res = DE.estimate(_dem(d), POINT, LINE)
    assert abs(res["crest_ft"] - CREST / 0.3048) < 0.05
    assert abs(res["height_ft"] - HEIGHT_FT) < 0.05
    assert res["closed"] and res["radius_m"] == 1500.0
    assert abs(res["storage_acft"] - VOL_ACFT) / VOL_ACFT < 0.01, res["storage_acft"]
    assert abs(res["area_ac"] - 40000 / 4046.8564224) < 0.2
    assert "metres" in res["notes"][0]


def test_us_survey_feet_dem_gives_the_same_answer():
    d = tempfile.mkdtemp()
    res = DE.estimate(_dem(d, unit_ft=True), POINT, LINE, z_choice="meters")   # raster wins
    assert "US survey feet" in res["notes"][0]
    assert abs(res["height_ft"] - HEIGHT_FT) < 0.05
    assert abs(res["storage_acft"] - VOL_ACFT) / VOL_ACFT < 0.01


def test_page_unit_choice_applies_when_the_raster_has_none():
    d = tempfile.mkdtemp()
    res = DE.estimate(_dem(d), POINT, LINE, z_choice="feet")    # values read as feet
    assert abs(res["height_ft"] - (CREST - TOE)) < 0.05


def test_leaking_basin_gives_no_storage():
    d = tempfile.mkdtemp()
    res = DE.estimate(_dem(d, leak=True), POINT, LINE, radii=(500.0, 1000.0))
    assert res["storage_acft"] is None and not res["closed"]
    assert any("no closed basin" in n for n in res["notes"])
    assert abs(res["height_ft"] - HEIGHT_FT) < 0.05            # the height still stands


def test_point_only_estimate():
    d = tempfile.mkdtemp()
    res = DE.estimate(_dem(d), POINT, None)
    assert abs(res["height_ft"] - HEIGHT_FT) < 0.05
    assert abs(res["storage_acft"] - VOL_ACFT) / VOL_ACFT < 0.05
    assert any("No dam line" in n for n in res["notes"])


def test_outside_dem_is_an_error():
    d = tempfile.mkdtemp()
    with pytest.raises(DE.EstimateError):
        DE.estimate(_dem(d), (-100.5, 48.0), None)


def test_flood_fill_without_scipy_matches(monkeypatch):
    mask = np.zeros((6, 6), bool)
    mask[1:4, 1:4] = True
    mask[5, 5] = True
    import builtins
    real = builtins.__import__

    def no_scipy(name, *a, **k):
        if name.startswith("scipy"):
            raise ImportError(name)
        return real(name, *a, **k)
    fast = DE._flood_fill(mask, (2, 2))
    monkeypatch.setattr(builtins, "__import__", no_scipy)
    slow = DE._flood_fill(mask, (2, 2))
    assert (fast == slow).all() and fast.sum() == 9


# ---- not-a-dam filter

def test_filter_patterns():
    assert DF.reason("Breached", "") == "status Breached"
    assert DF.reason("Not Built", "VOID SEE #80") == "status Not Built"
    assert DF.reason("", "VOID SEE #80").startswith('remarks start with VOID: "VOID SEE #80"')
    assert DF.reason("Location Incon", "From WPA Files - No Dam Visible on aerial photos") \
        .startswith("remarks: no dam visible")
    assert DF.reason("Inconclusive", "dam does not exist anymore").startswith("remarks: does")
    # an Intact status wins over an old remark; a voided permit is not a void record
    assert DF.reason("Intact", "No dam visible in 1962 aerial") == ""
    assert DF.reason("", "Water Usage Permit voided - Incomplete Application.") == ""
    assert DF.reason("", "Permit is void because ...") == ""
    assert DF.reason("Intact", "") == "" and DF.reason(None, None) == ""


# ---- the wizard

DAMS = [  # name, sid, county, height, max pool vol, lon, lat, status, remarks
    ("Blank Dam", "500", "Cass", 0.0, 0.0, POINT[0], POINT[1], "Intact", ""),
    ("Gone Dam", "501", "Cass", 9.0, 50.0, POINT[0] + 0.1, POINT[1], "Breached", ""),
    ("Void Dam", "502", "Cass", 0.0, 0.0, POINT[0] + 0.2, POINT[1], "", "VOID SEE #80"),
    ("Kept Dam", "503", "Cass", 12.0, 90.0, POINT[0] + 0.3, POINT[1], "Intact",
     "no dam visible in 1962 aerial"),
]


def _suite():
    folder = tempfile.mkdtemp(prefix="setup_est_suite_")
    con = sqlite3.connect(os.path.join(folder, "dwr_dams.gpkg"))
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('dwr_dams', 'features')")
    con.execute("INSERT INTO gpkg_geometry_columns VALUES ('dwr_dams', 'geom', 'POINT')")
    con.execute("CREATE TABLE dwr_dams (fid INTEGER PRIMARY KEY, geom BLOB, dam_name TEXT, "
                "structure_id TEXT, county TEXT, max_dam_height REAL, max_pool_vol REAL, "
                "longitude REAL, latitude REAL, status TEXT, remarks TEXT)")
    con.executemany("INSERT INTO dwr_dams (dam_name, structure_id, county, max_dam_height, "
                    "max_pool_vol, longitude, latitude, status, remarks) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", DAMS)
    con.commit()
    con.close()
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.CreateDataSource(os.path.join(folder, "dwr_dam_lines.gpkg"))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    lyr = ds.CreateLayer("dwr_dam_lines", srs, ogr.wkbLineString)
    lyr.CreateField(ogr.FieldDefn("structure_id", ogr.OFTString))
    f = ogr.Feature(lyr.GetLayerDefn())
    f.SetField("structure_id", "500")
    g = ogr.Geometry(ogr.wkbLineString)
    for lon, lat in LINE:
        g.AddPoint_2D(lon, lat)
    f.SetGeometry(g)
    lyr.CreateFeature(f)
    ds = None
    return folder


@pytest.fixture
def wiz(tmp_path):
    SC.set("dams_folder", str(tmp_path / "Dams"))
    w = P.SetupWizard()
    w.show()
    _app.processEvents()
    yield w
    w.hide()
    w.deleteLater()
    _app.processEvents()
    for key in ("dams_folder", "data_folder"):
        qgis_core.QgsSettings().remove(SC.PREFIX + key)


def _dam_page(w, folder):
    page = w.page(P.FOLDER_PAGE)
    page.path.setText(folder)
    page._scan()
    w.next()
    _app.processEvents()
    dam = w.page(P.DAM_PAGE)
    dam.county.setCurrentIndex(dam.county.findData("Cass"))
    _app.processEvents()
    return dam


def _names(dam):
    return [dam.combo.itemText(i) for i in range(dam.combo.count())]


def test_not_a_dam_records_hidden_until_show_all(wiz):
    dam = _dam_page(wiz, _suite())
    assert _names(dam) == ["Blank Dam (Cass)", "Kept Dam (Cass)"]
    assert "2 records hidden" in dam.count.text()
    assert not dam.show_all.isChecked()
    dam.show_all.setChecked(True)
    _app.processEvents()
    names = _names(dam)
    assert len(names) == 4 and "Gone Dam (Cass) \u2013 not a dam? status Breached" in names
    assert "Void Dam (Cass) \u2013 not a dam? remarks start with VOID" in names
    dam.combo.setCurrentIndex(names.index("Void Dam (Cass) \u2013 not a dam? remarks start "
                                          "with VOID"))
    _app.processEvents()
    assert 'Not a dam to model? remarks start with VOID: "VOID SEE #80"' in dam.note.text()
    assert "hidden" not in dam.count.text()


def _sync(work, done, progress):
    """run_task stand-in: runs at once, records the progress."""
    _sync.pcts = []

    def prog(p):
        _sync.pcts.append(p)
        progress(p)
    try:
        res = work(prog)
    except DE.EstimateError as e:
        return done(None, str(e))
    return done(res, "")


def test_estimate_fills_blanks_labels_and_hands_over(wiz):
    folder = _suite()
    dem = _dem(folder)
    dam = _dam_page(wiz, folder)
    dam.run_task = _sync
    assert dam.combo.currentText() == "Blank Dam (Cass)"
    assert dam.est_button.isEnabled() and dam.est_button.text().startswith("Choose DEM")
    wiz.page(P.DEM_PAGE).dem.setText(dem)           # a DEM is known: plain button
    dam._changed()
    assert dam.est_button.text() == "Estimate from DEM"
    dam.est_button.click()
    _app.processEvents()
    assert _sync.pcts and _sync.pcts[-1] == 100
    assert abs(dam.height.value() - round(HEIGHT_FT, 1)) < 0.05
    assert abs(dam.storage.value() - round(VOL_ACFT, 1)) < 0.05 * VOL_ACFT
    assert dam.height_src.text() == "estimated from DEM (screening only)"
    assert dam.storage_src.text() == "estimated from DEM (screening only)"
    assert "Estimated height and storage from the DEM (screening only)" in dam.est_status.text()
    assert dam.isComplete() and not dam.est_button.isEnabled()
    row = dam.selected()
    assert row["height_source"].startswith("estimated from DEM (screening only): crest")
    p = RP.build_params({"height": row["height"], "storage": row["storage"],
                         "height_source": row["height_source"],
                         "storage_source": row["storage_source"]})
    assert p["STORAGE_SOURCE"].startswith("estimated from DEM (screening only)")


def test_typed_value_beats_the_estimate(wiz):
    folder = _suite()
    dem = _dem(folder)
    dam = _dam_page(wiz, folder)
    dam.run_task = _sync
    wiz.page(P.DEM_PAGE).dem.setText(dem)
    dam.height.setValue(21)                         # typed before the estimate
    dam.est_button.click()
    _app.processEvents()
    assert dam.height.value() == 21 and dam.height_src.text() == "typed in"
    assert dam.storage_src.text() == "estimated from DEM (screening only)"
    dam.storage.setValue(300)                       # typed after: wins, says so
    assert dam.storage_src.text() == "typed in"
    assert dam.selected()["storage_source"] == "typed in"


def test_estimate_error_is_shown(wiz):
    folder = _suite()
    dam = _dam_page(wiz, folder)
    dam.run_task = _sync
    far = os.path.join(folder, "far.tif")
    ds = gdal.GetDriverByName("GTiff").Create(far, 10, 10, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((0, 10, 0, 0, 0, -10))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds = None
    wiz.page(P.DEM_PAGE).dem.setText(far)
    dam.est_button.click()
    _app.processEvents()
    assert "No estimate: The dam is outside the DEM." in dam.est_status.text()
    assert dam.height.value() == 0 and dam.est_button.isEnabled()


def test_estimate_runs_in_a_qgis_task(wiz):
    """The real run_task: a QgsTask off the window's thread, progress shown, result back."""
    folder = _suite()
    dem = _dem(folder)
    dam = _dam_page(wiz, folder)
    wiz.page(P.DEM_PAGE).dem.setText(dem)
    seen = []
    dam.est_status.setText("")
    real = P.run_task

    def spy(work, done, progress):
        return real(work, done, lambda p: (seen.append(p), progress(p)))
    dam.run_task = spy
    dam.est_button.click()
    assert dam._est_busy and "Estimating from bowl.tif" in dam.est_status.text()
    t = time.time()
    while dam._est_busy and time.time() - t < 30:
        _app.processEvents()
        time.sleep(0.01)
    assert not dam._est_busy
    assert abs(dam.storage.value() - round(VOL_ACFT, 1)) < 0.05 * VOL_ACFT
    assert dam.storage_src.text() == "estimated from DEM (screening only)"
    assert seen                                      # progress came through
