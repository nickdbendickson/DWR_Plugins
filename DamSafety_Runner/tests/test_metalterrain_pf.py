"""Priority-Flood+eps is fill_dem's default (Runner 3.83.0). These tests pin it against
core/terrain.priority_flood (D8, bit-identical) and against the pure-Python D4 twin, and keep
the old relaxation as a cross-check. They do not loosen the kernel tests in test_metalterrain.py.
"""
import os

import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalterrain import condition as C
from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if mfb.prebuilt_available(F.SPEC) or mfb.find_compiler():
        return
    raise Skip("no C++ compiler and no prebuilt fill library")


def _valid(z):
    return np.isfinite(z) & (z != F.NODATA)


def _oracle8(z):
    return terrain.priority_flood(np.where(_valid(z), z, np.nan))


def _oracle4(z):
    a = np.asarray(z, dtype=np.float64)
    valid = _valid(a)
    w, _n = F._priority_flood_py(a, valid, F._outlets(valid, 4), 1e-5, 4)
    return np.where(valid, w, np.nan)


def _same(a, b):
    """Bit-identical on the finite cells of both, and NaN in the same places."""
    if a.shape != b.shape:
        return False
    nan_a, nan_b = ~np.isfinite(a), ~np.isfinite(b)
    if not np.array_equal(nan_a, nan_b):
        return False
    if not nan_a.any():
        return np.array_equal(a, b)
    return np.array_equal(a[~nan_a], b[~nan_b])


def _cases():
    return [
        ("pitted_plane", cases.pitted_plane(ny=48, nx=64)),
        ("channel_with_barrier", cases.channel_with_barrier(ny=36, nx=72)),
        ("nested_basins", cases.nested_basins()[0]),
        ("nodata_ringed_pit", cases.nodata_ringed_pit(n=40)),
    ]


@test
def priority_flood_matches_the_d8_oracle_bit_for_bit():
    """conn=8 equals terrain.priority_flood on every synthetic case, including NoData cells."""
    _need_cpu_build()
    for name, z in _cases():
        filled, info = F.fill_dem(z, method="priority-flood", conn=8)
        assert info["method"] == "priority-flood" and info["backend"] == "cpu", info
        assert info["sweeps"] == 0 and info["converged"] and info["unreached"] == 0, info
        assert _same(filled, _oracle8(z)), name
        valid = _valid(z)
        assert np.all(filled[valid] >= z[valid]), name


@test
def priority_flood_matches_the_d4_oracle_bit_for_bit():
    """conn=4 equals the pure-Python D4 twin (4 neighbours, _outlets seeding)."""
    _need_cpu_build()
    for name, z in _cases():
        filled, info = F.fill_dem(z, method="priority-flood", conn=4)
        assert info["unreached"] == 0, (name, info)
        assert _same(filled, _oracle4(z)), name
        valid = _valid(z)
        assert np.all(filled[valid] >= z[valid]), name


@test
def priority_flood_stays_within_the_relaxation_bound():
    """The float32 relaxation is the one that rounds. Same bound the kernel tests already use."""
    _need_cpu_build()
    for name, z in _cases():
        for conn in (4, 8):
            new, _ = F.fill_dem(z, method="priority-flood", conn=conn)
            old, iold = F.fill_dem(z, method="relax", backend="cpu", conn=conn)
            assert iold["method"] == "relax" and iold["converged"], (name, conn, iold)
            dmax = float(np.nanmax(np.abs(new - old)))
            assert dmax <= 5e-3, (name, conn, dmax)


@test
def priority_flood_is_deterministic():
    _need_cpu_build()
    z = cases.pitted_plane(ny=40, nx=55, seed=3)
    a, _ = F.fill_dem(z, method="priority-flood")
    b, _ = F.fill_dem(z, method="priority-flood")
    assert a.tobytes() == b.tobytes()
    c, _ = F.fill_dem(z, method="priority-flood", conn=4)
    d, _ = F.fill_dem(z, method="priority-flood", conn=4)
    assert c.tobytes() == d.tobytes()


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
    # D4 ignores the corner, so the pit ramps along the flat channel (four cells) to the border
    assert f4[5, 5] == 20.0 + 4e-5, f4[5, 5]
    assert _same(f8, _oracle8(z))
    assert _same(f4, _oracle4(z))


@test
def priority_flood_ramps_a_flat_from_the_nearest_outlet():
    """Every interior cell of a flat is ground plus eps times its distance to the border."""
    _need_cpu_build()
    z = np.full((9, 11), 100.0)
    for conn in (4, 8):
        filled, info = F.fill_dem(z, method="priority-flood", conn=conn)
        assert info["unreached"] == 0
        oracle = _oracle8(z) if conn == 8 else _oracle4(z)
        assert _same(filled, oracle), conn
        yy, xx = np.mgrid[0:9, 0:11]
        dist = np.minimum(np.minimum(yy, 8 - yy), np.minimum(xx, 10 - xx)).astype(np.float64)
        # sequential (w + eps) additions, not dist * eps in one product: the last bit can differ
        assert np.max(np.abs(filled - (100.0 + 1e-5 * dist))) < 1e-12
        assert np.array_equal(filled[dist == 0], np.full(int(np.sum(dist == 0)), 100.0))
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
            assert _same(filled, _oracle8(z) if conn == 8 else _oracle4(z))
            assert np.all(filled >= z)


