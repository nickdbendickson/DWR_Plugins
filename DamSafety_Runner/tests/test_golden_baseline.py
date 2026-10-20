"""Golden-baseline regression for the deterministic built-in flood engine.

Every other engine test asserts only qualitative invariants (volume conserved, peaks ordered,
the breach adds flooding). A changed coefficient, a units slip or a sign error in the inertial
update would pass all of them while silently shifting the depth / velocity / flooded-area numbers
that feed hazard classification and loss of life. This test freezes those regulatory-facing
numbers for a FIXED synthetic breach run through the real write_inputs -> run -> read_results path
on the built-in engine (pure numpy, deterministic, no scipy / no real LISFLOOD-FP) and fails on
any drift beyond a tight tolerance.

Baseline: tests/data/golden/builtin_engine.json. Regenerate deliberately (after a vetted engine
change) with: python3 tools/regen_golden_builtin.py

The chart lookup and the classification mapping are golden-covered separately by
test_nd_hazard.py (fixed zone assertions + nd_hazard_shipped_charts_are_signed_off).
"""
import json
import os
import tempfile

import numpy as np

from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core.breach import Hydrograph
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "golden",
                      "builtin_engine.json")


def run_fixed_case():
    """The fixed synthetic breach, run on the built-in engine. Returns (scalars, raster_agg)."""
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    st = lf.LisfloodSettings(lf.BUILTIN_EXE, arrival_depth_m=0.3, sim_time_s=1200.0,
                             massint_s=60.0, initial_tstep_s=1.0)
    wd = tempfile.mkdtemp(prefix="golden_")
    par = lf.write_inputs(wd, dem, (30.0, nr * dx / 2 + 5), hy, st)
    lf.run(wd, par, st)
    env = lf.read_results(wd, dem, st, hy)
    s = getattr(env, "statistics", None) or getattr(env, "stats")
    keep = ("cells", "cell_size_m", "sim_time_hr", "peak_inflow_cfs", "injected_acft",
            "in_domain_acft", "flooded_acres", "max_depth_ft", "max_velocity_fps",
            "max_volume_error_pct")
    scalars = {k: s.get(k) for k in keep}
    d = env.depth.data
    wet = np.isfinite(d) & (d > 0)
    raster = {"wet_cells": int(wet.sum()),
              "max_depth_m": float(np.nanmax(np.where(wet, d, np.nan))) if wet.any() else 0.0,
              "sum_depth_m": float(np.nansum(np.where(wet, d, 0.0)))}
    return scalars, raster


@test
def golden_builtin_engine_regulatory_numbers():
    with open(GOLDEN, encoding="utf-8") as f:
        golden = json.load(f)
    scalars, raster = run_fixed_case()
    # exact for discrete quantities (a change here is a structural change, never fp noise)
    for k in ("cells", "cell_size_m"):
        assert scalars[k] == golden["scalars"][k], (k, scalars[k], golden["scalars"][k])
    assert raster["wet_cells"] == golden["raster"]["wet_cells"], (
        "wet_cells", raster["wet_cells"], golden["raster"]["wet_cells"])
    # mass must still be conserved
    assert abs(scalars["max_volume_error_pct"]) <= 0.1, scalars["max_volume_error_pct"]
    # regulatory scalars: tight relative tolerance (catches coefficient / units / sign drift;
    # tolerates only trivial last-digit fp noise across platforms)
    for k in ("sim_time_hr", "peak_inflow_cfs", "injected_acft", "in_domain_acft",
              "flooded_acres", "max_depth_ft", "max_velocity_fps"):
        got, want = float(scalars[k]), float(golden["scalars"][k])
        assert abs(got - want) <= 1e-3 * max(abs(want), 1.0), (k, got, want)
    for k in ("max_depth_m", "sum_depth_m"):
        got, want = float(raster[k]), float(golden["raster"][k])
        assert abs(got - want) <= 1e-3 * max(abs(want), 1.0), (k, got, want)
