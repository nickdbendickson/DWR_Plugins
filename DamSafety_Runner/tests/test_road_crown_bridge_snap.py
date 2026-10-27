"""3.10.90 (DWR-3034 review): road overtopping depth on the road centreline at the road
surface, structure cuts and bare-earth openings read on a deck stand-in (core/road_profile),
and inventory bridges checked against the road they carry and moved to its opening / channel
crossing only on evidence (core/bridge_snap; the review's items B1-B7). Tiny synthetic data
only."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

FT = 0.3048
ROAD_M = 100.0


# ------------------------------------------------------------------ synthetic pieces
class _Terrain:
    """read(bounds, cell) -> Grid of fn(X, Y) (as tests/synthetic.SyntheticTerrain)."""

    def __init__(self, fn):
        self.fn, self.reads = fn, []

    def read(self, bounds, cell):
        from nd_breach_runner.core.grid import Grid
        xmin, ymin, xmax, ymax = bounds
        nx = max(1, int(round((xmax - xmin) / cell)))
        ny = max(1, int(round((ymax - ymin) / cell)))
        xs = xmin + (np.arange(nx) + 0.5) * cell
        ys = ymax - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        self.reads.append((bounds, cell))
        return Grid(self.fn(X, Y), xmin, ymax, cell)


def _road_with_creek(X, Y, creek_x=155.0, half=15.0, depth=6.0, grade=0.0):
    """An east-west road embankment (y = 0) at ROAD_M (+ grade * x) over 90 m ground, with a
    creek opening `depth` m deep, 2 * half m wide at x = creek_x."""
    z = np.full(np.shape(X), 90.0)
    on = np.abs(Y) <= 10.0
    z = np.where(on, ROAD_M + grade * X, z)
    if depth:
        z = np.where(on & (np.abs(X - creek_x) <= half), ROAD_M - depth, z)
    return z


ROAD = np.array([[-400.0, 0.0], [400.0, 0.0]])


def _bridge(x, name, y0=-15.0, y1=15.0, w=30.0, carries=""):
    from nd_breach_runner.core import structures as sm
    b = sm.Structure(kind=sm.KIND_BRIDGE, line=np.array([[x, y0], [x, y1]]), width_m=w,
                     height_m=5.0, name=name, invert_up_m=95.0, invert_dn_m=95.0)
    b.carries = carries
    return b


def _valley(X, Y, creeks=(155.0,), openings=((155.0, 15.0),), crown=ROAD_M, slope=0.004,
            channel=1.5, grade=0.0, beds=None, road_y=(0.0,), half=10.0):
    """A valley whose floor (90 m) falls `slope` toward the nearest creek, each creek a
    `channel` m deep, 8 m wide channel running north-south; east-west road embankments at
    `road_y` (crown + grade * x, `half` m each side); `openings` (x, half width): the bare
    earth under a bridge deck (no deck in the LiDAR) on every road, at the floor or at
    beds[i]."""
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    d = np.min([np.abs(X - cx) for cx in creeks], axis=0) if creeks else np.full(X.shape, 400.0)
    floor = 90.0 + slope * np.minimum(d, 400.0) - np.where(d < 4.0, channel, 0.0)
    z = floor.copy()
    for ry in road_y:
        on = np.abs(Y - ry) <= half
        z = np.where(on, crown + grade * X, z)
        for i, (ox, h) in enumerate(openings):
            bed = floor if beds is None or beds[i] is None else np.full(X.shape, beds[i])
            z = np.where(on & (np.abs(X - ox) <= h), bed, z)
    return z


def _snap(structs, fn, roads=(ROAD,), streams=(), rails=()):
    from nd_breach_runner.core import bridge_snap as bs
    return bs.snap_bridges(structs, list(roads), _Terrain(fn).read, list(streams),
                           radius_m=300.0, rails=list(rails))


def _row(rows, name):
    return [r for r in rows if r["name"] == name][0]


# ------------------------------------------------------------------ (b) bridge snap
@test
def bridge_snap_moves_a_misplaced_nbi_cut_to_the_road_opening_and_logs_the_distance():
    """DWR-3034: the NBI cut on solid fill 190 m from Square Butte Creek's opening (the
    valley falls to the creek; the opening's bare-earth bed is well below the ground beside the
    road at the cut) moves onto the creek, with a run warning naming the move."""
    from nd_breach_runner.core import bridge_snap as bs
    t = _Terrain(_valley)
    b = _bridge(-35.0, "0025008224")              # 190 m from the creek, as on DWR-3034
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    r = rows[0]
    assert r["method"] == "dem_opening", r
    assert abs(r["moved_m"] - 190.0) < 6.0, r
    mid = b.line.mean(axis=0)
    assert abs(mid[0] - 155.0) < 6.0 and abs(mid[1]) < 2.0, b.line
    assert abs(b.line[0][0] - b.line[-1][0]) < 1.0           # still across the road
    assert b.line[0][1] < b.line[-1][1]                       # same direction as given
    assert b.invert_up_m is None and b.invert_dn_m is None    # cleared at the new place
    assert any("0025008224" in n and "moved" in n and str(int(round(r["moved_m"]))) in n
               for n in notes), notes
    # B7: every move is a run warning with the name, from, to, distance and method
    assert len(warns) == 1, warns
    w = warns[0]
    assert "0025008224" in w and "moved 190 m" in w and "dem_opening" in w, w
    assert "[-35.0, 0.0]" in w and str(r["new_xy"]) in w, w
    assert r["verified"], r
    assert t.reads                                            # read the DEM, nothing else
    # snapped again (a re-run on the moved line): kept, verified
    rows2, _n, warns2 = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    assert rows2[0]["method"] == "kept" and rows2[0]["verified"] and not warns2, rows2


@test
def bridge_snap_keeps_a_cut_already_at_the_opening():
    from nd_breach_runner.core import bridge_snap as bs
    t = _Terrain(_road_with_creek)
    b = _bridge(158.0, "on the creek")
    before = b.line.copy()
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    assert rows[0]["method"] == "kept" and rows[0]["moved_m"] == 0.0, rows
    assert np.allclose(b.line, before) and b.invert_up_m == 95.0
    assert not warns


@test
def bridge_snap_follows_a_channel_crossing_when_the_dem_shows_no_opening():
    from nd_breach_runner.core import bridge_snap as bs
    t = _Terrain(lambda X, Y: _road_with_creek(X, Y, depth=0.0, grade=0.01))
    stream = np.array([[100.0, -200.0], [100.0, 200.0]])
    b = _bridge(0.0, "channel")
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [stream], radius_m=300.0)
    r = rows[0]
    assert r["method"] == "channel_crossing", r
    assert abs(r["moved_m"] - 100.0) < 3.0, r
    assert abs(b.line.mean(axis=0)[0] - 100.0) < 3.0, b.line
    assert len(warns) == 1 and "channel" in warns[0] and "moved 100 m" in warns[0], warns


@test
def bridge_snap_falls_back_with_a_warning_when_no_crossing_is_found():
    from nd_breach_runner.core import bridge_snap as bs
    # graded road, no opening, no stream: nothing to snap to
    t = _Terrain(lambda X, Y: _road_with_creek(X, Y, depth=0.0, grade=0.01))
    b = _bridge(0.0, "nowhere")
    before = b.line.copy()
    rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [], radius_m=300.0)
    assert rows[0]["method"] == "none" and np.allclose(b.line, before), rows
    assert len(warns) == 1 and "nowhere" in warns[0] and "inventory location" in warns[0]
    # a bridge far from any road; and a run without road lines
    far = _bridge(0.0, "far away", 500.0, 530.0)
    rows, notes, warns = bs.snap_bridges([far], [ROAD], t.read, [], radius_m=300.0)
    assert rows[0]["method"] == "none" and "far away" in warns[0]
    rows, notes, warns = bs.snap_bridges([_bridge(0.0, "x")], [], t.read, [], radius_m=300.0)
    assert not rows and len(warns) == 1 and "no road or rail lines" in warns[0]


@test
def bridge_snap_leaves_culverts_and_guesses_alone_and_can_be_turned_off():
    from nd_breach_runner.core import bridge_snap as bs
    from nd_breach_runner.core import structures as sm
    t = _Terrain(_valley)
    c = sm.Structure(kind=sm.KIND_CULVERT if hasattr(sm, "KIND_CULVERT") else "culvert",
                     line=np.array([[-35.0, -15.0], [-35.0, 15.0]]), name="cv")
    g = _bridge(-35.0, "guess")
    g.source = "guess"
    rows, notes, warns = bs.snap_bridges([c, g], [ROAD], t.read, [], radius_m=300.0)
    assert rows == [] and not warns
    old = os.environ.get("NDBREACH_BRIDGE_SNAP_M")
    os.environ["NDBREACH_BRIDGE_SNAP_M"] = "0"
    try:
        b = _bridge(-35.0, "off")
        rows, notes, warns = bs.snap_bridges([b], [ROAD], t.read, [])
        assert rows == [] and b.line[0][0] == -35.0 and "off" in notes[0]
    finally:
        if old is None:
            os.environ.pop("NDBREACH_BRIDGE_SNAP_M", None)
        else:
            os.environ["NDBREACH_BRIDGE_SNAP_M"] = old


@test
def bridge_snap_runs_in_the_runner_before_the_inverts_are_read():
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "core",
                            "workflow.py"), encoding="utf-8").read()
    i = src.index("bridge_snap.check_run(")
    assert i < src.index("reconcile_inverts(st.structures") < src.index("key_settings(st),")
    assert 'report["bridge_snap"]' in src and "bridge_snap.report_lines(" in src


def _moved(b, before):
    return not np.allclose(np.asarray(b.line), before)


def _sag_valley(X, Y):
    """A 3 % road sag bottoming at x = 0 (a deck in the LiDAR there) over a valley falling to
    a creek at x = 150 with an open bridge (no deck) there."""
    z = _valley(X, Y, creeks=(150.0,), openings=((150.0, 6.0),), slope=0.01)
    on = (np.abs(Y) <= 10.0) & (np.abs(X - 150.0) > 6.0)
    return np.where(on, ROAD_M - 8.0 + 0.03 * np.minimum(np.abs(X), 120.0), z)


@test
def bridge_snap_b1_a_correct_bridge_is_not_moved_away_from_its_crossing():
    """3.10.90 review (bridges F1): the keep tests run BEFORE any target is chosen. A bridge
    whose deck (or a hydro-flattened water surface) is in the LiDAR, a weak or a wide opening,
    no-data at the bridge, the road's low point or a channel line at the bridge: the cut stays
    where the inventory put it, however good another opening within 300 m looks; one moves only
    from solid embankment."""
    flat_other = lambda X, Y: _road_with_creek(X, Y, creek_x=150.0, half=5.0,     # noqa: E731
                                               depth=4.0)
    cases = {
        # deck in the LiDAR, flat ground, another stream's opening at 150 (auditor case 1)
        "deck, flat": (flat_other, ()),
        # deck in the LiDAR over its creek (the channel beside the road), and another creek's
        # opening, lower, 150 m away
        "deck, valley": (lambda X, Y: _valley(X, Y, creeks=(0.0, 150.0),
                                              openings=((150.0, 6.0),)), ()),
        # a weak opening (1.2 m) at the bridge and a 4 m one at 150 (auditor case 2)
        "weak": (lambda X, Y: np.where((np.abs(Y) <= 10.0) & (np.abs(X) <= 4.0), 98.8,
                                       _valley(X, Y, creeks=(150.0,), openings=((150.0, 6.0),),
                                               slope=0.01)), ()),
        # no-data (water) under the bridge (auditor case N)
        "nodata": (lambda X, Y: np.where((np.abs(X) <= 10.0) & (np.abs(Y) <= 30.0), np.nan,
                                         _valley(X, Y, creeks=(150.0,), slope=0.01)), ()),
        # the road's low point: a 3 % sag bottoming at the deck in the LiDAR
        "sag": (_sag_valley, ()),
        # a channel line crosses the road at the bridge; a DEM opening 150 m away
        "channel line": (lambda X, Y: _valley(X, Y, creeks=(150.0,), openings=((150.0, 6.0),),
                                              slope=0.01),
                         (np.array([[0.0, -300.0], [0.0, 300.0]]),
                          np.array([[150.0, -300.0], [150.0, 300.0]]))),
    }
    for label, (fn, streams) in cases.items():
        b = _bridge(0.0, label)
        before = b.line.copy()
        rows, notes, warns = _snap([b], fn, streams=streams)
        r = rows[0]
        assert not _moved(b, before) and r["method"] not in ("dem_opening",
                                                             "channel_crossing"), (label, r)
        assert b.invert_up_m == 95.0, label
        if not r["verified"]:            # kept for want of evidence: said in a warning
            assert len(warns) == 1 and label in warns[0] and "inventory location" in warns[0], \
                (label, warns)
    # a long bridge over a wide (100 m) opening, and a short span over a 200 m one: the
    # shoulder window grows with the span, and the grade line sees the rest
    for label, w, half in (("wide, long bridge", 100.0, 50.0), ("wider, short span", 10.0, 100.0)):
        fn = lambda X, Y, h=half: _valley(X, Y, creeks=(0.0, 250.0),                  # noqa
                                          openings=((0.0, h), (250.0, 6.0)), channel=0.0)
        b = _bridge(0.0, label, w=w)
        before = b.line.copy()
        rows, notes, warns = _snap([b], fn)
        assert rows[0]["method"] == "kept" and not _moved(b, before), (label, rows[0])


@test
def bridge_snap_b2_road_pieces_are_merged_and_a_road_end_is_not_judged():
    """3.10.90 review (bridges F2): a road layer split at the bridge (an OSM bridge way) or at
    an intersection is merged (end points within 1 m) before the profile is read, so the
    opening at the split is seen; a crossing near an end that nothing continues cannot be
    judged and the cut stays (with a warning)."""
    from nd_breach_runner.core import bridge_snap as bs
    fn = lambda X, Y: _valley(X, Y, creeks=(0.0, -200.0), openings=((0.0, 8.0), (-200.0, 6.0)))  # noqa
    W = np.array([[-400.0, 0.0], [0.0, 0.0]])
    E = np.array([[400.0, 0.0], [0.0, 0.0]])                 # digitised the other way
    for label, x, pieces in (("split at the bridge", -2.0, (W, E)),
                             ("split 10 m west", -12.0, (np.array([[-400.0, 0.0], [-10.0, 0.0]]),
                                                         np.array([[-10.0, 0.0], [400.0, 0.0]])))):
        b = _bridge(x, label)
        before = b.line.copy()
        rows, notes, warns = _snap([b], fn, roads=pieces)
        assert rows[0]["method"] == "kept" and rows[0]["verified"] and not _moved(b, before), \
            (label, rows[0])
        assert not warns, warns
    # the merge itself: three pieces, out of order and in both directions
    parts = [np.array([[100.0, 0.0], [300.0, 5.0]]), np.array([[-200.0, 0.0], [0.0, 0.0]]),
             np.array([[100.0, 0.0], [0.0, 0.0]])]
    line, open_a, open_b, used = bs.merged_line(1, parts)
    assert sorted(used) == [0, 1, 2] and open_a and open_b, (line, used)
    assert np.allclose(line[0], [-200.0, 0.0]) and np.allclose(line[-1], [300.0, 5.0]), line
    # a cross road at the end (a 90 degree turn) is not a continuation
    line, _a, open_b, used = bs.merged_line(0, [np.array([[0.0, 0.0], [100.0, 0.0]]),
                                                np.array([[100.0, 0.0], [100.0, 200.0]])])
    assert sorted(used) == [0] and open_b
    # a 5 m gap in the road layer 5 m from a misplaced bridge (DWR-3034-like valley): the
    # profile cannot be judged there, so the cut stays, with a warning
    b = _bridge(-30.0, "gap")
    before = b.line.copy()
    rows, notes, warns = _snap([b], _valley, roads=(np.array([[-400.0, 0.0], [-40.0, 0.0]]),
                                                    np.array([[-35.0, 0.0], [400.0, 0.0]])))
    assert rows[0]["method"] == "none" and not _moved(b, before), rows[0]
    assert len(warns) == 1 and "gap" in warns[0] and "ends" in warns[0], warns


@test
def bridge_snap_b3_rail_bridges_stay_on_the_railway():
    """3.10.90 review (bridges F3): a rail bridge (NBI 42A = 2, runner_culverts placed on /
    meant for rail, rail in the facility carried) is matched to rail lines only; with none in
    the run it is not checked. A bridge line crossing no centreline matches one only when its
    midpoint is on that line's crown."""
    from nd_breach_runner.core import bridge_snap as bs

    def frail(X, Y):
        z = _valley(X, Y, creeks=(0.0, 200.0), openings=((200.0, 6.0),), half=8.0)
        rail = np.abs(Y + 30.0) <= 4.0
        return np.where(rail, np.where(np.abs(X) <= 6.0, 88.5, 99.0), z)  # rail bridge at x=0
    RAIL = np.array([[-400.0, -30.0], [400.0, -30.0]])
    for carries in ("rail", ""):
        b = _bridge(0.0, "RAIL_BR " + carries, -15.0, -45.0, w=12.0, carries=carries)
        before = b.line.copy()
        rows, notes, warns = _snap([b], frail)                   # no rail lines in the run
        assert rows[0]["method"] == "none" and not _moved(b, before), rows[0]
        assert len(warns) == 1 and "RAIL_BR" in warns[0], warns
        if carries == "rail":
            assert "rail" in rows[0]["note"], rows[0]
        rows, notes, warns = _snap([b], frail, rails=(RAIL,))    # with the railway: kept there
        assert rows[0]["method"] == "kept" and rows[0]["verified"], rows[0]
        assert rows[0]["line"] == "rail", rows[0]
    # a road bridge is not matched to a railway
    b = _bridge(0.0, "ROAD_BR", -15.0, -45.0, w=12.0, carries="road")
    rows, notes, warns = _snap([b], frail, roads=(), rails=(RAIL,))
    assert rows[0]["method"] == "none", rows[0]
    # the facility kind from the inventory's own fields
    fk = bs.facility_kind
    assert fk({"SERVICE_ON_042A": "2"}) == "rail" and fk({"service_on_042a": 1}) == "road"
    assert fk({"servtypondesc": "2 Railroad"}) == "rail"
    assert fk({"embankment": "road", "notes": "no rail line crosses a stream here: placed at a "
               "road crossing"}) == "rail"
    assert fk({"embankment": "rail"}) == "rail" and fk({"embankment": "road"}) == "road"
    assert fk({"carried": "BNSF RAILWAY", "embankment": "road"}) == "rail"
    assert fk({"carried": "ND 25"}) == "" and fk({}) == "" and fk(None) == ""


