"""Runner 3.10.67: ndbreach:chainbatch - a list of dams through the whole suite, dam by dam.

The plan, the settings, resume and the summary files are in core/chain.py (no QGIS); this
module runs each stage's tool by its algorithm ID with processing.run, so a missing plugin
fails that stage with a plain message (the preflight says so before anything starts):

  screening      nddam:runchain        (DamSafety Screening; that dam only)
  runner         ndbreach:batch        (one dam; Models from RUNNER_MODELS, or LISFLOOD-FP then
                                        HEC-RAS 2D when the HEC-RAS stage runs on Linux)
  hec_build      ndbreach:hecrasexport (the HEC-RAS 2D package; culverts / bridges from an
                                        existing ANUGA run when there is one)
  hec_run        the Runner's Linux HEC-RAS stage, or a wait for a manual run of the package
  hazard_import  ndhazard:hecrasplan   (ft, ft/s, h, 2D-area boundary; 3.10.84: depth on the
                                        HEC-RAS stage's basis - cell minimum by default, the
                                        terrain only for NDBREACH_HECRAS_DEPTH_BASIS=terrain)
  classify       ndhazard:classify     (ND + TM11), then ndhazard:savedamstate (the dam's
                                        Hazard window state, to add scenarios later)
"""
import json
import os

from qgis.core import (QgsApplication, QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputString, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource, QgsProcessingParameterField)

from .algorithms import _Base
from .compat import ALG_NO_THREADING, FIELD_ANY, SOURCE_POINT
from .core import chain as CH
from .core import hecras, nd_data
from .damsuite import settings as DS
from . import suite_settings_tools as ST

SCREEN_OPTIONAL_KEYS = ["0", "6", "9", "16", "18"]      # nddam:runchain OPTIONAL_KEYS
# parameters the chain sets itself (never from the settings rows)
RUNNER_FIXED = ("DAMS", "DAMS_FOLDER", "DEM", "TIER", "SKIP_DONE", "CLASSIFY", "F_ID")
HEC_FIXED = ("DAM_FOLDER", "OUT_FOLDER", "BUILD_MODEL", "STRUCTURES_FROM")
IMPORT_FIXED = ("PLAN", "TERRAIN", "OUTPUT")
CLASSIFY_FIXED = ("DAM_FOLDER", "DEPTH", "VELOCITY", "ARRIVAL", "DXV", "MODEL_BOUNDARY")
CLASSIFY_STATE_KEYS = ("KEEP_DRY", "JUDGMENT", "DOWNLOAD_NSI", "DOWNLOAD_ADDRESS",
                       "DOWNLOAD_USA", "DOWNLOAD_DAMS", "DOWNLOAD_LIFELINES", "EAP",
                       "DOWNLOAD_CROSSINGS", "SPREADSHEETS", "REPORT", "DOWNLOAD_CAMPS",
                       "EAP_LIST", "EAP_MAPBOOK", "EAP_TRAVEL", "COMPARE")


def registry_algs():
    try:
        return {a.id() for a in QgsApplication.processingRegistry().algorithms()}
    except Exception:                                            # noqa
        return set()


def hec_engine(platform=None, detect_windows=None):
    """{available, platform, reason, windows_launcher} of the HEC-RAS 2D engine here. On
    Windows (3.10.68): the Windows launcher (core/hecras_windows.py) when Ras.exe is found."""
    import sys
    plat = platform or sys.platform
    if plat.startswith("win"):
        try:
            if detect_windows is None:
                from .core import hecras_windows as HW
                detect_windows = HW.detect
            d = detect_windows()
            ok = bool(d.get("available"))
            return {"available": ok, "platform": plat, "windows_launcher": ok,
                    "reason": "" if ok else (d.get("reason") or "HEC-RAS 6.6 not found")}
        except Exception as e:                                   # noqa
            return {"available": False, "platform": plat, "reason": str(e),
                    "windows_launcher": False}
    try:
        from .core import hecras_engine as HE
        d = HE.detect()
        return {"available": bool(d.get("available")), "platform": d.get("platform", ""),
                "reason": d.get("reason") or "; ".join(d.get("missing") or []),
                "windows_launcher": False}
    except Exception as e:                                       # noqa
        import sys
        return {"available": False, "platform": sys.platform, "reason": str(e),
                "windows_launcher": False}


