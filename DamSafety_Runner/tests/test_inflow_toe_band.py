"""v3.12.10 (Nick, DWR-4031): "can we make the inflow point at the dam toe at the lowest
elevation?" The breach inflow is the lowest cell of a band 5 m to H x (downstream slope + 1)
downstream of the dam line (v3.13.0, Nick: "would do 2"; 3:1, i.e. 4H, when the slope is unknown),
along the full line, on the fine (1 m) DEM (terrain.toe_band_inflow). find_inflow_point (with
the 150 m cap) is the fallback, with a warning, when there is no dam line, no downstream side
or no valid cell in the band. Synthetic DEMs only."""
import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M

LINE = [(0.0, -60.0), (0.0, 60.0)]      # N-S dam line through the dam point; downstream is east
EAST = (1.0, 0.0)
FLOOR = TOP_M - H_M                     # the valley floor at the toe


def _dem(X, Y, toe_low=(10.0, 40.0), far_low=(60.0, 0.0), upstream_low=True):
    """Embankment along x = 0 (crest 6 m wide), a 2H:1V downstream face to the floor at x ~ 18 m,
    a flat floor (falling 2 m past x = 45 m), a 1 m-deep toe
    channel at `toe_low`, a deeper pit farther out at `far_low` and, upstream, a reservoir
    floor lower than all of them."""
    z = np.full_like(X, FLOOR)
    face = (X > 3.0) & (X < 3.0 + 2.0 * H_M / 0.8)
    z = np.where(face, TOP_M - (X - 3.0) / 2.0 * 0.8 * 1.25, z)
    z = np.where(np.abs(X) <= 3.0, TOP_M, z)
    z = np.maximum(z, FLOOR)
    z = np.where(X > 45.0, FLOOR - 2.0, z)
    if toe_low is not None:
        z = np.where(np.hypot(X - toe_low[0], Y - toe_low[1]) < 2.0, FLOOR - 1.0, z)
    if far_low is not None:
        z = np.where(np.hypot(X - far_low[0], Y - far_low[1]) < 5.0, FLOOR - 4.0, z)
    if upstream_low:
        z = np.where(X < -3.0, FLOOR - 3.0, z)          # reservoir floor (dry), upstream
        z = np.where(X < -200.0, TOP_M + 2.0, z)        # ... closed by upland
    z = np.where(np.abs(Y) > 70.0, TOP_M + 2.0, z)      # abutments
    return z


def _grid(half=120.0, cell=1.0, **kw):
    n = int(round(2 * half / cell))
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    return Grid(_dem(X, Y, **kw), -half, half, cell)


@register
def toe_band_picks_the_toe_low_point_over_a_lower_cell_farther_out():
    g = _grid()
    pt, why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, dam_xy=(0.0, 0.0))
    assert pt is not None, why
    assert abs(pt[0] - 10.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0, (pt, why)   # the toe channel
    outer = 4.0 * H_M                                   # H x (3 + 1), the slope not known
    assert "lowest ground at the downstream toe" in why and "5-{:.0f} m".format(outer) in why, why
    # the old search takes the deeper pit 60 m out (or the drop past 45 m), not the toe
    old, _w = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=EAST)
    assert old is not None and old[0] > 40.0, old


@register
def toe_band_keeps_to_the_downstream_side():
    g = _grid()
    band, dl = terrain.toe_band_mask(g, LINE, EAST)
    X, _Y = np.meshgrid(g.x_centers(), g.y_centers())
    assert band.any() and (X[band] > 0).all(), "the band is on the downstream side only"
    assert dl[band].min() >= 5.0 and dl[band].max() <= 30.0      # the default band
    # downstream given the other way: the band is the reservoir side and the pick is there
    pt, _why = terrain.toe_band_inflow(g, LINE, (-1.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] < 0, pt
    # the reservoir floor (3 m lower than anything downstream) is never picked going east
    pt, _why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M)
    assert pt[0] > 0, pt


@register
def toe_band_runs_the_full_crest_length_and_not_round_the_ends():
    # the toe channel near the north end of the line (y = 55), not at the dam point
    g = _grid(toe_low=(10.0, 55.0))
    pt, _why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, dam_xy=(0.0, 0.0))
    assert abs(pt[1] - 55.0) <= 2.0, pt
    band, _dl = terrain.toe_band_mask(g, LINE, EAST)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    # off the ends of the line the band only fans out within 60 degrees of downstream
    end = band & (Y > 60.0)
    assert ((X[end]) >= 0.5 * np.hypot(X[end], Y[end] - 60.0) - 1e-6).all()


