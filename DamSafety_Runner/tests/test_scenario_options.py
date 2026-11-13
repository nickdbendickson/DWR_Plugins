"""3.19.0 (Nick, 2026-11-13): the scenario choices.

* Sunny day still starts at the top of dam with a piping breach (ND 5.1.1) unless the user
  changes it; a normal-pool or stated-elevation start is held as its depth below the top of dam
  and uses the storage at that pool as Vw.
* Flood (PMF) with NO breach: the inflow is routed through the spillway rating, an auxiliary
  spillway weir, the outlets and over the crest, and that outflow is the flood modelled.
* The default run is unchanged: same numbers, same dam record, same checkpoint key.

Synthetic numbers; the end-to-end run (a model test) uses the built-in engine."""
import dataclasses
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import adequacy
from nd_breach_runner.core import breach as br
from nd_breach_runner.core import flood as fl
from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import workflow
from nd_breach_runner.core.units import ACFT, FT
from nd_breach_runner.tests.registry import model_test, test
from nd_breach_runner.tests.synthetic import SyntheticTerrain, npy_writer


def _dam(**k):
    d = dict(dam_id="S5", dam_height_ft=30.0, storage_top_acft=2000.0, top_of_dam_ft=1100.0,
             surface_area_top_ac=150.0, crest_length_ft=600.0)
    d.update(k)
    return br.DamData(**d)


def _inflow(**k):
    return fl.FloodInputs.triangle(9000.0, 6.0, 30.0, normal_pool_ft=1090.0,
                                   spillway=fl.Spillway.from_weir(1090.0, 20.0), **k)


# ------------------------------------------------------------------- sunny-day starting pool
@test
def start_pool_default_is_the_top_of_dam_and_leaves_the_record_alone():
    d = _dam()
    h = br.make_hydrograph(d)
    assert h.method == br.METHOD_PIPING
    assert h.params["starting_wse_ft"] == 1100.0 and h.params["starting_wse_rule"] == "top of dam"
    assert h.params["vw_source"] == br.VW_SOURCE_TEXT[br.VW_MAX_STORAGE]
    rec = br.dam_to_dict(d)
    assert "start_below_top_ft" not in rec and "start_wse_rule" not in rec, \
        "a default dam's record (and so its checkpoint key) must not change"
    assert br.dam_to_dict(br.set_start_pool(_dam(), br.START_TOP)) == rec
    assert br.dam_from_dict(rec).start_below_top_ft is None


@test
def start_pool_normal_uses_the_storage_at_that_pool():
    d = br.set_start_pool(_dam(), br.START_NORMAL, 1090.0)
    assert d.start_below_top_ft == 10.0 and d.start_wse_rule == br.START_NORMAL
    top, low = br.make_hydrograph(_dam()), br.make_hydrograph(d)
    p = low.params
    assert p["starting_wse_ft"] == 1090.0 and p["starting_wse_rule"].startswith("normal pool")
    assert p["vw_source"] == br.VW_SOURCE_TEXT[br.VW_START]
    assert abs(p["vw_acft"] - p["storage_at_failure_acft"]) < 0.01 and p["vw_acft"] < 2000.0
    b_m, _, _ = br.froehlich_2008(p["vw_acft"] * ACFT, 30.0 * FT, "piping")
    assert abs(p["avg_breach_width_ft"] - b_m / FT) < 0.06, p
    assert low.peak_cfs < top.peak_cfs and low.volume_m3 < top.volume_m3
    assert any("NOT that case" in n for n in low.notes), low.notes
    # the record keeps it, and a dam read back gives the same hydrograph
    back = br.dam_from_dict(br.dam_to_dict(d))
    assert back.start_below_top_ft == 10.0
    assert br.make_hydrograph(back).peak_cfs == low.peak_cfs


@test
def start_pool_is_relative_so_a_datum_shift_moves_it_with_the_crest():
    d = br.set_start_pool(_dam(), br.START_STATED, 1092.5)
    q0 = br.make_hydrograph(d).peak_cfs
    shifted = dataclasses.replace(d, top_of_dam_ft=d.top_of_dam_ft + 1.31)   # NGVD29 -> NAVD88
    h = br.make_hydrograph(shifted)
    assert abs(h.params["starting_wse_ft"] - (1092.5 + 1.31)) < 0.006
    assert abs(h.peak_cfs - q0) < 1e-6 * q0


