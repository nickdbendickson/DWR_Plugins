"""v3.10.16: a HEC-RAS 6.x 2D dam-breach package from a finished Runner run, ready to copy to
a Windows PC with HEC-RAS (or to the box's later Wine chain).

    <dam folder>/_breach_model/hecras/<DamID>_<YYYYMMDD>[_n]/
        <name>.prj .g01 .u01 .p01 .rasmap   the HEC-RAS project (core/hecras_model.build_model)
        GIS/Projection.prj                  the model's coordinate system
        GIS/HECRAS Layout.gpkg              the same geometry as layers, to check in QGIS
        Terrain/<name> Terrain.tif          the DEM in the model CRS and vertical units
        README_HECRAS.md                    what was built, the values and where they came from
        WINDOWS_README.txt                  plain-English steps in HEC-RAS 6.6, what to send back
        Runner_reference/                   the Runner's breach hydrograph (.u## block, CSV) and
                                            breach parameter table, for comparison
        PACKAGE_MANIFEST.json               versions, source run, every file with size and SHA-256
    <dam folder>/_breach_model/hecras/<DamID>_<YYYYMMDD>[_n].zip   the same folder, zipped

Nothing here runs HEC-RAS. The terrain HDF, the 2D mesh, the property tables and the compute
are made by HEC-RAS itself (WINDOWS_README.txt). The package has not been opened in HEC-RAS
when it is written: PACKAGE_MANIFEST.json says "opened_in_hecras": false.
"""
import datetime
import hashlib
import json
import os
import re
import zipfile

from . import hecras
from . import hecras_model as hm

FOLDER = "hecras"                  # under <dam folder>/_breach_model/
README = "WINDOWS_README.txt"
MANIFEST = "PACKAGE_MANIFEST.json"
REFERENCE = "Runner_reference"


def _safe(s):
    s = re.sub(r"[^\w\- ]+", "_", str(s)).strip().replace(" ", "_")
    return s or "dam"


def package_folder(dam_folder, dam_id, out_root=None, when=None):
    """A new folder <root>/<DamID>_<YYYYMMDD>, with _2, _3 ... when that exists already."""
    root = out_root or os.path.join(dam_folder, hecras.MODEL_FOLDER, FOLDER)
    day = (when or datetime.date.today()).strftime("%Y%m%d")
    base = "{}_{}".format(_safe(dam_id), day)
    path, n = os.path.join(root, base), 1
    while os.path.exists(path) or os.path.exists(path + ".zip"):
        n += 1
        path = os.path.join(root, "{}_{}".format(base, n))
    return path


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _files(folder):
    out = []
    for root, _d, files in os.walk(folder):
        for fn in sorted(files):
            p = os.path.join(root, fn)
            out.append(os.path.relpath(p, folder).replace(os.sep, "/"))
    return sorted(out)


