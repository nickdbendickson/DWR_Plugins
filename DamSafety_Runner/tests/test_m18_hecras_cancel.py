"""audit M18 (Runner 3.17.7): Cancel reaches the HEC-RAS 2D stage. A true cancel() polled in
wallclock.wait stops the process group through wallclock.stop_group and raises
lisflood.Cancelled - on the QGIS feedback path (fb.cancel / QgsProcessingFeedback.isCanceled)
and on the CLI Ctrl-C path (CANCEL["flag"], set by ndbreach_cli._install_sigint).

No Wine, no HEC-RAS: the long-running solver is a Python sleep that starts a grandchild of its
own, in its own session (start_new_session=True, as RasUnsteady), so "no orphan" means no
process of that process group is left. Real Wine + RasUnsteady validation is separate."""
import inspect
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time

from nd_breach_runner.core import hecras_engine as he
from nd_breach_runner.core import lisflood as lfmod
from nd_breach_runner.core import wallclock as wc
from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import Skip, test

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.dirname(ROOT)

# a parent that starts a sleeping grandchild in the same group, then sleeps itself
SLEEPER = [sys.executable, "-c",
           "import subprocess, sys, time\n"
           "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
           "time.sleep(120)\n"]


def _need_posix():
    if os.name != "posix" or not hasattr(os, "killpg"):
        raise Skip("process groups need POSIX")


def _group_alive(pgid):
    """pids (not zombies) still in process group `pgid` (Linux /proc; else ps; else killpg(0))."""
    if os.path.isdir("/proc/self"):
        left = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open("/proc/{}/stat".format(d)) as f:
                    st = f.read()
            except OSError:
                continue
            rest = st.rsplit(")", 1)[1].split()
            if rest[0] != "Z" and int(rest[2]) == pgid:
                left.append(int(d))
        return left
    try:                                    # macOS and the BSDs: no /proc, so ask ps
        out = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    left, read = [], False
    for line in out.splitlines():
        a = line.split()
        if len(a) >= 3 and a[0].isdigit() and a[1].isdigit():
            read = True
            if int(a[1]) == pgid and not a[2].startswith("Z"):
                left.append(int(a[0]))
    if read:
        return left
    try:
        os.killpg(pgid, 0)
        return [pgid]
    except ProcessLookupError:
        return []


def _wait_gone(pgid, within=3.0):
    t = time.time() + within
    while time.time() < t:
        if not _group_alive(pgid):
            return []
        time.sleep(0.05)
    return _group_alive(pgid)


def _kill_leftovers(pgid):
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


class _Flag(object):
    """A cancel callable that turns true `after` s from start() (a Timer, like a user's click)."""

    def __init__(self):
        self.on, self.t_set = False, None

    def __call__(self):
        return self.on

    def start(self, after):
        def flip():
            self.t_set = time.time()
            self.on = True
        threading.Timer(after, flip).start()


def _group_children(pgid, n=2, within=5.0):
    t = time.time() + within
    while time.time() < t:
        if len(_group_alive(pgid)) >= n:
            return
        time.sleep(0.05)
    raise AssertionError("the fake solver did not start its grandchild")


@test
def m18_wait_cancel_stops_the_group_via_stop_group_within_a_second():
    _need_posix()
    p = subprocess.Popen(SLEEPER, start_new_session=True)
    try:
        _group_children(p.pid)
        flag, logs, stops = _Flag(), [], []
        real_stop = wc.stop_group

        def spy(proc, grace=wc.DEFAULT_GRACE_S, log=None, **k):
            stops.append(grace)
            return real_stop(proc, grace, log=log, **k)
        wc.stop_group = spy
        try:
            flag.start(0.3)
            try:
                wc.wait(p, wc.Deadline(None), poll_s=0.2, cancel=flag, grace=5.0,
                        log=logs.append)
                raise AssertionError("expected Cancelled")
            except lfmod.Cancelled:
                took = time.time() - flag.t_set
        finally:
            wc.stop_group = real_stop
        assert stops == [5.0], stops                 # through stop_group, the given grace
        assert took < 1.5, took                      # ~ poll_s + SIGTERM exit
        assert p.poll() is not None
        assert _wait_gone(p.pid) == [], "orphan left in the group"
        assert any("SIGTERM" in m for m in logs), logs
    finally:
        _kill_leftovers(p.pid)
        p.wait()


@test
def m18_wait_without_cancel_is_unchanged():
    # no cancel: the loop only polls; Deadline(None) never expires
    class P(object):
        n = 0

        def poll(self):
            self.n += 1
            return 0 if self.n > 3 else None
    assert wc.wait(P(), wc.Deadline(None), poll_s=0.0, sleep=lambda s: None) == 0
    # stop_group's own two wait() calls take no cancel (a cancel cannot re-enter the stop)
    src = inspect.getsource(wc.stop_group)
    assert src.count("wait(proc, ") == 2 and "cancel" not in src


