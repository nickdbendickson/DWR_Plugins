"""0.58.0: roads without a traffic count take the count of the traffic line they run along (HPMS, ND county
roads) or the NDDOT count station on them, in tools 6 and 8 - the Runner's core/aadt_join.py, shared as a
byte-identical copy (checked here and by tools/ci_checks.py IDENTICAL_ACROSS_PLUGINS).

The offline pack's ND_roads_NG911 roads carry no AADT and the county roads (with AADT) come in as an extra
roads layer, so each road was tested twice and the NG911 copy was rated 'AADT unknown' (Medium): an
overtopped local road with a count of 83 kept the dam Medium (DWR-2827) and US 85 with 5,784 a day was
Medium, not High (DWR-3271). Checked here:
  * the copy is the Runner's file byte for byte;
  * core/road_aadt.py: sources (siblings, per-dam pack clips, ROADS_MORE), the area read (tiles, R-tree,
    a file that cannot be read), a county twin, an HPMS highway, a count station only, a short stub,
    nothing to match, the text;
  * the window's wiring: Core's suite_fill finds HPMS / NDDOT count stations, steps 6, 8 and 9 get them
    as AADT_LAYERS (nothing when the pack has none); hz_aadt_review reaches the Review tab and the report;
  * tool 8 (mock QGIS): NG911 + county twin 83 -> Low with hz_aadt_review / hz_conf review and the
    impacted feature's aadt_src; NG911 US highway + HPMS 5,784 -> High (no review flag); a station only;
    unmatched -> Medium as before; AADT_JOIN = False and no traffic files -> exactly the old result;
    a triage clear carries tool 6's tri_aadt;
  * tool 6 (mock QGIS): the envelope's road test (NG911 + county twin 83 no longer blocks the clear, and
    the clear is flagged in tri_aadt) and the corridor's High-capable road test (a local road matched to
    24,206 is a hit; a US highway matched to 5,784 is a hit with its count; one matched to 1,200 is not,
    and the clear is flagged).

Run: python3 DamSafety_Screening/tests/test_road_aadt_join.py (or with pytest)."""
import os
import re
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
ROOT = os.path.dirname(PLUGIN)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
from qgis.core import QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, make_layer  # noqa: E402
from osgeo import gdal  # noqa: E402
import _gpkg  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402
from nd_dam_screening.algorithms.triage import Triage  # noqa: E402
from nd_dam_screening.core import aadt_join as AJ, road_aadt as RA  # noqa: E402
from nd_dam_screening.damsuite import suitegpkg as G  # noqa: E402

S_, D_ = "string", "double"


def _lines_file(path, layer, rows, aadt_field="AADT", year_field="AADT_YR", rtree=True, srs=5070):
    """A traffic line layer: rows = [(points, aadt, year, name)]."""
    w = G.GpkgWriter(path)
    lyr = w.layer(layer, "LINESTRING", [("RouteName", "text"), (aadt_field, "real"), (year_field, "int")], srs,
                  spatial_index=rtree)
    for pts, aadt, yr, nm in rows:
        lyr.add({"type": "LineString", "coordinates": [list(p) for p in pts]},
                {"RouteName": nm, aadt_field: aadt, year_field: yr})
    w.close()
    return path


def _stations_file(path, rows):
    w = G.GpkgWriter(path)
    lyr = w.layer("NDDOT_traffic_counts", "POINT", [("name", "text"), ("aadt", "real"), ("aadt_year", "int")],
                  5070, spatial_index=True)
    for (x, y), aadt, yr in rows:
        lyr.add({"type": "Point", "coordinates": [x, y]}, {"name": "ND 23 W", "aadt": aadt, "aadt_year": yr})
    w.close()
    return path


# ------------------------------------------------------------------------------------------------ copy
def test_copy_is_the_runners_file():
    runner = os.path.join(ROOT, "DamSafety_Runner", "core", "aadt_join.py")
    mine = os.path.join(PLUGIN, "core", "aadt_join.py")
    if not os.path.isfile(runner):
        print("SKIP: the Runner is not beside Screening here (copy check)")
        return
    with open(mine, "rb") as a, open(runner, "rb") as b:
        assert a.read() == b.read(), "core/aadt_join.py differs from DamSafety_Runner/core/aadt_join.py"
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import ci_checks
    assert ci_checks.IDENTICAL_ACROSS_PLUGINS["core/aadt_join.py"] == ("DamSafety_Runner", "DamSafety_Screening")
    print("OK core/aadt_join.py is the Runner's, byte for byte")


