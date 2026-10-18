"""Setup 1.0.9: a dam's missing height and storage are filled from the National Inventory of
Dams (NID), never over a DWR value; the source shows next to the value ('from NID ...') and
goes to the Runner hand-off; the NID web service is asked only on the engineer's click (the
web calls are mocked here); a dam without a point takes its dam line's midpoint.

Real Qt, offscreen, in the QGIS python, with a throw-away QGIS settings folder:
    QT_QPA_PLATFORM=offscreen python -m pytest DamSafety_Setup/tests/test_nid_fill.py
"""
import json
import os
import sqlite3
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
_CFG = tempfile.mkdtemp(prefix="setup_nid_cfg_")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = _CFG          # never the user's QGIS settings
os.environ.pop("NDSUITE_SETTINGS", None)

qgis_core = pytest.importorskip("qgis.core")
from qgis.PyQt.QtCore import QSettings, QVariant  # noqa: E402
from qgis.PyQt.QtWidgets import QWizard  # noqa: E402

for _fmt in (QSettings.NativeFormat, QSettings.IniFormat):   # settings in _CFG only
    QSettings.setPath(_fmt, QSettings.UserScope, _CFG)

_app = qgis_core.QgsApplication.instance() or qgis_core.QgsApplication([], True)

from DamSafety_Setup import plugin as P  # noqa: E402
from DamSafety_Setup import nid_fill as N  # noqa: E402
from DamSafety_Setup import runner_params as RP  # noqa: E402
from DamSafety_Setup.damsuite import suite_config as SC  # noqa: E402

from qgis.core import (QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, QgsField,  # noqa
                       QgsVectorFileWriter, QgsCoordinateTransformContext,
                       QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject)


def _pump(n=4):
    for _ in range(n):
        _app.processEvents()


# DWR dams: name, structure id, NID id, county, height, max pool vol, normal vol, lon, lat
DWR = [
    ("Full Record", "100", "", "Cass", 18.0, 410.0, 300.0, -97.40, 46.90),
    ("By Id", "101", "ND00001", "Cass", 0.0, 0.0, 0.0, -97.50, 46.80),
    ("Near Only", "102", "", "Cass", 12.0, 0.0, 0.0, -97.60, 46.70),
    ("Too Far", "103", "", "Cass", 0.0, 0.0, 0.0, -97.70, 46.60),
    ("Normal Pool", "104", "ND00004", "Cass", 9.0, 0.0, 77.0, -97.80, 46.50),
    ("No Point", "105", "", "Cass", 5.0, 50.0, 0.0, 0.0, 0.0),
]
# NID dams: NIDID, NID_HEIGHT, DAM_HEIGHT, MAX_STORAGE, NID_STORAGE, NORMAL_STORAGE, lon, lat
NID = [
    ("ND00001", 25.0, 24.0, None, 900.0, 800.0, -97.50, 46.80),
    ("ND00002", 30.0, 30.0, 5000.0, 4000.0, 1000.0, -97.60 + 0.0006, 46.70),   # ~46 m east
    ("ND00003", 40.0, 40.0, 7000.0, 7000.0, 7000.0, -97.70 + 0.0030, 46.60),   # ~230 m east
    ("ND00004", 11.0, 11.0, 999.0, 999.0, 999.0, -97.80, 46.50),
    ("ND00100", 99.0, 99.0, 99999.0, 99999.0, 99999.0, -97.40, 46.90),         # on Full Record
]


def _dwr_gpkg(folder):
    path = os.path.join(folder, "dwr_dams.gpkg")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    con.execute("CREATE TABLE gpkg_geometry_columns (table_name TEXT, column_name TEXT, "
                "geometry_type_name TEXT)")
    con.execute("INSERT INTO gpkg_contents VALUES ('dwr_dams', 'features')")
    con.execute("INSERT INTO gpkg_geometry_columns VALUES ('dwr_dams', 'geom', 'POINT')")
    con.execute("CREATE TABLE dwr_dams (fid INTEGER PRIMARY KEY, geom BLOB, dam_name TEXT, "
                "structure_id TEXT, national_id TEXT, county TEXT, max_dam_height REAL, "
                "max_pool_vol REAL, normal_vol REAL, longitude REAL, latitude REAL)")
    con.executemany("INSERT INTO dwr_dams (dam_name, structure_id, national_id, county, "
                    "max_dam_height, max_pool_vol, normal_vol, longitude, latitude) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", DWR)
    con.commit()
    con.close()
    return path


