"""3.16.9 (audit P2 / P4, 2026-11-07): the LISFLOOD-FP progress poll reads only the tail of the
.mass file; every LISFLOOD-FP child has a wall-clock limit (the setting, else 6 x the run-time
estimate when this computer's speed is known, else 24 h); the domain-growth loop has a
cumulative budget; a collapsing solver time step is logged and warned about once.

No LISFLOOD-FP: the child is a stand-in (a shell script that writes a .mass file and sleeps),
as the other stub-harness tests do."""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_v320 import _dam, _settings

HEADER = ("Time         Tstep      MinTstep   NumTsteps    Area         Vol         Qin        "
          " Hds        Qout          Qerror         Verror       Rain-Inf+Evap\n")


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_wall_")


def _row(t, dt=1.5, vol=1000.0, qin=5.0):
    return ("{:<12.3f} {:<10.4f} {:<10.4f} {:<12d} {:<12.1f} {:<12.3f} {:<12.4f} {:<10.4f} "
            "{:<13.4f} {:<14.6f} {:<12.4f} {:<12.4f}\n".format(
                t, dt, dt / 2.0, int(t), 10.0 * t, vol, qin, 0.0, 0.0, 0.0, 0.1, 0.0))


# ------------------------------------------------------------------------------ P4: tail read
@test
def mass_tail_returns_the_last_complete_row_of_a_long_file():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        p = os.path.join(d, "run.mass")
        n = 5000                                          # a multi-thousand-row file
        with open(p, "w") as f:
            f.write(HEADER)
            for i in range(1, n + 1):
                f.write(_row(60.0 * i, dt=2.0 - i * 1e-4, vol=100.0 * i))
        size = os.path.getsize(p)
        assert size > 10 * lf.MASS_TAIL_BYTES
        row = lf.read_mass_tail(p)
        assert row["time_s"] == 60.0 * n, row
        assert abs(row["vol_m3"] - 100.0 * n) < 1e-6 and row["steps"] == int(60.0 * n), row
        assert abs(row["tstep_s"] - (2.0 - n * 1e-4)) < 1e-9, row
        # the tail read agrees with the full parse the end of the run does
        full = lf._read_mass(p)
        assert full[0] == row["time_s"] and full[2] == row["vol_m3"], (full, row)
        # a partial trailing line (the solver mid-write) is ignored: the previous row counts
        with open(p, "a") as f:
            f.write("{:<12.3f} {:<10.4f} 0.9".format(60.0 * (n + 1), 1.0))      # no newline
        row2 = lf.read_mass_tail(p)
        assert row2["time_s"] == 60.0 * n, row2
        # ... and once its newline is there, it is the last row (a short row needs >= 7 fields)
        with open(p, "a") as f:
            f.write("  12 50 1000 7 0 0 0 0 0\n")
        row3 = lf.read_mass_tail(p)
        assert row3["time_s"] == 60.0 * (n + 1) and row3["qin_cms"] == 7.0, row3
        # the seek lands inside a row: the cut piece is dropped, not parsed as a row
        for tail in (64, 100, 150, 4096):
            r = lf.read_mass_tail(p, tail_bytes=tail)
            assert r is not None and r["time_s"] == 60.0 * (n + 1), (tail, r)
    finally:
        shutil.rmtree(d)


@test
def mass_tail_handles_a_missing_short_or_header_only_file():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        p = os.path.join(d, "run.mass")
        assert lf.read_mass_tail(p) is None                            # not there yet
        open(p, "w").close()
        assert lf.read_mass_tail(p) is None                            # empty
        with open(p, "w") as f:
            f.write(HEADER)
        assert lf.read_mass_tail(p) is None                            # header only
        with open(p, "a") as f:
            f.write(_row(300.0)[:-1])                                  # first row, mid-write
        assert lf.read_mass_tail(p) is None
        with open(p, "a") as f:
            f.write("\n")
        assert lf.read_mass_tail(p)["time_s"] == 300.0                 # a small file: read whole
        # garbage the solver wrote (NaN text that is not a number) is skipped, not raised
        with open(p, "a") as f:
            f.write("x y z w v u t s r q p o\n")
        assert lf.read_mass_tail(p)["time_s"] == 300.0
    finally:
        shutil.rmtree(d)


