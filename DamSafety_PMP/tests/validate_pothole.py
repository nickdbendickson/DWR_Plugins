"""Synthetic check of core/pothole.py against an analytic cone, plus sensitivity on the real 3DEP DEM."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
import sys, json, math
import numpy as np
from osgeo import gdal, ogr, osr
from core.pothole import run_pothole, find_wbt
try:
    find_wbt(None)
except FileNotFoundError:
    print("SKIP: WhiteboxTools not found (set WBT_PATH)")
    sys.exit(77)
R = {}
# synthetic 1 m DEM 600x600, EPSG:32614. Flat ring at 100 m for r<=150, sloping away outside, cone pit r=100 depth 2 m
n = 600; c = 1.0
y, x = np.mgrid[0:n, 0:n] + 0.5
r = np.hypot(x - 300, y - 300)
z = np.where(r <= 150, 100.0, 100.0 - 0.01 * (r - 150))
z = np.where(r < 100, 100.0 - 2.0 * (1 - r / 100.0), z)
s = osr.SpatialReference(); s.ImportFromEPSG(32614)
ds = gdal.GetDriverByName("GTiff").Create("syn_dem.tif", n, n, 1, gdal.GDT_Float32)
ds.SetGeoTransform((500000, c, 0, 5200000, 0, -c)); ds.SetProjection(s.ExportToWkt()); ds.GetRasterBand(1).WriteArray(z); ds = None
b = ogr.GetDriverByName("GPKG").CreateDataSource("syn_basin.gpkg"); l = b.CreateLayer("basin", s, ogr.wkbPolygon)
f = ogr.Feature(l.GetLayerDefn()); f.SetGeometry(ogr.CreateGeometryFromWkt(
    "POLYGON((500010 5199410,500590 5199410,500590 5199990,500010 5199990,500010 5199410))")); l.CreateFeature(f); b = None
summ, st = run_pothole("syn_dem.tif", "syn_basin.gpkg", "syn_out", min_depth_m=0.0, min_area_m2=0, log=None)
cone_cells = (r < 100) & (np.abs(x - 300) < 290) & (np.abs(y - 300) < 290)
R["synthetic_cone"] = {"analytic_volume_m3": math.pi * 100 ** 2 * 2 / 3, "analytic_volume_acft": math.pi * 100 ** 2 * 2 / 3 / 1233.48,
                       "code_volume_m3": sum(v["volume_m3"] for v in st.values()), "n_potholes": summ["n_potholes"],
                       "code_max_depth_m": summ["max_pothole_depth_m"], "analytic_max_depth_m": 2.0,
                       "code_area_m2": sum(v["area_m2"] for v in st.values()), "analytic_area_m2": math.pi * 100 ** 2,
                       "spill_elev": [v["spill_elev"] for v in st.values()],
                       "basin_area_m2": summ["basin_area_mi2"] * 2589988.11,
                       "contributing_fraction": summ["contributing_fraction"], "contributing_any_exit_fraction": summ["contributing_any_exit_mi2"] / summ["basin_area_mi2"],
                       "analytic_contrib_fraction_if_only_cone_noncontrib": 1 - math.pi * 100 ** 2 / 580 ** 2}
# sensitivity on real 3DEP DEM (skipped when dem3dep.tif / dem_box_basin.gpkg are not in the working folder)
import os
if not (os.path.exists("dem3dep.tif") and os.path.exists("dem_box_basin.gpkg")):
    print("SKIP part 2: dem3dep.tif / dem_box_basin.gpkg not found")
    json.dump(R, open("validate_pothole.json", "w"), indent=1)
    sys.exit(0)
sens = []
for md, ma in ((0.05, 100), (0.15, 500), (0.3, 1000), (0.5, 4047)):
    s2, _ = run_pothole("dem3dep.tif", "dem_box_basin.gpkg", "pothole_sens", min_depth_m=md, min_area_m2=ma, log=None)
    sens.append({k: round(s2[k], 4) for k in ("n_potholes", "pothole_area_ac", "total_storage_acft",
                                              "total_storage_in_over_basin", "contributing_area_mi2",
                                              "contributing_fraction", "contributing_any_exit_mi2")} | {"min_depth_m": md, "min_area_m2": ma})
R["real_3dep_sensitivity"] = sens
json.dump(R, open("validate_pothole.json", "w"), indent=1)
print(json.dumps(R, indent=1))
