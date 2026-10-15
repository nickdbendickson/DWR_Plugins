"""DWR-3161: the Runner's ND 5.2.1.2 road rule must use the same AADT data as the Hazard plugin.

The Runner read AADT only from the roads layer's own field; the NG911 roads carry none, so every
overtopped road was 'AADT unknown (assumed Medium)' and the Runner said MEDIUM while Hazard
(which joins HPMS / NDDOT counts / county roads) said HIGH (39TH ST NW 10,113; HIGHWAY 23 8,501).
core/aadt_join.py joins those layers; without AADT the class is labelled preliminary and the
Hazard plugin's class (when its results exist) is the one to use."""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import aadt_join as AJ  # noqa: E402
from nd_breach_runner.core import nd_hazard as nh  # noqa: E402
from nd_breach_runner.damsuite import suitegpkg as G  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


def _hpms(folder, lines=(), points=(), name="HPMS_ND_2024.gpkg"):
    """A small HPMS-like (lines, AADT) and/or NDDOT-like (points, aadt + aadt_year) file."""
    p = os.path.join(folder, name)
    w = G.GpkgWriter(p)
    if lines:
        lyr = w.layer("hpms", "LINESTRING", [("RouteName", "text"), ("AADT", "real")], 5070)
        for pts, aadt in lines:
            lyr.add({"type": "LineString", "coordinates": [list(q) for q in pts]},
                    {"RouteName": "", "AADT": aadt})
    if points:
        lyr = w.layer("counts", "POINT", [("station_id", "text"), ("aadt", "real"),
                                          ("aadt_year", "int")], 5070)
        for (x, y), aadt, yr in points:
            lyr.add({"type": "Point", "coordinates": [x, y]},
                    {"station_id": "S1", "aadt": aadt, "aadt_year": yr})
    w.close()
    return p


