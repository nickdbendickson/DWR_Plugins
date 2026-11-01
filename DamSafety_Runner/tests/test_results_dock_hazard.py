"""3.13.6: the results dock's Hazard line, stale warning, receptor relabel, layer paths and
warning list (core/results_info.py). Docs & QA dock test on main db9c665, DWR-2801 r4: Hazard
2.19.1 printed HIGH (provisional) / TM 11 Significant, 45 flooded habitable structures, while the
Runner's summary said '37 structures'. Synthetic folders only (no DWR data)."""
import json
import os
import shutil
import tempfile
import time

from nd_breach_runner.core import results_info as RI
from nd_breach_runner.tests.registry import test

SUMMARY = """DamSafety Hazard v2.19.1 - Hazard Classification Summary
Dam: Test Dam 1    Generated: 2026-10-30 14:32
Hydraulic model: 2D (depth-velocity curves)

ND HAZARD CLASSIFICATION (screening): HIGH  |  incomplete: flood cut off (duration cap)
  - PROVISIONAL - HIGH is provisional: the judgment-zone dwelling is 0.16 ft past the line.
  - HIGH - 1 habitable structure(s) in the ACER judgment zone [5.2.1.1]
  - MEDIUM - 45 habitable structure(s) inundated (economic loss) [5.2.2]; damage $1
  Result   ND hazard classification (screening)          HIGH (provisional)

ACER TM 11 CLASSIFICATION: SIGNIFICANT  (lives in jeopardy = 3.2)
"""


def _dam(summary=SUMMARY, status=True):
    d = tempfile.mkdtemp(prefix="rdock_")
    os.makedirs(os.path.join(d, "_breach_model"))
    os.makedirs(os.path.join(d, "outputs", "Classification"))
    rep = {"work_crs": "EPSG:5070", "warnings": ["a", "b", "c"],
           "outputs": {"final": {"depth": "/nowhere/Depth (Max).tif"}}}
    json.dump(rep, open(os.path.join(d, "_breach_model", "run_report.json"), "w"))
    for n in ("Depth (Max).tif", "Velocity (Max).tif"):
        open(os.path.join(d, n), "wb").write(b"x")
    open(os.path.join(d, "_breach_model", "receptors_flooded.csv"), "w").write(
        "id,kind,x,y,depth_ft,velocity_fps,arrival_hr\n1,structure,0,0,1,1,1\n")
    open(os.path.join(d, "outputs", "Classification", "Habitable_Structures.gpkg"), "wb").write(b"x")
    old = time.time() - 3600
    for root, _dn, fn in os.walk(d):
        for f in fn:
            os.utime(os.path.join(root, f), (old, old))
    if summary is not None:
        open(os.path.join(d, "outputs", RI.SUMMARY_TXT), "w").write(summary)
    if status:
        rec = {"format": "nd_dam_suite/dam_record", "format_version": 1,
               "fields": {"dam_id": "T-1", "name": "Test Dam 1"}, "status": [
                   {"step": "modelled", "tool": "ndbreach", "version": "3.11.1",
                    "when": "2026-10-30T12:37:16", "result": {"breach_occurs": True}},
                   {"step": "classified", "tool": "ndbreach", "version": "3.11.1",
                    "when": "2026-10-30T13:17:58", "result": {"preview": True}},
                   {"step": "classified", "tool": "ndhazard", "version": "2.19.1",
                    "when": "2026-10-30T14:32:41",
                    "result": {"nd_class": "High", "tm11_class": "Significant",
                               "output_folder": "outputs"}}]}
        json.dump(rec, open(os.path.join(d, "dam.json"), "w"))
    return d


@test
def results_dock_reads_the_hazard_summary():
    d = _dam()
    try:
        hz = RI.hazard_summary(d)
        assert hz["hazard_version"] == "2.19.1", hz
        assert hz["nd_class"] == "High" and hz["tm11_class"] == "Significant", hz
        assert hz["provisional"] is True and hz["flooded_structures"] == 45, hz
        rec = RI.record_status(d)
        assert rec["nd_class"] == "High" and rec["classified_by"] == "ndhazard", rec
        assert rec["classified_version"] == "2.19.1" and rec["next_action"][0], rec
        line = RI.class_line(hz, rec)
        assert "Hazard 2.19.1" in line and "ND High (provisional)" in line, line
        assert "TM 11 Significant" in line and "class of record: not recorded" in line, line
    finally:
        shutil.rmtree(d)


@test
def results_dock_class_of_record_line_without_review_json():
    d = _dam()
    try:
        cor = RI.class_of_record(d, os.path.join(d, "_breach_model"), RI.hazard_summary(d), {})
        assert cor["state"] == "missing" and cor["warnings"] == [], cor
        assert "class of record: not recorded" in RI.class_line({"nd_class": "High"}, {}, cor)
    finally:
        shutil.rmtree(d)


