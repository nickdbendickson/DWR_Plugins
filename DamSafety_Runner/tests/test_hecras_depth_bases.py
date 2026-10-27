"""3.10.92 review of #204: the three extra HEC-RAS depth bases (core/hecras_fine.py).

terrain_mean (max WSE minus the mean ground over each grid cell), terrain_max (the deepest
fine-terrain point of each grid cell, the nearest cell's WSE; conservative, earliest
arrival) and fine (the WSE interpolated between wet cell centres minus the fine terrain;
an approximation of RAS Mapper's sloped surface). Tiny synthetic meshes: a sloped cell, a
ditch narrower than a grid cell, a wet / dry edge, arrival, the datum check, the memory
limit and the re-export record. No solver, no GDAL (stand-ins)."""
import json
import os

import numpy as np

from nd_breach_runner.core import hecras, hecras_engine, hecras_fine, workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_hecras import _FakeH5
from nd_breach_runner.tests.test_hecras_depth_cellmin import _Env, _reexport_fixture, \
    _with_patches
from nd_breach_runner.tests.test_hecras_depth_terrain_default import _DatumEnv, _sloped
from nd_breach_runner.tests.test_hecras_terrain_depth import (
    AREA, CELL, DITCH_A, DITCH_B, DX, GEO, HIGH, NP, SUMM, TS, WKT, WSE_C, _fake, _terrain)

EXT = 3 * CELL                          # ft, the 3 x 3 mesh's side (ytop)


def _ground_fn(t, step=DX):
    """ground(x0, ytop, dx, nx, ny) of a (rows, cols) ft array `t` at `step` ft from (0,
    EXT): nearest sample at each point's centre, HIGH outside."""
    def ground(x0, ytop, dx, nx, ny):
        xs = x0 + (np.arange(nx) + 0.5) * dx
        ys = ytop - (np.arange(ny) + 0.5) * dx
        c = np.floor(xs / step).astype(int)
        r = np.floor((EXT - ys) / step).astype(int)
        R, C = np.meshgrid(r, c, indexing="ij")
        ok = (R >= 0) & (R < t.shape[0]) & (C >= 0) & (C < t.shape[1])
        out = np.full(R.shape, HIGH)
        out[ok] = t[R[ok], C[ok]]
        return out
    return ground


def _centres(ground, x0, ytop, dx, nx, ny):
    return ground(x0, ytop, dx, nx, ny)


def _mean_on(ground, x0, ytop, dx, nx, ny, k=3):
    f = ground(x0, ytop, dx / k, nx * k, ny * k)
    return f.reshape(ny, k, nx, k).mean(axis=(1, 3))


def _run(h5, basis, grid, ground, k=3, **fine):
    x0, ytop, dx, nx, ny = grid
    t = _mean_on(ground, *grid, k=k) if basis == "terrain_mean" else \
        _centres(ground, *grid)
    f = dict({"ground": ground, "k": k}, **fine) if basis in hecras.FINE_BASES else None
    return hecras.terrain_depth(h5, x0, ytop, dx, nx, ny, t, basis=basis, fine=f)


# --- a slope of WSE over two wet rows of cells, a dry row on top ------------------------
SLOPE_WSE = (94.0, 96.0, 98.0)          # ft, per mesh column (x = 75, 225, 375)
FLOOR = 90.0


def _slope_layout():
    """3 x 3 cells of 150 ft. Rows y = 75 and 225: wet, floor 90 ft, max WSE 94 / 96 / 98 ft
    west to east (rising from 90 over 2 h); row y = 375: dry high ground (WSE = 100 ft, its
    minimum), so no WSE may be interpolated towards it."""
    ii, jj = [a.ravel() for a in np.meshgrid(np.arange(3), np.arange(3))]
    x, y = 75.0 + CELL * ii, 75.0 + CELL * jj
    wet = jj < 2
    elev = np.where(wet, FLOOR, HIGH)
    wse = np.where(wet, np.array(SLOPE_WSE)[ii], HIGH)
    t_hr = np.arange(5.0)
    frac = np.clip(t_hr / 2.0, 0.0, 1.0)[:, None]
    wse_t = elev[None, :] + (wse - elev)[None, :] * frac
    OUT = {GEO + "Cells Center Coordinate": (np.column_stack([x, y]), {}),
           GEO + "Cells Minimum Elevation": (elev, {"Units": np.bytes_(b"ft")}),
           SUMM + "Maximum Water Surface": (np.vstack([wse, np.full(9, 2.0 / 24.0)]),
                                            {"Units": np.bytes_(b"ft")}),
           SUMM + "Maximum Cell Velocity": (np.where(wet, 2.0, 0.0),
                                            {"Units": np.bytes_(b"ft/s")}),
           TS + "Time": (t_hr / 24.0, {"Time": np.bytes_(b"Days")}),
           TS + "2D Flow Areas/{}/Water Surface".format(AREA): (wse_t,
                                                               {"Units": np.bytes_(b"ft")})}
    root = {"Projection": np.bytes_(WKT.encode()), "Units System": "US Customary",
            "File Type": "HEC-RAS Results"}
    return OUT, root


