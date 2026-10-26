"""3.10.87: the crest trace on DWR-2801 Absaraka, where it fell back to the straight dam line.

(1) The DEM's crest (1066-1069 ft) is more than the tolerance above the top of dam on record
(1063.2 ft, a local datum): an embankment that stands above the ground on both sides is
traced at the DEM's crest level when it is no more than max(5 ft, 25 % of H) above the record,
instead of being called an abutment. (2) The dam.json point is on the pool, about 100 ft from
the crest, and the axis guessed from the run's inflow point is 60-65 degrees off the
embankment, so no ridge lay across it: hecras_crest.snap finds the nearest crest-high ground
and the embankment's direction, and the trace starts there."""
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
