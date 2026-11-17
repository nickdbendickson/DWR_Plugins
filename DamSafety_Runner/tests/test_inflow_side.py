"""3.20.12 (Nick, 2026-11-17: "Do breach inflow in res next"): with a dam line given, the toe-band
breach inflow landed on the reservoir of tall dams with a full hydro-flattened pond (DWR-2624,
2979, 2983, 2791, 2713, 2824, 2635, 2662, 2809, 2760 ...). The pond drains at its own level, so
pool_mask does not see it; pool_at_dam took a borrow pit below the dam as the pool, or
_lower_side found the water 20-80 m upstream lower than the downstream berm, and the side
turned round. Now the reservoir's flat water decides a dam line's side first
(terrain.water_side, on the DEM at 2 m round the line), and a toe pick on flat water against
the line is checked afterwards (workflow._inflow_water_check: the other side, else
CHECK THE INFLOW SIDE). Synthetic DEMs only (tests/synthetic_dams.inflow_side_dam).
Review: the water test does not override a dry reservoir (a substantial dry closed basin) on the
other side of the line, nor larger lower water there, and the check never moves the pick into
the pool that decided the side or a basin filling above its water: the old side, with
CHECK THE INFLOW SIDE.
Lead (2026-11-17): no more tuning against synthetic layouts; every override is visible. When the
water's side is taken against the old rules (as 3.20.11 ran them), the water's side is kept with
a CHECK THE INFLOW SIDE warning naming both readings (water_check.side_conflict, kind
terrain.CONFLICT_WATER_KEPT); rules that agree add nothing."""
import functools

import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests import synthetic_dams as sd

LINE = sd.SIDE_LINE                    # x = 0, A south -> B north: chord normal west (upstream)
EAST = (1.0, 0.0)                      # downstream


def _fn(**kw):
    f = functools.partial(sd.inflow_side_dam, **kw)
    f.__name__ = "inflow_side_dam_" + "_".join("{}{}".format(k, v) for k, v in sorted(kw.items()))
    return f


BUG_BERM = _fn(pool_drop_h=0.45, berm_drop_h=0.22)          # _lower_side picked the reservoir
BUG_PIT = _fn(pool_drop_h=0.3, pit=True)                    # the pit below the dam was the pool
POND_065 = _fn(pool_drop_h=0.65, berm_drop_h=0.4, pit=True)  # below pool_mask's 0.5 H band
DRY = _fn(pool_drop_h=None)                                 # a dry reservoir: a closed basin
DRY_DS_POND = _fn(pool_drop_h=None, ds_pond=(1.05, 50.0, 25.0))
FULL_DS_POND = _fn(pool_drop_h=0.3, ds_pond=(1.05, 50.0, 25.0))
DEEP_POND = _fn(pool_drop_h=0.9, floor_drop_h=1.5, berm_drop_h=0.22)   # past the water window
CAUSEWAY = _fn(pool_drop_h=0.3, ds_water=True)              # one level both sides (DWR-3161)
ROAD_LOW = _fn(pool_drop_h=0.3, road=(110.0, 0.35))         # a road-held low below (DWR-2791)
RES_072 = _fn(pool_drop_h=0.72, berm_drop_h=0.4)            # just inside the window (0.75 H)
TWO_LEVELS = _fn(pool_drop_h=0.3, ds_pond=(0.6, 50.0, 30.0))  # reservoir 0.3 H, a pond 0.6 H below
WITHIN_TIE = _fn(pool_drop_h=0.9, floor_drop_h=1.27, berm_drop_h=0.22)  # the toe at the water
DEEP_ROAD = _fn(pool_drop_h=0.9, floor_drop_h=1.5, berm_drop_h=0.22, road=(105.0, 0.95))
# review: a dry reservoir (a closed basin up to the crest) with a pond below the dam
DRY_POND_06 = _fn(pool_drop_h=None, ds_pond=(0.6, 50.0, 30.0))   # in the water window
DRY_POND_09 = _fn(pool_drop_h=None, ds_pond=(0.9, 45.0, 30.0))   # past it: the check's case
DRY_TAILWATER = _fn(pool_drop_h=None, road=(90.0, 0.5), tail_water=0.9)  # a road-held tail pond
DRAWN_DOWN = _fn(pool_drop_h=0.85, floor_drop_h=1.5, ds_pond=(0.6, 50.0, 30.0))
DRAWN_DOWN_BERM = _fn(pool_drop_h=0.85, floor_drop_h=1.5, berm_drop_h=0.22,
                      ds_pond=(0.6, 50.0, 30.0))


def _pick(fn, line=LINE, dam_line=True, water=True, ds_slope=3.0, earlier=(), **dam_attrs):
    """workflow._downstream_inflow with `line` as DAM_LINES (dam_line) or a batch's CREST_LINE.
    water=False: the 2 m water window is not read (the side as before 3.20.12). `dam_attrs`
    are set on the dam (a found line's _crest_line_source, _dpc_ds_dir); `earlier`: warnings
    the run gave before the inflow."""
    from nd_breach_runner.core import workflow
    src = sd.DamTerrain(fn)

    class Dam(object):
        crest_line_xy = line if dam_line else None
        ds_slope_h = ds_slope
    for k, v in dam_attrs.items():
        setattr(Dam, k, v)

    class St(object):
        midcrest_line = None if dam_line else line
        inflow_toe_band_m = None
        inflow_toe_cell_m = None
    local = src.read((-430.0, -430.0, 430.0, 430.0), 10.0)
    warnings, info = list(earlier), {}
    real = workflow._water_window
    if not water:
        def none(*_a, **_k):
            raise RuntimeError("not read")
        workflow._water_window = none
    try:
        pt, why = workflow._downstream_inflow(src, local, Dam(), (0.0, 0.0), sd.H, sd.TOP, 90.0,
                                              10.0, warnings, lambda *_a: None, out=info,
                                              **workflow._toe_kwargs(Dam(), St()))
    finally:
        workflow._water_window = real
    assert pt is not None, why
    z = fn(np.array([[pt[0]]]), np.array([[pt[1]]]))[0, 0]
    return {"xy": pt, "why": why, "warnings": warnings, "info": info,
            "drop_h": (sd.TOP - z) / sd.H, "east": info["direction"][0] > 0.9}


