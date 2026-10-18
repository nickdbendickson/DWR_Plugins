"""Independent checks of the core (no ArcGIS available - these are NOT ArcGIS parity tests)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not _os.environ.get("ND_PMP_DATA_HOME"):
    print("SKIP: needs ND_PMP_DATA_HOME and run_core.py outputs (out_*/)")
    _sys.exit(77)
import sys, glob, csv, json
import numpy as np
from osgeo import ogr, osr, gdal
gdal.UseExceptions()
from core.pmp import dad_lookup, py2round, read_dad
H = _os.environ.get("ND_PMP_DATA_HOME", "")
res = {}
# 1. dad_lookup semantics (synthetic table)
rows = [{"AREASQMI": a, "H_06": y} for a, y in [(1, 10.0), (10, 8.0), (100, 6.0)]]
res["dad_interp_55"] = dad_lookup(rows, "06", 55)          # expect 8 + 45*(-2)/90 = 7.0
res["dad_below_min_0.5"] = dad_lookup(rows, "06", 0.5)     # extrapolate with first segment: 10 + (-0.5)*(-2/9)
res["dad_above_max_200"] = dad_lookup(rows, "06", 200)     # original: x1/area*y1 = 100/200*6 = 3.0
res["dad_missing_dur"] = dad_lookup(rows, "48", 50)        # None
res["py2round"] = [py2round(0.25, 1), py2round(2.5, 0), py2round(1.0005, 3), round(2.5)]
# 2. Grid_Points Id == Id of the Vector_Grid cell that contains it (assumed by the original AddJoin on ID)
ds = ogr.Open(H + "/Input/Non_Storm_Data.gdb")
gp = ds.GetLayerByName("Grid_Points"); vg = ds.GetLayerByName("Vector_Grid")
cells = {int(f.GetField("Id")): f.GetGeometryRef().Centroid() for f in vg}
bad = 0; n = 0
for f in gp:
    g = f.GetGeometryRef(); c = cells.get(int(f.GetField("Id")))
    n += 1
    if c is None or abs(c.GetX() - g.GetX()) > 1e-6 or abs(c.GetY() - g.GetY()) > 1e-6:
        bad += 1
res["grid_id_join_check"] = {"points": n, "cells": len(cells), "mismatched": bad}
# 3. Independent per-point recompute (np.interp on DAD, brute force over storms) vs the GPKG output
def indep(stype, area, ids, dur):
    sds = gdal.OpenEx(H + "/Input/Storm_Adj_Factors.gdb", gdal.OF_VECTOR)
    names = sds.GetRootGroup().OpenGroup(stype).GetVectorLayerNames()
    best = {i: 0.0 for i in ids}
    for s in names:
        t = read_dad(H, s)
        if "H_" + dur not in t[0] or any(r["H_" + dur] is None for r in t):
            continue
        a = np.array([r["AREASQMI"] for r in t]); y = np.array([r["H_" + dur] for r in t])
        assert a.min() < area < a.max()
        d = py2round(float(np.interp(area, a, y)), 3)
        ads = ogr.Open(H + "/Input/Storm_Adj_Factors.gdb"); lyr = ads.GetLayerByName(s)
        lyr.SetAttributeFilter("ID >= %d AND ID <= %d" % (min(ids), max(ids)))
        for f in lyr:
            i = int(f.GetField("ID"))
            if i in best and f.GetField("TRANS") == 1:
                best[i] = max(best[i], py2round(d * f.GetField("TAF"), 1))
    return best
cmp = {}
for basin, area in (("small_geo", 103.4), ("two_sub_utm", 387.1)):
    for stype, dur in (("General", "24"), ("Local", "06"), ("CoolSeason", "72")):
        g = glob.glob("out_%s/%s/PMP_*.gpkg" % (basin, stype))[0]
        gds = ogr.Open(g); lyr = gds.GetLayer(0)
        mine = {int(f.GetField("ID")): f.GetField("PMP_" + dur) for f in lyr}
        ind = indep(stype, area, list(mine), dur)
        diff = [abs(mine[i] - ind[i]) for i in mine]
        cmp["%s/%s/PMP_%s" % (basin, stype, dur)] = {"points": len(mine), "max_abs_diff": max(diff),
                                                    "min": min(mine.values()), "max": max(mine.values())}
res["independent_recompute"] = cmp
# 4. Area: planar ESRI:102039 (tool logic) vs OGR geodesic area
for b in ("small_geo", "two_sub_utm"):
    bds = ogr.Open(b + ".shp"); l = bds.GetLayer(0)
    res["geodesic_area_sqmi_" + b] = round(sum(f.GetGeometryRef().GeodesicArea() for f in l) * 0.000000386102, 3)
# 5. Hyetograph invariants
inv = []
for b in ("small_geo", "two_sub_utm"):
    for p in sorted(glob.glob("out_%s/hyetographs/*.csv" % b)):
        hdr = {}; data = []
        for line in open(p):
            if line.startswith("#"):
                k, _, v = line[2:].partition(":"); hdr[k.strip()] = v.strip()
            elif not line.startswith("time_hr"):
                data.append([float(x) for x in line.strip().split(",")])
        a = np.array(data)
        inv.append({"file": p, "rows": len(a), "final_cum": a[-1, 2], "basin_avg": float(hdr["basin_avg_depth_in"]),
                    "end_hr": a[-1, 0], "dur": int(hdr["duration_hr"]), "neg_incr": int((a[:, 1] < -1e-9).sum()),
                    "sum_incr_eq_cum": bool(abs(a[:, 1].sum() - a[-1, 2]) < 1e-4)})
res["hyetographs"] = {"count": len(inv),
                      "end_time_equals_duration": all(abs(i["end_hr"] - i["dur"]) < 1e-6 for i in inv),
                      "sum_incr_equals_final_cum": all(i["sum_incr_eq_cum"] for i in inv),
                      "files_with_negative_increments": [i["file"] for i in inv if i["neg_incr"]],
                      "max_abs(final_cum - basin_avg_for_duration)": max(abs(i["final_cum"] - i["basin_avg"]) for i in inv
                                                                            if i["dur"] <= 24),
                      "48/72hr final vs basin_avg": [(i["file"].split("/")[-1], i["final_cum"], i["basin_avg"]) for i in inv
                                                     if i["dur"] > 24 and "SYNTHETIC" in i["file"]]}
json.dump(res, open("verify_results.json", "w"), indent=1, default=str)
print(json.dumps(res, indent=1, default=str))
