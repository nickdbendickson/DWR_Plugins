"""The logic behind the ND Dam Breach Runner popup window - pure Python, no Qt, no QGIS.

The window is a front door to the tools, not a second copy of them: it knows the DAMS FOLDER
(one folder per dam, named after the dam ID - the Runner's own layout) and the dam, shows what
is in that dam's folder, and opens the Processing dialog of any tool with the dam's folder (and
the DEM chosen in the window) filled in. The tools, the wizard and the batch / qgis_process use
are unchanged; every filled-in value can still be changed in the tool's own dialog.
"""
import json
import os

from .core import damjson, workflow

PROVIDER = "ndbreach"
WIZARD = "wizard:"
CHAIN = "chainbatch"        # 3.10.67: opens the Batch chain window, not the Processing dialog

# tab -> tools: (algorithm, button label, tooltip). "wizard:" opens the guided wizard.
TABS = (
    ("Dam", (
        (WIZARD, "Guided setup for one dam (wizard)\u2026",
         "Five pages that fill in 'Run breach model for one dam', with a live run-time estimate."),
        ("rundam", "Run breach model for one dam\u2026",
         "The breach hydrograph, the LISFLOOD-FP flood (and ANUGA if chosen), rasters, products and "
         "the run report, written into <Dams folder>/<Dam ID>."),
        ("batch", "Run breach models for a layer of dams\u2026",
         "One run per dam of a point layer; can hand each result to the hazard classification."),
        (CHAIN, "Batch chain: dams through the whole suite\u2026",
         "For each dam of a list: Screening, the Runner, HEC-RAS 2D (run, wait for a manual run "
         "or skip), the Hazard import and classification, then the next dam."),
        ("estimate", "Estimate the run time\u2026",
         "The plan and the time of a run for this dam, without running it."),
        ("finishanuga", "Finish an ANUGA run after QGIS closed\u2026",
         "ANUGA goes on when QGIS closes (3.12.2). This runs the dam again with the saved "
         "settings and Resume on: it waits for the ANUGA run or uses its result, then writes "
         "the rasters, products and report."),
    )),
    ("Model", (
        ("nested", "Nested fine grid near the dam\u2026", "A fine grid near the dam, a coarse one downstream."),
        ("sensitivity", "Sensitivity of the breach model\u2026",
         "Re-runs the dam with other breach widths, times, Manning n and equations."),
        ("probabilistic", "Probabilistic (Monte Carlo) run\u2026",
         "Runs drawn from parameter distributions you set; 'Write each run's rasters' lets ND Dam "
         "Hazard give a class probability."),
        ("cascade", "Cascade failure of dams in a chain\u2026", "Downstream dams that fail in turn."),
        ("hydrograph", "Breach hydrograph only\u2026", "No flood model: the breach outflow hydrograph."),
        ("rainfallrunoff", "Rainfall to PMF inflow hydrograph\u2026", "A PMF inflow from rainfall."),
        ("spillwayadequacy", "Spillway adequacy\u2026", "Does the dam pass its inflow design flood (no breach)?"),
    )),
    ("Data", (
        ("buildvrt", "Build a statewide LiDAR mosaic (VRT)\u2026", "One virtual raster over DEM tiles."),
        ("fillgaps", "Fill no-data gaps in a DEM\u2026", "Small gaps in a DEM, before modelling."),
        ("nlcdmanning", "Manning's n from NLCD\u2026", "A Manning's n raster from land cover."),
        ("conditiondem", "Preview the DEM conditioning\u2026",
         "What the culvert / channel / pond conditioning does to the DEM around the dam."),
        ("culvertprep", "Culvert prep: corrected copy of the structures layer\u2026",
         "Culvert inverts set to the lowest native fine ground in a new copy (the input is not "
         "changed); changes over the limit are flagged for review, with a change log."),
    )),
    ("Results", (
        ("products", "Flood polygons and EAP map from rasters\u2026", "Polygons, arrival contours, map."),
        ("consequences", "Life safety and damage estimate\u2026", "Incremental life safety and damage."),
        ("ndhazardclass", "Preview the Section 5.2 class\u2026",
         "The Runner's own preview of the ND 5.2 class with the ACER TM 11 charts. DamSafety "
         "Hazard is the classification authority."),
        ("classify", "Hand the results to the hazard classification\u2026",
         "Runs the DamSafety Hazard classification on the dam's folder (DAM_FOLDER only)."),
        ("animation", "Animation of the flood\u2026", "GIF / MP4 / time-enabled layers."),
        ("submittal", "Submittal report (Word)\u2026", "The report with figures, checklist and files."),
        ("benchmark", "Compare with a reference model (HEC-RAS)\u2026", "Rasters against a reference model."),
        ("hecrasexport", "Export to HEC-RAS\u2026", "A HEC-RAS 2D model package, the breach hydrograph and breach table."),
        ("hecrasimport", "Import HEC-RAS results\u2026", "HEC-RAS 2D results as rasters."),
        ("validationrecord", "Validation record (Word)\u2026", "From a benchmark comparison."),
    )),
    ("Checks", (
        ("verifymanifest", "Verify a run manifest\u2026", "Are the inputs and results unchanged?"),
        ("verification", "Verification against analytical solutions\u2026", "Analytic dam-break tests."),
        ("check", "Check the model installation\u2026", "LISFLOOD-FP and ANUGA found and working?"),
        ("selftest", "Self-test in this QGIS\u2026", "A short run of the whole chain in this QGIS."),
    )),
)
ALL_TOOLS = tuple(t for _tab, tools in TABS for t in tools)
TOOL_LABEL = {t[0]: t[1] for t in ALL_TOOLS}

