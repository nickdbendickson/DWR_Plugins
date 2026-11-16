"""v3.20.9 (Nick, 2026-11-16): the Runner's "flood still moving" clock rule is AREA-BASED.

Before, one cell first wet (at 0.1 ft) in the last 10% of the clock lengthened the clock 1.5x
and re-ran the pass, with no limit: DWR-4031 (6.9 ac-ft pond, ~53 ac wet, mostly a thin sheet)
went 4.3 -> 6.5 -> 9.7 -> 14.6 -> 21.9 -> 32.8 h on one DEM. Now (workflow.spread_still_moving)
the flood is still spreading only when the late area is >= 1% of the wet area and >= 0.25 ac,
and the wet area grew >= 1% since the previous pass of the same domain run (same DEM); at most
MAX_CLOCK_EXTENSIONS (4) clock extensions per domain run, then a warning.
v3.20.9 review fixes: a late area of SPREAD_LARGE_LATE_AC (25 ac) counts at any share (large
floods); pond cells are left out; no growth comparison with a pre-3.20.9 record or across the
ACC stability switch; the latest arrival is flagged a lower bound when area was still first wet
late (HEC-RAS then sizes its window from the clock); plan runs warn when still spreading; the
report's cut-off section names the wall-clock budget, the clock-extension limit and a fixed clock.

Pure-helper tests with numpy arrays, then the growth loop with LISFLOOD-FP stubbed (as
test_v320.fake_models / test_wall_limits._slow_models do). Synthetic values only.

    python3 -m nd_breach_runner.tests.run_tests
"""
import contextlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import workflow as W
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest

AD = 0.1 * FT              # the arrival depth, m
CELL10_AC = 100.0 / 4046.8564224     # one 10 m cell, ac


def _field(n_wet, n_late, sim_hr, shape=(100, 100), depth=0.05, late_depth=None):
    """n_wet cells wet (`depth` m) with an arrival, n_late of them first wet at 0.95 x sim_hr,
    the rest at 0.5 h; everything else dry (NaN arrival, 0 depth)."""
    arr = np.full(shape, np.nan)
    h = np.zeros(shape)
    flat_a, flat_h = arr.reshape(-1), h.reshape(-1)
    flat_a[:n_wet] = 0.5
    flat_h[:n_wet] = depth
    flat_a[n_wet - n_late:n_wet] = 0.95 * sim_hr
    if late_depth is not None:
        flat_h[n_wet - n_late:n_wet] = late_depth
    return arr, h


@register
def spread_rule_constants_are_the_agreed_ones():
    assert W.SPREAD_LATE_WINDOW == 0.9
    assert W.SPREAD_LATE_FRACTION == 0.01
    assert W.SPREAD_GROWTH_FRACTION == 0.01
    assert W.SPREAD_MIN_LATE_AC == 0.25
    assert W.SPREAD_LARGE_LATE_AC == 25.0          # v3.20.9 review: counts at any share
    assert W.MAX_CLOCK_EXTENSIONS == 4
    # the 0.25 ac floor in cells (the comment beside it): 11 at 10 m, 2 at 30 m, 1 at 50 m
    for cell_m, n in ((10.0, 11), (30.0, 2), (50.0, 1)):
        ac = cell_m ** 2 / 4046.8564224
        assert n * ac >= W.SPREAD_MIN_LATE_AC > (n - 1) * ac, (cell_m, n)


@register
def spread_thin_creep_like_dwr4031_is_not_spreading():
    # ~53 ac wet at 10 m (2,150 cells), a thin sheet (0.05 m = 0.16 ft); 8 fringe cells
    # (0.2 ac, 0.37%) first wet late: the old rule extended on these, the new one does not
    arr, h = _field(2150, 8, 9.7)
    assert np.nanmax(arr) > 0.9 * 9.7          # the old rule would have said "still moving"
    go, info = W.spread_still_moving(arr, h, 9.7, CELL10_AC, prev_flooded_ac=52.0)
    assert go is False, info
    assert abs(info["flooded_ac"] - 2150 * CELL10_AC) < 0.01 and info["late_ac"] < 0.25, info
    assert info["late_frac"] < 0.01 and info["reason"].startswith("only "), info
    assert "under 1.0%" in info["reason"], info["reason"]
    assert info["still_spreading"] is False and info["late_after_hr"] == round(0.9 * 9.7, 2)


