"""Runner 3.10.67: the batch chain - a list of dams, each through the whole suite in turn.

For each dam: Screening (that dam only) -> Runner -> HEC-RAS 2D build -> HEC-RAS run ->
Hazard import (ndhazard:hecrasplan) -> Hazard classify (ND + TM11) and its saved Hazard state;
then the next dam. A dam that fails never stops the list (unless 'keep going' is off).

This module has no QGIS: the stages are run by an `ops` object (algorithms_chain.QgisOps in
QGIS, a fake in the tests) and everything else - settings, overrides, the plan of stages,
resume, the summary files - is here.

Settings: ONE suite settings file (damsuite.settings format, plugin|tool|key|value rows).
  * the chain's own choices are the rows ("ndbreach", "chainbatch", KEY), KEYS below;
  * each stage's tool takes its rows: ("ndscreen", "window", ...) for Screening,
    ("ndbreach", "batch", ...), ("ndbreach", "hecrasexport", ...), ("ndhazard", "hecrasplan",
    ...), ("ndhazard", "classify", ...). They are passed to the tools as EXPLICIT parameters
    (a tool's own default for a key the file does not set), so the run does not depend on
    which settings file a plugin window chose in this QGIS session.
  * an overrides CSV changes values for single dams: a dam_id column, then one column per
    changed value, named 'plugin.tool.KEY' or a chain KEY ('HEC_MODE'); blank = the default.
Each dam gets <dam>/_chain/chain_state.json (stage status, outputs, fingerprints; the resume
record) and <dam>/_chain/chain_settings_used.json (the values used, the overrides applied,
software versions, input checksums). The batch writes Chain_Batch_Summary.csv (after each dam)
and chain_manifest.json into the output folder.

HEC-RAS stage (HEC_MODE): run / wait (for a manual run of the built package) / skip (the
Runner's LISFLOOD-FP or ANUGA rasters go straight to the Hazard classification).
"""
import csv
import datetime
import glob
import hashlib
import json
import os
import re
import time

from ..damsuite import settings as DS

PLUGIN, TOOL = "ndbreach", "chainbatch"
STAGES = ("screening", "runner", "hec_build", "hec_run", "hazard_import", "classify")
STAGE_LABELS = {"screening": "Screening", "runner": "Runner", "hec_build": "HEC-RAS build",
                "hec_run": "HEC-RAS run", "hazard_import": "Hazard import",
                "classify": "Hazard classify"}
ALGS = {"screening": "nddam:runchain", "runner": "ndbreach:batch",
        "hec_build": "ndbreach:hecrasexport", "hazard_import": "ndhazard:hecrasplan",
        "classify": "ndhazard:classify", "save_state": "ndhazard:savedamstate"}
# the settings rows each stage's tool takes
STAGE_TOOLS = {"screening": ("ndscreen", "window"), "runner": ("ndbreach", "batch"),
               "hec_build": ("ndbreach", "hecrasexport"),
               "hazard_import": ("ndhazard", "hecrasplan"), "classify": ("ndhazard", "classify")}
# an output a done stage must still have on disk to be kept on a resume
REQUIRED_OUTPUT = {"screening": "work", "runner": "depth", "hec_build": "package",
                   "hec_run": "plan_hdf", "hazard_import": "depth", "classify": "summary"}

HEC_MODES = ("run", "wait", "skip")
HEC_MODE_LABELS = ("Run HEC-RAS 2D", "Wait for a manual HEC-RAS run",
                   "Skip HEC-RAS (Runner rasters straight to Hazard)")
RUNNER_MODELS = ("LISFLOOD-FP only", "LISFLOOD-FP then ANUGA")
STRUCTURE_CHOICES = ("Automatic (an existing ANUGA run's culverts and bridges, else the "
                     "Runner's culvert inventory)", "The Runner's culvert inventory only")
STOP_CHOICES = ("The end (Hazard classify)",) + tuple(
    "After " + STAGE_LABELS[s] for s in STAGES[:-1])
STOP_STAGES = (None,) + STAGES[:-1]

FINAL_RASTERS = {"depth": "Depth (Max).tif", "velocity": "Velocity (Max).tif",
                 "arrival": "Arrival Time.tif", "dv": "D x V (Max).tif"}   # = workflow.FINAL_NAMES
SUMMARY_NAME = "Chain_Batch_Summary.csv"
MANIFEST_NAME = "chain_manifest.json"
CHAIN_DIR = "_chain"
STATE_NAME = "chain_state.json"
USED_NAME = "chain_settings_used.json"
TODO_NAME = "CHAIN_HECRAS_TODO.txt"
RESULTS_DROP = "hecras_results"         # <dam>/_chain/hecras_results: a finished plan HDF
DEPTH_BASIS_ENV = "NDBREACH_HECRAS_DEPTH_BASIS"   # = core/hecras.DEPTH_BASIS_ENV (no numpy here)
FULL_HASH_LIMIT = 256 * 1024 ** 2        # larger inputs are recorded by size and time


