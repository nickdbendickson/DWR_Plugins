"""3.12.4: host-pause detection on the ANUGA worker's PROGRESS lines and the checkpoint timer
on time.monotonic() (core/anuga_dtdiag.YieldClock / MonoTimer, core/anuga_worker.py).

DWR-2801 r3 logged 12 "stalls" (128-2083 s of wall time per 300 s of model time) with normal
step counts: the box's VM was being frozen, and ANUGA's checkpoint_time (time.time()) saved
an extra checkpoint at the first yield after each freeze. No real ANUGA here: the clocks are
mocked (realtime jumps, monotonic does not) and the worker runs on tests/fake_anuga.py."""
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_detach as AD  # noqa: E402
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_dtdiag as DD  # noqa: E402
from nd_breach_runner.core import anuga_slowcheck as SC  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.tests import fake_anuga as FA  # noqa: E402
from nd_breach_runner.tests import test_anuga_checkpoint as TC  # noqa: E402
from nd_breach_runner.tests.registry import test, model_test  # noqa: E402


class _Clocks:
    """Mock realtime / monotonic / CPU clocks; freeze(s) = a VM freeze of s seconds (the
    realtime clock steps forward on resume, monotonic and CPU do not)."""

    def __init__(self):
        self.r, self.m, self.c = 1.8e9, 1000.0, 5.0

    def run(self, s, cores=4.0):
        self.r += s
        self.m += s
        self.c += s * cores

    def freeze(self, s):
        self.r += s

    def real(self):
        return self.r

    def mono(self):
        return self.m

    def cpu(self):
        return self.c


class _D:
    number_of_steps = 200


def _clock(k):
    return DD.YieldClock(0.0, real=k.real, mono=k.mono, cpu=k.cpu)


# ------------------------------------------------------------------ YieldClock
@test
def pausecheck_progress_line_has_cpu_wall_and_box_paused_on_a_freeze():
    k = _Clocks()
    c = _clock(k)
    for i in range(5):                          # normal steps: 30 s wall, 4 cores busy
        k.run(30.0)
        f = c.tick(_D(), 300.0 * (i + 1))
        assert f["paused_s"] == 0.0 and abs(f["cpu_s"] - 120.0) < 1e-6
        assert abs(f["mono_s"] - 30.0) < 1e-6 and abs(f["wall_s"] - 30.0) < 1e-6
        assert "box paused" not in c.suffix(f)
    k.run(10.0)
    k.freeze(2000.0)                            # the VM is frozen mid-step
    k.run(20.0)
    f = c.tick(_D(), 1800.0)
    assert abs(f["paused_s"] - 2000.0) < 1e-6 and f["paused_by"] == "clock", f
    line = "PROGRESS {:.2f} t={:.0f}s{}".format(25.0, 1800.0, c.suffix(f))
    assert re.search(r" wall_s=2030\.0 ", line), line
    assert " cpu_s=120.0 mono_s=30.0 paused_s=2000" in line, line
    assert line.endswith(" box paused ~2000 s"), line
    s = c.summary()
    assert s["paused_s"] == 2000.0 and s["paused_yield_steps"] == 1 and s["largest_s"] == 2000.0
    assert "all threads" in s["note"]
    assert "box paused ~2000 s in total over 1 yield step(s)" in c.summary_line()


@test
def pausecheck_cpu_check_finds_a_freeze_monotonic_ran_through():
    k = _Clocks()
    c = _clock(k)
    for i in range(5):
        k.run(30.0)
        c.tick(_D(), 300.0 * (i + 1))
    k.run(30.0)                                 # 30 s of work ...
    k.m += 600.0                                # ... and 600 s monotonic + realtime, no CPU
    k.r += 600.0
    f = c.tick(_D(), 1800.0)
    assert f["paused_by"] == "cpu" and abs(f["paused_s"] - 600.0) < 1e-6, f
    # a real slowdown (many more steps, CPU busy all along) is NOT a pause
    class Busy:
        number_of_steps = 2000
    k.run(300.0)
    f = c.tick(Busy(), 2100.0)
    assert f["paused_s"] == 0.0, f
    # short gaps (scheduler noise) are not reported
    k.run(30.0)
    k.freeze(5.0)
    assert c.tick(_D(), 2400.0)["paused_s"] == 0.0


