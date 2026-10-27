"""3.10.92: the HEC-RAS mapped depth is measured from the ACTUAL TERRAIN again by default
(reverses 3.10.84's cell-minimum default): max WSE of the HEC-RAS cell minus the run's fine
DEM at the centre of each output grid cell. The cell minimum stays selectable
(NDBREACH_HECRAS_DEPTH_BASIS, RunSettings.hecras_depth_basis, the import tool's DEPTH_BASIS,
hecras-reexport --depth-basis). The terrain must be on the model's vertical datum and unit
(hecras.datum_check against the HDF's cell minimum elevations). Synthetic 3 x 3 cell HDF of
test_hecras_terrain_depth, with a SLOPED centre cell (90 ft at its west edge to 99 ft at its
east edge; max WSE 95 ft), no GDAL or h5py needed."""
import json
import os

import numpy as np

from nd_breach_runner.core import hecras, hecras_engine, workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ARRIVAL_DEPTH_FT, FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_hecras_depth_cellmin import _Env, _reexport_fixture, \
    _with_patches
from nd_breach_runner.tests.test_hecras_terrain_depth import (
    CELL, DITCH_A, DX, HIGH, NP, WKT, WSE_C, _fake, _grid_args, _terrain)

ROWS = slice(NP, 2 * NP)                  # the centre cell's rows and columns
COLS = slice(NP, 2 * NP)


def _sloped():
    """(45, 45) ground, ft: 100 ft high ground; the centre cell slopes from 90 ft (its
    minimum, = the HDF's Cells Minimum Elevation) to 99 ft, west to east."""
    t = np.full((3 * NP, 3 * NP), HIGH)
    t[ROWS, COLS] = DITCH_A + np.linspace(0.0, 9.0, NP)[None, :]
    return t


class _DatumEnv(_Env):
    """Sets / clears NDBREACH_HECRAS_DATUM_CHECK for a block."""

    def __enter__(self):
        self.saved = os.environ.get(hecras.DATUM_CHECK_ENV)
        os.environ.pop(hecras.DATUM_CHECK_ENV, None)
        if self.value is not None:
            os.environ[hecras.DATUM_CHECK_ENV] = self.value

    def __exit__(self, *a):
        os.environ.pop(hecras.DATUM_CHECK_ENV, None)
        if self.saved is not None:
            os.environ[hecras.DATUM_CHECK_ENV] = self.saved


@test
def hecras_depth_terrain_is_the_default_basis_again():
    with _Env(None):
        assert hecras.depth_basis() == hecras.DEPTH_BASIS_TERRAIN
    assert hecras.DEPTH_BASIS_DEFAULT == hecras.DEPTH_BASIS_TERRAIN
    assert hecras.DEPTH_BASES[0] == hecras.DEPTH_BASIS_TERRAIN
    assert "centre of each output grid cell" in hecras.DEPTH_BASIS_TEXT["terrain"]
    assert hecras_engine.TERRAIN_SAMPLE == "pixel_centre"
    assert hecras_engine._SAMPLE_ALG[hecras_engine.TERRAIN_SAMPLE] == "near"
    # RunSettings: None = the variable (terrain unset); cell_min selectable, not in the run key
    st = workflow.RunSettings()
    assert st.hecras_depth_basis is None
    with _Env(None):
        assert hecras.depth_basis(st.hecras_depth_basis) == "terrain"
    st2 = workflow.RunSettings(hecras_depth_basis="cell_min")
    assert hecras.depth_basis(st2.hecras_depth_basis, environ={}) == "cell_min"
    assert "hecras_depth_basis" not in workflow.key_settings(st2)
    # the report line names the basis, the terrain read and the datum check
    line = workflow.hecras_depth_basis_line({
        "depth_basis": "terrain", "depth_terrain": {"source": "run_dem", "cell_m": 1.0},
        "datum_check": {"status": "ok", "median_ft": 0.4}})
    assert "(default)" in line and "the run's DEM, 1.00 m cells" in line and \
        "datum check ok" in line, line
    line = workflow.hecras_depth_basis_line({
        "depth_basis": "cell_min", "depth_basis_requested": "terrain", "datum_stop": "x",
        "datum_check": {"status": "mismatch", "median_ft": -3.0}})
    assert "MISMATCH" in line and "datum check: mismatch (median -3.00 ft)" in line, line


