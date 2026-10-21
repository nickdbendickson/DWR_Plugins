"""2.18.54: restored 911 points (H1, #113) and the DWR-8278 inventory follow-ups
(Docs & QA 2026-10-21, items 4-10): duplicates of a site already counted, 911 points
with non-dwelling evidence, the Notes reason, the class-rests-on-lone-points judgment
item, the model-edge check of a raster with a no-data ring (Runner ANUGA mesh / LISFLOOD
grid), co-located 911 + NSI points snapped together, and the near-limit M3 anchor test.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_restored_911.py

Needs PyQGIS (inits a headless QgsApplication) and GDAL.
"""
import json
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
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import boundary as BND  # noqa: E402
from DamSafety_Hazard.compat import FT_STRING, FT_INT, WKB_POLYGON, WKB_POINT  # noqa: E402
from DamSafety_Hazard.helpers import create_memory_layer, RasterSampler  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")
CTX = QgsCoordinateTransformContext()
X0, Y0 = 100000.0, 2600000.0
FT = 0.3048


def _layer(kind, name, fields, rows):
    lyr = create_memory_layer(name, kind, CRS, [QgsField(n, t) for n, t in fields])
    feats = []
    for geom, attrs in rows:
        f = QgsFeature(lyr.fields())
        if kind == WKB_POLYGON:
            f.setGeometry(QgsGeometry.fromRect(QgsRectangle(*geom)))
        else:
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*geom)))
        f.setAttributes(list(attrs))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    lyr.updateExtents()
    return lyr


FEMA = [("BUILD_ID", FT_INT), ("OCC_CLS", FT_STRING), ("PRIM_OCC", FT_STRING),
        ("PROP_ADDR", FT_STRING), ("PROP_ZIP", FT_STRING)]
NSI = [("fd_id", FT_INT), ("occtype", FT_STRING), ("bldgtype", FT_STRING)]
NENA = [("Site_NGUID", FT_STRING), ("Add_Number", FT_INT), ("St_Name", FT_STRING),
        ("St_PosTyp", FT_STRING), ("St_PosDir", FT_STRING), ("Placement", FT_STRING),
        ("Place_Type", FT_STRING), ("NOTES", FT_STRING)]


def _house(x, y, addr, n=1, w=12.0):
    return ((x, y, x + w, y + w), (n, "Residential", "Single Family Dwelling", addr, "58000"))


def _by_nguid(structs, nguid):
    for r in structs:
        if any(s.get("nguid") == nguid for s in r["sources"]):
            return r
    raise AssertionError("no structure with 911 point " + nguid)


def _wet(structs, depth=6.0):
    for r in structs:
        r["outcome"], r["max_depth"] = ST.OUT_PROBABLE, depth
        r["zone"] = ST.CLASS_HIGH


def test_nondwelling_911_points_are_unverified():
    fema = _layer(WKB_POLYGON, "FEMA_Structures", FEMA, [_house(X0, Y0, "1 FAR RD")])
    pts = _layer(WKB_POINT, "NDGISHUB_address_points", NENA, [
        ((X0 + 900, Y0), ("A", 3520, "153RD", "AVE", "SE", "Unknown", "Unknown",
                          "added April 2024, WBI Energy is building a natural gas "
                          "transmission line station")),
        ((X0 + 1300, Y0), ("B", 0, "83RD", "AVE", "SE", "Unknown", "Unknown", "")),
        ((X0 + 1700, Y0), ("C", 1255, "SOUTHPORT", "LOOP", "", "Parcel", "Unknown", "")),
        ((X0 + 2100, Y0), ("D", 3550, "153RD", "AVE", "SE", "Unknown", "Unknown", "")),
    ])
    structs = ST.build_inventory(fema, [("NDGISHUB", pts)], CRS, CTX)
    for g, why in (("A", "non-residential use"), ("B", "address number 0"),
                   ("C", "Placement=Parcel")):
        r = _by_nguid(structs, g)
        assert r.get("lone_address"), g
        assert r["habitable"] == "Unverified", (g, r["habitable"], r.get("site_note"))
        notes = "; ".join(ST.structure_notes(r))
        assert why in notes and "not counted" in notes, (g, notes)
    d = _by_nguid(structs, "D")
    assert d["habitable"] == "Yes" and d["persons"] > 0
    notes = "; ".join(ST.structure_notes(d))
    assert "911 point with no building (H1 default)" in notes, notes      # item 6


