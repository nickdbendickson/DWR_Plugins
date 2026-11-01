"""3.13.6: the engineer's class of record - review.json in the dam folder (Nick, 2026-11-01).

review.json is written ONLY by sign_off_review(), an explicit engineer sign-off. No run,
rerun, classify or dock code calls it (tests/test_review_record.py checks every plugin's code
for references). Everything else only reads it, defensively: a missing, unreadable or invalid
file means "class of record: not recorded".

Schema (SCHEMA = "damsafety-review-v1"):

    {
      "schema": "damsafety-review-v1",
      "nd_class_of_record": "Low" | "Medium" | "High",          required
      "tm11_class_of_record": "Low" | "Significant" | "High",   required
      "decided_by": "<engineer name>",                          required, non-empty
      "decided_on": "YYYY-MM-DD",                               required, a real date
      "note": "<why>",                                          optional string
      "overrides": {                                            required
        "nd_class": "<printed ND class>",                       required
        "tm11_class": "<printed TM 11 class>",                  optional
        "hazard_version": "<Hazard version that printed it>"    required
      },
      "applies_to": {                                           required, at least one key
        "run_folder": "<run folder, relative to the dam folder, e.g. _breach_model>",
        "run_id": "<run_report.json run_started of that run>"
      }
    }
"""
import datetime
import json
import os

REVIEW_FILE = "review.json"
SCHEMA = "damsafety-review-v1"
ND_CLASSES = ("Low", "Medium", "High")
TM11_CLASSES = ("Low", "Significant", "High")
MAX_BYTES = 256 * 1024


def _canon(value, allowed):
    for a in allowed:
        if isinstance(value, str) and value.strip().lower() == a.lower():
            return a
    return None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def validate(data):
    """(clean dict, []) when valid, else (None, [problems])."""
    if not isinstance(data, dict):
        return None, ["review.json is not a JSON object"]
    probs = []
    if data.get("schema") != SCHEMA:
        probs.append("schema is {!r}, expected {!r}".format(data.get("schema"), SCHEMA))
    nd = _canon(data.get("nd_class_of_record"), ND_CLASSES)
    if not nd:
        probs.append("nd_class_of_record must be one of " + "/".join(ND_CLASSES))
    tm = _canon(data.get("tm11_class_of_record"), TM11_CLASSES)
    if not tm:
        probs.append("tm11_class_of_record must be one of " + "/".join(TM11_CLASSES))
    by = _text(data.get("decided_by"))
    if not by:
        probs.append("decided_by is missing")
    on = data.get("decided_on")
    try:
        on = datetime.date.fromisoformat(on).isoformat() if isinstance(on, str) else None
    except ValueError:
        on = None
    if not on:
        probs.append("decided_on must be a date YYYY-MM-DD")
    note = data.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str):
        probs.append("note must be text")
    ov = data.get("overrides")
    ov_clean = {}
    if not isinstance(ov, dict):
        probs.append("overrides is missing")
    else:
        for k in ("nd_class", "hazard_version"):
            if not _text(ov.get(k)):
                probs.append("overrides.{} is missing".format(k))
            else:
                ov_clean[k] = _text(ov[k])
        if ov.get("tm11_class") is not None:
            if not _text(ov.get("tm11_class")):
                probs.append("overrides.tm11_class must be text")
            else:
                ov_clean["tm11_class"] = _text(ov["tm11_class"])
    ap = data.get("applies_to")
    ap_clean = {}
    if not isinstance(ap, dict):
        probs.append("applies_to is missing")
    else:
        for k in ("run_folder", "run_id"):
            if ap.get(k) is not None:
                if _text(ap.get(k)):
                    ap_clean[k] = _text(ap[k])
                else:
                    probs.append("applies_to.{} must be text".format(k))
        if not ap_clean:
            probs.append("applies_to must name run_folder or run_id")
    if probs:
        return None, probs
    return {"schema": SCHEMA, "nd_class_of_record": nd, "tm11_class_of_record": tm,
            "decided_by": by, "decided_on": on, "note": note.strip(),
            "overrides": ov_clean, "applies_to": ap_clean}, []


def read_review(dam_dir):
    """{"state": "missing"|"invalid"|"ok", "review": dict|None, "problems": [...]}.
    Never raises."""
    path = os.path.join(dam_dir or "", REVIEW_FILE)
    out = {"state": "missing", "review": None, "problems": [], "path": path}
    try:
        if not os.path.isfile(path):
            return out
        if os.path.getsize(path) > MAX_BYTES:
            out.update(state="invalid", problems=["review.json is too large"])
            return out
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        out.update(state="invalid", problems=["review.json unreadable: {}".format(e)])
        return out
    except Exception as e:                       # never break the dock
        out.update(state="invalid", problems=["review.json unreadable: {}".format(e)])
        return out
    clean, probs = validate(data)
    if clean is None:
        out.update(state="invalid", problems=probs)
    else:
        out.update(state="ok", review=clean)
    return out


def _same(a, b):
    return str(a).strip().lower() == str(b).strip().lower()


def _rel_folder(dam_dir, folder):
    if not folder:
        return None
    p = folder if os.path.isabs(folder) else os.path.join(dam_dir, folder)
    try:
        return os.path.normcase(os.path.relpath(os.path.normpath(p), os.path.normpath(dam_dir)))
    except ValueError:
        return os.path.normcase(os.path.normpath(p))


def mismatches(review, dam_dir, model_dir=None, run_id=None, summary=None):
    """Warning texts when the review does not fit the current run or the current summary."""
    if not review:
        return []
    warns = []
    ap = review.get("applies_to") or {}
    if ap.get("run_folder") and model_dir:
        if _rel_folder(dam_dir, ap["run_folder"]) != _rel_folder(dam_dir, model_dir):
            warns.append("class of record applies to run folder {}, not this run ({})".format(
                ap["run_folder"], _rel_folder(dam_dir, model_dir)))
    if ap.get("run_id") and run_id and not _same(ap["run_id"], run_id):
        warns.append("class of record applies to run {}, not this run ({})".format(
            ap["run_id"], run_id))
    ov = review.get("overrides") or {}
    s = summary or {}
    for key, label in (("nd_class", "ND class"), ("tm11_class", "TM 11 class"),
                       ("hazard_version", "Hazard version")):
        if ov.get(key) and s.get(key if key != "hazard_version" else "hazard_version") and \
                not _same(ov[key], s[key]):
            warns.append("class of record overrides printed {} {} (Hazard {}), but the summary "
                         "now says {}".format(label, ov[key], ov.get("hazard_version"), s[key]))
    return warns


def sign_off_review(dam_dir, nd_class, tm11_class, decided_by, overrides, applies_to,
                    note="", decided_on=None, replace=False):
    """ENGINEER SIGN-OFF ONLY. Writes review.json; never call from run/classify code.
    Refuses to replace an existing review.json unless replace=True. Returns the path."""
    data = {"schema": SCHEMA, "nd_class_of_record": nd_class,
            "tm11_class_of_record": tm11_class, "decided_by": decided_by,
            "decided_on": decided_on or datetime.date.today().isoformat(), "note": note or "",
            "overrides": dict(overrides or {}), "applies_to": dict(applies_to or {})}
    clean, probs = validate(data)
    if clean is None:
        raise ValueError("review.json not written: " + "; ".join(probs))
    path = os.path.join(dam_dir, REVIEW_FILE)
    if os.path.exists(path) and not replace:
        raise FileExistsError("review.json already exists; pass replace=True to sign off again")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path
