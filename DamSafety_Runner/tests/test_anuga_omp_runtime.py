"""3.10.50: the OpenMP runtime's own thread count (core/omp_probe, anuga_worker.runtime_note,
the run report's anuga_reported) and ANUGA interpreters in a Windows conda layout
(core/nd_data.anuga_python_candidates). No ANUGA, no solver."""
import os
import shutil
import subprocess
import sys
import tempfile

from nd_breach_runner.core import anuga_model as AM
from nd_breach_runner.core import anuga_worker as AW
from nd_breach_runner.core import nd_data
from nd_breach_runner.core import omp_probe
from nd_breach_runner.tests.registry import test


@test
def omp_probe_returns_int_or_none_and_never_raises():
    n, name = omp_probe.omp_runtime_threads()
    assert (n is None and isinstance(name, str) and name) or (
        isinstance(n, int) and n >= 1 and name), (n, name)
    assert isinstance(omp_probe.loaded_runtimes(), list)
    for plat in ("linux", "darwin", "win32", "sunos5"):     # other OS branches: no raise
        assert isinstance(omp_probe.loaded_runtimes(plat), list)


@test
def omp_probe_no_runtime_or_bad_library_gives_none():
    n, why = omp_probe.omp_runtime_threads(libs=[])
    assert n is None and "no OpenMP runtime" in why, why
    d = tempfile.mkdtemp()
    try:
        bogus = os.path.join(d, "libgomp.so.1")
        with open(bogus, "w") as f:
            f.write("not a library")
        n, why = omp_probe.omp_runtime_threads(libs=[bogus, "/no/such/libomp.dylib"])
        assert n is None and "libgomp.so.1" in why, why
    finally:
        shutil.rmtree(d)


@test
def omp_probe_name_pattern():
    for p in ("/env/lib/libgomp.so.1", "/x/lib/libomp.dylib", "C:\\e\\Library\\bin\\libomp.dll",
              "libgomp-1.dll", "/opt/intel/libiomp5.so", "vcomp140.dll"):
        assert omp_probe._LIB_RE.search(p), p
    for p in ("/env/lib/libgompx_not.txt", "/env/lib/libpython3.12.so", "/env/lib/libm.so.6",
              "/x/lib/libcomp.so"):
        assert not omp_probe._LIB_RE.search(p), p


@test
def omp_probe_reads_a_loaded_runtime_in_a_fresh_process():
    """When numpy / scipy / ANUGA loaded an OpenMP runtime, the probe reads its count; with
    OMP_NUM_THREADS=3 that count is 3. Where none is loaded the probe says so (None)."""
    code = ("import sys; sys.path.insert(0, {!r}); import numpy, scipy.linalg; import omp_probe;"
            "print(omp_probe.omp_runtime_threads())").format(os.path.dirname(omp_probe.__file__))
    env = dict(os.environ, OMP_NUM_THREADS="3")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    n, name = eval(out.stdout.strip().splitlines()[-1])
    assert n is None or n == 3, (n, name)


@test
def runtime_note_says_when_the_runtime_disagrees():
    assert AW.runtime_note(0, None, "x") == ""
    assert AW.runtime_note(4, 4, "libgomp.so.1") == ""
    assert AW.runtime_note(4, 4, "libgomp.so.1", "old") == "old"
    n = AW.runtime_note(4, 1, "libomp.dylib")
    assert "reports 1 thread(s), not 4" in n and "libomp.dylib" in n, n
    n = AW.runtime_note(4, None, "no OpenMP runtime loaded in this process", "setter failed")
    assert n.startswith("setter failed; thread count not verified"), n


@test
def report_prefers_the_runtime_count():
    """run()'s run_report["anuga_threads"]: anuga_reported = omp_runtime_threads when the
    worker found one, else the old omp_threads (checked on the code that builds it)."""
    import inspect
    src = inspect.getsource(AM.run)
    i = src.index('rep = stats.get("omp_runtime_threads")')
    assert 'rep = stats.get("omp_threads")' in src[i:i + 200], src[i:i + 200]


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("")


@test
def anuga_candidates_windows_layout():
    d = tempfile.mkdtemp()
    try:
        home = os.path.join(d, "Users", "nick")
        local = os.path.join(home, "AppData", "Local")
        a = os.path.join(home, "miniforge3", "envs", "anuga", "python.exe")
        b = os.path.join(local, "miniforge3", "envs", "anuga-4", "python.exe")
        other = os.path.join(home, "miniforge3", "envs", "qgis", "python.exe")
        active = os.path.join(d, "conda", "envs", "my_anuga")
        posix_only = os.path.join(home, "miniforge3", "envs", "anuga_posix", "bin", "python")
        for p in (a, b, other, os.path.join(active, "python.exe"), posix_only):
            _touch(p)
        env = {"USERPROFILE": home, "LOCALAPPDATA": local, "CONDA_PREFIX": active + os.sep}
        got = nd_data.anuga_python_candidates(nt=True, environ=env)
        want = [os.path.realpath(p) for p in (a, b, os.path.join(active, "python.exe"))]
        assert got == want, got
        # the same tree seen as Linux / macOS: only bin/python, no Windows roots
        got = nd_data.anuga_python_candidates(nt=False, environ={"CONDA_PREFIX": ""})
        assert all(not p.endswith("python.exe") for p in got), got
        assert nd_data.env_python_rel(True) == ("python.exe",)
        assert nd_data.env_python_rel(False) == ("bin", "python")
    finally:
        shutil.rmtree(d)


@test
def anuga_candidates_posix_layout_unchanged():
    d = tempfile.mkdtemp()
    try:
        root = os.path.join(d, "mf")
        p = os.path.join(root, "envs", "anuga", "bin", "python")
        _touch(p)
        _touch(os.path.join(root, "envs", "anuga", "python.exe"))
        active = os.path.join(d, "anuga_env")
        _touch(os.path.join(active, "bin", "python"))
        got = nd_data.anuga_python_candidates(
            nt=False, environ={"MAMBA_ROOT_PREFIX": root, "CONDA_PREFIX": active,
                               "LOCALAPPDATA": root, "USERPROFILE": root})
        # CONDA_PREFIX is searched before MAMBA_ROOT_PREFIX (as before 3.10.50)
        want = [os.path.realpath(os.path.join(active, "bin", "python")), os.path.realpath(p)]
        assert [g for g in got if g.startswith(os.path.realpath(d))] == want, got
    finally:
        shutil.rmtree(d)
