"""Tests of the HEC-RAS 2D engine module (core/hecras_engine.py). No HEC-RAS, no Wine: the
results reader runs on a tiny synthetic plan HDF, the project writer on build_model's own
text writers with a synthetic Model, and detect() on fake folders."""
import datetime
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
            # a fixed edit-time stamp so the reference build and the breach-mode build stamp
            # the geometry identically. The two PointsPerimeterTime / Connection Last Edited
            # Time stamps are otherwise the wall clock, so the byte-identical assertion below
            # flaked (~2.5%) when the two writes straddled a one-second tick.
            paths = hm.write_text_files(m, out, now=datetime.datetime(2026, 1, 1))
            return {"folder": out, "paths": paths, "model": m, "notes": [],
                    "info": {"inflow_xy_model": [150.0, 550.0]}}
        before = {k: open(p, newline="").read() for k, p in
                  build(None, os.path.join(t, "ref"))["paths"].items()}
        r = he.write_project("dam", os.path.join(t, "m"), build=build,
                             load=lambda f: (hy, None, {}), env={})
        g = open(r["paths"]["g01"], newline="").read()
        u = open(r["paths"]["u01"], newline="").read()
        p = open(r["paths"]["p01"], newline="").read()
        assert "BC Line Name=" + hm.name32(he.INFLOW_BC_NAME) in g
        assert g.index(he.INFLOW_BC_NAME) < g.index("LCMann Time=")
        assert g.replace("\r\n", "\n").count("BC Line Name=") == 2
        # the hydrograph runs the whole simulation (24 h at 5 min: 289 values), not 2 h
        assert "Flow Hydrograph= 289 " in u and "Flow Hydrograph Slope= 0.001 " in u
        assert r["inflow_bc"]["series"]["end_hr"] == m.sim_hours == 24.0
        # the reservoir starts dry (lowest storage elevation) with no lateral inflow: its
        # water is already in the hydrograph
        assert "Initial Storage Elev={},990".format(hm.name16(hm.SA_NAME)) in u
        lat = u.split("Lateral Inflow Hydrograph=", 1)[1].split("DSS Path=", 1)[0]
        assert set(lat.split()[1:]) == {"0.00"}, lat
        assert "Breach Loc=" not in p and "Breach Loc=" in before["p01"]
        assert "\n" not in g.replace("\r\n", "")              # CRLF only
        line = r["inflow_bc"]["line"]
        assert abs(np.hypot(line[1][0] - line[0][0], line[1][1] - line[0][1]) - 300.0) < 1e-6
        assert abs(line[0][0] - 150.0) < 1e-6                 # across the flow (+x)
        assert r["inflow_bc"]["peak"] == 5000.0
        # breach mode leaves build_model's files byte-identical
        r2 = he.write_project("dam", os.path.join(t, "b"), inflow_mode="breach", build=build,
                              env={})
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


def _cliff_hydrograph():
    """A breach hydrograph that ends in a cliff, as the Runner writes it when the reservoir
    is empty (Absaraka: 250 cfs -> 0 at 2.47 h)."""
    t = np.array([0.0, 1800.0, 3600.0, 5400.0, 8400.0, 8880.0, 8890.0])
    q = np.array([0.0, 25800.0, 9000.0, 2300.0, 600.0, 300.0, 0.0]) * CFS
    return br.Hydrograph(t, q, "test")


