"""3.12.2 (DWR-2801): the ANUGA worker runs detached from QGIS (core/anuga_detach.py).

The GUI run died twice when desktop :2 was shut down under QGIS: the worker's stdout was a pipe
QGIS read, so its next PROGRESS print broke and the run ended. Now the worker writes its own
anuga.log, starts in its own session, ignores SIGPIPE / SIGHUP, keeps anuga_worker.json, and a
later session adopts it (anuga_model.adoption / run(run_key=...)) or a finished result.

All with a fake worker (a few lines of Python that print PROGRESS and write a dummy npz): no
ANUGA, no model. POSIX only where signals and sessions are tested."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from nd_breach_runner.core import anuga_detach as D  # noqa: E402
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import run_params as RP  # noqa: E402
from nd_breach_runner.core.lisflood import Cancelled, ModelError  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402

CORE = os.path.dirname(os.path.abspath(D.__file__))

# The fake worker: imports anuga_detach the way core/anuga_worker.py does when run as a script,
# prints PROGRESS, waits for <workdir>/go (FAKE_WAIT=1), then writes a dummy result. With the
# job file of anuga_model.run it writes grids of the job's output size.
FAKE = textwrap.dedent('''\
    import json, os, sys, time
    sys.path.insert(0, {core!r})
    import numpy as np
    import anuga_detach

    def main(job, out):
        print("PROGRESS 1 start")
        print("to stderr", file=sys.stderr)
        partial = os.path.join(os.path.dirname(os.path.abspath(out)), "anuga_partial.npz")
        try:
            t0 = time.time()
            while os.environ.get("FAKE_WAIT") == "1" and not os.path.exists("go"):
                print("PROGRESS 2 waiting")
                time.sleep(0.05)
                if time.time() - t0 > 60:
                    raise RuntimeError("no go file")
            for i in range(20):                       # lines after QGIS died / the pipe broke
                print("PROGRESS {{}} step {{}}".format(10 + 4 * i, i))
            ny = nx = 4
            if os.path.exists(job):
                with np.load(job, allow_pickle=True) as j:
                    if "out_ny" in j.files:
                        ny, nx = int(j["out_ny"]), int(j["out_nx"])
            z = np.zeros((ny, nx))
            stats = {{"triangles": 10, "max_edge_depth_m": 0.0, "fake": True}}
            anuga_detach.write_json_atomic(out + ".info", {{"pid": os.getpid()}})
            np.savez(out + ".tmp.npz", depth=z, velocity=z, arrival_hr=z + np.nan, wse=z,
                     stats=json.dumps(stats))
            os.replace(out + ".tmp.npz", out)
            print("PROGRESS 100 done")
        finally:
            if not os.path.exists(out):               # stopped: the partial flush still runs
                open(partial, "wb").close()

    sys.exit(anuga_detach.worker_entry(main, sys.argv[1], sys.argv[2]))
''')

# "QGIS": starts the worker through anuga_detach.launch, prints its pid, then waits to be killed
PARENT = textwrap.dedent('''\
    import json, os, sys, time
    sys.path.insert(0, {core!r})
    import anuga_detach
    cmd = [sys.executable, "-u", {fake!r}, "job.npz", "anuga_result.npz"]
    proc, log, spath = anuga_detach.launch(cmd, {wd!r}, dict(os.environ), tag={tag!r},
                                           script={fake!r})
    print(proc.pid, flush=True)
    time.sleep(120)
''')

POSIX = os.name != "nt"


def _fake(d):
    p = os.path.join(d, "fake_worker.py")
    with open(p, "w") as f:
        f.write(FAKE.format(core=CORE))
    return p


def _env(wait=True, **kw):
    env = dict(os.environ, OMP_NUM_THREADS="1", PYTHONPATH="")
    env["FAKE_WAIT"] = "1" if wait else "0"
    env.update(kw)
    return env


def _until(cond, timeout=30.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)


def _go(wd):
    open(os.path.join(wd, "go"), "w").close()


def _orphan(d, wd, tag=None):
    """Start the fake worker from a 'QGIS' process, then SIGKILL that process. Returns the
    worker's pid (the worker now has no parent session)."""
    fake = _fake(d)
    par = os.path.join(d, "parent.py")
    with open(par, "w") as f:
        f.write(PARENT.format(core=CORE, fake=fake, wd=wd, tag=tag or {}))
    p = subprocess.Popen([sys.executable, par], stdout=subprocess.PIPE, env=_env(),
                         text=True)
    pid = int(p.stdout.readline())
    _until(lambda: (D.read_status(wd) or {}).get("written_by") == "worker", what="worker")
    if POSIX:
        assert os.getsid(pid) == pid != os.getsid(p.pid), "worker not in its own session"
    p.kill()                                      # QGIS dies (desktop shut down)
    p.wait()
    p.stdout.close()
    return pid