# ------------------------------------------------------------------------------------- core/road_aadt
def test_join_core():
    d = tempfile.mkdtemp(prefix="raj_")
    try:
        roads_dir = os.path.join(d, "roads")
        os.makedirs(roads_dir)
        ng = os.path.join(roads_dir, "ND_roads_NG911.gpkg")
        _gpkg.write(ng, {"ND_roads_NG911": ("LINESTRING", [("FULL_NAME", "TEXT")], [])})
        # county roads: the local road's twin 4 m off (83), a crossing road (9,000: never taken), a line far away
        cty = _lines_file(os.path.join(roads_dir, "ND_county_roads.gpkg"), "ND_county_roads", [
            ([(0, 4), (400, 4)], 83.0, 2023, ""),
            ([(200, -300), (200, 300)], 9000.0, 2023, ""),
            ([(50000, 0), (50400, 0)], 60.0, 2023, "")])
        hp = _lines_file(os.path.join(roads_dir, "HPMS_ND_2024.gpkg"), "HPMS_ND_2024", [
            ([(0, 1003), (400, 1003)], 5784.0, 2024, "US 85")], year_field="DATA_YEAR")
        _stations_file(os.path.join(roads_dir, "NDDOT_traffic_counts.gpkg"), [((100, 1505), 4108.0, 2023)])
        # sources: the files next to the roads layer (its own file left out), each once, ROADS_MORE added
        src = RA.sources("", ng + "|layername=ND_roads_NG911", cty + "|layername=ND_county_roads")
        assert [os.path.basename(p) for p in src] == ["HPMS_ND_2024.gpkg", "NDDOT_traffic_counts.gpkg",
                                                      "ND_county_roads.gpkg"], src
        assert RA.sources(hp, ng, "") == [hp]                     # AADT_LAYERS replaces the siblings
        assert RA.sources("", os.path.join(d, "elsewhere.gpkg"), "") == []
        # tool 2 / Hazard per-dam clips: pack_<name>_<dam>.gpkg beside pack_ND_roads_NG911_<dam>.gpkg
        clip = os.path.join(d, "clip")
        os.makedirs(clip)
        for n in ("pack_ND_roads_NG911_6141.gpkg", "pack_HPMS_ND_2024_6141.gpkg", "pack_ND_county_roads_6141.gpkg",
                  "pack_NDDOT_traffic_counts_6141.gpkg", "pack_NSI_structures_6141.gpkg"):
            shutil.copy(ng, os.path.join(clip, n))
        assert len(RA.sources("", os.path.join(clip, "pack_ND_roads_NG911_6141.gpkg"), "")) == 3

        j = RA.Join(src)
        # 1. a NG911 local road: its county twin's 83 (the crossing county road's 9,000 is never taken)
        m = j.match([[(0.0, 0.0), (400.0, 0.0)]])
        assert m and m["aadt"] == 83.0 and m["year"] == 2023, m
        assert m["text"] == "AADT 83 (2023) from ND county roads (ND_county_roads.gpkg)", m["text"]
        # 2. a NG911 US highway: HPMS 5,784
        m = j.match([[(0.0, 1000.0), (400.0, 1000.0)]])
        assert m["aadt"] == 5784.0 and m["text"].startswith("AADT 5,784 (2024) from HPMS (HPMS_ND_2024.gpkg, US 85)")
        # 3. a station only: 4,108 within 30 m; at 40 m nothing
        m = j.match([[(0.0, 1500.0), (400.0, 1500.0)]])
        assert m["aadt"] == 4108.0 and "an NDDOT count station" in m["text"], m
        assert j.match([[(0.0, 1545.0), (400.0, 1545.0)]]) is None
        # 4. nothing within 30 m: None (the road keeps 'AADT unknown')
        assert j.match([[(0.0, 500.0), (400.0, 500.0)]]) is None
        # 5. a short stub (60 m, an intersection jog) no line runs along takes the line it touches;
        #    the same stub 100 m away takes nothing
        m = j.match([[(200.0, 4.0), (200.0, -56.0)]])
        assert m and m["aadt"] in (83.0, 9000.0), m                  # it runs along the crossing road too
        m = j.match([[(120.0, 4.0), (120.0, 64.0)]])
        assert m and m["aadt"] == 83.0 and m["share"] == 0.0, m      # touches the twin only
        assert j.match([[(120.0, 104.0), (120.0, 164.0)]]) is None
        # 6. the overtopped part decides: wet only where the twin is (x < 200); dry part ignored
        m = j.match([[(0.0, 0.0), (400.0, 0.0)]], is_wet=lambda x, y: x < 200)
        assert m["aadt"] == 83.0
        # read by area: one tile read served those roads (one R-tree query per file); a far road reads again
        assert j.reads == 1, j.reads
        assert j.match([[(50000.0, 3.0), (50400.0, 3.0)]])["aadt"] == 60.0 and j.reads == 2
        assert j.tried == 10 and j.matched == 7, (j.tried, j.matched)
        assert j.summary().startswith("7 of 10 road(s) without AADT matched in HPMS_ND_2024.gpkg")
        # without an R-tree (a plain file) the same answers
        _gpkg.write(os.path.join(d, "plain.gpkg"), {"cty": ("LINESTRING", [("AADT", "REAL")], [
            (("LineString", [(0, 4), (400, 4)]), {"AADT": 83.0})])})
        assert RA.Join([os.path.join(d, "plain.gpkg")]).match([[(0.0, 0.0), (400.0, 0.0)]])["aadt"] == 83.0
        # a file that cannot be read is left out and said once, the others still work
        bad = os.path.join(d, "HPMS_ND_2023.gpkg")
        with open(bad, "w") as fh:
            fh.write("not a GeoPackage")
        jb = RA.Join([bad, cty])
        assert jb.match([[(0.0, 0.0), (400.0, 0.0)]])["aadt"] == 83.0
        assert len(jb.errors) == 1 and jb.errors[0].startswith("HPMS_ND_2023.gpkg"), jb.errors
        # the transform hooks: a file in another CRS goes through to_work / to_layer_box
        calls = []
        jt = RA.Join([cty], to_work=lambda srs: (calls.append(srs), (lambda x, y: (x + 1000.0, y)))[1],
                     to_layer_box=lambda srs, b: (b[0] - 1000.0, b[1], b[2] - 1000.0, b[3]))
        assert jt.match([[(1000.0, 0.0), (1400.0, 0.0)]])["aadt"] == 83.0 and calls == [5070]
        # the review text
        t = RA.review_text("Class", "Low", "it would be Medium", ["road 7: AADT 83", "road 9: AADT 26", "a", "b", "c"])
        assert t.startswith("Class Low rests on traffic counts matched from other layers for 5 road(s) (road 7: "
                            "AADT 83; road 9: AADT 26; a; b; +1 more); without them it would be Medium: review"), t
        assert RA.source_label("pack_ND_county_roads_6141.gpkg (AADT)") == "ND county roads"
        assert RA.source_label("my_counts.gpkg (aadt)") == "my_counts"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("OK core/road_aadt.py: sources, area reads, twin / HPMS / station / stub / no match, text")


# ------------------------------------------------------------------------------------- wiring (window)
def test_wiring():
    """Core's suite_fill finds the pack's traffic layers (damsuite 1.18.11) and the window hands them to steps
    6, 8 and 9 as AADT_LAYERS; with no traffic file nothing is put (the tools then look next to the roads)."""
    from nd_dam_screening.core import workflow as wf
    from nd_dam_screening.damsuite import suite_fill as SF
    d = tempfile.mkdtemp(prefix="rajw_")
    try:
        suite = os.path.join(d, "ND_Dam_Suite_Pack")
        roads = os.path.join(suite, "roads")
        os.makedirs(roads)
        line = lambda f: ("LINESTRING", f, [])
        _gpkg.write(os.path.join(roads, "ND_roads_NG911.gpkg"), {"ND_roads_NG911": line([("FULL_NAME", "TEXT")])})
        lone = suite + "_lone"
        shutil.copytree(suite, lone)
        _gpkg.write(os.path.join(roads, "ND_county_roads.gpkg"), {"ND_county_roads": line([("AADT", "REAL")])})
        _gpkg.write(os.path.join(roads, "HPMS_ND_2024.gpkg"), {"HPMS_ND_2024": line([("AADT", "REAL")])})
        _gpkg.write(os.path.join(roads, "NDDOT_traffic_counts.gpkg"),
                    {"NDDOT_traffic_counts": ("POINT", [("aadt", "REAL")], [])})
        found = SF.scan(suite)[0]
        assert found["hpms"].endswith("HPMS_ND_2024.gpkg|layername=HPMS_ND_2024"), found
        assert found["traffic_counts"].endswith("NDDOT_traffic_counts.gpkg|layername=NDDOT_traffic_counts"), found
        assert found["roads"].endswith("ND_roads_NG911.gpkg|layername=ND_roads_NG911"), found
        want = ";;".join(found[k] for k in ("hpms", "traffic_counts", "county_roads"))
        assert SF.more_uris(found, "aadt") == want
        assert SF.more_uris(found, "roads") == found["county_roads"]          # the county roads stay a road layer
        work = os.path.join(d, "work")
        os.makedirs(work)
        for step in ("6", "8", "9"):
            assert wf.defaults(step, work, {"suite_folder": suite}).get("AADT_LAYERS") == want, step
            assert "AADT_LAYERS" not in wf.defaults(step, work, {"suite_folder": lone}), step
        assert wf.RESULTS_MOVED["8"][:2] == ("0.58.0", "hz_aadt")
        assert wf.RESULTS_MOVED["6"][:2] == ("0.58.0", "tri_aadt")          # a 0.52 - 0.57 triage is out of date
        # the review flag reaches the Review tab's queue and the report's review reasons
        from nd_dam_screening.core import report as rp, review as rv
        open(wf.path(work, "8", "OUT"), "wb").close()
        rows = {"dam_id": ["DWR-1", "DWR-2"], "name": ["A", "B"], "hz_class": ["Low", "Medium"],
                "hz_aadt_review": ["Class Low rests on traffic counts ...: review", None],
                "hz_border": ["no", "no"], "hz_conf": ["review", "medium"]}
        read = lambda ref, fields: list(zip(*[rows[k] for k in fields])) if ref == wf.path(work, "8", "OUT") else []
        q = {i["dam_id"]: i for i in rv.queue(work, read, decided={})}
        assert "DWR-2" not in q and "class Low rests on matched traffic counts" in q["DWR-1"]["why"], q
        assert "class rests on traffic counts matched from other layers" in rp.review_reasons(
            {"hz_aadt_review": "Class Low rests on ...", "hz_conf": "review"})
        assert "class rests on traffic counts matched from other layers" not in rp.review_reasons({"hz_conf": "medium"})
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("OK wiring: suite_fill hpms / traffic_counts slots, AADT_LAYERS for steps 6, 8 and 9")