@test
def bridge_snap_b3_the_facility_kind_reaches_the_structure_from_qgis_and_the_cli():
    import sys
    import types
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    from nd_breach_runner import algorithms as A
    importlib.reload(A)
    from nd_breach_runner.tests.fake_qgis import FakeGeometry, FakeFeature, FakeSource
    alg = A.RunDamAlgorithm()
    alg.initAlgorithm()
    src = FakeSource([FakeFeature(FakeGeometry("line", [[(1530.0, 20.0), (1530.0, -20.0)]]),
                                  kind="bridge", name="OSM 1 railway bridge", embankment="rail",
                                  carried="rail"),
                      FakeFeature(FakeGeometry("line", [[(1630.0, 20.0), (1630.0, -20.0)]]),
                                  kind="bridge", name="NBI 2", SERVICE_ON_042A="1")],
                     names=["kind", "name", "embankment", "carried", "SERVICE_ON_042A"])
    st = types.SimpleNamespace(max_extent_mi=45.0)
    alg.apply_features(st, {"STRUCTURES": src}, fake_qgis.QgsProcessingContext(),
                       fake_qgis.QgsCoordinateReferenceSystem("EPSG:5070"), (0.0, 0.0))
    assert [s.carries for s in st.structures] == ["rail", "road"], st.structures
    assert "carries" not in st.structures[0].to_dict()          # the run key is unchanged
    from nd_breach_runner.tests import test_cli as tc
    c = tc.cli()
    with tc.Tmp() as d:
        p, _cfg = tc.base_config(d)
        tc.dump(os.path.join(d, "s.geojson"), tc.fc([
            tc.line([[300, -20], [300, 20]], kind="bridge", name="R1", embankment="rail"),
            tc.line([[500, -20], [500, 20]], kind="bridge", name="B2")]))
        cfg = json.load(open(p))
        cfg["layers"] = {"structures": "s.geojson"}
        tc.dump(p, cfg)
        opts = c.build_parser().parse_args(["run", p, "--lisflood", sys.executable])
        st = c.prepare(c.load_config(p), opts).st
        assert {s.name: s.carries for s in st.structures} == {"R1": "rail", "B2": ""}, \
            st.structures


