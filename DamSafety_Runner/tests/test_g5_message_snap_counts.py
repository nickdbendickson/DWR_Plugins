"""3.10.95 (DWR-3034 REVIEW 9.8 items 2 and 6).

G5-MSG: the ANUGA volume note named the mesh edge or an instability for DWR-3034's +4.6 % gain,
which was the breach inlet's own integration over ANUGA's first 300 s step (edge 0, solver
residual 0, the whole gain in the fractional-step integral). The note now reports the inflow
operators' share of the gain (anuga_model.operator_share) and names a long first step
(anuga_worker.FirstStep -> volume_balance['first_step'], anuga_model.first_step_cause).

BRIDGE-SNAP-COUNTS: one breakdown of the bridge rows, kept_verified / kept_unverified / none /
moved by road and rail (bridge_snap.snap_counts), in run_report.json, the run report and the log.
"""
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
from nd_breach_runner.core import bridge_snap as BS  # noqa: E402
from nd_breach_runner.tests.registry import test, model_test  # noqa: E402

# DWR-3034 (main a8d3a72): supplied 88,186.4, in domain 92,251.1, edge 0, residual 0
SUP, VIN, EXCESS = 88186.4, 92251.1, 4064.8


def _3034_stats(first_step=True, operators=True, edge=0.0):
    vb = AW.volume_balance(0.0, SUP, VIN, boundary_in=edge,
                           operators=SUP + EXCESS if operators else None, solver_volume=VIN)
    if first_step:
        vb["first_step"] = {"first_yield_s": 300.0, "steps_in_first_yield": 1, "dt_s": 300.0,
                            "inlet_booked_m3": 9994.1, "hydrograph_m3": 5929.3,
                            "excess_m3": 4064.8}
    return {"volume_balance": vb, "boundary": "outflow", "injected_m3": SUP}


@test
def g5_note_names_the_inlet_and_the_first_step_not_the_edge():
    st = _3034_stats()
    sh = AM.operator_share(st, st["volume_balance"])
    assert abs(sh["excess_m3"] - EXCESS) < 0.2 and abs(sh["share"] - 1.0) < 1e-3, sh
    n = AM.volume_note(st)
    assert n.startswith("ANUGA volume check: the domain ends with 1.05 x"), n
    assert "the breach inlet added it" in n, n
    assert "added 92,251 m3 against the hydrograph's 88,186 m3, so 4,065 m3 = 100% of the gain" \
        in n, n
    assert "(edge: 0 m3 net)" in n, n
    assert "one 300 s step" in n and "booked 9,994 m3 for the 5,929 m3" in n \
        and "4,065 m3 extra" in n, n
    assert "conservative" in n and "3.10.94" in n, n
    for bad in ("instability", "mesh edge with caution", "came in there", "most likely"):
        assert bad not in n, (bad, n)


@test
def g5_note_without_the_first_step_record_still_gives_the_operator_share():
    n = AM.volume_note(_3034_stats(first_step=False))
    assert "100% of the gain is the inlet's own time integration" in n and "300 s" not in n, n
    assert "instability" not in n, n


@test
def g5_note_edge_gain_and_old_results_unchanged():
    # the edge brought it in: the edge sentence and the caution, as before
    st = _3034_stats(first_step=False, operators=True, edge=EXCESS)
    st["volume_balance"]["operators_m3"] = SUP           # the inlet added the hydrograph exactly
    n = AM.volume_note(st)
    assert "net inflow through the mesh edge" in n and "with caution" in n, n
    assert "inlet added it" not in n, n
    # no flux integral and no operators (old results): the edge is the likely cause, no
    # instability is named; a first-step record is named as the alternative
    old = {"volume_balance": AW.volume_balance(0.0, SUP, VIN), "boundary": "transmissive",
           "injected_m3": SUP}
    n = AM.volume_note(old)
    assert "most likely came in through the mesh edge (transmissive boundary)" in n, n
    assert "instability" not in n, n
    old["volume_balance"]["first_step"] = _3034_stats()["volume_balance"]["first_step"]
    n = AM.volume_note(old)
    assert "unless the first step explains it: ANUGA's first internal step" in n, n
    # a gain the operators do not explain, with a large solver residual: that line names it
    bad = AW.volume_balance(0.0, SUP, SUP * 1.05, boundary_in=0.0, operators=SUP,
                            solver_volume=SUP * 1.05)
    n = AM.volume_note({"volume_balance": bad, "boundary": "outflow", "injected_m3": SUP})
    assert "probably not from the edge" in n and "inflow operators account for 0 m3 (0%)" in n, n
    assert "own balance is off too" in n and "points at an instability" in n, n


