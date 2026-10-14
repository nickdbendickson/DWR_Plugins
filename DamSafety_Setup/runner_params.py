# -*- coding: utf-8 -*-
"""The wizard's answers as parameters of 'ndbreach:rundam'. No Qt here, so it is tested
without QGIS (tests/test_runner_params.py).

DEM_Z is an enum: an index into the Runner's choices, in the Runner's order
(DamSafety_Runner/core/gdal_io.Z_CHOICES = auto, feet, us_feet, meters). The index is
looked up by the unit's id, never typed by hand: a hand-typed index of 2 for 'Metres'
made the Runner read a metre DEM as US survey feet (release on 2026-10-13)."""

# The Runner's default largest domain side (DamSafety_Runner/algorithms.DEFAULT_MAX_EXTENT_MI).
DEFAULT_MAX_EXTENT_MI = 20.0

# The Runner's DEM_Z options in order (gdal_io.Z_CHOICES / algorithms.ZUNITS).
RUNNER_Z_IDS = ("auto", "feet", "us_feet", "meters")

# What the Setup page may hand over, by its item data, and the Runner id it means.
_Z_ALIASES = {
    "auto": "auto", "": "auto", None: "auto",
    "feet": "feet", "ft": "feet", "foot": "feet",
    "us_feet": "us_feet", "us-ft": "us_feet", "ftus": "us_feet",
    "meters": "meters", "metres": "meters", "meter": "meters", "metre": "meters", "m": "meters",
}


def runner_z_id(choice):
    """The Runner's unit id for the Setup page's choice; 'auto' when unknown (the Runner
    then refuses to run and asks for the unit instead of guessing)."""
    key = choice.strip().lower() if isinstance(choice, str) else choice
    return _Z_ALIASES.get(key, "auto")


def dem_z_index(choice):
    """DEM_Z index for the Runner. 'meters' is 3 (Meters), never 2 (US survey feet)."""
    return RUNNER_Z_IDS.index(runner_z_id(choice))


def build_params(a):
    from .damsuite.suite_config import safe_dams_folder
    """ndbreach:rundam parameters from the wizard answers `a`."""
    out = {
        "DAM_ID": a.get("dam_id") or "",
        "DAM_NAME": a.get("dam_name") or "",
        "DAM_POINT": a.get("point") or "",
        "DAM_LINES": a.get("lines") or "",
        "STRUCTURE_ID": a.get("structure_id") or "",
        "HEIGHT_FT": a.get("height") or 0,
        "STORAGE_ACFT": a.get("storage") or 0,
        "DEM": a.get("dem") or "",
        "DEM_Z": dem_z_index(a.get("dem_z")),
        "CELL": a.get("cell") or 10,
        "DURATION_HR": a.get("hours") or 12,
        "PRODUCTS": bool(a.get("eap")),
        "MAX_EXTENT_MI": float(a.get("max_extent_mi") or DEFAULT_MAX_EXTENT_MI),
        # Results go to the Dams folder, never the suite data folder (a.get("folder")),
        # which may be the read-only ND_Dam_Suite_Pack.
        "DAMS_FOLDER": safe_dams_folder(a.get("dams_folder")),
    }
    if a.get("area"):
        out["AREA_AC"] = a.get("area")
    if a.get("top"):
        out["TOP_FT"] = a.get("top")
    return out


DEM_SUFFIXES = ("_10m_5070.tif", "_1m_5070.tif")


def dem_stem(dam_id):
    """The stem 'Download DEM' writes for a dam ID (Screening core/dem_names.dem_stem)."""
    import re
    return re.sub(r"[^\w\-]+", "_", str(dam_id or "").strip()).strip("_")


def downloaded_dem(out, dam_id, point=""):
    """The DEM 'Download DEM' just wrote for this dam in `out`, or "": <DAM_ID>_10m/1m_5070.tif,
    else dam_<lon>_<lat>_* named by this dam's own point. Never another dam's file."""
    import os
    stems = [dem_stem(dam_id)]
    raw = (point or "").strip()
    if raw and "," in raw:
        stems.append("dam_{}_{}".format(*[p.strip() for p in raw.split(",")[:2]]))
    for stem in [s for s in stems if s]:
        for suf in DEM_SUFFIXES:
            path = os.path.join(out, stem + suf)
            if os.path.isfile(path):
                return path
    return ""
