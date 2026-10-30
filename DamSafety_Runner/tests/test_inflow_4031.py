"""v3.12.10: the DWR-4031 breach inflow (Washek, main cc9adc2, Runner 3.12.9), on a synthetic DEM
built like it (no DWR data in the repo):

  * the reservoir is hydro-flattened: ~0.35 m of fill behind the dam, less than the
    max(0.5 m, 15 % of H) pool_mask needs, so there was no pool at the dam;
  * a closed low 450 m ESE (the decoy) was the only "pool" within 600 m, so
    downstream_direction pointed away from it, WNW, into the reservoir;
  * on that side nothing was low enough within 90 m, and the 400 m search took the nearest low
    cell, a draw 393 m NNE of the dam;
  * inflow_side_check passed it: its checks lean on the same downstream vector.

The real numbers (run_report.json) are in the side-check tests."""
import math

import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M

# the DWR dam line relative to the dam point (DWR-4031: N-S, then a short arm east)
LINE = [(2.7, -27.7), (1.5, -3.6), (2.9, 12.8), (17.4, 15.0)]
FLIPPED = (-0.946, 0.325)          # the downstream side the 3.12.9 run used
DECOY = (426.0, -146.0)            # 450 m ESE: the closed low that flipped it
DRAW = (100.0, 380.0)              # the farmyard draw 393 m NNE


def _dem(X, Y, pond=True, decoy=True):
    z = np.full_like(X, TOP_M + 3.0)                             # upland
    valley = (X > 8.0) & (np.abs(Y) < 50.0)                      # downstream, drains east
    z = np.where(valley, TOP_M - H_M - 0.002 * X, z)
    emb = (np.abs(X - 2.0) <= 8.0) & (np.abs(Y) < 60.0)
    z = np.where(emb, TOP_M, z)
    toe = (X > 10.0) & (X < 25.0) & (np.abs(Y) < 60.0)           # downstream face
    z = np.where(toe, np.minimum(z, TOP_M - (X - 10.0) / 15.0 * H_M), z)
    if pond:
        # the hydro-flattened reservoir, 1 m below the top of dam, spilling north through a
        # notch 0.65 m below the top: ~0.35 m of fill
        z = np.where((X > -200.0) & (X < -6.0) & (np.abs(Y) < 60.0), TOP_M - 1.0, z)
        notch = (np.abs(X + 100.0) < 15.0) & (Y >= 55.0)
        z = np.where(notch, TOP_M - 0.65 - 0.001 * (Y - 60.0), z)
    if decoy:
        r = np.hypot(X - DECOY[0], Y - DECOY[1])
        z = np.where(r < 60.0, TOP_M - H_M - 2.0, z)
    # the draw: low enough (half the height down), drains east off the grid
    draw = (np.abs(Y - DRAW[1]) < 12.0) & (X > DRAW[0] - 10.0)
    z = np.where(draw, TOP_M - 0.5 * H_M - 0.5 - 0.001 * (X - DRAW[0]), z)
    return z


def _grid(half, cell=20.0, **kw):
    n = int(round(2 * half / cell))
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    return Grid(_dem(X, Y, **kw), -half, half, cell)


def _at(g, mask, xy):
    r, c = g.rc(*xy)
    return bool(mask[r, c])


@register
def pool_4031_flattened_reservoir_is_found_against_the_dam_line():
    g = _grid(terrain.POOL_HALF_M)
    old = terrain.pool_mask(g, H_M)
    new = terrain.pool_mask(g, H_M, (0.0, 0.0), TOP_M, LINE)
    assert not _at(g, old, (-100.0, 0.0)), "the depth rule alone sees the flattened pond"
    assert _at(g, old, DECOY) and _at(g, new, DECOY)           # deep depressions are kept
    assert _at(g, new, (-100.0, 0.0)) and _at(g, new, (-30.0, 0.0))
    assert not _at(g, new, (60.0, 0.0)) and not _at(g, new, DRAW)
    near = terrain.pool_at_dam(new, g, (0.0, 0.0), LINE)
    assert _at(g, near, (-100.0, 0.0)) and not _at(g, near, DECOY)


@register
def pool_4031_shallow_rule_needs_the_dam():
    # a shallow depression away from the dam line is not added (only the deep rule applies)
    g = _grid(terrain.POOL_HALF_M)
    far = terrain.pool_mask(g, H_M, (0.0, -700.0), TOP_M)
    assert not _at(g, far, (-100.0, 0.0))


@register
def downstream_4031_decoy_depression_does_not_flip_the_side():
    g = _grid(terrain.POOL_HALF_M)
    v, how = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=LINE, top_m=TOP_M)
    assert v is not None and v[0] > 0.9, (v, how)
    assert abs(v[0] - 0.946) < 0.01 and abs(v[1] + 0.325) < 0.01, v     # the line's normal
    assert "away from the pool" in how, how
    v2, how2 = terrain.downstream_direction(g, (0.0, 0.0), H_M, top_m=TOP_M)   # no dam line
    assert v2 is not None and v2[0] > 0.9, (v2, how2)
    # the decoy is not used even without top_m (no shallow pool): it does not reach the dam
    v3, how3 = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=LINE)
    assert v3 is not None and v3[0] > 0.9, (v3, how3)


@register
def downstream_4031_without_a_pool_takes_the_lower_side_of_the_line():
    g = _grid(terrain.POOL_HALF_M, pond=False)        # the reservoir side is upland
    v, how = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=LINE, top_m=TOP_M)
    assert v is not None and v[0] > 0.9 and "lower side" in how, (v, how)


