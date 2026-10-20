"""DamSafety Hazard Lite: core.run() on a made-up flood and made-up pack layers (GDAL + numpy, no QGIS).

    python3 DamSafety_HazardLite/tests/test_core.py

The grid is EPSG:5070 with 10 m cells; the layers are EPSG:4326 like the offline pack.
  rows 0-19, cols 0-19    depth 7 ft,  1 ft/s   house: high danger zone
  cols 0-99 (the rest)    depth 4 ft,  1 ft/s   house: judgment zone
  cols 100-149            depth 1 ft,  0.5 ft/s house and mobile home: low zone
  cols 150-199            dry
"""
import os
import shutil
import sys
import tempfile

import numpy as np
from osgeo import gdal, ogr, osr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_HazardLite import core, tm11  # noqa: E402

X0, Y0, CELL, N = -100000.0, 2800000.0, 10.0, 200
FAILS = []


def check(name, ok, info=""):
    print("{} {}{}".format("ok  " if ok else "FAIL", name, "  ({})".format(info) if not ok and str(info) else ""))
    if not ok:
        FAILS.append(name)


def xy(col, row):
    """Grid coordinates of a cell position (fractional cols / rows allowed)."""
    return X0 + col * CELL, Y0 - row * CELL


def srs(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def write_rasters(folder):
    d = np.zeros((N, N), np.float32)
    v = np.zeros((N, N), np.float32)
    d[:, :100], v[:, :100] = 4.0, 1.0
    d[:20, :20] = 7.0
    d[:, 100:150], v[:, 100:150] = 1.0, 0.5
    paths = []
    for name, a in (("depth.tif", d), ("vel.tif", v)):
        p = os.path.join(folder, name)
        ds = gdal.GetDriverByName("GTiff").Create(p, N, N, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((X0, CELL, 0, Y0, 0, -CELL))
        ds.SetProjection(srs(5070).ExportToWkt())
        ds.GetRasterBand(1).WriteArray(a)
        ds.GetRasterBand(1).SetNoDataValue(-9999)
        ds = None
        paths.append(p)
    return paths


def write_layer(path, gtype, fields, rows):
    """rows: (wkt in EPSG:5070, {field: value}); written in EPSG:4326."""
    to4326 = osr.CoordinateTransformation(srs(5070), srs(4326))
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer(os.path.splitext(os.path.basename(path))[0], srs(4326), gtype)
    for name, t in fields:
        lyr.CreateField(ogr.FieldDefn(name, t))
    for wkt, attrs in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        for k, val in attrs.items():
            f.SetField(k, val)
        g = ogr.CreateGeometryFromWkt(wkt)
        g.Transform(to4326)
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds = None


def box(c0, r0, c1, r1):
    (x0, y0), (x1, y1) = xy(c0, r0), xy(c1, r1)
    return "MULTIPOLYGON((({0} {1},{2} {1},{2} {3},{0} {3},{0} {1})))".format(x0, y0, x1, y1)


def pt(c, r):
    return "POINT({} {})".format(*xy(c, r))


def line(*cr):
    return "LINESTRING({})".format(",".join("{} {}".format(*xy(c, r)) for c, r in cr))


def mline(*cr):
    return "MULTILINESTRING(({}))".format(",".join("{} {}".format(*xy(c, r)) for c, r in cr))


def write_pack(folder):
    S, I = ogr.OFTString, ogr.OFTInteger
    p = {k: os.path.join(folder, os.path.basename(v)) for k, v in core.PACK_FILES.items()}
    write_layer(p["outlines"], ogr.wkbMultiPolygon, [("BUILD_ID", I), ("PRIM_OCC", S)], [
        (box(5, 5, 7, 7), {"BUILD_ID": 1, "PRIM_OCC": "Single Family Dwelling"}),      # A: high zone
        (box(50, 50, 52, 52), {"BUILD_ID": 2, "PRIM_OCC": "Single Family Dwelling"}),  # B: judgment, point outside
        (box(120, 50, 122, 52), {"BUILD_ID": 3, "PRIM_OCC": "Manufactured Home"}),     # C: mobile home, low zone
        (box(70, 70, 72, 72), {"BUILD_ID": 4, "PRIM_OCC": "Agriculture"}),             # F: shed, no 911 point
        (box(130, 80, 133, 83), {"BUILD_ID": 5, "PRIM_OCC": "Multi - Family Dwelling"}),  # G: two units
        (box(180, 50, 182, 52), {"BUILD_ID": 6, "PRIM_OCC": "Single Family Dwelling"}),   # H: dry
        (box(80, 45, 82, 47), {"BUILD_ID": 7, "PRIM_OCC": "Agriculture"}),      # I: ag shed, 911 point, excluded
        (box(85, 45, 87, 47), {"BUILD_ID": 8, "PRIM_OCC": "Agriculture"}),      # J: ag outline, 911 says residence
    ])
    write_layer(p["address"], ogr.wkbPoint, [("ADDRESS", S), ("Place_Type", S), ("Site_NGUID", S), ("Unit", S)], [
        (pt(6, 6), {"ADDRESS": "1 HIGH ST", "Site_NGUID": "a"}),
        (pt(51, 52.5), {"ADDRESS": "2 JUDGMENT ST", "Site_NGUID": "b"}),          # 5 m south of outline 2
        (pt(121, 51), {"ADDRESS": "3 MOBILE CT", "Site_NGUID": "c"}),
        (pt(140, 20), {"ADDRESS": "4 NO OUTLINE RD", "Site_NGUID": "d"}),          # no outline within 30 ft
        (pt(60, 20), {"ADDRESS": "5 VACANT LOT", "Place_Type": "LOT", "Site_NGUID": "e"}),
        (pt(131, 81), {"ADDRESS": "6 APT", "Unit": "1", "Site_NGUID": "g1"}),
        (pt(132, 82), {"ADDRESS": "6 APT", "Unit": "2", "Site_NGUID": "g2"}),
        (pt(181, 51), {"ADDRESS": "7 DRY LN", "Site_NGUID": "h"}),
        (pt(81, 46), {"ADDRESS": "8 AG SHED", "Site_NGUID": "i"}),
        (pt(86, 46), {"ADDRESS": "9 FARMHOUSE", "Place_Type": "RESIDENCE", "Site_NGUID": "j"}),
    ])
    write_layer(p["roads"], ogr.wkbMultiLineString, [("FULL_NAME", S), ("RoadClass", S)], [
        (mline((10, 100), (190, 100)), {"FULL_NAME": "INTERSTATE 94", "RoadClass": "Primary"}),
        (mline((30, 120), (180, 120)), {"FULL_NAME": "MAIN ST", "RoadClass": "Local"}),
        (mline((30, 140), (180, 140)), {"FULL_NAME": "COUNTY ROAD 5", "RoadClass": "Secondary"}),
        (mline((30, 160), (180, 160)), {"FULL_NAME": "HIGHWAY 2", "RoadClass": "Primary"}),
        (mline((30, 145), (180, 145)), {"FULL_NAME": "ELM ST", "RoadClass": "Local"}),
    ])
    write_layer(p["counts"], ogr.wkbPoint, [("station_id", S), ("name", S), ("aadt", I), ("aadt_year", I)], [
        (pt(170, 120), {"station_id": "s1", "aadt": 1500, "aadt_year": 2024}),    # on MAIN ST, past the water
        (pt(170, 145), {"station_id": "s2", "aadt": 2500, "aadt_year": 2024}),    # on ELM ST, not COUNTY ROAD 5
    ])
    write_layer(p["rail"], ogr.wkbLineString, [("RROWNER1", S), ("SUBDIV", S), ("PASSNGR", S), ("NET", S)], [
        (line((10, 180), (190, 180)), {"RROWNER1": "BNSF", "SUBDIV": "KO", "NET": "M"}),
        (line((10, 190), (190, 190)), {"RROWNER1": "RRVW", "SUBDIV": "FOURTH", "NET": "M"}),
        (line((10, 185), (190, 185)), {"RROWNER1": "BNSF", "SUBDIV": "OLD", "NET": "A"}),   # abandoned
    ])
    write_layer(p["crossings"], ogr.wkbPoint,
                [("CrossingID", S), ("RailroadCode", S), ("TotalDaylightThruTrains", I),
                 ("TotalNighttimeThruTrains", I), ("TotalSwitchingTrains", I)], [
        (pt(175, 180), {"CrossingID": "x1", "RailroadCode": "BNSF", "TotalDaylightThruTrains": 6,
                        "TotalNighttimeThruTrains": 5, "TotalSwitchingTrains": 0}),
    ])
    return p


def main():
    # the chart table must stay the suite's signed-off table
    sys.path.insert(0, os.path.join(ROOT, "shared", "damsuite"))
    import tm11_curves
    check("tm11 table = shared damsuite table", tm11.CURVES == tm11_curves.CURVES)
    z = tm11.zones("house", [2.94, 2.93, 5.968, 0.0], [0.0, 0.0, 0.0, 0.0])
    check("depth on a boundary takes the higher zone", list(z) == [1, 0, 2, 0], z)
    check("velocity past the chart reads the last value",
          list(tm11.zones("mobile", [1.86, 1.85], [40.0, 40.0])) == [2, 1])

    tmp = tempfile.mkdtemp()
    try:
        depth, vel = write_rasters(tmp)
        res = core.run(depth, vel, os.path.join(tmp, "out"), write_pack(tmp), log=lambda m: None)
        s = {r["address"]: r for r in res["structures"]}
        check("class High", res["class"] == "High", res["class"])
        check("structure in the deep patch: High zone", s.get("1 HIGH ST", {}).get("zone") == "High")
        b = s.get("2 JUDGMENT ST", {})
        check("point outside its outline snaps to the nearest one", b.get("build_id") == 2, b.get("matched"))
        check("judgment zone counted as High", b.get("zone") == "Judgment" and b.get("nd_class") == "High")
        c = s.get("3 MOBILE CT", {})
        check("manufactured home on Fig. 5.2, low zone, Medium",
              c.get("chart", "").startswith("Fig. 5.2") and c.get("zone") == "Low" and c.get("nd_class") == "Medium")
        check("hmax / vmax read over the outline", c.get("hmax_ft") == 1.0 and c.get("vmax_fps") == 0.5)
        check("no outline: 20 ft circle, flagged", s.get("4 NO OUTLINE RD", {}).get("matched", "").startswith("no outline"))
        check("vacant LOT point skipped", not any("VACANT" in a for a in s))
        check("outline with no 911 point not counted", not any(r["build_id"] == 4 for r in res["structures"]))
        g = [r for r in res["structures"] if r["build_id"] == 5]
        check("two 911 points in one outline: one structure, 2 units", len(g) == 1 and g[0]["units"] == 2)
        check("dry structure not listed", "7 DRY LN" not in s)
        check("FEMA agricultural out-building excluded by default", "8 AG SHED" not in s)
        check("ag outline kept when the 911 point says residence", "9 FARMHOUSE" in s)
        check("6 structures in all", len(res["structures"]) == 6, len(res["structures"]))

        r = {x["road"]: x for x in res["roads"]}
        check("Interstate overtopped: High", r.get("INTERSTATE 94", {}).get("nd_class") == "High")
        m = r.get("MAIN ST", {})
        check("AADT from the station on the same road: Medium", m.get("aadt") == 1500 and m.get("nd_class") == "Medium")
        c5 = r.get("COUNTY ROAD 5", {})
        check("a station on another road is not used", c5.get("aadt") is None and c5.get("verify"))
        check("US / State highway: Medium", r.get("HIGHWAY 2", {}).get("nd_class") == "Medium")
        check("road spot at the deepest point, hmax 4 ft", r.get("INTERSTATE 94", {}).get("hmax_ft") == 4.0)
        check("wet length counted", 1400 * 3.28 < r.get("INTERSTATE 94", {}).get("length_ft", 0) < 1600 * 3.29,
              r.get("INTERSTATE 94", {}).get("length_ft"))

        rl = {x["railroad"]: x for x in res["rail"]}
        check("11 trains/day from the crossing: High", rl.get("BNSF", {}).get("trains_day") == 11 and
              rl["BNSF"]["nd_class"] == "High")
        check("no crossing: Medium, verify", rl.get("RRVW", {}).get("nd_class") == "Medium" and rl["RRVW"]["verify"])
        check("abandoned line skipped", len(res["rail"]) == 2, len(res["rail"]))

        out = os.path.join(tmp, "out")
        for f in ("hazard_summary.md", "hazard_lite.gpkg", "structures.csv", "roads.csv", "railroads.csv"):
            check("output " + f, os.path.isfile(os.path.join(out, f)))
        ds = ogr.Open(res["gpkg"])
        check("gpkg layers", [ds.GetLayer(i).GetName() for i in range(ds.GetLayerCount())] ==
              ["structures", "road_spots", "rail_spots"] and ds.GetLayerByName("structures").GetFeatureCount() == 6)
        ds = None

        os.makedirs(os.path.join(tmp, "p2"))
        res2 = core.run(depth, vel, os.path.join(tmp, "out2"), write_pack(os.path.join(tmp, "p2")),
                        judgment_high=False, log=lambda m: None)
        b2 = {x["address"]: x for x in res2["structures"]}["2 JUDGMENT ST"]
        check("judgment zone not counted when switched off", b2["nd_class"] == "Medium")

        os.makedirs(os.path.join(tmp, "p3"))
        res3 = core.run(depth, vel, os.path.join(tmp, "out3"), write_pack(os.path.join(tmp, "p3")),
                        exclude_ag=False, log=lambda m: None)
        check("ag out-building kept when the exclusion is off",
              "8 AG SHED" in {x["address"]: x for x in res3["structures"]})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failed".format(len(FAILS)) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