@test
def hecras_depth_import_tool_offers_terrain_first_and_cell_min():
    from nd_breach_runner import algorithms_hecras as ah
    assert ah.DEPTH_BASIS_KEYS == ["terrain", "cell_min"]
    assert "default" in ah.DEPTH_BASIS_OPTIONS[0] and "Terrain" in ah.DEPTH_BASIS_OPTIONS[0]
    assert "Cell minimum" in ah.DEPTH_BASIS_OPTIONS[1]
    with _Env(None):
        assert ah.DEPTH_BASIS_KEYS[ah._depth_basis_default()] == "terrain"
    with _Env("cell_min"):
        assert ah.DEPTH_BASIS_KEYS[ah._depth_basis_default()] == "cell_min"


@test
def hecras_sloped_cell_terrain_depth_is_shallower_than_cell_min():
    """One HEC-RAS cell on a 9 ft slope, max WSE 95 ft, cell minimum 90 ft: the cell minimum
    gives every pixel 5 ft; the terrain gives 95 - ground (5 ft at the low edge down to dry
    above 95 ft), never deeper, so the wet area and the mean depth are smaller."""
    t = _sloped()
    with _Env(None):
        out, info = hecras.terrain_depth(_fake(), *_grid_args(), t)
    assert info["depth_basis"] == "terrain"
    cm = info["depth_cell_min"]
    d = out["depth"]
    assert np.allclose(cm[ROWS, COLS], WSE_C - DITCH_A)
    exp = WSE_C - t[ROWS, COLS]
    wet = exp >= 0.01
    assert np.allclose(d[ROWS, COLS][wet], exp[wet])
    assert not np.isfinite(d[ROWS, COLS][~wet]).any()
    both = np.isfinite(d) & np.isfinite(cm)
    assert (d[both] <= cm[both] + 1e-9).all()
    assert (d[ROWS, COLS][wet][1:] < cm[ROWS, COLS][wet][1:]).any()
    dc = np.where(np.isfinite(d[ROWS, COLS]), d[ROWS, COLS], 0.0)
    assert int(np.isfinite(d).sum()) == int(wet.sum()) < NP * NP     # less wet area
    assert dc.mean() < cm[ROWS, COLS].mean() - 2.0                   # 2.75 vs 5 ft
    assert np.allclose(d[ROWS, NP], WSE_C - DITCH_A)         # the low edge = the cell minimum
    assert info["datum_check"]["status"] == "ok" and not info["datum_warnings"]
    # the cell-min switch gives the old answer on the same inputs
    out2, info2 = hecras.terrain_depth(_fake(), *_grid_args(), t, basis="cell_min")
    assert info2["depth_basis"] == "cell_min"
    assert np.allclose(out2["depth"][ROWS, COLS], WSE_C - DITCH_A)


@test
def hecras_sloped_cell_arrival_uses_the_same_terrain_depth():
    """ARRIVAL_FT 0.1 ft on the terrain: the cell's WSE rises 90 -> 95 ft over 2 h, so a pixel
    of ground g arrives at (g + 0.1 - 90) / 5 x 2 h; the cell minimum gives all 0.04 h."""
    assert abs(ARRIVAL_DEPTH_FT - 0.1) < 1e-12
    t = _sloped()
    with _Env(None):
        out, _ = hecras.terrain_depth(_fake(), *_grid_args(), t,
                                      arrival_depth_ft=ARRIVAL_DEPTH_FT)
    d, arr = out["depth"], out["arrival"]
    assert np.array_equal(np.isfinite(arr), np.isfinite(d) & (d >= ARRIVAL_DEPTH_FT))
    g = t[ROWS, COLS]
    reach = np.isfinite(arr[ROWS, COLS])
    assert np.allclose(arr[ROWS, COLS][reach],
                       ((g + ARRIVAL_DEPTH_FT - DITCH_A) / (WSE_C - DITCH_A) * 2.0)[reach],
                       atol=1e-9)
    assert np.nanmax(arr) > 1.0
    oc, _ = hecras.terrain_depth(_fake(), *_grid_args(), t, arrival_depth_ft=ARRIVAL_DEPTH_FT,
                                 basis="cell_min")
    assert np.allclose(oc["arrival"][np.isfinite(oc["arrival"])], 0.04, atol=1e-9)
    # hecras_engine.results (the stage / re-export path): SI, the same mask
    like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
    with _Env(None):
        info = {}
        a = hecras_engine.results(_fake(), like, terrain=t, info=info)
    assert info["depth_basis"] == "terrain" and info["datum_check"]["status"] == "ok"
    assert np.array_equal(np.isfinite(a["arrival"]), np.isfinite(a["depth"]) &
                          (a["depth"] >= ARRIVAL_DEPTH_FT * FT - 1e-9))
    assert np.allclose(a["depth"][np.isfinite(a["depth"])], d[np.isfinite(d)] * FT)


