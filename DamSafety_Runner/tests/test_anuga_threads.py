"""3.10.47: ANUGA's OpenMP threads (core/anuga_model.resolve_threads, anuga_worker.apply_threads).

ANUGA 4 runs on ONE thread unless told otherwise (OMP_NUM_THREADS unset -> 1), so the Runner's
threads = 0 ("0 = all cores", true for LISFLOOD-FP's OpenMP) left ANUGA single-threaded. Now 0
resolves to NDBREACH_ANUGA_THREADS (when > 0), else min(CPUs - 2, 4), at least 1; an explicit
count is used as given. The job file, OMP_NUM_THREADS of the worker and ANUGA's
set_omp_num_threads all get the count, run_report records it. The thread count changes no
number (checkpoint fingerprints already skip it), and a build without OpenMP or without the
setter just runs on one thread. Also: the Envelope operator's per-step update
(anuga_worker.envelope_update, 3.10.47) gives the same envelope, bit for bit, as before.
The model test (--models, NDBREACH_ANUGA_PYTHON) runs the real worker at 1 and 4 threads."""
import os
import shutil
import subprocess
import tempfile
import types

import numpy as np

from nd_breach_runner.core import anuga_model as AM
from nd_breach_runner.core import anuga_worker as AW
from nd_breach_runner.core import workflow as W
from nd_breach_runner.tests import test_anuga_checkpoint as TC
from nd_breach_runner.tests.registry import test, model_test


class _Omp:
    """Keep OMP_NUM_THREADS / NDBREACH_ANUGA_THREADS of this process as they were."""
    KEYS = ("OMP_NUM_THREADS", AM.THREADS_ENV)

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.KEYS}
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def anuga_threads_resolve_rule():
    r = AM.resolve_threads
    assert r(3, env={}, cpus=8)[0] == 3                          # explicit: as given
    assert r(6, env={AM.THREADS_ENV: "2"}, cpus=8)[0] == 6        # explicit beats the env
    assert r(0, env={}, cpus=8) == (4, "automatic: min(8 CPUs - 2, 4)")
    assert r(None, env={}, cpus=5)[0] == 3
    assert r(0, env={}, cpus=2)[0] == 1 and r(0, env={}, cpus=1)[0] == 1
    assert r(0, env={}, cpus=64)[0] == 4
    n, why = r(0, env={AM.THREADS_ENV: "6"}, cpus=8)
    assert n == 6 and why == "NDBREACH_ANUGA_THREADS=6", why
    for bad in ("0", "-2", "lots"):
        n, why = r(0, env={AM.THREADS_ENV: bad}, cpus=8)
        assert n == 4 and "ignored" in why, (bad, why)
    assert r(0, env={AM.THREADS_ENV: " "}, cpus=8)[0] == 4
    assert r("x", env={}, cpus=8)[0] == 4
    assert AM.cpu_count() >= 1
    assert r(4.0, env={}, cpus=8)[0] == 4 and r("3", env={}, cpus=8)[0] == 3
    n, _ = r(0, env={})                       # this machine
    assert 1 <= n <= AM.AUTO_THREADS_MAX


@test
def anuga_threads_job_file_gets_the_resolved_count():
    """Through anuga_model.run (the worker fails without ANUGA after the job is written)."""
    from nd_breach_runner.tests.test_gmsh_mesher import _case
    from nd_breach_runner.core.lisflood import ModelError
    got = {}
    with _Omp():
        for name, threads, envv in (("auto", 0, None), ("env", 0, "3"), ("set", 2, "3")):
            if envv is None:
                os.environ.pop(AM.THREADS_ENV, None)
            else:
                os.environ[AM.THREADS_ENV] = envv
            dem, flooded, inflow, hy, st, roads = _case()
            st.threads = threads
            d = tempfile.mkdtemp()
            logs = []
            try:
                try:
                    AM.run(d, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st,
                           refine_lines=roads, log=logs.append)
                except ModelError:
                    pass
                with np.load(os.path.join(d, "anuga_job.npz")) as z:
                    got[name] = int(z["threads"])
                assert any(m.startswith("ANUGA threads: {} (".format(got[name])) for m in logs), logs
                assert st.threads == threads          # the caller's settings are not changed
            finally:
                shutil.rmtree(d)
    assert got["auto"] == AM.resolve_threads(0, env={})[0] >= 1, got
    assert got["env"] == 3 and got["set"] == 2, got


@test
def anuga_threads_launch_sets_omp_num_threads():
    if os.name == "nt":
        print("skip: POSIX shell")
        return
    d = tempfile.mkdtemp()
    try:
        py = os.path.join(d, "fakepy.sh")
        with open(py, "w") as f:
            f.write('#!/bin/sh\necho "$OMP_NUM_THREADS" > omp.txt\necho PROGRESS 100 done\n')
        os.chmod(py, 0o755)
        with _Omp():
            os.environ.pop("OMP_NUM_THREADS", None)
            AM._launch(AM.AnugaSettings(py, threads=4), "j.npz", "r.npz", d, None, None)
        with open(os.path.join(d, "omp.txt")) as f:
            assert f.read().strip() == "4"
    finally:
        shutil.rmtree(d)


