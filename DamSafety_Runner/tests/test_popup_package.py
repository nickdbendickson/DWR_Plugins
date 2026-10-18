"""3.10.15: run_detail, the DamSafety Hazard submittal-package row, its staleness and the
reviewed-package check (pure logic, temp folders)."""
import hashlib
import json
import os
import shutil
import tempfile
import time
import zipfile

from nd_breach_runner.tests.registry import test


def _write(path, text="", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


@test
def popup_run_detail_from_run_report():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow as wf
    d = tempfile.mkdtemp()
    try:
        rep = os.path.join(d, wf.MODEL_FOLDER, "run_report.json")
        assert pl.run_detail(d) == "" and pl.run_detail("") == ""
        _write(rep, "not json")
        assert pl.run_detail(d) == ""
        _write(rep, json.dumps({"summary": {"final_model": "LISFLOOD-FP", "flooded_acres": 1234.5}}))
        assert pl.run_detail(d) == "final LISFLOOD-FP; 1,234.5 ac flooded", pl.run_detail(d)
        _write(rep, json.dumps({"summary": {"final_model": "ANUGA", "flooded_acres": 10},
                                "cut_off": {"reason": "duration cap", "detail": "x"}}))
        assert pl.run_detail(d) == "final ANUGA; 10.0 ac flooded; RESULTS CUT OFF: duration cap"
        _write(rep, json.dumps({"summary": {"final_model": "none", "breach_occurs": False,
                                            "flooded_acres": 0}}))
        assert pl.run_detail(d) == "no breach"
        # the run row carries it
        for k in ("depth", "velocity", "arrival"):
            _write(os.path.join(d, wf.FINAL_NAMES[k]))
        run = {k: dd for k, _l, _v, dd in pl.dam_status(d)}["run"]
        assert run == "no breach", run
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def popup_submittal_package_row_stale_and_unreviewed():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow as wf
    from nd_breach_runner.damsuite import dam_record as DR
    d = tempfile.mkdtemp()
    try:
        row = lambda: {k: (v, dd) for k, _l, v, dd in pl.dam_status(d)}["package"]   # noqa: E731
        assert row() == ("no", "") and pl.package_warnings(d) == [] and not pl.package_unreviewed(d)
        now = time.time()
        sub = os.path.join(d, "outputs", "Submittal")
        m1 = _write(os.path.join(sub, "Dam_2026-10-01", "MANIFEST.csv"), "a,b\n1,2\n", mtime=now - 500)
        _write(os.path.join(sub, "Dam_2026-10-01", "README.txt"))
        _write(os.path.join(sub, "not_a_package", "x.txt"))
        assert row()[0] == "yes" and row()[1].startswith("Dam_2026-10-01 ("), row()
        # a classification run after the package -> part (stale), names the newer file
        _write(os.path.join(d, "outputs", "QA", "Last_Run_Snapshot.json"), "{}", mtime=now - 100)
        assert row()[0] == "part" and "outputs/QA/Last_Run_Snapshot.json" in row()[1], row()
        _write(os.path.join(d, wf.MODEL_FOLDER, "run_report.json"), "{}", mtime=now - 50)
        assert "_breach_model/run_report.json" in row()[1]
        w = pl.package_warnings(d)
        assert len(w) == 1 and "rebuild it in DamSafety Hazard" in w[0], w
        assert pl.stage_states(pl.dam_status(d), True, False, "", "D")["products"] == "part"
        # a rebuilt package (Dam_<date>_2) is newer: yes again, and it is the newest
        m2 = _write(os.path.join(sub, "Dam_2026-10-01_2", "MANIFEST.csv"), "a,b\n3,4\n", mtime=now)
        assert row()[0] == "yes" and row()[1].startswith("Dam_2026-10-01_2"), row()
        sha = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()           # noqa: E731
        assert pl.submittal_packages(d)[0][2] == sha(m2)
        # review of the OLD package recorded in dam.json -> unreviewed
        rec = DR.load(d, create=True)
        rec.add_status("reviewed", "DamSafety Hazard", "2.18", {"decision": "accepted",
                                                               "package_sha256": sha(m1)})
        assert pl.package_unreviewed(d, rec) is True
        rec.save()
        if True:                                    # read back from dam.json
            assert pl.package_unreviewed(d) is True
            assert any("has not been reviewed" in x for x in pl.package_warnings(d))
        # review of the newest package -> fine; no sha recorded -> not flagged
        rec.add_status("reviewed", "DamSafety Hazard", "2.18", {"package_sha256": sha(m2).upper()},
                       when="2099-01-01T00:00:00")
        assert pl.package_unreviewed(d, rec) is False
        rec.add_status("reviewed", "DamSafety Hazard", "2.18", {"decision": "accepted"},
                       when="2099-02-01T00:00:00")
        assert pl.package_unreviewed(d, rec) is False
        # a zip whose folder is gone counts by the manifest inside it
        e = tempfile.mkdtemp()
        z = os.path.join(e, "outputs", "Submittal", "Dam_2026-10-02.zip")
        os.makedirs(os.path.dirname(z))
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("Dam_2026-10-02/MANIFEST.csv", "z\n")
        pk = pl.submittal_packages(e)
        assert len(pk) == 1 and pk[0][0] == "Dam_2026-10-02.zip" and \
            pk[0][2] == hashlib.sha256(b"z\n").hexdigest(), pk
        shutil.rmtree(e, ignore_errors=True)
    finally:
        shutil.rmtree(d, ignore_errors=True)
