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


@test
def terrain_breach_baseline_deck_has_no_notch_and_the_breach_deck_does():
    """The breach pass writes a notch file. The no-breach baseline (tag _base) does not:
    it keeps the breach hydrograph, and it does not replace the breach report."""
    dem, line, dam_xy, inflow = _valley()
    dam = _dam()
    dam.crest_line_xy = line
    st = _settings()
    assert workflow._notch_this_pass("", True) is True
    assert workflow._notch_this_pass("_base", True) is False
    assert workflow._notch_this_pass("_base", False) is False
    geom = tb.prepare(dam, _hy(), workflow._breach_crest_lines(dam, st, 0.0),
                      dam_xy, (1.0, 0.0), inflow)
    lf.raise_crest(dem, dam_xy, inflow, geom["top_m"], line_xy=line)
    spec, _depth = tb.stage(dem, geom, [line])
    hy = br.Hydrograph(np.array([0.0, 60.0]), np.array([10.0, 0.0]), "t")
    d = tempfile.mkdtemp(prefix="tb_base_")
    try:
        breach_dir = os.path.join(d, "breach")
        base_dir = os.path.join(d, "base")
        on = lf.LisfloodSettings(
            lf.GPU_EXE, sim_time_s=60.0,
            terrain_breach=spec if workflow._notch_this_pass("", True) else None)
        off = lf.LisfloodSettings(
            lf.GPU_EXE, sim_time_s=60.0,
            terrain_breach=spec if workflow._notch_this_pass("_base", True) else None)
        lf.write_inputs(breach_dir, dem, inflow, hy, on)
        lf.write_inputs(base_dir, dem, inflow, hy, off)
        assert os.path.isfile(os.path.join(breach_dir, "run.breach.json"))
        assert "breachfile" in open(os.path.join(breach_dir, "run.par"), encoding="utf-8").read()
        assert not os.path.isfile(os.path.join(base_dir, "run.breach.json"))
        assert "breachfile" not in open(os.path.join(base_dir, "run.par"), encoding="utf-8").read()
        assert "QVAR breach" in open(os.path.join(base_dir, "run.bci"), encoding="utf-8").read()
        src = open(workflow.__file__, encoding="utf-8").read()
        # a resumed baseline must not be the pass that writes the report entry
        i = src.index("def load_lisflood")
        body = src[i:src.index("\n    # M5:", i)]
        assert "if tb_spec is not None:" in body
        assert "_notch_this_pass(tag, tb_on)" in body
    finally:
        import shutil
        shutil.rmtree(d, True)


@test
def terrain_breach_batch_crest_line_is_used_when_the_dam_has_none():
    """A batch CREST_LINE (st.midcrest_line) is enough when the dam has no crest line of
    its own. The ordinary raise, used when the option is off, still ignores that line."""
    dem, line, dam_xy, inflow = _valley()
    dam = _dam()
    dam.crest_line_xy = None
    st = _settings()
    st.midcrest_line = line
    try:
        tb.prepare(dam, _hy(), workflow._crest_raise_lines(dam, 0.0),
                   dam_xy, (1.0, 0.0), inflow)
    except br.BreachInputError:
        pass
    else:
        raise AssertionError("a dam with no crest line was accepted")
    lines = workflow._breach_crest_lines(dam, st, 0.0)
    assert len(lines) == 1 and len(lines[0]) >= 2
    geom = tb.prepare(dam, _hy(), lines, dam_xy, (1.0, 0.0), inflow)
    assert geom["lines"][0][0] == line[0]
    n = lf.raise_crest(dem, dam_xy, inflow, geom["top_m"],
                       length_m=(dam.crest_length_ft or 400.0) * FT, line_xy=lines[0])
    assert n > 0 and np.any(dem.data == geom["top_m"])
    # a dam line of its own still wins over the batch line
    dam.crest_line_xy = line
    other = [(0.0, 0.0), (5.0, 0.0)]
    st.midcrest_line = other
    assert workflow._breach_crest_lines(dam, st, 0.0)[0] is line
    src = open(workflow.__file__, encoding="utf-8").read()
    assert "_breach_crest_lines(dam, st, 0.0)" in src
    assert "_crest_raise_lines(dam, half) if not tb_on" in src