@test
def m18_wallclock_run_uses_the_poll_loop_without_a_limit():
    src = inspect.getsource(wc.run)
    assert "p.wait()" not in src and '"cancel": cancel' in src
    r = wc.run([sys.executable, "-c", "print('ok')"], timeout=None)
    assert r.returncode == 0 and r.stdout.strip() == "ok"


@test
def m18_feedback_cancel_stops_a_wine_step_without_a_limit():
    """hecras_engine._run (every Wine preprocess step) with no wall-clock limit - the default:
    a cancel stops it through stop_group and raises lisflood.Cancelled (not a
    HecRasEngineError)."""
    _need_posix()
    flag, seen = _Flag(), {}
    real_popen = subprocess.Popen

    def popen(*a, **k):
        p = real_popen(*a, **k)
        seen["p"] = p
        return p
    subprocess.Popen = popen
    try:
        flag.start(0.6)
        t0 = time.time()
        try:
            he._run(SLEEPER, "/", {wc.ENV_GRACE: "5"}, None, "terrain HDF (fake)",
                    cancel=flag)
            raise AssertionError("expected Cancelled")
        except lfmod.Cancelled:
            took = time.time() - flag.t_set
    finally:
        subprocess.Popen = real_popen
    p = seen["p"]
    try:
        assert took < 1.5, took
        assert time.time() - t0 < 5
        assert _wait_gone(p.pid) == [], "orphan left in the group"
    finally:
        _kill_leftovers(p.pid)


class _StandInFeedback(object):
    """QgsProcessingFeedback's cancel API, for a suite run where an earlier test installed the
    stub qgis package (tests/fake_qgis.py) - the cancel path under test is the same."""

    def __init__(self):
        self._c = False

    def isCanceled(self):
        return self._c

    def cancel(self):
        self._c = True

    def pushInfo(self, m):
        pass


def _qgis_feedback():
    try:
        from qgis.core import QgsProcessingFeedback
        fb = QgsProcessingFeedback()
    except Exception:
        return _StandInFeedback()
    if not (callable(getattr(fb, "cancel", None)) and callable(getattr(fb, "isCanceled", None))):
        return _StandInFeedback()                # the stub qgis of the fast suite
    return fb


@test
def m18_qgis_feedback_cancel_stops_rasunsteady_stand_in():
    """The QGIS path: a real QgsProcessingFeedback, the plugin's _Feedback.cancel (what
    algorithms.py passes as fb.cancel) and _run_logged (RasUnsteady) - Cancel pressed while it
    runs stops the group within ~1 s via stop_group and notes it in run.log."""
    _need_posix()
    feedback = _qgis_feedback()
    try:
        from nd_breach_runner.algorithms import _Feedback
        cancel = _Feedback(feedback).cancel
    except Exception:                    # the algorithms module needs a full QGIS
        cancel = feedback.isCanceled
    d = tempfile.mkdtemp(prefix="m18_")
    log_path = os.path.join(d, "run.log")
    stops = []
    real_stop = wc.stop_group

    def spy(proc, grace=wc.DEFAULT_GRACE_S, log=None, **k):
        stops.append(proc.pid)
        return real_stop(proc, grace, log=log, **k)
    t_set = {}

    def press():
        t_set["t"] = time.time()
        feedback.cancel()
    seen = {}
    real_popen = subprocess.Popen

    def popen(*a, **k):
        assert k.get("start_new_session") is True
        p = real_popen(*a, **k)
        seen["p"] = p
        return p
    wc.stop_group, subprocess.Popen = spy, popen
    try:
        threading.Timer(0.6, press).start()
        try:
            he._run_logged(SLEEPER, d, dict(os.environ, **{wc.ENV_GRACE: "5"}), None,
                           log_path, 1.0, None, poll_s=0.2, cancel=cancel)
            raise AssertionError("expected Cancelled")
        except lfmod.Cancelled:
            took = time.time() - t_set["t"]
    finally:
        wc.stop_group, subprocess.Popen = real_stop, real_popen
    p = seen["p"]
    try:
        assert stops == [p.pid], stops
        assert took < 1.5, took
        assert _wait_gone(p.pid) == [], "orphan left in the group"
        with open(log_path) as f:
            assert "[ndbreach] CANCELLED" in f.read()
    finally:
        _kill_leftovers(p.pid)


CLI_DRIVER = r"""
import os, sys, time
sys.path.insert(0, {repo!r})
from nd_breach_runner.scripts import ndbreach_cli as cli
from nd_breach_runner.core import hecras_engine as he, lisflood as lfmod, wallclock as wc
import subprocess
cli._install_sigint()
real = subprocess.Popen
def popen(*a, **k):
    p = real(*a, **k)
    print("PGID", p.pid, flush=True)
    return p
subprocess.Popen = popen
try:
    he._run_logged({sleeper!r}, {d!r}, dict(os.environ, **{{wc.ENV_GRACE: "5"}}), None,
                   os.path.join({d!r}, "run.log"), 1.0, None, poll_s=0.2,
                   cancel=lambda: cli.CANCEL["flag"])
    print("FINISHED", flush=True)
except lfmod.Cancelled:
    print("CANCELLED", time.time(), flush=True)
    sys.exit(130)
"""


