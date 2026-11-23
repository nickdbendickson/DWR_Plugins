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


def _build_fixed_deck(wd):
    """Write the fixed synthetic breach deck into wd and return (dem, par, st, hy). Deterministic:
    a 20 x 100, 20 m-cell sloped prairie DEM, a fixed triangular 30 cms breach hydrograph, the
    built-in engine, 1200 s of sim at a 1 s initial step. Shared by the golden-number test and the
    byte-identical repeat-run guard so both exercise exactly the same deck."""
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    # manning_n pinned to 0.06: this golden froze regulatory numbers on the pre-3.27.0
    # uniform-n fallback; 3.74.1 moved the leftover constructor default to 0.05.
    st = lf.LisfloodSettings(lf.BUILTIN_EXE, arrival_depth_m=0.3, sim_time_s=1200.0,
                             massint_s=60.0, initial_tstep_s=1.0, manning_n=0.06)
    par = lf.write_inputs(wd, dem, (30.0, nr * dx / 2 + 5), hy, st)
    return dem, par, st, hy


def run_fixed_case():
    """The fixed synthetic breach, run on the built-in engine. Returns (scalars, raster_agg)."""
    wd = tempfile.mkdtemp(prefix="golden_")
    dem, par, st, hy = _build_fixed_deck(wd)
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


# S5: byte-identical repeat-run determinism guard for the built-in engine.
_REPEAT_FILES = ("run.max", "run.maxVc", "run.inittm", "run.maxtm", "run.mxe", "run.mass")


def _result_bytes(wd):
    """Raw bytes of every result file the built-in engine writes for the fixed deck."""
    res = os.path.join(wd, "results")
    out = {}
    for fname in _REPEAT_FILES:
        with open(os.path.join(res, fname), "rb") as f:
            out[fname] = f.read()
    return out


@test
def builtin_engine_repeat_run_is_byte_identical():
    """The built-in engine is pure numpy, single-threaded and unseeded, so running the SAME deck
    twice must write byte-identical result files. This is a determinism guard: a regression that
    made a run nondeterministic (iterating a dict/set into the grid, an unseeded RNG, a thread
    race, a wall-clock-dependent change to the step sequence) would break this even while the
    golden tolerance test still passed. Compares the five result rasters (.max, .maxVc, .inittm,
    .maxtm, .mxe) and the .mass time-step trajectory log byte-for-byte between two independent
    runs; the non-deterministic lisflood.log (it records wall time) is deliberately excluded."""
    runs = []
    for _ in range(2):
        wd = tempfile.mkdtemp(prefix="repeat_")
        _dem, par, st, _hy = _build_fixed_deck(wd)
        lf.run(wd, par, st)
        runs.append(_result_bytes(wd))
    first, second = runs
    assert first.keys() == second.keys()
    for fname in _REPEAT_FILES:
        assert first[fname] == second[fname], (
            "built-in engine nondeterministic: {} differs between two identical runs".format(fname))
