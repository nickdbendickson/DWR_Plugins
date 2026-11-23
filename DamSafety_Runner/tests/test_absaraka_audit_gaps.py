"""Absaraka audit gaps (2026-11-22): a piping method started by a pool already over the crest
gets a note, and the downstream-extent checklist row reads the run report's own cut_off /
extent_is_minimum flags. Notes and flags only: no depth, extent, peak or class changes."""
from nd_breach_runner.core import breach as br
from nd_breach_runner.core import report_docx as rd
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test


def _dam():
    return br.DamData(dam_id="DWR-2801", name="Swan Buffalo Detention Dam 12",
                      dam_height_ft=28.0, storage_top_acft=1615.496,
                      top_of_dam_ft=1069.117, surface_area_top_ac=154.5)


@test
def piping_method_over_the_crest_gets_a_note():
    d = _dam()
    c = br._common(d)
    ov = br.BreachOverrides()
    _, _, notes = br.make_opening(br.METHOD_PIPING, c, d, ov, c.top_m + 2.0 * FT)
    assert any("ABOVE the crest" in n for n in notes), notes
    # at the top of dam (the ND 5.1.1 sunny-day start) there is no such note
    _, _, notes = br.make_opening(br.METHOD_PIPING, c, d, ov, c.top_m)
    assert not any("ABOVE the crest" in n for n in notes), notes


@test
def extent_flags_read_the_run_report():
    assert rd.extent_flags({}) == ("", False, None)
    assert rd.extent_flags({"cut_off": {"reason": "maximum extent"}})[0] == "maximum extent"
    assert rd.extent_flags({"cut_off": True})[0] == "cut off"
    r, emin, pct = rd.extent_flags({"extent_is_minimum": True, "boundary_outflow_pct": 37.2})
    assert r == "" and emin and pct == 37.2