class Key(object):
    __slots__ = ("key", "kind", "default", "options", "label", "help")

    def __init__(self, key, kind, default, label, help="", options=()):
        self.key, self.kind, self.default, self.label = key, kind, default, label
        self.help, self.options = help, tuple(options)


KEYS = (
    Key("OUT_FOLDER", "text", "", "Output folder (one folder per dam)",
        "Each dam gets <output folder>/<dam ID>; the summary files are written here."),
    Key("DEM", "text", "", "DEM (a raster or VRT covering every dam)"),
    Key("ID_FIELD", "text", "", "Dam ID field of the dams layer (empty = match by name)"),
    Key("SCREENING", "bool", True, "Run the Screening steps for each dam first"),
    Key("SCREEN_WORK", "text", "", "Screening work folder (the statewide run with its data "
        "package)", "Each dam is screened in <work>/one_dam/<dam ID>."),
    Key("SCREEN_INCLUDE", "text", "", "Optional Screening steps to include (e.g. '6, 9')"),
    Key("SCREEN_PREFLIGHT", "bool", True, "Screening preflight before each dam"),
    Key("RUNNER_MODELS", "enum", 0, "Runner models", options=RUNNER_MODELS),
    Key("HEC_MODE", "enum", 0, "HEC-RAS 2D stage", options=HEC_MODE_LABELS),
    Key("HEC_WAIT_MIN", "number", 0.0, "Wait for a manual HEC-RAS run (minutes; 0 = do not "
        "wait, the next run of the chain picks the results up)"),
    Key("HEC_STRUCTURES", "enum", 0, "Culverts and bridges in the HEC-RAS model",
        options=STRUCTURE_CHOICES),
    Key("ANUGA_RUN", "text", "", "Folder of an existing ANUGA run of the dam (per-dam "
        "override; empty = look in the dam folder)"),
    Key("HEC_PLAN_HDF", "text", "", "A finished HEC-RAS plan HDF to use (per-dam override)"),
    Key("HEC_TERRAIN", "text", "", "HEC-RAS terrain GeoTIFF (empty = the package's Terrain "
        "folder)", "3.10.84: used only for the per-pixel depth basis (the default "
        "again since 3.10.92; a Linux HEC-RAS stage on that basis gives Hazard its own "
        "rasters on the run's fine DEM unless this is set); the cell-minimum depth "
        "(NDBREACH_HECRAS_DEPTH_BASIS=cell_min) needs no terrain."),
    Key("HAZARD_PROFILE", "text", "", "Hazard settings profile (.ndprofile.json) for the "
        "saved Hazard window state"),
    Key("STOP_AFTER", "enum", 0, "Stop each dam at", options=STOP_CHOICES),
    Key("SKIP_DONE", "bool", True, "Skip stages already done with the same settings (resume)"),
    Key("KEEP_GOING", "bool", True, "Keep going with the next dam when one fails"),
)
KEY_INDEX = {k.key: k for k in KEYS}
_SETTING = {k.key: DS.Setting(PLUGIN, TOOL, k.key, k.label, k.kind, k.default, options=k.options)
            for k in KEYS}


class ChainError(Exception):
    """A stage failed; the message says why (and what to do)."""


class Waiting(Exception):
    """The HEC-RAS run is not there yet (manual run); the dam stops here, resumable."""


class Cancelled(Exception):
    pass


def now():
    return datetime.datetime.now().replace(microsecond=0).isoformat()


# ------------------------------------------------------------------ names --
def safe_name(s):
    """The dam folder name the Runner's batch uses (algorithms._safe_name)."""
    return re.sub(r"[^\w\- ]+", "_", str(s)).strip() or "dam"


def norm_id(s):
    return str(s if s is not None else "").strip()


def canon_id(s):
    """A dam ID in one canonical text form, so a layer feature, an IDs list and an overrides CSV
    all match: NULL/'' -> '', and an integer-valued number loses its decimals ('123.0' -> '123')."""
    t = norm_id(s)
    if t.upper() == "NULL":
        return ""
    try:
        f = float(t)
        if f == int(f):
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return t


def parse_ids(text):
    """Dam IDs typed in a box: separated by commas, semicolons, spaces or new lines; order
    kept, duplicates dropped."""
    out = []
    for p in re.split(r"[,;\s]+", str(text or "")):
        p = p.strip().strip("'\"")
        if p and p not in out:
            out.append(p)
    return out


_ID_COLUMNS = ("dam_id", "damid", "dam id", "id", "nid", "nidid", "dam")


def _id_column(header):
    low = [str(h or "").strip().lower() for h in header]
    for name in _ID_COLUMNS:
        if name in low:
            return low.index(name)
    return None