# ---------------------------------------------------------------------------------------- tool 8 (mock)
CELL = 10.0
GT = (500000.0, CELL, 0.0, 1700400.0, 0.0, -CELL)          # 40 x 40 cells
XY = lambda r, c: (GT[0] + (c + 0.5) * CELL, GT[3] - (r + 0.5) * CELL)
LINE = lambda pts: QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in pts])


def _ng911(path, rows):
    """A NG911-like roads file (no AADT): rows = [(points, FULL_NAME, SHIELD)]."""
    return _gpkg.write(path, {"ND_roads_NG911": ("LINESTRING", [("FULL_NAME", "TEXT"), ("SHIELD", "TEXT")],
                                                 [(("LineString", pts), {"FULL_NAME": nm, "SHIELD": sh})
                                                  for pts, nm, sh in rows])}) + "|layername=ND_roads_NG911"


def _tool8(work, roads, roads_more="", sim_rows=None, **kw):
    depth = np.zeros((40, 40))
    depth[18:23, :] = 0.6                                    # a 50 m wet band across the grid, rows 18-22
    dtif, vtif = os.path.join(work, "d.tif"), os.path.join(work, "v.tif")
    cm.write_tif(dtif, depth, GT)
    cm.write_tif(vtif, np.full((40, 40), 0.5), GT)
    pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*XY(r, c)))
    simf = [("dam_id", S_), ("sim_status", S_), ("depth_tif", S_), ("vel_tif", S_), ("tri_reason", S_),
            ("tri_aadt", S_)]
    sim = make_layer(simf, sim_rows or [(pt(0, 0), {"dam_id": "DWR-1", "sim_status": "ok", "depth_tif": dtif,
                                                    "vel_tif": vtif})], "point", "sim")
    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    p = {"SIM": ctx.add(sim), "ROADS": roads, "ROADS_MORE": roads_more, "CRIT": 2}
    p.update(kw)
    res = HazardClassification().run_mock(p, ctx, fb)
    out, imp = ctx.layers[res["OUT"]], ctx.layers[res["IMP"]]
    nms = out.fields().names()
    rows = {f["dam_id"]: {k: f[k] for k in nms} for f in out.getFeatures()}
    return rows, [(f["kind"], f["level"], f["aadt_src"]) for f in imp.getFeatures()]


def test_tool8():
    work = tempfile.mkdtemp(prefix="raj8_")
    try:
        roads_dir = os.path.join(work, "roads")
        os.makedirs(roads_dir)
        y = XY(20, 0)[1]
        x0, x1 = XY(0, 0)[0], XY(0, 39)[0]
        local = [(XY(0, 20)[0], XY(0, 0)[1]), (XY(0, 20)[0], XY(39, 0)[1])]   # N-S across the band, 390 m
        ng = _ng911(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "87TH ST NW", "")])
        cty = _lines_file(os.path.join(roads_dir, "ND_county_roads.gpkg"), "ND_county_roads",
                          [([(p[0] + 5.0, p[1]) for p in local], 83.0, 2023, "")])
        more = cty + "|layername=ND_county_roads"
        # 1. NG911 local road + county twin 83 (the pack's wiring: NG911 = ROADS, county = ROADS_MORE)
        rows, imp = _tool8(work, ng, more)
        d = rows["DWR-1"]
        assert d["hz_class"] == "Low" and d["n_road_wet"] == 2, d
        assert d["hz_conf"] == "review" and d["hz_border"] == "no" and not d["hz_gaps"], d
        assert d["hz_aadt_review"].startswith("Class Low rests on traffic counts matched from other layers for 1 "
                                              "road(s) (87TH ST NW (road 2): AADT 83 (2023) from ND county roads "
                                              "(ND_county_roads.gpkg) (Low instead of Medium)); without them it "
                                              "would be Medium: review"), d["hz_aadt_review"]
        assert d["hz_aadt"].startswith("1 of 1 overtopped road(s) without AADT matched; 87TH ST NW (road 2): AADT 83"), \
            d["hz_aadt"]
        assert "[count from ND county roads]" in d["hz_reason"], d["hz_reason"]
        assert sorted(imp) == [("road", "Low", "AADT 83 (2023) from ND county roads (ND_county_roads.gpkg)"),
                               ("road", "Low", "own AADT field")], imp
        # today's result: the join off, or no traffic files anywhere -> Medium by assumption, nothing flagged
        for kw, roads in (({"AADT_JOIN": False}, ng), ({}, None)):
            if roads is None:
                lone = os.path.join(work, "lone")
                os.makedirs(lone, exist_ok=True)
                roads = _ng911(os.path.join(lone, "ND_roads_NG911.gpkg"), [(local, "87TH ST NW", "")])
            rows, imp = _tool8(work, roads, more if kw else "", **kw)
            d = rows["DWR-1"]
            assert d["hz_class"] == "Medium" and d["hz_conf"] == "medium", d
            assert d["hz_aadt_review"] is None and "AADT not given; classified Medium by assumption" in d["hz_gaps"]
            assert ("road", "Medium", "no AADT; no traffic layer") in imp, imp
            assert d["hz_aadt"] is None
        # 2. NG911 US highway (SHIELD USHY) + HPMS 5,784 -> High; raised, so no review flag
        hw = [(x0, y), (x1, y)]                                  # along the band
        ng2 = _ng911(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(hw, "MAIN ST", "USHY")])
        _lines_file(os.path.join(roads_dir, "HPMS_ND_2024.gpkg"), "HPMS_ND_2024",
                    [([(p[0], p[1] + 6.0) for p in hw], 5784.0, 2024, "US 85")], year_field="DATA_YEAR")
        rows, imp = _tool8(work, ng2)
        d = rows["DWR-1"]
        assert d["hz_class"] == "High" and d["hz_aadt_review"] is None and d["hz_conf"] == "medium", d
        assert "road with AADT 5,784 (> 2,000) overtopped" in d["hz_reason"] and "[count from HPMS]" in d["hz_reason"]
        assert imp == [("road", "High", "AADT 5,784 (2024) from HPMS (HPMS_ND_2024.gpkg, US 85)")], imp
        rows, _imp = _tool8(work, ng2, AADT_JOIN=False)
        assert rows["DWR-1"]["hz_class"] == "Medium"             # today: US / State highway, AADT unknown
        # 3. a count station only (HPMS gone): 4,108 on the highway -> High
        os.remove(os.path.join(roads_dir, "HPMS_ND_2024.gpkg"))
        _stations_file(os.path.join(roads_dir, "NDDOT_traffic_counts.gpkg"), [((XY(0, 10)[0], y + 12.0), 4108.0, 2023)])
        rows, imp = _tool8(work, ng2)
        assert rows["DWR-1"]["hz_class"] == "High", rows["DWR-1"]
        assert imp[0][2].startswith("AADT 4,108 (2023) from an NDDOT count station"), imp
        # 4. unmatched (the station 200 m off): Medium as today, the gap kept, nothing flagged
        _stations_file(os.path.join(roads_dir, "NDDOT_traffic_counts.gpkg"), [((XY(0, 10)[0], y + 200.0), 4108.0, 2023)])
        rows, imp = _tool8(work, ng2)
        d = rows["DWR-1"]
        assert d["hz_class"] == "Medium" and d["hz_aadt_review"] is None, d
        assert d["hz_aadt"] == "0 of 1 overtopped road(s) without AADT matched", d["hz_aadt"]
        assert imp == [("road", "Medium", "no AADT; no traffic count matched within 30 m")], imp
        # 5. an explicit AADT_LAYERS list replaces the files next to the roads layer
        alt = _lines_file(os.path.join(work, "my_counts.gpkg"), "counts", [([(p[0], p[1] + 3.0) for p in hw], 300.0,
                                                                            2022, "")])
        rows, imp = _tool8(work, ng2, AADT_LAYERS=alt)
        assert rows["DWR-1"]["hz_class"] == "Medium" and imp[0][2].startswith("AADT 300 (2022) from my_counts")
        # 6. a triage clear that rests on matched counts carries tool 6's flag into tool 8
        pt0 = QgsGeometry.fromPointXY(QgsPointXY(*XY(0, 0)))
        rows, _imp = _tool8(work, ng2, sim_rows=[(pt0, {"dam_id": "DWR-9", "sim_status": "triage",
                                                        "tri_reason": "nothing inside", "tri_aadt": "Triage clear rests "
                                                        "on traffic counts ...: review"})])
        d = rows["DWR-9"]
        assert d["hz_class"] == "Low" and d["hz_conf"] == "triage" and d["hz_aadt_review"].startswith("Triage clear")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("OK tool 8: county twin 83 -> Low (flagged), HPMS 5,784 -> High, station -> High, unmatched -> Medium")