def _write(layer, path):
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = os.path.splitext(os.path.basename(path))[0]
    err = QgsVectorFileWriter.writeAsVectorFormatV3(
        layer, path, QgsCoordinateTransformContext(), opts)
    assert err[0] == QgsVectorFileWriter.NoError, err


def _nid_gpkg(folder, with_lonlat=False):
    """A real GeoPackage like the pack's hydro/NID_dams.gpkg (point geometry; the
    LATITUDE / LONGITUDE columns only when asked, so the geometry blob is read)."""
    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "NID_dams", "memory")
    fields = [QgsField("NIDID", QVariant.String)] + [
        QgsField(n, QVariant.Double) for n in
        ("NID_HEIGHT", "DAM_HEIGHT", "MAX_STORAGE", "NID_STORAGE", "NORMAL_STORAGE")]
    if with_lonlat:
        fields += [QgsField("LONGITUDE", QVariant.Double), QgsField("LATITUDE", QVariant.Double)]
    lyr.dataProvider().addAttributes(fields)
    lyr.updateFields()
    feats = []
    for rec in NID:
        f = QgsFeature(lyr.fields())
        vals = list(rec[:6]) + ([rec[6], rec[7]] if with_lonlat else [])
        f.setAttributes(vals)
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(rec[6], rec[7])))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    os.makedirs(os.path.join(folder, "hydro"), exist_ok=True)
    path = os.path.join(folder, "hydro", "NID_dams.gpkg")
    _write(lyr, path)
    return path + "|layername=NID_dams"


def _lines_gpkg(folder):
    """dwr_dam_lines in Web Mercator, so the midpoint has to be transformed to EPSG:4326.
    Structure 105 (no point) runs from -97.90 to -97.88 at 46.40."""
    lyr = QgsVectorLayer("LineString?crs=EPSG:3857", "dwr_dam_lines", "memory")
    lyr.dataProvider().addAttributes([QgsField("structure_id", QVariant.String)])
    lyr.updateFields()
    xf = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                QgsCoordinateReferenceSystem("EPSG:3857"), QgsProject.instance())
    feats = []
    for sid, pts in (("105", [(-97.90, 46.40), (-97.88, 46.40)]),
                     ("100", [(-90.0, 40.0), (-90.1, 40.0)])):     # 100 keeps its own point
        f = QgsFeature(lyr.fields())
        f.setAttributes([sid])
        f.setGeometry(QgsGeometry.fromPolylineXY([xf.transform(QgsPointXY(*p)) for p in pts]))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    path = os.path.join(folder, "dwr_dam_lines.gpkg")
    _write(lyr, path)
    return path + "|layername=dwr_dam_lines"


@pytest.fixture
def suite():
    folder = tempfile.mkdtemp(prefix="setup_nid_suite_")
    dwr = _dwr_gpkg(folder)
    nid = _nid_gpkg(folder)
    lines = _lines_gpkg(folder)
    return {"folder": folder, "dwr": dwr, "nid": nid, "lines": lines}


def _rows(suite, nid=True):
    rows = P._dams(suite["dwr"], "", "Cass", lines_uri=suite["lines"])
    N.fill_rows(rows, suite["nid"] if nid else "")
    return {r["name"]: r for r in rows}


# ---- no Qt widgets

def test_number_treats_zero_and_text_as_blank():
    assert N.number(0) is None and N.number("") is None and N.number(None) is None
    assert N.number("abc") is None and N.number(-3) is None
    assert N.number("452") == 452.0 and N.number(1.5) == 1.5


