"""The shared ND 5.2 / ACER TM 11 classification module.

The chart FILE used here carries TEST SHAPES with round numbers, not the ACER TM 11 figures.
Since nd_hazard 1.4.0 (audit P1) the zone of every point comes from the suite's digitized
REG_05.2026 table (damsuite.tm11_curves), the rule the DamSafety Hazard plugin classifies with;
the polygons only draw the review figures. The points below are therefore chosen on the TABLE:
house chart at 1 ft/s low < 2.916 ft <= judgment < 5.968 ft <= high; mobile chart at 1 ft/s
low < 1.905 ft <= judgment < 2.499 ft <= high. The invented polygons' own zones are checked
through DangerChart.polygon_zone (comparison only)."""
import json
import os
import tempfile
import xml.dom.minidom

from nd_breach_runner.core import nd_hazard as nh
from nd_breach_runner.tests.registry import test


def test_charts(tol=0.01):
    # high danger above the line (0 ft/s, 6 ft) - (20 ft/s, 1 ft); judgment between that and
    # (0, 3) - (20, 0.5) - (25, 0.5). Invented shapes for testing only.
    house = nh.DangerChart(
        "house", "TEST house chart", "test", 25.0, 10.0,
        high=[(0, 6), (20, 1), (25, 1), (25, 10), (0, 10)],
        judgment=[(0, 3), (20, 0.5), (25, 0.5), (25, 1), (20, 1), (0, 6)], tolerance=tol)
    mobile = nh.DangerChart(
        "mobile", "TEST mobile chart", "test", 16.0, 3.0,
        high=[(0, 2), (10, 0.5), (16, 0.5), (16, 3), (0, 3)],
        judgment=[(0, 1), (10, 0.25), (16, 0.25), (16, 0.5), (10, 0.5), (0, 2)], tolerance=tol)
    return nh.ChartSet(status="digitized", digitized_by="test", checked_by="test",
                       charts={"house": house.validate(), "mobile": mobile.validate()},
                       path="(test)", sha256="0" * 64)

test_charts.__test__ = False     # a fixture factory used by the registry tests, not a pytest test


@test
def nd_hazard_zones_on_a_test_chart():
    ch = test_charts().charts["house"]
    assert ch.zone(1.0, 1.0)[0] == nh.ZONE_LOW
    assert ch.zone(4.0, 1.0)[0] == nh.ZONE_JUDGMENT
    assert ch.zone(8.0, 1.0)[0] == nh.ZONE_HIGH
    assert ch.zone(1.2, 22.0)[0] == nh.ZONE_HIGH
    z, note = ch.zone(12.0, 0.0)                       # deeper than the axis
    assert z == nh.ZONE_HIGH and "beyond the chart" in note
    z, note = ch.zone(0.3, 30.0)                       # faster than the axis
    assert z == nh.ZONE_HIGH and "beyond" in note
    # 1.4.0 (audit P1): the zone is the TABLE's, whatever polygons the chart file carries - a
    # depth ON the boundary takes the higher zone (ND 5.3), a depth just below it does not
    assert ch.zone(5.968, 1.0)[0] == nh.ZONE_HIGH and ch.zone(5.967, 1.0)[0] == nh.ZONE_JUDGMENT
    assert ch.zone(2.916, 1.0)[0] == nh.ZONE_JUDGMENT and ch.zone(2.915, 1.0)[0] == nh.ZONE_LOW
    # the old polygon rule (comparison only): just below the test polygons' high edge at v=0
    # (6 ft), within 1% of the 10 ft axis -> high; without the tolerance -> judgment
    z, note = ch.polygon_zone(5.95, 0.0)
    assert z == nh.ZONE_HIGH and "boundary" in note
    z, _ = nh.DangerChart(**dict(ch.__dict__, tolerance=0.0)).polygon_zone(5.95, 0.0)
    assert z == nh.ZONE_JUDGMENT
    # the Hazard's near-threshold flag: 0.07 ft below the high boundary at 0 ft/s (5.968) is
    # "near" high within the 0.5 ft default, not moved; beyond the axes never near
    z, note, near, m = ch.near(5.9, 0.0)
    assert z == nh.ZONE_JUDGMENT and near == nh.ZONE_HIGH and abs(m - 0.068) < 1e-9 and not note
    assert ch.near(5.0, 0.0)[2] == "" and ch.near(2.5, 1.0)[2] == nh.ZONE_JUDGMENT
    assert ch.near(5.9, 0.0, near_ft=0.0)[2] == "" and ch.near(12.0, 0.0)[2] == ""
    mh, mj = ch.margins(2.0, 1.0)
    assert abs(mh - 3.968) < 1e-9 and abs(mj - 0.916) < 1e-9
    # the shipped chart file's polygons ARE the table; the invented ones are not
    assert nh.load_charts().charts["house"].table_mismatch_ft() < 1e-9
    assert ch.table_mismatch_ft() > 0.1
    try:
        nh.DangerChart(**dict(ch.__dict__, key="vehicle")).zone(1.0, 1.0)
        raise AssertionError("expected ValueError for a chart without a table")
    except ValueError as e:
        assert "house / mobile" in str(e)