@test
def aadt_dwr3161_joined_aadt_over_2000_gives_high():
    d = tempfile.mkdtemp()
    try:
        # the overtopped road (NG911: no AADT field) and HPMS lines along / across it
        road = [[(0.0, 0.0), (400.0, 0.0)]]
        p = _hpms(d, lines=[([(-50, 8), (500, 8)], 10113.0),          # same road, 8 m off
                            ([(200, -300), (200, 300)], 20000.0)])    # crossing road: not it
        tr = AJ.read_layers([p])
        assert len(tr) == 2 and all(t["kind"] == "line" for t in tr)
        r = nh.Road("39TH ST NW", 0.8)
        log = AJ.apply([r], {id(r): road}, tr)
        assert r.aadt == 10113.0 and log[0]["source"].startswith("HPMS_ND_2024.gpkg"), log
        c = nh.classify([], [r], [], [], [])
        assert c.hazard_class == "High", c.hazard_class
        assert AJ.class_status(c.hazard_class, c.data_gaps) == AJ.STATUS_COMPLETE
        # the same road without the join: the old Runner result (Medium, by assumption)
        c0 = nh.classify([], [nh.Road("39TH ST NW", 0.8)], [], [], [])
        assert c0.hazard_class == "Medium"
        assert AJ.class_status(c0.hazard_class, c0.data_gaps) == AJ.STATUS_PRELIMINARY
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def aadt_join_stations_distance_and_line_preference():
    d = tempfile.mkdtemp()
    try:
        road = [[(0.0, 0.0), (400.0, 0.0)]]
        p = _hpms(d, points=[((100, 5), 9785.0, 2023), ((100, 200), 50000.0, 2024)],
                  name="NDDOT_traffic_counts.gpkg")
        m = AJ.match(road, AJ.read_layers([p]))
        assert m["aadt"] == 9785.0 and m["year"] == 2023, m       # the far station is ignored
        # a traffic line on the road wins over a count station (as Hazard: 10,113 not 9,785)
        q = _hpms(d, lines=[([(0, 3), (400, 3)], 10113.0)])
        m = AJ.match(road, AJ.read_layers([p, q]))
        assert m["aadt"] == 10113.0, m
        # nothing within the distance: no match, class stays preliminary
        assert AJ.match(road, AJ.read_layers([p]), max_m=2.0) is None
        assert sorted(os.path.basename(x) for x in AJ.siblings(os.path.join(d, "roads.gpkg"))) \
            == ["HPMS_ND_2024.gpkg", "NDDOT_traffic_counts.gpkg"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def aadt_hazard_module_class_and_status():
    d = tempfile.mkdtemp()
    try:
        assert AJ.hazard_module_class(d) is None
        os.makedirs(os.path.join(d, "outputs"))
        with open(os.path.join(d, AJ.HAZARD_SUMMARY), "w", encoding="utf-8") as f:
            f.write("Item,Value\nND classification (screening),High\nND basis,\"x, y\"\n")
        assert AJ.hazard_module_class(d) == "High"
        assert AJ.class_status("High", ["road 1: AADT not given"]) == AJ.STATUS_COMPLETE
        assert AJ.class_status("Low", []) == AJ.STATUS_COMPLETE
        md = AJ.markdown_lines({"hazard_class_status": AJ.STATUS_PRELIMINARY,
                                "hazard_module_class": "High"})
        assert "preliminary (no AADT road check)" in md and "Use the Hazard plugin" in md
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def aadt_ndhazard_tool_dwr3161_end_to_end():
    """The Processing tool: a road layer without AADT plus AADT_LAYERS gives High; without them
    the class is Medium, labelled preliminary, and defers to Hazard's summary when present."""
    from nd_breach_runner.tests.test_ndhazard_tool import _Harness, _load_tool, _run
    an, fq, _ = _load_tool()
    h = _Harness()
    try:
        i1d = nh.LETHALITY_METHODS.index(nh.LETHALITY_1D)
        roads = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [[(-50, 55), (200, 55)]]),
                                              St_Name="39TH ST NW", RTTYP="M")],
                              source_name="ND_roads_NG911")
        p = _hpms(h.root, lines=[([(-60, 60), (210, 60)], 10113.0)])
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "ROADS": roads, "LETHALITY": i1d,
                                     "AADT_LAYERS": p})
        js = json.load(open(res["JSON"]))
        assert res["CLASS"] == "High", (res["CLASS"], fb.errors)
        assert js["hazard_class_status"] == AJ.STATUS_COMPLETE
        assert js["aadt_joined"][0]["aadt"] == 10113.0
        assert any("AADT joined" in m for m in fb.info)
        assert "Road / AADT check" in open(res["REPORT"], encoding="utf-8").read()
        # no AADT data: the old Medium, but labelled preliminary everywhere
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "ROADS": roads, "LETHALITY": i1d})
        js = json.load(open(res["JSON"]))
        assert res["CLASS"] == "Medium"
        assert js["hazard_class_status"] == "preliminary (no AADT road check)"
        assert js["class_to_use"] == "Medium" and js["hazard_module_class"] is None
        assert any("preliminary (no AADT road check)" in e for e in fb.errors)
        assert "preliminary (no AADT road check)" in open(res["REPORT"], encoding="utf-8").read()
        # Hazard's results exist: defer to them
        os.makedirs(os.path.join(h.folder, "outputs"), exist_ok=True)
        with open(os.path.join(h.folder, AJ.HAZARD_SUMMARY), "w", encoding="utf-8") as f:
            f.write("Item,Value\nND classification (screening),High\n")
        res, ctx, fb = _run(an, fq, {"DAM_FOLDER": h.folder, "ROADS": roads, "LETHALITY": i1d})
        js = json.load(open(res["JSON"]))
        assert js["hazard_module_class"] == "High" and js["class_to_use"] == "High"
        assert any("Use the Hazard plugin's class" in e for e in fb.errors)
    finally:
        h.close()


# ---------------------------------------------------------------------------------------
# Edge cases found on the real runs (Grok Bot, PR #40): the real NG911 road geometries and the
# traffic features within 80 m, extracted read-only from ND_Dam_Suite_Pack/roads (EPSG:5070)
def _real(oid):
    with open(os.path.join(HERE, "data", "aadt_join_real_roads.json"), encoding="utf-8") as f:
        r = json.load(f)["roads"][str(oid)]
    traffic = [dict(t, parts=[[tuple(p) for p in ln] for ln in t["parts"]]) if t["kind"] == "line"
               else dict(t, parts=[tuple(t["parts"][0])]) for t in r["traffic"]]
    return [[tuple(p) for p in ln] for ln in r["parts"]], traffic