@test
def priority_flood_python_fallback_matches_when_the_library_cannot_load():
    """method='auto' falls back to pure Python and matches the C++ fill. method='priority-flood'
    raises instead of falling back."""
    _need_cpu_build()
    z = cases.pitted_plane(ny=24, nx=30, seed=1)
    hole = cases.nodata_ringed_pit(n=24)
    good, _ = F.fill_dem(z, method="priority-flood")
    good4, _ = F.fill_dem(z, method="priority-flood", conn=4)
    good_hole, _ = F.fill_dem(hole, method="priority-flood")
    real = F._pf_library

    def boom(*_a, **_k):
        raise mfb.BackendError("no library")

    F._pf_library = boom
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


@test
def condition_with_priority_flood_matches_the_relaxation():
    """The carve list and the conditioned array do not move on the synthetic cases."""
    _need_cpu_build()
    grids = [
        ("pitted", cases.pitted_plane(ny=40, nx=56), 10.0),
        ("channel", cases.channel_with_barrier(ny=40, nx=80), 10.0),
        ("nested", cases.nested_basins()[0], 10.0),
        ("ringed", cases.nodata_ringed_pit(n=36), 10.0),
    ]
    for name, z, cell in grids:
        new, rec_n, info_n = C.condition(z, cell_m=cell, method="priority-flood")
        old, rec_o, info_o = C.condition(z, cell_m=cell, method="relax")
        assert info_n["fill_method"] == "priority-flood", info_n
        assert info_o["fill_method"] == "relax", info_o
        assert len(rec_n) == len(rec_o), name
        # crossings, cells, depths and refusals. spill_before_m is read off the filled surface,
        # so the float32 relaxation can differ in the last bits of a metre (measured 4.8e-7 m
        # on the channel case). That is the relaxation's rounding, not a different carve.
        for a, b in zip(rec_n, rec_o):
            assert a.keys() == b.keys(), name
            for k in a:
                if k == "spill_before_m":
                    assert abs(float(a[k]) - float(b[k])) <= 1e-3, (name, a[k], b[k])
                else:
                    assert a[k] == b[k], (name, k, a[k], b[k])
        assert np.array_equal(new, old, equal_nan=True), name


def _real_dem():
    env = os.environ.get("DAMSAFETY_PF_DEM")
    if env and os.path.isfile(env):
        return env
    for path in ("/tmp/dem10.tif", os.path.join(os.path.dirname(__file__), "dem10.tif")):
        if os.path.isfile(path):
            return path
    return None


def _read_dem(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise Skip("GDAL could not open {}".format(path))
    a = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    nd = ds.GetRasterBand(1).GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    return a


@test
def priority_flood_matches_relax_on_a_real_dem_when_one_is_present():
    """Opt-in. The 10 m Absaraka box (or DAMSAFETY_PF_DEM) must stay within 1 mm of the
    relaxation, with nothing over 1 cm. Absent file -> skip, not fail."""
    path = _real_dem()
    if not path:
        raise Skip("no real DEM (DAMSAFETY_PF_DEM or /tmp/dem10.tif)")
    try:
        from osgeo import gdal  # noqa: F401
    except ImportError:
        raise Skip("no GDAL to read the real DEM")
    _need_cpu_build()
    a = _read_dem(path)
    grids = [("native", a)]
    if min(a.shape) >= 6:
        grids.append(("every_third", a[::3, ::3]))
    for name, z in grids:
        new, inew = F.fill_dem(z, method="priority-flood", conn=4)
        old, _ = F.fill_dem(z, method="relax", backend="cpu", conn=4)
        assert inew["unreached"] == 0, (name, inew)
        d = np.abs(new - old)
        dmax = float(np.nanmax(d))
        over = int(np.nansum(d > 0.01))
        assert dmax <= 1e-3, (name, dmax)
        assert over == 0, (name, over, dmax)


if __name__ == "__main__":
    import sys
    import traceback
    fails = 0
    fns = (
        priority_flood_matches_the_d8_oracle_bit_for_bit,
        priority_flood_matches_the_d4_oracle_bit_for_bit,
        priority_flood_stays_within_the_relaxation_bound,
        priority_flood_is_deterministic,
        priority_flood_diagonal_nodata_is_an_outlet_only_for_d8,
        priority_flood_ramps_a_flat_from_the_nearest_outlet,
        priority_flood_handles_a_single_row_and_a_single_column,
        priority_flood_python_fallback_matches_when_the_library_cannot_load,
        condition_with_priority_flood_matches_the_relaxation,
        priority_flood_matches_relax_on_a_real_dem_when_one_is_present,
    )
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
