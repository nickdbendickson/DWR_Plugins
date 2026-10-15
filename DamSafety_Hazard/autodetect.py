# -*- coding: utf-8 -*-
"""Find the hydraulic rasters in a dam project folder.

HEC-RAS (RAS Mapper) exports are named "Depth (Max).vrt",
"Velocity (Max).Terrain.tif", "Arrival Time (...).tif", "D * V (Max).tif";
DSS-WISE Lite packages "MaxDepth_in_Ft.tif" and so on. The same file-name
rules as the DSS-WISE import (dsswise.detect_role) decide the role. A file
or folder whose name says "no breach" / "baseline" / "non-failure" is taken
as the no-breach baseline. Rasters of one output time ("Depth (<time>)")
are never taken for a maximum.

The plugin's own outputs folder is never searched. When several rasters fit
a role, the one named "(Max)" wins, then a .vrt (it covers every tile), then
the one nearest the dam folder; the others are reported so the user can
pick another one on the Setup tab.
"""

import os
import re

from .constants import OUTPUT_FOLDER_NAME
from .dsswise import (detect_role, R_DEPTH, R_VEL, R_DV, R_ARR, R_DEM, R_WSE, R_PEAK,
                      RASTER_EXT)

# RAS Mapper time-step exports "Depth (01JAN2020 12:00:00).tif": recognised
# only to be left out (the maxima are wanted)
SERIES_RE = re.compile(r"^(?P<kind>depth|velocity|vel|speed)(?![a-z])[\s_\-]*(?P<rest>.*)$",
                       re.I)
PAREN_RE = re.compile(r"^\((?P<label>[^)]*)\)")
# v2.9.1: rasters that are the difference of two floods, not a flood
_DERIVED_RE = re.compile(r"(?<![a-z])(incremental|increment|difference|diff)(?![a-z])"
                         r"|(?<![a-z])rise(?![a-z])", re.I)
SKIP_LABELS = ("max", "min", "maximum", "minimum", "max value", "min value")

MAX_DEPTH_LEVELS = 5
_SKIP_DIRS = {OUTPUT_FOLDER_NAME.lower(), "_downloads", "_reprojected", ".git", "__pycache__",
              "backup", "backups", "old", "archive",
              # v2.10: ND Dam Breach Runner ensemble runs (<dam>/Probabilistic/Runs/run_NNN)
              # are classified by the class-probability tool, never taken for the dam's rasters
              "probabilistic"}
# Match only at the START of a path component so that compound names like
# "Breach_No_Breach_Compare" do not produce a false positive.
_BASELINE_PART_RE = re.compile(
    r"^(?:no[\s_\-.]*breach|non[\s_\-.]*breach|non[\s_\-.]*fail|"
    r"no[\s_\-.]*fail|without[\s_\-.]*(breach|fail)|baseline|"
    r"existing[\s_\-.]*cond)",
    re.I,
)
_ROLES = {R_DEPTH: "depth", R_VEL: "vel", R_ARR: "arr", R_DV: "dv"}


def _is_baseline(rel_path):
    """True if any component of rel_path starts with a baseline indicator.

    Splits on path separators so that a compound name like
    "Breach_No_Breach_Compare" does not accidentally match.
    """
    return any(_BASELINE_PART_RE.match(p) for p in re.split(r"[\\/]", rel_path))


def _is_time_step(name):
    """"Depth (01JAN2020 12:00:00)" / "Velocity_0100": one output time, not
    the maximum."""
    base = os.path.splitext(name)[0]
    m = SERIES_RE.match(base.strip())
    if not m:
        return False
    rest = m.group("rest").strip()
    pm = PAREN_RE.match(rest)
    label = (pm.group("label") if pm else rest).strip(" _-.").lower()
    # "Depth_Hmax", "Velocity_Vmax" (v2.9), "Depth_x_Velocity_Hmax_Vmax"
    if _MAX_TOKEN_RE.search(label):
        return False
    if re.match(r"^(x|times|by)[\s_\-]*(vel|velocity|speed)(?![a-z])", label):   # D x V product
        return False
    return bool(label) and label not in SKIP_LABELS and not label.startswith("max")


# a "max" token (Max, Hmax, Dmax, Vmax, "(Max)"), not "max" inside a word ("Maxwell",
# "Climax"): the same word boundary as the time-step check (Lumo v1.6)
_MAX_TOKEN_RE = re.compile(r"(?<![a-z])[hdv]?max(?![a-z])")


