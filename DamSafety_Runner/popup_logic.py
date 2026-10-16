"""The logic behind the ND Dam Breach Runner popup window - pure Python, no Qt, no QGIS.

The window is a front door to the tools, not a second copy of them: it knows the DAMS FOLDER
(one folder per dam, named after the dam ID - the Runner's own layout) and the dam, shows what
is in that dam's folder, and opens the Processing dialog of any tool with the dam's folder (and
the DEM chosen in the window) filled in. The tools, the wizard and the batch / qgis_process use
are unchanged; every filled-in value can still be changed in the tool's own dialog.
"""
import os

from .core import damjson, workflow

PROVIDER = "ndbreach"
WIZARD = "wizard:"

# tab -> tools: (algorithm, button label, tooltip). "wizard:" opens the guided wizard.
TABS = (
    ("Dam", (
        (WIZARD, "Guided setup for one dam (wizard)...",
         "Five pages that fill in 'Run breach model for one dam', with a live run-time estimate."),
        ("rundam", "Run breach model for one dam...",
         "The breach hydrograph, the LISFLOOD-FP flood (and ANUGA if chosen), rasters, products and "
         "the run report, written into <Dams folder>/<Dam ID>."),
        ("batch", "Run breach models for a layer of dams...",
         "One run per dam of a point layer; can hand each result to the hazard classification."),
        ("estimate", "Estimate the run time...",
         "The plan and the time of a run for this dam, without running it."),
    )),
    ("Model", (
        ("nested", "Nested fine grid near the dam...", "A fine grid near the dam, a coarse one downstream."),
        ("sensitivity", "Sensitivity of the breach model...",
         "Re-runs the dam with other breach widths, times, Manning n and equations."),
        ("probabilistic", "Probabilistic (Monte Carlo) run...",
         "Runs drawn from parameter distributions you set; 'Write each run's rasters' lets ND Dam "
         "Hazard give a class probability."),
        ("cascade", "Cascade failure of dams in a chain...", "Downstream dams that fail in turn."),
        ("hydrograph", "Breach hydrograph only...", "No flood model: the breach outflow hydrograph."),
        ("rainfallrunoff", "Rainfall to PMF inflow hydrograph...", "A PMF inflow from rainfall."),
        ("spillwayadequacy", "Spillway adequacy...", "Does the dam pass its inflow design flood (no breach)?"),
    )),
    ("Data", (
        ("buildvrt", "Build a statewide LiDAR mosaic (VRT)...", "One virtual raster over DEM tiles."),
        ("fillgaps", "Fill no-data gaps in a DEM...", "Small gaps in a DEM, before modelling."),
        ("nlcdmanning", "Manning's n from NLCD...", "A Manning's n raster from land cover."),
        ("conditiondem", "Preview the DEM conditioning...",
         "What the culvert / channel / pond conditioning does to the DEM around the dam."),
    )),
    ("Results", (
        ("products", "Flood polygons and EAP map from rasters...", "Polygons, arrival contours, map."),
        ("consequences", "Life safety and damage estimate...", "Incremental life safety and damage."),
        ("ndhazardclass", "ND hazard classification (Section 5.2)...",
         "The Runner's own preview of the ND 5.2 class with the ACER TM 11 charts. ND Dam Hazard "
         "is the classification authority."),
        ("classify", "Hand the results to the hazard classification...",
         "Runs the ND Dam Hazard classification on the dam's folder (DAM_FOLDER only)."),
        ("animation", "Animation of the flood...", "GIF / MP4 / time-enabled layers."),
        ("submittal", "Submittal report (Word)...", "The report with figures, checklist and files."),
        ("benchmark", "Compare with a reference model (HEC-RAS)...", "Rasters against a reference model."),
        ("hecrasexport", "Export to HEC-RAS...", "The breach hydrograph and breach table."),
        ("hecrasimport", "Import HEC-RAS results...", "HEC-RAS 2D results as rasters."),
        ("validationrecord", "Validation record (Word)...", "From a benchmark comparison."),
    )),
    ("Checks", (
        ("verifymanifest", "Verify a run manifest...", "Are the inputs and results unchanged?"),
        ("verification", "Verification against analytical solutions...", "Analytic dam-break tests."),
        ("check", "Check the model installation...", "LISFLOOD-FP and ANUGA found and working?"),
        ("selftest", "Self-test in this QGIS...", "A short run of the whole chain in this QGIS."),
    )),
)
ALL_TOOLS = tuple(t for _tab, tools in TABS for t in tools)
TOOL_LABEL = {t[0]: t[1] for t in ALL_TOOLS}

