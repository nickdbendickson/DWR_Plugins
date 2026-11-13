"""Hazard 2.25.0: a class that rests on velocity is flagged; nothing is re-classified.

A structure's zone is read at its peak depth and the PEAK velocity over its wet cells (2.22.3).
Peak velocity is the model output that differs most between solvers and grids. A structure
whose zone would be less severe with the 95th-percentile velocity of its own cells, or on depth
alone, gets a Notes line (rec["vel_hinge"]); classification.velocity_hinge_judgment computes
the ND / TM 11 class on those two bases and says, in a judgment item, whether the class would
be lower. Zones, outcomes and classes stay as they were. Synthetic rasters only.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_velocity_hinge.py

Needs PyQGIS (inits a headless QgsApplication) and GDAL.
"""
import copy
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature, QgsField,
    QgsGeometry, QgsPointXY, QgsRasterLayer, QgsRectangle,
)

from DamSafety_Hazard import classification as CL, scenarios as SC, structures as ST  # noqa: E402
from DamSafety_Hazard.compat import FT_INT, FT_STRING, WKB_POINT, WKB_POLYGON  # noqa: E402
from DamSafety_Hazard.constants import CLASS_HIGH, CLASS_JUDGMENT, CLASS_LOW  # noqa: E402
from DamSafety_Hazard.helpers import create_memory_layer  # noqa: E402
from DamSafety_Hazard.processing import make_samplers  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")
CTX = QgsCoordinateTransformContext()
X0, Y0 = 100000.0, 2600000.0
NODATA = -9999.0
TMP = tempfile.mkdtemp(prefix="vel_hinge_")


def _tif(path, arr):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + arr.shape[0], 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(NODATA)
    ds.GetRasterBand(1).WriteArray(arr.astype(np.float32))
    ds = None


def _house():
    lyr = create_memory_layer("FEMA_Structures", WKB_POLYGON, CRS,
                              [QgsField("building", FT_STRING)])
    f = QgsFeature(lyr.fields())
    f.setGeometry(QgsGeometry.fromRect(QgsRectangle(X0 + 10, Y0 + 10, X0 + 20, Y0 + 20)))
    f.setAttributes(["house"])
    lyr.dataProvider().addFeatures([f])
    nena = [("Add_Number", FT_INT), ("St_Name", FT_STRING), ("Placement", FT_STRING),
            ("Place_Type", FT_STRING)]
    pts = create_memory_layer("NDGISHUB_address_points", WKB_POINT, CRS,
                              [QgsField(n, t) for n, t in nena])
    p = QgsFeature(pts.fields())
    p.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(X0 + 15, Y0 + 15)))
    p.setAttributes([9250, "56TH", "Unknown", "Residence"])
    pts.dataProvider().addFeatures([p])
    return lyr, pts


def _run(depth_ft, vel, sid=1, **kw):
    """One house in `depth_ft` of water with the velocity grid `vel` (30 x 30, 1 ft cells)."""
    depth = np.zeros((30, 30))
    depth[2:28, 2:28] = depth_ft
    dpath, vpath = os.path.join(TMP, "d.tif"), os.path.join(TMP, "v.tif")
    _tif(dpath, depth)
    _tif(vpath, vel)
    house, pts = _house()
    structs = ST.build_inventory(house, [("NDGISHUB", pts)], CRS, CTX)
    assert len(structs) == 1, structs
    smp = make_samplers(QgsRasterLayer(dpath, "d"), QgsRasterLayer(vpath, "v"),
                        QgsRasterLayer(vpath, "a"), 0)
    ST.evaluate_structures(structs, smp, CRS, CTX, **kw)
    rec = structs[0]
    rec["id"] = sid
    assert rec["habitable"] == "Yes", rec["habitable"]
    return rec


def _one_fast_cell(v=12.0, base=1.0):
    vel = np.full((30, 30), base)
    vel[15, 15] = v                              # one cell of the footprint, much faster
    return vel


def _classes(recs, **kw):
    nd = CL.classify_nd(recs, [], [], **kw)
    tm = CL.classify_tm11(recs, [])
    return nd, tm


def test_a_zone_that_rests_on_one_fast_cell_is_flagged_and_nothing_changes():
    rec = _run(2.0, _one_fast_cell())
    # as run: 2 ft at the peak 12 ft/s is the judgment zone (house on foundation)
    assert rec["zone"] == CLASS_JUDGMENT and rec["outcome"] == ST.OUT_JUDGMENT
    assert abs(rec["max_vel"] - 12.0) < 1e-6 and abs(rec["max_depth"] - 2.0) < 1e-6
    assert rec["vel_p95"] is not None and abs(rec["vel_p95"] - 1.0) < 1e-6, rec["vel_p95"]
    assert rec["vel_hinge"] == {"zone": CLASS_JUDGMENT, "zone_p95": CLASS_LOW,
                                "zone_dep": CLASS_LOW}, rec["vel_hinge"]
    notes = "; ".join(ST.structure_notes(rec))
    assert ("zone rests on velocity: JUDGMENT at the peak velocity (12.0 ft/s), LOW at the "
            "95th-percentile velocity of its cells (1.0 ft/s); LOW on depth alone") in notes, notes
    assert notes.rstrip().endswith("verify") or "verify" in notes
    # the flag rides the scenario snapshot (a second scenario must not inherit it)
    assert "vel_hinge" in SC.EVAL_KEYS and "vel_p95" in SC.EVAL_KEYS
    assert ST._RESET["vel_hinge"] is None and ST._RESET["vel_p95"] is None


