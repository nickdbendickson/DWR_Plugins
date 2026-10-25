"""3.21.1: 'Import HEC-RAS 2D results as rasters' - depth on the terrain of each grid cell.

Before, depth was the cell's maximum water surface minus the cell's MINIMUM elevation over the
whole cell, so a 30 ft cell with a ditch through it was ditch-deep over its dry banks too, and
the arrival was the first time the ditch bottom took water. With the terrain given, depth is
WSE minus the terrain under each grid cell, the wet mask and the arrival use that same depth,
and the basis (terrain / cell_min) is recorded. A 3-cell synthetic plan through the stand-in
HDF reader of test_hecras (no h5py, no GDAL, no solver)."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import npy_writer
from nd_breach_runner.tests.test_hecras import (GEO, OUT, SUMM, TS, WKT, _FakeH5, _Patch,
                                                _algs)

A = "Perimeter 1"
T_HR = np.arange(5.0)                                    # output every hour, 0-4 h
# cells: x = 15 / 45 / 75 ft (30 ft apart), y = 15; the middle one holds a ditch
ELEV = np.array([100.0, 90.0, 100.0])                    # cell minimum (the ditch bottom)
WSE_MAX = np.array([100.0, 95.5, 100.0])                 # only the middle cell takes water
# grid: 9 x 1 at 10 ft from x = 0; the middle cell covers pixels 3-5
TERRAIN = np.array([[100.0, 100.0, 100.0, 94.0, 90.0, 96.0, 100.0, 100.0, 100.0]])
X0, YTOP, DX, NX, NY = 0.0, 20.0, 10.0, 9, 1


def _plan():
    x, y = np.array([15.0, 45.0, 75.0]), np.full(3, 15.0)
    frac = np.clip(T_HR / 2.0, 0.0, 1.0)[:, None]        # the water rises over 2 h
    wse_t = ELEV[None, :] + (WSE_MAX - ELEV)[None, :] * frac
    g, s = GEO.format(A), SUMM.format(A)
    ft = {"Units": np.bytes_(b"ft")}
    L = {g + "Cells Center Coordinate": (np.column_stack([x, y]), {}),
         g + "Faces Cell Indexes": (np.array([[0, 1], [1, 2]], dtype=np.int32), {}),
         g + "Cells Minimum Elevation": (ELEV.copy(), ft),
         s + "Maximum Water Surface": (np.vstack([WSE_MAX, np.full(3, 2.0 / 24.0)]), ft),
         s + "Maximum Face Velocity": (np.vstack([[3.0, 3.0], [0.0, 0.0]]),
                                       {"Units": np.bytes_(b"ft/s")}),
         TS + "Time": (T_HR / 24.0, {"Time": np.bytes_(b"Days")}),
         TS + "2D Flow Areas/{}/Water Surface".format(A): (wse_t, ft)}
    root = {"Projection": np.bytes_(WKT.encode()), "Units System": "US Customary",
            "File Type": "HEC-RAS Results"}
    return _FakeH5(L, root)


def _fin(a):
    return np.isfinite(a[0])


@test
def v3211_hecras_ditch_cell_depth_and_arrival_on_the_terrain():
    f = _plan()
    out, info = hecras.terrain_depth(f, X0, YTOP, DX, NX, NY, TERRAIN, arrival_depth_ft=1.0)
    d, arr = out["depth"][0], out["arrival"][0]
    assert info["depth_basis"] == hecras.DEPTH_BASIS_TERRAIN
    # only the ditch (4) and the low bank (3) are wet; the high bank (5) stays dry
    assert list(np.flatnonzero(_fin(out["depth"]))) == [3, 4], d
    assert abs(d[3] - 1.5) < 1e-9 and abs(d[4] - 5.5) < 1e-9
    # wet mask and arrival on the same depth: arrival exactly where depth >= 1 ft
    assert list(np.flatnonzero(_fin(out["arrival"]))) == [3, 4]
    assert abs(arr[4] - 2.0 / 5.5) < 1e-9                # 90 + 5.5 t / 2 >= 91
    assert abs(arr[3] - 10.0 / 5.5) < 1e-9               # ... >= 95 (bank at 94)
    for k in ("wse", "velocity", "time_of_max"):
        assert list(np.flatnonzero(_fin(out[k]))) == [3, 4], k
    # the old basis on the same grid: the whole cell 5.5 ft deep, arrival from the ditch
    old, oinfo = hecras.import_on_grid(_plan(), X0, YTOP, DX, NX, NY, arrival_depth_ft=1.0)
    assert oinfo["depth_basis"] == hecras.DEPTH_BASIS_CELL_MIN
    assert hecras.CELL_MIN_WARNING in oinfo["notes"]
    assert list(np.flatnonzero(_fin(old["depth"]))) == [3, 4, 5]
    assert np.allclose(old["depth"][0][3:6], 5.5)
    assert np.allclose(old["arrival"][0][3:6], 2.0 / 5.5)
    # import_on_grid with the terrain = terrain_depth, recorded as terrain
    new, ninfo = hecras.import_on_grid(_plan(), X0, YTOP, DX, NX, NY, terrain_ft=TERRAIN,
                                       arrival_depth_ft=1.0)
    assert ninfo["depth_basis"] == "terrain" and "old" not in ninfo
    assert hecras.CELL_MIN_WARNING not in ninfo["notes"]
    np.testing.assert_allclose(np.nan_to_num(new["depth"], nan=-1),
                               np.nan_to_num(out["depth"], nan=-1))


@test
def v3211_hecras_terrain_gaps_and_between_step_arrival():
    # no terrain under the ditch pixel: the cell minimum is used there, and counted
    t = TERRAIN.copy()
    t[0, 4] = np.nan
    out, info = hecras.terrain_depth(_plan(), X0, YTOP, DX, NX, NY, t, arrival_depth_ft=1.0)
    assert abs(out["depth"][0][4] - 5.5) < 1e-9 and info["no_terrain_cells"] == 1
    assert any("no terrain" in n for n in info["notes"])
    # a bank whose maximum depth reaches the arrival depth only between output steps
    # (series peak below the maximum): arrival = the cell's time of the maximum (2 h)
    f = _plan()
    p = TS + "2D Flow Areas/{}/Water Surface".format(A)
    ts, at = f.layout[p]
    f.layout[p] = (np.minimum(ts, 94.8), at)
    t = TERRAIN.copy()
    t[0, 3] = 94.3                                       # max depth 1.2, series 0.5
    out, info = hecras.terrain_depth(f, X0, YTOP, DX, NX, NY, t, arrival_depth_ft=1.0)
    assert abs(out["arrival"][0][3] - 2.0) < 1e-9
    assert any("between output steps" in n for n in info["notes"])
    # per-point first reach = brute force
    rng = np.random.default_rng(3)
    n, m = 4, 25
    series = rng.uniform(0.0, 3.0, (6, n)).cumsum(axis=0)
    th = np.arange(6.0)
    cols = rng.integers(0, n, m)
    ground = rng.uniform(0.0, 4.0, m)
    got = hecras._first_reach(series, th, ground, 1.0, n, cols=cols, rows=2)
    for i in range(m):
        dd = series[:, cols[i]] - ground[i]
        hit = np.flatnonzero(dd >= 1.0)
        if not len(hit):
            assert np.isnan(got[i])
            continue
        j = hit[0]
        exp = th[0] if j == 0 else th[j - 1] + (1.0 - dd[j - 1]) / (dd[j] - dd[j - 1]) * (
            th[j] - th[j - 1])
        assert abs(got[i] - exp) < 1e-9, (i, got[i], exp)


@test
def v3211_hecras_import_algorithm_terrain_parameter_and_depth_basis():
    h, fq = _algs()
    d = tempfile.mkdtemp()
    like = Grid(np.zeros((NY, NX)), X0, YTOP, DX, crs_wkt=WKT)
    plan = _plan()

    class _Layer:
        def source(self):
            return "terrain.tif"
    seen = []

    def read_like(p, g, alg="bilinear"):
        seen.append((p, alg, (g.ny, g.nx)))
        return TERRAIN.copy()
    try:
        with _Patch((hecras, "_open_hdf", lambda p: (plan, lambda: None)),
                    (h.gdal_io, "GeoTiffWriter", lambda crs: npy_writer),
                    (h.gdal_io, "read_grid", lambda p: like),
                    (h.gdal_io, "read_like", read_like),
                    (h._ToolBase, "_need_gdal", lambda self: None)):
            a = h.HecRasImportAlgorithm()
            a.initAlgorithm()
            assert "terrain" in a.shortHelpString().lower()
            # with the terrain: depth on it, terrain basis recorded
            out = os.path.join(d, "t")
            fb = fq.QgsProcessingFeedback()
            r = a.processAlgorithm({"HDF": "run.p01.hdf", "OUT_FOLDER": out, "LIKE": _Layer(),
                                    "TERRAIN": _Layer(), "ARRIVAL_FT": 1.0},
                                   fq.QgsProcessingContext(), fb)
            assert seen and seen[-1][1] == "average"
            dep = np.load(os.path.join(out, "HECRAS Depth (Max).npy"))
            arr = np.load(os.path.join(out, "HECRAS Arrival Time.npy"))
            assert list(np.flatnonzero(np.isfinite(dep[0]))) == [3, 4]
            assert list(np.flatnonzero(np.isfinite(arr[0]))) == [3, 4]
            rec = json.load(open(os.path.join(out, hecras.DEPTH_BASIS_JSON)))
            assert rec["depth_basis"] == "terrain" and rec["terrain"].endswith("terrain.tif")
            assert "depth basis terrain" in r["RESULT"]
            assert not any(hecras.CELL_MIN_WARNING in e for e in fb.errors)
            # without: the cell minimum, warned and recorded
            out = os.path.join(d, "c")
            fb = fq.QgsProcessingFeedback()
            r = a.processAlgorithm({"HDF": "run.p01.hdf", "OUT_FOLDER": out, "LIKE": _Layer(),
                                    "ARRIVAL_FT": 1.0}, fq.QgsProcessingContext(), fb)
            dep = np.load(os.path.join(out, "HECRAS Depth (Max).npy"))
            assert list(np.flatnonzero(np.isfinite(dep[0]))) == [3, 4, 5]
            rec = json.load(open(os.path.join(out, hecras.DEPTH_BASIS_JSON)))
            assert rec["depth_basis"] == "cell_min" and rec["terrain"] is None
            assert "depth basis cell_min" in r["RESULT"]
            assert any(hecras.CELL_MIN_WARNING in e for e in fb.errors)
    finally:
        shutil.rmtree(d)
