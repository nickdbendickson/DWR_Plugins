"""FINDING H6 (regression): the near-dam FV1 refine merged its results into the FINAL rasters
with no sanity check, and every domain edge was written FREE. In LISFLOOD-FP 8.1 a free FV1 edge
standing against reservoir water imports water into the fine box (Elm River Dam 1: the box grew
to 6.5e10 m3 against 9.7e6 m3 injected) and the garbage was merged, inflating near-dam depths and
the hazard class. These tests cover the three mitigations ported from nd_breach_runner:
  (1) the fine run is checked (refine.check_fine) before it is merged and the ACC results are
      kept when it fails (refine.run_refine);
  (2) the edges that face the reservoir are closed (refine.upstream_edges + the fine run sets
      close_upstream_edges; lisflood.LisfloodSettings.closed_edges + write_inputs);
  (3) the fine run lasts the hydrograph plus the travel time, not the main run's whole clock
      (refine.fine_sim_time).
"""
import json
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.tests.registry import test

FT = 0.3048


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_h6_")


# --------------------------------------------------------------- (2) closed upstream edges
@test
def h6_upstream_edges_face_away_from_the_flow():
    from nd_breach_runner.core import refine
    assert refine.upstream_edges((0.0, 1.0)) == ("S",)
    assert refine.upstream_edges((0.0, -1.0)) == ("N",)
    assert refine.upstream_edges((1.0, 0.0)) == ("W",)
    assert refine.upstream_edges((-1.0, 0.0)) == ("E",)
    assert refine.upstream_edges((0.7071, 0.7071)) == ("S", "W")
    assert refine.upstream_edges((0.7071, -0.7071)) == ("N", "W")
    # mostly east with a little north: the side edges stay free
    assert refine.upstream_edges((0.95, 0.31)) == ("W",)


@test
def h6_closed_edges_are_left_out_of_the_bci_file():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    d = _tmp()
    try:
        dem = Grid(np.full((20, 20), 100.0), 0.0, 400.0, 20.0)
        hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([10.0, 10.0, 0.0]), "t")
        lines = {}
        for tag, closed in (("open", ()), ("closed", ("S", "w"))):
            st = lf.LisfloodSettings("exe", solver=lf.SOLVER_FV1, closed_edges=closed)
            lf.write_inputs(os.path.join(d, tag), dem, (200.0, 200.0), hy, st)
            lines[tag] = open(os.path.join(d, tag, "run.bci")).read().splitlines()
        free = lambda ls: sorted(l.split()[0] for l in ls if l.endswith("FREE"))  # noqa: E731
        assert free(lines["open"]) == ["E", "N", "S", "W"]
        assert free(lines["closed"]) == ["E", "N"]              # S and W are closed walls
        assert any(l.startswith("P ") for l in lines["closed"])  # the breach source stays
        # the default (no closed_edges) keeps all four free, as before
        st0 = lf.LisfloodSettings("exe")
        assert st0.closed_edges == ()
        try:
            lf.LisfloodSettings("exe", closed_edges=("X",))
            assert False, "expected ModelError"
        except lf.ModelError as e:
            assert "unknown domain edge" in str(e)
    finally:
        shutil.rmtree(d)


def _mass(path, rows):
    with open(path, "w") as f:
        f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain\n")
        for t, vol, qin, qout in rows:
            f.write("{} 1 1 10 100 {} {} 0 {} 0 0 0\n".format(t, vol, qin, qout))


@test
def h6_water_coming_in_after_the_hydrograph_is_measured_from_the_mass_file():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        p = os.path.join(d, "run.mass")
        # hydrograph to 1000 s; afterwards no inflow
        _mass(p, [(0, 0, 0, 0), (500, 5e3, 10, 0), (1000, 1e4, 10, 0), (1500, 1e4, 0, 1),
                  (2000, 1e4, 0, 1)])
        assert lf.late_inflow(p, 1000.0) == (0.0, 0.0, 0.0), lf.late_inflow(p, 1000.0)
        # the edge imports 20 m3/s from 1500 s
        _mass(p, [(0, 0, 0, 0), (1000, 1e4, 10, 0), (1500, 1e4, 20, 0), (2000, 2e4, 20, 0)])
        vol, qmax, tmax = lf.late_inflow(p, 1000.0)
        assert qmax == 20.0 and 9.0e3 < vol <= 2.0e4 and tmax == 1500.0, (vol, qmax, tmax)
        # under the 0.5 m3/s tolerance nothing counts
        _mass(p, [(0, 0, 0, 0), (1000, 1e4, 10, 0), (2000, 1e4, 0.2, 0)])
        assert lf.late_inflow(p, 1000.0)[0] == 0.0
    finally:
        shutil.rmtree(d)