# ------------------------------------------------------------------------------ P2: limits
@test
def wall_limit_is_the_setting_else_six_times_the_estimate_else_24_h():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core import estimate as est
    assert lf.wall_limit_s(3600.0) == 3600.0                           # the setting wins
    assert lf.wall_limit_s(3600.0, 1e6, 36000.0, 10.0, 1e7) == 3600.0
    assert lf.wall_limit_s(0.0) == lf.DEFAULT_MAX_WALL_S == 24 * 3600.0
    assert lf.wall_limit_s(None, 1e6, 36000.0, 10.0, None) == lf.DEFAULT_MAX_WALL_S   # no rate
    one = est.seconds_for(4e6, 36000.0, 10.0, 2e7)
    assert one > lf.WALL_FLOOR_S / lf.WALL_ESTIMATE_FACTOR
    assert abs(lf.wall_limit_s(0, 4e6, 36000.0, 10.0, 2e7) - 6.0 * one) < 1e-6
    # a tiny estimate never gives a limit under the floor
    assert lf.wall_limit_s(0, 100, 600.0, 10.0, 1e9) == lf.WALL_FLOOR_S
    assert lf.wall_limit_s("bad") == lf.DEFAULT_MAX_WALL_S
    # the CFL collapse threshold: 0.7 x 10 m / sqrt(g x 100 m) = 0.2236 s
    assert abs(lf.dt_collapse_s(10.0) - 0.7 * 10.0 / (9.80665 * 100.0) ** 0.5) < 1e-9
    assert abs(lf.dt_collapse_s(10.0, 0.5) - 0.5 * 10.0 / (9.80665 * 100.0) ** 0.5) < 1e-9