# which parameters carry the dams folder / the dam
WITH_DAM = frozenset({"rundam", "estimate", "sensitivity", "nested", "probabilistic"})   # + DEM
WITH_DAMS_FOLDER = frozenset({"batch", "cascade"})                                       # + DEM
WITH_DAM_FOLDER = frozenset({"products", "consequences", "ndhazardclass", "animation", "benchmark",
                             "hecrasexport", "hecrasimport", "submittal",
                             "finishanuga"})                                            # DAM_FOLDER
WITH_DAM_ID = frozenset({"products", "submittal"})
WITH_DEM = frozenset({"conditiondem", "animation"})

DEFAULT_SETTINGS = {"dams_folder": "", "dam_id": "", "dem": "", "settings_file": "",
                    "grid_m": 10.0, "simh": 12.0, "simmax": 96.0, "force_anuga": False, "length_mi": 4.0, "half_width_mi": 1.0,
                    "reservoir_carve": "off", "hecras_gmsh": False,
                    "hecras_dam_axis": "crest"}
CARVE_CHOICES = ("off", "auto", "on")   # core/reservoir_carve.MODES, the RESERVOIR_CARVE enum
# 3.12.0: the ELEV_DATUM enum (algorithms.ELEV_DATUMS): the source datum of the dam elevations
ELEV_DATUM_CHOICES = ("auto", "NAVD88", "NGVD29", "local")
ELEV_DATUM_TIP = ("The datum of the dam elevations (top of dam, normal / PMF pool, spillway "
                  "rating, breach bottom). The DEM is NAVD88. Auto: the datum dam.json or the "
                  "inventory states; NGVD29 when none is stated (converted to NAVD88 with "
                  "VERTCON at the dam, with a warning). NAVD88: used as given. NGVD29: always "
                  "converted. Local (use DEM): the elevations are on the plans' own "
                  "datum (e.g. top of dam 100.0 ft): the top of dam is read from the DEM and "
                  "the pools, spillway and breach bottom keep their height below it; Auto "
                  "detects this (outside 500 - 3,600 ft, more than 50 ft from the DEM crest, or "
                  "a round 100 ft multiple more than 10 ft from it). The shift is written to "
                  "the run report and dam.json.")
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
                 run_detail(folder) if len(have) == 3 else ("only " + ", ".join(have)) if have
                 else "not run yet"))
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
                 "no per-run rasters: DamSafety Hazard cannot give a class probability"))
    rows.append(("sensitivity", "Sensitivity runs", "yes" if _dir(folder, "Sensitivity") else "no", ""))
    rows.append(("nested", "Nested-grid run", "yes" if _dir(folder, "Nested") else "no", ""))
    rows.append(("preview", "ND 5.2 preview class (Runner's own)",
                 "yes" if _file(folder, workflow.STALE_ON_RERUN[1]) else "no", ""))
    rows.append(("handoff", "Hand-off to the hazard classification",
                 "yes" if _dir(folder, "_classification") else "no", ""))
    rows.append(("animation", "Animation", "yes" if _dir(folder, "Animation") else "no", ""))
    rows.append(("hecras", "HEC-RAS files", "yes" if _dir(folder, "_hecras") else "no", ""))
    rows.append(submittal_row(folder))
    rows.append(package_row(folder))
    ar = anuga_worker_row(folder)
    if ar:
        rows.insert(2, ar)
    return rows