def windows_readme(m, info, notes, version, run_tool):
    """The plain-English steps (CRLF, so Notepad shows the lines)."""
    u = info["len_name"]
    b = info["breach"]
    L = [
        "HEC-RAS 2D DAM-BREACH MODEL - {}".format(m.title),
        "Built by DamSafety Runner {} from the run made with {}.".format(version, run_tool),
        "",
        "READ THIS FIRST",
        "- This model was written by the plugin. It has NOT yet been opened in HEC-RAS.",
        "  The files follow HEC's own example projects line by line, but HEC-RAS may still",
        "  report problems. Please send back what it says (step 8).",
        "- The values are the plugin's screening estimates (breach size and timing from",
        "  regression equations, one Manning's n, a reservoir storage law). Review them before",
        "  using the results for anything official.",
        "- Tested target: HEC-RAS 6.6 on Windows. Other 6.x versions should open it and",
        "  upgrade the files; 7.x and HEC-RAS 2025 were not considered.",
        "",
        "WHAT IS IN THE FOLDER",
        "  {0}.prj   the HEC-RAS project - open this one".format(m.name),
        "  {0}.g01   geometry: reservoir storage area, 2D area '{1}' ({2:,} cells of {3:g} {4}),".format(
            m.name, hm.AREA_NAME, info["cells"], info["cell"], u),
        "            the dam connection with the breach, the downstream boundary line",
        "  {0}.u01   unsteady flow: starting reservoir level, inflow, normal depth".format(m.name),
        "  {0}.p01   plan: {1:g} hours, {2} time step, {3}".format(
            m.name, info["sim_hours"], info["comp_interval"], info["equation"]),
        "  {0}.rasmap   RAS Mapper settings (projection, geometry)".format(m.name),
        "  GIS\\Projection.prj   coordinate system {}".format(info["crs"]),
        "  GIS\\HECRAS Layout.gpkg   the same geometry as GIS layers (for QGIS)",
        "  Terrain\\{}.tif   ground elevations in {}, {:g} {} cells".format(
            m.terrain_name, u, info["terrain_cell"], u),
        "  README_HECRAS.md   every value and where it came from",
        "  Runner_reference\\   the plugin's own breach hydrograph and breach table",
        "",
        "STEPS IN HEC-RAS 6.6 (WINDOWS)",
        "1. Copy the whole folder (or unzip the .zip) to a local drive, for example",
        "   C:\\HEC_Models\\{}\\. Keep the folder structure. Avoid OneDrive/network folders.".format(
            m.name),
        "2. Start HEC-RAS 6.6. File > Open Project... and pick {}.prj.".format(m.name),
        "   If HEC-RAS asks to convert or upgrade the files, say yes.",
        "3. Open RAS Mapper (the globe button). If it asks for a projection, choose",
        "   GIS\\Projection.prj in this folder. (Or: Project > Set Projection.)",
        "4. Make the terrain: Project > Create a New RAS Terrain. Add",
        "   Terrain\\{}.tif, set the vertical units to {} if asked,".format(m.terrain_name,
                                                                         "feet" if u == "ft" else "meters"),
        "   save the terrain in the Terrain folder, and click Create.",
        "5. Connect the geometry to the terrain: right-click Geometries > {} >".format(m.name),
        "   Manage Geometry Associations (in 6.6 also under Geometry > Associations),",
        "   and choose the new terrain.",
        "6. Build the 2D mesh: in RAS Mapper turn on editing for the geometry, right-click the",
        "   2D Flow Area '{}' > Edit 2D Area Properties > Generate Computation Points".format(hm.AREA_NAME),
        "   ONLY if you want another cell size (the points are already there, {:g} {}).".format(
            info["cell"], u),
        "   Stop editing and save. Then Geometry > Compute Property Tables (right-click the",
        "   geometry). Note every message HEC-RAS shows about the mesh.",
        "7. Run: in the main HEC-RAS window, Run > Unsteady Flow Analysis. Pick plan '{}',".format(
            m.title),
        "   tick Geometry Preprocessor, Unsteady Flow Simulation and Post Processor, and Compute.",
        "   The plan breaches the dam at {:.2f} {} (water level in the reservoir).".format(
            b["trigger_wse"], u),
        "8. SEND BACK (zip them, or the whole folder):",
        "   - {}.p01.hdf (the results; it appears after a run, even a failed one)".format(m.name),
        "   - the computation messages: the text in the Unsteady Flow Analysis window after",
        "     the run (copy it into a .txt file), and {}.p01.computeMsgs.txt if it exists".format(
            m.name),
        "   - {}.g01 and {}.p01 as HEC-RAS saved them (so the plugin can see what".format(m.name, m.name),
        "     HEC-RAS changed)",
        "   - screenshots of any error or warning windows, and of the mesh near the dam",
        "",
        "IF SOMETHING GOES WRONG",
        "- 'No terrain associated' or property tables fail: redo steps 4-5 and keep the terrain",
        "  inside this project's Terrain folder.",
        "- Mesh errors (cells with too many faces, points outside the area): in RAS Mapper move",
        "  or delete the reported points, or regenerate the points at a different cell size,",
        "  then Compute Property Tables again.",
        "- The run is very slow: the time step is {} (fixed). In the plan's Computation Options".format(
            info["comp_interval"]),
        "  you can switch to an adjustable time step based on the Courant number.",
        "- Anything else: stop and send back what you have (step 8).",
        "",
        "NOT VERIFIED YET",
        "- The files have not been opened in HEC-RAS; the dam connection, the breach and the",
        "  culvert/bridge connections (if any) follow HEC's example files but are untested.",
        "- Spillways and outlets are not in the dam connection (the dam is a weir at its top).",
        "- DEM edits the Runner made (channels, culvert burns, buildings) are not in the terrain.",
        "- The vertical datum is the DEM's own (for 3DEP / ND LiDAR: NAVD88); the top of dam",
        "  must be in that datum.",
        "",
    ]
    if notes:
        L += ["NOTES FROM THE BUILD"] + ["- " + n for n in notes] + [""]
    return "\r\n".join(L)


