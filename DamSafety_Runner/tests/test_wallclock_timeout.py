"""Runner 3.10.24: HEC-RAS (and gmsh) timeouts on the wall clock, SIGTERM then SIGKILL, and the
timeout logged before any cleanup (core/wallclock.py, core/hecras_engine.py). Fake clocks,
fake processes and tiny shell scripts only; no HEC-RAS."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from nd_breach_runner.core import hecras_engine as he, wallclock as wc
from nd_breach_runner.tests.registry import test

H = 3600.0


class Clock:
    """Wall and monotonic clocks that only move when told; sleep() moves both."""

    def __init__(self, wall=1792350000.0, mono=1000.0):
        self.w, self.m = wall, mono

    def wall(self):
        return self.w

    def mono(self):
        return self.m

    def sleep(self, dt):
        self.w += dt
        self.m += dt


class Proc:
    """A Popen stand-in: exits `exit_after_term` s (clock time) after SIGTERM (None: ignores
    it), at once on SIGKILL."""

    def __init__(self, clock, exit_after_term=None, pid=4242):
        self.clock, self.pid, self.after = clock, pid, exit_after_term
        self.returncode, self.term_at, self.signals = None, None, []

    def poll(self):
        if (self.returncode is None and self.term_at is not None and self.after is not None
                and self.clock.w - self.term_at >= self.after):
            self.returncode = -15
        return self.returncode

    def got(self, sig):
        self.signals.append((sig, self.clock.w))
        if sig == signal.SIGTERM and self.term_at is None:
            self.term_at = self.clock.w
        if sig == signal.SIGKILL:
            self.returncode = -9

    def kill(self):
        self.got(signal.SIGKILL)

    def terminate(self):
        self.got(signal.SIGTERM)


# ---------------------------------------------------------------------------- Deadline
@test
def wallclock_monotonic_stall_does_not_extend_the_deadline():
    # Grok Bot's run: a 18,099 s timeout; the VM paused, monotonic fell ~16,650 s behind and
    # the old (monotonic) timeout fired after 34,749 s of wall time.
    c, logs = Clock(), []
    dl = wc.Deadline(18099, what="RasUnsteady", log=logs.append, wall=c.wall, mono=c.mono)
    c.sleep(4000)
    c.w += 13200                      # paused: wall moves on, monotonic does not
    assert not dl.expired()           # 17,200 s of wall time
    c.sleep(898)
    assert not dl.expired()
    c.sleep(1)                        # 18,099 s of wall time, 4,899 s monotonic
    assert dl.expired(), dl.summary()
    s = dl.summary()
    assert s["wall_elapsed_s"] >= 18099 and s["monotonic_elapsed_s"] < 5200, s
    assert s["clock_jumps"] and s["clock_jumps"][0]["skew_s"] > 13000, s
    assert any("clock jump" in m and "paused" in m for m in logs), logs


@test
def wallclock_pause_alone_expires_the_deadline_at_the_wall_time():
    c = Clock()
    dl = wc.Deadline(100, wall=c.wall, mono=c.mono)
    c.sleep(50)
    assert not dl.expired()
    c.w += 60                          # 110 s of wall time, 50 s monotonic
    assert dl.expired() and abs(dl.elapsed() - 110) < 1e-9


@test
def wallclock_backwards_jump_never_fires_early_or_late():
    c, logs = Clock(), []
    dl = wc.Deadline(1000, log=logs.append, wall=c.wall, mono=c.mono)
    c.sleep(400)
    c.w -= 3 * H                       # wall clock stepped back 3 h
    assert not dl.expired() and abs(dl.elapsed() - 400) < 1e-9
    assert any("stepped back" in m for m in logs), logs
    c.sleep(599)
    assert not dl.expired()            # not early
    c.sleep(1)
    assert dl.expired()                # and not late: monotonic is the floor


@test
def wallclock_small_drift_is_not_logged():
    c, logs = Clock(), []
    dl = wc.Deadline(1000, log=logs.append, wall=c.wall, mono=c.mono, margin_s=60)
    c.sleep(10)
    c.w += 30
    dl.elapsed()
    assert not logs and not dl.events


@test
def wallclock_wait_returns_none_on_expiry_and_the_code_on_exit():
    c = Clock()
    p = Proc(c)
    dl = wc.Deadline(10, wall=c.wall, mono=c.mono)
    assert wc.wait(p, dl, poll_s=1.0, sleep=c.sleep) is None and p.returncode is None
    p.returncode = 0
    assert wc.wait(p, wc.Deadline(10, wall=c.wall, mono=c.mono), sleep=c.sleep) == 0


# ---------------------------------------------------------------------------- stop_group
@test
def wallclock_sigterm_then_sigkill_after_the_grace_period():
    c = Clock()
    p = Proc(c, exit_after_term=None)                  # ignores SIGTERM
    sent = []

    def killpg(pid, sig):
        assert pid == p.pid
        sent.append(sig)
        p.got(sig)
    r = wc.stop_group(p, grace=60, wall=c.wall, mono=c.mono, sleep=c.sleep, killpg=killpg)
    assert sent == [signal.SIGTERM, signal.SIGKILL], sent
    (_, t_term), (_, t_kill) = p.signals
    assert t_kill - t_term >= 60 - 1e-9 and t_kill - t_term < 61, p.signals
    assert r["exited_after"] == "SIGKILL" and r["returncode"] == -9, r
    assert r["sent"][0].startswith("SIGTERM to the process group"), r


@test
def wallclock_no_sigkill_when_it_exits_on_sigterm():
    c = Clock()
    p = Proc(c, exit_after_term=12)
    sent = []

    def killpg(pid, sig):
        sent.append(sig)
        p.got(sig)
    r = wc.stop_group(p, grace=60, wall=c.wall, mono=c.mono, sleep=c.sleep, killpg=killpg)
    assert sent == [signal.SIGTERM], sent
    assert r["exited_after"] == "SIGTERM" and 12 <= r["term_wait_s"] < 13, r


@test
def wallclock_grace_is_on_the_wall_clock_too():
    # a pause during the grace period counts: SIGKILL follows after 60 s of wall time
    c = Clock()
    p = Proc(c)
    sent = []

    def sleep(dt):
        c.sleep(dt)
        c.w += 30                      # every sleep, 30 s of pause
    r = wc.stop_group(p, grace=60, wall=c.wall, mono=c.mono, sleep=sleep,
                      killpg=lambda pid, sig: (sent.append(sig), p.got(sig)))
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    assert p.signals[1][1] - p.signals[0][1] < 62, p.signals


@test
def wallclock_stop_group_falls_back_without_a_process_group():
    c = Clock()
    p = Proc(c)

    def killpg(pid, sig):
        raise PermissionError("not ours")
    r = wc.stop_group(p, grace=5, wall=c.wall, mono=c.mono, sleep=c.sleep, killpg=killpg)
    assert [s for s, _ in p.signals] == [signal.SIGTERM, signal.SIGKILL], p.signals
    assert r["sent"] == ["SIGTERM to the process", "SIGKILL to the process"], r


@test
def wallclock_kill_grace_setting():
    assert wc.grace_s({}) == 60.0
    assert wc.grace_s({wc.ENV_GRACE: "5"}) == 5.0
    assert wc.grace_s({wc.ENV_GRACE: "0"}) == 0.0
    assert wc.grace_s({wc.ENV_GRACE: "x"}) == 60.0 and wc.grace_s({wc.ENV_GRACE: "-3"}) == 60.0


@test
def wallclock_real_process_that_ignores_sigterm_is_killed():
    if os.name != "posix":
        return
    p = subprocess.Popen(["/bin/sh", "-c", "trap '' TERM; sleep 30 & wait; sleep 30"],
                         start_new_session=True)
    time.sleep(0.3)
    t0 = time.time()
    r = wc.stop_group(p, grace=1.0, poll_s=0.05)
    assert r["exited_after"] == "SIGKILL" and p.returncode == -signal.SIGKILL, r
    assert time.time() - t0 >= 1.0
    p = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    r = wc.stop_group(p, grace=5.0, poll_s=0.05)
    assert r["exited_after"] == "SIGTERM" and r["sent"] == ["SIGTERM to the process group"], r


@test
def wallclock_run_captures_output_and_raises_timeout():
    r = wc.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
               timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "out" and r.stderr.strip() == "err"
    if os.name != "posix":
        return
    t0 = time.time()
    try:
        wc.run(["/bin/sh", "-c", "echo started; sleep 30"], timeout=0.5, grace=2.0, poll_s=0.05)
        raise AssertionError("must time out")
    except wc.Timeout as e:
        assert isinstance(e, subprocess.TimeoutExpired)
        assert e.stop["exited_after"] == "SIGTERM" and "started" in (e.output or ""), e.stop
        assert e.deadline["timeout_s"] == 0.5
    assert time.time() - t0 < 10


# ---------------------------------------------------------------------------- RasUnsteady
def _fake_exe(t, body):
    exe = os.path.join(t, "RasUnsteady")
    with open(exe, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(exe, 0o755)
    return exe


def _expired_now(timeout, what="", log=None):
    c = Clock()
    dl = wc.Deadline(timeout, what=what, log=log, wall=c.wall, mono=c.mono)
    c.w += timeout + 16650.0           # wall: past the deadline; monotonic: stalled
    return dl


@test
def hecras_timeout_logged_before_cleanup_even_when_cleanup_raises():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        exe = _fake_exe(t, "echo ' SIMTIME=        8.0000'\nexec sleep 30\n")
        logp = os.path.join(t, "run.log")
        seen = {}
        logs = []

        def bad_stop(p, grace, log=None):
            seen["run_log"] = open(logp).read()     # what was logged BEFORE cleanup
            seen["logs"] = list(logs)
            seen["grace"] = grace
            raise RuntimeError("cleanup broke")
        try:
            he._run_logged([exe], t, {wc.ENV_GRACE: "7"}, 18099.0, logp, 32.0, 11120.0,
                           log=logs.append, deadline=_expired_now, stop=bad_stop, poll_s=0.05)
            raise AssertionError("must time out")
        except he.HecRasTimeout as e:
            info = e.info
            assert isinstance(e, he.HecRasEngineError)
            assert "did not finish in 18099 s" in str(e), e
        assert "[ndbreach] TIMEOUT" in seen["run_log"], seen
        assert "SIGKILL after 7 s" in seen["run_log"], seen["run_log"]
        assert any(m.startswith("ERROR: Linux RasUnsteady did not finish") for m in seen["logs"])
        assert seen["grace"] == 7.0
        assert info["stop_error"] == "RuntimeError: cleanup broke", info
        assert info["wall_elapsed_s"] > 30000 and info["monotonic_elapsed_s"] < 100, info
        assert info["clock_jumps"], info
        txt = open(logp).read()
        assert "[ndbreach] stopped:" in txt and "cleanup broke" in txt, txt
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_timeout_log_lines_survive_later_output():
    # run.log is opened for append, so RasUnsteady's own lines (forrtl after SIGTERM) can not
    # overwrite the timeout line
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        exe = _fake_exe(t, "trap 'echo \"forrtl: error (78): process killed (SIGTERM)\"; exit 1' TERM\n"
                           "echo ' SIMTIME=        8.0000'\nwhile :; do sleep 0.05; done\n")
        logp = os.path.join(t, "run.log")
        try:
            he._run_logged([exe], t, {wc.ENV_GRACE: "5"}, 1.0, logp, 32.0, None,
                           poll_s=0.05)
            raise AssertionError("must time out")
        except he.HecRasTimeout as e:
            info = e.info
        txt = open(logp).read()
        assert txt.index("SIMTIME") < txt.index("[ndbreach] TIMEOUT") < txt.index(
            "[ndbreach] stopped:"), txt
        assert "forrtl" in txt, txt
        assert info["stop"]["exited_after"] == "SIGTERM", info
        assert info["stop"]["sent"] == ["SIGTERM to the process group"], info
    finally:
        shutil.rmtree(t, ignore_errors=True)


def _plan(t, cells=50000):
    with open(os.path.join(t, "M.p01"), "w") as f:
        f.write("Plan Title=t\nGeom File=g01\nSimulation Date=01JAN2000,0000,02JAN2000,0800\n"
                "Computation Interval=3SEC\n")
    with open(os.path.join(t, "hecras_engine.json"), "w") as f:
        json.dump({"cells": cells}, f)


@test
def hecras_compute_timeout_keeps_the_partial_hdf_and_writes_timeout_json():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        _plan(t)
        exe = _fake_exe(t, "echo ' SIMTIME=        8.0000'\necho partial > M.p01.tmp.hdf\n"
                           "exec sleep 30\n")
        for x in ("b01", "x01", "p01.tmp.hdf"):
            open(os.path.join(t, "M." + x), "w").close()
        logs = []
        try:
            he.compute(t, "M", d={"linux_dir": t, "rasunsteady": exe},
                       env={he.ENV_TIMEOUT: "1", wc.ENV_GRACE: "3"}, log=logs.append)
            raise AssertionError("must time out")
        except he.HecRasTimeout as e:
            info = e.info
        work = os.path.join(t, "linux_run")
        ph = info["partial_hdf"]
        assert ph["path"] == os.path.join(work, "M.p01.partial.hdf") and ph["size"] > 0, ph
        assert not os.path.exists(os.path.join(work, "M.p01.hdf"))     # never as results
        assert ph["readable"] is False and ph["error"], ph                # not a real HDF
        tj = json.load(open(os.path.join(work, "timeout.json")))
        assert tj["timeout_s"] == 1.0 and tj["stop"]["exited_after"] == "SIGTERM", tj
        assert "(25 % done)" in tj["message"] and tj["cells"] == 50000, tj
        assert any("partial results kept as M.p01.partial.hdf" in m for m in logs), logs
        # run_stage's record: the partial HDF next to the dam, the timeout in run_report.json
        dam = os.path.join(t, "dam")
        os.makedirs(os.path.join(dam, "_breach_model"))
        with open(os.path.join(dam, "_breach_model", "run_report.json"), "w") as f:
            json.dump({"summary": {"flooded_ac": 1.0}}, f)
        out = he.record_timeout(dam, info)
        rep = json.load(open(os.path.join(dam, "_breach_model", "run_report.json")))
        assert rep["summary"] == {"flooded_ac": 1.0}, rep                 # kept
        assert rep["hecras_timeout"]["timeout_s"] == 1.0, rep
        assert os.path.exists(os.path.join(dam, "_hecras", "M.p01.partial.hdf"))
        assert out["partial_hdf_kept_as"].endswith("M.p01.partial.hdf")
        st = json.load(open(os.path.join(dam, "_hecras", "hecras_stage.json")))
        assert st["status"] == "timeout", st
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_record_timeout_never_raises_without_a_run_report():
    t = tempfile.mkdtemp()
    try:
        logs = []
        out = he.record_timeout(os.path.join(t, "dam"), {"timeout_s": 5,
                                                         "partial_hdf": {"path": "/nope"}},
                                log=logs.append)
        assert out["timeout_s"] == 5 and any("could not be added" in m for m in logs), logs
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_preprocess_step_timeout_is_on_the_wall_clock():
    if os.name != "posix":
        return
    t0 = time.time()
    try:
        he._run(["/bin/sh", "-c", "sleep 30"], "/", {wc.ENV_GRACE: "2"}, 0.5, "terrain HDF")
        raise AssertionError("must time out")
    except he.HecRasEngineError as e:
        assert "terrain HDF did not finish in 0 s (wall clock); stopped with SIGTERM" in str(e), e
    assert time.time() - t0 < 10


@test
def hecras_ras_compile_deadline_counts_a_pause():
    # Ras.exe -c's polling loop: the deadline is a wallclock.Deadline
    import inspect
    src = inspect.getsource(he.ras_compile)
    assert "wallclock.Deadline(timeout" in src and "time.time() - t0 < timeout" not in src
    src = inspect.getsource(he._run_logged)
    assert "p.wait(timeout" not in src and "SIGKILL)" not in src
