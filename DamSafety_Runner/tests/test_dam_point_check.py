"""3.20.11: the dam point checked on the run's DEM (core/dam_point_check.py).

Synthetic dams (tests/synthetic_dams.py): a 9 m earth dam across a coulee at x = 0 holding a
hydro-flattened pond to the west, and variants (a knoll beside the pond, a spillway notch, a
seam between two LiDAR projects, water that is not hydro-flattened, a bent dam, a ring dike, a
saddle dike, two dams in one coulee). The check moves the point only when every gate passes;
anything less keeps the point (with a REVIEW warning when it looks wrong)."""
import contextlib
import json
import math
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import dam_point_check as dpc
from nd_breach_runner.core import workflow
from nd_breach_runner.core.breach import DamData
from nd_breach_runner.tests import synthetic_dams as S
from nd_breach_runner.tests.registry import need_osgeo, test

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _dam(crest_line_xy=None, **k):
    d = dict(dam_id="T1", dam_height_ft=S.H_FT, storage_top_acft=300.0, top_of_dam_ft=None,
             surface_area_top_ac=None)
    d.update(k)
    dam = DamData(**d)
    if crest_line_xy is not None:              # a user's dam line (DAM_LINES / dam.json)
        dam.crest_line_xy = crest_line_xy
    return dam


def _st(**k):
    """Run settings for the check's tests: mode "move" (the default is "warn") unless given."""
    d = dict(cell_size_m=20.0, planning_cell_m=90.0, initial_length_mi=1.0, half_width_mi=0.5,
             expand_step_mi=1.0, max_iterations=1, sim_time_hr=4.0, products=False,
             dam_point_check="move")
    d.update(k)
    return workflow.RunSettings(**d)


@contextlib.contextmanager
def _env(value):
    old = os.environ.get(dpc.ENV)
    try:
        if value is None:
            os.environ.pop(dpc.ENV, None)
        else:
            os.environ[dpc.ENV] = value
        yield
    finally:
        if old is None:
            os.environ.pop(dpc.ENV, None)
        else:
            os.environ[dpc.ENV] = old


_MEMO = {}


def _check(fn, xy, mode="move", **dam_kw):
    """The check's record for builder `fn` at point xy (memoised: each case is read once)."""
    key = (fn.__name__, tuple(xy), mode, tuple(sorted(dam_kw.items())))
    if key not in _MEMO:
        dam = _dam(**dam_kw)
        _MEMO[key] = workflow._dpc_compute(dam, _st(), tuple(map(float, xy)), S.DamTerrain(fn),
                                           20.0, mode, 1.0)
    return _MEMO[key]


def _survey(fn, xy):
    """(sv, rp) for the decision tests, memoised."""
    key = ("survey", fn.__name__, tuple(xy))
    if key not in _MEMO:
        t = S.DamTerrain(fn)
        g2, g4, _how = dpc.read_grids(t, xy)
        sv = dpc.survey(g2, g4, xy, S.H, None)
        rp = workflow._dpc_replay(_dam(), _st(), xy, t, 20.0)
        sv["inflow"] = dpc.inflow_facts(sv, rp.get("inflow_xy"))
        _MEMO[key] = (sv, rp)
    return _MEMO[key]


def _decide(*a, **k):
    """dam_point_check.decide in mode "move" (the default mode is "warn") unless given."""
    k.setdefault("mode", "move")
    return dpc.decide(*a, **k)


def _codes(items):
    """The gate / trigger codes of a record's texts or a decision's (code, text) pairs."""
    return {(t[0] if isinstance(t, (tuple, list)) else str(t).split(" ", 1)[0])
            for t in items or ()}


def _fake_models():
    from nd_breach_runner.tests.test_v320 import fake_models
    return fake_models()


def _full_run(fn, xy, preview_first=False, **st_kw):
    """run_dam (LISFLOOD-FP stubbed) on builder fn from xy; what the tests read, memoised."""
    key = ("run", fn.__name__, tuple(xy), preview_first, tuple(sorted(st_kw.items())))
    if key in _MEMO:
        return _MEMO[key]
    from nd_breach_runner.tests.synthetic import npy_writer
    dpc.cache_clear()
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        t = S.DamTerrain(fn)
        out = {}
        if preview_first:
            plan = workflow.preview(_dam(), tuple(xy), _st(**st_kw), t)
            out["preview"] = plan
            out["checks_after_preview"] = t.checks()
        with _fake_models():
            res = workflow.run_dam(_dam(), tuple(xy), _st(qa_maps="inflow", **st_kw), t,
                                   npy_writer, os.path.join(d, "T1"))
        out["checks_after_run"] = t.checks()
        with open(res.report_json) as f:
            out["rep"] = json.load(f)
        with open(res.report_md) as f:
            out["md"] = f.read()
        gj = os.path.join(d, "T1", "inputs", dpc.FOUND_LINE_FILE)
        out["geojson"] = json.load(open(gj)) if os.path.exists(gj) else None
        out["modelled_dam_xy"] = res.modelled_dam_xy
        out["res_check"] = res.dam_point_check
        out["warnings"] = list(res.warnings)

        class _R(object):
            modelled_dam_xy = res.modelled_dam_xy
        out["res"] = _R()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    _MEMO[key] = out
    return out


# ------------------------------------------------------------------------- the module ------
@test
def dpc_modes_and_the_environment_switch():
    # 2026-11-17: the default is warn (the hold-out left one move 30 m off its dam line)
    assert dpc.DEFAULT_MODE == "warn" == workflow.RunSettings().dam_point_check
    assert dpc.normalize_mode(None) == "warn" and dpc.normalize_mode("") == "warn"
    assert dpc.normalize_mode(True) == "move" and dpc.normalize_mode(False) == "off"
    assert [dpc.normalize_mode(i) for i in (0, 1, 2)] == ["off", "warn", "move"]
    assert dpc.normalize_mode("Warn only") == "warn" and dpc.normalize_mode("ON") == "move"
    try:
        dpc.normalize_mode("sometimes")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "off, warn or move" in str(e)
    assert dpc.env_mode({}) is None
    assert dpc.env_mode({dpc.ENV: "off"}) == "off" and dpc.env_mode({dpc.ENV: "0"}) == "off"
    assert dpc.env_mode({dpc.ENV: "warn"}) == "warn" and dpc.env_mode({dpc.ENV: "yes"}) == "move"
    assert dpc.env_mode({dpc.ENV: "maybe"}) is None
    # critique 13: _env_flag cannot say "off"; _env_off can, and the environment wins
    with _env(None):
        assert workflow._env_off("DAM_POINT_CHECK") is None
        assert workflow._dam_point_mode(_st()) == ("move", "setting")
        assert workflow._dam_point_mode(workflow.RunSettings()) == ("warn", "setting")
        assert workflow._dam_point_mode(_st(dam_point_check="warn")) == ("warn", "setting")
    for v, want in (("off", "off"), ("0", "off"), ("no", "off"), ("false", "off"),
                    ("warn", "warn"), ("move", "move"), ("1", "move")):
        with _env(v):
            assert workflow._dam_point_mode(_st(dam_point_check="warn"))[0] == want, (v, want)
            assert workflow._dam_point_mode(_st())[1] == "environment"
    with _env("off"):
        assert workflow._env_off("DAM_POINT_CHECK") is True
    with _env("on"):
        assert workflow._env_off("DAM_POINT_CHECK") is False


@test
def dpc_when_the_check_runs_precedence_rows():
    st, dam = _st(), _dam()
    assert dpc.should_check(dam, st, None, 1.0) == (True, None)
    assert dpc.should_check(dam, st, None, 1.0, mode="off") == (False, "off")
    assert dpc.should_check(dam, _st(dam_point_check="off"), None, 1.0)[1] == "off"
    for attr, val, why in (("proposed", {"crest_m": 1.0}, "proposed dam"),
                           ("fixed_plan", {}, "fixed plan")):
        s_ = _st()
        setattr(s_, attr, val)
        assert dpc.should_check(dam, s_, None, 1.0)[1].startswith(why), attr
    assert dpc.should_check(_dam(crest_line_xy=[(0, 0), (1, 1)]), st, None, 1.0)[1] == \
        "dam line given"
    st2 = _st()
    st2.midcrest_line = [(0, 0), (1, 1)]
    assert dpc.should_check(dam, st2, None, 1.0)[1] == "crest line given"
    st3 = _st()
    st3.breach_xy = (0.0, 0.0)
    assert dpc.should_check(dam, st3, None, 1.0)[1] == "breach position given"
    assert dpc.should_check(dam, st, (5.0, 5.0), 1.0)[1] == "breach inflow point given"
    assert dpc.should_check(_dam(top_of_dam_ft=1640.0), st, None, 1.0)[1] == "top of dam given"
    filled = _dam(top_of_dam_ft=1640.0)
    filled._top_from_dem = True               # the single-dam tool's / the cascade's DEM fill
    assert dpc.should_check(filled, st, None, 1.0) == (True, None)
    # critique 12: a DEM whose cell is unknown (SyntheticTerrain) is not checked
    assert dpc.should_check(dam, st, None, None) == (False, "DEM cell unknown")
    assert dpc.should_check(dam, st, None, 10.0)[1].startswith("DEM coarser than 5 m")
    assert dpc.should_check(_dam(dam_height_ft=0.0), st, None, 1.0)[1] == "no dam height"