def test_restored_point_of_a_counted_site_is_not_counted_again():
    # HEC-RAS tier2 #761 / #733: the FEMA outline carries the address, the NSI RES1 record
    # is on it; the 911 point sits on a shop 105 ft away that has no outline
    fema = _layer(WKB_POLYGON, "FEMA_Structures", FEMA,
                  [_house(X0, Y0, "3210 150TH AVENUE SOUTHEAST"),
                   _house(X0 + 2000, Y0, "9 OTHER RD", n=2)])
    nsi = _layer(WKB_POINT, "NSI_points", NSI, [((X0 + 6, Y0 + 6), (103945889, "RES1-1SWB", "W")),
                                                ((X0 + 2600, Y0), (5, "RES1-1SNB", "W")),
                                                ((X0 + 3600, Y0), (6, "RES1-1SNB", "W"))])
    pts = _layer(WKB_POINT, "NDGISHUB_address_points", NENA, [
        ((X0 + 12 + 105 * FT + 40, Y0 + 6), ("E", 3210, "150TH", "AVE", "SE", "Unknown",
                                              "Unknown", "")),
        # NSI RES 120 ft away, no address match -> the same site
        ((X0 + 2600 + 120 * FT, Y0), ("F", 77, "ELM", "ST", "", "Unknown", "Unknown", "")),
        # NSI RES 120 ft away that has its own (other) 911 address -> another house
        ((X0 + 3600, Y0 + 0.2), ("G0", 80, "OAK", "ST", "", "Structure", "Residence", "")),
        ((X0 + 3600 + 120 * FT, Y0), ("G", 82, "OAK", "ST", "", "Unknown", "Unknown", "")),
    ])
    structs = ST.build_inventory(fema, [("NSI", nsi), ("NDGISHUB", pts)], CRS, CTX)
    host = [r for r in structs if r["footprint"] and r["habitable"] == "Yes"
            and any((s.get("nsi") or {}).get("fd_id") == 103945889 for s in r["sources"])]
    assert host, [(r["id"], r["habitable"]) for r in structs]
    e = _by_nguid(structs, "E")
    assert e.get("lone_address") and e.get("dwelling_extra"), (e.get("dup_cands"), e["habitable"])
    assert e["persons"] == 0 and "same site as structure {}".format(host[0]["id"]) in \
        "; ".join(ST.structure_notes(e)), ST.structure_notes(e)
    f = _by_nguid(structs, "F")
    assert f.get("dwelling_extra") and "NSI residential" in f["persons_basis"], f
    g = _by_nguid(structs, "G")
    assert not g.get("dwelling_extra") and g["persons"] > 0, (g.get("dup_cands"), g["persons"])
    # TM 11 counts the site once
    _wet(structs)
    tm = CL.classify_tm11(structs, [])
    assert tm["lij"] == sum(r["persons"] for r in structs if r["habitable"] == "Yes"
                            and not r.get("dwelling_extra"))
    # reinterpret() keeps the duplicate
    ST.reinterpret(structs)
    assert _by_nguid(structs, "E").get("dwelling_extra")


def test_class_resting_on_lone_points_is_a_judgment_item():
    fema = _layer(WKB_POLYGON, "FEMA_Structures", FEMA, [_house(X0, Y0, "1 FAR RD")])
    pts = _layer(WKB_POINT, "NDGISHUB_address_points", NENA, [
        ((X0 + 900, Y0), ("H", 3550, "153RD", "AVE", "SE", "Unknown", "Unknown", ""))])
    structs = ST.build_inventory(fema, [("NDGISHUB", pts)], CRS, CTX)
    for r in structs:
        if r.get("lone_address"):
            r["outcome"], r["max_depth"], r["zone"] = ST.OUT_PROBABLE, 8.17, ST.CLASS_HIGH
        else:
            r["outcome"], r["max_depth"], r["zone"] = ST.OUT_DRY, None, None
    nd = CL.classify_nd(structs, [], [])
    tm = CL.classify_tm11(structs, [])
    CL.extra_criteria(nd, structs, {}, {})
    msg = CL.lone_point_judgment(nd, tm, structs, [], [])
    assert nd["cls"] == "High" and msg, (nd["cls"], msg)
    assert "rests on 1 911 address point" in msg and "3550 153RD AVE SE" in msg, msg
    assert msg in nd["judgment"] and msg in tm["notes"]
    # nothing said when the class does not depend on them
    nd2 = CL.classify_nd([], [], [])
    assert CL.lone_point_judgment(nd2, CL.classify_tm11([], []), [], [], []) == ""


def test_colocated_911_and_nsi_points_snap_together():
    # DWR-8278 9250 56th Ave SW: 911 + NSI 0.2 m apart, 35 ft from the outline
    fema = _layer(WKB_POLYGON, "FEMA_Structures", FEMA, [_house(X0, Y0, "9250 56TH AVE SW")])
    px = X0 + 12 + 35 * FT

    def run(px):
        nsi = _layer(WKB_POINT, "NSI_points", NSI, [((px + 0.2, Y0 + 6), (1, "AGR1", "W"))])
        pts = _layer(WKB_POINT, "NDGISHUB_address_points", NENA, [
            ((px, Y0 + 6), ("K", 9250, "56TH", "AVE", "SW", "Unknown", "Unknown", ""))])
        return ST.build_inventory(fema, [("NSI", nsi), ("NDGISHUB", pts)], CRS, CTX)
    structs = run(px)
    k = _by_nguid(structs, "K")
    assert not k["footprint"] and "NSI" in k["families"], [(r["id"], r["families"])
                                                         for r in structs]
    # within the shortest reach (20 ft) both go onto the outline
    structs = run(X0 + 12 + 20 * FT)
    k = _by_nguid(structs, "K")
    assert k["footprint"] and "NSI" in k["families"], [(r["id"], r["families"])
                                                     for r in structs]


