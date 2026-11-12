"""3.16.10 (audit P3, 2026-11-07): lisflood.read_results returns float32 depth / velocity /
arrival / WSE grids (LISFLOOD-FP writes them to 3 decimals from single-precision arrays, so
float32 keeps every digit of the files and halves four full-domain arrays), while the
elevation grids the models read stay float64. The statistics and the wet / dry rules are
checked against a float64 read of the same files; the fixed built-in-engine case of
test_golden_baseline supplies the files."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test


def _case():
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    nr, nc, dx = 20, 100, 20.0
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    dem = Grid(z[::-1], 0.0, nr * dx, dx)
    hy = Hydrograph(np.array([0.0, 600.0, 1200.0]), np.array([30.0, 30.0, 0.0]), "test")
    st = lf.LisfloodSettings(lf.BUILTIN_EXE, arrival_depth_m=0.3, sim_time_s=1200.0,
                             massint_s=60.0, initial_tstep_s=1.0)
    wd = tempfile.mkdtemp(prefix="ndbr_f32_")
    par = lf.write_inputs(wd, dem, (30.0, nr * dx / 2 + 5), hy, st)
    lf.run(wd, par, st)
    return wd, dem, st, hy


@test
def read_results_gives_float32_results_and_the_same_statistics():
    from nd_breach_runner.core import lisflood as lf
    wd, dem, st, hy = _case()
    try:
        assert dem.data.dtype == np.float64                     # elevations: unchanged
        env = lf.read_results(wd, dem, st, hy)
        for k in ("depth", "velocity", "arrival_hr", "wse"):
            g = getattr(env, k)
            assert g.data.dtype == np.float32, (k, g.data.dtype)
            assert g.data.shape == dem.data.shape
            assert g.dx == dem.dx and g.x0 == dem.x0 and g.y0 == dem.y0
        # the float64 read of the same files (the dtype is a module constant)
        old = lf.RESULT_DTYPE
        lf.RESULT_DTYPE = np.float64
        try:
            env64 = lf.read_results(wd, dem, st, hy)
        finally:
            lf.RESULT_DTYPE = old
        assert env64.depth.data.dtype == np.float64
        for k in ("depth", "velocity", "arrival_hr", "wse"):
            a, b = getattr(env, k).data, getattr(env64, k).data
            assert np.array_equal(np.isfinite(a), np.isfinite(b)), k     # the same wet cells
            m = np.isfinite(a)
            if m.any():
                tol = 1e-4 * max(1.0, float(np.nanmax(np.abs(b))))     # float32 of a 100 m WSE
                assert np.max(np.abs(a[m].astype(np.float64) - b[m])) <= tol, k
        skip = ("max_velocity_xy",)
        for key, v64 in env64.stats.items():
            if key in skip or not isinstance(v64, (int, float)) or isinstance(v64, bool):
                assert env.stats.get(key) == v64, (key, env.stats.get(key), v64)
                continue
            v32 = env.stats[key]
            assert abs(float(v32) - float(v64)) <= 1e-3 * max(1.0, abs(float(v64))), (
                key, v32, v64)
        assert env.stats["flooded_acres"] == env64.stats["flooded_acres"]
        assert env.notes == env64.notes
        # the result grid's wet rule works as before: depth at the arrival depth counts
        wet32 = np.isfinite(env.arrival_hr.data)
        wet64 = np.isfinite(env64.arrival_hr.data)
        assert np.array_equal(wet32, wet64)
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@test
def final_arrays_and_products_accept_float32_results():
    """workflow._final_arrays / _to_us on float32 envelopes: dtype kept (no silent promotion
    back to float64), NaN where dry, feet where metres were."""
    from nd_breach_runner.core import lisflood as lf, workflow
    wd, dem, st, hy = _case()
    try:
        env = lf.read_results(wd, dem, st, hy)
        fa = workflow._final_arrays(env, st.arrival_depth_m, 0.0, None)
        for k, a in fa.items():
            assert a.dtype == np.float32, (k, a.dtype)
            us = workflow._to_us(k, a)
            assert us.dtype == np.float32
            if k in ("depth", "velocity", "wse"):
                m = np.isfinite(a)
                assert np.allclose(us[m], a[m] / 0.3048, rtol=1e-6)
        d = fa["depth"]
        assert np.all(d[np.isfinite(d)] > workflow.WET_M)
        # a Grid of them keeps float32 when asked and defaults to float64 for the writers
        g32 = dem.like(fa["depth"], np.nan, dtype=np.float32)
        g64 = dem.like(fa["depth"], np.nan)
        assert g32.data.dtype == np.float32 and g64.data.dtype == np.float64
        # the grid helpers work on it
        wet = g32.valid()
        assert wet.dtype == bool and wet.sum() > 0
        m = g32.masked()
        assert m.dtype == np.float32
    finally:
        shutil.rmtree(wd, ignore_errors=True)


@test
def read_asc_dtype_round_trip():
    from nd_breach_runner.core.grid import Grid
    d = tempfile.mkdtemp(prefix="ndbr_asc_")
    try:
        g = Grid(np.array([[1.234, -9999.0], [0.001, 500.123]]), 0.0, 20.0, 10.0)
        p = os.path.join(d, "a.asc")
        g.write_asc(p)
        r64 = Grid.read_asc(p)
        r32 = Grid.read_asc(p, dtype=np.float32)
        assert r64.data.dtype == np.float64 and r32.data.dtype == np.float32
        assert r32.nodata == -9999.0 and np.float32(r32.nodata) == r32.data[0, 1]
        assert abs(float(r32.data[1, 1]) - 500.123) < 1e-4 and float(r32.data[1, 0]) == np.float32(0.001)
    finally:
        shutil.rmtree(d)
