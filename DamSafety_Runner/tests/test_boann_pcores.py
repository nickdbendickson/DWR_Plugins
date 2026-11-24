"""Runner 3.82.0 / Boann 5.4.1: on Apple silicon the CPU build uses the performance cores only
unless DAMSAFETY_MF_THREADS says otherwise (backends.default_threads / threads_wanted).

- boann_cpu_threads_default_to_the_performance_cores: the choice from the core counts, the
  environment variable over it, and the CPU backend reporting what it got.
- boann_cpu_build_gives_the_same_answer_on_any_core_count: the reason the default can change
  freely: every solver's result files are byte-identical on 1, 3 and 4 cores (FV1, FV2, DG2, SGC
  and sub-grid FV run the static row split; ACC's dry-cell skip is tested in test_boann_dry_skip)."""
import filecmp
import os
import shutil
import sys
import tempfile

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood.cases import valley_deck
from nd_breach_runner.tests.registry import Skip, test


class _Env:
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.was = {k: os.environ.get(k) for k in self.kv}
        for k, v in self.kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _need_omp_build():
    if os.name == "nt" and not mfb.prebuilt_available():
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.prebuilt_available() and not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")
    with _Env(DAMSAFETY_MF_OPENMP="1"):
        if not mfb.build_cpu_library(mfb.FLOOD_SPEC).endswith("_omp" + mfb.library_ext()):
            raise Skip("this C++ compiler has no OpenMP")


@test
def boann_cpu_threads_default_to_the_performance_cores():
    # M2 Pro, 12 cores (8 performance + 4 efficiency) and 10 cores (6 + 4)
    assert mfb.default_threads(perf_cores=8, all_cores=12) == 8
    assert mfb.default_threads(perf_cores=6, all_cores=10) == 6
    # every core a performance core (a VM, a chip without efficiency cores): the runtime decides
    assert mfb.default_threads(perf_cores=8, all_cores=8) == 0
    # no performance-level key (Linux, Windows, an Intel Mac): the runtime decides
    assert mfb.default_threads(perf_cores=0, all_cores=16) == 0
    if sys.platform != "darwin":
        assert mfb.default_threads() == 0              # not a Mac: unchanged
    # the environment variable wins, whatever the default
    with _Env(DAMSAFETY_MF_THREADS="3"):
        assert mfb.threads_wanted() == 3
    with _Env(DAMSAFETY_MF_THREADS="oops"):
        assert mfb.threads_wanted() == (mfb.default_threads() or
                                         (mfb._sysctl_int("hw.ncpu") or 0
                                          if sys.platform == "darwin" else 0))
    with _Env(DAMSAFETY_MF_THREADS="0"):
        w = mfb.threads_wanted()
        # every core: 0 (the runtime's own choice), or on a Mac the logical core count
        assert w == (mfb._sysctl_int("hw.ncpu") or 0 if sys.platform == "darwin" else 0), w
    with _Env(DAMSAFETY_MF_THREADS=None):
        w = mfb.threads_wanted()
        if sys.platform != "darwin":
            assert w == 0, w
        elif mfb.default_threads():
            assert w == mfb._sysctl_int("hw.perflevel0.physicalcpu"), w
    # the backend reports the count it got
    _need_omp_build()
    with _Env(DAMSAFETY_MF_OPENMP="1", DAMSAFETY_MF_THREADS="3"):
        bk = mfb.CpuBackend(mfb.FLOOD_SPEC)
        assert bk.threads == 3 and "3 cores" in bk.device, bk.device
    with _Env(DAMSAFETY_MF_OPENMP="1", DAMSAFETY_MF_THREADS=None):
        bk = mfb.CpuBackend(mfb.FLOOD_SPEC)
        want = mfb.threads_wanted()
        if want:
            assert bk.threads == want, (bk.threads, want)


@test
def boann_cpu_build_gives_the_same_answer_on_any_core_count():
    _need_omp_build()
    d = tempfile.mkdtemp(prefix="boann_pcores_")
    try:
        for solver in ("fv", "fv2", "dg2", "subfv", "subli", "sgc"):
            runs = []
            for threads in ("1", "3", "4"):
                f = os.path.join(d, solver, threads)
                par = valley_deck(f, sim_s=900.0)
                with _Env(DAMSAFETY_MF_OPENMP="1", DAMSAFETY_MF_THREADS=threads):
                    try:
                        st = cli.run(par, solver=solver, backend="cpu", quiet=True)
                    except (cli.CliError, ImportError):
                        if solver == "sgc":         # needs the Runner's core/sgc
                            break
                        raise
                runs.append((threads, st, f))
            if not runs:
                continue
            (_t1, s1, f1) = runs[0]
            for threads, st, f in runs[1:]:
                assert st["steps"] == s1["steps"], (solver, threads)
                for ext in ("max", "maxVc", "inittm", "maxtm", "mxe", "mass"):
                    a = os.path.join(f1, "results", "run." + ext)
                    b = os.path.join(f, "results", "run." + ext)
                    assert filecmp.cmp(a, b, shallow=False), \
                        "{} run.{} differs on {} cores".format(solver, ext, threads)
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_cpu_threads_default_to_the_performance_cores,
               boann_cpu_build_gives_the_same_answer_on_any_core_count):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
