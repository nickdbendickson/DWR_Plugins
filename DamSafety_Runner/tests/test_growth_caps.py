"""The shallow-outflow domain growth must STOP at a cap and SAY SO (audit Grok 3.1 follow-up).

On flat terrain - common on the ND plains - a breach flood sheets outward and the outflow rule
keeps extending the domain, because the edge never falls below the outflow share. The loop must
then be bounded by a cap (the domain-run limit or the maximum extent), and a run that stops at a
cap with the flood still leaving must be reported as a MINIMUM, not the full flood, so a
cap-truncated extent can't be read as the final answer.

This is the light (GDAL-free, no flood run) companion to the model tests: it pins the cap
conditions the loop uses and the way the run report flags a truncated, still-leaking run. A
regression that let the growth run past max_iterations / max_extent, or stop silently, is caught
here in the fast gate rather than only in a multi-minute model run.
"""
from nd_breach_runner.core import workflow as W
from nd_breach_runner.tests.registry import test

MILE = 1609.344


@test
def growth_stops_at_the_domain_run_cap_while_the_flood_is_leaving():
    over = ["east", "south"]                      # flat terrain: edges still shedding water
    # it climbs 1..max_iterations; it only stops at the last run, and only while `over`
    assert not W.hit_run_cap(1, 3, over), "must keep growing before the last allowed run"
    assert not W.hit_run_cap(2, 3, over)
    assert W.hit_run_cap(3, 3, over), "must stop at the last allowed domain run"
    # a converged run (nothing leaving) is NOT a cap stop, even at the last run
    assert not W.hit_run_cap(3, 3, []), "a run with nothing leaving is convergence, not a cap"


@test
def growth_stops_at_the_maximum_extent_cap_while_the_flood_is_leaving():
    cap = 20.0 * MILE                             # the default max_extent_mi, in metres
    over = ["east"]
    assert not W.hit_extent_cap(10.0 * MILE, 8.0 * MILE, cap, over), "within the cap: keep growing"
    assert W.hit_extent_cap(21.0 * MILE, 8.0 * MILE, cap, over), "past the cap on width: stop"
    assert W.hit_extent_cap(8.0 * MILE, 25.0 * MILE, cap, over), "past the cap on height: stop"
    assert not W.hit_extent_cap(50.0 * MILE, 50.0 * MILE, cap, []), "nothing leaving: not a cap stop"


@test
def a_run_that_stops_with_outflow_still_leaving_is_flagged_a_minimum():
    assert W.extent_is_minimum(40.0) is True, "40% leaving -> the extent is a minimum"
    assert W.extent_is_minimum(W.OUTFLOW_WARN_PCT) is True, "at the warn threshold -> minimum"
    assert W.extent_is_minimum(W.OUTFLOW_WARN_PCT - 0.1) is False, "below the threshold -> not"
    assert W.extent_is_minimum(0.0) is False
    assert W.extent_is_minimum(None) is False, "no outflow figure -> no claim"


@test
def the_extent_cap_warning_names_the_cap_the_edges_and_the_fix():
    msg = W.max_extent_warning(20.0, ["east", "south"])
    assert "20 mi" in msg, "the warning must name the cap so a truncated run is not read as final"
    assert "east" in msg and "south" in msg, "it must name the edges the flood is still leaving"
    assert "TRUNCATED" in msg and "re-run" in msg.lower(), "it must say it is cut off and how to fix"
    assert "understated" in msg.lower(), "it must say the hazard may be understated"