def _slope_ground():
    t = np.full((3 * NP, 3 * NP), FLOOR)
    t[:NP, :] = HIGH                   # rows 0..14 = y 300..450, the dry cells
    return _ground_fn(t)


G30 = (0.0, EXT, 30.0, 15, 15)          # 30 ft output grid, k = 3 -> 10 ft fine points


class _Writer:
    """A hecras_fine `rasters` factory keeping the fine arrays in memory."""

    def __init__(self):
        self.a = {}

    def __call__(self, x0w, ytopw, dxf, nxw, nyw):
        self.win = (x0w, ytopw, dxf, nxw, nyw)
        self.a = {k: np.full((nyw, nxw), np.nan) for k in hecras_fine.FINE_NAMES}
        me = self

        class W:
            def write(self, kind, a, xoff, yoff):
                a, xo, yo = hecras_fine.clip_window(a, xoff, yoff, nxw, nyw)
                me.a[kind][yo:yo + a.shape[0], xo:xo + a.shape[1]] = a

            def close(self):
                return {k: "/mem/" + n for k, n in hecras_fine.FINE_NAMES.items()}
        return W()


@test
def hecras_depth_bases_sloped_cell_order():
    """The centre cell slopes 90 -> 99 ft: cell_min >= terrain_max >= terrain per pixel,
    terrain_max >= terrain_mean; all five bases named in the record."""
    g = _ground_fn(_sloped())
    res = {}
    for b in hecras.DEPTH_BASES:
        with _Env(None), _DatumEnv(None):
            out, info = _run(_fake(), b, G30, g)
        assert info["depth_basis"] == b, info["depth_basis"]
        assert b in hecras.DEPTH_BASIS_TEXT
        res[b] = np.nan_to_num(out["depth"])
    assert np.all(res["cell_min"] >= res["terrain_max"] - 1e-9)
    assert np.all(res["terrain_max"] >= res["terrain"] - 1e-9)
    assert np.all(res["terrain_max"] >= res["terrain_mean"] - 1e-9)
    assert res["terrain_max"].sum() > res["terrain"].sum() > 0
    # one wet cell: 'fine' has no triangle to slope over, so it equals terrain_max
    assert np.allclose(res["fine"], res["terrain_max"])
    # terrain_mean: the 5 x 5 pixels of the centre cell, each its mean ground
    m = _mean_on(g, *G30)[5:10, 5:10]
    exp = np.where(WSE_C - m >= 0.01, WSE_C - m, 0.0)
    assert np.allclose(res["terrain_mean"][5:10, 5:10], exp)


@test
def hecras_depth_bases_narrow_channel():
    """A 20 ft ditch inside a 30 ft pixel whose centre is on high ground: 'terrain' and
    'terrain_mean' miss it, 'terrain_max' keeps its depth (5 ft) and arrives early."""
    grid = (-10.0, EXT, 30.0, 16, 15)        # pixels 200-230 and 230-260 split the ditch
    g = _ground_fn(_terrain())
    with _Env(None), _DatumEnv(None):
        t, _ = _run(_fake(), "terrain", grid, g)
        mx, info = _run(_fake(), "terrain_max", grid, g)
        mn, _ = _run(_fake(), "terrain_mean", grid, g)
    assert not np.isfinite(t["depth"]).any()
    col_a, col_b = 7, 8                          # x 200-230 (90 ft), 230-260 (93 ft)
    assert np.allclose(mx["depth"][5:10, col_a], WSE_C - DITCH_A)
    assert np.allclose(mx["depth"][5:10, col_b], WSE_C - DITCH_B)
    assert np.isfinite(mx["depth"]).sum() == 10
    # the mean ground of (100, 100, 90) and (93, 100, 100): (96.7, 97.7) above 95: dry
    assert not np.isfinite(mn["depth"]).any()
    # arrival: the first time any point of the pixel is 0.1 ft deep (WSE 90 -> 95 over 2 h)
    assert np.allclose(mx["arrival"][5:10, col_a], 0.1 / 5.0 * 2.0)
    assert np.allclose(mx["arrival"][5:10, col_b], (DITCH_B + 0.1 - DITCH_A) / 5.0 * 2.0)
    assert info["fine"]["fine_points_per_cell_side"] == 3 and info["fine"]["tiles"] >= 1