@test
def start_pool_refuses_what_it_cannot_do():
    for args, word in (((br.START_STATED, 1101.0), "above the top of dam"),
                       ((br.START_NORMAL, None), "normal pool elevation"),
                       (("lake", 1090.0), "must be one of")):
        try:
            br.set_start_pool(_dam(), *args)
        except br.BreachInputError as e:
            assert word in str(e), e
        else:
            raise AssertionError("no error for {}".format(args))
    try:                                   # no top of dam to measure the pool from
        br.set_start_pool(_dam(top_of_dam_ft=None), br.START_NORMAL, 1090.0)
    except br.BreachInputError as e:
        assert "top of dam elevation too" in str(e)
    else:
        raise AssertionError("a pool with no top of dam was accepted")
    try:                                   # a pool at the breach bottom has nothing to release
        br.make_hydrograph(br.set_start_pool(_dam(), br.START_STATED, 1070.0))
    except br.BreachInputError as e:
        assert "not above the breach bottom" in str(e)
    else:
        raise AssertionError("a pool at the breach bottom was accepted")


@test
def overtopping_below_the_crest_says_so():
    d = br.set_start_pool(_dam(), br.START_NORMAL, 1090.0)
    h = br.make_hydrograph(d, br.METHOD_OVERTOPPING)
    assert h.params["failure_mode"] == "overtopping"
    assert any("cannot overtop the dam from there" in n for n in h.notes), h.notes
    assert not any("cannot overtop" in n for n in br.make_hydrograph(
        _dam(), br.METHOD_OVERTOPPING).notes)


# ------------------------------------------------------------------- flood with no breach
@test
def no_breach_routing_is_the_baseline_of_the_breach_run():
    d = _dam()
    nb = fl.route_flood(d, None, None, _inflow(), breach=False)
    wb = fl.route_flood(d, br.METHOD_PIPING, br.BreachOverrides(), _inflow())
    assert nb.failed is False and nb.no_breach_only and nb.with_breach is None
    assert nb.summary["no_breach_scenario"] is True
    assert nb.summary["peak_pool_ft_no_breach"] == wb.summary["peak_pool_ft_no_breach"]
    assert nb.summary["peak_outflow_no_breach_cfs"] == wb.summary["peak_outflow_no_breach_cfs"]
    h = nb.no_breach
    assert h.method == fl.METHOD_NO_BREACH and h.t_ref_s == 0.0 and h.q_cms[-1] == 0.0
    assert h.peak_cfs < 9000.0, "the reservoir attenuates the inflow"
    assert any(n.startswith("NO BREACH is modelled") for n in nb.notes)
    # the breach method is not looked at when nothing breaches
    assert fl.route_flood(d, br.METHOD_USER, None, _inflow(), breach=False).failed is False


@test
def aux_spillway_weir_lowers_the_pool_and_carries_flow():
    d = _dam()
    plain = fl.route_flood(d, None, None, _inflow(), breach=False)
    fi = _inflow().set_aux_weir(1094.0, 200.0)
    aux = fl.route_flood(d, None, None, fi, breach=False)
    assert aux.summary["peak_pool_ft_no_breach"] < plain.summary["peak_pool_ft_no_breach"]
    assert aux.summary["aux_spillway_peak_cfs"] > 0
    assert aux.no_breach.params["aux_spillway"] == "crest 1094.00 ft, 200 ft long, weir C 2.6"
    assert any(n.startswith("Auxiliary spillway as a weir") for n in aux.notes)
    # the weir alone: Q = C L H^1.5
    assert abs(fi.aux_spillway.q(1096.0 * FT) / br.CFS - 2.6 * 200.0 * 2.0 ** 1.5) < 0.5
    # with a breach the auxiliary spillway is in both routings
    wb = fl.route_flood(d, br.METHOD_PIPING, br.BreachOverrides(), _inflow().set_aux_weir(
        1094.0, 200.0))
    assert wb.summary["peak_pool_ft_no_breach"] == aux.summary["peak_pool_ft_no_breach"]
    # and in the spillway-adequacy routing (the same scheme)
    c0 = br._common(d, 1090.0 * FT, "normal pool", br.VW_POOL)
    r0 = adequacy.route_no_breach(c0.res, _inflow(crest_length_ft=600.0), c0.top_m, 1090.0 * FT)
    r1 = adequacy.route_no_breach(c0.res, _inflow(crest_length_ft=600.0).set_aux_weir(
        1094.0, 200.0), c0.top_m, 1090.0 * FT)
    assert r1["peak_wse_m"] < r0["peak_wse_m"]


