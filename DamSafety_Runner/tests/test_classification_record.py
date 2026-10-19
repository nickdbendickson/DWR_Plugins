"""The signed, hash-bound Classification Record (damsuite/record.py). QGIS-free; run with
PYTHONPATH=<repo root> python3 DamSafety_Runner/tests/test_classification_record.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))   # repo root (for nd_breach_runner)

from nd_breach_runner.damsuite import record, chain
from nd_breach_runner.tests.registry import test

_CLASS = {"hazard_class": "High",
          "governing": [{"reason": "2 inhabited structures in the high-danger zone",
                         "section": "5.2.1.1"}],
          "policy": "ND REG_05.2026", "module_version": "1.1.0"}
_CHARTS_SIGNED = {"signed_off_by": "Jane Doe, PE", "signed_off_on": "2026-10-12",
                  "digitized_by": "Jane Doe, PE", "reference": "Reclamation (1988) ACER TM-11"}


def _write_manifest(dam_folder, exe_path, inputs=None, extra=None):
    """A self-consistent run_manifest.json in <dam>/_breach_model/ (what the Runner writes)."""
    man = {"tool": "ND Dam Breach Runner 3.10.21",
           "software": {"lisflood_exe": {"path": exe_path, "sha256": "e" * 64}},
           "inputs": inputs or {"DEM": {"path": "dem.tif", "sha256": "d" * 64}},
           "outputs": {"Depth (Max).tif": {"path": "Depth (Max).tif", "sha256": "f" * 64}}}
    man["manifest_sha256"] = chain.digest_of(man)
    md = os.path.join(dam_folder, chain.MODEL_FOLDER)
    os.makedirs(md, exist_ok=True)
    with open(os.path.join(md, chain.MANIFEST_NAME), "w", encoding="utf-8") as f:
        import json
        json.dump(man, f)
    return man


@test
def classification_record_binds_and_verifies_a_good_run():
    d = tempfile.mkdtemp(prefix="rec_")
    _write_manifest(d, "/opt/lisflood/lisflood")             # a real LISFLOOD-FP binary
    rec = record.build_record(_CLASS, dam_folder=d, charts=_CHARTS_SIGNED, loss_rates=None)
    assert rec["hazard_class"] == "High" and rec["record_sha256"]
    assert rec["engine"]["model"] == "LISFLOOD-FP" and rec["engine"]["built_in"] is False
    assert rec["run_manifest"]["found"] and rec["run_manifest"]["self_consistent"]
    assert rec["charts"]["signer"] == "Jane Doe, PE"
    v = record.verify_record(rec, dam_folder=d)
    assert v["ok"], v["problems"]
    # round-trips through disk
    p = record.write_record(d, rec)
    assert record.read_record(p) == rec


@test
def classification_record_detects_tampering():
    rec = record.build_record(_CLASS, charts=_CHARTS_SIGNED,
                              manifest=_good_manifest("/opt/lisflood/lisflood"))
    rec["hazard_class"] = "Low"                              # someone downgraded the class
    v = record.verify_record(rec)
    assert not v["ok"] and any("edited after it was written" in p for p in v["problems"]), v


@test
def classification_record_refuses_the_builtin_engine():
    man = _good_manifest("/x/DamSafety_Runner/core/builtin_lisflood.py")
    rec = record.build_record(_CLASS, charts=_CHARTS_SIGNED, manifest=man)
    assert rec["engine"]["built_in"] is True
    v = record.verify_record(rec)
    assert not v["ok"] and any("built-in screening engine" in p for p in v["problems"]), v


@test
def classification_record_refuses_unsigned_charts_and_unsigned_loss():
    man = _good_manifest("/opt/lisflood/lisflood")
    # unsigned charts
    rec = record.build_record(_CLASS, charts={"digitized_by": ""}, manifest=man)
    v = record.verify_record(rec)
    assert not v["ok"] and any("not signed off (no signer)" in p for p in v["problems"]), v
    # a rate table present but not signed off
    rec2 = record.build_record(_CLASS, charts=_CHARTS_SIGNED, manifest=man,
                               loss_rates={"source": "a table with no signer"})
    v2 = record.verify_record(rec2)
    assert not v2["ok"] and any("not signed off" in p for p in v2["problems"]), v2


@test
def classification_record_refuses_without_a_bound_manifest():
    rec = record.build_record(_CLASS, charts=_CHARTS_SIGNED, manifest=None)
    assert rec["run_manifest"]["found"] is False
    v = record.verify_record(rec)
    assert not v["ok"] and any("no breach-run manifest is bound" in p for p in v["problems"]), v


@test
def classification_record_detects_a_changed_manifest_on_disk():
    d = tempfile.mkdtemp(prefix="rec_")
    _write_manifest(d, "/opt/lisflood/lisflood")
    rec = record.build_record(_CLASS, dam_folder=d, charts=_CHARTS_SIGNED)
    assert record.verify_record(rec, dam_folder=d)["ok"]
    # the run is re-done / edited after the record was signed -> a different manifest digest
    _write_manifest(d, "/opt/lisflood/lisflood",
                    inputs={"DEM": {"path": "dem.tif", "sha256": "a" * 64}})
    v = record.verify_record(rec, dam_folder=d)
    assert not v["ok"] and any("changed since this record was made" in p for p in v["problems"]), v


def _good_manifest(exe):
    man = {"tool": "ND Dam Breach Runner 3.10.21",
           "software": {"lisflood_exe": {"path": exe, "sha256": "e" * 64}},
           "inputs": {"DEM": {"path": "dem.tif", "sha256": "d" * 64}}, "outputs": {}}
    man["manifest_sha256"] = chain.digest_of(man)
    return man


if __name__ == "__main__":
    from nd_breach_runner.tests.registry import TESTS
    fns = [f for f in TESTS if f.__name__.startswith("classification_record_")]
    for f in fns:
        f()
        print("ok    ", f.__name__)
    print("{} tests, 0 failed".format(len(fns)))
