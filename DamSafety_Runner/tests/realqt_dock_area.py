"""3.13.4 (built as 3.13.3) / Setup 1.0.18 (QGIS 4 fulltest 2026-10-30): the docks are added with the scoped
Qt.DockWidgetArea enum. QGIS 4 (PyQt6) has no Qt.RightDockWidgetArea / Qt.LeftDockWidgetArea,
so the Runner's results dock and Setup's status dock raised AttributeError / TypeError there.

Real Qt, offscreen, in the QGIS python (QGIS 3.34 and 4):
    QT_QPA_PLATFORM=offscreen python3 DamSafety_Runner/tests/realqt_dock_area.py
The fast suite runs it in its own process through tests/test_dock_area_enum.py.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FAILS = []


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


try:
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtWidgets import QApplication, QDockWidget, QMainWindow
except ImportError:                                    # pragma: no cover
    print("SKIP: no real QGIS")
    sys.exit(0)

app = QApplication.instance() or QApplication([])
area = getattr(Qt, "DockWidgetArea", None)

from DamSafety_Runner import results_dock as rd        # noqa: E402

check("Runner RIGHT_DOCK is Qt.DockWidgetArea.RightDockWidgetArea",
      area is None or rd.RIGHT_DOCK == area.RightDockWidgetArea, rd.RIGHT_DOCK)
win = QMainWindow()
d = QDockWidget("results")
d.setAllowedAreas(rd.LEFT_DOCK | rd.RIGHT_DOCK)
win.addDockWidget(rd.RIGHT_DOCK, d)
check("Runner: addDockWidget(RIGHT_DOCK) puts the dock on the right",
      win.dockWidgetArea(d) == rd.RIGHT_DOCK, win.dockWidgetArea(d))

# Setup's plugin.py imports qgis.utils / iface pieces; read its LEFT_DOCK without the GUI
src = open(os.path.join(ROOT, "DamSafety_Setup", "plugin.py"), encoding="utf-8").read()
check("Setup: addDockWidget is given LEFT_DOCK", "addDockWidget(LEFT_DOCK" in src)
ns = {"Qt": Qt}
exec(re.search(r"try:\n    LEFT_DOCK = .*?\n(?:.*\n){2}", src).group(0), ns)
s = QDockWidget("status")
win.addDockWidget(ns["LEFT_DOCK"], s)
check("Setup: addDockWidget(LEFT_DOCK) puts the dock on the left",
      win.dockWidgetArea(s) == ns["LEFT_DOCK"], win.dockWidgetArea(s))

# no bare Qt.<X>DockWidgetArea left in either plugin
bare = []
for plug in ("DamSafety_Runner", "DamSafety_Setup", "DamSafety_Hazard", "DamSafety_Screening",
             "DamSafety_Data"):
    for dp, _dn, fn in os.walk(os.path.join(ROOT, plug)):
        if "tests" in dp.split(os.sep):
            continue
        for f in fn:
            if f.endswith(".py"):
                t = open(os.path.join(dp, f), encoding="utf-8").read()
                for m in re.finditer(r"Qt\.(Left|Right|Top|Bottom)DockWidgetArea", t):
                    line = t[:m.start()].count("\n") + 1
                    ctx = t.splitlines()[line - 1]
                    if ctx.lstrip().startswith("#"):
                        continue
                    if "except AttributeError" not in t[max(0, m.start() - 120):m.start()]:
                        bare.append("{}:{}: {}".format(os.path.relpath(os.path.join(dp, f), ROOT),
                                                      line, ctx.strip()))
check("no bare Qt.*DockWidgetArea outside a fallback", not bare, bare)

# 3.13.6: the results dock on a synthetic dam folder (Hazard line, stale warning, relabel,
# layer buttons, expandable warnings)
import json as _json, shutil as _sh, tempfile as _tf  # noqa: E401,E402
_d = _tf.mkdtemp(prefix="rdock_qt_")
os.makedirs(os.path.join(_d, "_breach_model"))
os.makedirs(os.path.join(_d, "outputs"))
_json.dump({"summary": {"dam_id": "T-1", "receptors_flooded": "37 structures, 1.0 km of road"},
            "warnings": ["w1", "w2"]},
           open(os.path.join(_d, "_breach_model", "run_report.json"), "w"))
open(os.path.join(_d, "outputs", "Hazard_Classification_Summary.txt"), "w").write(
    "DamSafety Hazard v2.0.0 - Hazard Classification Summary\n"
    "ND HAZARD CLASSIFICATION (screening): HIGH\n"
    "  - MEDIUM - 45 habitable structure(s) inundated (economic loss) [5.2.2]\n"
    "ACER TM 11 CLASSIFICATION: SIGNIFICANT\n")


class _Iface:
    def mainWindow(self):
        return win


rdock = rd.ResultsDock(_Iface())
rdock.set_folder(_d)
check("results dock: Runner receptors relabelled, Hazard count beside",
      "37 Runner receptor structure(s)" in rdock.summary.text() and
      "Hazard: 45 flooded habitable" in rdock.summary.text(), rdock.summary.text())
check("results dock: Hazard line", "Hazard 2.0.0: ND High" in rdock.hazard.text(),
      rdock.hazard.text())
check("results dock: stale summary warned (installed Hazard is newer)",
      rdock.stale.isVisibleTo(rdock) and "2.0.0" in rdock.stale.text(), rdock.stale.text())
check("results dock: stale summary - Next says re-classify, never 'Nothing to do'",
      "Next: Re-classify with the installed Hazard" in rdock.hazard.text() and
      "Nothing to do" not in rdock.hazard.text(), rdock.hazard.text())
check("results dock: warnings collapsed then listed",
      not rdock.warn_list.isVisibleTo(rdock) and rdock.warn_list.count() == 2 and
      "2 thing(s) to check" in rdock.warn_toggle.text(), rdock.warn_toggle.text())
rdock.warn_toggle.setChecked(True)
check("results dock: warnings expand", rdock.warn_list.isVisibleTo(rdock))
check("results dock: no layer files, buttons off",
      not any(b.isEnabled() for b in rdock.layer_buttons.values()))
_json.dump({"schema": "damsafety-review-v1", "nd_class_of_record": "Medium",
            "tm11_class_of_record": "Significant", "decided_by": "N. B.",
            "decided_on": "2026-11-01", "note": "test",
            "overrides": {"nd_class": "Medium", "hazard_version": "2.0.0"},
            "applies_to": {"run_folder": "_breach_model"}},
           open(os.path.join(_d, "review.json"), "w"))
rdock.set_folder(_d)
check("results dock: class of record from review.json beside the printed class",
      "class of record ND Medium, TM 11 Significant (N. B., 2026-11-01) - differs from printed"
      in rdock.hazard.text(), rdock.hazard.text())
check("results dock: review overriding another printed class is flagged",
      "Class of record: class of record overrides printed ND class Medium" in rdock.stale.text(),
      rdock.stale.text())
with open(os.path.join(_d, "review.json"), "w") as _f:
    _f.write("{broken")
rdock.set_folder(_d)
check("results dock: invalid review.json is not recorded",
      "not recorded (review.json invalid)" in rdock.hazard.text(), rdock.hazard.text())
_sh.rmtree(_d, ignore_errors=True)

# 3.13.6: all five layer buttons open their data read-only (Docs & QA: the Wet Hazard
# structures button opened Habitable_Structures.gpkg read-write - change counter, md5 and
# mtime changed). Load each layer from a copy, then compare md5, mtime and the folder listing.
import hashlib as _hl, time as _time
from osgeo import gdal as _gdal, ogr as _ogr, osr as _osr
from qgis.core import QgsProject as _QP
_ro = _tf.mkdtemp(prefix="dock_ro_")
RI = rd.RI
_srs = _osr.SpatialReference(); _srs.ImportFromEPSG(2265)
for _k, _fn in RI.FINAL_NAMES.items():
    _r = _gdal.GetDriverByName("GTiff").Create(os.path.join(_ro, _fn), 4, 4, 1, _gdal.GDT_Float32)
    _r.SetGeoTransform((0, 10, 0, 40, 0, -10)); _r.SetProjection(_srs.ExportToWkt())
    _r.GetRasterBand(1).Fill(1.5); _r.GetRasterBand(1).SetNoDataValue(-9999); _r = None
with open(os.path.join(_ro, "receptors_flooded.csv"), "w") as _f:
    _f.write("x,y,kind,depth_ft\n1,1,structure,2.5\n2,2,road,0\n3,3,rail,1.0\n")
_gp = os.path.join(_ro, "Habitable_Structures.gpkg")
_ds = _ogr.GetDriverByName("GPKG").CreateDataSource(_gp)
_l = _ds.CreateLayer("Habitable_Structures", _srs, _ogr.wkbPoint)
_l.CreateField(_ogr.FieldDefn("Max_Depth_ft", _ogr.OFTReal))
for _i in range(5):
    _ft = _ogr.Feature(_l.GetLayerDefn()); _ft["Max_Depth_ft"] = _i - 2.0
    _g = _ogr.Geometry(_ogr.wkbPoint); _g.AddPoint(_i, _i); _ft.SetGeometry(_g); _l.CreateFeature(_ft)
_ds = _l = _ft = None
_paths = {k: os.path.join(_ro, fn) for k, fn in RI.FINAL_NAMES.items()}
_paths["receptors"] = os.path.join(_ro, "receptors_flooded.csv")
_paths["hazard_structures"] = _gp
_time.sleep(1.1)
def _snap():
    return ({k: (_hl.md5(open(p, "rb").read()).hexdigest(), os.stat(p).st_mtime_ns)
             for k, p in _paths.items()}, sorted(os.listdir(_ro)))
_before = _snap()
rdock._layers = dict(_paths); rdock._counts = None; rdock._report = {"work_crs": "EPSG:2265"}
_loaded = {}
for _k in RI.LAYER_KEYS:
    _lyr = rdock.load_layer(_k)
    _loaded[_k] = _lyr
    if _lyr is not None:
        list(_lyr.getFeatures()) if hasattr(_lyr, "getFeatures") else _lyr.dataProvider().block(1, _lyr.extent(), 4, 4)
check("results dock: all five layer buttons load", all(v is not None and v.isValid() for v in _loaded.values()),
      ({k: v is not None for k, v in _loaded.items()}, rdock.summary.text(), __import__("qgis.core").core.QgsProviderRegistry.instance().providerList()))
check("results dock: vector layers are read-only",
      _loaded["receptors"].readOnly() and _loaded["hazard_structures"].readOnly())
check("results dock: filtered subsets on the read-only layers",
      _loaded["receptors"].featureCount() == 2 and _loaded["hazard_structures"].featureCount() == 2,
      (_loaded["receptors"].featureCount(), _loaded["hazard_structures"].featureCount()))
_QP.instance().removeAllMapLayers(); _loaded = _lyr = None
_after = _snap()
check("results dock: loading the five layers leaves md5, mtime and the folder unchanged",
      _before == _after, (_before, _after))
_sh.rmtree(_ro, ignore_errors=True)

print("\n{} failure(s)".format(len(FAILS)) if FAILS else "\nall checks passed")
sys.exit(1 if FAILS else 0)