@test
def bridge_snap_b4_the_nearest_crossing_not_the_deepest_and_no_grade_separation():
    """3.10.90 review (bridges F4): the target is the NEAREST opening of at least 1.5 m (depth
    only breaks a tie), never one past it; with channel lines an opening without one (a grade
    separation) is no target, nor is one where another road or rail passes under."""
    # the bridge 40 m from its creek's 3 m opening; a 7 m opening 200 m the other way
    fn = lambda X, Y: _valley(X, Y, creeks=(0.0, -200.0), crown=91.5,              # noqa
                              openings=((0.0, 10.0), (-200.0, 10.0)), beds=(None, 84.5))
    b = _bridge(40.0, "A_off40")
    rows, notes, warns = _snap([b], fn)
    r = rows[0]
    assert r["method"] == "dem_opening" and abs(b.line.mean(axis=0)[0]) < 3.0, r
    assert abs(r["moved_m"] - 40.0) < 3.0, r
    # channel lines in the run: the DEM opening at 60 has none (a grade separation); the creek's
    # channel line crosses at -100 (its deck in the LiDAR)
    fn = lambda X, Y: _valley(X, Y, creeks=(), openings=((60.0, 8.0),), slope=0.0)  # noqa
    b = _bridge(0.0, "B")
    rows, notes, warns = _snap([b], fn, streams=(np.array([[-100.0, -300.0], [-100.0, 300.0]]),))
    assert rows[0]["method"] == "channel_crossing", rows[0]
    assert abs(b.line.mean(axis=0)[0] + 100.0) < 3.0, b.line
    # no channel lines: a county road passing under the highway's opening at 60 is a grade
    # separation; the creek's opening further on is the target
    fn = lambda X, Y: _valley(X, Y, openings=((60.0, 8.0), (155.0, 15.0)))         # noqa
    b = _bridge(-35.0, "C")
    county = np.array([[60.0, -300.0], [60.0, 300.0]])
    rows, notes, warns = _snap([b], fn, roads=(ROAD, county))
    assert rows[0]["method"] == "dem_opening" and abs(b.line.mean(axis=0)[0] - 155.0) < 6.0, \
        rows[0]


@test
def bridge_snap_b5_each_crossing_goes_to_the_nearest_bridge_whatever_the_order():
    """3.10.90 review (bridges F5): crossings are handed out once, nearest bridge first, so the
    inventory's order does not matter; twin bridges of a divided highway each get their own
    carriageway's opening (the first one's new place does not block the second)."""
    import itertools
    fn = lambda X, Y: _valley(X, Y, creeks=(150.0,), openings=((150.0, 10.0),))     # noqa
    results = []
    for order in itertools.permutations(("A_deck", "B_off40", "C_far")):
        bs_ = {"A_deck": _bridge(0.0, "A_deck"), "B_off40": _bridge(190.0, "B_off40"),
               "C_far": _bridge(-100.0, "C_far")}
        rows, notes, warns = _snap([bs_[n] for n in order], fn)
        results.append({n: (_row(rows, n)["method"], tuple(np.round(bs_[n].line.ravel(), 3)))
                        for n in order})
    assert all(r == results[0] for r in results), results
    assert results[0]["B_off40"][0] == "dem_opening", results[0]
    assert results[0]["A_deck"][0] == "none" and results[0]["C_far"][0] == "none", results[0]
    # a divided highway: carriageways at y = 0 and y = 30, the creek's openings on both, twin
    # NBI bridges both 120 m off
    fn = lambda X, Y: _valley(X, Y, creeks=(0.0,), openings=((0.0, 10.0),),         # noqa
                              road_y=(0.0, 30.0), half=8.0)
    north = np.array([[-400.0, 30.0], [400.0, 30.0]])
    for order in ((0, 1), (1, 0)):
        twins = [_bridge(-120.0, "S", -12.0, 12.0), _bridge(-120.0, "N", 18.0, 42.0)]
        rows, notes, warns = _snap([twins[i] for i in order], fn, roads=(ROAD, north))
        for b, y in zip(twins, (0.0, 30.0)):
            r = _row(rows, b.name)
            assert r["method"] == "dem_opening", (order, r)
            assert np.allclose(b.line.mean(axis=0), [0.0, y], atol=2.0), (order, b.line)
        assert len(warns) == 2, warns


