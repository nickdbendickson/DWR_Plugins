"""damsuite.cascade: a dam at the source location is the source, not a downstream hit.

Regression guard for the proximity exclusion in cascade.downstream(): the source dam AND
anything within SAME_DAM_MI of it are left out, regardless of id. A re-listed source dam
carrying a different id but sitting at the same spot must NOT come back as a downstream hit.
The nd_hazard fork tests this as suite116; DWR had no damsuite-cascade test, so a regression
(an `and not did` guard that skipped the proximity rule for id-bearing dams) went uncaught here.
"""
from nd_breach_runner.tests.registry import test
from nd_breach_runner.damsuite import cascade as CA

_SQUARE = [[(-100.0, 46.0), (-99.0, 46.0), (-99.0, 47.0), (-100.0, 47.0)]]


@test
def damsuite_cascade_a_dam_at_the_source_is_not_downstream():
    dams = [{"dam_id": "SRC", "lon": -99.9, "lat": 46.1},
            {"dam_id": "NEAR", "lon": -99.9001, "lat": 46.1},   # the same structure again
            {"dam_id": "DOWN", "lon": -99.2, "lat": 46.8},
            {"dam_id": "OUT", "lon": -98.0, "lat": 46.5}]       # outside the flood
    got = CA.downstream(dams, _SQUARE, source=(-99.9, 46.1), source_id="SRC")
    assert [h["dam_id"] for h in got] == ["DOWN"], [h["dam_id"] for h in got]
    assert CA.in_ring((-100.0, 46.5), _SQUARE[0])               # a point on the edge counts
    assert not CA.in_ring((-98.5, 46.5), _SQUARE[0])


@test
def damsuite_cascade_nearest_downstream_dam_comes_first():
    dams = [{"dam_id": "FAR", "lon": -99.1, "lat": 46.9},
            {"dam_id": "CLOSE", "lon": -99.8, "lat": 46.2}]
    got = CA.downstream(dams, _SQUARE, source=(-99.9, 46.1))
    assert [h["dam_id"] for h in got] == ["CLOSE", "FAR"], [h["dam_id"] for h in got]
    assert got[0]["distance_mi"] < got[1]["distance_mi"]