def _on_reservoir(r):
    return r["xy"][0] < 0.0


@register
def full_reservoir_lower_than_the_downstream_berm_keeps_the_inflow_below_the_dam():
    old = _pick(BUG_BERM, water=False)          # the bug: _lower_side took the water upstream
    assert not old["east"] and _on_reservoir(old) and old["info"]["side_how"] == "lower_side", old
    r = _pick(BUG_BERM)
    assert r["east"] and not _on_reservoir(r), r
    assert r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
    assert "away from the reservoir's flat water" in r["why"] and "0.45 H" in r["why"], r["why"]
    assert r["drop_h"] >= terrain.INFLOW_TOE_DROP, r
    assert r["info"]["side_water"]["below_top_h"] == 0.45, r["info"]
    wc = r["info"]["water_check"]
    assert wc["ok"] and not wc["on_water"], wc
    # lead: the old rule (the lower side) pointed the other way: kept the water's side, warned
    sc = wc["side_conflict"]
    assert sc["kind"] == terrain.CONFLICT_WATER_KEPT, sc
    assert sc["old"]["side_how"] == terrain.SIDE_LOWER and sc["old"]["direction"][0] < -0.9, sc
    assert r["warnings"] == [sc["warning"]], r["warnings"]
    assert "the older rule (the lower ground beside the dam line" in sc["warning"], sc


@register
def borrow_pit_below_the_dam_no_longer_turns_the_side_to_the_reservoir():
    old = _pick(BUG_PIT, water=False)           # the bug: the pit was "the pool"
    assert not old["east"] and old["info"]["side_how"] == "pool", old
    r = _pick(BUG_PIT)
    assert r["east"] and not _on_reservoir(r) and r["drop_h"] > 0.75, r
    assert r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]


@register
def pond_0_65_h_below_the_top_is_the_reservoir():
    old = _pick(POND_065, water=False)          # outside pool_mask's shallow band (0.5 H)
    assert not old["east"], old
    r = _pick(POND_065)
    assert r["east"] and not _on_reservoir(r) and r["drop_h"] > 0.75, r
    assert "0.65 H" in r["why"], r["why"]
    assert r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]       # not the flip
    assert not r["info"]["water_check"].get("flipped"), r["info"]
    assert r["warnings"] == [r["info"]["water_check"]["side_conflict"]["warning"]], r["warnings"]
    # 0.72 H is still in the window (WATER_SIDE_DROP 0.75 H)
    r = _pick(RES_072)
    assert r["east"] and r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
    assert r["info"]["side_water"]["below_top_h"] == 0.72, r["info"]


@register
def dry_reservoir_keeps_the_old_rule_and_text():
    old = _pick(DRY, water=False)
    r = _pick(DRY)
    assert r["east"] and r["xy"] == old["xy"] and r["why"] == old["why"], (r, old)
    assert r["info"]["side_how"] == terrain.SIDE_POOL, r["info"]
    assert r["why"].endswith("downstream side (crest-line normal away from the pool)"), r["why"]
    assert r["info"]["side_water"]["why"].startswith("no flat water"), r["info"]
    assert not r["warnings"], r["warnings"]


@register
def downstream_pond_does_not_pull_the_side_upstream():
    # a dry reservoir: the old rule (the pool); the inflow is on the pond at the toe, 1.05 H
    # below the top, and the other side's toe is higher than that water: kept, no warning
    r = _pick(DRY_DS_POND)
    assert r["east"] and r["info"]["side_how"] == terrain.SIDE_POOL, r
    wc = r["info"]["water_check"]
    assert wc["on_water"] and wc["ok"] and "does not hold it up" in wc["kept"], wc
    assert not r["warnings"], r["warnings"]
    # a full reservoir upstream: its water decides; the pond below is lower: kept
    r = _pick(FULL_DS_POND)
    assert r["east"] and r["info"]["side_how"] == terrain.SIDE_WATER, r
    wc = r["info"]["water_check"]
    assert wc["on_water"] and wc["ok"] and "higher flat water" in wc["kept"], wc
    assert not r["warnings"], r["warnings"]
    # the water test alone: the pond (1.05 H down) is not the reservoir, the reservoir is
    g = sd.grid_of(DRY_DS_POND, (-120.0, -180.0, 120.0, 180.0), 2.0)
    n = terrain.chord_normal(LINE)
    up, _b, why = terrain.water_side(g, LINE, n, sd.H, sd.TOP)
    assert up == 0 and why.startswith("no flat water"), why
    g = sd.grid_of(FULL_DS_POND, (-120.0, -180.0, 120.0, 180.0), 2.0)
    up, b, _why = terrain.water_side(g, LINE, n, sd.H, sd.TOP)
    assert up == 1 and abs(b["level"] - (sd.TOP - 0.3 * sd.H)) < 0.01, (up, b)   # +n is west


