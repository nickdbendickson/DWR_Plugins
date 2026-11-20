"""3.26.0: LISFLOOD-FP sub-grid river channels (core/sgc.py, lisflood.write_inputs /
read_results, the arrival-depth patch of the SGC solver, scripts/patch_lisflood.py edit 4)."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test, model_test


def _tmp():
    return tempfile.mkdtemp(prefix="ndbr_sgc_")


def _valley(nr=40, nc=200, dx=20.0):
    """A valley sloping east (0.2 %) with 2 % side slopes: the thalweg is the middle row."""
    from nd_breach_runner.core.grid import Grid
    X, Y = np.meshgrid((np.arange(nc) + 0.5) * dx, (np.arange(nr) + 0.5) * dx)
    z = 100.0 - 0.002 * X + 0.02 * np.abs(Y - nr * dx / 2)
    return Grid(z[::-1], 0.0, nr * dx, dx)


def _hy():
    from nd_breach_runner.core.breach import Hydrograph
    return Hydrograph(np.array([0.0, 1800.0, 3600.0]), np.array([60.0, 60.0, 0.0]), "t")


@test
def sgc_hydraulic_geometry_is_bieger_2015_us():
    from nd_breach_runner.core import sgc
    w, d = sgc.hydraulic_geometry(np.array([1.0, 100.0]))
    assert np.allclose(w, [2.70, 2.70 * 100 ** 0.352]) and np.allclose(d, [0.30, 0.30 * 100 ** 0.213])
    assert sgc.BIEGER_2015_US == (2.70, 0.352, 0.30, 0.213)


@test
def sgc_flow_accumulation_counts_the_cells_above_and_the_area_outside():
    from nd_breach_runner.core import sgc
    from nd_breach_runner.core.grid import Grid
    # a plane tilted east: every row drains to its own east edge cell
    z = np.tile(np.linspace(10.0, 0.0, 10), (3, 1))
    g = Grid(z, 0.0, 30.0, 10.0)
    a = sgc.flow_accumulation(g)
    cell = 1e-4
    assert np.allclose(a[:, -1], 10 * cell) and np.allclose(a[:, 0], cell)
    a2 = sgc.flow_accumulation(g, [(1, 0, 5.0)])
    assert np.isclose(a2[1, -1], 5.0 + 10 * cell) and np.isclose(a2[0, -1], 10 * cell)


@test
def sgc_channels_follow_the_valley_and_take_the_dam_watershed():
    from nd_breach_runner.core import sgc
    dem = _valley()
    rc = dem.rc(30.0, 405.0)
    ch = sgc.build(dem, inflow_rc=rc, upstream_km2=50.0, start="empty")
    mid = ch.width[rc[0], rc[1]:]
    assert (mid > 0).all(), "the thalweg below the inflow is one channel"
    # sized from 50 km2 + the valley: W = 2.70 A^0.352, capped at the 20 m cell
    assert np.isclose(ch.width[rc[0], rc[1]], min(20.0, 2.70 * ch.area_km2[rc[0], rc[1]] ** 0.352))
    assert np.isclose(ch.depth[rc[0], -1], 0.30 * ch.area_km2[rc[0], -1] ** 0.213)
    assert ch.info["area_at_inflow_km2"] > 50.0 and ch.info["upstream_km2"] == 50.0
    assert np.allclose(ch.bed[ch.mask], dem.data[ch.mask] - ch.depth[ch.mask])
    assert ch.start_volume_m3() == 0.0 and ch.capacity_m3() > 0
    # without the dam's area only valley cells drain enough: far fewer (or no) channel cells
    small = sgc.build(dem, inflow_rc=rc, upstream_km2=None, start="empty", min_area_km2=0.1)
    assert small.info["cells"] > 0
    assert small.width[rc[0], -1] < ch.width[rc[0], -1]
    assert "was not given" in sgc.describe(small.info)


@test
def sgc_start_modes():
    from nd_breach_runner.core import sgc
    dem = _valley()
    rc = dem.rc(30.0, 405.0)
    full = sgc.build(dem, inflow_rc=rc, upstream_km2=50.0, start="full")
    assert np.allclose(full.start_depth[full.mask], full.depth[full.mask])
    flat = np.zeros(dem.data.shape, bool)
    flat[:, :50] = True
    auto = sgc.build(dem, inflow_rc=rc, upstream_km2=50.0, start="auto", flat_water=flat)
    assert (auto.start_depth[:, 50:] == 0).all() and (auto.start_depth[rc[0], rc[1]:50] > 0).all()
    try:
        sgc.build(dem, start="half")
        raise AssertionError("bad start mode accepted")
    except sgc.SGCError:
        pass


@test
def sgc_par_and_start_file():
    from nd_breach_runner.core import lisflood as lf, sgc
    from nd_breach_runner.core.grid import Grid
    dem = _valley()
    ch = sgc.build(dem, inflow_rc=dem.rc(30.0, 405.0), upstream_km2=50.0, start="full")
    d = _tmp()
    try:
        st = lf.LisfloodSettings("/no/lisflood", sim_time_s=60.0, sgc=ch)
        par = lf.write_inputs(d, dem, (30.0, 405.0), _hy(), st)
        txt = open(par, encoding="utf-8").read()
        for k in ("SGCwidth run.sgcwidth.asc", "SGCbank run.sgcbank.asc",
                  "SGCbed run.sgcbed.asc", "SGCn 0.0350", "startfile run.start", "acceleration"):
            assert k in txt, k
        w = Grid.read_asc(os.path.join(d, "run.sgcwidth.asc")).data
        assert np.allclose(w, ch.width, atol=1e-3)
        start = Grid.read_asc(os.path.join(d, "run.start")).data
        assert np.allclose(start, ch.start_depth, atol=1e-3)
        # a reservoir's initial depth is kept and the channel's own depth goes under it
        init = dem.like(np.where(np.arange(dem.nx)[None, :] < 5, 2.0, 0.0) * np.ones(dem.data.shape))
        st2 = lf.LisfloodSettings("/no/lisflood", sim_time_s=60.0, sgc=ch, initial_depth=init)
        lf.write_inputs(d, dem, (30.0, 405.0), _hy(), st2)
        start2 = Grid.read_asc(os.path.join(d, "run.start")).data
        assert np.allclose(start2, init.data + ch.start_depth, atol=1e-3)
        # ACC only, and never the built-in engine
        for bad in (dict(solver=lf.SOLVER_FV2), dict(solver=lf.SOLVER_FV1)):
            try:
                lf.LisfloodSettings("/no/lisflood", sgc=ch, **bad)
                raise AssertionError("SGC accepted with " + str(bad))
            except lf.ModelError:
                pass
        try:
            lf.write_inputs(d, dem, (30.0, 405.0), _hy(),
                            lf.LisfloodSettings(lf.BUILTIN_EXE, sim_time_s=60.0, sgc=ch))
            raise AssertionError("built-in engine accepted SGC")
        except lf.ModelError:
            pass
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def sgc_results_are_depth_above_the_bank():
    """The SGC solver's depth in a channel cell is from the bed: the Runner reports it above the
    bank, a flood inside the channel is dry ground, and the WSE comes from the DEM."""
    from nd_breach_runner.core import lisflood as lf, sgc
    dem = _valley()
    ch = sgc.build(dem, inflow_rc=dem.rc(30.0, 405.0), upstream_km2=50.0, start="empty")
    d = _tmp()
    try:
        os.makedirs(os.path.join(d, "results"))
        loc = lf.local_grid(dem)
        h = np.where(ch.mask, ch.depth + 0.5, 0.0)       # 0.5 m over the bank in the channel
        h[0, 0] = 0.8                                     # and a floodplain cell
        rowc = np.argwhere(ch.mask)[0]
        h[rowc[0], rowc[1]] = ch.depth[rowc[0], rowc[1]] * 0.5   # half full: still in the channel
        loc.like(h).write_asc(os.path.join(d, "results", "run.max"))
        loc.like(np.full(h.shape, 99.0)).write_asc(os.path.join(d, "results", "run.mxe"))
        st = lf.LisfloodSettings("/no/lisflood", sim_time_s=60.0, sgc=ch)
        env = lf.read_results(d, dem, st, _hy())
        dep = env.depth.data
        m = ch.mask.copy()
        m[rowc[0], rowc[1]] = False
        assert np.allclose(dep[m], 0.5, atol=2e-3)
        assert dep[rowc[0], rowc[1]] == 0.0 and np.isclose(dep[0, 0], 0.8, atol=1e-3)
        assert np.allclose(env.wse.data[m], dem.data[m] + 0.5, atol=2e-3)
        assert env.stats["sgc"]["cells"] == ch.info["cells"]
        assert env.stats["sgc_capacity_acft"] > 0
        assert not any("flow sections" in n for n in env.notes)
        st_g = lf.LisfloodSettings("/no/lisflood", sim_time_s=60.0, sgc=ch,
                                   gauges=[{"name": "g", "xy": (100.0, 400.0), "direction": "E",
                                            "width_m": 200.0}])
        env_g = lf.read_results(d, dem, st_g, _hy())
        assert any("flow sections" in n and "not be used" in n for n in env_g.notes)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def sgc_settings_key_and_solver_gate():
    from nd_breach_runner.core import workflow, breach as br
    st = workflow.RunSettings()
    assert st.lisflood_sgc is False
    k = workflow.key_settings(st)
    assert not any(x.startswith("sgc_") or x == "lisflood_sgc" for x in k)
    k2 = workflow.key_settings(workflow.RunSettings(lisflood_sgc=True, sgc_upstream_sqmi=20.0))
    assert k2["lisflood_sgc"] is True and k2["sgc_upstream_sqmi"] == 20.0
    d = _tmp()
    try:
        for kw, word in ((dict(lisflood_solver="fv2"), "ACC solver only"),
                         (dict(lisflood_exe=workflow.lfmod.BUILTIN_EXE), "built-in engine"),
                         (dict(sgc_start="half"), "auto, empty or full")):
            s = workflow.RunSettings(lisflood_sgc=True, **kw)
            try:
                workflow._check_solver(s, d)
                raise AssertionError("accepted " + str(kw))
            except br.BreachInputError as e:
                assert word in str(e), (kw, str(e))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def sgc_near_dam_refine_runs_without_channels():
    from nd_breach_runner.core import refine, workflow
    from nd_breach_runner.core.breach import Hydrograph
    st = workflow.RunSettings(lisflood_sgc=True)
    hy = Hydrograph(np.array([0.0, 60.0]), np.array([1.0, 0.0]), "t")
    f = refine.fine_settings(st, 1.0, 10.0, 3600.0, hy, hy)
    assert f.lisflood_sgc is False and st.lisflood_sgc is True


@test
def sgc_patch_edits_apply_to_the_sgc_solver():
    """scripts/patch_lisflood.py edit 4 on the SGC solver's own lines (copied from LISFLOOD-FP
    8.3), applied twice."""
    import importlib.util
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "patch_lisflood", os.path.join(here, "scripts", "patch_lisflood.py"))
    pl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pl)
    edits = [e for e in pl.EDITS if e[0].startswith("lisflood2/")]
    assert len([e for e in edits if e[0] == "lisflood2/sgm_fast.cpp"]) == 3
    src = {
        "lisflood2/sgm_fast.cpp":
            '#include "../utility.h"\n#include "sgm_fast.h"\n#include <math.h>\n'
            "\t\t\t\tif (h_grid[index] > depth_thresh)\n\t\t\t\t{\n"
            "\t\t\t\t\tif (initHtm_grid[index] == NULLVAL)\n"
            "\t\t\t\t\t\tinitHtm_grid[index] = current_time_hours;\n"
            "\t\t\t\tif (h_tmp > depth_thresh)\n\t\t\t\t{\n"
            "\t\t\t\t\tif (initHtm_grid[grid_index] == NULLVAL)\n"
            "\t\t\t\t\t\tinitHtm_grid[grid_index] = current_time_hours;\n",
        "lisflood2/lisflood_processing.cpp":
            '#include "DataTypes.h"\n#include "sgm_fast.h"\n#ifdef __unix__\n#endif\n'
            "{\n\tSolverptr->itrn_time_now = Solverptr->itrn_time;\n\n"
            "\t// Populating Tstep variables prior to start of simulation\n",
    }
    d = _tmp()
    try:
        for rel, txt in src.items():
            os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
            with open(os.path.join(d, rel), "w") as f:
                f.write(txt)
        for _ in range(2):
            for rel, old, new, marker in edits:
                path = os.path.join(d, rel)
                t = open(path).read()
                if marker in t:
                    continue
                assert old in t, (rel, old)
                with open(path, "w") as f:
                    f.write(t.replace(old, new))
        sg = open(os.path.join(d, "lisflood2/sgm_fast.cpp")).read()
        assert sg.count("> ndbreach_sgc_inittm_depth)") == 2
        assert "NUMERIC_TYPE ndbreach_sgc_inittm_depth = C(0.0);" in sg
        lp = open(os.path.join(d, "lisflood2/lisflood_processing.cpp")).read()
        assert "ndbreach_sgc_inittm_depth = Parptr->inittm_depth;" in lp
        assert '"nd-breach-runner: SGC arrival depth' in lp
        assert lp.count("extern NUMERIC_TYPE ndbreach_sgc_inittm_depth") == 1
    finally:
        shutil.rmtree(d, ignore_errors=True)


@model_test
def sgc_runs_on_the_real_lisflood():
    """The patched LISFLOOD-FP passes the SGC check (arrival at the arrival depth above the
    bank), a run with channels conveys water in the channel (the flood front gets further in the
    same time, no deeper), and the report records the channels."""
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer
    from nd_breach_runner.tests.test_v320 import _dam
    exe = os.environ.get("NDBREACH_LISFLOOD", "")
    assert exe, "set NDBREACH_LISFLOOD"
    d = _tmp()
    try:
        chk = lf.check_lisflood(exe, os.path.join(d, "chk"), sgc=True)
        assert chk["ok"] and chk["patched"] and chk["velocity"], chk
    finally:
        shutil.rmtree(d, ignore_errors=True)
    workflow._SOLVER_CHECKED.clear()
    out = {}
    for on in (False, True):
        d = _tmp()
        try:
            st = workflow.RunSettings(cell_size_m=20.0, initial_length_mi=1.0, half_width_mi=0.4,
                                      max_iterations=1, lisflood_exe=exe, threads=2,
                                      sim_time_hr=1.0, lisflood_sgc=on, sgc_upstream_sqmi=20.0,
                                      sgc_min_area_km2=0.5)
            r = workflow.run_dam(_dam(), (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                                 os.path.join(d, "T1"))
            with open(r.report_json, encoding="utf-8") as f:
                rep = json.load(f)
            dep = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
            wet = np.isfinite(dep) & (dep > 0.03)
            out[on] = (int(np.nonzero(wet)[1].max()), float(np.nanmax(dep)), rep.get("sgc"))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    assert out[False][2] is None and out[True][2]["cells"] > 0, out
    # the channel carries the breach water: in the same hour the flood reaches further down the
    # valley (the synthetic valley runs east) and stands no deeper above the ground
    assert out[True][0] > out[False][0] and out[True][1] <= out[False][1], out


@test
def sgc_from_boanngrid_honors_the_notch_and_refuses_a_frozen_crest():
    """Step 3a: SGC from the BoannGrid network (D4 + reconnection), not raw-DEM D8.

    The carved crossing lowers the bank the channel is built on; the raw path does not.
    A freeze mask on the road crest refuses the carve, leaves the crest standing, and the
    delta-log then blocks export of that DEM. build() itself is not this path, so the
    published raw-DEM SGC numbers are unchanged."""
    import os
    import tempfile
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.core import sgc
    from nd_breach_runner.core.metalterrain import cases as C
    from nd_breach_runner.core.metalterrain import delta_log as DL
    from nd_breach_runner.core.metalflood import backends as mfb
    from nd_breach_runner.tests.registry import Skip
    if os.name == "nt" or not mfb.find_compiler():
        raise Skip("no C++ compiler for the BoannGrid CPU build")
    dx = 10.0
    z = C.channel_with_barrier(ny=40, nx=80, dx=dx, seed=0)
    ny, nx = z.shape
    g = Grid(z, 0.0, ny * dx, dx)
    cr, bc = ny // 2, nx // 2
    raw = sgc.build(g, min_area_km2=0.001, start="empty")
    cond = sgc.from_boanngrid(g, min_area_km2=0.001, start="empty", backend="cpu",
                              min_accum_cells=5)
    assert cond.info["source"] == "boanngrid-d4" and cond.info["conn"] == 4
    assert cond.info["flow_converged"], cond.info
    assert cond.info["freeze_refusals"] == 0
    assert cond.info["crossings_carved"] >= 1, cond.info
    assert cond.dem.data[cr, bc] < z[cr, bc] - 0.2, (cond.dem.data[cr, bc], z[cr, bc])
    assert abs(float(raw.dem.data[cr, bc]) - float(z[cr, bc])) < 1e-9
    freeze = np.zeros(z.shape, bool)
    freeze[cr, bc:bc + 2] = True
    log = DL.DeltaLog(run_id="SGC-FREEZE")
    held = sgc.from_boanngrid(g, min_area_km2=0.001, start="empty", backend="cpu",
                              min_accum_cells=5, freeze_mask=freeze, delta_log=log)
    assert held.info["freeze_refusals"] >= 1, held.info
    assert float(held.dem.data[cr, bc]) >= float(z[cr, bc]) - 1e-6
    d = tempfile.mkdtemp()
    try:
        try:
            log.export_conditioned_dem(held.dem.data, os.path.join(d, "no.asc"), dx=dx)
            raise AssertionError("a frozen-crest conditioned DEM was exported")
        except DL.ExportRefused:
            pass
        assert not os.path.exists(os.path.join(d, "no.asc"))
    finally:
        shutil.rmtree(d, True)
        log.close()
