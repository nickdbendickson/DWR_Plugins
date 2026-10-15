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
