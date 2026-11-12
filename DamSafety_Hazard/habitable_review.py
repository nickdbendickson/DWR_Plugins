# -*- coding: utf-8 -*-
"""The engineer's habitability review of structures: habitable_review.json in the dam folder.

Only an ND 911 address point is habitable by itself. Any other structure (NSI, building
outlines, ...) is "unverified" until an engineer reviews it in Hazard. This file is written
ONLY by sign_off() / the ndhazard:reviewhabitable algorithm, an explicit engineer action;
classify and the depth-velocity chart never write it (a test checks this).

Aligned with DamIT Hazard's habitable_review.json (damit PR #66). The per-structure field
`reviewed_habitable` uses DamSafety Hazard's words: yes / unverified / no. The DamIT file
stores a boolean (true / false); both are accepted when reading. The existing Review tab
file Structure_Review_Decisions.csv is still applied separately (dvchart.reviewed_habitable
reads both); this file is the dedicated habitability sign-off.

    {"schema": "dwr-habitable-review-v1",
     "reviews": {"<structure key>": {"reviewed_habitable": "yes", "decided_by": "A. Engineer",
                                     "decided_on": "2026-11-12", "note": "occupied farmhouse"}},
     "history": [{"key": ..., "reviewed_habitable": "yes", "decided_by": ..., "decided_on": ...,
                  "note": ...}]}

The structure key is "field:value" of a stable attribute (NSI fd_id, nguid, Struct_ID), else
the structure id of that run. Pure Python and the standard library.
"""
import datetime
import json
import csv
import os

REVIEW_FILE = "habitable_review.json"
SCHEMA = "dwr-habitable-review-v1"
SCHEMA_DAMIT = "damit-habitable-review-v1"
ACCEPTED_SCHEMAS = (SCHEMA, SCHEMA_DAMIT)
MAX_BYTES = 32 * 1024 * 1024
LEVELS_UP = 2
YES, UNVERIFIED, NO = "yes", "unverified", "no"
# values the engineer can save (unverified is "remove the review")
LEVELS = (YES, NO)


