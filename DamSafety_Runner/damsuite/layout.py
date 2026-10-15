"""Standard work-folder layout of the dam suite (damsuite 1.4.2+).

STEP_OUTPUTS is the one table of every Screening tool's output name in a work folder
(the Screening window's core/workflow.LAYOUT is built from it). `ensure_layout(root)`
creates the standard EMPTY folders up front so every project looks the same:

    <work>/2_terrain 3_stage_storage 5_hydrographs 6_triage_envelopes 7_simulations
           9_sensitivity 10_reports 11_benchmark _downloads _logs Dams/  README.txt _layout.json

Rules (safe to call any number of times, from any plugin):
  * only os.makedirs(exist_ok=True); nothing is ever deleted, moved or overwritten
    (README.txt and _layout.json are written only when missing);
  * nothing is created inside the Dams folder (every folder there is read as a dam);
  * refused (LayoutRefused): a path inside the read-only data pack (ND_Dam_Suite_Pack), a
    Hazard reproduction work folder (_Reproduction_Work_Folder.txt) or a single dam's folder.
An empty step folder does not mark a step done: the Screening window counts a folder
output as done only when it holds something.

Pure Python standard library, no QGIS.
"""
import json
import os

# every Screening tool's output in the work folder (relative paths). Keys: step -> {parameter: path}
STEP_OUTPUTS = {
    "0": {"OUT": "0_data_package.gpkg"},
    "1": {"OUT": "1_dams.gpkg"},
    "2": {"OUT": "2_terrain"},
    "3": {"CURVES": "3_stage_storage", "OUT": "3_dams_storage.gpkg"},
    "4": {"OUTP": "4_breach_points.gpkg", "OUTL": "4_flow_arrows.gpkg"},
    "5": {"OUTDIR": "5_hydrographs", "OUT": "5_dams_breach.gpkg"},
    "6": {"OUTDIR": "6_triage_envelopes", "OUT": "6_triage.gpkg"},
    "7": {"OUTDIR": "7_simulations", "OUT": "7_simulations.gpkg"},
    "7b": {"DAMS_FOLDER": "Dams", "OUT": "7b_runner_results.gpkg"},
    "8": {"OUT": "8_hazard.gpkg", "IMP": "8_impacted.gpkg"},
    "9": {"OUTDIR": "9_sensitivity", "OUT": "9_sensitivity.gpkg"},
    "10": {"OUTDIR": "10_reports"},
    # 0.11
    "11": {"OUTDIR": "11_benchmark"},
    "12": {"OUT_CSV": "12_runtime_estimate.csv"},
    "13": {"OUT_CSV": "13_inventory_changes.csv", "OUT": "13_changed_dams.gpkg"},
    "14": {"REPORT": "14_selftest_report.txt"},
    "15": {"LOG": "15_chain_log.txt"},
    # 0.12: ponds and dugouts (does the work of 1-4 for water bodies; use a work folder of its own)
    "16": {"OUT": "16_ponds.gpkg", "OUTP": "16_pond_breach_points.gpkg", "OUTL": "16_pond_flow_arrows.gpkg",
           "OUTX": "16_pond_excluded.gpkg", "TERRAIN": "16_terrain", "CURVES": "16_stage_storage"},
    # 0.13: the preflight check; the merge of dams (1-4) and ponds (16) for the Simple tab's "Both"
    "17": {"REPORT": "17_preflight.txt"},
    "18": {"OUT": "18_dams_all.gpkg", "OUTP": "18_breach_points_all.gpkg", "OUTL": "18_flow_arrows_all.gpkg",
           "CURVES": "18_stage_storage", "TERRAIN": "18_terrain"},
    # 0.14: a proposed embankment drawn as a crest line (stands in for 1-4; its own work folder)
    "19": {"OUT": "19_proposed.gpkg", "OUTP": "19_breach_points.gpkg", "OUTL": "19_flow_arrows.gpkg",
           "CREST": "19_crest_lines.gpkg", "TERRAIN": "19_terrain", "CURVES": "19_stage_storage",
           "SPEC": "19_proposed.json"},
}

LAYOUT_VERSION = 1
CORE_STEPS = ("2", "3", "5", "6", "7", "9", "10", "11")
OPTIONAL_STEPS = ("16", "18", "19")          # ponds / all dams / proposed dam (own work folders)
FOLDER_PARAMS = ("OUTDIR", "CURVES", "TERRAIN")
SUPPORT_FOLDERS = ("_downloads", "_logs")
DAMS_DEFAULT = STEP_OUTPUTS["7b"]["DAMS_FOLDER"]
README_NAME = "README.txt"
MARKER_NAME = "_layout.json"
PACK_NAME = "ND_Dam_Suite_Pack"
REPRO_MARKER = "_Reproduction_Work_Folder.txt"     # DamSafety_Hazard/reproduce.py WORK_MARKER
DAM_FOLDER_SIGNS = ("dam.json", "_breach_model", "_screening_model")


