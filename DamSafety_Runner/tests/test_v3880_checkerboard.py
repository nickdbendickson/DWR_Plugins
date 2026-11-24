"""3.88.0: an ACC run that goes unstable WITHOUT losing its mass balance is caught and run again.

Lake Delhi 2010 at 10 m (4.6 M cells, Boann ACC with LISFLOOD-FP's defaults, theta 1 / cfl 0.7):
at the breach peak the water level checkerboards in the deep pool, the deepest water reads 38 m
against 13.5 m, the run is 8.1 ft high at the 8 USGS high-water marks - and its volume error is
703 m3 of 1.1e8, so the 3.15.1 mass-balance re-run (over 1 %) never fires. CFL 0.5 alone cures it
(+0.34 ft at the marks). The test is on the peak water surface: the share of deep cells that stand
above, or below, all four neighbours (lisflood.checkerboard). Over the threshold, a plain ACC run
is run again with CFL 0.5 (theta left alone) and every later run of the dam keeps it; where a
re-run cannot help (retry off, CFL already small, a shock engine) the run is flagged instead.
LISFLOOD-FP is stubbed as in test_v3151."""
import contextlib
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import _dam, _settings


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_v3880_")


def _pool(ny=60, nx=80, depth=5.0):
    """A level pool: (wse, depth) with a dry rim."""
    d = np.full((ny, nx), depth, dtype=np.float32)
    d[0, :] = d[-1, :] = d[:, 0] = d[:, -1] = 0.0
    w = np.where(d > 0, np.float32(100.0), np.float32(np.nan)).astype(np.float32)
    return w, d


@test
def v3880_a_smooth_peak_surface_has_no_rough_cell():
    from nd_breach_runner.core import lisflood as lf
    w, d = _pool()
    pct, n, rough = lf.checkerboard(w, d)
    assert pct == 0.0 and rough == 0 and n == (60 - 4) * (80 - 4), (pct, n, rough)
    # a steep, even water slope (1 m per cell) is not a checkerboard: no cell is above or below
    # ALL four neighbours
    ramp = (100.0 + np.arange(80, dtype=np.float32))[None, :] * np.ones((60, 1), np.float32)
    w2 = np.where(d > 0, ramp, np.float32(np.nan))
    assert lf.checkerboard(w2, d)[0] == 0.0
    assert not lf.is_checkerboard({"checkerboard_pct": 0.0, "checkerboard_rough_cells": 0})
    assert not lf.is_checkerboard({}) and not lf.is_checkerboard(None)
    assert not lf.is_checkerboard({"checkerboard_pct": None})


@test
def v3880_a_checkerboarded_pool_is_seen():
    from nd_breach_runner.core import lisflood as lf
    w, d = _pool()
    yy, xx = np.indices(w.shape)
    saw = np.where((yy + xx) % 2 == 0, 1.0, -1.0).astype(np.float32)      # +-1 m, cell to cell
    w2 = np.where(d > 0, w + saw, np.float32(np.nan))
    pct, n, rough = lf.checkerboard(w2, d)
    assert pct == 100.0 and rough == n, (pct, n, rough)
    assert lf.is_checkerboard({"checkerboard_pct": pct, "checkerboard_rough_cells": rough})
    # a ripple under the jump (0.1 m) is not counted
    w3 = np.where(d > 0, w + 0.04 * saw, np.float32(np.nan))
    assert lf.checkerboard(w3, d)[0] == 0.0
    # only a patch of the pool: the share is the patch
    w4 = w.copy()
    w4[20:30, 20:40] += saw[20:30, 20:40]
    pct4, n4, rough4 = lf.checkerboard(w4, d)
    assert rough4 == 200 and abs(pct4 - 100.0 * 200 / n4) < 1e-3, (pct4, n4, rough4)
    assert lf.is_checkerboard({"checkerboard_pct": pct4, "checkerboard_rough_cells": rough4})


@test
def v3880_a_few_cells_shallow_water_or_a_small_flood_do_not_count():
    from nd_breach_runner.core import lisflood as lf
    # the mound on an inflow cell: a handful of cells, under CHECKER_MIN_ROUGH
    w, d = _pool(ny=40, nx=40)
    for r, c in ((10, 10), (10, 20), (20, 10), (20, 20), (30, 30)):
        w[r, c] += 3.0
    pct, n, rough = lf.checkerboard(w, d)
    assert rough == 5 and pct > lf.CHECKER_RETRY_PCT, (pct, n, rough)
    assert not lf.is_checkerboard({"checkerboard_pct": pct, "checkerboard_rough_cells": rough})
    # rough water that is shallow (under CHECKER_MIN_DEPTH_M) is thin-film noise, not judged
    w, d = _pool(depth=0.2)
    yy, xx = np.indices(w.shape)
    w = np.where(d > 0, w + np.where((yy + xx) % 2 == 0, 1.0, -1.0), np.nan).astype(np.float32)
    assert lf.checkerboard(w, d) == (None, 0, 0)
    # too few deep cells to judge: no share, never a re-run
    w, d = _pool(ny=10, nx=10)
    pct, n, rough = lf.checkerboard(w, d)
    assert pct is None and n == 36 and n < lf.CHECKER_MIN_CELLS
    # arrays that do not fit are left alone
    assert lf.checkerboard(np.zeros((3, 4)), np.zeros((4, 3))) == (None, 0, 0)
    assert lf.checkerboard(np.zeros((2, 50)), np.zeros((2, 50))) == (None, 0, 0)


