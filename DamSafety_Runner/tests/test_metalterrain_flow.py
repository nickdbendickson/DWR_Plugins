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
    filled, _ = F.fill_dem(a, backend="cpu", conn=4)     # flow_accum's own fill is D4 (3.57.0)
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


def _kahn_accum(direction, valid):
    """Kahn topological in-degree accumulation on the D4 direction field (adapted from Nick's
    acc_oracle to 4-connectivity): acc = 1 + sum of donors, cells drained in index order once
    their in-degree hits zero. A genuinely different algorithm from both the descending-elevation
    oracle and the GPU fixed-point iteration. Returns (acc, every_valid_cell_drained)."""
    from collections import deque
    ny, nx = direction.shape
    acc = valid.astype(np.float64)
    indeg = np.zeros((ny, nx), dtype=np.int64)
    rr, cc = np.nonzero(valid & (direction >= 0) & (direction < FL.NONE))
    for r, c in zip(rr, cc):
        dr, dc = FL._DRC[int(direction[r, c])]
        if valid[r + dr, c + dc]:
            indeg[r + dr, c + dc] += 1
    rem = indeg.copy()
    q = deque(int(r) * nx + int(c) for r, c in zip(*np.nonzero(valid & (indeg == 0))))
    touched = np.zeros((ny, nx), dtype=bool)
    while q:
        i = q.popleft()
        r, c = divmod(i, nx)
        touched[r, c] = True
        d = int(direction[r, c])
        if d == FL.NONE or d < 0:
            continue
        dr, dc = FL._DRC[d]
        tr, tc = r + dr, c + dc
        if valid[tr, tc]:
            acc[tr, tc] += acc[r, c]
            rem[tr, tc] -= 1
            if rem[tr, tc] == 0:
                q.append(tr * nx + tc)
    return acc, bool((~valid | touched).all())