@test
def pausecheck_suffix_without_clock_fields_is_the_old_format():
    f = {"utc": "2026-10-28T22:35:00Z", "steps_total": 34, "steps": 17, "wall_s": 2.0,
         "rate": 150.0}
    assert DD.YieldClock.suffix(f) == \
        " utc=2026-10-28T22:35:00Z steps=34 dsteps=17 wall_s=2.0 rate=150"


# ------------------------------------------------------------------ MonoTimer
@test
def pausecheck_checkpoint_timer_ignores_a_realtime_jump():
    k = _Clocks()
    t = DD.MonoTimer(1800.0, mono=k.mono)
    k.run(600.0)
    k.freeze(7200.0)                            # 2 h frozen: realtime jumps, monotonic not
    assert not t.due() and abs(t.elapsed() - 600.0) < 1e-6
    k.run(1200.0)
    assert t.due()
    t.reset()
    assert not t.due()
    k.m += 1700.0                               # a freeze monotonic ran through ...
    t.add_pause(1600.0)                         # ... that the CPU check measured
    assert not t.due()


# ------------------------------------------------------------------ parsers: old and new lines
OLD = ("PROGRESS 2.50 t=600s utc=2026-10-28T20:00:00Z steps=34 dsteps=17 wall_s=30.1 "
       "rate=9.97")
NEW = ("PROGRESS 5.00 t=900s utc=2026-10-28T22:35:00Z steps=51 dsteps=17 wall_s=2030.0 "
       "rate=0.148 cpu_s=120.0 mono_s=30.0 paused_s=2000 box paused ~2000 s")
OLDEST = "PROGRESS 1.25 t=300s"


@test
def pausecheck_parsers_read_old_and_new_progress_lines():
    for ln in (OLDEST, OLD, NEW):
        p = AD.parse_progress(ln + "\n")         # anuga_model._watch / anuga_detach tail
        assert p is not None and p[1].startswith("t="), (ln, p)
    assert AD.parse_progress(NEW)[0] == 5.0
    assert AD.parse_progress(NEW)[1].endswith("box paused ~2000 s")
    ys, resumed, done = SC.parse_log([OLDEST, OLD, NEW, "PROGRESS 100.00 box paused ~2000 s in "
                                      "total over 1 yield step(s) (largest ~2000 s); cpu_s=1 "
                                      "mono_s=1 wall_s=1", "PROGRESS 100 done"])
    assert [y["t"] for y in ys] == [300.0, 600.0, 900.0] and done
    assert ys[1]["wall_s"] == 30.1 and "cpu_s" not in ys[1]
    assert ys[2]["cpu_s"] == 120.0 and ys[2]["mono_s"] == 30.0 and ys[2]["paused_s"] == 2000


# ------------------------------------------------------------------ the worker (fake ANUGA)
def _worker_run(d, step_s, freeze_at=None, freeze_s=0.0, stop_at=None, **extra):
    """The worker on the fake ANUGA with mocked clocks: each yield step takes step_s of
    monotonic time; at model time freeze_at the realtime clock jumps freeze_s ahead."""
    k = _Clocks()
    orig = DD.YieldClock.tick
    jumped = {"done": False}

    def tick(self, domain, t):
        k.run(step_s, cores=1.0)
        if freeze_at is not None and t >= freeze_at and not jumped["done"]:
            k.freeze(freeze_s)
            jumped["done"] = True
        return orig(self, domain, t)

    saved = (time.time, time.monotonic, time.process_time)
    DD.YieldClock.tick = tick
    time.time, time.monotonic, time.process_time = k.real, k.mono, k.cpu
    job = TC._job(os.path.join(d, "anuga_job.npz"), **extra)
    res = os.path.join(d, "anuga_result.npz")
    undo = FA.install()
    FA.STOP_AT["t"] = stop_at
    out = io.StringIO()
    omp = os.environ.get("OMP_NUM_THREADS")
    try:
        with contextlib.redirect_stdout(out):
            try:
                AW.main(job, res)
            except SystemExit:
                res = None
    finally:
        time.time, time.monotonic, time.process_time = saved
        DD.YieldClock.tick = orig
        FA.STOP_AT["t"] = None
        undo()
        if omp is None:
            os.environ.pop("OMP_NUM_THREADS", None)
        else:
            os.environ["OMP_NUM_THREADS"] = omp
    return res, out.getvalue()


def _stats(p):
    with np.load(p) as z:
        return json.loads(str(z["stats"]))


