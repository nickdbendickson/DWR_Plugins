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
    """Skip only when this computer has neither a prebuilt fill library nor a C++ compiler.
    A Windows release ships the library; a compiler there is enough too."""
    if mfb.prebuilt_available(F.SPEC) or mfb.find_compiler():
        return
    if os.name == "nt":
        raise Skip("the fill's CPU build is not made on Windows (a release ships it prebuilt)")
    raise Skip("no C++ compiler for the fill's CPU build")


@test
def metalterrain_cpu_build_matches_priority_flood():
    """The GPU fill (CPU build) equals the serial priority-flood surface to float precision, and
    never lowers the ground."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=80, nx=120)
    filled, info = F.fill_dem(z, backend="cpu", method="relax")
    assert info["backend"] == "cpu" and info["converged"], info
    orc = terrain.priority_flood(z)
    dmax = float(np.abs(filled - orc).max())
    assert dmax <= 5e-3, dmax                       # eps*path at most; same surface
    # fill only raises: filled >= z to float32-relief precision (the kernel relaxes in a
    # datum-shifted space, so it is accurate to ULP(relief) ~ 1e-6 m, not ULP(altitude)).
    assert (filled + 1e-4 >= z).all(), float((filled - z).min())
    # it really removed depressions (the pits held water)
    assert float(np.maximum(filled - z, 0.0).sum()) > 10.0, "no depressions filled"


@test
def metalterrain_fill_is_deterministic():
    """Water levels only decrease, so the fixed point is unique: two runs are byte-identical.
    (On the CPU build trivially; on a real Metal GPU this is the determinism guarantee.)"""
    _need_cpu_build()
    z = cases.pitted_plane(ny=70, nx=70, seed=3)
    a, _ = F.fill_dem(z, backend="cpu", method="relax")
    b, _ = F.fill_dem(z, backend="cpu", method="relax")
    assert np.array_equal(a, b), "two fills differ"


@test
def metalterrain_nodata_is_a_free_exit():
    """A basin ringed by NoData must drain to the NoData, so the fill is FINITE - the Phase-A
    conditioner read NoData as a wall and the whole island became one unbounded depression."""
    _need_cpu_build()
    z = cases.nodata_ringed_pit(n=64, depth=8.0)
    filled, info = F.fill_dem(z, backend="cpu", method="relax")
    assert info["converged"], info
    valid = np.isfinite(z)
    # every valid cell got a finite, sane level (no 1e18 "infinite depression")
    fv = filled[valid]
    assert np.isfinite(fv).all(), "some cells did not drain (left at the fill sentinel)"
    assert fv.max() < 1e6, "a cell filled toward the unbounded sentinel: NoData not a free exit"
    # NoData stays NoData
    assert np.isnan(filled[~valid]).all()
    assert (filled[valid] + 1e-4 >= z[valid]).all(), float((filled[valid] - z[valid]).min())
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
def metalterrain_nested_basin_spill_sequence():
    """A basin inside a basin - the case where eps-relaxation PD and priority-flood classically
    diverge in fill *level* (the inner basin must chain through its own saddle to the OUTER
    spill, not stop at the inner saddle). Assert the fill levels of both basins and the
    inter-basin spill, not just that the surface is smooth."""
    _need_cpu_build()
    z, meta = cases.nested_basins()
    filled, info = F.fill_dem(z, backend="cpu", method="relax")
    assert info["converged"], info
    inner, outer = meta["inner"], meta["outer"]
    inner_rim, outer_rim = meta["inner_rim"], meta["outer_rim"]   # 325, 330
    fi, fo = filled[inner], filled[outer]
    # 1) The inner basin fills to the OUTER spill (330), NOT the inner saddle (325): the spill
    #    sequence chained. A fill that treated the inner saddle as an exit would stop near 325.
    assert fi.min() >= outer_rim - 1e-3, ("inner basin under-filled", fi.min(), outer_rim)
    assert fi.min() > inner_rim + 1.0, ("inner basin stopped at the inner saddle", fi.min())
    assert fi.max() <= outer_rim + 0.05, ("inner basin over-filled", fi.max())
    # 2) The outer basin fills to its spill (330) too.
    assert abs(float(np.median(fo)) - outer_rim) <= 1e-2, (float(np.median(fo)), outer_rim)
    assert fo.min() >= outer_rim - 1e-3 and fo.max() <= outer_rim + 0.05, (fo.min(), fo.max())
    # 3) The eps drainage ramp runs the right way: it rises from the outer spill back into the
    #    deeper inner basin, so the whole enclosed pool drains toward the one exit as a single
    #    connected path (not two disconnected flat pools). The datum shift is what keeps this
    #    eps ramp representable in float32 at ~330 m, so this also guards that.
    assert float(fi.mean()) >= float(fo.mean()) - 1e-6, (fi.mean(), fo.mean())
    # never lowers ground
    assert (filled + 1e-4 >= z).all(), float((filled - z).min())
    # 4) Cross-check against the serial oracle over the enclosed region specifically (no NoData
    #    here, so priority_flood needs no scipy). A global tolerance can hide a wrong spill level
    #    in a small region, so compare where the two algorithms diverge.
    enc = meta["enclosed"]
    orc = terrain.priority_flood(z)
    assert float(np.abs(filled[enc] - orc[enc]).max()) <= 5e-3, \
        float(np.abs(filled[enc] - orc[enc]).max())
    # and the oracle agrees the inner basin reached the outer spill
    assert orc[inner].min() > inner_rim + 1.0, ("oracle inner basin", orc[inner].min())


@test
def metalterrain_case_makers_keep_their_border_outlet_convention():
    """The "border cells are outlets (no NoData)" convention that pitted_plane, channel_with_barrier
    and nested_basins document is load-bearing: fill._outlets reads the grid border as outlets, so if
    a border cell were ever NoData the fill's free-exit behaviour would change materially. Pin the
    convention on the case-makers themselves (review #419), not in prose only."""
    grids = [cases.pitted_plane(ny=40, nx=60),
             cases.channel_with_barrier(ny=50, nx=120),
             cases.nested_basins()[0]]                     # returns (z, meta)
    for z in grids:
        z = np.asarray(z, dtype=np.float64)
        border = np.zeros(z.shape, bool)
        border[0, :] = border[-1, :] = border[:, 0] = border[:, -1] = True
        b = z[border]
        assert np.isfinite(b).all(), "a border cell is NaN (NoData) - breaks the outlet convention"
        assert (b != F.NODATA).all(), "a border cell holds the NoData sentinel"
        # and these makers carry no NoData anywhere (the NoData case is nodata_ringed_pit's job)
        assert np.isfinite(z).all() and (z != F.NODATA).all(), "unexpected NoData in the case"
        # the convention is what fill._outlets consumes: the whole border rings as outlets
        assert F._outlets(np.isfinite(z) & (z != F.NODATA))[border].all(), \
            "the border does not ring as outlets"


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
    a, ia = F.fill_dem(z, backend="cpu", method="relax")
    b, ib = F.fill_dem(z, backend="metal", method="relax")
    assert ib["backend"] == "metal", ib
    assert float(np.abs(a - b).max()) <= 1e-3, np.abs(a - b).max()


if __name__ == "__main__":
    fails = 0
    for fn in (metalterrain_cpu_build_matches_priority_flood, metalterrain_fill_is_deterministic,
               metalterrain_nodata_is_a_free_exit, metalterrain_nested_basin_spill_sequence,
               metalterrain_case_makers_keep_their_border_outlet_convention,
               metalterrain_params_layout_matches_kernel,
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