def anuga_worker_row(folder):
    """3.12.2: a detached ANUGA worker (core/anuga_detach.py) that is still running, or that
    finished after its QGIS session ended and has no products yet; None otherwise."""
    try:
        from .core import run_params
        waiting = run_params.pending_workers(folder)
    except Exception:                                    # noqa: BLE001 - only a status line
        return None
    if not waiting:
        return None
    parts = []
    for sub, w in waiting:
        stt = w["status"] or {}
        what = "running (pid {})".format(stt.get("pid")) if w["state"] == "running" else \
            "finished, products not built yet"
        parts.append("{}: {} since {}".format(sub, what, stt.get("started_utc")))
    return ("anuga_worker", "ANUGA run after QGIS closed", "part", "; ".join(parts))


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
    if _file(model, "run_report.json"):          # the run cut and used a clip, then removed it
        return ("terrain", label, "yes", "run finished; model grids not kept")
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
                   "hecrasexport": "hecras", "hecrasimport": "hecras",
                   "finishanuga": "anuga_worker"}                        # 3.12.2


def tool_status(alg, rows):
    """'yes' / 'part' / 'no' for a tool from the dam's status rows; None = no status for it."""
    key = TOOL_STATUS_KEY.get(alg)
    if key is None or not rows:
        return None
    for k, _l, st, _d in rows:
        if k == key:
            return st
    return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def run_detail(folder):
    """One line from _breach_model/run_report.json: no breach / final model / flooded acres, and
    'RESULTS CUT OFF: reason' when the run recorded cut_off. '' when there is no readable report."""
    rep = _read_json(os.path.join(folder or "", workflow.MODEL_FOLDER, "run_report.json"))
    if not rep:
        return ""
    summ = rep.get("summary") if isinstance(rep.get("summary"), dict) else {}
    parts = []
    if summ.get("breach_occurs") is False or summ.get("final_model") == "none":
        parts.append("no breach")
    else:
        if summ.get("final_model"):
            parts.append("final {}".format(summ["final_model"]))
        if summ.get("flooded_acres") is not None:
            try:
                parts.append("{:,.1f} ac flooded".format(float(summ["flooded_acres"])))
            except (TypeError, ValueError):
                pass
    cut = rep.get("cut_off")
    if cut:
        reason = cut.get("reason") if isinstance(cut, dict) else str(cut)
        parts.append("RESULTS CUT OFF: {}".format(reason or "see the run report"))
    return "; ".join(parts)


# DamSafety Hazard's submittal packages (Hazard submittal.py): <dam>/outputs/Submittal/<Dam>_<date>[_n]/
# with MANIFEST.csv (and the same folder as a .zip). A package is stale when a later
# classification or model run wrote one of these files (all relative to the dam folder).
HAZ_OUTPUTS = "outputs"
PACKAGE_DIR = "Submittal"
PACKAGE_MANIFEST = "MANIFEST.csv"
PACKAGE_STALE_ON = (("outputs", "QA", "Run_Settings.json"), ("outputs", "QA", "Last_Run_Snapshot.json"),
                    ("outputs", "Hazard_Classification_Run_Log.txt"),
                    (workflow.MODEL_FOLDER, "run_report.json"))


def _sha256_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


def submittal_packages(folder):
    """[(name, built time, manifest sha256)] of the dam's Hazard submittal packages, newest first.
    A package folder counts by its MANIFEST.csv (build time = its mtime); a .zip whose folder is
    gone counts by the manifest inside it (build time = the zip's mtime). Never raises."""
    import zipfile
    root = os.path.join(folder or "", HAZ_OUTPUTS, PACKAGE_DIR)
    out, seen = [], set()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    for n in names:
        p = os.path.join(root, n)
        m = os.path.join(p, PACKAGE_MANIFEST)
        try:
            if os.path.isdir(p) and os.path.isfile(m):
                with open(m, "rb") as fh:
                    out.append((n, os.path.getmtime(m), _sha256_bytes(fh.read())))
                seen.add(n)
        except OSError:
            continue
    for n in names:
        p = os.path.join(root, n)
        if not n.lower().endswith(".zip") or n[:-4] in seen:
            continue
        try:
            with zipfile.ZipFile(p) as zf:
                ms = sorted((x for x in zf.namelist() if x.replace("\\", "/").split("/")[-1] == PACKAGE_MANIFEST),
                            key=lambda x: x.replace("\\", "/").count("/"))
                if ms:
                    out.append((n, os.path.getmtime(p), _sha256_bytes(zf.read(ms[0]))))
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def package_stale(folder, packages=None):
    """'' or the name of the run file that is newer than the newest package."""
    pk = submittal_packages(folder) if packages is None else packages
    if not pk:
        return ""
    newest = None
    for parts in PACKAGE_STALE_ON:
        f = os.path.join(folder, *parts)
        try:
            t = os.path.getmtime(f)
        except OSError:
            continue
        if t > pk[0][1] and (newest is None or t > newest[0]):
            newest = (t, "/".join(parts))
    return newest[1] if newest else ""


