"""Runner 3.10.48: the HEC-RAS RasUnsteady compute has NO wall-clock limit unless
NDBREACH_HECRAS_TIMEOUT_S is a positive number (3.10.17-3.10.47 stopped it at max(2 h,
3 x the estimate), capped at 48 h). The subprocess is mocked (fake Popen, fake clocks); no
HEC-RAS, no Wine, nothing sleeps for long."""
import json
import os
import shutil
import tempfile

from nd_breach_runner.core import hecras_engine as he, wallclock as wc, workflow
from nd_breach_runner.tests.registry import test

H = 3600.0


class Clock:
    def __init__(self, wall=1792350000.0, mono=1000.0):
        self.w, self.m = wall, mono

    def wall(self):
        return self.w

    def mono(self):
        return self.m


class FakePopen:
    """RasUnsteady stand-in: every poll() moves the fake clock on by `step_s`; it exits 0 once
    `run_s` of clock time has passed (None: never) and writes the 'Finished' line to the log
    file it was given. Records signals."""
    instances = []

    def __init__(self, clock, run_s, step_s=H):
        self.clock, self.run_s, self.step_s = clock, run_s, step_s
        self.t0 = clock.w
        self.returncode = None
        self.pid = 4242
        self.signals = []
        self.out = None

    def __call__(self, cmd, cwd=None, env=None, stdout=None, stderr=None, **kw):
        self.cmd, self.out = cmd, stdout
        FakePopen.instances.append(self)
        return self

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        self.clock.w += self.step_s
        self.clock.m += self.step_s
        if self.run_s is not None and self.clock.w - self.t0 >= self.run_s:
            with open(self.out.name, "a") as f:
                f.write(" SIMTIME=       72.0000\nFinished Unsteady Flow Simulation\n")
            self.returncode = 0
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9