def versions():
    out = {}
    try:
        from qgis.core import Qgis
        out["QGIS"] = Qgis.version()
    except Exception:                                            # noqa
        pass
    reg = QgsApplication.processingRegistry()
    for pid in ("nddam", "ndbreach", "ndhazard"):
        try:
            p = reg.providerById(pid)
            if p is not None:
                out[pid] = "{} {}".format(p.name(), p.versionInfo()).strip()
        except Exception:                                        # noqa
            pass
    return out


def _id_text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        v = int(v)
    s = str(v).strip()
    return "" if s.upper() == "NULL" else s


def load_values(path):
    """The settings file's values ({} when there is none). Raises SettingsError."""
    if not path:
        return {}
    return dict(DS.Settings.load(path).values)


def fresh_defs(alg):
    try:
        inst = ST.fresh(alg)
    except Exception:                                            # noqa - not a Python class
        inst = alg.createInstance()
        inst.initAlgorithm({})
    return ST.param_defs(inst)


class QgisOps(object):
    """Runs one stage of one dam in QGIS."""

    def __init__(self, context, feedback, settings_path="", algs=None, run=None, lookup=None,
                 win_run=None):
        self.context, self.feedback = context, feedback
        self.win_run = win_run          # (folder, log) -> {plan_hdf, method, ...}; tests
        self.lookup = lookup or QgsApplication.processingRegistry().algorithmById
        self.settings_path = settings_path
        self.algs = algs
        if run is None:
            from qgis import processing

            def run(aid, params):
                return processing.run(aid, params, context=context, feedback=feedback,
                                      is_child_algorithm=True)
        self._run = run

    def log(self, m):
        if self.feedback is not None:
            self.feedback.pushInfo(m)

    def _alg(self, stage_or_id):
        aid = CH.ALGS.get(stage_or_id, stage_or_id)
        alg = self.lookup(aid)
        if alg is None:
            raise CH.ChainError("{} is not installed or not enabled (algorithm {} not "
                                "found)".format(CH._plugin_of(aid), aid))
        return aid, alg

    def tool_params(self, dam, stage, fixed):
        aid, alg = self._alg(stage)
        plugin, tool = CH.STAGE_TOOLS[stage]
        params, probs = CH.params_from_settings(dam.values, plugin, tool, fresh_defs(alg),
                                                skip=tuple(fixed))
        for p in probs:
            self.log("{}: {}".format(dam.dam_id, p))
        params.update({k: v for k, v in fixed.items() if v is not None})
        return aid, params

    def run(self, stage, dam):
        return getattr(self, "run_" + stage)(dam)

    # -- stages ------------------------------------------------------------------------
    def run_screening(self, dam):
        aid, _alg = self._alg("screening")
        work_root = str(dam.cv.get("SCREEN_WORK") or "").strip()
        work = os.path.join(work_root, "one_dam", screening_safe_name(dam.dam_id))
        os.makedirs(work, exist_ok=True)
        S = CH.window_rows(dam.values, "ndscreen")
        S.update({"only_ids": dam.dam_id, "package_work": work_root,
                  "settings_file": self.settings_path or ""})
        inc = [SCREEN_OPTIONAL_KEYS.index(k) for k in CH.parse_ids(dam.cv.get("SCREEN_INCLUDE"))
               if k in SCREEN_OPTIONAL_KEYS]
        log = os.path.join(CH.chain_dir(dam.folder), "screening_log.txt")
        os.makedirs(os.path.dirname(log), exist_ok=True)
        params = {"WORK": work, "SETTINGS": json.dumps(S, default=str),
                  "PREFLIGHT": bool(dam.cv.get("SCREEN_PREFLIGHT")), "DRY_RUN": False,
                  "FAIL_ON_ERROR": False, "LOG": log}
        if inc:
            params["INCLUDE"] = inc
        old = os.environ.get(DS.ENV_PATH)
        if self.settings_path:
            os.environ[DS.ENV_PATH] = self.settings_path
        try:
            res = self._run(aid, params) or {}
        finally:
            if self.settings_path:
                if old is None:
                    os.environ.pop(DS.ENV_PATH, None)
                else:
                    os.environ[DS.ENV_PATH] = old
        if not res.get("OK", False):
            raise CH.ChainError("Screening did not finish: {} (log: {})".format(
                res.get("RESULT") or "no result", log))
        return {"work": work, "ran": res.get("RAN", ""), "log": log}

    def run_runner(self, dam):
        pt = os.path.join(CH.chain_dir(dam.folder), "dam_point.gpkg")
        write_point(dam.feature, pt)
        tier = 2 if dam.mode == "run_linux" else int(dam.cv.get("RUNNER_MODELS") or 0)
        fixed = {"DAMS": pt, "DAMS_FOLDER": os.path.dirname(dam.folder),
                 "DEM": str(dam.cv.get("DEM") or ""), "TIER": tier, "SKIP_DONE": False,
                 "CLASSIFY": False, "F_ID": dam.id_field}
        aid, params = self.tool_params(dam, "runner", fixed)
        res = self._run(aid, params) or {}
        row = batch_row(res.get("SUMMARY") or os.path.join(os.path.dirname(dam.folder),
                                                           "Batch_Breach_Runs.csv"), dam.dam_id)
        depth = os.path.join(dam.folder, CH.FINAL_RASTERS["depth"])
        status = (row or {}).get("Status", "")
        msg = (row or {}).get("Message", "")
        if status == "done" and os.path.exists(depth):
            return {"depth": depth, "final_model": row.get("Final_Model", "")}
        if status == "failed" and dam.mode == "run_linux" and "HEC-RAS" in msg \
                and os.path.exists(depth):
            return {"depth": depth, "hec_message": msg,
                    "message": "LISFLOOD-FP done; HEC-RAS 2D stage failed"}
        if status == "no breach":
            raise CH.ChainError("the dam does not fail in this flood (Runner: no breach); "
                                "nothing to classify")
        raise CH.ChainError("Runner: {}{}".format(status or "no result",
                                                  " - " + msg if msg else ""))

    def run_hec_build(self, dam):
        import time as _t
        src, note = CH.structures_source(dam.cv, dam.folder)
        self.log("{}: {}".format(dam.dam_id, note))
        fixed = {"DAM_FOLDER": dam.folder, "BUILD_MODEL": True}
        if src:
            fixed["STRUCTURES_FROM"] = src
        aid, params = self.tool_params(dam, "hec_build", fixed)
        t0 = _t.time()
        res = self._run(aid, params) or {}
        pkg = res.get("MODEL_FOLDER") or ""
        if not pkg or not os.path.isdir(pkg):
            raise CH.ChainError("HEC-RAS model not built: {}".format(
                res.get("RESULT") or "no model folder (see the log)"))
        drop = os.path.join(CH.chain_dir(dam.folder), CH.RESULTS_DROP)
        os.makedirs(drop, exist_ok=True)
        todo = CH.hecras_todo_text(dam.dam_id, pkg, drop)
        for d in (CH.chain_dir(dam.folder), pkg):
            try:
                with open(os.path.join(d, CH.TODO_NAME), "w", encoding="utf-8") as f:
                    f.write(todo)
            except OSError:
                pass
        return {"package": pkg, "zip": res.get("MODEL_ZIP", ""), "built_at": t0,
                "message": note}

    def run_hec_run(self, dam, sleep=None):
        if dam.mode == "run_linux":
            rec = hecras_stage(dam.folder)
            if not rec:
                why = (dam.outputs.get("runner") or {}).get("hec_message") or \
                    "no finished HEC-RAS 2D stage in _hecras/hecras_stage.json"
                raise CH.ChainError(why)
            return {"plan_hdf": rec["results_hdf"], "project": rec.get("project", "")}
        explicit = str(dam.cv.get("HEC_PLAN_HDF") or "").strip()
        if explicit:
            if not os.path.isfile(explicit):
                raise CH.ChainError("HEC_PLAN_HDF {} does not exist".format(explicit))
            return {"plan_hdf": explicit}
        b = dam.outputs.get("hec_build") or {}
        pkg = b.get("package", "")
        drop = os.path.join(CH.chain_dir(dam.folder), CH.RESULTS_DROP)
        since = float(b.get("built_at") or 0)
        launch_note = ""
        if dam.mode == "run_windows":
            done = CH.find_finished_plan([pkg, drop], since)
            if done:                                 # computed by hand since the build
                return {"plan_hdf": done, "project": "", "method": "manual"}
            run = self.win_run
            if run is None:
                from .core import hecras_windows as HW

                def run(folder, log=None):
                    return HW.run_plan(folder, log=log)
            try:
                self.log("{}: running HEC-RAS 6.6 (Windows launcher)".format(dam.dam_id))
                r = run(pkg, log=self.log)
                return {"plan_hdf": r["plan_hdf"], "project": r.get("project", ""),
                        "method": r.get("method", ""), "attempts": r.get("attempts", []),
                        "seconds": r.get("seconds")}
            except Exception as e:                               # noqa - wait instead
                launch_note = "the Windows launcher did not run it ({}); ".format(e)
                self.log("{}: {}waiting for a manual run".format(dam.dam_id, launch_note))
        kw = {"cancelled": self.feedback.isCanceled if self.feedback else (lambda: False),
              "log": self.log}
        if sleep is not None:
            kw["sleep"] = sleep
        p = CH.wait_for_plan([pkg, drop], float(dam.cv.get("HEC_WAIT_MIN") or 0),
                             since=since, **kw)
        if not p:
            raise CH.Waiting("waiting for HEC-RAS: {}run the plan of {} by hand (see {}), then "
                             "run the chain again".format(launch_note, pkg, os.path.join(
                                 CH.chain_dir(dam.folder), CH.TODO_NAME)))
        return {"plan_hdf": p, "project": "", "method": "manual"}

    def run_hazard_import(self, dam):
        r = dam.outputs.get("hec_run") or {}
        plan = r.get("plan_hdf", "")
        # 3.10.84: the depth basis the HEC-RAS stage used (cell minimum by default). Only the
        # per-pixel 'terrain' basis needs the terrain; without TERRAIN ndhazard:hecrasplan
        # makes its own cell-minimum depth from the plan (the Runner's basis), unless the
        # newest current Runner re-export of the plan (or a fresh terrain stage) is on the
        # terrain basis, which Hazard 2.18.63 then adopts
        basis, why, stage = import_depth_basis(dam, plan)
        terrain = ""
        if basis == hecras.DEPTH_BASIS_TERRAIN:
            terrain = str(dam.cv.get("HEC_TERRAIN") or "").strip()
            if not terrain and stage.get("terrain_tif") and \
                    os.path.isfile(str(stage["terrain_tif"])):
                terrain = str(stage["terrain_tif"])          # the Linux stage's own terrain
            if not terrain:
                extra = [(dam.outputs.get("hec_build") or {}).get("package", "")]
                if r.get("project"):
                    extra += [r["project"], os.path.dirname(r["project"])]
                terrain = CH.find_terrain(plan, extra) or ""
            if not terrain:
                raise CH.ChainError("no HEC-RAS terrain GeoTIFF found next to {} (Terrain "
                                    "folder) for the per-pixel 'terrain' depth basis ({}); "
                                    "give HEC_TERRAIN for this dam".format(plan, why))
            self.log("{}: HEC-RAS import depth basis: per pixel, max WSE minus the terrain "
                     "{} ({})".format(dam.dam_id, terrain, why))
        else:
            env = hecras.DEPTH_BASIS_ENV
            if stage:          # the Linux stage's own record: the variable is read by that stage
                how = ("set {}=terrain before the Runner's HEC-RAS stage runs (a resumed chain "
                       "does not run it again), or re-export the plan with ndbreach_cli.py "
                       "hecras-reexport --depth-basis terrain (Hazard 2.18.63 adopts the newest "
                       "current re-export of the plan)".format(env))
            else:              # wait / manual / Windows run: read when this import runs
                how = "set {}=terrain; it is read when this import runs".format(env)
            self.log("{}: HEC-RAS import depth basis: cell minimum, max WSE minus the HEC-RAS "
                     "cell's minimum elevation ({}); for per-pixel depth {}".format(
                         dam.dam_id, why, how))
        out = os.path.join(CH.chain_dir(dam.folder), "hecras_import")
        os.makedirs(out, exist_ok=True)
        rec_p = os.path.join(out, "depth_basis.json")      # Hazard's record of the basis used
        try:
            os.remove(rec_p)                                # not a previous import's
        except OSError:
            pass
        aid, params = self.tool_params(dam, "hazard_import",
                                       {"PLAN": plan, "TERRAIN": terrain or None, "OUTPUT": out})
        res = self._run(aid, params) or {}
        depth = res.get("DEPTH") or ""
        if not depth or not os.path.exists(depth):
            raise CH.ChainError("the HEC-RAS import wrote no depth raster")
        try:                                # terrain / runner_reexport / cell_min (Hazard)
            with open(rec_p, encoding="utf-8") as f:
                used = str((json.load(f) or {}).get("depth_basis") or "")
        except (OSError, ValueError, AttributeError):
            used = ""
        if used and used != basis:
            self.log("{}: the Hazard import used depth basis {} (see {})".format(
                dam.dam_id, used, rec_p))
        return {"depth": depth, "velocity": res.get("VELOCITY", ""),
                "arrival": res.get("ARRIVAL", ""), "boundary": res.get("BOUNDARY", ""),
                "terrain": terrain, "depth_basis": basis, "hazard_depth_basis": used}

    def run_classify(self, dam):
        if dam.mode == "skip":
            ras = {k: os.path.join(dam.folder, v) for k, v in CH.FINAL_RASTERS.items()}
            ras = {k: (p if os.path.exists(p) else "") for k, p in ras.items()}
            ras["boundary"] = ""
            scen = "Breach ({})".format((dam.outputs.get("runner") or {}).get(
                "final_model") or "Runner")
        else:
            imp = dam.outputs.get("hazard_import") or {}
            ras = {"depth": imp.get("depth", ""), "velocity": imp.get("velocity", ""),
                   "arrival": imp.get("arrival", ""), "dv": "",
                   "boundary": imp.get("boundary", "")}
            scen = "Breach (HEC-RAS 2D)"
        if not ras["depth"]:
            raise CH.ChainError("no depth raster to classify")
        fixed = {"DAM_FOLDER": dam.folder, "DEPTH": ras["depth"],
                 "VELOCITY": ras["velocity"] or None, "ARRIVAL": ras["arrival"] or None,
                 "DXV": ras["dv"] or None, "MODEL_BOUNDARY": ras["boundary"] or None}
        aid, params = self.tool_params(dam, "classify", fixed)
        res = self._run(aid, params) or {}
        summary = res.get("SUMMARY") or os.path.join(dam.folder, "outputs",
                                                     "Hazard_Classification_Summary.txt")
        if not os.path.exists(summary):
            raise CH.ChainError("the classification wrote no summary ({})".format(summary))
        out = {"summary": summary, "nd_class": res.get("ND_CLASS", ""),
               "tm11_class": res.get("TM11_CLASS", ""), "report": res.get("REPORT_PDF", ""),
               "project": ""}
        if self.algs is not None and CH.ALGS["save_state"] not in self.algs:
            out["message"] = "Hazard state not saved (ndhazard:savedamstate not installed)"
            return out
        opts = {k: params[k] for k in CLASSIFY_STATE_KEYS if k in params}
        sp = {"DAM_FOLDER": dam.folder, "DEPTH": ras["depth"], "VELOCITY": ras["velocity"],
              "ARRIVAL": ras["arrival"], "DXV": ras["dv"], "BOUNDARY": ras["boundary"],
              "SCENARIO_NAME": scen, "SUBJECT_NID": str(params.get("SUBJECT_NID") or ""),
              "DAM_ID": dam.dam_id, "PROFILE": str(dam.cv.get("HAZARD_PROFILE") or ""),
              "CLASSIFY_OPTIONS": json.dumps(opts), "PROJECT_FILE": ""}
        try:
            r = self._run(CH.ALGS["save_state"], {k: v for k, v in sp.items() if v != ""}) or {}
            out["project"] = r.get("PROJECT", "")
            out["state_json"] = r.get("STATE_JSON", "")
        except Exception as e:                                   # noqa - classified anyway
            out["message"] = "Hazard state not saved: {}".format(e)
        return out


