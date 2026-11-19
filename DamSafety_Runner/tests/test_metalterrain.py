"""The BoannGrid terrain fill (core/metalterrain): its one kernel, run through the CPU build of
the same source, against core/terrain.priority_flood (the Barnes serial ground truth), for
closed depressions and - the case the Phase-A CPU conditioner got wrong - a basin ringed by
NoData, which must drain to the NoData rather than fill without bound. Where this computer has
an Apple GPU, Metal is also checked against the CPU build.

It reuses core/metalflood's backend (backends.Spec) and shim; these tests also pin that reuse.
"""
import ctypes
import os
import sys

import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if os.name == "nt":
        raise Skip("the fill's CPU build is not made on Windows")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the fill's CPU build")


@test
def metalterrain_cpu_build_matches_priority_flood():
    """The GPU fill (CPU build) equals the serial priority-flood surface to float precision, and
    never lowers the ground."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=80, nx=120)
    filled, info = F.fill_dem(z, backend="cpu")
    assert info["backend"] == "cpu" and info["converged"], info
    orc = terrain.priority_flood(z)
    dmax = float(np.abs(filled - orc).max())
    assert dmax <= 5e-3, dmax                       # eps*path at most; same surface
    z32 = z.astype(np.float32).astype(np.float64)   # the ground the float32 kernel sees
    assert (filled + 1e-6 >= z32).all()             # fill only raises (never lowers the ground)
    # it really removed depressions (the pits held water)
    assert float(np.maximum(filled - z, 0.0).sum()) > 10.0, "no depressions filled"


@test
def metalterrain_fill_is_deterministic():
    """Water levels only decrease, so the fixed point is unique: two runs are byte-identical.
    (On the CPU build trivially; on a real Metal GPU this is the determinism guarantee.)"""
    _need_cpu_build()
    z = cases.pitted_plane(ny=70, nx=70, seed=3)
    a, _ = F.fill_dem(z, backend="cpu")
    b, _ = F.fill_dem(z, backend="cpu")
    assert np.array_equal(a, b), "two fills differ"


@test
def metalterrain_nodata_is_a_free_exit():
    """A basin ringed by NoData must drain to the NoData, so the fill is FINITE - the Phase-A
    conditioner read NoData as a wall and the whole island became one unbounded depression."""
    _need_cpu_build()
    z = cases.nodata_ringed_pit(n=64, depth=8.0)
    filled, info = F.fill_dem(z, backend="cpu")
    assert info["converged"], info
    valid = np.isfinite(z)
    # every valid cell got a finite, sane level (no 1e18 "infinite depression")
    fv = filled[valid]
    assert np.isfinite(fv).all(), "some cells did not drain (left at the fill sentinel)"
    assert fv.max() < 1e6, "a cell filled toward the unbounded sentinel: NoData not a free exit"
    # NoData stays NoData
    assert np.isnan(filled[~valid]).all()
    z32 = z.astype(np.float32).astype(np.float64)   # the ground the float32 kernel sees
    assert (filled[valid] + 1e-6 >= z32[valid]).all()
    # the deep pit actually took water
    assert float(np.nansum(np.maximum(filled - z, 0.0))) > 5.0, "the ringed pit did not fill"
    # cross-check against the serial oracle when it can run (its NaN path needs scipy, which is
    # absent/broken on some cloud runners); the invariants above are the real free-exit test
    try:
        orc = terrain.priority_flood(z)
    except Exception:
        return
    assert float(np.nanmax(np.abs(filled - orc))) <= 5e-3, np.nanmax(np.abs(filled - orc))


@test
def metalterrain_params_layout_matches_kernel():
    """TParams in Python is the struct the kernel source declares, and the buffers are named at
    the indices the kernel reads them from - the same contract the flood engine checks."""
    _need_cpu_build()
    lib = ctypes.CDLL(mfb.build_cpu_library(F.SPEC))
    assert lib.mf_params_size() == ctypes.sizeof(F.TParams)
    names = [n for n, _, _ in F._layout(3, 4)]
    with open(F.KERNEL_SOURCE, encoding="utf-8") as fh:
        src = fh.read()
    for k, n in enumerate(names):
        assert "{} B({})".format(n, k) in src, (n, k)
    assert "constant TParams& p B({})".format(len(names)) in src


@test
def metalterrain_reuses_the_metalflood_backend():
    """BoannGrid does not fork the backend: it builds on core/metalflood's Spec and its shim."""
    assert isinstance(F.SPEC, mfb.Spec)
    assert os.path.basename(F.SHIM) == "mf_shim.h"
    assert os.path.dirname(os.path.abspath(F.SHIM)) == \
        os.path.dirname(os.path.abspath(mfb.__file__)), "shim is not metalflood's"


@test
def metalterrain_gpu_matches_cpu_build():
    """Runs only on a Mac with a Metal GPU (the macOS CI job, Nick's Mac): the Apple GPU fill
    equals the CPU build's to float precision."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    z = cases.pitted_plane(ny=96, nx=140)
    a, ia = F.fill_dem(z, backend="cpu")
    b, ib = F.fill_dem(z, backend="metal")
    assert ib["backend"] == "metal", ib
    assert float(np.abs(a - b).max()) <= 1e-3, np.abs(a - b).max()


if __name__ == "__main__":
    fails = 0
    for fn in (metalterrain_cpu_build_matches_priority_flood, metalterrain_fill_is_deterministic,
               metalterrain_nodata_is_a_free_exit, metalterrain_params_layout_matches_kernel,
               metalterrain_reuses_the_metalflood_backend, metalterrain_gpu_matches_cpu_build):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