@register
def inflow_4031_far_draw_is_not_picked_beyond_the_limit():
    g = _grid(430.0)
    # the 3.12.9 path: the flipped side; with no limit the draw 393 m away is the pick
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=FLIPPED,
                                        crest_line=LINE, max_from_dam_m=1.0e6)
    assert pt is not None and abs(pt[1] - DRAW[1]) < 15 and math.hypot(*pt) > 350, (pt, why)
    assert why.startswith("toe is outside the usual"), why
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=FLIPPED,
                                        crest_line=LINE)
    assert why.startswith(terrain.INFLOW_FAR), why
    assert pt is not None and terrain.nearest_line_distance(pt, [LINE]) <= \
        terrain.INFLOW_MAX_FROM_DAM_M, (pt, why)
    # the right side: the toe within the usual radius, below half the dam height
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=(0.946, -0.325),
                                        crest_line=LINE)
    assert pt is not None and 10 < pt[0] < 90 and why.startswith("lowest ground within"), \
        (pt, why)


@register
def inflow_4031_limit_is_configurable_and_from_the_dam_line():
    g = _grid(430.0)
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=FLIPPED,
                                        crest_line=LINE, max_from_dam_m=450.0)
    assert pt is not None and not why.startswith(terrain.INFLOW_FAR), (pt, why)
    pts = [(20.0, 0.0), (2.0, 100.0), (-50.0, -80.0)]
    d = terrain.dam_distance(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]),
                             (0.0, 0.0), LINE)
    for p, dd in zip(pts, d):
        assert abs(dd - terrain.nearest_line_distance(p, [LINE])) < 1e-6, (p, dd)
    assert abs(terrain.dam_distance(np.array([3.0]), np.array([4.0]), (0.0, 0.0))[0] - 5.0) < 1e-9


@register
def inflow_4031_workflow_helper_finds_the_toe():
    from nd_breach_runner.core import workflow

    class Src(object):
        def read(self, bounds, cell):
            return _grid((bounds[2] - bounds[0]) / 2.0, cell)

    class Dam(object):
        crest_line_xy = LINE
        dwr_ds_slope_h = 2.0              # DWR-4031's record: downstream_slope "2"
        dwr_ds_slope_note = "DWR record downstream_slope '2' = 2:1"
    rad = max(90.0, 8.0 * H_M)
    local = Src().read((-430, -430, 430, 430), 20.0)
    warnings, logs, info = [], [], {}
    pt, why = workflow._downstream_inflow(Src(), local, Dam(), (0.0, 0.0), H_M, TOP_M, rad, 20.0,
                                          warnings, logs.append, out=info,
                                          **workflow._toe_kwargs(Dam(), object()))
    assert "2:1" in info["toe_band_how"] and "DWR record" in info["toe_band_how"], info
    assert pt is not None and 10 < pt[0] < 90 and abs(pt[1]) < 80, (pt, why)
    assert info["direction"][0] > 0.9 and not warnings, (info, warnings)


# ---------------------------------------- the side check, on DWR-4031's run_report.json numbers
DAM = (-407005.971, 2822706.891)
DWR_LINE = [(-407003.295, 2822679.161), (-407004.483, 2822703.254), (-407003.043, 2822719.672),
            (-406988.593, 2822721.933)]
RUN1 = (-406905.97, 2823086.89)
TOE = (-406986.0, 2822699.9)                  # the explicit inflow that worked


def _path(start, d, n=20, step=20.0):
    return [(start[0] + d[0] * step * k, start[1] + d[1] * step * k) for k in range(n)]


@register
def side_check_4031_flags_the_trace_against_the_downstream_side():
    chk = terrain.inflow_side_check(DAM, RUN1, _path(RUN1, (0.949, 0.316)), FLIPPED)
    assert not chk["ok"], chk                               # 3.12.9 said ok
    assert abs(chk["trace_vs_downstream"] + 0.80) < 0.02, chk
    assert "far" in chk and "393 m from the dam point" in chk["far"], chk
    chk = terrain.inflow_side_check(DAM, RUN1, _path(RUN1, (0.949, 0.316)), FLIPPED,
                                    crest_line=DWR_LINE)
    assert not chk["ok"] and "dam line" in chk["far"], chk
    # without a downstream vector the dam line alone still catches it
    chk = terrain.inflow_side_check(DAM, RUN1, _path(RUN1, (0.949, 0.316)), None,
                                    crest_line=DWR_LINE)
    assert not chk["ok"] and chk["trace_vs_dam_line_normal"] < 0, chk


@register
def side_check_4031_passes_the_toe():
    chk = terrain.inflow_side_check(DAM, TOE, _path(TOE, (0.95, -0.31)), (0.946, -0.325),
                                    crest_line=DWR_LINE)
    assert chk["ok"] and "far" not in chk, chk
    assert chk["inflow_from_dam_line_m"] < 25.0, chk


@register
def site_check_4031_warns_on_a_far_inflow():
    from nd_breach_runner.core import workflow

    class St(object):
        road_lines = []

    class Dam(object):
        crest_line_xy = DWR_LINE
    rep, warns, logs = {"inflow_point": {"xy": list(RUN1)}}, ["other"], []
    workflow._check_inflow_site(rep, St(), Dam(), DAM, RUN1, _path(RUN1, (0.949, 0.316)),
                                FLIPPED, False, warns, logs.append)
    assert warns[0].startswith(terrain.INFLOW_FAR) and warns[1].startswith(terrain.SIDE_PROBLEM), \
        warns
    assert rep["inflow_point"]["side_check"]["far"], rep