def _text(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def normalize_level(v):
    """yes / no / unverified, or None when unset. Accepts DamIT booleans and common words."""
    if v is None:
        return None
    if isinstance(v, bool):
        return YES if v else NO
    if isinstance(v, (int, float)) and v in (0, 1):
        return YES if v else NO
    k = str(v).strip().lower()
    if k in ("yes", "y", "true", "t", "1", "habitable"):
        return YES
    if k in ("no", "n", "false", "f", "0", "not habitable", "not_habitable"):
        return NO
    if k in ("unverified", "remove", "none", "unset", ""):
        return UNVERIFIED if k else None
    return None


def load(path):
    """({key: entry}, [problems]). A missing file is ({}, []); an unreadable or invalid one
    is ({}, [why]): classify then carries on with no reviews and says why."""
    if not path or not os.path.isfile(path):
        return {}, []
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return {}, ["{} is too large".format(os.path.basename(path))]
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return {}, ["{} unreadable: {}".format(os.path.basename(path), e)]
    if not isinstance(data, dict) or data.get("schema") not in ACCEPTED_SCHEMAS \
            or not isinstance(data.get("reviews"), dict):
        return {}, ["{} is not a {} file".format(os.path.basename(path),
                                                 " or ".join(ACCEPTED_SCHEMAS))]
    out, probs = {}, []
    for k, e in data["reviews"].items():
        if not isinstance(e, dict):
            probs.append("review of {} is not an object: ignored".format(k))
            continue
        level = normalize_level(e.get("reviewed_habitable"))
        if level not in (YES, NO):
            probs.append("review of {} has no yes / no reviewed_habitable: ignored".format(k))
            continue
        entry = dict(e)
        entry["reviewed_habitable"] = level
        out[str(k)] = entry
    return out, probs


def find(paths):
    """The habitable_review.json in the folder of the first path (a raster or dam folder)
    or up to LEVELS_UP folders above it, else None."""
    for p in paths or []:
        raw = os.path.abspath(str(p).split("|")[0])
        d = raw if os.path.isdir(raw) else os.path.dirname(raw)
        for _ in range(LEVELS_UP + 1):
            f = os.path.join(d, REVIEW_FILE)
            if os.path.isfile(f):
                return f
            nd = os.path.dirname(d)
            if nd == d:
                break
            d = nd
    return None


def review_keys(rec):
    """Possible keys a saved review of this structure may use (first is the preferred struct_key)."""
    keys = []

    def add(v, prefix=None):
        s = _text(v) if not isinstance(v, (int, float)) else str(v)
        if not s:
            return
        if prefix:
            keys.append("{}:{}".format(prefix, s))
        keys.append(s)

    add(rec.get("struct_key"))
    add(rec.get("id", rec.get("Struct_ID")), "Struct_ID")
    add(rec.get("fd_id") or rec.get("NSI_fd_id"), "fd_id")
    nguid = rec.get("nguid") or rec.get("ND_Site_NGUID") or rec.get("ng911_guid")
    if not nguid:
        for s in rec.get("sources") or ():
            if isinstance(s, dict) and s.get("nguid"):
                nguid = s["nguid"]
                break
    add(nguid, "nguid")
    # unique, keep order
    seen, out = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def preferred_key(rec):
    ks = review_keys(rec)
    return ks[0] if ks else ""


def apply(structs, reviews):
    """Set reviewed_habitable (yes / no) and review_by / review_on / review_note on structures
    that have a review in the file; returns how many. Others keep reviewed_habitable unset
    (unverified unless a 911 point or an existing Review-tab decision)."""
    if not reviews:
        return 0
    n = 0
    for rec in structs or []:
        entry = None
        for k in review_keys(rec):
            if k in reviews:
                entry = reviews[k]
                break
        if entry is None:
            continue
        rec["reviewed_habitable"] = entry["reviewed_habitable"]
        rec["review_by"] = entry.get("decided_by") or ""
        rec["review_on"] = entry.get("decided_on") or ""
        rec["review_note"] = entry.get("note") or ""
        rec["struct_key"] = rec.get("struct_key") or preferred_key(rec)
        n += 1
    return n


def sign_off(path, keys, habitable, decided_by, note="", decided_on=None):
    """ENGINEER ACTION ONLY (never call it from run / classify / chart code). Save
    reviewed_habitable for each structure key (yes / no, or None to remove the review).
    The file is rewritten atomically and every change is appended to its history.
    Returns the path."""
    keys = [str(k).strip() for k in keys if str(k).strip()]
    if not keys:
        raise ValueError("no structure keys given")
    by = _text(decided_by)
    if not by:
        raise ValueError("decided_by is required: who reviewed these structures")
    level = None if habitable is None else normalize_level(habitable)
    if habitable is not None and level not in (YES, NO):
        raise ValueError("habitable must be yes, no, or None (remove)")
    on = decided_on or datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(on)
    except (TypeError, ValueError):
        raise ValueError("decided_on must be a date YYYY-MM-DD")
    data = {"schema": SCHEMA, "reviews": {}, "history": []}
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if not isinstance(old, dict) or old.get("schema") not in ACCEPTED_SCHEMAS:
            raise ValueError("{} exists and is not a habitability review file; not "
                             "overwritten".format(path))
        data["reviews"] = dict(old.get("reviews") or {})
        data["history"] = list(old.get("history") or [])
        # normalise any DamIT booleans already in the file
        for k, e in list(data["reviews"].items()):
            if isinstance(e, dict) and "reviewed_habitable" in e:
                lv = normalize_level(e.get("reviewed_habitable"))
                if lv in (YES, NO):
                    e = dict(e)
                    e["reviewed_habitable"] = lv
                    data["reviews"][k] = e
    for k in keys:
        entry = {"key": k, "reviewed_habitable": level, "decided_by": by,
                 "decided_on": on, "note": _text(note) or ""}
        data["history"].append(entry)
        if level is None:
            data["reviews"].pop(k, None)
        else:
            data["reviews"][k] = {x: entry[x] for x in
                                  ("reviewed_habitable", "decided_by", "decided_on", "note")}
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def keys_from(text, csv_path=None):
    """Structure keys from the typed list (commas, semicolons, new lines) and the
    struct_key column of a CSV (the unverified worklist)."""
    keys = [k.strip() for k in (text or "").replace(";", ",").replace("\n", ",").split(",")
            if k.strip()]
    if csv_path:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            cols = rd.fieldnames or []
            col = "struct_key" if "struct_key" in cols else (
                "Struct_ID" if "Struct_ID" in cols else None)
            if not col:
                raise ValueError("{} has no struct_key or Struct_ID column".format(
                    os.path.basename(csv_path)))
            keys += [(row.get(col) or "").strip() for row in rd if (row.get(col) or "").strip()]
    return list(dict.fromkeys(keys))