@test
def hecras_depth_fine_interpolates_between_wet_cells_not_into_dry():
    """'fine': WSE linear between the wet cell centres (94 -> 98 ft west to east); the strip
    between the wet row and the dry row is held flat at the nearest wet cell's WSE (not
    pulled up towards the dry cells' 100 ft); the dry row stays dry; the fine rasters at
    10 ft."""
    g = _slope_ground()
    w = _Writer()
    with _Env(None), _DatumEnv(None):
        fo, finfo = _run(_FakeH5(*_slope_layout()), "fine", G30, g, rasters=w)
        tr, _ = _run(_FakeH5(*_slope_layout()), "terrain", G30, g)
        tm, _ = _run(_FakeH5(*_slope_layout()), "terrain_max", G30, g)
    fi = finfo["fine"]
    assert fi["interpolated_points"] > 0 and fi["aggregate"] == "max"
    # rows 0..4 = y 300..450 are the dry cells: dry for every basis
    for o in (fo, tr, tm):
        assert not np.isfinite(o["depth"][:5]).any()
    # inside the hull of the wet centres (x 75..375, y 75..225): the max of the 3 points
    # per pixel side = the WSE at the pixel's last point (x = right edge - 5 ft)
    def wse_at(x):
        return SLOPE_WSE[0] + (x - 75.0) * (SLOPE_WSE[1] - SLOPE_WSE[0]) / CELL
    rows = slice(8, 12)                          # y 90..210
    cols = np.arange(3, 12)                      # x 90..360
    exp = wse_at(30.0 * cols + 25.0) - FLOOR
    assert np.allclose(fo["depth"][rows, cols], exp[None, :], atol=1e-6)
    # smooth: no 2 ft step at the cell faces as in 'terrain' (nearest cell, flat)
    assert np.nanmax(np.abs(np.diff(fo["depth"][9, 3:12]))) < 0.5
    assert np.nanmax(np.abs(np.diff(tr["depth"][9, 3:12]))) >= 2.0 - 1e-9
    # y 240..300 (rows 5, 6): nearest wet cell, flat: the terrain / terrain_max depth
    assert np.isfinite(fo["depth"][5:7]).all()
    assert np.allclose(fo["depth"][5:7], tm["depth"][5:7])
    # the fine rasters: 10 ft, WSE only where wet, never on the dry cells
    x0w, ytopw, dxf, nxw, nyw = w.win
    assert dxf == 10.0
    wf, df = w.a["wse"], w.a["depth"]
    ys = ytopw - (np.arange(nyw) + 0.5) * dxf
    assert not np.isfinite(wf[ys > 300.0]).any()
    assert np.nanmax(wf) <= max(SLOPE_WSE) + 1e-9 and np.nanmin(wf) >= min(SLOPE_WSE) - 1e-9
    assert np.allclose(df[np.isfinite(df)], wf[np.isfinite(df)] - FLOOR)
    assert fi["rasters"]["depth"].endswith("Depth_fine (Max).tif")
    assert fi["fine_raster_shape"] == [nyw, nxw]


@test
def hecras_depth_fine_mean_aggregate_and_arrival():
    """NDBREACH_HECRAS_FINE_AGG=mean: the mean of the pixel's points (<= max); the fine
    arrival is that of the pixel's deepest point on the interpolated series."""
    g = _slope_ground()
    with _Env(None), _DatumEnv(None):
        mx, _ = _run(_FakeH5(*_slope_layout()), "fine", G30, g)
        mn, info = _run(_FakeH5(*_slope_layout()), "fine", G30, g, agg="mean")
    assert info["fine"]["aggregate"] == "mean"
    ok = np.isfinite(mx["depth"]) & np.isfinite(mn["depth"])
    assert ok.sum() > 0 and np.all(mn["depth"][ok] <= mx["depth"][ok] + 1e-9)
    assert np.nansum(mn["depth"]) < np.nansum(mx["depth"])
    # pixel row 9, col 6 (x 180-210): its deepest point x = 205, depth d = 4 + 130*2/150 ft,
    # reached linearly over 2 h: 0.1 ft at 0.2 / d h
    d = SLOPE_WSE[0] - FLOOR + 130.0 * 2.0 / CELL
    assert abs(mx["depth"][9, 6] - d) < 1e-6
    assert abs(mx["arrival"][9, 6] - 0.1 / d * 2.0) < 1e-6
    os.environ[hecras_fine.FINE_AGG_ENV] = "median"
    try:
        hecras_fine.fine_agg()
        raise AssertionError("accepted median")
    except hecras.HecRasError as e:
        assert "max" in str(e) and "mean" in str(e)
    finally:
        os.environ.pop(hecras_fine.FINE_AGG_ENV, None)