# which parameters carry the dams folder / the dam
WITH_DAM = frozenset({"rundam", "estimate", "sensitivity", "nested", "probabilistic"})   # + DEM
WITH_DAMS_FOLDER = frozenset({"batch", "cascade"})                                       # + DEM
WITH_DAM_FOLDER = frozenset({"products", "consequences", "ndhazardclass", "animation", "benchmark",
                             "hecrasexport", "hecrasimport", "submittal"})              # DAM_FOLDER
WITH_DAM_ID = frozenset({"products", "submittal"})
WITH_DEM = frozenset({"conditiondem", "animation"})

DEFAULT_SETTINGS = {"dams_folder": "", "dam_id": "", "dem": "", "settings_file": "",
                    "grid_m": 10.0, "simh": 12.0, "simmax": 96.0, "force_anuga": False, "length_mi": 4.0, "half_width_mi": 1.0}
PLUGIN_KEY = "ndbreach"                 # the suite settings file key (damsuite.versions)
TAB_ORDER = tuple(t for t, _tools in TABS)


def settings_group(alg):
    """(settings tab, order) of a tool: the popup tab it sits on; tools of no tab last."""
    for i, (tab, tools) in enumerate(TABS):
        for j, (a, _label, _tip) in enumerate(tools):
            if a == alg:
                return tab, j
    return (TAB_ORDER[-1], 99)


def dam_folder(dams_folder, dam_id):
    """<Dams folder>/<Dam ID> as the tools name it ('' when either is empty)."""
    if not dams_folder or not str(dam_id or "").strip():
        return ""
    return os.path.join(dams_folder, damjson.folder_name(str(dam_id).strip()))


def list_dams(dams_folder):
    """Names of the folders in the dams folder that hold a dam: a dam.json, a model folder or
    final rasters."""
    names = []
    try:
        entries = sorted(os.listdir(dams_folder))
    except OSError:
        return names
    for n in entries:
        p = os.path.join(dams_folder, n)
        if not os.path.isdir(p):
            continue
        if (os.path.isfile(os.path.join(p, "dam.json")) or os.path.isdir(os.path.join(p, workflow.MODEL_FOLDER))
                or os.path.isfile(os.path.join(p, workflow.FINAL_NAMES["depth"]))):
            names.append(n)
    return names


def _file(folder, *parts):
    return os.path.isfile(os.path.join(folder, *parts))


def _dir(folder, *parts):
    p = os.path.join(folder, *parts)
    try:
        if not os.path.isdir(p):
            return False
        with os.scandir(p) as it:
            return any(True for _ in it)
    except OSError:
        return False


def dam_status(folder):
    """[(key, label, state, detail)] for a dam folder. state: 'yes', 'part', 'no'."""
    rows = []
    if not folder or not os.path.isdir(folder):
        return rows
    fin = workflow.FINAL_NAMES
    have = [k for k in ("depth", "velocity", "arrival") if _file(folder, fin[k])]
    rows.append(terrain_row(folder))
    rows.append(("run", "Flood rasters (depth, velocity, arrival)",
                 "yes" if len(have) == 3 else "part" if have else "no",
                 "" if len(have) == 3 else ("only " + ", ".join(have)) if have else "not run yet"))
    base = [k for k in ("depth", "velocity", "arrival") if _file(folder, workflow.BASELINE_NAMES[k])]
    rows.append(("baseline", "Baseline (no-breach) rasters", "yes" if len(base) == 3 else "part" if base else "no",
                 "" if len(base) == 3 else "none (a sunny-day run has none)" if not base else
                 "only " + ", ".join(base)))
    rows.append(("incremental", "Incremental depth",
                 "yes" if _file(folder, workflow.INCREMENT_NAMES["depth"]) else "no", ""))
    manifest = _file(folder, *workflow.MODEL_FOLDER.split("/"), "run_manifest.json")
    rows.append(("manifest", "Run manifest (SHA-256 of inputs and results)", "yes" if manifest else "no",
                 "" if manifest else "written by a run of this version"))
    rows.append(("report", "Run report", "yes" if _file(folder, workflow.MODEL_FOLDER, "run_report.json")
                 else "no", ""))
    prod = any(_file(folder, n) for n in ("Breach Products.gpkg", "Flood Extent.geojson",
                                          "Flood Polygons (Max Depth).geojson"))
    rows.append(("products", "Flood polygons / products", "yes" if prod else "no", ""))
    ens = _file(folder, "Probabilistic", "Runs", "index.json") or _file(folder, "Probabilistic",
                                                                         "Probabilistic_Summary.csv")
    rows.append(("ensemble", "Probabilistic runs", "yes" if ens else "no",
                 "" if _file(folder, "Probabilistic", "Runs", "index.json") or not ens else
                 "no per-run rasters: ND Dam Hazard cannot give a class probability"))
    rows.append(("sensitivity", "Sensitivity runs", "yes" if _dir(folder, "Sensitivity") else "no", ""))
    rows.append(("nested", "Nested-grid run", "yes" if _dir(folder, "Nested") else "no", ""))
    rows.append(("preview", "ND 5.2 preview class (Runner's own)",
                 "yes" if _file(folder, workflow.STALE_ON_RERUN[1]) else "no", ""))
    rows.append(("handoff", "Hand-off to the hazard classification",
                 "yes" if _dir(folder, "_classification") else "no", ""))
    rows.append(("animation", "Animation", "yes" if _dir(folder, "Animation") else "no", ""))
    rows.append(("hecras", "HEC-RAS files", "yes" if _dir(folder, "_hecras") else "no", ""))
    rows.append(submittal_row(folder))
    return rows