@register
def toe_band_flat_ties_go_to_the_line_then_the_dam_point():
    g = _grid(toe_low=None, far_low=None)                 # the face is even along the line
    pt, _why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, dam_xy=(0.0, 10.0))
    X = pt[0]
    # the face's lowest cells (the band's outer edge) tie along the line: nearest the dam point
    assert X < 3.0 + 2.0 * H_M / 0.8 + 2.0 and abs(pt[1] - 10.0) <= 1.0, pt


@register
def toe_band_widens_when_the_band_is_all_dam_face():
    tall = 4.0 * H_M          # a dam this tall: the 2H:1V face runs past 15 m
    g = _grid(toe_low=None, far_low=None, upstream_low=False)
    z = g.data.copy()
    X, _Y = np.meshgrid(g.x_centers(), g.y_centers())
    face = (X > 3.0) & (X < 3.0 + 2.0 * tall)
    z = np.where(face, TOP_M - (X - 3.0) / 2.0, z)
    z = np.where(X >= 3.0 + 2.0 * tall, TOP_M - tall, z)
    g2 = Grid(z, g.x0, g.y0, g.dx)
    # the face test is a safeguard past the outer limit (a given band, or a slope that is wrong)
    pt, why = terrain.toe_band_inflow(g2, LINE, EAST, tall, TOP_M, band_m=(5.0, 15.0))
    assert pt is not None and "band widened" in why, why
    assert TOP_M - z[g2.rc(*pt)] >= 0.5 * tall - 1e-6, (pt, why)
    # never past the cap
    pt, why = terrain.toe_band_inflow(g2, LINE, EAST, tall, TOP_M, band_m=(5.0, 15.0),
                                      max_from_dam_m=25.0)
    assert pt is not None and pt[0] <= 25.0 + 1.0 and "less than half" in why, (pt, why)


@register
def toe_band_says_why_it_cannot_be_used():
    g = _grid()
    for args, word in (((None, EAST), "no dam line"), ((LINE, None), "downstream side"),
                       (([(0.0, 0.0)], EAST), "no dam line")):
        pt, why = terrain.toe_band_inflow(g, args[0], args[1], H_M, TOP_M)
        assert pt is None and why.startswith(terrain.NO_TOE) and word in why, why
    high = Grid(np.full(g.data.shape, TOP_M + 1.0), g.x0, g.y0, g.dx)
    pt, why = terrain.toe_band_inflow(high, LINE, EAST, H_M, TOP_M)
    assert pt is None and why.startswith(terrain.NO_TOE), why