@test
def bridge_snap_b6_the_radius_reads_like_the_other_ndbreach_numbers():
    """3.10.90 review (bridges F6): NDBREACH_BRIDGE_SNAP_M off / no / false = 0 = off, as
    workflow._env_number reads the other NDBREACH_* numbers; a value that is not a number is
    said; the radius is logged and recorded in run_report.json bridge_snap."""
    import types
    from nd_breach_runner.core import bridge_snap as bs
    from nd_breach_runner.core import workflow as W
    old = os.environ.get("NDBREACH_BRIDGE_SNAP_M")
    try:
        for v, want in (("off", 0.0), ("no", 0.0), ("FALSE", 0.0), ("0", 0.0), ("150", 150.0),
                        ("", 300.0), ("abc", 300.0), ("-5", 0.0)):
            os.environ["NDBREACH_BRIDGE_SNAP_M"] = v
            assert bs.radius_from_env() == want, (v, bs.radius_from_env())
            assert bs.radius_from_env() == max(0.0, W._env_number("BRIDGE_SNAP_M", 300.0)), v
            b = _bridge(-35.0, "x")
            rows, notes, warns = _snap_env([b], _valley)
            if want == 0.0:
                assert rows == [] and b.line[0][0] == -35.0 and "off" in notes[0], (v, notes)
            else:
                assert rows and any("{:g} m".format(want) in n for n in notes), (v, notes)
            assert ("not a number" in " ".join(warns)) == (v == "abc"), (v, warns)
        os.environ["NDBREACH_BRIDGE_SNAP_M"] = "off"
        st = types.SimpleNamespace(structures=[_bridge(-35.0, "x")], road_lines=[ROAD],
                                   rail_lines=[], edits=None)
        entry, notes, warns, moved = bs.check_run(st, _Terrain(_valley).read)
        assert entry["off"] and entry["radius_m"] == 0.0 and entry["env"] == "off" and not moved
        os.environ["NDBREACH_BRIDGE_SNAP_M"] = "250"
        entry, notes, warns, moved = bs.check_run(st, _Terrain(_valley).read)
        assert entry["radius_m"] == 250.0 and moved and entry["moved"] == 1, entry
        json.dumps(entry)                                       # goes into run_report.json
    finally:
        if old is None:
            os.environ.pop("NDBREACH_BRIDGE_SNAP_M", None)
        else:
            os.environ["NDBREACH_BRIDGE_SNAP_M"] = old


def _snap_env(structs, fn):
    from nd_breach_runner.core import bridge_snap as bs
    return bs.snap_bridges(structs, [ROAD], _Terrain(fn).read, [])


_MIN_REPORT = {                         # as tests/test_culvert_match.py
    "hydrograph": [], "policy": [], "run_finished": [], "tool": [],
    "inflow_point": {"xy": [0.0, 0.0], "how": "given"},
    "lisflood_runs": [{"run": 1, "bounds": [0.0, 0.0, 4000.0, 4000.0], "size_mi": [2.49, 2.49],
                       "cells": 10000, "dem_coverage": 1.0, "sim_time_hr": 10.0,
                       "runtime_min": 0.2, "edge_max_depth_ft": {"N": 0.0, "S": 0.0, "W": 0.0,
                                                                 "E": 0.0},
                       "edges_over_tolerance": [], "latest_arrival_hr": 9.0,
                       "stats": {"cells": 10000, "cell_size_m": 40.0, "sim_time_hr": 10.0,
                                 "max_depth_ft": 9.0, "max_velocity_fps": 11.0,
                                 "flooded_acres": 1000.0, "peak_inflow_cfs": 25000.0,
                                 "max_volume_error_pct": 0.0, "worst_interval_error_pct": 0.0,
                                 "injected_acft": 1600.0, "in_domain_acft": 1400.0,
                                 "velocity_written": True}}],
    "outputs": {"final": {"depth": "d.tif", "velocity": "v.tif", "arrival": "a.tif",
                          "wse": "w.tif"}},
    "settings": {"edge_tolerance_ft": 1.0, "expand_step_mi": 3.0},
    "summary": {"dam_id": "T1", "final_model": "LISFLOOD-FP", "peak_breach_cfs": 25000.0,
                "breach_method": "piping", "storage_at_failure_acft": 1600.0,
                "flooded_acres": 1000.0, "flooded_acres_over_arrival_depth": 900.0,
                "max_depth_ft": 9.0, "max_velocity_fps": 11.0, "latest_arrival_hr": 9.0,
                "lisflood_runs": 1, "domain_mi": [2.49, 2.49], "units": "ft"}}


@test
def bridge_snap_b7_moves_are_warnings_and_a_table_in_run_report_md():
    """3.10.90 review (bridges F7): every moved bridge is a run warning (name, from, to,
    distance, method) and run_report.md has a table of every bridge checked."""
    import copy
    import types
    from nd_breach_runner.core import bridge_snap as bs
    from nd_breach_runner.core import workflow as W
    st = types.SimpleNamespace(structures=[_bridge(-35.0, "0025008224"),
                                           _bridge(-252.0, "on the creek"),
                                           _bridge(-100.0, "nowhere", 500.0, 530.0)],
                               road_lines=[ROAD], rail_lines=[], edits=None)
    fn = lambda X, Y: _valley(X, Y, creeks=(155.0, -250.0),                         # noqa
                              openings=((155.0, 15.0), (-250.0, 10.0)))
    old = os.environ.pop("NDBREACH_BRIDGE_SNAP_M", None)
    try:
        entry, notes, warns, moved = bs.check_run(st, _Terrain(fn).read)
    finally:
        if old is not None:
            os.environ["NDBREACH_BRIDGE_SNAP_M"] = old
    assert moved and entry["moved"] == 1 and entry["kept_verified"] == 1, entry
    assert entry["not_verified"] == 1 and entry["radius_m"] == 300.0, entry
    mv = [w for w in warns if "moved" in w]
    assert len(mv) == 1 and "0025008224" in mv[0] and "from [-35.0, 0.0] to" in mv[0], warns
    assert any("nowhere" in w and "inventory location" in w for w in warns), warns
    r = copy.deepcopy(_MIN_REPORT)
    assert "Inventory bridges checked" not in W.render_report(r)
    r["bridge_snap"] = json.loads(json.dumps(entry))
    md = W.render_report(r)
    assert "## Inventory bridges checked against the road (3.10.90)" in md, md
    row = [ln for ln in md.splitlines() if ln.startswith("| 0025008224 |")]
    assert row and "| dem_opening | yes | 190 | -35.0, 0.0 | 155.0, 0.0 |" in row[0], row
    assert any(ln.startswith("| on the creek |") and "| kept | yes | 0 |" in ln
               for ln in md.splitlines()), md
    assert any(ln.startswith("| nowhere |") and "| none | no |" in ln
               for ln in md.splitlines()), md
    # the snap off: said in the report
    r["bridge_snap"] = {"radius_m": 0.0, "env": "off", "off": True, "bridges": []}
    assert "every inventory bridge is cut where the inventory puts it" in W.render_report(r)


# ------------------------------------------------------------------ (a) road crown depth
def _line_x(x0=0.0, x1=200.0):
    return [np.array([[x0, 0.0], [x1, 0.0]])]


def _ground(trench_x=None, notch_x=None):
    def g(xy):
        xy = np.asarray(xy, dtype=float)
        z = np.full(len(xy), ROAD_M)
        if notch_x is not None:                      # bare earth under a bridge (no deck)
            z[np.abs(xy[:, 0] - notch_x) <= 10.0] = ROAD_M - 5.0
        return z
    return g


def _wse(level_m, where=None):
    def w(xy):
        xy = np.asarray(xy, dtype=float)
        out = np.full(len(xy), level_m / FT)
        if where is not None:
            out[~where(xy)] = np.nan
        return out
    return w


SAG_ROAD = [np.array([[-300.0, 15.0], [300.0, 15.0]])]