@test
def dpc_dem_cell_unknown_is_skipped_in_the_run():
    from nd_breach_runner.tests.synthetic import SyntheticTerrain
    rep, w, logs = {}, [], []
    dam = _dam()
    xy = workflow._dam_point_check(dam, _st(), (-120.0, 0.0), None, SyntheticTerrain(S.enclosed),
                                   20.0, rep, w, logs.append)
    assert xy == (-120.0, 0.0) and not w
    assert rep["dam_point_check"]["outcome"] == "skipped"
    assert rep["dam_point_check"]["skipped_because"] == "DEM cell unknown"
    assert dpc.batch_text(rep["dam_point_check"]) == "skipped: DEM cell unknown"
    assert dpc.report_line(rep["dam_point_check"]) is None
    assert not getattr(dam, "crest_line_xy", None)


@test
def dpc_run_settings_key_and_settings_record():
    st = workflow.RunSettings()
    assert st.dam_point_check == "warn" == dpc.DEFAULT_MODE
    k = workflow.key_settings(st)
    assert "dam_point_check" not in k and "dam_point_others" not in k
    # the switch never enters the checkpoint key (the moved line does, through the dam)
    assert "dam_point_check" not in workflow.key_settings(workflow.RunSettings(
        dam_point_check="off"))
    assert "dam_point_check" not in st.as_dict()
    assert st.as_dict().get("dam_point_lines") is None
    assert workflow.RunSettings(dam_point_check="move").as_dict()["dam_point_check"] == "move"
    assert "dam_point_check" not in workflow.RunSettings(dam_point_check="warn").as_dict()


@test
def dpc_pin_first_keeps_the_move_warning_first():
    w = ["a", dpc.MOVED_PREFIX + ": x", "b"]
    w.insert(0, "CHECK THE INFLOW POINT: later")
    dpc.pin_first(w)
    assert w[0].startswith(dpc.MOVED_PREFIX) and w[1].startswith("CHECK THE INFLOW")
    assert dpc.pin_first([]) == [] and dpc.pin_first(["a", "b"]) == ["a", "b"]


@test
def dpc_hecras_stage_and_products_get_the_modelled_point():
    class R(object):
        modelled_dam_xy = None
    assert workflow.hecras_dam_xy(R(), (5.0, 6.0)) == (5.0, 6.0)
    R.modelled_dam_xy = [1.5, 2.5]
    assert workflow.hecras_dam_xy(R(), (5.0, 6.0)) == (1.5, 2.5)
    # critique 7: the tools and the CLI pass the modelled point to the HEC-RAS stage (run in
    # dpc_tools_and_the_cli_pass_neighbours_and_the_modelled_point)
    from nd_breach_runner.core import hecras_model as hm
    assert hm.FOUND_LINE_SOURCE == dpc.FOUND_LINE_SOURCE
    assert hm.FOUND_LINE_SOURCE in hm.USER_LINE_SOURCES and "your dam line" in hm.USER_LINE_SOURCES
    # the found line is never labelled as the user's own (review 2026-11-17)
    assert dpc.FOUND_LINE_SOURCE != "your dam line"
    assert "your dam line" not in dpc.FOUND_LINE_SOURCE and "found" in dpc.FOUND_LINE_SOURCE


@test
def dpc_batch_columns_are_the_same_in_the_tool_and_the_cli():
    from nd_breach_runner.tests.test_v320 import _algs
    from nd_breach_runner.tests.test_cli import cli
    algs, _fq = _algs()
    c = cli()
    assert tuple(algs.BATCH_FIELDS) == tuple(c.BATCH_FIELDS)
    assert algs.BATCH_FIELDS[-1] == "Dam_Point_Check"
    # the CLI setting (model.dam_point_check) and its default: warn in the template and
    # without the key
    assert c.MODEL_KEYS["dam_point_check"][0] == "dam_point_check"
    assert c.TEMPLATE["model"]["dam_point_check"] == dpc.DEFAULT_MODE == "warn"
    import sys
    opts = type("O", (), {"lisflood": sys.executable, "anuga_python": None})()
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        tp = os.path.join(d, "t.json")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(c.template_text())
        for cfg in (c.load_config(tp), c.Config({"model": {}}, "")):
            assert c.build_settings(cfg, opts, need_models=False).dam_point_check == "warn"
        assert c.build_settings(c.Config({"model": {"dam_point_check": "move"}}, ""), opts,
                                need_models=False).dam_point_check == "move"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    # the tools' parameter: an advanced enum, warn by default
    for cls in (algs.RunDamAlgorithm, algs.BatchAlgorithm):
        a = cls()
        a.initAlgorithm()
        p = a.parameterDefinition("DAM_POINT_CHECK")
        assert p is not None, cls
        assert algs.dam_point_check_param(p.defaultValue()) == dpc.DEFAULT_MODE == "warn", \
            (cls, p.defaultValue())
    assert algs.DAM_POINT_CHECK_MODES == dpc.MODES
    assert [algs.dam_point_check_param(i) for i in (0, 1, 2)] == ["off", "warn", "move"]
    assert algs.dam_point_check_param("warn") == "warn"


@test
def dpc_window_passes_the_setting_only_when_turned_down():
    from nd_breach_runner import popup_logic as pl
    assert pl.DAM_POINT_CHECK_DEFAULT == dpc.DEFAULT_MODE
    assert "DAM_POINT_CHECK" not in pl.defaults("rundam", "", "")
    assert "DAM_POINT_CHECK" not in pl.defaults("rundam", "", "", {"dam_point_check": "warn"})
    assert pl.defaults("rundam", "", "", {"dam_point_check": "off"})["DAM_POINT_CHECK"] == 0
    assert pl.defaults("batch", "", "", {"dam_point_check": "move"})["DAM_POINT_CHECK"] == 2
    assert pl.dam_point_check_choice(False) == "off" and pl.dam_point_check_choice(None) == "warn"
    assert pl.dam_point_check_choice(True) == "move"
    assert pl.dam_point_check_choice("nonsense") == "warn"


@test
def dpc_cascade_dem_fill_does_not_skip_the_check():
    """Critique 8: the cascade fills a missing top from the DEM before run_dam; that fill is
    marked, so the check still runs, and the chain's other dams are passed as neighbours."""
    from nd_breach_runner.core import cascade
    from nd_breach_runner.core import breach as brmod
    seen = []
    real = workflow.run_dam

    def fake_run_dam(dam, xy, st, *a, **k):
        seen.append((dam.dam_id, getattr(dam, "_top_from_dem", False), st.dam_point_others,
                     dpc.should_check(dam, st, None, 1.0)))
        raise brmod.BreachInputError("stop here")
    chain = [cascade.CascadeDam(_dam(dam_id="A"), (0.0, 0.0)),
             cascade.CascadeDam(_dam(dam_id="B"), (350.0, 0.0))]
    workflow.run_dam = fake_run_dam
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        from nd_breach_runner.tests.synthetic import npy_writer
        r = cascade.run_cascade(chain, _st(), S.DamTerrain(S.neighbours), npy_writer, d)
    finally:
        workflow.run_dam = real
        shutil.rmtree(d, ignore_errors=True)
    assert seen and seen[0][0] == "A" and seen[0][1] is True, seen
    assert seen[0][2] == [("B", (350.0, 0.0), S.H_FT)], seen
    assert seen[0][3] == (True, None), seen
    assert any("stop here" in w for w in r.warnings)


