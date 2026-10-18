# -*- coding: utf-8 -*-
"""2.18.35: what counts as ONE dam folder, and which rasters belong to it (no Qt).

The GUI test of 8b0601c found two ways to classify a dam with another dam's rasters:
switching the dam folder kept the rasters filled from the previous folder, and the Dams
folder itself (or '//') was taken as a dam folder, whose search then walked into the dam
folders below it. The rules here are the suite's dam-folder rules: a dam folder holds the
dam's record (dam.json, or the older dam_location.json, as damsuite.damdata.dam_folders),
a Runner run (run_report.json, in _breach_model/ or the folder itself), or is named after
the dam's ID (DWR-2801, NID-ND00199, 2801)."""

import json
import os
import re

RECORD_FILES = ("dam.json", "dam_location.json")
RUN_REPORTS = ("run_report.json", os.path.join("_breach_model", "run_report.json"))
# a folder named after a dam ID: DWR-2801, DWR_2801, NID-ND00199, ND00199, 2801
ID_NAME_RE = re.compile(r"^(?:(?:DWR|NID)[-_ ]?(?:ND)?\d+|ND\d{3,}|\d{3,6})$", re.I)
# a dam ID written inside a path component or file name ("DWR-3161_depth.tif")
ID_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:DWR|NID)[-_ ]?(?:ND)?(\d+)", re.I)
_SKIP = {"outputs", "_breach_model", "_downloads", "_reprojected", "_smoke"}


def normalize(path):
    """An absolute, normalised path ('' for empty): '~' expanded, repeated separators
    collapsed ('//' is '/', 'Dams//' is 'Dams'), no trailing separator. A Windows UNC
    prefix (\\\\server) is kept."""
    text = str(path or "").strip().strip('"').strip()
    if not text:
        return ""
    text = os.path.expanduser(text)
    unc = os.name == "nt" and text[:2] in ("\\\\", "//")
    text = re.sub(r"[\\/]{2,}", lambda m: m.group(0)[0], text)
    if unc:
        text = "\\" + text
    return os.path.normpath(os.path.abspath(text))


def is_root(path):
    p = normalize(path)
    return bool(p) and os.path.dirname(p) == p


def inside(path, folder):
    """True when `path` is `folder` or below it (both normalised; symlinks resolved)."""
    p, f = normalize(path), normalize(folder)
    if not p or not f:
        return False
    try:
        p, f = os.path.realpath(p), os.path.realpath(f)
        return os.path.commonpath([p, f]) == f
    except ValueError:                      # other drive on Windows
        return False


def has_record(folder):
    """dam.json / dam_location.json in the folder (a dam folder of the suite)."""
    return any(os.path.isfile(os.path.join(folder, n)) for n in RECORD_FILES)


def has_run(folder):
    return any(os.path.isfile(os.path.join(folder, n)) for n in RUN_REPORTS)


def _digits(text):
    m = re.search(r"\d+", str(text or ""))
    return m.group(0).lstrip("0") if m else ""


def dam_id(folder):
    """The dam's ID: dam.json fields.dam_id, else the folder name when it is an ID."""
    try:
        with open(os.path.join(folder, "dam.json"), encoding="utf-8") as fh:
            v = (json.load(fh).get("fields") or {}).get("dam_id")
        if v not in (None, ""):
            return str(v)
    except (OSError, ValueError, AttributeError):
        pass
    name = os.path.basename(normalize(folder))
    return name if ID_NAME_RE.match(name) else ""


def looks_like_dam_folder(folder):
    return bool(folder) and os.path.isdir(folder) and (
        has_record(folder) or has_run(folder) or bool(ID_NAME_RE.match(os.path.basename(folder))))


def is_other_dam_dir(path, own_id=""):
    """A sub-folder that is another dam's folder: its own record, or named after an ID
    other than `own_id`. Never searched for this dam's rasters."""
    name = os.path.basename(path.rstrip("/\\"))
    if name.lower() in _SKIP:
        return False
    if has_record(path):
        return True
    if ID_NAME_RE.match(name):
        return not own_id or _digits(name) != _digits(own_id)
    return False


def child_dam_folders(folder):
    """The immediate sub-folders of `folder` that are dam folders."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for n in names:
        p = os.path.join(folder, n)
        if n.startswith(".") or n.lower() in _SKIP or not os.path.isdir(p):
            continue
        if has_record(p) or has_run(p) or ID_NAME_RE.match(n):
            out.append(p)
    return out


def dam_folder_problem(folder, dams_roots=(), has_flood_rasters=None):
    """Why `folder` cannot be the dam folder, '' when it can. Refused: a filesystem root,
    a Dams folder of the suite or a folder above it, a folder holding dam folders, and a
    folder that is no dam folder at all (no record, no run, no ID name and - when
    `has_flood_rasters` is given - no flood raster of its own)."""
    raw = str(folder or "").strip()
    if not raw:
        return ""
    p = normalize(raw)
    if is_root(p) or not raw.strip("/\\") or (len(raw.strip("/\\")) == 2 and raw.strip("/\\")[1] == ":"):
        return "'{}' is a filesystem root, not a dam folder".format(raw)
    if not os.path.isdir(p):
        return "the folder '{}' does not exist".format(raw)
    for root in dams_roots or ():
        r = normalize(root)
        if r and (r == p or inside(r, p)):
            return ("'{}' is the Dams folder{} - choose one dam's folder in it".format(
                raw, "" if r == p else " or a folder above it"))
    kids = child_dam_folders(p)
    if kids and not has_record(p):
        names = ", ".join(os.path.basename(k) for k in kids[:3]) + (" ..." if len(kids) > 3 else "")
        return ("'{}' holds {} dam folder(s) ({}) - it is a Dams folder; choose one dam's "
                "folder".format(raw, len(kids), names))
    if not looks_like_dam_folder(p):
        if has_flood_rasters is None or not has_flood_rasters(p):
            return ("'{}' is not a dam folder (no dam.json, no run_report.json, not named "
                    "after a dam ID, and no depth raster in it)".format(raw))
    return ""


def raster_problems(folder, paths, labels=None):
    """[text] for each raster of `paths` ({key: path}) that is not this dam's: outside the
    dam folder, inside another dam's folder below it, or named after another dam ID."""
    labels = labels or {}
    f = normalize(folder)
    if not f:
        return []
    own = dam_id(f) or os.path.basename(f)
    own_d = _digits(own)
    out = []
    for key, path in paths.items():
        if not path:
            continue
        label = labels.get(key, key)
        p = normalize(path)
        if not inside(p, f):
            out.append("{} raster is outside the dam folder: {} (dam folder {})".format(
                label, path, f))
            continue
        rel = os.path.relpath(p, f)
        parts = rel.split(os.sep)
        bad = ""
        d = f
        for part in parts[:-1]:
            d = os.path.join(d, part)
            if is_other_dam_dir(d, own):
                bad = dam_id(d) or part
                break
        if not bad:
            for part in parts:
                for m in ID_TOKEN_RE.finditer(part):
                    if own_d and m.group(1).lstrip("0") != own_d:
                        bad = m.group(0)
                        break
                if bad:
                    break
        if bad:
            out.append("{} raster belongs to another dam ({}): {}".format(label, bad, path))
    return out