# ---------------------------------------------------------------------------------------- tool 6 (mock)
class _World:
    """A valley sloping south (the test_qgis_tools world) with one pond on its east wall, DWR-3, draining
    west: the envelope is a 500 m strip along row 60 (cols 160-210); the stream runs down the valley floor."""
    cell, NY, NX = 10.0, 520, 260
    X0, YT = 400000.0, 1800000.0

    def __init__(self, work):
        self.work = work
        yy, xx = np.mgrid[0:self.NY, 0:self.NX]
        Z = 300.0 - 0.003 * yy * self.cell + 0.0009 * np.abs((xx - self.NX / 2) * self.cell) ** 1.5
        r3, c3 = 60, 215
        Z[r3 - 3:r3 + 4, c3 - 1:c3 + 2] = np.maximum(Z[r3 - 3:r3 + 4, c3 - 1:c3 + 2], Z[r3, c3] + 2.0)
        self.demdir = os.path.join(work, "dem")
        os.makedirs(self.demdir)
        a = Z[0:r3 + 120, c3 - 120:]
        ds = gdal.GetDriverByName("GTiff").Create(os.path.join(self.demdir, "dam_DWR-3.tif"), a.shape[1],
                                                  a.shape[0], 1, 6)
        ds.SetGeoTransform((self.X0 + (c3 - 120) * self.cell, self.cell, 0, self.YT, 0, -self.cell))
        ds.GetRasterBand(1).WriteArray(a)
        ds.FlushCache()
        pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*self.xy(r, c)))
        self.dams = make_layer([("dam_id", S_), ("td_vw_m3", D_), ("td_qp_m3s", D_), ("td_rel_m3", D_),
                                ("haz_existing", S_)],
                               [(pt(r3, c3), dict(dam_id="DWR-3", td_vw_m3=2000.0, td_qp_m3s=3.0, td_rel_m3=2000.0,
                                                  haz_existing="Low"))], "point", "dams")
        self.bpts = make_layer([("dam_id", S_), ("flow_az", D_)], [(pt(r3, c3 - 2), dict(dam_id="DWR-3",
                                                                                       flow_az=270.0))],
                               "point", "bpts")
        self.streams = make_layer([("gnis_name", S_)], [(LINE([self.xy(0, self.NX // 2), self.xy(self.NY - 1,
                                                                                               self.NX // 2)]),
                                                         {"gnis_name": "Test Creek"})], "line", "streams")
        ring = [self.xy(510, 5), self.xy(510, 15), self.xy(518, 15), self.xy(518, 5), self.xy(510, 5)]
        self.wb = make_layer([("gnis_name", S_)], [(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in ring]]),
                                                    {"gnis_name": "far pond"})], "polygon", "waterbodies")
        self.far_bld = _gpkg.write(os.path.join(work, "far_structures.gpkg"), {"far_structures": (
            "POINT", [("OCC_CLS", "TEXT"), ("PRIM_OCC", "TEXT")],
            [(("Point", self.xy(500, 5)), {"OCC_CLS": "Residential", "PRIM_OCC": "Single Family Dwelling"})])})
        self.n = 0

    def xy(self, r, c):
        return self.X0 + (c + 0.5) * self.cell, self.YT - (r + 0.5) * self.cell

    def run(self, **kw):
        self.n += 1
        ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
        p = dict(DAMS=ctx.add(self.dams), BPTS=ctx.add(self.bpts), DEMDIR=self.demdir, AUDIT=0.0,
                 OUTDIR=os.path.join(self.work, "tri{}".format(self.n)))
        if kw.pop("corridor", False):
            p.update(STREAMS=ctx.add(self.streams), WATERBODIES=ctx.add(self.wb),
                     BLD_MORE=self.far_bld + "|layername=far_structures", CORR_D_FT=3280.84)
        else:
            p["CORRIDOR"] = False
        p.update(kw)
        res = Triage().run_mock(p, ctx, fb)
        f = next(iter(ctx.layers[res["OUT"]].getFeatures()))
        return {k: f[k] for k in ("tri_result", "tri_reason", "tri_corr", "tri_corr_hit", "tri_aadt")}