class _Src(object):
    """terrain_src: the synthetic DEM at any cell (nearest sample of a 1 m grid)."""
    def __init__(self, **kw):
        self.g = _grid(half=1000.0, cell=1.0, **kw)
        self.cells = []

    def native_cell_m(self):
        return 1.0

    def read(self, bounds, cell):
        self.cells.append(cell)
        n = max(1, int(round((bounds[2] - bounds[0]) / cell)))
        m = max(1, int(round((bounds[3] - bounds[1]) / cell)))
        xs = bounds[0] + (np.arange(n) + 0.5) * cell
        ys = bounds[3] - (np.arange(m) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        return Grid(_dem(X, Y), bounds[0], bounds[3], cell)


def _workflow_pick(line, crest_line=None, band=None):
    from nd_breach_runner.core import workflow

    class Dam(object):
        crest_line_xy = crest_line

    class St(object):
        midcrest_line = line if crest_line is None else None
        inflow_toe_band_m = band
        inflow_toe_cell_m = None
    src = _Src()
    rad = max(90.0, 8.0 * H_M)
    local = src.read((-430.0, -430.0, 430.0, 430.0), 20.0)
    warnings, info = [], {}
    pt, why = workflow._downstream_inflow(src, local, Dam(), (0.0, 0.0), H_M, TOP_M, rad, 20.0,
                                          warnings, lambda *_a: None, out=info,
                                          **workflow._toe_kwargs(Dam(), St()))
    return pt, why, warnings, info, src


@register
def workflow_places_the_inflow_at_the_toe_on_the_fine_dem():
    pt, why, warnings, info, src = _workflow_pick(LINE, crest_line=LINE)
    assert info["direction"][0] > 0.9, info
    assert abs(pt[0] - 10.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0, (pt, why)
    assert "downstream toe" in why and not warnings, (why, warnings)
    assert 1.0 in src.cells, src.cells                   # the band is read at 1 m
    # a batch's CREST_LINE (st.midcrest_line) is the dam line when DAM_LINES has none
    pt, why, warnings, _info, _src = _workflow_pick(LINE, crest_line=None)
    assert "downstream toe" in why and not warnings, (why, warnings)
    # the band is a setting
    pt, why, _w, _i, _s = _workflow_pick(LINE, crest_line=LINE, band=(5.0, 12.0))
    assert "5-12 m" in why and pt[0] <= 12.5, (pt, why)


@register
def workflow_without_a_dam_line_falls_back_with_a_warning():
    pt, why, warnings, info, _src = _workflow_pick(None, crest_line=None)
    assert pt is not None and "downstream toe" not in why, why
    assert any("not placed at the downstream toe" in w and "no dam line" in w for w in warnings), \
        warnings
    assert info.get("toe", "").startswith(terrain.NO_TOE), info


def _long_dam(pad=True, channel=True):
    """A DWR-2801-like dam: a 320 m line bent at (0, 0) (south arm to the SW, north arm N),
    downstream east, a 2.5H:1V face that fills the whole 5-15 m strip (it meets the floor ~22 m
    out), a 0.5 m-deep toe channel 20-26 m out near the bend and, 20 m from the south end, a
    flat pad on the face, lower than the face's lowest strip cell but not 75 % of H down."""
    line = [(-80.0, -150.0), (0.0, 0.0), (0.0, 150.0)]
    half, cell = 250.0, 1.0
    n = int(round(2 * half / cell))
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    dl, fx, fy, seg = terrain._line_foot(X, Y, line)
    nrm = terrain._segment_normals(line, EAST)
    side = (X - fx) * nrm[seg, 0] + (Y - fy) * nrm[seg, 1]
    floor = FLOOR - 0.01 * np.maximum(X, 0.0)             # falls away east (the trace runs on)
    z = np.maximum(TOP_M - np.maximum(dl - 3.0, 0.0) / 2.5, floor)
    z = np.where(side < -3.0, FLOOR - 3.0, z)            # the reservoir floor, upstream
    if channel:
        z = np.where((np.hypot(X - 23.0, Y - 10.0) < 2.5) & (side > 0), floor - 0.5, z)
    ax, ay = line[0]
    sx, sy = ax + 20.0 * 0.4706, ay + 20.0 * 0.8824       # 20 m along the south arm
    on_pad = (np.hypot(X - sx, Y - sy) < 14.0) & (side > 6.0) & (dl < 16.0)
    if pad:
        z = np.where(on_pad, TOP_M - 0.73 * H_M, z)
    z = np.where((np.abs(Y) > 160.0) | (X < -100.0), TOP_M + 2.0, z)   # abutments, upland
    return Grid(z, -half, half, cell), line, (sx, sy)


@register
def toe_band_long_line_skips_a_high_pad_near_one_end():
    # Grok Bot's review of DWR-2801 (pack dam line): the 5-15 m strip of a 27.5 ft dam is its
    # face; its lowest cell was a pad ~20 m from the south end (14.9 ft below the top, half H
    # passed), 140 m from mid-crest, and the side check from the mid-crest point failed
    g, line, pad = _long_dam()
    old = terrain.INFLOW_TOE_DROP
    try:                                  # the bug: with the old half-height test, the pad
        terrain.INFLOW_TOE_DROP = 0.5
        pt, why = terrain.toe_band_inflow(g, line, EAST, H_M, TOP_M, band_m=(5.0, 15.0),
                                          dam_xy=line[0])
        assert np.hypot(pt[0] - pad[0], pt[1] - pad[1]) < 15.0, (pt, why)
        path, _ = terrain.trace_downstream(g, pt, 200.0)
        chk = terrain.inflow_side_check((0.0, 10.0), pt, path, EAST, crest_line=line)
        assert not chk["ok"] and any("upstream side" in p for p in chk["problems"]), chk
    finally:
        terrain.INFLOW_TOE_DROP = old
    # v3.13.0 (Nick): the band out to H x (3 + 1) = 30.5 m reaches the toe channel as it is
    pt0, why0 = terrain.toe_band_inflow(g, line, EAST, H_M, TOP_M, dam_xy=line[0])
    assert np.hypot(pt0[0] - 23.0, pt0[1] - 10.0) <= 3.0, (pt0, why0)
    assert "widened" not in why0 and "5-30 m" in why0, why0
    # and the 5-15 m strip, all face, widens past the pad to it (the 75 % face test)
    pt, why = terrain.toe_band_inflow(g, line, EAST, H_M, TOP_M, band_m=(5.0, 15.0),
                                      dam_xy=line[0])
    assert np.hypot(pt[0] - 23.0, pt[1] - 10.0) <= 3.0, (pt, why)       # the toe channel
    assert "band widened from 15 m" in why and "dam's face" in why, why
    assert TOP_M - g.data[g.rc(*pt)] >= terrain.INFLOW_TOE_DROP * H_M, why
    # the run's side check, from the NID end point and from mid-crest (160 m along)
    path, _ = terrain.trace_downstream(g, pt, 200.0)
    for dam_xy in (line[0], (0.0, 10.0)):
        chk = terrain.inflow_side_check(dam_xy, pt, path, EAST, crest_line=line)
        assert chk["ok"], (dam_xy, chk)
    # without the pad the pick is the same (the pad never mattered once the strip is right)
    g2, _l, _p = _long_dam(pad=False)
    pt2, _w = terrain.toe_band_inflow(g2, line, EAST, H_M, TOP_M, dam_xy=line[0])
    assert np.hypot(pt2[0] - pt[0], pt2[1] - pt[1]) <= 1.5, (pt, pt2)


@register
def toe_band_uses_the_local_side_of_a_bent_line():
    # a north arm bent 45 degrees to the NE: past its end, ground within 60 degrees of the
    # overall downstream (east) can be behind the arm; the side is the arm's own normal
    line = [(0.0, -60.0), (0.0, 60.0), (50.0, 110.0)]
    g = _grid(half=160.0)
    band, dl = terrain.toe_band_mask(g, line, EAST)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    _d, fx, fy, seg = terrain._line_foot(X, Y, line)
    nrm = terrain._segment_normals(line, EAST)
    side = (X - fx) * nrm[seg, 0] + (Y - fy) * nrm[seg, 1]
    assert band.any() and (side[band] >= 0.5 * dl[band] - 1e-6).all()
    behind = (55.7, 118.2)                # 10 m from the end, 55 deg from east, behind the arm
    assert not band[g.rc(*behind)]
    assert band[g.rc(50.0 + 7.07, 110.0 - 7.07)]          # 10 m out, square off the arm
    # a line that runs with the flow has no downstream side
    pt, why = terrain.toe_band_inflow(g, [(-60.0, 0.0), (60.0, 1.0)], EAST, H_M, TOP_M)
    assert pt is None and why.startswith(terrain.NO_TOE) and "with the flow" in why, why


@register
def toe_band_leaves_out_the_pool():
    g = _grid()
    # the reservoir (5 m pool cells) wraps round onto the toe channel: it is not the toe
    data = np.zeros((48, 48))
    pool = Grid(data, -120.0, 120.0, 5.0)
    r, c = pool.rc(10.0, 40.0)
    data[r - 1:r + 2, c - 1:c + 2] = 1.0
    pt, why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, pool=pool, dam_xy=(0.0, 0.0))
    assert pt is not None and np.hypot(pt[0] - 10.0, pt[1] - 40.0) > 5.0, (pt, why)
    # a coarse (20 m) pool cell across the line, its centre upstream: the toe still counts
    coarse = Grid(np.zeros((12, 12)), -120.0, 120.0, 20.0)
    coarse.data[coarse.rc(0.0, 40.0)] = 1.0
    pt, why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, pool=coarse, dam_xy=(0.0, 0.0))
    assert abs(pt[0] - 10.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0, (pt, why)
    # all pool: no toe
    pt, why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M,
                                      pool=Grid(np.ones((48, 48)), -120.0, 120.0, 5.0))
    assert pt is None and why.startswith(terrain.NO_TOE) and "pool" in why, why


