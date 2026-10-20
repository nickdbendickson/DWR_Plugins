"""3.10.35: opt-in ANUGA checkpoints, partial results and resume (core/anuga_worker.py).

Off by default: the job file and the worker are as before. With checkpoints on, ANUGA's
domain.set_checkpointing pickles the domain (the envelope operator rides along), the worker
lists the complete checkpoints in checkpoint.json, keeps the newest few, flushes
anuga_partial.npz atomically, and a resumed run gives the same envelope as an
uninterrupted one. The worker tests use tests/fake_anuga.py (no hydraulics, no ANUGA);
the model test (--models, NDBREACH_ANUGA_PYTHON) runs the real worker on a 589-triangle
domain for 30 simulated minutes, a few seconds."""
import contextlib
import io
import json
import os
import pickle
import shutil
import signal
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import workflow as W  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.tests import fake_anuga as FA  # noqa: E402
from nd_breach_runner.tests.registry import test, model_test  # noqa: E402

ENV_KEYS = ("NDBREACH_ANUGA_CHECKPOINT_MIN", "NDBREACH_ANUGA_CHECKPOINT_STEPS",
            "NDBREACH_ANUGA_CHECKPOINT_KEEP", "NDBREACH_ANUGA_PARTIAL_MIN",
            "NDBREACH_ANUGA_RESUME", "ANUGA_CHECKPOINT_MIN", "ANUGA_CHECKPOINT_STEPS",
            "ANUGA_CHECKPOINT_KEEP", "ANUGA_PARTIAL_MIN", "ANUGA_RESUME")