def test_tool6():
    work = tempfile.mkdtemp(prefix="raj6_")
    try:
        w = _World(work)
        r0 = w.run()
        assert r0["tri_result"] == "clear" and r0["tri_aadt"] is None, r0      # nothing in the envelope
        roads_dir = os.path.join(work, "roads")
        os.makedirs(roads_dir)
        # envelope: a NG911 local road crossing the strip (col 185, rows 40-80) + its county twin (83)
        local = [w.xy(40, 185), w.xy(80, 185)]
        ng = _ng911(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "87TH ST NW", "")])
        cty = _lines_file(os.path.join(roads_dir, "ND_county_roads.gpkg"), "ND_county_roads",
                          [([(p[0] + 5.0, p[1]) for p in local], 83.0, 2023, "")]) + "|layername=ND_county_roads"
        r = w.run(ROADS=ng, ROADS_MORE=cty, AADT_JOIN=False)              # today: the NG911 copy is Medium
        assert r["tri_result"] == "needs 2D" and r["tri_reason"].startswith("1 road(s) inside the envelope"), r
        r = w.run(ROADS=ng, ROADS_MORE=cty)
        assert r["tri_result"] == "clear" and "rests on matched traffic counts for 1 road(s)" in r["tri_reason"], r
        assert re.match(r"Triage clear rests on traffic counts matched from other layers for 1 road\(s\) \(87TH ST NW "
                        r"\(road \d+\): AADT 83 \(2023\) from ND county roads \(ND_county_roads\.gpkg\) \(Low instead "
                        r"of Medium\)\); without them it needs 2D: review$", r["tri_aadt"]), r["tri_aadt"]
        # the rating guard and the fail-safes stay: a dam DWR rates Medium is not cleared on a matched count
        w.dams._feats[0].setAttributes(["DWR-3", 2000.0, 3.0, 2000.0, "Medium"])
        r = w.run(ROADS=ng, ROADS_MORE=cty)
        assert r["tri_result"] == "needs 2D" and r["tri_aadt"] is None and "DWR rates it Medium" in r["tri_reason"], r
        w.dams._feats[0].setAttributes(["DWR-3", 2000.0, 3.0, 2000.0, "Low"])
        # a count over 500 still blocks the clear
        _lines_file(os.path.join(roads_dir, "ND_county_roads.gpkg"), "ND_county_roads",
                    [([(p[0] + 5.0, p[1]) for p in local], 900.0, 2023, "")])
        r = w.run(ROADS=ng, ROADS_MORE=cty)
        assert r["tri_result"] == "needs 2D" and r["tri_aadt"] is None, r
        os.remove(os.path.join(roads_dir, "ND_county_roads.gpkg"))

        # corridor: roads 600 m down the valley (row 120), beyond the envelope, within the 1 km corridor
        r = w.run(corridor=True)
        assert r["tri_result"] == "clear" and r["tri_corr"] == "none", r
        cross = [w.xy(120, 0), w.xy(120, w.NX - 1)]
        hpms = os.path.join(roads_dir, "HPMS_ND_2024.gpkg")

        def corr(name, shield, aadt, **kw):
            road = _ng911(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(cross, name, shield)])
            _lines_file(hpms, "HPMS_ND_2024", [([(p[0], p[1] + 6.0) for p in cross], aadt, 2024, "")],
                        year_field="DATA_YEAR")
            return w.run(corridor=True, ROADS=road, **kw)
        # a local road (no US / State name) HPMS counts 24,206: not High-capable unknown, High on its count
        r = corr("E BISMARCK EXPY", "", 24206.0, AADT_JOIN=False)
        assert r["tri_result"] == "clear" and r["tri_corr"] == "none", r
        r = corr("E BISMARCK EXPY", "", 24206.0)
        assert r["tri_result"] == "needs 2D" and r["tri_corr"] == "hit", r
        assert r["tri_corr_hit"].startswith("E BISMARCK EXPY (AADT 24,206 from HPMS)"), r["tri_corr_hit"]
        # a US highway with no AADT is High-capable; matched to 5,784 it still is, with its count
        r = corr("MAIN ST", "USHY", 5784.0, AADT_JOIN=False)
        assert r["tri_corr"] == "hit" and "US/State highway, AADT unknown" in r["tri_corr_hit"], r
        r = corr("MAIN ST", "USHY", 5784.0)
        assert r["tri_corr"] == "hit" and r["tri_corr_hit"].startswith("MAIN ST USHY (AADT 5,784 from HPMS)"), r
        # matched to 1,200 it is Medium, not High-capable: the dam clears and the clear is flagged
        r = corr("MAIN ST", "USHY", 1200.0)
        assert r["tri_result"] == "clear" and r["tri_corr"] == "none", r
        assert re.match(r"Triage clear rests on traffic counts matched from other layers for 1 road\(s\) \(MAIN ST USHY "
                        r"\(road \d+\): AADT 1,200 \(2024\) from HPMS", r["tri_aadt"]), r["tri_aadt"]
        assert "in the corridor (not High-capable; without it: MAIN ST USHY (US/State highway, AADT unknown))" \
            in r["tri_aadt"], r["tri_aadt"]
        # any error in the corridor still sends the dam to 2D (the fail-safe is untouched)
        from nd_dam_screening.core import road_aadt as _ra
        real = _ra.Join.match

        def boom(*a, **k):
            raise RuntimeError("synthetic join failure")
        _ra.Join.match = boom
        try:
            r = corr("E BISMARCK EXPY", "", 24206.0)
        finally:
            _ra.Join.match = real
        assert r["tri_result"] == "needs 2D" and r["tri_corr"] == "failed", r
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("OK tool 6: envelope and corridor road tests use the matched count; clears on it are flagged")


# ------------------------------------------------------------------- the review's fixes (0.58.0 review)
def _named_stations(path, rows):
    """NDDOT count stations: rows = [((x, y), name, aadt, year)]."""
    w = G.GpkgWriter(path)
    lyr = w.layer("NDDOT_traffic_counts", "POINT", [("name", "text"), ("aadt", "real"), ("aadt_year", "int")],
                  5070, spatial_index=True)
    for (x, y), nm, aadt, yr in rows:
        lyr.add({"type": "Point", "coordinates": [x, y]}, {"name": nm, "aadt": aadt, "aadt_year": yr})
    w.close()
    return path


def _ng911x(path, rows):
    """NG911 roads with RoadClass and FUNC_SYS: rows = [(points, FULL_NAME, RoadClass, FUNC_SYS[, SHIELD[,
    SHIELD_LBL]])]."""
    return _gpkg.write(path, {"ND_roads_NG911": ("LINESTRING", [("FULL_NAME", "TEXT"), ("SHIELD", "TEXT"),
                                                                ("SHIELD_LBL", "TEXT"), ("RoadClass", "TEXT"),
                                                                ("FUNC_SYS", "INTEGER")],
                                                 [(("LineString", r[0]), {"FULL_NAME": r[1],
                                                                          "SHIELD": r[4] if len(r) > 4 else "",
                                                                          "SHIELD_LBL": r[5] if len(r) > 5 else "",
                                                                          "RoadClass": r[2], "FUNC_SYS": r[3]})
                                                  for r in rows])}) + "|layername=ND_roads_NG911"