@test
def v3880_the_rerun_courant_number():
    from nd_breach_runner.core import lisflood as lf
    assert (lf.CHECKER_CFL, lf.DEFAULT_CFL, lf.CHECKER_RETRY_PCT) == (0.5, 0.7, 0.05)
    assert lf.checker_cfl() == 0.5 and lf.checker_cfl(None) == 0.5
    assert lf.checker_cfl(0.9) == 0.5 and lf.checker_cfl(0.3) == 0.3


@test
def v3880_read_results_measures_the_peak_surface():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    d = _tmp()
    try:
        ny, nx = 40, 60
        dem = Grid(np.full((ny, nx), 100.0), 0.0, ny * 10.0, 10.0)
        hy = Hydrograph(np.array([0.0, 600.0]), np.array([10.0, 0.0]), "t")
        st = lf.LisfloodSettings("x")
        res = os.path.join(d, "results")
        os.makedirs(res)
        yy, xx = np.indices((ny, nx))
        for rough_m, want in ((0.0, False), (0.6, True)):
            saw = np.where((yy + xx) % 2 == 0, rough_m, -rough_m)
            dem.like(np.full((ny, nx), 4.0) + saw).write_asc(os.path.join(res, "run.max"))
            dem.like(np.full((ny, nx), 104.0) + saw).write_asc(os.path.join(res, "run.mxe"))
            with open(os.path.join(res, "run.mass"), "w") as f:
                f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror "
                        "Rain-Inf+Evap\n")
            s = lf.read_results(d, dem, st, hy).stats
            assert s["checkerboard_cells"] == (ny - 2) * (nx - 2), s
            assert lf.is_checkerboard(s) is want, s
            assert (s["checkerboard_pct"] == 100.0) is want
    finally:
        shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _models(calls, rough_until_cfl=0.5, rough_pct=2.0, error_pct=0.0, mass_bad_at=None,
            long_flood=False, fail_at=None):
    """LISFLOOD-FP stubbed as in test_v3151. The peak surface is checkerboarded (`rough_pct` % of
    the deep cells) while the run's CFL is above `rough_until_cfl` (None = always rough); the
    volume error is `error_pct` (and 50 % on call number `mass_bad_at`, counted from 1, while
    theta is 1). `long_flood`: the flood reaches the first domain's downstream edge, so the
    domain is extended and run again. `fail_at`: that call (from 1) dies, as a crash would.
    `calls` gets (folder, theta, cfl) for every run."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        calls.append((os.path.basename(wd), st.theta, st.cfl))
        if fail_at is not None and len(calls) == fail_at:
            raise lf.ModelError("stub: the flood model died")
        return 60.0

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        dd = np.where((X > 0) & (X < (2500 if long_flood else 1000)) & (np.abs(Y) < 300),
                      0.5, np.nan)
        vel = np.where(np.isfinite(dd), 1.2, np.nan)
        arr = np.where(np.isfinite(dd), 0.5, np.nan)
        wse = np.where(np.isfinite(dd), dem.data + 0.5, np.nan)
        cfl = st.cfl if st.cfl is not None else 0.7
        rough = rough_until_cfl is None or cfl > rough_until_cfl + 1e-9
        err = error_pct
        if mass_bad_at is not None and len(calls) == mass_bad_at and st.theta == 1.0:
            err = 50.0
        stats = {"max_volume_error_pct": err, "peak_inflow_cfs": 1000.0,
                 "checkerboard_pct": rough_pct if rough else 0.0,
                 "checkerboard_cells": 50000,
                 "checkerboard_rough_cells": int(500 * rough_pct) if rough else 0}
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


def _marker(folder):
    return os.path.join(folder, "_breach_model", "lisflood", "stable_settings.json")


@test
def v3880_a_sound_run_is_not_changed():
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=2), rough_pct=0.0)
        assert calls and all(c[1:] == (1.0, None) for c in calls), calls
        assert "lisflood_stability" not in r and "lisflood_checkerboard" not in r
        assert not any("checkerboard" in w for w in r["warnings"])
        assert not os.path.exists(_marker(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_a_checkerboarded_run_is_run_again_with_cfl_only():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=3), long_flood=True)
        # the first run, then the same run again with CFL 0.5 and theta untouched
        assert calls[0][1:] == (1.0, None) and calls[1][1:] == (1.0, 0.5), calls[:2]
        assert calls[0][0] == calls[1][0] == "lisflood"
        # every later run of the dam keeps it (the extended domain: no second default run)
        assert len(calls) >= 3 and all(c[1:] == (1.0, 0.5) for c in calls[1:]), calls
        ls = r["lisflood_stability"]
        assert ls["stable_settings"] and ls["kind"] == lf.STABILITY_KIND_CHECKER
        assert ls["theta"] == 1.0 and ls["cfl"] == 0.5 and len(ls["events"]) == 1
        ev = ls["events"][0]
        assert ev["kind"] == lf.STABILITY_KIND_CHECKER and ev["cfl"] == 0.5
        assert ev["checkerboard_pct"] == 2.0 and ev["stable_checkerboard_pct"] == 0.0, ev
        w = [x for x in r["warnings"] if "checkerboard" in x]
        assert len(w) == 1 and "2.00%" in w[0] and "CFL 0.5" in w[0] and "0.00% now" in w[0], w
        assert "although its volume balanced" in w[0]
        assert not any("lost its mass balance" in x for x in r["warnings"])
        assert "lisflood_checkerboard" not in r            # cured: nothing left to flag
        with open(_marker(d)) as f:
            m = json.load(f)
        assert m == {"theta": 1.0, "cfl": 0.5, "kind": lf.STABILITY_KIND_CHECKER}, m
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_the_users_theta_is_kept_and_a_small_cfl_is_not_raised():
    d = _tmp()
    try:
        st = _settings(max_iterations=1, user_theta=0.9)
        r, calls = _run(d, st)
        assert calls == [("lisflood", 0.9, None), ("lisflood", 0.9, 0.5)], calls
    finally:
        shutil.rmtree(d, ignore_errors=True)
    d = _tmp()
    try:
        # the user's CFL 0.6 is rough; the re-run goes down to 0.5
        r, calls = _run(d, _settings(max_iterations=1, user_cfl=0.6))
        assert calls == [("lisflood", 1.0, 0.6), ("lisflood", 1.0, 0.5)], calls
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_no_rerun_where_it_cannot_help_but_the_run_is_flagged():
    from nd_breach_runner.core import lisflood as lf
    # the retry switched off
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1, lisflood_stability_retry=False))
        assert calls == [("lisflood", 1.0, None)], calls
        assert "lisflood_stability" not in r
        assert r["lisflood_checkerboard"] == {"run": "lisflood", "pct": 2.0, "cells": 1000}
        w = [x for x in r["warnings"] if "is a checkerboard" in x]
        assert len(w) == 1 and "2.00%" in w[0] and "smaller CFL" in w[0], r["warnings"]
        assert not os.path.exists(_marker(d))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # the user's CFL is already at or under the re-run's: nothing to try
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1, user_cfl=0.4), rough_until_cfl=None)
        assert calls == [("lisflood", 1.0, 0.4)], calls
        assert "lisflood_stability" not in r and r["lisflood_checkerboard"]["pct"] == 2.0
        assert sum("is a checkerboard" in x for x in r["warnings"]) == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # a shock-capturing Boann engine (its Courant number is clamped to its own ceiling and the
    # measure was not tried on it): flagged, not re-run
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1, lisflood_exe=lf.GPU_FV2_EXE))
        assert len(calls) == 1, calls
        assert "lisflood_stability" not in r and "lisflood_checkerboard" in r
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_still_rough_after_the_rerun_is_reported_not_hidden_and_never_loops():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1), rough_until_cfl=None)
        assert calls == [("lisflood", 1.0, None), ("lisflood", 1.0, 0.5)], calls
        ev = r["lisflood_stability"]["events"][0]
        assert ev["stable_checkerboard_pct"] == 2.0
        assert any("2.00% now" in x for x in r["warnings"])
        assert sum("is a checkerboard" in x for x in r["warnings"]) == 1
        assert r["lisflood_checkerboard"]["run"] == "lisflood"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # several passes, always rough: one re-run in all, and one flag (the last pass's), not one
    # per pass
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=3), rough_until_cfl=None, long_flood=True)
        assert len(calls) >= 3, calls
        assert calls[0][1:] == (1.0, None) and all(c[1:] == (1.0, 0.5) for c in calls[1:])
        assert len(r["lisflood_stability"]["events"]) == 1
        assert sum("is a checkerboard" in x for x in r["warnings"]) == 1, r["warnings"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_a_lost_mass_balance_still_gets_the_full_stable_settings():
    from nd_breach_runner.core import lisflood as lf
    # the first run both makes water and checkerboards: the 3.15.1 re-run (theta and CFL) wins,
    # and one re-run is all it takes
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=1), mass_bad_at=1)
        assert calls == [("lisflood", 1.0, None), ("lisflood", 0.7, 0.5)], calls
        ls = r["lisflood_stability"]
        assert ls["kind"] == lf.STABILITY_KIND_MASS and ls["theta"] == 0.7 and ls["cfl"] == 0.5
        assert any("lost its mass balance" in x for x in r["warnings"])
        assert not any("checkerboard" in x for x in r["warnings"])
        with open(_marker(d)) as f:
            assert json.load(f)["kind"] == lf.STABILITY_KIND_MASS
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # checkerboard first (CFL only), then a later run at CFL 0.5 makes water: the full stable
    # settings take over
    d = _tmp()
    try:
        r, calls = _run(d, _settings(max_iterations=3), mass_bad_at=3, long_flood=True)
        assert calls[:2] == [("lisflood", 1.0, None), ("lisflood", 1.0, 0.5)], calls
        assert len(calls) >= 4 and calls[2][1:] == (1.0, 0.5) and calls[3][1:] == (0.7, 0.5), calls
        assert all(c[1:] == (0.7, 0.5) for c in calls[3:]), calls
        ls = r["lisflood_stability"]
        assert ls["kind"] == lf.STABILITY_KIND_MASS and len(ls["events"]) == 2
        assert [e.get("kind", lf.STABILITY_KIND_MASS) for e in ls["events"]] == [
            lf.STABILITY_KIND_CHECKER, lf.STABILITY_KIND_MASS]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_the_marker_says_which_rerun_left_it():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core import workflow as wf
    d = _tmp()
    try:
        p = os.path.join(d, "stable_settings.json")
        with open(p, "w") as f:                      # written before 3.88.0: no "kind"
            json.dump({"theta": 0.7, "cfl": 0.5}, f)
        assert wf._stability_marker_kind(p) == lf.STABILITY_KIND_MASS
        with open(p, "w") as f:
            json.dump({"theta": 1.0, "cfl": 0.5, "kind": lf.STABILITY_KIND_CHECKER}, f)
        assert wf._stability_marker_kind(p) == lf.STABILITY_KIND_CHECKER
        with open(p, "w") as f:
            f.write("not json")
        assert wf._stability_marker_kind(p) == lf.STABILITY_KIND_MASS
        assert wf._stability_marker_kind(os.path.join(d, "none")) == lf.STABILITY_KIND_MASS
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_a_resumed_dam_keeps_the_cfl_only_settings():
    d = _tmp()
    try:
        # the first domain checkerboards and is cured; the run of the extended domain dies
        try:
            _run(d, _settings(max_iterations=3), long_flood=True, fail_at=3)
            raise AssertionError("the stub should have stopped the run")
        except Exception as e:
            assert "stub" in str(e), e
        with open(_marker(d)) as f:
            assert json.load(f)["cfl"] == 0.5
        # resumed: what it still has to run starts with CFL 0.5 and theta 1 (not the default
        # settings again, and not the mass-balance pair)
        r2, calls2 = _run(d, _settings(max_iterations=3, resume=True), long_flood=True)
        assert calls2 and all(c[1:] == (1.0, 0.5) for c in calls2), calls2
        assert r2["lisflood_stability"]["kind"] == "checkerboard"
        assert r2["lisflood_stability"]["cfl"] == 0.5 and r2["lisflood_stability"]["theta"] == 1.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def v3880_cli_keys_cfl_and_theta_and_the_checkpoint_key():
    from nd_breach_runner.core import workflow as wf
    # the settings exist (the CLI refuses a key whose run setting is missing) but stay out of the
    # checkpoint key until a run sets them, so every earlier checkpoint resumes
    st = wf.RunSettings()
    assert st.user_cfl is None and st.user_theta is None
    k = wf.key_settings(st)
    assert "user_cfl" not in k and "user_theta" not in k
    st2 = wf.RunSettings(user_cfl=0.5)
    assert wf.key_settings(st2)["user_cfl"] == 0.5 and "user_theta" not in wf.key_settings(st2)
    try:
        from nd_breach_runner.scripts import ndbreach_cli as cli
    except Exception:                                  # the CLI needs QGIS's python
        return
    assert cli.MODEL_KEYS["cfl"] == ("user_cfl", ("num", 0.05, 1.0))
    assert cli.MODEL_KEYS["theta"] == ("user_theta", ("num", 0.05, 1.0))
    cli._check_value("model.cfl", 0.5, cli.MODEL_KEYS["cfl"][1])
    for bad in (0.0, 1.5, "0.5"):
        try:
            cli._check_value("model.cfl", bad, cli.MODEL_KEYS["cfl"][1])
            raise AssertionError("expected CliError for model.cfl = {!r}".format(bad))
        except cli.CliError:
            pass