def package_row(folder):
    """('package', 'Submittal package (DamSafety Hazard)', state, detail): 'part' = older than the
    last classification / model run."""
    label = "Submittal package (DamSafety Hazard)"
    pk = submittal_packages(folder)
    if not pk:
        return ("package", label, "no", "")
    import time
    detail = "{} ({})".format(pk[0][0], time.strftime("%Y-%m-%d", time.localtime(pk[0][1])))
    stale = package_stale(folder, pk)
    if stale:
        return ("package", label, "part", detail + " - older than {}; rebuild it".format(stale))
    return ("package", label, "yes", detail)


def package_unreviewed(folder, record=None):
    """True when dam.json's newest 'reviewed' status records a package_sha256 (Hazard
    markreviewed.py) that is not the newest package's MANIFEST.csv SHA-256: the package the
    reviewer saw is not the newest one. False when there is no review, no sha or no package."""
    if record is None:
        try:
            from .damsuite import dam_record as DR
            record = DR.load(folder)
        except Exception:                                        # noqa
            return False
    if record is None:
        return False
    try:
        rev = record.latest("reviewed")
    except Exception:                                            # noqa
        return False
    res = rev.get("result") if isinstance(rev, dict) and isinstance(rev.get("result"), dict) else {}
    sha = str(res.get("package_sha256") or "").strip().lower()
    if not sha:
        return False
    pk = submittal_packages(folder)
    return bool(pk) and pk[0][2] != sha


def package_warnings(folder, record=None):
    """[warning lines] about the dam's Hazard submittal package, for the dam card."""
    out = []
    pk = submittal_packages(folder)
    if not pk:
        return out
    stale = package_stale(folder, pk)
    if stale:
        out.append("The newest submittal package ({}) is older than {}: rebuild it in DamSafety "
                   "Hazard so it holds the current results.".format(pk[0][0], stale))
    if package_unreviewed(folder, record):
        out.append("The review recorded in dam.json is of a different package than the newest one "
                   "({}): the newest package has not been reviewed.".format(pk[0][0]))
    return out


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
    prod = [st.get(k) for k in ("products", "handoff", "submittal", "package")]
    products = "yes" if st.get("products") == "yes" and st.get("handoff") == "yes" else \
        "part" if any(v in ("yes", "part") for v in prod) else "no"
    return {"dam": dam, "terrain": terrain, "run": run, "products": products}


STAGE_TIPS = {
    ("dam", "yes"): "the dam folder and its dam.json record exist",
    ("dam", "part"): "dam.json not written yet - the dam folder (or only the dam ID) is set; "
                     "the first run, Screening tool 7b or DamSafety Hazard writes it",
    ("dam", "no"): "set the Dams folder and the dam ID",
    ("terrain", "yes"): "the model grids / terrain of the last run are in the dam folder",
    ("terrain", "part"): "a DEM is chosen; the run cuts the terrain from it",
    ("terrain", "no"): "no DEM chosen and no terrain in the dam folder yet",
    ("run", "yes"): "a finished model run is in the dam folder",
    ("run", "part"): "a model run is there but incomplete - see the Flood run chip",
    ("run", "no"): "no model run yet",
    ("products", "yes"): "the products and the hand-off to DamSafety Hazard are made",
    ("products", "part"): "some products are made - see the chips",
    ("products", "no"): "no products yet",
}


def stage_tip(key, state, has_folder=False, has_dam_json=False):
    """The tooltip of one stage chip: '<Stage>: <state> - <why>'."""
    return "{}: {} - {}".format(STAGE_LABELS.get(key, key), state,
                                STAGE_TIPS.get((key, state), ""))


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
    if st.get("anuga_worker") == "part":                                 # 3.12.2
        return "finishanuga", ("An ANUGA run went on after QGIS closed: finish it (same "
                               "settings, Resume on) to build the rasters, products and report.")
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
        carve = str(S.get("reservoir_carve") or "off").strip().lower()
        if carve in CARVE_CHOICES and carve != "off":                    # v3.10.20
            p["RESERVOIR_CARVE"] = CARVE_CHOICES.index(carve)
        if S.get("hecras_gmsh"):                                         # v3.10.22
            p["HECRAS_GMSH"] = True
        if str(S.get("hecras_dam_axis") or "crest").strip().lower() == "straight":
            p["HECRAS_CREST"] = False                                    # crest dam line
    if alg in ("rundam", "batch"):                                       # 3.12.0
        dm = str(S.get("elev_datum") or "auto").strip()
        if dm in ELEV_DATUM_CHOICES and dm != "auto":
            p["ELEV_DATUM"] = ELEV_DATUM_CHOICES.index(dm)
    return p