def read_ids_csv(path):
    """Dam IDs from a CSV: its dam_id / id / nid / dam column, else the first column."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and any(c.strip() for c in r)]
    if not rows:
        return []
    col = _id_column(rows[0])
    body = rows[1:] if col is not None else rows
    col = col or 0
    out = []
    for r in body:
        v = r[col].strip() if col < len(r) else ""
        if v and v not in out:
            out.append(v)
    return out


# --------------------------------------------------------------- settings --
def chain_values(values):
    """{KEY: typed value} of the chain's own rows (a key the file does not set -> default);
    returns (values, problems)."""
    out, problems = {}, []
    for k in KEYS:
        raw = values.get((PLUGIN, TOOL, k.key))
        v, p = DS.coerce(raw, _SETTING[k.key])
        if p:
            problems.append("{}: {}".format(k.key, p))
        out[k.key] = k.default if v is None else v
    return out, problems


def hec_mode(cv):
    i = int(cv.get("HEC_MODE") or 0)
    return HEC_MODES[i] if 0 <= i < len(HEC_MODES) else "run"


def key_of_column(col):
    """An overrides column -> (plugin, tool, key) or None. 'plugin.tool.KEY' (also with / or
    :), a chain KEY ('HEC_MODE', any case) or 'chainbatch.KEY'."""
    c = str(col or "").strip()
    if not c:
        return None
    if c.upper() in KEY_INDEX:
        return (PLUGIN, TOOL, c.upper())
    parts = [p.strip() for p in re.split(r"[./:|]", c) if p.strip()]
    if len(parts) == 3:
        p, t, k = parts
        return (p.lower(), t.lower(), k.upper())
    if len(parts) == 2 and parts[0].lower() == TOOL and parts[1].upper() in KEY_INDEX:
        return (PLUGIN, TOOL, parts[1].upper())
    return None


def read_overrides(path):
    """{dam ID: {(plugin, tool, key): value}} from an overrides CSV, and [problems]. Blank
    cells are the batch value; a column whose name is not a key is a problem (not used)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and any(c.strip() for c in r)]
    if not rows:
        return {}, []
    header = rows[0]
    col = _id_column(header)
    if col is None:
        return {}, ["{}: no dam_id column".format(os.path.basename(path))]
    problems, keys = [], {}
    for i, h in enumerate(header):
        if i == col or not str(h).strip():
            continue
        k = key_of_column(h)
        if k is None:
            problems.append("overrides column '{}' is not a setting (use plugin.tool.KEY, e.g. "
                            "ndbreach.batch.METHOD, or a chain key such as HEC_MODE)".format(h))
        else:
            keys[i] = k
    out = {}
    for r in rows[1:]:
        raw = r[col].strip() if col < len(r) else ""
        dam = canon_id(raw)
        if not dam:
            continue
        if dam in out:
            problems.append("dam {} is in the overrides twice; the first row is used".format(raw))
            continue
        out[dam] = {k: r[i].strip() for i, k in keys.items()
                    if i < len(r) and r[i].strip() != ""}
    return out, problems


def resolve_for_dam(values, overrides):
    """The batch values with one dam's overrides on top: (values, applied {key: value})."""
    v = dict(values)
    applied = {}
    for k, val in (overrides or {}).items():
        if val is None or val == "":
            continue
        v[k] = val
        applied["{}.{}.{}".format(*k)] = val
    return v, applied


def params_from_settings(values, plugin, tool, defs, skip=()):
    """Explicit parameters of one tool from the settings values: every scalar parameter of
    `defs` (suite_settings_tools.param_defs of a FRESH tool: its own defaults) gets the file's
    value, else its default. Returns (params, problems)."""
    params, problems = {}, []
    cat = DS.catalog_from_definitions(plugin, tool, defs, skip=skip)
    for s in cat:
        raw = values.get(s.id)
        v, p = DS.coerce(raw, s)
        if p:
            problems.append("{}.{}.{}: {} (the default is used)".format(plugin, tool, s.key, p))
        if v is None:
            v = s.default
        if v is None:
            continue
        params[s.key] = v
    return params, problems


def window_rows(values, plugin):
    return {k: v for (p, t, k), v in values.items() if p == plugin and t == "window"}


# ------------------------------------------------------------- checksums --
def file_digest(path):
    """sha256 of a file (size:mtime for one above FULL_HASH_LIMIT); '' when missing."""
    try:
        st = os.stat(path)
    except OSError:
        return ""
    if os.path.isdir(path):
        return "folder"
    if st.st_size > FULL_HASH_LIMIT:
        return "size:{}:mtime:{}".format(st.st_size, int(st.st_mtime))
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)


def fingerprint(obj):
    return hashlib.sha256(json.dumps(_jsonable(obj), sort_keys=True).encode("utf-8")
                          ).hexdigest()[:16]


