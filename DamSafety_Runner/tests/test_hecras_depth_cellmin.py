"""3.10.84: the HEC-RAS mapped depth is the cell's max WSE minus the cell's MINIMUM terrain
elevation by default again (reverses the 3.10.79 per-pixel default); the per-pixel terrain
basis stays selectable (argument, RunSettings.hecras_depth_basis, NDBREACH_HECRAS_DEPTH_BASIS,
the re-export CLI's --depth-basis). The wet mask and the 0.1 ft arrival follow the depth of
the basis used. Same synthetic 3 x 3 cell HDF as test_hecras_terrain_depth (a ditch cell)."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras, hecras_engine, workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ARRIVAL_DEPTH_FT, FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import npy_writer
from nd_breach_runner.tests.test_hecras import _FakeH5, _has_h5py, _write_h5
from nd_breach_runner.tests.test_hecras_terrain_depth import (
    AREA, CELL, DITCH_A, DX, NP, TS, WKT, WSE_C, _fake, _grid_args, _layout, _terrain)


class _Env:
    """Sets / clears NDBREACH_HECRAS_DEPTH_BASIS for a block."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.saved = os.environ.get(hecras.DEPTH_BASIS_ENV)
        if self.value is None:
            os.environ.pop(hecras.DEPTH_BASIS_ENV, None)
        else:
            os.environ[hecras.DEPTH_BASIS_ENV] = self.value

    def __exit__(self, *a):
        if self.saved is None:
            os.environ.pop(hecras.DEPTH_BASIS_ENV, None)
        else:
            os.environ[hecras.DEPTH_BASIS_ENV] = self.saved


@test
def hecras_depth_basis_default_is_cell_min_and_env_selects_terrain():
    with _Env(None):
        assert hecras.depth_basis() == hecras.DEPTH_BASIS_CELL_MIN
        assert hecras.DEPTH_BASIS_DEFAULT == hecras.DEPTH_BASIS_CELL_MIN
    assert hecras.depth_basis(environ={hecras.DEPTH_BASIS_ENV: "Terrain"}) == "terrain"
    assert hecras.depth_basis(environ={hecras.DEPTH_BASIS_ENV: "per_pixel"}) == "terrain"
    assert hecras.depth_basis(environ={hecras.DEPTH_BASIS_ENV: "cellmin"}) == "cell_min"
    # an explicit value wins over the variable
    assert hecras.depth_basis("cell_min", environ={hecras.DEPTH_BASIS_ENV: "terrain"}) == \
        "cell_min"
    for bad in ("average", "1"):
        try:
            hecras.depth_basis(environ={hecras.DEPTH_BASIS_ENV: bad})
            raise AssertionError("accepted " + bad)
        except hecras.HecRasError as e:
            assert hecras.DEPTH_BASIS_ENV in str(e)


@test
def hecras_cell_min_depth_floods_the_whole_ditch_cell_with_the_terrain_given():
    """Default basis with a terrain: every pixel of the centre cell is WSE - cell min (5 ft);
    the per-pixel depth is kept only as the comparison."""
    t = _terrain()
    with _Env(None):
        out, info = hecras.terrain_depth(_fake(), *_grid_args(), t)
    assert info["depth_basis"] == hecras.DEPTH_BASIS_CELL_MIN
    d = out["depth"]
    assert int(np.isfinite(d).sum()) == NP * NP
    assert np.allclose(d[np.isfinite(d)], WSE_C - DITCH_A)
    # same as the 3.10.78 read_mesh + rasterise export
    old = hecras.rasterise(hecras.read_mesh(_fake()), *_grid_args())["depth"]
    assert np.array_equal(np.isfinite(old), np.isfinite(d)) and np.allclose(
        old[np.isfinite(old)], d[np.isfinite(d)])
    # the comparison per-pixel depth: only the two ditch columns wet
    dt = info["depth_terrain"]
    assert int((dt >= 0.01).sum()) == 2 * NP
    # cell-min depth is never smaller than the per-pixel depth (terrain >= cell minimum)
    both = np.isfinite(dt) & np.isfinite(info["depth_cell_min"])
    assert (info["depth_cell_min"][both] >= dt[both] - 1e-9).all()


@test
def hecras_cell_min_needs_no_terrain_and_terrain_basis_does():
    with _Env(None):
        out, info = hecras.terrain_depth(_fake(), *_grid_args(), None)
    assert info["depth_basis"] == "cell_min" and info["depth_terrain"] is None
    assert int(np.isfinite(out["depth"]).sum()) == NP * NP
    try:
        hecras.terrain_depth(_fake(), *_grid_args(), None, basis="terrain")
        raise AssertionError("terrain basis without a terrain")
    except hecras.HecRasError as e:
        assert "terrain" in str(e)


