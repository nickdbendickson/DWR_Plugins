import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not _os.environ.get("ND_PMP_DATA_HOME") or not all(_os.path.exists(p) for p in ['medina_pmp', 'dem_watershed_basin.gpkg']):
    print("SKIP: needs ND_PMP_DATA_HOME, medina_pmp/ and dem_watershed_basin.gpkg")
    _sys.exit(77)
import sys, json, glob
from core import spatial as S, snowmelt as M
home = _os.environ.get("ND_PMP_DATA_HOME", ""); B = "dem_watershed_basin.gpkg"
R = {"spatial": {}}
for t in ("Local", "General", "CoolSeason"):
    g = glob.glob("medina_pmp/%s/*.gpkg" % t)[0]
    rs = S.run_spatial(g, B, home, "sd_out/medina_" + t, log=None)
    R["spatial"][t] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()
                        if k in ("pattern", "storm_name", "n_points", "n_points_nodata", "saf_min", "saf_max", "mean_PMP_24", "max_PMP_24", "mean_PMP_06", "max_PMP_06")} for r in rs]
big = sorted(glob.glob("out_two_sub_utm/*/*.gpkg"))
if big:
    rs = S.run_spatial(big[0], "two_sub_utm.shp", home, "sd_out/two_sub", log=None)
    R["spatial"]["two_sub_utm:" + big[0]] = [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()
        if k in ("pattern", "n_points", "n_points_nodata", "saf_min", "saf_max", "mean_PMP_24", "max_PMP_24")} for r in rs]
m = M.run_snowmelt(home, "2026-03-15", "2026-04-15", 3, 0.06, B, "snow_out", sample_xy=(-99.33, 46.88), log=print)
R["snowmelt"] = {k: v for k, v in m.items() if k != "rows"}
R["snowmelt"]["first_rows"] = m["rows"][:6]; R["snowmelt"]["last_row"] = m["rows"][-1]
v = m["rain_window_melt_in"]
R["snowmelt"]["filled"] = M.fill_snowmelt("medina_pmp/hyetographs_snow", v)
json.dump(R, open("sd_snow_results.json", "w"), indent=1, default=str); print(json.dumps(R, indent=1, default=str))
