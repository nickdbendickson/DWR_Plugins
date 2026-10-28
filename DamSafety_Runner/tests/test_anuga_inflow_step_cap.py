"""3.10.94 (DWR-3034 REVIEW 9.2, G5): ANUGA's step is capped at the hydrograph interval while
the breach hydrograph flows (core/anuga_worker.inflow_max_timestep / InflowStepCap).

On a dry domain ANUGA has no CFL limit, so its first step was the whole yield step (300 s on
DWR-3034) and Inlet_operator's 0.5*(Q(t)+Q(t+dt))*dt booked 9,994 m3 for the first 300 s
against the hydrograph's 5,929 m3 (G5 ratio 1.0461). The unit tests check the cap rule and
the release after the hydrograph ends; the model test (--models, NDBREACH_ANUGA_PYTHON) runs
the real worker on a ~400-triangle bowl (nothing leaves) with a convex rising limb sampled
every 2 s and a 300 s yield step: the water in the domain equals the hydrograph volume
within 0.5 % with the cap, and the uncapped run (NDBREACH_ANUGA_INFLOW_MAX_DT=0) shows the
old over-injection."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.tests.registry import test, model_test  # noqa: E402


def _hyd():
    """Convex rise to 60 m3/s at 300 s (Q ~ t^3, like a breach's rising limb), linear fall to
    0 at 900 s, sampled every 2 s. Volume 4,500 + 18,000 = 22,500 m3."""
    t = np.arange(0.0, 900.0 + 1e-9, 2.0)
    q = np.where(t <= 300.0, 60.0 * (t / 300.0) ** 3, 60.0 * (900.0 - t) / 600.0)
    return t, np.clip(q, 0.0, None)


@test
def anuga_inflow_cap_rule():
    t, q = _hyd()
    assert abs(AW.inflow_max_timestep(t, q, 300.0, {}) - 2.0) < 1e-9
    # DWR-3034: 404 points at 2.14 s, yield step 300 s -> 2.14 s
    t34 = np.arange(404) * 2.14
    assert abs(AW.inflow_max_timestep(t34, np.ones(404), 300.0, {}) - 2.14) < 1e-9
    # a coarse 4-point hydrograph: 1/100 of its duration (18 s), not the 300+ s spacing
    assert abs(AW.inflow_max_timestep([0., 300., 900., 1800.], [0., 80., 20., 0.], 60.0, {})
               - 18.0) < 1e-9
    # never above the yield step, never below the 0.5 s floor
    assert AW.inflow_max_timestep([0., 300., 900., 1800.], [0., 80., 20., 0.], 5.0, {}) == 5.0
    tf = np.arange(0.0, 10.0, 0.01)
    assert AW.inflow_max_timestep(tf, np.ones_like(tf), 60.0, {}) == AW.INFLOW_MAX_DT_FLOOR_S
    # override / off; no inflow = no cap
    assert AW.inflow_max_timestep(t, q, 300.0, {AW.INFLOW_MAX_DT_ENV: "1.5"}) == 1.5
    for off in ("0", "off", "-1"):
        assert AW.inflow_max_timestep(t, q, 300.0, {AW.INFLOW_MAX_DT_ENV: off}) is None, off
    assert AW.inflow_max_timestep(t, np.zeros_like(t), 300.0, {}) is None
    assert AW.inflow_end_time(t, q) == 900.0
    assert AW.inflow_end_time([0., 10., 20., 30.], [0., 5., 0., 0.]) == 20.0


class _Dom:
    def __init__(self, t=0.0, mx=1000.0):
        self.t, self.evolve_max_timestep = t, mx

    def get_time(self):
        return self.t


@test
def anuga_inflow_cap_held_then_released():
    d = _Dom()
    c = AW.InflowStepCap(d, 2.0, 900.0)
    assert c.active and d.evolve_max_timestep == 2.0
    for tt in (0.0, 300.0, 600.0):
        c.update(d, tt)
        assert d.evolve_max_timestep == 2.0
    c.update(d, 900.0)
    assert not c.active and d.evolve_max_timestep == 1000.0 and c.released_t == 900.0
    rec = c.record()
    assert rec["applied"] and rec["max_timestep_s"] == 2.0 and rec["released_at_s"] == 900.0
    # no cap / resumed after the hydrograph ended / a tighter limit already set: untouched
    for d2, cap in ((_Dom(), None), (_Dom(t=1200.0), 2.0), (_Dom(mx=1.0), 2.0)):
        old = d2.evolve_max_timestep
        c2 = AW.InflowStepCap(d2, cap, 900.0)
        assert not c2.active and d2.evolve_max_timestep == old
        assert not c2.record()["applied"]


def _job(path):
    ny, nx, dx = 30, 50, 20.0
    x = (np.arange(nx) + 0.5) * dx
    y = (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(x, y)
    dem = 100.0 + 1e-4 * ((X - 500.0) ** 2 + (Y - 300.0) ** 2)     # a bowl: nothing leaves
    x0, y0 = 500000.0, 5200600.0
    b = np.array([[x0 + 10, y0 - 590], [x0 + 990, y0 - 590], [x0 + 990, y0 - 10],
                  [x0 + 10, y0 - 10]])
    t, q = _hyd()
    np.savez(path, dem=dem.astype(np.float32), dem_x0=x0, dem_y0=y0, dem_dx=dx, manning_n=0.05,
             manning=np.zeros(0), boundary=b, fine=np.zeros((0, 2)),
             inlet=np.array([[x0 + 500, y0 - 270], [x0 + 500, y0 - 330]]),
             fine_area=500.0, coarse_area=1500.0, hyd_t=t, hyd_q=q,
             sim_time=900.0, yieldstep=300.0, arrival_depth=0.3048, velocity_min_depth=0.03,
             algorithm="DE0", threads=1, out_x0=x0, out_y0=y0, out_dx=dx, out_nx=nx, out_ny=ny)


def _run(py, d, cap_env):
    _job(os.path.join(d, "j.npz"))
    env = dict(AM._clean_env(), OMP_NUM_THREADS="1")
    env.pop(AW.INFLOW_MAX_DT_ENV, None)
    if cap_env is not None:
        env[AW.INFLOW_MAX_DT_ENV] = cap_env
    subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d, env=env, check=True,
                   stdout=subprocess.DEVNULL, timeout=600)
    with np.load(os.path.join(d, "r.npz")) as z:
        return json.loads(str(z["stats"]))


@model_test
def anuga_inflow_cap_real_anuga_volume_matches_hydrograph():
    py = os.environ.get("NDBREACH_ANUGA_PYTHON")
    if not (py and os.path.isfile(py)):
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    t, q = _hyd()
    want = float(np.trapezoid(q, t)) if hasattr(np, "trapezoid") else float(np.trapz(q, t))
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        s = _run(py, d0, None)
        vb = s["volume_balance"]
        assert vb["inflow_step_cap"]["applied"] and vb["inflow_step_cap"]["max_timestep_s"] == 2.0
        assert abs(s["injected_m3"] - want) < 1.0, (s["injected_m3"], want)
        assert s["max_edge_depth_m"] < 1e-6, s["max_edge_depth_m"]
        r = s["volume_in_domain_m3"] / want
        assert abs(r - 1.0) < 0.005, (r, vb)
        if vb.get("operators_m3") is not None:          # what the inlet actually added
            assert abs(vb["operators_m3"] / want - 1.0) < 0.005, vb
        # the old behaviour: one 300 s first step books 0.5*Q(300)*300 = 9,000 m3 for 4,500
        s0 = _run(py, d1, "0")
        r0 = s0["volume_in_domain_m3"] / want
        assert not s0["volume_balance"]["inflow_step_cap"]["applied"]
        assert r0 > 1.10, r0
        print("inflow cap: ratio {:.4f} capped vs {:.4f} uncapped".format(r, r0))
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)