@test
def first_step_record_exact_for_one_step_and_none_otherwise():
    t = np.arange(0.0, 861.0, 2.14)
    q = np.interp(t, [0.0, 300.0, 861.0], [0.0, 66.63, 0.0])
    fs = AW.FirstStep(0.0, t, q)
    fs.on_yield(0.0, 0)                       # evolve's first yield is the start: ignored
    assert fs.record() is None and not fs.done
    fs.on_yield(300.0, 1)
    r = fs.record()
    q300 = float(np.interp(300.0, t, q))
    assert r["dt_s"] == 300.0 and r["steps_in_first_yield"] == 1, r
    assert abs(r["inlet_booked_m3"] - 0.5 * q300 * 300.0) < 0.1, r
    assert abs(r["hydrograph_m3"] - AW.hydrograph_integral(t, q, 0.0, 300.0)) < 0.1, r
    assert abs(r["excess_m3"] - (r["inlet_booked_m3"] - r["hydrograph_m3"])) < 0.2, r
    fs.on_yield(600.0, 1)                     # only the first yield counts
    assert fs.record() is r
    many = AW.FirstStep(0.0, t, q)
    many.on_yield(300.0, 140)
    assert many.record()["dt_s"] is None and "excess_m3" not in many.record()
    res = AW.FirstStep(1200.0, t, q, resumed=True)
    res.on_yield(1500.0, 1)
    assert res.record() is None
    assert AM.first_step_cause({"first_step": many.record()}) == ""
    assert abs(AW.hydrograph_integral([0, 10, 20], [0, 10, 0], 0, 30) - 100.0) < 1e-9
    assert abs(AW.hydrograph_integral([0, 10, 20], [0, 10, 0], 5, 15) - 75.0) < 1e-9


def _row(method, verified, carries):
    return {"name": "b", "method": method, "verified": verified, "carries": carries,
            "line": carries, "moved_m": 0.0, "old_xy": None, "new_xy": None, "note": ""}


def _3034_rows():
    rows = ([_row("kept", True, "road")] * 24 + [_row("kept", True, "rail")] * 25
            + [_row("kept", False, "road")] * 5 + [_row("kept", False, "rail")]
            + [_row("none", False, "road")] * 2 + [_row("dem_opening", True, "road")])
    return [dict(r) for r in rows]


@test
def bridge_snap_counts_one_breakdown_road_and_rail():
    c = BS.snap_counts(_3034_rows())
    want = {"kept_verified": (49, 24, 25), "kept_unverified": (6, 5, 1), "none": (2, 2, 0),
            "moved": (1, 1, 0)}
    for k, (tot, road, rail) in want.items():
        assert (c[k]["total"], c[k]["road"], c[k]["rail"]) == (tot, road, rail), (k, c)
    txt = BS.snap_counts_text(c)
    assert txt == ("49 kept, verified (24 road + 25 rail); 6 kept, not verified (5 road + "
                   "1 rail); 2 not matched to a line (2 road + 0 rail); 1 moved (1 road + "
                   "0 rail)"), txt
    # unknown carries are counted apart; the totals add up to the rows
    r = BS.snap_counts([_row("kept", True, ""), _row("channel_crossing", True, "rail")])
    assert r["kept_verified"]["other"] == 1 and r["moved"]["rail"] == 1, r
    assert "1 kept, verified (0 road + 0 rail + 1 unknown)" in BS.snap_counts_text(r)
    # the run report: from the entry's counts, or from the rows of an older run_report.json
    rows = _3034_rows()
    for entry in ({"radius_m": 300.0, "bridges": rows, "counts": BS.snap_counts(rows)},
                  {"radius_m": 300.0, "bridges": rows, "moved": 1, "kept_verified": 49,
                   "not_verified": 8}):
        lines = BS.report_lines(entry)
        assert "58 bridge(s): " + txt + "." in lines, lines[:6]