def _fake_child(d, body):
    """A stand-in LISFLOOD-FP: a shell script run with the .par name, writing results/run.mass."""
    exe = os.path.join(d, "lisflood")
    with open(exe, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(exe, 0o755)
    return exe


def _fake_child_py(d, body):
    """A stand-in LISFLOOD-FP that every system can start: a Python script behind a .bat on
    Windows and behind a shell script elsewhere (as tools/test_lisflood_smoke.py does)."""
    py = os.path.join(d, "lisflood_stand_in.py")
    with open(py, "w") as f:
        f.write(body)
    if os.name == "nt":
        exe = os.path.join(d, "lisflood_py.bat")
        with open(exe, "w") as f:
            f.write('@"{}" "{}" %*\r\n'.format(sys.executable, py))
    else:
        exe = os.path.join(d, "lisflood_py")
        with open(exe, "w") as f:
            f.write('#!/bin/sh\nexec "{}" "{}" "$@"\n'.format(sys.executable, py))
        os.chmod(exe, 0o755)
    return exe


def _inputs(d, exe, sim_s=3600.0, **kw):
    """write_inputs on the 20-cell synthetic channel with the stand-in executable."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    st = lf.LisfloodSettings(exe, arrival_depth_m=0.3, sim_time_s=sim_s, massint_s=60.0,
                             initial_tstep_s=1.0, **kw)
    wd = os.path.join(d, "wd")
    par = lf.write_inputs(wd, dem, (30.0, nr * dx / 2 + 5), hy, st)
    return wd, par, st, dem


@test
def slow_lisflood_child_is_killed_at_the_wall_limit_with_a_plain_message():
    if os.name != "posix":
        return
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        exe = _fake_child(d, "mkdir -p results\nprintf '%s' 'Time Tstep MinTstep NumTsteps Area "
                             "Vol Qin Hds Qout Qerror Verror Rain' > results/run.mass\n"
                             "echo >> results/run.mass\n"
                             "echo '60 1.5 0.7 40 10 1000 5 0 0 0 0.1 0' >> results/run.mass\n"
                             "sleep 60\n")
        wd, par, st, _dem = _inputs(d, exe, max_wall_s=2.0)
        logs, prog = [], []
        t0 = time.time()
        try:
            lf.run(wd, par, st, progress=lambda p, m: prog.append((p, m)), log=logs.append)
            raise AssertionError("the stand-in must be stopped by the wall-clock limit")
        except lf.ModelError as e:
            msg = str(e)
        assert time.time() - t0 < 30.0, "the child was not killed promptly"
        assert "wall time" in msg and "limit" in msg and "LISFLOOD_MAX_WALL_S" in msg, msg
        assert "0.00 h" in msg and "{:.2f} h".format(2.0 / 3600.0) in msg, msg
        assert st.wall_limit_used == 2.0
        assert any("wall-clock limit for this run: 0.0 h" in m for m in logs), logs
        assert any("dt 1.5 s" in m for _p, m in prog), prog       # the tail read fed progress
        with open(os.path.join(wd, "lisflood.log")) as f:
            assert "Stopped by ND Dam Breach Runner: wall-clock limit" in f.read()
    finally:
        shutil.rmtree(d)


@test
def a_lisflood_file_that_is_no_program_is_named_in_plain_words():
    """3.20.5 (found by the Windows test run): a LISFLOOD-FP path that is not a program for this
    computer stopped the run with the system's own words ('[WinError 193] %1 is not a valid
    Win32 application'). The Runner now names the file and says it is not executable here. A
    file that is simply missing still raises what it did."""
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        exe = os.path.join(d, "lisflood.exe" if os.name == "nt" else "lisflood")
        with open(exe, "w") as f:
            f.write("this is not a program\n")
        os.chmod(exe, 0o755)
        wd, par, st, _dem = _inputs(d, exe)
        try:
            lf.run(wd, par, st)
            raise AssertionError("expected ModelError")
        except lf.ModelError as e:
            assert "could not be started" in str(e) and exe in str(e), str(e)
            assert "is not executable on this computer" in str(e), str(e)
        assert lf._not_a_program(OSError(8, "Exec format error"))
        win = OSError(22, "%1 is not a valid Win32 application")
        win.winerror = 193
        assert lf._not_a_program(win)
        assert not lf._not_a_program(FileNotFoundError(2, "No such file or directory"))
        assert not lf._not_a_program(PermissionError(13, "Permission denied"))
    finally:
        shutil.rmtree(d)


@test
def automatic_wall_limit_uses_the_par_grid_and_the_known_rate():
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        exe = _fake_child(d, "exit 0\n")
        wd, par, st, dem = _inputs(d, exe)
        cells, cell_m = lf._par_grid_size(par)
        assert (cells, cell_m) == (dem.data.size, dem.dx), (cells, cell_m)
        assert lf._par_grid_size(os.path.join(d, "nothing.par")) == (None, None)
        # no rate anywhere: 24 h; a session rate from calibrate(): 6 x the estimate (floored)
        lf.RATES.pop(os.path.abspath(exe), None)
        assert lf.known_rate(exe) is None
        assert lf.wall_limit_s(0, cells, st.sim_time_s, cell_m, lf.known_rate(exe)) == \
            lf.DEFAULT_MAX_WALL_S
        lf.RATES[os.path.abspath(exe)] = 1e4             # a very slow computer
        from nd_breach_runner.core import estimate as est
        want = max(lf.WALL_FLOOR_S, 6.0 * est.seconds_for(cells, st.sim_time_s, cell_m, 1e4))
        assert abs(lf.wall_limit_s(0, cells, st.sim_time_s, cell_m, lf.known_rate(exe)) - want) \
            < 1e-6
        lf.RATES.pop(os.path.abspath(exe), None)
        # a run that finishes under the limit records it and raises nothing
        wd2 = os.path.join(d, "ok")
        exe2 = _fake_child_py(d, "import os\nos.makedirs('results', exist_ok=True)\n"
                                 "open(os.path.join('results', 'run.mass'), 'w').close()\n")
        from nd_breach_runner.core.grid import Grid
        from nd_breach_runner.core.breach import Hydrograph
        hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
        st2 = lf.LisfloodSettings(exe2, sim_time_s=600.0, max_wall_s=300.0)
        par2 = lf.write_inputs(wd2, dem, (30.0, 205.0), hy, st2)
        lf.run(wd2, par2, st2)
        assert st2.wall_limit_used == 300.0 and st2.dt_collapsed is None
    finally:
        shutil.rmtree(d)


@test
def collapsed_time_step_is_warned_about_once_and_the_run_goes_on():
    if os.name != "posix":
        return
    from nd_breach_runner.core import lisflood as lf
    d = _tmp()
    try:
        # the stand-in writes a healthy row, then rows with a 1e-4 s step, over ~3 s
        exe = _fake_child(d, "mkdir -p results\necho 'Time Tstep MinTstep NumTsteps Area Vol "
                             "Qin Hds Qout Qerror Verror Rain' > results/run.mass\n"
                             "echo '60 1.5 0.7 40 10 1000 5 0 0 0 0.1 0' >> results/run.mass\n"
                             "sleep 1.2\n"
                             "echo '120 0.0001 0.0001 9000 10 1000 5 0 0 0 0.1 0' >> "
                             "results/run.mass\nsleep 1.2\n"
                             "echo '180 0.0001 0.0001 19000 10 1000 5 0 0 0 0.1 0' >> "
                             "results/run.mass\nsleep 1.2\nexit 0\n")
        wd, par, st, _dem = _inputs(d, exe, max_wall_s=60.0)
        logs = []
        lf.run(wd, par, st, log=logs.append)
        warn = [m for m in logs if m.startswith("WARNING: LISFLOOD-FP's time step collapsed")]
        assert len(warn) == 1, logs
        assert "0.0001 s" in warn[0] and "20 m cell" in warn[0], warn[0]
        assert st.dt_collapsed is not None and st.dt_collapsed[1] == 0.0001, st.dt_collapsed
        assert any("time step" in m and "steps" in m for m in logs), logs   # the dt log line
    finally:
        shutil.rmtree(d)


# ---------------------------------------------------------------- the workflow: budget + report
@contextlib.contextmanager
def _slow_models(seconds, calls):
    """LISFLOOD-FP stubbed as in test_v320.fake_models, but every run takes `seconds` of wall
    time and the flood always leaves the east edge, so the domain would grow for ever."""
    from nd_breach_runner.core import lisflood as lf

    def fake_run(wd, par, st, progress=None, cancel=None, log=None, name="run"):
        calls.append(wd)
        st.wall_limit_used = lf.wall_limit_s(st.max_wall_s)
        time.sleep(seconds)
        return seconds

    def fake_read(wd, dem, st, hydrograph, name="run"):
        X, Y = np.meshgrid(dem.x_centers(), dem.y_centers())
        dep = np.where((X > 0) & (np.abs(Y) < 300), 2.0, np.nan)        # 2 m everywhere east
        vel = np.where(np.isfinite(dep), 1.2, np.nan)
        arr = np.where(np.isfinite(dep), 0.1, np.nan)
        wse = np.where(np.isfinite(dep), dem.data + np.nan_to_num(dep), np.nan)
        stats = {"max_volume_error_pct": 0.1, "peak_inflow_cfs": 1000.0}
        return lf.Envelopes(dem.like(dep, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                            dem.like(wse, np.nan), stats, [], {})
    old = (lf.run, lf.read_results)
    lf.run, lf.read_results = fake_run, fake_read
    try:
        yield
    finally:
        lf.run, lf.read_results = old


@test
def growth_loop_stops_on_the_cumulative_wall_budget_with_a_plain_message():
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        calls = []
        # 6 domain runs allowed, 20 mi extent: without the budget the flood that always leaves
        # the east edge would run all 6; with a 1.5 s budget and 1 s runs it stops after 2
        st = _settings(max_iterations=6, max_extent_mi=40.0, max_total_wall_s=1.5,
                       products=False, qa_maps="off")
        with _slow_models(1.0, calls):
            res = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                   os.path.join(d, "T1"))
        assert len(calls) == 2, calls
        rep = json.load(open(res.report_json))
        assert rep["cut_off"]["reason"] == "wall-clock budget", rep["cut_off"]
        w = [x for x in res.warnings if "wall-clock budget" in x.lower() or
             "budget for the domain-growth loop" in x]
        assert w and "Stopped after 2 LISFLOOD-FP pass(es)" in w[0] and \
            "LISFLOOD_MAX_TOTAL_WALL_S" in w[0] and "the setting" in w[0], res.warnings
        assert rep["lisflood_wall"]["budget_s"] == 1.5 and rep["lisflood_wall"]["spent_s"] >= 1.5
        assert len(rep["lisflood_wall"]["runs"]) == 2
        # the record stands: two domain runs, the second larger
        runs = rep["lisflood_runs"]
        assert len(runs) == 2 and runs[1]["size_mi"][0] > runs[0]["size_mi"][0]
        for k in ("depth", "velocity", "arrival", "wse"):
            assert os.path.exists(res.final[k])
        # the limits never change the run key (a 3.16.8 checkpoint still resumes)
        k0 = workflow.key_settings(_settings())
        k1 = workflow.key_settings(_settings(max_wall_s=10.0, max_total_wall_s=20.0,
                                             lisflood_rate=1e6))
        assert k0 == k1
        assert "max_wall_s" not in _settings().as_dict()
        assert _settings(max_wall_s=10.0).as_dict()["max_wall_s"] == 10.0
    finally:
        shutil.rmtree(d)


@test
def automatic_budget_is_max_runs_times_the_per_run_limit_and_does_not_stop_a_quick_loop():
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    d = _tmp()
    try:
        calls = []
        st = _settings(max_iterations=2, max_extent_mi=40.0, products=False, qa_maps="off")
        assert st.max_wall_s == 0.0 and st.max_total_wall_s == 0.0 and st.lisflood_rate is None
        with _slow_models(0.0, calls):
            res = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                   os.path.join(d, "T1"))
        rep = json.load(open(res.report_json))
        assert len(calls) == 2 and rep["cut_off"]["reason"] == "domain-run limit", rep["cut_off"]
        lw = rep["lisflood_wall"]
        assert lw["budget_s"] == 2 * 24 * 3600.0, lw                 # 2 runs x 24 h
        assert all(r["limit_s"] == 24 * 3600.0 for r in lw["runs"]), lw
    finally:
        shutil.rmtree(d)


@test
def processing_tool_and_cli_carry_the_wall_settings():
    from nd_breach_runner.tests import fake_qgis   # noqa: F401  (stub qgis)
    import importlib
    algs = importlib.import_module("nd_breach_runner.algorithms")
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    assert a.parameterDefinition("LISFLOOD_MAX_WALL_S") is not None
    assert a.parameterDefinition("LISFLOOD_MAX_TOTAL_WALL_S") is not None
    cli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    for k in ("max_wall_s", "max_total_wall_s", "lisflood_rate"):
        assert k in cli.MODEL_KEYS and k in cli.TEMPLATE["model"], k
    # the settings catalog (Settings window rows) sees both numbers, 0 = automatic
    from nd_breach_runner import suite_settings_tools as sst
    defs = {p["name"]: p for p in sst.param_defs(a)}
    assert defs["LISFLOOD_MAX_WALL_S"]["default"] == 0.0
    assert "0 = automatic" in defs["LISFLOOD_MAX_WALL_S"]["description"]
    assert defs["LISFLOOD_MAX_TOTAL_WALL_S"]["minimum"] == 0.0
    # the tool reads them into the run settings
    from nd_breach_runner.core import workflow
    st = workflow.RunSettings()
    assert st.max_wall_s == 0.0 and st.max_total_wall_s == 0.0 and st.lisflood_rate is None
    # the CLI's model section builds them (a 0 lisflood_rate is "unknown")
    cfg = cli.Config({"model": {"max_wall_s": 120.0, "max_total_wall_s": 600.0,
                                "lisflood_rate": 0}}, "")
    st2 = cli.build_settings(cfg, type("O", (), {"lisflood": sys.executable})(),
                             need_models=False)
    assert (st2.max_wall_s, st2.max_total_wall_s, st2.lisflood_rate) == (120.0, 600.0, None)
    cfg = cli.Config({"model": {"lisflood_rate": 2.5e6}}, "")
    st3 = cli.build_settings(cfg, type("O", (), {"lisflood": sys.executable})(),
                             need_models=False)
    assert st3.lisflood_rate == 2.5e6