def _sag(grade=0.03, x0=15.0, opening_half=None, bed=494.0):
    """A road crown 500 m + grade * |x - x0| (a sag at x0), with a bare-earth opening (no deck)
    down to `bed` within opening_half of x0."""
    def g(xy):
        x = np.asarray(xy, dtype=float)[:, 0] - x0
        z = 500.0 + grade * np.abs(x)
        if opening_half:
            z = np.where(np.abs(x) <= opening_half, bed, z)
        return z
    return g


@test
def road_crown_culverts_never_mask_and_bridge_cuts_read_the_deck_stand_in():
    """3.10.90 review (roads F1): a culvert is buried, so the fine DEM is the road over it and
    its cut never masks; a bridge / notch cut reads the road on a deck stand-in from the
    approaches (no extra 50 ft buffer), never below what that stand-in gives."""
    from nd_breach_runner.core import road_profile as rp
    culv = [{"kind": "culvert", "line": [[15.0, 0.0], [15.0, 30.0]], "width_m": 1.5,
             "barrels": 1, "name": "culvert 1"}]
    assert rp.cut_corridors(culv, 10.0) == []
    # a 3 % sag with the culvert at its low point, 0.5 m (1.64 ft) of real overtopping
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.5), _sag(), cuts=rp.cut_corridors(culv, 10.0))
    assert abs(got["depth_ft"] - 0.5 / FT) < 0.02 and got["n_cut"] == 0, got
    br = [{"kind": "bridge", "line": [[15.0, 0.0], [15.0, 30.0]], "width_m": 24.0,
           "barrels": 1, "name": "NBI"}]
    cuts = rp.cut_corridors(br, 30.0)
    assert len(cuts) == 1 and abs(cuts[0][1] - 15.0) < 1e-9, cuts     # 0.5 * max(30, 26.4)
    # the bridge at the sag: the deck and approaches 0.6 m (1.97 ft) under water, the bare earth
    # under the removed deck 6 m down: read on the stand-in (the approaches' grade carried in)
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.6), _sag(opening_half=12.0), cuts=cuts)
    assert got["n_cut"] > 0 and got["depth_ft"] >= 0.6 / FT - 0.05, got
    assert got["depth_ft"] < 2.5, got                                # not the 6 m bare earth
    # DWR-3034: the water stands 3 m below the road in the cut's trench only -> dry
    trench = _wse(497.0, lambda xy: np.abs(xy[:, 0] - 15.0) <= 15.0)
    for ground in (_sag(opening_half=12.0), _sag()):     # at the opening, or on solid fill
        got = rp.road_crown_depth(SAG_ROAD, trench, ground, cuts=cuts)
        assert got["depth_ft"] == 0.0 and got["freeboard_ft"] > 5.0, got
    # the stand-in never hides water: a deck in the LiDAR lower than both approaches reads on
    # its own surface
    deck = _sag(grade=0.0)
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.3), lambda xy: deck(xy) - np.where(
        np.abs(np.asarray(xy)[:, 0] - 15.0) <= 10.0, 0.2, 0.0), cuts=cuts)
    assert abs(got["depth_ft"] - 0.5 / FT) < 0.02, got


@test
def road_crown_openings_need_a_mapped_bridge_and_a_multi_metre_baseline():
    """3.10.90 review (roads F6): a bare-earth opening takes the deck stand-in only where it
    lines up with a mapped bridge / notch, and its steep side is measured over several metres
    (pixel steps, LiDAR noise and seams do not make a road sag an opening)."""
    from nd_breach_runner.core import road_profile as rp
    wet = lambda xy: np.abs(np.asarray(xy)[:, 0]) < 60.0                 # noqa: E731
    # a 0.25 m collection seam 8 m from the bottom of a 5 % sag, 0.4 m of real overtopping
    seam = lambda xy: 500.0 + 0.05 * np.abs(xy[:, 0]) + np.where(xy[:, 0] > 8.0, 0.25, 0.0)  # noqa: E731,E501
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.4), seam, wet=wet)
    assert abs(got["depth_ft"] - 0.4 / FT) < 0.02 and got["n_opening"] == 0, got
    # noisy LiDAR (sigma 5 cm) on an 8 % sag: never hidden
    for seed in range(10):
        nz = np.random.default_rng(seed).normal(0.0, 0.05, 1000)
        g = lambda xy, nz=nz: 500.0 + 0.08 * np.abs(xy[:, 0]) + nz[np.floor(xy[:, 0]).astype(int) + 500]  # noqa: E731,E501
        got = rp.road_crown_depth(SAG_ROAD, _wse(500.3), g, wet=wet)
        assert got["depth_ft"] > 0.5, (seed, got)
    # a 10 m DEM (pixel steps of 0.6 m) on a 6 % sag: no opening
    class Coarse:
        pixel_m = 10.0

        def __call__(self, xy):
            return 500.0 + 0.06 * np.abs((np.floor(xy[:, 0] / 10.0) + 0.5) * 10.0)
    got = rp.road_crown_depth(SAG_ROAD, _wse(500.6), Coarse(), wet=wet)
    raw = rp.road_crown_depth(SAG_ROAD, _wse(500.6), Coarse(), wet=wet, mask_openings=False)
    assert got["n_opening"] == 0 and got["depth_ft"] == raw["depth_ft"] > 0.5, (got, raw)
    # a real bare-earth opening with no mapped structure: read as it is (counted)
    w = _wse(ROAD_M - 3.0, lambda xy: np.abs(xy[:, 0] - 100.0) <= 10.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0))
    assert abs(got["depth_ft"] - 2.0 / FT) < 0.01 and got["n_opening_unmatched"] == 1, got
    # the same opening at a mapped bridge: the deck stand-in from the approaches
    cuts = rp.cut_corridors([{"kind": "bridge", "line": [[100.0, -5.0], [100.0, 5.0]],
                              "width_m": 4.0}], 1.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0), cuts=cuts)
    assert got["depth_ft"] == 0.0 and got["n_opening"] > 0 and got["n_cut"] > 0, got
    assert abs(got["freeboard_ft"] - 3.0 / FT) < 0.05, got
    # a culvert is no evidence of an opening (a sag over a culvert is the usual crossing)
    cv = rp.cut_corridors([{"kind": "culvert", "line": [[100.0, -5.0], [100.0, 5.0]]}], 1.0)
    got = rp.road_crown_depth(_line_x(), w, _ground(notch_x=100.0), cuts=cv)
    assert abs(got["depth_ft"] - 2.0 / FT) < 0.01, got


@test
def road_crown_wet_points_with_no_road_surface_take_the_depth_raster():
    """3.10.90 review (roads F4): a wet centreline point with no road surface (DEM no-data,
    outside the DEM, a refilled area) takes the depth raster there; a road whose wet points
    have no water surface falls back to the raster (None), not 0."""
    from nd_breach_runner.core import road_profile as rp
    road = [np.array([[-100.0, 0.0], [100.0, 0.0]])]
    part_dem = lambda xy: np.where(xy[:, 0] < 0.0, np.nan, 501.0)       # noqa: E731
    raster = lambda xy: np.where(xy[:, 0] < 0.0, 1.0 / FT, 0.5 / FT)    # noqa: E731
    wet = lambda xy: np.ones(len(xy), bool)                              # noqa: E731
    got = rp.road_crown_depth(road, _wse(500.0), part_dem, wet=wet, depth_ft=raster)
    assert abs(got["depth_ft"] - 1.0 / FT) < 0.01 and got["n_nodem"] > 90, got
    assert got["x"] < 0.0 and abs(got["freeboard_ft"] - 1.0 / FT) < 0.01, got
    assert abs(got["raster_points_ft"] - 1.0 / FT) < 0.01, got
    # no depth raster to fall back on: the caller keeps the raster depth (None), never 0
    assert rp.road_crown_depth(road, _wse(500.0), part_dem, wet=wet) is None
    # wet by the depth raster, no water surface anywhere: None (the raster), not 0
    nan_w = lambda xy: np.full(len(xy), np.nan)                          # noqa: E731
    assert rp.road_crown_depth(road, nan_w, _ground(), wet=wet, depth_ft=raster) is None
    # no road surface anywhere: the raster for the road
    assert rp.road_crown_depth(_line_x(), _wse(ROAD_M + 1.0),
                               lambda xy: np.full(len(xy), np.nan)) is None
    # a road layer split at the bridge (an OSM bridge way): no approach on the line, so no
    # deck stand-in - those points take the depth raster (the whole way: None, the raster)
    cuts = rp.cut_corridors([{"kind": "bridge", "line": [[0.0, -15.0], [0.0, 15.0]],
                              "width_m": 40.0}], 10.0)
    bed = lambda xy: np.full(len(xy), 494.0)                             # noqa: E731
    assert rp.road_crown_depth([np.array([[-15.0, 0.0], [15.0, 0.0]])], _wse(497.0), bed,
                               wet=wet, cuts=cuts, depth_ft=raster) is None
    got = rp.road_crown_depth([np.array([[-15.0, 0.0], [60.0, 0.0]])], _wse(497.0),
                              lambda xy: np.where(xy[:, 0] > 22.0, 500.0, 494.0), wet=wet,
                              cuts=cuts, depth_ft=raster)
    assert got["n_no_approach"] == 0 and got["n_cut"] > 0, got     # one approach is enough
    assert got["depth_ft"] == 0.0, got          # the stand-in from the east approach: dry
    nan_east = lambda xy: np.where(xy[:, 0] > 22.0, np.nan, 494.0)       # noqa: E731
    got = rp.road_crown_depth([np.array([[-15.0, 0.0], [60.0, 0.0]])], _wse(497.0), nan_east,
                              wet=wet, cuts=cuts, depth_ft=raster)
    assert got is None                           # no approach surface and no DEM: the raster


