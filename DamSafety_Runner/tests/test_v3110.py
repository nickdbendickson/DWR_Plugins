"""M5: the built-in numpy flood engine (core/builtin_lisflood.py), ported into DamSafety and
made STRICTLY OPT-IN with mandatory labeling.

These tests cover (a) the opt-in resolver contract — the built-in is never selected on an empty
value, so it can never silently stand in for LISFLOOD-FP; (b) that it runs, conserves mass, and
names itself in the self-check and selftest; (c) physics checks that CAN be verified in a numpy
sandbox without a real LISFLOOD-FP binary: lake-at-rest well-balancedness and mass conservation
with bounded limiter clipping on a steep channel; (d) that the near-dam refine is skipped AND
honestly flagged for the built-in engine; and (e) that no report surface claims LISFLOOD-FP for a
built-in run.

A true numerical parity run against a real patched LISFLOOD-FP binary cannot be done in this
sandbox (no binary, no GDAL, no network) and remains an off-sandbox release gate before built-in
results are used for anything beyond preliminary screening.
"""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import lisflood as lf, nd_data
from nd_breach_runner.tests.registry import test


def _run_engine(z_north_top, dx=20.0, start_depth=None, source=None, sim_time=600.0,
                initial_tstep=2.0, massint=60.0, fpfric=0.06):
    """Drive core/builtin_lisflood.run_par on a synthetic grid. `z_north_top` is the bed with
    row 0 = north. `source` is (x, y, series) where series is [(q_cms, t_s), ...]; None = no
    inflow and (with no FREE edges in the bci) all four edges are closed walls. Returns
    (workdir, stats)."""
    from nd_breach_runner.core import builtin_lisflood as bl
    wd = tempfile.mkdtemp(prefix="v3110_eng_")
    ny, nx = z_north_top.shape
    yll = 0.0
    xll = 0.0
    bl._write_asc(os.path.join(wd, "run.dem.asc"), z_north_top, xll, yll, dx)
    par = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
           "sim_time {:g}".format(sim_time), "initial_tstep {:g}".format(initial_tstep),
           "massint {:g}".format(massint), "saveint {:g}".format(sim_time + 10.0),
           "acceleration", "fpfric {:g}".format(fpfric), "inittm_depth 0.3"]
    if start_depth is not None:
        bl._write_asc(os.path.join(wd, "run.start"), start_depth, xll, yll, dx)
        par.append("startfile run.start")
    if source is not None:
        x, y, series = source
        with open(os.path.join(wd, "run.bci"), "w") as f:
            f.write("P {:.3f} {:.3f} QVAR breach\n".format(x, y))
            # list every edge FREE so the flood can leave the domain
            f.write("N 0.000 1e9 FREE\nS 0.000 1e9 FREE\nE 0.000 1e9 FREE\nW 0.000 1e9 FREE\n")
        with open(os.path.join(wd, "run.bdy"), "w") as f:
            f.write("hdr\nbreach\n{} seconds\n".format(len(series)))
            for q, t in series:
                f.write("{:.6f} {:.1f}\n".format(q / dx, t))   # QVAR is per unit width
        par += ["bcifile run.bci", "bdyfile run.bdy"]
    with open(os.path.join(wd, "run.par"), "w") as f:
        f.write("\n".join(par) + "\n")
    stats = bl.run_par(os.path.join(wd, "run.par"), log=lambda m: None)
    return wd, stats


# --- opt-in resolver contract (the M5 safety guarantee) --------------------------------------

@test
def v3110_builtin_words_resolve_but_empty_stays_empty():
    for w in ("builtin", "Built-In", " internal "):
        assert lf.resolve_exe(w) == lf.BUILTIN_EXE and lf.is_builtin(lf.resolve_exe(w))
    assert lf.resolve_exe("/x/lisflood") == "/x/lisflood"
    # the DWR difference from the nd_ sibling: an empty value NEVER becomes the built-in, so the
    # run guard raises and the numpy approximation is never substituted silently for LISFLOOD-FP.
    assert lf.resolve_exe("") == "" and lf.resolve_exe(None) == ""
    assert os.path.isfile(lf.BUILTIN_EXE)
    assert "built-in" in lf.describe_exe(lf.BUILTIN_EXE)
    assert "NOT LISFLOOD-FP" in lf.engine_label(lf.BUILTIN_EXE)
    assert lf.engine_label("/x/lisflood") == "LISFLOOD-FP"
    assert lf.engine_name(lf.BUILTIN_EXE) == "the built-in engine"