def stage_inputs(stage, values, cv):
    """What a stage's result depends on: its tool's rows and the chain choices it uses."""
    out = {}
    pt = STAGE_TOOLS.get(stage)
    if pt:
        out["rows"] = {k: v for (p, t, k), v in sorted(values.items()) if (p, t) == pt}
    used = {"screening": ("SCREEN_WORK", "SCREEN_INCLUDE"),
            "runner": ("DEM", "ID_FIELD", "RUNNER_MODELS", "HEC_MODE"),
            "hec_build": ("HEC_STRUCTURES", "ANUGA_RUN"),
            "hec_run": ("HEC_MODE", "HEC_PLAN_HDF"),
            "hazard_import": ("HEC_TERRAIN",),
            "classify": ("HEC_MODE", "HAZARD_PROFILE")}.get(stage, ())
    out["chain"] = {k: cv.get(k) for k in used}
    if stage == "hazard_import":
        # 3.10.84: the import's depth basis (algorithms_chain.import_depth_basis); a resume
        # imports again when it changes - and once after 3.10.84, whose default is the cell
        # minimum (the import of 3.10.83 and earlier always used the terrain)
        out["depth_basis_env"] = str(os.environ.get(DEPTH_BASIS_ENV) or "").strip().lower()
    return out


# ---------------------------------------------------------------- plan ----
def effective_hec_mode(cv, engine):
    """(mode, note): 'run_linux' (the Runner's own HEC-RAS 2D stage, Models = LISFLOOD-FP then
    HEC-RAS), 'run_windows' (the Windows launcher), 'wait' or 'skip'. `engine` is
    {available, platform, windows_launcher}."""
    m = hec_mode(cv)
    if m != "run":
        return m, ""
    engine = engine or {}
    plat = str(engine.get("platform") or "")
    if plat.startswith("win") and engine.get("windows_launcher"):
        return "run_windows", ""
    if engine.get("available") and not plat.startswith("win") and plat != "darwin":
        return "run_linux", ""
    why = engine.get("reason") or "HEC-RAS 2D cannot be run from here"
    return "wait", ("HEC-RAS stage 'Run' is not available here ({}); the chain builds the "
                    "model and waits for a manual run instead".format(why))


def plan_stages(cv, mode):
    """The stages of one dam, in order, for the effective HEC mode."""
    st = []
    if cv.get("SCREENING"):
        st.append("screening")
    st.append("runner")
    if mode == "run_linux":
        st += ["hec_run", "hazard_import"]
    elif mode in ("wait", "run_windows"):
        st += ["hec_build", "hec_run", "hazard_import"]
    st.append("classify")
    i = int(cv.get("STOP_AFTER") or 0)
    stop = STOP_STAGES[i] if 0 <= i < len(STOP_STAGES) else None
    if stop:
        if stop in st:
            st = st[:st.index(stop) + 1]
        else:      # a stage this plan has not got (e.g. HEC with Skip): stop at the last before
            order = list(STAGES)
            st = [s for s in st if order.index(s) <= order.index(stop)]
    return st


# --------------------------------------------------------------- HEC files --
def _is_partial(path):
    b = os.path.basename(path).lower()
    return any(t in b for t in (".partial", ".tmp", "~", ".lock"))


def plan_hdfs(folders):
    """Plan HDFs (*.p01.hdf ... *.p99.hdf) in the folders (and one level below)."""
    out = []
    for d in folders:
        if not d or not os.path.isdir(d):
            continue
        for pat in ("*.p[0-9][0-9].hdf", os.path.join("*", "*.p[0-9][0-9].hdf")):
            for p in glob.glob(os.path.join(d, pat)):
                if not _is_partial(p) and p not in out:
                    out.append(p)
    return sorted(out, key=lambda p: os.path.getmtime(p), reverse=True)


def plan_has_results(path):
    """True / False when h5py can tell (an unsteady 'Results' group), None when it cannot."""
    try:
        import h5py
    except ImportError:
        return None
    try:
        with h5py.File(path, "r") as f:
            return "Results" in f and "Unsteady" in f["Results"]
    except Exception:                                            # noqa - a file still written
        return False


def find_finished_plan(folders, since=0.0, has_results=None):
    """The newest plan HDF newer than `since` that has results (or cannot be checked)."""
    has_results = has_results or plan_has_results
    for p in plan_hdfs(folders):
        try:
            if os.path.getmtime(p) + 1 < since or os.path.getsize(p) <= 0:
                continue
        except OSError:
            continue
        if has_results(p) is not False:
            return p
    return None