def screening_safe_name(text):
    """The Screening's one-dam folder name (DamSafety_Screening core/workflow.safe_name)."""
    out = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(text or "").strip())
    return out.strip("._") or "dam"


def hecras_stage(folder):
    """The finished Linux HEC-RAS stage record of a dam folder, or None."""
    from .core import workflow
    if not workflow.hecras_done(folder):
        return None
    with open(os.path.join(folder, "_hecras", "hecras_stage.json"), encoding="utf-8") as f:
        return json.load(f)


def import_depth_basis(dam, plan):
    """3.10.84: (basis, why, stage record or {}) of the chain's HEC-RAS import, decided as the
    HEC-RAS stage decided it: the depth_basis of the dam's _hecras/hecras_stage.json when the
    chain ran the Runner's Linux HEC-RAS stage and that record is of this plan, else
    core/hecras.depth_basis() (NDBREACH_HECRAS_DEPTH_BASIS, default 'cell_min'). Raises
    ChainError for an unknown NDBREACH_HECRAS_DEPTH_BASIS."""
    if dam.mode == "run_linux":
        try:
            rec = hecras_stage(dam.folder) or {}
        except (OSError, ValueError):
            rec = {}
        b = rec.get("depth_basis")
        if b in hecras.DEPTH_BASES and _same_file(rec.get("results_hdf"), plan):
            return b, "as the HEC-RAS stage, _hecras/hecras_stage.json", rec
    try:
        b = hecras.depth_basis(None)
    except hecras.HecRasError as e:
        raise CH.ChainError(str(e))
    env = hecras.DEPTH_BASIS_ENV
    why = "{}={}".format(env, os.environ[env]) if os.environ.get(env) else "the default"
    return b, why, {}