@test
def hecras_datum_check_stops_a_terrain_on_another_datum_or_unit():
    t = _sloped()
    for bad, why in ((t - 3.0, "below"), (t * FT, "units"), (t / FT, "units")):
        with _Env(None), _DatumEnv(None):
            try:
                hecras.terrain_depth(_fake(), *_grid_args(), bad)
                raise AssertionError("accepted a terrain " + why)
            except hecras.DatumMismatch as e:
                assert "TERRAIN DATUM/UNIT MISMATCH" in str(e) and why in str(e), str(e)
        # the cell-min basis keeps going, the warning says why the comparison is off
        with _DatumEnv(None):
            out, info = hecras.terrain_depth(_fake(), *_grid_args(), bad, basis="cell_min")
        assert info["datum_check"]["status"] == "mismatch"
        assert any("MISMATCH" in w for w in info["datum_warnings"])
        assert np.allclose(out["depth"][ROWS, COLS], WSE_C - DITCH_A)
        # NDBREACH_HECRAS_DATUM_CHECK=warn keeps the terrain basis, loudly
        with _Env(None), _DatumEnv("warn"):
            out, info = hecras.terrain_depth(_fake(), *_grid_args(), bad)
        assert info["depth_basis"] == "terrain" and info["datum_check"]["status"] == "mismatch"
        assert any("=warn" in w for w in info["datum_warnings"])
    # hecras_engine.results raises it too (the stage then falls back to cell_min)
    like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
    with _Env(None), _DatumEnv(None):
        try:
            hecras_engine.results(_fake(), like, terrain=t - 3.0, info={})
            raise AssertionError("results accepted a terrain 3 ft low")
        except hecras.DatumMismatch:
            pass


@test
def hecras_datum_check_warns_on_a_terrain_above_the_model_everywhere():
    t = _sloped() + 3.0
    with _Env(None), _DatumEnv(None):
        out, info = hecras.terrain_depth(_fake(), *_grid_args(), t)
    assert info["depth_basis"] == "terrain" and info["datum_check"]["status"] == "suspect"
    assert any("above the model" in w for w in info["datum_warnings"])
    r = hecras.datum_check(np.array([1.0, 2.0]), np.array([1.0, 2.0]))
    assert r["status"] == "no_data" and not r["fatal"]
    r = hecras.datum_check(_sloped(), _sloped() - 0.2)
    assert r["status"] == "ok" and r["median_ft"] == 0.2


def _run_dem_fixture(tmp, patch, carve=True):
    """_reexport_fixture plus the run's 1 m DEM (sloped centre cell, its 93-ish ft column a
    lidar pool 3 ft over the carved bottom) and the 3 m model terrain (_terrain)."""
    from nd_breach_runner.core import gdal_io
    run = _reexport_fixture(tmp, patch)
    model = os.path.join(run, hecras.MODEL_FOLDER)
    os.makedirs(model, exist_ok=True)
    dem = os.path.join(tmp, "dem_1m.tif")
    open(dem, "w").write("stand-in")
    rep = {"settings": {"input_files": {"DEM": dem}}, "dem_z_to_m": 1.0,
           "dem_elevation_units": "metres"}
    if carve:
        open(os.path.join(model, "carve_depth.tif"), "w").write("stand-in")
        rep["reservoir_carve"] = {"applied": True, "files": {"depth": "carve_depth.tif"}}
    with open(os.path.join(model, hecras.RUN_REPORT_JSON), "w") as f:
        json.dump(rep, f)
    ground = _sloped()
    pool = np.zeros_like(ground)
    pool[ROWS, NP + 4] = 3.0 * FT                      # m, the carve depth
    lidar = ground.copy()
    lidar[ROWS, NP + 4] += 3.0                        # the lidar water surface

    def read_like(p, g, alg="bilinear"):
        assert alg == "near", alg                     # pixel centres
        if p == dem:
            return lidar * FT
        if p.endswith("carve_depth.tif"):
            return pool.copy()
        return _terrain() * FT                        # the model terrain
    patch(gdal_io, "read_like", read_like)
    patch(hecras_engine, "_cell_m", lambda p: 1.0 if p == dem else 3.0)
    return run, dem, ground


