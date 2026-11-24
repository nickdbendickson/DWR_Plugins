"""Priority-Flood+eps is fill_dem's default (Runner 3.84.0). It copies the relaxation's float32
arithmetic (datum-shifted ground, w = max(z, float32(w_low + eps))), so it lands on the
relaxation's own fixed point in one pass. The gate, on the synthetic cases and on a real
Absaraka DEM that ships with the tests (runs by default):

  * the filled surface is within 2 float32 rounding steps of the relaxation on every cell
    (the relaxation's 1e-6 m dead band leaves a cell a step or two high, nothing else);
  * everything downstream is identical: depression census, D4 flow directions, drainage areas,
    carve records (every field) and the conditioned DEM.

The first build (#507 as opened) filled in float64. Its surface rounded differently in the
flow step's float32 and made flats the relaxation did not have; on the real crop below it is
33 float32 steps off, so this gate refuses it. The kernel tests in test_metalterrain.py are not
loosened.
"""
import os

import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalterrain import audit as A
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalterrain import condition as C
from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import flow as FL
from nd_breach_runner.tests.registry import Skip, test

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_CROP = os.path.join(HERE, "data", "absaraka_30m_crop.npz")
MAX_STEPS = 2.0          # float32 rounding steps allowed between the two fills, per cell


def _need_cpu_build():
    if mfb.prebuilt_available(F.SPEC) or mfb.find_compiler():
        return
    raise Skip("no C++ compiler and no prebuilt fill library")


def _valid(z):
    return np.isfinite(z) & (z != F.NODATA)


def _same(a, b):
    """Bit-identical on the finite cells of both, and NaN in the same places."""
    if a.shape != b.shape:
        return False
    nan_a, nan_b = ~np.isfinite(a), ~np.isfinite(b)
    if not np.array_equal(nan_a, nan_b):
        return False
    return np.array_equal(a[~nan_a], b[~nan_b])


def _steps(new, old, z):
    """Largest difference between two filled surfaces in float32 rounding steps of the
    relaxation's working (datum-shifted) value, the number the kernel actually rounds."""
    valid = _valid(z)
    if not valid.any():
        return 0.0
    assert np.array_equal(np.isfinite(new), np.isfinite(old))
    datum = float(np.asarray(z, dtype=np.float64)[valid].min())
    work = (old[valid] - datum).astype(np.float32)
    ulp = np.spacing(np.abs(work)).astype(np.float64)
    return float(np.max(np.abs(new[valid] - old[valid]) / ulp))


def _real_crop():
    d = np.load(REAL_CROP)
    z = (int(d["base_mm"]) + d["rel_mm"].astype(np.int64)) / 1000.0
    return z, float(d["cell"])


def _cases():
    return [
        ("pitted_plane", cases.pitted_plane(ny=48, nx=64)),
        ("channel_with_barrier", cases.channel_with_barrier(ny=36, nx=72)),
        ("nested_basins", cases.nested_basins()[0]),
        ("nodata_ringed_pit", cases.nodata_ringed_pit(n=40)),
    ]


def _downstream_same(z, cell, new, old, where):
    """Census, D4 flow and drainage area off the two filled surfaces are identical."""
    ln, itn = A.depression_census(z, new, cell_m=cell)
    lo, ito = A.depression_census(z, old, cell_m=cell)
    assert len(itn) == len(ito), (where, len(itn), len(ito))
    assert np.array_equal(ln, lo), where
    assert itn == ito, where
    dn, an, fn = FL.flow_accum(z, filled=new, backend="cpu", conn=4)
    do, ao, fo = FL.flow_accum(z, filled=old, backend="cpu", conn=4)
    assert np.array_equal(dn, do), (where, int(np.sum(dn != do)))
    assert np.array_equal(an, ao, equal_nan=True), (where, int(np.nansum(an != ao)))
    assert fn["interior_sinks"] == fo["interior_sinks"] == 0, (where, fn, fo)