def _same_file(a, b):
    if not (a and b):
        return False
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def batch_row(path, dam_id):
    import csv
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if CH.norm_id(r.get("Dam")) == CH.norm_id(dam_id):
                    return r
    except OSError:
        return None
    return None


def write_point(feature, path):
    """One dam feature (a (fields, crs, QgsFeature) tuple) as a GeoPackage for the Runner's
    batch tool."""
    from qgis.core import QgsFeature, QgsProject, QgsVectorFileWriter, QgsVectorLayer
    fields, crs, feat = feature
    lyr = QgsVectorLayer("Point?crs={}".format(crs.authid() or "EPSG:4326"), "dam", "memory")
    if not crs.authid():
        lyr.setCrs(crs)
    pr = lyr.dataProvider()
    pr.addAttributes(fields.toList())
    lyr.updateFields()
    f = QgsFeature(lyr.fields())
    f.setGeometry(feat.geometry())
    for i, name in enumerate(fields.names()):
        j = lyr.fields().indexOf(name)
        if j >= 0 and name.lower() != "fid":
            f.setAttribute(j, feat.attribute(i))
    pr.addFeatures([f])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = "dam"
    err = QgsVectorFileWriter.writeAsVectorFormatV3(lyr, path,
                                                   QgsProject.instance().transformContext(), opts)
    code = err[0] if isinstance(err, tuple) else err
    if code != QgsVectorFileWriter.NoError:
        raise CH.ChainError("the dam point could not be written to {}: {}".format(
            path, err[1] if isinstance(err, tuple) and len(err) > 1 else code))
    return path