def terrain_row(folder):
    """The model's terrain clip. A run writes it as <name>.dem.asc in _breach_model/lisflood (and
    lisflood_base); the run removes it again unless the run files are kept, so a model folder with
    no clip is 'part' (the run report still names the DEM)."""
    label = "Terrain clip (model DEM)"
    model = os.path.join(folder, workflow.MODEL_FOLDER)
    if not os.path.isdir(model):
        return ("terrain", label, "no", "no model folder yet")
    for sub in ("lisflood", "lisflood_base"):
        try:
            if any(n.endswith(".dem.asc") for n in os.listdir(os.path.join(model, sub))):
                return ("terrain", label, "yes", "kept in {}/{}".format(workflow.MODEL_FOLDER, sub))
        except OSError:
            continue
    if _file(model, "dem_refill.tif"):
        return ("terrain", label, "yes", "dem_refill.tif")
    return ("terrain", label, "part", "model folder, but the clip was cleaned up after the run")


def submittal_row(folder):
    """The Word submittal report. The tool writes a .docx wherever OUTPUT says; the dam folder's
    own .docx files are the ones found. 'part' = older than the run report (stale)."""
    label = "Submittal report (Word)"
    try:
        docs = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(".docx")
                and not n.startswith("~$") and os.path.isfile(os.path.join(folder, n))]
    except OSError:
        docs = []
    if not docs:
        return ("submittal", label, "no", "")
    newest = max(docs, key=os.path.getmtime)
    rep = os.path.join(folder, workflow.MODEL_FOLDER, "run_report.json")
    if os.path.isfile(rep) and os.path.getmtime(newest) < os.path.getmtime(rep):
        return ("submittal", label, "part", "{} is older than the last run".format(os.path.basename(newest)))
    return ("submittal", label, "yes", os.path.basename(newest))


STAGE_KEYS = ("dam", "terrain", "run", "products")
STAGE_LABELS = {"dam": "Dam", "terrain": "Terrain", "run": "Model run", "products": "Products"}
# which status row tells whether a tool has been run for the dam
TOOL_STATUS_KEY = {"rundam": "run", "batch": "run", "nested": "nested", "sensitivity": "sensitivity",
                   "probabilistic": "ensemble", "products": "products", "ndhazardclass": "preview",
                   "classify": "handoff", "animation": "animation", "submittal": "submittal",
                   "hecrasexport": "hecras", "hecrasimport": "hecras"}


def tool_status(alg, rows):
    """'yes' / 'part' / 'no' for a tool from the dam's status rows; None = no status for it."""
    key = TOOL_STATUS_KEY.get(alg)
    if key is None or not rows:
        return None
    for k, _l, st, _d in rows:
        if k == key:
            return st
    return None


def stage_states(rows, has_folder, has_dam_json, dem, dam_id):
    """{stage key: 'yes' / 'part' / 'no'} for Dam -> Terrain -> Model run -> Products."""
    st = {k: v for k, _l, v, _d in rows}
    dam = "yes" if has_folder and has_dam_json else "part" if has_folder or dam_id else "no"
    if has_folder:
        terrain = st.get("terrain", "no")
        if terrain == "no" and dem:
            terrain = "part"
    else:
        terrain = "part" if dem else "no"
    run = st.get("run", "no") if has_folder else "no"
    prod = [st.get(k) for k in ("products", "handoff", "submittal")]
    products = "yes" if st.get("products") == "yes" and st.get("handoff") == "yes" else \
        "part" if any(v in ("yes", "part") for v in prod) else "no"
    return {"dam": dam, "terrain": terrain, "run": run, "products": products}


def status_lines(rows):
    mark = {"yes": "✓", "part": "◐", "no": "·"}
    return ["{} {}{}".format(mark[st], label, " - " + detail if detail else "")
            for _k, label, st, detail in rows]


