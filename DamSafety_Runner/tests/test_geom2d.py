"""audit R1 (2026-11-07): core/geom2d.cross2, the 2-vector cross product numpy 2 took away
(np.cross on two 2-vectors raises under numpy >= 2.0; numpy 1.x returned the z-component). The
HEC-RAS crest walk (hecras_crest._walk, its junction stop) and the weir-gap merge
(hecras_structures.merge_gap) use it: both are exercised here with no GDAL, so the test runs
under whichever numpy the QGIS at hand ships, and fails the way the crest tests failed if a
bare np.cross comes back."""
import numpy as np

from nd_breach_runner.core import hecras_crest as hc
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import hecras_structures as hs
from nd_breach_runner.core import structures as sm
from nd_breach_runner.core.geom2d import cross2
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_hecras_crest import H, CW, TOP, _grid_sampler
from nd_breach_runner.tests.test_hecras_crest_offcrest import TOE, _flat_floor


@test
def geom2d_cross2_is_the_z_component_for_arrays_lists_and_tuples():
    # the numbers numpy 1.x's np.cross gave for 2-vectors
    assert cross2((1.0, 0.0), (0.0, 1.0)) == 1.0
    assert cross2((0.0, 1.0), (1.0, 0.0)) == -1.0
    assert cross2([3.0, 4.0], [3.0, 4.0]) == 0.0
    assert cross2(np.array([2.0, 1.0]), np.array([-1.0, 3.0])) == 7.0
    assert cross2(np.array([2, 1]), [0.5, 0.5]) == 0.5          # ints and a list
    assert cross2(np.array([[2.0, 1.0]]), (0.0, 1.0)) == 2.0     # a (1, 2) row
    v = cross2(np.array([1.0, 2.0]), np.array([3.0, 4.0]))
    assert isinstance(v, float) and v == -2.0
    # against numpy's own 3-D cross product, which both numpy 1 and 2 keep
    rng = np.random.default_rng(7)
    for _ in range(50):
        a, b = rng.normal(size=2), rng.normal(size=2)
        z = np.cross(np.append(a, 0.0), np.append(b, 0.0))[2]
        assert abs(cross2(a, b) - float(z)) < 1e-12
    # the distance-from-a-line use: |cross2(unit, P - A)| is the perpendicular distance
    u = np.array([1.0, 1.0]) / np.sqrt(2.0)
    assert abs(abs(cross2(u, np.array([0.0, 2.0]))) - np.sqrt(2.0)) < 1e-12
    for bad in (((1.0, 2.0, 3.0), (1.0, 2.0)), ((1.0,), (1.0, 2.0))):
        try:
            cross2(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError("cross2 accepted {}".format(bad))


@test
def geom2d_crest_walk_junction_stop_runs_under_this_numpy():
    """The junction stop in hecras_crest._walk (its 'another crest at its level' branch, the
    only place the walk projects points on a heading) under the numpy at hand: the three
    offcrest crest tests died here on numpy 2 before audit R1."""
    d2 = np.array([np.cos(np.radians(40.0)), np.sin(np.radians(40.0))])
    g = _flat_floor(dams=[(400.0, 0.0, 0.0, 0.0, TOP + 5.0, CW),
                          (0.0, 0.0, 400.0 * d2[0], 400.0 * d2[1], TOP + 5.0, CW)])
    tr = hc.trace(_grid_sampler(*g), (250.0, 0.0), (-1.0, 0.0), TOP, H, 10.0, TOE, 1500.0,
                  300.0, crest_w=CW)
    assert tr["ends"]["B"][0].startswith("a junction"), tr["ends"]
    assert len(tr["line"]) >= 2 and np.isfinite(tr["line"]).all()


@test
def geom2d_merge_gap_measures_off_line_distance_under_this_numpy():
    """hecras_structures.merge_gap: a bridge on a culvert's road becomes a gap in that
    connection's weir when its crest is within a cell of the connection line (the distance
    is |cross2(t, C - A)|); one two cells off stays its own connection (None)."""
    road_z, floor = 1006.0, 1000.0

    def ground(xy):
        xy = np.atleast_2d(np.asarray(xy, float))
        dy = np.abs(xy[:, 1])
        return np.clip(road_z - np.maximum(dy - 15.0, 0.0) / 3.0, floor, road_z)

    units = hm.Units(si=False)
    cell, step = 100.0, 16.0
    # a culvert connection already placed along the road (y = 0), weir level at the road
    conn = {"name": "culvert 1", "line": [(-600.0, 0.0), (600.0, 0.0)],
            "weir": [(float(s), road_z) for s in np.arange(0.0, 1201.0, 50.0)], "notes": []}
    bridge = sm.Structure(kind="bridge", line=np.array([[0.0, 120.0], [0.0, -120.0]]) * 0.3048,
                          width_m=30.0 * 0.3048, barrels=1, name="bridge A")
    line_u = np.array([[0.0, 120.0], [0.0, -120.0]])        # crosses the road at (0, 0)
    host = hs.merge_gap([conn], bridge, line_u, ground, step, units, cell)
    assert host == "culvert 1", host
    gap = conn["gaps"][0]
    assert gap["structure"] == "bridge A" and abs(gap["station"] - 600.0) < step, gap
    # off_line is |cross2(t, C - A)|: the crest sample's offset from the road line, within
    # the sampling step (the level top is sampled every `step`, its middle taken)
    assert gap["off_line"] <= step and abs(gap["width"] - 30.0) < 1e-6, gap
    assert min(z for _, z in conn["weir"]) < road_z - 1.0, conn["weir"]
    # a crossing two cells away from the connection line is not merged into it
    far = {"name": "culvert 2", "line": [(-600.0, 250.0), (600.0, 250.0)],
           "weir": [(float(s), road_z) for s in np.arange(0.0, 1201.0, 50.0)], "notes": []}
    assert hs.merge_gap([far], bridge, line_u, ground, step, units, cell) is None