def _rank(path, root):
    name = os.path.basename(path).lower()
    depth = os.path.relpath(path, root).count(os.sep)
    return (0 if _MAX_TOKEN_RE.search(os.path.splitext(name)[0]) else 1, 0 if name.endswith(".vrt") else 1, depth, len(name))


def find_rasters(folder):
    """{"depth", "vel", "arr", "dv", "base_depth", "base_vel", "base_arr":
    path or None, "notes": [text]} for the dam folder."""
    out = {k: None for k in ("depth", "vel", "arr", "dv", "base_depth", "base_vel",
                             "base_arr")}
    out["notes"] = []
    if not folder or not os.path.isdir(folder):
        return out
    root = os.path.abspath(folder)
    if root in ("/", os.path.sep) or len(root) < 4:
        out["notes"].append("Folder is too broad to search.")
        return out
    cands = {}
    unknown = []
    derived = []
    for dirpath, dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        level = 0 if rel == "." else rel.count(os.sep) + 1
        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS and not d.startswith(".")
                   and not d.upper().startswith(("ACER_", "EAP_"))
                   and level < MAX_DEPTH_LEVELS]
        rasters = [f for f in files if os.path.splitext(f)[1].lower() in RASTER_EXT
                   and not f.lower().endswith((".aux.xml", ".ovr"))]
        if not rasters:
            continue
        for f in rasters:
            if _is_time_step(f):
                continue
            if _DERIVED_RE.search(f):
                # v2.9.1: a difference of two floods (ND Dam Breach Runner "Incremental
                # Depth (Max)", "... WSE Rise", "Difference ...") is never a flood depth
                derived.append(os.path.relpath(os.path.join(dirpath, f), root))
                continue
            # RAS Mapper writes tiles "Depth (Max).Terrain.<name>.tif" next to
            # "Depth (Max).vrt": the tiles are parts of the VRT
            if f.lower().endswith((".tif", ".tiff")) and ".terrain." in f.lower():
                stem = f.split(".")[0]
                if any(x.lower() == (stem + ".vrt").lower() for x in files):
                    continue
            if re.match(r"^d\s*[*x]\s*v(?![a-z])", f, re.I):     # RAS Mapper "D * V (Max)"
                role = "dv"
            else:
                role = _ROLES.get(detect_role(f))
            if role is None:
                # v2.9: say which rasters were passed over (not DEM / WSE / duration ...)
                if detect_role(f) not in (R_DEM, R_WSE, R_PEAK):
                    unknown.append(os.path.relpath(os.path.join(dirpath, f), root))
                continue
            path = os.path.join(dirpath, f)
            base = _is_baseline(os.path.join(rel, f))
            cands.setdefault(("base_" if base else "") + role, []).append(path)
    if unknown and not (out["depth"] or cands.get("depth")):
        out["notes"].append(
            "{} raster file(s) not recognised as depth / velocity / arrival (the file name "
            "should say Depth or Hmax / Dmax, Velocity or Vmax, Arrival): {}".format(
                len(unknown), ", ".join(sorted(unknown)[:6]) + (" ..." if len(unknown) > 6
                                                                else "")))
    if derived:
        out["notes"].append("{} raster(s) of the DIFFERENCE between two floods left out "
                            "(incremental / rise / difference): {}".format(
                                len(derived), ", ".join(sorted(derived)[:4]) +
                                (" ..." if len(derived) > 4 else "")))
    for key, paths in cands.items():
        if key == "base_dv":
            continue
        paths.sort(key=lambda p: _rank(p, root))
        out[key] = paths[0]
        if len(paths) > 1:
            out["notes"].append("{}: {} other candidate(s), e.g. {}".format(
                key, len(paths) - 1, os.path.relpath(paths[1], root)))
    return out


def describe(found, root):
    parts = []
    for key, label in (("depth", "depth"), ("vel", "velocity"), ("arr", "arrival"),
                       ("dv", "D x V"), ("base_depth", "no-breach depth"),
                       ("base_vel", "no-breach velocity"), ("base_arr", "no-breach arrival")):
        if found.get(key):
            parts.append("{} = {}".format(label, os.path.relpath(found[key], root)))
    return "; ".join(parts) if parts else "no rasters recognised"