@test
def terrain_breach_resume_keeps_the_stable_settings():
    """load_lisflood is a nested closure, so a resumed run is not built in these tests.
    The resume settings must still take theta, CFL and the sub-grid knobs (stable_kw)
    as well as the notch. LisfloodSettings keeps each of those."""
    src = open(workflow.__file__, encoding="utf-8").read()
    i = src.index("def load_lisflood")
    body = src[i:src.index("\n    # M5:", i)]
    assert "terrain_breach=tb_spec" in body
    assert "**stable_kw()" in body
    run = src[src.index("def lisflood_run"):i]
    assert "**stable_kw()" in run and "terrain_breach=tb_spec" in run
    st = lf.LisfloodSettings(lf.GPU_EXE, theta=0.7, cfl=0.5, subgrid_r=2,
                             subgrid_upwind=True, terrain_breach=None)
    assert st.theta == 0.7 and st.cfl == 0.5 and st.subgrid_r == 2
    assert st.subgrid_upwind is True and st.terrain_breach is None


def _nested_fn(name):
    """The AST of a function nested in workflow.py (lisflood_run / load_lisflood are closures
    inside the dam run, so they are checked by structure, not by text: a comment or a dead
    string cannot satisfy these checks)."""
    import ast
    tree = ast.parse(open(workflow.__file__, encoding="utf-8").read())
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(hits) == 1, (name, len(hits))
    return hits[0]


def _calls(node, name):
    import ast
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute)
                                                         else None)
            if fname == name:
                out.append(n)
    return out


def _gate(node, negated):
    """The If statements of node whose test is _notch_this_pass(tag, tb_on) (or its 'not')."""
    import ast
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.If):
            continue
        t = n.test
        if negated:
            if not (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)):
                continue
            t = t.operand
        if (isinstance(t, ast.Call) and isinstance(t.func, ast.Name)
                and t.func.id == "_notch_this_pass"
                and [a.id for a in t.args if isinstance(a, ast.Name)] == ["tag", "tb_on"]):
            out.append(n)
    return out