@test
def aux_spillway_needs_both_values():
    assert _inflow().set_aux_weir(None, None).aux_spillway is None
    for args in ((1094.0, None), (None, 200.0), (1094.0, 0.0)):
        try:
            _inflow().set_aux_weir(*args)
        except br.BreachInputError as e:
            assert "auxiliary spillway" in str(e)
        else:
            raise AssertionError("accepted {}".format(args))


@test
def no_breach_settings_keep_the_default_key():
    """A run with no auxiliary spillway records its flood settings as before 3.19.0."""
    st = workflow.RunSettings(scenario=workflow.SCENARIO_FLOOD, flood=_inflow())
    assert "aux_spillway" not in st.as_dict()["flood"]
    st.flood.set_aux_weir(1094.0, 200.0)
    assert st.as_dict()["flood"]["aux_spillway"].startswith("crest 1094.00 ft")
    assert workflow.SCENARIOS == ("sunny", "flood", "flood_no_breach")


@test
def no_breach_is_refused_where_there_is_nothing_to_vary():
    st = workflow.RunSettings(scenario=workflow.SCENARIO_FLOOD_NO_BREACH, flood=_inflow())
    try:
        workflow.require_breach(st, "The sensitivity analysis")
    except br.BreachInputError as e:
        assert "varies the breach" in str(e)
    else:
        raise AssertionError("not refused")
    workflow.require_breach(workflow.RunSettings(), "x")          # sunny day: fine
    # the HEC-RAS 2D stage models the breach: its callers refuse the no-breach flood
    assert "HEC-RAS 2D" in workflow.no_breach_tier_problem(
        workflow.SCENARIO_FLOOD_NO_BREACH, workflow.TIER_HECRAS)
    for scen, tier in ((workflow.SCENARIO_FLOOD_NO_BREACH, workflow.TIER_BOTH),
                       (workflow.SCENARIO_FLOOD, workflow.TIER_HECRAS),
                       (workflow.SCENARIO_SUNNY, workflow.TIER_HECRAS)):
        assert workflow.no_breach_tier_problem(scen, tier) == ""


@model_test
def no_breach_flood_runs_end_to_end_on_the_builtin_engine():
    """With the model tests (as every other run_dam test); it needs no LISFLOOD-FP binary."""
    dm = br.DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                    top_of_dam_ft=1850.0, surface_area_top_ac=60.0, crest_length_ft=800.0)
    fi = fl.FloodInputs.triangle(4000.0, 2.0, 8.0, normal_pool_ft=1846.0,
                                 spillway=fl.Spillway.from_weir(1846.0, 20.0))
    fi.set_aux_weir(1848.0, 120.0)
    st = workflow.RunSettings(cell_size_m=40.0, initial_length_mi=1.0, half_width_mi=0.4,
                              max_iterations=1, lisflood_exe=lf.BUILTIN_EXE, threads=1,
                              scenario=workflow.SCENARIO_FLOOD_NO_BREACH, flood=fi,
                              sim_time_hr=3.0, products=False)
    d = tempfile.mkdtemp()
    try:
        warnings = []
        hy, hy_nb, fr = workflow._flood_hydrographs(dm, st, warnings)
        assert hy_nb is None and hy.method == fl.METHOD_NO_BREACH and fr.no_breach_only
        assert any("enters the model at the breach inflow point" in w for w in warnings)
        r = workflow.run_dam(dm, (0.0, 0.0), st, SyntheticTerrain(), npy_writer,
                             os.path.join(d, "T1"))
        s = r.summary
        assert s["scenario"] == "flood_no_breach" and s["no_breach_scenario"] is True
        assert s["breach_occurs"] is False, "nothing fails: Hazard must not read it as a failure"
        assert s["final_model"] != "none" and s["flooded_acres"] > 0 and r.final.get("depth")
        assert not r.baseline and not r.increment, "there is no baseline to subtract"
        assert "after the start of the inflow" in s["units"]
        rep = json.load(open(r.report_json))
        assert rep["scenario"] == "flood_no_breach"
        assert rep["flood"]["no_breach_scenario"] is True and rep["flood"]["failed"] is False
        assert rep["settings"]["flood"]["aux_spillway"].startswith("crest 1848.00 ft")
        assert "incremental" not in rep
        md = open(r.report_md, encoding="utf-8").read()
        assert "NO breach" in md and "Peak outflow, NO breach" in md
        assert "## Flood outflow, no breach" in md and "## Breach (ND 5.1.1)" not in md
        dep = np.load(os.path.splitext(r.final["depth"])[0] + ".npy")
        assert np.nanmax(dep) > 0.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------- the wizard's answers