def wait_for_plan(folders, minutes, since=0.0, poll_s=30.0, sleep=time.sleep, clock=time.time,
                  cancelled=lambda: False, log=None, has_results=None):
    """Wait up to `minutes` for a finished plan HDF whose size is the same on two polls.
    Returns its path or None (minutes 0 = look once). A verified result (h5py says it has an
    unsteady Results group) is taken at once when minutes <= 0; a result h5py cannot verify
    (plan_has_results -> None, the usual case on a HEC-RAS box without h5py) must still prove
    its size is stable over two polls - even when minutes <= 0 - so a plan a manual run is
    still writing is not read truncated and handed to Hazard as finished."""
    log = log or (lambda m: None)
    check = has_results or plan_has_results
    end = clock() + max(0.0, float(minutes or 0)) * 60.0
    last = {}
    unstable_left = 2          # bounded re-polls to prove a one-shot (minutes<=0) unverified result
    while True:
        p = find_finished_plan(folders, since, check)
        if p:
            size = os.path.getsize(p)
            if (check(p) is True and minutes <= 0) or last.get(p) == size:
                return p
            last = {p: size}
            if minutes <= 0:                 # unverified: re-poll a bounded number of times
                if unstable_left <= 0:
                    return None
                unstable_left -= 1
                if clock() >= end:
                    end = clock() + poll_s
        if clock() >= end:
            return None
        if cancelled():
            raise Cancelled()
        log("waiting for the HEC-RAS results\u2026")
        sleep(poll_s)


def find_terrain(plan_hdf, extra=()):
    """The HEC-RAS terrain GeoTIFF next to a plan: <plan dir>/Terrain/*.tif, up to two folders
    up, or in the `extra` folders."""
    dirs = []
    d = os.path.dirname(os.path.abspath(plan_hdf)) if plan_hdf else ""
    for _i in range(3):
        if d:
            dirs.append(d)
            d = os.path.dirname(d)
    dirs += [x for x in extra if x]
    for d in dirs:
        tifs = sorted(glob.glob(os.path.join(d, "Terrain", "*.tif")))
        tifs = [t for t in tifs if not t.lower().endswith((".vrt.tif",))]
        if tifs:
            return tifs[0]
    return None


def is_anuga_run(folder):
    """True when the folder (a dam folder or its _breach_model) holds a finished ANUGA run."""
    if not folder or not os.path.isdir(folder):
        return False
    for bm in (folder, os.path.join(folder, "_breach_model")):
        if os.path.isfile(os.path.join(bm, "anuga", "anuga_job.npz")):
            return True
        rp = os.path.join(bm, "run_report.json")
        try:
            with open(rp, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError):
            continue
        if str((rep.get("summary") or {}).get("final_model", "")).upper() == "ANUGA":
            return True
    return False


def structures_source(cv, dam_folder):
    """(STRUCTURES_FROM folder or '', note). Never launches ANUGA: an existing run only."""
    if int(cv.get("HEC_STRUCTURES") or 0) == 1:
        return "", "culverts and bridges: the Runner's culvert inventory"
    run = str(cv.get("ANUGA_RUN") or "").strip()
    if run:
        if is_anuga_run(run):
            return run, "culverts and bridges: the ANUGA run in " + run
        return "", ("ANUGA_RUN {} is not a finished ANUGA run; the Runner's culvert inventory "
                    "is used".format(run))
    if is_anuga_run(dam_folder):
        return "", "culverts and bridges: this dam's own ANUGA run"
    return "", "culverts and bridges: the Runner's culvert inventory (no ANUGA run)"


def hecras_todo_text(dam_id, package, drop):
    return (
        "HEC-RAS 2D run needed for dam {dam}\n\n"
        "The chain built the HEC-RAS model and is waiting for its results.\n"
        "1. Copy (or share) this folder to the Windows computer with HEC-RAS 6.6:\n"
        "   {pkg}\n"
        "2. Open the .prj in HEC-RAS, open RAS Mapper once so it makes the terrain, then run\n"
        "   the unsteady plan (Run > Unsteady Flow Analysis > Compute).\n"
        "3. Put the finished plan file (<name>.p01.hdf) back into that folder, or into\n"
        "   {drop}\n"
        "4. Run the batch chain again (with 'Skip stages already done' on): this dam continues\n"
        "   with the Hazard import and classification.\n").format(dam=dam_id, pkg=package,
                                                               drop=drop)


# ----------------------------------------------------------------- state ----
def chain_dir(dam_folder):
    return os.path.join(dam_folder, CHAIN_DIR)


def load_state(dam_folder):
    try:
        with open(os.path.join(chain_dir(dam_folder), STATE_NAME), encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_jsonable(obj), f, indent=1)
    os.replace(tmp, path)


def stage_kept(rec, fp):
    """A recorded stage that can be kept: done, same fingerprint, its output still there."""
    if not isinstance(rec, dict) or rec.get("status") != "done" or rec.get("fingerprint") != fp:
        return False
    out = rec.get("outputs") or {}
    req = out.get(REQUIRED_OUTPUT.get(rec.get("stage"), ""), None)
    return bool(req) and os.path.exists(str(req))