@register
def spread_real_advancing_front_is_spreading():
    # 5% of a 1,000-cell (24.7 ac) wet area first wet in the last 10% of the clock, wet area
    # +10% over the previous pass: still spreading
    arr, h = _field(1000, 50, 6.5)
    prev = 900 * CELL10_AC
    go, info = W.spread_still_moving(arr, h, 6.5, CELL10_AC, prev_flooded_ac=prev)
    assert go is True, info
    assert abs(info["late_frac"] - 0.05) < 1e-9 and abs(info["growth_frac"] - 1 / 9.0) < 1e-3
    assert info["reason"] == ("1.24 ac (5.0% of the 24.7 ac wet area) first wet in the last 10% "
                              "of the clock, wet area +11% since the last pass"), info["reason"]
    # no earlier pass of this domain run: the late share alone decides
    go, info = W.spread_still_moving(arr, h, 6.5, CELL10_AC)
    assert go is True and info["growth_frac"] is None and info["prev_flooded_ac"] is None, info
    assert "since the last pass" not in info["reason"]
    # thin late cells count: no depth floor above the arrival depth (0.1-0.5 ft sheets matter)
    arr, h = _field(1000, 50, 6.5, depth=1.0, late_depth=AD + 0.002)
    assert W.spread_still_moving(arr, h, 6.5, CELL10_AC)[0] is True


@register
def spread_late_share_but_no_growth_is_not_spreading():
    arr, h = _field(1000, 50, 14.6)            # 5% late ...
    prev = 995 * CELL10_AC                     # ... but the wet area grew only 0.5%
    go, info = W.spread_still_moving(arr, h, 14.6, CELL10_AC, prev_flooded_ac=prev)
    assert go is False, info
    assert 0 < info["growth_frac"] < 0.01 and "grew only +0.5%" in info["reason"], info
    # exactly 1% growth is enough (>=), and a shrinking wet area is not spreading
    go, _ = W.spread_still_moving(arr, h, 14.6, CELL10_AC, prev_flooded_ac=1000 * CELL10_AC / 1.01)
    assert go is True
    go, info = W.spread_still_moving(arr, h, 14.6, CELL10_AC, prev_flooded_ac=1100 * CELL10_AC)
    assert go is False and info["growth_frac"] < 0 and "wet area shrank 9.1%" in info["reason"], info
    # a previous pass with nothing wet: the area is new, so it grew
    go, info = W.spread_still_moving(arr, h, 14.6, CELL10_AC, prev_flooded_ac=0.0)
    assert go is True and "new since the last pass" in info["reason"], info


@register
def spread_small_flood_floor():
    # a 2 ac pond flood (81 cells at 10 m): 3 late cells are 3.7% but only 0.07 ac
    arr, h = _field(81, 3, 4.3)
    go, info = W.spread_still_moving(arr, h, 4.3, CELL10_AC)
    assert go is False and "under the 0.25 ac floor" in info["reason"], info
    # 11 late cells (0.27 ac) pass the floor
    arr, h = _field(81, 11, 4.3)
    assert W.spread_still_moving(arr, h, 4.3, CELL10_AC)[0] is True
    # at 50 m one cell is 0.62 ac: the floor is met by a single cell, the 1% share decides
    cell50 = 2500.0 / 4046.8564224
    arr, h = _field(50, 1, 4.3)
    assert W.spread_still_moving(arr, h, 4.3, cell50)[0] is True
    arr, h = _field(200, 1, 4.3)
    assert W.spread_still_moving(arr, h, 4.3, cell50)[0] is False


