"""2.18.59: a dam's Hazard window state saved as its own QGIS project, so a dam classified by a
batch (the Runner's batch chain) can be opened later in the Hazard window with its scenario
table, folders and options filled in, and more scenarios added ('Add scenario' /
'Add from a folder…') before classifying again.

The window keeps its state in the QGIS project (settings.SCOPE / settings.KEY, a JSON dict of
widget name -> value, the scenario table included) and restores it when it opens and when a
project is read. save_dam_state() writes that same entry into a NEW project (never the open
one) holding the dam's flood rasters and its model boundary, and saves it as
<dam folder>/Hazard_<dam>.qgz, with a readable JSON copy beside it.

To reopen: Project > Open… the .qgz, then open the Hazard window (it fills itself from the
project); add scenarios on the Classify tab and press Classify."""

import datetime
import json
import os

from . import settings as S

STATE_FORMAT = "ND_Dam_Hazard_Helper/dam_state"
STATE_FORMAT_VERSION = 1
NONE_TOKEN = "__none__"

# classify tool parameter -> the window's widget (same meaning, same type: True/False)
CLASSIFY_TO_WIDGET = {
    "KEEP_DRY": "keep_dry_check", "JUDGMENT": "judgment_check",
    "ND_JUDGMENT_HIGH": "nd_judgment_check",
    "DOWNLOAD_NSI": "nsi_download_check", "DOWNLOAD_ADDRESS": "addr_download_check",
    "DOWNLOAD_USA": "usa_download_check", "DOWNLOAD_DAMS": "dams_download_check",
    "DOWNLOAD_LIFELINES": "lifelines_download_check", "EAP": "eap_check",
    "DOWNLOAD_CROSSINGS": "crossings_download_check", "SPREADSHEETS": "spreadsheets_check",
    "REPORT": "report_check", "DOWNLOAD_CAMPS": "camps_download_check",
    "EAP_LIST": "eap_list_check", "EAP_MAPBOOK": "eap_mapbook_check",
    "EAP_TRAVEL": "eap_travel_check", "COMPARE": "compare_check",
}


def safe_name(text):
    out = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in str(text or "").strip())
    return out.strip("._") or "dam"


def default_project_path(dam_folder, dam_id=""):
    return os.path.join(dam_folder, "Hazard_{}.qgz".format(
        safe_name(dam_id or os.path.basename(os.path.normpath(dam_folder)))))


def build_state(dam_folder, depth, velocity="", arrival="", dv="", name="Breach", nid="",
                boundary_layer_id="", profile_state=None, classify_options=None):
    """The window state (dict) for one dam: the shared options of a settings profile
    (`profile_state`, already loaded), the classify options that have a widget, then the
    dam's own entries (folders, rasters, scenario name, NID ID, boundary, an empty scenario
    table that 'Add scenario' adds to)."""
    from .constants import OUTPUT_FOLDER_NAME
    state = {}
    for k, v in (profile_state or {}).items():
        if k not in S.DAM_SPECIFIC_KEYS:
            state[k] = v
    for k, v in (classify_options or {}).items():
        w = CLASSIFY_TO_WIDGET.get(str(k).upper())
        if w and isinstance(v, (bool, int)):
            state[w] = bool(v)
    state.update({
        "dam_folder_edit": dam_folder,
        "output_folder_edit": os.path.join(dam_folder, OUTPUT_FOLDER_NAME),
        "depth_edit": depth or "", "vel_edit": velocity or "", "arr_edit": arrival or "",
        "dv_edit": dv or "", "main_name_edit": name or "Breach",
        "subject_nid_edit": nid or "",
        "boundary_combo": boundary_layer_id or NONE_TOKEN,
        "scenario_rows": [], "main_baseline": "",
    })
    return state


def check_inputs(dam_folder, depth, velocity="", arrival="", dv="", boundary=""):
    """Problems (list of text) that stop the save: a missing dam folder or depth raster, or a
    raster / boundary file given that does not exist."""
    probs = []
    if not dam_folder or not os.path.isdir(dam_folder):
        probs.append("dam folder not found: {}".format(dam_folder))
    if not depth or not os.path.isfile(depth):
        probs.append("depth raster not found: {}".format(depth))
    for label, p in (("velocity raster", velocity), ("arrival raster", arrival),
                     ("depth x velocity raster", dv), ("model boundary", boundary)):
        if p and not os.path.isfile(p):
            probs.append("{} not found: {}".format(label, p))
    return probs


def save_dam_state(dam_folder, depth, velocity="", arrival="", dv="", name="Breach", nid="",
                   boundary="", profile="", classify_options=None, project_path="",
                   dam_id="", log=None):
    """Writes <dam folder>/Hazard_<dam>.qgz (or `project_path`) and its JSON copy.
    Returns {"project", "json", "state", "notes"}; raises ValueError on bad inputs.
    Needs QGIS (qgis.core); the open project is never touched."""
    from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer
    log = log or (lambda m: None)
    probs = check_inputs(dam_folder, depth, velocity, arrival, dv, boundary)
    if probs:
        raise ValueError("; ".join(probs))
    notes = []
    prof_state = {}
    if profile:
        prof_state, meta = S.load_profile(profile)         # ProfileError: clear message
        notes += list(meta.get("warnings") or [])
    path = project_path or default_project_path(dam_folder, dam_id)
    proj = QgsProject()
    for p, lname in ((depth, "Max depth (ft)"), (velocity, "Max velocity (ft/s)"),
                     (arrival, "Arrival time (h)"), (dv, "Depth x velocity (ft2/s)")):
        if p:
            lyr = QgsRasterLayer(p, "{} - {}".format(name or "Breach", lname))
            if lyr.isValid():
                proj.addMapLayer(lyr)
            else:
                notes.append("not a readable raster, left out of the project: {}".format(p))
    bid = ""
    if boundary:
        b = QgsVectorLayer(boundary, "Model boundary (2D flow area)", "ogr")
        if b.isValid():
            proj.addMapLayer(b)
            bid = b.id()
        else:
            notes.append("model boundary not readable, not set: {}".format(boundary))
    state = build_state(dam_folder, depth, velocity, arrival, dv, name, nid, bid, prof_state,
                        classify_options)
    proj.writeEntry(S.SCOPE, S.KEY, json.dumps(state))
    try:
        lyrs = list(proj.mapLayers().values())
        if lyrs and lyrs[0].crs().isValid():
            proj.setCrs(lyrs[0].crs())
    except Exception:                                       # noqa - the CRS is cosmetic
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if not proj.write(path):
        raise OSError("the project could not be written: {}".format(path))
    jpath = os.path.splitext(path)[0] + ".hazard_state.json"
    rec = {"format": STATE_FORMAT, "format_version": STATE_FORMAT_VERSION,
           "saved": datetime.datetime.now().isoformat(timespec="seconds"),
           "plugin_version": S._plugin_version()[1], "project": path,
           "profile": profile or "", "notes": notes,
           "reopen": "Project > Open… this .qgz, then open the Hazard window; add scenarios "
                     "with 'Add scenario' / 'Add from a folder…' and press Classify.",
           "state": state}
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1)
    log("Hazard window state saved: {}".format(path))
    return {"project": path, "json": jpath, "state": state, "notes": notes}