@test
def terrain_breach_pass_staging_cuts_the_notch_only_on_the_breach_pass():
    """The breach pass raises the crest on the breach line, then stages the notch and the pool.
    The no-breach baseline gets no notch, and its crest goes up after the start depth on the
    same line (a batch crest line when the dam has none). Option off: the old raise, on the
    dam's own line only. A fresh run and a resume both go through these two helpers, and
    neither calls the stage or the raise around them."""
    _dem0, line, dam_xy, inflow = _valley()
    dam = _dam()
    dam.crest_line_xy = None
    st = _settings()
    st.midcrest_line = line
    geom = tb.prepare(dam, _hy(), workflow._breach_crest_lines(dam, st, 0.0),
                      dam_xy, (1.0, 0.0), inflow)
    calls = []
    real_raise = lf.raise_crest

    def spy_raise(dem, dxy, ixy, top_m, length_m=None, line_xy=None):
        calls.append(("raise", line_xy))
        return real_raise(dem, dxy, ixy, top_m, length_m=length_m, line_xy=line_xy)

    def start_fn(dem):
        def f():
            calls.append(("start", None))
            return None
        return f

    workflow.lfmod.raise_crest = spy_raise
    try:
        # the breach pass: raise on the batch line first, then the start depth, then a notch
        dem = _valley()[0]
        warnings = []
        init, spec = workflow._stage_notch_pass(dem, dam, st, dam_xy, inflow, geom,
                                                start_fn(dem), warnings, lambda m: None)
        assert spec is not None
        assert calls[0] == ("raise", line) and calls[-1] == ("start", None), calls
        assert float(np.nanmax(np.asarray(init.data, float))) > 0.0   # the staged pool
        assert any("along the dam line" in w for w in warnings), warnings
        # the baseline, option on: no notch; raise after the start depth, on the same line
        calls[:] = []
        dem = _valley()[0]
        assert not workflow._notch_this_pass("_base", True)
        workflow._raise_crest_after_start(dem, dam, st, True, dam_xy, inflow, [], None)
        assert calls == [("raise", line)], calls
        # option off: the batch line is not used; the dam has none, so the crest point
        calls[:] = []
        dem = _valley()[0]
        workflow._raise_crest_after_start(dem, dam, st, False, dam_xy, inflow, [], None)
        assert calls == [("raise", None)], calls
        # a dam line of its own: used both ways
        dam.crest_line_xy = line
        st.midcrest_line = None
        calls[:] = []
        workflow._raise_crest_after_start(_valley()[0], dam, st, False, dam_xy, inflow)
        assert calls == [("raise", line)], calls
    finally:
        workflow.lfmod.raise_crest = real_raise
    # no crest geometry on a breach pass is refused, not silently run without a notch
    try:
        workflow._stage_notch_pass(_valley()[0], dam, st, dam_xy, inflow, None,
                                   lambda: None)
    except br.BreachInputError:
        pass
    else:
        raise AssertionError("a breach pass without crest geometry ran")
    # the fresh run and the resume are wired through these helpers
    run = _nested_fn("lisflood_run")
    load = _nested_fn("load_lisflood")
    for fn in (run, load):
        g = _gate(fn, negated=False)
        assert len(g) == 1, (fn.name, len(g))
        assert _calls(g[0].body[0], "_stage_notch_pass"), fn.name
        assert _calls(g[0].orelse[0], "start_depth"), fn.name
        assert not _calls(fn, "stage") and not _calls(fn, "raise_crest"), fn.name
        assert not _calls(fn, "_crest_raise_lines") and not _calls(fn, "_breach_crest_lines")
    late_gate = _gate(run, negated=True)
    assert len(late_gate) == 1 and _calls(late_gate[0], "_raise_crest_after_start")
    # option off keeps its order: start depth, then the settings, then the crest raise
    import ast
    notch_gate = _gate(run, negated=False)[0]
    settings_at = [n.lineno for n in ast.walk(run) if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", None) == "lst" for t in n.targets)
                   and isinstance(n.value, ast.Call)
                   and getattr(n.value.func, "id", None) == "settings"]
    assert settings_at and late_gate[0].lineno > notch_gate.lineno
    assert late_gate[0].lineno > min(settings_at), (late_gate[0].lineno, settings_at)
    # the resumed baseline with the option on raises as the fresh baseline did, and only then
    raise_ifs = [n for n in ast.walk(load) if isinstance(n, ast.If)
                 and any(_calls(b, "_raise_crest_after_start") for b in n.body)]
    assert len(raise_ifs) == 1 and len(_calls(load, "_raise_crest_after_start")) == 1
    t = raise_ifs[0].test
    assert isinstance(t, ast.BoolOp) and isinstance(t.op, ast.And), ast.dump(t)
    parts = {ast.unparse(v) for v in t.values}
    assert parts == {"tag == '_base'", "tb_on"}, parts


@test
def terrain_breach_resume_passes_the_stable_settings_by_structure():
    """The resume's LisfloodSettings takes **stable_kw() and the notch spec, checked on the
    parsed call (not the text), so removing the argument fails even if the words remain in a
    comment."""
    import ast
    for name in ("load_lisflood", "lisflood_run"):
        fn = _nested_fn(name)
        sets = _calls(fn, "LisfloodSettings")
        assert len(sets) == 1, (name, len(sets))
        kws = sets[0].keywords
        star = [k for k in kws if k.arg is None]
        assert len(star) == 1 and isinstance(star[0].value, ast.Call), name
        assert getattr(star[0].value.func, "id", None) == "stable_kw", name
        tbk = [k for k in kws if k.arg == "terrain_breach"]
        assert len(tbk) == 1 and getattr(tbk[0].value, "id", None) == "tb_spec", name


if __name__ == "__main__":
    for fn in (terrain_breach_setting_is_out_of_the_key_and_the_report_when_off,
               terrain_breach_is_refused_unless_the_run_can_lower_the_bed,
               terrain_breach_deck_has_no_breach_point_source_and_a_rerun_keeps_the_file,
               terrain_breach_pool_stays_upstream_of_a_crest_at_the_starting_level,
               terrain_breach_mass_uses_the_pool_and_products_omit_the_start,
               terrain_breach_baseline_deck_has_no_notch_and_the_breach_deck_does,
               terrain_breach_batch_crest_line_is_used_when_the_dam_has_none,
               terrain_breach_resume_keeps_the_stable_settings,
               terrain_breach_pass_staging_cuts_the_notch_only_on_the_breach_pass,
               terrain_breach_resume_passes_the_stable_settings_by_structure):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Exception as e:
            print("FAIL", fn.__name__, type(e).__name__, e, flush=True)
            raise