def test_join_rules():
    """What the review found (real pack cases), in core/road_aadt.py; aadt_join.py stays the Runner's file."""
    # names: the same road across the layers' spellings; a route number; never an empty name
    assert RA.same_name("ASH COULEE DR", "ASHCOULEEDR") and RA.same_name("JOHNSON ST S", "JOHNSON ST")
    assert RA.same_name("158th Street Northeast", "158TH ST NE") and RA.same_name("HIGHWAY 85 N USHY", "U85N")
    assert RA.same_name("HIGHWAY 2 AND 52 W", "U52E") and RA.same_name("9TH ST E", "9THSTE")
    assert not RA.same_name("HIGHWAY 2 FRONTAGE RD E", "U2E") and not RA.same_name("HIGHWAY 2 ON RAMP", "U2E")
    assert not RA.same_name("UNIVERSITY DR S", "2ND AVE S") and not RA.same_name("MAIN AVE S", "MAINSTS")
    assert not RA.same_name("87TH ST NW", "") and not RA.same_name("", "") and not RA.same_name("N 7TH ST", "U83S")
    # the interstate system its name does not show, by the Hazard plugin's rule (NG911 FUNC_SYS, HPMS F_SYSTEM 1,
    # HPMS ROUTE_SIGNING 2): rated Interstate; an NG911 SHIELD I / EXIT outside it is only never lowered
    assert RA.interstate_system({"func_sys": 1}) and RA.interstate_system({"f_system": "1"})
    assert RA.interstate_system({"func_sys": 1.0, "shield": ""}) and RA.interstate_system({"route_signing": 2})
    assert RA.interstate_system({"routesigning": "2"}) and not RA.interstate_system({"route_signing": 3})
    assert not RA.interstate_system({"shield": "I"}) and not RA.interstate_system({"shield": "EXIT", "func_sys": 7})
    assert not RA.interstate_system({"func_sys": 7, "shield": "USHY"}) and not RA.interstate_system({})
    assert not RA.interstate_system({"func_sys": None}) and not RA.interstate_system({"func_sys": "x"})
    assert not RA.interstate_system({"func_sys": "NULL", "f_system": ""})
    # as Hazard: F_SYSTEM first, FUNC_SYS only when it is missing; exact values; inf is no value
    assert not RA.interstate_system({"f_system": 3, "func_sys": 1}) and RA.interstate_system({"f_system": None,
                                                                                             "func_sys": 1})
    assert not RA.interstate_system({"func_sys": 1.5}) and not RA.interstate_system({"route_signing": 2.5})
    assert RA.interstate_system({"func_sys": float("inf"), "route_signing": 2}) and not RA.interstate_system(
        {"func_sys": "inf"})
    # an NG9-1-1 SHIELD 'I' with the route number in SHIELD_LBL (Hazard reads 'I 29'): the 21 Grand Forks I-29 ramps
    assert RA.interstate_system({"func_sys": 7, "shield": "I", "shield_lbl": "29"})
    assert RA.interstate_system({"shield": " i ", "shield_lbl": 94})
    assert not RA.interstate_system({"shield": "I", "shield_lbl": ""}) and not RA.interstate_system(
        {"shield": "I", "shield_lbl": "NULL"}) and not RA.interstate_system({"shield": "EXIT", "shield_lbl": "184"})
    assert not RA.interstate_system({"shield": "USHY", "shield_lbl": "2"})
    assert RA.interstate_shield({"shield": "I"}) and RA.interstate_shield({"shield": " exit "})
    assert not RA.interstate_shield({"shield": "USHY"}) and not RA.interstate_shield({"func_sys": 1})
    assert [RA.band(a) for a in (83, 499.9, 500, 2000, 2000.5)] == [0, 0, 1, 1, 2]
    d = tempfile.mkdtemp(prefix="rajr_")
    try:
        road = [[(0.0, 0.0), (400.0, 0.0)]]
        # 1. ASH COULEE DR (pack fid 132420): county 311 (no year) 2.7 m off and the 2025 NDDOT count 2,265 on it.
        #    aadt_join takes the line whatever the years; the higher count on the same road wins (Hazard: High)
        cty = _lines_file(os.path.join(d, "ND_county_roads.gpkg"), "ND_county_roads",
                          [([(-50, 2.7), (450, 2.7)], 311.0, None, "")])
        st = _named_stations(os.path.join(d, "NDDOT_traffic_counts.gpkg"),
                             [((200.0, 1.0), "ASH COULEE DR", 2265.0, 2025)])
        assert AJ.match(road, RA.Join([cty, st]).features((-100, -100, 500, 100)))["aadt"] == 311.0   # the Runner's pick
        m = RA.Join([cty, st]).match(road, name="ASH COULEE DR")
        assert m["aadt"] == 2265.0 and m["over"]["aadt"] == 311.0, m
        assert m["text"] == ("AADT 2,265 (2025) from an NDDOT count station (NDDOT_traffic_counts.gpkg, ASH COULEE DR) "
                             "(not the AADT 311 from ND county roads: the higher count on the same road)"), m["text"]
        j = RA.Join([cty, st])
        assert j.match(road)["aadt"] == 2265.0 and j.raised == 1      # unnamed: the station lies along the road
        assert "1 took a higher count on the same road" in j.summary()
        # a station on the highway beside a frontage road is not the frontage road's (pack: FRONTAGE RD took US 2's
        # 10,515): its nearest traffic line is the highway's, not one the frontage road runs along
        fr = _lines_file(os.path.join(d, "fr.gpkg"), "ND_county_roads", [([(-50, 2.0), (450, 2.0)], 227.0, 2023, ""),
                                                                         ([(-50, 26.0), (450, 26.0)], 10515.0, 2023, "")])
        frs = _named_stations(os.path.join(d, "frs.gpkg"), [((200.0, 25.0), "U2E", 10515.0, 2026)])
        assert RA.Join([fr, frs]).match(road, name="FRONTAGE RD")["aadt"] == 227.0
        assert RA.Join([fr, frs]).match(road, name="HIGHWAY 2 USHY")["aadt"] == 10515.0   # named as the road
        # a station in the same band does not replace the line; a lower one never does
        _named_stations(st, [((200.0, 1.0), "ASH COULEE DR", 450.0, 2025)])
        assert RA.Join([cty, st]).match(road, name="ASH COULEE DR")["aadt"] == 311.0
        # 2. a line NAMED as the road beats a nearer line with a lower band (9TH ST E: county 1,394 vs HPMS 22,966)
        hp = _lines_file(os.path.join(d, "HPMS_ND_2024.gpkg"), "HPMS_ND_2024",
                         [([(-50, 6.0), (450, 6.0)], 22966.0, 2024, "9THSTE")], year_field="DATA_YEAR")
        cty2 = _lines_file(os.path.join(d, "cty2.gpkg"), "ND_county_roads", [([(-50, 1.0), (450, 1.0)], 1394.0, 2023, "")])
        assert RA.Join([hp, cty2]).match(road, name="9TH ST E")["aadt"] == 22966.0
        assert RA.Join([hp, cty2]).match(road, name="")["aadt"] == 1394.0             # no name: the nearest line
        assert RA.Join([hp, cty2]).match(road, name="FRONTAGE RD")["aadt"] == 1394.0  # another road's line
        # 3. a station counts only where the water crosses the road and only on this road
        long_ = [[(0.0, 0.0), (1600.0, 0.0)]]
        wet = lambda x, y: 280.0 <= x <= 320.0
        far = _named_stations(os.path.join(d, "far.gpkg"), [((1590.0, 20.0), "45TH ST", 40.0, 2023)])
        assert RA.Join([far]).match(long_, is_wet=wet) is None                  # by the dry far end: not taken
        assert RA.Join([far]).match(long_)["aadt"] == 40.0                      # no water given: anywhere on it
        xing = _named_stations(os.path.join(d, "xing.gpkg"), [((300.0, 20.0), "45TH ST", 40.0, 2023)])
        xl = _lines_file(os.path.join(d, "xl.gpkg"), "ND_county_roads", [([(302.0, -300.0), (302.0, 300.0)], 40.0,
                                                                          2023, "")])
        assert RA.Join([xing, xl]).match(long_, is_wet=wet, name="12TH AVE") is None   # on the crossing road
        _named_stations(xing, [((300.0, 20.0), "12TH AVE", 40.0, 2023)])
        assert RA.Join([xing, xl]).match(long_, is_wet=wet, name="12TH AVE")["aadt"] == 40.0   # named as this road
        # 4. no wet 15 m sample (a 0.2 m wet sliver): the samples next to the point the tool found overtopped,
        #    not the whole road (DWR-2635 'RAMP': 32 on the whole road, 12,550 where the water crosses it)
        two = _lines_file(os.path.join(d, "two.gpkg"), "ND_county_roads",
                          [([(0, 3.0), (300, 3.0)], 32.0, 2023, ""), ([(300, 3.0), (1600, 3.0)], 12550.0, 2023, "")])
        sliver = lambda x, y: 100.2 <= x <= 100.4
        j = RA.Join([two])
        assert j.match(long_, is_wet=sliver, at=(100.3, 0.0))["aadt"] == 32.0 and j.near_point == 1
        assert RA.Join([two]).match(long_, is_wet=sliver)["aadt"] == 12550.0      # no point given: the whole road
        # 5. the cache is bounded: over max_feats it restarts and the answers stay the same
        jc = RA.Join([cty], max_feats=0)
        assert jc.match(road)["aadt"] == 311.0
        assert jc.match([[(0.0, 9000.0), (400.0, 9000.0)]]) is None and jc.resets == 1
        assert jc.match(road)["aadt"] == 311.0 and jc.reads == 3 and "restarted 1 time(s)" in jc.summary()
        # 6. review texts: a road cut into NG911 segments is listed once and counted once
        items = [("98TH ST SE", "71804", "AADT 28 (Low instead of Medium)"), ("98TH ST SE", "71646", "AADT 28 (Low "
                 "instead of Medium)"), ("", "7", "AADT 5 (Low instead of Medium)"), ("87TH ST NW", "3", "AADT 60")]
        assert RA.grouped(items) == (["98TH ST SE (2 segments): AADT 28 (Low instead of Medium)",
                                      "road 7: AADT 5 (Low instead of Medium)", "87TH ST NW (road 3): AADT 60"], 3, 4)
        t = RA.review_text("Class", "Low", "it would be Medium", items)
        assert t.startswith("Class Low rests on traffic counts matched from other layers for 3 road(s), 4 segments "
                            "(98TH ST SE (2 segments): AADT 28"), t
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("OK join rules: same-road higher count, named line, station on the wet part / this road, sliver, cache")


