"""3.101.0: a few cells that blow up while the rest of the peak water surface is sound are reported.

Absaraka (DWR-2801) at 30 m, Boann ACC on Runner 3.57.0: one flat water-body cell in Casselton read
17.33 ft and 20 ft/s where every other engine read 5.6 to 6.9 ft, and that cell alone made the run's
TM-11 High. The 3.88.0 checkerboard re-run needs 10 rough cells, so one cell never reached it. Now
every run's peak surface is searched for spikes (lisflood.spikes: a deep cell whose peak level
stands more than 0.5 m above, or below, all four deep neighbours, away from the inflow cells), and a
run that has any, and is not already flagged as a checkerboard, gets a warning saying where they
are. Nothing is run again and no result moves. LISFLOOD-FP is stubbed as in test_v3880."""
import contextlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import _dam, _settings
from nd_breach_runner.tests.test_v3880_checkerboard import _pool


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v31010_")


@test
def v31010_a_sound_surface_has_no_spike():
    from nd_breach_runner.core import lisflood as lf
    w, d = _pool()
    assert lf.spikes(w, d) == (0, [])
    # a steep even slope (1 m per cell) has no cell above or below ALL four neighbours
    ramp = (100.0 + np.arange(80, dtype=np.float32))[None, :] * np.ones((60, 1), np.float32)
    assert lf.spikes(np.where(d > 0, ramp, np.float32(np.nan)), d) == (0, [])
    # a bump under SPIKE_JUMP_M is not a spike (the checkerboard's 0.1 m is far below it)
    w2 = w.copy()
    w2[30, 40] += 0.4
    assert lf.spikes(w2, d) == (0, [])
    assert lf.SPIKE_JUMP_M == 0.5 and lf.SPIKE_SOURCE_CELLS == 3 and lf.SPIKE_LIST_MAX == 5


@test
def v31010_one_cell_is_found_up_or_down_and_where():
    from nd_breach_runner.core import lisflood as lf
    w, d = _pool()
    w[30, 40] += 3.4                              # Absaraka's Casselton cell: about 11 ft up
    w[10, 10] -= 0.8                              # a hole in the surface
    n, where = lf.spikes(w, d)
    assert n == 2, (n, where)
    assert where[0][:2] == (30, 40) and abs(where[0][2] - 3.4) < 1e-4, where
    assert where[1][:2] == (10, 10) and abs(where[1][2] + 0.8) < 1e-4, where
    # the checkerboard share is far too small to re-run for: this is the gap it fills
    pct, n_j, rough = lf.checkerboard(w, d)
    assert rough == 2 and not lf.is_checkerboard({"checkerboard_pct": pct,
                                                  "checkerboard_rough_cells": rough})


@test
def v31010_inflow_cells_and_shallow_water_are_left_out():
    from nd_breach_runner.core import lisflood as lf
    w, d = _pool()
    w[20, 20] += 3.0                              # the mound on an inflow cell
    w[20, 23] += 2.0                              # 3 cells away: still the inflow's
    w[24, 20] += 2.0                              # 4 cells away: counted
    n, where = lf.spikes(w, d, sources=[(20, 20)])
    assert n == 1 and where[0][:2] == (24, 20), where
    assert lf.spikes(w, d)[0] == 3
    # shallow water (under CHECKER_MIN_DEPTH_M) is not judged
    w, d = _pool(depth=0.2)
    w[30, 40] += 3.0
    assert lf.spikes(w, d) == (0, [])
    # arrays that do not fit are left alone
    assert lf.spikes(np.zeros((3, 4)), np.zeros((4, 3))) == (0, [])
    assert lf.spikes(np.zeros((2, 50)), np.zeros((2, 50))) == (0, [])


