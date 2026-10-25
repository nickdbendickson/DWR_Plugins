"""Runner 3.10.75: the Linux RasUnsteady child gets a raised stack limit (RLIMIT_STACK).

DWR-2801 NickReview (2026-10-24, Runner 3.10.68, 421,317 cells): RasUnsteady exit 174 =
SIGSEGV in GridGeometry.f90:151 ("Reading 2D Area(s)") under the default 8 MB stack; by hand
with `ulimit -s unlimited` it read the 2D area. compute() now sets the soft limit to unlimited
(or the hard limit) in the child before exec. Mocked Popen plus one tiny real /bin/sh child;
no HEC-RAS, no Wine."""
import os
import shutil
import tempfile

from nd_breach_runner.core import hecras_engine as he
from nd_breach_runner.tests.registry import test


class FakeResource:
    """resource stand-in: records setrlimit calls; refuse_unlimited makes RLIM_INFINITY fail
    (an unprivileged process whose hard limit is finite)."""
    RLIMIT_STACK = 3
    RLIM_INFINITY = -1

    def __init__(self, soft, hard, refuse_unlimited=False):
        self.soft, self.hard, self.refuse = soft, hard, refuse_unlimited
        self.calls = []

    def getrlimit(self, which):
        assert which == self.RLIMIT_STACK
        return self.soft, self.hard

    def setrlimit(self, which, lim):
        self.calls.append(lim)
        if self.refuse and lim[0] == self.RLIM_INFINITY and self.hard != self.RLIM_INFINITY:
            raise ValueError("not allowed to raise maximum limit")
        self.soft = lim[0]


def _with_resource(fake, fn):
    real = he.resource
    he.resource = fake
    try:
        return fn()
    finally:
        he.resource = real


@test
def hecras_stack_limit_unlimited_when_the_hard_limit_allows():
    f = FakeResource(8 << 20, FakeResource.RLIM_INFINITY)
    _with_resource(f, he.raise_stack_limit)
    assert f.calls == [(f.RLIM_INFINITY, f.RLIM_INFINITY)] and f.soft == f.RLIM_INFINITY, f.calls
    target, text = _with_resource(f.__class__(8 << 20, -1), he.stack_limit_plan)
    assert target == -1 and text.startswith("unlimited (was 8 MB"), text


@test
def hecras_stack_limit_falls_back_to_the_hard_limit():
    hard = 64 << 20
    f = FakeResource(8 << 20, hard, refuse_unlimited=True)
    _with_resource(f, he.raise_stack_limit)
    assert f.calls == [(f.RLIM_INFINITY, hard), (hard, hard)] and f.soft == hard, f.calls
    target, text = _with_resource(FakeResource(8 << 20, hard), he.stack_limit_plan)
    assert target == hard and text == "64 MB (was 8 MB; hard limit 64 MB)", text


@test
def hecras_stack_limit_without_resource_is_a_no_op():
    # Windows (no resource module): nothing set, compute passes no preexec_fn
    assert _with_resource(None, he.raise_stack_limit) is None
    target, text = _with_resource(None, he.stack_limit_plan)
    assert target is None and "no RLIMIT_STACK" in text, text


def _project(t):
    with open(os.path.join(t, "M.p01"), "w") as f:
        f.write("Plan Title=t\nGeom File=g01\nSimulation Date=01JAN2000,0000,01JAN2000,0200\n"
                "Computation Interval=3SEC\n")
    with open(os.path.join(t, "M.g01"), "w") as f:
        f.write("Storage Area=D\nStorage Area Is2D=-1\nStorage Area 2D Points= 100\n")
    for x in ("b01", "x01", "p01.tmp.hdf"):
        open(os.path.join(t, "M." + x), "w").close()


class RecPopen:
    """Popen stand-in: records its kwargs, writes the 'Finished' line, exits 0."""
    seen = []

    def __init__(self, cmd, **kw):
        RecPopen.seen.append((cmd, kw))
        kw["stdout"].write(" Finished Unsteady Flow Simulation\n")
        kw["stdout"].flush()
        self.returncode, self.pid = 0, 4242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


@test
def hecras_compute_passes_the_stack_preexec_fn_to_rasunsteady():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    real = he.subprocess.Popen
    RecPopen.seen = []
    he.subprocess.Popen = RecPopen
    try:
        _project(t)
        logs, lim = [], {}
        he.compute(t, "M", d={"linux_dir": t, "rasunsteady": os.path.join(t, "RasUnsteady")},
                   env={}, log=logs.append, limit=lim)
        (cmd, kw), = RecPopen.seen
        assert kw.get("preexec_fn") is he.raise_stack_limit, kw
        assert kw.get("start_new_session") is True, kw
        assert any(m.startswith("HEC-RAS compute: stack limit for RasUnsteady ") for m in logs), logs
        assert lim["stack_limit"] and lim["limit"] == "none", lim
    finally:
        he.subprocess.Popen = real
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_compute_child_sees_the_raised_stack_limit():
    # a real child (nice -> /bin/sh, as a wrapper script would be) reports `ulimit -s`; the
    # parent's soft limit is lowered to 8 MB for the test so the raise is visible
    if os.name != "posix" or he.resource is None:
        return
    R = he.resource
    soft0, hard = R.getrlimit(R.RLIMIT_STACK)
    eight = 8 << 20
    if hard != R.RLIM_INFINITY and hard <= eight:
        return                              # nothing to raise to on this machine
    t = tempfile.mkdtemp()
    try:
        _project(t)
        exe = os.path.join(t, "RasUnsteady")
        with open(exe, "w") as f:
            f.write("#!/bin/sh\necho \"STACK=$(ulimit -s)\"\n"
                    "echo ' Finished Unsteady Flow Simulation'\n")
        os.chmod(exe, 0o755)
        R.setrlimit(R.RLIMIT_STACK, (eight, hard))
        logs = []
        try:
            he.compute(t, "M", d={"linux_dir": t, "rasunsteady": exe}, env={}, log=logs.append)
        finally:
            R.setrlimit(R.RLIMIT_STACK, (soft0, hard))
        txt = open(os.path.join(t, "linux_run", "run.log")).read()
        want = "unlimited" if hard == R.RLIM_INFINITY else str(hard // 1024)
        assert "STACK={}".format(want) in txt, (txt, want)
        assert "STACK=8192" not in txt, txt
        line = [m for m in logs if "stack limit for RasUnsteady" in m]
        assert line and "(was 8 MB" in line[0], logs
    finally:
        shutil.rmtree(t, ignore_errors=True)
