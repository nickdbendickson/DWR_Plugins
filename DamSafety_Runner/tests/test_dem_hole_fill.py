"""3.58.2 (batch 44, DWR-2631): enclosed DEM no-data holes the flood reaches are filled from the
ground around them before a run is called "held up by a hole".

Nick's run1118 put DWR-2631 (Short Creek Dam 1, 38 ft, 2,765 ac-ft, Significant of record) at
Low / Low: 28 wet cells sat on a 1 m LiDAR gap the one 3DEP refill did not close, and LISFLOOD-FP
treats no-data as high ground, so the gap was a wall across the valley. dem_refill.interior_holes /
fill_interior_holes fill a hole that is inside surveyed ground (it does not touch the grid's
border) and at most HOLE_FILL_MAX_AC acres; the workflow does it once and runs the same domain run
again. QGIS-free (numpy + scipy; the end-to-end test uses the built-in engine)."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import dem_refill as R
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer, valley

ND = -9999.0


def _plane(ny=40, nx=50, dx=10.0):
    X, Y = np.meshgrid(np.arange(nx) * dx, np.arange(ny) * dx)
    return Grid(100.0 + 0.01 * X - 0.02 * Y, 0.0, ny * dx, dx, nodata=ND)


@test
def hole_fill_interior_hole_meets_the_ground_around_it():
    g = _plane()
    truth = g.data.copy()
    d = g.data.copy()
    d[10:20, 15:30] = ND                                    # inside surveyed ground
    out, n = R.fill_interior_holes(g.like(d))
    assert n == 150, n
    assert out.valid().all()
    # a plane is harmonic: the Laplace fill recovers it (start = nearest ground, then sweeps)
    assert np.abs(out.data - truth).max() < 0.05, np.abs(out.data - truth).max()
    assert np.array_equal(out.data[~(d == ND)], truth[~(d == ND)])     # real cells untouched


@test
def hole_fill_leaves_the_dem_edge_and_big_holes():
    g = _plane()
    d = g.data.copy()
    d[:, :5] = ND                                           # the DEM's coverage ends: not a hole
    d[30:35, 30:35] = ND                                    # 25 cells = 0.62 ac at 10 m
    fill, left = R.interior_holes(g.like(d))
    assert fill.sum() == 25 and left.sum() == 40 * 5, (fill.sum(), left.sum())
    fill2, left2 = R.interior_holes(g.like(d), max_ac=0.5)    # the 0.62 ac hole is over the cap
    assert fill2.sum() == 0 and left2.sum() == 225
    out, n = R.fill_interior_holes(g.like(d))
    assert n == 25 and (~out.valid()).sum() == 200
    assert R.fill_interior_holes(g)[1] == 0                 # nothing to do: the same grid back


@test
def hole_fill_marker_in_model_dem():
    from nd_breach_runner.core import workflow

    class Holed(SyntheticTerrain):
        def read(self, bounds, cell):
            g = SyntheticTerrain.read(self, bounds, cell)
            g.data[5:8, 5:8] = g.nodata
            return g
    b = (0.0, -200.0, 400.0, 200.0)
    g0 = workflow.model_dem(Holed(), [], "lisflood", b, 20.0, [])
    g1 = workflow.model_dem(Holed(), [], "lisflood", b, 20.0, [R.HOLE_FILL])
    assert (~g0.valid()).sum() == 9 and g1.valid().all()


class _GapAcrossTheValley(SyntheticTerrain):
    """valley() with a LiDAR gap across the whole valley floor 600-700 m below the dam (|y| <
    500 m): enclosed by surveyed ground on every side, so it is not the DEM's outer edge."""

    def read(self, bounds, cell):
        g = SyntheticTerrain.read(self, bounds, cell)
        xs = bounds[0] + (np.arange(g.nx) + 0.5) * cell
        ys = bounds[3] - (np.arange(g.ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        g.data[(X > 600) & (X < 700) & (np.abs(Y) < 500)] = g.nodata
        return g


def _run(tmp, refill_fails=True):
    from nd_breach_runner.core import workflow
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core import lisflood as lf
    st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, half_width_mi=0.5,
                              max_iterations=1, lisflood_exe=lf.BUILTIN_EXE, threads=1)
    dm = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                    top_of_dam_ft=1850.0, surface_area_top_ac=60.0)
    orig = R.fetch_3dep

    def no_3dep(*a, **k):
        raise OSError("no network in the test")
    if refill_fails:
        R.fetch_3dep = no_3dep
    try:
        r = workflow.run_dam(dm, (0.0, 0.0), st, _GapAcrossTheValley(), npy_writer,
                             os.path.join(tmp, "T1"))
    finally:
        R.fetch_3dep = orig
    rep = json.load(open(r.report_json))
    depth = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
    js = json.load(open(os.path.splitext(r.final["depth"])[0] + ".json"))
    xs = js["x0"] + (np.arange(depth.shape[1]) + 0.5) * js["dx"]
    return r, rep, depth, xs


@test
def hole_fill_lets_the_flood_through_a_lidar_gap():
    tmp = tempfile.mkdtemp()
    try:
        r, rep, depth, xs = _run(tmp)
        warns = " ".join(rep.get("warnings") or [])
        assert "were filled from the ground around them" in warns, warns
        assert "held up by a hole" not in warns, warns
        below = np.nan_to_num(depth[:, xs > 800]) > 0.3          # metres, past the gap
        # 3.58.2 measured: 60 of 2,460 cells past the gap wet on the old code (around its ends),
        # 882 with the fill (the same as with no gap)
        assert below.sum() > 500, below.sum()
        runs = rep["lisflood_runs"]
        assert any(x.get("followed_by_dem_refill") for x in runs), runs
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