def test_m3_anchor_near_its_limit_is_flagged():
    fema = _layer(WKB_POLYGON, "FEMA_Structures", FEMA,
                  [_house(X0, Y0, "9250 56TH AVE SW"), _house(X0 + 30, Y0, "9250 56TH AVE SW")])
    nsi = _layer(WKB_POINT, "NSI_points", NSI, [((X0 + 36, Y0 + 6), (2, "RES1-1SNB", "W"))])

    def run(ft):
        pts = _layer(WKB_POINT, "NDGISHUB_address_points", NENA, [
            ((X0 - ft * FT, Y0 + 6), ("M", 9250, "56TH", "AVE", "SW", "Unknown", "Unknown",
                                      ""))])
        return ST.build_inventory(fema, [("NSI", nsi), ("NDGISHUB", pts)], CRS, CTX)
    structs = run(29.4)
    anchor = _by_nguid(structs, "M")
    other = [r for r in structs if r is not anchor][0]
    assert other.get("site_anchor") and other["habitable"] == "Unverified", other
    assert other.get("site_near") and abs(other["site_near"]["moved_ft"] - 29.4) < 0.1
    notes = "; ".join(ST.structure_notes(other))
    assert "site-anchor limit" in notes and "probably an outbuilding" in notes, notes  # 9, 10
    other["outcome"], other["max_depth"] = ST.OUT_JUDGMENT, 5.27
    j = ST.inventory_judgment(structs)
    assert any("near-threshold" in x and "29.4 ft" in x for x in j), j
    # far from the limit: no flag
    structs = run(15.0)
    assert not any(r.get("site_near") for r in structs)


def _tif(path, arr, x0, y0, cell):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0.0, y0 + cell * arr.shape[0], 0.0, -cell))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    ds.GetRasterBand(1).WriteArray(arr)
    ds = None


def _run_folder(final, depth_name="Depth (Max).tif", shape=(20, 20)):
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "_breach_model", "anuga"))
    os.makedirs(os.path.join(tmp, "_breach_model", "lisflood"))
    with open(os.path.join(tmp, "_breach_model", "run_report.json"), "w") as fh:
        json.dump({"summary": {"final_model": final},
                   "outputs": {"final": {"depth": "/elsewhere/" + depth_name}}}, fh)
    # mesh: x0+80 .. x0+720 (cells 2..17 of a 40 m grid), raster ring stays no-data
    with open(os.path.join(tmp, "_breach_model", "anuga", "mesh_boundary.csv"), "w") as fh:
        fh.write("x,y\n")
        for x, y in ((80, 80), (720, 80), (720, 720), (80, 720)):
            fh.write("{},{}\n".format(X0 + x, Y0 + y))
    with open(os.path.join(tmp, "_breach_model", "lisflood", "run.dem.asc"), "w") as fh:
        fh.write("ncols {}\nnrows {}\nxllcorner 0\nyllcorner 0\ncellsize 40\n"
                 "NODATA_value -9999\n".format(shape[1], shape[0]))
    arr = np.full(shape, -9999.0, dtype=np.float32)
    arr[2:18, 2:18] = 1.0
    arr[2:18, 17] = 8.4                       # the last valid column is wet to 8.4 ft
    path = os.path.join(tmp, depth_name)
    _tif(path, arr, X0, Y0, 40.0)
    return path


def _check(path):
    smp = RasterSampler(QgsRasterLayer(path, "d"))
    try:
        return BND.check(smp, CRS, CTX, depth_path=path)
    finally:
        smp.close()


def test_model_edge_with_nodata_ring():
    # ANUGA: the mesh boundary is found and its wet edge reported (anuga_structures)
    path = _run_folder("ANUGA")
    assert BND.runner_domain(path)[0] == "mesh"
    res = _check(path)
    assert res["flagged"] and res["wet"] > 0 and "ANUGA mesh" in res["method"], res
    assert res["max_depth"] >= 8.3, res.get("max_depth")
    # not a Runner result: the ring has no data -> "NOT checked", a judgment item
    os.remove(os.path.join(os.path.dirname(path), "_breach_model", "run_report.json"))
    res = _check(path)
    assert res.get("unchecked") and res["flagged"] and not res["wet"], res
    assert "NOT checked" in BND.describe(res) and "NOT checked" in BND.judgment_text(res)
    # LISFLOOD-FP final on its own grid: a no-data edge cell is dry and was checked
    path = _run_folder("LISFLOOD-FP")
    assert BND.runner_domain(path, 20, 20) == ("grid", None)
    res = _check(path)
    assert not res["flagged"] and res["checked"] == 76 and not res.get("unchecked"), res
    assert "LISFLOOD-FP model grid" in BND.describe(res) and "may end inside" not in \
        BND.describe(res)
    # another raster than the Runner's final one: no domain
    assert BND.runner_domain(os.path.join(os.path.dirname(path), "other.tif")) is None


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
