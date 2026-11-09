"""H06 residual: a truncated run report is not a finished run.

run_finished() used to return True on any parse error once the three final rasters
existed, so a kill during json.dump (the report was written in place) still made the
batch skip the dam. A readable report with a truthy run_finished stamp is finished;
a truncated file, a report with no stamp, or rasters alone are not.
"""
import json
import os
import tempfile

from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test


def _rasters(folder):
    for key in ("depth", "velocity", "arrival"):
        with open(os.path.join(folder, workflow.FINAL_NAMES[key]), "w", encoding="utf-8") as fh:
            fh.write("x")


@test
def run_finished_rejects_a_truncated_report():
    folder = tempfile.mkdtemp()
    _rasters(folder)
    assert workflow.run_finished(folder) is False          # rasters, no report
    model = os.path.join(folder, workflow.MODEL_FOLDER)
    os.makedirs(model)
    rep = os.path.join(model, "run_report.json")
    with open(rep, "w", encoding="utf-8") as fh:
        fh.write("{")                                      # killed mid-dump
    assert workflow.run_finished(folder) is False
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"summary": "no stamp"}, fh)
    assert workflow.run_finished(folder) is False
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"run_finished": ""}, fh)                # present but empty
    assert workflow.run_finished(folder) is False
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"run_finished": "2026-11-08T00:00:00"}, fh)
    assert workflow.run_finished(folder) is True


@test
def write_json_replaces_the_report_only_when_complete():
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "run_report.json")
    workflow._write_json(path, {"run_finished": "2026-11-08T00:00:00"})
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["run_finished"] == "2026-11-08T00:00:00"
    assert not os.path.exists(path + ".tmp")
    workflow._write_json(path, {"run_finished": "2026-11-08T01:00:00", "n": 1})
    with open(path, encoding="utf-8") as fh:
        got = json.load(fh)
    assert got["run_finished"] == "2026-11-08T01:00:00" and got["n"] == 1
    assert not os.path.exists(path + ".tmp")


@test
def write_text_is_atomic_and_leaves_no_tmp():
    """3.16.16 (L06): _write_text replaces run_report.md only after the whole file is on disk, so
    a failed write never leaves a truncated or zero-length report, nor a stray .tmp."""
    folder = tempfile.mkdtemp()
    path = os.path.join(folder, "run_report.md")
    workflow._write_text(path, "GOOD ORIGINAL")
    assert open(path, encoding="utf-8").read() == "GOOD ORIGINAL"
    assert not os.path.exists(path + ".tmp")
    real_replace = os.replace
    os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("ENOSPC"))
    try:
        raised = False
        try:
            workflow._write_text(path, "PARTIAL / FAILS")
        except OSError:
            raised = True
        assert raised, "the write error must propagate out of _write_text"
        assert open(path, encoding="utf-8").read() == "GOOD ORIGINAL", "original preserved"
        assert not os.path.exists(path + ".tmp"), "the half .tmp is removed on failure"
    finally:
        os.replace = real_replace


@test
def amend_report_json_is_written_even_when_the_markdown_render_fails():
    """3.16.17 (L06): _amend_report writes the machine-readable run_report.json FIRST and on its
    own, so a render_report failure (it indexes rep["summary"]/["hydrograph"] directly) can never
    block the grid_refine record - the pre-L06 behaviour, which test_crest_burn relies on. The
    markdown is rendered before its file is opened (no zero-length run_report.md) and a render
    failure leaves the prior markdown intact and is reported, not swallowed."""
    folder = tempfile.mkdtemp()
    model = os.path.join(folder, workflow.MODEL_FOLDER)
    os.makedirs(model)
    rep_json = os.path.join(model, "run_report.json")
    rep_md = os.path.join(model, "run_report.md")
    with open(rep_json, "w", encoding="utf-8") as fh:
        json.dump({"summary": {"peak_breach_cfs": 1000.0}, "warnings": []}, fh)
    workflow._write_text(rep_md, "PRIOR MARKDOWN")

    res = workflow.RunResult()
    res.report_json = rep_json
    res.report_md = rep_md
    res.warnings = []
    res.summary = {}
    entry = {"coarse_cell_m": 10.0, "record_cell_m": 5.0}

    real_render = workflow.render_report
    workflow.render_report = lambda rep: (_ for _ in ()).throw(KeyError("hydrograph"))
    try:
        workflow._amend_report(res, entry, ["re-run at 5 m (record)"])
    finally:
        workflow.render_report = real_render

    # the JSON IS written (grid_refine persisted) despite the render failure; the prior markdown
    # is not truncated; the markdown failure is surfaced, not swallowed; no stray .tmp.
    assert json.load(open(rep_json, encoding="utf-8")).get("grid_refine") == entry, "json written"
    assert open(rep_md, encoding="utf-8").read() == "PRIOR MARKDOWN", "report_md not truncated"
    assert any("markdown not updated" in w for w in res.warnings), res.warnings
    assert not os.path.exists(rep_md + ".tmp") and not os.path.exists(rep_json + ".tmp")

    # a normal amend writes both files atomically, no warning, no .tmp
    workflow.render_report = lambda rep: "# ok\n"
    try:
        res.warnings = []
        workflow._amend_report(res, dict(entry, coarse_cell_m=20.0), ["note"])
    finally:
        workflow.render_report = real_render
    assert open(rep_md, encoding="utf-8").read() == "# ok\n"
    assert json.load(open(rep_json, encoding="utf-8"))["grid_refine"]["coarse_cell_m"] == 20.0
    assert not os.path.exists(rep_md + ".tmp") and not os.path.exists(rep_json + ".tmp")
    assert not any("not updated" in w for w in res.warnings), res.warnings