# ------------------------------------------------------------------------- the decision ----
@test
def dpc_gates_never_move_onto_a_neighbour_and_warn_mode_never_moves():
    """Critique 2: G7a-G7e, G4c and warn mode, on the survey of a clear case (the point on the
    pond 120 m west of the dam, which moves)."""
    sv, rp = _survey(S.enclosed, (-120.0, 0.0))
    dec = _decide(sv, rp, S.H)
    assert dec["outcome"] == "move", dec
    assert "A1" in _codes(dec["triggers"]), dec["triggers"]
    line = sv["emb"]["line"]
    # another inventory dam nearer the found line than this point (G7c)
    d = _decide(sv, rp, S.H, others=[("B", (3.0, 40.0))])
    assert d["outcome"] == "review" and "G7c" in _codes(d["gates_failed"]), d
    assert _decide(sv, rp, S.H, others=[("B", (-700.0, 0.0))])["outcome"] == "move"
    # another dam's crest line (G7c)
    shifted = [(x + 4.0, y) for x, y in line]
    assert "G7c" in _codes(_decide(sv, rp, S.H, other_lines=[shifted])["gates_failed"])
    # the point on another ridge (G7a) and on the low side of the found crest (G7b)
    sv_a = dict(sv, point_ridge={"height_m": 5.0, "length_m": 80.0, "distance_m": 4.0})
    assert "G7a" in _codes(_decide(sv_a, rp, S.H)["gates_failed"])
    sv_b = dict(sv, point_side_m=60.0)
    assert "G7b" in _codes(_decide(sv_b, rp, S.H)["gates_failed"])
    # the inventory crest length (G7d) and the normal pool (G7e)
    L = sv["line_q"]["length_m"]
    assert _decide(sv, rp, S.H, crest_length_m=L)["outcome"] == "move"
    # validation 2026-11-17: no lower bound (the line found is often only the part between the
    # pond and the low ground), at most 3 x the inventory crest length
    assert _decide(sv, rp, S.H, crest_length_m=3 * L)["outcome"] == "move"
    assert "G7d" in _codes(_decide(sv, rp, S.H, crest_length_m=L / 3.5)["gates_failed"])
    assert _decide(sv, rp, S.H, crest_length_m=L / 2.9)["outcome"] == "move"
    lvl = sv["level_m"]
    assert _decide(sv, rp, S.H, normal_pool_m=lvl + 0.2)["outcome"] == "move"
    # the pond 3 m above the normal pool (limit max(3 ft, 0.15 H) = 1.35 m), or 6 m below it
    # (limit 0.5 H = 4.5 m)
    assert "G7e" in _codes(_decide(sv, rp, S.H, normal_pool_m=lvl - 3.0)["gates_failed"])
    assert "G7e" in _codes(_decide(sv, rp, S.H, normal_pool_m=lvl + 6.0)["gates_failed"])
    assert _decide(sv, rp, S.H, normal_pool_m=lvl + 3.0)["outcome"] == "move"
    # the inventory area (G6)
    assert "G6" in _codes(_decide(sv, rp, S.H, sa_ac=sv["pool_area_ac"] * 20)["gates_failed"])
    # a closed line (G4c)
    sv_c = dict(sv, closed=(True, "the line found closes on itself"))
    assert "G4c" in _codes(_decide(sv_c, rp, S.H)["gates_failed"])
    # review 2026-11-17: each of G2, G3, G4 and G7f on its own sends the move to review
    emb = sv["emb"]
    lq = sv["line_q"]
    cases = [("G2", dict(sv, emb=dict(emb, height_ratio=dpc.HR_MAX + 0.5))),
             ("G2", dict(sv, emb=dict(emb, height_ratio=dpc.HR_MIN - 0.2))),
             ("G3", dict(sv, emb=dict(emb, point_to_crest_m=dpc.MAX_MOVE_M + 50.0))),
             ("G4", dict(sv, line_q=dict(lq, on_crest=dpc.LINE_ON_CREST - 0.2))),
             ("G4", dict(sv, line_q=dict(lq, on_water=0.1))),
             ("G4", dict(sv, line_q=dict(lq, below_pool=dpc.LINE_BELOW_POOL + 0.1))),
             ("G4", dict(sv, line_q=dict(lq, perp95_m=dpc.perp95_limit_m(S.H) + 5.0))),
             ("G4", dict(sv, line_q=dict(lq, length_m=dpc.LEN_MIN_M - 5.0))),
             ("G4", dict(sv, emb=dict(emb, line=None))),
             ("G7f", dict(sv, pond_between=dpc.POND_BETWEEN_FRAC + 0.1, point_side_m=None))]
    for code, sv_x in cases:
        d = _decide(sv_x, rp, S.H)
        assert d["outcome"] == "review" and d["warn"], (code, d)
        assert code in _codes(d["gates_failed"]), (code, d["gates_failed"])
    # a pond that is not between them (G7f below the share) still moves
    assert _decide(dict(sv, pond_between=dpc.POND_BETWEEN_FRAC - 0.1, point_side_m=None), rp,
                   S.H)["outcome"] == "move"
    # warn mode: every gate passed, still not moved
    w = _decide(sv, rp, S.H, mode="warn")
    assert w["outcome"] == "review" and w["would_move"] and w["warn"] and not w["gates_failed"]
    rec = dpc.record("review", "warn", (-120.0, 0.0), sv=sv, rp=rp, dec=w)
    assert dpc.batch_text(rec) == "review (would move)"
    assert "would be used as the dam line" in dpc.review_text(rec)


@test
def dpc_upstream_face_inflow_triggers_a3():
    """Critique 3: the inflow on the pond side of the found crest (the upstream face) is A3,
    measured along the local downstream direction, also with no water under it."""
    sv, rp = _survey(S.enclosed, (-120.0, 0.0))
    f = dpc.inflow_facts(sv, (-25.0, 5.0))               # on the pond
    assert f["upstream"] and f["side_m"] < -20, f
    f = dpc.inflow_facts(sv, (40.0, 5.0))                # the toe
    assert not f["upstream"] and f["side_m"] > 30 and not f["on_water"], f
    sv2 = dict(sv, inflow={"on_water": False, "water_wse_m": None, "on_pool_water": None,
                           "in_pool": False, "side_m": -25.0, "upstream": True})
    rp2 = dict(rp, inflow_xy=(-25.0, 5.0))
    assert "A3" in _codes(_decide(sv2, rp2, S.H)["triggers"])
    # the critique's own case: tests/synthetic.valley from (-150, 0). The pool is only 1 m
    # below the top and the water reaches round the embankment ends, so the embankment is a
    # ridge only (G1): never moved, but A3 now fires and the run gets a REVIEW warning
    from nd_breach_runner.tests import synthetic as SY
    rec = workflow._dpc_compute(_dam(dam_height_ft=SY.H_M / 0.3048), _st(), (-150.0, 0.0),
                                S.DamTerrain(SY.valley), 20.0, "move", 1.0)
    assert rec["outcome"] == "review", rec
    assert "A3" in _codes(rec["triggers"]) and "G1" in _codes(rec["gates_failed"]), rec
    assert rec["point_only"]["inflow_side_m"] < -10


# ------------------------------------------------------------------------- the cases -------
@test
def dpc_correct_point_is_kept():
    out = _full_run(S.enclosed, (0.0, 0.0))
    pc = out["rep"]["dam_point_check"]
    assert pc["outcome"] == "kept" and pc["flag"] == "OK", pc
    assert out["modelled_dam_xy"] is None
    assert out["rep"]["dam_xy"][:2] == [0.0, 0.0]
    assert not any(w.startswith((dpc.MOVED_PREFIX, dpc.REVIEW_PREFIX)) for w in out["warnings"])
    assert out["rep"]["summary"]["dam_point_check"] == "kept"
    assert "| Dam location | DWR point 0.0, 0.0, used as given" in out["md"]
    assert out["geojson"] is None


@test
def dpc_point_on_the_pond_is_moved_to_the_embankment():
    out = _full_run(S.enclosed, (-120.0, 0.0), preview_first=True)
    rep, pc = out["rep"], out["rep"]["dam_point_check"]
    assert pc["outcome"] == "moved", pc
    assert "A1" in _codes(pc["triggers"]) and not pc["gates_failed"], pc
    line = np.asarray(pc["embankment"]["line_xy"], float)
    # the line along the true crest (x = 0, |y| <= 112.5 m): within 4 m, length within 15 %
    assert np.abs(line[:, 0]).max() <= 4.0, line
    true_len = 2 * S.CREST_HALF
    assert abs(pc["embankment"]["length_m"] - true_len) <= 0.15 * true_len, pc["embankment"]
    mx, my = out["modelled_dam_xy"]
    assert abs(mx) <= 4.0 and abs(my) <= 30.0, (mx, my)
    assert pc["modelled_xy"] == [round(mx, 2), round(my, 2)]
    assert abs(pc["moved_m"] - math.hypot(mx + 120.0, my)) < 0.2
    assert pc["verify"]["ok"] and pc["verify"]["toe_band"]
    assert 0.5 <= pc["verify"]["drop_x_h"] <= 1.3
    assert rep["dam_xy"][0] == round(mx, 3) or abs(rep["dam_xy"][0] - mx) < 0.01
    # the run follows the dam-line path: the line labelled as found, the mid-crest top
    assert rep["crest_line_source"] == dpc.FOUND_LINE_SOURCE
    assert "dam point check" in rep["dem_crest_from"]
    assert abs(rep["dem_crest_ft"] - S.TOP / 0.3048) < 1.0
    # the inflow at the downstream toe, not in the pond
    ix, iy = rep["inflow_point"]["xy"]
    assert 5.0 < ix < 60.0, rep["inflow_point"]
    # the warning first, the batch text, the report line, the found line, the QA map
    assert out["warnings"][0].startswith(dpc.MOVED_PREFIX), out["warnings"][:2]
    assert rep["warnings"][0].startswith(dpc.MOVED_PREFIX)
    assert rep["summary"]["dam_point_check"].startswith("moved ")
    assert "| Dam location | DWR point -120.0, 0.0; modelled point" in out["md"]
    feat = out["geojson"]["features"][0]
    assert feat["properties"]["used"] is True
    assert np.allclose(feat["geometry"]["coordinates"], pc["embankment"]["line_xy"], atol=0.01)
    # the overlay is always worked out; the flag says it was drawn, so it is set exactly when
    # the inflow map was written (the fast CI job has no plotting library and writes no map)
    from nd_breach_runner.core import qa_maps as QA
    assert QA.point_check_overlay(pc, (-120.0, 0.0)) is not None
    qa = rep.get("qa_maps", {})
    assert qa.get("point_check") is (True if qa.get("inflow_map") else None), qa
    # HEC-RAS and the products get the modelled point
    assert workflow.hecras_dam_xy(out["res"], (-120.0, 0.0)) == (mx, my)
    assert pc["dem"]["resampling"] == dpc.RESAMPLING_SOURCE
    # critique 9: preview ran the check first; the run reused it (no new DEM reads) and both
    # put the dam at the same place
    assert out["checks_after_preview"] == 1 and out["checks_after_run"] == 1, out
    plan = out["preview"]
    assert plan["dam_point_check"]["outcome"] == "moved"
    assert np.allclose(plan["dam_xy"], (mx, my), atol=0.01), (plan["dam_xy"], mx, my)


