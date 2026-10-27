"""0.17.54: DWR dugouts (core/dugouts.py) in tool 1 (left out, listed with the reason) and the
suite-folder auto-fill of the DWR export's own file names. Tool 16 is in test_findponds.py and
test_ponds.py (they need scipy / GDAL).

Run: python3 tests/test_dwr_dugouts.py   (no QGIS needed: tests/qgis_mock)
"""
import csv
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
sys.path.insert(0, HERE)
from qgis.core import QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, make_layer  # noqa: E402
import _gpkg  # noqa: E402
from nd_dam_screening.core import dugouts as dg, workflow as wf  # noqa: E402
from nd_dam_screening.algorithms.prepare_dams import PrepareDams, OUT_FIELDS  # noqa: E402
from nd_dam_screening.damsuite import suite_fill as SFL, suitecheck as SC  # noqa: E402

Sx, Dd = "string", "double"
work = tempfile.mkdtemp(prefix="dugouts_")
try:
    # ---- 1. the matcher (metres)
    sq = [[(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0), (0.0, 0.0)]]
    ix = dg.DugoutIndex(points=[("7", 1000.0, 1000.0)], polygons=[("5", sq)])
    assert ix.buffer_m == dg.DEFAULT_BUFFER_M and len(ix) == 2 and ix.n_points == 1 and ix.n_polygons == 1
    assert abs(dg.DEFAULT_BUFFER_FT * 0.3048 - dg.DEFAULT_BUFFER_M) < 0.5
    assert ix.match(10.0, 10.0)[0] == "5" and "inside" in ix.match(10.0, 10.0)[1]
    assert ix.match(1020.0, 1000.0)[0] == "7" and "20 m" in ix.match(1020.0, 1000.0)[1]
    assert ix.match(1031.0, 1000.0) is None and ix.match(60.0, 10.0) is None
    assert dg.DugoutIndex(points=[("7", 1000.0, 1000.0)], buffer_m=50.0).match(1031.0, 1000.0)
    nan = float("nan")                           # empty geometries in the real export read as NaN
    ixn = dg.DugoutIndex(points=[("8", nan, nan), ("9", None, 1.0)], polygons=[("6", [[(nan, nan)] * 4])])
    assert len(ixn) == 0 and ixn.match(nan, nan) is None and ix.match(nan, 1.0) is None
    assert dg.reason("12") == "DWR dugout 12"
    assert dg.protected("Low", "") and dg.protected("", "ND00001") and dg.protected("High", "ND1")
    assert not dg.protected("Undetermined", "") and not dg.protected(None, None) and not dg.protected("", " ")
    d = dg.decide(ix, 10.0, 10.0, "Undetermined", "")
    assert d[0] == "exclude" and d[1] == "5" and d[2].startswith("DWR dugout 5")
    k = dg.decide(ix, 10.0, 10.0, "Significant", "ND00999")
    assert k[0] == "keep" and "kept" in k[2] and "ND00999" in k[2]
    assert dg.decide(ix, 500.0, 500.0, "", "") is None
    # a water body: its inner point, a dugout point in it, or a dugout polygon corner in it
    wb = [[(990.0, 990.0), (1010.0, 990.0), (1010.0, 1010.0), (990.0, 1010.0), (990.0, 990.0)]]
    assert ix.match_area(wb, (1000.0, 1000.0))[0] == "7"
    assert ix.match_area(wb, (2000.0, 2000.0))[0] == "7"           # a far inner point: the point in it
    wb2 = [[(30.0, 15.0), (60.0, 15.0), (60.0, 40.0), (30.0, 40.0), (30.0, 15.0)]]
    assert ix.match_area(wb2, (45.0, 30.0))[0] == "5"                # the polygon's corner (40, 20)
    assert ix.match_area([[(500.0, 500.0), (510.0, 500.0), (510.0, 510.0), (500.0, 500.0)]], (505.0, 503.0)) is None
    # a feature with a stray far-away part (the real export has one): each ring has its own box,
    # so the index stays small; a ring tens of km across is skipped and said in the summary
    far = [(5.0e6 + x, -4.6e6 + y) for x, y in sq[0]]
    huge = [(0.0, 0.0), (90000.0, 0.0), (90000.0, 90000.0), (0.0, 0.0)]
    ixf = dg.DugoutIndex(polygons=[("21", [sq[0], far]), ("22", [huge])])
    assert sum(len(v) for v in ixf._polys.values()) < 10 and ixf.n_polygons == 1 and ixf.n_skipped == 1
    assert ixf.match(10.0, 10.0)[0] == "21" and ixf.match(5.0e6 + 10.0, -4.6e6 + 10.0)[0] == "21"
    assert "1 polygon ring(s)" in ixf.summary() and "skipped" in ixf.summary()
    assert ix.summary() == "DWR dugouts: 1 point(s), 1 polygon(s), buffer 30 m"
    assert dg.above_grade_note(None, 1.0) == ("", False) and dg.above_grade_note(0.0, 1.0) == ("", False)
    n, w = dg.above_grade_note(2.0, 1.8)
    assert "2.00 ac-ft" in n and "LiDAR value is released" in n and not w
    assert dg.above_grade_note(12.0, 1.0)[1]
    assert dg.DugoutIndex(points=[(7, 0.0, 0.0)], above_grade_af={7: 3}).above_grade_af == {"7": 3.0}
    print("OK (matcher)")

    # ---- 2. tool 1
    DWRF = [("structure_", Dd), ("dam_name", Sx), ("max_dam_he", Dd), ("normal_vol", Dd),
            ("max_pool_v", Dd), ("normal_are", Dd), ("hazard_cla", Sx), ("status", Sx),
            ("national_i", Sx), ("dam_length", Dd)]

    def dam(sid, x, y, hz="Undetermined", nid=""):
        return (QgsGeometry.fromPointXY(QgsPointXY(x, y)),
                {"structure_": float(sid), "dam_name": "Dam %d" % sid, "max_dam_he": 10.0, "normal_vol": 20.0,
                 "max_pool_v": 30.0, "normal_are": 5.0, "hazard_cla": hz, "status": "Intact",
                 "national_i": nid, "dam_length": 100.0})
    dwr = make_layer(DWRF, [dam(1, 10.0, 10.0),                      # in dugout polygon 501
                            dam(2, 2020.0, 2000.0),                  # 20 m from dugout point 502
                            dam(3, 3005.0, 3000.0, hz="Low"),        # matched, DWR hazard class: kept
                            dam(4, 4000.0, 4005.0, nid="ND00444"),   # matched, NID id: kept
                            dam(5, 5000.0, 5000.0),                  # no dugout near
                            dam(6, 6045.0, 6000.0)], "point", "dwr")  # 45 m: outside 100 ft
    pts = make_layer([("structure_id", Dd), ("longitude", Dd)],
                     [(QgsGeometry.fromPointXY(QgsPointXY(x, y)), {"structure_id": float(s), "longitude": -100.0})
                      for s, x, y in ((502, 2000.0, 2000.0), (503, 3000.0, 3000.0), (504, 4000.0, 4000.0),
                                      (506, 6000.0, 6000.0))], "point", "dugouts")
    pol = make_layer([("structure_id", Dd)],
                     [(QgsGeometry.fromPolygonXY([[QgsPointXY(x, y) for x, y in sq[0]]]), {"structure_id": 501.0})],
                     "polygon", "dugout_polys")
    ctx = QgsProcessingContext()
    DW, DP, DPOL = ctx.add(dwr), ctx.add(pts), ctx.add(pol)
    alg = PrepareDams(); alg.initAlgorithm()
    assert {"DUGOUTS", "DUGOUT_POLYS", "DUGOUT_BUF_FT"} <= set(alg._params)
    assert float(getattr(alg._params["DUGOUT_BUF_FT"], "defaultValue")) == dg.DEFAULT_BUFFER_FT
    assert len(OUT_FIELDS) == 12                                    # the dam layer's fields unchanged

    def ids(r):
        return {str(f["dam_id"]) for f in ctx.layers[r["OUT"]].getFeatures()}
    fb0 = QgsProcessingFeedback()
    r0 = PrepareDams().run_mock({"DWR": DW, "OUT": "memory:"}, ctx, fb0)    # no dugouts: no change
    assert ids(r0) == {"DWR-%d" % i for i in range(1, 7)}, ids(r0)
    assert not any("dugout" in m.lower() for m in fb0.log), fb0.log
    fb = QgsProcessingFeedback()
    r1 = PrepareDams().run_mock({"DWR": DW, "DUGOUTS": DP, "DUGOUT_POLYS": DPOL, "OUT": "memory:"}, ctx, fb)
    assert ids(r1) == {"DWR-3", "DWR-4", "DWR-5", "DWR-6"}, ids(r1)
    log = "\n".join(fb.log)
    assert "DWR dugouts: 4 point(s), 1 polygon(s), buffer 30 m" in log, log
    assert "DWR-1: left out - DWR dugout 501 (inside the DWR dugout polygon)" in log, log
    assert "DWR-2: left out - DWR dugout 502 (20 m from the DWR dugout point)" in log, log
    assert any("DWR-3" in w and "kept" in w and "hazard class Low" in w for w in fb.warnings), fb.warnings
    assert any("DWR-4" in w and "kept" in w and "ND00444" in w for w in fb.warnings), fb.warnings
    assert "DWR-5" not in log and "DWR-6:" not in log
    assert "DWR dugouts left out: 2, kept with a warning: 2" in log, log
    # the buffer is the engineer's: 200 ft catches DWR-6 (45 m)
    r2 = PrepareDams().run_mock({"DWR": DW, "DUGOUTS": DP, "DUGOUT_BUF_FT": 200.0, "OUT": "memory:"}, ctx,
                                QgsProcessingFeedback())
    assert ids(r2) == {"DWR-1", "DWR-3", "DWR-4", "DWR-5"}, ids(r2)
    # only polygons
    r3 = PrepareDams().run_mock({"DWR": DW, "DUGOUT_POLYS": DPOL, "OUT": "memory:"}, ctx, QgsProcessingFeedback())
    assert ids(r3) == {"DWR-%d" % i for i in range(2, 7)}, ids(r3)
    # 1_dugouts.csv next to a file output
    PrepareDams()._write_dugouts({}, os.path.join(work, "1_dams.gpkg"),
                                 [("DWR-1", "Dam 1", "501", "exclude", "DWR dugout 501 (inside)")])
    with open(os.path.join(work, "1_dugouts.csv")) as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [{"dam_id": "DWR-1", "name": "Dam 1", "dwr_dugout": "501", "action": "exclude",
                     "reason": "DWR dugout 501 (inside)"}], rows
    print("OK (tool 1: left out with the reason, protected dams kept with a warning, empty = no change)")

    # ---- 3. the suite folder: DWR's own export file names (two-layer DWR_Dams.gpkg too)
    suite = os.path.join(work, "suite")
    os.makedirs(suite)
    DAMF = [("structure_id", "INTEGER"), ("dam_name", "TEXT"), ("hazard_class", "TEXT"), ("longitude", "REAL")]
    P = ("Point", (0.0, 0.0))
    _gpkg.write(os.path.join(suite, "DWR_Dams.gpkg"), {"dams": ("POINT", DAMF, [(P, {"structure_id": 1})]),
                                                       "DWR_Dams": ("POINT", DAMF, [(P, {"structure_id": 1})])})
    _gpkg.write(os.path.join(suite, "DWR_Dam_Lines.gpkg"), {"dams_linear_features": (
        "MULTILINESTRING", [("feature_id", "INTEGER"), ("structure_id", "INTEGER")], [])})
    _gpkg.write(os.path.join(suite, "DWR_Unknown_Structures.gpkg"), {"DWR_Unknown_Structures": (
        "POINT", [("structure_id", "INTEGER"), ("dam_name", "TEXT")], [])})
    _gpkg.write(os.path.join(suite, "DWR_Dugouts.gpkg"), {"DWR_Dugouts": (
        "POINT", [("structure_id", "INTEGER"), ("longitude", "REAL"), ("storage_above_grade_acft", "REAL")], [])})
    _gpkg.write(os.path.join(suite, "DWR_Dugouts_Polygons.gpkg"), {"DWR_Dugouts_Polygons": (
        "MULTIPOLYGON", [("feature_id", "INTEGER"), ("structure_id", "INTEGER")], [])})
    found, notes = SFL.scan(suite)
    want = {"dwr": ("DWR_Dams.gpkg", "DWR_Dams"), "dam_lines": ("DWR_Dam_Lines.gpkg", "dams_linear_features"),
            "unknown": ("DWR_Unknown_Structures.gpkg", "DWR_Unknown_Structures"),
            "dugouts": ("DWR_Dugouts.gpkg", "DWR_Dugouts"),
            "dugout_polygons": ("DWR_Dugouts_Polygons.gpkg", "DWR_Dugouts_Polygons")}
    for slot, (fname, layer) in want.items():
        assert found.get(slot) == "{}|layername={}".format(os.path.join(suite, fname), layer), (slot, found, notes)
    # tools 1 and 16 get them from the suite folder (no Inputs row); nothing there = nothing filled
    for step in ("1", "16"):
        p = wf.defaults(step, work, {"suite_folder": suite})
        assert str(p.get("DUGOUTS", "")).startswith(os.path.join(suite, "DWR_Dugouts.gpkg")), (step, p)
        assert str(p.get("DUGOUT_POLYS", "")).startswith(os.path.join(suite, "DWR_Dugouts_Polygons.gpkg")), p
    empty = os.path.join(work, "empty_suite"); os.makedirs(empty)
    p = wf.defaults("1", work, {"suite_folder": empty})
    assert not p.get("DUGOUTS") and not p.get("DUGOUT_POLYS"), p
    # the suite check says what the file is for
    txt = " ".join(str(r) for r in SC.FILE_ROLES if r and r[0] == "dwr_dugouts")
    assert "optional" in txt.lower() and "tool 1" in txt.lower() and "16" in txt, txt
    print("OK (suite folder names, auto-fill, suite check text)")

    # ---- 4. damsuite 1.17.7: the offline pack's dwr/DWR_dugouts*.gpkg when the suite folder has none
    from nd_dam_screening.damsuite import gpkgpack as GP
    for k in ("NDSUITE_PACK", "NDSUITE_SOURCE"):
        os.environ.pop(k, None)
    pack = os.path.join(work, "ND_Dam_Suite_Pack")
    man = GP.new_manifest((-104.1, 45.9, -96.5, 49.1))
    GP.add_dataset(pack, man, "DWR_dugouts", lambda: [{"type": "Feature", "geometry": {
        "type": "Point", "coordinates": [-100.25, 46.85]}, "properties": {"structure_id": 501, "longitude": -100.25}}],
        {"label": "ND DWR dugouts", "note": "test"})
    GP.add_dataset(pack, man, "DWR_dugout_polygons", lambda: [{"type": "Feature", "geometry": {
        "type": "Polygon", "coordinates": [[[-100.251, 46.849], [-100.249, 46.849], [-100.249, 46.851],
                                            [-100.251, 46.849]]]}, "properties": {"structure_id": 501}}],
        {"label": "ND DWR dugout polygons", "note": "test"})
    GP.write_manifest(pack, man)
    SFL._SCAN.clear()
    p = wf.defaults("1", work, {"suite_folder": empty, "pack": pack})
    assert str(p.get("DUGOUTS", "")) == os.path.join(pack, "dwr", "DWR_dugouts.gpkg") + "|layername=DWR_dugouts", p
    assert str(p.get("DUGOUT_POLYS", "")).startswith(os.path.join(pack, "dwr", "DWR_dugout_polygons.gpkg")), p
    p = wf.defaults("16", work, {"suite_folder": suite, "pack": pack})          # the engineer's file wins
    assert str(p.get("DUGOUTS", "")).startswith(os.path.join(suite, "DWR_Dugouts.gpkg")), p
    assert str(p.get("DUGOUT_POLYS", "")).startswith(os.path.join(suite, "DWR_Dugouts_Polygons.gpkg")), p
    os.environ["NDSUITE_SOURCE"] = "download"                          # Download mode: no pack
    SFL._SCAN.clear()
    try:
        p = wf.defaults("1", work, {"suite_folder": empty})
        assert not p.get("DUGOUTS") and not p.get("DUGOUT_POLYS"), p
    finally:
        os.environ.pop("NDSUITE_SOURCE", None)
    print("OK (the offline pack's DWR dugouts fill tools 1 / 16 when the suite folder has none)")
finally:
    shutil.rmtree(work, ignore_errors=True)