def test_storage_prefers_max_then_nid_then_normal():
    assert N.nid_values({"MAX_STORAGE": 10, "NID_STORAGE": 20, "NORMAL_STORAGE": 30})[2:] == \
        (10.0, "MAX_STORAGE")
    assert N.nid_values({"MAX_STORAGE": None, "NID_STORAGE": 20, "NORMAL_STORAGE": 30})[2:] == \
        (20.0, "NID_STORAGE")
    assert N.nid_values({"MAX_STORAGE": 0, "NORMAL_STORAGE": 30})[2:] == (30.0, "NORMAL_STORAGE")
    # the web service's camelCase strings
    h, hf, s, sf = N.nid_values({"nidHeight": "18", "nidStorage": "452", "maxStorage": None})
    assert (h, hf, s, sf) == (18.0, "nidHeight", 452.0, "nidStorage")
    assert N.nid_values({"NID_HEIGHT": None, "DAM_HEIGHT": 7})[:2] == (7.0, "DAM_HEIGHT")


def test_layer_reads_the_point_geometry_without_lat_lon_columns(suite):
    recs = N.load_layer(suite["nid"])
    assert len(recs) == len(NID)
    one = [r for r in recs if r["NIDID"] == "ND00001"][0]
    assert abs(one["_lon"] + 97.5) < 1e-9 and abs(one["_lat"] - 46.8) < 1e-9


def test_layer_with_lat_lon_columns(tmp_path):
    uri = _nid_gpkg(str(tmp_path), with_lonlat=True)
    assert {r["NIDID"] for r in N.load_layer(uri)} == {r[0] for r in NID}


def test_match_by_id_then_nearest_within_100_m(suite):
    recs = N.load_layer(suite["nid"])
    rec, how = N.match(recs, "nd00001", (-90, 40))
    assert rec["NIDID"] == "ND00001" and how == "NID id"
    rec, how = N.match(recs, "", (-97.60, 46.70))
    assert rec["NIDID"] == "ND00002" and how.startswith("nearest NID dam, 4")
    assert N.match(recs, "", (-97.70, 46.60)) == (None, "")             # 230 m: too far
    assert N.match(recs, "ND99999", None) == (None, "")


def test_fill_only_blanks_never_over_dwr(suite):
    rows = _rows(suite)
    full = rows["Full Record"]                    # an NID dam sits on it: DWR still wins
    assert (full["height"], full["storage"]) == (18.0, 410.0)
    assert full["height_source"] == full["storage_source"] == "DWR"
    by_id = rows["By Id"]
    assert (by_id["height"], by_id["storage"]) == (25.0, 900.0)       # NID_HEIGHT, NID_STORAGE
    assert by_id["height_source"].startswith("NID ND00001 NID_HEIGHT (local NID layer, NID id")
    assert by_id["storage_source"].startswith("NID ND00001 NID_STORAGE")
    near = rows["Near Only"]
    assert near["height"] == 12.0 and near["height_source"] == "DWR"   # DWR height kept
    assert near["storage"] == 5000.0 and "MAX_STORAGE" in near["storage_source"]
    assert near["nid"] == "ND00002"                                    # the match is shown
    assert N.needs(rows["Too Far"]) and not rows["Too Far"].get("height_source")
    normal = rows["Normal Pool"]                  # DWR normal pool before the NID's 999
    assert normal["storage"] == 77.0 and normal["storage_source"].startswith("DWR normal pool")


def test_no_nid_layer_leaves_the_blanks(suite):
    rows = _rows(suite, nid=False)
    assert N.needs(rows["By Id"]) and N.needs(rows["Near Only"])
    assert N.fill_rows([{"height": None}], "/no/such.gpkg") == 0


def test_point_from_the_dam_line_midpoint_in_4326(suite):
    rows = _rows(suite)
    row = rows["No Point"]
    assert row["from_line"] is True and row["point"].endswith("[EPSG:4326]")
    lon, lat = N.parse_point(row["point"])
    assert abs(lon + 97.89) < 1e-5 and abs(lat - 46.40) < 1e-5
    assert rows["Full Record"]["point"] == "-97.4,46.9 [EPSG:4326]"   # its own point kept
    assert not rows["Full Record"].get("from_line")


