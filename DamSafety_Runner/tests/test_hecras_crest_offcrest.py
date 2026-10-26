"""3.10.87: the crest trace on DWR-2801 Absaraka, where it fell back to the straight dam line.

(1) The DEM's crest (1066-1069 ft) is more than the tolerance above the top of dam on record
(1063.2 ft, a local datum): an embankment that stands above the ground on both sides is
traced at the DEM's crest level when it is no more than max(5 ft, 25 % of H) above the record,
instead of being called an abutment. (2) The dam.json point is on the pool, about 100 ft from
the crest, and the axis guessed from the run's inflow point is 60-65 degrees off the
embankment, so no ridge lay across it: hecras_crest.snap finds the nearest crest-high ground
and the embankment's direction, and the trace starts there. Both only where the old rule (the
highest ridge across the section, at the record's level) finds no crest, and the line is used
only with hecras_crest.evidence that it is the dam (the DWR #195 audit, rounds 1 and 2)."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_crest as hc
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_hecras_crest import H, CW, TOP, _grid_sampler, _seg_dist


def _dem(top_dem, c=10.0, half=900.0):
    """test_hecras_crest's valley with the embankment along y = 0 at top_dem."""
    xs = np.arange(-half, half, c) + 0.5 * c
    ys = np.arange(half, -half, -c) - 0.5 * c
    X, Y = np.meshgrid(xs, ys)
    g = 100.0 + 40.0 * (np.abs(X) / 600.0) ** 1.5 + 0.005 * Y
    dd = _seg_dist(X, Y, [(-700.0, 0.0), (700.0, 0.0)])
    emb = top_dem - np.maximum(dd - 0.5 * CW, 0.0) / 3.0
    return _grid_sampler(np.maximum(g, emb), -half, half, c)


TOE = 3.0 * H + 0.5 * CW


