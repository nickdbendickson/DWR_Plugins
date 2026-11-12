"""3.13.6: the class of record in review.json (core/review_record.py; Nick, 2026-11-01).
Synthetic folders only."""
import json
import os
import re
import shutil
import tempfile

from nd_breach_runner.core import results_info as RI
from nd_breach_runner.core import review_record as RR
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_results_dock_hazard import _dam

GOOD = {"schema": "damsafety-review-v1", "nd_class_of_record": "medium",
        "tm11_class_of_record": "Significant", "decided_by": "N. Bendickson",
        "decided_on": "2026-11-01", "note": "judgment-zone dwelling is 0.16 ft past the line",
        "overrides": {"nd_class": "High", "tm11_class": "Significant", "hazard_version": "2.19.1"},
        "applies_to": {"run_folder": "_breach_model", "run_id": "2026-10-29T14:58:28"}}
REPORT = {"run_started": "2026-10-29T14:58:28"}


def _write(d, data):
    if isinstance(data, bytes):
        with open(os.path.join(d, RR.REVIEW_FILE), "wb") as f:
            f.write(data)
        return
    with open(os.path.join(d, RR.REVIEW_FILE), "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


@test
def review_json_valid_is_read_and_shown_beside_the_printed_class():
    d = _dam()
    try:
        _write(d, GOOD)
        hz = RI.hazard_summary(d)
        cor = RI.class_of_record(d, os.path.join(d, "_breach_model"), hz, REPORT)
        assert cor["state"] == "ok" and cor["warnings"] == [], cor
        assert cor["review"]["nd_class_of_record"] == "Medium", cor
        line = RI.class_line(hz, RI.record_status(d), cor)
        assert "ND High (provisional)" in line, line
        assert "class of record ND Medium, TM 11 Significant (N. Bendickson, 2026-11-01)" \
            " - differs from printed" in line, line
    finally:
        shutil.rmtree(d)


@test
def review_json_missing_or_invalid_is_not_recorded():
    d = _dam()
    try:
        bad = [("{not json", "unreadable"), ("[1, 2]", "not a JSON object"),
               (dict(GOOD, nd_class_of_record="Severe"), "nd_class_of_record"),
               (dict(GOOD, tm11_class_of_record=None), "tm11_class_of_record"),
               (dict(GOOD, decided_by="  "), "decided_by"),
               (dict(GOOD, decided_on="Nov 1"), "decided_on"),
               (dict(GOOD, schema="v0"), "schema"),
               (dict(GOOD, note=5), "note"),
               (dict(GOOD, overrides={"nd_class": "High"}), "overrides.hazard_version"),
               (dict(GOOD, applies_to={}), "applies_to"),
               (b"\xff\xfe{", "unreadable")]
        for data, word in bad:
            _write(d, data)
            cor = RI.class_of_record(d, os.path.join(d, "_breach_model"), {}, REPORT)
            assert cor["state"] == "invalid" and cor["review"] is None, (data, cor)
            assert any(word in p for p in cor["problems"]), (word, cor["problems"])
            line = RI.class_line({"nd_class": "High"}, {}, cor)
            assert "class of record: not recorded (review.json invalid)" in line, line
        os.remove(os.path.join(d, RR.REVIEW_FILE))
        assert RR.read_review(d)["state"] == "missing"
        assert RR.read_review(os.path.join(d, "no_such_dir"))["state"] == "missing"
    finally:
        shutil.rmtree(d)


@test
def review_json_flags_another_run_or_a_changed_printed_class():
    d = _dam()
    try:
        _write(d, dict(GOOD, applies_to={"run_folder": "_breach_model_r3",
                                         "run_id": "2026-10-01T00:00:00"}))
        cor = RI.class_of_record(d, os.path.join(d, "_breach_model"), RI.hazard_summary(d), REPORT)
        w = " | ".join(cor["warnings"])
        assert "run folder _breach_model_r3" in w and "run 2026-10-01T00:00:00" in w, w
        _write(d, dict(GOOD, overrides={"nd_class": "Medium", "hazard_version": "2.18.0"}))
        cor = RI.class_of_record(d, os.path.join(d, "_breach_model"), RI.hazard_summary(d), REPORT)
        w = " | ".join(cor["warnings"])
        assert "printed ND class Medium" in w and "now says High" in w, w
        assert "Hazard version 2.18.0" in w, w
        # an absolute run folder that is the current one matches
        _write(d, dict(GOOD, applies_to={"run_folder": os.path.join(d, "_breach_model")}))
        cor = RI.class_of_record(d, os.path.join(d, "_breach_model"), RI.hazard_summary(d), REPORT)
        assert cor["warnings"] == [], cor
    finally:
        shutil.rmtree(d)


@test
def review_sign_off_validates_and_never_replaces_silently():
    d = tempfile.mkdtemp(prefix="review_")
    try:
        args = dict(nd_class="Medium", tm11_class="Significant", decided_by="N. B.",
                    overrides={"nd_class": "High", "hazard_version": "2.19.1"},
                    applies_to={"run_id": "2026-10-29T14:58:28"}, decided_on="2026-11-01")
        try:
            RR.sign_off_review(d, **dict(args, nd_class="Huge"))
            raise AssertionError("invalid class accepted")
        except ValueError:
            pass
        assert not os.path.exists(os.path.join(d, RR.REVIEW_FILE))
        RR.sign_off_review(d, **args)
        assert RR.read_review(d)["state"] == "ok"
        try:
            RR.sign_off_review(d, **dict(args, nd_class="Low"))
            raise AssertionError("replaced without replace=True")
        except FileExistsError:
            pass
        assert RR.read_review(d)["review"]["nd_class_of_record"] == "Medium"
        RR.sign_off_review(d, replace=True, **dict(args, nd_class="Low"))
        assert RR.read_review(d)["review"]["nd_class_of_record"] == "Low"
    finally:
        shutil.rmtree(d)


@test
def review_json_survives_record_writes_and_the_dock_read():
    """What a run / rerun / classify does to the dam folder's record (dam_record set, add_status,
    save) and the dock's read leave an existing review.json byte-for-byte untouched."""
    from nd_breach_runner.damsuite import dam_record as DR
    d = _dam()
    try:
        _write(d, GOOD)
        p = os.path.join(d, RR.REVIEW_FILE)
        before = (open(p, "rb").read(), os.path.getmtime(p))
        rec = DR.load(d)
        rec.add_status("modelled", "ndbreach", "3.13.6", {"breach_occurs": True})
        rec.add_status("classified", "ndhazard", "2.22.2",
                       {"nd_class": "Low", "tm11_class": "Low", "output_folder": "outputs"})
        rec.save()
        RI.hazard_summary(d)
        RI.record_status(d)
        RI.class_of_record(d, os.path.join(d, "_breach_model"), {}, REPORT)
        assert (open(p, "rb").read(), os.path.getmtime(p)) == before
    finally:
        shutil.rmtree(d)


@test
def review_writer_is_referenced_by_no_run_or_classify_code():
    """Only core/review_record.py (and tests) may name sign_off_review; review.json is named
    only by the readers (results_info.py, the dock's "review.json ignored" text)."""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    plugins = [n for n in os.listdir(root) if n.startswith("DamSafety_")]    # Core 1.0.0: shared/ is gone
    bad = []
    for name in plugins:
        for dirpath, dirnames, files in os.walk(os.path.join(root, name)):
            dirnames[:] = [x for x in dirnames if x not in ("tests", "__pycache__")]
            for f in files:
                if not f.endswith(".py") or f == "review_record.py":
                    continue
                path = os.path.join(dirpath, f)
                text = open(path, encoding="utf-8", errors="replace").read()
                # review.json as a whole name (not habitable_review.json, which classify
                # may read; only ndhazard:reviewhabitable writes that file).
                if "sign_off_review" in text or (re.search(r"(?<![\w.-])review\.json", text) and
                                                 f not in ("results_info.py", "results_dock.py")):
                    bad.append(os.path.relpath(path, root))
    assert not bad, bad
    # results_info only reads it
    ri = open(os.path.join(root, "DamSafety_Runner", "core", "results_info.py")).read()
    assert "sign_off_review" not in ri and "open(" not in ri.split("def class_of_record")[1] \
        .split("\ndef ")[0]