@test
def m18_cli_ctrl_c_stops_rasunsteady_stand_in():
    """The CLI path: _install_sigint's first Ctrl-C only sets CANCEL["flag"]; the solver, in its
    own session, never sees the terminal's SIGINT. The stage's cancel=lambda: CANCEL["flag"]
    stops the group via stop_group within ~1 s; the driver exits 130 with no orphan."""
    _need_posix()
    d = tempfile.mkdtemp(prefix="m18cli_")
    code = CLI_DRIVER.format(repo=REPO, sleeper=SLEEPER, d=d)
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("NDBREACH_") or k == "NDSUITE_SETTINGS":
            env.pop(k)
    drv = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True, env=env)
    pgid = None
    try:
        line = drv.stdout.readline()
        assert line.startswith("PGID"), (line, drv.stderr.read() if drv.poll() else "")
        pgid = int(line.split()[1])
        _group_children(pgid)
        t_sig = time.time()
        drv.send_signal(signal.SIGINT)                     # one Ctrl-C
        out, err = drv.communicate(timeout=20)
        assert drv.returncode == 130, (drv.returncode, out, err)
        assert "CANCELLED" in out, out
        t_done = float(out.split("CANCELLED", 1)[1].split()[0])
        assert t_done - t_sig < 1.5, t_done - t_sig
        assert _wait_gone(pgid) == [], "orphan left in the group"
        assert "Cancelling" in err
    finally:
        if drv.poll() is None:
            drv.kill()
        if pgid:
            _kill_leftovers(pgid)


@test
def m18_cancel_is_threaded_through_the_stage():
    """run_hecras_stage(cancel=) keyword-only (engine stays the 7th positional) -> run_stage
    -> preprocess (every Wine step, Ras.exe -c) and compute -> _run_logged; no default
    limit is introduced; the call sites pass fb.cancel / the CLI flag."""
    sig = inspect.signature(workflow.run_hecras_stage)
    assert list(sig.parameters)[6] == "engine"
    assert sig.parameters["cancel"].kind is inspect.Parameter.KEYWORD_ONLY
    for fn in (he.run_stage, he.preprocess, he.compute, he._run_logged, he._run,
               he.ras_compile):
        assert inspect.signature(fn).parameters["cancel"].default is None, fn.__name__
    src = inspect.getsource(he.run_stage)
    assert src.count("**ck)") == 2
    src = inspect.getsource(he.preprocess)
    assert src.count("**ck)") == 7, src.count("**ck)")
    assert '{"cancel": cancel}' in inspect.getsource(he.compute)
    src = inspect.getsource(he.ras_compile)
    assert "cancel()" in src and "_cancelled_error()" in src
    # the 3.10.48 decision stands: no default compute limit
    assert he.compute_timeout(10000, 24.0, 2.0, env={})[0] is None
    alg = open(os.path.join(ROOT, "algorithms.py"), encoding="utf-8").read()
    assert alg.count("cancel=fb.cancel") >= 2
    cli = open(os.path.join(ROOT, "scripts", "ndbreach_cli.py"), encoding="utf-8").read()
    assert 'cancel=lambda: CANCEL["flag"]' in cli


@test
def m18_run_hecras_stage_passes_cancel_and_lets_cancelled_through():
    class Eng(object):
        class HecRasEngineError(Exception):
            pass

        def __init__(self):
            self.kw = None

        def run_stage(self, folder, final, writer, st, log, **kw):
            self.kw = kw
            if kw.get("cancel") and kw["cancel"]():
                raise lfmod.Cancelled("cancelled")
            return {"final": {}, "warnings": []}
    import types
    old = workflow.adopt_final_engine
    workflow.adopt_final_engine = lambda folder, name, st, xy, log: {"final_model": name}
    try:
        res = types.SimpleNamespace(final={}, warnings=[], summary={})
        e = Eng()
        workflow.run_hecras_stage("/tmp/x", res, None, workflow.RunSettings(), (0, 0), None, e)
        assert e.kw == {}                     # no cancel: the engine is called as before
        try:
            workflow.run_hecras_stage("/tmp/x", res, None, workflow.RunSettings(), (0, 0),
                                      None, e, cancel=lambda: True)
            raise AssertionError("expected Cancelled")
        except lfmod.Cancelled:
            pass                              # not wrapped in HecRasStageError
        assert "cancel" in e.kw
    finally:
        workflow.adopt_final_engine = old
