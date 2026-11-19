"""BoannGrid D4 flow directions + accumulation (core/metalterrain/flow.py), through the CPU build
of the same kernels, against a serial descending-elevation accumulation oracle. Exact match on a
DAG (the filled surface drains everywhere, so the D4 network is acyclic), plus the drainage-area
invariants (every cell drains to exactly one outlet; accumulation only grows downstream).

It reuses core/metalflood's backend, like the fill; where this computer has an Apple GPU, Metal is
also checked against the CPU build.
"""
import ctypes
import os

import numpy as np

from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import flow as FL
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if os.name == "nt":
        raise Skip("the flow CPU build is not made on Windows")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the flow CPU build")


def _oracle(dem):
    """Serial D4 flow direction + accumulation on the same datum-shifted float32 surface the
    kernel sees: steepest descent to the lowest strictly-lower N/E/S/W neighbour (ties in the
    order N,E,S,W), then drainage area by pushing each cell's area to its downstream cell in
    descending-elevation order (correct because the downstream cell is always strictly lower)."""
    a = np.asarray(dem, dtype=np.float64)
    ny, nx = a.shape
    valid = np.isfinite(a) & (a != F.NODATA)
    filled, _ = F.fill_dem(a, backend="cpu")
    datum = float(filled[valid].min())
    w = np.where(valid, filled - datum, 1.0e18).astype(np.float32)
    direction = np.full((ny, nx), FL.NONE, dtype=np.int16)
    off = [(FL.N, -1, 0), (FL.E, 0, 1), (FL.S, 1, 0), (FL.W, 0, -1)]
    for r in range(ny):
        for c in range(nx):
            if not valid[r, c]:
                direction[r, c] = -1
                continue
            bestw = w[r, c]
            bestd = FL.NONE
            for d, dr, dc in off:
                rr, cc = r + dr, c + dc
                if 0 <= rr < ny and 0 <= cc < nx and valid[rr, cc] and w[rr, cc] < bestw:
                    bestw = w[rr, cc]
                    bestd = d
            direction[r, c] = bestd
    accum = valid.astype(np.float64)
    order = np.argsort(-w.ravel(), kind="stable")
    for idx in order:
        r, c = divmod(int(idx), nx)
        if not valid[r, c]:
            continue
        d = int(direction[r, c])
        if d == FL.NONE:
            continue
        dr, dc = FL._DRC[d]
        accum[r + dr, c + dc] += accum[r, c]
    return direction, accum, valid


@test
def flow_cpu_build_matches_oracle():
    """The GPU flow (CPU build) gives the exact same D4 directions and drainage areas as the
    serial oracle: same filled surface, same steepest-descent rule, same counts."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=70, nx=100)
    direction, accum, info = FL.flow_accum(z, backend="cpu")
    assert info["backend"] == "cpu" and info["converged"], info
    od, oa, valid = _oracle(z)
    assert np.array_equal(direction[valid], od[valid]), "D4 directions differ from the oracle"
    assert np.array_equal(accum[valid], oa[valid]), "drainage areas differ from the oracle"


@test
def flow_accum_invariants():
    """Drainage-area invariants, independent of the oracle: every valid cell has area >= 1; the
    area only grows downstream; and every cell's area reaches exactly one outlet/sink, so the
    areas at the terminal cells sum to the valid-cell count."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=64, nx=96, seed=2)
    direction, accum, info = FL.flow_accum(z, backend="cpu")
    valid = np.isfinite(accum)
    assert (accum[valid] >= 1.0).all(), "a valid cell has drainage area < 1"
    ny, nx = z.shape
    # accumulation never decreases downstream
    for d, (dr, dc) in FL._DRC.items():
        m = valid & (direction == d)
        rr, cc = np.nonzero(m)
        dn = accum[rr + dr, cc + dc]
        assert (dn + 1e-9 >= accum[rr, cc]).all(), "accumulation dropped downstream (dir {})".format(d)
    # conservation: all area ends at the terminal (outlet / sink) cells
    term = valid & (direction == FL.NONE)
    assert abs(float(accum[term].sum()) - float(valid.sum())) < 0.5, "area not conserved to outlets"
    # at least one outlet exists (water has to leave the grid)
    assert term.any(), "no outlet/sink cell"


@test
def flow_is_deterministic():
    """The double-buffered accumulation has a unique fixed point, so two runs are byte-identical
    (trivially on the CPU build; the determinism guarantee on a real Metal GPU)."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=60, nx=60, seed=5)
    d1, a1, _ = FL.flow_accum(z, backend="cpu")
    d2, a2, _ = FL.flow_accum(z, backend="cpu")
    assert np.array_equal(d1, d2) and np.array_equal(a1, a2), "two flow runs differ"


@test
def flow_nodata_drains_out():
    """A basin ringed by NoData routes its drainage off the island: the accumulation is finite,
    every valid cell drains, and the interior pit carries real upstream area."""
    _need_cpu_build()
    z = cases.nodata_ringed_pit(n=64, depth=8.0)
    direction, accum, info = FL.flow_accum(z, backend="cpu")
    assert info["converged"], info
    valid = np.isfinite(accum)
    assert np.isfinite(accum[valid]).all()
    assert float(accum[valid].max()) <= float(valid.sum()), "area exceeds the valid-cell count"
    # the deepest ground collects the most upstream area
    zc = np.where(valid, z, np.nan)
    deep = np.unravel_index(np.nanargmin(zc), zc.shape)
    assert accum[deep] > 1.0, "the pit floor collected no upstream drainage"


@test
def flow_params_layout_matches_kernel():
    """TParams in Python is the struct the kernel declares, and the buffers are named at the
    indices the kernel reads them from - the same contract the fill checks."""
    _need_cpu_build()
    lib = ctypes.CDLL(mfb.build_cpu_library(FL.SPEC))
    assert lib.mf_params_size() == ctypes.sizeof(FL.TParams)
    names = [n for n, _, _ in FL._layout(3, 4)]
    with open(FL.KERNEL_SOURCE, encoding="utf-8") as fh:
        src = fh.read()
    for k, n in enumerate(names):
        assert "{} B({})".format(n, k) in src, (n, k)
    assert "constant TParams& p B({})".format(len(names)) in src


@test
def flow_gpu_matches_cpu_build():
    """Runs only on a Mac with a Metal GPU: the Apple-GPU flow equals the CPU build's exactly."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    z = cases.pitted_plane(ny=72, nx=108)
    d_cpu, a_cpu, _ = FL.flow_accum(z, backend="cpu")
    d_gpu, a_gpu, ig = FL.flow_accum(z, backend="metal")
    assert ig["backend"] == "metal", ig
    assert np.array_equal(d_cpu, d_gpu), "D4 directions differ CPU vs Metal"
    assert np.array_equal(a_cpu, a_gpu), "drainage areas differ CPU vs Metal"


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (flow_cpu_build_matches_oracle, flow_accum_invariants, flow_is_deterministic,
               flow_nodata_drains_out, flow_params_layout_matches_kernel,
               flow_gpu_matches_cpu_build):
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