@test
def hecras_engine_inflow_hydrograph_covers_the_simulation_without_a_cliff():
    hy = _cliff_hydrograph()
    sim = 40.0
    lab, v, info = he.inflow_series(hy, lambda q: q / CFS, sim, dt_min=5)
    assert lab == "5MIN" and len(v) == info["n"] == 40 * 12 + 1 and info["end_hr"] == sim
    assert v[0] == 0.0 and max(v) == 25800.0
    i_end = int(8880.0 // 300)                      # last sample inside the record (2.42 h)
    tail = v[i_end:]
    assert all(b <= a + 1e-9 for a, b in zip(tail, tail[1:]))      # recedes, never rises
    assert all(b >= 0.25 * a for a, b in zip(tail[:12], tail[1:13]) if a > 1.0), tail[:13]
    # no cliff: the first tail value is not 0 and the volume grows by under 2 %
    assert tail[1] > 50.0, tail[:3]
    assert info["volume_exported_acft"] < 1.02 * info["volume_record_acft"]
    # a configured base flow: never below it, held to the end
    _, vb, ib = he.inflow_series(hy, lambda q: q / CFS, sim, base_flow_cfs=40.0)
    assert min(vb) == 40.0 and vb[-1] == 40.0 and len(vb) == 481 and ib["held_cfs"] == 40.0
    # 'hold': the last value of the record to the end (or the base flow when set)
    _, vh, ih = he.inflow_series(hy, lambda q: q / CFS, sim, tail="hold")
    assert vh[-1] == 300.0 and min(vh[i_end:]) == 300.0 and ih["held_cfs"] == 300.0
    _, vh2, _ = he.inflow_series(hy, lambda q: q / CFS, sim, tail="hold", base_flow_cfs=20.0)
    assert vh2[-1] == 20.0
    # the interval and count follow the window
    _, v15, i15 = he.inflow_series(hy, lambda q: q / CFS, 6.0, dt_min=15)
    assert len(v15) == 25 and i15["interval"] == "15MIN"
    for bad in (dict(dt_min=7), dict(tail="cliff")):
        try:
            he.inflow_series(hy, lambda q: q / CFS, sim, **bad)
            raise AssertionError(bad)
        except he.HecRasEngineError:
            pass


@test
def hecras_engine_breach_mode_plan_has_the_breach_and_a_full_pool():
    t = tempfile.mkdtemp()
    try:
        m = _model()

        def build(dam_folder, out, options=None, log=None):
            return {"folder": out, "paths": hm.write_text_files(m, out), "model": m,
                    "notes": [], "info": {"inflow_xy_model": [150.0, 550.0]}}
        r = he.write_project("dam", os.path.join(t, "b"), build=build,
                             env={he.ENV_MODE: "breach"})
        assert r["inflow_mode"] == "breach" and r["inflow_bc"] is None
        p = open(r["paths"]["p01"], newline="").read().replace("\r\n", "\n")
        u = open(r["paths"]["u01"], newline="").read()
        # the layout of HEC's SA/2D-connection breach (BaldEagleCrkMulti2D/BaldEagleDamBrk.p06)
        assert "\nBreach Loc={},{},{},True,{}\n".format(" " * 16, " " * 16, " " * 8,
                                                         hm.name16(hm.CONN_NAME)) in p
        assert "\nBreach Geom=50,40,990,1,1,False,0.5,,0.5,2.6\n" in p
        assert "\nBreach Start=True,1008,,,False,,,0\n" in p
        assert "Initial Storage Elev={},1008".format(hm.name16(hm.SA_NAME)) in u
        assert "Breach Inflow" not in u
        try:
            he.write_project("dam", os.path.join(t, "x"), build=build,
                             env={he.ENV_MODE: "sideways"})
            raise AssertionError("bad mode should raise")
        except he.HecRasEngineError as e:
            assert he.ENV_MODE in str(e)
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_engine_plan_adaptive_time_step_is_hecs_layout():
    """HEC-RAS 6.x adaptive time step, lines as in HEC's BaldEagleDamBrk.p06 (6.6 examples)."""
    m = _model()
    p = hm.plan_text(m).replace("\r\n", "\n")
    want = ["Computation Time Step Use Courant=        -1",
            "Computation Time Step Use Time Series=    0",
            "Computation Time Step Max Courant=1", "Computation Time Step Min Courant=0.45",
            "Computation Time Step Count To Double=4", "Computation Time Step Max Doubling=3",
            "Computation Time Step Max Halving=3", "Computation Time Step Residence Courant=0"]
    assert "\n".join(want) in p, p
    assert p.index("Mapping Interval=") < p.index(want[0]) < p.index("Run HTab=")
    m.courant = dict(hm.COURANT_DEFAULTS, max=0.9, min=0.4, max_doubling=1, max_halving=0)
    p = hm.plan_text(m)
    assert "Max Courant=0.9\r\n" in p and "Min Courant=0.4\r\n" in p and "Max Halving=0\r\n" in p
    m.courant = dict(hm.COURANT_DEFAULTS, adaptive=False)
    p = hm.plan_text(m)
    assert "Use Courant=        0\r\n" in p and "Max Doubling=0\r\n" in p
    # HEC-RAS 6.6 data error unless min < 0.5 x max (Absaraka: max 1 / min 0.5 refused)
    for bad in (dict(max=1.0, min=1.5), dict(max=1.0, min=0.5), dict(max=2.0, min=1.0),
                dict(max=1.0, min=0.0), dict(max=1.0, min=-0.1)):
        m.courant = dict(hm.COURANT_DEFAULTS, **bad)
        try:
            hm.plan_text(m)
            raise AssertionError("{} should raise".format(bad))
        except hm.HecRasModelError as e:
            assert "0.5 x max" in str(e), str(e)
    m.courant = dict(hm.COURANT_DEFAULTS, max=1.0, min=0.49)
    assert "Min Courant=0.49\r\n" in hm.plan_text(m)
    m.equation = hm.EQ_DWE
    m.courant = dict(hm.COURANT_DEFAULTS)
    assert "Max Courant=2\r\n" in hm.plan_text(m) and "Min Courant=0.9\r\n" in hm.plan_text(m)


@test
def hecras_engine_fixed_width_tables_raise_when_a_value_cannot_fit():
    m = _model()
    m.elev_volume = [(990.0, 0.0), (1010.0, 123456789.0)]
    try:
        hm.geometry_text(m)
        raise AssertionError("a 9-digit volume should raise")
    except hm.HecRasModelError as e:
        assert "does not fit" in str(e)
    m.elev_volume = [(990.0, 0.0), (1010.0, 1234567.0)]
    g = hm.geometry_text(m).replace("\r\n", "\n")
    assert "\n  990.00    0.00 1010.00 1234567\n" in g


# ---------------------------------------------------------------------------- preprocess order
_FAKE_D = {"prefix": "/pfx", "windows_dir": "/pfx/drive_c/HEC/RAS66", "wine": "wine",
           "ras_exe": "/pfx/drive_c/HEC/RAS66/Ras.exe", "platform": "linux"}


class _Fake:
    """Stands in for _run / ras_compile / geometry_hdf_state: records the calls and changes
    the fake geometry HDF state the way each HEC-RAS step does (or does not)."""

    def __init__(self, broken=()):
        self.calls, self.broken = [], set(broken)
        self.state = {"exists": False, "size": 0, "cells": 0, "areas": {}, "terrain": "",
                      "props": False, "error": ""}

    def run(self, cmd, cwd, env, timeout, what, ok=None, log=None):
        step = "terrain" if cmd[-1] == "Terrain" else cmd[cmd.index("wine") + 3]
        self.calls.append(step)
        if step in self.broken:
            return "OK " + step                     # exit 0, prints OK, writes nothing
        if step == "terrain":
            p = os.path.join(cmd[-2].replace("Z:", "").replace("\\", "/"), "Terrain.hdf")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write("x")
        elif step == "assoc":
            self.state["terrain"] = ".\\Terrain\\Terrain.hdf"
        elif step == "props":
            self.state["props"] = True
            self.state["areas"] = {"Downstream": {"cells": 65739, "props": True}}
        return "OK " + step + (" tables complete" if step == "props" else "")

    def ras(self, project_dir, name, plan, d, e, timeout, log=None, poll_s=2.0, until=None,
            what=None):
        self.calls.append("ras1" if until is not None else "ras2")
        if until is not None and "ras1" not in self.broken:
            self.state.update(exists=True, size=1000, cells=65739,
                              areas={"Downstream": {"cells": 65739, "props": False}})

    def hdf(self, path):
        return dict(self.state)


def _with_fake(fake, fn):
    saved = he._run, he.ras_compile, he.geometry_hdf_state
    he._run, he.ras_compile, he.geometry_hdf_state = fake.run, fake.ras, fake.hdf
    try:
        return fn()
    finally:
        he._run, he.ras_compile, he.geometry_hdf_state = saved


@test
def hecras_engine_preprocess_step_order_on_a_new_project():
    """A build_model project has no geometry HDF: Ras.exe -c pass 1 makes it (mesh), then
    terrain association, property tables, Ras.exe -c pass 2 (Absaraka, 2026-10-18)."""
    t = tempfile.mkdtemp()
    try:
        f = _Fake()
        times = _with_fake(f, lambda: he.preprocess(t, "DWR_2801", d=_FAKE_D, env={},
                                                    terrain_tif="/x/t.tif", prj="/x/p.prj"))
        assert f.calls == ["terrain", "ras1", "assoc", "props", "ras2"], f.calls
        assert list(times) == list(he.STEPS) == ["terrain", "geometry", "assoc", "props", "ras"]
        # an existing geometry HDF with a mesh (HEC's examples): pass 1 is skipped
        f2 = _Fake()
        f2.state.update(exists=True, size=10, cells=5391)
        _with_fake(f2, lambda: he.preprocess(t, "M", d=_FAKE_D, env={},
                                             steps=["geometry", "assoc", "props", "ras"]))
        assert f2.calls == ["assoc", "props", "ras2"], f2.calls
        # drv.exe complete on a new project is refused with the reason (it writes nothing)
        try:
            _with_fake(_Fake(), lambda: he.preprocess(t, "N", d=_FAKE_D, env={},
                                                      steps=["complete"]))
            raise AssertionError("complete without a geometry HDF should raise")
        except he.HecRasEngineError as e:
            assert "existing" in str(e) and "'geometry' step" in str(e)
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_engine_preprocess_fails_when_a_step_writes_nothing():
    """Exit 0 and 'OK' are not enough: each step's output is checked."""
    t = tempfile.mkdtemp()
    try:
        cases = (("ras1", "has no 2D cells", ["geometry"]),
                 ("assoc", "no terrain associated", ["geometry", "assoc"]),
                 ("props", "no 2D property tables", ["geometry", "assoc", "props"]),
                 ("terrain", "was not written", ["terrain"]))
        for broken, text, stop in cases:
            f = _Fake(broken=[broken])
            if broken == "ras1":
                f.state.update(exists=True, size=10)      # an HDF without a mesh
            try:
                _with_fake(f, lambda: he.preprocess(t, "B", d=_FAKE_D, env={},
                                                    terrain_tif="/x/t.tif", prj="/x/p.prj",
                                                    steps=stop))
                raise AssertionError(broken + " should raise")
            except he.HecRasEngineError as e:
                assert text in str(e), (broken, str(e))
            assert "ras2" not in f.calls, (broken, f.calls)
        # props on an HDF that was never written (the c232f65 failure) stops before drv.exe
        f = _Fake()
        try:
            _with_fake(f, lambda: he.preprocess(t, "B", d=_FAKE_D, env={}, steps=["props"]))
            raise AssertionError("props without a geometry HDF should raise")
        except he.HecRasEngineError as e:
            assert "B.g01.hdf was not written" in str(e) and f.calls == []
        # a HEC-RAS data-errors file is quoted
        open(os.path.join(t, "B.p01.data_errors.txt"), "w").write(
            "The advanced time step control requires that the Minimum Courant is < 0.5 "
            "Maximum Courant.")
        try:
            _with_fake(_Fake(broken=["ras1"]), lambda: he.preprocess(
                t, "B", d=_FAKE_D, env={}, steps=["geometry"]))
            raise AssertionError("should raise")
        except he.HecRasEngineError as e:
            assert "Minimum Courant is < 0.5" in str(e)
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_engine_run_reports_exit_zero_without_the_ok_text():
    saved = he.wallclock.run

    class R:
        returncode, stdout, stderr = 0, "OK props 0.59 s", ""
    he.wallclock.run = lambda *a, **k: R()
    try:
        he._run(["x"], "/", {}, 10, "2D property tables", ok="tables complete")
        raise AssertionError("should raise")
    except he.HecRasEngineError as e:
        assert "exit 0 but it never printed 'tables complete'" in str(e)
    finally:
        he.wallclock.run = saved


@test
def hecras_engine_geometry_hdf_state_reads_cells_terrain_and_tables():
    if not th._has_h5py():
        return
    import h5py
    t = tempfile.mkdtemp()
    try:
        p = os.path.join(t, "g.g01.hdf")
        assert he.geometry_hdf_state(p)["exists"] is False
        dt = np.dtype([("Name", "S16"), ("Cell Count", "<i4")])
        with h5py.File(p, "w") as f:
            g = f.create_group("Geometry")
            a = g.create_group("2D Flow Areas")
            a.create_dataset("Attributes", data=np.array([(b"Downstream", 120)], dtype=dt))
            a.create_group("Downstream")
        st = he.geometry_hdf_state(p)
        assert st["cells"] == 120 and st["terrain"] == "" and st["props"] is False, st
        with h5py.File(p, "a") as f:
            f["Geometry"].attrs["Terrain Filename"] = b".\\Terrain\\Terrain.hdf"
            for k in he.PROPS_DATASETS:
                f["Geometry/2D Flow Areas/Downstream"].create_dataset(k, data=np.ones((3, 2)))
        st = he.geometry_hdf_state(p)
        assert st["terrain"] and st["props"] is True and st["areas"]["Downstream"]["props"]
        open(os.path.join(t, "bad.g01.hdf"), "w").write("not hdf")
        assert he.geometry_hdf_state(os.path.join(t, "bad.g01.hdf"))["error"]
    finally:
        shutil.rmtree(t, ignore_errors=True)


# ============================================ compute timeout scaled to the model (not 7,200 s)
@test
def hecras_engine_compute_timeout_scales_with_the_model():
    H = 3600.0
    # DWR-3161: 68,949 cells, 32 h, 3 s base step; needed ~3 h (65 % at 7,200 s)
    t, est, how = he.compute_timeout(68949, 32.0, 3.0, env={})
    assert 2.5 * H < est < 4.0 * H, est
    assert abs(t - he.COMPUTE_SAFETY * est) < 1e-6 and t > 7200.0 / 0.65 * 2, (t, est)
    assert "x the estimate" in how, how
    # Absaraka: 64,090 cells, 40 h, 6 s; ~21 min on one core: the estimate is not below it
    t, est, _ = he.compute_timeout(64090, 40.0, 6.0, env={})
    assert est >= 21 * 60 and t >= 3 * 21 * 60, est
    # more cells, a longer simulation, a smaller base step: proportionally longer
    e1 = he.compute_estimate_s(10000, 10.0, 5.0)
    assert abs(he.compute_estimate_s(20000, 10.0, 5.0) - 2 * e1) < 1e-6
    assert abs(he.compute_estimate_s(10000, 20.0, 5.0) - 2 * e1) < 1e-6
    assert abs(he.compute_estimate_s(10000, 10.0, 2.5) - 2 * e1) < 1e-6
    # floor 2 h, cap 48 h, unknown size: the floor
    assert he.compute_timeout(2000, 6.0, 10.0, env={})[0] == he.COMPUTE_MIN_S == 2 * H
    assert he.compute_timeout(500000, 48.0, 1.0, env={})[0] == he.COMPUTE_MAX_S == 48 * H
    t, est, how = he.compute_timeout(None, 32.0, 3.0, env={})
    assert t == 2 * H and est is None and "unknown" in how


@test
def hecras_engine_compute_timeout_env_override():
    env = {he.ENV_TIMEOUT: "7200"}
    t, est, how = he.compute_timeout(68949, 32.0, 3.0, env=env)
    assert t == 7200.0 and est > 7200.0 and he.ENV_TIMEOUT in how, (t, est, how)
    # above the cap too (the override is the user's call)
    assert he.compute_timeout(68949, 32.0, 3.0, env={he.ENV_TIMEOUT: "200000"})[0] == 200000
    assert he.compute_timeout(None, None, None, env={he.ENV_TIMEOUT: "90"})[0] == 90.0
    # empty, zero or junk: the scaled default
    for v in ("", "0", "soon"):
        assert he.compute_timeout(68949, 32.0, 3.0, env={he.ENV_TIMEOUT: v})[0] > 7200.0, v
    # the preprocess steps keep their own 1,800 s default
    assert he._timeout({}) == 1800.0 and he._timeout(env) == 7200.0


def _plan_project(t, cells=1234, sim="01JAN2000,0000,02JAN2000,0800", interval="3SEC"):
    with open(os.path.join(t, "M.p01"), "w") as f:
        f.write("Plan Title=x\nGeom File=g01\nSimulation Date={}\nComputation Interval={}\n"
                .format(sim, interval))
    with open(os.path.join(t, "M.g01"), "w") as f:
        f.write("Storage Area=Downstream\nStorage Area Is2D=-1\nStorage Area 2D Points= {} \n"
                .format(cells))


@test
def hecras_engine_plan_timing_and_timeout_message():
    t = tempfile.mkdtemp()
    try:
        _plan_project(t)
        pt = he.plan_timing(t, "M")
        assert pt == {"cells": 1234, "sim_hours": 32.0, "base_dt_s": 3.0}, pt
        _plan_project(t, sim="01JAN2000,0000,01JAN2000,1230", interval="1MIN")
        pt = he.plan_timing(t, "M")
        assert pt["sim_hours"] == 12.5 and pt["base_dt_s"] == 60.0, pt
        assert he.plan_timing(os.path.join(t, "none"), "M") == {
            "cells": None, "sim_hours": None, "base_dt_s": None}
    finally:
        shutil.rmtree(t)
    assert he._interval_s("1HOUR") == 3600.0 and he._interval_s("x") is None
    # DWR-3161: stopped at 7,200 s at 65 % of 32 h -> needs ~11,077 s; suggest >= that
    text = "LABEL= Unsteady Flow Computations\n SIMTIME=        0.0009\n" \
           " SIMTIME=       20.7900\n ABSDATE= 01JAN2000\n"
    msg = he.compute_timeout_message(7200.0, 7200.0, text, 32.0, estimate=11120.0)
    assert "did not finish in 7200 s (2.0 h)" in msg and "(65 % done)" in msg, msg
    sug = int(msg.split(he.ENV_TIMEOUT + "=")[1].split()[0])
    assert sug >= 7200 / 0.65 and sug % 900 == 0, (sug, msg)
    assert "Estimate before the run: 11120 s" in msg, msg
    msg = he.compute_timeout_message(60.0, 60.0, "LABEL= Reading Data\n", 32.0)
    assert "printed no simulation time" in msg, msg


def _fake_rasunsteady(t, body):
    exe = os.path.join(t, "RasUnsteady")
    with open(exe, "w") as f:
        f.write("#!/bin/sh\n" + body)
    os.chmod(exe, 0o755)
    for x in ("b01", "x01", "p01.tmp.hdf"):
        open(os.path.join(t, "M." + x), "w").close()
    return {"linux_dir": t, "rasunsteady": exe}


@test
def hecras_engine_compute_logs_the_estimate_and_reports_progress_on_timeout():
    if os.name != "posix":
        return
    t = tempfile.mkdtemp()
    try:
        _plan_project(t, cells=50000)
        d = _fake_rasunsteady(t, "echo ' SIMTIME=        8.0000'\nsleep 30\n")
        logs = []
        try:
            he.compute(t, "M", d=d, env={he.ENV_TIMEOUT: "1"}, log=logs.append)
            raise AssertionError("must time out")
        except he.HecRasEngineError as e:
            msg = str(e)
        assert "(25 % done)" in msg and he.ENV_TIMEOUT + "=" in msg, msg
        start = [m for m in logs if m.startswith("HEC-RAS compute: 50,000 cells")]
        assert start and "32 h simulated" in start[0] and "3 s base step" in start[0], logs
        assert "timeout 1 s" in start[0] and "override" in start[0], start
        assert any(m.startswith("ERROR: Linux RasUnsteady did not finish") for m in logs)
        assert "SIMTIME" in open(os.path.join(t, "linux_run", "run.log")).read()
        # a run that finishes: the results HDF, the timing in the log, the scaled default
        d = _fake_rasunsteady(t, "echo ' SIMTIME=       32.0000'\n"
                                 "echo 'Finished Unsteady Flow Simulation'\n")
        logs = []
        out = he.compute(t, "M", d=d, env={}, log=logs.append)
        assert out.endswith("M.p01.hdf") and os.path.exists(out)
        start = [m for m in logs if m.startswith("HEC-RAS compute: 50,000 cells")][0]
        want = he.compute_timeout(50000, 32.0, 3.0, env={})[0]           # 3 x 8,064 s
        assert "timeout {:.0f} s".format(want) in start and "x the estimate" in start, start
        assert any("RasUnsteady finished in" in m for m in logs), logs
    finally:
        shutil.rmtree(t)