@test
def v3110_builtin_is_opt_in_not_auto_detected():
    # DamSafety ships no bundled binary, and auto-detection never yields the built-in: it is
    # reached only via the explicit 'builtin' keyword or the provider flag (algorithms.find_lisflood).
    assert lf.bundled_exe() is None
    assert nd_data.detect_lisflood() != lf.BUILTIN_EXE


# --- runs, conserves mass, names itself ------------------------------------------------------

@test
def v3110_check_runs_with_the_builtin_engine_and_conserves_volume():
    tmp = tempfile.mkdtemp(prefix="v3110_")
    try:
        r = lf.check_lisflood(lf.BUILTIN_EXE, os.path.join(tmp, "b"))
        assert r["ok"] and r["patched"], r
        assert "built-in" in r["message"].lower() and "NOT LISFLOOD-FP" in r["message"]
        m = np.loadtxt(os.path.join(tmp, "b", "results", "run.mass"), skiprows=1)
        # column 10 is Verror (cumulative volume error); it must be a small share of the volume
        assert abs(m[-1, 10]) < 0.01 * max(m[-1, 5], 1.0), m[-1]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def v3110_selftest_check_accepts_the_builtin_engine():
    from nd_breach_runner.core import selftest
    tmp = tempfile.mkdtemp(prefix="v3110_")
    try:
        msg = selftest.check_lisflood_exe(lf.BUILTIN_EXE, tmp)
        assert "built-in" in msg.lower(), msg
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --- physics that IS verifiable without a real LISFLOOD-FP binary ----------------------------

@test
def v3110_builtin_lake_at_rest_is_well_balanced():
    """A flat water surface over a sloping bed, with closed walls and no inflow, must stay at
    rest: a well-balanced scheme produces no spurious current and conserves volume exactly. This
    is the single most important correctness gate the sandbox can run."""
    ny, nx, dx = 20, 40, 20.0
    X = (np.arange(nx) + 0.5) * dx                   # 10 .. 790 m
    z = np.tile(100.0 - 0.01 * X, (ny, 1))          # bed 99.9 (west) down to 92.1 (east)
    wse = 96.0                                       # flat surface: east half wet, west half dry
    h0 = np.clip(wse - z, 0.0, None)                 # initial DEPTH (startfile), dry where z > wse
    v0 = float(h0.sum()) * dx * dx
    assert v0 > 0 and (h0 > 0).any() and (h0 == 0).any()   # genuinely part wet, part dry
    wd, stats = _run_engine(z, dx=dx, start_depth=h0, source=None, sim_time=600.0)
    try:
        assert stats["injected_m3"] == 0.0 and stats["outflow_m3"] == 0.0   # closed, no sources
        # volume conserved to round-off (nothing added or removed)
        assert abs(stats["final_volume_m3"] - v0) < 1e-6 * v0, (stats["final_volume_m3"], v0)
        assert abs(stats["mass_error_m3"]) < 1e-6 * v0
        # no spurious current: the max velocity raster is essentially zero
        vmx = lf.Grid.read_asc(os.path.join(wd, "results", "run.maxVc"))
        vdata = vmx.data[vmx.data != vmx.nodata]
        assert vdata.size and float(np.nanmax(np.abs(vdata))) < 0.02, float(np.nanmax(vdata))
        # depths did not drift: the final max depth matches the initial lake depth
        hmx = lf.Grid.read_asc(os.path.join(wd, "results", "run.max"))
        hdata = hmx.data[hmx.data != hmx.nodata]
        assert abs(float(np.nanmax(hdata)) - float(h0.max())) < 1e-3, float(np.nanmax(hdata))
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@test
def v3110_builtin_conserves_mass_on_a_steep_channel_and_bounds_clipping():
    """On a genuinely steep channel the stability limiter can clip outflow to keep depth >= 0;
    mass must still be conserved and the clipped volume must be reported and bounded, so a
    steep-terrain screening result cannot quietly lean on large clipping unnoticed."""
    ny, nx, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nx) + 0.5) * dx, (np.arange(ny) + 0.5) * dx)
    z = (200.0 - 0.03 * X + 0.03 * np.abs(Y - ny * dx / 2))[::-1]    # ~3% down, steep side walls
    source = (30.0, ny * dx / 2 + 5, [(40.0, 0.0), (40.0, 600.0), (0.0, 1200.0)])
    wd, stats = _run_engine(z, dx=dx, source=source, sim_time=1200.0, initial_tstep=1.0)
    try:
        inj = float(stats["injected_m3"])
        assert inj > 0
        assert abs(stats["mass_error_m3"]) < 1e-3 * inj, stats["mass_error_m3"]
        assert "clipped_m3" in stats                       # limiter loss is surfaced in the stats
        assert 0.0 <= float(stats["clipped_m3"]) < 0.10 * inj, stats["clipped_m3"]
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# --- the near-dam refine must not run a bogus FV1 on the built-in engine ---------------------

