"""2.18.49: building geometry - Microsoft footprints first, the FEMA USA Structures
scene-offset check, and points moved from a wet cell onto a dry outline (Docs & QA,
DWR-8278 hand check 2026-10-21, items 1-3).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_footprint_geometry.py

Needs PyQGIS (inits a headless QgsApplication) and GDAL.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)
ROOT = os.path.dirname(HAZARD)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature, QgsField,
    QgsGeometry, QgsPointXY, QgsRasterLayer, QgsRectangle,
)

from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard import footprints as FP  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard.compat import FT_STRING, FT_INT, FT_DOUBLE, WKB_POLYGON, WKB_POINT  # noqa
from DamSafety_Hazard.helpers import create_memory_layer  # noqa: E402
from DamSafety_Hazard.processing import make_samplers  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")
CTX = QgsCoordinateTransformContext()
X0, Y0 = 100000.0, 2600000.0
SHIFT = (-8.8, -11.7)                      # DWR-8278 scene 1030010045BF2F00 (m)


def _poly_layer(name, fields, rows):
    lyr = create_memory_layer(name, WKB_POLYGON, CRS, [QgsField(n, t) for n, t in fields])
    feats = []
    for (x0, y0, x1, y1), attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x0, y0, x1, y1)))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


def _pt_layer(name, fields, rows):
    lyr = create_memory_layer(name, WKB_POINT, CRS, [QgsField(n, t) for n, t in fields])
    feats = []
    for (x, y), attrs in rows:
        f = QgsFeature(lyr.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


FEMA_FIELDS = [("BUILD_ID", FT_INT), ("OCC_CLS", FT_STRING), ("PRIM_OCC", FT_STRING),
               ("PROP_ADDR", FT_STRING), ("IMAGE_NAME", FT_STRING)]
NSI_FIELDS = [("fd_id", FT_INT), ("occtype", FT_STRING), ("bldgtype", FT_STRING),
              ("ftprntsqft", FT_DOUBLE)]


def _farm():
    """12 true buildings (10 x 15 m) 60 m apart; FEMA scene S1 (first 6) offset SHIFT,
    scene S2 (last 6) in place; Microsoft footprints and NSI points on the truth."""
    truth = [(X0 + 60.0 * (i % 6), Y0 + 60.0 * (i // 6)) for i in range(12)]
    fema, ms, nsi = [], [], []
    for i, (x, y) in enumerate(truth):
        dx, dy = SHIFT if i < 6 else (0.0, 0.0)
        fema.append(((x + dx, y + dy, x + dx + 10, y + dy + 15),
                     (i + 1, "Residential", "Single Family Dwelling",
                      "{} 56TH AVE SW".format(9000 + i), "S1" if i < 6 else "S2")))
        ms.append(((x + 0.5, y + 0.4, x + 10.5, y + 15.4), ("Microsoft",)))
        nsi.append(((x + 5.0, y + 7.5), (1000 + i, "RES1-1SNB", "W", 1615.0)))
    return (_poly_layer("FEMA_Structures", FEMA_FIELDS, fema),
            _poly_layer("MS_building_footprints", [("ms_source", FT_STRING)], ms),
            _pt_layer("NSI_structures", NSI_FIELDS, nsi), truth)


def test_pure_parts():
    assert FP.quadkeys_for_extent((-101.63, 46.01, -101.39, 46.16))[0] == "021321313"
    assert FP.outline_kind("OUTLINES", "MS_building_footprints_20261021_ab.geojson") == FP.KIND_MS
    assert FP.outline_kind("FEMA_STRUCTURES", "x", ["BUILD_ID", "OCC_CLS", "IMAGE_NAME"]) == \
        FP.KIND_FEMA
    assert FP.outline_kind("OUTLINES", "gis_osm_buildings_a_free_1") == FP.KIND_OSM
    assert FP.outline_kind("OUTLINES", "my_buildings") == FP.KIND_OTHER
    outl = [("A", 50.0 * i, 0.0, 150.0) for i in range(8)] + \
        [("B", 50.0 * i, 500.0, 150.0) for i in range(8)]
    refs = [(50.0 * i + 9.0, 11.0, 150.0) for i in range(8)] + \
        [(50.0 * i + 0.5, 500.4, 150.0) for i in range(8)]
    r = FP.scene_offsets(outl, refs)
    assert r["A"]["flagged"] and abs(r["A"]["shift_m"] - (81 + 121) ** 0.5) < 1e-6, r["A"]
    assert not r["B"]["flagged"], r["B"]
    few = FP.scene_offsets(outl[:3], refs)
    assert not few["A"]["flagged"]                       # under SCENE_MIN_N pairs
    assert abs(FP.parse_size_mb("376.6KB") - 0.3766) < 1e-9
    assert abs(FP.parse_size_mb("1.2GB") - 1200.0) < 1e-6


def test_scene_flag_with_nsi_only():
    fema, _ms, nsi, truth = _farm()
    rep = {}
    structs = ST.build_inventory(fema, [("NSI", nsi)], CRS, CTX, scene_report=rep)
    assert rep["S1"]["flagged"] and not rep["S2"]["flagged"], rep
    assert abs(rep["S1"]["dx_m"] - 8.8) < 0.6 and abs(rep["S1"]["dy_m"] - 11.7) < 0.6, rep["S1"]
    s1 = [r for r in structs if r["footprint"] and any(
        s.get("scene_offset") for s in r["sources"])]
    assert len(s1) == 6
    # no other outline source: the FEMA outline stays where it is and says so
    assert all("NOT moved" in ST.geometry_note(r) for r in s1)
    qa = [i for _r, i in ST.inventory_qa(structs) if i.startswith("FEMA scene")]
    assert len(qa) == 1 and "S1" in qa[0], qa
    j = ST.inventory_judgment(structs)
    assert len(j) == 1 and "scene S1" in j[0] and "still use the offset FEMA outline" in j[0], j


def test_scene_shift_setting():
    fema, _ms, nsi, truth = _farm()
    structs = ST.build_inventory(fema, [("NSI", nsi)], CRS, CTX, scene_shift=True)
    moved = [r for r in structs if r["footprint"] and "moved by the scene offset"
             in ST.geometry_note(r)]
    assert len(moved) == 6
    for r in moved:
        c = r["geom"].centroid().asPoint()
        assert min(abs(c.x() - (x + 5)) + abs(c.y() - (y + 7.5)) for x, y in truth) < 1.5


def test_microsoft_geometry_carries_fema_attributes():
    fema, ms, nsi, truth = _farm()
    structs = ST.build_inventory([(ST.STRUCTURES_FAMILY, fema), (FP.MS_FAMILY, ms)],
                                 [("NSI", nsi)], CRS, CTX)
    outl = [r for r in structs if r["footprint"]]
    assert len(outl) == 12, [(r["id"], r["geom_kind"]) for r in outl]
    assert all(r["geom_kind"] == FP.KIND_MS for r in outl)
    for r in outl:
        kinds = [s.get("outline_kind") for s in r["sources"]]
        assert FP.KIND_FEMA in kinds, (r["id"], kinds)          # parcel / occupancy carried
        assert any(s.get("parcel") for s in r["sources"])
        assert any(s.get("nsi") for s in r["sources"])         # NSI point inside the MS outline
    # the old order is still there on request: FEMA geometry, MS merged where it overlaps
    old = ST.build_inventory([(ST.STRUCTURES_FAMILY, fema), (FP.MS_FAMILY, ms)],
                             [("NSI", nsi)], CRS, CTX, geometry="listed")
    assert old[0]["geom_kind"] == FP.KIND_FEMA


def _depth_tif(path, wet_from_x):
    """1 m grid, 60 x 40 cells; dry (0) west of wet_from_x, 2.5 ft east of it."""
    arr = np.zeros((40, 60), dtype=np.float32)
    xs = X0 + 0.5 + np.arange(60)
    arr[:, xs >= wet_from_x] = 2.5
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, 60, 40, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, 1.0, 0.0, Y0 + 40.0, 0.0, -1.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None


def test_snap_from_wet_cell_onto_dry_outline_is_listed():
    tmp = tempfile.mkdtemp()
    dpath = os.path.join(tmp, "depth.tif")
    _depth_tif(dpath, X0 + 13.0)
    vpath = os.path.join(tmp, "vel.tif")
    _depth_tif(vpath, X0 + 13.0)
    house = _poly_layer("FEMA_Structures", [("building", FT_STRING)],
                        [((X0, Y0 + 15, X0 + 10, Y0 + 25), ("house",))])
    nena = [("Add_Number", FT_INT), ("St_Name", FT_STRING), ("Placement", FT_STRING),
            ("Place_Type", FT_STRING)]
    addr = _pt_layer("NDGISHUB_address_points", nena,
                     [((X0 + 25.0, Y0 + 20.0), (9250, "56TH", "Unknown", "Residence"))])
    structs = ST.build_inventory(house, [("NDGISHUB", addr)], CRS, CTX)
    assert len(structs) == 1 and structs[0]["footprint"]
    smp = make_samplers(QgsRasterLayer(dpath, "d"), QgsRasterLayer(vpath, "v"),
                        QgsRasterLayer(vpath, "a"), 0)
    ST.evaluate_structures(structs, smp, CRS, CTX)
    rec = structs[0]
    assert rec["max_depth"] is None or rec["max_depth"] < ST.CONSEQUENCE_DEPTH_FT
    assert rec["snap_flip"] and abs(rec["snap_flip"][0]["moved_ft"] - 49.2) < 1.0, \
        rec["snap_flip"]
    assert any("crossed into another flood cell" in n for n in ST.structure_notes(rec))
    j = ST.inventory_judgment(structs)
    assert any("moved more than 33 ft" in x for x in j), j
    # the judgment item reaches the ND result
    nd = CL.classify_nd(structs, [], [])
    CL.extra_criteria(nd, structs, {}, {})
    assert any("moved more than 33 ft" in x for x in nd["judgment"]), nd["judgment"]
    # a short move (inside SNAP_FLIP_FT) is not listed
    addr2 = _pt_layer("NDGISHUB_address_points", nena,
                      [((X0 + 14.0, Y0 + 20.0), (9250, "56TH", "Unknown", "Residence"))])
    structs = ST.build_inventory(house, [("NDGISHUB", addr2)], CRS, CTX)
    ST.evaluate_structures(structs, smp, CRS, CTX)
    assert not structs[0]["snap_flip"]


def main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name)
    sys.stdout.flush()
    os._exit(1 if fails else 0)


if __name__ == "__main__":
    main()