@test
def wizard_passes_the_scenario_choices_to_the_tool():
    from nd_breach_runner import wizard_logic as wl
    from nd_breach_runner import algorithms as algs
    # the wizard's ids are in the tool's enum order
    assert list(wl.SCENARIO_IDS) == [s[0] for s in algs.SCENARIOS]
    assert list(wl.START_POOL_IDS) == [s[0] for s in algs.START_POOL_CHOICES]
    assert [s[0] for s in algs.BATCH_START_POOLS] == list(wl.START_POOL_IDS[:2])
    assert algs.BATCH_SCENARIOS[3] == algs.SCENARIOS[2][1] and len(algs.BATCH_SCENARIOS) == 4
    a = wl.default_answers("") if hasattr(wl, "default_answers") else None
    base = {"dam": {"DAM_ID": "D1", "HEIGHT_FT": 30.0, "STORAGE_ACFT": 2000.0, "TOP_FT": 1100.0},
            "method": br.METHOD_PIPING, "scenario": workflow.SCENARIO_SUNNY}
    if a is not None:
        assert a["start_pool"] == br.START_TOP and a["method"] == br.METHOD_PIPING
    p = wl.build_rundam_params(dict(base))
    assert p["SCENARIO"] == 0 and "START_POOL" not in p and "START_WSE_FT" not in p
    p = wl.build_rundam_params(dict(base, start_pool=br.START_NORMAL, start_wse_ft=1090.0))
    assert p["START_POOL"] == 1 and p["NORMAL_POOL_FT"] == 1090.0 and "START_WSE_FT" not in p
    p = wl.build_rundam_params(dict(base, start_pool=br.START_STATED, start_wse_ft=1093.0))
    assert p["START_POOL"] == 2 and p["START_WSE_FT"] == 1093.0
    p = wl.build_rundam_params(dict(base, scenario=workflow.SCENARIO_FLOOD_NO_BREACH,
                                    inflow_csv="in.csv", normal_pool_ft=1090.0,
                                    aux_crest_ft=1094.0, aux_length_ft=200.0,
                                    start_pool=br.START_NORMAL, start_wse_ft=1090.0))
    assert p["SCENARIO"] == 2 and p["AUX_CREST_FT"] == 1094.0 and p["AUX_LENGTH_FT"] == 200.0
    assert "TRIGGER" not in p and "START_POOL" not in p, "a flood scenario has its own pool"

    ok = lambda _p: True
    errs, warns = wl.validate_page("scenario", dict(
        base, start_pool=br.START_STATED, start_wse_ft=1093.0), exists=ok)
    assert not errs and any("7 ft below the top of dam" in w for w in warns), (errs, warns)
    errs, _ = wl.validate_page("scenario", dict(base, start_pool=br.START_STATED), exists=ok)
    assert any("starting pool elevation" in e for e in errs)
    errs, _ = wl.validate_page("scenario", dict(
        base, start_pool=br.START_STATED, start_wse_ft=1101.0), exists=ok)
    assert any("above the top of dam" in e for e in errs)
    _, warns = wl.validate_page("scenario", dict(
        base, method=br.METHOD_OVERTOPPING, start_pool=br.START_NORMAL, start_wse_ft=1090.0),
        exists=ok)
    assert any("cannot overtop" in w for w in warns)
    nb = dict(base, scenario=workflow.SCENARIO_FLOOD_NO_BREACH, inflow_csv="in.csv",
              normal_pool_ft=1090.0, trigger=fl.TRIGGER_WSE, trigger_value=None)
    errs, _ = wl.validate_page("scenario", nb, exists=ok)
    assert not errs, "no breach: the trigger is not asked for ({})".format(errs)
    errs, _ = wl.validate_page("scenario", dict(nb, aux_crest_ft=1094.0), exists=ok)
    assert any("auxiliary spillway needs both" in e for e in errs)
    errs, _ = wl.validate_page("model", dict(nb, tier=workflow.TIER_HECRAS, cell_m=10.0))
    assert any("HEC-RAS 2D" in e for e in errs)