class LayoutRefused(ValueError):
    """The folder must not get the standard layout (data pack, reproduction, dam folder)."""


def step_folders(optional=False):
    """The step output FOLDERS (not files) of the standard layout, in step order."""
    out = []
    for step in CORE_STEPS + (OPTIONAL_STEPS if optional else ()):
        for param, rel in STEP_OUTPUTS[step].items():
            if (param in FOLDER_PARAMS or (step == "2" and param == "OUT")) and rel not in out:
                out.append(rel)
    return out


def _parts(path):
    p = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    out = []
    while True:
        head, tail = os.path.split(p)
        if tail:
            out.append(tail)
        if head == p:
            return out
        p = head


def refusal(path, dam_check=True):
    """Why `path` must not be laid out (a sentence), or None."""
    if not path or not str(path).strip():
        return "no folder given"
    ap = os.path.abspath(path)
    if os.path.normcase(PACK_NAME) in _parts(ap):
        return "{} is inside the read-only data pack ({})".format(ap, PACK_NAME)
    p = ap
    while True:                        # the folder itself or any parent is a reproduction folder
        if os.path.isfile(os.path.join(p, REPRO_MARKER)):
            return "{} is a Hazard reproduction work folder ({})".format(ap, REPRO_MARKER)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if os.path.dirname(ap) == ap or os.path.normcase(ap) == os.path.normcase(os.path.expanduser("~")):
        return "{} is a drive root or the home folder; choose a project folder".format(ap)
    if dam_check:
        for sign in DAM_FOLDER_SIGNS:
            if os.path.exists(os.path.join(ap, sign)):
                return "{} is a dam's folder ({} found), not a work folder".format(ap, sign)
    return None


def _mkdir(path, created):
    if os.path.isdir(path):
        return
    if os.path.lexists(path):          # a file (or link) of that name: leave it alone
        return
    os.makedirs(path, exist_ok=True)
    created.append(path)


def _write_new(path, text, created):
    """Write `text` only when `path` does not exist (exclusive create: never overwrites)."""
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
        created.append(path)
    except FileExistsError:
        pass


def readme_text(dams_rel):
    lines = [
        "ND dam suite - work folder",
        "",
        "These folders were created empty so every project has the same layout. A step is",
        "done when its file or folder holds results; an empty folder means 'not run yet'.",
        "Nothing here deletes or moves your files.",
        "",
        "  0_data_package.gpkg .. 8_hazard.gpkg   Screening tool outputs (each with a",
        "                                         .manifest.json beside it)",
    ]
    for rel in step_folders(optional=True):
        lines.append("  {:<39}{}".format(rel + "/", "Screening tool output folder"))
    lines += [
        "  _downloads/                            downloaded layers and DEM tiles",
        "  _logs/                                 run logs",
        "  {:<39}{}".format(dams_rel + "/", "one folder per dam (Runner, Hazard); only dam folders go here"),
        "",
        "Per dam (<Dams>/<DAM-ID>/, created by the Runner): dam.json, the final rasters",
        "(Depth (Max).tif, Arrival Time.tif, ...), _breach_model/ (LISFLOOD-FP run),",
        "outputs/ (Hazard: Classification/, EAP/, QA/).",
        "",
    ]
    return "\n".join(lines)


def ensure_dams_folder(path):
    """Create the Dams folder itself (never anything inside it). Returns [path] when created."""
    why = refusal(path)
    if why:
        raise LayoutRefused(why)
    created = []
    _mkdir(os.path.abspath(path), created)
    return created


def ensure_layout(root, dams_folder=None, optional=False):
    """Create the standard empty folders (and README.txt / _layout.json when missing) in the
    work folder `root`. `dams_folder` (absolute, or relative to root) defaults to <root>/Dams.
    Returns the list of paths created ([] when all were there). Raises LayoutRefused."""
    why = refusal(root)
    if why:
        raise LayoutRefused(why)
    root = os.path.abspath(root)
    dams = dams_folder or DAMS_DEFAULT
    if not os.path.isabs(dams):
        dams = os.path.join(root, dams)
    why = refusal(dams)
    if why:
        raise LayoutRefused(why)
    created = []
    _mkdir(root, created)
    for rel in step_folders(optional) + list(SUPPORT_FOLDERS):
        _mkdir(os.path.join(root, rel), created)
    _mkdir(dams, created)
    try:
        dams_rel = os.path.relpath(dams, root)
    except ValueError:                 # another drive (Windows)
        dams_rel = dams
    _write_new(os.path.join(root, README_NAME), readme_text(dams_rel), created)
    _write_new(os.path.join(root, MARKER_NAME), json.dumps(
        {"layout_version": LAYOUT_VERSION, "dams_folder": dams_rel,
         "folders": step_folders(optional) + list(SUPPORT_FOLDERS)}, indent=1) + "\n", created)
    return created