# --------------------------------------------------------------- (1) the fine run is checked
@test
def h6_check_fine_rejects_out_of_range_and_keeps_good():
    from nd_breach_runner.core import refine
    good = {"injected_acft": 7870.0, "max_domain_volume_ratio": 0.98,
            "late_boundary_inflow_pct": 0.0, "max_volume_error_pct": 0.1, "max_depth_ft": 22.0,
            "initial_water_acft": 0.0}
    assert refine.check_fine(good, 21.5) == []
    # Elm River Dam 1 (40 m): 6.5e10 m3 in the box against 9.7e6 m3 injected
    blown = dict(good, max_domain_volume_ratio=6700.0, late_boundary_inflow_pct=90000.0,
                 max_depth_ft=39137.0)
    why = refine.check_fine(blown, 21.5)
    assert len(why) == 3 and any("ac-ft against" in w for w in why), why
    assert any("came in through the edges" in w for w in why)
    assert any("maximum depth is 39,137" in w for w in why)
    # a pond that starts full is not water the edge brought in
    pond = dict(good, max_domain_volume_ratio=1.5, initial_water_acft=4000.0)
    assert refine.check_fine(pond, 21.5) == []
    # no statistics: not trusted (so a fine run whose stats could not be read is NOT merged)
    assert refine.check_fine({}, 21.5)


# --------------------------------------------------------------- (3) the fine run's short clock
@test
def h6_fine_sim_time_is_the_hydrograph_plus_travel_time_not_the_main_clock():
    from nd_breach_runner.core import refine, workflow
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.units import MILE
    st = workflow.RunSettings(cell_size_m=50.0)
    assert st.front_speed_mps == 0.5
    hy = Hydrograph(np.array([0.0, 10000.0, 16760.0]), np.array([0.0, 1000.0, 0.0]), "t")
    t = refine.fine_sim_time(st, hy, 1.0, 70 * 3600.0)
    expect = 16760.0 + 2.0 * 1.3 * MILE / 0.5 + 3600.0
    assert abs(t - expect) < 1.0 and t < 10 * 3600.0, t
    assert refine.fine_sim_time(st, hy, 1.0, 3 * 3600.0) == 3 * 3600.0       # never longer
    short = Hydrograph(np.array([0.0, 60.0]), np.array([0.0, 0.0]), "t")
    fast = workflow.RunSettings(cell_size_m=50.0, front_speed_mps=50.0)
    assert refine.fine_sim_time(fast, short, 0.05, 70 * 3600.0) == 2 * 3600.0  # at least 2 h
    # the fine settings carry the short clock and close the upstream edges; the main run does not
    f = refine.fine_settings(st, 1.0, 25.0, t, hy, None)
    assert f.close_upstream_edges is True and getattr(st, "close_upstream_edges", False) is False
    assert abs(f.sim_time_hr - t / 3600.0) < 1e-9


# --------------------------------------------------------- (1) run_refine rejects and keeps ACC
def _decision():
    return {"refine": True, "dist_mi": 1.0, "fine_cell_m": 25.0, "counts": {"total": 1},
            "reason": "1 receptor(s) within 1 mi of the breach; the ACC velocities are unreliable"}


def _blown_stats():
    # the fine box imported reservoir water through a free FV1 edge (Elm River Dam 1)
    return {"injected_acft": 1000.0, "max_domain_volume_ratio": 500.0,
            "late_boundary_inflow_pct": 400.0, "late_boundary_inflow_acft": 4000.0,
            "max_depth_ft": 39137.0, "initial_water_acft": 0.0, "max_volume_error_pct": 0.1,
            "max_velocity_fps": 120.0, "in_domain_acft": 5.0e5, "sim_time_hr": 2.0,
            "velocity_written": True}