@test
def hecras_crest_offcrest_dem_crest_above_the_record_is_traced():
    f = _dem(TOP + 5.8)
    try:
        hc.trace(f, (30.0, 15.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0, tol=3.0)
    except hc.CrestTraceError as e:
        raise AssertionError("not traced: {}".format(e))
    tr = hc.trace(f, (30.0, 15.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0, tol=3.0)
    assert abs(tr["top_offset"] - 5.8) < 0.5, tr["top_offset"]
    assert np.abs(tr["line"][:, 1]).max() < 8.0
    assert hc.cumlen(tr["line"])[-1] > 600.0
    # on the record's level: no offset
    tr0 = hc.trace(_dem(TOP), (30.0, 15.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0)
    assert tr0["top_offset"] == 0.0


@test
def hecras_crest_offcrest_far_above_the_record_is_still_an_abutment():
    try:
        hc.trace(_dem(TOP + 12.0), (30.0, 15.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0,
                 300.0, tol=3.0)
    except hc.CrestTraceError as e:
        assert "abutment" in str(e), e
    else:
        raise AssertionError("a crest 12 ft above the record (40 % of H) was traced")


@test
def hecras_crest_offcrest_snap_finds_the_embankment_from_the_pool():
    f = _dem(TOP)
    P = (0.0, 120.0)                       # on the pool, 120 ft upstream (north) of the crest
    a = np.radians(65.0)
    axis = (-np.cos(a), -np.sin(a))        # guessed 65 degrees off the embankment
    try:
        hc.trace(f, P, axis, TOP, H, 10.0, TOE, 1500.0, 300.0, search=40.0)
        raise AssertionError("expected the trace across the guessed axis to fail")
    except hc.CrestTraceError:
        pass
    sn = hc.snap(f, P, TOP, 3.0, 300.0, 200.0, 10.0)
    assert sn is not None
    Q, t = sn
    assert abs(Q[0]) < 15.0 and 0.0 <= Q[1] < 20.0, Q
    assert hc.angle_deg(t, (1.0, 0.0)) < 5.0, t
    tr = hc.trace(f, Q, t, TOP, H, 10.0, TOE, 1500.0, 300.0)
    assert np.abs(tr["line"][:, 1]).max() < 8.0
    # on the crest already, or a round hill: nothing to snap to
    assert hc.snap(f, (0.0, 0.0), TOP, 3.0, 300.0, 200.0, 10.0) is None
    xs = np.arange(-600.0, 600.0, 10.0) + 5.0
    X, Y = np.meshgrid(xs, xs[::-1])
    hill = _grid_sampler(TOP - np.hypot(X, Y) / 3.0, -600.0, 600.0, 10.0)
    assert hc.snap(hill, (0.0, -100.0), TOP, 3.0, 300.0, 150.0, 10.0) is None


@test
def hecras_crest_offcrest_dam_line_in_another_crs():
    """A dam line given in the DEM's CRS (the export tool's DAM_LINE layer) is moved into the
    model's before use: the connection follows it as when it is given in the model's CRS."""
    import os
    import shutil
    import tempfile
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "a"))
        line_m = np.asarray(r["model"].conn_line, float)
        to_dem = hm.transformer(hm.srs_from(r["info"]["crs"]), hm.srs_from("EPSG:26914"))
        line_d = to_dem(line_m[[0, len(line_m) // 2, -1]])
        rb = hm.build_model(d, out_folder=os.path.join(d, "b"), options={
            "dam_line_xy": line_d.tolist(), "dam_line_crs": "EPSG:26914"})
        assert "your dam line" in rb["info"]["dam_line_how"], rb["info"]["dam_line_how"]
        lb = np.asarray(rb["model"].conn_line, float)
        cell = float(rb["info"]["cell"])
        assert hm.point_segment_distance(lb[:, 0], lb[:, 1], line_m).max() < cell, lb
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_crest_offcrest_dam_line_points_reads_the_first_line():
    from nd_breach_runner import algorithms  # noqa: F401  (algorithms_tools imports it first)
    from nd_breach_runner.algorithms_hecras import dam_line_points

    class P:
        def __init__(self, x, y):
            self._x, self._y = x, y

        def x(self):
            return self._x

        def y(self):
            return self._y

    class G:
        def __init__(self, pts, multi=False):
            self.pts, self.multi = pts, multi

        def isEmpty(self):
            return not self.pts

        def isMultipart(self):
            return self.multi

        def asPolyline(self):
            return [P(*q) for q in self.pts]

        def asMultiPolyline(self):
            return [[P(*q) for q in self.pts]]

    class F:
        def __init__(self, g):
            self.g = g

        def geometry(self):
            return self.g

    class S:
        def __init__(self, feats):
            self.feats = feats

        def getFeatures(self):
            return iter(self.feats)

    assert dam_line_points(None) == []
    assert dam_line_points(S([F(G([])), F(G([(1, 2), (3, 4)]))])) == [(1.0, 2.0), (3.0, 4.0)]
    assert dam_line_points(S([F(G([(0, 0), (5, 0), (9, 1)], multi=True))]))[-1] == (9.0, 1.0)


# --- 3.10.87 fixes (DWR #195 audit): the dam point's own ridge, the snap's checks, the line's
# check against the run's flow, the breach on a crest traced off the record, a dam line of
# your own ----------------------------------------------------------------------------------
def _valley_with(top_dem, c=10.0, half=900.0, roads=(), notch=None, grid=False, cw=CW,
                 nodata_above=None):
    """_dem's valley and embankment (crest at top_dem, cw wide) plus road embankments (x0, y0,
    x1, y1, z, w: top w ft wide at z, 2:1 sides) and a crest notch (x, w, depth); the pool
    left out of the DEM (no data) upstream below nodata_above. grid: (z, x0, ytop, c)."""
    xs = np.arange(-half, half, c) + 0.5 * c
    ys = np.arange(half, -half, -c) - 0.5 * c
    X, Y = np.meshgrid(xs, ys)
    g = 100.0 + 40.0 * (np.abs(X) / 600.0) ** 1.5 + 0.005 * Y
    dd = _seg_dist(X, Y, [(-700.0, 0.0), (700.0, 0.0)])
    crest = np.full(X.shape, float(top_dem))
    if notch is not None:
        nx, nw, ndep = notch
        crest = crest - np.clip(ndep - np.maximum(np.abs(X - nx) - 0.5 * nw, 0.0), 0.0, None)
    z = np.maximum(g, crest - np.maximum(dd - 0.5 * cw, 0.0) / 3.0)
    for x0, y0, x1, y1, rz, rw in roads:
        rd = _seg_dist(X, Y, [(x0, y0), (x1, y1)])
        z = np.maximum(z, rz - np.maximum(rd - 0.5 * rw, 0.0) / 2.0)
    if nodata_above is not None:
        z = np.where((Y > 0.0) & (z < nodata_above), np.nan, z)
    return (z, -half, half, c) if grid else _grid_sampler(z, -half, half, c)


def _wet_below(y, half=400.0, c=50.0):
    """Flooded cells (c wide) below y across |x| < half, as the run's Depth (Max)."""
    g = np.arange(-1775.0, 1775.0, c)
    X, Y = np.meshgrid(g, g)
    m = (Y < y) & (np.abs(X) < half)
    return np.column_stack([X[m], Y[m]])


@test
def hecras_crest_offcrest_the_old_rule_decides_where_it_traces():
    """R1 (round 2 of the DWR #195 audit): wherever the old rule (the highest ridge across the
    section, at the record's level) finds a crest, trace() returns its line - with a road 1-5 ft
    above the dam 25-40 ft beyond half the toe distance, and the dam point on the crest or on
    the road. Where it finds none, the dam point's ground is at the record's level (on the
    crest, or on a road at the record's level), so the crest is not followed at the DEM's
    level onto the higher ridge beside it (the datum allowance needs the dam point's ground
    off the record): the error stands, and never the road."""
    step = 32.8
    for gap in (25.0, 30.0, 35.0, 40.0):
        yr = -(0.5 * TOE + gap)
        for dz in (1.0, 2.0, 3.0, 4.0, 5.0):
            f = _valley_with(TOP, roads=[(-800.0, yr, 800.0, yr, TOP + dz, 24.0)])
            for P in ((30.0, 0.0), (30.0, yr)):
                try:
                    old = hc.trace(f, P, (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0,
                                   relax=False)
                except hc.CrestTraceError as e:
                    old = e
                try:
                    tr = hc.trace(f, P, (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0)
                except hc.CrestTraceError as e:
                    assert isinstance(old, Exception), (gap, dz, P, e)
                    assert str(e) == str(old), (e, old)
                    continue
                if isinstance(old, Exception):
                    # the dam point on a road more than the tolerance above the record: its
                    # own ridge at its own level (another datum?) - evidence() decides, and
                    # the dam beside it says it is not clearly the dam
                    assert P[1] == yr and dz > 3.0 and tr["how"] == "datum", (gap, dz, P)
                    assert abs(tr["p0"][1] - yr) < 10.0 and abs(tr["top_offset"] - dz) < 0.5
                    line = hc.tidy(tr["line"], step, 100.0, keep_len=1500.0, centre=tr["p0"])
                    ok, ev = hc.evidence(line, 0.5 * hc.cumlen(line)[-1], f, P, TOP,
                                         tr["top_used"], tr["tol"], tr["prom"], H, TOE, CW,
                                         step, wet_xy=_wet_below(yr - 60.0), wet_cell=50.0)
                    assert not ok and "another ridge" in ev["why"], (gap, dz, ev)
                    continue
                assert np.array_equal(tr["line"], old["line"]), (gap, dz, P)
                assert not tr["relaxed"] and tr["how"] == "" and tr["top_offset"] == 0.0
    # the datum allowance is what the dam point's ground stops: given a dam point whose ground
    # is off the record (the start of a trace found elsewhere), the higher road is followed
    yr = -(0.5 * TOE + 40.0)
    f = _valley_with(TOP, roads=[(-800.0, yr, 800.0, yr, TOP + 4.0, 24.0)])
    try:
        hc.trace(f, (30.0, 0.0), (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0)
    except hc.CrestTraceError as e:
        assert "rises above the top of dam" in str(e) and e.on_ridge, e
    else:
        raise AssertionError("traced with the dam point at the record's level")
    tr = hc.trace(f, (30.0, yr - 20.0), (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0,
                  dam_ground=TOP - 20.0)
    assert tr["how"] == "datum" and abs(tr["p0"][1] - yr) < 15.0, (tr["how"], tr["p0"])
    assert abs(tr["top_offset"] - 4.0) < 0.5, tr["top_offset"]
    # ... and a ridge at the record's level (the dam) within the toe distance of the dam
    # point: no datum allowance either
    try:
        hc.trace(f, (30.0, yr + 30.0), (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0,
                 dam_ground=TOP - 20.0)
    except hc.CrestTraceError as e:
        assert e.on_ridge and "rises above the top of dam" in str(e), e
    else:
        raise AssertionError("the road was followed with the dam within the toe distance")


@test
def hecras_crest_offcrest_own_ridge_over_a_higher_road():
    """The dam point on the downstream face, its ground off the record, the crest at the
    record's level within half the toe distance and a road 4 ft higher farther across the
    section: the old rule takes the road (the highest ridge) and finds it above the record;
    the dam point's own ridge is followed at the record's level ('own'), not the road at the
    DEM's level. Without the dam point's ridge in reach (near), not the road either."""
    step = 16.4
    yr = -(TOE + 30.0)
    f = _valley_with(TOP, roads=[(-800.0, yr, 800.0, yr, TOP + 4.0, 24.0)])
    P = (30.0, -25.0)                       # on the downstream face, 5 ft below the crest
    try:
        hc.trace(f, P, (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0, relax=False,
                 search=TOE + 60.0)
    except hc.CrestTraceError as e:
        assert "rises above the top of dam" in str(e), e
    else:
        raise AssertionError("the old rule found a crest")
    tr = hc.trace(f, P, (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0, search=TOE + 60.0)
    assert tr["how"] == "own" and abs(tr["p0"][1]) < 10.0 and tr["top_offset"] == 0.0, tr["p0"]
    assert np.abs(tr["line"][:, 1]).max() < 15.0
    # without the dam point's ridge in reach (near) the road is not taken at its own level
    # either: a ridge at the record's level (the dam) lies within the toe distance
    try:
        hc.trace(f, P, (-1.0, 0.0), TOP, H, step, TOE, 1500.0, 300.0, search=TOE + 60.0,
                 near=5.0)
    except hc.CrestTraceError as e:
        assert e.on_ridge and "rises above the top of dam" in str(e), e
    else:
        raise AssertionError("the road was followed with the dam within the toe distance")
    # a 100 ft crest read 5 ft below the record, a road 5 ft higher on its upstream edge (the
    # crest is no ridge then, only a level top): the dam point on the downstream face is on
    # the embankment all the same - on_ridge, so no start from other ground (the road)
    f = _valley_with(TOP - 5.0, cw=100.0, roads=[(-800.0, 70.0, 800.0, 70.0, TOP, 40.0)])
    try:
        hc.trace(f, (30.0, -60.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0,
                 crest_w=100.0)
    except hc.CrestTraceError as e:
        assert e.on_ridge and "falls below the crest" in str(e), e
    else:
        raise AssertionError("traced from beside a crest read below the record")


@test
def hecras_crest_offcrest_a_crest_read_low_is_not_left_for_other_ground():
    """F1 (a): a dam point on a ridge that stands above both sides but reads more than the
    tolerance below the record (a narrow crest on a coarse DEM, a record above the DEM crest):
    the first trace's error stands and says on_ridge, so build_model does not start from other
    crest-high ground nearby (a road fill, a dike) and falls back to the straight line."""
    for low in (4.0, 12.0):
        try:
            hc.trace(_dem(TOP - low), (30.0, 0.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0)
        except hc.CrestTraceError as e:
            assert e.on_ridge and "falls below" in str(e), e
        else:
            raise AssertionError("a crest {} ft below the record was traced".format(low))
    # on the pool, off any ridge: not on_ridge (a start from the crest nearby may be tried)
    try:
        hc.trace(_dem(TOP), (0.0, 120.0), (-np.cos(1.13), -np.sin(1.13)), TOP, H, 10.0, TOE,
                 1500.0, 300.0, search=40.0)
    except hc.CrestTraceError as e:
        assert not e.on_ridge, e
    else:
        raise AssertionError("the pool point's guessed axis found a crest")


@test
def hecras_crest_offcrest_snap_climbs_from_the_dam_point():
    """F1 (c): snap takes crest-high ground only where the ground climbs to it from the dam
    point (a pool or face point climbs the dam face); a road at the record's level beyond a
    low valley floor is not taken from a crest that reads low; nothing far above the record
    (allow) is taken."""
    yr = -250.0
    f = _valley_with(TOP - 6.0, roads=[(-800.0, yr, 800.0, yr, TOP, 24.0)])
    sn = hc.snap(f, (0.0, 0.0), TOP, 3.0, 300.0, 200.0, 10.0)
    assert sn is not None and abs(sn[0][1] - yr) < 30.0, sn       # without the checks: the road
    assert hc.snap(f, (0.0, 0.0), TOP, 3.0, 300.0, 200.0, 10.0, allow=7.5, prom=3.0) is None
    # from the pool: still the dam
    sn = hc.snap(_dem(TOP), (0.0, 100.0), TOP, 3.0, TOE + 0.5 * CW, 200.0, 10.0, allow=7.5,
                 prom=3.0)
    assert sn is not None and 0.0 <= sn[0][1] < 30.0 and hc.angle_deg(sn[1], (1, 0)) < 5.0, sn
    # ground far above the record is no candidate: the point is on the face, within the band
    f = _dem(TOP + 20.0)
    sn = hc.snap(f, (0.0, 100.0), TOP, 3.0, TOE + 0.5 * CW, 200.0, 10.0, allow=7.5, prom=3.0)
    assert sn is not None and TOP - 3.0 <= f(sn[0][None, :])[0] <= TOP + 7.5, sn


@test
def hecras_crest_offcrest_snap_band_and_start_within():
    """snap takes crest-high ground only up to the datum allowance above the record: a wall
    20 ft above the record beside a pool point is nearer than the dam, but the dam is the
    start; and none from a dam point on the face of another ridge. trace's start_within: a start whose section's crest is farther away than that is
    not used (the trace from a start found nearby follows that start's crest only)."""
    base_f = _dem(TOP)

    def f(xy):
        xy = np.atleast_2d(np.asarray(xy, float))
        wall = (np.abs(xy[:, 0] + 60.0) < 15.0) & (xy[:, 1] > 20.0)
        return np.where(wall, TOP + 20.0, base_f(xy))

    sn = hc.snap(f, (0.0, 100.0), TOP, 3.0, 150.0, 200.0, 10.0, allow=7.5, prom=3.0)
    assert sn is not None and abs(sn[0][1]) < 30.0 and abs(sn[0][0]) < 30.0, sn
    assert hc.angle_deg(sn[1], (1.0, 0.0)) < 15.0, sn
    # the dam point on the downstream face of a crest that reads 5 ft below the record, a road
    # at the record's level whose side meets that face in a shallow dip: the ground climbs to
    # the road, but the dam point is on the dam's face (its crest rises behind it) - no start
    f2 = _valley_with(TOP - 5.0, roads=[(-800.0, -60.0, 800.0, -60.0, TOP, 24.0)])
    sn = hc.snap(f2, (0.0, -60.0 + 35.0), TOP, 3.0, 150.0, 200.0, 10.0, allow=7.5, prom=3.0)
    assert sn is None, sn
    sn = hc.snap(f2, (0.0, -25.0), TOP, 3.0, 150.0, 200.0, 10.0, allow=7.5)    # no prom: no check
    assert sn is not None and abs(sn[0][1] + 60.0) < 25.0, sn
    try:
        hc.trace(base_f, (30.0, 20.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0,
                 start_within=10.0)
    except hc.CrestTraceError as e:
        assert "of the start (the crest across the section is" in str(e), e
    else:
        raise AssertionError("a start 20 ft from its section's crest was used")
    tr = hc.trace(base_f, (30.0, 20.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0, 300.0,
                  start_within=30.0)
    assert np.abs(tr["line"][:, 1]).max() < 8.0


@test
def hecras_crest_offcrest_flow_check():
    """H1 / F1 (d): a line from a snap or traced off the record's level is used only when it
    crosses the run's flow: a dam line across the valley passes; a road along the valley
    (half the flood on each side), a causeway across the pool (the dam between it and the
    flood) and a line the dam point lies far downstream of do not."""
    f = _valley_with(TOP, roads=[(-800.0, 300.0, 800.0, 300.0, TOP, 24.0)])
    g = np.arange(-875.0, 875.0, 50.0)
    X, Y = np.meshgrid(g, g)
    wet = np.column_stack([X[(Y < -120.0) & (np.abs(X) < 400.0)],
                           Y[(Y < -120.0) & (np.abs(X) < 400.0)]])
    kw = dict(sample=f, crest=TOP, tol=3.0, toe=TOE, drop=0.5 * H, prom=3.0, step=10.0)
    dam = np.array([(450.0, 0.0), (0.0, 0.0), (-450.0, 0.0)])        # A -> B: downstream left
    ok, info = hc.flow_check(dam, wet, 50.0, **kw)
    assert ok and info["downstream_frac"] > 0.95 and info["flow_angle_deg"] < 10.0, info
    road = np.array([(100.0, 0.0), (100.0, -400.0), (100.0, -800.0)])
    ok, info = hc.flow_check(road, wet, 50.0, **kw)
    assert not ok and "downstream side" in info["why"], info
    cause = dam + (0.0, 300.0)
    ok, info = hc.flow_check(cause, wet, 50.0, **kw)
    assert not ok and "ridge" in info["why"], info
    ok, info = hc.flow_check(cause, wet, 50.0, dam_point=(0.0, 100.0), beyond=TOE + 0.5 * CW)
    assert not ok and "downstream of it" in info["why"], info
    ok, info = hc.flow_check(dam, np.zeros((0, 2)), 50.0)
    assert not ok and "no flooded cells" in info["why"], info


@test
def hecras_crest_offcrest_flow_check_each_rule():
    """flow_check's rules one at a time (each case fails one rule only): (a) a quarter of the
    flood upstream of the line, its centroid still square below it; (b) all of it downstream
    but its centroid 62 degrees off the line's normal (a straight line: every part the same);
    (c) an L-shaped line whose arm runs along the flood's edge, the centroid 15 degrees off."""
    g = np.arange(-1775.0, 1775.0, 50.0)
    X, Y = np.meshgrid(g, g)
    dam = np.array([(450.0, 0.0), (0.0, 0.0), (-450.0, 0.0)])
    below = (Y < -120.0) & (Y > -900.0) & (np.abs(X) < 400.0)
    above = (Y > 120.0) & (Y < 420.0) & (np.abs(X) < 400.0)
    wet = np.column_stack([X[below | above], Y[below | above]])
    ok, info = hc.flow_check(dam, wet, 50.0)
    assert not ok and info["why"].startswith("only ") and "downstream side" in info["why"], info
    assert 0.6 < info["downstream_frac"] < 0.8 and info["flow_angle_deg"] < 5.0, info
    ok, info = hc.flow_check(dam, np.column_stack([X[below], Y[below]]), 50.0)
    assert ok and info["downstream_frac"] == 1.0, info
    side = (Y < -200.0) & (Y > -400.0) & (X > 477.0) & (X < 677.0)
    ok, info = hc.flow_check(dam, np.column_stack([X[side], Y[side]]), 50.0)
    assert not ok and "degrees off its downstream side" in info["why"], info
    assert 60.0 < info["flow_angle_deg"] < 65.0 and info["downstream_frac"] == 1.0, info
    ell = np.array([(800.0, 0.0), (-400.0, 0.0), (-400.0, -600.0)])
    strip = (Y < -120.0) & (np.abs(X) < 350.0)
    ok, info = hc.flow_check(ell, np.column_stack([X[strip], Y[strip]]), 50.0)
    assert not ok and info["why"].startswith("parts of it run"), info
    assert info["flow_angle_deg"] < 30.0 and info["downstream_frac"] > 0.95, info


@test
def hecras_crest_offcrest_flow_path_follows_the_valley():
    """flow_path goes down the valley: from the toe of a dam across it, straight down; from
    beside a levee along the valley, to the channel and then down it (its local slope points
    across the valley, the path does not stay that way)."""
    xs = np.arange(-900.0, 900.0, 10.0) + 5.0
    X, Y = np.meshgrid(xs, xs[::-1])
    z = 100.0 + 0.02 * np.abs(X) + 0.004 * Y - np.where(np.abs(X) < 30.0, 1.0 - np.abs(X) / 30.0,
                                                         0.0)
    f = _grid_sampler(z, -900.0, 900.0, 10.0)
    p = hc.flow_path(f, (0.0, 100.0), (0.0, -1.0), 600.0, 30.0)
    assert np.abs(p[:, 0]).max() < 15.0 and p[-1][1] < -450.0, p[-1]
    p = hc.flow_path(f, (-300.0, 400.0), (1.0, 0.0), 800.0, 30.0)
    v = p[-1] - p[0]
    assert abs(p[-1][0]) < 30.0 and hc.angle_deg(v, (1.0, 0.0)) > 60.0, p[-1]
    # it stops at the edge of the DEM
    p = hc.flow_path(f, (0.0, -700.0), (0.0, -1.0), 2000.0, 30.0)
    assert hc.cumlen(p)[-1] < 250.0, hc.cumlen(p)[-1]


def _ev(line, f, P, top_used=TOP, wet=None, step=10.0, sc=None):
    line = np.asarray(line, float)
    return hc.evidence(line, 0.5 * hc.cumlen(line)[-1] if sc is None else sc, f, P, TOP,
                       top_used, 3.0, 3.0, H, TOE, CW, step,
                       wet_xy=_wet_below(-120.0, c=50.0) if wet is None else wet, wet_cell=50.0)


@test
def hecras_crest_offcrest_evidence():
    """hecras_crest.evidence, one rule at a time: the dam across the valley with the flood
    below it passes (from its crest, its upstream face, the pool); a dam point farther than the
    toe distance plus half the crest width and half a step from the line, a ridge between the
    dam point and the line, a levee along the valley (the flooded area all beside it, but the
    flow path below its breach runs down the valley), a road beside the dam (another crest-high
    ridge within twice the toe distance) and a line with crest-high ground behind it fail."""
    f = _valley_with(TOP, half=1800.0)
    dam = [(450.0, 0.0), (0.0, 0.0), (-450.0, 0.0)]
    for P in ((30.0, 0.0), (30.0, 40.0), (30.0, TOE + 0.5 * CW)):
        ok, ev = _ev(dam, f, P)
        assert ok, (P, ev)
        assert ev["flow_path_angle_deg"] < 10.0 and ev["parallel_ridge_frac"] == 0.0, ev
        assert abs(ev["dam_point_to_line"] - abs(P[1])) < 1.0, ev
    ok, ev = _ev(dam, f, (30.0, TOE + 0.5 * CW + 10.0))
    assert not ok and ev["why"].startswith("the dam point is") and "more than the toe" in \
        ev["why"], ev
    # a ridge between: the dam point on the pool beyond a causeway, the line on the dam
    fc = _valley_with(TOP, half=1800.0, roads=[(-800.0, 60.0, 800.0, 60.0, TOP - 2.0, 10.0)])
    ok, ev = _ev(dam, fc, (30.0, 100.0))
    assert not ok and "does not climb" in ev["why"], ev
    # a levee along the valley: the line on it, the flood all on its downstream side
    fl = _valley_with(TOP, half=1800.0, roads=[(-200.0, -60.0, -200.0, -1500.0, TOP, 24.0)])
    lev = [(-200.0, -100.0), (-200.0, -500.0), (-200.0, -900.0)]
    g = np.arange(-1775.0, 1775.0, 50.0)
    X, Y = np.meshgrid(g, g)
    m = (X > -200.0) & (X < 400.0) & (Y < -60.0) & (Y > -1400.0)
    wet = np.column_stack([X[m], Y[m]])
    ok, fi = hc.flow_check(np.asarray(lev), wet, 50.0)
    assert ok, fi                      # the flooded area alone cannot tell
    ok, ev = _ev(lev, fl, (-180.0, -500.0), wet=wet)
    assert not ok and "flow path" in ev["why"] and ev["flow_path_angle_deg"] > 45.0, ev
    # a road beside the dam: the line on the dam, a road at the record's level downstream
    # (open at the channel: a bridge)
    fr = _valley_with(TOP, half=1800.0, roads=[(-800.0, -150.0, -60.0, -150.0, TOP, 24.0),
                                               (60.0, -150.0, 800.0, -150.0, TOP, 24.0)])
    ok, ev = _ev(dam, fr, (30.0, 0.0), wet=_wet_below(-50.0))     # (flooded past the road)
    assert not ok and "another ridge" in ev["why"] and ev["parallel_ridge_frac"] > 0.5, ev
    # a road on the upstream edge of a 100 ft crest, 5 ft above it: the line on the road, the
    # crest a level top at the record's level beside it (no ridge: the road keeps it from
    # being one)
    fw = _valley_with(TOP, half=1800.0, cw=100.0, roads=[(-800.0, 70.0, 800.0, 70.0, TOP + 5.0,
                                                          40.0)])
    ok, ev = _ev([(450.0, 70.0), (-450.0, 70.0)], fw, (30.0, 70.0), top_used=TOP + 5.0)
    assert not ok and "another ridge" in ev["why"] and ev["parallel_ridge_frac"] > 0.5, ev
    # crest-high ground behind the line (beyond its toe, on the upstream side)
    fh = _valley_with(TOP, half=1800.0, roads=[(-800.0, 180.0, 800.0, 180.0, TOP + 8.0, 300.0)])
    ok, ev = _ev(dam, fh, (30.0, 0.0))
    assert not ok and "behind it" in ev["why"] and ev["upstream_high_frac"] > 0.5, ev
    # no flow path on the DEM below it
    ok, ev = _ev([(450.0, -1700.0), (-450.0, -1700.0)], f, (30.0, -1700.0),
                 wet=np.column_stack([X[Y < -1720.0], Y[Y < -1720.0]]))
    assert not ok and "no flow path" in ev["why"], ev


@test
def hecras_crest_offcrest_walk_off_the_old_rule():
    """The walk of a trace off the old rule (here at the DEM's level, 5 ft above the record):
    (a) on a level crest 100 ft wide (wider than three times the walk's window) it keeps to
    the middle of the top instead of drifting across it a little every step; (b) where the
    pool is left out of the DEM (no data) up to 2 ft below the crest of a 48 ft wide crest,
    the side that runs into no data falls (the one face sample left is not the ground beside
    the crest), so the crest is followed past it; (c) a single high point past a stretch
    stepped over at the end (a knoll on the abutment) is not crest: the line ends before it.
    The old rule's own walk is unchanged."""
    z, x0, ytop, c = _valley_with(TOP + 5.0, cw=100.0, grid=True)
    f = _grid_sampler(z, x0, ytop, c)
    tr = hc.trace(f, (30.0, 0.0), (-1.0, 0.0), TOP, H, 10.0, 3 * H + 50.0, 1500.0, 300.0,
                  crest_w=100.0)
    assert tr["how"] == "datum" and hc.cumlen(tr["line"])[-1] > 900.0, tr["ends"]
    assert np.abs(tr["line"][:, 1]).max() < 15.0, np.abs(tr["line"][:, 1]).max()
    # (b) the pool left out of the DEM beside the left half of the crest
    z, x0, ytop, c = _valley_with(TOP + 5.0, cw=48.0, grid=True)
    X, Y = np.meshgrid(x0 + (np.arange(z.shape[1]) + 0.5) * c,
                       ytop - (np.arange(z.shape[0]) + 0.5) * c)
    z = np.where((Y > 0.0) & (X < -100.0) & (z < TOP + 3.0), np.nan, z)
    f = _grid_sampler(z, x0, ytop, c)
    tr = hc.trace(f, (30.0, 0.0), (-1.0, 0.0), TOP, 10.0, 10.0, 54.0, 1500.0, 300.0,
                  crest_w=48.0)
    assert tr["how"] == "datum" and tr["line"][:, 0].min() < -450.0, tr["ends"]
    # (c) a 500 ft dam on a flat floor, a knoll at the crest's level 160 ft past its end
    xs = np.arange(-900.0, 900.0, 10.0) + 5.0
    X, Y = np.meshgrid(xs, xs[::-1])
    z = np.maximum(100.0 + 0.005 * Y, TOP + 5.0 - np.maximum(np.abs(Y) - 10.0, 0.0) / 3.0 -
                   np.maximum(np.abs(X) - 500.0, 0.0))
    z = np.maximum(z, TOP + 5.0 - np.maximum(np.hypot(X + 660.0, Y) - 12.0, 0.0) / 2.0)
    f = _grid_sampler(z, -900.0, 900.0, 10.0)
    tr = hc.trace(f, (30.0, 0.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 3000.0, 300.0, crest_w=CW)
    assert tr["how"] == "datum" and -520.0 < tr["line"][:, 0].min() < -480.0, \
        tr["line"][:, 0].min()
    old = hc.trace(f, (30.0, 0.0), (-1.0, 0.0), TOP + 5.0, H, 10.0, TOE, 3000.0, 300.0)
    assert old["line"][:, 0].min() < -640.0, old["line"][:, 0].min()


def _cdl(folder, grid, P, flow, Hh=H, cw=CW, top=TOP, wet=None, step=None):
    """hecras_model.crest_dam_line on a _valley_with grid (written in EPSG:2266 feet, the
    local frame moved to 2,744,000 / 495,000): the dam point P and the flow direction (to
    the run's inflow point) in the local frame; wet: the run's flooded cells (local) or None.
    Returns (line, sc, rec) in the local frame, or raises CrestTraceError."""
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    z, x0, ytop, c = grid
    O = np.array((2744000.0, 495000.0))
    path = os.path.join(folder, "v.tif")
    p0._tif(path, np.where(np.isfinite(z), z, -9999.0), x0 + O[0], ytop + O[1], c, epsg=2266)
    srs = hm.srs_from("EPSG:2266")
    units, _ = hm.crs_units(srs)
    dam = br.DamData(dam_id="T", dam_height_ft=Hh, storage_top_acft=300.0, top_of_dam_ft=top,
                     crest_width_ft=cw, crest_length_ft=1400.0)
    params = br.make_hydrograph(dam, br.METHOD_PIPING).summary()
    dv = np.asarray(flow, float) / np.hypot(*flow)
    Pm = np.asarray(P, float) + O
    line, sc, rec = hm.crest_dam_line(
        path, srs, 0.3048, units, dam, params, Pm, dv, np.array([-dv[1], dv[0]]), 100.0,
        step or c, dict(hm.DEFAULTS), inflow_m=Pm + 300.0 * dv,
        wet_xy=None if wet is None else np.asarray(wet, float) + O, wet_cell=50.0)
    return line - O, sc, rec


@test
def hecras_crest_offcrest_crest_dam_line_what_was_tried():
    """crest_dam_line where the old rule finds no crest says what else was tried and why it
    was not used, after the old rule's own error (which stands): the dam point on a crest
    that a coarse DEM reads low - no start from other ground (on_ridge); a pool point farther
    than the toe distance plus half the crest width and half a step from any crest-high
    ground - nothing in reach to start from; from 90 ft the start is the dam, traced (its
    crest within the toe distance of the start, the datum rule judged by the dam point's own
    ground) and used; a start whose trace fails keeps the old rule's error first; without the
    run's flooded area, only the old rule's error."""
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        # (1) on a ridge read 5 ft low (the record above the DEM's crest), a road at the record
        # level 160 ft downstream: on_ridge, no start from other high ground
        yr = -160.0
        g = _valley_with(TOP - 5.0, half=1800.0, grid=True,
                         roads=[(-800.0, yr, 800.0, yr, TOP, 24.0)])
        try:
            _cdl(d, g, (30.0, 0.0), (0.0, -1.0), wet=_wet_below(-200.0))
        except Exception as e:
            assert "falls below the crest" in str(e) and e.on_ridge, e
            assert "so no start from other high ground nearby was tried" in str(e), e
            assert getattr(e, "snap_tried", None) is None, e
        else:
            raise AssertionError("traced from a ridge read below the record")
        # (2) a pool point beyond reach: toe 100 + crest 10 + half a step 5 = 115 ft
        g = _valley_with(TOP, half=1800.0, grid=True)
        a = np.radians(65.0)
        try:
            _cdl(d, g, (0.0, 160.0), (np.sin(a), -np.cos(a)), wet=_wet_below(-120.0))
        except Exception as e:
            assert "no crest-high ground that the ground climbs to" in str(e), e
            assert e.snap_tried == {"found": False, "within": 115.0}, e.snap_tried
        else:
            raise AssertionError("traced from a pool point 160 ft from the crest")
        # (3) the same from 90 ft: the start is the dam, traced and used
        line, sc, rec = _cdl(d, g, (0.0, 90.0), (np.sin(a), -np.cos(a)),
                             wet=_wet_below(-120.0))
        assert rec["off_rule"] == "snap" and np.abs(line[:, 1]).max() < 15.0, rec
        assert rec["evidence"]["dam_point_to_line"] > 80.0, rec["evidence"]
        # the start's trace: from the start, its crest within the toe distance of it, the
        # datum rule judged by the dam point's own ground (M07); when that trace fails, the
        # old rule's error stands first and the start's error follows (M27)
        calls, real = [], hc.trace

        def spy(*a_, **k_):
            calls.append(k_)
            return real(*a_, **k_)

        def failing(*a_, **k_):
            if k_.get("start_within") is not None:
                raise hc.CrestTraceError("test: the start's trace")
            return real(*a_, **k_)
        try:
            hc.trace = spy
            _cdl(d, g, (0.0, 90.0), (np.sin(a), -np.cos(a)), wet=_wet_below(-120.0))
            hc.trace = failing
            try:
                _cdl(d, g, (0.0, 90.0), (np.sin(a), -np.cos(a)), wet=_wet_below(-120.0))
            except hc.CrestTraceError as e:
                err = e
            else:
                raise AssertionError("the start's failed trace was used")
        finally:
            hc.trace = real
        k = [c_ for c_ in calls if c_.get("start_within") is not None]
        assert len(k) == 1 and abs(k[0]["start_within"] - TOE) < 0.5, calls
        assert abs(k[0]["dam_ground"] - (TOP - (90.0 - 0.5 * CW) / 3.0)) < 0.5, k  # its face
        assert str(err).startswith("no embankment crest within 100 of the dam point"), err
        assert " ft away was tried: test: the start's trace" in str(err), err
        assert err.snap_tried["why"] == "test: the start's trace", err.snap_tried
        # (4) without the run's flooded area: the old rule's error alone
        try:
            _cdl(d, g, (0.0, 90.0), (np.sin(a), -np.cos(a)))
        except Exception as e:
            assert str(e).startswith("no embankment crest within") and "tried" not in str(e), e
        else:
            raise AssertionError("traced off the old rule without the flooded area")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _absaraka_tif(folder):
    from nd_breach_runner.tests import test_hecras_p0 as p0
    from nd_breach_runner.tests.test_hecras_crest import _absaraka
    z, x0, y0, c = _absaraka()
    path = os.path.join(folder, "abs.tif")
    p0._tif(path, z, x0, y0, c, epsg=2266)
    return path, z, x0, y0, c


@test
def hecras_crest_offcrest_absaraka_record_level_breach():
    """F2 on DWR-2801: with the top of dam on record (1063.2 ft, the DEM crest 1066-1069 ft)
    the crest is traced at the DEM's level, and the breach is at the deepest dam section (the
    v2e breach, within 150 ft), not where the crest dips to the record's level; from the pool
    point 80 ft upstream with the axis guessed 63 degrees off, the line starts from the crest
    nearby (it crosses the run's flow) and gives the same breach."""
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        dem, z, x0, y0, c = _absaraka_tif(d)
        f = _grid_sampler(z, x0, y0, c)
        srs = hm.srs_from("EPSG:2266")
        units, _ = hm.crs_units(srs)
        P0 = np.array((2744159.0, 495413.2))
        I0 = np.array((2744423.5, 495361.6))
        dd = (I0 - P0) / np.hypot(*(I0 - P0))
        ref = hc.trace(f, P0, (dd[1], -dd[0]), 1069.117, 27.5, c, 92.5, 1700.0, 300.0)
        ref_line = hc.tidy(ref["line"], c, 100.0, keep_len=1700.0, centre=ref["p0"])
        # the flooded area: below 1049 ft on the downstream side of the crest, 125 ft cells
        ny, nx = z.shape
        XX, YY = np.meshgrid(x0 + (np.arange(nx) + 0.5) * c, y0 - (np.arange(ny) + 0.5) * c)
        pts = np.column_stack([XX.ravel(), YY.ravel()])
        ext = hc.extend(ref_line, 5000.0, ref_line[-1] - ref_line[0])
        side = hc._seg_nearest(ext, pts)[1]
        nl = hc.left_normal(ref_line[-1] - ref_line[0])
        down = side * (1.0 if nl @ dd > 0 else -1.0) > 0
        fine = down & np.isfinite(z.ravel()) & (z.ravel() < 1049.0)
        cells = sorted(set(zip(((pts[fine, 0] - x0) // 125.0).astype(int),
                               ((y0 - pts[fine, 1]) // 125.0).astype(int))))
        wet = np.array([(x0 + (i + 0.5) * 125.0, y0 - (j + 0.5) * 125.0) for i, j in cells])
        dam = br.DamData(dam_id="DWR-2801", dam_height_ft=27.5, storage_top_acft=1616.0,
                         top_of_dam_ft=1063.2, crest_width_ft=20.0)
        params = br.make_hydrograph(dam, br.METHOD_PIPING).summary()
        v2e = np.array((2744349.6, 495646.7))
        st = hc.station_of(ref_line, P0)
        t = hc.tangent_at(ref_line, st)
        nd = hc.left_normal(t) * (1.0 if hc.left_normal(t) @ dd > 0 else -1.0)
        a = np.radians(63.0)
        for P, dv in ((P0, dd), (hc.point_at(ref_line, st) - nd * 80.0,
                                 nd * np.cos(a) + t * np.sin(a))):
            n_left = np.array([-dv[1], dv[0]])
            line, sc, rec = hm.crest_dam_line(dem, srs, 0.3048, units, dam, params, P, dv, n_left,
                                              100.0, c, dict(hm.DEFAULTS), inflow_m=P + 300 * dv,
                                              wet_xy=wet, wet_cell=125.0)
            dev = hm.point_segment_distance(line[:, 0], line[:, 1], ref_line).max()
            assert dev < 60.0, (P, dev)
            assert rec["top_offset"] > 3.0, rec["top_offset"]
            assert np.hypot(*(np.asarray(rec["breach_xy"]) - v2e)) < 150.0, (
                rec["breach_xy"], rec["breach_detail"])
            assert rec["breach_detail"]["crest_band"][0] < 1063.2 < 1066.0 < \
                rec["breach_detail"]["crest_band"][1], rec["breach_detail"]
            assert rec["off_rule"] in ("datum", "snap") and rec["flow_path_angle_deg"] < 45.0
            assert rec["evidence"]["parallel_ridge_frac"] <= 0.25, rec["evidence"]
        assert rec.get("snapped_to") and rec["off_rule"] == "snap", rec
        assert abs(rec["dam_point_to_line"] - 80.0) < 25.0, rec["dam_point_to_line"]
        # without the run's flooded area no line off the old rule is tried: its error stands
        try:
            hm.crest_dam_line(dem, srs, 0.3048, units, dam, params, P0, dd,
                              np.array([-dd[1], dd[0]]), 100.0, c, dict(hm.DEFAULTS))
        except hc.CrestTraceError as e:
            assert "rises above the top of dam" in str(e) and "tried" not in str(e), e
        else:
            raise AssertionError("traced off the record's level without the flooded area")
        # on a DEM coarser than the crest width (32.8 ft cells, a 20 ft crest): none either
        try:
            hm.crest_dam_line(dem, srs, 0.3048, units, dam, params, P0, dd,
                              np.array([-dd[1], dd[0]]), 100.0, 32.8, dict(hm.DEFAULTS),
                              wet_xy=wet, wet_cell=125.0)
        except hc.CrestTraceError as e:
            assert "coarser than the crest width on record" in str(e), e
        else:
            raise AssertionError("traced off the old rule on a DEM coarser than the crest")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- build_model on a synthetic site --------------------------------------------------------
def _site(folder, H_ft=16.0, cw_ft=40.0, half_m=250.0, P=(0.0, 0.0), skew=0.0, roads=(),
          pool_ft=None, top_off_ft=0.0, dx=10.0, notch=None, len_ft=None):
    """A finished Runner run on a synthetic valley (UTM 14N metres, flow to the south): an
    embankment along y = Y0 (crest cw_ft wide at 310 m, 3:1 faces) for |x - X0| <= half_m,
    abutments rising beyond, road embankments (x0, y0, x1, y1 (m from X0, Y0), z_m, w_m; 2:1
    sides), the pool's water surface pool_ft below the crest, a crest notch (x, w, depth_ft),
    a flooded strip below the dam; the record's top of dam top_off_ft from the DEM crest; the
    dam point P (m from X0, Y0) and the inflow point 60 m beyond it, skew degrees off south."""
    import json
    import math
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core import hecras
    from nd_breach_runner.tests import test_hecras_p0 as p0
    X0, Y0, FT = p0.X0, p0.Y0, 0.3048
    Hm, C = H_ft * FT, 310.0
    x0, x1, y0, y1 = X0 - 1500, X0 + 1500, Y0 - 3000, Y0 + 1500
    nx, ny = int((x1 - x0) / dx), int((y1 - y0) / dx)
    X, Y = np.meshgrid(x0 + (np.arange(nx) + 0.5) * dx, y1 - (np.arange(ny) + 0.5) * dx)
    ax = np.abs(X - X0)
    z = C - Hm + 0.002 * (Y - Y0) + 0.004 * ax - np.where(ax < 12, 1.0 - ax / 12, 0.0)
    if pool_ft is not None:
        z = np.where(Y > Y0, np.maximum(z, C - pool_ft * FT), z)
    crest = np.full(X.shape, C)
    if notch is not None:
        crest = crest - np.clip(notch[2] * FT - np.maximum(np.abs(X - X0 - notch[0]) -
                                                           0.5 * notch[1], 0.0), 0.0, None)
    emb = crest - np.maximum(np.abs(Y - Y0) - 0.5 * cw_ft * FT, 0.0) / 3.0
    z = np.where(ax <= half_m + 3 * Hm + 50, np.maximum(z, emb), z)
    z = np.where(ax > half_m, np.maximum(z, C - 0.6 + (ax - half_m) * 0.15), z)
    for rx0, ry0, rx1, ry1, rz, rw in roads:
        rd = _seg_dist(X - X0, Y - Y0, [(rx0, ry0), (rx1, ry1)])
        z = np.maximum(z, rz - np.maximum(rd - 0.5 * rw, 0.0) / 2.0)
    dem = os.path.join(folder, "dem.tif")
    p0._tif(dem, z, x0, y1, dx)
    depth = np.where((Y < Y0 - 20) & (Y > Y0 - 2800) & (ax < 400.0), 1.5, -9999.0)
    p0._tif(os.path.join(folder, "Depth (Max).tif"), depth, x0, y1, dx)
    dam = br.DamData(dam_id="SYN-1", dam_height_ft=H_ft, storage_top_acft=300.0,
                     top_of_dam_ft=C / FT + top_off_ft, name="Synthetic", crest_width_ft=cw_ft,
                     crest_length_ft=len_ft or 2 * half_m / FT)
    m = os.path.join(folder, hecras.MODEL_FOLDER)
    os.makedirs(m)
    hy = br.make_hydrograph(dam, br.METHOD_PIPING)
    hy.to_csv(os.path.join(m, hecras.HYDROGRAPH_CSV))
    Pw = (X0 + P[0], Y0 + P[1])
    a = math.radians(skew)
    L = 60.0 + max(P[1], 0.0)
    rep = {"tool": "test", "dam": br.dam_to_dict(dam), "dam_xy": list(Pw),
           "hydrograph": hy.summary(), "scenario": "sunny", "work_crs": "EPSG:26914",
           "dem_z_to_m": 1.0, "dem_elevation_units": "metres",
           "settings": {"cell_size_m": 20.0, "manning_n": 0.06, "input_files": {"DEM": dem}},
           "summary": {"latest_arrival_hr": 2.0, "max_velocity_fps": 10.0,
                       "max_depth_ft": 5.0},
           "inflow_point": {"xy": [Pw[0] + L * math.sin(a), Pw[1] - L * math.cos(a)],
                            "how": "test"}}
    with open(os.path.join(m, hecras.RUN_REPORT_JSON), "w", encoding="utf-8") as fh:
        json.dump(rep, fh)
    return np.array(Pw)


def _local(r, xy):
    """Model-CRS points of a build_model result in the site's metres from (X0, Y0)."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    to = hm.transformer(hm.srs_from(r["info"]["crs"]), hm.srs_from("EPSG:26914"))
    return to(np.asarray(xy, float).reshape(-1, 2)) - (p0.X0, p0.Y0)


@test
def hecras_crest_offcrest_build_model_snap_and_its_warning():
    """(e) A dam point on the pool 30 m upstream of the crest, the inflow guessed 60 degrees
    off: the dam line is traced from the crest found nearby, along the crest, and the note
    about it is a WARNING with the distance and the angle to the run's flow."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        _site(d, P=(0.0, 30.0), skew=60.0, pool_ft=4.0)
        r = hm.build_model(d, out_folder=os.path.join(d, "out"), options={"allow_bad_site": True})
        ax = r["info"]["dam_axis"]
        assert ax["used"] == "crest" and ax.get("snapped_to"), ax
        cl = _local(r, r["model"].conn_line)
        assert np.abs(cl[:, 1]).max() < 15.0 and np.ptp(cl[:, 0]) > 300.0, cl
        notes = [n for n in r["notes"] if n.startswith("WARNING: the dam line is not the old")]
        assert len(notes) == 1 and "from crest-high ground" in notes[0], r["notes"]
        # the true distance from the dam point to the line (V6), the angle to the flow path
        dist = "It lies {:,.0f} ft from the dam point".format(ax["dam_point_to_line"])
        assert dist in notes[0] and 80.0 < ax["dam_point_to_line"] < 110.0, (notes, ax)
        assert "degrees off square to the flow path below the breach" in notes[0], notes
        assert ax["flow_path_angle_deg"] < 20.0 and ax["off_rule"] == "snap", ax
        assert ax["evidence"]["flow"]["downstream_frac"] > 0.9, ax["evidence"]
        # the cut follows the line: the dam point moved to its nearest point on it
        mv = _local(r, ax["dam_point_moved_to"])[0]
        assert abs(mv[1]) < 15.0 and abs(mv[0]) < 5.0, mv
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_crest_offcrest_build_model_a_road_along_the_valley_is_not_the_dam():
    """H1: a dam point 45 m downstream of the crest, the inflow 30 degrees off, a road
    embankment 0.3 m below the crest running down the valley 30 m to the side: the line along
    the road does not cross the run's flow, so the straight line through the dam point is
    used with the WARNING, which keeps the first trace's reason and says a start from other
    high ground was tried (it used to become a 90-degree dam line, the 2D area halved)."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        Pw = _site(d, P=(10.0, -45.0), skew=30.0,
                   roads=[(30.0, -30.0, 30.0, -1500.0, 310.0 - 0.3, 24.0)])
        r = hm.build_model(d, out_folder=os.path.join(d, "out"), options={"allow_bad_site": True})
        ax = r["info"]["dam_axis"]
        assert ax["used"] == "straight" and ax.get("fallback"), ax
        assert ax.get("snap_tried") and ax["why"].startswith("no embankment crest"), ax
        assert "was tried: the line it gives is not clearly the dam" in ax["why"], ax
        assert not any(n.startswith("WARNING: the dam line is not") for n in r["notes"])
        cl = _local(r, r["model"].conn_line)
        P = Pw - (p0.X0, p0.Y0)
        u = (cl[-1] - cl[0]) / np.hypot(*(cl[-1] - cl[0]))
        assert abs(float(np.cross(u, P - cl[0]))) < 2.0, (cl[[0, -1]], P)
        assert abs(hc.angle_deg(u, (np.cos(np.radians(30.0)), np.sin(np.radians(30.0))))) < 2.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_crest_offcrest_build_model_notch_breach_and_datum_note():
    """F2 / L2: the record 4 ft below the DEM crest (a local datum) and a 4 ft deep, 30 m wide
    notch in the crest 100 m from the old channel: the crest is traced at the DEM's level and
    the breach stays at the channel (within about a cell), not in the notch; the WARNING says
    the weir follows the higher of the terrain and the top of dam on record."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        _site(d, H_ft=20.0, cw_ft=20.0, half_m=150.0, top_off_ft=-4.0, dx=5.0,
              notch=(100.0, 30.0, 4.0), len_ft=1250.0)
        r = hm.build_model(d, out_folder=os.path.join(d, "out"), options={"allow_bad_site": True})
        ax = r["info"]["dam_axis"]
        assert ax["used"] == "crest" and abs(ax["top_offset"] - 4.0) < 0.6, ax
        b = _local(r, ax["breach_xy"])[0]
        assert abs(b[0]) < float(r["info"]["cell"]) * 0.3048 + 5.0, (b, ax["breach_detail"])
        head = "WARNING: the dam line is not the old rule's"
        note = [n for n in r["notes"] if n.startswith(head)]
        assert len(note) == 1 and "The weir follows the higher of the terrain" in note[0], note
        assert "at the DEM's crest level, {:.1f} ft above the top of dam on record".format(
            ax["top_offset"]) in note[0], note
        assert "or another embankment" in note[0] and ax["off_rule"] == "datum", note
        assert "It lies {:,.0f} ft from the dam point".format(ax["dam_point_to_line"]) in note[0]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_crest_offcrest_build_model_your_dam_line_reframes_the_pool():
    """M2: a dam line of your own (DAM_LINE) along the crest with the dam point 30 m upstream
    on the pool and the inflow guessed 60 degrees off: the reservoir outline and the storage
    check are built from that line's frame, so the outline does not overlap the 2D area."""
    from osgeo import ogr
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return

    def poly(pts):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in list(pts) + [pts[0]]:
            ring.AddPoint_2D(float(x), float(y))
        g = ogr.Geometry(ogr.wkbPolygon)
        g.AddGeometry(ring)
        return g

    d = tempfile.mkdtemp()
    try:
        _site(d, P=(0.0, 30.0), skew=60.0, pool_ft=6.0)
        line = [(p0.X0 - 250.0, p0.Y0), (p0.X0 + 250.0, p0.Y0)]
        r = hm.build_model(d, out_folder=os.path.join(d, "out"), options={
            "allow_bad_site": True, "dam_line_xy": line, "dam_line_crs": "EPSG:26914"})
        assert r["info"]["dam_axis"]["source"] == "your dam line", r["info"]["dam_axis"]
        m = r["model"]
        ov = poly(m.perimeter).Intersection(poly(m.reservoir)).GetArea() * 0.3048 ** 2 / 4046.86
        assert ov < 0.5, ov
        res = _local(r, m.reservoir)
        assert res[:, 1].min() > -10.0, res[:, 1].min()          # the pool is upstream
        # V5: the dam point moves to its nearest point on the line (not the nearest vertex)
        mv = _local(r, r["info"]["dam_axis"]["dam_point_moved_to"])[0]
        assert np.hypot(*mv) < 1.0, mv
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_crest_offcrest_build_model_a_failed_snap_keeps_the_dam_point():
    """L3: when a later step fails after a snap (the snapped crest is outside the 2D area),
    the straight line goes through the dam point as given along the axis as guessed, and the
    snap's note does not stay; the reason keeps the first trace's error."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    real = hm.crest_dam_line

    def far_snap(*a, **k):
        line, sc, rec = real(*a, **k)
        far = line + 1.0e6
        rec = dict(rec, snapped_from=[float(v) for v in a[6]], snapped_to=far[0].tolist(),
                   snap_distance=1.0e6, snap_turn_deg=45.0, off_rule="snap",
                   off_rule_reason="test: first trace", snap_reason="test: first trace",
                   flow_path_angle_deg=0.0)
        return far, sc, rec
    try:
        Pw = _site(d)
        r0 = hm.build_model(d, out_folder=os.path.join(d, "a"), options={"allow_bad_site": True,
                                                                         "dam_axis": "straight"})
        hm.crest_dam_line = far_snap
        try:
            r = hm.build_model(d, out_folder=os.path.join(d, "b"),
                               options={"allow_bad_site": True})
        finally:
            hm.crest_dam_line = real
        ax = r["info"]["dam_axis"]
        assert ax["used"] == "straight" and ax["why"].startswith("test: first trace; a start "
                                                                 "from crest-high ground"), ax
        assert "was tried: the traced crest is outside the 2D area" in ax["why"], ax
        assert ax.get("snap_tried") and not ax.get("dam_point_moved_to"), ax
        assert not any(n.startswith("WARNING: the dam line is not") for n in r["notes"])
        a0 = np.asarray(r0["model"].conn_line, float)
        a1 = np.asarray(r["model"].conn_line, float)
        assert np.abs(a0[[0, -1]] - a1[[0, -1]]).max() < 1.0, (a0[[0, -1]], a1[[0, -1]])
        assert Pw is not None
    finally:
        shutil.rmtree(d, ignore_errors=True)