@test
def anuga_threads_apply_is_harmless_without_openmp():
    calls = []

    def setter(n, verbose=True):
        calls.append(n)

    ok = types.SimpleNamespace(set_omp_num_threads=setter, get_omp_num_threads=lambda: 4)
    with _Omp():
        assert AW.apply_threads(ok, 4) == (4, "") and calls == [4]
        assert os.environ["OMP_NUM_THREADS"] == "4"

        def broken(n, verbose=True):
            raise ImportError("no sw_domain_openmp_ext")
        n, note = AW.apply_threads(types.SimpleNamespace(set_omp_num_threads=broken,
                                                         get_omp_num_threads=lambda: 1), 4)
        assert n == 1 and "failed" in note, note
        n, note = AW.apply_threads(types.SimpleNamespace(), 4)          # an old ANUGA
        assert n is None and "no set_omp_num_threads" in note, note
        calls.clear()
        assert AW.apply_threads(ok, 0) == (4, "") and calls == []       # 0: nothing set


@test
def anuga_threads_worker_results_and_resume_ignore_the_thread_count():
    """Fake ANUGA: 1 vs 4 threads give the same grids; a run stopped at 1 thread resumes at 4
    from its checkpoint (same fingerprint) and still matches."""
    d0, d1, d2 = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        r0 = TC._fake_run(d0, threads=1)
        r1 = TC._fake_run(d1, threads=4)
        g0, s0 = TC._grids(r0)
        g1, s1 = TC._grids(r1)
        TC._same(g0, g1)
        assert s1.get("omp_note"), s1        # the fake has no setter: said, not fatal
        assert TC._fake_run(d2, stop_at=900.0, threads=1, ckpt_every_steps=5) is None
        r2 = TC._fake_run(d2, threads=4, ckpt_every_steps=5, resume=1)
        g2, s2 = TC._grids(r2)
        TC._same(g0, g2)
        assert s2["checkpoint"]["resumed_from_t_s"] is not None, s2["checkpoint"]
    finally:
        for d in (d0, d1, d2):
            shutil.rmtree(d)


def _envelope_before_3_10_47(op, h_stage, elev, xm, ym, t):
    """The Envelope.__call__ body of 3.10.46 and earlier (reference)."""
    h = h_stage - elev
    np.maximum(op["hmax"], h, out=op["hmax"])
    np.maximum(op["wmax"], np.where(h > 0.001, h_stage, -np.inf), out=op["wmax"])
    deep = h >= op["vmin"]
    sp = np.zeros_like(h)
    sp[deep] = np.hypot(xm[deep], ym[deep]) / h[deep]
    np.maximum(op["vmax"], sp, out=op["vmax"])
    new = np.isnan(op["arr"]) & (h >= op["ad"])
    if new.any():
        op["arr"][new] = t / 3600.0


@test
def anuga_threads_envelope_update_is_bit_identical():
    rng = np.random.default_rng(47)
    n = 20000
    elev = rng.uniform(0.0, 10.0, n)
    for vmin, ad in ((0.03, 0.3048), (0.03, 0.03048), (0.0, 0.1)):
        a = dict(hmax=np.zeros(n), vmax=np.zeros(n), wmax=np.full(n, -np.inf),
                 arr=np.full(n, np.nan), vmin=vmin, ad=ad)
        b = {k: (v.copy() if isinstance(v, np.ndarray) else v) for k, v in a.items()}
        for step in range(40):
            wet = rng.random(n) < (0.1 + step / 50.0)
            stage = elev + np.where(wet, rng.exponential(0.5, n), -rng.uniform(0, 1e-3, n))
            stage[rng.integers(0, n, 5)] = elev[:5] + 0.03          # exactly at a threshold
            stage[rng.integers(0, n, 3)] = np.nan
            xm, ym = rng.normal(0, 1, n), rng.normal(0, 1, n)
            t = 30.0 * step
            with np.errstate(divide="ignore", invalid="ignore"):
                _envelope_before_3_10_47(a, stage, elev, xm, ym, t)
                AW.envelope_update(stage, elev, xm, ym, b["hmax"], b["vmax"], b["wmax"],
                                   b["arr"], vmin, ad, lambda t=t: t)
        for k in ("hmax", "vmax", "wmax", "arr"):
            assert np.array_equal(a[k], b[k], equal_nan=True), (vmin, k)
            assert a[k].tobytes() == b[k].tobytes(), (vmin, k)


@test
def anuga_threads_report_line():
    th = {"requested": 0, "used": 4, "rule": "automatic: min(8 CPUs - 2, 4)",
          "anuga_reported": 4}
    assert W.anuga_threads_line(th) == "4 OpenMP thread(s) (automatic: min(8 CPUs - 2, 4))"
    line = W.anuga_threads_line(dict(th, anuga_reported=1, note="set_omp_num_threads failed"))
    assert "ANUGA reported 1" in line and "failed" in line, line


