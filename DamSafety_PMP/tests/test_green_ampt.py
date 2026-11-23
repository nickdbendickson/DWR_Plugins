"""Green-Ampt loss (core/hydro.py green_ampt_excess, PMP 0.7.0) against an independent fine-step
integration of the Green-Ampt rate law f = min(i, Ks (1 + psi*dtheta / F)), plus mass balance, the
limits (no ponding below Ks, Ks = 0, all impervious, initial loss), the PRODUCTION / PROVISIONAL
stamp of the texture table and the table itself against two USACE reprints. No data needed."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from core import hydro as H


def reference(precip_incr, dt, ks, psi, dth, sub=4000):
    """Cumulative infiltration per step by explicit integration with `sub` substeps per step: a
    separate route from the ponding-time + implicit-equation scheme under test."""
    pd = psi * dth
    F, out = 0.0, []
    for p in precip_incr:
        i = p / dt
        h = dt / sub
        F0 = F
        for _ in range(sub):
            cap = np.inf if F <= 0 else ks * (1.0 + pd / F)
            F += min(i, cap) * h
        out.append(F - F0)
    return np.array(out)


# 1. a storm that ponds part way through, on coarse steps, against the fine reference
dt = 0.25
p = np.array([0.05, 0.2, 0.5, 0.9, 0.6, 0.3, 0.1, 0.05, 0.0, 0.4]) * dt * 4   # in per step
ks, psi, dth = 0.15, 6.0, 0.30
ex, info = H.green_ampt_excess(p, dt, ks, psi, dth)
ref_inf = reference(p, dt, ks, psi, dth)
assert np.allclose(p - ex, ref_inf, atol=2e-4), (p - ex, ref_inf)
assert info["ga_ponded"]
# mass balance
assert abs(p.sum() - ex.sum() - info["ga_infiltrated_in"]) < 1e-9

# 2. analytic ponding time for steady rain i > Ks: tp = Ks psi dth / (i (i - Ks)) (Chow et al. eq. 4.4.3)
i = 1.0
tp = ks * psi * dth / (i * (i - ks))
fine = 1.0 / 600
n = int(round(1.0 / fine))
ex1, _ = H.green_ampt_excess(np.full(n, i * fine), fine, ks, psi, dth)
first = np.argmax(ex1 > 1e-9) * fine
assert abs(first - tp) <= fine + 1e-9, (first, tp)

# 3. rain no faster than Ks never ponds
ex2, info2 = H.green_ampt_excess(np.full(40, 0.1 * 0.25), 0.25, 0.1, psi, dth)
assert np.allclose(ex2, 0.0) and not info2["ga_ponded"]

# 4. Ks = 0: everything after the initial loss runs off; all impervious: everything runs off
ex3, _ = H.green_ampt_excess(np.full(8, 0.2), 0.5, 0.0, psi, dth, initial_in=0.5)
assert abs(ex3.sum() - (1.6 - 0.5)) < 1e-12 and abs(ex3[0]) < 1e-12 and abs(ex3[2] - 0.1) < 1e-12
ex4, _ = H.green_ampt_excess(p, dt, ks, psi, dth, impervious_pct=100.0)
assert np.allclose(ex4, p)

# 5. half impervious = mean of the two
ex5, _ = H.green_ampt_excess(p, dt, ks, psi, dth, impervious_pct=50.0)
assert np.allclose(ex5, 0.5 * p + 0.5 * ex)

# 6. no suction (psi*dtheta = 0) = constant loss at Ks
ex6, _ = H.green_ampt_excess(p, dt, ks, 0.0, dth)
assert np.allclose(ex6, np.maximum(p - ks * dt, 0.0))

# 7. texture preset: deficit from Se, unit conversion, PRODUCTION once the table is checked
psi_l, ks_l, dth_l = H.ga_texture("Loam", 0.25)
assert abs(psi_l - 8.89 / 2.54) < 1e-12 and abs(ks_l - 0.34 / 2.54) < 1e-12 and abs(dth_l - 0.434 * 0.75) < 1e-12
cfg = H.HydroConfig(loss="green_ampt", ga_texture="silt loam", ga_initial_saturation=0.3, lag_hr=2.0)
res, t, pp, e, q = H.compute({"area_mi2": "10", "storm_id": "x"}, dt, p, cfg)
assert res["loss"] == "green_ampt" and res["loss_texture"] == "silt loam"
assert res["loss_param_status"] == ("PRODUCTION" if H.GA_TEXTURE_VERIFIED else "PROVISIONAL")
assert H.GA_TEXTURE_VERIFIED is True
# 8. the table against two USACE reprints of Rawls et al. (1983) (checked 2026-11-22):
# HEC-RAS 1D Technical Reference, "Green-Ampt": total porosity, Ks (cm/hr), to the printed digits
RAS = {"sand": (0.437, 11.78), "loamy sand": (0.437, 2.99), "sandy loam": (0.453, 1.09), "loam": (0.463, 0.34),
       "silt loam": (0.501, 0.65), "sandy clay loam": (0.398, 0.15), "clay loam": (0.464, 0.10),
       "silty clay loam": (0.471, 0.10), "sandy clay": (0.430, 0.06), "silty clay": (0.479, 0.05),
       "clay": (0.475, 0.03)}
# HEC-HMS guide, "Applying the Green and Ampt Loss Method": effective porosity, suction (in), Ks (in/hr), rounded
HMS = {"sand": (0.42, 1.9, 4.6), "loamy sand": (0.40, 2.4, 1.2), "sandy loam": (0.41, 4.3, 0.4),
       "loam": (0.43, 3.5, 0.1), "silt loam": (0.49, 6.6, 0.3), "sandy clay loam": (0.33, 8.6, 0.06),
       "clay loam": (0.31, 8.2, 0.04), "silty clay loam": (0.43, 10.7, 0.04), "sandy clay": (0.32, 9.4, 0.02),
       "silty clay": (0.42, 11.5, 0.02), "clay": (0.39, 12.5, 0.01)}
assert set(H.GA_TEXTURES) == set(RAS) == set(HMS)
for name, (psi_t, ks_t, por_t, te_t) in H.GA_TEXTURES.items():
    por_r, ks_r = RAS[name]
    te_h, psi_h, ks_h = HMS[name]
    assert por_t == por_r and abs(ks_t * 2.54 - ks_r) < 1e-9, name
    assert abs(te_t - te_h) <= 0.005 + 1e-12 and abs(psi_t - psi_h) <= 0.05 + 1e-12, name
    ks_digits = 1 if ks_h >= 0.1 else 2
    assert round(ks_t, ks_digits) == ks_h, name
assert 0 < res["excess_in"] < p.sum()
cfg = H.HydroConfig(loss="green_ampt", ga_ks_in_hr=ks, ga_suction_in=psi, ga_moisture_deficit=dth, lag_hr=2.0)
res, *_ = H.compute({"area_mi2": "10", "storm_id": "x"}, dt, p, cfg)
assert res["loss_texture"] == "custom" and "loss_param_status" not in res
assert abs(res["excess_in"] - ex.sum()) < 1e-12
try:
    H.ga_texture("peat")
    raise AssertionError("unknown texture accepted")
except ValueError:
    pass
print("OK (Green-Ampt loss: matches fine-step integration, ponding time, limits, checked texture table)")