@test
def nd_hazard_structures_follow_5_2_1_1_and_5_3():
    cs = test_charts()
    S = nh.Structure
    c = nh.classify([S("a", 1.0, 1.0)], charts=cs)
    assert c.hazard_class == nh.MEDIUM and c.governing[0].section == "5.2.2"
    assert any("Engineer" in n for n in c.notes)
    # 1.3.0 (Nick): a habitable structure in the judgment zone is MEDIUM by rule
    c = nh.classify([S("a", 1.0, 1.0), S("b", 4.0, 1.0)], charts=cs)
    assert c.hazard_class == nh.MEDIUM and not c.borderline_raised
    assert c.counts["judgment_zone"] == 1 and c.counts["habitable_lethal"] == 0
    jf = [f for f in c.findings if f.zone == nh.ZONE_JUDGMENT][0]
    assert jf.level == nh.MEDIUM and "MEDIUM by rule" in jf.reason and "5.3" not in jf.section
    # the old option: the judgment zone counts as potentially lethal (HIGH, 5.3)
    old = nh.Settings(judgment_zone_high=True)
    c = nh.classify([S("a", 1.0, 1.0), S("b", 4.0, 1.0)], settings=old, charts=cs)
    assert c.hazard_class == nh.HIGH and c.borderline_raised           # judgment zone only
    assert c.counts["judgment_zone"] == 1 and "5.3" in c.governing[0].section
    c = nh.classify([S("b", 8.0, 1.0)], charts=cs)
    assert c.hazard_class == nh.HIGH and not c.borderline_raised
    # a mobile home uses its own chart: 2.0 ft at 1 ft/s is judgment there (low >= 1.905),
    # low danger for a house (low >= 2.916)
    assert nh.classify([S("m", 2.0, 1.0, mobile=True)], charts=cs).hazard_class == nh.MEDIUM
    assert nh.classify([S("m", 2.0, 1.0, mobile=True)], settings=old,
                       charts=cs).hazard_class == nh.HIGH
    assert nh.classify([S("h", 2.0, 1.0)], charts=cs).hazard_class == nh.MEDIUM
    assert nh.classify([S("h", 2.0, 1.0)], charts=cs).findings[0].zone == nh.ZONE_LOW
    assert nh.classify([S("m", 2.0, 1.0, mobile=True)], charts=cs).findings[0].zone == \
        nh.ZONE_JUDGMENT
    # uninhabited building, and dry structures
    c = nh.classify([S("barn", 5.0, 3.0, habitable=False), S("dry", 0.05, 0.0)], charts=cs)
    assert c.hazard_class == nh.LOW and c.counts["structures_wet"] == 1
    c = nh.classify([], charts=cs)
    assert c.hazard_class == nh.LOW and "Nothing listed" in c.notes[0]


