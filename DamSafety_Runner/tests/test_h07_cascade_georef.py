"""H07 (cascade georef): core/cascade._combine must draw each run on its true ground.

Before the fix _combine used the first run's cell size for every run, so a dam that
auto-refines to a finer cell than another run was placed at half scale (and the union was
truncated) on the four combined Cascade rasters - the EAP map deliverables. These rasters feed
no hazard class (each dam's class comes from its own run), but they are life-safety maps, so a
feature must land at its real-world location whatever cell size its run used.
"""
import types

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import cascade
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT


def _run(grid, feats):
    """A fake cascade run: a grid and its final SI arrays. `feats` maps (row, col) -> a dict of
    the SI values to place there (depth / velocity in m, m/s; wse in m; arrival in h)."""
    n = (grid.data.shape[0], grid.data.shape[1])
    si = {"depth": np.zeros(n), "velocity": np.zeros(n),
          "wse": np.full(n, -np.inf), "arrival": np.full(n, np.inf)}
    for (r, c), v in feats.items():
        for k, val in v.items():
            si[k][r, c] = val
    return types.SimpleNamespace(grid=grid, final_si=si, cascade_offset_h=0.0)


def _combine(runs):
    captured = {}
    out = types.SimpleNamespace(runs=[(None, r) for r in runs], folder="/tmp/_h07", combined={})
    cascade._combine(out, types.SimpleNamespace(),
                     lambda g, p: captured.__setitem__(p.rsplit("/", 1)[-1], g))
    return out.combined_grid, captured


@test
def h07_cascade_combine_places_each_run_on_its_true_ground():
    # Coarse run (20 m): world x[0,200] y[0,200]; a feature at run-cell (9, 9) -> world (190, 10),
    # which the fine run does not cover.
    coarse_g = Grid(np.zeros((10, 10)), x0=0.0, y0=200.0, dx=20.0, crs_wkt="EPSG:26914")
    coarse = _run(coarse_g, {(9, 9): {"depth": 3.0, "velocity": 1.0, "wse": 300.0, "arrival": 4.0}})
    # Fine run (10 m): world x[0,100] y[0,100]; a feature at run-cell (0, 9) -> world (95, 95),
    # carrying all four SI quantities so the shared gr/gc indexing is checked for each output.
    fine_g = Grid(np.zeros((10, 10)), x0=0.0, y0=100.0, dx=10.0, crs_wkt="EPSG:26914")
    fine = _run(fine_g, {(0, 9): {"depth": 5.0, "velocity": 2.0, "wse": 560.0, "arrival": 1.5}})

    base, captured = _combine([coarse, fine])
    assert base.dx == 10.0, base.dx
    assert (base.x0, base.y0, base.nx, base.ny) == (0.0, 200.0, 20, 20), \
        (base.x0, base.y0, base.nx, base.ny)

    def at(name, x, y):
        r, c = base.rc(x, y)
        return captured[name].data[r, c]

    # the fine feature lands at its real-world centre (95, 95) in every combined raster
    assert abs(at("Cascade Depth (Max).tif", 95.0, 95.0) - 5.0 / FT) < 1e-6
    assert abs(at("Cascade Velocity (Max).tif", 95.0, 95.0) - 2.0 / FT) < 1e-6
    assert abs(at("Cascade WSE (Max).tif", 95.0, 95.0) - 560.0 / FT) < 1e-6
    assert abs(at("Cascade Arrival Time.tif", 95.0, 95.0) - 1.5) < 1e-6   # hours, not /FT
    # the coarse feature (190, 10) lands on its true ground, upsampled onto the 10 m base
    assert abs(at("Cascade Depth (Max).tif", 190.0, 10.0) - 3.0 / FT) < 1e-6
    assert abs(at("Cascade Arrival Time.tif", 190.0, 10.0) - 4.0) < 1e-6
    # the old bug drew the fine feature half-scale near (190, 90): that ground must be dry now,
    # and the never-covered -inf WSE / +inf arrival sentinels must not leak into a raster
    assert not np.isfinite(at("Cascade Depth (Max).tif", 190.0, 90.0))
    assert not np.isfinite(at("Cascade WSE (Max).tif", 190.0, 90.0))
    assert not np.isfinite(at("Cascade Arrival Time.tif", 190.0, 90.0))


@test
def h07_cascade_combine_three_cells_fine_first():
    # Three cell sizes (20 / 10 / 5 m), finest run listed FIRST (the reverse ordering the old code
    # over-drew 2x). Base = min dx = 5 m; each feature still lands on its true ground.
    fine = _run(Grid(np.zeros((20, 20)), x0=0.0, y0=100.0, dx=5.0, crs_wkt="EPSG:26914"),
                {(0, 0): {"depth": 6.0}})                      # world centre (2.5, 97.5)
    mid = _run(Grid(np.zeros((10, 10)), x0=0.0, y0=100.0, dx=10.0, crs_wkt="EPSG:26914"),
               {(9, 0): {"depth": 4.0}})                       # world centre (5, 5)
    coarse = _run(Grid(np.zeros((10, 10)), x0=100.0, y0=200.0, dx=20.0, crs_wkt="EPSG:26914"),
                  {(0, 9): {"depth": 2.0}})                    # world centre (290, 190)
    base, captured = _combine([fine, mid, coarse])
    assert base.dx == 5.0, base.dx
    d = captured["Cascade Depth (Max).tif"].data

    def at(x, y):
        r, c = base.rc(x, y)
        return d[r, c]

    assert abs(at(2.5, 97.5) - 6.0 / FT) < 1e-6, at(2.5, 97.5)
    assert abs(at(5.0, 5.0) - 4.0 / FT) < 1e-6, at(5.0, 5.0)
    assert abs(at(290.0, 190.0) - 2.0 / FT) < 1e-6, at(290.0, 190.0)


@test
def h07_cascade_combine_equal_cells_unchanged():
    # Two equal-cell (10 m) runs on offset extents: the combine still max-envelopes correctly
    # (the equal-cell path must behave as it always did).
    a = _run(Grid(np.zeros((8, 8)), x0=0.0, y0=80.0, dx=10.0, crs_wkt="EPSG:26914"),
             {(1, 1): {"depth": 2.0}})                         # world centre (15, 65)
    b = _run(Grid(np.zeros((8, 8)), x0=40.0, y0=120.0, dx=10.0, crs_wkt="EPSG:26914"),
             {(6, 6): {"depth": 4.0}})                         # world centre (105, 55)
    base, captured = _combine([a, b])
    assert base.dx == 10.0
    d = captured["Cascade Depth (Max).tif"].data

    def at(x, y):
        r, c = base.rc(x, y)
        return d[r, c]

    assert abs(at(15.0, 65.0) - 2.0 / FT) < 1e-6, at(15.0, 65.0)
    assert abs(at(105.0, 55.0) - 4.0 / FT) < 1e-6, at(105.0, 55.0)