@test
def results_dock_warns_when_the_hazard_summary_is_stale():
    d = _dam()
    try:
        hz = RI.hazard_summary(d)
        run_t = RI.run_output_mtime(d, os.path.join(d, "_breach_model"))
        assert RI.staleness(hz, "2.19.1", run_t) == []
        w = RI.staleness(hz, "2.22.2", run_t)
        assert len(w) == 1 and "2.19.1" in w[0] and "2.22.2" in w[0], w
        assert RI.staleness(hz, "2.19.1", hz["mtime"] + 60)[0].startswith(
            "Hazard summary is older than the run outputs"), "run newer"
        assert RI.staleness({}, "9.9", 0) == []
        assert RI.staleness(hz, None, None) == []
    finally:
        shutil.rmtree(d)


@test
def results_dock_labels_runner_receptors_beside_the_hazard_count():
    assert RI.structure_count_text(37, {"flooded_structures": 45}) == \
        "37 Runner receptor structure(s); Hazard: 45 flooded habitable"
    assert RI.structure_count_text(37, {}) == "37 Runner receptor structure(s)"


@test
def results_dock_finds_layers_in_the_dam_folder_first():
    d = _dam(summary=None, status=False)
    try:
        m = os.path.join(d, "_breach_model")
        rep = json.load(open(os.path.join(m, "run_report.json")))
        p = RI.layer_paths(d, m, rep)
        assert set(p) == {"depth", "velocity", "receptors", "hazard_structures"}, p
        assert p["depth"] == os.path.join(d, "Depth (Max).tif"), p
        uri = RI.receptor_csv_uri(p["receptors"], "EPSG:5070")
        assert "xField=x&yField=y" in uri and uri.endswith("crs=EPSG:5070"), uri
        assert RI.warning_texts(rep) == ["a", "b", "c"]
        assert RI.hazard_summary(d) == {} and RI.record_status(d) == {"has_record": False}
        assert RI.class_line({}, {}).startswith("Hazard: not classified")
    finally:
        shutil.rmtree(d)


@test
def results_dock_json_summary_wins_key_by_key():
    d = _dam()
    try:
        json.dump({"nd_class": "Medium", "flooded_structures": 44},
                  open(os.path.join(d, "outputs", RI.SUMMARY_JSON), "w"))
        hz = RI.hazard_summary(d)
        assert hz["nd_class"] == "Medium" and hz["flooded_structures"] == 44, hz
        assert hz["hazard_version"] == "2.19.1", hz
    finally:
        shutil.rmtree(d)


@test
def results_dock_vector_layers_load_filtered_with_counts():
    """Docs & QA: 'Flooded receptors' loaded all 673 receptors and 'Hazard structures' all 3,809
    structures. Both now carry a subset string and a count of the wet features."""
    assert RI.LAYER_FILTERS == {"receptors": '"depth_ft" > 0',
                                "hazard_structures": '"Max_Depth_ft" > 0'}
    d = tempfile.mkdtemp(prefix="rdock_cnt_")
    try:
        csvp = os.path.join(d, "receptors_flooded.csv")
        open(csvp, "w").write("id,kind,x,y,depth_ft,velocity_fps,arrival_hr\n"
                              "1,structure,0,0,1.5,1,1\n2,road,0,0,0.2,1,1\n"
                              "3,structure,0,0,0,0,\n4,road,0,0,,,\n")
        layers = {"receptors": csvp}
        try:
            from osgeo import ogr, osr
        except ImportError:
            ogr = None
        if ogr is not None:
            gp = os.path.join(d, "Habitable_Structures.gpkg")
            ds = ogr.GetDriverByName("GPKG").CreateDataSource(gp)
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(5070)
            lyr = ds.CreateLayer("Habitable_Structures", srs, ogr.wkbPoint)
            lyr.CreateField(ogr.FieldDefn("Habitable", ogr.OFTString))
            lyr.CreateField(ogr.FieldDefn("Max_Depth_ft", ogr.OFTReal))
            for hab, dep in (("Yes", 2.0), ("Unverified", 0.5), ("Yes", 0.0), ("Yes", None)):
                f = ogr.Feature(lyr.GetLayerDefn())
                f.SetField("Habitable", hab)
                if dep is not None:
                    f.SetField("Max_Depth_ft", dep)
                f.SetGeometry(ogr.CreateGeometryFromWkt("POINT (0 0)"))
                lyr.CreateFeature(f)
            ds = None
            layers["hazard_structures"] = gp
        c = RI.flooded_counts(layers)
        assert c["receptors"] == (2, 4, {"structure": 1, "road": 1}), c
        assert RI.layer_label("receptors", c) == "Flooded receptors (2)"
        assert "2 of 4 (1 road, 1 structure)" in RI.layer_tooltip("receptors", csvp, c)
        if ogr is not None:
            assert c["hazard_structures"] == (2, 4, {"habitable": 1}), c
            assert RI.layer_label("hazard_structures", c) == "Wet Hazard structures (2)"
        assert RI.flooded_counts({"receptors": os.path.join(d, "nope.csv")}) == {}
        assert RI.layer_label("depth", c) == "Depth"
    finally:
        shutil.rmtree(d)
