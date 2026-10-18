"""find_inflow_point: the usual radius takes the LOWEST low-enough cell (the note says so); only
the wider 400 m fallback takes the NEAREST. Taking the nearest in the usual radius put the
inflow on the upstream face of the embankment / the floor of a dry reservoir."""
import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M


def _grid(fn, half=200.0, cell=10.0):
    n = int(2 * half / cell)
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    return Grid(fn(X, Y), -half, half, cell)


def _dry_dam(X, Y):
    """A dry flood-control dam at x = 0: the empty reservoir floor (west) is half the dam height
    down, right against the upstream toe; the channel below the dam (east) is lower."""
    up = TOP_M - 0.55 * H_M + 0.0 * X
    down = TOP_M - H_M - 0.002 * np.clip(X, 0, None) - np.where(np.abs(Y) < 12, 0.3, 0.0)
    z = np.where(X < 0, up, down)
    emb = TOP_M - np.abs(X) / 40.0 * H_M
    return np.where(np.abs(X) <= 40, np.maximum(z, emb), z)


@register
def inflow_point_of_a_dry_dam_is_below_the_dam_not_on_the_reservoir_floor():
    pt, why = terrain.find_inflow_point(_grid(_dry_dam), (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] > 0, (pt, why)
    assert why.startswith("lowest ground within")


@register
def inflow_point_wider_search_takes_the_nearest_low_cell():
    # nothing low enough within the usual 90 m: a wide embankment, the toe 150 m east and a
    # deeper far valley 350 m east; the wider search takes the toe, not the far valley
    def wide(X, Y):
        z = np.full_like(X, TOP_M - 0.2 * H_M)
        z = np.where((X > 140) & (X < 170), TOP_M - 0.6 * H_M, z)
        return np.where(X > 330, TOP_M - 2.0 * H_M, z)
    pt, why = terrain.find_inflow_point(_grid(wide, half=420.0), (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and 140 < pt[0] < 170, (pt, why)
    assert why.startswith("toe is outside the usual")


# ------------------------------------------------- downstream side of the crest (Absaraka)
def _basin_lower_than_toe(X, Y):
    """The wizard case at Absaraka DWR-2801: a closed dry basin west of the embankment whose
    floor is LOWER than the toe east of it (lowest-in-radius alone picks the reservoir floor).
    The basin is ringed by ground near the top of dam; the downstream valley drains east off
    the grid."""
    floor = TOP_M - 0.9 * H_M                          # reservoir floor
    toe = TOP_M - 0.6 * H_M - 0.002 * np.clip(X, 0, None)
    z = np.where(X < 0, floor, toe)
    rim = (X < -380) | ((X < 0) & (np.abs(Y) > 380))     # the basin is closed on its other sides
    z = np.where(rim, TOP_M - 0.05 * H_M, z)
    emb = TOP_M - np.abs(X) / 40.0 * H_M
    return np.where(np.abs(X) <= 40, np.maximum(z, emb), z)


def _setup(half=1000.0, cell=20.0):
    g = _grid(_basin_lower_than_toe, half=half, cell=cell)
    pool = g.like(terrain.pool_mask(g, H_M).astype(float), nodata=-1.0)
    return g, pool


@register
def inflow_lowest_alone_picks_the_basin_floor_lower_than_the_toe():
    g, _pool = _setup()
    pt, _why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] < 0, pt                     # the bug the constraint fixes


@register
def inflow_pool_and_direction_find_the_downstream_toe():
    g, pool = _setup()
    assert pool.data[g.rc(-200.0, 0.0)] == 1.0 and pool.data[g.rc(200.0, 0.0)] == 0.0
    v, how = terrain.downstream_direction(g, (0.0, 0.0), H_M)
    assert v[0] > 0.9 and "pool" in how, (v, how)
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=v, pool=pool)
    assert pt is not None and pt[0] > 0, (pt, why)
    assert why.startswith("lowest ground within")
    # the side test alone also works, and so does a crest line (oriented away from the pool)
    pt2, _ = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=v)
    assert pt2[0] > 0
    n, how = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=[(0, 300), (0, -300)])
    assert n[0] > 0.99 and "crest-line" in how, (n, how)
    n, _ = terrain.downstream_direction(g, (0.0, 0.0), H_M, crest_line=[(0, -300), (0, 300)])
    assert n[0] > 0.99


@register
def inflow_ties_on_a_flat_floor_go_to_the_nearest():
    def flat(X, Y):
        z = np.full_like(X, TOP_M - 0.8 * H_M)
        return np.where(np.abs(X) <= 20, TOP_M, z)
    g = _grid(flat, half=200.0, cell=10.0)
    pt, _why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=(1.0, 0.0))
    assert pt is not None and abs(pt[0] - 25.0) < 1e-6 and abs(pt[1]) <= 5.0, pt


@register
def inflow_with_no_downstream_cell_warns_and_falls_back():
    g, pool = _setup()
    pt, why = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=(-1.0, 0.0),
                                        pool=pool)          # everything west is the pool
    assert pt is not None and why.startswith(terrain.NO_DOWNSTREAM), why


