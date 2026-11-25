"""Runner staging for a breach lowered into the terrain. Off, the checkpoint key and the
report omit the switch and the deck is today's breach hydrograph. On, the deck has no
breach point source, the pool stays upstream, and the products leave the starting
reservoir out.

    python3 -m nd_breach_runner.tests.test_terrain_breach_runner
"""
import os
import tempfile

import numpy as np

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import cascade
from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import terrain_breach as tb
from nd_breach_runner.core import workflow
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import ACFT, CFS, FT
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.synthetic import npy_writer


def _dam():
    return br.DamData(dam_id="T-TB", dam_height_ft=40.0, storage_top_acft=200.0,
                      top_of_dam_ft=120.0 / FT, crest_width_ft=20.0, us_slope_h=3.0,
                      ds_slope_h=3.0, crest_length_ft=400.0)


def _hy(**over):
    p = {"bottom_width_ft": 40.0, "side_slope_h": 1.0, "formation_time_hr": 0.5,
         "starting_wse_ft": 120.0 / FT, "breach_bottom_ft": 100.0 / FT,
         "storage_at_failure_acft": 200.0, "regression_peaks_cfs": {"froehlich": 5000.0}}
    p.update(over)

    class H:
        params = p
        t_ref_s = 0.0
        peak_cfs = 4200.0
    return H()


def _settings(**k):
    st = workflow.RunSettings()
    st.breach_in_terrain = True
    st.lisflood_exe = lf.GPU_EXE
    st.tier = workflow.TIER_LISFLOOD
    st.scenario = workflow.SCENARIO_SUNNY
    st.breach_method = br.METHOD_PIPING
    st.user_hydrograph = None
    st.user_hydrograph_obj = None
    st.proposed = None
    for name, value in k.items():
        setattr(st, name, value)
    return st