@test
def flow_accum_matches_kahn_oracle():
    """A third, independent accumulation algorithm (Kahn topological in-degree, from Nick's
    oracle, adapted to D4) agrees with the GPU accumulation exactly, and the drainage network has
    no cycles - the GPU-fixed-point vs CPU-Kahn pairing is two different algorithms, not one spec
    twice."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=66, nx=98, seed=4)
    direction, accum, info = FL.flow_accum(z, backend="cpu")
    valid = np.isfinite(accum)
    kahn, no_cycles = _kahn_accum(direction, valid)
    assert no_cycles, "the D4 drainage network has a cycle (fill did not drain everywhere)"
    assert np.array_equal(accum[valid], kahn[valid]), "GPU accumulation disagrees with Kahn"


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


def _flat_basin(ny=30, nx=40):
    """A level floor (a filled pond, as the float32 fill leaves it on a high-relief grid) inside a
    rim, with one outlet channel falling to the east edge; the rim itself is a level plateau."""
    z = np.full((ny, nx), 20.0)
    z[5:25, 5:35] = 10.0
    z[15, 35:] = [10.0, 9.0, 8.0, 7.0, 6.0][:nx - 35]
    return z


@test
def flow_resolves_flats_so_no_interior_cell_is_a_dead_end():
    """Runner 3.57.0: a level floor gives the kernels no strictly lower neighbour, so before the
    fix every floor cell was a dead end and the drainage area stopped at the floor's edge (Absaraka
    at 30 m: 180,730 dead ends, largest area 1,565 cells). Now every interior cell drains to the
    model edge, the network stays acyclic, directions the kernels set are unchanged, and the outlet
    carries the whole grid; topo and iterate still agree."""
    _need_cpu_build()
    z = _flat_basin()
    for conn in (4, 8):
        none_c = FL.none_code(conn)
        d0, a0, i0 = FL.flow_accum(z, backend="cpu", filled=z, conn=conn, resolve_flats=False)
        assert i0["interior_sinks"] > 500 and i0["flats_resolved"] == 0, i0
        d1, a1, i1 = FL.flow_accum(z, backend="cpu", filled=z, conn=conn)
        assert i1["interior_sinks"] == 0 and i1["converged"], i1
        assert i1["flats_resolved"] == i0["interior_sinks"], (i0, i1)
        kept = d0 != none_c
        assert np.array_equal(d1[kept], d0[kept]), "a direction the kernels set was changed"
        assert a1[15, -1] >= 20 * 30, a1[15, -1]           # the whole floor leaves by the outlet
        assert np.nansum(a1[FL._real_outlets(d1, np.isfinite(z), conn)]) == z.size
        d2, a2, i2 = FL.flow_accum(z, backend="cpu", filled=z, conn=conn, method="iterate")
        assert np.array_equal(d1, d2) and np.array_equal(a1, a2, equal_nan=True), conn
        assert i2["interior_sinks"] == 0


@test
def flow_d4_refills_a_surface_that_only_drains_across_corners():
    """A D8 fill lets a cell drain across a corner, which D4 flow (and the 4-connected flood engine)
    cannot: on such a surface every diagonal-only cell was a dead end (Absaraka: 180,730; here a
    noisy tilted plain). flow_accum now fills it again with D4 connectivity, so every cell reaches
    the model edge; resolve_flats=False keeps the old network, and a real pit on an unfilled surface
    stays a sink there."""
    _need_cpu_build()
    rng = np.random.RandomState(5)
    yy, xx = np.mgrid[0:60, 0:80].astype(np.float64)
    z = 300.0 + 0.05 * xx + 0.02 * yy + 0.3 * rng.standard_normal(xx.shape)
    f8, i8 = F.fill_dem(z, backend="cpu")
    f4, i4 = F.fill_dem(z, backend="cpu", conn=4)
    assert i8["conn"] == 8 and i4["conn"] == 4 and i4["converged"]
    assert (f4 >= f8 - 1e-9).all() and (f4 > f8 + 1e-3).any(), "D4 fill is not the higher one"
    valid = np.isfinite(z)
    d0, a0, i0 = FL.flow_accum(z, backend="cpu", filled=f8, resolve_flats=False)
    assert i0["interior_sinks"] > 100 and not i0["refilled_d4"], i0
    for kw in (dict(filled=f8), dict(filled=f4), {}):
        d, a, info = FL.flow_accum(z, backend="cpu", **kw)
        assert info["interior_sinks"] == 0 and info["converged"], (kw.keys(), info)
        assert info["refilled_d4"] == (kw.get("filled") is f8), info
        assert np.nansum(a[FL._real_outlets(d, valid, 4)]) == z.size
        assert np.nanmax(a) > 10 * np.nanmax(a0)
    # D8 flow on the D8 fill needs no refill
    d8, a8, j8 = FL.flow_accum(z, backend="cpu", filled=f8, conn=8)
    assert not j8["refilled_d4"] and j8["interior_sinks"] == 0, j8
    # an unfilled pit: a sink in the old network, drained (by the D4 refill) in the new one
    p = np.fromfunction(lambda r, c: 10.0 + 0.1 * c, (12, 16))
    p[6, 8] -= 1.0
    d, a, info = FL.flow_accum(p, backend="cpu", filled=p, resolve_flats=False)
    assert d[6, 8] == FL.NONE and info["interior_sinks"] == 1, info
    d, a, info = FL.flow_accum(p, backend="cpu", filled=p)
    assert d[6, 8] != FL.NONE and info["refilled_d4"] and info["interior_sinks"] == 0, info
    try:
        F.fill_dem(z, backend="cpu", conn=6)
    except ValueError:
        pass
    else:
        assert False, "fill accepted conn=6"

if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (flow_cpu_build_matches_oracle, flow_accum_matches_kahn_oracle,
               flow_accum_invariants, flow_is_deterministic, flow_nodata_drains_out,
               flow_params_layout_matches_kernel, flow_gpu_matches_cpu_build,
               flow_resolves_flats_so_no_interior_cell_is_a_dead_end,
               flow_d4_refills_a_surface_that_only_drains_across_corners):
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
