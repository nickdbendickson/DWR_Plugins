import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not _os.environ.get("ND_PMP_HMS_HOME") or not all(_os.path.exists(p) for p in ['medina_pmp/hyetographs', 'pothole_ws/pothole_network.json']):
    print("SKIP: needs ND_PMP_HMS_HOME (HEC-HMS install), medina_pmp/hyetographs and pothole_ws/")
    _sys.exit(77)
import sys, json, glob, os, shutil
from core.hydro import HydroConfig, run_folder
from core.hms import export_hms, run_hms
case = sys.argv[1] if len(sys.argv) > 1 else "scs"
hy = ["medina_pmp/hyetographs/local_06hr_LS_6HR_SYNTHETIC.csv", "medina_pmp/hyetographs/general_72hr_GS_72HR_GENERAL_SYNTHETIC.csv",
      "medina_pmp/hyetographs/coolseason_72hr_CS_72HR_GENERAL_SYNTHETIC.csv"]
cfgs = {"scs": HydroConfig(cn=80, lag_hr=2.0),
        "clark_ic": HydroConfig(loss="init_const", initial_in=1.0, constant_in_hr=0.1, transform="clark", tc_hr=3.0, clark_r_hr=2.0),
        "scs_abs": HydroConfig(cn=80, lag_hr=2.0, pothole_mode="abstraction", pothole_network="pothole_ws/pothole_network.json")}
cfg = cfgs[case]
od = "hms_out/" + case; shutil.rmtree(od, ignore_errors=True)
ex = export_hms(hy, cfg, od, project="NDPMP_" + case)
rows, _ = run_folder(hy, od + "_ndpmp", cfg, log=None)
rc, so, se, pk = run_hms(od)
ours = {r["storm_id"]: r for r in rows}
cmp = []
for p in pk:
    o = [r for k, r in ours.items() if k.startswith(p["storm_id"][:30]) or p["storm_id"].startswith(k[:30])]
    o = o[0] if o else None
    d = {"storm": p["storm_id"], "hms_peak_cfs": p["hms_peak_cfs"], "hms_tp_hr": p["hms_time_to_peak_hr"], "status": p["status"]}
    if o and p["hms_peak_cfs"]:
        d.update(ndpmp_peak_cfs=round(o["peak_cfs"], 2), ndpmp_tp_hr=o["time_to_peak_hr"],
                 diff_pct=round(100 * (o["peak_cfs"] - float(p["hms_peak_cfs"])) / float(p["hms_peak_cfs"]), 3))
    cmp.append(d)
res = {"case": case, "rc": rc, "warnings": ex["warnings"], "compare": cmp, "stdout_tail": so[-1500:], "stderr_tail": se[-1500:]}
json.dump(res, open("hms_out/%s_compare.json" % case, "w"), indent=1); print(json.dumps(res, indent=1))