@test
def v3110_refine_is_skipped_and_flagged_for_the_builtin_engine():
    from types import SimpleNamespace
    from nd_breach_runner.core import refine as refmod
    st = SimpleNamespace(lisflood_exe=lf.BUILTIN_EXE, refine_near=True, refine_dist_mi=1.0,
                         refine_cell_m=0.0, cell_size_m=10.0, lisflood_solver=lf.SOLVER_ACC)
    # final_model is the built-in label (what workflow sets), so decide takes the built-in branch
    info = refmod.decide(st, lf.engine_label(lf.BUILTIN_EXE), None,
                         [{"xy": (0.0, 0.0)}], (0.0, 0.0))
    assert info["refine"] is False
    assert "cannot be refined" in info["reason"].lower() and "built-in" in info["reason"].lower()


# --- no report surface may claim LISFLOOD-FP for a built-in run ------------------------------

@test
def v3110_builtin_report_never_claims_lisflood_fp():
    from nd_breach_runner.core import workflow
    s = {"dam_id": "TEST-1", "scenario": "sunny",
         "final_model": lf.engine_label(lf.BUILTIN_EXE),
         "peak_breach_cfs": 1000.0, "breach_method": "test",
         "storage_at_failure_acft": 100.0, "flooded_acres": 10,
         "flooded_acres_over_arrival_depth": 5, "max_depth_ft": 3.0,
         "max_velocity_fps": 1.0, "latest_arrival_hr": 0.5,
         "domain_mi": [1.0, 1.0], "lisflood_runs": 2, "units": "depth ft",
         "flood_engine_short": lf.engine_name(lf.BUILTIN_EXE)}
    run1 = {"run": 1, "size_mi": [1.0, 1.0], "cells": 1000, "sim_time_hr": 6.0,
            "edge_max_depth_ft": {"N": 0.0, "S": 0.0, "E": 0.0, "W": 0.0},
            "edges_over_tolerance": [], "runtime_min": 1.0,
            "stats": {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 1000}}
    r = {"summary": s, "hydrograph": {}, "tool": "ND Dam Breach Runner",
         "policy": "ND REG_05.2026", "run_finished": "2026-01-01",
         "warnings": ["This run uses the BUILT-IN screening engine (local inertial, in numpy), "
                      "NOT LISFLOOD-FP."],
         "flood_engine": "Built-in engine (local inertial, numpy; NOT LISFLOOD-FP)",
         "flood_engine_short": lf.engine_name(lf.BUILTIN_EXE),
         "inflow_point": {"xy": [0.0, 0.0], "how": "just below the dam"},
         "settings": {"edge_tolerance_ft": 1.0, "expand_step_mi": 3.0,
                      "impact_look_ahead_mi": 0.5, "receptors": None},
         "lisflood_runs": [run1],
         "outputs": {"final": {}, "baseline": {}, "increment": {}, "vectors": {}}}
    md = workflow.render_report(r)
    # every mention of LISFLOOD-FP in a built-in report is a "NOT LISFLOOD-FP" disclaimer; there
    # is never an affirmative claim that the run used LISFLOOD-FP.
    assert md.count("LISFLOOD-FP") == md.count("NOT LISFLOOD-FP"), md
    assert "built-in engine" in md and "NOT LISFLOOD-FP" in md