def test_tool8_review_fixes():
    work = tempfile.mkdtemp(prefix="raj8r_")
    try:
        roads_dir = os.path.join(work, "roads")
        os.makedirs(roads_dir)
        local = [(XY(0, 20)[0], XY(0, 0)[1]), (XY(0, 20)[0], XY(39, 0)[1])]   # N-S across the wet band
        hp = os.path.join(roads_dir, "HPMS_ND_2024.gpkg")
        _lines_file(hp, "HPMS_ND_2024", [([(p[0] + 1.0, p[1]) for p in local], 41.0, 2024, "")], year_field="DATA_YEAR")
        # 1. an I-94 ramp as the pack has it (FULL_NAME 'RAMP', FUNC_SYS 1): Interstate, as the Hazard plugin rates
        #    it, whatever its HPMS ramp count (41) and with or without the join; never matched
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Ramp", 1)])
        isys = ("no AADT; interstate system (F_SYSTEM / FUNC_SYS 1, ROUTE_SIGNING 2 or SHIELD I + route number; "
                "High whatever its count)")
        for kw in ({}, {"AADT_JOIN": False}):
            rows, imp = _tool8(work, ng, **kw)
            d = rows["DWR-1"]
            assert d["hz_class"] == "High" and d["hz_aadt_review"] is None, (kw, d)
            assert imp == [("road", "High", isys)], imp
            assert d["hz_aadt"] in (None, ""), (kw, d["hz_aadt"])
        # ... a Grand Forks I-29 'RAMP' coded FUNC_SYS 7, SHIELD I, SHIELD_LBL 29 likewise (Hazard: 'I 29')
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Local", 7, "I", "29")])
        rows, imp = _tool8(work, ng)
        assert rows["DWR-1"]["hz_class"] == "High" and imp == [("road", "High", isys)], (rows, imp)
        # ... an HPMS F_SYSTEM 1 line given as an extra roads layer likewise
        f1 = os.path.join(work, "hpms_ramp.gpkg")
        w1 = G.GpkgWriter(f1)
        w1.layer("hpms_ramp", "LINESTRING", [("RouteName", "text"), ("F_SYSTEM", "int")], 5070).add(
            {"type": "LineString", "coordinates": [[p[0] + 1.0, p[1]] for p in local]}, {"RouteName": "", "F_SYSTEM": 1})
        w1.close()
        ng0 = _ng911x(os.path.join(work, "none.gpkg"), [([(p[0] + 900.0, p[1]) for p in local], "FAR RD", "Local", 7)])
        assert _tool8(work, ng0, f1 + "|layername=hpms_ramp")[0]["DWR-1"]["hz_class"] == "High"
        # 2. a ramp coded FUNC_SYS 7 with SHIELD I and no route number: not Interstate (Hazard reads no 'I <n>'
        #    either), but a matched count never lowers it
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Local", 7, "I")])
        rows, imp = _tool8(work, ng)
        d = rows["DWR-1"]
        assert d["hz_class"] == "Medium" and d["hz_aadt_review"] is None, d
        assert imp[0][2].startswith("no AADT; interstate shield (SHIELD I / EXIT), so not lowered by the matched "
                                    "AADT 41 (2024) from HPMS"), imp
        assert d["hz_aadt"] == "0 of 1 overtopped road(s) without AADT matched", d["hz_aadt"]
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Ramp", 7)])
        assert _tool8(work, ng)[0]["DWR-1"]["hz_class"] == "Low"           # no FUNC_SYS 1, no shield: lowered, flagged
        # ... a count over 2,000 still raises it (High), no flag
        _lines_file(hp, "HPMS_ND_2024", [([(p[0] + 1.0, p[1]) for p in local], 12550.0, 2024, "")], year_field="DATA_YEAR")
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Local", 7, "I")])
        d = _tool8(work, ng)[0]["DWR-1"]
        assert d["hz_class"] == "High" and d["hz_aadt_review"] is None, d
        os.remove(hp)
        # 2. ASH COULEE DR: the county twin 311 and the 2025 station 2,265 on the road -> High (Hazard: High)
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "ASH COULEE DR", "Local", 4)])
        cty = _lines_file(os.path.join(roads_dir, "ND_county_roads.gpkg"), "ND_county_roads",
                          [([(p[0] + 2.7, p[1]) for p in local], 311.0, None, "")])
        st = _named_stations(os.path.join(roads_dir, "NDDOT_traffic_counts.gpkg"),
                             [((XY(20, 20)[0] + 1.0, XY(20, 20)[1]), "ASH COULEE DR", 2265.0, 2025)])
        rows, imp = _tool8(work, ng, cty + "|layername=ND_county_roads")
        d = rows["DWR-1"]
        assert d["hz_class"] == "High" and d["hz_aadt_review"] is None and d["hz_conf"] == "medium", d
        assert ("road", "High", "AADT 2,265 (2025) from an NDDOT count station (NDDOT_traffic_counts.gpkg, ASH COULEE "
                                "DR) (not the AADT 311 from ND county roads: the higher count on the same road)") in imp, imp
        os.remove(st)
        # 3. one street cut into 2 NG911 segments, both matched to 28: one road, 2 segments in the review flag
        half = (local[0][0], XY(20, 0)[1])
        ng = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [([local[0], half], "98TH ST SE", "Local", 7),
                                                                      ([half, local[1]], "98TH ST SE", "Local", 7)])
        _lines_file(cty, "ND_county_roads", [([(p[0] + 2.7, p[1]) for p in local], 28.0, 2023, "")])
        d = _tool8(work, ng)[0]["DWR-1"]
        assert d["hz_class"] == "Low", d
        assert d["hz_aadt_review"].startswith("Class Low rests on traffic counts matched from other layers for 1 road(s), "
                                              "2 segments (98TH ST SE (2 segments): AADT 28 (2023)"), d["hz_aadt_review"]
        assert d["hz_aadt"].startswith("2 of 2 overtopped road(s) without AADT matched; 98TH ST SE (2 segments): AADT 28")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("OK tool 8: FUNC_SYS 1 ramp Interstate, SHIELD I ramp not lowered, same-road station 2,265 wins, "
          "segments counted once")