@test
def nd_hazard_borderline_option_matches_the_hazard_plugin():
    """1.4.0 (audit P1): a structure within near_ft (0.5 ft, the Hazard's
    DEFAULT_NEAR_THRESHOLD_FT) below the next boundary is noted as near it; it is moved into
    that zone only with borderline_higher (off by default, as in the Hazard). The 0.01-axis
    tolerance band of 1.0.0 - 1.3.0 no longer decides anything."""
    cs = test_charts()
    S = nh.Structure
    # 5.9 ft at 0 ft/s: 0.068 ft below the house high boundary (5.968)
    c = nh.classify([S("a", 5.9, 0.0)], charts=cs)
    f = c.findings[0]
    assert f.level == nh.MEDIUM and f.zone == nh.ZONE_JUDGMENT and not f.borderline
    assert f.near == nh.ZONE_HIGH and f.margin_ft == 0.07 and "not moved" in f.reason
    assert c.counts["near_boundary"] == 1 and not c.borderline_raised
    assert any("Zone rule" in n and "classifier of record" in n and "noted, not moved" in n
               for n in c.notes)
    # the Hazard's option on: moved into the high danger zone, borderline (5.3(f))
    c = nh.classify([S("a", 5.9, 0.0)], settings=nh.Settings(borderline_higher=True), charts=cs)
    f = c.findings[0]
    assert f.level == nh.HIGH and f.zone == nh.ZONE_HIGH and f.borderline and f.near == \
        nh.ZONE_HIGH
    assert "moved from the judgment zone" in f.reason and "5.3(f)" in f.reason
    assert c.borderline_raised and any("were moved" in n for n in c.notes)
    # low danger near the judgment boundary: noted; moved with the option
    c = nh.classify([S("b", 2.5, 1.0)], charts=cs)
    assert c.findings[0].zone == nh.ZONE_LOW and c.findings[0].near == nh.ZONE_JUDGMENT
    c = nh.classify([S("b", 2.5, 1.0)], settings=nh.Settings(borderline_higher=True), charts=cs)
    assert c.findings[0].zone == nh.ZONE_JUDGMENT and c.findings[0].level == nh.MEDIUM
    # a wider or no threshold; a negative one is refused
    c = nh.classify([S("a", 5.0, 0.0)], settings=nh.Settings(near_ft=1.0), charts=cs)
    assert c.findings[0].near == nh.ZONE_HIGH
    c = nh.classify([S("a", 5.9, 0.0)], settings=nh.Settings(near_ft=0.0), charts=cs)
    assert c.findings[0].near == "" and any("flag off" in n for n in c.notes)
    try:
        nh.Settings(near_ft=-0.1).validate()
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "near-threshold" in str(e)
    # the old rule would have put 5.9 ft at 0 ft/s in the high zone (within 0.1 ft of the
    # polygon edge); every such point now agrees with the Hazard's table rule
    assert cs.charts["house"].polygon_zone(5.9, 0.0)[0] == nh.ZONE_HIGH
    rows = nh.finding_rows(c)
    assert "near" in rows[0] and "margin_ft" in rows[0]


@test
def nd_hazard_1d_rule_and_conservative_method():
    S = nh.Structure
    st = nh.Settings(lethality=nh.LETHALITY_1D)
    assert nh.classify([S("a", 2.0, 9.0)], settings=st).hazard_class == nh.MEDIUM   # not > 2
    assert nh.classify([S("a", 2.01, 0.0)], settings=st).hazard_class == nh.HIGH
    st = nh.Settings(lethality=nh.LETHALITY_ALL_WET)
    c = nh.classify([S("a", 0.2, 0.1)], settings=st)
    assert c.hazard_class == nh.HIGH and any("over-classifies" in n for n in c.notes)


@test
def nd_hazard_roads_railroads_lifelines_and_cascade():
    cs = test_charts()
    R, RR = nh.Road, nh.Railroad
    k = lambda **kw: nh.classify(charts=cs, **kw)
    assert k(roads=[R("r", 0.5, aadt=300)]).hazard_class == nh.LOW
    assert k(roads=[R("r", 0.5, aadt=500)]).hazard_class == nh.MEDIUM
    assert k(roads=[R("r", 0.5, aadt=2000)]).hazard_class == nh.MEDIUM
    assert k(roads=[R("r", 0.5, aadt=2001)]).hazard_class == nh.HIGH
    assert k(roads=[R("r", 0.5, interstate=True)]).hazard_class == nh.HIGH
    assert k(roads=[R("r", 0.5, private=True)]).hazard_class == nh.LOW
    assert k(roads=[R("r", 0.5, aadt=50, sole_access=True)]).hazard_class == nh.MEDIUM
    assert k(roads=[R("r", 0.05, aadt=9000)]).hazard_class == nh.LOW       # not overtopped
    c = k(roads=[R("r", 0.5)])
    assert c.hazard_class == nh.MEDIUM and c.data_gaps and c.borderline_raised
    c = nh.classify(roads=[R("r", 0.5)], charts=cs, settings=nh.Settings(unknown_aadt=nh.HIGH))
    assert c.hazard_class == nh.HIGH
    assert k(railroads=[RR("x", 0.2, trains_per_day=3)]).hazard_class == nh.MEDIUM
    assert k(railroads=[RR("x", 0.2, trains_per_day=10)]).hazard_class == nh.HIGH
    assert k(railroads=[RR("x", 0.2, passenger=True)]).hazard_class == nh.HIGH
    c = k(railroads=[RR("x", 0.2)])
    assert c.hazard_class == nh.MEDIUM and c.data_gaps
    assert k(lifelines=[nh.Lifeline("sub", 0.3, "substation")]).hazard_class == nh.MEDIUM
    c = k(downstream_dams=[nh.DownstreamDam("D2", "Significant")])
    assert c.hazard_class == nh.MEDIUM and c.governing[0].section == "5.3 cascading"
    assert k(downstream_dams=[nh.DownstreamDam("D2", "High", affected=False)]).hazard_class \
        == nh.LOW
    assert k(downstream_dams=[nh.DownstreamDam("D2", "??")]).data_gaps