@test
def hecras_reexport_terrain_default_reads_the_run_dem_both_bases():
    def body(tmp, patch):
        run, dem, ground = _run_dem_fixture(tmp, patch)
        with _Env(None):
            rep = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "a"))
        assert rep["depth_basis"] == "terrain"
        tr = rep["depth_terrain"]
        assert tr["source"] == "run_dem" and tr["path"] == dem and tr["cell_m"] == 1.0
        assert tr["sample"] == "pixel_centre" and "carve" in (tr["carve"] or "")
        assert rep["datum_check"]["status"] == "ok", rep["datum_check"]
        d = np.load(os.path.join(tmp, "a", "Depth (Max).npy"))   # ft (the grid's unit)
        exp = WSE_C - ground[ROWS, COLS]
        wet = exp >= 0.01
        assert np.allclose(d[ROWS, COLS][wet], exp[wet], atol=1e-6)
        # the carved pool column measured from the carved bottom, not the lidar water
        assert np.allclose(d[ROWS, NP + 4], WSE_C - ground[NP, NP + 4], atol=1e-6)
        js = json.load(open(os.path.join(tmp, "a", hecras_engine.REEXPORT_JSON)))
        assert js["depth_basis"] == "terrain" and js["depth_terrain"]["source"] == "run_dem"
        assert js["terrain_sample"] == "pixel_centre" and js["datum_check"]["status"] == "ok"
        # cell minimum on the same run: the whole cell 5 ft, deeper than the terrain
        rep2 = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "b"),
                                                     depth_basis="cell_min")
        assert rep2["depth_basis"] == "cell_min"
        d2 = np.load(os.path.join(tmp, "b", "Depth (Max).npy"))
        assert np.allclose(d2[ROWS, COLS], WSE_C - DITCH_A, atol=1e-6)
        assert np.nansum(d2) > np.nansum(d)
        by = rep2["areas_by_basis"]
        assert by["terrain"]["wet_ac"] < by["cell_min"]["wet_ac"]
        # NDBREACH_HECRAS_DEPTH_TERRAIN=model: the HEC-RAS model terrain instead
        os.environ[hecras_engine.TERRAIN_SOURCE_ENV] = "model"
        try:
            with _Env(None):
                rep3 = hecras_engine.reexport_hecras_results(run,
                                                             out_dir=os.path.join(tmp, "c"))
        finally:
            os.environ.pop(hecras_engine.TERRAIN_SOURCE_ENV, None)
        assert rep3["depth_terrain"]["source"] == "hecras_terrain"
        d3 = np.load(os.path.join(tmp, "c", "Depth (Max).npy"))
        assert int(np.isfinite(d3).sum()) == 2 * NP                  # the ditch terrain
    _with_patches(body)


@test
def hecras_reexport_terrain_on_another_datum_is_refused():
    def body(tmp, patch):
        from nd_breach_runner.core import gdal_io
        run = _reexport_fixture(tmp, patch)
        patch(gdal_io, "read_like", lambda p, g, alg="bilinear": (_sloped() - 3.0) * FT)
        with _Env(None), _DatumEnv(None):
            try:
                hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "a"))
                raise AssertionError("re-exported on a terrain 3 ft low")
            except hecras_engine.HecRasEngineError as e:
                assert "MISMATCH" in str(e)
            assert not os.path.exists(os.path.join(tmp, "a", "Depth (Max).tif"))
            rep = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "b"),
                                                        depth_basis="cell_min")
        assert rep["depth_basis"] == "cell_min" and rep["datum_check"]["status"] == "mismatch"
        assert any("MISMATCH" in w for w in rep["warnings"])
    _with_patches(body)