@register
def spread_no_arrivals_and_nan_handling():
    shape = (20, 20)
    # nothing wet at all
    go, info = W.spread_still_moving(np.full(shape, np.nan), np.zeros(shape), 6.0, CELL10_AC)
    assert go is False and info["flooded_ac"] == 0 and info["late_frac"] == 0, info
    assert info["reason"] == "nothing is wet at the arrival depth"
    # NaN / negative arrivals and NaN depth are not wet; a cell under the arrival depth with an
    # arrival (an inconsistent raster) is not counted either
    arr = np.full(shape, np.nan)
    h = np.full(shape, np.nan)
    arr[0, :10] = 1.0
    h[0, :10] = 0.2
    arr[1, 0] = 5.9; h[1, 0] = np.nan        # late, but no depth
    arr[1, 1] = -9999.0; h[1, 1] = 0.2       # nodata arrival
    arr[1, 2] = 5.9; h[1, 2] = 0.001         # late, under the arrival depth
    go, info = W.spread_still_moving(arr, h, 6.0, CELL10_AC)
    assert go is False and abs(info["flooded_ac"] - 10 * CELL10_AC) < 0.01, info
    assert info["late_ac"] == 0 and "nothing first wet in the last 10%" in info["reason"], info
    # hmax None: the arrival raster alone (read_results already filters it to the wet cells)
    go, info = W.spread_still_moving(arr, None, 6.0, CELL10_AC)
    assert info["flooded_ac"] == round(12 * CELL10_AC, 2), info
    # a cell at exactly the late threshold is not late (strictly after 90% of the clock)
    arr2 = np.where(np.isfinite(arr) & (arr >= 0), 0.9 * 6.0, np.nan)
    assert W.spread_still_moving(arr2, None, 6.0, CELL10_AC)[1]["late_ac"] == 0
    # the info dict is JSON-safe (it goes into run_report.json)
    json.dumps(info)


@register
def spread_previous_pass_is_the_same_run_on_the_same_dem():
    p = W.previous_pass_flooded_ac
    assert p([], 1, 6.5) is None
    it1 = {"run": 1, "sim_time_hr": 4.3, "spread": {"flooded_ac": 40.0}, "clock_extension": False}
    # the first extension of run 1 compares with pass 1 (which was itself NOT an extension)
    assert p([it1], 1, 6.45) == 40.0
    # a new domain run (the domain grew): nothing to compare with
    assert p([it1], 2, 6.45) is None
    # a pass after a 3DEP refill (same run, same clock, different DEM): nothing to compare with
    it1r = dict(it1, followed_by_dem_refill=True)
    assert p([it1r], 1, 4.3) is None
    assert p([it1r], 1, 6.45) is None
    # same clock (a resumed or repeated pass) is not "a previous pass with a shorter clock"
    assert p([it1], 1, 4.3) is None
    # a pre-3.20.9 record (resumed checkpoint) has no `spread`: its stats.flooded_acres counts
    # the total depth (pond water included, no tolerance), not the same measure - no comparison
    # (v3.20.9 review: it gave a false "the wet area shrank" stop on pond dams)
    old = {"run": 1, "sim_time_hr": 4.3, "stats": {"flooded_acres": 38.5}}
    assert p([old], 1, 6.45) is None
    # the ACC stability switch between the passes (other theta / CFL): not comparable
    assert p([it1], 1, 6.45, stable_settings=True) is None
    it1s = dict(it1, stable_settings=True)
    assert p([it1s], 1, 6.45, stable_settings=True) == 40.0
    assert p([it1s], 1, 6.45) is None
    # only the LAST pass is compared with (pass 2 after pass 1, not pass 1)
    it2 = {"run": 1, "sim_time_hr": 6.45, "spread": {"flooded_ac": 45.0}, "clock_extension": True}
    assert p([it1, it2], 1, 9.68) == 45.0


@register
def spread_clock_extension_cap():
    runs = [{"run": 1, "clock_extension": False}, {"run": 1, "clock_extension": True},
            {"run": 2, "clock_extension": False}, {"run": 2, "clock_extension": True},
            {"run": 2, "clock_extension": True}]
    assert W.clock_extensions_used(runs, 1) == 1 and W.clock_extensions_used(runs, 2) == 2
    assert W.clock_extensions_used(runs, 3) == 0 and W.clock_extensions_used([], 1) == 0
    cap = W.hit_clock_extension_cap
    assert not cap(True, [], 3) and cap(True, [], 4) and cap(True, [], 5)
    assert not cap(False, [], 4)                 # not spreading: the normal stop, no cap
    assert not cap(True, ["E"], 4)               # the domain grows: a new run, no cap
    w = W.clock_extension_cap_warning(4, 21.9, "5 ac (3%) ...")
    assert w.startswith("Flood still spreading after 4 clock extensions (clock 21.9 h)"), w
    assert ("check Arrival Time.tif near the edge of the wet area, or set a simulation duration "
            "longer than 21.9 h.") in w, w
    # (the run report's cut-off section naming this limit: the loop test below)