def _tiny_dem(path, value=ROAD_M, unit=None):
    from osgeo import gdal, osr
    ds = gdal.GetDriverByName("GTiff").Create(path, 80, 80, 1, gdal.GDT_Float32)
    assert ds is not None, path
    ds.SetGeoTransform((0.0, 1.0, 0.0, 80.0, 0.0, -1.0))
    sr = osr.SpatialReference()
    sr.ImportFromEPSG(26914)
    ds.SetProjection(sr.ExportToWkt())
    a = np.full((80, 80), value, dtype=np.float32) if np.isscalar(value) else \
        np.asarray(value, dtype=np.float32)
    ds.GetRasterBand(1).WriteArray(a)
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    if unit:
        ds.GetRasterBand(1).SetUnitType(unit)
    ds = None


def _have_gdal():
    try:
        from osgeo import gdal  # noqa: F401
        return True
    except ImportError:
        print("      (skipped: no GDAL)")
        return False


@test
def ndhazard_tool_road_overtopping_on_the_centreline_not_in_the_cut_trench():
    if not _have_gdal():
        return
    from nd_breach_runner.core import workflow
    from nd_breach_runner.tests import test_ndhazard_tool as tt
    an, fq, _ = tt._load_tool()
    h = tt._Harness()
    tmp = tempfile.mkdtemp()
    try:
        dem = os.path.join(tmp, "dem_1m.tif")
        z = np.full((80, 80), ROAD_M)
        z[20:30, 22:29] = ROAD_M - 5.0                   # bare earth under the bridge on Hwy 1
        _tiny_dem(dem, z)
        bm = os.path.join(h.folder, "_breach_model")
        os.makedirs(bm)
        json.dump({"settings": {"input_files": {"DEM": dem}, "cell_size_m": 10.0},
                   "dem_z_to_m": 1.0}, open(os.path.join(bm, "run_report.json"), "w"))
        # a bridge cut across Hwy 1 (y 55) at x 25: its trench is Hwy 1's only wet cell
        json.dump([{"kind": "bridge", "line": [[25.0, 50.0], [25.0, 60.0]], "width_m": 6.0,
                    "barrels": 1, "name": "NBI 1"}], open(os.path.join(bm, "structures.json"), "w"))
        d = h.arrays[os.path.join(h.folder, workflow.FINAL_NAMES["depth"])]
        wse = np.where(d > 0, ROAD_M / FT + np.nan_to_num(d), np.nan)
        wse[2, 2] = (ROAD_M - 3.0) / FT                  # the trench water: 3 m below the road
        wp = os.path.join(h.folder, workflow.FINAL_NAMES["wse"])
        open(wp, "w").write("x")
        h.arrays[wp] = wse
        s, r, t, l, dm = tt._layers(fq)
        base = {"DAM_FOLDER": h.folder, "STRUCTURES": s, "ROADS": r, "RAIL": t,
                "LIFELINES": l, "DOWNSTREAM_DAMS": dm, "CHART_FILE": h.chart_file()}
        res, ctx, fb = tt._run(an, fq, base)
        js = json.load(open(res["JSON"]))
        rd = js["road_depth"]
        assert rd["basis"] == "centreline" and rd["bridge_notch_cuts"] == 1, rd
        assert not [f for f in js["findings"] if f["kind"] == "road" and f["id"] == "Hwy 1"]
        drop = [x for x in rd["dropped"] if x["id"] == "Hwy 1"]
        assert drop and drop[0]["raster_depth_ft"] == 0.5 and drop[0]["n_cut"] > 0, rd
        assert any("Hwy 1" in e and "not counted as overtopped" in e for e in fb.errors), \
            fb.errors
        i94 = tt._find(js, "road", "I- 94")                       # real overtopping stays
        assert abs(i94["depth_ft"] - 0.2) < 0.02, i94
        md = open(res["REPORT"], encoding="utf-8").read()
        assert "Road overtopping depth" in md and "Not counted as overtopped: Hwy 1" in md, md
        # the old basis on request
        res, ctx, fb = tt._run(an, fq, dict(base, ROAD_DEPTH_BASIS=1))
        js = json.load(open(res["JSON"]))
        assert tt._find(js, "road", "Hwy 1")["depth_ft"] == 0.5
        # no WSE raster: the depth raster, with a note
        os.remove(wp)
        res, ctx, fb = tt._run(an, fq, base)
        js = json.load(open(res["JSON"]))
        assert tt._find(js, "road", "Hwy 1")["depth_ft"] == 0.5
        assert any("depth raster for every road" in e for e in fb.errors), fb.errors
    finally:
        h.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- the ND 5.2 tool on real rasters
class _Site:
    """A dam folder of real GDAL rasters for the ND 5.2 tool (fake QGIS): a 10 m model grid of
    40 x 3 cells (local x 0..400, y 0..30 m), the run's 1 m DEM (400 x 30 pixels), County 12
    along y = 15. `u` metres per CRS unit and `origin` the lower-left corner in the CRS: the
    same site in State Plane feet (EPSG:2265) or UTM. fine (30, 400) and cell_ground (3, 40)
    are metres; the DEM is written as fine / dem_z (dem_z metres per DEM unit)."""

    def __init__(self, fine, cell_ground, wse_m, structures=None, report=None, epsg=26914,
                 u=1.0, origin=(0.0, 0.0), dem_z=1.0, dem_unit=None, work_epsg=None):
        self.tmp = tempfile.mkdtemp()
        self.epsg, self.u, self.origin = epsg, float(u), origin
        self.folder = os.path.join(self.tmp, "ND00999")
        bm = os.path.join(self.folder, "_breach_model")
        os.makedirs(bm)
        self.dem = self.tif(os.path.join(self.tmp, "dem_1m.tif"), np.asarray(fine) / dem_z, 1.0,
                            unit=dem_unit)
        wse = np.broadcast_to(np.asarray(wse_m, dtype=float), np.shape(cell_ground))
        depth = wse - np.asarray(cell_ground, dtype=float)
        wet = depth > 0.003
        self.tif(os.path.join(self.folder, "Depth (Max).tif"), np.where(wet, depth / FT, np.nan),
                 10.0)
        self.tif(os.path.join(self.folder, "Velocity (Max).tif"), np.where(wet, 1.0, np.nan),
                 10.0)
        self.tif(os.path.join(self.folder, "WSE (Max).tif"), np.where(wet, wse / FT, np.nan),
                 10.0)
        rep = {"settings": {"input_files": {"DEM": self.dem}, "cell_size_m": 10.0},
               "dem_z_to_m": dem_z}
        if work_epsg:
            rep["work_crs"] = "EPSG:{}".format(work_epsg)
        for k, v in (report or {}).items():
            if v is None:
                rep.pop(k, None)
            else:
                rep[k] = v
        json.dump(rep, open(os.path.join(bm, "run_report.json"), "w"))
        if structures is not None:
            json.dump(structures, open(os.path.join(bm, "structures.json"), "w"))

    def xy(self, x, y):
        return (self.origin[0] + x / self.u, self.origin[1] + y / self.u)

    def tif(self, path, a, px_m, unit=None, values=None):
        from osgeo import gdal, osr
        a = np.asarray(a, dtype=float)
        ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1,
                                                  gdal.GDT_Float32)
        x0, y1 = self.xy(0.0, 30.0)
        ds.SetGeoTransform((x0, px_m / self.u, 0.0, y1, 0.0, -px_m / self.u))
        sr = osr.SpatialReference()
        sr.ImportFromEPSG(self.epsg)
        ds.SetProjection(sr.ExportToWkt())
        b = ds.GetRasterBand(1)
        b.SetNoDataValue(-9999.0)
        b.WriteArray(np.where(np.isfinite(a), a, -9999.0).astype(np.float32))
        if unit:
            b.SetUnitType(unit)
        ds = None
        return path

    def run(self, roads=None, **params):
        from nd_breach_runner.core import nd_hazard as nh
        from nd_breach_runner.tests import test_ndhazard_tool as tt
        an, fq, _ = tt._load_tool()
        roads = roads or [("County 12", [(0.5, 15.0), (399.5, 15.0)], 600.0)]
        src = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry(
            "line", [[self.xy(x, y) for x, y in pts]]), NAME=name, AADT=aadt, RTTYP="C",
            private="") for name, pts, aadt in roads], source_name="roads")
        p = {"DAM_FOLDER": self.folder, "ROADS": src,
             "LETHALITY": nh.LETHALITY_METHODS.index(nh.LETHALITY_ALL_WET)}
        p.update(params)
        res, ctx, fb = tt._run(an, fq, p)
        js = json.load(open(res["JSON"], encoding="utf-8"))
        md = open(res["REPORT"], encoding="utf-8").read()
        return js, md, fb

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