def _valley():
    """Flat ground at 100 m, a crest line north-south. Downstream is east and is also at
    100 m, so only the upstream test keeps the pool off the valley below the dam."""
    ny, nx, dx = 80, 80, 10.0
    z = np.full((ny, nx), 100.0)
    dem = Grid(z, 0.0, ny * dx, dx)
    c = 40
    line = [(dem.xy(r, c)[0], dem.xy(r, c)[1]) for r in (2, ny // 2, ny - 3)]
    dam_xy = dem.xy(ny // 2, c)
    inflow = dem.xy(ny // 2, c + 3)
    return dem, line, dam_xy, inflow


@test
def terrain_breach_setting_is_out_of_the_key_and_the_report_when_off():
    off = workflow.RunSettings()
    on = workflow.RunSettings()
    on.breach_in_terrain = True
    assert "breach_in_terrain" not in off.as_dict()
    assert on.as_dict()["breach_in_terrain"] is True
    assert "breach_in_terrain" not in workflow.key_settings(off)
    assert workflow.key_settings(on)["breach_in_terrain"] is True
    assert workflow.key_settings(off) == workflow.key_settings(workflow.RunSettings())
    import importlib
    ncli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    assert ncli.MODEL_KEYS["breach_in_terrain"] == ("breach_in_terrain", ("bool",))
    opts = type("O", (), {"lisflood": lf.GPU_EXE})()
    st = ncli.build_settings(ncli.Config({"model": {"breach_in_terrain": True}}, ""), opts,
                             need_models=False)
    assert st.breach_in_terrain is True
    plain = ncli.build_settings(ncli.Config({"model": {}}, ""), opts, need_models=False)
    assert plain.breach_in_terrain is False
    assert "breach_in_terrain" not in workflow.key_settings(plain)
    assert "breach_in_terrain" not in plain.as_dict()


@test
def terrain_breach_is_refused_unless_the_run_can_lower_the_bed():
    tb.refuse(workflow.RunSettings())                       # off: nothing to check
    tb.refuse(_settings())                                  # the one combination that runs
    cases = (
        {"lisflood_exe": lf.GPU_SGC_EXE},
        {"lisflood_exe": lf.GPU_SUBFV_EXE},
        {"lisflood_exe": lf.GPU_SUBLI_EXE},
        {"lisflood_exe": lf.BUILTIN_EXE},
        {"lisflood_exe": "/usr/bin/false"},
        {"tier": workflow.TIER_BOTH},
        {"tier": workflow.TIER_HECRAS},
        {"scenario": workflow.SCENARIO_FLOOD},
        {"user_hydrograph_obj": object()},
        {"user_hydrograph": "given.csv"},
        {"breach_method": br.METHOD_PIPING_ORIFICE},
        {"breach_method": "user"},
        {"proposed": {"phrase": "x"}},
    )
    for kw in cases:
        try:
            tb.refuse(_settings(**kw))
        except br.BreachInputError as e:
            assert "off" in str(e).lower() or "turn" in str(e).lower(), (kw, e)
        else:
            raise AssertionError("accepted {}".format(kw))
    st = _settings()
    try:
        cascade.run_cascade([], st, None, None, tempfile.gettempdir())
    except br.BreachInputError as e:
        assert "cascade" in str(e).lower()
    else:
        raise AssertionError("a cascade was allowed to lower the bed")


@test
def terrain_breach_deck_has_no_breach_point_source_and_a_rerun_keeps_the_file():
    dem, _line, _xy, inflow = _valley()
    spec = {
        "version": 1, "start_s": 0.0, "formation_s": 20.0, "invert_start_m": 115.0,
        "invert_final_m": 100.0, "bottom_width_final_m": 20.0, "side_slope_h": 1.0,
        "update_s": 10.0, "cells": [[40, 40, 0.0]], "pool_volume_m3": 1000.0,
        "_pool": np.zeros(dem.data.shape),
    }
    spec["_pool"][10:30, 10:30] = 1.0
    hy = br.Hydrograph(np.array([0.0, 60.0, 120.0]), np.array([0.0, 25.0, 0.0]), "t")
    d = tempfile.mkdtemp(prefix="tb_deck_")
    try:
        st = lf.LisfloodSettings(lf.GPU_EXE, sim_time_s=120.0, terrain_breach=spec)
        lf.write_inputs(d, dem, inflow, hy, st)
        bci = open(os.path.join(d, "run.bci"), encoding="utf-8").read()
        bdy = open(os.path.join(d, "run.bdy"), encoding="utf-8").read()
        par = open(os.path.join(d, "run.par"), encoding="utf-8").read()
        assert "QVAR breach" not in bci
        assert "breach\n" not in bdy
        assert "breachfile run.breach.json" in par
        assert os.path.isfile(os.path.join(d, "run.breach.json"))
        assert os.path.isfile(os.path.join(d, "run.pool.asc"))
        body = open(os.path.join(d, "run.breach.json"), encoding="utf-8").read()
        assert "pool_volume_m3" not in body and "_pool" not in body
        lf.write_inputs(d, dem, inflow, hy, st)             # a stability re-run writes it again
        assert "breachfile run.breach.json" in open(os.path.join(d, "run.par"), encoding="utf-8").read()
        plain = os.path.join(d, "off")
        st0 = lf.LisfloodSettings(lf.GPU_EXE, sim_time_s=120.0)
        lf.write_inputs(plain, dem, inflow, hy, st0)
        bci0 = open(os.path.join(plain, "run.bci"), encoding="utf-8").read()
        bdy0 = open(os.path.join(plain, "run.bdy"), encoding="utf-8").read()
        assert "QVAR breach" in bci0
        assert bdy0.startswith("breach hydrograph")
        assert "breachfile" not in open(os.path.join(plain, "run.par"), encoding="utf-8").read()
    finally:
        import shutil
        shutil.rmtree(d, True)


@test
def terrain_breach_pool_stays_upstream_of_a_crest_at_the_starting_level():
    dem, line, dam_xy, inflow = _valley()
    dam = _dam()
    dam.crest_line_xy = line
    top = 120.0
    lf.raise_crest(dem, dam_xy, inflow, top, line_xy=line)
    geom = tb.prepare(dam, _hy(), [line], dam_xy, (1.0, 0.0), inflow)
    spec, depth = tb.stage(dem, geom, [line])
    pool = spec["_pool"] > 0
    assert pool.any()
    crest_c = 40
    assert int(pool[:, crest_c + 1:].sum()) == 0, "pool crossed the crest"
    assert float(depth[pool].min()) > 0.0
    # the crest cells are in the notch, so the wall is not left standing at the top of dam
    cols = {c for r, c, _a in spec["cells"]}
    assert crest_c in cols
    assert tb.resolution_warning(spec, dem.dx) is None or spec["bottom_width_final_m"] < 3 * dem.dx
    narrow = dict(spec, bottom_width_final_m=dem.dx)
    assert tb.resolution_warning(narrow, dem.dx)
    far = dict(spec, pool_volume_m3=spec["pool_volume_m3"] * 3.0,
               _storage_at_failure_acft=spec["_storage_at_failure_acft"])
    # force a gap: storage curve 200 ac-ft, pool made tiny
    tiny = dict(spec, pool_volume_m3=10.0)
    assert tb.storage_warning(tiny)


@test
def terrain_breach_mass_uses_the_pool_and_products_omit_the_start():
    dem = Grid(np.zeros((4, 5)), 0.0, 40.0, 10.0)
    h = np.array([[0.0, 0.2, 1.5, 0.05, 0.0],
                  [0.0, 2.0, 2.0, 0.0, 0.0],
                  [0.0, 0.4, 0.0, 0.0, 0.0],
                  [0.0, 0.0, 0.0, 0.0, 0.0]])
    res = tempfile.mkdtemp(prefix="tb_mass_")
    try:
        os.makedirs(os.path.join(res, "results"))
        dem.like(h).write_asc(os.path.join(res, "results", "run.max"))
        dem.like(np.where(h > 0, 0.3, 0.0)).write_asc(os.path.join(res, "results", "run.maxVc"))
        dem.like(np.where(h > 0, 0.1, 0.0)).write_asc(os.path.join(res, "results", "run.inittm"))
        dem.like(h).write_asc(os.path.join(res, "results", "run.mxe"))
        with open(os.path.join(res, "results", "run.mass"), "w", encoding="utf-8") as f:
            f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain\n")
            f.write("100.000 1.0000 1.0000 10 200.0 50.000 1.0000 0.0000 0.0000 0.000000 "
                    "0.0000 0.0000\n")
        with open(os.path.join(res, "results", "run.pool"), "w", encoding="utf-8") as f:
            f.write("time_s,pool_volume_m3,outflow_m3s\n")
            f.write("0.000000,1200.000000,0.000000\n")
            f.write("50.000000,800.000000,12.500000\n")
            f.write("100.000000,400.000000,8.000000\n")
        spec = {"pool_volume_m3": 1200.0, "_routed_peak_cfs": 4200.0,
                "_storage_at_failure_acft": 200.0, "_regression_peaks_cfs": {"a": 3000.0, "b": 8000.0},
                "bottom_width_final_m": 20.0}
        hy = br.Hydrograph(np.array([0.0, 100.0]), np.array([80.0, 0.0]), "t")
        st = lf.LisfloodSettings(lf.GPU_EXE, sim_time_s=100.0, terrain_breach=spec,
                                 arrival_depth_m=0.1)
        env = lf.read_results(res, dem, st, hy)
        assert env.stats["peak_inflow_input_cfs"] is None
        assert abs(env.stats["injected_acft"] - 1200.0 / ACFT) < 0.05
        assert env.stats["terrain_breach_peak_cfs"] == round(12.5 / CFS, 0)
        assert env.stats["routed_peak_cfs_not_injected"] == 4200.0
        entry = tb.report_entry(spec, tb.read_pool(os.path.join(res, "results", "run.pool")), dem.dx)
        assert entry["peak_outflow_cfs"] == env.stats["terrain_breach_peak_cfs"]
        assert entry["routed_peak_cfs_not_injected"] == 4200.0
        assert "not injected" in entry["note"]
        assert "crest" in entry["note"]
        # products: the cell that started full is not flood and has no arrival
        init = dem.like(np.array([[0.0, 0.0, 1.5, 0.0, 0.0],
                                   [0.0, 2.0, 0.2, 0.0, 0.0],
                                   [0.0, 0.0, 0.0, 0.0, 0.0],
                                   [0.0, 0.0, 0.0, 0.0, 0.0]]))
        env.depth = dem.like(h)
        env.velocity = dem.like(np.where(h > 0, 0.3, 0.0))
        env.arrival_hr = dem.like(np.where(h > 0, 0.05, np.nan))
        out = tempfile.mkdtemp(prefix="tb_prod_")
        try:
            workflow._write_pass_products(out, dem, env, 0.1 * FT, npy_writer, 1, init)
            depth = np.load(os.path.join(out, "Depth (Max).npy"))
            assert np.isnan(depth[1, 1]) or depth[1, 1] == 0.0
            assert depth[0, 1] > 0.0
            arr = np.load(os.path.join(out, "Arrival Time.npy"))
            assert np.isnan(arr[1, 1]) and np.isnan(arr[0, 2]) and np.isnan(arr[1, 2])
            assert np.isfinite(arr[0, 1])
        finally:
            import shutil
            shutil.rmtree(out, True)
        fa = workflow._final_arrays(env, 0.1 * FT, 0.0, init, blank_start_arrival=True)
        assert np.isnan(fa["depth"][1, 1]) or fa["depth"][1, 1] == 0.0
        assert np.isnan(fa["arrival"][1, 2])
        assert np.isfinite(fa["depth"][1, 2]) and fa["depth"][1, 2] > 0.0
        fa0 = workflow._final_arrays(env, 0.1 * FT, 0.0, init, blank_start_arrival=False)
        assert np.isfinite(fa0["arrival"][1, 2])
    finally:
        import shutil
        shutil.rmtree(res, True)


if __name__ == "__main__":
    for fn in (terrain_breach_setting_is_out_of_the_key_and_the_report_when_off,
               terrain_breach_is_refused_unless_the_run_can_lower_the_bed,
               terrain_breach_deck_has_no_breach_point_source_and_a_rerun_keeps_the_file,
               terrain_breach_pool_stays_upstream_of_a_crest_at_the_starting_level,
               terrain_breach_mass_uses_the_pool_and_products_omit_the_start):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Exception as e:
            print("FAIL", fn.__name__, type(e).__name__, e, flush=True)
            raise
