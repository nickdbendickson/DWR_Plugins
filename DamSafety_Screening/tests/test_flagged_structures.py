"""0.17.26: flagged structures (damsuite/structure_flags.py, docs/Flagged_structures.md).

DWR-3161 / ND00597 (Sanish Bay WPA Dam 1) is the ND-23 road fill over backwater: Screening lists
it with 'road fill over backwater; breach analysis not applicable; hazard Low (DWR record)'
instead of computing a breach flood or a breach hazard - tool 1 writes struct_cls / flag_note,
tools 5, 6, 7 and 12 skip it, 7b never sends it, tool 8 gives Low from the record.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_flagged_structures.py
"""
import inspect, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from qgis.core import QgsGeometry, QgsPointXY, QgsProcessingContext, QgsProcessingFeedback, make_layer  # noqa: E402
import numpy as np  # noqa: E402
from nd_dam_screening.damsuite import structure_flags as SF  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.core import handoff as ho, report as rp  # noqa: E402
from nd_dam_screening.algorithms.hazard_classification import HazardClassification  # noqa: E402
from nd_dam_screening.algorithms.prepare_dams import PrepareDams, OUT_FIELDS, FLAG_FIELDS  # noqa: E402

NOTE = "road fill over backwater; breach analysis not applicable; hazard Low (DWR record)"
work = tempfile.mkdtemp(prefix="flag_")
Sx, Dd = "string", "double"
try:
    # ---- 1. the list and the lookup
    assert SF.problems() == [], SF.problems()
    e = SF.lookup("DWR-3161")
    assert e and e["nid_id"] == "ND00597" and e["hazard"] == "Low" and SF.note(e) == NOTE
    for ids in (("dwr-3161",), ("3161",), (None, "ND00597"), ("NID-ND00597",), ("X", "nd00597")):
        assert SF.lookup(*ids), ids
    assert SF.lookup("DWR-31610") is None and SF.lookup("ND00598") is None and SF.lookup("") is None
    print("OK (lookup)")

    # ---- 2. cm.flagged on a feature, handoff selection, review queue
    lay = make_layer([("dam_id", Sx), ("nid_id", Sx)],
                     [(QgsGeometry.fromPointXY(QgsPointXY(0, 0)), {"dam_id": "DWR-3161", "nid_id": "ND00597"}),
                      (QgsGeometry.fromPointXY(QgsPointXY(1, 1)), {"dam_id": "NID-ND00597", "nid_id": None}),
                      (QgsGeometry.fromPointXY(QgsPointXY(2, 2)), {"dam_id": "DWR-5289", "nid_id": "ND00123"})],
                     "point")
    fs = list(lay.getFeatures()); names = lay.fields().names()
    got = [cm.flagged(f, names) for f in fs]
    assert got[0][0] and got[1][0] and got[2] == (None, ""), got
    assert got[0][1] == "flagged structure (DWR-3161 / ND00597): " + NOTE, got[0][1]
    for mode in (ho.SELECT_ALL, ho.SELECT_HIGH_MEDIUM, ho.SELECT_CLASSIFIED, ho.SELECT_HIGH):
        ok, why = ho.selected({"dam_id": "DWR-3161", "hz_class": "High"}, mode, True)
        assert not ok and why.startswith("flagged structure") and NOTE in why, (mode, why)
    assert ho.selected({"dam_id": "DWR-5289"}, ho.SELECT_ALL) == (True, "all dams")
    assert "simulation failed" not in rp.review_reasons({"status_sim": "flagged", "hz_class": "Low"})
    print("OK (feature flag, 7b selection, review queue)")

    # ---- 3. tool 1 keeps the dam with struct_cls / flag_note (new fields; the old ones unchanged)
    assert [n for n, _ in OUT_FIELDS][:12] == ["dam_id", "name", "source", "nid_id", "height_m", "inv_norm_m3",
                                               "inv_max_m3", "inv_area_m2", "haz_existing", "status",
                                               "crest_len_m", "shared_vol"]
    assert len(OUT_FIELDS) == 12 and [n for n, _ in FLAG_FIELDS] == ["struct_cls", "flag_note"]
    # the DWR shapefile export's 10-character names (the tool's defaults)
    DWRF = [("structure_", Dd), ("dam_name", Sx), ("max_dam_he", Dd), ("normal_vol", Dd),
            ("max_pool_v", Dd), ("normal_are", Dd), ("hazard_cla", Sx), ("status", Sx),
            ("national_i", Sx), ("dam_length", Dd)]
    dwr = make_layer(DWRF, [
        (QgsGeometry.fromPointXY(QgsPointXY(-102.5116, 47.97959)),
         {"structure_": 3161.0, "dam_name": "Sanish Bay WPA Dam 1", "max_dam_he": 28.1,
          "normal_vol": 62.5, "max_pool_v": 2861.0, "normal_are": 22.1, "hazard_cla": "Low",
          "status": "Intact", "national_i": "ND00597", "dam_length": 0.0}),
        (QgsGeometry.fromPointXY(QgsPointXY(-100.0, 47.0)),
         {"structure_": 5289.0, "dam_name": "Other Dam", "max_dam_he": 20.0, "normal_vol": 100.0,
          "max_pool_v": 300.0, "normal_are": 10.0, "hazard_cla": "Low", "status": "Intact",
          "national_i": "ND00123", "dam_length": 300.0})], "point", "dwr")
    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    r1 = PrepareDams().run_mock({"DWR": ctx.add(dwr), "OUT": "memory:"}, ctx, fb)
    out = ctx.layers[r1["OUT"]]
    rows = {f["dam_id"]: f for f in out.getFeatures()}
    assert set(rows) == {"DWR-3161", "DWR-5289"}, set(rows)
    assert rows["DWR-3161"]["struct_cls"] == "road_fill_backwater" and rows["DWR-3161"]["flag_note"] == NOTE
    assert rows["DWR-3161"]["haz_existing"] == "Low" and rows["DWR-3161"]["nid_id"] == "ND00597"
    assert rows["DWR-5289"]["struct_cls"] in (None, "") and rows["DWR-5289"]["flag_note"] in (None, "")
    assert any("flagged structures (no breach analysis): 1" in m for m in fb.log), fb.log
    print("OK (tool 1)")

    # ---- 4. tool 8: a breach result that would be High is NOT used: hazard Low from the record
    cell = 10.0
    gt = (0.0, cell, 0.0, 100.0, 0.0, -cell)
    depth = np.zeros((10, 10)); vel = np.zeros((10, 10))
    depth[2, 2], vel[2, 2] = 3.0, 3.0
    dtif, vtif = os.path.join(work, "d.tif"), os.path.join(work, "v.tif")
    cm.write_tif(dtif, depth, gt); cm.write_tif(vtif, vel, gt)
    xy = lambda r, c: (gt[0] + (c + 0.5) * cell, gt[3] - (r + 0.5) * cell)
    pt = lambda r, c: QgsGeometry.fromPointXY(QgsPointXY(*xy(r, c)))
    SIMF = [("dam_id", Sx), ("nid_id", Sx), ("sim_status", Sx), ("depth_tif", Sx), ("vel_tif", Sx),
            ("haz_existing", Sx)]
    sim = make_layer(SIMF, [
        (pt(0, 0), {"dam_id": "DWR-3161", "nid_id": "ND00597", "sim_status": "ok", "depth_tif": dtif,
                    "vel_tif": vtif, "haz_existing": "Low"}),
        (pt(0, 1), {"dam_id": "DWR-1", "nid_id": "", "sim_status": "ok", "depth_tif": dtif,
                    "vel_tif": vtif, "haz_existing": "Low"}),
        (pt(0, 2), {"dam_id": "DWR-2", "nid_id": "ND00597", "sim_status": "flagged", "haz_existing": "Low"})],
        "point", "sim")
    bld = make_layer([("OCC_CLS", Sx), ("PRIM_OCC", Sx)],
                     [(QgsGeometry.fromMultiPointXY([QgsPointXY(*xy(2, 2))]), {"OCC_CLS": "Residential",
                                                                                "PRIM_OCC": "Single Family"})],
                     "point", "bld")
    # a railroad through the flooded cell: the flag check must not disturb the other dams' rail
    # sampling (a variable clash broke it once)
    rail = make_layer([("trains_day", Dd)],
                      [(QgsGeometry.fromPolylineXY([QgsPointXY(*xy(2, 0)), QgsPointXY(*xy(2, 9))]),
                        {"trains_day": 4.0})], "line", "rail")
    ctx2, fb2 = QgsProcessingContext(), QgsProcessingFeedback()
    r8 = HazardClassification().run_mock({"SIM": ctx2.add(sim), "BLD": ctx2.add(bld), "RAIL": ctx2.add(rail),
                                          "CRIT": 2}, ctx2, fb2)
    o8 = ctx2.layers[r8["OUT"]]
    n8 = o8.fields().names()
    rows8 = {f["dam_id"]: {k: f[k] for k in n8} for f in o8.getFeatures()}
    d = rows8["DWR-3161"]
    assert d["hz_class"] == "Low" and d["hz_conf"] == "flagged", d
    assert d["hz_reason"] == "flagged structure (DWR-3161 / ND00597): " + NOTE, d["hz_reason"]
    assert "breach analysis not applicable; hazard Low from the DWR record, not computed" in d["hz_notes"]
    assert d["n_bldg_wet"] == 0 and d["hz_vs_existing"] == "agree", d["hz_vs_existing"]
    assert rows8["DWR-1"]["hz_class"] != "Low" and rows8["DWR-1"]["n_bldg_wet"] == 1, rows8["DWR-1"]
    assert rows8["DWR-1"]["n_rail_wet"] == 1, rows8["DWR-1"]
    assert rows8["DWR-2"]["hz_class"] == "Low" and rows8["DWR-2"]["hz_conf"] == "flagged"   # by its NID id
    print("OK (tool 8):", d["hz_class"], d["hz_conf"])

    # ---- 5. tools 5, 6, 7 and 12 check the flag (source check; tool 8 above runs it)
    from nd_dam_screening.algorithms import (breach_hydrograph as t5, triage as t6, run_breach_sim as t7,
                                             estimate_runtime as t12)
    for mod in (t5, t6, t7, t12):
        assert "cm.flagged(f, " in inspect.getsource(mod), mod.__name__
    src7 = inspect.getsource(t7)
    assert '"status": "flagged"' in src7 and '"flagged": 0' in src7
    print("OK (tools 5, 6, 7, 12 skip)")
finally:
    shutil.rmtree(work, ignore_errors=True)
print("ALL OK")
