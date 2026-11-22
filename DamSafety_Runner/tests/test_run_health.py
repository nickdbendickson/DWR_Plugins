"""The run health card (core/run_health.py, Runner 3.46.0): one pass / warn / fail line from what
the run report already records. Made-up report dicts; no model run."""
from nd_breach_runner.core import run_health as rh
from nd_breach_runner.tests.registry import test


def _rep(**kw):
    r = {"flood_engine": "LISFLOOD-FP (inertial solver)", "flood_engine_short": "LISFLOOD-FP",
         "warnings": [], "lisflood_runs": [{"dem_coverage": 1.0, "spread": {"still_spreading": False},
                                           "stats": {"max_volume_error_pct": 0.01,
                                                     "velocity_written": True}}]}
    r.update(kw)
    return r


def _st(h, name):
    return next((c["status"] for c in h["checks"] if c["name"] == name), None)


@test
def run_health_clean_run_passes():
    h = rh.assess(_rep())
    assert h["status"] == "pass", h
    assert _st(h, "extent") == "pass" and _st(h, "mass balance") == "pass"
    assert rh.card_line(h).startswith("**Run health: PASS**")


@test
def run_health_flags_extent_mass_and_engine():
    h = rh.assess(_rep(cut_off={"reason": "domain-run limit"}, boundary_outflow_pct=80.0))
    assert h["status"] == "fail" and _st(h, "extent") == "fail"
    assert "80%" in rh.card_line(h) and rh.card_line(h).index("extent FAIL") < rh.card_line(h).index("engine")
    h = rh.assess(_rep(extent_is_minimum=True, boundary_outflow_pct=12.0))
    assert h["status"] == "warn" and _st(h, "extent") == "warn"
    runs = [{"stats": {"max_volume_error_pct": 1.6}, "spread": {"still_spreading": True},
             "dem_coverage": 0.9}]
    h = rh.assess(_rep(lisflood_runs=runs))
    assert _st(h, "mass balance") == "fail" and _st(h, "spreading") == "warn"
    assert _st(h, "terrain") == "warn" and h["status"] == "fail"
    h = rh.assess(_rep(flood_engine="Built-in engine (local inertial, numpy; NOT LISFLOOD-FP)"))
    assert _st(h, "engine") == "warn" and h["status"] == "warn"
    h = rh.assess(_rep(warnings=["The simulation duration (48.0 h) is shorter than the breach "
                                 "hydrograph (49.1 h); the rest is not run."]))
    assert _st(h, "duration") == "warn"


@test
def run_health_skips_what_the_report_lacks():
    h = rh.assess({})
    assert h == {"status": "pass", "checks": []} and rh.card_line(h) == ""
