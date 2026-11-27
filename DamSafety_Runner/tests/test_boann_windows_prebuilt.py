"""Boann 6.8.1 (DamSafety Core 1.8.1): a Windows computer with no C++ compiler runs the CPU
libraries a release ships under boann/hydro/prebuilt/<platform>/.

- boann_source_hash_ignores_line_ends: a CRLF copy of the kernel sources (what Git for Windows
  checks out) names the same library as the LF copy the release built from, and an LF copy names
  exactly what it did before 6.8.1.
- boann_prebuilt_counts_without_a_compiler: with no compiler and a prebuilt library in place,
  backends.cpu_available, BoannGrid's available() and bench.py's CPU run all use it, and bench.py
  makes a --out folder that does not exist yet."""
import copy
import hashlib
import json
import os
import shutil
import tempfile

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import bench
from nd_breach_runner.tests.registry import Skip, test


def _crlf_spec(d):
    """FLOOD_SPEC with its three sources copied into d with CRLF line ends."""
    paths = []
    for p in (mfb.FLOOD_SPEC.kernel_source, mfb.FLOOD_SPEC.shim, mfb.FLOOD_SPEC.cpu_source):
        with open(p, "rb") as f:
            data = f.read()
        assert b"\r\n" not in data, "{} is not LF in this checkout".format(p)
        q = os.path.join(d, os.path.basename(p))
        with open(q, "wb") as f:
            f.write(data.replace(b"\n", b"\r\n"))
        paths.append(q)
    spec = copy.copy(mfb.FLOOD_SPEC)
    spec.kernel_source, spec.shim, spec.cpu_source = paths
    return spec


@test
def boann_source_hash_ignores_line_ends():
    spec = mfb.FLOOD_SPEC
    # the pre-6.8.1 hash of the LF files, computed the old way: unchanged
    h = hashlib.sha256()
    for p in (spec.kernel_source, spec.shim, spec.cpu_source):
        with open(p, "rb") as f:
            h.update(f.read())
    h.update(" ".join(mfb.CPU_FLAGS).encode())
    assert mfb._source_hash(spec) == h.hexdigest()[:16], "an LF checkout's library name moved"
    d = tempfile.mkdtemp(prefix="boann_crlf_")
    try:
        crlf = _crlf_spec(d)
        for omp in (True, False):
            assert mfb.cpu_library_name(crlf, ".dll", omp) == mfb.cpu_library_name(spec, ".dll", omp)
    finally:
        shutil.rmtree(d, True)


class _NoCompiler:
    """find_compiler() answers None (in backends and in BoannGrid, which imported the name),
    PREBUILT_DIR points at a folder of our own."""

    def __init__(self, prebuilt):
        self.prebuilt = prebuilt

    def __enter__(self):
        from DamSafety_Core.boann import grid as bg
        self.bg = bg
        self.was = (mfb.find_compiler, bg.find_compiler, mfb.PREBUILT_DIR,
                    os.environ.get("DAMSAFETY_MF_CACHE"))
        mfb.find_compiler = bg.find_compiler = lambda: None
        mfb.PREBUILT_DIR = self.prebuilt
        # an empty build cache, so only the prebuilt folder can answer
        self.cache = tempfile.mkdtemp(prefix="boann_cache_")
        os.environ["DAMSAFETY_MF_CACHE"] = self.cache

    def __exit__(self, *exc):
        mfb.find_compiler, self.bg.find_compiler, mfb.PREBUILT_DIR, cache = self.was
        if cache is None:
            os.environ.pop("DAMSAFETY_MF_CACHE", None)
        else:
            os.environ["DAMSAFETY_MF_CACHE"] = cache
        shutil.rmtree(self.cache, True)


@test
def boann_prebuilt_counts_without_a_compiler():
    if not mfb.find_compiler():
        raise Skip("no C++ compiler to make the stand-in prebuilt libraries")
    from DamSafety_Core.boann import grid as bg
    from DamSafety_Core.boann.grid import fill as mt_fill
    ok, _why = mfb.metal_available()
    if ok:
        raise Skip("a Metal GPU answers available() before the CPU build is asked")
    d = tempfile.mkdtemp(prefix="boann_prebuilt_")
    was_omp = os.environ.get("DAMSAFETY_MF_OPENMP")
    try:
        os.environ["DAMSAFETY_MF_OPENMP"] = "0"
        plat = os.path.join(d, mfb.platform_tag())
        os.makedirs(plat)
        for spec in (mfb.FLOOD_SPEC, mt_fill.SPEC):
            shutil.copy(mfb.build_cpu_library(spec), plat)
        empty = tempfile.mkdtemp(prefix="boann_noprebuilt_", dir=d)
        with _NoCompiler(empty):
            assert not mfb.cpu_available(mfb.FLOOD_SPEC)
            assert not bg.available()[0]
        with _NoCompiler(d):
            assert mfb.cpu_available(mfb.FLOOD_SPEC)
            assert not mfb.cpu_available(mfb.FV_SPEC)      # none shipped for FV1 here
            ok, why = bg.available()
            assert ok and "shipped" in why, why
            bench.SIZES["tiny"] = dict(ny=40, nx=60, dx=10.0, peak_cms=50.0, sim_s=600.0)
            out = os.path.join(d, "not", "yet")
            try:
                assert bench.main(["--size", "tiny", "--out", out]) == 0
            finally:
                bench.SIZES.pop("tiny", None)
            with open(os.path.join(out, "metalflood_bench.json")) as f:
                info = json.load(f)
            assert "cpu" in info["runs"], info["runs"]
            assert info["runs"]["cpu"]["device"].lower().startswith("cpu"), info["runs"]["cpu"]
            assert os.listdir(os.environ["DAMSAFETY_MF_CACHE"]) == [], "built, not prebuilt"
    finally:
        if was_omp is None:
            os.environ.pop("DAMSAFETY_MF_OPENMP", None)
        else:
            os.environ["DAMSAFETY_MF_OPENMP"] = was_omp
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_source_hash_ignores_line_ends, boann_prebuilt_counts_without_a_compiler):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