@model_test
def g5_first_step_recorded_by_the_real_worker():
    """A ~400-triangle bowl (nothing leaves), a convex rise sampled every 2 s, yield 300 s,
    run with the step cap off (NDBREACH_ANUGA_INFLOW_MAX_DT=0; Runner 3.10.94): the first
    yield is one 300 s step, its booked excess is exactly the gain, and the note says so."""
    py = os.environ.get("NDBREACH_ANUGA_PYTHON")
    if not (py and os.path.isfile(py)):
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    ny, nx, dx = 30, 50, 20.0
    X, Y = np.meshgrid((np.arange(nx) + 0.5) * dx, (np.arange(ny) + 0.5) * dx)
    dem = 100.0 + 1e-4 * ((X - 500.0) ** 2 + (Y - 300.0) ** 2)
    x0, y0 = 500000.0, 5200600.0
    t = np.arange(0.0, 900.0 + 1e-9, 2.0)
    q = np.clip(np.where(t <= 300.0, 60.0 * (t / 300.0) ** 3, 60.0 * (900.0 - t) / 600.0), 0, None)
    d = tempfile.mkdtemp()
    try:
        np.savez(os.path.join(d, "j.npz"), dem=dem.astype(np.float32), dem_x0=x0, dem_y0=y0,
                 dem_dx=dx, manning_n=0.05, manning=np.zeros(0),
                 boundary=np.array([[x0 + 10, y0 - 590], [x0 + 990, y0 - 590],
                                    [x0 + 990, y0 - 10], [x0 + 10, y0 - 10]]),
                 fine=np.zeros((0, 2)), inlet=np.array([[x0 + 500, y0 - 270],
                                                        [x0 + 500, y0 - 330]]),
                 fine_area=500.0, coarse_area=1500.0, hyd_t=t, hyd_q=q, sim_time=900.0,
                 yieldstep=300.0, arrival_depth=0.3048, velocity_min_depth=0.03,
                 algorithm="DE0", threads=1, out_x0=x0, out_y0=y0, out_dx=dx, out_nx=nx,
                 out_ny=ny)
        env = dict(AM._clean_env(), OMP_NUM_THREADS="1", NDBREACH_ANUGA_INFLOW_MAX_DT="0")
        subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d, env=env, check=True,
                       stdout=subprocess.DEVNULL, timeout=600)
        with np.load(os.path.join(d, "r.npz")) as z:
            st = json.loads(str(z["stats"]))
        vb = st["volume_balance"]
        f = vb["first_step"]
        assert f["steps_in_first_yield"] == 1 and f["dt_s"] == 300.0, f
        assert abs(f["inlet_booked_m3"] - 9000.0) < 1.0 and abs(f["hydrograph_m3"] - 4500.0) \
            < 1.0, f
        gain = vb["in_domain_m3"] - vb["supplied_m3"]
        assert abs(gain - f["excess_m3"]) < 0.005 * vb["supplied_m3"], (gain, f)
        n = AM.volume_note(st)
        assert n and "the breach inlet added it" in n and "one 300 s step" in n, n
        assert "instability" not in n, n
        print("first step: " + json.dumps(f))
    finally:
        shutil.rmtree(d)
