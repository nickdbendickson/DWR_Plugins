"""audit 2026-11-20: one driver per dam folder (core/runlock.py), and temp files unique per writer.

workflow.run_dam claims its out_folder (O_EXCL <folder>/.run_claim.lock: pid, host, thread, start time)
before it writes anything; a second driver of the same folder gets runlock.RunClaimError naming the
first. anuga_detach.launch claims the workdir and looks for a live worker again before it deletes the
old status (the busy check and the launch used to be two steps, so two sessions could both start a
worker). workflow._write_json / _write_text, anuga_worker's atomic writers and ckpt_sign.sign write
"<file>.<pid>.<thread>.tmp" (was a fixed "<file>.tmp") and leave no temp behind on failure.
Same module and tests as DamIT (damit_runner/core/runlock.py, engine parity). The processes started
here are `python -c sleep` stand-ins, never a model."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import runlock as RL  # noqa: E402
from nd_breach_runner.core import anuga_detach as AD  # noqa: E402
from nd_breach_runner.core import ckpt_sign as CS  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


def _sleeper(seconds=30):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep({})".format(seconds)])


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _plant(folder, name=RL.LOCK_NAME, **info):
    os.makedirs(folder, exist_ok=True)
    d = {"pid": 1, "host": socket.gethostname(), "thread": 1, "what": "planted", "started": "now",
         "started_epoch": time.time(), "token": "x"}
    d.update(info)
    with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
        json.dump(d, fh)


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as e:                          # noqa: BLE001
            out["error"] = e
    t = threading.Thread(target=run)
    t.start()
    t.join(30)
    return out


@test
def runlock_claim_nest_refuse_release():
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "dam A")
        with RL.claim(f, "outer"):
            info = RL.read_claim(f)
            assert (info["pid"], info["host"], info["what"]) == (os.getpid(), socket.gethostname(), "outer")
            with RL.claim(f, "inner"):
                assert RL.read_claim(f)["what"] == "outer"
            out = _in_thread(lambda: RL.acquire(f, "other thread"))
            assert isinstance(out.get("error"), RL.RunClaimError), out
        assert RL.read_claim(f) is None
        p = _sleeper()
        try:
            _plant(f, pid=p.pid, what="QGIS session 2")
            try:
                RL.acquire(f, "me")
                raise AssertionError("expected RunClaimError")
            except RL.RunClaimError as e:
                assert str(p.pid) in str(e) and "QGIS session 2" in str(e), e
        finally:
            p.kill()
            p.wait()
        RL.release(RL.acquire(f, "after it died"))


@test
def runlock_stale_takeover_and_foreign_claims():
    with tempfile.TemporaryDirectory() as f:
        _plant(f, pid=_dead_pid())
        RL.release(RL.acquire(f, "a"))
        _plant(f, pid=os.getpid(), host="another-computer", started_epoch=time.time() - RL.STALE_S - 60)
        RL.release(RL.acquire(f, "b"))
        _plant(f, pid=os.getpid(), host="another-computer")
        try:
            RL.acquire(f, "c")
            raise AssertionError("a fresh claim from another computer was taken over")
        except RL.RunClaimError:
            pass
        os.remove(RL.lock_path(f))
        path = RL.acquire(f, "mine")
        _plant(f, pid=os.getpid() + 1, token="theirs")
        RL.release(path)
        assert RL.read_claim(f)["token"] == "theirs"          # never removes another's claim


@test
def runlock_threads_race_one_wins():
    with tempfile.TemporaryDirectory() as f:
        barrier = threading.Barrier(8)
        wins, errors = [], []

        def go():
            barrier.wait()
            try:
                RL.acquire(f, "racer")
                wins.append(1)
                time.sleep(0.2)
            except RL.RunClaimError as e:
                errors.append(e)
        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        assert (len(wins), len(errors)) == (1, 7), (wins, errors)
        RL._held.clear()


@test
def runlock_run_dam_claims_before_it_runs():
    from nd_breach_runner.core import workflow as WF
    with tempfile.TemporaryDirectory() as f:
        p = _sleeper()
        try:
            _plant(f, pid=p.pid, what="the other driver")
            with mock.patch.object(WF, "_run_dam_claimed", side_effect=AssertionError("ran without a claim")):
                try:
                    WF.run_dam(None, (0, 0), None, None, None, f)
                    raise AssertionError("expected RunClaimError")
                except RL.RunClaimError as e:
                    assert "the other driver" in str(e)
        finally:
            p.kill()
            p.wait()
        os.remove(RL.lock_path(f))
        with mock.patch.object(WF, "_run_dam_claimed", return_value="ok"):
            assert WF.run_dam("dam", (0, 0), "st", "src", "w", f) == "ok"
        assert RL.read_claim(f) is None


@test
def anuga_launch_claims_and_looks_again():
    with tempfile.TemporaryDirectory() as w:
        script = os.path.join(w, "fake_anuga_worker.py")
        with open(script, "w") as fh:
            fh.write("import time\ntime.sleep(30)\n")
        cmd = [sys.executable, "-u", script, "job.json", "result.npz"]
        proc, _, spath = AD.launch(cmd, w, dict(os.environ), script=script)
        try:
            assert AD.read_status(spath)["pid"] == proc.pid
            assert not os.path.exists(os.path.join(w, AD.LAUNCH_CLAIM))
            try:
                p2, _, _ = AD.launch(cmd, w, dict(os.environ), script=script)
                p2.kill()
                raise AssertionError("a second worker was started over a running one")
            except RL.RunClaimError as e:
                assert str(proc.pid) in str(e)
            assert AD.read_status(spath)["pid"] == proc.pid
        finally:
            proc.kill()
            proc.wait()
        other = _sleeper()
        try:
            _plant(w, name=AD.LAUNCH_CLAIM, pid=other.pid, what="ANUGA worker launch")
            try:
                AD.launch(cmd, w, dict(os.environ), script=script)
                raise AssertionError("launched while another session was launching")
            except RL.RunClaimError:
                pass
        finally:
            other.kill()
            other.wait()


@test
def atomic_writers_use_unique_temps():
    from nd_breach_runner.core import workflow as WF
    from nd_breach_runner.core import anuga_worker as AW
    with tempfile.TemporaryDirectory() as d:
        mine = ".{}.{}.tmp".format(os.getpid(), threading.get_ident())
        assert WF._tmp_name(os.path.join(d, "a.json")).endswith(mine)
        assert AW._tmp_name(os.path.join(d, "a.json")).endswith(mine)
        p = os.path.join(d, "run_report.json")
        WF._write_json(p, {"n": 1})
        AW.write_json_atomic(os.path.join(d, "s.json"), {"n": 1})
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            for fn in (lambda: WF._write_json(p, {"n": 2}), lambda: WF._write_text(p, "x"),
                       lambda: AW.write_json_atomic(p, {"n": 2})):
                try:
                    fn()
                    raise AssertionError("no error")
                except OSError:
                    pass
        with open(p, encoding="utf-8") as fh:
            assert json.load(fh) == {"n": 1}
        ck = os.path.join(d, "domain_1.0.pickle")
        with open(ck, "wb") as fh:
            fh.write(b"x" * 10)
        assert CS.sign(ck) == ""
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            assert "disk full" in CS.sign(ck)
        left = [f for f in os.listdir(d) if ".tmp" in f]
        assert left == [], left