_X = np.arange(400) + 0.5
_Y = 30.0 - (np.arange(30) + 0.5)
_XX, _YY = np.meshgrid(_X, _Y)
_ON_ROAD = np.abs(_YY - 15.0) <= 5.0


def _cells(fine):
    """The model's cell ground: the 1 m DEM averaged over each 10 m cell (the default)."""
    return np.asarray(fine, dtype=float).reshape(3, 10, 40, 10).mean(axis=(1, 3))


def _roads_of(js):
    return {f["id"]: f["depth_ft"] for f in js["findings"] if f["kind"] == "road"}


def _flat_site(**kw):
    fine = np.where(_ON_ROAD, 500.0, 497.0)
    return _Site(fine, _cells(fine), 500.5, **kw), fine


@test
def ndhazard_tool_culvert_at_a_sag_keeps_its_overtopping_and_lists_the_lower_depth():
    """3.10.90 review (roads F1, F7): a culvert at a road sag no longer hides the overtopping;
    a road read lower than the depth raster gets a Check and a road_depth.lowered row."""
    if not _have_gdal():
        return
    fine = np.where(_ON_ROAD, 500.0 + 0.03 * np.abs(_XX - 205.0), 497.0)
    cg = _cells(fine)
    cg[:, 20] = 497.0                                              # the culvert's trench cell
    site = _Site(fine, cg, 500.5, structures=[{
        "kind": "culvert", "line": [[205.0, 0.0], [205.0, 30.0]], "width_m": 1.5,
        "barrels": 1, "name": "culvert 1"}])
    try:
        js, md, fb = site.run()
        got = _roads_of(js)
        assert abs(got.get("County 12", 0.0) - 0.5 / FT) < 0.1, (got, fb.errors)
        assert js["hazard_class"] == "Medium", js["hazard_class"]
        low = js["road_depth"]["lowered"]
        assert low and low[0]["id"] == "County 12" and low[0]["raster_depth_ft"] > 10.0, low
        assert any("County 12" in e and "shallower than the depth raster" in e
                   for e in fb.errors), fb.errors
        assert "Overtopped, shallower than the depth raster: County 12" in md
        assert js["road_depth"]["culverts_not_masked"] == 1
    finally:
        site.close()