def test_the_class_that_rests_on_velocity_gets_a_judgment_item_and_stays():
    rec = _run(2.0, _one_fast_cell())
    nd, tm = _classes([rec])
    assert nd["cls"] == "Medium", nd["cls"]              # the judgment-zone rule (2.22.0)
    before = (nd["cls"], tm["cls"], rec["zone"], rec["outcome"], copy.deepcopy(rec["_types"]))
    n_items = len(nd["judgment"])
    msg = CL.velocity_hinge_judgment(nd, tm, [rec], [], [])
    # ND stays Medium on every basis (an inundated house is Medium anyway, 5.2.2); the TM 11
    # class is the one that rests on the velocity here
    assert tm["cls"] == "Significant", tm["cls"]
    assert msg.startswith("The class rests on velocity: ND MEDIUM / TM 11 Significant as run"), msg
    assert "with the 95th-percentile velocity of each structure's cells: TM 11 Low" in msg, msg
    assert "with depth alone (velocity 0): TM 11 Low" in msg, msg
    assert "ND LOW" not in msg, msg
    assert "structure 1 (2.0 ft deep, peak 12.0 ft/s, 95th percentile 1.0 ft/s)" in msg, msg
    assert "The class is not changed." in msg
    vh = nd["velocity_hinge"]
    assert vh["rests"] is True and vh["structures"] == 1 and vh["ids"] == [1]
    for key in (CL.VEL_KEY_P95, CL.VEL_KEY_DEPTH):
        assert vh[key] == {"nd": "Medium", "tm11": "Low", "nd_lower": False,
                           "tm11_differs": True}, vh[key]
    assert len(nd["judgment"]) == n_items + 1 and nd["judgment"][nd["struct_items"] - 1] == msg
    assert msg in tm["notes"]
    # nothing was re-classified
    assert (nd["cls"], tm["cls"], rec["zone"], rec["outcome"], rec["_types"]) == before
    assert rec["vel_hinge"] is not None


def test_velocity_everywhere_is_flagged_on_depth_alone_only():
    rec = _run(2.0, np.full((30, 30), 12.0))
    assert rec["zone"] == CLASS_JUDGMENT and abs(rec["vel_p95"] - 12.0) < 1e-6
    assert rec["vel_hinge"] == {"zone": CLASS_JUDGMENT, "zone_p95": CLASS_JUDGMENT,
                                "zone_dep": CLASS_LOW}
    notes = "; ".join(ST.structure_notes(rec))
    assert "LOW on depth alone" in notes and "95th-percentile" not in notes, notes
    nd, tm = _classes([rec])
    msg = CL.velocity_hinge_judgment(nd, tm, [rec], [], [])
    assert "with depth alone (velocity 0): TM 11 Low" in msg and "95th-percentile velocity of each" \
        not in msg.split("structure(s) are in")[0], msg
    assert nd["velocity_hinge"][CL.VEL_KEY_P95]["tm11_differs"] is False
    assert nd["velocity_hinge"][CL.VEL_KEY_DEPTH]["tm11_differs"] is True and nd["cls"] == "Medium"


def test_a_zone_the_depth_gives_by_itself_is_not_flagged():
    for depth, zone in ((6.5, CLASS_HIGH), (3.5, CLASS_JUDGMENT), (1.0, CLASS_LOW)):
        rec = _run(depth, _one_fast_cell(v=3.0))
        assert rec["zone"] == zone, (depth, rec["zone"])
        assert rec["vel_hinge"] is None, (depth, rec["vel_hinge"])
        assert "rests on velocity" not in "; ".join(ST.structure_notes(rec))
        nd, tm = _classes([rec])
        n = len(nd["judgment"])
        assert CL.velocity_hinge_judgment(nd, tm, [rec], [], []) == ""
        assert nd["velocity_hinge"]["structures"] == 0 and len(nd["judgment"]) == n


