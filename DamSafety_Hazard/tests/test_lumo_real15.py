"""Lumo vReal.15 DF.1/DF.2/DF.5, WE.1/WE.3 (Hazard 2.18.26).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real15.py
"""
import builtins
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import datafolder as DF  # noqa: E402
from DamSafety_Hazard import webexport as WE  # noqa: E402


def test_df1_borrow_degrades():
    assert DF.IMPORT_PROBLEMS == [], DF.IMPORT_PROBLEMS        # all names exist today
    n = len(DF.IMPORT_PROBLEMS)
    v = DF._borrow("linear", "_NO_SUCH_NAME_XYZ", ("fallback",))
    assert v == ("fallback",) and len(DF.IMPORT_PROBLEMS) == n + 1
    assert "detection degraded" in DF.IMPORT_PROBLEMS[-1]
    del DF.IMPORT_PROBLEMS[n:]


def test_df2_find_data_without_classify_tab():
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "rail_lines.csv"), "w") as fh:
        fh.write("lon,lat,name\n-100.1,46.5,a\n")
    real_import = builtins.__import__

    def blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if "classify_tab" in name or (fromlist and "classify_tab" in fromlist):
            raise ModuleNotFoundError("blocked for the test")
        return real_import(name, globals, locals, fromlist, level)
    saved = sys.modules.pop("DamSafety_Hazard.classify_tab", None)
    DF._TAB_RULES.clear()
    builtins.__import__ = blocked
    try:
        layers = DF.find_data(d)               # must not raise
    finally:
        builtins.__import__ = real_import
        if saved is not None:
            sys.modules["DamSafety_Hazard.classify_tab"] = saved
        DF._TAB_RULES.clear()
    assert layers is not None
    assert any("classify_tab" in p for p in DF.IMPORT_PROBLEMS)
    DF.IMPORT_PROBLEMS[:] = [p for p in DF.IMPORT_PROBLEMS if "classify_tab" not in p]


def test_df5_csv_logical_rows():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "pts.csv")
    with open(p, "w", newline="") as fh:
        fh.write('lon,lat,"long\nnote"\n-100.1,46.5,"a\nb"\n-100.2,46.6,c\n\n')
    out, _n = DF._read_csv(p, os.stat(p))
    assert out, "not read as points"
    ds = out[0]
    assert ds.count == 2, ds.count
    assert len(ds.fields) == 3 and ds.fields[2] == "long\nnote", ds.fields


def test_we1_style_reason():
    d = tempfile.mkdtemp()
    bad = os.path.join(d, "doc.kml")
    with open(bad, "w") as fh:
        fh.write("not xml <<<")
    ok, why = WE.style_kml_reason(bad, "EAP_Notification_List")
    assert not ok and why
    assert WE.style_kml(bad, "x") is False
    with open(bad, "w") as fh:
        fh.write('<kml xmlns="http://www.opengis.net/kml/2.2"><Folder/></kml>')
    assert WE.style_kml_reason(bad, "x") == (False, "no KML Document element")


def test_we1_write_kmz_warning():
    from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "EAP_Notification_List",
                         "memory")
    f = QgsFeature(lyr.fields())
    f.setAttributes(["a"])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-100.0, 46.0)))
    lyr.dataProvider().addFeatures([f])
    orig = WE.style_kml_reason
    WE.style_kml_reason = lambda k, b: (False, "boom")
    try:
        warn = []
        p = WE.write_kmz(lyr, os.path.join(tempfile.mkdtemp(), "x.kmz"), warnings=warn)
    finally:
        WE.style_kml_reason = orig
    assert os.path.isfile(p) and warn and "boom" in warn[0]


def test_we3_termination_note():
    root = tempfile.mkdtemp()
    eap = os.path.join(root, "EAP")
    os.makedirs(eap)
    from qgis.core import QgsVectorLayer, QgsFeature, QgsGeometry, QgsPointXY, \
        QgsVectorFileWriter, QgsCoordinateTransformContext
    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "x", "memory")
    f = QgsFeature(lyr.fields())
    f.setAttributes(["a"])
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-100.0, 46.0)))
    lyr.dataProvider().addFeatures([f])
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName = "GPKG"
    QgsVectorFileWriter.writeAsVectorFormatV3(lyr, os.path.join(eap, "EAP_Notification_List.gpkg"),
                                              QgsCoordinateTransformContext(), o)
    logs = []
    WE.export_eap_web(eap, "Dam", log=logs.append, formats=("geojson",), bands=False)
    assert any("EAP_Termination_Point" in m for m in logs), logs


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", n, e)
    sys.exit(1 if fails else 0)