@test
def nd_hazard_refuses_undigitized_charts():
    cs = nh.load_charts()                         # the file shipped with the plugin
    if cs.ready:
        return                                    # digitized: nothing to refuse
    try:
        nh.classify([nh.Structure("a", 3.0, 2.0)], charts=cs)
        raise AssertionError("expected ChartsNotDigitized")
    except nh.ChartsNotDigitized as e:
        assert "not digitized" in str(e) and "1D" in str(e)
    # dry everywhere still needs the charts (they would be used for any wet structure)
    try:
        nh.classify([], charts=cs)
        raise AssertionError("expected ChartsNotDigitized")
    except nh.ChartsNotDigitized:
        pass


@test
def nd_hazard_chart_file_round_trip_and_validation():
    cs = test_charts()
    d = tempfile.mkdtemp()
    p = nh.save_charts(os.path.join(d, "c.json"), cs)
    back = nh.load_charts(p)
    assert back.ready and back.sha256 and len(back.sha256) == 64
    assert back.charts["house"].zone(4.0, 1.0)[0] == nh.ZONE_JUDGMENT
    raw = json.load(open(p))
    raw["charts"]["house"]["high"][0] = [40, 1]                    # outside the axes
    json.dump(raw, open(p, "w"))
    try:
        nh.load_charts(p)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "outside the chart axes" in str(e)
    raw["charts"].pop("house")
    json.dump(raw, open(p, "w"))
    try:
        nh.load_charts(p)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "not both present" in str(e)


@test
def nd_hazard_outputs_markdown_rows_and_chart_svg():
    cs = test_charts()
    S = nh.Structure
    structs = [S("a", 1.0, 1.0), S("b", 4.0, 2.0), S("c <&>", 12.0, 3.0)]
    c = nh.classify(structs, roads=[nh.Road("r1", 0.4)], charts=cs)
    md = nh.markdown(c, "ND-1")
    assert "**Class: HIGH**" in md and "Data gaps" in md and "Governing" in md
    assert "Department of Water Resources" in md
    rows = nh.finding_rows(c)
    assert len(rows) == 4 and set(rows[0]) == set(nh.FINDING_COLUMNS)
    assert len(rows[0]) == 14
    json.dumps(c.to_dict())                                         # serialisable
    svg = nh.chart_svg(cs.charts["house"], structs)
    xml.dom.minidom.parseString(svg)
    assert svg.count("<circle") == 3 and "c &lt;&amp;&gt;" in svg


@test
def nd_hazard_nan_and_none_depths_are_dry():
    cs = test_charts()
    nan = float("nan")
    c = nh.classify([nh.Structure("a", nan, nan), nh.Structure("b", None)],
                    roads=[nh.Road("r", nan, aadt=9000)], railroads=[nh.Railroad("x", None)],
                    lifelines=[nh.Lifeline("l", nan)], charts=cs)
    assert c.hazard_class == nh.LOW and c.counts["structures_wet"] == 0