@test
def pausecheck_worker_logs_the_pause_and_saves_no_extra_checkpoint():
    d = tempfile.mkdtemp()
    try:
        with TC._Env():
            res, log = _worker_run(d, 1.0, freeze_at=600.0, freeze_s=3600.0,
                                   ckpt_every_min=1.0, partial_every_min=0.0)
        assert res is not None, log
        st = _stats(res)
        # 31 yields x 1 s of monotonic time < 60 s: the 1 h realtime jump saves nothing
        assert st["checkpoint"]["checkpoints_saved"] == 0, (st["checkpoint"], log)
        assert st["checkpoint"]["checkpoint_timer"] == "monotonic"
        assert st["host_pauses"]["paused_s"] == 3600.0 and \
            st["host_pauses"]["paused_yield_steps"] == 1, st["host_pauses"]
        lines = [ln for ln in log.splitlines() if ln.startswith("PROGRESS") and " t=600s " in ln]
        assert lines and lines[0].endswith("box paused ~3600 s"), lines
        assert " cpu_s=" in lines[0] and " mono_s=1.0 " in lines[0]
        assert "box paused ~3600 s in total over 1 yield step(s)" in log
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def pausecheck_worker_monotonic_checkpoints_resume_like_an_uninterrupted_run():
    d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        with TC._Env():
            # 25 s of monotonic time per yield step, a checkpoint every minute (~3rd yield)
            full, _ = _worker_run(d1, 25.0, ckpt_every_min=1.0, partial_every_min=0.0)
            st = _stats(full)
            assert st["checkpoint"]["checkpoints_saved"] >= 5, st["checkpoint"]
            assert _worker_run(d2, 25.0, stop_at=1000.0, ckpt_every_min=1.0,
                               partial_every_min=0.0)[0] is None
            ck = [f for _, _, fs in os.walk(d2) for f in fs if f.endswith(".pickle")]
            assert ck and not [f for _, _, fs in os.walk(d2) for f in fs
                               if f.endswith(".tmp")], ck
            res, log = _worker_run(d2, 25.0, ckpt_every_min=1.0, partial_every_min=0.0,
                                   resume=1)
            assert res is not None and "resumed from the checkpoint" in log, log
            TC._same(TC._grids(full)[0], TC._grids(res)[0])
    finally:
        shutil.rmtree(d1, ignore_errors=True)
        shutil.rmtree(d2, ignore_errors=True)


@model_test
def pausecheck_real_anuga_monotonic_checkpoints_kill_and_resume_is_bit_identical():
    """Real ANUGA (589 triangles, seconds): the worker's own monotonic-timed checkpoints
    (ckpt_every_min tiny = one at every yield) load back after SIGKILL, and the envelope
    equals the uninterrupted run."""
    py = TC._anuga_python()
    if not py:
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    env = dict(AM._clean_env(), OMP_NUM_THREADS="1")
    try:
        TC._job(os.path.join(d0, "j.npz"))
        subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d0, env=env, check=True,
                       stdout=subprocess.DEVNULL, timeout=300)
        TC._job(os.path.join(d1, "j.npz"), ckpt_every_min=1e-6)
        p = subprocess.Popen([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d1, env=env,
                             stdout=subprocess.PIPE, text=True)
        seen = []
        for ln in p.stdout:
            seen.append(ln)
            if "t=1020s" in ln:
                p.send_signal(signal.SIGKILL)
                break
        p.wait()
        assert any(" cpu_s=" in ln and " mono_s=" in ln for ln in seen), seen[-3:]
        assert not os.path.exists(os.path.join(d1, "r.npz"))
        TC._job(os.path.join(d1, "j.npz"), ckpt_every_min=1e-6, resume=1)
        subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d1, env=env, check=True,
                       stdout=subprocess.DEVNULL, timeout=300)
        g0, _ = TC._grids(os.path.join(d0, "r.npz"))
        g1, s1 = TC._grids(os.path.join(d1, "r.npz"))
        TC._same(g0, g1)
        ck = s1["checkpoint"]
        assert ck["checkpoint_timer"] == "monotonic" and ck["resumed_from_t_s"] >= 900.0, ck
        assert "paused_s" in s1["host_pauses"]
    finally:
        shutil.rmtree(d0, ignore_errors=True)
        shutil.rmtree(d1, ignore_errors=True)