@test
def dpc_point_on_the_knoll_is_moved():
    rec = _check(S.with_knoll, (-80.0, 190.0))
    assert rec["outcome"] == "moved", rec
    assert "A2" in _codes(rec["triggers"]), rec["triggers"]
    mx, my = rec["modelled_xy"]
    assert abs(mx) <= 4.0 and abs(my) <= 112.0, rec["modelled_xy"]


@test
def dpc_point_on_the_upstream_face_is_moved():
    rec = _check(S.enclosed, (-35.0, 5.0))
    assert rec["outcome"] == "moved", rec
    assert abs(rec["modelled_xy"][0]) <= 4.0


@test
def dpc_neighbour_dam_in_the_same_coulee_is_not_taken():
    """Dam A at x = 0 has a dry pond; dam B at x = 350 m holds water that backs up to A's toe.
    The survey sees B's backwater as the pond and A from its downstream side: A's dry basin is
    the "low side", a closed basin that fills to A's crest, so G1 refuses it (the run would
    otherwise put the breach inflow in A's own reservoir). Never B's line either."""
    for xy in ((0.0, 0.0), (-60.0, 0.0)):
        rec = _check(S.neighbours, xy)
        assert rec["outcome"] == "review", (xy, rec)
        assert rec["embankment"]["low_side_closed"], rec["embankment"]
        g1 = [g for g in rec["gates_failed"] if g.startswith("G1 ")]
        assert g1 and "closed basin" in g1[0], rec["gates_failed"]
        line = np.asarray((rec.get("embankment") or {}).get("line_xy") or [[0.0, 0.0]])
        assert np.abs(line[:, 0] - 350.0).min() > 100.0, (xy, line)   # never B's crest
    # a point in A's dry basin, on B's low side: in a closed basin of its own (G7b)
    rec = _check(S.neighbours, (-60.0, 0.0))
    assert any(g.startswith("G7b ") and "closed basin" in g for g in rec["gates_failed"]), rec


@test
def dpc_upstream_neighbour_is_not_taken_with_or_without_others():
    """Review 2026-11-17: B upstream of A and higher, holding water; A dry. A point on A's crest
    (also near its abutment, where A's fill is under 0.5 H) or in A's dry basin sees B's pond
    and B's embankment, and the top read on A is far below B's crest (A2). It is on B's low
    side but not on the low ground there (G7b), with or without B passed as a neighbour; with
    B passed, G7c refuses it as well (B's point is on the line)."""
    xb = -250.0
    for xy, why in (((0.0, 0.0), "above the low ground"), ((0.0, 85.0), "above the low ground"),
                    ((-40.0, 0.0), "closed basin")):
        for others in (None, [("B", (xb, 0.0), S.H_FT)]):
            t = S.DamTerrain(S.upstream_neighbour)
            g2, g4, _how = dpc.read_grids(t, xy)
            sv = dpc.survey(g2, g4, xy, S.H, None)
            rp = workflow._dpc_replay(_dam(), _st(), xy, t, 20.0)
            sv["inflow"] = dpc.inflow_facts(sv, rp.get("inflow_xy"))
            line = np.asarray(sv["emb"]["line"])
            assert np.abs(line[:, 0] - xb).max() < 10.0, line           # B's crest was found
            d = _decide(sv, rp, S.H, others=others)
            assert d["outcome"] == "review", (xy, d)
            assert "A2" in _codes(d["triggers"]), d["triggers"]
            g7b = [t_ for c, t_ in d["gates_failed"] if c == "G7b"]
            assert g7b and why in g7b[0], (xy, d["gates_failed"])
            if others:
                assert "G7c" in _codes(d["gates_failed"]), d["gates_failed"]
    # G7c on its own: a neighbour point nearer the found line than this point blocks only when
    # it is on the line (25 m) or its inventory height fits the embankment found
    sv, rp = _survey(S.enclosed, (-120.0, 0.0))
    assert "G7c" in _codes(_decide(sv, rp, S.H, others=[("B", (0.0, 0.0))])["gates_failed"])
    assert _decide(sv, rp, S.H, others=[("B", (-60.0, 0.0))])["outcome"] == "move"
    assert _decide(sv, rp, S.H, others=[("B", (-60.0, 0.0), 6.0)])["outcome"] == "move"
    assert "G7c" in _codes(_decide(sv, rp, S.H, others=[("B", (-60.0, 0.0), S.H_FT)])[
        "gates_failed"])


@test
def dpc_ring_dike_goes_to_review():
    rec = _check(S.ring, (0.0, 0.0), dam_height_ft=4.0 / 0.3048)
    assert rec["outcome"] == "review", rec
    assert "G4c" in _codes(rec["gates_failed"]), rec["gates_failed"]
    assert rec["embankment"]["closed"] is True


@test
def dpc_saddle_dike_lower_than_the_main_dam():
    rec = _check(S.saddle, (-120.0, 0.0))
    assert rec["outcome"] == "moved", rec
    mx, my = rec["modelled_xy"]
    assert abs(mx) <= 4.0 and abs(my) <= 112.0, rec["modelled_xy"]   # the main dam
    rec2 = _check(S.saddle, (-260.0, 140.0))                         # a point on the saddle
    assert rec2["outcome"] != "moved", rec2


@test
def dpc_spillway_notch_is_left_open_by_the_crest_raise():
    """Critique 6: the crest raise burns only the on-crest parts of a found line."""
    from nd_breach_runner.core import lisflood as lf
    rec = _check(S.with_notch, (-120.0, 0.0))
    assert rec["outcome"] == "moved", rec
    parts = rec["_parts"]
    assert len(parts) >= 2, parts
    for p in parts:
        q = dpc.sample_line(p, 1.0)
        assert not np.any((np.abs(q[:, 1] - 60.0) < 5.0) & (np.abs(q[:, 0]) < 6.0)), p
    dam = _dam()
    dam.crest_line_xy = list(rec["_line"])
    dam._crest_line_source = dpc.FOUND_LINE_SOURCE
    dam._crest_raise_parts = parts
    assert workflow._crest_raise_lines(dam) == parts
    g = S.grid_of(S.with_notch, (-40.0, -160.0, 40.0, 160.0), 2.0)
    before = float(g.sample_bilinear(np.array([0.0]), np.array([60.0]))[0])
    half = max(12.0, g.dx) / 2.0                        # the burn's half-width (raise_crest)
    for ln in workflow._crest_raise_lines(dam, half):
        lf.raise_crest(g, None, None, S.TOP, line_xy=ln)
    after = float(g.sample_bilinear(np.array([0.0]), np.array([60.0]))[0])
    assert after < S.TOP - 1.0 and abs(after - before) < 0.05, (before, after)
    assert float(g.sample_bilinear(np.array([0.0]), np.array([-50.0]))[0]) >= S.TOP - 0.05
    # a user's own dam line is raised whole, as before
    user = _dam(crest_line_xy=[(0.0, -100.0), (0.0, 100.0)])
    assert workflow._crest_raise_lines(user) == [[(0.0, -100.0), (0.0, 100.0)]]
    assert workflow._crest_raise_lines(_dam()) == [None]


@test
def dpc_bent_dam_keeps_its_bend():
    rec = _check(S.l_shaped, (-100.0, -100.0))
    assert rec["outcome"] == "moved", rec
    line = np.asarray(rec["embankment"]["line_xy"], float)
    assert len(line) >= 3, line
    d = S._polyline_dist(line[:, 0], line[:, 1], S.CHEVRON)
    assert d.max() <= 6.0, d.max()                        # a chord would be ~70 m off
    q = dpc.sample_line(line, 2.0)
    assert np.hypot(q[:, 0], q[:, 1]).min() <= 6.0        # through the bend


@test
def dpc_two_level_seam_goes_to_review():
    rec = _check(S.two_level, (-120.0, 0.0))
    assert rec["outcome"] == "review" and "G5" in _codes(rec["gates_failed"]), rec


@test
def dpc_water_not_hydro_flattened_is_never_moved():
    rec = _check(S.not_flattened, (-120.0, 0.0))
    assert rec["outcome"] != "moved", rec


@test
def dpc_runs_once_per_dam_and_reuses_the_result():
    dpc.cache_clear()
    t = S.DamTerrain(S.enclosed)
    dam = _dam()
    rep, w = {}, []
    xy = workflow._dam_point_check(dam, _st(), (-120.0, 0.0), None, t, 20.0, rep, w,
                                   lambda m: None)
    n = t.checks()
    assert n == 1 and rep["dam_point_check"]["outcome"] == "moved"
    assert dam.crest_line_xy and dam._crest_line_source == dpc.FOUND_LINE_SOURCE
    assert dam._point_original == (-120.0, 0.0)
    rep2, w2 = {}, []
    xy2 = workflow._dam_point_check(dam, _st(), (-120.0, 0.0), None, t, 20.0, rep2, w2,
                                    lambda m: None)
    assert t.checks() == n and xy2 == xy and w2[0] == w[0]
    # a second dam object at the same point and DEM: the cache, no reads
    rep3, w3 = {}, []
    workflow._dam_point_check(_dam(), _st(), (-120.0, 0.0), None, S.DamTerrain(S.enclosed),
                              20.0, rep3, w3, lambda m: None)
    assert rep3["dam_point_check"]["outcome"] == "moved"
    # warn mode keeps the point and says it would move
    dpc.cache_clear()
    rep4, w4 = {}, []
    d4 = _dam()
    xy4 = workflow._dam_point_check(d4, _st(dam_point_check="warn"), (-120.0, 0.0), None,
                                    S.DamTerrain(S.enclosed), 20.0, rep4, w4, lambda m: None)
    assert xy4 == (-120.0, 0.0) and not getattr(d4, "crest_line_xy", None)
    assert rep4["dam_point_check"]["outcome"] == "review" and \
        rep4["dam_point_check"]["would_move"]
    assert any(x.startswith(dpc.REVIEW_PREFIX) for x in w4)
    # the environment's off wins over the setting
    with _env("off"):
        rep5, w5 = {}, []
        xy5 = workflow._dam_point_check(_dam(), _st(), (-120.0, 0.0), None,
                                        S.DamTerrain(S.enclosed), 20.0, rep5, w5, lambda m: None)
    assert xy5 == (-120.0, 0.0) and rep5["dam_point_check"]["skipped_because"] == "off"
    assert rep5["dam_point_check"]["mode_source"] == "environment"