@test
def priority_flood_matches_the_relaxation_on_the_synthetic_cases():
    """Within 2 float32 steps, D4 and D8, and the D4 downstream identical."""
    _need_cpu_build()
    for name, z in _cases():
        for conn in (4, 8):
            new, inew = F.fill_dem(z, method="priority-flood", conn=conn)
            old, iold = F.fill_dem(z, method="relax", backend="cpu", conn=conn)
            assert inew["method"] == "priority-flood" and inew["backend"] == "cpu", inew
            assert inew["sweeps"] == 0 and inew["unreached"] == 0, inew
            assert iold["method"] == "relax" and iold["converged"], (name, conn, iold)
            s = _steps(new, old, z)
            assert s <= MAX_STEPS, (name, conn, s)
            valid = _valid(z)
            assert np.all(new[valid] >= z[valid] - 1e-4), name   # float32-relief precision
            if conn == 4:
                _downstream_same(z, 10.0, new, old, name)


@test
def priority_flood_matches_the_relaxation_on_a_real_dem():
    """Runs by default. A 250 x 300 crop of the Absaraka 30 m 3DEP box (tests/data): within 2
    float32 steps of the relaxation, census / flow / drainage area identical."""
    _need_cpu_build()
    z, cell = _real_crop()
    for conn in (4, 8):
        new, inew = F.fill_dem(z, method="priority-flood", conn=conn)
        old, iold = F.fill_dem(z, method="relax", backend="cpu", conn=conn)
        assert inew["unreached"] == 0 and iold["converged"], (conn, inew, iold)
        s = _steps(new, old, z)
        assert s <= MAX_STEPS, (conn, s)
        if conn == 4:
            _downstream_same(z, cell, new, old, "absaraka crop")


@test
def condition_with_priority_flood_matches_the_relaxation():
    """Carve records (every field, spill_before_m included) and the conditioned array do not move,
    on the synthetic cases and the real crop."""
    _need_cpu_build()
    z_real, cell_real = _real_crop()
    grids = [
        ("pitted", cases.pitted_plane(ny=40, nx=56), 10.0),
        ("channel", cases.channel_with_barrier(ny=40, nx=80), 10.0),
        ("nested", cases.nested_basins()[0], 10.0),
        ("ringed", cases.nodata_ringed_pit(n=36), 10.0),
        ("absaraka crop", z_real, cell_real),
    ]
    carved_real = 0
    for name, z, cell in grids:
        new, rec_n, info_n = C.condition(z, cell_m=cell, method="priority-flood", backend="cpu")
        old, rec_o, info_o = C.condition(z, cell_m=cell, method="relax", backend="cpu")
        assert info_n["fill_method"] == "priority-flood", info_n
        assert info_o["fill_method"] == "relax", info_o
        assert rec_n == rec_o, name
        assert np.array_equal(new, old, equal_nan=True), name
        for k in info_n:
            if k not in ("fill_sweeps", "fill_method"):
                assert info_n[k] == info_o[k], (name, k, info_n[k], info_o[k])
        if name == "absaraka crop":
            carved_real = info_n["carved"]
    assert carved_real >= 10, carved_real        # the crop really exercises the carve path


@test
def a_float64_fill_fails_the_real_dem_gate():
    """The gate is tight enough to refuse #507's first build: Priority-Flood+eps in float64
    (core/terrain.priority_flood's arithmetic, here its D4 twin) is far more than 2 float32 steps
    off the relaxation on the real crop."""
    _need_cpu_build()
    import heapq
    z, _cell = _real_crop()
    ny, nx = z.shape
    w = z.copy()
    closed = F._outlets(np.ones(z.shape, bool), 4)
    heap = [(w[r, c], r, c) for r, c in zip(*np.nonzero(closed))]
    heapq.heapify(heap)
    while heap:
        _l, r, c = heapq.heappop(heap)
        for dr, dc in F._NB4:
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and not closed[rr, cc]:
                closed[rr, cc] = True
                if w[rr, cc] <= w[r, c]:
                    w[rr, cc] = w[r, c] + 1e-5
                heapq.heappush(heap, (w[rr, cc], rr, cc))
    old, _ = F.fill_dem(z, method="relax", backend="cpu", conn=4)
    assert _steps(w, old, z) > 10 * MAX_STEPS


