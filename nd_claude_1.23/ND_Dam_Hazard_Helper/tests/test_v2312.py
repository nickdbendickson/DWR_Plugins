"""2.31.2 (Lumo round 2): reproduction fingerprints and comparison methods, export drop counts,
model-chain reason codes, DSS-WISE plausibility.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


@test
def v2312_folder_fingerprint_sees_an_edit_inside_the_folder():
    from ND_Dam_Hazard_Helper import reproduce as RP
    d = os.path.join(tempfile.mkdtemp(), "x.gdb")
    os.makedirs(os.path.join(d, "sub"))
    for rel, body in (("a0001.gdbtable", b"aaaa"), ("sub/b.gdbx", b"bb")):
        with open(os.path.join(d, rel), "wb") as fh:
            fh.write(body)
    fp = RP.fingerprint(d)
    assert fp["kind"] == "folder" and fp["sha256"] and fp["unhashed_files"] == 0
    assert [r for r, _s, _h in fp["files"]] == ["a0001.gdbtable", "sub/b.gdbx"]
    assert RP.comparison_method(fp)[0] == "folder-sha256"
    st = os.stat(os.path.join(d, "a0001.gdbtable"))
    with open(os.path.join(d, "a0001.gdbtable"), "wb") as fh:
        fh.write(b"aaab")                                  # same size
    os.utime(os.path.join(d, "a0001.gdbtable"), (st.st_atime, st.st_mtime))
    assert RP._changed_since_run(d, fp) is True
    # an old record (no per-file list) is still read: size + date
    old = {"path": d, "exists": True, "kind": "folder", "size": fp["size"],
           "modified": fp["modified"], "sha256": None}
    assert RP._changed_since_run(d, old) is False
    assert RP.comparison_method(old)[0] == "size-date-folder"
    assert RP.fingerprint(d, hash_files=False).get("files") is None


@test
def v2312_comparison_method_names_large_files():
    from ND_Dam_Hazard_Helper import reproduce as RP
    big = {"exists": True, "kind": "file", "size": RP.HASH_MAX_BYTES + 1, "sha256": None}
    assert RP.comparison_method(big)[0] == "size-date-large"
    assert "over 1 GB" in RP.comparison_method(big)[1]
    assert RP.comparison_method({"exists": True, "kind": "file", "sha256": "ab"})[0] == "sha256"
    part = {"exists": True, "kind": "folder", "files": [], "sha256": "x", "unhashed_files": 2}
    assert RP.comparison_method(part)[0] == "folder-partial"


@test
def v2312_projected_counts_dropped_features():
    from ND_Dam_Hazard_Helper import suiteexport as SE
    feats = [{"geometry": {"type": "Point", "coordinates": [-100.5, 47.0]}},
             {"geometry": None}, {"geometry": {"type": "Point", "coordinates": []}}]
    dropped = []
    out = SE.projected(feats, dropped)
    assert len(out) == 1 and len(dropped) == 2
    assert "2 feature(s) could not be read/projected" in SE.dropped_note("NSI", 2)
    assert len(SE.projected(feats)) == 1                     # old call still works


@test
def v2312_model_chain_overall_uses_reason_codes():
    from ND_Dam_Hazard_Helper import modelchain as MC

    def row(name, reason, note="note text without the old phrases"):
        r = MC._row({"scenario": name, "role": "depth", "recorded_path": name + ".tif",
                     "fingerprint": {}})
        r.update(Status="DIFFERENT", Reason=reason, Note=note)
        return r
    rows = [row("A", MC.REASON_CHANGED_AFTER_RUN), row("B", MC.REASON_CHANGED_AFTER_CLASSIFICATION),
            row("C", MC.REASON_NOT_RUN_OUTPUT, note="changed after the model run (text only)")]
    st, text, _c = MC._overall(rows, {"x": 1}, (True, ""))
    assert st == "DIFFERENT"
    assert "A depth changed after the model run" in text
    assert "B depth changed after the classification" in text
    assert "C depth not the raster(s) the model wrote" in text
    assert "Reason" in MC.FIELDS
    assert not hasattr(MC, "read_check")


@test
def v2312_dsswise_implausible_maxima_are_warned():
    from ND_Dam_Hazard_Helper import dsswise as DW
    assert DW.implausible_warnings({"depth": 40.0, "velocity": 12.0}) == []
    w = DW.implausible_warnings({"depth": 984.3, "velocity": None})
    assert len(w) == 1 and "depth" in w[0] and "300" in w[0]
    w = DW.implausible_warnings({"depth": 10.0, "velocity": 150.0})
    assert len(w) == 1 and "velocity" in w[0]