@test
def hecras_depth_new_bases_run_the_datum_check_and_the_limit():
    g = _ground_fn(_sloped())
    low = _ground_fn(_sloped() - 3.0)
    for b in ("terrain_mean", "terrain_max", "fine"):
        with _Env(None), _DatumEnv(None):
            try:
                _run(_fake(), b, G30, low)
                raise AssertionError(b + " accepted a terrain 3 ft low")
            except hecras.DatumMismatch as e:
                assert "MISMATCH" in str(e), e
        with _Env(None), _DatumEnv("warn"):
            out, info = _run(_fake(), b, G30, low)
        assert info["depth_basis"] == b and info["datum_check"]["status"] == "mismatch"
    for b in hecras.FINE_BASES:
        with _Env(None), _DatumEnv(None):
            try:
                _run(_fake(), b, G30, g, limit=10)
                raise AssertionError("no memory guard")
            except hecras.HecRasError as e:
                assert "more than 10" in str(e) and hecras_fine.FINE_MAX_MPX_ENV in str(e)
    # without a fine ground the engine falls back to 'terrain' and says so
    like = Grid(np.zeros((15, 15)), *G30[:3], crs_wkt=WKT)
    info = {}
    with _Env(None), _DatumEnv(None):
        hecras_engine.results(_fake(), like, terrain=_centres(g, *G30), info=info,
                              depth_basis="fine")
    assert info["depth_basis"] == "terrain"
    assert any("'fine' was asked for" in n for n in info["notes"])
    assert hecras.depth_basis("RAS_Mapper") == "fine"
    for bad in ("average", "max", "mean"):
        try:
            hecras.depth_basis(bad)
            raise AssertionError("accepted " + bad)
        except hecras.HecRasError as e:
            assert "terrain_max" in str(e)
    assert hecras_fine.sub_factor(30.0, 1.0) == 30 and hecras_fine.sub_factor(30.0, None) == 1
    assert hecras_fine.sub_factor(1000.0, 1.0) == hecras_fine.FINE_K_MAX


def _geo_read_like(t):
    """gdal_io.read_like stand-in that samples ground `t` (ft, 10 ft) by the target grid's
    geometry: nearest at the cell centre, or the mean of 3 x 3 points ('average')."""
    g = _ground_fn(t)

    def read_like(p, grid, alg="bilinear"):
        ny, nx = grid.data.shape
        if alg == "average":
            return _mean_on(g, grid.x0, grid.y0, grid.dx, nx, ny) * FT
        return g(grid.x0, grid.y0, grid.dx, nx, ny) * FT
    return read_like


@test
def hecras_reexport_records_each_new_basis():
    """hecras-reexport with terrain_mean / terrain_max / fine: depth_basis, the terrain
    sample, the fine record (and the fine rasters for 'fine', skippable) in reexport.json."""
    def body(tmp, patch):
        from nd_breach_runner.core import gdal_io
        run = _reexport_fixture(tmp, patch)
        patch(gdal_io, "read_like", _geo_read_like(_sloped()))
        patch(hecras_engine, "_cell_m", lambda p: 10.0 * FT)
        made = []

        def fake_rasters(folder, crs):
            w = _Writer()
            made.append((folder, w))
            return w
        patch(hecras_fine, "gdal_rasters", fake_rasters)
        sums = {}
        for b in ("terrain", "terrain_mean", "terrain_max", "fine"):
            out = os.path.join(tmp, b)
            with _Env(None), _DatumEnv(None):
                rep = hecras_engine.reexport_hecras_results(run, out_dir=out, depth_basis=b)
            js = json.load(open(os.path.join(out, hecras_engine.REEXPORT_JSON)))
            assert rep["depth_basis"] == js["depth_basis"] == b
            assert js["terrain_sample"] == ("average" if b == "terrain_mean" else
                                            "pixel_centre")
            assert js["datum_check"]["status"] == "ok"
            assert js["method"].startswith(hecras.DEPTH_BASIS_TEXT[b])
            if b in hecras.FINE_BASES:
                fi = js["depth_fine"]
                assert fi["fine_points_per_cell_side"] == 1 and fi["seconds"] >= 0
            else:
                assert js["depth_fine"] is None
            sums[b] = float(np.nansum(np.load(os.path.join(out, "Depth (Max).npy"))))
        assert [m[0] for m in made] == [os.path.join(tmp, "fine")]
        assert "fine_depth" in js["outputs"]
        assert sums["terrain_max"] >= sums["terrain"] > 0
        # NDBREACH_HECRAS_FINE_RASTER=0 / fine_rasters=False: no fine rasters
        with _Env(None), _DatumEnv(None):
            hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "f2"),
                                                  depth_basis="fine", fine_rasters=False)
        assert len(made) == 1
        # the run report line names the basis and the fine record
        line = workflow.hecras_depth_basis_line(dict(js, depth_basis="fine"))
        assert "RAS Mapper" in line and "1 x 1 points" in line
        line = workflow.hecras_depth_basis_line({"depth_basis": "terrain_max"})
        assert "conservative" in line
    _with_patches(body)