@test
def anuga_detach_worker_writes_its_own_log_and_status():
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        fake = _fake(d)
        cmd = [sys.executable, "-u", fake, "job.npz", "anuga_result.npz"]
        proc, log, spath = D.launch(cmd, d, _env(), tag={"run_key": "k1"}, script=fake)
        assert log == os.path.join(d, "anuga.log") and spath == os.path.join(d, D.STATUS_NAME)
        if POSIX:
            assert os.getsid(proc.pid) == proc.pid != os.getsid(0)
        _until(lambda: "PROGRESS 2" in open(log).read(), what="worker's own log lines")
        st = D.worker_state(d)
        assert st["state"] == D.RUNNING and st["status"]["pid"] == proc.pid, st
        assert st["status"]["tag"] == {"run_key": "k1"} and D.pending(d)
        _go(d)
        assert proc.wait(30) == 0
        txt = open(log).read()
        assert "to stderr" in txt and "PROGRESS 100 done" in txt, txt[-300:]
        assert D.last_progress(log) == (100.0, "done")
        st = D.read_status(d)
        assert st["state"] == D.FINISHED and st["written_by"] == "worker", st
        assert st["returncode"] == 0 and st["ended_utc"]
        assert D.worker_state(d)["state"] == D.FINISHED and D.pending(d)
        D.mark_processed(d)
        assert D.pending(d) is None and D.worker_state(d)["processed"]
        assert D.parse_progress("PROGRESS 12.5 mesh 9 triangles\n") == (12.5, "mesh 9 triangles")
        assert D.parse_progress("PROGRESS x") is None and D.parse_progress("hello") is None
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_worker_survives_qgis_being_killed():
    if not POSIX:
        print("skip: POSIX sessions / signals")
        return
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        pid = _orphan(d, d, tag={"run_key": "k"})
        assert D.is_worker(D.read_status(d)), "the worker died with its parent"
        os.kill(pid, signal.SIGHUP)               # the session's hang-up: ignored
        time.sleep(0.2)
        assert D.worker_state(d)["state"] == D.RUNNING
        _go(d)
        _until(lambda: D.worker_state(d)["state"] == D.FINISHED, what="finished")
        assert os.path.isfile(os.path.join(d, "anuga_result.npz"))
        assert "PROGRESS 100 done" in open(os.path.join(d, "anuga.log")).read()
        w = D.pending(d)
        assert w and w["have_result"] and not w["processed"], w
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_worker_survives_a_broken_stdout_pipe():
    """The 3.12.1 situation: stdout is a pipe whose reader (QGIS) went away. The run goes on and
    writes its result; SIGPIPE is ignored and BrokenPipeError is swallowed."""
    if not POSIX:
        print("skip: POSIX pipes")
        return
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        fake = _fake(d)
        p = subprocess.Popen([sys.executable, "-u", fake, "job.npz", "anuga_result.npz"],
                             cwd=d, env=_env(), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, preexec_fn=lambda: signal.signal(
                                 signal.SIGPIPE, signal.SIG_DFL))
        assert p.stdout.readline().startswith(b"PROGRESS 1")
        p.stdout.close()                          # the reader is gone
        _go(d)
        assert p.wait(30) == 0, p.returncode
        assert os.path.isfile(os.path.join(d, "anuga_result.npz"))
        st = D.read_status(d)
        assert st["state"] == D.FINISHED and st["returncode"] == 0, st
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_sigterm_still_flushes_the_partial():
    if not POSIX:
        print("skip: POSIX signals")
        return
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        fake = _fake(d)
        proc, log, spath = D.launch([sys.executable, "-u", fake, "job.npz", "anuga_result.npz"],
                                    d, _env(), script=fake)
        _until(lambda: "PROGRESS 2" in open(log).read(), what="waiting")
        proc.terminate()
        assert proc.wait(30) == 128 + signal.SIGTERM
        st = D.read_status(d)
        assert st["state"] == D.STOPPED and st["partial"].endswith("anuga_partial.npz"), st
        assert os.path.isfile(st["partial"]) and D.pending(d) is None
    finally:
        shutil.rmtree(d)