@test
def dpc_other_resampling_settings_do_not_change_the_check():
    """Critique 10: the check reads with average resampling whatever the run's method."""
    need_osgeo()
    from nd_breach_runner.core import gdal_io as GI
    from nd_breach_runner.core.gdal_io import GdalTerrain
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        path = S.write_geotiff(S.enclosed, os.path.join(d, "dem.tif"),
                               bounds=(-1200.0, -1200.0, 1200.0, 1200.0))
        got = []
        for how in ("bilinear", "cubic", "average"):
            t = GdalTerrain(path, "EPSG:26914", z_choice=GI.Z_METERS, resample=how)
            g2, g4, label = dpc.read_grids(t, (-120.0, 0.0))
            assert label == dpc.RESAMPLING_AVERAGE
            sv = dpc.survey(g2, g4, (-120.0, 0.0), S.H, None)
            got.append((sv["n_water_400"], round(sv["level_m"], 3),
                        round(sv["emb"]["crest_z_m"], 2), sv["emb"]["line"]))
            assert dpc.dem_stamp(t)[0] == path
        assert got[0] == got[1] == got[2], got
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def dpc_record_texts():
    rec = {"outcome": "moved", "original_xy": [-120.0, 0.0], "modelled_xy": [1.2, 0.5],
           "moved_m": 121.2, "triggers": ["A1 the point is on the water"],
           "embankment": {"distance_m": 122.0, "length_m": 244.0, "crest_ft": 1640.4,
                          "line_xy": [[0, -100], [0, 100]]}}
    t = dpc.warning_text(rec)
    assert t.startswith(dpc.MOVED_PREFIX) and "122 m" in t and "dam_line_found.geojson" in t
    assert dpc.batch_text(rec) == "moved 121 m"
    assert "moved 121 m" in dpc.report_line(rec)
    fc = dpc.found_line_geojson(rec)
    assert fc["properties"]["used"] is True and fc["geometry"]["type"] == "LineString"
    for o in ("review", "kept"):
        fr = dpc.found_line_geojson(dict(rec, outcome=o))
        assert fr["properties"]["used"] is False and fr["properties"]["outcome"] == o
    assert dpc.found_line_geojson({"embankment": None}) is None
    from nd_breach_runner.core import report_docx
    assert report_docx.dam_location_text({"outcome": "skipped"}) is None
    assert "used as given" in report_docx.dam_location_text(
        {"outcome": "kept", "original_xy": [1.0, 2.0], "kept_because": "on the embankment (3 m)"})


def _vd_run(xy, pmf_ft, **st_kw):
    from nd_breach_runner.tests.synthetic import npy_writer
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        with _fake_models():
            r = workflow.run_dam(_dam(pmf_max_wse_ft=pmf_ft), xy, _st(**st_kw),
                                 S.DamTerrain(S.enclosed), npy_writer, os.path.join(d, "T1"))
        with open(r.report_json) as f:
            return json.load(f)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def dpc_local_datum_dam_takes_the_dam_line_branch_after_a_move():
    """Critique 15 (decided): a found line counts as a dam line for the local-datum rule
    (vdatum_run.apply_local: tie_ok = a mid-crest was found). A local (0 - 500 ft) pool with no
    top of dam is then held for review because no top was given, as for a user dam line; the
    same dam with its point kept (no line, no mid-crest) is held because there is no dam line.
    And the no-top pool check (a pool more than 50 ft above the DEM crest is local) reads the
    found line's mid-crest crest, not the water level read at the point."""
    moved, kept = _vd_run((-120.0, 0.0), 97.0), _vd_run((0.0, 0.0), 97.0)
    assert moved["dam_point_check"]["outcome"] == "moved"
    assert kept["dam_point_check"]["outcome"] == "kept"
    nm = moved["vertical_datum"]["fields"]["pmf_max_wse_ft"]
    nk = kept["vertical_datum"]["fields"]["pmf_max_wse_ft"]
    assert nm.get("unconverted") and nk.get("unconverted"), (nm, nk)
    assert "no top of dam was given" in nm["note"] and "dam line" not in nm["note"], nm
    assert "dam line" in nk["note"], nk
    assert "dam point check" in moved["dem_crest_from"]
    assert abs(moved["dem_crest_ft"] - kept["dem_crest_ft"]) < 1.0
    # a NAVD88 PMF pool 45.6 ft above the crest (1640.4 ft) but 53.8 ft above the water level
    # at the point (1632.2 ft): local only when the point's water level is taken as the crest
    pmf = 1686.0
    moved = _vd_run((-120.0, 0.0), pmf)
    off = _vd_run((-120.0, 0.0), pmf, dam_point_check="off")
    assert moved["dam_point_check"]["outcome"] == "moved"
    assert off["dam_point_check"]["outcome"] == "skipped"
    assert not moved["vertical_datum"].get("local"), moved["vertical_datum"]
    assert off["vertical_datum"].get("local"), off["vertical_datum"]
    assert moved["dam"]["pmf_max_wse_ft"] == pmf


# ------------------------------------------------------- validation and review, 2026-11-17 ---
@test
def dpc_closed_line_is_judged_by_net_turning():
    """G4c: the net turning of the line simplified at max(8 m, 0.04 L); zig-zag noise on an open
    line (hold-out: 364-1681 deg summed on correctly found lines) is not a ring."""
    xs = np.arange(0.0, 300.0, 4.0)
    zig = [(float(x), 3.0 * (1 if i % 2 else -1)) for i, x in enumerate(xs)]
    assert dpc.turning_deg(zig) < 90.0
    assert not dpc.line_closed(zig)[0]
    a = np.radians(np.arange(0.0, 330.0, 10.0))
    ring = [(100.0 * math.cos(t), 100.0 * math.sin(t)) for t in a]
    assert dpc.turning_deg(ring) > dpc.CLOSED_TURN_DEG and dpc.line_closed(ring)[0]
    # review 2026-11-17: each rule on its own
    # the end gap only: a hairpin (ends 10 m apart on a 400 m line, 177 deg of turning)
    hairpin = [(0.0, 0.0), (200.0, 5.0), (0.0, 10.0)]
    assert dpc.turning_deg(hairpin) <= dpc.CLOSED_TURN_DEG
    c, why = dpc.line_closed(hairpin)
    assert c is True and "closes on itself" in why and "ends 10 m apart" in why, why
    # the turning only: an opening spiral (330 deg, 315 deg after simplifying; ends 0.29 L
    # apart)
    a = np.radians(np.arange(0.0, 331.0, 5.0))
    r = 20.0 + 280.0 * a / a[-1]
    spiral = [(float(rr * math.cos(t)), float(rr * math.sin(t))) for rr, t in zip(r, a)]
    sp = np.asarray(spiral)
    assert math.hypot(*(sp[-1] - sp[0])) >= dpc.CLOSED_GAP_FRAC * dpc.line_length(spiral)
    assert dpc.turning_deg(spiral) > dpc.CLOSED_TURN_DEG
    c, why = dpc.line_closed(spiral)
    assert c is True and why.startswith("the line found turns") and "a ring or a U" in why, why
    # the left-out crest cells only: tested on the synthetic ring (dpc_ring_dike_goes_to_review)
    # a U with long arms closes by its ends (120 m apart on a 534 m line), not by its 180 deg
    u = [(0.0, 200.0), (0.0, 0.0), (60.0, -30.0), (120.0, 0.0), (120.0, 200.0)]
    assert abs(dpc.turning_deg(u) - 180.0) < 1.0
    c, why = dpc.line_closed(u)
    assert c is True and "ends 120 m apart on a 534 m line" in why, why
    # a wide-open U (short arms) is not closed
    u2 = [(0.0, 50.0), (0.0, 0.0), (60.0, -30.0), (120.0, 0.0), (120.0, 50.0)]
    assert dpc.line_closed(u2) == (False, ""), dpc.line_closed(u2)


@test
def dpc_tailwater_is_not_a_second_pond():
    """G5 counts the flat water within 400 m at the pond's level or above (less max(0.5 m,
    0.25 H)): tailwater and ponds below the dam are not a second pond; one above it is."""
    sv = {"level_m": 100.0, "bodies": [{"wse_m": 100.0, "edge_dist_m": 10.0},
                                       {"wse_m": 95.0, "edge_dist_m": 50.0}]}
    assert dpc.ponds_at_level(sv, 9.0) == 1
    sv["bodies"].append({"wse_m": 101.0, "edge_dist_m": 300.0})
    assert dpc.ponds_at_level(sv, 9.0) == 2
    sv["bodies"].append({"wse_m": 103.0, "edge_dist_m": 600.0})      # beyond 400 m
    assert dpc.ponds_at_level(sv, 9.0) == 2
    assert dpc.perp95_limit_m(3.0) == 15.0 and dpc.perp95_limit_m(12.0) == 24.0


