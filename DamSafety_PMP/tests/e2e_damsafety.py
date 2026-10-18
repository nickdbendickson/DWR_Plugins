"""PMP -> ndpmp hydrograph CSV -> DamSafety_Runner core.flood.route_flood (read-only import) -> freeboard.
HYPOTHETICAL dam (no real dam data for the Medina test basin)."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not all(_os.path.exists(p) for p in ['medina_pmp/hyetographs']):
    print("SKIP: needs medina_pmp/hyetographs in the working folder")
    _sys.exit(77)
import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # repo root (DamSafety_Runner)
from DamSafety_Runner.core import flood as F, breach as B
dam_kw = dict(dam_id="HYPOTHETICAL_MEDINA", dam_height_ft=30.0, storage_top_acft=1500.0, top_of_dam_ft=30.0,
              surface_area_top_ac=120.0, crest_length_ft=800.0)
spill = "e2e/spillway_rating.csv"; os.makedirs("e2e", exist_ok=True)
with open(spill, "w") as f:   # 100-ft broad-crested weir, crest 20 ft, C=3.0
    f.write("elevation_ft,discharge_cfs\n")
    for i in range(0, 41):
        z = 20 + 0.25 * i; f.write("%.2f,%.2f\n" % (z, 3.0 * 100 * max(z - 20, 0) ** 1.5))
out = {}
for mode in ("none", "cascade"):
    for st in ("local_06hr_LS_6HR_SYNTHETIC", "general_72hr_GS_72HR_GENERAL_SYNTHETIC"):
        csv = "cascade_test/%s/%s_hydrograph.csv" % (mode, st)
        if not os.path.exists(csv):
            import glob; csv = glob.glob("cascade_test/%s/hydrographs/%s*.csv" % (mode, st))[0]
        fi = F.FloodInputs.from_files(csv, spill, normal_pool_ft=20.0, crest_length_ft=800.0)
        dam = B.DamData(**dam_kw)
        r = F.route_flood(dam, B.METHOD_OVERTOPPING, None, fi)
        s = r.summary
        pk = s.get("peak_pool_no_breach_ft", s.get("peak_pool_ft_no_breach"))
        out[mode + "/" + st] = {"inflow_csv": csv, "inflow_peak_cfs_read": round(float(fi.inflow_q_cms.max() / F.CFS), 1),
                                "summary": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()},
                                "freeboard_ft": None if pk is None else round(30.0 - pk, 3),
                                "notes": getattr(r, "notes", [])[:6]}
json.dump(out, open("e2e/e2e_results.json", "w"), indent=1, default=str)
print(json.dumps(out, indent=1, default=str))