# ------------------------------------------------------------- the growth loop, LISFLOOD-FP stubbed
@contextlib.contextmanager
def _front_models(kind, calls):
    """LISFLOOD-FP stubbed on the 50 m test domain (34 x 67 cells), the flood kept 5 cells off
    every edge (so no edge is over and the domain never grows):
      "front": the wet area widens with the clock and its last column (~5%) is first wet in the
               last 10% of the clock - a front still on its way, every pass;
      "creep": a fixed 24 x 20 wet area with 2 fringe cells (0.4%) first wet late (DWR-4031);
      "stall": a fixed wet area with its last column (5%) first wet late every pass."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        calls.append(st.sim_time_s / 3600.0)
        return 1.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        sim_hr = st.sim_time_s / 3600.0
        ny, nx = dem.data.shape
        ncol = {"front": min(nx - 10, 10 + int(2 * sim_hr)), "creep": 20, "stall": 20}[kind]
        d = np.full((ny, nx), np.nan)
        d[5:ny - 5, 5:5 + ncol] = 0.06               # a 0.2 ft sheet
        arr = np.where(np.isfinite(d), 0.2, np.nan)
        if kind == "creep":
            arr[5, 5 + ncol - 1] = arr[6, 5 + ncol - 1] = 0.95 * sim_hr
        else:
            arr[5:ny - 5, 5 + ncol - 1] = 0.95 * sim_hr
        vel = np.where(np.isfinite(d), 0.5, np.nan)
        wse = np.where(np.isfinite(d), dem.data + np.nan_to_num(d), np.nan)
        stats = {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 1000.0}
        return lf.Envelopes(dem.like(d, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan), stats, [], {})
    old = (lf.run, lf.read_results)
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


def _loop(kind, **k):
    from nd_breach_runner.tests.test_v320 import _dam, _settings
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = tempfile.mkdtemp(prefix="ndbr_spread_")
    try:
        calls, logs = [], []
        st = _settings(products=False, qa_maps="off", **k)
        with _front_models(kind, calls):
            res = W.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                            os.path.join(d, "T1"), log=logs.append)
        with open(res.report_json, encoding="utf-8") as f:
            rep = json.load(f)
        with open(res.report_md, encoding="utf-8") as f:
            md = f.read()
        return calls, logs, rep, md, res
    finally:
        shutil.rmtree(d, ignore_errors=True)


@register
def spread_loop_advancing_front_stops_after_four_clock_extensions():
    for k in ({"sim_time_hr": None},                       # the automatic clock
              {"sim_time_hr": 4.0, "sim_max_hr": 200.0}):  # a chosen duration, longest 200 h
        calls, logs, rep, md, res = _loop("front", **k)
        assert len(calls) == 1 + W.MAX_CLOCK_EXTENSIONS, (k, calls)
        assert all(b > a * 1.4 for a, b in zip(calls, calls[1:])), calls      # 1.5x each
        runs = rep["lisflood_runs"]
        assert [r["run"] for r in runs] == [1] * 5, runs
        assert [r["clock_extension"] for r in runs] == [False] + [True] * 4, runs
        assert all(r["spread"]["still_spreading"] for r in runs), [r["spread"] for r in runs]
        assert runs[0]["spread"]["growth_frac"] is None
        assert all(r["spread"]["growth_frac"] > 0.01 for r in runs[1:])
        assert rep["cut_off"]["reason"] == "clock-extension limit", rep["cut_off"]
        w = [x for x in res.warnings if x.startswith("Flood still spreading after 4 clock")]
        assert len(w) == 1 and "clock {:.1f} h".format(calls[-1]) in w[0], res.warnings
        ext = [m for m in logs if m.startswith("Flood still spreading: ")]
        assert len(ext) == 4 and "first wet in the last 10% of the clock" in ext[0], ext
        assert "since the last pass" not in ext[0] and "since the last pass" in ext[1], ext
        assert "(clock extension 1 of at most 4)" in ext[0], ext[0]
        assert rep["spread_rule"]["max_clock_extensions"] == 4
        assert "Clock rule (v3.20.9)" in md and "- Pass 5: still spreading" in md, md
        # run_report.md's cut-off section names this limit, not the duration cap / run limit
        assert "## Results cut off" in md and "The clock-extension limit (4 per domain run)" in md


@register
def spread_loop_thin_creep_runs_once():
    calls, logs, rep, md, res = _loop("creep", sim_time_hr=None)
    assert len(calls) == 1, calls
    r = rep["lisflood_runs"][0]
    assert r["latest_arrival_hr"] > 0.9 * r["sim_time_hr"]      # the old rule would extend
    assert r["spread"]["still_spreading"] is False and r["spread"]["late_frac"] < 0.01, r
    assert "cut_off" not in rep, rep.get("cut_off")
    stop = [m for m in logs if m.startswith("Flood stays inside the domain")]
    assert stop and "is no longer spreading: only " in stop[0], stop
    assert not any("still spreading" in w for w in res.warnings), res.warnings
    # v3.20.9 review: the creep's late cells make the latest arrival a lower bound, said so
    s = rep["summary"]
    assert s["latest_arrival_lower_bound"] is True and s["lisflood_clock_hr"] == r["sim_time_hr"], s
    assert s["latest_arrival_note"].startswith("a lower bound: "), s["latest_arrival_note"]
    assert "h after breach initiation (a lower bound: " in md, md
    assert "## Results cut off" not in md


@register
def spread_loop_stops_when_the_wet_area_stops_growing():
    # 5% late every pass, but the wet area is the same: one extension, then the comparison
    # with the previous pass of the same domain run stops it
    calls, logs, rep, md, res = _loop("stall", sim_time_hr=None)
    assert len(calls) == 2, calls
    runs = rep["lisflood_runs"]
    assert runs[0]["spread"]["still_spreading"] and not runs[1]["spread"]["still_spreading"]
    assert runs[1]["spread"]["growth_frac"] == 0.0 and runs[1]["clock_extension"] is True
    assert "grew only +0.0%" in runs[1]["spread"]["reason"], runs[1]["spread"]
    assert "cut_off" not in rep


@register
def spread_loop_chosen_duration_without_longest_clock_still_stops_at_the_duration():
    # unchanged: a chosen duration with no longest clock does not lengthen; the flood still
    # spreading is reported as the duration cap
    calls, logs, rep, md, res = _loop("front", sim_time_hr=4.0)
    assert calls == [4.0], calls
    assert rep["cut_off"]["reason"] == "duration cap", rep["cut_off"]


# ------------------------------------------------------------------ v3.20.9 review fixes
@register
def spread_large_flood_thin_sheet_counts_at_any_share():
    # Review 2 finding 1: 20,000 ac at 30 m, a thin sheet still advancing - 51 ac first wet in
    # the last 3 h of a 30 h clock is only 0.26% of the wet area, but it is a flood on its way
    cell30 = 900.0 / 4046.8564224
    n_wet, n_late = 89930, 229
    arr, h = _field(n_wet, n_late, 30.0, shape=(300, 300))
    go, info = W.spread_still_moving(arr, h, 30.0, cell30)
    assert go is True and info["late_frac"] < 0.01 and info["late_ac"] >= 25.0, info
    assert "at least the 25 ac that counts at any share" in info["reason"], info["reason"]
    # compared with the last pass: the wet area grew by the late area (51 ac, 0.26%) - enough
    flooded = info["flooded_ac"]
    go, info = W.spread_still_moving(arr, h, 30.0, cell30, prev_flooded_ac=flooded - 51.0)
    assert go is True and info["growth_frac"] < 0.01, info
    # ... but a wet area that grew under both 1% and 25 ac is not (an inconsistent pass)
    go, info = W.spread_still_moving(arr, h, 30.0, cell30, prev_flooded_ac=flooded - 10.0)
    assert go is False and "grew only" in info["reason"], info
    # 20 ac late on the same flood is fringe noise (under 1% and under 25 ac)
    arr, h = _field(n_wet, 90, 30.0, shape=(300, 300))
    assert W.spread_still_moving(arr, h, 30.0, cell30)[0] is False


@register
def spread_pond_cells_are_left_out():
    # Review 2 finding 5: 9,000 cells under pond water at the start (arrival ~0, raised by the
    # flood) and 1,000 newly wet cells with 50 (5%) first wet late
    arr, h = _field(10000, 50, 6.5)
    arr.reshape(-1)[:9000] = 0.0
    go, info = W.spread_still_moving(arr, h, 6.5, CELL10_AC)
    assert go is False and info["late_frac"] == 0.005, info        # diluted by the ponds
    init = np.zeros_like(h)
    init.reshape(-1)[:9000] = 0.5
    go, info = W.spread_still_moving(arr, h, 6.5, CELL10_AC, init_m=init)
    assert go is True and info["late_frac"] == 0.05, info
    assert abs(info["flooded_ac"] - round(1000 * CELL10_AC, 2)) < 0.01, info
    # a cell with pond water under the arrival depth is not a pond cell for this rule
    init.reshape(-1)[:9000] = AD / 2
    assert W.spread_still_moving(arr, h, 6.5, CELL10_AC, init_m=init)[1]["late_frac"] == 0.005


@register
def spread_no_arrival_times_written():
    # Review 1 finding 4: no .inittm (read_results leaves the arrival grid all NaN) while the
    # max-depth grid is wet - say so instead of "nothing is wet"
    h = np.zeros((10, 10))
    h[:3] = 0.3
    go, info = W.spread_still_moving(np.full(h.shape, np.nan), h, 6.0, CELL10_AC)
    assert go is False and info["reason"].startswith("no arrival times were written"), info
    go, info = W.spread_still_moving(np.full(h.shape, np.nan), np.zeros(h.shape), 6.0, CELL10_AC)
    assert info["reason"] == "nothing is wet at the arrival depth", info


@register
def spread_latest_arrival_lower_bound_helper():
    f = W.latest_arrival_lower_bound
    assert f([]) == (False, None, "")
    last = {"sim_time_hr": 9.7, "spread": {"late_ac": 0.2, "late_frac": 0.0037}}
    lb, clock, note = f([last], t_ref_hr=0.5)
    assert lb is True and clock == 9.2, (lb, clock)
    assert note == ("a lower bound: 0.20 ac (0.4% of the wet area) was first wet in the last 10% "
                    "of the 9.7 h LISFLOOD-FP clock"), note
    assert f([dict(last, spread={"late_ac": 0.0})])[0] is False
    assert f([{"sim_time_hr": 4.0}])[0] is False             # a pre-3.20.9 record


def _render_with(rep, warnings, runs=None):
    r = json.loads(json.dumps(rep))
    r["warnings"] = list(warnings)
    if runs is not None:
        r["lisflood_runs"] = runs
    r.pop("cut_off", None)
    return W.render_report(r), r


@register
def spread_report_cut_off_section_names_each_limit():
    # Review 1 finding 1: render_report matched "still" + ("advanc"|"moving"|"edge") only; the
    # wall-clock budget warning now says "still spreading inside the domain"
    calls, logs, rep, md, res = _loop("creep", sim_time_hr=None)
    budget = ("Stopped after 3 LISFLOOD-FP pass(es) and 2.0 h of wall time, the budget for the "
              "domain-growth loop (2.0 h = the setting), with the flood still spreading inside "
              "the domain. The results are this pass's; the domain and clock were not extended "
              "again. Raise 'LISFLOOD-FP: wall-clock budget for all runs' "
              "(LISFLOOD_MAX_TOTAL_WALL_S; 0 = automatic, Most LISFLOOD-FP runs x the per-run "
              "limit) to let it go on.")
    md2, r2 = _render_with(rep, [budget])
    assert "## Results cut off" in md2 and r2["cut_off"]["reason"] == "wall-clock budget", r2
    assert "The wall-clock budget for the domain-growth loop stopped the run" in md2
    # the clock-extension limit matches by name, not by the word "edge" in its sentence
    w = W.clock_extension_cap_warning(4, 21.9, "5 ac").replace("near the edge of the wet area", "")
    md2, r2 = _render_with(rep, [w])
    assert r2["cut_off"]["reason"] == "clock-extension limit", r2["cut_off"]
    # a run that reuses a fixed clock and is still spreading at its end
    md2, r2 = _render_with(rep, [W.fixed_clock_spreading_warning(6.5, "5 ac")])
    assert r2["cut_off"]["reason"] == "fixed clock" and "## Results cut off" in md2, r2["cut_off"]
    # and a generic "still spreading" sentence still counts as cut off
    md2, r2 = _render_with(rep, ["The flood was still spreading somewhere."])
    assert r2["cut_off"]["reason"] == "still moving", r2["cut_off"]
    md2, r2 = _render_with(rep, ["Nothing to see."])
    assert "cut_off" not in r2 and "## Results cut off" not in md2


@register
def spread_report_clock_rule_lines_say_when_the_pass_was_not_a_clock_decision():
    # Review 1 finding 3: a pass followed by a 3DEP refill, and a pass after which the domain
    # grew, did not decide a clock extension; the "Clock rule" lines say so
    calls, logs, rep, md, res = _loop("creep", sim_time_hr=None)
    p1 = dict(rep["lisflood_runs"][0])
    sp = dict(p1["spread"], still_spreading=True, reason="5 ac first wet late")
    runs = [dict(p1, run=1, followed_by_dem_refill=True, spread=sp),
            dict(p1, run=1, **{"pass": 2}, spread=sp),
            dict(p1, run=2, **{"pass": 3})]
    md2, _ = _render_with(rep, [], runs)
    assert "- Pass 1: still spreading - 5 ac first wet late (wet cells on DEM no-data: the same " \
           "domain run was re-run on the 3DEP-refilled DEM, not a clock decision)." in md2, md2
    assert "- Pass 2: still spreading - 5 ac first wet late (an edge was over, so the domain " \
           "grew; this only set whether the clock grew 1.5x with it)." in md2, md2
    assert "- Pass 3: not spreading - only " in md2 and "or 25 ac at any share" in md2, md2


@register
def spread_loop_wall_budget_stop_with_no_edge_over_is_cut_off():
    # Review 1 finding 1, through the loop: the budget stops the first pass of a front still
    # spreading inside the domain; run_report.md has the cut-off section naming the budget
    calls, logs, rep, md, res = _loop("front", sim_time_hr=None, max_total_wall_s=1e-6)
    assert len(calls) == 1, calls
    assert rep["cut_off"]["reason"] == "wall-clock budget", rep["cut_off"]
    w = [x for x in res.warnings if "budget for the domain-growth loop" in x]
    assert len(w) == 1 and "still spreading inside the domain" in w[0], res.warnings
    assert "## Results cut off" in md and "The wall-clock budget for the domain-growth loop" in md


@register
def spread_loop_fixed_plan_run_warns_when_still_spreading():
    # Review 2 finding 3: a run that repeats a plan (probabilistic / sensitivity case, finer-grid
    # re-run) keeps the plan's clock; it now says when the flood is still spreading at its end
    calls, logs, rep, md, res = _loop("creep", sim_time_hr=None)
    plan = W._plan_copy(res.plan)
    calls2, logs2, rep2, md2, res2 = _loop("front", sim_time_hr=None, fixed_plan=plan)
    assert len(calls2) == 1 and abs(calls2[0] - plan["sim_time"] / 3600.0) < 1e-6, calls2
    w = [x for x in res2.warnings if "end of the fixed clock" in x]
    assert len(w) == 1 and "still spreading" in w[0], res2.warnings
    assert "## Results cut off" in md2
    # not spreading at the end: no such warning
    calls3, logs3, rep3, md3, res3 = _loop("creep", sim_time_hr=None, fixed_plan=plan)
    assert not any("end of the fixed clock" in x for x in res3.warnings), res3.warnings


@register
def spread_hecras_window_uses_the_clock_when_the_arrival_is_a_lower_bound():
    # Review 2 finding 2: HEC-RAS sized its window from the latest arrival; a lower-bound
    # arrival now gives way to the LISFLOOD-FP clock (the code path in build_model)
    from nd_breach_runner.core import hecras_model as H
    assert H.window_arrival_hr({"latest_arrival_hr": 8.9}) == (8.9, "")
    assert H.window_arrival_hr({}) == (0.0, "")
    s = {"latest_arrival_hr": 8.9, "latest_arrival_lower_bound": True, "lisflood_clock_hr": 9.7}
    hr, note = H.window_arrival_hr(s)
    assert hr == 9.7 and "lower bound" in note and "(9.7 h)" in note, (hr, note)
    # the clock only stands in when it is later (an arrival from a longer final model stays)
    assert H.window_arrival_hr(dict(s, latest_arrival_hr=12.0)) == (12.0, "")
    assert H.window_arrival_hr(dict(s, latest_arrival_lower_bound=False))[0] == 8.9
    assert H.simulation_hours(2.0, 9.7)[0] >= H.simulation_hours(2.0, 8.9)[0]