@test
def priority_flood_stays_near_the_float64_oracle():
    """Sanity against the independent oracle (core/terrain.priority_flood, D8, float64): the same
    bound the relaxation's kernel tests use."""
    _need_cpu_build()
    for name, z in _cases():
        new, _ = F.fill_dem(z, method="priority-flood", conn=8)
        try:
            orc = terrain.priority_flood(np.where(_valid(z), z, np.nan))
        except ImportError as err:          # the oracle needs scipy
            raise Skip("core/terrain.priority_flood unavailable: {}".format(err))
        assert np.array_equal(np.isfinite(new), np.isfinite(orc)), name
        assert float(np.nanmax(np.abs(new - orc))) <= 5e-3, name


@test
def priority_flood_python_twin_is_bit_identical():
    """The pure-Python fallback does the same float32 arithmetic as the C++ fill."""
    _need_cpu_build()
    z_real, _c = _real_crop()
    grids = [(n, z) for n, z in _cases()] + [("absaraka strip", z_real[:60, :80])]
    for name, z in grids:
        a = np.asarray(z, dtype=np.float64)
        valid = _valid(a)
        for conn in (4, 8):
            c, _ = F.fill_dem(a, method="priority-flood", conn=conn)
            p, info = F._fill_pf_python(a, valid, F._outlets(valid, conn), 1e-5, conn)
            assert info["unreached"] == 0, (name, conn)
            assert _same(c, p), (name, conn)


@test
def priority_flood_is_deterministic():
    _need_cpu_build()
    z = cases.pitted_plane(ny=40, nx=55, seed=3)
    for conn in (4, 8):
        a, _ = F.fill_dem(z, method="priority-flood", conn=conn)
        b, _ = F.fill_dem(z, method="priority-flood", conn=conn)
        assert a.tobytes() == b.tobytes()


@test
def priority_flood_diagonal_nodata_is_an_outlet_only_for_d8():
    """A pit that touches a NoData hole only on a corner drains to that hole at conn=8
    (the pit cell is an outlet) and to the border at conn=4 (the hole is not 4-adjacent)."""
    _need_cpu_build()
    z = np.full((11, 11), 30.0)
    z[5, 5] = 10.0
    z[5, 6:10] = 20.0
    z[5, 10] = 10.0
    z[4, 4] = np.nan
    f8, i8 = F.fill_dem(z, method="priority-flood", conn=8)
    f4, i4 = F.fill_dem(z, method="priority-flood", conn=4)
    assert i8["unreached"] == 0 and i4["unreached"] == 0
    assert f8[5, 5] == 10.0, f8[5, 5]          # the corner hole is an outlet
    # D4 ignores the corner, so the pit ramps along the flat channel (four cells) to the border:
    # four float32 (+eps) adds on the datum-shifted level 10 m
    e = np.float32(1e-5)
    v = np.float32(10.0)
    for _k in range(4):
        v = np.float32(v + e)
    assert f4[5, 5] == float(v) + 10.0, (f4[5, 5], float(v) + 10.0)
    for conn, f in ((8, f8), (4, f4)):
        old, _ = F.fill_dem(z, method="relax", backend="cpu", conn=conn)
        assert _steps(f, old, z) <= MAX_STEPS, conn


@test
def priority_flood_ramps_a_flat_from_the_nearest_outlet():
    """Every interior cell of a flat is ground plus eps per cell to the border, as float32 adds
    on the datum-shifted surface; same as the relaxation."""
    _need_cpu_build()
    z = np.full((9, 11), 100.0)
    for conn in (4, 8):
        filled, info = F.fill_dem(z, method="priority-flood", conn=conn)
        assert info["unreached"] == 0
        old, _ = F.fill_dem(z, method="relax", backend="cpu", conn=conn)
        assert _same(filled, old), conn
        yy, xx = np.mgrid[0:9, 0:11]
        dist = np.minimum(np.minimum(yy, 8 - yy), np.minimum(xx, 10 - xx))
        e = np.float32(1e-5)
        expect = np.zeros(z.shape, dtype=np.float32)
        for k in range(1, int(dist.max()) + 1):
            expect[dist >= k] = np.float32(expect[dist >= k] + e)
        assert np.array_equal(filled, expect.astype(np.float64) + 100.0), conn
        assert np.all(filled[dist > 0] > 100.0)


