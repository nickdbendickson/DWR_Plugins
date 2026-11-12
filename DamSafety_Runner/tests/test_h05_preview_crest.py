"""H05: the pre-flight plan (workflow.preview) must not write a guessed crest onto the dam.

When a dam has no top_of_dam_ft, preview estimates one for the run-time/plan hydrograph. It
used to write that guess back onto the caller's dam with nanmax (the single highest cell within
30 m - a nearby hill, road crown or spike reads the crest several feet high). The real run then
skipped its own crest estimator and recorded the guess as a surveyed crest; an over-read crest
can let the PMF route without overtopping, so the dam reads "no breach" and is dropped silently.

preview now works on a copy of the dam (no write-back) and, when it must estimate, uses the
suite crest rule (98th percentile within 30 m), not nanmax.
"""
from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import workflow
from nd_breach_runner.core.breach import DamData
from nd_breach_runner.tests.synthetic import SyntheticTerrain


def _dam(top=None):
    return DamData(dam_id="T1", dam_height_ft=25.0, storage_top_acft=500.0,
                   top_of_dam_ft=top, surface_area_top_ac=60.0)


def _settings():
    return workflow.RunSettings(cell_size_m=10.0, initial_length_mi=1.0, half_width_mi=0.5,
                                max_iterations=1, products=False, sim_time_hr=3.0)


@test
def h05_preview_does_not_write_a_crest_onto_the_caller_dam():
    d = _dam(top=None)
    plan = workflow.preview(d, (0.0, 0.0), _settings(), SyntheticTerrain())
    # the plan is produced (the copy + 98th-percentile estimate path works end to end) ...
    assert plan.get("breach_occurs") is True, plan
    # ... but the caller's dam is left exactly as it was: no guessed crest leaks into the real
    # run or into dam.json as if it were surveyed.
    assert d.top_of_dam_ft is None, d.top_of_dam_ft
    # a second plan still does not mutate it
    workflow.preview(d, (0.0, 0.0), _settings(), SyntheticTerrain())
    assert d.top_of_dam_ft is None, d.top_of_dam_ft


@test
def h05_preview_keeps_a_given_crest():
    d = _dam(top=1850.0)
    workflow.preview(d, (0.0, 0.0), _settings(), SyntheticTerrain())
    assert d.top_of_dam_ft == 1850.0, d.top_of_dam_ft
