"""Runner 3.10.75: the Linux RasUnsteady child gets a raised stack limit (RLIMIT_STACK).

DWR-2801 NickReview (2026-10-24, Runner 3.10.68, 421,317 cells): RasUnsteady exit 174 =
SIGSEGV in GridGeometry.f90:151 ("Reading 2D Area(s)") under the default 8 MB stack; by hand
with `ulimit -s unlimited` it read the 2D area. compute() now sets the soft limit to unlimited
(or the hard limit) in the child before exec. Then (02:12Z) RasUnsteady died again at
PROGRESS 0.50 in the Intel OpenMP worker threads (4 MB stacks, not covered by ulimit -s):
KMP_STACKSIZE / OMP_STACKSIZE now default to 1g in its env (and in the Wine env and the
Windows launcher), a value the user set is kept. Mocked Popen plus tiny real /bin/sh
children; no HEC-RAS, no Wine."""
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
                    "echo \"OMP=$KMP_STACKSIZE,$OMP_STACKSIZE\"\n"
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
        assert "OMP=1g,1g" in txt, txt                 # through nice, as a wrapper would
        line = [m for m in logs if "stack limit for RasUnsteady" in m]
        assert line and "(was 8 MB" in line[0], logs
    finally:
        shutil.rmtree(t, ignore_errors=True)


# ------------------------------------------------------------ OpenMP thread stacks ------
@test
def hecras_omp_stack_defaults_to_1g_and_keeps_the_users_value():
    put, info = he.omp_stack_env({})
    assert put == {"KMP_STACKSIZE": "1g", "OMP_STACKSIZE": "1g"}, put
    assert info["KMP_STACKSIZE_from"] == "default"
    put, info = he.omp_stack_env({"KMP_STACKSIZE": "2G"})
    assert put == {"OMP_STACKSIZE": "1g"} and info["KMP_STACKSIZE"] == "2G", (put, info)
    assert info["KMP_STACKSIZE_from"] == "environment"
    put, info = he.omp_stack_env({he.ENV_OMP_STACK: "512M", "OMP_STACKSIZE": "64m"})
    assert put == {"KMP_STACKSIZE": "512m"} and info["OMP_STACKSIZE"] == "64m", (put, info)
    put, info = he.omp_stack_env({he.ENV_OMP_STACK: "off"})
    assert put == {} and info["KMP_STACKSIZE"] is None, info
    put, info = he.omp_stack_env({he.ENV_OMP_STACK: "lots"})
    assert put["KMP_STACKSIZE"] == "1g" and "not N[b|k|m|g]" in info["note"], info
    assert "KMP_STACKSIZE=1g (default) OMP_STACKSIZE=1g (default)" == he.omp_stack_text(
        he.omp_stack_env({})[1])


@test
def hecras_compute_passes_the_omp_stacks_to_rasunsteady():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    real = he.subprocess.Popen
    he.subprocess.Popen = RecPopen
    try:
        _project(t)
        d = {"linux_dir": t, "rasunsteady": os.path.join(t, "RasUnsteady")}
        for env, kmp, omp in (({}, "1g", "1g"),
                              ({"KMP_STACKSIZE": "3g", "PATH": "/bin"}, "3g", "1g")):
            RecPopen.seen = []
            logs, lim = [], {}
            he.compute(t, "M", d=d, env=env, log=logs.append, limit=lim)
            (cmd, kw), = RecPopen.seen
            assert kw["env"]["KMP_STACKSIZE"] == kmp and kw["env"]["OMP_STACKSIZE"] == omp, kw
            assert lim["omp_stacksize"]["KMP_STACKSIZE"] == kmp, lim
            assert "stack_limit" in lim, lim
            line = [m for m in logs if "OpenMP thread stacks for RasUnsteady" in m]
            assert line and "KMP_STACKSIZE={}".format(kmp) in line[0], logs
            assert "KMP_STACKSIZE" not in env or env["KMP_STACKSIZE"] == "3g"   # not mutated
    finally:
        he.subprocess.Popen = real
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_omp_stacks_reach_wine_and_the_windows_launcher():
    # Wine passes the Unix environment to the Windows programs it starts (Ras.exe -c and the
    # RasUnsteady.exe it starts); wine_env drops only the GDAL/PROJ/Python/LD variables
    t = tempfile.mkdtemp()
    try:
        d = {"prefix": t, "windows_dir": os.path.join(t, "drive_c", "HEC", "RAS66")}
        e = he.wine_env(d, {"PATH": "/usr/bin"})
        assert e["KMP_STACKSIZE"] == "1g" and e["OMP_STACKSIZE"] == "1g", e
        assert e["WINEPREFIX"] == t
        e = he.wine_env(d, {"OMP_STACKSIZE": "256m"})
        assert e["OMP_STACKSIZE"] == "256m" and e["KMP_STACKSIZE"] == "1g", e
        # the Wine Ras.exe -c launch: the env handed to Popen carries them
        from nd_breach_runner.core import hecras_windows as hw
        seen = []

        class P(RecPopen):
            def __init__(self, cmd, **kw):
                seen.append(kw.get("env"))
                raise OSError("stop here")
        real = he.subprocess.Popen
        he.subprocess.Popen = P
        try:
            open(os.path.join(t, "M.p01"), "w").close()
            he.ras_compile(t, "M", "01", dict(d, wine="wine", ras_exe=os.path.join(
                d["windows_dir"], "Ras.exe")), he.wine_env(d, {}), None, until=lambda: True,
                what="x")
        except OSError:
            pass
        finally:
            he.subprocess.Popen = real
        assert seen and seen[0]["KMP_STACKSIZE"] == "1g", seen
        # the Windows launcher's runner (real child; a POSIX shell stands in for RasUnsteady)
        if os.name == "posix" and shutil.which("sh"):
            ce = hw.child_env({"PATH": os.environ.get("PATH", "/bin"), "KMP_STACKSIZE": "2g"})
            assert ce["KMP_STACKSIZE"] == "2g" and ce["OMP_STACKSIZE"] == "1g", ce
            old = {k: os.environ.pop(k, None) for k in he.OMP_STACK_VARS + (he.ENV_OMP_STACK,)}
            try:
                r = hw._popen_run(["sh", "-c", "echo K=$KMP_STACKSIZE O=$OMP_STACKSIZE"],
                                  cwd=t, timeout=30)
            finally:
                for k, v in old.items():
                    if v is not None:
                        os.environ[k] = v
            assert "K=1g O=1g" in r.stdout, r.stdout
    finally:
        shutil.rmtree(t, ignore_errors=True)