def collect_dams(source, id_field, wanted):
    """[(dam ID, (fields, crs, feature))] of the source's features, filtered to `wanted` (in
    its order) when given; [wanted IDs not found]; and [notes about features dropped for a
    blank/NULL or a duplicate ID] so a feature the operator selected is never silently
    skipped."""
    from qgis.core import QgsFeature
    fields = source.fields()
    crs = source.sourceCrs()
    byid = {}
    dropped = []
    blank = 0
    dup_ids = []
    for feat in source.getFeatures():
        i = _id_text(feat.attribute(id_field))
        if not i:
            blank += 1
            continue
        if i in byid:
            if i not in dup_ids:
                dup_ids.append(i)
            continue
        byid[i] = (fields, crs, QgsFeature(feat))
    for i in dup_ids:
        dropped.append("two or more features share the dam ID {}; the first is used, the rest "
                       "are skipped".format(i))
    if blank:
        dropped.insert(0, "{} feature(s) have a blank/NULL '{}' and were skipped (no dam "
                          "ID)".format(blank, id_field))
    if wanted:
        return ([(i, byid[i]) for i in wanted if i in byid],
                [i for i in wanted if i not in byid], dropped)
    return list(byid.items()), [], dropped


class ChainBatchAlgorithm(_Base):
    def name(self):
        return "chainbatch"

    def displayName(self):
        return "Batch chain: Screening \u2192 Runner \u2192 HEC-RAS \u2192 Hazard, dam by dam"

    def flags(self):
        return super().flags() | ALG_NO_THREADING

    def shortHelpString(self):
        return ("Takes a list of dams and runs each one through the suite before starting the "
                "next: the Screening steps for that dam, the Runner, the HEC-RAS 2D model "
                "build and run, the Hazard import of the HEC-RAS results and the ND + TM11 "
                "classification (whose window state is saved as <dam>/Hazard_<ID>.qgz). A "
                "dam that fails does not stop the list.\n\nHEC-RAS 2D stage: 'Run' (the "
                "Runner's own HEC-RAS stage where HEC-RAS can run, else as 'Wait'), 'Wait for "
                "a manual HEC-RAS run' (the model is built and the chain waits for its "
                "<name>.p01.hdf; 0 minutes = the dam stops at 'waiting for HEC-RAS' and the "
                "next run of the chain continues it) or 'Skip' (the Runner's rasters go "
                "straight to the classification).\n\nSettings: the suite settings file (each "
                "tool's rows), an overrides CSV (dam_id + only the values that differ, columns "
                "plugin.tool.KEY or a chain key), a preflight before anything starts. Writes "
                "Chain_Batch_Summary.csv and chain_manifest.json in the output folder and "
                "<dam>/_chain/chain_settings_used.json for each dam.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            "DAMS", "Dams (points; tick 'Selected features only' for the selected dams)",
            [SOURCE_POINT]))
        self.addParameter(QgsProcessingParameterField(
            "DAM_ID_FIELD", "Dam ID field (empty = the chain setting, else match by name)",
            parentLayerParameterName="DAMS", type=FIELD_ANY, optional=True))
        self._text("DAM_IDS", "Dam IDs (comma separated; empty = every dam of the layer or "
                   "the IDs CSV)")
        self._file("IDS_CSV", "Dam IDs CSV (a dam_id column)", "csv")
        self._file("OVERRIDES_CSV", "Per-dam overrides CSV (dam_id + the values that "
                   "differ)", "csv")
        self._file("SETTINGS_FILE", "Suite settings file (.xlsx / .csv; empty = the current "
                   "one)")
        for k in CH.KEYS:
            if k.kind == "bool":
                self._bool(k.key, k.label, k.default)
            elif k.kind == "enum":
                self.addParameter(QgsProcessingParameterEnum(k.key, k.label, list(k.options),
                                                             defaultValue=k.default))
            elif k.kind == "number":
                self._num(k.key, k.label, k.default, minimum=0)
            else:
                self._text(k.key, k.label, advanced=k.key in (
                    "ANUGA_RUN", "HEC_PLAN_HDF", "HEC_TERRAIN"))
        self._bool("CHECK_ONLY", "Only run the preflight check (nothing is started)", False)
        self.addOutput(QgsProcessingOutputFile("SUMMARY", CH.SUMMARY_NAME))
        self.addOutput(QgsProcessingOutputFile("MANIFEST", CH.MANIFEST_NAME))
        self.addOutput(QgsProcessingOutputString("COUNTS", "Counts"))

    def batch_values(self, parameters, context):
        """(settings file path, values with this run's chain choices on top)."""
        path = self._fileparam(parameters, "SETTINGS_FILE", context) or DS.current_path()
        try:
            values = load_values(path)
        except DS.SettingsError as e:
            raise QgsProcessingException(str(e))
        for k in CH.KEYS:
            if k.key not in parameters or parameters.get(k.key) in (None, ""):
                continue
            if k.kind == "bool":
                v = self.parameterAsBool(parameters, k.key, context)
            elif k.kind == "enum":
                v = self.parameterAsEnum(parameters, k.key, context)
            elif k.kind == "number":
                v = self.parameterAsDouble(parameters, k.key, context)
            else:
                v = self._str(parameters, k.key, context)
            if v != "":
                values[(CH.PLUGIN, CH.TOOL, k.key)] = v
        return path, values

    def processAlgorithm(self, parameters, context, feedback):
        spath, values = self.batch_values(parameters, context)
        cv, _p = CH.chain_values(values)
        src = self.parameterAsSource(parameters, "DAMS", context)
        if src is None:
            raise QgsProcessingException("Choose the dams layer (points)")
        id_field = self._str(parameters, "DAM_ID_FIELD", context) or \
            str(cv.get("ID_FIELD") or "").strip()
        if not id_field:
            auto, _how = nd_data.suggest_fields(src.fields().names())
            id_field = auto.get("F_ID", "")
        if not id_field or src.fields().indexOf(id_field) < 0:
            raise QgsProcessingException("Choose the dam ID field of the dams layer")
        wanted = CH.parse_ids(self._str(parameters, "DAM_IDS", context))
        ids_csv = self._fileparam(parameters, "IDS_CSV", context)
        if ids_csv:
            wanted += [i for i in CH.read_ids_csv(ids_csv) if i not in wanted]
        dams, missing, dropped = collect_dams(src, id_field, wanted)
        ov_path = self._fileparam(parameters, "OVERRIDES_CSV", context)
        overrides, ov_probs = CH.read_overrides(ov_path) if ov_path else ({}, [])
        algs = registry_algs()
        engine = hec_engine()
        checks = CH.preflight([d for d, _f in dams], values,
                              {"algs": algs, "engine": engine}, overrides, ov_probs, missing)
        for note in dropped:
            checks.append((CH.WARNING, "", note))
        n_err = 0
        for level, dam, text in checks:
            line = "{}: {}".format(level, text)
            if level == CH.ERROR:
                n_err += 1
                feedback.reportError(line, False)
            else:
                feedback.pushInfo(line)
        out = str(cv.get("OUT_FOLDER") or "").strip()
        if self.parameterAsBool(parameters, "CHECK_ONLY", context):
            return {"SUMMARY": "", "MANIFEST": "",
                    "COUNTS": "preflight: {} error(s)".format(n_err)}
        if n_err:
            raise QgsProcessingException("The preflight found {} error(s); nothing was "
                                         "started (see the log)".format(n_err))
        items = []
        for dam_id, feat in dams:
            v, applied = CH.resolve_for_dam(values, overrides.get(CH.canon_id(dam_id)))
            dcv, _p = CH.chain_values(v)
            mode, note = CH.effective_hec_mode(dcv, engine)
            d = CH.Dam(dam_id, os.path.join(out, CH.safe_name(dam_id)), v, dcv, applied,
                       feat, mode, note)
            d.id_field = id_field
            d.inputs = {"dem": str(dcv.get("DEM") or ""),
                        "hazard_profile": str(dcv.get("HAZARD_PROFILE") or ""),
                        "plan_hdf": str(dcv.get("HEC_PLAN_HDF") or "")}
            items.append(d)
        ops = QgisOps(context, feedback, spath, algs)
        rows, sp, mp = CH.run_batch(
            items, ops, out, keep_going=bool(cv.get("KEEP_GOING")),
            skip_done=bool(cv.get("SKIP_DONE")), log=feedback.pushInfo,
            cancelled=feedback.isCanceled, versions=versions(), settings_path=spath,
            input_files={"overrides_csv": ov_path, "ids_csv": ids_csv},
            manifest_extra={"hec_engine": engine, "missing_ids": missing})
        counts = {}
        for r in rows:
            counts[r["Status"]] = counts.get(r["Status"], 0) + 1
        msg = ", ".join("{} {}".format(n, s) for s, n in sorted(counts.items())) or "no dams"
        feedback.pushInfo("Batch chain: " + msg)
        return {"SUMMARY": sp, "MANIFEST": mp, "COUNTS": msg}


CHAIN_ALGORITHMS = (ChainBatchAlgorithm,)