class _Env:
    """Set some environment variables (None = unset) for a block, then restore."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in set(ENV_KEYS) | set(self.kw)}
        for k in self.old:
            os.environ.pop(k, None)
        for k, v in self.kw.items():
            if v is not None:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _job(path, sim=1800.0, ys=60.0, **extra):
    """A small job file (the layout write_job makes)."""
    ny, nx, dx = 30, 50, 20.0
    x = (np.arange(nx) + 0.5) * dx
    y = (np.arange(ny) + 0.5) * dx
    X, Y = np.meshgrid(x, y)
    dem = 100.0 - 0.005 * X + 0.02 * np.abs(Y - 300.0)
    x0, y0 = 500000.0, 5200600.0
    b = np.array([[x0 + 10, y0 - 590], [x0 + 990, y0 - 590], [x0 + 990, y0 - 10],
                  [x0 + 10, y0 - 10]])
    job = dict(dem=dem.astype(np.float32), dem_x0=x0, dem_y0=y0, dem_dx=dx, manning_n=0.05,
               manning=np.zeros(0), boundary=b, fine=np.zeros((0, 2)),
               inlet=np.array([[x0 + 60, y0 - 260], [x0 + 60, y0 - 340]]),
               fine_area=500.0, coarse_area=1500.0,
               hyd_t=np.array([0., 300., 900., 1800.]), hyd_q=np.array([0., 80., 20., 0.]),
               sim_time=sim, yieldstep=ys, arrival_depth=0.3048, velocity_min_depth=0.03,
               algorithm="DE0", threads=1, out_x0=x0, out_y0=y0, out_dx=dx, out_nx=nx,
               out_ny=ny)
    job.update(extra)
    np.savez(path, **job)
    return path


def _fake_run(d, stop_at=None, **extra):
    """Run the worker's main() with the fake ANUGA in folder d; returns the result path, or
    None when it was stopped (SystemExit, like SIGTERM) at model time stop_at."""
    job = _job(os.path.join(d, "anuga_job.npz"), **extra)
    res = os.path.join(d, "anuga_result.npz")
    undo = FA.install()
    FA.STOP_AT["t"] = stop_at
    try:
        with contextlib.redirect_stdout(io.StringIO()):    # the PROGRESS lines
            AW.main(job, res)
        return res
    except SystemExit:
        return None
    finally:
        FA.STOP_AT["t"] = None
        undo()


def _grids(p):
    with np.load(p) as z:
        return ({k: z[k].copy() for k in ("depth", "velocity", "arrival_hr", "wse")},
                json.loads(str(z["stats"])))


def _same(a, b):
    for k in a:
        assert np.array_equal(a[k], b[k], equal_nan=True), k


# ----------------------------------------------------------------------- defaults: off
@test
def anuga_checkpoint_off_by_default_job_has_no_checkpoint_keys():
    st = AM.AnugaSettings("python")
    assert AM.checkpoint_job(st) == {} and not AM.checkpointing(st)
    d = tempfile.mkdtemp()
    try:
        g = Grid(np.full((4, 5), 10.0), 0.0, 40.0, 10.0)
        hy = type("H", (), {"t_s": np.array([0.0, 60.0]), "q_cms": np.array([0.0, 1.0])})()
        sq = np.array([[0, 0], [50, 0], [50, 40], [0, 40]], float)
        AM.write_job(os.path.join(d, "j.npz"), g, sq, np.zeros((0, 2)), sq[:2], hy, st, g)
        with np.load(os.path.join(d, "j.npz")) as z:
            assert not set(z.files) & set(AW.CKPT_KEYS), z.files
        assert AW.checkpoint_config({"sim_time": 1.0}) is None
        st.checkpoint_min, st.partial_min = 15.0, 5.0
        AM.write_job(os.path.join(d, "j.npz"), g, sq, np.zeros((0, 2)), sq[:2], hy, st, g)
        with np.load(os.path.join(d, "j.npz")) as z:
            cfg = AW.checkpoint_config(dict(z))
        assert cfg["checkpoint"] and cfg["every_min"] == 15.0 and cfg["keep"] == 2
        assert cfg["partial_every_min"] == 5.0 and not cfg["resume"]
        assert AM.checkpointing(st)
    finally:
        shutil.rmtree(d)


@test
def anuga_checkpoint_settings_env_report_and_run_key():
    with _Env():
        st = W.RunSettings()
        d = st.as_dict()
        assert not set(d) & set(W.ANUGA_CHECKPOINT_DEFAULTS), "defaults stay out of the report"
        k0 = W.key_settings(st)
    with _Env(NDBREACH_ANUGA_CHECKPOINT_MIN="20", ANUGA_PARTIAL_MIN="5",
              NDBREACH_ANUGA_RESUME="yes", NDBREACH_ANUGA_CHECKPOINT_KEEP="x"):
        st = W.RunSettings()
        assert st.anuga_checkpoint_min == 20.0 and st.anuga_partial_min == 5.0
        assert st.anuga_resume is True and st.anuga_checkpoint_keep == 2   # bad number: default
        assert st.as_dict()["anuga_checkpoint_min"] == 20.0
        assert W.key_settings(st) == k0, "checkpoint settings do not change the run key"
    line = W.anuga_checkpoint_line({"every_min": 20.0, "checkpoints_saved": 3,
                                    "partial_flushes": 4, "resumed_from_t_s": 7200.0})
    assert line == ("every 20 min of wall time; 3 saved; partial results flushed 4 times; "
                    "resumed from t = 2.00 h"), line
    assert "OFF: boom" in W.anuga_checkpoint_line({"every_steps": 5, "checkpoint_error": "boom"})


# ----------------------------------------------------------------------- helpers
@test
def anuga_checkpoint_fingerprint_ignores_checkpoint_keys_and_threads():
    job = {"dem": np.ones((3, 3), np.float32), "sim_time": np.float64(10.0), "threads": 4}
    f0 = AW.job_fingerprint(job)
    assert AW.job_fingerprint(dict(job, ckpt_every_min=5.0, resume=1, threads=8,
                                   mesh_stats='{"seconds": 3}')) == f0
    assert AW.job_fingerprint(dict(job, sim_time=np.float64(11.0))) != f0
    j2 = dict(job)
    j2["dem"] = job["dem"].copy()
    j2["dem"][1, 1] = 2.0
    assert AW.job_fingerprint(j2) != f0


@test
def anuga_checkpoint_partial_write_is_atomic():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "anuga_partial.npz")
        AW.savez_atomic(p, a=np.arange(3))
        real = np.savez_compressed

        def broken(f, **kw):
            f.write(b"half a file")
            raise OSError("disk full")
        np.savez_compressed = broken
        try:
            AW.savez_atomic(p, a=np.arange(5))
            assert False, "expected OSError"
        except OSError:
            pass
        finally:
            np.savez_compressed = real
        with np.load(p) as z:                    # the old file, whole
            assert z["a"].tolist() == [0, 1, 2]
        AW.savez_atomic(p, a=np.arange(4))
        with np.load(p) as z:
            assert z["a"].tolist() == [0, 1, 2, 3]
        assert sorted(os.listdir(d)) == ["anuga_partial.npz"]     # no .tmp left behind
        AW.write_json_atomic(os.path.join(d, "c.json"), {"times": [1.0]})
        assert AW.read_meta(d) is None
        os.replace(os.path.join(d, "c.json"), os.path.join(d, AW.META_NAME))
        assert AW.read_meta(d) == {"times": [1.0]}
    finally:
        shutil.rmtree(d)


@test
def anuga_checkpoint_prune_keeps_the_newest():
    d = tempfile.mkdtemp()
    try:
        for t in (300.0, 600.0, 900.0):
            open(AW.checkpoint_path(d, t), "wb").close()
        assert os.path.basename(AW.checkpoint_path(d, 600)) == "anuga_breach_600.0.pickle"
        kept = AW.prune_checkpoints(d, [600.0, 300.0, 900.0], 2)
        assert kept == [600.0, 900.0]
        assert sorted(os.listdir(d)) == ["anuga_breach_600.0.pickle", "anuga_breach_900.0.pickle"]
    finally:
        shutil.rmtree(d)


@test
def anuga_checkpoint_inflow_functions_pickle_and_match_the_old_lambda():
    t, q = np.array([0.0, 300.0, 900.0]), np.array([0.0, 80.0, 20.0])
    old = lambda x: float(np.interp(x, t, q, right=0.0))        # noqa: E731 - before 3.10.35
    h = pickle.loads(pickle.dumps(AW.Hydrograph(t, q)))
    for x in (-5.0, 0.0, 150.0, 300.0, 899.9, 900.0, 1e6):
        assert h(x) == old(x), x
    assert pickle.loads(pickle.dumps(AW.Constant(0.0)))(123.0) == 0.0
    assert AW.picklable(AW.Hydrograph(t, q)) == ""
    assert AW.picklable(lambda x: x) != "" or AW.pickler() is not pickle   # dill pickles lambdas


# ----------------------------------------------------------------------- worker (fake ANUGA)
@test
def anuga_checkpoint_worker_default_run_has_no_checkpoint_files():
    d = tempfile.mkdtemp()
    try:
        g, stats = _grids(_fake_run(d))
        assert "checkpoint" not in stats
        assert sorted(os.listdir(d)) == ["anuga_job.npz", "anuga_result.npz"]
        assert np.nanmax(g["depth"]) > 1.0
    finally:
        shutil.rmtree(d)


@test
def anuga_checkpoint_worker_results_equal_with_checkpoints_on():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        g0, s0 = _grids(_fake_run(d0))
        g1, s1 = _grids(_fake_run(d1, ckpt_every_steps=5, ckpt_keep=2))
        _same(g0, g1)
        ck = s1["checkpoint"]
        assert ck["checkpoints_saved"] == 7 and ck["last_checkpoint_t_s"] == 1800.0, ck
        assert ck["partial_flushes"] == 7 and ck["checkpoints_removed_after_run"]
        assert not os.path.exists(os.path.join(d1, "anuga_checkpoints"))   # cleaned up
        assert not os.path.exists(os.path.join(d1, AW.PARTIAL_NAME))
        assert {k: v for k, v in s1.items() if k not in ("checkpoint", "runtime_min")} == \
            {k: v for k, v in s0.items() if k != "runtime_min"}
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def anuga_checkpoint_worker_stop_flushes_partial_and_resume_matches():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        g0, _ = _grids(_fake_run(d0))
        assert _fake_run(d1, stop_at=1000.0, ckpt_every_steps=4, partial_every_min=0.0) is None
        ckroot = os.path.join(d1, "anuga_checkpoints")
        (fp,) = os.listdir(ckroot)
        meta = AW.read_meta(os.path.join(ckroot, fp))
        assert meta["times"] == [480.0, 720.0, 960.0][-2:], meta["times"]     # keep = 2
        assert sorted(os.listdir(os.path.join(ckroot, fp))) == [
            "anuga_breach_720.0.pickle", "anuga_breach_960.0.pickle", "checkpoint.json"]
        with np.load(os.path.join(d1, AW.PARTIAL_NAME)) as z:
            info = json.loads(str(z["info"]))
            part = z["depth"].copy()
            assert z["tri_hmax"].shape == (120,)
        assert info["reason"] == "stopped" and info["complete"] is False
        assert 960.0 <= info["t_s"] <= 1020.0 and info["fingerprint"] == fp
        assert np.nanmax(part) > 0 and np.all(part[np.isfinite(part)] <= np.nanmax(g0["depth"]))
        g1, s1 = _grids(_fake_run(d1, ckpt_every_steps=4, resume=1))
        _same(g0, g1)
        ck = s1["checkpoint"]
        assert ck["resumed_from_t_s"] == 960.0 and ck["fingerprint"] == fp, ck
        assert ck["checkpoints_saved"] == 3, ck       # 1200, 1440, 1680 s (yield steps 20, 24, 28)
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def anuga_checkpoint_worker_resume_of_another_job_starts_over():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        _fake_run(d1, stop_at=700.0, ckpt_every_steps=3)
        g0, _ = _grids(_fake_run(d0, sim=1500.0))
        g1, s1 = _grids(_fake_run(d1, sim=1500.0, ckpt_every_steps=3, resume=1))
        _same(g0, g1)
        ck = s1["checkpoint"]
        assert ck["resumed_from_t_s"] is None and "no complete checkpoint" in ck["resume_skipped"]
        # the stopped run's checkpoints (another fingerprint) are left alone
        assert len(os.listdir(os.path.join(d1, "anuga_checkpoints"))) == 1
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def anuga_checkpoint_worker_resume_skips_a_broken_newest_checkpoint():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        g0, _ = _grids(_fake_run(d0))
        _fake_run(d1, stop_at=1000.0, ckpt_every_steps=4)
        ckroot = os.path.join(d1, "anuga_checkpoints")
        ckdir = os.path.join(ckroot, os.listdir(ckroot)[0])
        with open(AW.checkpoint_path(ckdir, 960.0), "wb") as f:
            f.write(b"truncated")                     # killed while ANUGA was writing it
        g1, s1 = _grids(_fake_run(d1, ckpt_every_steps=4, resume=1))
        _same(g0, g1)
        assert s1["checkpoint"]["resumed_from_t_s"] == 720.0
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def anuga_checkpoint_worker_unpicklable_domain_runs_on_without_checkpoints():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    real = AW.picklable
    AW.picklable = lambda obj: "PicklingError: test"
    try:
        g1, s1 = _grids(_fake_run(d1, ckpt_every_steps=2))
    finally:
        AW.picklable = real
    try:
        g0, _ = _grids(_fake_run(d0))
        _same(g0, g1)
        ck = s1["checkpoint"]
        assert ck["checkpoint_error"] == "PicklingError: test" and ck["checkpoints_saved"] == 0
        assert ck["partial_flushes"] == 0
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def anuga_checkpoint_partial_note_names_the_file():
    d = tempfile.mkdtemp()
    try:
        assert AM.partial_note(d) == ""
        AW.savez_atomic(os.path.join(d, AM.PARTIAL_NAME), info=json.dumps(
            {"t_s": 5400.0, "percent": 25.0, "reason": "checkpoint"}))
        n = AM.partial_note(d)
        assert "t = 1.50 h (25.0% of the run, checkpoint)" in n and AM.PARTIAL_NAME in n, n
        assert AM.PARTIAL_NAME == AW.PARTIAL_NAME
    finally:
        shutil.rmtree(d)


@test
def anuga_checkpoint_cancel_sends_sigterm_first_only_when_on():
    """With checkpoints on, Cancel lets the worker write anuga_partial.npz (SIGTERM, then
    SIGKILL after STOP_GRACE_S); off, it is the plain kill as before."""
    if os.name == "nt":
        print("skip: POSIX signals")
        return
    from nd_breach_runner.core.lisflood import Cancelled
    for on in (False, True):
        d = tempfile.mkdtemp()
        try:
            py = os.path.join(d, "fakepy.sh")
            with open(py, "w") as f:
                f.write("#!/bin/sh\ntrap 'echo term > got_term; exit 143' TERM\n"
                        "echo PROGRESS 1 start\nwhile true; do sleep 0.05; done\n")
            os.chmod(py, 0o755)
            st = AM.AnugaSettings(py)
            st.checkpoint_min = 30.0 if on else 0.0
            started = []                   # cancel once the trap is in place
            try:
                AM._launch(st, "j.npz", "r.npz", d, lambda p, m: started.append(p),
                           lambda: bool(started))
                assert False, "expected Cancelled"
            except Cancelled:
                pass
            assert os.path.exists(os.path.join(d, "got_term")) == on, on
        finally:
            shutil.rmtree(d)


# ----------------------------------------------------------------------- real ANUGA (tiny)
def _anuga_python():
    p = os.environ.get("NDBREACH_ANUGA_PYTHON")
    return p if p and os.path.isfile(p) else None


@model_test
def anuga_checkpoint_real_anuga_kill_and_resume_is_bit_identical():
    """589 triangles, 30 simulated minutes, a few seconds: SIGKILL after the 900 s
    checkpoint, resume, and the envelope equals the uninterrupted default run."""
    py = _anuga_python()
    if not py:
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    env = dict(AM._clean_env(), OMP_NUM_THREADS="1")
    try:
        _job(os.path.join(d0, "j.npz"))
        subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d0, env=env, check=True,
                       stdout=subprocess.DEVNULL, timeout=300)
        _job(os.path.join(d1, "j.npz"), ckpt_every_steps=5)
        p = subprocess.Popen([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d1, env=env,
                             stdout=subprocess.PIPE, text=True)
        for ln in p.stdout:
            if "t=1020s" in ln:
                p.send_signal(signal.SIGKILL)
                break
        p.wait()
        assert not os.path.exists(os.path.join(d1, "r.npz"))
        _job(os.path.join(d1, "j.npz"), ckpt_every_steps=5, resume=1)
        subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d1, env=env, check=True,
                       stdout=subprocess.DEVNULL, timeout=300)
        g0, _ = _grids(os.path.join(d0, "r.npz"))
        g1, s1 = _grids(os.path.join(d1, "r.npz"))
        _same(g0, g1)
        assert s1["checkpoint"]["resumed_from_t_s"] == 900.0, s1["checkpoint"]
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("anuga_checkpoint_")]:
        fn()
        print("ok", fn.__name__)
