import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not _os.environ.get("ND_PMP_DATA_HOME") or not all(_os.path.exists(p) for p in ['small_geo.shp', 'two_sub_utm.shp']):
    print("SKIP: needs ND_PMP_DATA_HOME (the PMP_Evaluation_Tool folder) and small_geo.shp/two_sub_utm.shp in the working folder")
    _sys.exit(77)
import sys, json, time
from core import run_pmp, PMPConfig
HOME = _os.environ.get("ND_PMP_DATA_HOME", "")
out = {}
for name, sub in [("small_geo", None), ("two_sub_utm", "SUB_ID")]:
    t = time.time()
    r = run_pmp(PMPConfig(basin_path=f"{name}.shp", data_home=HOME, out_dir=f"out_{name}", subbasin_field=sub),
                log=lambda m: None)
    out[name] = {"secs": round(time.time()-t,1), "area_raw": r["area_sqmi_raw"], "area": r["area_sqmi"],
                 "types": {k: {"n": v["n_points"], "avg": v["basin_average"], "sub": v["subbasin_average"],
                               "ctrl": v["controlling_storms"]} for k, v in r["types"].items()}}
json.dump(out, open("core_results.json", "w"), indent=1)
print(json.dumps(out, indent=1))