def _run_refine_with(fr_factory):
    """Call refine.run_refine with workflow.run_dam stubbed to return `fr_factory(folder)`.
    Returns (merged, base_m, info, fin, logs)."""
    from nd_breach_runner.core import refine, workflow
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.tests.test_v320 import _dam
    st = workflow.RunSettings(cell_size_m=50.0)
    dam = _dam(dam_height_ft=25.0)
    hy = Hydrograph(np.array([0.0, 3600.0, 7200.0]), np.array([0.0, 500.0, 0.0]), "t")
    fin = {"depth": np.full((4, 4), 3.0), "velocity": np.full((4, 4), 1.2),
           "wse": np.full((4, 4), 103.0), "arrival": np.full((4, 4), 0.5)}
    model_dir = _tmp()
    logs = []
    holder = {}

    def fake_run_dam(dam_, dam_xy, fst, raw_terrain, writer, folder, inflow_xy,
                     progress=None, log=None, cancel=None):
        holder["fst"] = fst
        holder["folder"] = folder
        return fr_factory(folder)

    orig = workflow.run_dam
    workflow.run_dam = fake_run_dam
    try:
        # a long main clock (70 h) so the fine run's short clock is visibly shorter
        out = refine.run_refine(dam, (0.0, 0.0), st, None, None, model_dir, hy, None,
                                (0.0, 0.0), None, 70 * 3600.0, None, fin, None, _decision(),
                                log=logs.append)
    finally:
        workflow.run_dam = orig
        shutil.rmtree(model_dir, ignore_errors=True)
    merged, base_m, info = out
    return merged, base_m, info, fin, logs, holder


@test
def h6_run_refine_rejects_an_out_of_range_fine_run_and_keeps_the_acc_results():
    from nd_breach_runner.core import refine

    def fr_factory(folder):
        fr = types.SimpleNamespace()
        fr.final_si = {"depth": np.zeros((4, 4))}   # non-None: the fine run produced results
        fr.grid = object()                          # non-None
        fr.base_si = None
        fr.lisflood_stats = _blown_stats()
        fr.report_json = ""
        fr.warnings = []
        return fr

    merged, base_m, info, fin, logs, holder = _run_refine_with(fr_factory)
    # the ACC arrays are returned UNCHANGED (nothing merged in)
    assert merged is fin and base_m is None
    assert info["applied"] is False
    assert len(info["rejected"]) == 3
    assert any("ac-ft against" in w for w in info["rejected"])
    assert any("came in through the edges" in w for w in info["rejected"])
    assert any("maximum depth is 39,137" in w for w in info["rejected"])
    assert "NOT merged" in info["warning"] and "ACC results are" in info["warning"]
    assert any(m.startswith("Near-dam refine: the fine FV1 run was rejected") for m in logs), logs
    # the fine run closed its upstream edges and ran a short clock, not the 70 h main clock
    assert holder["fst"].close_upstream_edges is True
    assert holder["fst"].sim_time_hr < 10.0
    assert 0 < info["fine_sim_time_hr"] < 10.0
    # the report renders the rejection and the fine-run numbers
    md = "\n".join(refine.report_lines(info))
    assert "**Rejected: the fine FV1 run failed its checks" in md
    assert "Largest box volume / put in" in md and "39,137" in md


@test
def h6_run_refine_reads_the_fine_stats_from_the_report_json_when_no_lisflood_stats():
    # DWR's RunResult does not carry lisflood_stats (the sibling sets it in workflow.py). Prove
    # the fallback: the stats are read from the fine run's report JSON, so the check still fires.
    d = _tmp()
    try:
        rep = os.path.join(d, "run_report.json")
        with open(rep, "w", encoding="utf-8") as f:
            json.dump({"lisflood_runs": [{"stats": {"max_volume_error_pct": 0.1}},
                                         {"stats": _blown_stats()}]}, f)

        def fr_factory(folder):
            fr = types.SimpleNamespace()
            fr.final_si = {"depth": np.zeros((4, 4))}
            fr.grid = object()
            fr.base_si = None
            fr.report_json = rep                 # no lisflood_stats attribute at all
            fr.warnings = []
            return fr

        merged, base_m, info, fin, logs, _holder = _run_refine_with(fr_factory)
        assert merged is fin and info["applied"] is False
        assert len(info["rejected"]) == 3        # read the LAST run's stats from the report
        assert info["fine_stats"]["max_domain_volume_ratio"] == 500.0
    finally:
        shutil.rmtree(d, ignore_errors=True)
