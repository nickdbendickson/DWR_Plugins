"""Runner 3.10.40: the gmsh-points subprocess of the HEC-RAS 2D area (core/hecras_seeds.py
_mesh_subprocess) times out on the wall clock through core/wallclock.run, as the HEC-RAS
engine and the ANUGA mesher do (#96): elapsed = max(wall, monotonic), then SIGTERM to the
process group, KILL_GRACE_S, SIGKILL. Before, subprocess.run(timeout=) used the monotonic clock
only and killed the interpreter alone, so a child it started kept running.
Tiny shell scripts stand in for the gmsh Python; no gmsh, no HEC-RAS."""
import os
import shutil
import signal
import subprocess
import tempfile
import time

from nd_breach_runner.core import hecras_seeds as hs, wallclock as wc
from nd_breach_runner.tests.registry import test

SPEC = {"what": "a stand-in spec"}


def _script(folder, body):
    p = os.path.join(folder, "fake_python.sh")
    with open(p, "w") as fh:
        fh.write("#!/bin/sh\n" + body)
    os.chmod(p, 0o755)
    return p


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                   # a zombie is not running
        with open("/proc/{}/stat".format(pid)) as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@test
def hecras_seeds_gmsh_subprocess_uses_wallclock_run():
    """The default runner is wallclock.run, with the timeout, KILL_GRACE_S, the clean
    environment and the script's own path; wallclock.Timeout becomes SeedError ('timeout')."""
    seen = {}

    def fake_run(cmd, env=None, timeout=None, grace=None, what="", **kw):
        seen.update(cmd=cmd, env=env, timeout=timeout, grace=grace, what=what)
        raise wc.Timeout(cmd, timeout, output="", stop={"how": "SIGTERM"}, deadline={})

    old = wc.run
    wc.run = fake_run
    try:
        try:
            hs._mesh_subprocess(SPEC, "/some/python", 12.0)
            raise AssertionError("no SeedError")
        except hs.SeedError as e:
            assert "took over 12 s (timeout)" in str(e), e
    finally:
        wc.run = old
    assert seen["timeout"] == 12.0 and seen["grace"] == hs.KILL_GRACE_S == 5.0, seen
    assert seen["cmd"][0] == "/some/python" and seen["cmd"][1].endswith("hecras_seeds.py"), seen
    assert seen["cmd"][2].endswith("seeds_in.json") and seen["cmd"][3].endswith("seeds_out.npz")
    assert "PYTHONPATH" not in seen["env"] and "PYTHONHOME" not in seen["env"]
    assert seen["what"] == "gmsh points"
    # an interpreter that cannot start is still 'could not start'
    try:
        hs._mesh_subprocess(SPEC, "/no/such/python-for-gmsh", 5.0)
        raise AssertionError("no SeedError")
    except hs.SeedError as e:
        assert "could not start" in str(e), e


@test
def hecras_seeds_gmsh_timeout_stops_the_whole_group():
    """A gmsh Python that ignores SIGTERM and has started a child: on the timeout the group
    gets SIGTERM, then SIGKILL after the grace, the child included, and SeedError says timeout
    - within the timeout plus the grace, not when the child ends."""
    if os.name != "posix":
        print("  (skipped: process groups are POSIX)")
        return
    d = tempfile.mkdtemp(prefix="seeds_wc_")
    pidfile = os.path.join(d, "child.pid")
    py = _script(d, "trap '' TERM\nsleep 60 &\necho $! > \"$SEEDS_TEST_PIDFILE\"\nwait\n")
    old_grace, old_env = hs.KILL_GRACE_S, os.environ.get("SEEDS_TEST_PIDFILE")
    hs.KILL_GRACE_S = 0.5
    os.environ["SEEDS_TEST_PIDFILE"] = pidfile
    try:
        t0 = time.time()
        try:
            hs._mesh_subprocess(SPEC, py, 1.0)
            raise AssertionError("no SeedError")
        except hs.SeedError as e:
            assert "took over 1 s (timeout)" in str(e), e
        took = time.time() - t0
        assert took < 15.0, took                      # not the 60 s of the child
        child = int(open(pidfile).read().strip())
        end = time.time() + 5.0
        while _alive(child) and time.time() < end:
            time.sleep(0.05)
        assert not _alive(child), "the gmsh child survived the timeout"
    finally:
        hs.KILL_GRACE_S = old_grace
        if old_env is None:
            os.environ.pop("SEEDS_TEST_PIDFILE", None)
        else:
            os.environ["SEEDS_TEST_PIDFILE"] = old_env
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_seeds_gmsh_failure_and_success_paths_unchanged():
    """A gmsh Python that fails says why (SEEDERROR line, else its last line); one that writes
    the result file is read as before."""
    if os.name != "posix":
        print("  (skipped: shell scripts)")
        return
    d = tempfile.mkdtemp(prefix="seeds_wc_")
    try:
        py = _script(d, "echo 'SEEDERROR gmsh says no'\nexit 3\n")
        try:
            hs._mesh_subprocess(SPEC, py, 30.0)
            raise AssertionError("no SeedError")
        except hs.SeedError as e:
            assert str(e) == "gmsh says no", e
        py = _script(d, "echo 'last words' 1>&2\nexit 1\n")
        try:
            hs._mesh_subprocess(SPEC, py, 30.0)
            raise AssertionError("no SeedError")
        except hs.SeedError as e:
            assert str(e) == "last words", e
        import numpy as np
        src = os.path.join(d, "out.npz")
        np.savez(src, points=np.zeros((3, 2)), triangles=np.zeros((1, 3), int),
                 stats=np.array('{"nodes": 3}'), gmsh_version=np.array("4.13.1"))
        py = _script(d, "cp '{}' \"$3\"\n".format(src))      # args: script, spec, out
        p, t, stats, ver = hs._mesh_subprocess(SPEC, py, 30.0)
        assert p.shape == (3, 2) and t.shape == (1, 3) and stats == {"nodes": 3}, stats
        assert ver == "4.13.1", ver
    finally:
        shutil.rmtree(d, ignore_errors=True)