@test
def v31010_the_list_is_short_and_largest_first():
    from nd_breach_runner.core import lisflood as lf
    w, d = _pool()
    for k in range(8):
        w[10 + 5 * (k % 4), 10 + 20 * (k // 4)] += 1.0 + k
    n, where = lf.spikes(w, d)
    assert n == 8 and len(where) == lf.SPIKE_LIST_MAX
    assert [round(j) for _r, _c, j in where] == [8, 7, 6, 5, 4], where


@test
def v31010_the_inflow_cells_are_read_from_the_bci():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.grid import Grid
    d = _tmp()
    try:
        dem = Grid(np.zeros((40, 60)), 0.0, 400.0, 10.0)
        loc = lf.local_grid(dem)
        p = os.path.join(d, "run.bci")
        with open(p, "w") as f:
            for r, c in ((5, 7), (5, 8)):
                f.write("P {:.3f} {:.3f} QVAR breach\n".format(*loc.xy(r, c)))
            f.write("N 0.000 600.000 FREE\nP 15.000 395.000 QVAR src1\n")
        assert lf.bci_sources(p, dem) == [(5, 7), (5, 8), (0, 1)]
        assert lf.bci_sources(os.path.join(d, "none.bci"), dem) == []
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v31010_read_results_reports_the_spikes_in_feet_on_the_dem():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    d = _tmp()
    try:
        ny, nx = 40, 60
        dem = Grid(np.full((ny, nx), 100.0), 1000.0, 5000.0 + ny * 10.0, 10.0)
        hy = Hydrograph(np.array([0.0, 600.0]), np.array([10.0, 0.0]), "t")
        st = lf.LisfloodSettings("x")
        res = os.path.join(d, "results")
        os.makedirs(res)
        depth = np.full((ny, nx), 4.0)
        depth[20, 30] += 3.0                     # a spike
        depth[5, 5] += 3.0                       # the inflow cell's mound
        dem.like(depth).write_asc(os.path.join(res, "run.max"))
        dem.like(100.0 + depth).write_asc(os.path.join(res, "run.mxe"))
        with open(os.path.join(res, "run.mass"), "w") as f:
            f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror "
                    "Rain-Inf+Evap\n")
        with open(os.path.join(d, "run.bci"), "w") as f:
            f.write("P {:.3f} {:.3f} QVAR breach\n".format(*lf.local_grid(dem).xy(5, 5)))
        s = lf.read_results(d, dem, st, hy).stats
        assert s["spike_cells"] == 1, s
        x, y, j = s["spikes"][0]
        assert (x, y) == dem.xy(20, 30) and abs(j - 3.0 / 0.3048) < 0.01, s["spikes"]
        assert not lf.is_checkerboard(s)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _models(calls, spike_cells=1, rough_pct=0.0, long_flood=False):
    """LISFLOOD-FP stubbed as in test_v3880: every run has `spike_cells` spikes and a checkerboard
    share of `rough_pct` % (the CFL never changes either)."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        calls.append((os.path.basename(wd), st.theta, st.cfl))
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        dd = np.where((X > 0) & (X < (2500 if long_flood else 1000)) & (np.abs(Y) < 300),
                      0.5, np.nan)
        vel = np.where(np.isfinite(dd), 1.2, np.nan)
        arr = np.where(np.isfinite(dd), 0.5, np.nan)
        wse = np.where(np.isfinite(dd), dem.data + 0.5, np.nan)
        stats = {"max_volume_error_pct": 0.0, "peak_inflow_cfs": 1000.0,
                 "checkerboard_pct": rough_pct, "checkerboard_cells": 50000,
                 "checkerboard_rough_cells": int(500 * rough_pct),
                 "spike_cells": spike_cells,
                 "spikes": [[500.0 + 30.0 * k, 12.5, 11.2 - k] for k in range(min(spike_cells, 5))]}
        return lf.Envelopes(dem.like(dd, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan), stats, [], {})
    old = (lf.run, lf.read_results)
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


def _run(folder, st, **kw):
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    calls = []
    with _models(calls, **kw):
        res = wf.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer, folder)
    with open(res.report_json, encoding="utf-8") as f:
        return json.load(f), calls


@test
def v31010_a_run_with_a_spike_is_flagged_not_rerun():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1), spike_cells=1)
        assert calls == [("lisflood", 1.0, None)], calls          # nothing run again
        assert "lisflood_stability" not in r and "lisflood_checkerboard" not in r
        assert r["lisflood_spikes"] == {"run": "lisflood", "cells": 1,
                                        "where_x_y_jump_ft": [[500.0, 12.5, 11.2]]}, r
        w = [x for x in r["warnings"] if "isolated spike" in x]
        assert len(w) == 1, r["warnings"]
        assert "1 isolated spike in lisflood:" in w[0] and "+11.2 ft at 500, 12" in w[0], w
        assert "that cell" in w[0] and "smaller CFL" in w[0] and "1.6 ft" in w[0], w
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v31010_no_spike_no_warning_and_a_checkerboard_is_not_flagged_twice():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1), spike_cells=0)
        assert "lisflood_spikes" not in r
        assert not any("isolated spike" in x for x in r["warnings"])
    finally:
        shutil.rmtree(d, ignore_errors=True)
    d = _tmp()
    try:
        # checkerboarded (re-run at CFL 0.5, still rough in the stub): its own warning only
        r, calls = _run(d, _settings(max_iterations=1), spike_cells=40, rough_pct=2.0)
        assert len(calls) == 2 and "lisflood_spikes" not in r, calls
        assert not any("isolated spike" in x for x in r["warnings"])
        assert any("checkerboard" in x for x in r["warnings"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v31010_several_passes_leave_one_warning():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=3), spike_cells=3, long_flood=True)
        assert len(calls) >= 2, calls
        w = [x for x in r["warnings"] if "isolated spike" in x]
        assert len(w) == 1 and "3 isolated spikes" in w[0] and "those cells" in w[0], w
        assert r["lisflood_spikes"]["cells"] == 3
    finally:
        shutil.rmtree(d, ignore_errors=True)