@test
def aadt_edge_a_hpms_line_beats_a_newer_station_dwr3161_88347():
    """(a) ND-23 / 39TH ST NW (1.7 km): HPMS is cut into ~200 m pieces, so no single piece
    covered 30 % of the road and only the 2026 station (6,671) was left. Pieces with the same
    value are now pooled, and a line wins over a station."""
    parts, tr = _real(88347)
    assert any(t["kind"] == "point" and t["aadt"] == 6671.0 for t in tr)
    pieces = [t for t in tr if t["kind"] == "line" and t["aadt"] == 10113.0]
    assert len(pieces) > 5
    m = AJ.match(parts, tr)
    assert m["aadt"] == 10113.0 and m["source"].startswith("HPMS"), m


@test
def aadt_edge_b_long_segment_partial_overlap_dwr3161_105710():
    """(b) 105710 (1.65 km, overtopped 3.3 ft) under HPMS: was unjoined (pieces < 30 % each).
    Pooled pieces join it; only the overtopped part counts when the depths are known."""
    parts, tr = _real(105710)
    assert AJ.match(parts, tr)["aadt"] == 10113.0
    # only a 200 m stretch overtopped, under HPMS: still joined
    xs = [p[0] for ln in parts for p in ln]
    x0 = min(xs)
    m = AJ.match(parts, tr, is_wet=lambda x, y: x0 <= x <= x0 + 200.0)
    assert m and m["aadt"] == 10113.0, m
    # a long road with only 160 m (< 30 %) under a traffic line: accepted by length
    road = [[(0.0, 0.0), (2000.0, 0.0)]]
    line = [{"kind": "line", "parts": [[(0.0, 2.0), (160.0, 2.0)]], "aadt": 3000.0,
             "year": 2024, "name": "", "source": "HPMS"}]
    assert AJ.match(road, line)["aadt"] == 3000.0
    assert AJ.match(road, [dict(line[0], parts=[[(0.0, 2.0), (60.0, 2.0)]])]) is None
    # an important road left unjoined keeps the class preliminary, also when High rests on it
    gap = "road 105710: AADT not given; classified High by assumption"
    assumed = nh.Finding("High", "5.2.1.2", "road", "105710", "AADT unknown", gap=gap)
    firm = nh.Finding("High", "5.2.1.2", "road", "70263", "AADT 10,113")
    assert AJ.class_status("High", [gap], [assumed]) == AJ.STATUS_PRELIMINARY
    assert AJ.class_status("High", [gap], [assumed, firm]) == AJ.STATUS_COMPLETE
    assert AJ.class_status("Medium", [gap], []) == AJ.STATUS_PRELIMINARY


@test
def aadt_edge_c_crossing_road_rejected_and_overtopped_part_absaraka_149313():
    """(c) 151ST AVE SE (1.6 km, N-S): ND_county_roads cuts 151ST into a northern piece
    (AADT 66, the same value as the crossing 29TH ST) and a southern piece (AADT 10). The
    crossing 29TH line is rejected by its bearing; the overtopped part decides the piece:
    the Runner's flood touches 151ST only at y 2,666,761-2,666,791 (0.25 ft), on the 66
    piece, which is also what the Hazard plugin's Roads_Inundated gives that stretch."""
    parts, tr = _real(149313)
    cross = [t for t in tr if t["kind"] == "line" and
             abs(t["parts"][0][-1][0] - t["parts"][0][0][0]) > 5000]           # 29TH ST, E-W
    assert cross and AJ.match(parts, cross) is None
    assert AJ.match(parts, tr, is_wet=lambda x, y: 2666750 <= y <= 2666800)["aadt"] == 66.0
    assert AJ.match(parts, tr, is_wet=lambda x, y: y <= 2666300)["aadt"] == 10.0


@test
def aadt_edge_d_short_curved_connector_absaraka_158229():
    """(d) 153RD AVE SE 158229 is a 45 m curved jog between two county lines (AADT 10): it
    touches them (0.7 m at its end) but follows neither, so the bearing test rejected every
    sample. A short road that no line follows now takes the lines it touches."""
    parts, tr = _real(158229)
    m = AJ.match(parts, tr)
    assert m and m["aadt"] == 10.0, m
    # a long road is never joined this way (a crossing road only touches it)
    road = [[(0.0, 0.0), (0.0, 500.0)]]
    cross = [{"kind": "line", "parts": [[(-300.0, 250.0), (300.0, 250.0)]], "aadt": 66.0,
              "year": None, "name": "", "source": "county"}]
    assert AJ.match(road, cross) is None