def datum_info(folder, mode="auto"):
    """3.12.0: one line for the Runner window: the datum of the dam folder's elevations as a
    run would take it (dam.json's top_of_dam_datum, the ELEV_DATUM choice) and the NGVD29 ->
    NAVD88 shift at the dam when dam.json has its location."""
    try:
        from .damsuite import vdatum as VD
        from .core import vdatum_run as vdrun
    except Exception as e:                                       # noqa
        return "Elevation datum: not available ({})".format(e)
    rec = None
    if folder and os.path.isdir(folder):
        try:
            from .damsuite import dam_record as DR
            rec = DR.load(folder)
        except Exception:                                        # noqa
            rec = None
    stated, _note = vdrun.stated_from_record(rec)
    src, how = VD.resolve(stated, mode)
    text = {"override": "set here", "stated": "stated in dam.json ({!r})".format(stated),
            "assumed": "not stated: NGVD29 assumed"}.get(how, how)
    line = "Elevation datum: {} ({})".format(src, text)
    if src == VD.NGVD29:
        lon = rec.get("lon") if rec is not None else None
        lat = rec.get("lat") if rec is not None else None
        sm, _m = VD.shift_m(lon, lat) if lon is not None and lat is not None else (None, "")
        line += "; converted to NAVD88 ({})".format(
            "{:+.2f} ft at the dam".format(sm / VD.FT) if sm is not None else
            "the shift is taken at the dam point when it runs")
    elif src == VD.NAVD88:
        line += "; used as given (the DEM is NAVD88)"
    else:
        line += "; local datum: the top of dam is taken from the DEM when it runs"
    # 3.12.5 (damsuite 1.18.2, Nick 2026-10-28 / 10-29): 0 - 500 ft is always local and is
    # converted to NAVD88 by the DEM tie (no override wording)
    band = None
    if rec is not None and hasattr(VD, "local_band_hit"):
        band = VD.local_band_hit({k: rec.get(k) for k in
                                  ("top_of_dam_ft", "normal_pool_ft", "pmf_max_wse_ft")})
    if band:
        return ("Elevation datum: local (0 - 500 ft rule: {} {:.2f} ft); never "
                "datum-converted: converted to NAVD88 by the DEM tie when it runs - the top of "
                "dam taken from the DEM at mid-crest, the other local elevations DEM-derived (no tie, or "
                "a tied value more than 100 ft from the DEM crest: flagged for review, left "
                "unconverted)").format(
                    band[0].replace("_ft", "").replace("_", " "), band[1])
    if src != VD.LOCAL and rec is not None and VD.mode(mode) == VD.AUTO:
        hit, why = VD.local_check(rec.get("top_of_dam_ft"))
        if hit:
            line += ". Looks local ({}): the run will take the top from the DEM".format(why)
    return line


# v3.10.22: tooltip of the Runner's Advanced checkbox (and the HECRAS_GMSH parameter's help)
HECRAS_GMSH_TIP = ("Optional, off by default. Uses gmsh to place the HEC-RAS 2D computation "
                   "points, finer along the dam, breach and channel. Needs gmsh (pip install "
                   "gmsh, in QGIS's Python or NDBREACH_GMSH_PYTHON). Without gmsh, the regular "
                   "grid is used and the log says so. Only for Models with HEC-RAS 2D.")


# tooltip of the Runner's Advanced 'HEC-RAS dam line follows the crest' checkbox (and the
# HECRAS_CREST parameter's help)
HECRAS_CREST_TIP = ("On by default (recommended). HEC-RAS needs a line where the reservoir "
                    "meets the flood area: the dam. With this on, that line follows the top of "
                    "the embankment as the ground model (DEM) shows it, bends and all, and the "
                    "breach is cut straight through the dam there. Off: a straight line through "
                    "the dam point, square to the flow, as before; use it only to repeat an "
                    "older model. If the top of the dam cannot be found, the straight line is "
                    "used and the log says so. Only for Models with HEC-RAS 2D.")


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