def test_online_urls_and_answers():
    row = {"nid": "ND00182", "point": "-96.78555,46.66722 [EPSG:4326]"}
    urls = N.online_urls(row)
    assert urls[0] == ("id", "https://nid.sec.usace.army.mil/api/dams/ND00182/inventory")
    assert urls[1][0] == "near" and "nr=-96.785550,46.667220," in urls[1][1]
    assert N.online_urls({"point": "500000,5000000 [EPSG:26914]"}) == []    # not degrees
    inv = json.dumps({"nidId": "ND00182", "nidHeight": 18, "maxStorage": None,
                      "nidStorage": 452})
    work = {"nid": "ND00182", "height": 18.0, "storage": None, "point": row["point"]}
    done, msg = N.fill_online(work, [("id", inv)])
    assert done == ["storage"] and work["storage"] == 452.0 and work["height"] == 18.0
    assert "NID web service" in work["storage_source"] and "ND00182" in msg
    done, msg = N.fill_online({"height": None, "storage": None}, [("id", "{}")])
    assert done == [] and "not in the NID" in msg


# ---- the wizard

class FakeNet(object):
    """Stands in for nid_get: records the URLs, answers from a table (async like Qt)."""

    def __init__(self, answers=None, error=""):
        self.urls, self.answers, self.error = [], answers or {}, error

    def __call__(self, url, done):
        self.urls.append(url)
        for key, text in self.answers.items():
            if key in url:
                return done(text, "")
        return done("", self.error) if self.error else done("[]", "")


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
    for key in ("dams_folder", "data_folder"):
        qgis_core.QgsSettings().remove(SC.PREFIX + key)


def _dam_page(w, folder, net=None):
    page = w.page(P.FOLDER_PAGE)
    page.path.setText(folder)
    page._scan()
    _pump()
    assert w.found.get("nid"), w.found               # the folder scan finds the NID layer
    w.next()
    _pump()
    dam = w.page(P.DAM_PAGE)
    if net is not None:
        dam.fetch = net
    dam.county.setCurrentIndex(dam.county.findData("Cass"))
    _pump()
    return dam


def _pick(dam, name):
    for i in range(dam.combo.count()):
        if dam.combo.itemText(i).startswith(name + " ("):
            dam.combo.setCurrentIndex(i)
            _pump()
            return
    raise AssertionError(name)


def test_dam_page_shows_from_nid_and_hands_the_source_over(wiz, suite):
    net = FakeNet()
    dam = _dam_page(wiz, suite["folder"], net)
    _pick(dam, "By Id")
    assert dam.height.value() == 25.0 and dam.storage.value() == 900.0
    assert dam.height_src.text() == "from NID ND00001"
    assert dam.storage_src.text() == "from NID ND00001"
    assert dam.nid.text() == "ND00001"
    assert dam.isComplete() and wiz.button(QWizard.NextButton).isEnabled()
    assert not dam.nid_button.isEnabled()          # nothing blank: no web call to offer
    _pick(dam, "Full Record")
    assert dam.height_src.text() == "from DWR"
    _pick(dam, "Near Only")
    assert dam.height_src.text() == "from DWR" and dam.storage_src.text() == "from NID ND00002"
    dam.storage.setValue(4321)                      # the engineer's own number
    assert dam.storage_src.text() == "typed in"
    row = dam.selected()
    assert row["storage"] == 4321 and row["storage_source"] == "typed in"
    assert row["height_source"] == "DWR"
    assert net.urls == []                           # no silent network call


def test_answers_and_params_carry_the_source(wiz, suite):
    dam = _dam_page(wiz, suite["folder"], FakeNet())
    _pick(dam, "By Id")
    wiz.next()
    _pump()
    dem_page = wiz.page(P.DEM_PAGE)
    assert "from NID ND00001" in dem_page.note.text()
    dem = os.path.join(suite["folder"], "dem.tif")
    open(dem, "wb").close()
    dem_page.dem.setText(dem)
    wiz.next()
    _pump()
    run = wiz.page(P.RUN_PAGE)
    assert "storage 900 ac-ft (from NID ND00001)" in run.summary.text()
    assert run.validatePage()
    a = wiz.answers()
    assert a["height"] == 25.0 and a["height_source"].startswith("NID ND00001 NID_HEIGHT")
    p = RP.build_params(a)
    assert p["HEIGHT_FT"] == 25.0 and p["STORAGE_ACFT"] == 900.0
    assert p["STORAGE_SOURCE"].startswith("NID ND00001 NID_STORAGE")