@model_test
def anuga_threads_real_anuga_1_vs_4_threads_bit_identical():
    """The real worker on the 589-triangle test domain (30 simulated minutes, seconds) at 1
    and at 4 OpenMP threads: the same depth, velocity, arrival and WSE, bit for bit, and
    ANUGA reports the count it was given."""
    py = TC._anuga_python()
    if not py:
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    out = {}
    for n in (1, 4):
        d = tempfile.mkdtemp()
        try:
            TC._job(os.path.join(d, "j.npz"), threads=n)
            env = AM._clean_env()
            env.pop("OMP_NUM_THREADS", None)            # the worker sets it from the job
            subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d, env=env,
                           check=True, stdout=subprocess.DEVNULL, timeout=300)
            out[n] = TC._grids(os.path.join(d, "r.npz"))
        finally:
            shutil.rmtree(d)
    TC._same(out[1][0], out[4][0])
    for k in out[1][0]:
        assert out[1][0][k].tobytes() == out[4][0][k].tobytes(), k
    assert out[1][1]["omp_threads"] == 1 and out[4][1]["omp_threads"] == 4, (
        out[1][1].get("omp_threads"), out[4][1].get("omp_threads"))


# ---------------------------------------- 3.10.56: Docs & QA follow-ups to #132 (see #135)
@test
def anuga_threads_env_inf_nan_and_garbage_fall_back_with_a_note():
    """(1) NDBREACH_ANUGA_THREADS=inf raised OverflowError (int(float('inf'))) and stopped the
    run; inf / nan / garbage are now ignored with a note and the automatic count is used."""
    r = AM.resolve_threads
    for bad, why in (("inf", "not a finite number"), ("-inf", "not a finite number"),
                     ("Infinity", "not a finite number"), ("nan", "not a finite number"),
                     ("lots", "not a number"), ("1e400", "not a finite number"),
                     ("4 threads", "not a number")):
        n, rule = r(0, env={AM.THREADS_ENV: bad}, cpus=8)
        assert n == 4 and rule.startswith("automatic"), (bad, n, rule)
        assert "ignored ({})".format(why) in rule and repr(bad) in rule, (bad, rule)
    # an explicit request that is not a finite whole number never raises either
    for bad in (float("inf"), float("nan"), "inf", object()):
        n, rule = r(bad, env={}, cpus=8)
        assert n == 4 and "ignored" in rule, (bad, rule)


@test
def anuga_threads_decimals_are_rejected_not_truncated():
    """(2) 2.7 used to become 2; a count must be a whole number."""
    r = AM.resolve_threads
    for bad in ("2.7", "0.5", "3.000001"):
        n, rule = r(0, env={AM.THREADS_ENV: bad}, cpus=8)
        assert n == 4 and "ignored (not a whole number)" in rule, (bad, rule)
    n, rule = r(2.7, env={}, cpus=8)
    assert n == 4 and "threads=2.7 ignored (not a whole number)" in rule, rule
    n, rule = r(2.7, env={AM.THREADS_ENV: "3"}, cpus=8)       # falls back to the env
    assert n == 3 and rule.startswith("NDBREACH_ANUGA_THREADS=3") and "2.7" in rule, rule
    assert r(0, env={AM.THREADS_ENV: "6.0"}, cpus=8)[0] == 6  # a whole number written 6.0
    assert r(0, env={AM.THREADS_ENV: " 5 "}, cpus=8)[0] == 5


@test
def anuga_threads_count_is_capped_at_the_cpus():
    """(4) 64 threads on 8 CPUs: clamped to 8 with a note (explicit and env)."""
    r = AM.resolve_threads
    n, rule = r(64, env={}, cpus=8)
    assert n == 8 and rule == "set (64), clamped to the 8 CPUs", rule
    n, rule = r(0, env={AM.THREADS_ENV: "64"}, cpus=8)
    assert n == 8 and rule == "NDBREACH_ANUGA_THREADS=64, clamped to the 8 CPUs", rule
    assert r(8, env={}, cpus=8) == (8, "set (8)")               # at the limit: as given
    assert r(0, env={AM.THREADS_ENV: "8"}, cpus=8) == (8, "NDBREACH_ANUGA_THREADS=8")
    assert r(3, env={}, cpus=1)[0] == 1
    n, _ = r(10 ** 6, env={})                                    # this machine
    assert n == AM.cpu_count(), n


@test
def ndbreach_cli_threads_help_states_the_anuga_rule():
    """(3) --threads said '(0 = all)', true only for LISFLOOD-FP since 3.10.47."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(AM.__file__))),
                        "scripts", "ndbreach_cli.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert '"model threads (0 = all)"' not in src
    i = src.index('"--threads"')
    seg = src[i:i + 400]
    for w in ("LISFLOOD-FP uses all cores", "NDBREACH_ANUGA_THREADS", "CPUs - 2, at most 4",
              "clamped"):
        assert w in seg, (w, seg)


@test
def anuga_threads_negative_explicit_count_is_noted():
    """Review (Docs & QA): threads=-3 used to fall through to the automatic count silently."""
    n, rule = AM.resolve_threads(-3, env={}, cpus=8)
    assert n == 4 and "threads=-3 ignored (not > 0)" in rule and rule.startswith("automatic"), rule