# --------------------------------------------------------------- one dam ----
class Dam(object):
    """One dam of the batch: its ID, folder, resolved values and the run's state."""

    def __init__(self, dam_id, folder, values, cv, applied=None, feature=None, mode="skip",
                 mode_note=""):
        self.dam_id, self.folder, self.values, self.cv = dam_id, folder, values, cv
        self.applied = applied or {}
        self.feature = feature
        self.mode, self.mode_note = mode, mode_note
        self.outputs = {}          # stage -> outputs (also from kept stages)
        self.notes = []


SUMMARY_FIELDS = ("Dam", "Folder", "Status", "Last_Stage", "Failed_Stage", "Message",
                  "HEC_Mode", "ND_Class", "TM11_Class", "Hazard_Project", "Minutes") + tuple(
    "Stage_" + s for s in STAGES)


def run_dam(dam, ops, skip_done=True, log=None, cancelled=lambda: False):
    """Run one dam's stages. Returns its summary row. ops.run(stage, dam) -> outputs dict
    (raises ChainError / Waiting / Cancelled)."""
    log = log or (lambda m: None)
    t0 = time.time()
    stages = plan_stages(dam.cv, dam.mode)
    state = load_state(dam.folder)
    if state.get("dam") and state.get("dam") != dam.dam_id:
        state = {}             # a different dam's state in this folder (safe_name collision): drop it
    recs = dict(state.get("stages") or {})
    row = dict.fromkeys(SUMMARY_FIELDS, "")
    row.update({"Dam": dam.dam_id, "Folder": dam.folder, "HEC_Mode": dam.mode})
    for s in STAGES:
        row["Stage_" + s] = "not in plan" if s not in stages else ""
    rerun = False
    status = "done"

    def write_state():
        save_json(os.path.join(chain_dir(dam.folder), STATE_NAME),
                  {"dam": dam.dam_id, "updated": now(), "hec_mode": dam.mode,
                   "stages": recs, "plan": stages})

    for stage in stages:
        if cancelled():
            status = "cancelled"
            break
        fp = fingerprint(stage_inputs(stage, dam.values, dam.cv))
        rec = recs.get(stage)
        if skip_done and not rerun and stage_kept(rec, fp):
            dam.outputs[stage] = rec.get("outputs") or {}
            row["Stage_" + stage] = "kept"
            row["Last_Stage"] = stage
            log("{}: {} already done - kept".format(dam.dam_id, STAGE_LABELS[stage]))
            continue
        rerun = True             # every later stage runs again
        log("{}: {}\u2026".format(dam.dam_id, STAGE_LABELS[stage]))
        st0 = time.time()
        rec = {"stage": stage, "status": "running", "started": now(), "fingerprint": fp,
               "outputs": {}, "message": ""}
        recs[stage] = rec
        try:
            out = ops.run(stage, dam) or {}
            rec.update(status="done", outputs=out)
            dam.outputs[stage] = out
            row["Stage_" + stage] = "done"
            row["Last_Stage"] = stage
            if out.get("message"):
                dam.notes.append(out["message"])
        except Waiting as e:
            rec.update(status="waiting", message=str(e))
            row["Stage_" + stage] = "waiting"
            status = "waiting for HEC-RAS"
            row["Message"] = str(e)
        except Cancelled:
            rec.update(status="cancelled")
            row["Stage_" + stage] = "cancelled"
            status = "cancelled"
        except Exception as e:                                   # noqa - next dam
            msg = str(e) if isinstance(e, ChainError) else "{}: {}".format(type(e).__name__, e)
            rec.update(status="failed", message=msg[:2000])
            row["Stage_" + stage] = "failed"
            row["Failed_Stage"] = stage
            row["Message"] = msg[:500]
            status = "failed"
            log("{}: {} failed: {}".format(dam.dam_id, STAGE_LABELS[stage], msg))
        rec["finished"] = now()
        rec["minutes"] = round((time.time() - st0) / 60.0, 2)
        write_state()
        if status != "done":
            break
    else:
        if stages and stages[-1] != "classify":
            status = "stopped after " + STAGE_LABELS[stages[-1]]
    for s in stages[stages.index(row["Last_Stage"]) + 1 if row["Last_Stage"] in stages else 0:]:
        if not row["Stage_" + s]:
            row["Stage_" + s] = "not run"
    write_state()
    cl = dam.outputs.get("classify") or {}
    row.update({"Status": status, "ND_Class": cl.get("nd_class", ""),
                "TM11_Class": cl.get("tm11_class", ""),
                "Hazard_Project": cl.get("project", ""),
                "Minutes": round((time.time() - t0) / 60.0, 1)})
    if status == "done" and not row["Message"]:
        row["Message"] = " | ".join(n for n in ([dam.mode_note] + dam.notes) if n)[:500]
    return row