@test
def nd_hazard_attribute_rules_shared_by_both_plugins():
    assert nh.aadt_value("1,200") is None and nh.aadt_value("1200") == 1200.0
    assert nh.aadt_value(float("nan")) is None and nh.aadt_value(0) is None
    assert nh.aadt_value(-1) is None and nh.aadt_value(None) is None
    for t in ("Interstate 94", "I-94", "I 94", "I94", "IH-29", "I", "i- 94"):
        assert nh.interstate_from_text(t), t
    for t in ("Industrial Rd", "I-Street", "US 2", "ND 1804", "", None):
        assert not nh.interstate_from_text(t), t
    for v in ("A", "b", "yes", "True", "1", True):
        assert nh.passenger_from_value(v), v
    for v in ("T", "X", "F", "", None, "no"):
        assert not nh.passenger_from_value(v), v
    for t in ("RES1", "Residential", "Agriculture", "Heavy", "Storage", "", None, "Vacant"):
        assert nh.habitable_from_text(t), t
    for t in ("Barn", "Detached garage", "Shed", "Grain Bin", "oil well pad", "Uninhabited"):
        assert not nh.habitable_from_text(t), t
    assert nh.mobile_from_text("Manufactured Home") and nh.mobile_from_text("RES2")
    assert not nh.mobile_from_text("RES21") and not nh.mobile_from_text("RES1")
    assert nh.class_from_text("Significant") == nh.MEDIUM and nh.class_from_text("H") == nh.HIGH
    assert nh.class_from_text("Intermediate") == nh.MEDIUM and nh.class_from_text("U") is None


@test
def nd_hazard_nan_aadt_unknown_velocity_and_zero_wet_depth():
    cs = test_charts()
    c = nh.classify(roads=[nh.Road("r", 0.5, aadt=float("nan"))], charts=cs)
    assert c.hazard_class == nh.MEDIUM and c.data_gaps                    # not Low
    c = nh.classify([nh.Structure("a", 1.0, float("nan"))], charts=cs)
    assert c.hazard_class == nh.HIGH and c.data_gaps and c.borderline_raised
    c = nh.classify([nh.Structure("a", 1.0, None)], charts=cs)
    assert c.hazard_class == nh.HIGH
    for bad in (0.0, -1.0, float("nan")):
        try:
            nh.Settings(wet_ft=bad).validate()
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "at least" in str(e)
    c = nh.classify(downstream_dams=[nh.DownstreamDam("D", "U")], charts=cs)
    assert c.hazard_class == nh.MEDIUM and c.data_gaps


@test
def nd_hazard_charts_need_a_signing_engineer():
    cs = test_charts()
    cs.checked_by = ""
    assert cs.ready and cs.signer == "test"           # one engineer is enough (suite 1.14.1)
    cs.digitized_by = ""
    assert not cs.ready
    try:
        nh.classify([nh.Structure("a", 1.0, 1.0)], charts=cs)
        raise AssertionError("expected ChartsNotDigitized")
    except nh.ChartsNotDigitized as e:
        assert "signed off" in str(e) and "1D model" in str(e)
    cs.signed_off_by = "another engineer"
    assert cs.ready and cs.signer == "another engineer"


@test
def nd_hazard_shipped_charts_are_signed_off():
    from nd_breach_runner.damsuite import tm11_curves as tm
    cs = nh.load_charts()                         # the file shipped with the plugin
    assert cs.ready and cs.status == "digitized" and cs.signer == "Nicholas Bendickson"
    assert cs.signed_on == "2026-10-12" and cs.info()["signed_off_by"] == "Nicholas Bendickson"
    names = {tm.LOW: nh.ZONE_LOW, tm.JUDGMENT: nh.ZONE_JUDGMENT, tm.HIGH: nh.ZONE_HIGH}
    n = 0
    for kind, key in (("house", nh.CHART_HOUSE), ("mobile", nh.CHART_MOBILE)):
        vmax, dmax = tm.AXES[kind]
        for i in range(0, 21):
            v = vmax * i / 20.0
            for j in range(1, 21):
                d = dmax * j / 20.0
                # 1.4.0: no tolerance band any more - every point, on or next to a line too
                assert cs.charts[key].zone(d, v)[0] == names[tm.zone(kind, d, v)], (kind, d, v)
                n += 1
    assert n == 2 * 21 * 20


