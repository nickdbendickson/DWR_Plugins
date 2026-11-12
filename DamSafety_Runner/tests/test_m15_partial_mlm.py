# -*- coding: utf-8 -*-
"""M15 (MLM, signed off 2026-11-09): the MacDonald & Langridge-Monopolis embankment cross-section
A is measured over the BREACH height (crest to breach bottom), not the full dam height. MLM's
average width is eroded volume / A, so a partial (raised-bottom) breach that kept the full-section
A published a width biased LOW. A full breach snaps to the dam height and is unchanged. TR-60's A
is held at the full section.
"""
import types

from nd_breach_runner.core import breach as br
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test


def _dam(crest_len=400.0, crest_w=12.0):
    return br.DamData(dam_id="M15", dam_height_ft=50.0, storage_top_acft=5000.0,
                      top_of_dam_ft=1000.0, crest_width_ft=crest_w, us_slope_h=3.0,
                      ds_slope_h=3.0, crest_length_ft=crest_len)


def _ctx(depth_frac):
    top_m = 1000.0 * FT
    return types.SimpleNamespace(top_m=top_m, bottom_m=top_m - (50.0 * depth_frac) * FT,
                                 wse0_m=top_m, vw=5.0e6)


def _mlm(dam, depth_frac):
    return br.breach_parameters(br.METHOD_MLM, _ctx(depth_frac), dam, br.BreachOverrides())


@test
def m15_mlm_partial_breach_uses_breach_height_area():
    dam = _dam()
    c = _ctx(0.5)                                            # 25 ft breach of a 50 ft dam
    b, tf, z, src, mode, notes = br.breach_parameters(br.METHOD_MLM, c, dam, br.BreachOverrides())
    ver = 0.0261 * (c.vw * (c.wse0_m - c.bottom_m)) ** 0.769
    a_breach = br.embankment_area_ft2(25.0, 12.0, 3.0, 3.0) * FT * FT     # over the breach height
    a_full = br.embankment_area_ft2(50.0, 12.0, 3.0, 3.0) * FT * FT       # the old (full) section
    assert abs(b - ver / a_breach) < 1e-6, (b, ver / a_breach)           # uses the breach-height A
    assert abs(b - ver / a_full) > 1.0, b                                # NOT the full-section A
    assert any("measured over the breach height" in n for n in notes), notes


@test
def m15_mlm_full_breach_bit_identical_to_full_section():
    dam = _dam()
    cf = _ctx(1.0)                                           # breach to the foundation
    b, tf, z, src, mode, notes = br.breach_parameters(br.METHOD_MLM, cf, dam, br.BreachOverrides())
    ver = 0.0261 * (cf.vw * (cf.wse0_m - cf.bottom_m)) ** 0.769
    a_full = br.embankment_area_ft2(50.0, 12.0, 3.0, 3.0) * FT * FT
    assert abs(b - ver / a_full) < 1e-6, (b, ver / a_full)              # full dam section (snapped)
    assert not any("measured over the breach height" in n for n in notes), notes


@test
def m15_width_sanity_note_fires_only_above_crest_length():
    # a shallow partial breach can return a width wider than the dam; flag it (publish anyway)
    wide = _mlm(_dam(crest_len=50.0), 0.5)[5]               # 50 ft crest -> width exceeds it
    ok = _mlm(_dam(crest_len=4000.0), 0.5)[5]              # huge crest -> no sanity note
    fired = lambda notes: any("exceeds the" in n and "crest length" in n for n in notes)
    assert fired(wide), wide
    assert not fired(ok), ok
    # a FULL breach (not partial) never gets the sanity note, even if its width exceeds the crest
    # length - so a full breach stays bit-identical and is never mislabelled "partial".
    full = _mlm(_dam(crest_len=50.0), 1.0)                 # full breach, width > 50 ft crest
    assert full[5][0].startswith("MacDonald"), full[5]    # it does compute an MLM width
    assert not fired(full[5]), full[5]


@test
def m15_tr60_embankment_area_held_at_full_section():
    # the MLM-only change must not move TR-60: _embankment_area with no height is the full section
    dam = _dam()
    assert abs(br._embankment_area(dam) - 8100.0) < 1e-6, br._embankment_area(dam)
    assert abs(br._embankment_area(dam, 25.0) - 2175.0) < 1e-6, br._embankment_area(dam, 25.0)


def _tr60_dam(bottom=None):
    # a 50 ft dam; breach_bottom_ft=975 raises the breach 25 ft above the reservoir bottom (950)
    return br.DamData(dam_id="M15T", dam_height_ft=50.0, storage_top_acft=5000.0,
                      top_of_dam_ft=1000.0, crest_width_ft=12.0, us_slope_h=3.0, ds_slope_h=3.0,
                      crest_length_ft=400.0, breach_bottom_ft=bottom)


@test
def m15_tr60_partial_breach_note_follows_tr60():
    # NRCS TR-210-60 defines A (the Br = Vs*Hw/A denominator) as the dam's template (full) cross-
    # section, not the breach cross-section. So the partial-breach disclosure must NOT claim the
    # peak is "biased LOW": A is held at the full section BY THE METHOD, and the partial breach is
    # flagged as outside TR-60's foundation-breach basis (an extrapolation).
    hy = br.tr60_hydrograph(_tr60_dam(bottom=975.0))              # 25 ft breach of a 50 ft dam
    note = " ".join(hy.notes)
    assert "template (full-height) cross" in note, hy.notes
    assert "outside its basis" in note, hy.notes
    assert "biased LOW" not in note and "biased low" not in note, hy.notes
    # the regulated number is unchanged: A is still the full-section template area
    assert hy.params["embankment_area_ft2"] == round(br.embankment_area_ft2(50.0, 12.0, 3.0, 3.0), 0), \
        hy.params["embankment_area_ft2"]


@test
def m15_tr60_full_breach_no_partial_note():
    # a breach to the foundation is within TR-60's basis -> no partial-breach disclosure
    hy = br.tr60_hydrograph(_tr60_dam(bottom=None))
    assert not any("outside its basis" in n for n in hy.notes), hy.notes


if __name__ == "__main__":
    m15_mlm_partial_breach_uses_breach_height_area()
    m15_mlm_full_breach_bit_identical_to_full_section()
    m15_width_sanity_note_fires_only_above_crest_length()
    m15_tr60_embankment_area_held_at_full_section()
    m15_tr60_partial_breach_note_follows_tr60()
    m15_tr60_full_breach_no_partial_note()
    print("test_m15_partial_mlm: all tests passed")