# ----------------------------------------------------------------- batch ----
def write_summary(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def settings_used(dam, versions, files, settings_path=""):
    """The per-dam record of what the chain used."""
    vals = {"{}.{}.{}".format(*k): v for k, v in sorted(dam.values.items())}
    return {"format": "ND_Dam_Suite/chain_settings_used", "format_version": 1,
            "dam": dam.dam_id, "folder": dam.folder, "written": now(),
            "settings_file": settings_path, "settings_file_checksum":
                file_digest(settings_path) if settings_path else "",
            "hec_mode": dam.mode, "hec_mode_note": dam.mode_note,
            "chain": dam.cv, "overrides_applied": dam.applied, "values": vals,
            "versions": versions or {},
            "inputs": {name: {"path": p, "checksum": file_digest(p)}
                       for name, p in sorted((files or {}).items()) if p}}


def run_batch(dams, ops, out_folder, keep_going=True, skip_done=True, log=None,
              cancelled=lambda: False, versions=None, settings_path="", input_files=None,
              manifest_extra=None):
    """Run every dam in turn. Writes the summary CSV after each dam, the manifest at the end.
    Returns (rows, summary path, manifest path)."""
    log = log or (lambda m: None)
    os.makedirs(out_folder, exist_ok=True)
    spath = os.path.join(out_folder, SUMMARY_NAME)
    mpath = os.path.join(out_folder, MANIFEST_NAME)
    rows = []
    started = now()

    def _try_write(what, fn):
        """A summary/manifest write must never abort the batch (Summary.csv open in Excel ->
        PermissionError, disk full -> OSError): log it and keep going so every dam still runs."""
        try:
            fn()
        except OSError as e:
            log("could not write {} ({}); the run continues".format(what, e))

    for i, dam in enumerate(dams):
        if cancelled():
            break
        log("\u2014 Dam {} of {}: {} \u2014".format(i + 1, len(dams), dam.dam_id))
        files = dict(input_files or {})
        files.update({k: v for k, v in (getattr(dam, "inputs", None) or {}).items()})
        try:
            save_json(os.path.join(chain_dir(dam.folder), USED_NAME),
                      settings_used(dam, versions, files, settings_path))
            row = run_dam(dam, ops, skip_done=skip_done, log=log, cancelled=cancelled)
        except Exception as e:                                   # noqa - next dam
            row = dict.fromkeys(SUMMARY_FIELDS, "")
            row.update({"Dam": dam.dam_id, "Folder": dam.folder, "Status": "failed",
                        "Message": "{}: {}".format(type(e).__name__, e)[:500]})
        rows.append(row)
        _try_write(SUMMARY_NAME, lambda: write_summary(spath, rows))
        if row["Status"] == "failed" and not keep_going:
            log("Stopped: {} failed and 'keep going' is off".format(dam.dam_id))
            break
        if row["Status"] == "cancelled":
            break
    counts = {}
    for r in rows:
        counts[r["Status"]] = counts.get(r["Status"], 0) + 1
    man = {"format": "ND_Dam_Suite/chain_manifest", "format_version": 1,
           "started": started, "finished": now(), "out_folder": out_folder,
           "settings_file": settings_path, "versions": versions or {},
           "dams_requested": [d.dam_id for d in dams], "counts": counts,
           "dams": [{"dam": r["Dam"], "status": r["Status"], "folder": r["Folder"],
                     "state": os.path.join(chain_dir(r["Folder"]), STATE_NAME) if r["Folder"]
                     else "",
                     "settings_used": os.path.join(chain_dir(r["Folder"]), USED_NAME)
                     if r["Folder"] else "", "hazard_project": r["Hazard_Project"]}
                    for r in rows]}
    man.update(manifest_extra or {})
    _try_write(MANIFEST_NAME, lambda: save_json(mpath, man))
    _try_write(SUMMARY_NAME, lambda: write_summary(spath, rows))
    return rows, spath, mpath


# ------------------------------------------------------------- preflight ----
ERROR, WARNING, OK = "ERROR", "WARNING", "OK"


def _needed_algs(cv, engine):
    """The stage keys a dam needs given its chain values and the HEC-RAS engine detect dict."""
    need = []
    if cv.get("SCREENING"):
        need.append("screening")
    need.append("runner")
    mode, _n = effective_hec_mode(cv, engine)
    if mode in ("wait", "run_windows"):
        need += ["hec_build", "hazard_import"]
    elif mode == "run_linux":
        need.append("hazard_import")
    need.append("classify")
    return need


def preflight(dam_ids, values, env, overrides=None, override_problems=(), missing_ids=()):
    """[(level, dam or '', text)] before a run. `env`: {algs: set of installed algorithm IDs,
    engine: HEC-RAS detect dict, exists: fn(path) (os.path.exists)}."""
    exists = env.get("exists") or os.path.exists
    algs = env.get("algs")
    out = []
    cv, probs = chain_values(values)
    for p in probs:
        out.append((ERROR, "", "Chain setting " + p))
    if not dam_ids:
        out.append((ERROR, "", "No dams: choose a dams layer (or its selected dams), type dam "
                               "IDs or give an IDs CSV"))
    else:
        out.append((OK, "", "{} dam(s) in the list".format(len(dam_ids))))
    for d in missing_ids:
        out.append((ERROR, d, "dam ID {} is not in the dams layer".format(d)))
    folders = {}            # two distinct IDs that collapse to one folder cross-attribute results
    for d in (dam_ids or ()):
        folders.setdefault(safe_name(d), []).append(d)
    for fn, ds in folders.items():
        if len(ds) > 1:
            out.append((ERROR, "", "dam IDs {} share the output folder '{}' (their IDs differ "
                                   "only in characters the folder name replaces); rename them or "
                                   "run them in separate batches".format(", ".join(ds), fn)))
    of = str(cv.get("OUT_FOLDER") or "").strip()
    if not of:
        out.append((ERROR, "", "Choose an output folder"))
    else:
        parent = of if exists(of) else os.path.dirname(os.path.abspath(of))
        if not exists(parent) or not os.access(parent, os.W_OK):
            out.append((ERROR, "", "The output folder {} cannot be written".format(of)))
        elif "ND_Dam_Suite_Pack" in of.replace("\\", "/"):
            out.append((ERROR, "", "The output folder is inside the read-only "
                                   "ND_Dam_Suite_Pack; choose another folder"))
        else:
            out.append((OK, "", "Output folder " + of))
    dem = str(cv.get("DEM") or "").strip()
    if not dem:
        out.append((ERROR, "", "Choose the DEM for the Runner"))
    elif "://" not in dem and not exists(dem):
        out.append((ERROR, "", "The DEM {} does not exist".format(dem)))
    needed = []
    if cv.get("SCREENING"):
        needed.append("screening")
        sw = str(cv.get("SCREEN_WORK") or "").strip()
        if not sw:
            out.append((ERROR, "", "Choose the Screening work folder (or untick Screening)"))
        elif not exists(sw):
            out.append((ERROR, "", "The Screening work folder {} does not exist".format(sw)))
    needed.append("runner")
    mode, note = effective_hec_mode(cv, env.get("engine"))
    if mode in ("wait", "run_windows"):
        needed += ["hec_build", "hazard_import"]
    elif mode == "run_linux":
        needed.append("hazard_import")
    needed.append("classify")
    if algs is not None:
        for s in needed:
            if ALGS[s] not in algs:
                out.append((ERROR, "", "{} needs {} ({}), which is not installed or not "
                                       "enabled".format(STAGE_LABELS[s], ALGS[s], _plugin_of(
                                           ALGS[s]))))
        if ALGS["save_state"] not in algs:
            out.append((WARNING, "", "ndhazard:savedamstate is missing (DamSafety Hazard "
                                     "2.18.59 or later): the dams are classified but their "
                                     "Hazard window state is not saved"))
    if note:
        out.append((WARNING, "", note))
    if mode == "wait" and float(cv.get("HEC_WAIT_MIN") or 0) <= 0:
        out.append((WARNING, "", "HEC-RAS: the chain builds each model and stops that dam at "
                                 "'waiting for HEC-RAS'; run the plans by hand, then run the "
                                 "chain again to finish them"))
    if mode == "skip":
        out.append((OK, "", "HEC-RAS skipped: the Runner rasters go straight to Hazard"))
    prof = str(cv.get("HAZARD_PROFILE") or "").strip()
    if prof and not exists(prof):
        out.append((ERROR, "", "The Hazard profile {} does not exist".format(prof)))
    for p in override_problems:
        out.append((ERROR, "", p))
    known = set(canon_id(x) for x in (dam_ids or ()))
    engine = env.get("engine")
    base_need = set(_needed_algs(cv, engine))
    for d, ov in (overrides or {}).items():
        if canon_id(d) not in known:
            out.append((WARNING, d, "overrides for dam {}, which is not in the list".format(d)))
            continue
        v, _a = resolve_for_dam(values, ov)
        dcv, dprobs = chain_values(v)
        for p in dprobs:
            out.append((ERROR, d, "{}: override {}".format(d, p)))
        if algs is not None:          # a per-dam override that needs a stage the global run does not
            for s in _needed_algs(dcv, engine):
                if s not in base_need and ALGS[s] not in algs:
                    out.append((ERROR, d, "{}: its override needs {} ({}), which is not installed "
                                          "or not enabled".format(d, ALGS[s], _plugin_of(ALGS[s]))))
        for k in ("ANUGA_RUN", "HEC_PLAN_HDF", "HEC_TERRAIN", "DEM"):
            pth = str(dcv.get(k) or "").strip()
            if pth and (k != "DEM" or "://" not in pth) and not exists(pth):
                out.append((ERROR, d, "{}: {} {} does not exist".format(d, k, pth)))
    return out


def _plugin_of(alg):
    return {"nddam": "DamSafety Screening", "ndbreach": "DamSafety Runner",
            "ndhazard": "DamSafety Hazard"}.get(alg.split(":")[0], alg.split(":")[0])