@test
def dpc_crest_raise_bridges_ruts_but_not_a_notch():
    """Review 2026-11-17: a dip under 5 m (a rut, a 2 m DEM pit) is not a gap in the crest
    raise; a 12 m spillway notch still is."""
    line = [(0.0, 0.0), (200.0, 0.0)]

    def z(x, y, a=None):
        x = np.asarray(x, float)
        out = np.full(x.shape, 10.0)
        out[(x > 50.0) & (x < 53.0)] = 9.0            # a 3 m rut
        out[(x > 120.0) & (x < 132.0)] = 8.5          # a 12 m notch
        return out
    parts = dpc.on_crest_parts(line, z, 10.0, 0.3)
    assert len(parts) == 2, parts
    assert parts[0][0][0] == 0.0 and 119.0 <= parts[0][-1][0] <= 121.0, parts
    assert dpc.on_crest_parts(line, z, 10.0, 0.3, min_gap_m=0.0)[0][-1][0] < 51.0


@test
def dpc_found_line_keeps_the_embankments_downstream_side():
    """Validation 2026-11-17 (DWR-2635, 2662, 2760): a run on the found line takes the side the
    survey found the embankment to fall away from its pond, not terrain.downstream_direction;
    a user's dam line is unchanged."""
    from nd_breach_runner.core import reservoir_carve as rc
    rec = _check(S.enclosed, (-120.0, 0.0))
    assert rec["outcome"] == "moved"
    ds = rec["_ds_dir"]
    assert ds[0] > 0.9, ds                                   # the coulee falls away east
    dam = _dam(crest_line_xy=[(0.0, 100.0), (0.0, -100.0)])
    assert dpc.found_line_side(dam) is None                  # a user's line
    dam._crest_line_source = dpc.FOUND_LINE_SOURCE
    dam._dpc_ds_dir = ds
    v, how = dpc.found_line_side(dam)
    assert v[0] > 0.99 and "dam point check" in how, (v, how)
    assert rc.downstream_for(None, dam, (0.0, 0.0), S.H, None, None, 10.0)[0] == v
    assert "dam point check" in rec["verify"]["inflow_how"]


@test
def dpc_cache_key_holds_what_the_decision_reads():
    """Review 2026-11-17: the normal pool, the other dams' crest lines and the toe-band settings
    are in the cache key; the cache is locked (a preview and a run in two tasks)."""
    dpc.cache_clear()
    t = S.DamTerrain(S.enclosed)
    dam = _dam()
    rep = {}
    workflow._dam_point_check(dam, _st(), (-120.0, 0.0), None, t, 20.0, rep, [], lambda m: None)
    assert t.checks() == 1 and rep["dam_point_check"]["outcome"] == "moved"
    # review 2026-11-17: the neighbours reach the decision (G7c) through the full check, and
    # a check made without them is not reused
    n = 1
    for st, g7c in ((_st(dam_point_lines=[[(4.0, 120.0), (4.0, -120.0)]]), True),
                    (_st(dam_point_others=[("B", (0.0, 0.0), S.H_FT)]), True),
                    (_st(dam_point_others=[("B", (-700.0, 0.0), S.H_FT)]), False),
                    (_st(inflow_toe_min_outer_m=40.0), False)):
        rep, w = {}, []
        xy = workflow._dam_point_check(_dam(), st, (-120.0, 0.0), None, t, 20.0, rep, w,
                                       lambda m: None)
        n += 1
        assert t.checks() == n, (t.checks(), n)
        pc = rep["dam_point_check"]
        if g7c:
            assert pc["outcome"] == "review" and xy == (-120.0, 0.0), pc
            assert "G7c" in _codes(pc["gates_failed"]), pc["gates_failed"]
            assert any(x.startswith(dpc.REVIEW_PREFIX) for x in w), w
        else:
            assert pc["outcome"] == "moved", pc
    d2 = _dam()
    d2.normal_pool_ft = 1700.0                      # as the batch / DWR record sets it
    d2._vdatum_done = True
    rep = {}
    workflow._dam_point_check(d2, _st(), (-120.0, 0.0), None, t, 20.0, rep, [], lambda m: None)
    assert t.checks() == n + 1 and rep["dam_point_check"]["outcome"] == "review"
    assert "G7e" in _codes(rep["dam_point_check"]["gates_failed"])
    assert dpc.cache_get(("nothing",)) is None and isinstance(dpc._LOCK, type(dpc._LOCK))


@test
def dpc_preview_and_a_dem_filled_top_read_once_and_agree():
    """Critique 9 / review 2026-11-17: the preview (no top) and the single-dam tool's DEM fill
    of the top (nanmax within 30 m, marked _top_from_dem) replay the same top, so they share
    one check; precheck_dam_point gives the filled dam the mid-crest top of the found line."""
    dpc.cache_clear()
    t = S.DamTerrain(S.enclosed)
    xy = (-120.0, 0.0)
    workflow.preview(_dam(), xy, _st(), t)
    n = t.checks()
    assert n == 1, n
    dam = _dam(top_of_dam_ft=S.POOL / 0.3048)        # the fill: the water level at the point
    dam._top_from_dem = True
    rec = workflow.precheck_dam_point(dam, _st(), xy, None, t)
    assert t.checks() == n and rec["outcome"] == "moved", rec
    assert abs(dam.top_of_dam_ft - S.TOP / 0.3048) < 1.0 and dam._top_from_dem
    assert dam._point_check is rec
    # review 2026-11-17: run_dam's own check (no precheck) clears the filled top on a move, so
    # the run reads the mid-crest top; a top given is never touched (the check does not run)
    d3 = _dam(top_of_dam_ft=S.POOL / 0.3048)
    d3._top_from_dem = True
    workflow._dam_point_check(d3, _st(), xy, None, t, 20.0, {}, [], lambda m: None)
    assert d3._point_check["outcome"] == "moved" and d3.top_of_dam_ft is None
    d4 = _dam(top_of_dam_ft=S.POOL / 0.3048)
    d4._top_from_dem = True
    workflow._dam_point_check(d4, _st(dam_point_check="warn"), xy, None, t, 20.0, {}, [],
                              lambda m: None)
    assert d4._point_check["outcome"] == "review" and d4.top_of_dam_ft == S.POOL / 0.3048


@test
def dpc_cascade_takes_the_mid_crest_top_before_routing():
    """Review 2026-11-17 (critique 8): the cascade's DEM fill at a point on the pond is the
    water level; the check runs before the overtop routing and the WSE trigger, so the dam
    reaches run_dam with the found line's mid-crest top and the moved result on it."""
    from nd_breach_runner.core import cascade
    from nd_breach_runner.core import breach as brmod
    dpc.cache_clear()
    seen = []
    real = workflow.run_dam

    def fake_run_dam(dam, xy, st, *a, **k):
        seen.append((dam.top_of_dam_ft, (getattr(dam, "_point_check", None) or {}).get(
            "outcome")))
        raise brmod.BreachInputError("stop here")
    chain = [cascade.CascadeDam(_dam(dam_id="A"), (-120.0, 0.0)),
             cascade.CascadeDam(_dam(dam_id="B"), (900.0, 0.0))]
    workflow.run_dam = fake_run_dam
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        from nd_breach_runner.tests.synthetic import npy_writer
        cascade.run_cascade(chain, _st(), S.DamTerrain(S.enclosed), npy_writer, d)
    finally:
        workflow.run_dam = real
        shutil.rmtree(d, ignore_errors=True)
    assert seen and seen[0][1] == "moved", seen
    assert abs(seen[0][0] - S.TOP / 0.3048) < 1.0, seen          # not the water level


@test
def dpc_hecras_gets_the_modelled_point_and_the_found_label():
    """Critique 20: run_hecras_stage hands the modelled (mid-crest) point to adopt_final_engine
    (the products, the EAP map, the dam point feature), and the HEC-RAS model labels the found
    line as found, not "your dam line"."""
    from nd_breach_runner.core import hecras_model as hm
    import types
    seen = []
    old = workflow.adopt_final_engine

    class _Eng(object):
        HecRasEngineError = RuntimeError

        def run_stage(self, folder, final, writer, st, log):
            return {"final": {"depth": "d.tif"}, "warnings": []}
    res = types.SimpleNamespace(final={"depth": "l.tif"}, warnings=[],
                                summary={"final_model": "LISFLOOD-FP"},
                                modelled_dam_xy=(1.1, 0.1))
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        workflow.adopt_final_engine = lambda folder, name, st, xy, log: (
            seen.append(xy) or {"final_model": name})
        workflow.run_hecras_stage(d, res, None, workflow.RunSettings(),
                                  workflow.hecras_dam_xy(res, (-120.0, 0.0)), None,
                                  engine=_Eng())
    finally:
        workflow.adopt_final_engine = old
        shutil.rmtree(d, ignore_errors=True)
    assert seen == [(1.1, 0.1)], seen
    run = _full_run(S.enclosed, (-120.0, 0.0))
    assert run["rep"]["crest_line_source"] == dpc.FOUND_LINE_SOURCE
    assert hm.report_line_source(run["rep"]) == dpc.FOUND_LINE_SOURCE
    assert hm.report_line_source({"crest_line_xy": [[0, 0], [1, 1]]}) == "your dam line"