@test
def nd_hazard_sampling_matches_the_runner_tool():
    """The shared samplers (Screening, Runner core/nd_hazard.py) and the runner tool's Sampler
    give the same depth and the same classifying velocity for the same raster and footprint:
    the peak depth and the peak velocity over the wet cells, even from different cells (TM 11
    practice, Nick 2026-11-01, option C; reverts db9c665's same-cell pairing that #246 /
    3af4328 copied into the Runner). vel_at_max_fts (the deepest cell's) is information only."""
    import numpy as np
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms_ndhazard as an
    importlib.reload(an)
    from nd_breach_runner.core.grid import Grid
    rng = np.random.default_rng(3)
    d = rng.uniform(0, 4, (30, 40))
    d[d < 1.0] = 0.0
    v = rng.uniform(0, 9, (30, 40))
    v[5, 5] = np.nan
    g = Grid(np.zeros((30, 40)), 100.0, 700.0, 10.0)
    s = an.Sampler(g, d, v, wet_ft=0.1)
    edge = 5.0 * 0.3048                           # the grid is metres here
    for _ in range(40):
        cx, cy = rng.uniform(110, 480), rng.uniform(420, 690)
        w, h = rng.uniform(3, 60, 2)
        ring = [(cx, cy), (cx + w, cy), (cx + w, cy + h), (cx, cy + h)]
        a = s.polygon([np.array(ring)], (cx + w / 2, cy + h / 2))
        dd, vv, _how = nh.sample_footprint(d, v, 100.0, 700.0, 10.0, ring, 0.1)
        assert abs(a["depth_ft"] - dd) < 1e-9, (a, dd)
        assert (a["vel_fts"] is None and vv is None) or abs(a["vel_fts"] - vv) < 1e-9, (a, vv)
        if a["vel_at_max_fts"] is not None and vv is not None:
            assert a["vel_fts"] >= a["vel_at_max_fts"] - 1e-9, a
        # 3.16.9 (audit P1): a STRUCTURE reads the footprint + the 5 ft edge ring in both tools
        b = s.polygon([np.array(ring)], (cx + w / 2, cy + h / 2), structure=True)
        de, ve, how_e = nh.sample_footprint(d, v, 100.0, 700.0, 10.0, ring, 0.1,
                                            1.0 / 0.3048, edge)
        assert abs(b["depth_ft"] - de) < 1e-9 and "edge ring" in how_e and "edge ring" in b["how"]
        assert (b["vel_fts"] is None and ve is None) or abs(b["vel_fts"] - ve) < 1e-9, (b, ve)
        assert b["cells"] >= a["cells"] and b["depth_ft"] >= a["depth_ft"] - 1e-9
    # peak depth and peak velocity from different cells: both tools classify on the pair
    d2 = np.zeros((3, 3)); v2 = np.zeros((3, 3))
    d2[1, 0], v2[1, 0] = 3.0, 1.0          # deepest cell, slow
    d2[1, 2], v2[1, 2] = 0.5, 8.0          # shallower wet cell, fast
    d2[0, 1], v2[0, 1] = 0.05, 12.0        # trace-depth wet cell (d > 0), still fast
    g2 = Grid(np.zeros((3, 3)), 0.0, 30.0, 10.0)
    ring2 = [(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (0.0, 30.0)]
    # H07: the classifying velocity is the peak over ALL wet (d > 0) cells, matching Hazard's
    # structures.peak_pair - the trace-depth 0.05 ft cell at 12 ft/s now counts, so vel is 12.0
    # (was 8.0 under the old wet_ft=0.1 floor). Peak depth (3.0) and vel_at_max (1.0) unchanged.
    a2 = an.Sampler(g2, d2, v2, wet_ft=0.1).polygon([np.array(ring2)], (15.0, 15.0))
    assert (a2["depth_ft"], a2["vel_fts"], a2["vel_at_max_fts"]) == (3.0, 12.0, 1.0), a2
    assert nh.sample_footprint(d2, v2, 0.0, 30.0, 10.0, ring2, 0.1)[:2] == (3.0, 12.0)
    line = [(105.0, 695.0), (390.0, 430.0), (480.0, 600.0)]
    a = s.lines([np.array(line)])
    assert abs(a["depth_ft"] - nh.sample_line_max(d, 100.0, 700.0, 10.0, line)) < 1e-9
    assert nh.sample_point(d, v, 100.0, 700.0, 10.0, 155.0, 645.0) == (None if np.isnan(
        d[5, 5]) else float(d[5, 5]), None)
    assert nh.sample_point(d, v, 100.0, 700.0, 10.0, 50.0, 645.0) == (None, None)
