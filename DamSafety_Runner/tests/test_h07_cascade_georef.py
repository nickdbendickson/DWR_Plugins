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


def _run(grid, depth):
    """A fake cascade run: a grid and its final SI arrays (only depth carries a feature)."""
    si = {"depth": depth,
          "velocity": np.zeros_like(depth),
          "wse": np.full_like(depth, -np.inf),
          "arrival": np.full_like(depth, np.inf)}
    return types.SimpleNamespace(grid=grid, final_si=si, cascade_offset_h=0.0)


@test
def h07_cascade_combine_places_each_run_on_its_true_ground():
    # Coarse run (20 m): world x[0,200] y[0,200]; a 3 m feature at run-cell (9, 9) -> world
    # centre (190, 10), which the fine run does not cover.
    coarse_g = Grid(np.zeros((10, 10)), x0=0.0, y0=200.0, dx=20.0, crs_wkt="EPSG:26914")
    coarse_d = np.zeros((10, 10))
    coarse_d[9, 9] = 3.0
    # Fine run (10 m): world x[0,100] y[0,100]; a 5 m feature at run-cell (0, 9) -> world
    # centre (95, 95).
    fine_g = Grid(np.zeros((10, 10)), x0=0.0, y0=100.0, dx=10.0, crs_wkt="EPSG:26914")
    fine_d = np.zeros((10, 10))
    fine_d[0, 9] = 5.0

    captured = {}

    def writer(grid, path):
        captured[path.rsplit("/", 1)[-1]] = grid

    out = types.SimpleNamespace(
        runs=[(None, _run(coarse_g, coarse_d)), (None, _run(fine_g, fine_d))],
        folder="/tmp/_h07", combined={})
    cascade._combine(out, types.SimpleNamespace(), writer)

    base = out.combined_grid
    # the base cell is the finest run cell, and the extent is the true union of both runs
    assert base.dx == 10.0, base.dx
    assert (base.x0, base.y0, base.nx, base.ny) == (0.0, 200.0, 20, 20), \
        (base.x0, base.y0, base.nx, base.ny)

    depth = captured["Cascade Depth (Max).tif"].data   # feet, NaN where dry

    def at(x, y):
        r, c = base.rc(x, y)
        return depth[r, c]

    # the fine feature lands at its real-world centre (95, 95), in feet
    assert abs(at(95.0, 95.0) - 5.0 / FT) < 1e-6, at(95.0, 95.0)
    # the coarse feature (190, 10) lands on its true ground, upsampled onto the 10 m base
    assert abs(at(190.0, 10.0) - 3.0 / FT) < 1e-6, at(190.0, 10.0)
    # the old bug drew the fine feature half-scale near (190, 90): that ground must be dry now
    assert not np.isfinite(at(190.0, 90.0)), at(190.0, 90.0)


@test
def h07_cascade_combine_equal_cells_unchanged():
    # Two equal-cell (10 m) runs on offset extents: the combine still max-envelopes correctly
    # (the equal-cell path must behave as it always did).
    a_g = Grid(np.zeros((8, 8)), x0=0.0, y0=80.0, dx=10.0, crs_wkt="EPSG:26914")
    a_d = np.zeros((8, 8))
    a_d[1, 1] = 2.0                                   # world centre (15, 65)
    b_g = Grid(np.zeros((8, 8)), x0=40.0, y0=120.0, dx=10.0, crs_wkt="EPSG:26914")
    b_d = np.zeros((8, 8))
    b_d[6, 6] = 4.0                                   # world centre (105, 55)

    captured = {}
    out = types.SimpleNamespace(
        runs=[(None, _run(a_g, a_d)), (None, _run(b_g, b_d))],
        folder="/tmp/_h07b", combined={})
    cascade._combine(out, types.SimpleNamespace(),
                     lambda g, p: captured.__setitem__(p.rsplit("/", 1)[-1], g))
    base = out.combined_grid
    assert base.dx == 10.0
    depth = captured["Cascade Depth (Max).tif"].data

    def at(x, y):
        r, c = base.rc(x, y)
        return depth[r, c]

    assert abs(at(15.0, 65.0) - 2.0 / FT) < 1e-6, at(15.0, 65.0)
    assert abs(at(105.0, 55.0) - 4.0 / FT) < 1e-6, at(105.0, 55.0)