@test
def priority_flood_handles_a_single_row_and_a_single_column():
    _need_cpu_build()
    row = np.linspace(10.0, 12.0, 17).reshape(1, -1)
    col = np.linspace(5.0, 6.0, 13).reshape(-1, 1)
    for z in (row, col):
        for conn in (4, 8):
            filled, info = F.fill_dem(z, method="priority-flood", conn=conn)
            assert info["unreached"] == 0, info
            old, _ = F.fill_dem(z, method="relax", backend="cpu", conn=conn)
            assert _same(filled, old)


@test
def priority_flood_python_fallback_when_the_library_cannot_load():
    """method='auto' falls back to pure Python and matches the C++ fill. method='priority-flood'
    raises instead of falling back."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=24, nx=30, seed=1)
    hole = cases.nodata_ringed_pit(n=24)
    good, _ = F.fill_dem(z, method="priority-flood")
    good4, _ = F.fill_dem(z, method="priority-flood", conn=4)
    good_hole, _ = F.fill_dem(hole, method="priority-flood")
    real = F._pf_library
    real_metal = mfb.metal_available

    def boom(*_a, **_k):
        raise mfb.BackendError("no library")

    F._pf_library = boom
    mfb.metal_available = lambda *a, **k: (False, "test: no Metal")
    try:
        fb, info = F.fill_dem(z, method="auto")
        assert info["backend"] == "python" and info["method"] == "priority-flood", info
        assert _same(fb, good)
        fb4, info4 = F.fill_dem(z, method="auto", conn=4)
        assert info4["backend"] == "python"
        assert _same(fb4, good4)
        fbh, _ = F.fill_dem(hole, method="auto")
        assert _same(fbh, good_hole)
        try:
            F.fill_dem(z, method="priority-flood")
        except mfb.BackendError:
            pass
        else:
            assert False, "priority-flood fell back instead of raising"
    finally:
        F._pf_library = real
        mfb.metal_available = real_metal


def _big_real_dem():
    env = os.environ.get("DAMSAFETY_PF_DEM")
    if env and os.path.isfile(env):
        return env
    return None


@test
def priority_flood_matches_relax_on_a_big_real_dem_when_one_is_present():
    """Opt-in (DAMSAFETY_PF_DEM = a GeoTIFF, e.g. the 10 m Absaraka box; the relaxation takes
    minutes there). Same gate as the shipped crop: 2 float32 steps, downstream identical."""
    path = _big_real_dem()
    if not path:
        raise Skip("DAMSAFETY_PF_DEM not set (the shipped Absaraka crop always runs)")
    try:
        from osgeo import gdal
    except ImportError:
        raise Skip("no GDAL to read the real DEM")
    _need_cpu_build()
    ds = gdal.Open(path)
    if ds is None:
        raise Skip("GDAL could not open {}".format(path))
    z = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    nd = ds.GetRasterBand(1).GetNoDataValue()
    if nd is not None:
        z[z == nd] = np.nan
    cell = abs(float(ds.GetGeoTransform()[1]))
    new, inew = F.fill_dem(z, method="priority-flood", conn=4)
    old, _ = F.fill_dem(z, method="relax", backend="cpu", conn=4)
    assert inew["unreached"] == 0, inew
    s = _steps(new, old, z)
    assert s <= MAX_STEPS, s
    _downstream_same(z, cell, new, old, path)


if __name__ == "__main__":
    import sys
    import traceback
    fails = 0
    fns = [v for k, v in sorted(globals().items())
           if callable(v) and getattr(v, "__module__", "") == __name__
           and (k.startswith("priority_flood_") or k.startswith("condition_")
                or k.startswith("a_float64_"))]
    for fn in fns:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