def build_package(dam_folder, out_root=None, dt_min=hecras.DEFAULT_STEP_MIN, crs=None,
                  options=None, dem_path=None, log=None, progress=None, when=None,
                  make_zip=True):
    """Builds the package (see the module docstring). Returns {"folder", "zip", "model",
    "info", "notes", "manifest"}. Raises hecras_model.HecRasModelError / hecras.HecRasError
    with a plain reason when the run cannot give a model."""
    log = log or (lambda m: None)
    hy, dam, params = hecras.load_run(dam_folder)
    with open(os.path.join(dam_folder, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON),
              encoding="utf-8") as f:
        report = json.load(f)
    folder = package_folder(dam_folder, dam.dam_id, out_root, when)
    os.makedirs(folder)
    try:
        r = hm.build_model(dam_folder, out_folder=folder, dem_path=dem_path, crs=crs,
                           options=options, log=log, progress=progress)
    except Exception:
        # leave no half package behind
        import shutil
        shutil.rmtree(folder, ignore_errors=True)
        raise
    ref = os.path.join(folder, REFERENCE)
    h = hecras.write_hydrograph_files(hy, ref, dt_min)
    hecras.write_breach_table(dam, params, ref, peak_cfs=params.get("peak_cfs"),
                              notes=h["info"]["notes"])
    from .damjson import plugin_version
    version = plugin_version()
    run_tool = report.get("tool") or "the Runner"
    with open(os.path.join(folder, README), "w", encoding="utf-8", newline="") as f:
        f.write(windows_readme(r["model"], r["info"], r["notes"], version, run_tool))
    files = _files(folder)
    man = {"format": "damsafety_runner/hecras_package", "format_version": 1,
           "built_by": "DamSafety Runner {}".format(version),
           "built": datetime.datetime.now().isoformat(timespec="seconds"),
           "source_run": {"dam_folder": os.path.abspath(dam_folder), "tool": run_tool,
                          "run_started": report.get("run_started"),
                          "scenario": report.get("scenario")},
           "hecras_target": "HEC-RAS 6.6 (Windows)", "opened_in_hecras": False,
           "project": r["model"].name + ".prj",
           "crs": r["info"]["crs"], "units": r["info"]["units"],
           "cells": r["info"]["cells"], "cell": r["info"]["cell"],
           "dam_line": r["info"]["dam_line"], "notes": r["notes"],
           "files": [{"path": p, "bytes": os.path.getsize(os.path.join(folder, p)),
                      "sha256": _sha256(os.path.join(folder, p))} for p in files]}
    with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    zpath = None
    if make_zip:
        zpath = folder + ".zip"
        top = os.path.basename(folder)
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            for p in _files(folder):
                z.write(os.path.join(folder, p), top + "/" + p)
    log("HEC-RAS package: {}{}".format(folder, " and {}".format(zpath) if zpath else ""))
    return {"folder": folder, "zip": zpath, "model": r["model"], "info": r["info"],
            "notes": r["notes"], "manifest": man}