@register
def road_held_low_below_the_dam_is_not_left_out_of_the_toe_band():
    """Review (DWR-2791): the reservoir's water decides the side, but pool_mask takes the dry
    closed low between the dam and a road below it as the pool; left out of the toe band, the
    inflow went on the dam's face (0.38 H). terrain.pool_downstream_off drops those cells."""
    r = _pick(ROAD_LOW)
    assert r["east"] and r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
    assert r["drop_h"] >= terrain.INFLOW_TOE_DROP, r
    assert 5.0 <= r["xy"][0] <= 40.0 and abs(r["xy"][1]) < 10.0, r    # the toe, in the low
    wc = r["info"]["water_check"]
    assert wc["ok"] and not wc["on_water"], wc
    # the old rule took the low as the pool (west): the water's side, with the warning
    assert wc["side_conflict"]["old"]["side_how"] == terrain.SIDE_POOL, wc
    assert r["warnings"] == [wc["side_conflict"]["warning"]], r["warnings"]
    real = terrain.pool_downstream_off                 # the low still in the pool: on the face
    terrain.pool_downstream_off = lambda pool, _line, _d: pool
    try:
        old = _pick(ROAD_LOW)
    finally:
        terrain.pool_downstream_off = real
    assert old["east"] and old["drop_h"] < terrain.INFLOW_TOE_DROP, old
    # the mask itself: nothing left on the downstream side, the upstream side untouched
    pool = terrain.pool_downstream_off(
        sd.grid_of(lambda X, Y: np.ones_like(X), (-50.0, -50.0, 50.0, 50.0), 10.0), LINE, EAST)
    xs = pool.x_centers()
    assert not pool.data[:, xs > 0].any() and pool.data[:, xs < 0].all(), pool.data
    assert terrain.pool_downstream_off(None, LINE, EAST) is None


@register
def dam_line_drawn_either_way_gives_the_same_side():
    for fn in (BUG_BERM, BUG_PIT, DRY):
        a = _pick(fn, line=LINE)
        b = _pick(fn, line=LINE[::-1])
        assert a["east"] and b["east"], (fn.__name__, a, b)
        assert np.hypot(a["xy"][0] - b["xy"][0], a["xy"][1] - b["xy"][1]) < 2.0, (a, b)


@register
def crest_line_of_a_batch_uses_the_water_too():
    r = _pick(BUG_BERM, dam_line=False)         # CREST_LINE: dam.crest_line_xy is None
    assert r["east"] and not _on_reservoir(r), r
    assert r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]


@register
def water_check_turns_the_side_round_when_the_inflow_is_on_the_reservoir():
    # the reservoir 0.9 H below the top is past the water window (0.75 H): the old rule takes
    # it (the lower side) and the toe pick lands on it; the check moves it below the dam
    old = _pick(DEEP_POND, water=False)
    assert not old["east"] and _on_reservoir(old), old
    r = _pick(DEEP_POND)
    assert r["east"] and not _on_reservoir(r), r
    assert r["info"]["side_how"] == terrain.SIDE_FLIPPED, r["info"]
    wc = r["info"]["water_check"]
    assert wc["flipped"] and wc["below_top_h"] == 0.9 and wc["first_xy"][0] < 0, wc
    assert "turned round by the inflow water check" in r["why"], r["why"]
    assert any(w.startswith("Breach inflow: moved to the other side of the dam line")
               for w in r["warnings"]), r["warnings"]
    assert not any(terrain.INFLOW_SIDE in w for w in r["warnings"]), r["warnings"]


@register
def water_at_one_level_both_sides_warns_check_the_inflow_side():
    g = sd.grid_of(CAUSEWAY, (-120.0, -180.0, 120.0, 180.0), 2.0)
    up, _b, why = terrain.water_side(g, LINE, terrain.chord_normal(LINE), sd.H, sd.TOP)
    assert up == 0 and "one level on both sides" in why, why
    r = _pick(CAUSEWAY)
    assert r["drop_h"] < 0.5, r                  # on the water, whichever side
    assert r["warnings"] and r["warnings"][0].startswith(terrain.INFLOW_SIDE), r["warnings"]
    wc = r["info"]["water_check"]
    assert not wc["ok"] and wc["warning"] == r["warnings"][0], wc
    assert "on flat water too" in wc["other_side"], wc
    assert r["info"]["side_how"] != terrain.SIDE_FLIPPED, r["info"]


@register
def downstream_direction_without_the_water_grid_is_unchanged():
    pool = sd.grid_of(BUG_BERM, (-1000.0, -1000.0, 1000.0, 1000.0), 10.0)
    info = {}
    d, how = terrain.downstream_direction(pool, (0.0, 0.0), sd.H, crest_line=LINE,
                                          top_m=sd.TOP, info=info)
    assert d[0] < -0.9 and how == "crest-line normal on the lower side (no pool against the dam)"
    assert info["side_how"] == terrain.SIDE_LOWER, info
    g = sd.grid_of(BUG_BERM, (-80.0, -180.0, 80.0, 180.0), 2.0)
    info = {}
    d, how = terrain.downstream_direction(pool, (0.0, 0.0), sd.H, crest_line=LINE,
                                          top_m=sd.TOP, water_grid=g, info=info)
    assert d[0] > 0.9 and info["side_how"] == terrain.SIDE_WATER, (d, how, info)
    # no top of dam: no water test
    d, _how = terrain.downstream_direction(pool, (0.0, 0.0), sd.H, crest_line=LINE,
                                           water_grid=g)
    assert d[0] < -0.9, d


@register
def reservoir_carve_side_uses_the_water():
    from nd_breach_runner.core import reservoir_carve as rc
    src = sd.DamTerrain(BUG_BERM)

    class Dam(object):
        crest_line_xy = LINE
    d, how = rc.downstream_for(src, Dam(), (0.0, 0.0), sd.H, sd.TOP, None, 10.0)
    assert d[0] > 0.9 and "reservoir's flat water" in how, (d, how)