def _project(t, cells=200000, hours=72):
    with open(os.path.join(t, "M.p01"), "w") as f:
        f.write("Plan Title=x\nGeom File=g01\nSimulation Date=01JAN2000,0000,{},{}\n"
                "Computation Interval=2SEC\n".format(
                    "{:02d}JAN2000".format(1 + hours // 24), "{:02d}00".format(hours % 24)))
    with open(os.path.join(t, "M.g01"), "w") as f:
        f.write("Storage Area=D\nStorage Area Is2D=-1\nStorage Area 2D Points= {}\n".format(cells))
    for x in ("b01", "x01", "p01.tmp.hdf"):
        open(os.path.join(t, "M." + x), "w").close()
    return {"linux_dir": t, "rasunsteady": os.path.join(t, "RasUnsteady")}


def _compute(t, d, env, clock, popen, stop=None):
    """he.compute with the fake Popen and a Deadline on the fake clock."""
    real_popen, real_deadline, real_stop = he.subprocess.Popen, wc.Deadline, wc.stop_group
    real_wait = wc.wait

    def wait(proc, deadline, poll_s=1.0, sleep=None):
        return real_wait(proc, deadline, poll_s=poll_s, sleep=lambda s: None)  # fake clock

    def deadline(timeout, what="", log=None, **kw):
        return real_deadline(timeout, what=what, log=log, wall=clock.wall, mono=clock.mono)

    he.subprocess.Popen = popen
    wc.Deadline = deadline
    wc.wait = wait
    if stop is not None:
        wc.stop_group = stop
    logs, lim = [], {}
    try:
        out = he.compute(t, "M", d=d, env=env, log=logs.append, limit=lim)
        return out, logs, lim, None
    except he.HecRasTimeout as e:
        return None, logs, lim, e
    finally:
        he.subprocess.Popen, wc.Deadline, wc.stop_group = real_popen, real_deadline, real_stop
        wc.wait = real_wait


@test
def hecras_deadline_none_never_expires():
    c = Clock()
    dl = wc.Deadline(None, what="RasUnsteady", wall=c.wall, mono=c.mono)
    c.w += 500 * H
    c.m += 500 * H
    assert not dl.expired() and dl.remaining() == float("inf")
    s = dl.summary()
    assert s["timeout_s"] is None and s["deadline"] is None and s["wall_elapsed_s"] > 0, s
    assert wc.Deadline(5, wall=c.wall, mono=c.mono).timeout_s == 5.0


@test
def hecras_compute_unset_runs_past_48_h_without_a_stop():
    """The bug: 3.10.47 stopped a long run at <= 48 h with the variable unset."""
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        d = _project(t)
        c = Clock()
        p = FakePopen(c, run_s=60 * H, step_s=H)          # 60 h of wall clock to finish

        def no_stop(*a, **k):
            raise AssertionError("no limit: nothing may be stopped")
        for env in ({}, {he.ENV_TIMEOUT: ""}, {he.ENV_TIMEOUT: "0"}):
            p.returncode, p.t0 = None, c.w
            out, logs, lim, err = _compute(t, d, env, c, p, stop=no_stop)
            assert err is None and out and out.endswith("M.p01.hdf"), (env, err)
            assert p.signals == [], p.signals
            start = [m for m in logs if m.startswith("HEC-RAS compute:")][0]
            assert "wall-clock limit none" in start and "no limit" in start, start
            assert lim["limit_s"] is None and lim["limit"] == "none", lim
            assert lim["estimate_s"] and lim["estimate_s"] > 0, lim
            os.replace(out, os.path.join(t, "M.p01.tmp.hdf"))
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_compute_invalid_value_is_ignored_with_a_note():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        d = _project(t)
        c = Clock()
        for v in ("soon", "-60"):
            p = FakePopen(c, run_s=50 * H)
            out, logs, lim, err = _compute(t, d, {he.ENV_TIMEOUT: v}, c, p)
            assert err is None and out, (v, err)
            start = [m for m in logs if m.startswith("HEC-RAS compute:")][0]
            assert "wall-clock limit none" in start and "ignored" in start and v in start, start
            assert lim["limit_s"] is None and "ignored" in lim["how"], lim
            os.replace(out, os.path.join(t, "M.p01.tmp.hdf"))
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_compute_explicit_limit_still_stops_with_sigterm_then_sigkill():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        d = _project(t)
        c = Clock()
        p = FakePopen(c, run_s=None, step_s=H)            # never finishes
        calls = []

        def stop(proc, grace, log=None):
            calls.append((proc, grace))
            return {"sent": ["SIGTERM to the process group", "SIGKILL to the process group"],
                    "exited_after": "SIGKILL", "returncode": -9}
        out, logs, lim, err = _compute(t, d, {he.ENV_TIMEOUT: str(int(10 * H)),
                                              wc.ENV_GRACE: "7"}, c, p, stop=stop)
        assert out is None and isinstance(err, he.HecRasTimeout), err
        assert calls and calls[0][0] is p and calls[0][1] == 7.0, calls
        assert err.info["timeout_s"] == 10 * H and "explicit limit" in err.info["timeout_how"]
        assert 10 * H <= err.info["wall_elapsed_s"] < 12 * H, err.info
        start = [m for m in logs if m.startswith("HEC-RAS compute:")][0]
        assert "wall-clock limit 36000 s (10.0 h)" in start, start
        assert lim["limit_s"] == 10 * H, lim
        runlog = open(os.path.join(t, "linux_run", "run.log")).read()
        assert "[ndbreach] TIMEOUT" in runlog and "SIGTERM" in runlog, runlog
        assert "SIGKILL" in runlog, runlog
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_time_limit_goes_into_run_report_and_its_md_line():
    t = tempfile.mkdtemp()
    try:
        dam = os.path.join(t, "dam")
        os.makedirs(os.path.join(dam, "_breach_model"))
        rp = os.path.join(dam, "_breach_model", "run_report.json")
        with open(rp, "w") as f:
            json.dump({"summary": {"flooded_ac": 1.0}}, f)
        lim = he.limit_record(None, 12345.6, "no limit: NDBREACH_HECRAS_TIMEOUT_S unset")
        he.record_limit(dam, lim)
        rep = json.load(open(rp))
        assert rep["summary"] == {"flooded_ac": 1.0}, rep
        assert rep["hecras_time_limit"]["limit"] == "none", rep
        assert rep["hecras_time_limit"]["limit_s"] is None, rep
        line = workflow.hecras_time_limit_line(rep["hecras_time_limit"])
        assert line.startswith("none (no limit: NDBREACH_HECRAS_TIMEOUT_S unset)"), line
        assert "12346 s (3.4 h)" in line, line
        line = workflow.hecras_time_limit_line(he.limit_record(7200.0, None, "x (explicit limit)"))
        assert line == "7200 s (2.0 h) (x (explicit limit))", line
        logs = []                                     # no run report: a warning, no raise
        he.record_limit(os.path.join(t, "nope"), lim, log=logs.append)
        assert logs and "could not be added" in logs[0], logs
        he.record_limit(dam, {})                      # nothing to record
    finally:
        shutil.rmtree(t, ignore_errors=True)


class _PreFake:
    """Stands in for _run / ras_compile / geometry_hdf_state in preprocess(): records the
    timeout each Wine step got."""

    def __init__(self):
        self.timeouts = []
        self.state = {"exists": False, "size": 0, "cells": 0, "areas": {}, "terrain": "",
                      "props": False, "error": ""}

    def run(self, cmd, cwd, env, timeout, what, ok=None, log=None):
        self.timeouts.append(("run", timeout))
        step = "terrain" if cmd[-1] == "Terrain" else cmd[cmd.index("wine") + 3]
        if step == "terrain":
            q = os.path.join(cmd[-2].replace("Z:", "").replace("\\", "/"), "Terrain.hdf")
            os.makedirs(os.path.dirname(q), exist_ok=True)
            open(q, "w").write("x")
        elif step == "assoc":
            self.state["terrain"] = ".\\Terrain\\Terrain.hdf"
        elif step == "props":
            self.state["props"] = True
            self.state["areas"] = {"Downstream": {"cells": 10, "props": True}}
        return "OK " + step + (" tables complete" if step == "props" else "")

    def ras(self, project_dir, name, plan, d, e, timeout, log=None, poll_s=2.0, until=None,
            what=None):
        self.timeouts.append(("ras", timeout))
        if until is not None:
            self.state.update(exists=True, size=1000, cells=10,
                              areas={"Downstream": {"cells": 10, "props": False}})

    def hdf(self, path):
        return dict(self.state)


def _preprocess(env, default_timeout=None):
    from nd_breach_runner.tests.test_hecras_engine import _FAKE_D
    t = tempfile.mkdtemp()
    f = _PreFake()
    saved = he._run, he.ras_compile, he.geometry_hdf_state
    he._run, he.ras_compile, he.geometry_hdf_state = f.run, f.ras, f.hdf
    logs = []
    try:
        he.preprocess(t, "M", d=_FAKE_D, env=env, terrain_tif="/x/t.tif", prj="/x/p.prj",
                      log=logs.append, default_timeout=default_timeout)
    finally:
        he._run, he.ras_compile, he.geometry_hdf_state = saved
        shutil.rmtree(t, ignore_errors=True)
    return f.timeouts, logs


@test
def hecras_preprocess_has_no_limit_without_a_value():
    """Nick, 2026-10-21: no 1,800 s cap on terrain / mesh / drv / Ras.exe -c either."""
    for env in ({}, {he.ENV_TIMEOUT: ""}, {he.ENV_TIMEOUT: "0"}, {he.ENV_TIMEOUT: "off"},
                {he.ENV_TIMEOUT: "soon"}):
        tos, logs = _preprocess(env)
        assert len(tos) == 5 and all(t is None for _, t in tos), (env, tos)
        line = [m for m in logs if m.startswith("HEC-RAS preprocess:")]
        assert line and "wall-clock limit per step none" in line[0], (env, logs)
        assert "1800" not in line[0], line


@test
def hecras_preprocess_explicit_value_limits_each_step():
    tos, logs = _preprocess({he.ENV_TIMEOUT: "900"})
    assert len(tos) == 5 and all(t == 900.0 for _, t in tos), tos
    line = [m for m in logs if m.startswith("HEC-RAS preprocess:")][0]
    assert "wall-clock limit per step 900 s" in line and "explicit limit" in line, line


@test
def hecras_ras_compile_without_a_limit_waits_for_the_process():
    """Ras.exe -c's polling loop with timeout=None: a Deadline that never expires, so it
    returns when the run inputs appear (no Wine: Popen and wineserver are faked)."""
    import subprocess as sp
    clock = Clock()
    real_popen, real_run, real_deadline = sp.Popen, sp.run, wc.Deadline
    polls = {"n": 0}

    class P:
        def poll(self):
            return None

        def kill(self):
            pass

    def until():
        polls["n"] += 1
        clock.w += 10 * H                      # 10 h of wall clock per poll
        clock.m += 10 * H
        return polls["n"] >= 6                 # inputs appear after 60 h

    t = tempfile.mkdtemp()
    try:
        sp.Popen = lambda *a, **k: P()
        sp.run = lambda *a, **k: sp.CompletedProcess(a, 0, b"", b"")
        wc.Deadline = lambda timeout, what="", log=None, **kw: real_deadline(
            timeout, what=what, log=log, wall=clock.wall, mono=clock.mono)
        d = {"prefix": t, "wine": "wine", "ras_exe": os.path.join(t, "Ras.exe")}
        he.ras_compile(t, "M", "01", d, {}, None, poll_s=0.0, until=until, what="run inputs")
    finally:
        sp.Popen, sp.run, wc.Deadline = real_popen, real_run, real_deadline
        shutil.rmtree(t, ignore_errors=True)
    assert polls["n"] == 6, polls


# ---------------------------------------------------------------- audit R6 (3.16.7) --
# An unattended run (ndbreach:batch, the CLI batch, the chain) defaults to BATCH_TIMEOUT_S
# (12 h) per compute and per preprocess step when NDBREACH_HECRAS_TIMEOUT_S gives none; a
# single interactive run keeps 'no limit unless set'. The words 'none' / 'off' / 'no' /
# 'unlimited' remove the unattended default too; an explicit number wins over it.

@test
def hecras_unattended_default_is_12_h_and_interactive_stays_unlimited():
    assert he.BATCH_TIMEOUT_S == 12 * H
    assert he.unattended_default(True) == he.BATCH_TIMEOUT_S
    assert he.unattended_default(False) is None
    for env in ({}, {he.ENV_TIMEOUT: ""}, {he.ENV_TIMEOUT: "0"}, {he.ENV_TIMEOUT: "soon"},
                {he.ENV_TIMEOUT: "-60"}, {he.ENV_TIMEOUT: "nan"}):
        # interactive: as before (3.10.48)
        assert he.timeout_setting(env)[0] is None, env
        assert he.compute_timeout(68949, 32.0, 3.0, env=env)[0] is None, env
        # unattended: the 12 h default, and the note says so
        to, note = he.timeout_setting(env, he.BATCH_TIMEOUT_S)
        assert to == 12 * H and "unattended default 43200 s (12.0 h)" in note, (env, note)
        assert he.ENV_TIMEOUT in note and "'none' for no limit" in note, note
        t, est, how = he.compute_timeout(68949, 32.0, 3.0, env=env, default=he.BATCH_TIMEOUT_S)
        assert t == 12 * H and est and how.startswith("unattended batch default: "), (env, how)
    # the words that mean no limit remove the unattended default
    for v in ("none", "off", "no", "unlimited", "NONE"):
        to, note = he.timeout_setting({he.ENV_TIMEOUT: v}, he.BATCH_TIMEOUT_S)
        assert to is None and "(no limit)" in note, (v, note)
        assert he.compute_timeout(1, 1, 1, env={he.ENV_TIMEOUT: v},
                                  default=he.BATCH_TIMEOUT_S)[2].startswith("no limit: ")
    # an explicit number wins in both modes
    for env in ({he.ENV_TIMEOUT: "900"}, {he.ENV_TIMEOUT: " 172800 "}):
        want = float(env[he.ENV_TIMEOUT])
        assert he.timeout_setting(env, he.BATCH_TIMEOUT_S)[0] == want
        t, _e, how = he.compute_timeout(1, 1, 1, env=env, default=he.BATCH_TIMEOUT_S)
        assert t == want and how.endswith("(explicit limit)"), how
    # the report line reads the 'how' text unchanged
    lim = he.limit_record(*he.compute_timeout(68949, 32.0, 3.0, env={}, default=he.BATCH_TIMEOUT_S))
    assert lim["limit_s"] == 12 * H and lim["limit"] == "43200 s (12.0 h)", lim
    line = workflow.hecras_time_limit_line(lim)
    assert line.startswith("43200 s (12.0 h) (unattended batch default: ") and \
        "estimated RasUnsteady time" in line, line


@test
def hecras_unattended_compute_stops_at_12_h_interactive_does_not():
    """The hang (audit R6): a RasUnsteady that never finishes held an unattended queue for ever.
    Unattended it is now stopped at 12 h of wall clock; a single run with no setting still waits."""
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        d = _project(t)
        c = Clock()
        p = FakePopen(c, run_s=None, step_s=H)            # never finishes
        calls = []

        def stop(proc, grace, log=None):
            calls.append((proc, grace))
            return {"sent": ["SIGTERM to the process group", "SIGKILL to the process group"],
                    "exited_after": "SIGKILL", "returncode": -9}
        real_popen, real_deadline, real_stop, real_wait = \
            he.subprocess.Popen, wc.Deadline, wc.stop_group, wc.wait
        he.subprocess.Popen = p
        wc.Deadline = lambda timeout, what="", log=None, **kw: real_deadline(
            timeout, what=what, log=log, wall=c.wall, mono=c.mono)
        wc.wait = lambda proc, deadline, poll_s=1.0, sleep=None: real_wait(
            proc, deadline, poll_s=poll_s, sleep=lambda s: None)
        wc.stop_group = stop
        logs, lim = [], {}
        try:
            he.compute(t, "M", d=d, env={}, log=logs.append, limit=lim,
                       default_timeout=he.BATCH_TIMEOUT_S)
            assert False, "expected HecRasTimeout"
        except he.HecRasTimeout as err:
            assert calls and calls[0][0] is p, calls
            assert err.info["timeout_s"] == 12 * H, err.info
            assert "unattended batch default" in err.info["timeout_how"], err.info
            assert 12 * H <= err.info["wall_elapsed_s"] < 14 * H, err.info
        finally:
            he.subprocess.Popen, wc.Deadline, wc.stop_group, wc.wait = \
                real_popen, real_deadline, real_stop, real_wait
        start = [m for m in logs if m.startswith("HEC-RAS compute:")][0]
        assert "wall-clock limit 43200 s (12.0 h)" in start and "unattended" in start, start
        assert lim["limit_s"] == 12 * H and "unattended batch default" in lim["how"], lim
        runlog = open(os.path.join(t, "linux_run", "run.log")).read()
        assert "[ndbreach] TIMEOUT" in runlog, runlog
        # the same run, interactive (no default): 60 h of wall clock, never stopped
        p2 = FakePopen(c, run_s=60 * H, step_s=H)

        def no_stop(*a, **k):
            raise AssertionError("interactive: nothing may be stopped")
        out, logs2, lim2, err2 = _compute(t, d, {}, c, p2, stop=no_stop)
        assert err2 is None and out and lim2["limit_s"] is None, (err2, lim2)
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_unattended_preprocess_steps_get_the_default_too():
    tos, logs = _preprocess({}, default_timeout=he.BATCH_TIMEOUT_S)
    assert len(tos) == 5 and all(t == 12 * H for _, t in tos), tos
    line = [m for m in logs if m.startswith("HEC-RAS preprocess:")][0]
    assert "wall-clock limit per step 43200 s (12.0 h)" in line, line
    assert "unattended batch default" in line and "explicit limit" not in line, line
    tos, logs = _preprocess({he.ENV_TIMEOUT: "none"}, default_timeout=he.BATCH_TIMEOUT_S)
    assert all(t is None for _, t in tos), tos
    tos, logs = _preprocess({he.ENV_TIMEOUT: "900"}, default_timeout=he.BATCH_TIMEOUT_S)
    assert all(t == 900.0 for _, t in tos), tos
    line = [m for m in logs if m.startswith("HEC-RAS preprocess:")][0]
    assert "explicit limit" in line, line


@test
def hecras_unattended_flag_reaches_run_stage_from_the_shared_helper():
    """workflow.run_hecras_stage(..., unattended=True) (the batch tool and the CLI batch) asks
    the engine for the unattended default; without the flag (one interactive run) the engine's
    signature is called as before."""
    import types

    class Eng(object):
        class HecRasEngineError(Exception):
            pass

        def __init__(self):
            self.calls = []

        def run_stage(self, folder, final, writer, st, log, unattended=False):
            self.calls.append(unattended)
            return {"final": {}, "warnings": []}
    old = workflow.adopt_final_engine
    workflow.adopt_final_engine = lambda folder, name, st, xy, log: {"final_model": name}
    try:
        res = types.SimpleNamespace(final={}, warnings=[], summary={})
        e = Eng()
        workflow.run_hecras_stage("/tmp/x", res, None, workflow.RunSettings(), (0, 0), None,
                                  engine=e, unattended=True)
        workflow.run_hecras_stage("/tmp/x", res, None, workflow.RunSettings(), (0, 0), None,
                                  engine=e)
        assert e.calls == [True, False], e.calls
    finally:
        workflow.adopt_final_engine = old
    # the Windows launcher reads the same words and default
    from nd_breach_runner.core import hecras_windows as HW
    assert HW.BATCH_TIMEOUT_S == he.BATCH_TIMEOUT_S
    assert HW.timeout_s({}, default=HW.BATCH_TIMEOUT_S) == 12 * H
    assert HW.timeout_s({}) is None
    assert HW.timeout_s({HW.ENV_TIMEOUT: "none"}, default=HW.BATCH_TIMEOUT_S) is None
    assert HW.timeout_s({HW.ENV_TIMEOUT: "0"}, default=HW.BATCH_TIMEOUT_S) == 12 * H
    assert HW.timeout_s({HW.ENV_TIMEOUT: "900"}, default=HW.BATCH_TIMEOUT_S) == 900.0
    logs = []
    assert HW.timeout_s({HW.ENV_TIMEOUT: "1h"}, log=logs.append,
                        default=HW.BATCH_TIMEOUT_S) == 12 * H
    assert logs and "unattended default" in logs[0], logs
