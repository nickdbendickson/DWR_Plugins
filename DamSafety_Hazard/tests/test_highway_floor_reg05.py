"""Hazard 2.18.48: US / State highways (REG_05.2026 5.2.1.2(b)) and blank arrival times.

  * Roads: a US or State highway is at least Medium whatever its AADT (5.2.1.2(b), p. 23); the
    private / AADT < 500 Low rule (5.2.1.2(a)) is for local, county and private roads. This
    plugin already floored US / State highways; it now also reads a bare 'Highway 23' / 'Hwy 2'
    (ND NG9-1-1 names) and 'North Dakota Highway 200' as State highways, not 'County Highway 12',
    and the criteria table has a 5.2.1.2(b) row.
  * Parity with the Runner / Screening rules module (core/nd_hazard.py 1.2.0), when those
    plugins are next to this one: the same route texts are US / State highways and interstates,
    and nd_road_class gives the class nd_hazard.road_class gives for every road class x AADT.
  * A flagged structure (DWR-3161, the ND-23 road fill over backwater) is refused before any
    road rule: its Low of record stands.
  * Arrival (5.4(c)): a wet structure with no arrival-raster value at its wet cells (the flood
    there never exceeded the model's arrival depth: Runner ARRIVAL_FT, 0.1 ft by default since
    Runner 3.10.45, 1 ft in older results) keeps a blank First_Arrival_hr - nothing is
    invented - and its Notes say why and that an older result needs a re-run.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_highway_floor_reg05.py
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsFeature,  # noqa: E402
                       QgsField, QgsGeometry, QgsPointXY, QgsProject, QgsRasterLayer,
                       QgsVectorLayer)
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import structures as ST, classification as CL, linear as LN  # noqa: E402
from DamSafety_Hazard import pipeline as PL  # noqa: E402
from DamSafety_Hazard.helpers import RasterSampler  # noqa: E402
from DamSafety_Hazard.constants import ND_HIGH, ND_MEDIUM, ND_LOW  # noqa: E402
from DamSafety_Hazard.compat import FT_STRING  # noqa: E402

FAILS = []
US_STATE = ("US 2", "U.S. Highway 85", "US-85", "US HWY 52", "ND 23", "ND-1804", "ND HWY 22",
            "State Hwy 22", "State Route 5", "North Dakota Highway 200", "HIGHWAY 23", "Hwy 2",
            "OLD HIGHWAY 10")
LOCAL = ("COUNTY HIGHWAY 12", "Co Hwy 12", "CO. HWY 5", "Township Highway 3", "39TH ST NW",
         "2ND AVE 5", "151ST AVE SE", "County Road 8", "BUS 2", "Highway Ave", "Industrial Rd")
INTERSTATE = ("I-94", "Interstate 29", "I 94")


def check(what, ok, got=""):
    print("{:4} {}{}".format("ok" if ok else "FAIL", what, "" if ok else "  -> {!r}".format(got)))
    if not ok:
        FAILS.append(what)


def _nd_hazard():
    for p in ("DamSafety_Runner", "DamSafety_Screening"):
        f = os.path.join(ROOT, p, "core", "nd_hazard.py")
        if os.path.isfile(f):
            spec = importlib.util.spec_from_file_location("nd_hazard_parity", f)
            m = importlib.util.module_from_spec(spec)
            sys.modules["nd_hazard_parity"] = m
            spec.loader.exec_module(m)
            return m
    return None


def road_classes(texts):
    lyr = QgsVectorLayer("LineString?crs=EPSG:5070", "roads", "memory")
    lyr.dataProvider().addAttributes([QgsField("FULLNAME", FT_STRING)])
    lyr.updateFields()
    rd = LN.AttrReader(lyr)
    out = {}
    for t in texts:
        f = QgsFeature(lyr.fields())
        f.setAttributes([t])
        out[t] = rd.road_class(f.attributes())[0]
    return out


def road(name, rclass, aadt, nd_cls=None):
    cls, basis, judge = LN.nd_road_class(rclass, aadt)
    return {"fid": 1, "name": name, "road_class": rclass, "class_basis": "", "paved": "Yes",
            "paved_basis": "", "bridge": False, "aadt": aadt, "aadt_year": 2024 if aadt else None,
            "aadt_source": "", "nd_class": nd_cls or cls, "nd_basis": basis, "nd_judgment": judge,
            "vehicle_zone": None, "persons": None, "length_ft": 500.0, "max_depth": 0.5,
            "max_vel": 0.5}


def roads_part():
    got = road_classes(US_STATE + LOCAL + INTERSTATE)
    for t in US_STATE:
        check("Hazard reads {!r} as a US / State highway".format(t),
              got[t] in (LN.ROAD_US, LN.ROAD_STATE), got[t])
    for t in LOCAL:
        check("Hazard reads {!r} as a local road".format(t), got[t] == LN.ROAD_OTHER, got[t])
    for t in INTERSTATE:
        check("Hazard reads {!r} as an interstate".format(t), got[t] == LN.ROAD_INTERSTATE, got[t])
    # 5.2.1.2 by road class and AADT
    for rc in (LN.ROAD_US, LN.ROAD_STATE):
        for aadt in (None, 4, 300, 499, 500, 2000):
            cls, basis, _j = LN.nd_road_class(rc, aadt)
            check("{} AADT {} -> Medium citing 5.2.1.2(b)".format(rc, aadt),
                  cls == ND_MEDIUM and "5.2.1.2(b)" in basis, (cls, basis))
        check("{} AADT 8,501 -> High".format(rc), LN.nd_road_class(rc, 8501)[0] == ND_HIGH)
    check("local road AADT 300 -> Low (5.2.1.2(a))",
          LN.nd_road_class(LN.ROAD_OTHER, 300)[0] == ND_LOW)
    check("private road -> Low", LN.nd_road_class(LN.ROAD_PRIVATE, None)[0] == ND_LOW)
    # ND NG9-1-1 roads: SHIELD USHY / STHY (US 83 through a town is 'S WASHINGTON ST')
    lyr = QgsVectorLayer("LineString?crs=EPSG:5070", "ng911", "memory")
    lyr.dataProvider().addAttributes([QgsField("FULL_NAME", FT_STRING), QgsField("SHIELD", FT_STRING),
                                      QgsField("RoadClass", FT_STRING)])
    lyr.updateFields()
    rd = LN.AttrReader(lyr)
    for name, shield, want in (("S WASHINGTON ST", "USHY", LN.ROAD_US),
                               ("MAIN ST", "STHY", LN.ROAD_STATE),
                               ("100TH AVE NE", "CORD", LN.ROAD_OTHER),
                               ("107M AVE SE", "", LN.ROAD_OTHER)):
        f = QgsFeature(lyr.fields())
        f.setAttributes([name, shield, "Primary"])
        got_c = rd.road_class(f.attributes())[0]
        check("NG9-1-1 {!r} SHIELD {!r} RoadClass Primary -> {}".format(name, shield, want),
              got_c == want, got_c)

    # criteria table: the 5.2.1.2(b) row, and the class
    nd = CL.classify_nd([], [road("HIGHWAY 23", LN.ROAD_STATE, 300.0),
                             road("30TH ST SE", LN.ROAD_OTHER, 66.0)], [],
                        structs_evaluated=False)
    row = next((c for c in nd["criteria"] if c["section"] == "5.2.1.2(b)"), None)
    check("criteria table has the 5.2.1.2(b) US / State highway row", row is not None,
          [c["section"] for c in nd["criteria"]])
    if row:
        check("5.2.1.2(b) row: 1 stretch, at least Medium",
              row["found"] == "1 stretch(es)" and row["result"].startswith(ND_MEDIUM), row)
    check("State highway AADT 300 + local road AADT 66 -> Medium", nd["cls"] == ND_MEDIUM,
          nd["cls"])
    nd0 = CL.classify_nd([], [road("30TH ST SE", LN.ROAD_OTHER, 66.0)], [],
                         structs_evaluated=False)
    check("local road AADT 66 alone -> Low", nd0["cls"] == ND_LOW, nd0["cls"])
    row0 = next(c for c in nd0["criteria"] if c["section"] == "5.2.1.2(b)")
    check("5.2.1.2(b) row not met without highways", row0["result"] == "not met", row0)

    # parity with the Runner / Screening rules module
    nh = _nd_hazard()
    if nh is None or not hasattr(nh, "us_state_highway_from_text"):
        print("NOTE Runner / Screening nd_hazard 1.2.0 not next to this plugin: parity skipped")
        return
    for t in US_STATE + LOCAL + INTERSTATE:
        h_us = got[t] in (LN.ROAD_US, LN.ROAD_STATE)
        h_i = got[t] == LN.ROAD_INTERSTATE
        check("parity {!r}: US/State {} / interstate {}".format(t, h_us, h_i),
              (nh.us_state_highway_from_text(t), nh.interstate_from_text(t)) == (h_us, h_i),
              (nh.us_state_highway_from_text(t), nh.interstate_from_text(t)))
    st = nh.Settings()
    for rc in (LN.ROAD_INTERSTATE, LN.ROAD_US, LN.ROAD_STATE, LN.ROAD_PRIVATE, LN.ROAD_OTHER):
        for aadt in (None, 4, 300, 499, 500, 1500, 2000, 2001, 8501):
            if aadt is None and rc == LN.ROAD_OTHER:
                continue        # no AADT on a local road: Hazard asks to verify (Low), the
                # Runner assumes its unknown_aadt (Medium) - a known, separate judgment item
            r = nh.Road("r", 1.0, aadt=aadt, interstate=rc == LN.ROAD_INTERSTATE,
                        private=rc == LN.ROAD_PRIVATE,
                        us_state=rc in (LN.ROAD_US, LN.ROAD_STATE))
            a, b = LN.nd_road_class(rc, aadt)[0], nh.road_class(r, st)[0]
            check("parity {} AADT {}: Hazard {} = Runner/Screening {}".format(rc, aadt, a, b),
                  a == b, (a, b))


def flagged_part():
    os.environ.pop("ALLOW_FLAGGED_STRUCTURE", None)

    class _Run(object):
        def __init__(self):
            self.lines, self.sources = [], []

        def log(self, m):
            self.lines.append(m)
    tmp = tempfile.mkdtemp(prefix="hz_3161_")
    try:
        folder = os.path.join(tmp, "DWR-3161")
        os.makedirs(folder)
        try:
            PL._flagged_structure_gate(_Run(), {"dam_folder": folder})
            check("DWR-3161 refused before any road rule", False, "not refused")
        except RuntimeError as e:
            check("DWR-3161 refused before any road rule (hazard of record Low)",
                  "flagged structure" in str(e) and "Low" in str(e), str(e))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tif(path, arr, nodata=-9999.0):
    from osgeo import gdal
    import numpy as np
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, arr.shape[1], arr.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((0.0, 10.0, 0.0, 100.0, 0.0, -10.0))
    ds.SetProjection(QgsCoordinateReferenceSystem("EPSG:5070").toWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isfinite(arr), arr, nodata).astype("float32"))
    ds = None


def arrival_part():
    import numpy as np
    tmp = tempfile.mkdtemp(prefix="hz_arr_")
    try:
        depth = np.zeros((10, 10)); vel = np.full((10, 10), 0.5); arr = np.full((10, 10), np.nan)
        depth[2, 1] = 0.5                      # structure A: wet, no time (a run at the old 1 ft)
        depth[2, 6], arr[2, 6] = 1.5, 0.8      # structure B: deeper, the raster has a time
        paths = {}
        for k, a in (("depth", depth), ("vel", vel), ("arr", arr)):
            paths[k] = os.path.join(tmp, k + ".tif")
            _tif(paths[k], a)
        samplers = {k: RasterSampler(QgsRasterLayer(p, k)) for k, p in paths.items()}
        check("test rasters open", all(s.ok for s in samplers.values()))

        def rec(i, x0):
            g = QgsGeometry.fromPolygonXY([[QgsPointXY(x0 + 2, 72), QgsPointXY(x0 + 8, 72),
                                            QgsPointXY(x0 + 8, 78), QgsPointXY(x0 + 2, 78)]])
            return {"id": i, "families": {"NSI"}, "fd_id": str(i), "occupancy": "RES1",
                    "fema_category": "Residential", "habitable": "Yes",
                    "acer_type": ST.TYPE_HOUSE, "type_basis": "test", "type_conflict": False,
                    "lifeline": False, "persons": 3, "persons_basis": "test", "area_ft2": 400.0,
                    "category": ST.CAT_DWELLING, "sources": [], "footprint": True, "geom": g}
        structs = [rec(1, 10.0), rec(2, 60.0)]
        crs = QgsCoordinateReferenceSystem("EPSG:5070")
        ST.evaluate_structures(structs, samplers, crs, QgsProject.instance().transformContext(),
                               edge_ft=1.0)
        a, b = structs
        check("A is wet ({} ft) with no arrival".format(a.get("max_depth")),
              a.get("max_depth") and abs(a["max_depth"] - 0.5) < 1e-3 and a["arrival"] is None,
              (a.get("max_depth"), a.get("arrival")))
        check("B has its arrival 0.8 h", b.get("arrival") is not None and
              abs(b["arrival"] - 0.8) < 1e-3, b.get("arrival"))
        na, nb = "; ".join(ST.structure_notes(a)), "; ".join(ST.structure_notes(b))
        check("A's Notes say why First_Arrival_hr is blank (arrival depth not exceeded)",
              "no arrival time" in na and "0.50 ft" in na and "ARRIVAL_FT" in na and
              "0.1 ft by default" in na and "re-run" in na, na)
        check("B's Notes have no arrival note", "no arrival time" not in nb, nb)
        lyr = ST.structures_to_layer(structs, crs)
        rows = {f["Struct_ID"]: f for f in lyr.getFeatures()}
        fa = rows[1]["First_Arrival_hr"]
        check("A's First_Arrival_hr stays blank (nothing invented)",
              fa is None or str(fa) in ("NULL", "") or (hasattr(fa, "isNull") and fa.isNull()),
              fa)
        check("A's Notes column carries the reason", "no arrival time" in str(rows[1]["Notes"]),
              rows[1]["Notes"])
        # no arrival raster at all: no note (nothing to explain per structure)
        s2 = dict(samplers, arr=RasterSampler(None))
        st2 = [rec(3, 10.0)]
        ST.evaluate_structures(st2, s2, crs, QgsProject.instance().transformContext(),
                               edge_ft=1.0)
        check("no arrival raster: no per-structure arrival note",
              "no arrival time" not in "; ".join(ST.structure_notes(st2[0])),
              ST.structure_notes(st2[0]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    roads_part()
    flagged_part()
    arrival_part()
    print("\n{} failed".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    rc = main()
    sys.exit(rc)