def test_tool6_interstate_system():
    work = tempfile.mkdtemp(prefix="raj6r_")
    try:
        w = _World(work)
        roads_dir = os.path.join(work, "roads")
        os.makedirs(roads_dir)
        local = [w.xy(40, 185), w.xy(80, 185)]
        cty = _lines_file(os.path.join(roads_dir, "ND_county_roads.gpkg"), "ND_county_roads",
                          [([(p[0] + 5.0, p[1]) for p in local], 83.0, 2023, "")]) + "|layername=ND_county_roads"
        ramp = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Ramp", 1)])
        r = w.run(ROADS=ramp, ROADS_MORE=cty)               # an interstate ramp matched to 83 still blocks the clear
        assert r["tri_result"] == "needs 2D" and r["tri_aadt"] is None, r
        r = w.run(ROADS=ramp, ROADS_MORE=cty, AADT_JOIN=False)
        assert r["tri_result"] == "needs 2D" and r["tri_aadt"] is None, r
        shield = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Local", 7, "EXIT")])
        r = w.run(ROADS=shield, ROADS_MORE=cty)             # SHIELD EXIT: a matched count never clears it either
        assert r["tri_result"] == "needs 2D" and r["tri_aadt"] is None, r
        loc = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(local, "RAMP", "Ramp", 7)])
        r = w.run(ROADS=loc, ROADS_MORE=cty)
        assert r["tri_result"] == "clear" and r["tri_aadt"], r
        os.remove(os.path.join(roads_dir, "ND_county_roads.gpkg"))
        # corridor (beyond the envelope, row 120): a FUNC_SYS 1 ramp is an interstate there, High-capable whatever its
        # count and with or without the join; before 0.58.0 it was an unknown-AADT road, not High-capable, and cleared
        cross = [w.xy(120, 0), w.xy(120, w.NX - 1)]
        hp = _lines_file(os.path.join(roads_dir, "HPMS_ND_2024.gpkg"), "HPMS_ND_2024",
                         [([(p[0], p[1] + 6.0) for p in cross], 41.0, 2024, "")], year_field="DATA_YEAR")
        for row in ((cross, "RAMP", "Ramp", 1), (cross, "RAMP", "Local", 7, "I", "29")):
            ramp = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [row])
            for kw in ({}, {"AADT_JOIN": False}):
                r = w.run(corridor=True, ROADS=ramp, **kw)
                assert r["tri_result"] == "needs 2D" and r["tri_corr"] == "hit", (row, kw, r)
                assert r["tri_corr_hit"].startswith("RAMP (interstate)"), (row, kw, r["tri_corr_hit"])
        # ... a SHIELD I ramp with no route number is not (Hazard: by its count): unknown AADT, not High-capable
        ramp = _ng911x(os.path.join(roads_dir, "ND_roads_NG911.gpkg"), [(cross, "RAMP", "Local", 7, "I")])
        r = w.run(corridor=True, ROADS=ramp)
        assert r["tri_result"] == "clear" and r["tri_corr"] == "none", r
        os.remove(hp)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("OK tool 6: a FUNC_SYS 1 ramp (Interstate) or a SHIELD EXIT ramp is never cleared on a matched count")


def test_handoff_and_tool9():
    """Tool 7b sends a dam whose class rests on matched counts (before the join it was Medium, so it was sent);
    dam.json records the flag; tool 9 has tool 8's AADT_JOIN switch and passes it on."""
    from nd_dam_screening.core import handoff as ho, damrecord_io as dri
    low = {"dam_id": "DWR-4031", "hz_class": "Low", "hz_vs_existing": "agree",
           "hz_aadt_review": "Class Low rests on traffic counts matched from other layers for 1 road(s) (...): review"}
    for mode in (ho.SELECT_HIGH_MEDIUM, ho.SELECT_CHANGED):
        ok, why = ho.selected(low, mode)
        assert ok and "rests on traffic counts matched from other layers (hz_aadt_review)" in why, (mode, why)
        assert not ho.selected(dict(low, hz_aadt_review=None), mode)[0]
        assert not ho.selected(dict(low, hz_aadt_review="NULL"), mode)[0]
    assert not ho.selected(low, ho.SELECT_HIGH)[0]                           # 'High only' stays High only
    assert "hz_aadt_review" in ho.SELECT_LABELS[ho.SELECT_HIGH_MEDIUM] and "hz_aadt_review" in ho.SELECT_LABELS[
        ho.SELECT_CHANGED]
    assert dri.screening_result(low, "tool 8")["aadt_review"].startswith("Class Low rests on")
    assert "aadt_review" not in dri.screening_result({"hz_class": "Low"}, "tool 8")
    from nd_dam_screening.algorithms.sensitivity import Sensitivity
    alg = Sensitivity()
    alg.initAlgorithm()
    assert alg.parameterDefinition("AADT_JOIN").defaultValue is True
    import inspect
    src = inspect.getsource(Sensitivity.processAlgorithm)
    assert '"AADT_JOIN": self.parameterAsBool(params, "AADT_JOIN", context)' in src
    print("OK tool 7b sends flagged Lows; dam.json aadt_review; tool 9 AADT_JOIN")


if __name__ == "__main__":
    test_copy_is_the_runners_file()
    test_join_core()
    test_wiring()
    test_tool8()
    test_tool6()
    test_join_rules()
    test_tool8_review_fixes()
    test_tool6_interstate_system()
    test_handoff_and_tool9()
    print("ALL OK")