@test
def dpc_far_clean_embankment_warns_and_a_doubtful_far_one_is_a_note():
    """Validation 2026-11-17: a point 50 m or more from a clean embankment of the dam's height
    on flat water, with no trigger, gets a REVIEW warning (it was kept silently); a trigger
    measured against an embankment 100 m or more away that fails G1 is a note only."""
    sv, rp = _survey(S.enclosed, (-120.0, 0.0))
    rp_ok = dict(rp, top_m=sv["emb"]["crest_z_m"], failed=False, inflow_xy=None)
    sv2 = dict(sv, point_on_water=False, point_wid=None, inflow={})
    d = _decide(sv2, rp_ok, S.H)
    assert d["outcome"] == "review" and d["warn"] and not d["triggers"], d
    assert d["reasons"][0].startswith("the point is 122 m from a clean embankment"), d
    emb = dict(sv["emb"], point_to_crest_m=150.0, confirmed=False)
    sv3 = dict(sv2, emb=emb)
    rp3 = dict(rp_ok, top_m=sv["emb"]["crest_z_m"] - 4.0)          # A2: 13 ft low
    d3 = _decide(sv3, rp3, S.H)
    assert _codes(d3["triggers"]) == {"A2"} and "G1" in _codes(d3["gates_failed"]), d3
    assert d3["outcome"] == "kept" and not d3["warn"] and "a note only" in d3["kept_why"], d3


# ------------------------------------------------------------ review fixes, 2026-11-17 ---
def _flood(npft):
    from nd_breach_runner.core import flood as fl
    return fl.FloodInputs.triangle(5000.0, 2.0, 8.0, normal_pool_ft=npft)


@test
def dpc_g7e_reads_the_normal_pool_before_the_runs_datum_conversion():
    """Review 2026-11-17: the batch tool and the CLI convert the dam elevations (NGVD29 ->
    NAVD88) only in run_dam, after the dam point check; G7e used to see no normal pool there
    and was skipped. The check now converts a copy of the pool the same way, changing nothing
    on the dam or the settings, and the run (batch / CLI order: no conversion before run_dam)
    sends a pond far from its normal pool to review."""
    from nd_breach_runner.core import vdatum_run as vdrun
    lvl_ft = S.POOL / 0.3048
    ll = {"point": {"lon": -100.5, "lat": 47.0}}
    # the pool as the run's datum block makes it, before that block runs
    st = _st(flood=_flood(1450.0), dam_record=ll)
    dam = _dam()
    got = workflow._dpc_normal_pool_m(dam, st, (-120.0, 0.0))
    assert st.vertical_datum is None and st.flood.normal_pool_ft == 1450.0
    assert not getattr(dam, "_vdatum_done", False) and not getattr(st.flood, "_vdatum_done", False)
    vdrun.apply(dam, st, -100.5, 47.0, top_is_dem=True)
    assert st.flood.normal_pool_ft != 1450.0                     # NGVD29 shifted
    assert abs(got - st.flood.normal_pool_ft * 0.3048) < 1e-9, (got, st.flood.normal_pool_ft)
    assert workflow._dpc_normal_pool_m(dam, st) == got           # the same after it ran
    assert workflow._dpc_normal_pool_m(_dam(), _st(flood=_flood(400.0), dam_record=ll)) is None
    # the full check and the run, in the batch / CLI order
    from nd_breach_runner.tests.synthetic import npy_writer
    for npft, want in ((lvl_ft - 20.0, "review"), (lvl_ft, "moved")):
        dpc.cache_clear()
        st = _st(flood=_flood(npft), dam_record=ll)
        rep = {}
        workflow._dam_point_check(_dam(), st, (-120.0, 0.0), None, S.DamTerrain(S.enclosed),
                                  20.0, rep, [], lambda m: None)
        pc = rep["dam_point_check"]
        assert pc["outcome"] == want, (npft, pc)
        assert ("G7e" in _codes(pc["gates_failed"])) == (want == "review"), pc["gates_failed"]
        assert st.flood.normal_pool_ft == npft and st.vertical_datum is None
        dpc.cache_clear()
        d = tempfile.mkdtemp(prefix="ndbr_dpc_")
        try:
            st = _st(flood=_flood(npft), dam_record=ll)
            with _fake_models():
                r = workflow.run_dam(_dam(), (-120.0, 0.0), st, S.DamTerrain(S.enclosed),
                                     npy_writer, os.path.join(d, "T1"))
            with open(r.report_json) as f:
                rr = json.load(f)
        finally:
            shutil.rmtree(d, ignore_errors=True)
        pc = rr["dam_point_check"]
        assert pc["outcome"] == want, (npft, pc)
        if want == "review":
            g = [x for x in pc["gates_failed"] if x.startswith("G7e ")]
            # the pool G7e read is the pool the run converted
            assert g and "{:.1f} ft".format(rr["vertical_datum"]["fields"]["normal_pool_ft"][
                "navd88"]) in g[0], (g, rr["vertical_datum"]["fields"])
    dpc.cache_clear()


@test
def dpc_warn_mode_runs_v_and_a_failed_v_is_never_moved():
    """Review 2026-11-17: V (the toe inflow 0.5-1.3 H below the found mid-crest) runs in warn
    mode too, so "would move" is said only of an embankment that move would use; a failed V
    sends a move to review and keeps the point."""
    t = S.DamTerrain(S.enclosed)
    dpc.cache_clear()
    rep = {}
    workflow._dam_point_check(_dam(), _st(dam_point_check="warn"), (-120.0, 0.0), None, t,
                              20.0, rep, [], lambda m: None)
    pc = rep["dam_point_check"]
    assert pc["outcome"] == "review" and pc["would_move"] and pc["verify"]["ok"], pc
    assert dpc.batch_text(pc) == "review (would move)"
    old = workflow._dpc_verify
    why = "the toe band along the line found no inflow"
    workflow._dpc_verify = lambda *a, **k: {"ok": False, "why": why, "toe_band": False}
    try:
        for mode in ("move", "warn"):
            dpc.cache_clear()
            dam, rep, w = _dam(), {}, []
            xy = workflow._dam_point_check(dam, _st(dam_point_check=mode), (-120.0, 0.0), None,
                                           t, 20.0, rep, w, lambda m: None)
            pc = rep["dam_point_check"]
            assert pc["outcome"] == "review" and not pc["would_move"], (mode, pc)
            v = [g for g in pc["gates_failed"] if g.startswith("V ")]
            assert v and why in v[0], (mode, pc["gates_failed"])
            assert pc["verify"]["ok"] is False and pc.get("modelled_xy") is None
            assert xy == (-120.0, 0.0) and not getattr(dam, "crest_line_xy", None)
            assert getattr(dam, "_crest_line_source", None) is None
            assert dpc.batch_text(pc) == "review", dpc.batch_text(pc)
            txt = [x for x in w if x.startswith(dpc.REVIEW_PREFIX)]
            assert txt and "would be used" not in txt[0] and "found but not used" in txt[0], txt
            assert not any(x.startswith(dpc.MOVED_PREFIX) for x in w)
    finally:
        workflow._dpc_verify = old
        dpc.cache_clear()


@test
def dpc_cache_key_holds_the_runs_resample_method():
    """Review 2026-11-17: the replay and V read through the run's terrain read (its resample
    method); a check made with one method is not reused with another."""
    need_osgeo()
    from nd_breach_runner.core import gdal_io as GI
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        path = S.write_geotiff(S.enclosed, os.path.join(d, "dem.tif"),
                               bounds=(-1200.0, -1200.0, 1200.0, 1200.0))
        mk = lambda how: GI.GdalTerrain(path, "EPSG:26914", z_choice=GI.Z_METERS, resample=how)
        assert dpc.dem_stamp(mk("bilinear")) != dpc.dem_stamp(mk("near"))
        assert dpc.dem_stamp(mk("bilinear")) == dpc.dem_stamp(mk("bilinear"))
        assert dpc.dem_stamp(mk("near"))[-1] == "near"

        class _Wrap(object):                     # a wrapper over the file source (the run's)
            def __init__(self, base):
                self.base = base
        assert dpc.dem_stamp(_Wrap(mk("near"))) == dpc.dem_stamp(mk("near"))
        dpc.cache_clear()
        reused = []
        for how in ("bilinear", "bilinear", "near"):
            logs = []
            workflow._dam_point_check(_dam(), _st(dam_point_check="warn"), (-120.0, 0.0), None,
                                      mk(how), 20.0, {}, [], logs.append)
            reused.append(any("earlier check" in m for m in logs))
        assert reused == [False, True, False], reused
    finally:
        dpc.cache_clear()
        shutil.rmtree(d, ignore_errors=True)


@test
def dpc_found_line_file_says_not_used_in_warn_mode():
    """Review 2026-11-17: inputs/dam_line_found.geojson of a warn-mode run says used = false."""
    dpc.cache_clear()
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    try:
        rep = {}
        workflow._dam_point_check(_dam(), _st(dam_point_check="warn"), (-120.0, 0.0), None,
                                  S.DamTerrain(S.enclosed), 20.0, rep, [], lambda m: None,
                                  out_folder=d)
        assert rep["dam_point_check"]["outcome"] == "review"
        with open(os.path.join(d, "inputs", dpc.FOUND_LINE_FILE)) as f:
            gj = json.load(f)
        props = gj["features"][0]["properties"]
        assert props["used"] is False and props["outcome"] == "review", props
    finally:
        dpc.cache_clear()
        shutil.rmtree(d, ignore_errors=True)


