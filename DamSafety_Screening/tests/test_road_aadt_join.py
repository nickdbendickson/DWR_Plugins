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
        st = _stations_file(os.path.join(roads_dir, "NDDOT_traffic_counts.gpkg"), [((100, 1505), 4108.0, 2023)])
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
                                              "road(s) (road 2 (87TH ST NW): AADT 83 (2023) from ND county roads "
                                              "(ND_county_roads.gpkg) (Low instead of Medium)); without them it "
                                              "would be Medium: review"), d["hz_aadt_review"]
        assert d["hz_aadt"].startswith("1 of 1 overtopped road(s) without AADT matched; road 2 (87TH ST NW): AADT 83"), \
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
        assert r["tri_aadt"].startswith("Triage clear rests on traffic counts matched from other layers for 1 road(s) "
                                        "(road 87TH ST NW: AADT 83 (2023) from ND county roads (ND_county_roads.gpkg)"
                                        " (Low instead of Medium)); without them it needs 2D: review"), r["tri_aadt"]
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
        assert r["tri_aadt"].startswith("Triage clear rests on traffic counts matched from other layers for 1 road(s) "
                                        "(road MAIN ST USHY in the corridor: AADT 1,200 (2024) from HPMS"), r["tri_aadt"]
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


if __name__ == "__main__":
    test_copy_is_the_runners_file()
    test_join_core()
    test_wiring()
    test_tool8()
    test_tool6()
    print("ALL OK")
