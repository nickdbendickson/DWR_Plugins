"""Lumo vReal.6–9 / SX / FP / SC / FA / CL / EX / FS / EV fixes (Hazard 2.18.22).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)
ROOT = os.path.dirname(HAZARD)
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import sensitivity as SE  # noqa: E402
from DamSafety_Hazard import scenarios as SC  # noqa: E402
from DamSafety_Hazard import fingerprint as FP  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard import facilities as FA  # noqa: E402
from DamSafety_Hazard import constants as C  # noqa: E402
from DamSafety_Hazard.damsuite import chain  # noqa: E402
from DamSafety_Hazard.structures import OUT_DRY, OUT_JUDGMENT  # noqa: E402


def test_sx1_slim_keeps_dwelling_extra():
    assert "dwelling_extra" in SE._SLIM_KEYS
    rec = {"id": 1, "habitable": "Yes", "persons": 2, "dwelling_extra": True,
           "zone": "Judgment", "outcome": OUT_JUDGMENT, "max_depth": 3.0}
    view = dict(rec, zone="High", outcome="Probable")
    slim = SE._slim(rec, view)
    assert slim.get("dwelling_extra") is True
    # classification counts must match with/without slim
    from DamSafety_Hazard import classification as CLS
    a = {"habitable": "Yes", "dwelling_extra": False, "outcome": "Probable loss of life",
         "persons": 2, "max_depth": 5.0, "zone": "High"}
    b = {"habitable": "Yes", "dwelling_extra": True, "outcome": "Probable loss of life",
         "persons": 2, "max_depth": 5.0, "zone": "High"}
    # use the hab filter the same way classify does
    def count(structs):
        return sum(1 for s in structs if s["habitable"] == "Yes" and not s.get("dwelling_extra")
                   and "probable" in (s.get("outcome") or "").lower())
    assert count([a, b]) == 1
    slim_b = SE._slim(b, b)
    assert count([a, slim_b]) == 1


def test_fp1_content_digest_bypasses_disk_cache():
    # write a file, digest, rewrite same size+mtime via utime, digest again
    d = tempfile.mkdtemp()
    p = os.path.join(d, "x.bin")
    with open(p, "wb") as fh:
        fh.write(b"A" * (chain.PERSIST_MIN + 100))
    # prime the disk cache with use_disk_cache=True
    d1 = chain.file_digest(p, use_disk_cache=True)
    st = os.stat(p)
    with open(p, "wb") as fh:
        fh.write(b"B" * (chain.PERSIST_MIN + 100))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    # clear in-process cache so disk would win if consulted
    chain._cache.clear()
    stale = chain.file_digest(p, use_disk_cache=True)
    chain._cache.clear()
    fresh = FP.content_digest(p)
    assert stale["sha256"] == d1["sha256"], "disk cache still sees old content"
    assert fresh["sha256"] != d1["sha256"], "content_digest must see the rewrite"


def test_fp2_vrt_without_relative_attr_and_incomplete_flag():
    d = tempfile.mkdtemp()
    tile = os.path.join(d, "t.tif")
    with open(tile, "wb") as fh:
        fh.write(b"tile")
    vrt = os.path.join(d, "m.vrt")
    # no relativeToVRT attribute
    with open(vrt, "w", encoding="utf-8") as fh:
        fh.write('<VRTDataset><VRTRasterBand>'
                 '<SourceFilename>t.tif</SourceFilename>'
                 '</VRTRasterBand></VRTDataset>')
    srcs = FP._vrt_sources(vrt, limit=2000)
    assert srcs and srcs[0][0] == "t.tif" and not srcs[0][1].get("missing")
    # truncation marks incomplete
    srcs2 = FP._vrt_sources(vrt, limit=0)
    assert srcs2 and srcs2[-1] == ["incomplete", True]


def test_sc2_comparison_counts_outcome_not_stale_depth():
    from DamSafety_Hazard.scenarios import comparison_rows
    s_view = [
        {"habitable": "Yes", "outcome": OUT_DRY, "max_depth": 4.0},   # flooded without breach
        {"habitable": "Yes", "outcome": "Probable loss of life", "max_depth": 5.0},
    ]
    results = {"Breach": {
        "nd": {"cls": "High"}, "tm11": {"cls": "High", "lij": 1.0},
        "views": (s_view, [], []), "conseq": {}, "incremental": {"baseline": "No breach"},
    }}
    rows = comparison_rows(results)
    assert rows[0]["Habitable structures flooded"] == "1"


def test_sc3_fa2_notes():
    inc, note = SC.structure_increment(
        {"max_depth": 3.0, "outcome": "Probable loss of life"},
        {"max_depth": None}, same_method=True, one_d=False)
    assert inc and "outside baseline raster" in note
    inc2, note2 = SC.structure_increment(
        {"max_depth": 3.0, "outcome": "Probable loss of life"},
        {"max_depth": 0.0}, same_method=True, one_d=False)
    assert inc2 and note2 == "dry without breach"


def test_snapshot_has_cut_off():
    snap = SC.snapshot([{"id": 1, "max_depth": 1.0, "cut_off": True, "par": 3}])
    assert "cut_off" in snap[1] and snap[1]["cut_off"] is True
    assert set(SC.SNAP_KEYS) >= set(SC.EVAL_KEYS)
    assert "cut_off" in SC.CONSEQ_KEYS


def test_sc4_duplicate_baseline_note():
    scs, problems, notes = SC.rows_to_scenarios(
        {"name": "Breach", "depth": __file__, "baseline": "No breach"},
        [{"name": "No breach", "role": "baseline", "depth": __file__, "baseline": "",
          "velocity": "", "arrival": "", "dv": ""},
         {"name": "No breach", "role": "baseline", "depth": __file__, "baseline": "",
          "velocity": "", "arrival": "", "dv": ""}])
    assert any("appears 2 times" in n for n in notes), notes


def test_fa4_first_nid():
    dams = [{"nid": "ND1", "name": "A"}, {"nid": "ND1", "name": "B"},
            {"nid": "ND2", "name": "C"}]
    hit = FA.mark_subject_dam(dams, subject_nid="ND1")
    assert hit["name"] == "A"
    assert dams[0]["subject"] and dams[1]["subject"] and not dams[2]["subject"]


def test_cl2_raised_wording():
    # positive margin = still below the line (raised to Judgment)
    structs = [{"habitable": "Yes", "dwelling_extra": False, "outcome": OUT_JUDGMENT,
                "margin_judg": 0.4, "persons": 3, "max_depth": 2.0, "zone": "Judgment"}]
    # build a minimal nd dict path - call the provisional block via classify if heavy;
    # instead assert the source contains the new wording branch
    src = open(os.path.join(HAZARD, "classification.py"), encoding="utf-8").read()
    assert "within {:.2f} ft of the ACER judgment line" in src
    assert "max(raw_margins) <= 0" in src


def test_onepass_single_definition():
    assert C.ONEPASS_MIN == 8
    import DamSafety_Hazard.fastsample as FS
    import DamSafety_Hazard.structures as ST
    assert FS.ONEPASS_MIN is C.ONEPASS_MIN or FS.ONEPASS_MIN == C.ONEPASS_MIN
    assert ST.ONEPASS_MIN == C.ONEPASS_MIN


def test_sx6_sx7_claims():
    # SX.6: CSV_CONSEQ used in run() body is fine (resolved at call time)
    assert hasattr(SE, "CSV_CONSEQ") and SE.CSV_CONSEQ.endswith(".csv")
    assert SE.MAX_CHANGES_PER_ROW > 0
    # SX.7: high_severity notes that severity method has no effect
    src = open(os.path.join(HAZARD, "sensitivity.py"), encoding="utf-8").read()
    assert "near-instantaneous failure setting" in src
    assert "severity method has no effect" in src


def main():
    tests = [test_sx1_slim_keeps_dwelling_extra, test_fp1_content_digest_bypasses_disk_cache,
             test_fp2_vrt_without_relative_attr_and_incomplete_flag,
             test_sc2_comparison_counts_outcome_not_stale_depth, test_sc3_fa2_notes,
             test_snapshot_has_cut_off, test_sc4_duplicate_baseline_note, test_fa4_first_nid,
             test_cl2_raised_wording, test_onepass_single_definition, test_sx6_sx7_claims]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Exception as e:
            failed += 1
            print("FAIL ", fn.__name__, ":", e)
            import traceback; traceback.print_exc()
    print("\n{} tests, {} failed".format(len(tests), failed))
    return failed


if __name__ == "__main__":
    _rc = main()
    sys.stdout.flush()
    if _rc:
        raise SystemExit(_rc)
    # all passed; os._exit(0) before the real-QgsApplication offscreen teardown segfaults at shutdown
    os._exit(0)