@test
def dpc_hecras_model_labels_the_found_line_as_found():
    """Review 2026-11-17: build_model hands the run's found line to the crest step with the
    found label (its notes and record), a user's line with "your dam line"."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.core import hecras_crest as hc
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    seen = []
    old = hm.crest_dam_line

    def fake(*a, **k):
        seen.append(k.get("user_line_source"))
        raise hc.CrestTraceError("test: stop at the crest step")
    try:
        p0._run(d)
        rp = os.path.join(d, "_breach_model", "run_report.json")
        with open(rp) as f:
            rep = json.load(f)
        x0, y0 = rep["dam_xy"][:2]
        rep["crest_line_xy"] = [[x0 - 150.0, y0], [x0 + 150.0, y0]]
        hm.crest_dam_line = fake
        for src, want in ((dpc.FOUND_LINE_SOURCE, dpc.FOUND_LINE_SOURCE),
                          (None, "your dam line")):
            rep["crest_line_source"] = src
            with open(rp, "w") as f:
                json.dump(rep, f)
            hm.build_model(d, out_folder=os.path.join(d, "m{}".format(len(seen))))
            assert seen and seen[-1] == want, (seen, want)
    finally:
        hm.crest_dam_line = old
        shutil.rmtree(d, ignore_errors=True)
    assert seen == [dpc.FOUND_LINE_SOURCE, "your dam line"], seen


@test
def dpc_tools_and_the_cli_pass_neighbours_and_the_modelled_point():
    """Review 2026-11-17: what the QGIS tools and the CLI hand run_dam and the HEC-RAS stage:
    the other crest lines (CREST_LINE) and the batch's other dams as neighbours for G7c, and the
    modelled (moved) point to HEC-RAS (critique 7), not the point given."""
    import csv
    import sys
    import types
    from nd_breach_runner.core import hecras_engine
    from nd_breach_runner.tests.preflight_stub import without_dem_and_smoke_checks
    from nd_breach_runner.tests.run_tests import _patched_algs
    from nd_breach_runner.tests.test_v320 import _FakeRaster
    from nd_breach_runner.tests import test_cli as tc
    algs, fq = _patched_algs()
    # the CREST_LINE layer: this dam's line and another dam's (apply_features)
    own, other = [(0.0, -100.0), (0.0, 100.0)], [(400.0, -100.0), (400.0, 100.0)]
    lines = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [own])),
                           fq.FakeFeature(fq.FakeGeometry("line", [other]))])
    a0 = algs.RunDamAlgorithm()
    a0.initAlgorithm()
    st0 = workflow.RunSettings()
    a0.apply_features(st0, {"CREST_LINE": lines}, fq.QgsProcessingContext(),
                      fq.QgsCoordinateReferenceSystem("EPSG:26914"), (0.0, 0.0))
    assert st0.midcrest_line == own and st0.dam_point_lines == [other], st0.dam_point_lines
    # Model one dam and the batch tool with models = LISFLOOD-FP then HEC-RAS 2D
    tier = [t_[0] for t_ in algs.TIERS].index(workflow.TIER_HECRAS)
    seen, staged = [], []

    def fake_run_dam(dam, xy, st, *a, **k):
        seen.append((dam.dam_id, st.dam_point_others, st.dam_point_lines))
        return types.SimpleNamespace(summary={"breach_occurs": True}, warnings=[],
                                     modelled_dam_xy=(float(xy[0]) + 7.0, float(xy[1]) + 3.0))

    def fake_stage(folder, res, writer, st, dam_xy, log, **k):
        staged.append((float(dam_xy[0]), float(dam_xy[1])))
        raise algs.HecRasTierError("test: stop at the HEC-RAS stage")
    keep = (workflow.run_dam, algs.hecras_tier_stage, hecras_engine.detect)
    workflow.run_dam = fake_run_dam
    algs.hecras_tier_stage = fake_stage
    hecras_engine.detect = lambda *a, **k: {"available": True, "reason": ""}
    d = tempfile.mkdtemp(prefix="ndbr_dpc_")
    common = {"DEM": _FakeRaster(), "CELL": 20.0, "LENGTH_MI": 1.0, "HALF_WIDTH_MI": 0.5,
              "MAX_RUNS": 1, "LISFLOOD_EXE": sys.executable, "DAMS_FOLDER": d, "TIER": tier}

    @without_dem_and_smoke_checks
    def tools():
        a = algs.RunDamAlgorithm()
        a.initAlgorithm()
        try:
            a.processAlgorithm(dict(common, DAM_ID="A", DAM_POINT=fq.QgsPointXY(0, 0),
                                    HEIGHT_FT=25, STORAGE_ACFT=500, TOP_FT=1850, AREA_AC=60,
                                    CREST_LINE=lines),
                               fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
            raise AssertionError("expected the stop at the HEC-RAS stage")
        except fq.QgsProcessingException as e:
            assert "stop at the HEC-RAS stage" in str(e), e
        pt = lambda x: fq.FakeGeometry("point", [[(x, 0.0)]])
        feats = [fq.FakeFeature(pt(0.0), NIDID="B1", H=25.0, S=500.0, TOP=1850.0),
                 fq.FakeFeature(pt(300.0), NIDID="B2", H=30.0, S=500.0, TOP=1850.0)]
        b = algs.BatchAlgorithm()
        b.initAlgorithm()
        b.processAlgorithm(dict(common, DAMS=fq.FakeSource(feats), F_ID="NIDID", F_HEIGHT="H",
                                F_STORAGE="S", F_TOP="TOP", SKIP_DONE=False),
                           fq.QgsProcessingContext(), fq.QgsProcessingFeedback())
    try:
        tools()
    finally:
        workflow.run_dam, algs.hecras_tier_stage, hecras_engine.detect = keep
        shutil.rmtree(d, ignore_errors=True)
    assert [s_[0] for s_ in seen] == ["A", "B1", "B2"], seen
    assert seen[0][2] == [other], seen[0]                            # Model one dam
    assert seen[1][1] == [("B2", (300.0, 0.0), 30.0)], seen[1]       # the batch tool
    assert seen[2][1] == [("B1", (0.0, 0.0), 25.0)], seen[2]
    assert staged == [(7.0, 3.0), (7.0, 3.0), (307.0, 3.0)], staged
    # the CLI: the batch's other dams, and the modelled point to its HEC-RAS stage
    c = tc.cli()
    calls = []
    with tc.Tmp() as d:
        p, cfg = tc.base_config(d)
        cfg["dam"] = {"point": None}
        cfg["batch"] = {"crs": None}
        cfg["scenario"] = {"type": "sunny"}
        tc.dump(p, cfg)
        table = os.path.join(d, "dams.csv")
        with open(table, "w") as f:
            f.write("NIDID,Dam Name,Dam Height (ft),Max Storage (acre-feet),Surface Area (acres),"
                    "Top of Dam (ft),x,y\n")
            f.write("C1,Alpha,25,20,4,1850,0,0\n")
            f.write("C2,Beta,30,20,4,1850,300,0\n")
        fake_exe = os.path.join(d, "lisflood")
        with open(fake_exe, "w") as f:
            f.write("x")

        def cli_run_dam(dam, dam_xy, st, *a, **k):
            calls.append((dam.dam_id, st.dam_point_others))
            return tc._FakeResult(dam.dam_id)
        keep = c.workflow.run_dam
        c.workflow.run_dam = cli_run_dam
        try:
            tc.run_cli("batch", table, p, "--lisflood", fake_exe)
        finally:
            c.workflow.run_dam = keep
        with open(os.path.join(d, "out", "Batch_Breach_Runs.csv")) as fh:
            rows = {r["Dam"]: r for r in csv.DictReader(fh)}
    assert rows["C1"]["Status"] == "done" and rows["C2"]["Status"] == "done", rows
    assert [x[0] for x in calls] == ["C1", "C2"], calls
    for (did, oth), (oid, oxy, oh) in zip(calls, (("C2", (300.0, 0.0), 30.0),
                                                  ("C1", (0.0, 0.0), 25.0))):
        assert oth and len(oth) == 1 and oth[0][0] == oid and oth[0][2] == oh, (did, oth)
        assert np.allclose(oth[0][1], oxy, atol=0.01), (did, oth)
    got = []
    keep = c.workflow.run_hecras_stage
    c.workflow.run_hecras_stage = lambda folder, res, writer, st, xy, log, **k: got.append(xy)
    try:
        pp = types.SimpleNamespace(st=types.SimpleNamespace(tier=workflow.TIER_HECRAS),
                                   folder="", setup=types.SimpleNamespace(writer=None),
                                   dam_xy=(-120.0, 0.0))
        for mxy in ((1.5, 2.5), None):
            res = types.SimpleNamespace(summary={"breach_occurs": True}, modelled_dam_xy=mxy)
            c._hecras_stage(pp, res, lambda m: None)
            c._hecras_stage(pp, res, lambda m: None, unattended=True)
    finally:
        c.workflow.run_hecras_stage = keep
    assert got == [(1.5, 2.5), (1.5, 2.5), (-120.0, 0.0), (-120.0, 0.0)], got