@register
def inflow_workflow_helper_uses_the_pool_read_and_warns():
    from nd_breach_runner.core import workflow

    class Src(object):
        def read(self, bounds, cell):
            half = (bounds[2] - bounds[0]) / 2.0
            return _grid(_basin_lower_than_toe, half=half, cell=cell)

    class Dam(object):
        crest_line_xy = None
    rad = max(90.0, 8.0 * H_M)
    local = Src().read((-430, -430, 430, 430), 20.0)
    warnings, logs = [], []
    pt, why = workflow._downstream_inflow(Src(), local, Dam(), (0.0, 0.0), H_M, TOP_M, rad, 20.0,
                                          warnings, logs.append)
    assert pt is not None and pt[0] > 0 and "downstream side" in why and not warnings, (pt, why)
    assert any("Downstream side of the crest" in m for m in logs)


# ------------------------------- v3.10.18: the downstream side is kept by place_from_crest
def _road_fill(X, Y):
    """DWR-3161 (Sanish Bay WPA Dam 1 on the ND-23 fill): a low embankment along y ~ 30 m
    between two hydro-flattened water surfaces only ~1.2 m below the top of dam. The pool is
    south (it drains off the south edge of a small grid, so the filled DEM falls that way),
    downstream is north. The dam point (5, 15) is on the south slope of the fill, inside the
    crest band, so the steepest step from it is south, into the pool."""
    south = TOP_M - 1.25 + 0.0 * X
    north = TOP_M - 1.2 - 0.0005 * np.clip(Y - 60.0, 0, None)
    z = np.where(Y < 30.0, south, north)
    ridge = TOP_M - 0.2 - 1.05 * np.abs(Y - 30.0) / 50.0
    return np.where((Y > -20.0) & (Y < 80.0), np.maximum(z, ridge), z)


DAM_3161 = (5.0, 15.0)


@register
def inflow_place_from_crest_ignored_the_downstream_side():
    g = _grid(_road_fill, half=420.0, cell=10.0)
    pt, crest, why = terrain.place_from_crest(g, DAM_3161, H_M, TOP_M, 400.0)
    assert pt is not None and pt[1] < DAM_3161[1], (pt, why)        # the bug: into the pool


@register
def inflow_place_from_crest_keeps_to_the_downstream_side():
    g = _grid(_road_fill, half=420.0, cell=10.0)
    pt, crest, why = terrain.place_from_crest(g, DAM_3161, H_M, TOP_M, 400.0,
                                              downstream=(0.0, 1.0))
    assert pt is not None and pt[1] > 80.0, (pt, why)               # past the fill, north
    assert "downstream side" in why and crest is None, why
    z = g.masked()
    assert z[g.rc(*pt)] < TOP_M - 1.0
    # the trace from the new point leaves the dam; the side check agrees
    path, _ = terrain.trace_downstream(g, pt, 400.0)
    chk = terrain.inflow_side_check(DAM_3161, pt, path, (0.0, 1.0))
    assert chk["ok"], chk


@register
def inflow_side_check_flags_a_point_behind_the_dam():
    """The Oct 18 DWR-3161 run: inflow 40 m south, the trace from it runs north over the dam
    point (4 m from it)."""
    path = [(-489751.8, 2792670.5 + 40.0 * k) for k in range(30)]
    chk = terrain.inflow_side_check((-489749.0, 2792707.7), (-489749.0, 2792667.7), path,
                                    (-0.2, 0.98))
    assert not chk["ok"] and len(chk["problems"]) == 3, chk
    assert chk["trace_min_to_dam_m"] < 5.0
    ok = terrain.inflow_side_check((-489749.0, 2792707.7), (-489749.0, 2792767.7),
                                   [(-489751.8, 2792770.5 + 40.0 * k) for k in range(30)],
                                   (-0.2, 0.98))
    assert ok["ok"], ok


@register
def inflow_site_check_warns_on_side_and_road():
    from nd_breach_runner.core import workflow

    class St(object):
        road_lines = [[(-600.0, 30.0), (600.0, 32.0)]]

    class Dam(object):
        crest_line_xy = None
    rep, warns, logs = {"inflow_point": {"xy": [5.0, -25.0]}}, ["other"], []
    path = [(5.0, -25.0 + 20.0 * k) for k in range(30)]               # runs north over the dam
    workflow._check_inflow_site(rep, St(), Dam(), DAM_3161, (5.0, -25.0), path, (0.0, 1.0),
                                False, warns, logs.append)
    assert warns[0].startswith(terrain.SIDE_PROBLEM), warns
    assert rep["inflow_point"]["side_check"]["ok"] is False
    assert rep["dam_point_checks"]["road_m"] < 20.0 and "road's fill" in warns[-1], warns
    # with the DWR dam line given, the road note says the crest comes from the dam line
    Dam.crest_line_xy = [(-60.0, 15.0), (60.0, 15.0)]
    rep2, w2 = {}, []
    workflow._check_inflow_site(rep2, St(), Dam(), DAM_3161, (5.0, 60.0),
                                [(5.0, 60.0 + 20.0 * k) for k in range(30)], (0.0, 1.0), False,
                                w2, logs.append)
    assert rep2["inflow_point"]["side_check"]["ok"] and rep2["dam_point_checks"]["dam_line_m"] < 1
    assert len(w2) == 1 and "on the DWR dam line" in w2[0], w2
