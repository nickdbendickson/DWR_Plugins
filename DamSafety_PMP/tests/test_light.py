"""Fast checks that need no AWA/SWC data, no HEC-HMS and no QGIS: hand calculations for the
loss / unit-hydrograph code, the DAD lookup semantics, and the hydrograph CSV column contract
that the DamSafety Runner's spillway adequacy tool reads (time_hr, flow_cfs)."""
import os, sys, csv, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from core.hydro import scs_cn_excess, cn_amc3, scs_uh, HydroConfig, run_folder
from core.pmp import dad_lookup

# CN: P=10 in, CN=80 -> S=2.5, Ia=0.5, Q=9.5^2/12
ex, _ = scs_cn_excess(np.array([10.0]), 80)
assert abs(float(ex.sum()) - 9.5 ** 2 / 12.0) < 1e-9
assert abs(cn_amc3(80) - 23 * 80 / (10 + 0.13 * 80)) < 1e-9
# SCS UH: A=10 mi2, lag 3 h, dt 1 h -> Tp 3.5 h, qp = 484*10/3.5; point samples bracket the peak
uh, _t = scs_uh(10, 1.0, 3.0)
assert max(uh) <= 484 * 10 / 3.5 + 1e-6 and max(uh) > 0.8 * 484 * 10 / 3.5
# DAD lookup (synthetic table)
rows = [{"AREASQMI": a, "H_06": y} for a, y in [(1, 10.0), (10, 8.0), (100, 6.0)]]
assert abs(dad_lookup(rows, "06", 55) - 7.0) < 1e-9
assert abs(dad_lookup(rows, "06", 200) - 3.0) < 1e-9
print("OK (hand calcs, DAD lookup)")

# hydrograph CSV contract: a synthetic hyetograph -> run_folder -> first two columns time_hr, flow_cfs
d = tempfile.mkdtemp()
hy = os.path.join(d, "synthetic_6hr.csv")
with open(hy, "w", newline="") as f:
    f.write("time_hr,incremental_in,cumulative_in\n")
    cum = 0.0
    for i, p in enumerate([0.5, 1.0, 3.0, 2.0, 1.0, 0.5], 1):
        cum += p; f.write("%d,%.3f,%.3f\n" % (i, p, cum))
try:
    rows, _sp = run_folder([hy], os.path.join(d, "out"), HydroConfig(cn=80, lag_hr=2.0, area_mi2=10.0), log=None)
except TypeError:
    rows, _sp = run_folder([hy], os.path.join(d, "out"), HydroConfig(cn=80, lag_hr=2.0), log=None)
outs = [os.path.join(dp, f) for dp, _dn, fn in os.walk(os.path.join(d, "out")) for f in fn if f.endswith(".csv")]
heads = []
for p in outs:
    with open(p) as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            heads.append(line.strip().split(",")[:2]); break
assert ["time_hr", "flow_cfs"] in heads, heads
print("OK (hydrograph CSV: time_hr, flow_cfs first)")