class _Worker:
    """anuga_model._launch / run with the fake worker as WORKER and this Python."""

    def __init__(self, d):
        self.fake = _fake(d)

    def __enter__(self):
        self.old = AM.WORKER
        self.env = {k: os.environ.get(k) for k in ("FAKE_WAIT", AM.TIMEOUT_ENV,
                                                   "OMP_NUM_THREADS")}
        AM.WORKER = self.fake
        os.environ["OMP_NUM_THREADS"] = "1"
        return self

    def __exit__(self, *a):
        AM.WORKER = self.old
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def anuga_detach_launch_progress_cancel_and_timeout():
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        with _Worker(d):
            st = AM.AnugaSettings(sys.executable, threads=1)
            os.environ["FAKE_WAIT"] = "0"
            got = []
            proc, log = AM._launch(st, "job.npz", "r.npz", d, lambda p, m: got.append((p, m)),
                                   None, tag={"run_key": "a"})
            assert proc.returncode == 0 and os.path.isfile(os.path.join(d, "r.npz"))
            assert got[0] == (1.0, "ANUGA start") and got[-1] == (100.0, "ANUGA done"), got
            assert D.read_status(d)["state"] == D.FINISHED
            # Cancel while the worker waits: stopped, Cancelled as before
            os.environ["FAKE_WAIT"] = "1"
            seen = []
            try:
                AM._launch(st, "job.npz", "r2.npz", d, lambda p, m: seen.append(p),
                           lambda: bool(seen))
                assert False, "expected Cancelled"
            except Cancelled:
                pass
            assert D.read_status(d)["state"] in (D.STOPPED, D.FAILED)
            assert not os.path.exists(os.path.join(d, "r2.npz"))
            # the optional wall-clock limit (none by default)
            assert AM.timeout_s(st) is None
            os.environ[AM.TIMEOUT_ENV] = "0.5"
            assert AM.timeout_s(st) == 0.5
            try:
                AM._launch(st, "job.npz", "r3.npz", d, None, None)
                assert False, "expected the time limit"
            except ModelError as e:
                assert "limit" in str(e), e
            assert not D.is_worker(D.read_status(d))
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_adoption_waits_for_or_uses_a_left_worker():
    if not POSIX:
        print("skip: POSIX sessions / signals")
        return
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        res = os.path.join(d, "anuga_result.npz")
        _orphan(d, d, tag={"run_key": "K", "buffer_m": 123.0})
        assert AM.adoption(d, res, "K")[0] == "wait"
        assert AM.adoption(d, res, "other")[0] == "busy"
        assert AM.adoption(d, res, None)[0] == "busy"
        act, w = AM.adoption(d, res, "K")
        got = []
        st = AM.AnugaSettings(sys.executable, threads=1)
        _go(d)
        proc, log = AM._attach(w, st, lambda p, m: got.append(p), None)
        assert proc.returncode == 0 and got and got[-1] == 100.0, (proc.returncode, got)
        assert AM.adoption(d, res, "K")[0] == "use"
        assert AM.adoption(d, res, "other")[0] is None
        D.mark_processed(d)
        assert AM.adoption(d, res, "K")[0] is None          # built already: a fresh run
        assert AM.adoption(d, res, "K", reuse=True)[0] == "use"   # unless Resume asks for it
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_status_died_and_stale_pid():
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        D.write_json_atomic(D.status_path(d), {"state": "running", "pid": p.pid,
                                              "script": "anuga_worker.py",
                                              "result": os.path.join(d, "r.npz"),
                                              "started_epoch": time.time() - 5})
        assert D.worker_state(d)["state"] == D.DIED and D.pending(d) is None
        np.savez(os.path.join(d, "r.npz"), a=1)      # died after writing its result
        assert D.worker_state(d)["state"] == D.FINISHED
        # a live pid that is not the worker (a recycled pid): not running
        D.update_status(D.status_path(d), pid=os.getpid(), script="no_such_worker_script.py")
        if os.path.isdir("/proc"):
            assert not D.is_worker(D.read_status(d))
        assert D.worker_state(os.path.join(d, "none"))["state"] == D.NONE
        assert not D.create_status(D.status_path(d), x=1)    # never over the worker's record
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_finish_builds_products_from_a_finished_npz():
    """QGIS is killed while ANUGA runs inside anuga_model.run; the worker finishes alone. The
    next run with the same settings (run_key) starts no worker: it builds the envelopes from the
    finished anuga_result.npz (the existing post-processing) and marks it processed."""
    if not POSIX:
        print("skip: POSIX sessions / signals")
        return
    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        wd = os.path.join(d, "anuga")
        fake = _fake(d)
        qgis = os.path.join(d, "qgis.py")
        with open(qgis, "w") as f:
            f.write(textwrap.dedent('''\
                import sys
                sys.path.insert(0, {root!r})
                from nd_breach_runner.core import anuga_model as AM
                from nd_breach_runner.tests.test_gmsh_mesher import _case
                AM.WORKER = {fake!r}
                dem, flooded, inflow, hy, st, roads = _case()
                st.threads = 1
                AM.run({wd!r}, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st,
                       refine_lines=roads, run_key="KEY")
            ''').format(root=ROOT, fake=fake, wd=wd))
        p = subprocess.Popen([sys.executable, qgis], env=_env(), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        try:
            _until(lambda: (D.read_status(wd) or {}).get("written_by") == "worker", 120,
                   "the worker")
        finally:
            p.kill()
            p.wait()
        pid = D.read_status(wd)["pid"]
        _go(wd)
        _until(lambda: D.worker_state(wd)["state"] == D.FINISHED, what="finished")
        from nd_breach_runner.tests.test_gmsh_mesher import _case
        dem, flooded, inflow, hy, st, roads = _case()
        st.threads = 1
        logs = []
        with _Worker(d):
            os.environ["FAKE_WAIT"] = "1"         # a new worker would hang: none may start
            os.environ[AM.TIMEOUT_ENV] = "20"
            env, _, _ = AM.run(wd, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st,
                               refine_lines=roads, run_key="KEY", log=logs.append)
        assert env.depth.data.shape[0] > 0 and env.stats.get("fake"), env.stats
        assert any("using the result of the worker" in m for m in logs), logs
        s = D.read_status(wd)
        assert s["pid"] == pid and s.get("processed_utc"), s
        assert D.pending(wd) is None
    finally:
        shutil.rmtree(d)


@test
def anuga_detach_run_params_round_trip():
    class Prop:
        def staticValue(self):
            return "layer_id_1"

    class SrcDef:                                 # QgsProcessingFeatureSourceDefinition
        source = Prop()

    class Layer:
        def source(self):
            return "/data/dams.gpkg|layername=dams"

    d = tempfile.mkdtemp(prefix="anudet_")
    try:
        layers = {"layer_id_1": "/data/dams.gpkg|layername=dams", "dem_id": "/data/dem.tif"}
        p = RP.save(d, "ndbreach:rundam",
                    {"DAMS": SrcDef(), "DEM": "dem_id", "BUILDINGS": Layer(), "DAM_NAME": "dem_id",
                     "RESUME": False, "SCENARIO": 2, "BUFFER": 1.5, "LIST": ["dem_id", None]},
                    resolve=layers.get, layer_params={"DAMS", "DEM", "LIST"})
        assert p == RP.path(d) and p.endswith(os.path.join("_breach_model", "run_params.json"))
        alg, params = RP.load(d)
        assert alg == "ndbreach:rundam", alg
        assert params == {"DAMS": "/data/dams.gpkg|layername=dams", "DEM": "/data/dem.tif",
                          "BUILDINGS": "/data/dams.gpkg|layername=dams",
                          "DAM_NAME": "dem_id",          # not a layer parameter: kept as text
                          "SCENARIO": 2, "BUFFER": 1.5,
                          "LIST": ["/data/dem.tif", None]}, params
        assert RP.load(os.path.join(d, "x")) == (None, None)
        assert RP.pending_workers(d) == []
        wd = os.path.join(d, "_breach_model", "anuga")
        os.makedirs(wd)
        np.savez(os.path.join(wd, "anuga_result.npz"), a=1)
        D.write_json_atomic(D.status_path(wd), {"state": "finished", "pid": 1,
                                               "result": os.path.join(wd, "anuga_result.npz")})
        assert [s for s, _w in RP.pending_workers(d)] == ["anuga"]
        from nd_breach_runner import popup_logic as pl
        row = pl.anuga_worker_row(d)
        assert row and row[0] == "anuga_worker" and row[2] == "part", row
        assert pl.suggest([row], "")[0] == "finishanuga"
        assert pl.tool_status("finishanuga", [row]) == "part"
        D.mark_processed(wd)
        assert pl.anuga_worker_row(d) is None
    finally:
        shutil.rmtree(d)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("anuga_detach_")]:
        fn()
        print("ok", fn.__name__)