@test
def hecras_cell_min_arrival_uses_the_cell_min_depth_at_0p1_ft():
    """Arrival at ARRIVAL_FT (0.1 ft) on the cell-min depth: the cell's WSE rises 90 -> 95 ft
    over 2 h, so every pixel of the cell (also the 93 ft and 100 ft ground) arrives at
    0.1 / 5 x 2 = 0.04 h; the per-pixel basis gives the 93 ft column 1.24 h instead."""
    assert abs(ARRIVAL_DEPTH_FT - 0.1) < 1e-12
    t = _terrain()
    out, _ = hecras.terrain_depth(_fake(), *_grid_args(), t, arrival_depth_ft=ARRIVAL_DEPTH_FT,
                                  basis="cell_min")
    d, arr = out["depth"], out["arrival"]
    assert np.array_equal(np.isfinite(arr), np.isfinite(d) & (d >= ARRIVAL_DEPTH_FT))
    assert np.allclose(arr[np.isfinite(arr)], 0.04, atol=1e-9)
    assert int(np.isfinite(arr).sum()) == NP * NP
    # identical to the per-cell arrival of read_mesh
    cell = hecras.rasterise(hecras.read_mesh(_fake(), arrival_depth_ft=ARRIVAL_DEPTH_FT),
                            *_grid_args())["arrival"]
    assert np.allclose(cell[np.isfinite(cell)], arr[np.isfinite(arr)])
    outp, _ = hecras.terrain_depth(_fake(), *_grid_args(), t, arrival_depth_ft=0.1,
                                   basis="terrain")
    assert np.allclose(outp["arrival"][t == 93.0], 1.24, atol=1e-9)


@test
def hecras_cell_min_arrival_between_output_steps_uses_time_of_max():
    """A series that peaks 0.05 ft over the cell minimum: max WSE says 5 ft deep, the saved
    steps never reach 0.1 ft; arrival is the cell's time of the max (2 h)."""
    L, root = _layout()
    k = TS + "2D Flow Areas/{}/Water Surface".format(AREA)
    ser = L[k][0].copy()
    ser[:, 4] = np.minimum(ser[:, 4], DITCH_A + 0.05)
    L[k] = (ser, L[k][1])
    out, info = hecras.terrain_depth(_FakeH5(L, root), *_grid_args(), _terrain(),
                                     arrival_depth_ft=0.1, basis="cell_min")
    d, arr = out["depth"], out["arrival"]
    assert np.array_equal(np.isfinite(arr), np.isfinite(d) & (d >= 0.1))
    assert np.allclose(arr[np.isfinite(arr)], 2.0)


@test
def hecras_engine_results_default_cell_min_and_terrain_selectable():
    like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
    t = _terrain()
    with _Env(None):
        info = {}
        a = hecras_engine.results(_fake(), like, terrain=t, info=info)
    assert info["depth_basis"] == info["depth_basis_requested"] == "cell_min"
    assert info["other_basis"] == "terrain"
    assert int(np.isfinite(a["depth"]).sum()) == NP * NP
    assert np.allclose(a["depth"][np.isfinite(a["depth"])], (WSE_C - DITCH_A) * FT)
    assert np.array_equal(np.isfinite(a["arrival"]), np.isfinite(a["depth"]))
    assert int(np.nansum(info["other_depth_m"] > 0.003)) == 2 * NP
    with _Env("terrain"):
        info2 = {}
        b = hecras_engine.results(_fake(), like, terrain=t, info=info2)
    assert info2["depth_basis"] == "terrain" and info2["other_basis"] == "cell_min"
    assert int(np.isfinite(b["depth"]).sum()) == 2 * NP
    # 'terrain' without a terrain falls back to cell_min and says so
    info3 = {}
    c = hecras_engine.results(_fake(), like, info=info3, depth_basis="terrain")
    assert info3["depth_basis"] == "cell_min" and info3["depth_basis_requested"] == "terrain"
    assert info3["other_depth_m"] is None
    assert any("no terrain" in n for n in info3["notes"])
    assert int(np.isfinite(c["depth"]).sum()) == NP * NP


@test
def hecras_run_settings_depth_basis_is_not_part_of_the_run_key():
    st = workflow.RunSettings()
    assert st.hecras_depth_basis is None and "hecras_depth_basis" not in st.as_dict()
    st2 = workflow.RunSettings(hecras_depth_basis="terrain")
    assert st2.as_dict()["hecras_depth_basis"] == "terrain"
    assert "hecras_depth_basis" not in workflow.key_settings(st2)
    line = workflow.hecras_depth_basis_line({"depth_basis": "cell_min"})
    assert "minimum terrain elevation" in line
    line = workflow.hecras_depth_basis_line({"depth_basis": "cell_min",
                                             "depth_basis_requested": "terrain"})
    assert "no terrain" in line