@register
def run_report_records_how_the_side_was_decided():
    """run_dam with the dam line given (DAM_LINES): run_report.json inflow_point gets side_how,
    side_water and water_check; the inflow is below the dam."""
    import json
    import os
    import shutil
    import tempfile
    from nd_breach_runner.core import workflow
    from nd_breach_runner.core.breach import DamData
    from nd_breach_runner.tests.synthetic import npy_writer
    from nd_breach_runner.tests.test_v320 import fake_models
    dam = DamData(dam_id="T1", dam_height_ft=sd.H_FT, storage_top_acft=300.0, top_of_dam_ft=None,
                  surface_area_top_ac=None)
    dam.crest_line_xy = list(LINE)
    st = workflow.RunSettings(cell_size_m=20.0, planning_cell_m=90.0, initial_length_mi=1.0,
                              half_width_mi=0.5, expand_step_mi=1.0, max_iterations=1,
                              sim_time_hr=4.0, products=False, qa_maps="off")
    d = tempfile.mkdtemp(prefix="ndbr_side_")
    try:
        with fake_models():
            res = workflow.run_dam(dam, (0.0, 0.0), st, sd.DamTerrain(BUG_BERM), npy_writer,
                                   os.path.join(d, "T1"))
        with open(res.report_json) as f:
            ip = json.load(f)["inflow_point"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert ip["xy"][0] > 5.0, ip
    assert ip["side_how"] == terrain.SIDE_WATER, ip
    assert ip["side_water"]["below_top_h"] == 0.45, ip
    assert ip["water_check"]["ok"] and not ip["water_check"]["on_water"], ip
    sc = ip["water_check"]["side_conflict"]        # lead: the override recorded, both readings
    assert sc["kind"] == terrain.CONFLICT_WATER_KEPT and sc["old"]["side_how"] == "lower_side"
    assert sc["warning"].startswith(terrain.INFLOW_SIDE) and "0.45 H below" in sc["warning"], sc
    assert "away from the reservoir's flat water" in ip["how"], ip


# ---- review (2026-11-17): the gaps the review found, and the dry reservoir -------------------
def _no_dry_basin(r):
    """The pick is not in the dry reservoir (west of the line) and the side is the old one."""
    assert r["east"] and r["xy"][0] > 0.0, r
    assert r["info"]["side_how"] == terrain.SIDE_POOL, r["info"]


@register
def dry_reservoir_with_a_pond_below_keeps_the_old_side_and_warns():
    """Review: a dry reservoir (a closed basin up to the crest) and a pond 0.6 H below the top
    against the line below the dam. The water test alone takes the pond as the reservoir; the
    dry basin (30 ac, 0.87 H deep, filling above the pond) outweighs it: the old rule's side,
    CHECK THE INFLOW SIDE first, both readings in water_check.side_conflict, and the dry basin
    is not cleared from the pool (pool_downstream_off)."""
    g = sd.grid_of(DRY_POND_06, (-120.0, -180.0, 120.0, 180.0), 2.0)
    up, _b, _why = terrain.water_side(g, LINE, terrain.chord_normal(LINE), sd.H, sd.TOP)
    assert up == -1, up                          # the pond alone: east "up"
    old = _pick(DRY_POND_06, water=False)
    r = _pick(DRY_POND_06)
    _no_dry_basin(r)
    assert np.hypot(r["xy"][0] - old["xy"][0], r["xy"][1] - old["xy"][1]) < 2.0, (r, old)
    assert r["warnings"] and all(w.startswith(terrain.INFLOW_SIDE) for w in r["warnings"]), r
    sc = r["info"]["water_check"]["side_conflict"]
    assert sc["kept"]["side_how"] == terrain.SIDE_POOL and sc["kept"]["direction"][0] > 0.9, sc
    assert sc["water"]["side_how"] == terrain.SIDE_WATER and sc["water"]["direction"][0] < -0.9
    assert sc["water"]["below_top_h"] == 0.6 and sc["dry_basin"]["depth_h"] >= 0.5, sc
    assert sc["dry_basin"]["area_ac"] > 10.0 > sc["dry_basin"]["water_area_ac"], sc
    assert sc["warning"] in r["warnings"], sc
    # the given-inflow carve's side: the same rule
    from nd_breach_runner.core import reservoir_carve as rc

    class Dam(object):
        crest_line_xy = LINE
    info = {}
    d, how = rc.downstream_for(sd.DamTerrain(DRY_POND_06), Dam(), (0.0, 0.0), sd.H, sd.TOP,
                               None, 10.0, info=info)
    assert d[0] > 0.9 and how == "crest-line normal away from the pool", (d, how)
    assert info["side_conflict"]["dry_basin"]["area_ac"] > 10.0, info


@register
def run_hook_warns_when_the_carve_side_is_in_conflict():
    from nd_breach_runner.core import reservoir_carve as rc

    class Dam(object):
        crest_line_xy = LINE
        dam_height_ft = sd.H_FT
        top_of_dam_ft = sd.TOP / 0.3048

    class St(object):
        reservoir_carve = rc.MODE_ON
        cell_size_m = 10.0
    real_curve = rc.Curve

    def stop(_dam):                               # the side is found; stop the carve there
        raise RuntimeError("stop after the side")
    rc.Curve = stop
    warnings, report = [], {}
    try:
        rc.run_hook(St(), Dam(), (0.0, 0.0), None, sd.DamTerrain(DRY_POND_06), None,
                    "/nonexistent", report, warnings)
    finally:
        rc.Curve = real_curve
    assert warnings[0].startswith(terrain.INFLOW_SIDE + ": reservoir carve"), warnings
    assert "stop after the side" in report[rc.REPORT_KEY]["why"], report


@register
def dry_reservoir_with_a_deeper_pond_below_is_not_flipped_into_the_basin():
    """Review: the pond (0.9 H below the top, past the water window) at the toe of a dry
    reservoir: the old rule (the pool) is right and the toe pick is on the pond; the water
    check must not move the pick into the dry basin that decided the side: a warning."""
    for fn in (DRY_POND_09, DRY_TAILWATER):
        r = _pick(fn)
        _no_dry_basin(r)
        wc = r["info"]["water_check"]
        assert wc["on_water"] and not wc["ok"] and not wc.get("flipped"), (fn.__name__, wc)
        assert "decided the side" in wc["other_side"], wc
        assert r["warnings"][0].startswith(terrain.INFLOW_SIDE), r["warnings"]
        old = _pick(fn, water=False)
        assert old["east"] and abs(old["xy"][0] - r["xy"][0]) < 2.0, (old, r)


@register
def flip_never_goes_into_a_basin_filling_above_the_water():
    """Review: the deciding pool aside, a flip pick in a closed depression whose fill reaches the
    first pick's water is not used (workflow._flip_blocked)."""
    from nd_breach_runner.core import workflow
    pg = sd.grid_of(DRY, (-1000.0, -1000.0, 1000.0, 1000.0), 10.0)
    lvl = sd.TOP - 0.9 * sd.H
    why = workflow._flip_blocked(pg, None, terrain.SIDE_LOWER, LINE, (0.0, 0.0), (-30.0, 0.0),
                                 lvl)
    assert why and "closed depression" in why, why
    assert workflow._flip_blocked(pg, None, terrain.SIDE_LOWER, LINE, (0.0, 0.0), (-30.0, 0.0),
                                  sd.TOP + 1.0) is None          # fills below that water: used
    assert workflow._flip_blocked(pg, None, terrain.SIDE_LOWER, LINE, (0.0, 0.0), (40.0, 0.0),
                                  lvl) is None                  # below the dam: drains


@register
def drawn_down_reservoir_with_a_higher_pond_below_keeps_the_old_side():
    """Review: the reservoir 0.85 H below the top (past the window) and a small pond 0.6 H
    below it against the line below the dam: the pond is higher but much smaller than the lower
    water on the other side (WATER_SIDE_AREA_RATIO): no answer from the water, the old rule's
    side, CHECK THE INFLOW SIDE."""
    g = sd.grid_of(DRAWN_DOWN, (-120.0, -180.0, 120.0, 180.0), 2.0)
    detail = {}
    up, _b, why = terrain.water_side(g, LINE, terrain.chord_normal(LINE), sd.H, sd.TOP,
                                     detail=detail)
    assert up == 0 and "much smaller" in why and detail["both"]["up"] == -1, (why, detail)
    old = _pick(DRAWN_DOWN, water=False)
    r = _pick(DRAWN_DOWN)
    assert r["east"] and old["east"] and r["info"]["side_how"] == old["info"]["side_how"], r
    assert r["warnings"][0].startswith(terrain.INFLOW_SIDE), r["warnings"]
    sc = r["info"]["water_check"]["side_conflict"]
    assert sc["water"]["both"]["lower"]["below_top_h"] == 0.85, sc
    # after the run's earlier warnings, still first
    r = _pick(DRAWN_DOWN, earlier=["an earlier warning"])
    assert r["warnings"][0] == sc["warning"] and r["warnings"][1] == "an earlier warning", r
    # the old rule takes the reservoir (a berm below the dam): the pick on its water is not
    # kept for the small higher pond on the other side; the check moves it below the dam
    r = _pick(DRAWN_DOWN_BERM)
    wc = r["info"]["water_check"]
    assert r["east"] and wc["flipped"] and "kept" not in wc and wc["first_xy"][0] < 0, wc
    assert r["info"]["side_how"] == terrain.SIDE_FLIPPED, r["info"]


@register
def reservoir_water_against_a_bigger_road_held_low_still_decides():
    """Review guard (DWR-7449: 6.4 ac of reservoir water, a 17.5 ac road-held low below filling
    above it, S = 15 ac): the dry basin outweighs the water only when the water is pond-sized
    next to it and to the dam's surface area."""
    basin = {"area_ac": 17.5, "depth_h": 1.21, "spill_m": sd.TOP - 0.18 * sd.H}
    assert terrain.dry_basin_is_reservoir(basin, sd.H, sd.TOP - 0.6 * sd.H, 15.0)
    assert not terrain.pond_sized(6.4, basin, 15.0)
    assert not terrain.pond_sized(4.0, basin, 15.0)              # >= 0.25 S
    assert terrain.pond_sized(3.0, basin, 15.0) and terrain.pond_sized(0.6, basin, None)
    # a borrow pit or a shallow low: not a reservoir, nor one filling below the water
    assert not terrain.dry_basin_is_reservoir(dict(basin, area_ac=0.2), sd.H, sd.TOP - sd.H)
    assert not terrain.dry_basin_is_reservoir(dict(basin, depth_h=0.3), sd.H, sd.TOP - sd.H)
    assert not terrain.dry_basin_is_reservoir(basin, sd.H, basin["spill_m"] - 0.2)
    assert not terrain.dry_basin_is_reservoir(dict(basin, area_ac=3.0), sd.H, sd.TOP - sd.H,
                                              surface_ac=15.0)   # < 0.25 S


@register
def flip_takes_the_pool_off_the_side_it_moves_to():
    """Review gap: on a flip the pool mask's cells on the side now taken as downstream are left
    out (pool_downstream_off(pool, line, flip)), the other side's kept. Here the road-held low
    below the dam is that pool and it decided the side (west, onto the reservoir 0.9 H below
    the top), so the flip into it is refused: CHECK THE INFLOW SIDE."""
    from nd_breach_runner.core import workflow
    calls = []
    real = workflow._toe_inflow

    def spy(terrain_src, toe_line, direction, h_m, top_m, pool, *a, **k):
        calls.append((tuple(direction), pool))
        return real(terrain_src, toe_line, direction, h_m, top_m, pool, *a, **k)
    workflow._toe_inflow = spy
    try:
        r = _pick(DEEP_ROAD)
    finally:
        workflow._toe_inflow = real
    assert len(calls) == 2 and calls[0][0][0] < -0.9 and calls[1][0][0] > 0.9, calls
    first, flip = calls[0][1], calls[1][1]
    xs = first.x_centers()
    east = (xs > 20.0) & (xs < 120.0)
    assert first.data[:, east].sum() > 10, "the road-held low is in the pool"
    assert not flip.data[:, xs > 0].any(), "the flip side's pool left out"
    assert (flip.data[:, xs < 0] == first.data[:, xs < 0]).all(), "the other side's kept"
    assert r["info"]["side_how"] == terrain.SIDE_POOL and not r["east"], r["info"]
    wc = r["info"]["water_check"]
    assert not wc.get("flipped") and "decided the side" in wc["other_side"], wc
    assert r["warnings"][0].startswith(terrain.INFLOW_SIDE), r["warnings"]


@register
def found_line_leaves_the_pool_below_the_dam_out():
    """Review gap: a dam line the dam point check found decides the side (SIDE_FOUND_LINE): the
    road-held low below the dam is not the reservoir, so the toe is found in it, not on the
    dam's face."""
    from nd_breach_runner.core import dam_point_check as dpc
    r = _pick(ROAD_LOW, _crest_line_source=dpc.FOUND_LINE_SOURCE, _dpc_ds_dir=EAST)
    assert r["info"]["side_how"] == terrain.SIDE_FOUND_LINE and r["east"], r["info"]
    assert r["drop_h"] >= terrain.INFLOW_TOE_DROP and 5.0 <= r["xy"][0] <= 40.0, r
    assert not r["warnings"], r["warnings"]


@register
def higher_flat_water_wins_when_both_sides_hold_some():
    g = sd.grid_of(TWO_LEVELS, (-120.0, -180.0, 120.0, 180.0), 2.0)
    n = terrain.chord_normal(LINE)
    lab, bodies = terrain.flat_water(g)
    near = terrain._touching_water(g, LINE, n, lab, bodies)
    assert {b["plus"] > 0 for b in near} == {True, False}, near     # water on both sides
    up, b, _why = terrain.water_side(g, LINE, n, sd.H, sd.TOP)
    assert up == 1 and abs(b["level"] - (sd.TOP - 0.3 * sd.H)) < 0.01, (up, b)
    r = _pick(TWO_LEVELS)
    assert r["east"] and r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
    assert "higher flat water" in r["info"]["water_check"].get("kept", ""), r["info"]
    assert not r["warnings"], r["warnings"]


@register
def toe_within_the_tie_of_the_water_is_not_flipped_but_warned():
    """Review gap: the other side's toe is within WATER_LEVEL_TIE_M of the first pick's water:
    the dam may not hold it up, so no flip: CHECK THE INFLOW SIDE."""
    r = _pick(WITHIN_TIE)
    wc = r["info"]["water_check"]
    assert wc["on_water"] and not wc["ok"] and not wc.get("flipped"), wc
    assert "within" in wc["other_side"] and "of that water" in wc["other_side"], wc
    assert r["info"]["side_how"] == terrain.SIDE_LOWER, r["info"]
    assert r["warnings"] == [wc["warning"]] and wc["warning"].startswith(terrain.INFLOW_SIDE)


@register
def check_the_inflow_side_stays_first_among_the_warnings():
    r = _pick(CAUSEWAY, ds_slope=None, earlier=["an earlier warning"])  # the slope warning too
    assert len(r["warnings"]) >= 3, r["warnings"]
    assert r["warnings"][0].startswith(terrain.INFLOW_SIDE), r["warnings"]
    assert any("downstream slope is not known" in w for w in r["warnings"][1:]), r["warnings"]


def _plain(level, disc):
    """A sloping plain (no dam) with a flat disc (x, y, r) at `level` m: the guards' grids."""
    def f(X, Y):
        g = sd.TOP - 1.2 * sd.H - 0.01 * X + sd.texture(X, Y)
        x, y, rr = disc
        return np.where(np.hypot(X - x, Y - y) < rr, level, g)
    return sd.grid_of(f, (-160.0, -200.0, 200.0, 200.0), 2.0)


@register
def water_side_guards():
    n = terrain.chord_normal(LINE)
    # a flat more than WATER_LEVEL_TIE_M above the top of dam is not the reservoir
    g = _plain(level=sd.TOP + 0.6, disc=(-50.0, 0.0, 45.0))
    up, _b, why = terrain.water_side(g, LINE, n, sd.H, sd.TOP)
    assert up == 0 and why.startswith("no flat water"), why
    g = _plain(level=sd.TOP + 0.2, disc=(-50.0, 0.0, 45.0))
    assert terrain.water_side(g, LINE, n, sd.H, sd.TOP)[0] == 1
    # water evenly across the line: no side (WATER_SIDE_SHARE)
    g = _plain(level=sd.TOP - 0.3 * sd.H, disc=(0.0, 0.0, 45.0))
    up, _b, why = terrain.water_side(g, LINE, n, sd.H, sd.TOP)
    assert up == 0 and "does not touch one side" in why, why
    # water off the line (beyond POOL_TOUCH_M) does not count
    g = _plain(level=sd.TOP - 0.3 * sd.H, disc=(130.0, 0.0, 40.0))
    up, _b, why = terrain.water_side(g, LINE, n, sd.H, sd.TOP)
    assert up == 0 and "does not touch one side" in why, why


@register
def inflow_water_hit_guards():
    # a pond at the toe that does not reach the line: not the reservoir
    g = _plain(level=sd.TOP - 0.6 * sd.H, disc=(130.0, 0.0, 40.0))
    assert terrain.inflow_water_hit(g, LINE, EAST, sd.H, sd.TOP, (130.0, 0.0)) is None
    # the same pond reaching the line: a hit
    g = _plain(level=sd.TOP - 0.6 * sd.H, disc=(70.0, 0.0, 40.0))
    hit = terrain.inflow_water_hit(g, LINE, EAST, sd.H, sd.TOP, (70.0, 0.0))
    assert hit and hit["below_top_h"] == 0.6 and hit["other_level_m"] is None, hit
    # above the top of dam (by more than the tie) or deeper than INFLOW_WATER_DROP x H: none
    g = _plain(level=sd.TOP + 0.6, disc=(70.0, 0.0, 40.0))
    assert terrain.inflow_water_hit(g, LINE, EAST, sd.H, sd.TOP, (70.0, 0.0)) is None
    g = _plain(level=sd.TOP - 1.6 * sd.H, disc=(70.0, 0.0, 40.0))
    assert terrain.inflow_water_hit(g, LINE, EAST, sd.H, sd.TOP, (70.0, 0.0)) is None
    # on the line's other side from the inflow's downstream: not on its side
    assert terrain.inflow_water_hit(_plain(level=sd.TOP - 0.6 * sd.H, disc=(70.0, 0.0, 40.0)),
                                    LINE, (-1.0, 0.0), sd.H, sd.TOP, (70.0, 0.0)) is None


# ---- lead (2026-11-17): every override of the old rule is visible -------------------------
# the layouts the last check found (verify c1 .. c7): the water test takes a pond or a drawn-down
# reservoir's water as the reservoir; whatever side is chosen, the run must say CHECK THE
# INFLOW SIDE
F1_DRAWN_COULEE_BERM_POND = _fn(pool_drop_h=0.85, floor_drop_h=1.5, wall=0.2, berm_drop_h=0.4,
                                ds_pond=(0.6, 40.0, 28.0))      # drawn down, narrow coulee
F2_DRY_COULEE_POND = _fn(pool_drop_h=None, wall=0.4, ds_pond=(0.6, 50.0, 30.0))
F2_DRY_COULEE_BERM_POND = _fn(pool_drop_h=None, wall=0.4, berm_drop_h=0.4,
                              ds_pond=(0.6, 45.0, 33.0))
F3_DRY_SHALLOW_BASIN = _fn(pool_drop_h=None, floor_drop_h=0.4, ds_pond=(0.6, 50.0, 30.0))
FULL = _fn(pool_drop_h=0.3)                                     # a full reservoir, nothing below


def _warned(r, name):
    """A CHECK THE INFLOW SIDE warning, first, and recorded in water_check."""
    ws = r["warnings"]
    assert ws and ws[0].startswith(terrain.INFLOW_SIDE), (name, ws)
    wc = r["info"].get("water_check") or {}
    rec = (wc.get("side_conflict") or {}).get("warning") or wc.get("warning")
    assert rec in ws, (name, wc, ws)


@register
def new_failure_layouts_warn_check_the_inflow_side():
    """F1 (a drawn-down reservoir in a narrow coulee, a pond on a downstream berm), F2 (a dry
    reservoir in a narrow coulee, a pond below, with and without a berm) and F3 (a dry basin
    shallower than the inventory H, a pond below): the water test may take the pond as the
    reservoir. Whatever side the run takes, it warns CHECK THE INFLOW SIDE, first, also after
    earlier warnings."""
    for name, fn in (("F1", F1_DRAWN_COULEE_BERM_POND), ("F2", F2_DRY_COULEE_POND),
                     ("F2 berm", F2_DRY_COULEE_BERM_POND), ("F3", F3_DRY_SHALLOW_BASIN)):
        old = _pick(fn, water=False)
        assert not any(terrain.INFLOW_SIDE in w for w in old["warnings"]), (name, old)
        r = _pick(fn)
        _warned(r, name)
        if r["info"]["side_how"] == terrain.SIDE_WATER and r["east"] != old["east"]:
            sc = r["info"]["water_check"]["side_conflict"]
            assert sc["kind"] == terrain.CONFLICT_WATER_KEPT, (name, sc)
            assert sc["old"]["side_how"] == old["info"]["side_how"], (name, sc, old["info"])
            assert "pointed the other way" in sc["warning"], sc
        r = _pick(fn, earlier=["an earlier warning"])
        _warned(r, name)
        assert "an earlier warning" in r["warnings"][1:], (name, r["warnings"])


@register
def full_reservoir_with_a_borrow_pit_keeps_the_water_side_and_warns():
    """The 17 real dams' layout (a full hydro-flattened reservoir, a borrow pit below the dam
    that the old pool test took as the reservoir): the water's side is kept, and the run now
    names both readings: CHECK THE INFLOW SIDE, the flat water's level and depth below the top,
    and the older rule in plain words."""
    old = _pick(BUG_PIT, water=False)
    r = _pick(BUG_PIT)
    assert not old["east"] and r["east"] and not _on_reservoir(r), (old, r)
    assert r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
    sc = r["info"]["water_check"]["side_conflict"]
    assert sc["kind"] == terrain.CONFLICT_WATER_KEPT, sc
    assert sc["kept"]["side_how"] == terrain.SIDE_WATER and sc["kept"]["direction"][0] > 0.9, sc
    assert sc["old"]["side_how"] == terrain.SIDE_POOL and sc["old"]["direction"][0] < -0.9, sc
    assert sc["water"]["below_top_h"] == 0.3, sc
    w = r["warnings"][0]
    assert w == sc["warning"] and w.startswith(terrain.INFLOW_SIDE + ": the reservoir's flat "
                                                "water ("), w
    assert "0.30 H below the top of dam" in w and "pointed the other way" in w, w
    assert "a closed pool against the dam line taken as the reservoir" in w, w
    assert "qa_inflow_map.png" in w, w
    # the side itself is unchanged by the warning (the toe below the dam)
    assert r["drop_h"] >= terrain.INFLOW_TOE_DROP, r
    # downstream_direction alone records it too
    pool = sd.grid_of(BUG_PIT, (-1000.0, -1000.0, 1000.0, 1000.0), 10.0)
    g = sd.grid_of(BUG_PIT, (-80.0, -180.0, 80.0, 180.0), 2.0)
    info = {}
    d, _how = terrain.downstream_direction(pool, (0.0, 0.0), sd.H, crest_line=LINE,
                                           top_m=sd.TOP, water_grid=g, info=info)
    assert d[0] > 0.9 and info["side_conflict"]["kind"] == terrain.CONFLICT_WATER_KEPT, info


@register
def rules_that_agree_add_no_warning():
    """A full reservoir (alone, with a lower pond below, with a pond at two levels): the old
    rules and the water agree: no side_conflict, no CHECK THE INFLOW SIDE."""
    for fn in (FULL, FULL_DS_POND, TWO_LEVELS):
        old = _pick(fn, water=False)
        r = _pick(fn)
        assert old["east"] and r["east"], (fn.__name__, old, r)
        assert r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
        assert "side_conflict" not in r["info"]["water_check"], r["info"]
        assert not any(terrain.INFLOW_SIDE in w for w in r["warnings"]), r["warnings"]
    pool = sd.grid_of(FULL, (-1000.0, -1000.0, 1000.0, 1000.0), 10.0)
    g = sd.grid_of(FULL, (-80.0, -180.0, 80.0, 180.0), 2.0)
    info = {}
    d, _how = terrain.downstream_direction(pool, (0.0, 0.0), sd.H, crest_line=LINE,
                                           top_m=sd.TOP, water_grid=g, info=info)
    assert d[0] > 0.9 and info["side_how"] == terrain.SIDE_WATER, info
    assert "side_conflict" not in info, info



@register
def old_rules_that_fail_leave_the_water_side_flagged():
    """Final review: if the pool / ground rules raise, the water's side cannot be compared with
    anything; the run keeps it but warns CHECK THE INFLOW SIDE first (never silently)."""
    def boom(*_a, **_k):
        raise ValueError("pool_mask failed")
    real = terrain.pool_mask
    terrain.pool_mask = boom
    try:
        r = _pick(BUG_BERM, earlier=("an earlier warning",))
    finally:
        terrain.pool_mask = real
    assert r["east"] and r["info"]["side_how"] == terrain.SIDE_WATER, r["info"]
    sc = r["info"]["water_check"]["side_conflict"]
    assert sc["kind"] == terrain.CONFLICT_NO_OTHER_RULE and "could not be run" in sc["why"], sc
    assert r["warnings"][0].startswith(terrain.INFLOW_SIDE), r["warnings"]
    assert "could not be run" in r["warnings"][0], r["warnings"][0]


def _carve_side(fn):
    """reservoir_carve.run_hook up to the side (the carve stopped there): (warnings, report)."""
    from nd_breach_runner.core import reservoir_carve as rc

    class Dam(object):
        crest_line_xy = LINE
        dam_height_ft = sd.H_FT
        top_of_dam_ft = sd.TOP / 0.3048

    class St(object):
        reservoir_carve = rc.MODE_ON
        cell_size_m = 10.0
    real_curve = rc.Curve

    def stop(_dam):
        raise RuntimeError("stop after the side")
    rc.Curve = stop
    warnings, report = [], {}
    try:
        rc.run_hook(St(), Dam(), (0.0, 0.0), None, sd.DamTerrain(fn), None, "/nonexistent",
                    report, warnings)
    finally:
        rc.Curve = real_curve
    return warnings, report


@register
def reservoir_carve_warns_when_the_water_overrides_the_old_side():
    """reservoir_carve.downstream_for / run_hook: the same rule as the inflow. The water's side
    against the old rules: kept, side_conflict in `info`, CHECK THE INFLOW SIDE first; rules
    that agree: no warning."""
    from nd_breach_runner.core import reservoir_carve as rc

    class Dam(object):
        crest_line_xy = LINE
    info = {}
    d, how = rc.downstream_for(sd.DamTerrain(BUG_PIT), Dam(), (0.0, 0.0), sd.H, sd.TOP, None,
                               10.0, info=info)
    assert d[0] > 0.9 and "reservoir's flat water" in how, (d, how)
    assert info["side_conflict"]["kind"] == terrain.CONFLICT_WATER_KEPT, info
    warnings, report = _carve_side(BUG_PIT)
    assert warnings and warnings[0].startswith(
        terrain.INFLOW_SIDE + ": reservoir carve: the reservoir's flat water ("), warnings
    assert "pointed the other way" in warnings[0], warnings
    warnings, _report = _carve_side(FULL)
    assert not any(terrain.INFLOW_SIDE in w for w in warnings), warnings