def dam_record_text(folder):
    """(one line, next-action code) of the dam folder's dam.json."""
    text, code, _has = dam_record_info(folder)
    return text, code


def dam_record_info(folder):
    """(one line, next-action code, has a readable dam.json) of the dam folder."""
    if not folder or not os.path.isdir(folder):
        return "dam.json: no dam folder", "", False
    try:
        from .damsuite import dam_record as DR
        rec = DR.load(folder)
    except ValueError as e:
        return "dam.json: cannot be read ({})".format(e), "", False
    except Exception as e:                                       # noqa
        return "dam.json: not available ({})".format(e), "", False
    if rec is None:
        return ("dam.json: none yet (written by the next run, DamSafety Screening tool 7b or "
                "DamSafety Hazard)"), "", False
    code, why = DR.next_action(rec)
    steps = []
    for step in DR.STEPS:
        e = rec.latest(step)
        if e:
            steps.append("{} {} ({})".format(step, (e.get("when") or "")[:10], e.get("tool") or "?"))
    disputed = len([c for c in rec.conflicts if not c.get("applied")])
    return ("dam.json: {}; next: {}{}".format(
        ", ".join(steps) or "no status", why or code.upper(),
        " - {} value(s) disputed".format(disputed) if disputed else "")), code, True


def suggest(rows, code=""):
    """(tool, text): the tool the dam most likely needs next, from the folder and dam.json's
    next-action code. Only a suggestion."""
    st = {k: s for k, _l, s, _d in rows}
    if not rows or st.get("run") != "yes" or code == "model":
        return "rundam", "Run the breach model."
    if code == "classify" or (st.get("handoff") != "yes" and code in ("", "none")):
        return "classify", "Hand the results to the DamSafety Hazard classification."
    if code == "review":
        return "classify", "Open DamSafety Hazard's reviewer's view; its classification has open review items."
    return None, "Nothing is waiting for this dam. The submittal report is on the Results tab."


def defaults(alg, dams_folder, dam_id, settings=None):
    """The parameters to fill into tool `alg` for the dam."""
    S = dict(DEFAULT_SETTINGS)
    S.update(settings or {})
    dams = (dams_folder or "").strip()
    did = str(dam_id or "").strip()
    folder = dam_folder(dams, did)
    p = {}

    def put(k, v):
        if v not in (None, ""):
            p[k] = v

    if alg in WITH_DAM:
        put("DAMS_FOLDER", dams)
        put("DAM_ID", did)
    if alg in WITH_DAMS_FOLDER:
        put("DAMS_FOLDER", dams)
    if alg in WITH_DAM or alg in WITH_DAMS_FOLDER or alg in WITH_DEM or alg == "conditiondem":
        put("DEM", (S.get("dem") or "").strip())
    if alg in WITH_DAM_FOLDER and folder and os.path.isdir(folder):
        put("DAM_FOLDER", folder)
    if alg in WITH_DAM_ID:
        put("DAM_ID", did)
    if alg == "classify" and folder and os.path.isdir(folder):
        p.update({"MODE": 0, "FOLDER": folder})
        put("DAM_ID", did)
    if alg == "verifymanifest" and folder and os.path.isdir(folder):
        put("TARGET", folder)
    if alg == "validationrecord" and folder and _file(folder, "Benchmark_Summary.csv"):
        put("BENCH_FOLDER", folder)
        put("DAM_ID", did)
    if alg == "hecrasimport" and folder and os.path.isdir(folder):
        put("DAM_FOLDER", folder)
    if alg == "rundam":
        put("CELL", float(S["grid_m"] if S.get("grid_m") is not None else 10.0))
        put("DURATION_HR", float(S["simh"] if S.get("simh") is not None else 12.0))
        put("DURATION_MAX_HR", float(S["simmax"] if S.get("simmax") is not None else 96.0))
        if S.get("force_anuga"):
            p["FORCE_ANUGA"] = True
            p["TIER"] = 1
    return p


RESULT_RASTERS = tuple(workflow.FINAL_NAMES.values()) + tuple(workflow.BASELINE_NAMES.values()) + \
    tuple(workflow.INCREMENT_NAMES.values())
RESULT_VECTORS = ("Flood Extent.geojson", "Flood Polygons (Max Depth).geojson", "Arrival Zones.geojson",
                  "Incremental Depth Polygons.geojson")


def result_files(folder):
    """[(kind, path)] of the dam folder's result files that exist, rasters first, in the order the
    Runner writes them (for 'Add the dam's results to the project')."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for n in RESULT_RASTERS:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            out.append(("raster", p))
    for n in RESULT_VECTORS:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            out.append(("vector", p))
    return out
