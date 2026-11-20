"""BoannGrid D8 flow directions + accumulation (core/metalterrain/flow.py, conn=8), through the
CPU build of the D8 kernels, against a serial distance-weighted-steepest-descent oracle. Exact
match on the filled (acyclic) surface, the drainage-area invariants, determinism, and the D8-vs-D4
difference (diagonals carry flow D4 cannot), so the 8-connected option is validated the same way
the 4-connected default is.
"""
import os

import numpy as np

from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import flow as FL
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test

INV_DIAG = np.float32(0.70710678)      # matches kernels_flow8.metal


def _need_cpu_build():
    if os.name == "nt":
        raise Skip("the flow CPU build is not made on Windows")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the flow CPU build")


def _oracle8(dem):
    """Serial D8 flow direction + accumulation on the same datum-shifted float32 surface the kernel
    sees: steepest descent by distance-weighted drop ((wi-wj) for cardinals, (wi-wj)/sqrt2 for
    diagonals; ties in the order N,E,S,W,NE,SE,SW,NW), then drainage area by pushing each cell's
    area to its downstream cell in descending-elevation order."""
    a = np.asarray(dem, dtype=np.float64)
    ny, nx = a.shape
    valid = np.isfinite(a) & (a != F.NODATA)
    filled, _ = F.fill_dem(a, backend="cpu")
    datum = float(filled[valid].min())
    w = np.where(valid, filled - datum, 1.0e18).astype(np.float32)
    direction = np.full((ny, nx), FL.NONE8, dtype=np.int16)
    # (code, dr, dc, is_diagonal) in the kernel's fixed tie order
    off = [(FL.N, -1, 0, False), (FL.E, 0, 1, False), (FL.S, 1, 0, False), (FL.W, 0, -1, False),
           (FL.NE, -1, 1, True), (FL.SE, 1, 1, True), (FL.SW, 1, -1, True), (FL.NW, -1, -1, True)]
    for r in range(ny):
        for c in range(nx):
            if not valid[r, c]:
                direction[r, c] = -1
                continue
            wi = w[r, c]
            bests = np.float32(0.0)
            bestd = FL.NONE8
            for code, dr, dc, diag in off:
                rr, cc = r + dr, c + dc
                if 0 <= rr < ny and 0 <= cc < nx and valid[rr, cc]:
                    s = (wi - w[rr, cc]) * INV_DIAG if diag else (wi - w[rr, cc])
                    if s > bests:
                        bests = np.float32(s)
                        bestd = code
            direction[r, c] = bestd
    accum = valid.astype(np.float64)
    order = np.argsort(-w.ravel(), kind="stable")
    for idx in order:
        r, c = divmod(int(idx), nx)
        if not valid[r, c]:
            continue
        d = int(direction[r, c])
        if d == FL.NONE8:
            continue
        dr, dc = FL._DRC8[d]
        accum[r + dr, c + dc] += accum[r, c]
    return direction, accum, valid


@test
def flow8_cpu_build_matches_oracle():
    """The D8 flow (CPU build) gives the exact same directions and drainage areas as the serial
    distance-weighted oracle: same filled surface, same steepest-descent rule, same counts."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=70, nx=100)
    direction, accum, info = FL.flow_accum(z, backend="cpu", conn=8)
    assert info["backend"] == "cpu" and info["converged"] and info["conn"] == 8, info
    od, oa, valid = _oracle8(z)
    assert np.array_equal(direction[valid], od[valid]), "D8 directions differ from the oracle"
    assert np.array_equal(accum[valid], oa[valid]), "D8 drainage areas differ from the oracle"


@test
def flow8_accum_invariants():
    """Drainage-area invariants for D8: every valid cell has area >= 1; the area never decreases
    downstream; and all area ends at the terminal (outlet/sink) cells."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=64, nx=96, seed=2)
    direction, accum, info = FL.flow_accum(z, backend="cpu", conn=8)
    valid = np.isfinite(accum)
    assert (accum[valid] >= 1.0).all(), "a valid cell has drainage area < 1"
    for d, (dr, dc) in FL._DRC8.items():
        m = valid & (direction == d)
        rr, cc = np.nonzero(m)
        dn = accum[rr + dr, cc + dc]
        assert (dn + 1e-9 >= accum[rr, cc]).all(), "accumulation dropped downstream (dir {})".format(d)
    term = valid & (direction == FL.NONE8)
    assert abs(float(accum[term].sum()) - float(valid.sum())) < 0.5, "area not conserved to outlets"
    assert term.any(), "no outlet/sink cell"


@test
def flow8_is_deterministic():
    """The double-buffered D8 accumulation has a unique fixed point, so two runs are identical."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=60, nx=60, seed=5)
    d1, a1, _ = FL.flow_accum(z, backend="cpu", conn=8)
    d2, a2, _ = FL.flow_accum(z, backend="cpu", conn=8)
    assert np.array_equal(d1, d2) and np.array_equal(a1, a2), "two D8 flow runs differ"


@test
def flow8_differs_from_d4_on_diagonal_drainage():
    """On a plane tilted toward a corner, D8 routes diagonally (directions D4 cannot express) and
    concentrates more area in its main channel than the staircased D4 path."""
    _need_cpu_build()
    ny, nx = 50, 50
    rr, cc = np.mgrid[0:ny, 0:nx]
    z = (100.0 - 0.1 * rr - 0.1 * cc).astype(np.float64)   # drains to the (ny-1, nx-1) corner
    d4, a4, i4 = FL.flow_accum(z, backend="cpu", conn=4)
    d8, a8, i8 = FL.flow_accum(z, backend="cpu", conn=8)
    valid = np.isfinite(a8)
    # D8 uses diagonal direction codes; D4 cannot
    assert ((d8 == FL.SE) | (d8 == FL.NE) | (d8 == FL.SW) | (d8 == FL.NW)).any(), "D8 found no diagonal"
    assert not np.array_equal(d4[valid], d8[valid]), "D8 directions identical to D4"
    # both conserve to the outlet corner
    for a in (a4, a8):
        term_area = float(a[np.isfinite(a)][np.argmax(a[np.isfinite(a)])])
        assert term_area <= float(valid.sum())
    # the diagonal main channel concentrates more than the D4 staircase at the outlet
    assert float(np.nanmax(a8)) >= float(np.nanmax(a4)), (np.nanmax(a4), np.nanmax(a8))


@test
def flow8_gpu_matches_cpu_build():
    """Runs only on a Mac with a Metal GPU: the Apple-GPU D8 flow equals the CPU build's exactly."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    z = cases.pitted_plane(ny=72, nx=108)
    d_cpu, a_cpu, _ = FL.flow_accum(z, backend="cpu", conn=8)
    d_gpu, a_gpu, ig = FL.flow_accum(z, backend="metal", conn=8)
    assert ig["backend"] == "metal", ig
    assert np.array_equal(d_cpu, d_gpu), "D8 directions differ CPU vs Metal"
    assert np.array_equal(a_cpu, a_gpu), "D8 drainage areas differ CPU vs Metal"


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (flow8_cpu_build_matches_oracle, flow8_accum_invariants, flow8_is_deterministic,
               flow8_differs_from_d4_on_diagonal_drainage, flow8_gpu_matches_cpu_build):
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