def test_extra_source_keys_do_not_reach_runner_parameters():
    """The processing dialog sets only the parameters the algorithm defines."""
    p = RP.build_params({"height": 5, "storage": 6, "height_source": "DWR",
                         "storage_source": "typed in"})
    assert p["HEIGHT_SOURCE"] == "DWR" and p["STORAGE_SOURCE"] == "typed in"
    assert "HEIGHT_SOURCE" not in RP.build_params({"height": 5})


def test_online_button_fills_blanks_only_on_click(wiz, suite):
    inv = json.dumps({"nidId": "ND77777", "nidHeight": "14", "nidStorage": "321"})
    net = FakeNet({"query?nr=-97.700000,46.600000": json.dumps(
        [{"nidId": "ND77777", "nidHeight": "14", "nidStorage": "321",
          "longitude": "-97.7003", "latitude": "46.6"}]), "ND77777/inventory": inv})
    dam = _dam_page(wiz, suite["folder"], net)
    _pick(dam, "Too Far")                           # not in the local layer within 100 m
    assert not dam.isComplete() and dam.nid_button.isEnabled()
    dam.height.setValue(16)                         # typed before the click: kept
    assert net.urls == []
    dam.nid_button.click()
    _pump()
    assert len(net.urls) == 1 and "/query?nr=" in net.urls[0]     # no NID id: by point
    assert dam.height.value() == 16 and dam.height_src.text() == "typed in"
    assert dam.storage.value() == 321 and dam.storage_src.text() == "from NID ND77777"
    assert "Filled storage from NID ND77777 (online)" in dam.nid_status.text()
    assert dam.isComplete() and not dam.nid_button.isEnabled()
    assert dam.selected()["storage_source"].startswith("NID ND77777 nidStorage (NID web service")


def test_online_offline_or_unknown_says_type_it_in(wiz, suite):
    dam = _dam_page(wiz, suite["folder"], FakeNet(error="Host not found"))
    _pick(dam, "Too Far")
    dam.nid_button.click()
    _pump()
    assert "did not answer (Host not found)" in dam.nid_status.text()
    assert not dam.isComplete() and dam.nid_button.isEnabled()      # can try again
    dam.fetch = FakeNet()                                           # answers: no dam there
    dam.nid_button.click()
    _pump()
    assert "not in the NID" in dam.nid_status.text() and dam.storage.value() == 0


def test_large_nid_storage_is_not_clipped(wiz, suite):
    dam = _dam_page(wiz, suite["folder"], FakeNet())
    dam.storage.setValue(26000000)                   # Snake Creek / Lake Sakakawea
    assert dam.storage.value() == 26000000


def test_real_nid_get_uses_qgis_network_manager(monkeypatch):
    """nid_get goes through QgsNetworkAccessManager (QGIS proxy settings) and does not
    block: it returns the reply at once. No real request: the manager is replaced."""
    seen = {}

    class Reply(object):
        class Sig(object):
            def connect(self, fn):
                seen["slot"] = fn
        finished = Sig()

    class Manager(object):
        def get(self, req):
            seen["url"] = req.url().toString()
            seen["timeout"] = req.transferTimeout() if hasattr(req, "transferTimeout") else 0
            return Reply()
    monkeypatch.setattr(qgis_core.QgsNetworkAccessManager, "instance",
                        staticmethod(lambda: Manager()))
    reply = P.nid_get("https://nid.sec.usace.army.mil/api/dams/ND1/inventory", lambda *a: None)
    assert isinstance(reply, Reply) and "ND1/inventory" in seen["url"] and "slot" in seen
    assert seen["timeout"] in (0, N.TIMEOUT_S * 1000)
    P._REPLIES.discard(reply)
