import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not all(_os.path.exists(p) for p in ['medina_pmp/hyetographs', 'pothole_ws/pothole_network.json']):
    print("SKIP: needs medina_pmp/hyetographs and pothole_ws/pothole_network.json in the working folder")
    _sys.exit(77)
import sys, json, glob, numpy as np
from core.hydro import HydroConfig, run_folder
from core import cascade as C
hy = ["medina_pmp/hyetographs/general_72hr_GS_72HR_GENERAL_SYNTHETIC.csv",
      "medina_pmp/hyetographs/local_06hr_LS_6HR_SYNTHETIC.csv",
      "medina_pmp/hyetographs/coolseason_72hr_CS_72HR_GENERAL_SYNTHETIC.csv"]
net = "pothole_ws/pothole_network.json"
R = {}
for mode, extra in (("none", {}), ("abstraction", {"pothole_network": net}), ("cascade", {"pothole_network": net}),
                    ("cascade_50pct_full", {"pothole_network": net, "pothole_initial_full": 0.5})):
    cfg = HydroConfig(cn=80, lag_hr=2.0, pothole_mode=mode.split("_")[0], **extra)
    rows, sp = run_folder(hy, "cascade_test/" + mode, cfg, log=None)
    R[mode] = {r["storm_id"]: {k: (round(r[k], 3) if isinstance(r.get(k), float) else r.get(k)) for k in
               ("precip_in", "excess_before_potholes_in", "excess_in", "runoff_volume_in", "peak_cfs", "time_to_peak_hr",
                "pothole_capacity_in", "pothole_stored_end_in", "pothole_min_contrib_fraction",
                "pothole_max_contrib_fraction")} for r in rows}
# mass balance check on one cascade series
import csv
f = "cascade_test/cascade/pothole_cascade/general_72hr_GS_72HR_GENERAL_SYNTHETIC.csv"
rows = [r for r in csv.reader(l for l in open(f) if not l.startswith("#"))][1:]
a = np.array([[float(x) for x in r] for r in rows])
R["mass_balance_general72"] = {"sum_excess_in": float(a[:, 1].sum()), "sum_outlet_in": float(a[:, 2].sum()),
                               "end_stored_acft": float(a[-1, 4]),
                               "end_stored_in": float(a[-1, 4] * 1233.48 / (3.3 * 2589988.11) / 0.0254),
                               "first_time_contrib_gt_50pct_hr": float(a[np.argmax(a[:, 6] > 0.5), 0]) if (a[:, 6] > 0.5).any() else None,
                               "contrib_fraction_at_24h": float(a[np.argmin(abs(a[:, 0] - 24)), 6]),
                               "contrib_fraction_end": float(a[-1, 6])}
json.dump(R, open("cascade_results.json", "w"), indent=1)
print(json.dumps(R, indent=1))
