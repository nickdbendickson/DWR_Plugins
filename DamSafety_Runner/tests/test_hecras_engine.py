"""Tests of the HEC-RAS 2D engine module (core/hecras_engine.py). No HEC-RAS, no Wine: the
results reader runs on a tiny synthetic plan HDF, the project writer on build_model's own
text writers with a synthetic Model, and detect() on fake folders."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br, hecras_engine as he, hecras_model as hm
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import CFS, FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras as th


def _touch(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()


@test
def hecras_engine_detect_reports_each_missing_piece():
    d = he.detect(env={}, platform="linux", which=lambda n: None)
    assert not d["available"]
    for k in (he.ENV_PREFIX, he.ENV_LINUX, "wine", "Ras.exe", "drv.exe"):
        assert k in d["reason"], (k, d["reason"])
    assert "Nothing was run" not in d["reason"]
    try:
        he.require(env={}, platform="linux", which=lambda n: None)
        raise AssertionError("require() should raise")
    except he.HecRasEngineError as e:
        assert "Nothing was run" in str(e)
    assert "macOS" in he.detect(env={}, platform="darwin")["reason"]


@test
def hecras_engine_detect_finds_a_complete_install():
    t = tempfile.mkdtemp()
    try:
        pre = os.path.join(t, "pfx")
        ras = os.path.join(pre, he.WINDOWS_DEFAULT_SUB)
        for n in ("Ras.exe",) + he.DRIVERS:
            _touch(os.path.join(ras, n))
        lin = os.path.join(t, "Linux_RAS_v66")
        _touch(os.path.join(lin, "bin", "RasUnsteady"))
        env = {he.ENV_PREFIX: pre, he.ENV_LINUX: lin}
        d = he.detect(env=env, platform="linux", which=lambda n: "/usr/bin/" + n)
        if th._has_h5py():
            assert d["available"], d["reason"]
        assert d["ras_exe"].endswith("Ras.exe") and d["rasunsteady"].endswith("RasUnsteady")
        assert all(d["drivers"].values())
        assert he.to_wine_path(os.path.join(ras, "Ras.exe"), pre) == "C:\\HEC\\RAS66\\Ras.exe"
        assert he.to_wine_path("/tmp/a b/x.tif", pre) == "Z:\\tmp\\a b\\x.tif"
        e = he.wine_env(d, {"PROJ_LIB": "/x", "GDAL_DATA": "/y", "PATH": "/bin"})
        assert "PROJ_LIB" not in e and e["GDAL_DATA"].startswith("C:\\HEC\\RAS66")
        assert e["WINEPREFIX"] == pre and e["PATH"] == "/bin"
    finally:
        shutil.rmtree(t, ignore_errors=True)


def _model():
    m = hm.Model()
    m.name, m.title = "T", "Test dam"
    m.perimeter = [(0.0, 0.0), (2000.0, 0.0), (2000.0, 1000.0), (0.0, 1000.0)]
    m.points = np.array([[100.0, 100.0], [300.0, 100.0]])
    m.conn_line = [(0.0, 500.0), (0.0, 600.0)]
    m.weir = [(0.0, 1010.0), (50.0, 1000.0), (100.0, 1010.0)]
    m.bc_line = [(2000.0, 200.0), (2000.0, 800.0)]
    m.initial_wse = 1008.0
    m.reservoir = [(-500.0, 400.0), (0.0, 400.0), (0.0, 700.0), (-500.0, 700.0)]
    m.elev_volume = [(990.0, 0.0), (1010.0, 500.0)]
    m.breach = {"center_station": 50.0, "bottom_width": 40.0, "bottom_elev": 990.0,
                "left_slope": 1.0, "right_slope": 1.0, "formation_hr": 0.5, "weir_coef": 2.6,
                "trigger_wse": 1008.0}
    return m


@test
def hecras_engine_write_project_hydrograph_inflow():
    t = tempfile.mkdtemp()
    try:
        m = _model()
        hy = br.Hydrograph(np.array([0.0, 1800.0, 7200.0]),
                           np.array([0.0, 5000.0 * CFS, 0.0]), "test")

        def build(dam_folder, out, options=None, log=None):
            paths = hm.write_text_files(m, out)
            return {"folder": out, "paths": paths, "model": m, "notes": [],
                    "info": {"inflow_xy_model": [150.0, 550.0]}}
        before = {k: open(p, newline="").read() for k, p in
                  build(None, os.path.join(t, "ref"))["paths"].items()}
        r = he.write_project("dam", os.path.join(t, "m"), build=build,
                             load=lambda f: (hy, None, {}))
        g = open(r["paths"]["g01"], newline="").read()
        u = open(r["paths"]["u01"], newline="").read()
        p = open(r["paths"]["p01"], newline="").read()
        assert "BC Line Name=" + hm.name32(he.INFLOW_BC_NAME) in g
        assert g.index(he.INFLOW_BC_NAME) < g.index("LCMann Time=")
        assert g.replace("\r\n", "\n").count("BC Line Name=") == 2
        assert "Flow Hydrograph= 25 " in u and "Flow Hydrograph Slope= 0.001 " in u
        assert "Initial Storage Elev={},1000".format(hm.name16(hm.SA_NAME)) in u
        assert "Breach Loc=" not in p and "Breach Loc=" in before["p01"]
        assert "\n" not in g.replace("\r\n", "")              # CRLF only
        line = r["inflow_bc"]["line"]
        assert abs(np.hypot(line[1][0] - line[0][0], line[1][1] - line[0][1]) - 300.0) < 1e-6
        assert abs(line[0][0] - 150.0) < 1e-6                 # across the flow (+x)
        assert r["inflow_bc"]["peak"] == 5000.0
        # breach mode leaves build_model's files byte-identical
        r2 = he.write_project("dam", os.path.join(t, "b"), inflow_mode="breach", build=build)
        for k, pth in r2["paths"].items():
            if k in before:
                assert open(pth, newline="").read() == before[k], k
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_engine_results_on_runner_grid_and_final_names():
    if not th._has_h5py():
        return
    t = tempfile.mkdtemp()
    try:
        layout, root = th._mesh_layout()
        hdf = os.path.join(t, "T.p01.hdf")
        th._write_h5(hdf, layout, root)
        # the mesh is in ft (x 1000-1060, y 2000-2060); the Runner grid in metres
        g = Grid(np.zeros((6, 6)), 1000.0 * FT, 2060.0 * FT, 10.0 * FT)
        to_m = lambda xy: np.asarray(xy) * FT
        a = he.results(hdf, g, arrival_m=1.0 * FT, to_grid_xy=to_m)
        assert set(a) == {"depth", "velocity", "arrival", "wse"}
        for v in a.values():
            assert v.shape == (6, 6)
        d = a["depth"]
        th._close(np.nanmax(d), 5.0 * FT, rel=1e-6)            # SI
        assert np.isnan(d[:, 4:]).all() and np.isfinite(d[:, :4]).all()
        th._close(np.nanmax(a["wse"]), 105.0 * FT, rel=1e-6)
        assert np.isnan(a["arrival"][:, 4:]).all() and np.nanmin(a["arrival"]) >= 0.0
        written = {}
        paths = he.write_results(a, g, lambda grid, p: written.__setitem__(p, grid), t)
        from nd_breach_runner.core import workflow
        assert {os.path.basename(p) for p in paths.values()} == set(workflow.FINAL_NAMES.values())
        dg = written[paths["depth"]]
        th._close(np.nanmax(dg.data), 5.0, rel=1e-6)          # back in ft
        assert dg.x0 == g.x0 and dg.data.shape == (6, 6)
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_engine_rasmap_terrain_and_cr_strip():
    t = tempfile.mkdtemp()
    try:
        m = _model()
        paths = hm.write_text_files(m, t)
        he.write_rasmap_terrain(paths["rasmap"])
        s = open(paths["rasmap"]).read()
        assert 'Type="TerrainLayer"' in s and "Terrain\\Terrain.hdf" in s
        p = os.path.join(t, "x.b01")
        open(p, "wb").write(b"a\r\nb\r\n")
        he.strip_cr(p)
        assert open(p, "rb").read() == b"a\nb\n"
    finally:
        shutil.rmtree(t, ignore_errors=True)
