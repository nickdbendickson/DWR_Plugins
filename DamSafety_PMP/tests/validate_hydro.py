"""Validation of core/hydro.py: hand calcs, volume conservation, HEC-HMS published example."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))  # the plugin folder
if not all(_os.path.exists(p) for p in ['NOAA_Dist_C.xlsx', 'out_small_geo/hyetographs']):
    print("SKIP: needs NOAA_Dist_C.xlsx (HEC-HMS published example) and run_core.py outputs (out_small_geo/) in the working folder")
    _sys.exit(77)
import sys, json, os, openpyxl
import numpy as np
from core.hydro import *
R = {}
# 1. CN hand calc: P=10 in, CN=80 -> S=2.5, Ia=0.5, Q=(9.5^2)/(9.5+2.5)=7.520833
ex, info = scs_cn_excess(np.array([10.0]), 80)
R["cn_hand"] = {"code": float(ex.sum()), "hand": 9.5 ** 2 / 12.0}
R["amc3_cn80"] = {"code": cn_amc3(80), "hand": 23 * 80 / (10 + 0.13 * 80)}
# 2. SCS UH hand calc: A=10 mi2, lag=3 h, dt=1 h -> Tp=3.5 h, qp=484*10/3.5=1382.86 cfs/in.
#    With point sampling at t=k*dt the ordinate at t=3 h (t/Tp=0.857) and 4 h (1.143) bracket the peak.
uh, ti = scs_uh(10, 1.0, 3.0)
hand_qp = 484 * 10 / 3.5
hand_at = {k: hand_qp * np.interp(k / 3.5, SCS_DUH_T, SCS_DUH_Q) for k in (1, 2, 3, 4, 5)}
R["scs_uh_hand"] = {"qp_formula": hand_qp, "code_qp_formula": ti["qp_formula_cfs_per_in"],
                    "ordinates_hand_t1..5": [round(v, 2) for v in hand_at.values()],
                    "ordinates_code_raw_t1..5": [round(v * ti["uh_raw_volume_in"], 2) for v in uh[:5]],
                    "raw_volume_in_before_normalisation": ti["uh_raw_volume_in"]}
uh5, ti5 = scs_uh(10, 5 / 60, 3.0)
R["scs_uh_5min_raw_volume_in"] = ti5["uh_raw_volume_in"]
R["scs_uh_5min_peak_vs_formula"] = {"peak": float(uh5.max()), "formula": ti5["qp_formula_cfs_per_in"]}
# 3. Volume conservation on a real nd_pmp hyetograph
h = "out_small_geo/hyetographs/general_24hr_GS_24HR_GENERAL_SYNTHETIC.csv"
hdr, dt, p = read_hyetograph(h)
cons = {}
for name, cfg in {"scs_cn80_scsUH": HydroConfig(cn=80, lag_hr=3),
                  "init_const_clark": HydroConfig(loss="init_const", initial_in=1, constant_in_hr=0.15,
                                                  transform="clark", tc_hr=6, clark_r_hr=4),
                  "scs_amc3_peak25": HydroConfig(cn=70, amc3=True, lag_hr=3, peaking_pct=25),
                  "prf300": HydroConfig(cn=80, lag_hr=3, prf=300)}.items():
    res, t, pp, e, q = compute(hdr, dt, p, cfg)
    cons[name] = {"precip_in": round(res["precip_in"], 4), "excess_in": round(res["excess_in"], 6),
                  "runoff_in_from_flow": round(res["runoff_volume_in"], 6), "peak_cfs": round(res["peak_cfs"], 1),
                  "tpeak_hr": round(res["time_to_peak_hr"], 3), "acft": round(res["runoff_volume_acft"], 1)}
R["volume_conservation_small_geo_GS24_synthetic"] = cons
# 4. HEC-HMS 'Example for Typical NRCS Applications' (v4.10), 100-yr, 8.4 in NOAA Type C, 5-min, no baseflow
wb = openpyxl.load_workbook("NOAA_Dist_C.xlsx", data_only=True); ws = wb["NOAA_C"]
pct = [(r[4], r[5]) for r in ws.iter_rows(min_row=3, max_row=243, values_only=True)]
x = np.array([a for a, b in pct]); y = np.array([b for a, b in pct]) / 100.0
dt = 5 / 60.0
tt = dt * np.arange(0, 24 / dt + 1)
cum = 8.4 * np.interp(tt / 24 * 100, x, y)
precip = np.diff(cum)
hms = {"Area 1": (1.6, 80, 61.2, 2149, 2133), "Area 2": (2.0, 78, 93.6, 1962, 1923), "Area 3": (1.1, 82, 54.0, 1652, 1644),
       "Area 4": (2.2, 72, 93.6, 1894, 1856), "Area 5": (1.2, 79, 46.8, 1861, 1863)}
cmp = {}
for k, (a, cn, lag, q_hms, q_tr20) in hms.items():
    out = {}
    for smp in ("point", "average"):
        res, *_ = compute({"area_mi2": a, "storm_id": k}, dt, precip, HydroConfig(cn=cn, lag_hr=lag / 60, uh_sampling=smp))
        out[smp] = round(res["peak_cfs"], 0)
    cmp[k] = {"ours_point": out["point"], "ours_average": out["average"], "HEC-HMS": q_hms, "WinTR-20": q_tr20,
              "pct_diff_vs_HMS_point": round(100 * (out["point"] - q_hms) / q_hms, 2)}
R["hec_hms_nrcs_example_100yr"] = cmp
json.dump(R, open("validate_hydro.json", "w"), indent=1, default=float)
print(json.dumps(R, indent=1, default=float))