@register
def workflow_passes_the_pool_to_the_toe_strip():
    seen = {}
    real = terrain.toe_band_inflow

    def spy(*a, **kw):
        seen["pool"] = kw.get("pool")
        return real(*a, **kw)
    terrain.toe_band_inflow = spy
    try:
        pt, why, warnings, _info, _src = _workflow_pick(LINE, crest_line=LINE)
    finally:
        terrain.toe_band_inflow = real
    pool = seen.get("pool")
    assert pool is not None and (pool.data > 0.5).any(), "the pool reaches the toe strip"
    xs = pool.x_centers()
    assert (np.broadcast_to(xs, pool.data.shape)[pool.data > 0.5] < 0).mean() > 0.9
    assert abs(pt[0] - 10.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0 and not warnings, (pt, why)


@register
def toe_band_outer_limit_is_height_times_slope_plus_one():
    # Nick ("would do 2"): outer = H x (downstream slope + 1); 3:1 (4H) when the slope is unknown
    (lo, hi), how, known = terrain.toe_band_for_dam(6.1, None)      # DWR-4031, ~20 ft
    assert known and lo == 5.0 and abs(hi - 24.4) < 1e-9 and "not known, 3:1" in how, how
    (lo, hi), how, known = terrain.toe_band_for_dam(70 * 0.3048, 3.0)  # a 70 ft dam
    assert abs(hi - 85.344) < 1e-6, hi
    (lo, hi), how, known = terrain.toe_band_for_dam(70 * 0.3048, 4.0)
    assert abs(hi - 106.68) < 1e-6, hi
    (lo, hi), how, _k = terrain.toe_band_for_dam(8.382, 2.5)        # the dam's own slope
    assert abs(hi - 8.382 * 3.5) < 1e-9 and "2.5:1" in how, how
    # a low dam: never nearer than INFLOW_TOE_MIN_OUTER_M (15 m), always past the inner limit
    (lo, hi), how, _k = terrain.toe_band_for_dam(1.5, 2.0)
    assert hi == terrain.INFLOW_TOE_MIN_OUTER_M == 15.0 and "at least 15 m" in how, how
    (lo, hi), _how, _k = terrain.toe_band_for_dam(1.0, 1.0, min_outer_m=2.0)
    assert hi > lo, (lo, hi)
    # a tall dam: never past the 150 m cap (or the run's)
    (lo, hi), how, _k = terrain.toe_band_for_dam(60.0, 3.0)
    assert hi == 150.0 and "capped" in how, how
    assert terrain.toe_band_for_dam(60.0, 3.0, cap_m=100.0)[0][1] == 100.0
    # height not known: the default band, flagged so the run warns
    for h in (None, 0.0, float("nan"), "x"):
        (lo, hi), how, known = terrain.toe_band_for_dam(h, 3.0)
        assert not known and (lo, hi) == terrain.INFLOW_TOE_BAND_M == (5.0, 30.0), (h, lo, hi)
        assert "not known" in how


@register
def workflow_toe_band_follows_the_dams_downstream_slope():
    from nd_breach_runner.core import workflow

    class Dam(object):
        crest_line_xy = LINE
        ds_slope_h = 2.0

    class St(object):
        midcrest_line = None
        inflow_toe_band_m = None
        inflow_toe_cell_m = None
        inflow_toe_ds_slope_h = None
        inflow_toe_min_outer_m = None
    src = _Src()
    local = src.read((-430.0, -430.0, 430.0, 430.0), 20.0)
    for st_slope, want in ((None, 3.0 * H_M), (5.0, 6.0 * H_M)):
        St.inflow_toe_ds_slope_h = st_slope
        warnings, info = [], {}
        pt, why = workflow._downstream_inflow(src, local, Dam(), (0.0, 0.0), H_M, TOP_M, 90.0,
                                              20.0, warnings, lambda *_a: None, out=info,
                                              **workflow._toe_kwargs(Dam(), St()))
        assert abs(info["toe_band_m"][1] - want) < 1e-9, (info, want)
        assert abs(pt[0] - 10.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0 and not warnings, (pt, why)
    # an explicit band (RunSettings.inflow_toe_band_m) is used as given
    St.inflow_toe_band_m = (5.0, 15.0)
    info = {}
    workflow._downstream_inflow(src, local, Dam(), (0.0, 0.0), H_M, TOP_M, 90.0, 20.0, [],
                                lambda *_a: None, out=info, **workflow._toe_kwargs(Dam(), St()))
    assert info["toe_band_m"] == (5.0, 15.0) and info["toe_band_how"] is None, info


@register
def workflow_warns_when_the_toe_band_has_no_dam_height():
    from nd_breach_runner.core import workflow
    real = terrain.toe_band_for_dam
    seen = []

    def no_height(h_m, *a, **k):          # the run always has H; the band's own fallback
        seen.append(h_m)
        return real(None, *a, **k)
    terrain.toe_band_for_dam = no_height
    try:
        src = _Src()
        local = src.read((-430.0, -430.0, 430.0, 430.0), 20.0)
        warnings, info = [], {}
        pt, _why = workflow._downstream_inflow(src, local, object(), (0.0, 0.0), H_M, TOP_M,
                                               90.0, 20.0, warnings, lambda *_a: None, out=info,
                                               toe_line=LINE)
    finally:
        terrain.toe_band_for_dam = real
    assert seen == [H_M] and info["toe_band_m"] == terrain.INFLOW_TOE_BAND_M, info
    assert any("dam height not known" in w for w in warnings), warnings
    assert pt is not None and pt[0] > 0, pt