def _reexport_fixture(tmp, patch, with_terrain=True):
    from nd_breach_runner.core import gdal_io
    run = os.path.join(tmp, "DAM-1")
    proj = os.path.join(tmp, "proj")
    hdf = os.path.join(proj, "linux_run", "DAM_1.p01.hdf")
    for d in (os.path.join(run, "_hecras"), os.path.join(proj, "Terrain"),
              os.path.dirname(hdf)):
        os.makedirs(d)
    if with_terrain:
        open(os.path.join(proj, "Terrain", "DAM_1 Terrain.tif"), "w").write("stand-in")
    with open(os.path.join(run, "_hecras", "hecras_stage.json"), "w") as f:
        json.dump({"engine": "hecras", "project": proj, "results_hdf": hdf, "t0_hr": 0.0,
                   "depth_basis": "terrain"}, f)
    with open(os.path.join(proj, "hecras_engine.json"), "w") as f:
        json.dump({"model_wkt": WKT}, f)
    layout, root = _layout()
    if _has_h5py():
        _write_h5(hdf, layout, root)
    else:
        open(hdf, "w").write("stand-in")
        real_open = hecras._open_hdf
        patch(hecras, "_open_hdf", lambda h: (_FakeH5(layout, root), (lambda: None))
              if str(h) == hdf else real_open(h))
    like = Grid(np.zeros((3 * NP, 3 * NP)), 0.0, 3 * CELL, DX, crs_wkt=WKT)
    patch(gdal_io, "read_grid", lambda p: like)
    patch(gdal_io, "read_like", lambda p, g, alg="bilinear": _terrain() * FT)
    patch(gdal_io, "z_to_m_factor", lambda p, choice="auto": (1.0, "metres"))
    patch(gdal_io, "GeoTiffWriter", lambda crs: npy_writer)
    patch(gdal_io, "linear_unit_m", lambda wkt: FT)
    open(os.path.join(run, "Depth (Max).tif"), "w").write("the run's own raster")
    return run


def _with_patches(fn):
    tmp = tempfile.mkdtemp()
    saved = []

    def patch(obj, name, val):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    try:
        return fn(tmp, patch)
    finally:
        for obj, name, val in reversed(saved):
            setattr(obj, name, val)
        shutil.rmtree(tmp, ignore_errors=True)


@test
def hecras_reexport_both_bases_and_cell_min_without_terrain():
    px_ac = DX * DX * FT * FT / 4046.8564224

    def body(tmp, patch):
        run = _reexport_fixture(tmp, patch)
        with _Env(None):
            rep = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "a"))
        assert rep["depth_basis"] == "cell_min" and rep["depth_basis_stage"] == "terrain"
        d = np.load(os.path.join(tmp, "a", "Depth (Max).npy"))
        assert int(np.isfinite(d).sum()) == NP * NP
        arr = np.load(os.path.join(tmp, "a", "Arrival Time.npy"))
        assert np.array_equal(np.isfinite(arr), np.isfinite(d))
        by = rep["areas_by_basis"]
        assert abs(by["cell_min"]["ge_1ft_ac"] - NP * NP * px_ac) < 1e-9
        assert abs(by["terrain"]["ge_1ft_ac"] - 2 * NP * px_ac) < 1e-9
        assert rep["areas_new"] == by["cell_min"]
        rep2 = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "b"),
                                                     depth_basis="terrain")
        assert rep2["depth_basis"] == "terrain"
        d2 = np.load(os.path.join(tmp, "b", "Depth (Max).npy"))
        assert int(np.isfinite(d2).sum()) == 2 * NP
        js = json.load(open(os.path.join(tmp, "b", hecras_engine.REEXPORT_JSON)))
        assert js["depth_basis"] == "terrain" and set(js["areas_by_basis"]) == {
            "cell_min", "terrain"}
    _with_patches(body)

    def body2(tmp, patch):
        run = _reexport_fixture(tmp, patch, with_terrain=False)
        rep = hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "c"),
                                                    depth_basis="cell_min")
        assert rep["depth_basis"] == "cell_min" and rep["terrain_tif"] is None
        assert list(rep["areas_by_basis"]) == ["cell_min"]
        try:
            hecras_engine.reexport_hecras_results(run, out_dir=os.path.join(tmp, "d"),
                                                  depth_basis="terrain")
            raise AssertionError("terrain basis without a terrain")
        except hecras_engine.HecRasEngineError as e:
            assert "cell_min" in str(e)
    _with_patches(body2)


@test
def hecras_reexport_cli_depth_basis_option():
    import importlib
    cli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    ns = cli.build_parser().parse_args(["hecras-reexport", "/x"])
    assert ns.depth_basis == ""
    ns = cli.build_parser().parse_args(["hecras-reexport", "/x", "--depth-basis", "terrain"])
    assert ns.depth_basis == "terrain"
    seen = {}
    real = hecras_engine.reexport_hecras_results

    def fake(run_dir, **kw):
        seen.update(kw)
        return {"depth_basis": kw["depth_basis"] or "cell_min", "areas_by_basis": {
            "cell_min": {"wet_ac": 1.0, "ge_0p1ft_ac": 1.0, "ge_1ft_ac": 1.0}}}
    hecras_engine.reexport_hecras_results = fake
    try:
        assert cli.cmd_hecras_reexport(ns) == 0
        assert seen["depth_basis"] == "terrain"
        ns = cli.build_parser().parse_args(["hecras-reexport", "/x"])
        assert cli.cmd_hecras_reexport(ns) == 0 and seen["depth_basis"] is None
    finally:
        hecras_engine.reexport_hecras_results = real