def test_high_by_velocity_reports_each_basis():
    # 4.5 ft: HIGH at 12 ft/s (boundary about 3.5 ft), JUDGMENT at 1 ft/s and on depth alone
    rec = _run(4.5, _one_fast_cell())
    assert rec["zone"] == CLASS_HIGH and rec["outcome"] == ST.OUT_PROBABLE
    assert rec["vel_hinge"] == {"zone": CLASS_HIGH, "zone_p95": CLASS_JUDGMENT,
                                "zone_dep": CLASS_JUDGMENT}
    nd, tm = _classes([rec])
    assert nd["cls"] == "High"
    msg = CL.velocity_hinge_judgment(nd, tm, [rec], [], [])
    assert msg.startswith("The class rests on velocity: ND HIGH"), msg
    assert msg.count("ND MEDIUM") == 2, msg                 # judgment zone: Medium by rule
    assert nd["velocity_hinge"][CL.VEL_KEY_P95]["nd"] == "Medium"
    assert nd["velocity_hinge"][CL.VEL_KEY_DEPTH]["nd_lower"] is True
    assert nd["cls"] == "High"                              # still the class
    # with the conservative judgment-zone setting the class is High on every basis
    nd, tm = _classes([rec], judgment_as_probable=True)
    msg = CL.velocity_hinge_judgment(nd, tm, [rec], [], [], judgment_as_probable=True)
    assert nd["velocity_hinge"]["rests"] is False, msg
    assert msg.startswith("1 structure(s) are in a more severe zone only because of velocity")
    assert "the ND and TM 11 classes are the same" in msg and nd["judgment"][-1] == msg


def test_another_house_that_holds_the_class_gives_the_lighter_line():
    hinge = _run(2.0, _one_fast_cell(), sid=1)
    solid = _run(3.5, np.full((30, 30), 1.0), sid=2)          # judgment zone by depth
    assert solid["vel_hinge"] is None and solid["zone"] == CLASS_JUDGMENT
    nd, tm = _classes([hinge, solid])
    msg = CL.velocity_hinge_judgment(nd, tm, [hinge, solid], [], [])
    assert nd["cls"] == "Medium" and nd["velocity_hinge"]["rests"] is False
    assert nd["velocity_hinge"]["ids"] == [1]
    assert msg.startswith("1 structure(s) are in a more severe zone only because of velocity")
    assert "structure 1 (" in msg and "structure 2" not in msg


def test_no_flag_without_velocity_or_with_the_1d_rule():
    rec = _run(2.0, np.full((30, 30), NODATA))               # velocity unknown at every cell
    assert rec.get("vel_unknown") and rec["vel_hinge"] is None and rec["vel_p95"] is None
    rec = _run(4.5, _one_fast_cell(), one_d=True)            # 1D: depth > 2 ft decides
    assert rec["outcome"] == ST.OUT_PROBABLE and rec["vel_hinge"] is None
    nd, tm = _classes([rec], one_d=True)
    assert CL.velocity_hinge_judgment(nd, tm, [rec], [], [], one_d=True) == ""
    # re-applying a type or setting keeps the flag in step (no rasters read)
    rec = _run(2.0, _one_fast_cell())
    ST.apply_type(rec, one_d=True)
    assert rec["vel_hinge"] is None
    ST.apply_type(rec, one_d=False)
    assert rec["vel_hinge"] is not None


def test_the_view_is_a_copy_and_follows_the_borderline_rule():
    rec = _run(2.0, _one_fast_cell())
    for basis in (ST.VEL_P95, ST.VEL_DEPTH):
        view = ST.velocity_view(rec, basis)
        assert view is not rec and view["zone"] == CLASS_LOW
        assert view["outcome"] == ST.OUT_ECONOMIC and rec["outcome"] == ST.OUT_JUDGMENT
    # 2.9 ft at 1 ft/s is 0.02 ft below the judgment line: with the borderline rule on
    # (5.3(f)) the view is raised to the judgment zone exactly as a record would be
    rec = _run(2.9, _one_fast_cell())
    assert rec["zone"] == CLASS_JUDGMENT and rec["vel_hinge"]["zone_p95"] == CLASS_LOW
    plain = ST.velocity_view(rec, ST.VEL_P95, near_ft=0.5, borderline_higher=False)
    raised = ST.velocity_view(rec, ST.VEL_P95, near_ft=0.5, borderline_higher=True)
    assert plain["zone"] == CLASS_LOW and plain["near"] == "Judgment"
    assert raised["zone"] == CLASS_JUDGMENT and raised["outcome"] == ST.OUT_JUDGMENT
    # a structure that is not flagged, or does not count, is returned as it is
    dry = _run(1.0, _one_fast_cell(v=3.0))
    assert ST.velocity_view(dry, ST.VEL_P95) is dry


def test_the_structures_layer_carries_the_two_fields():
    flagged = _run(2.0, _one_fast_cell(), sid=1)
    plain = _run(1.0, _one_fast_cell(v=3.0), sid=2)
    lyr = ST.structures_to_layer([flagged, plain], CRS, include_dry=True)
    names = [f.name() for f in lyr.fields()]
    assert "Vel_P95_fps" in names and "Zone_On_Velocity" in names, names
    rows = {f["Struct_ID"]: f for f in lyr.getFeatures()}
    assert abs(rows[flagged["id"]]["Vel_P95_fps"] - 1.0) < 1e-6
    assert rows[flagged["id"]]["Zone_On_Velocity"] == "Yes"
    assert not rows[plain["id"]]["Zone_On_Velocity"]             # NULL: nothing to check


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