def _dwr3034_site(misplaced, **kw):
    """DWR-3034-like: Hwy 25's embankment at 500 m over 494 m ground; Square Butte Creek's
    bare-earth opening (the deck is not in the LiDAR) at x 190-220 down to 493 m; the NBI
    bridge cut at the creek (after the snap) or 100 m south of it on solid fill (misplaced).
    The flood stands at 494 m: 6 m below the road, wet in the creek and in the cut's trench."""
    fine = np.where(_ON_ROAD, 500.0, 494.0)
    fine = np.where(_ON_ROAD & (np.abs(_XX - 205.0) <= 15.0), 493.0, fine)
    cg = _cells(fine)
    xc = 105.0 if misplaced else 205.0
    cg[:, int(xc // 10)] = 493.0                                    # the cut's trench cell
    st = [{"kind": "bridge", "line": [[xc, 0.0], [xc, 30.0]], "width_m": 30.0, "barrels": 1,
           "name": "NBI 0025008224"}]
    if misplaced:      # the creek's own (correct) bridge record, e.g. OSM
        st.append({"kind": "bridge", "line": [[205.0, 0.0], [205.0, 30.0]], "width_m": 30.0,
                   "barrels": 1, "name": "OSM bridge"})
    return _Site(fine, cg, 494.0, structures=st, **kw)


@test
def ndhazard_tool_dwr3034_bridge_trench_reads_dry_and_is_listed():
    """DWR-3034: the trench of a bridge cut (snapped onto the creek, or misplaced on solid
    fill) is not road overtopping; the road the depth raster had overtopped is listed with
    the raster depth and a Check (no silent drop)."""
    if not _have_gdal():
        return
    for misplaced in (False, True):
        site = _dwr3034_site(misplaced)
        try:
            js, md, fb = site.run(roads=[("Hwy 25", [(0.5, 15.0), (399.5, 15.0)], 1500.0)])
            assert "Hwy 25" not in _roads_of(js), (misplaced, _roads_of(js), fb.errors)
            rd = js["road_depth"]
            drop = rd["dropped"]
            assert drop and drop[0]["id"] == "Hwy 25" and drop[0]["raster_depth_ft"] > 3.0, rd
            assert drop[0]["n_cut"] > 0 and drop[0]["freeboard_ft"] > 15.0, drop
            assert any("Hwy 25" in e and "not counted as overtopped" in e for e in fb.errors)
            assert "Not counted as overtopped: Hwy 25" in md
            # the old basis still reads the trench
            js, md, fb = site.run(roads=[("Hwy 25", [(0.5, 15.0), (399.5, 15.0)], 1500.0)],
                                  ROAD_DEPTH_BASIS=1)
            assert _roads_of(js).get("Hwy 25", 0.0) > 3.0, _roads_of(js)
        finally:
            site.close()


@test
def ndhazard_tool_road_surface_dem_has_its_own_units_and_is_checked_against_the_run():
    """3.10.90 review (roads F2): the 'Road surface DEM' is read in its own vertical unit (band
    unit or vertical CRS, or the units chosen), and is used only when its median offset from
    the run's own ground is within 1 m; otherwise every road reads the depth raster, with a
    Check."""
    if not _have_gdal():
        return
    site, fine = _flat_site()
    try:
        ref = _roads_of(site.run()[0])
        assert abs(ref["County 12"] - 0.5 / FT) < 0.02, ref
        ft_tag = site.tif(os.path.join(site.tmp, "road_ft_tagged.tif"), fine / FT, 1.0, unit="ft")
        js, md, fb = site.run(ROAD_DEM=ft_tag)
        assert js["road_depth"]["basis"] == "centreline", js["road_depth"]
        assert abs(js["road_depth"]["datum_check"]["median_offset_m"]) < 0.01
        assert _roads_of(js) == ref, (_roads_of(js), ref)
        ft_raw = site.tif(os.path.join(site.tmp, "road_ft.tif"), fine / FT, 1.0)
        js, md, fb = site.run(ROAD_DEM=ft_raw)                   # no unit recorded: the raster
        assert js["road_depth"]["basis"] == "raster", js["road_depth"]
        assert any("Road surface DEM elevation units" in e for e in fb.errors), fb.errors
        assert "County 12" in _roads_of(js)
        js, md, fb = site.run(ROAD_DEM=ft_raw, ROAD_DEM_UNITS=2)   # feet, chosen
        assert js["road_depth"]["basis"] == "centreline" and _roads_of(js) == ref
        shifted = site.tif(os.path.join(site.tmp, "road_ngvd.tif"), fine + 2.0, 1.0, unit="m")
        js, md, fb = site.run(ROAD_DEM=shifted)                  # 2 m off the run's datum
        assert js["road_depth"]["basis"] == "raster", js["road_depth"]
        assert any("vertical datum" in e for e in fb.errors), fb.errors
        # without the run's DEM: checked against the run's model ground (WSE - depth)
        os.remove(site.dem)
        js, md, fb = site.run(ROAD_DEM=ft_tag)
        assert js["road_depth"]["basis"] == "centreline", (js["road_depth"], fb.errors)
        assert "model ground" in js["road_depth"]["datum_check"]["against"]
        assert _roads_of(js) == ref
    finally:
        site.close()


@test
def ndhazard_tool_old_run_reports_keep_their_dem_units():
    """3.10.90 review (roads F3): a run_report.json from before 3.10.16 has no dem_z_to_m; the
    unit comes from dem_elevation_units (as core/hecras_model._z_factor), then the raster's own
    unit, never a metres default (unknown -> the depth raster, with a Check)."""
    if not _have_gdal():
        return
    fine = np.where(_ON_ROAD, 500.0, 497.0)
    for note, unit, want in (("feet (chosen)", None, "centreline"),
                             (None, "ft", "centreline"), (None, None, "raster")):
        rep = {"dem_z_to_m": None}
        if note:
            rep["dem_elevation_units"] = note
        site = _Site(fine, _cells(fine), 500.5, report=rep, dem_z=FT, dem_unit=unit)
        try:
            js, md, fb = site.run()
            assert js["road_depth"]["basis"] == want, (note, unit, js["road_depth"])
            assert abs(_roads_of(js).get("County 12", 0.0) - 0.5 / FT) < 0.02, _roads_of(js)
            if want == "raster":
                assert any("records no unit" in e for e in fb.errors), fb.errors
        finally:
            site.close()


@test
def ndhazard_tool_road_dem_read_error_keeps_the_raster_for_that_road():
    """3.10.90 review (roads F5): a road surface DEM that opens but cannot be read (a VRT tile
    gone since the run) no longer stops the tool: that road keeps the depth raster, with a
    Check naming it, as on main."""
    if not _have_gdal():
        return
    from osgeo import gdal
    site, fine = _flat_site()
    try:
        t0 = site.tif(os.path.join(site.tmp, "t0.tif"), fine[:, :200], 1.0)
        site.origin = (site.origin[0] + 200.0, site.origin[1])
        t1 = site.tif(os.path.join(site.tmp, "t1.tif"), fine[:, 200:], 1.0)
        site.origin = (site.origin[0] - 200.0, site.origin[1])
        vrt = os.path.join(site.tmp, "dem.vrt")
        gdal.BuildVRT(vrt, [t0, t1])
        os.remove(t1)
        rp = os.path.join(site.folder, "_breach_model", "run_report.json")
        rep = json.load(open(rp))
        rep["settings"]["input_files"]["DEM"] = vrt
        json.dump(rep, open(rp, "w"))
        js, md, fb = site.run()
        assert abs(_roads_of(js).get("County 12", 0.0) - 0.5 / FT) < 0.02, _roads_of(js)
        rr = js["road_depth"]["raster_roads"]
        assert rr and rr[0]["id"] == "County 12" and "could not be read" in rr[0]["reason"], rr
        assert any("County 12" in e and "could not be read" in e for e in fb.errors), fb.errors
    finally:
        site.close()


@test
def ndhazard_tool_feet_crs_folder_moves_the_structure_lines_and_works_in_metres():
    """3.10.90 review (roads F8): with the DEM in State Plane feet the final rasters are in that
    CRS while structures.json is in the run's work CRS (UTM m); the cut lines are moved into the
    rasters' CRS and the profile's metre constants are applied in metres, so the bridge trench
    reads dry there too, and the report says what matched."""
    if not _have_gdal():
        return
    from osgeo import osr
    sp, utm = osr.SpatialReference(), osr.SpatialReference()
    sp.ImportFromEPSG(2265)                       # NAD83 / North Dakota North (ft)
    utm.ImportFromEPSG(26914)
    for s in (sp, utm):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tf = osr.CoordinateTransformation(sp, utm)
    unit = sp.GetLinearUnits()
    origin = (1968500.0, 300000.0)
    fine = np.where(_ON_ROAD, 500.0, 494.0)
    fine = np.where(_ON_ROAD & (np.abs(_XX - 205.0) <= 15.0), 493.0, fine)
    cg = _cells(fine)
    cg[:, 20] = 493.0
    line = [tf.TransformPoint(origin[0] + 205.0 / unit, origin[1] + y / unit)[:2]
            for y in (0.0, 30.0)]
    site = _Site(fine, cg, 494.0, epsg=2265, u=unit, origin=origin, dem_z=unit,
                 work_epsg=26914, structures=[{"kind": "bridge", "line": [list(p) for p in line],
                                               "width_m": 30.0, "barrels": 1, "name": "NBI"}])
    try:
        js, md, fb = site.run(roads=[("Hwy 25", [(0.5, 15.0), (399.5, 15.0)], 1500.0)])
        rd = js["road_depth"]
        assert "Hwy 25" not in _roads_of(js), (_roads_of(js), rd)
        assert rd["points_on_cut_stand_in"] > 0 and rd["roads_on_a_stand_in"] == 1, rd
        assert abs(rd["unit_m"] - unit) < 1e-9 and "work CRS" in rd["structures_note"], rd
        assert rd["dropped"] and rd["dropped"][0]["freeboard_ft"] > 15.0, rd
    finally:
        site.close()
    # a cut that matches no road: the report says so, and the real overtopping stays
    site, _f = _flat_site(structures=[{"kind": "bridge", "line": [[50.0, -100.0], [50.0, -80.0]],
                                       "width_m": 10.0, "barrels": 1, "name": "far away"}])
    try:
        js, md, fb = site.run()
        assert js["road_depth"]["roads_on_a_stand_in"] == 0, js["road_depth"]
        assert "1 bridge / notch cut(s) in structures.json, 0 road(s) with wet points in one" \
            in js["road_depth"]["text"], js["road_depth"]["text"]
        assert abs(_roads_of(js)["County 12"] - 0.5 / FT) < 0.02
    finally:
        site.close()


@test
def cli_runs_record_road_and_rail_lines_and_the_dem():
    """3.10.90 review (bookkeeping F4): the command-line runner fills st.road_lines /
    st.rail_lines from the kept receptor roads / rail (as the QGIS tools' apply_features) and
    records input_files['DEM'], so the bridge snap and the ND tool's centreline basis work."""
    import sys
    from nd_breach_runner.tests import test_cli as tc
    c = tc.cli()
    with tc.Tmp() as d:
        p, _cfg = tc.base_config(d)
        tc.dump(os.path.join(d, "road.geojson"), tc.fc([tc.line([[400, -200], [400, 200]]),
                                                        tc.line([[9e6, 0], [9e6, 5]])]))
        tc.dump(os.path.join(d, "rail.geojson"), tc.fc([tc.line([[600, -200], [600, 200]])]))
        cfg = json.load(open(p))
        cfg["layers"] = {"receptor_roads": "road.geojson", "receptor_rail": "rail.geojson"}
        tc.dump(p, cfg)
        opts = c.build_parser().parse_args(["run", p, "--lisflood", sys.executable])
        st = c.prepare(c.load_config(p), opts).st
        assert len(st.road_lines) == 1 and np.allclose(st.road_lines[0][:, 0], 400.0), \
            st.road_lines
        assert len(st.rail_lines) == 1 and np.allclose(st.rail_lines[0][:, 0], 600.0)
        assert os.path.samefile(st.input_files["DEM"], os.path.join(d, "dem.asc")), \
            st.input_files
        assert "DEM" in st.as_dict()["input_files"]


@test
def readme_documents_the_bridge_snap_radius_and_the_road_depth_basis():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(here, "README.md"), encoding="utf-8").read()
    assert "NDBREACH_BRIDGE_SNAP_M" in text
    # 3.10.90 review: a moved bridge's inverts are cleared, not re-read
    assert "inverts are cleared" in text and "solid embankment" in text
    assert "Road surface DEM elevation units" in text and "deck stand-in" in text
