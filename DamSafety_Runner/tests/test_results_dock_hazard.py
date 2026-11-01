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
def results_dock_class_of_record_is_a_hook_until_nick_decides():
    assert RI.CLASS_OF_RECORD_KEYS == ()
    assert RI.class_of_record({"fields": {"nd_class": "Medium"}}) is None
    line = RI.class_line({"nd_class": "High", "hazard_version": "2.19.1"},
                         {"class_of_record": "Medium"})
    assert "class of record Medium (differs from printed High)" in line, line
    line = RI.class_line({"nd_class": "High"}, {"class_of_record": "high"})
    assert "differs" not in line, line


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
