"""Runs one dam from breach hydrograph to result rasters.

1. Breach hydrograph (ND 5.1.1): sunny day with piping / overtopping
   parameters or TR-60, or the flood-inflow scenario (PMF inflow routed
   through the reservoir with and without a breach).
2. Initial domain: a steepest-descent trace from the breach inflow point,
   buffered on both sides.
3. LISFLOOD-FP over that domain. Wherever the flood leaves an edge of the
   domain deeper than the tolerance, that edge moves out and the model runs
   again (ND 5.1.1(f): extend downstream until further impacts would not
   change the hazard classification). With receptor layers the extension stops
   where no structure, road or rail lies in the flood's path. v3.2.0, optional
   (st.class_stop, stoprule.py): an edge is also accepted where the breach
   flood's depth, or its rise over the no-breach baseline, has died out along
   the flow path near it (ND Dam Hazard's termination point) and no receptor
   lies beyond. The duration is also extended if the flood is still advancing
   at the end.
4. Flood scenario: the no-breach baseline runs on the final domain, and the
   incremental depth and water-surface rise are worked out.
5. Optionally ANUGA on a mesh around the LISFLOOD-FP flood extent, refined
   near the dam and around culverts. Its results become the final rasters.
6. Rasters in the dam folder, in feet, ft/s and hours, named so the ND
   hazard classification plugin finds them: "Depth (Max).tif",
   "Velocity (Max).tif", "Arrival Time.tif", "WSE (Max).tif".
7. Products: flood polygons, arrival contours, receptor list, an EAP map preview
   (v3.2.0: the EAP map is ND Dam Hazard's map book; st.eap_map) and a
   self-contained HTML report.
8. v3.2.0 (st.dam_record, set by the rundam / batch tools): the values the run
   used and a "modelled" status go into <dam folder>/dam.json (damjson.py);
   another tool's value is never overwritten, a conflict is a report warning.

The GIS parts (reading the DEM, writing GeoTIFFs) come in through the
`terrain` and `writer` objects so this module runs without QGIS."""
import datetime
import hashlib
import json
import math
import os
import time

import numpy as np

from . import breach as brmod
from . import conditioning as cond
from . import flood as floodmod
from . import impacts as impmod
from . import structures as structmod
from . import terrain
from . import lisflood as lfmod
from . import anuga_model as anmod
from . import stoprule
from .units import FT, MILE, ACFT, CFS

def _metadata_version():
    """The plugin version from metadata.txt (the single source of truth)."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip().lower().startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


VERSION = _metadata_version()

TIER_LISFLOOD = "lisflood"
TIER_BOTH = "lisflood+anuga"
# HEC-RAS 2D (design #68): LISFLOOD-FP as for TIER_LISFLOOD, then the HEC-RAS 2D stage
# (core/hecras_engine.run_stage) replaces the final rasters. Appended so 0 and 1 keep their meaning.
TIER_HECRAS = "lisflood+hecras"
TIERS = (TIER_LISFLOOD, TIER_BOTH, TIER_HECRAS)

SCENARIO_SUNNY = "sunny"
SCENARIO_FLOOD = "flood"

MODEL_FOLDER = "_breach_model"
FINAL_NAMES = {"depth": "Depth (Max).tif", "velocity": "Velocity (Max).tif",
               "arrival": "Arrival Time.tif", "wse": "WSE (Max).tif"}
BASELINE_NAMES = {k: "Baseline " + v for k, v in FINAL_NAMES.items()}
# written by the ND 5.2 classification tool from the final rasters; removed when they change
STALE_ON_RERUN = ("Hazard Classification (ND 5.2).md", "Hazard Classification (ND 5.2).json",
                  "Hazard Classification Findings.csv", "Hazard Classification Findings.geojson",
                  "ND Fig 5.1 Houses.svg", "ND Fig 5.2 Mobile Homes.svg")
INCREMENT_NAMES = {"depth": "Incremental Depth (Max).tif", "wse": "Incremental WSE Rise (Max).tif"}
QA_NAMES = {"depth": "hmax_ft.tif", "velocity": "vmax_fps.tif", "arrival": "arr_hr.tif",
            "wse": "wsemax_ft.tif"}
WET_M = 0.003          # a cell counts as flooded above 0.01 ft
MAX_SIM_S = 336 * 3600.0   # longest simulation the automatic duration will ask for
STATE_FILE = "state.json"


def _carve_default():
    from . import reservoir_carve
    return reservoir_carve.env_mode()


class RunSettings:
    def __init__(self, **kw):
        self.tier = TIER_LISFLOOD
        self.breach_method = brmod.METHOD_PIPING
        self.overrides = brmod.BreachOverrides()
        self.user_hydrograph = None
        self.cell_size_m = 10.0
        self.planning_cell_m = 30.0
        self.manning_n = 0.06
        self.manning_scale = 1.0         # multiplies the Manning raster too (sensitivity)
        self.arrival_depth_ft = 1.0
        self.edge_tolerance_ft = 1.0
        self.initial_length_mi = 5.0
        self.half_width_mi = 1.0
        self.expand_step_mi = 3.0
        self.max_extent_mi = 20.0      # = algorithms.DEFAULT_MAX_EXTENT_MI (was 60 before 2026-10)
        self.qa_maps = "inflow+depth"  # QA PNGs: off | inflow | inflow+depth (core/qa_maps.py)
        self.max_iterations = 6
        self.sim_time_hr = None          # None = automatic
        self.sim_max_hr = None           # None = the built-in cap; a number is the longest clock
        self.front_speed_mps = 0.5       # for the automatic duration
        self.lisflood_exe = ""
        self.anuga_python = ""
        self.threads = 0
        self.anuga_fine_area_m2 = 100.0
        self.anuga_coarse_area_m2 = 1000.0
        self.anuga_near_field_mi = 1.0
        self.anuga_buffer_m = 500.0
        self.anuga_algorithm = "DE0"
        # hidden: "default" (ANUGA's own mesh, unchanged), "gmsh" (core/gmsh_mesher.py; needs
        # gmsh in the ANUGA environment; a rejected gmsh mesh stops the run) or
        # "gmsh_or_default" (a rejected gmsh mesh falls back to the default, with a WARNING
        # and a line in run_report). NDBREACH_ANUGA_MESHER (or ANUGA_MESHER) sets it.
        self.anuga_mesher = (os.environ.get("NDBREACH_ANUGA_MESHER")
                             or os.environ.get("ANUGA_MESHER") or "default")
        # optional dict of gmsh sizes (gmsh_mesher.build_mesh, e.g. min_area_tol,
        # culvert_area_m2) and embankment options (anuga_model.EMBANKMENT_DEFAULTS)
        self.anuga_gmsh = None
        # 3.10.31: the ANUGA bed. "fine" = the DEM source at its own cell size (read in blocks)
        # sampled at the triangle vertices, so road and dam crowns on mesh lines survive;
        # "model_grid" = the model-cell DEM at the centroids (before 3.10.31). LISFLOOD-FP
        # always uses the model grid. NDBREACH_ANUGA_ELEVATION (or ANUGA_ELEVATION) sets it;
        # NDBREACH_ANUGA_ELEVATION_CELL_M fixes the fine cell size (0 = the source's own).
        self.anuga_elevation = (os.environ.get("NDBREACH_ANUGA_ELEVATION")
                                or os.environ.get("ANUGA_ELEVATION") or "fine")
        self.anuga_elevation_cell_m = 0.0
        try:
            self.anuga_elevation_cell_m = float(os.environ.get("NDBREACH_ANUGA_ELEVATION_CELL_M")
                                                or 0.0)
        except ValueError:
            pass
        # 3.10.38: ANUGA checkpoints. anuga_checkpoint_min None = automatic: every 30
        # wall-clock minutes for a long run (>= 12 h simulated or >= 50,000 triangles
        # expected; anuga_model.resolve_checkpoints), none otherwise; 0 = off; > 0 = always,
        # that often (or every anuga_checkpoint_steps yield steps of 300 s). The newest
        # anuga_checkpoint_keep are kept. anuga_partial_min writes anuga/anuga_partial.npz (the
        # envelope so far) that often (None = 10 min when checkpoints are on) and after each
        # checkpoint. anuga_resume continues the same ANUGA job from its newest checkpoint
        # (also when the run resumes, st.resume, with checkpoints on). Env:
        # NDBREACH_ANUGA_CHECKPOINT_MIN (0 = off), _CHECKPOINT_STEPS, _CHECKPOINT_KEEP,
        # _PARTIAL_MIN, _RESUME (or ANUGA_CHECKPOINT_MIN...). The hydraulics are the same.
        self.anuga_checkpoint_min = _env_number("ANUGA_CHECKPOINT_MIN", None)
        self.anuga_checkpoint_steps = int(_env_number("ANUGA_CHECKPOINT_STEPS", 0))
        self.anuga_checkpoint_keep = int(_env_number("ANUGA_CHECKPOINT_KEEP", 2))
        self.anuga_partial_min = _env_number("ANUGA_PARTIAL_MIN", None)
        self.anuga_resume = _env_flag("ANUGA_RESUME")
        _tol = os.environ.get("NDBREACH_ANUGA_GMSH_MIN_AREA_TOL")
        if _tol:
            try:
                self.anuga_gmsh = {"min_area_tol": float(_tol)}
            except ValueError:
                pass
        self.keep_model_files = True
        # scenario
        self.scenario = SCENARIO_SUNNY
        self.flood = None                # flood.FloodInputs for the flood scenario
        self.baseline = True             # flood scenario: also run without the breach
        self.incr_thresholds_ft = (1.0, 2.0, 3.0)
        # things in the flow path
        self.receptors = None            # impacts.Receptors
        self.impact_look_ahead_mi = 5.0
        self.structures = []             # structures.Structure
        self.road_lines = []             # road polylines in the work CRS (culvert guesses)
        self.rail_lines = []             # rail polylines in the work CRS (gmsh embankment lines)
        self.culvert_candidates = []     # listed in culvert_candidates.csv; not cut into the DEM
        # culvert matching: inventory records (culvert_guess.inventory_record dicts, work CRS)
        # size a guess within culvert_match_m; matched guesses are cut into the DEM only
        # when culvert_burn_matched is set (screening values chosen by the engineer)
        self.culvert_inventory = []
        self.culvert_match_m = 75.0
        self.culvert_burn_matched = False
        self.invert_tol_m = 3.0 * 0.3048  # 3.5: inventory inverts within this of the LiDAR bed are used
        self.edits = None                # conditioning.Edits (channels, ponds, notches)
        self.refine_lines = []           # polylines used as ANUGA mesh edges
        # outputs and run control
        self.products = True
        self.arrival_contours_hr = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
        self.resume = False
        self.gauges = []                 # [{"name", "xy"}]: discharge past these points
        self.gauge_massint_s = 60.0
        self.fixed_plan = None           # dict from a previous run: reuse inflow point and domain
        self.write_final = True          # write the rasters into the dam folder
        self.ambient_cfs = 0.0           # steady stream flow when the event starts (added at the inflow)
        self.spinup_hr = 2.0             # ambient flow runs this long before the breach hydrograph
        self.input_files = {}            # {label: path} of the inputs, recorded in the run manifest
        self.breach_xy = None            # point on the crest where the breach starts (default: dam_xy)
        self.inflow_width_m = None       # width the inflow is spread over (default: the breach width)
        self.user_hydrograph_obj = None  # breach.Hydrograph used as given (nested runs)
        self.user_baseline_obj = None    # its no-breach counterpart, for the incremental rasters
        self.snapshot_frames = 0         # >0: depth snapshots for an animation (about this many frames)
        self.animation = False           # render GIF / MP4 / frame files from the snapshots
        self.keep_arrays = False         # keep the result arrays on the RunResult
        # v3.2.0: stop extending the domain when the class stops changing (stoprule.py);
        # off by default. The values are ND Dam Hazard's termination-point defaults:
        # screening values chosen by the engineer, not requirements.
        self.class_stop = False
        self.class_stop_threshold_ft = stoprule.DEFAULT_THRESHOLD_FT
        self.class_stop_confirm = stoprule.DEFAULT_CONFIRM
        self.class_stop_station_mi = stoprule.DEFAULT_STATION_MI
        self.class_stop_search_ft = stoprule.DEFAULT_SEARCH_FT
        # v3.2.0: this tool's EAP map is a preview; ND Dam Hazard's map book is the EAP map.
        # "auto" = draw the preview only when ND Dam Hazard is not installed, "mapbook" =
        # never, "svg" = always (products.EAP_CHOICES)
        self.eap_map = "auto"
        # v3.2.0: write <dam folder>/dam.json after the run (the rundam / batch tools set it):
        # None, or {"point": {"lon", "lat", "x", "y", "crs"}, "read": [log lines],
        # "top_source": "parameter" | "record", "nid_id": text}
        self.dam_record = None
        # v3.4.0: LISFLOOD-FP floodplain solver keyword ("acceleration" everywhere by default;
        # "fv1" is what the near-dam refine uses for its fine run)
        self.lisflood_solver = lfmod.SOLVER_ACC
        # v3.4.0: near-dam refine (refine.py): when a receptor lies within refine_dist_mi of
        # the breach and the final rasters come from the ACC run, re-run that stretch on a
        # fine FV1 grid (refine_cell_m; 0 = half the main cell) and merge it in. Off by
        # default; the distance and cell are the engineer's screening values.
        self.refine_near = False
        self.refine_dist_mi = 1.0
        self.refine_cell_m = 0.0
        # v3.10.20: carve the reservoir bottom into the DEM from the storage curve where the
        # LiDAR shows a water surface (core/reservoir_carve.py): "off" (default) | "auto" |
        # "on"; NDBREACH_RESERVOIR_CARVE overrides the default
        self.reservoir_carve = _carve_default()
        # v3.10.22: HEC-RAS 2D computation points: "grid" (default) | "gmsh" (optional; falls
        # back to the grid without gmsh; core/hecras_seeds.py)
        self.hecras_mesh_seeds = "grid"
        # v3.10.22 (crest dam line): the HEC-RAS dam line: "crest" (default; traced along the
        # embankment crest from the DEM) | "straight" (the old line across the flow;
        # core/hecras_crest.py)
        self.hecras_dam_axis = "crest"
        # v3.10.27 (#91): water-body mask for the run report only (core/report_metrics.py): a
        # vector file path (several separated by ;;), a list of polygons in the work CRS, or
        # "off". None = NDBREACH_WATERBODY_MASK, else the dam folder's / the suite's waterbody
        # layer when there is one. Never changes the model.
        self.water_mask = None
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError("unknown setting {}".format(k))
            setattr(self, k, v)

    def as_dict(self):
        d = {}
        for k, v in self.__dict__.items():
            if k == "overrides":
                d[k] = v.__dict__
            elif k == "flood":
                d[k] = None if v is None else {
                    "trigger": v.trigger, "trigger_value": v.trigger_value,
                    "normal_pool_ft": v.normal_pool_ft, "crest_length_ft": v.crest_length_ft,
                    "spillway": v.spillway is not None, "inflow_peak_cfs": round(
                        float(v.inflow_q_cms.max()) / CFS, 0),
                    "baseline_csv": v.baseline_csv,
                    "outlets": [(c.name, c.invert_m, c.width_m, c.height_m, c.cd,
                                 c.schedule) for c in (v.outlets or [])],
                    "spillway_schedule": v.spillway_schedule}
            elif k == "receptors":
                d[k] = None if v is None else len(v)
            elif k == "structures":
                d[k] = [s.to_dict() for s in v]
            elif k == "road_lines":
                d[k] = [[(round(float(p[0]), 1), round(float(p[1]), 1))
                         for p in np.asarray(ln)] for ln in (v or [])]
            elif k == "culvert_candidates":
                continue
            elif k == "rail_lines":
                if v:
                    d[k] = len(v)
            elif k == "culvert_inventory":
                d[k] = len(v or [])
            elif k == "edits":
                d[k] = None if v is None else {"channels": len(v.channels), "ponds": len(v.ponds),
                                              "notches": len(v.notches),
                                              "tailwater": [round(float(x.wse_m), 3) for x in v.stages],
                                              "buildings": len(getattr(v, "buildings", [])),
                                              "embankments": len(getattr(v, "embankments", []))}
            elif k in ("refine_lines", "fixed_plan"):
                d[k] = len(v) if k == "refine_lines" else (v is not None)
            elif k == "dam_record":
                d[k] = v is not None
            elif k in ("anuga_mesher", "anuga_gmsh") and v in ("default", None):
                continue                 # default mesh: the report stays as before
            elif (k == "anuga_elevation" and v == "fine") or (k == "anuga_elevation_cell_m"
                                                              and not v):
                continue                 # 3.10.31 defaults: run_report["anuga_elevation_source"]
            elif k in ANUGA_CHECKPOINT_DEFAULTS and v == ANUGA_CHECKPOINT_DEFAULTS[k]:
                continue                 # checkpoint defaults: run_report["anuga_checkpoint"]
            elif k == "reservoir_carve" and v in ("off", None, ""):
                continue                 # v3.10.20: off - the report stays as before
            elif k == "hecras_mesh_seeds" and v in ("grid", None, ""):
                continue                 # v3.10.22: the default - the report stays as before
            elif k == "hecras_dam_axis" and v in ("crest", None, ""):
                continue                 # the default - the report stays as before
            elif k == "water_mask":
                if v in (None, ""):
                    continue             # v3.10.27: not set - the report stays as before
                d[k] = v if isinstance(v, str) else "polygons ({})".format(len(v))
            else:
                d[k] = v
        return d


_CLASS_STOP_KEYS = ("class_stop", "class_stop_threshold_ft", "class_stop_confirm",
                    "class_stop_station_mi", "class_stop_search_ft")


def key_settings(st):
    """st.as_dict() for checkpoint / run keys: without "resume", the EAP-map choice and the
    dam.json switch (they do not change the flood), and without the v3.2.0 class-stop
    settings while that rule is off - so a checkpoint of 3.1.x still resumes."""
    d = {k: v for k, v in st.as_dict().items() if k not in ("resume", "eap_map", "dam_record",
                                                            *ANUGA_CHECKPOINT_DEFAULTS,
                                                            "hecras_mesh_seeds",
                                                            "hecras_dam_axis",
                                                            "water_mask")}
    if not getattr(st, "class_stop", False):
        for k in _CLASS_STOP_KEYS:
            d.pop(k, None)
    # v3.4.0 keys are left out while they are at their defaults, so a 3.3.x checkpoint resumes
    if not getattr(st, "refine_near", False):
        for k in _REFINE_KEYS:
            d.pop(k, None)
    if getattr(st, "lisflood_solver", lfmod.SOLVER_ACC) == lfmod.SOLVER_ACC:
        d.pop("lisflood_solver", None)
    ed = d.get("edits")
    if isinstance(ed, dict) and not ed.get("buildings"):
        d["edits"] = {k: v for k, v in ed.items() if k != "buildings"}
    ed = d.get("edits")
    if isinstance(ed, dict) and not ed.get("embankments"):
        d["edits"] = {k: v for k, v in ed.items() if k != "embankments"}
    return d


_REFINE_KEYS = ("refine_near", "refine_dist_mi", "refine_cell_m")
# 3.10.38: ANUGA checkpoint settings and their defaults (automatic). They do not change the flood:
# left out of the run key and, at these values, of the settings in run_report.
ANUGA_CHECKPOINT_DEFAULTS = {"anuga_checkpoint_min": None, "anuga_checkpoint_steps": 0,
                             "anuga_checkpoint_keep": 2, "anuga_partial_min": None,
                             "anuga_resume": False}


def _env_number(name, default):
    """NDBREACH_<name> (or <name>) as a float; default when unset or not a number."""
    v = os.environ.get("NDBREACH_" + name) or os.environ.get(name)
    if v is not None and v.strip().lower() in ("off", "no", "false"):
        return 0.0
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _env_flag(name):
    v = os.environ.get("NDBREACH_" + name) or os.environ.get(name) or ""
    return v.strip().lower() in ("1", "true", "yes", "on")


class RunResult:
    def __init__(self):
        self.dam_id = ""
        self.folder = ""
        self.final = {}           # kind -> path
        self.baseline = {}        # kind -> path (flood scenario)
        self.increment = {}       # kind -> path (flood scenario)
        self.qa = {}              # model -> {kind: path}
        self.report_md = ""
        self.report_json = ""
        self.report_html = ""
        self.hydrograph_csv = ""
        self.summary = {}
        self.warnings = []
        self.plan = None
        self.grid = None          # Grid of the final arrays
        self.final_si = None      # {kind: array} metres, m/s, hours, metres
        self.base_si = None
        self.gauges = {}          # name -> (t_s, q_cms)
        self.gauges_base = {}     # the same for the no-breach baseline run
        self.vectors = {}
        self.receptor_csv = ""
        self.eap_map = ""
        self.flood = None         # flood.FloodResult
        self.t_ref_hr = 0.0       # breach initiation within the record (flood scenario)
        self.cascade_offset_h = 0.0
        self.animation = None     # animation.AnimationResult when frames were rendered
        self.manifest = ""        # path of run_manifest.json


class _Skip(Exception):
    """The dam does not fail in this flood: nothing to model."""


def _auto_sim_time(duration_s, bounds, st):
    if st.sim_time_hr:
        return st.sim_time_hr * 3600.0
    w, h = terrain.bounds_size(bounds)
    t = duration_s + math.hypot(w, h) / st.front_speed_mps
    return float(min(MAX_SIM_S, max(2 * 3600.0, t)))


def _to_us(kind, a):
    """Model results are SI; the rasters are ft, ft/s and hours."""
    if kind in ("depth", "velocity", "wse"):
        return a / FT
    return a


def _final_arrays(env, arrival_m, t_ref_hr=0.0, init=None):
    """Result arrays (SI) with the wet/dry rules applied. `init` is the initial
    water depth of ponds (m): the depth shown is the water the flood adds above
    it. Arrival times count from breach initiation (t_ref_hr)."""
    d = env.depth.data
    if init is not None:
        d = np.where(np.isfinite(d), np.clip(d - np.nan_to_num(init.data), 0.0, None), d)
    wet = np.isfinite(d) & (d > WET_M)
    arr = env.arrival_hr.data - t_ref_hr
    arr = np.where(np.isfinite(arr), np.clip(arr, 0.0, None), np.nan)
    out = {
        "depth": np.where(wet, d, np.nan),
        "velocity": np.where(wet, env.velocity.data, np.nan),
        "arrival": np.where(wet & (d >= arrival_m - lfmod.ARRIVAL_TOL_M), arr, np.nan),
        "wse": np.where(wet, env.wse.data, np.nan),
    }
    return out


def compare(a_env, b_env, thr_m):
    """Agreement between two flood extents on the same grid."""
    da, db = a_env.depth.data, b_env.depth.data
    wa = np.isfinite(da) & (da >= thr_m)
    wb = np.isfinite(db) & (db >= thr_m)
    both = wa & wb
    union = wa | wb
    cell = a_env.depth.dx ** 2 / 4046.8564224
    out = {"lisflood_acres": round(float(wa.sum()) * cell, 1),
           "anuga_acres": round(float(wb.sum()) * cell, 1),
           "both_acres": round(float(both.sum()) * cell, 1),
           "fit_index_F": round(float(both.sum()) / max(1, union.sum()), 3)}
    if both.any():
        diff = (db - da)[both] / FT
        out.update({"depth_diff_mean_ft": round(float(diff.mean()), 2),
                    "depth_diff_abs_p90_ft": round(float(np.percentile(np.abs(diff), 90)), 2)})
        va, vb = a_env.velocity.data[both], b_env.velocity.data[both]
        out["velocity_diff_mean_fps"] = round(float(np.nanmean(vb - va)) / FT, 2)
    return out


def _direction_at(path, xy, span=200.0):
    """Unit vector of the flow path around the vertex nearest `xy`."""
    p = np.asarray(path)
    i = int(np.argmin(np.hypot(p[:, 0] - xy[0], p[:, 1] - xy[1])))
    step = max(1, int(round(span / max(1.0, np.hypot(*(p[1] - p[0])) if len(p) > 1 else 1.0))))
    a, b = p[max(0, i - step)], p[min(len(p) - 1, i + step)]
    d = b - a
    n = float(np.hypot(*d)) or 1.0
    return (d[0] / n, d[1] / n), i


def _hash(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def _span(progress, lo, hi):
    def f(p, m=""):
        progress(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0, m)
    return f


class _State:
    """Checkpoint after each LISFLOOD-FP run so an interrupted dam can resume."""

    def __init__(self, model_dir, key, enabled):
        self.path = os.path.join(model_dir, STATE_FILE)
        self.key = key
        self.enabled = enabled
        self.data = {"key": key}
        if enabled and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    old = json.load(f)
                if old.get("key") == key:
                    self.data = old
                    self.resumed = True
                    return
            except (OSError, ValueError):
                pass
        self.resumed = False

    def save(self, **kw):
        self.data.update(kw)
        self.data["key"] = self.key
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, default=_json_default)

    def get(self, k, default=None):
        return self.data.get(k, default) if self.resumed else default


def attach_proposed(st, out_folder, dam, warnings, log):
    """Insert a proposed embankment from <out_folder>/dam.json onto every grid the
    models read. Nothing is done when the record has no such block, so a run of an
    existing dam keeps the same checkpoint key."""
    from . import damjson
    from . import proposed as prop
    rec, problem = damjson.load(out_folder)
    if problem or rec is None:
        return
    spec = (rec.extra or {}).get("proposed_embankment")
    if not isinstance(spec, dict) or spec.get("phrase") != prop.PHRASE:
        return
    if not spec.get("line_xy") or spec.get("crest_m") in (None, ""):
        warnings.append("dam.json names a proposed embankment but has no crest line; "
                        "it was not inserted.")
        return
    st.proposed = spec
    if getattr(st, "edits", None) is None:
        st.edits = cond.Edits()
    if not any(isinstance(e, cond.EmbankmentInsert) for e in st.edits.embankments):
        st.edits.embankments.append(cond.EmbankmentInsert(spec))
    if not getattr(dam, "crest_length_ft", None) and spec.get("crest_len_m"):
        dam.crest_length_ft = float(spec["crest_len_m"]) / FT
        log("Crest length from the proposed embankment: {:.1f} ft.".format(dam.crest_length_ft))
    if prop.PHRASE not in warnings:
        warnings.append(prop.PHRASE)
    kind = spec.get("rim_kind")
    if kind and kind != "contained":
        rim = spec.get("rim_m")
        warnings.append(
            "Proposed dam: the pool leaves the basin ({}) below the design crest{}.".format(
                str(kind).replace("_", " "),
                "" if rim is None else " (rim {:.1f} ft)".format(float(rim) / FT)))



def _write_pass_products(out_folder, dem, env, arrival_m, writer, it):
    """Depth, velocity, and arrival rasters plus polygons for one finished pass.

    LISFLOOD has already written run.max, run.maxVc, and run.inittm. This copies
    them into the dam folder so a domain that is still growing still leaves a result.
    """
    import numpy as np
    from . import products
    os.makedirs(out_folder, exist_ok=True)
    crs = getattr(dem, "crs_wkt", None)

    def grid(obj):
        if obj is None:
            return None
        return np.asarray(getattr(obj, "data", obj), float)

    depth, vel, arr = grid(env.depth), grid(getattr(env, "velocity", None)), grid(getattr(env, "arrival", None))
    names = {"depth": "Depth (Max).tif", "velocity": "Velocity (Max).tif", "arrival": "Arrival Time.tif"}
    show = {}
    if depth is not None:
        show["depth"] = np.where(np.isfinite(depth) & (depth > 0), depth / FT, np.nan)
    if vel is not None:
        show["velocity"] = np.where(np.isfinite(vel) & (vel > 0), vel / FT, np.nan)
    if arr is not None:
        show["arrival"] = np.where(np.isfinite(arr) & (arr >= 0), arr / 3600.0, np.nan)
    for kind, a in show.items():
        writer(dem.like(a, np.nan), os.path.join(out_folder, names[kind]))
    with open(os.path.join(out_folder, "pass_{}_checkpoint.txt".format(it)), "w", encoding="utf-8") as f:
        f.write("Pass {} checkpoint. These rasters are this pass, not a finished flood.\n".format(it))
    if depth is None:
        return
    wet = np.isfinite(depth) & (depth >= arrival_m)
    layers = {}
    ext = products.mask_polygons(dem, wet, min_cells=4, simplify=0.8)
    if ext:
        props = {"pass": it, "kind": "extent", "arrival_depth_ft": round(arrival_m / FT, 2)}
        products.write_geojson(os.path.join(out_folder, "Flood Extent.geojson"),
                               [products.geojson_polygons(ext, props)], crs)
        layers["flood_extent"] = ("polygon", [(ext, props)])
    if vel is not None:
        vfeats, vlayer = [], []
        for lo, hi, label in ((0.0, 2.0, "0-2 ft/s"), (2.0, 5.0, "2-5 ft/s"), (5.0, 1e9, "over 5 ft/s")):
            mk = wet & np.isfinite(vel) & (vel / FT >= lo) & (vel / FT < hi)
            polys = products.mask_polygons(dem, mk, min_cells=2, simplify=0.8)
            if not polys:
                continue
            props = {"pass": it, "kind": "velocity", "class": label}
            vfeats.append(products.geojson_polygons(polys, props))
            vlayer.append((polys, props))
        if vfeats:
            products.write_geojson(os.path.join(out_folder, "Velocity Polygons.geojson"), vfeats, crs)
            layers["velocity"] = ("polygon", vlayer)
    if arr is not None:
        hours = np.where(np.isfinite(arr) & (arr >= 0), arr / 3600.0, np.nan)
        az = products._arrival_polys(dem, hours, (0.5, 1, 2, 4, 8))
        if az:
            feats, alayer = [], []
            for h, polys in az:
                props = {"pass": it, "kind": "arrival", "within_hr": h}
                feats.append(products.geojson_polygons(polys, props))
                alayer.append((polys, props))
            products.write_geojson(os.path.join(out_folder, "Arrival Zones.geojson"), feats, crs)
            layers["arrival_zones"] = ("polygon", alayer)
    if layers:
        products.write_gpkg(os.path.join(out_folder, "Breach Products.gpkg"), layers, crs)


def model_dem(terrain_src, structures, model, bounds, cell, refill_paths=()):
    """The DEM an engine gets: the (conditioned) terrain read at the model's cell size, the
    refill paths, then the structure cuts applied AT THAT CELL SIZE (after any resampling,
    so an averaged or bilinear read cannot wash a narrow cut out)."""
    g = terrain_src.read(bounds, cell)
    if refill_paths:
        from . import dem_refill
        for _p in refill_paths:
            g = dem_refill.fill_grid(g, _p)
    cuts = structmod.structure_edits(structures or [], cell, model)
    if cuts:
        g, _, _ = cond.Edits(notches=cuts).apply(g)
    return g


def terrain_native_cell(terrain_src):
    """The terrain source's own cell size in work-CRS metres (None when unknown):
    terrain_src.native_cell_m() through the wrappers (conditioning, refill, carve)."""
    fn = getattr(terrain_src, "native_cell_m", None)
    if fn is None:
        return None
    try:
        v = fn()
        return float(v) if v else None
    except Exception:                               # noqa - unknown: prepare_elevation uses 2 m
        return None


def terrain_label(terrain_src):
    """A short name of the terrain source for the report (its DEM path when known)."""
    p = getattr(terrain_src, "dem_path", None)
    if p is None and hasattr(terrain_src, "base"):
        p = getattr(terrain_src.base, "dem_path", None)
    return str(p) if p else None


def anuga_checkpoint_line(ck):
    """One line for run_report.md from run_report["anuga_checkpoint"] (3.10.35 / 3.10.38)."""
    if ck.get("every_min"):
        how = "every {:g} min of wall time".format(ck["every_min"])
    elif ck.get("every_steps"):
        how = "every {} yield steps".format(ck["every_steps"])
    else:
        how = "none written"
    if (ck.get("rule") or {}).get("mode") == "auto":
        how += " (automatic: long run, {})".format(ck["rule"]["rule"])
    parts = [how, "{} saved".format(ck.get("checkpoints_saved", 0))]
    if ck.get("checkpoint_error"):
        parts.append("OFF: " + ck["checkpoint_error"])
    if ck.get("partial_flushes"):
        parts.append("partial results flushed {} times".format(ck["partial_flushes"]))
    if ck.get("resumed_from_t_s") is not None:
        parts.append("resumed from t = {:.2f} h".format(ck["resumed_from_t_s"] / 3600.0))
    elif ck.get("resume_skipped"):
        parts.append("not resumed: " + ck["resume_skipped"])
    return "; ".join(parts)


def anuga_threads_line(th):
    """One line for run_report.md from run_report["anuga_threads"] (3.10.47)."""
    txt = "{} OpenMP thread(s) ({})".format(th.get("used"), th.get("rule", ""))
    rep = th.get("anuga_reported")
    if rep is not None and rep != th.get("used"):
        txt += "; ANUGA reported {}".format(rep)
    if th.get("note"):
        txt += "; " + th["note"]
    return txt


def max_extent_warning(max_extent_mi, over):
    """The warning when the domain hits the largest-domain cap with the flood still leaving.
    The default cap is 20 mi (60 mi before 2026-10); say so, so a large dam's truncated run is
    not read as complete (audit 2026-10-15)."""
    return ("TRUNCATED: the domain reached the maximum extent ({:.0f} mi) with the flood still "
            "leaving through {}; results (extent, depths, consequences) are cut off there and the "
            "hazard may be understated. Raise 'Largest domain side (mi)' (the default was 60 mi "
            "before 2026-10) and re-run.".format(float(max_extent_mi or 0), ", ".join(over)))


def run_dam(dam, dam_xy, st, terrain_src, writer, out_folder, inflow_xy=None,
            progress=None, log=None, cancel=None):
    """dam: breach.DamData; dam_xy / inflow_xy in the working (metric) CRS.
    terrain_src.read(bounds, cell) -> Grid of elevations in metres (NaN = no data)
    terrain_src.manning(bounds, cell) -> Grid or None
    terrain_src.z_to_m : factor that converts DEM elevations to metres
    writer(grid, path) writes a raster in the output CRS."""
    t_start = time.time()
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or (lambda: False)
    if st.breach_xy is not None:
        dam_xy = (float(st.breach_xy[0]), float(st.breach_xy[1]))
    res = RunResult()
    res.dam_id = dam.dam_id
    res.folder = out_folder
    model_dir = os.path.join(out_folder, MODEL_FOLDER)
    os.makedirs(model_dir, exist_ok=True)
    warnings = []
    flood_mode = st.scenario == SCENARIO_FLOOD
    report = {"tool": "ND Dam Breach Runner " + VERSION,
              "run_started": datetime.datetime.now().isoformat(timespec="seconds"),
              "dam": brmod.dam_to_dict(dam), "dam_xy": list(dam_xy),
              "settings": st.as_dict(), "scenario": st.scenario,
              # v3.10.16: what the HEC-RAS model builder (core/hecras_model) needs to put the
              # run back in place: the model CRS and the DEM's elevation unit (m per unit)
              "work_crs": _work_crs_text(writer, terrain_src),
              "dem_z_to_m": _z_to_m_of(terrain_src),
              "policy": "North Dakota Dam Safety Standards Policy REG_05.2026, Section 5.1 "
                        "(breach analysis) and 5.4 (outputs)"}
    rs = _dem_resample_of(terrain_src)
    if rs is not None:
        report["dem_resample"] = rs
    arrival_m = st.arrival_depth_ft * FT
    tol_m = st.edge_tolerance_ft * FT
    cs = st.cell_size_m

    # DEM edits (channels, ponds, notches, v3.4.0 buildings, v3.7.0 a proposed
    # embankment from dam.json) apply to everything the models read
    raw_terrain = terrain_src                  # the near-dam refine run wraps the edits itself
    attach_proposed(st, out_folder, dam, warnings, log)
    if st.edits is not None and not st.edits.empty():
        terrain_src = cond.ConditionedTerrain(terrain_src, st.edits, default_n=st.manning_n)
    has_ponds = st.edits is not None and st.edits.has_water()

    def manning_for(bnds, cell):
        man = terrain_src.manning(bnds, cell) if hasattr(terrain_src, "manning") else None
        if man is not None and getattr(st, "manning_scale", 1.0) != 1.0:
            man = man.like(np.where(man.valid(), man.data * st.manning_scale, man.data))
        return man

    refill_paths = []

    def dem_for(model, bounds, cell):
        return model_dem(terrain_src, st.structures, model, bounds, cell, refill_paths)

    # --- 3.5: inventory inlet / outlet elevations against the LiDAR -------------
    if st.structures:
        try:
            inv_notes = structmod.reconcile_inverts(st.structures, terrain_src.read,
                                                    getattr(st, "invert_tol_m", 3.0 * FT), min(cs, 3.0))
        except Exception as e:                                   # noqa - never stop the run for this
            inv_notes = ["inventory inverts not checked: {}".format(e)]
        for n in inv_notes:
            log(n)
        if inv_notes:
            report["structure_inverts"] = inv_notes

    # --- top of dam from the DEM if not given ---------------------------------
    near = terrain_src.read((dam_xy[0] - 60, dam_xy[1] - 60, dam_xy[0] + 60, dam_xy[1] + 60),
                            min(cs, 3.0))
    z_note = getattr(terrain_src, "z_note", "")
    report["dem_elevation_units"] = z_note
    if "check" in z_note.lower():
        warnings.append("DEM elevation units: {}. Set 'DEM elevation units' explicitly if this "
                        "is wrong; a feet DEM read as metres gives a wrong flood without "
                        "any error.".format(z_note))
    z = near.masked()
    X, Y = np.meshgrid(near.x_centers(), near.y_centers())
    spec = getattr(st, "proposed", None)
    if spec:
        from .proposed import crest_check
        crest_dem_ft, err = crest_check(
            z, X, Y, dam_xy, float(spec["crest_m"]), float(spec.get("crest_width_m") or 0.0))
        if err:
            raise brmod.BreachInputError("{}: {}".format(dam.dam_id, err))
    else:
        # the 98th percentile of the DEM within 30 m, not the single highest cell (nanmax):
        # nanmax can latch onto a nearby hill, road embankment or spike and read the crest
        # several feet high (matching the Screener's estimate_storage percentile approach)
        sel = np.hypot(X - dam_xy[0], Y - dam_xy[1]) <= 30.0
        fin_z = z[sel & np.isfinite(z)]
        crest_dem_ft = float(np.nanpercentile(fin_z, 98)) / FT if fin_z.size else None
        if crest_dem_ft is None:
            raise brmod.BreachInputError("{}: no DEM data at the dam point".format(dam.dam_id))
        report["dem_crest_from"] = "DEM near the dam point (98th percentile, 30 m)"
    report["dem_crest_ft"] = round(crest_dem_ft, 2)
    if not spec and dam.top_of_dam_ft is not None and dam.dam_height_ft:
        diff = crest_dem_ft - dam.top_of_dam_ft
        if abs(diff) > max(0.6 * dam.dam_height_ft, 8.0):
            msg = ("{}: the crest read from the DEM ({}) is {:.1f} ft but the "
                   "top of dam is {:.1f} ft (difference {:+.1f} ft, dam height {:.1f} ft). "
                   "The inventory crest is kept. Check the datum and that the point is on the "
                   "crest. DEM units: {}."
                   ).format(dam.dam_id, report.get("dem_crest_from", "DEM near the dam point"),
                            crest_dem_ft, dam.top_of_dam_ft, diff,
                            dam.dam_height_ft, z_note or "unknown")
            warnings.append(msg)
            report["crest_source"] = "inventory crest; DEM near the point differs"
        if abs(diff) > 3.0:
            warnings.append("The DEM crest near the dam point ({:.1f} ft) differs from the top "
                            "of dam ({:.1f} ft) by {:+.1f} ft.".format(
                                crest_dem_ft, dam.top_of_dam_ft, diff))
    top_from_dem = dam.top_of_dam_ft is None
    if dam.top_of_dam_ft is None:
        dam.top_of_dam_ft = crest_dem_ft
        warnings.append("Top of dam not given: estimated from the {} ({:.1f} ft). Check it "
                        "against the as-built crest.".format(
                            report.get("dem_crest_from", "DEM near the dam point"),
                            dam.top_of_dam_ft))
        report["dam"]["top_of_dam_ft"] = dam.top_of_dam_ft

    # --- breach hydrograph(s) ------------------------------------------------------
    progress(1, "breach hydrograph")
    hy_nb = None
    fr = None
    if st.user_hydrograph_obj is not None:
        hy, hy_nb = st.user_hydrograph_obj, st.user_baseline_obj
        warnings.append("The inflow hydrograph was handed over from another model run and is "
                        "used as given.")
    elif flood_mode:
        hy, hy_nb, fr = _flood_hydrographs(dam, st, warnings)
        res.flood = fr
        report["flood"] = fr.summary if fr is not None else {"user_hydrographs": True}
        if hy is None:
            report["summary"] = {"dam_id": dam.dam_id, "final_model": "none",
                                 "breach_occurs": False, "flooded_acres": 0}
            warnings += fr.notes
            if st.dam_record is not None and st.write_final:
                report["dam_record"] = _record_dam(dam, st, res, dam_xy, out_folder,
                                                   top_from_dem, "none", warnings, log,
                                                   crest_from=report.get("dem_crest_from", ""))
            _write_no_breach(res, report, warnings, model_dir, hy_nb)
            return res
    else:
        hy = brmod.make_hydrograph(dam, st.breach_method, st.overrides, st.user_hydrograph)
    amb_cms = max(0.0, float(st.ambient_cfs or 0.0)) * CFS
    spin_s = max(0.0, float(st.spinup_hr)) * 3600.0 if amb_cms > 0 else 0.0
    res.hydrograph_csv = os.path.join(model_dir, "breach_hydrograph.csv")
    hy.to_csv(res.hydrograph_csv)                   # as computed, before any spin-up shift
    hy_summary = hy.summary()                       # the report and the HEC-RAS export use this
    hy_pre, hy_nb_pre = hy, hy_nb                   # before the spin-up shift (refine run)
    if amb_cms > 0:
        if hy_nb is None and st.baseline:
            hy_nb = _zero_hydrograph(hy, "no breach (ambient flow only)")
        if hy_nb is not None:
            hy_nb.to_csv(os.path.join(model_dir, "no_breach_hydrograph.csv"))
        hy, hy_nb = _shift(hy, spin_s), _shift(hy_nb, spin_s)
        hy.to_csv(os.path.join(model_dir, "model_inflow_hydrograph.csv"))
        warnings.append(
            "Ambient stream flow of {:,.0f} cfs is added at the breach inflow from the start, "
            "with {:.1f} h of spin-up before the breach hydrograph. The depth and flooded "
            "area include the water the ambient flow already carries; the incremental rasters "
            "(with the breach minus ambient flow only) show what the failure adds.".format(
                st.ambient_cfs, st.spinup_hr))
        report["ambient"] = {"cfs": st.ambient_cfs, "spinup_hr": st.spinup_hr,
                             "breach_starts_in_model_at_hr": (hy.t_ref_s / 3600.0)}
    elif hy_nb is not None:
        hy_nb.to_csv(os.path.join(model_dir, "no_breach_hydrograph.csv"))
    t_ref_hr = hy.t_ref_s / 3600.0
    res.t_ref_hr = t_ref_hr
    has_base = bool(hy_nb is not None and st.baseline and (
        flood_mode or amb_cms > 0 or st.user_baseline_obj is not None))
    report["hydrograph"] = hy_summary
    warnings += hy.notes
    log("Breach hydrograph ({}): peak {:.0f} cfs at {:.2f} h{}, {:.0f} ac-ft".format(
        hy.method, hy.peak_cfs, hy.time_to_peak_hr,
        " after initiation (breach at {:.2f} h of the flood)".format(t_ref_hr) if flood_mode
        else "", hy.volume_m3 / ACFT))

    state = _State(model_dir, _hash(brmod.dam_to_dict(dam), dam_xy, key_settings(st),
                                    hy.summary(), inflow_xy), st.resume)
    if st.resume and state.resumed:
        log("Resuming from the checkpoint in {}".format(model_dir))
    elif st.resume:
        log("No matching checkpoint (nothing saved, or the settings changed): starting over.")

    # --- inflow point -------------------------------------------------------------
    h_m = dam.dam_height_ft * FT
    top_m = dam.top_of_dam_ft * FT
    plan = st.fixed_plan
    qa_ctx = {"breach_xy": tuple(dam_xy), "local": None, "radius_m": None}
    if plan is not None:
        inflow_xy = plan["inflow_xy"]
        report["inflow_point"] = {"xy": list(inflow_xy), "how": "from the base run"}
    else:
        rad = max(90.0, 8.0 * h_m)
        search_rad = max(rad, 400.0)
        search_cell = min(cs, 20.0)
        local = terrain_src.read((dam_xy[0] - search_rad - 30, dam_xy[1] - search_rad - 30,
                                  dam_xy[0] + search_rad + 30, dam_xy[1] + search_rad + 30),
                                 search_cell)
        qa_ctx["local"], qa_ctx["radius_m"] = local, rad
        if inflow_xy is None:
            ds_info = {}
            inflow_xy, why = _downstream_inflow(terrain_src, local, dam, dam_xy, h_m, top_m, rad,
                                                search_cell, warnings, log, out=ds_info)
            ds_dir = ds_info.get("direction")
            qa_ctx["downstream"] = ds_dir
            if inflow_xy is None:
                inflow_xy, crest, why = terrain.place_from_crest(local, dam_xy, h_m, top_m,
                                                                 search_rad, downstream=ds_dir)
                if inflow_xy is None:
                    raise brmod.BreachInputError("{}: could not place the breach inflow: {}"
                                                 .format(dam.dam_id, why))
                if crest is not None:
                    dam_xy = crest
                    report["crest_xy"] = [float(crest[0]), float(crest[1])]   # v3.10.16
                    warnings.append("Dam point was not on the embankment; crest moved to the "
                                    "nearest ground at the top of dam within {:.0f} m.".format(
                                        search_rad))
            report["inflow_point"] = {"xy": list(inflow_xy), "how": why}
        else:
            report["inflow_point"] = {"xy": list(inflow_xy), "how": "given"}
    log("Breach inflow at {:.1f}, {:.1f} ({})".format(inflow_xy[0], inflow_xy[1],
                                                      report["inflow_point"]["how"]))
    # --- v3.10.20: reservoir bottom carved from the storage curve (opt-in) -----------
    # the engines (model_dem: LISFLOOD-FP, ANUGA, the refine run) and the HEC-RAS terrain read
    # the carved copy from here on; the source DEM is never written
    from . import reservoir_carve as rcarve
    terrain_src, raw_terrain = rcarve.run_hook(
        st, dam, dam_xy, inflow_xy, terrain_src, raw_terrain, model_dir, report, warnings, log,
        downstream=qa_ctx.get("downstream"), crs_wkt=report.get("work_crs"))

    # --- initial domain from a flow trace --------------------------------------
    progress(3, "tracing the flow path")
    L = st.initial_length_mi * MILE
    W = st.half_width_mi * MILE
    if plan is not None:
        path, direction, bounds = plan["path"], plan["direction"], tuple(plan["bounds"])
        plen = plan.get("trace_length_m", 0.0)
    else:
        half = L + W + 2 * st.planning_cell_m
        pl = terrain_src.read((inflow_xy[0] - half, inflow_xy[1] - half,
                               inflow_xy[0] + half, inflow_xy[1] + half), st.planning_cell_m)
        path, plen = terrain.trace_downstream(pl, inflow_xy, L)
        direction = terrain.flow_direction(path)
        bounds = terrain.path_bounds(path + [dam_xy], W, snap=cs)
        d_start = math.hypot(path[0][0] - dam_xy[0], path[0][1] - dam_xy[1])
        d_end = math.hypot(path[-1][0] - dam_xy[0], path[-1][1] - dam_xy[1])
        if plen < 0.25 * L or d_end < d_start + 150.0:
            warnings.append(
                "The steepest-descent path from the breach inflow point is only {:.0f} m long "
                "and ends {:.0f} m from the dam. The inflow point may be in the reservoir or in "
                "a closed basin; give the breach inflow point explicitly (just below the "
                "dam).".format(plen, d_end))
    report["trace"] = {"length_mi": round(plen / MILE, 2), "points": len(path)}
    _check_inflow_site(report, st, dam, dam_xy, inflow_xy, path, qa_ctx.get("downstream"),
                       plan is not None, warnings, log)
    np.savetxt(os.path.join(model_dir, "flow_trace.csv"), np.asarray(path), delimiter=",",
               header="x,y", comments="", fmt="%.2f")
    candidates = list(getattr(st, "culvert_candidates", None) or [])
    if not candidates:
        roads = getattr(st, "road_lines", None) or []
        if roads and len(path) >= 2:
            from .culvert_guess import guess_crossings
            candidates = guess_crossings(
                [[(float(p[0]), float(p[1])) for p in np.asarray(ln)] for ln in roads],
                [[(float(p[0]), float(p[1])) for p in path]])
    if candidates:
        from . import culvert_guess as cgmod
        match_m = float(getattr(st, "culvert_match_m", cgmod.MATCH_M) or cgmod.MATCH_M)
        # guesses and STRUCTURES together: a guess near an inventory structure is dropped
        if st.structures:
            candidates, dropped = cgmod.drop_near_structures(candidates, st.structures, match_m)
            if dropped:
                log("{} culvert guess(es) within {:.0f} m of a STRUCTURES line dropped".format(
                    len(dropped), match_m))
        inv = getattr(st, "culvert_inventory", None) or []
        if inv and candidates:
            candidates, mnotes = cgmod.match_inventory(candidates, inv, match_m)
            for n in mnotes:
                log(n)
                warnings.append(n)
    if candidates:
        from . import culvert_guess as cgmod
        n_match = sum(1 for c in candidates if c.get("match_m") is not None)
        burn = bool(getattr(st, "culvert_burn_matched", False))
        csv_path = cgmod.write_candidates_csv(
            os.path.join(model_dir, "culvert_candidates.csv"), candidates)
        if n_match:
            if burn:
                burned = cgmod.matched_structures(candidates)
                try:
                    for n in cgmod.lidar_inverts(burned, terrain_src.read, min(cs, 1.0)):
                        log(n)
                except Exception as e:                           # noqa - never stop the run
                    log("matched culvert inverts not read from the LiDAR: {}".format(e))
                for m, b in zip([c for c in candidates if c.get("match_m") is not None],
                                burned):
                    m["invert_up_m"], m["invert_dn_m"] = b.invert_up_m, b.invert_dn_m
                st.structures = list(st.structures) + burned
            report["culvert_matches"] = cgmod.report_entries(candidates, burn)
            warnings.append(
                "{} culvert guess(es) took their size from an inventory record within {:.0f} m "
                "({}); listed in {}.".format(
                    n_match, float(getattr(st, "culvert_match_m", 75.0)),
                    "cut into the DEM" if burn else "not cut into the DEM; set "
                    "culvert_burn_matched to cut them", os.path.basename(csv_path)))
            log(warnings[-1])
        if len(candidates) > n_match:
            warnings.append(
                "{} culvert candidate(s) where a road crosses a channel or the flow path. "
                "They are listed in {} and are not cut into the DEM. "
                "The name starts with 'guess:'. They were not surveyed.".format(
                    len(candidates) - n_match, os.path.basename(csv_path)))
            log(warnings[-1])
    max_extent = st.max_extent_mi * MILE
    step = st.expand_step_mi * MILE
    duration_s = max(hy.duration_s, hy_nb.duration_s if hy_nb is not None else 0.0)
    sim_time = plan["sim_time"] if plan is not None else _auto_sim_time(duration_s, bounds, st)
    width = hy.params.get("avg_breach_width_ft")
    inlet_w = st.inflow_width_m or max(cs, (width or 100.0) * FT)
    inline = anmod.inlet_line(inflow_xy, direction, inlet_w)
    if st.sim_time_hr and st.sim_time_hr * 3600.0 < duration_s:
        warnings.append("The simulation duration ({:.1f} h) is shorter than the breach "
                        "hydrograph ({:.1f} h); the rest of the breach volume never enters the "
                        "model. Leave the duration automatic or lengthen it.".format(
                            st.sim_time_hr, duration_s / 3600.0))
    if st.structures:
        report["structures"] = structmod.describe(st.structures)
        # v3.10.16: the structures as the run used them (inverts after the LiDAR check, matched
        # culverts added), for the HEC-RAS model builder
        report["structures_used"] = [s.to_dict() for s in st.structures]
        json.dump([s.to_dict() for s in st.structures],
                  open(os.path.join(model_dir, "structures.json"), "w", encoding="utf-8"), indent=2)

    # --- LISFLOOD-FP run helper -----------------------------------------------------
    def gauge_list(bnds, gauges):
        gl = []
        for g in (gauges or []):
            gdir, _ = _direction_at(path, g["xy"])
            w_full = max(terrain.bounds_size(bnds))
            gg = lfmod.gauge_section(g["xy"], gdir, w_full)
            gg["name"] = g["name"]
            if not (bnds[0] < g["xy"][0] < bnds[2] and bnds[1] < g["xy"][1] < bnds[3]):
                msg = "Gauge '{}' is outside the model domain; no discharge record.".format(
                    g["name"])
                if msg not in warnings:
                    warnings.append(msg)
                continue
            gl.append(gg)
        return gl

    def extra_sources(sim_s):
        if amb_cms <= 0:
            return []
        return [{"name": "ambient", "xy": inflow_xy, "line": inline,
                 "t_s": [0.0, sim_s + 3600.0], "q_cms": [amb_cms, amb_cms]}]

    frames = int(st.snapshot_frames or (24 if st.animation else 0))

    def snap_for(sim_s):
        return max(60.0, round(sim_s / frames / 60.0) * 60.0) if frames > 0 else None

    def closed_edges():
        """H6 / v3.11.0: the near-dam FV1 refine run closes the box edges that face the
        reservoir, so an FV1 free edge cannot import reservoir water into the fine grid.
        Returns () for an ordinary run (st.close_upstream_edges unset)."""
        if getattr(st, "close_upstream_edges", False):
            from . import refine as refmod
            return refmod.upstream_edges(direction)
        return ()

    def lisflood_run(tag, hyd, bnds, sim_s, prog, gauges=None, snap=None):
        dem = dem_for("lisflood", bnds, cs)
        man = manning_for(bnds, cs)
        init = terrain_src.initial_depth(bnds, cs) if has_ponds else None
        gl = gauge_list(bnds, gauges)
        lst = lfmod.LisfloodSettings(st.lisflood_exe, st.threads, st.manning_n, man, arrival_m,
                                     sim_s, massint_s=st.gauge_massint_s if gl else 300.0,
                                     initial_depth=init, gauges=gl, snapshot_s=snap,
                                     extra_sources=extra_sources(sim_s),
                                     solver=getattr(st, "lisflood_solver", lfmod.SOLVER_ACC),
                                     closed_edges=closed_edges())
        wd = os.path.join(model_dir, "lisflood" + tag)
        if snap:        # snapshots of an earlier (smaller) run must not mix with these
            for _i, fp in lfmod.snapshot_files(wd):
                for ext in (".wd", ".Vx", ".Vy"):
                    try:
                        os.remove(fp[:-3] + ext)
                    except OSError:
                        pass
        top_m = (dam.top_of_dam_ft * FT) if getattr(dam, "top_of_dam_ft", None) else None
        if top_m and getattr(st, "raise_crest", True):
            nraise = lfmod.raise_crest(
                dem, dam_xy, inflow_xy, top_m,
                length_m=(dam.crest_length_ft or 400.0) * FT,
                line_xy=getattr(dam, "crest_line_xy", None))
            if nraise and "crest raised" not in " ".join(warnings):
                how = "along the dam line" if getattr(dam, "crest_line_xy", None) else "at the crest point"
                warnings.append("Crest raised to the top of dam in the model grid "
                                "({} cells, {}). The source DEM is unchanged.".format(nraise, how))
                log(warnings[-1])
        par = lfmod.write_inputs(wd, dem, inflow_xy, hyd, lst, inflow_line=inline)
        runtime = lfmod.run(wd, par, lst, prog, cancel, log)
        env = lfmod.read_results(wd, dem, lst, hyd)
        return env, dem, init, runtime, wd, lst

    def load_lisflood(tag, hyd, bnds, sim_s):
        """Results of a finished run (resume)."""
        dem = dem_for("lisflood", bnds, cs)
        man = manning_for(bnds, cs)
        init = terrain_src.initial_depth(bnds, cs) if has_ponds else None
        gl = gauge_list(bnds, st.gauges)
        lst = lfmod.LisfloodSettings(st.lisflood_exe, st.threads, st.manning_n, man, arrival_m,
                                     sim_s, massint_s=st.gauge_massint_s if gl else 300.0,
                                     initial_depth=init, gauges=gl,
                                     extra_sources=extra_sources(sim_s),
                                     solver=getattr(st, "lisflood_solver", lfmod.SOLVER_ACC),
                                     closed_edges=closed_edges())
        return lfmod.read_results(os.path.join(model_dir, "lisflood" + tag), dem, lst, hyd), \
            dem, init

    # M5: a run on the built-in engine is a SCREENING approximation, not LISFLOOD-FP. Say so
    # prominently before anything runs, so the numbers are never mistaken for LISFLOOD-FP's.
    if lfmod.is_builtin(st.lisflood_exe):
        bmsg = ("This run uses the BUILT-IN screening engine (local inertial, in numpy), NOT "
                "LISFLOOD-FP. The results are preliminary estimates: near-breach velocities may "
                "be understated (which can lower the depth-velocity hazard zone) and nothing has "
                "been validated against LISFLOOD-FP. Do not use them as the basis of a final "
                "hazard classification or loss-of-life determination without a LISFLOOD-FP run.")
        if bmsg not in warnings:
            warnings.append(bmsg)
            log(bmsg)

    # --- LISFLOOD-FP with domain extension ----------------------------------------
    iterations = list(state.get("iterations", []))
    lf_env, dem, init_grid = None, None, None
    lst, wd = None, None
    lf_span = (5.0, 60.0 if not has_base else 45.0)
    start_it = len(iterations) + 1
    saved_warn = state.get("warnings")
    if state.resumed and saved_warn:
        warnings += [w for w in saved_warn if w not in warnings]
    if state.resumed and iterations:
        bounds = tuple(state.get("next_bounds", bounds))
        sim_time = state.get("next_sim_time", sim_time)
    edge_info_all = []
    class_stop_all = []
    pre_base = None           # a no-breach run made for the class-stop rule (reused if final)
    if state.resumed and state.get("lisflood_done"):
        bounds = tuple(state.get("final_bounds"))
        sim_time = state.get("final_sim_time", sim_time)
        lf_env, dem, init_grid = load_lisflood("", hy, bounds, sim_time)
        log("Results loaded from the checkpoint ({}).".format(lfmod.engine_name(st.lisflood_exe)))
        start_it = st.max_iterations + 1
    max_it = 1 if plan is not None else st.max_iterations
    it = start_it
    refilled = False
    hole_held = False
    stop_reason = None
    while it <= max_it:
        if cancel():
            raise lfmod.Cancelled("cancelled")
        base = lf_span[0] + (lf_span[1] - lf_span[0]) * (it - 1) / max_it
        span = (lf_span[1] - lf_span[0]) / max_it

        def prog(p, m, base=base, span=span, it=it):
            progress(base + span * p / 100.0, "run {}: {}".format(it, m))

        nx_ = int(round((bounds[2] - bounds[0]) / cs))
        ny_ = int(round((bounds[3] - bounds[1]) / cs))
        log("Run {} with {}: {:.1f} x {:.1f} mi, {:,} cells, {:.1f} h".format(
            it, lfmod.engine_name(st.lisflood_exe),
            (bounds[2] - bounds[0]) / MILE, (bounds[3] - bounds[1]) / MILE, nx_ * ny_,
            sim_time / 3600.0))
        lf_env, dem, init_grid, runtime, wd, lst = lisflood_run("", hy, bounds, sim_time, prog,
                                                                st.gauges, snap_for(sim_time))
        cover = float(np.isfinite(dem.masked()).mean())
        depth_eff = lf_env.depth.data
        if init_grid is not None:
            depth_eff = np.clip(depth_eff - np.nan_to_num(init_grid.data), 0.0, None)
        edges = terrain.edge_depths(depth_eff, dem.valid())
        over = [e for e in terrain.EDGES if edges[e] > tol_m]
        arr_max = float(np.nanmax(lf_env.arrival_hr.data)) if np.isfinite(
            lf_env.arrival_hr.data).any() else 0.0
        still_moving = arr_max > 0.9 * sim_time / 3600.0
        rec = {"run": it, "bounds": [round(b, 1) for b in bounds],
               "size_mi": [round((bounds[2] - bounds[0]) / MILE, 2),
                           round((bounds[3] - bounds[1]) / MILE, 2)],
               "cells": int(dem.data.size), "dem_coverage": round(cover, 4),
               "sim_time_hr": round(sim_time / 3600.0, 2), "runtime_min": round(runtime / 60, 1),
               "edge_max_depth_ft": {k: round(v / FT, 2) for k, v in edges.items()},
               "edges_over_tolerance": over, "latest_arrival_hr": round(arr_max, 2),
               "stats": lf_env.stats}
        iterations.append(rec)
        warnings += [n for n in lf_env.notes if n not in warnings]
        # a finished pass has run.max / run.maxVc / run.inittm. Copy them now so a
        # growing domain still leaves depth, velocity, and arrival rasters and polygons.
        if st.write_final:    # per-run rasters off (the probabilistic/Monte-Carlo default):
            try:              # do not write Depth/Velocity/Arrival(Max)/Extent into every run
                _write_pass_products(out_folder, dem, lf_env, arrival_m, writer, it)
            except Exception as _e:
                log("Could not write pass {} products: {}".format(it, _e))
        nodata = ~dem.valid()
        touch_n = 0
        if nodata.any():
            from scipy.ndimage import binary_dilation
            touch = binary_dilation(nodata, structure=np.ones((3, 3), bool)) & (
                np.nan_to_num(lf_env.depth.data) > WET_M)
            touch_n = int(touch.sum())
            if touch_n and not refilled and plan is None:
                from . import dem_refill
                xs, ys = [], []
                rr, cc = np.nonzero(touch)
                for r, c in zip(rr, cc):
                    x, y = dem.xy(int(r), int(c))
                    xs.append(x); ys.append(y)
                cap = None
                if st.max_extent_mi:
                    half = st.max_extent_mi * MILE / 2.0
                    cap = (dam_xy[0] - half, dam_xy[1] - half, dam_xy[0] + half, dam_xy[1] + half)
                box = dem_refill.hole_bounds(xs, ys, MILE, cap)
                if box is None:
                    hole_held = True
                    stop_reason = "no-data outside the {:.0f} mi cap".format(st.max_extent_mi or 0)
                else:
                    tile = os.path.join(model_dir, "dem_refill.tif")
                    try:
                        dem_refill.fetch_3dep(box, cs, tile, crs=getattr(dem, "crs_wkt", None))
                        refill_paths.append(tile)
                        refilled = True
                        msg = ("Wet cells sit on DEM no-data ({:,} cells). Requested one 3DEP "
                               "refill of that footprint plus 1 mile and will run again. "
                               "Dry no-data was left alone.".format(touch_n))
                        log(msg)
                        if msg not in warnings:
                            warnings.append(msg)
                        state.save(iterations=iterations, next_bounds=list(bounds),
                                   next_sim_time=sim_time, warnings=warnings, lisflood_done=False)
                        continue
                    except Exception as ex:
                        hole_held = True
                        stop_reason = "3DEP refill failed ({})".format(ex)
                        log(stop_reason)
            elif touch_n and refilled:
                hole_held = True
                stop_reason = "no-data after one refill"
        if hole_held and not any("held up by a hole" in w for w in warnings):
            warnings.append(
                "Depth is held up by a hole in the terrain ({:,} wet cells still on no-data, {}). "
                "LISFLOOD-FP treats no-data as high ground. This is not a clock cutoff."
                .format(touch_n, stop_reason or "second hole"))
            break
        # receptors decide which edges still matter (ND 5.1.1(f))
        if over and st.receptors is not None and len(st.receptors) and plan is None:
            need, info = impmod.edge_decisions(
                st.receptors, terrain_src, dem, depth_eff, over, tol_m,
                st.impact_look_ahead_mi * MILE, st.planning_cell_m)
            rec["edge_decisions"] = info
            edge_info_all.append({"run": it, "edges": info})
            for e in over:
                if e not in need:
                    i = info[e]
                    msg = ("The flood leaves through the {} edge {:.1f} ft deep, but {}; the "
                           "domain stops growing there (ND 5.1.1(f)). Confirm nothing that "
                           "would change the classification lies beyond.".format(
                               e, i["edge_depth_ft"], i["reason"]))
                    log(msg)
                    if msg not in warnings:
                        warnings.append(msg)
            over = [e for e in over if e in need]
            rec["edges_over_after_receptors"] = over
        # v3.2.0: stop where the class stops changing (optional rule, stoprule.py)
        if over and st.class_stop and plan is None:
            has_layers = st.receptors is not None and len(st.receptors) > 0
            rinfo = rec.get("edge_decisions") or {}
            base_eff, fpath = None, None
            if not has_layers or any(not rinfo.get(e, {}).get("extend", True) for e in over):
                if has_base:
                    log("Class-stop rule: running the no-breach flood on this domain for the "
                        "rise over the baseline.")
                    b_env, _bd, _bi, rt_b0, _bw, _bl = lisflood_run(
                        "_base", hy_nb, bounds, sim_time, prog, st.gauges)
                    pre_base = {"bounds": tuple(bounds), "sim_time": sim_time, "env": b_env,
                                "runtime": rt_b0}
                    base_eff = b_env.depth.data
                    if init_grid is not None:
                        base_eff = np.clip(base_eff - np.nan_to_num(init_grid.data), 0.0, None)
                try:
                    fpath = stoprule.flow_path(dem, inflow_xy)
                except (ValueError, IndexError) as ex:
                    fpath = None
                    log("Class-stop rule: the flow path could not be traced ({}).".format(ex))
            decs = {}
            for e in over:
                decs[e] = stoprule.judge_edge(
                    e, dem, depth_eff, fpath, base_eff, st.class_stop_threshold_ft,
                    st.class_stop_confirm, st.class_stop_station_mi, st.class_stop_search_ft,
                    receptors=rinfo.get(e) if has_layers else None)
                msg = "Class-stop rule, {} edge ({:.1f} ft deep at the edge): {}.".format(
                    e, edges[e] / FT, decs[e]["reason"])
                log(msg)
                if decs[e]["accept"]:
                    msg += (" The domain stops growing there (ND 5.1.1(f)). Confirm that "
                            "nothing beyond it would change the classification.")
                    if msg not in warnings:
                        warnings.append(msg)
            rec["class_stop"] = decs
            class_stop_all.append({"run": it, "edges": decs})
            over = [e for e in over if not decs[e]["accept"]]
            rec["edges_over_after_class_stop"] = over
        if plan is not None:
            if over:
                warnings.append("The flood leaves the base domain through {} deeper than "
                                "{:.1f} ft in this case.".format(", ".join(over),
                                                                 st.edge_tolerance_ft))
            break
        if not over and not still_moving:
            log("Flood stays inside the domain (edge depths below {:.1f} ft, or nothing "
                "downstream to reach).".format(st.edge_tolerance_ft))
            break
        if still_moving and st.sim_time_hr and not over:
            # lengthen up to the cap instead of stopping on the first clock
            pass
        if it == st.max_iterations and over:
            stop_reason = "domain-run limit"
            warnings.append(
                "Stopped after {} domain runs with the flood leaving through {} deeper than "
                "{:.1f} ft. A clock extension is not counted as a run. The duration cap did "
                "not stop this run.".format(it, ", ".join(over), st.edge_tolerance_ft))
            break
        new_bounds = terrain.expand_bounds(bounds, over, step)
        w, h = terrain.bounds_size(new_bounds)
        if over and (w > max_extent or h > max_extent):
            stop_reason = "maximum extent ({:.0f} mi)".format(st.max_extent_mi)
            warnings.append(max_extent_warning(st.max_extent_mi, over))
            break
        if over:
            log("Flood leaves through {} deeper than {:.1f} ft; extending.".format(
                ", ".join(over), st.edge_tolerance_ft))
            bounds = new_bounds
        new_sim = sim_time
        cap_s = MAX_SIM_S if not st.sim_max_hr else min(MAX_SIM_S, float(st.sim_max_hr) * 3600.0)
        if not st.sim_time_hr:
            new_sim = min(cap_s, max(_auto_sim_time(duration_s, bounds, st),
                                     sim_time * (1.5 if still_moving else 1.0)))
        elif still_moving and sim_time < cap_s - 1.0 and st.sim_max_hr:
            # a chosen duration still lengthens, up to the longest clock, while the
            # flood is moving (the same rule as the screening tool)
            new_sim = min(cap_s, sim_time * 1.5)
        if not over and new_sim <= sim_time * 1.001:
            stop_reason = "duration cap"
            warnings.append("The flood is still advancing after {:.0f} h, the duration cap; "
                            "results stop there. Edges were under {:.1f} ft. This is a clock "
                            "cutoff, not the domain-run limit.".format(
                                sim_time / 3600.0, st.edge_tolerance_ft))
            break
        sim_time = new_sim
        if not over:
            log("Flood still moving inside the domain; extending the clock to {:.1f} h "
                "without using a domain run.".format(sim_time / 3600.0))
            state.save(iterations=iterations, next_bounds=list(bounds), next_sim_time=sim_time,
                       warnings=warnings, lisflood_done=False)
            continue
        it += 1
        state.save(iterations=iterations, next_bounds=list(bounds), next_sim_time=sim_time,
                   warnings=warnings, lisflood_done=False)
        continue
    state.save(iterations=iterations, lisflood_done=True, final_bounds=list(bounds),
               final_sim_time=sim_time, warnings=warnings)
    report["lisflood_runs"] = iterations
    if stop_reason:
        report["cut_off"] = {
            "reason": stop_reason,
            "detail": next((w for w in warnings if "still" in w.lower() or "cap" in w.lower() or "limit" in w.lower() or "hole" in w.lower()), stop_reason)
        }
    snap_npz = os.path.join(model_dir, "depth_snapshots.npz")
    if frames > 0 and lst is not None and lst.snapshot_s:
        try:
            times, _p = lfmod.collect_snapshots(wd, lst, snap_npz)
            report["animation_frames"] = 0 if times is None else int(len(times))
        except Exception as e:
            warnings.append("Could not pack the depth snapshots: {}: {}".format(
                type(e).__name__, e))
    if edge_info_all:
        report["receptor_edge_decisions"] = edge_info_all
    if st.class_stop:
        report["class_stop_rule"] = {
            "threshold_ft": st.class_stop_threshold_ft, "confirm": st.class_stop_confirm,
            "station_mi": st.class_stop_station_mi, "search_ft": st.class_stop_search_ft,
            "note": stoprule.DEFAULTS_NOTE, "decisions": class_stop_all}
    res.gauges = dict(lf_env.gauges)
    for name, (tt, qq) in res.gauges.items():
        with open(os.path.join(model_dir, "gauge_{}.csv".format(_safe(name))), "w", encoding="utf-8") as f:
            f.write("time_hr,discharge_cfs\n")
            for a, b in zip(tt, qq):
                f.write("{:.5f},{:.3f}\n".format(a / 3600.0, b / CFS))
    res.plan = {"inflow_xy": tuple(inflow_xy), "path": path, "direction": direction,
                "bounds": tuple(bounds), "sim_time": sim_time, "trace_length_m": plen}

    def write_qa(tag, env, model, init):
        fa = _final_arrays(env, arrival_m, t_ref_hr, init)
        res.qa.setdefault(model, {})
        for kind, arr in fa.items():
            p = os.path.join(model_dir, "{}{}_{}".format(model, tag, QA_NAMES[kind]))
            writer(dem.like(_to_us(kind, arr), np.nan), p)
            res.qa[model]["baseline_" + kind if tag else kind] = p
        return fa

    lf_final = write_qa("", lf_env, "lisflood", init_grid)
    final_env, final_model, final_arrays = lf_env, lfmod.engine_label(st.lisflood_exe), lf_final
    lf_base_env = None
    lf_base_arrays = None

    # --- no-breach baseline (flood scenario) ----------------------------------------
    if has_base:
        progress(46, "no-breach baseline")
        if state.resumed and state.get("baseline_done"):
            lf_base_env, _, _ = load_lisflood("_base", hy_nb, bounds, sim_time)
            res.gauges_base = dict(lf_base_env.gauges)
        elif pre_base is not None and tuple(pre_base["bounds"]) == tuple(bounds) and \
                pre_base["sim_time"] == sim_time:
            lf_base_env = pre_base["env"]          # already run for the class-stop rule
            state.save(baseline_done=True)
            report["baseline_runtime_min"] = round(pre_base["runtime"] / 60.0, 1)
        else:
            lf_base_env, _, _, rt_b, _, _ = lisflood_run("_base", hy_nb, bounds, sim_time,
                                                          _span(progress, 46, 60), st.gauges)
            state.save(baseline_done=True)
            report["baseline_runtime_min"] = round(rt_b / 60.0, 1)
        lf_base_arrays = write_qa("_base", lf_base_env, "lisflood", init_grid)
        res.gauges_base = dict(lf_base_env.gauges)
        warnings += [n for n in lf_base_env.notes if n not in warnings]

    # --- ANUGA ------------------------------------------------------------------------
    an_base_env = None
    if st.tier == TIER_BOTH:
        if sim_time > 48 * 3600.0:
            warnings.append("ANUGA will simulate {:.0f} h; that can take a very long time. "
                            "Consider LISFLOOD-FP only for long floods.".format(
                                sim_time / 3600.0))
        dem_a = dem_for("anuga", bounds, cs)
        flooded = np.isfinite(lf_env.depth.data) & (lf_env.depth.data > 0.05)
        man_a = manning_for(dem_a.bounds, cs)
        ast = anmod.AnugaSettings(st.anuga_python, st.threads, st.manning_n, man_a,
                                  arrival_m, sim_time, 300.0, st.anuga_near_field_mi * MILE,
                                  st.anuga_fine_area_m2, st.anuga_coarse_area_m2,
                                  st.anuga_buffer_m, st.anuga_algorithm)
        ast.mesher = getattr(st, "anuga_mesher", "default") or "default"
        ast.gmsh = getattr(st, "anuga_gmsh", None)
        # gmsh only: road / rail centrelines on an embankment become mesh lines
        emb_kw = {}
        if anmod.mesher_mode(ast) in anmod.GMSH_MESHERS:
            emb_kw = {"embankment_lines": list(getattr(st, "road_lines", None) or [])
                      + list(getattr(st, "rail_lines", None) or []),
                      "terrain_read": terrain_src.read}
        # 3.10.31: ANUGA's bed from the finest terrain (the same conditioning, refills and
        # structure cuts as the model DEM, at the source's own cell size); LISFLOOD-FP
        # keeps the model grid
        ast.elevation = getattr(st, "anuga_elevation", "fine") or "fine"
        ast.elevation_cell_m = getattr(st, "anuga_elevation_cell_m", 0.0) or None
        # 3.10.38: ANUGA checkpoints / partial results / resume; None = the automatic rule
        # (anuga_model.resolve_checkpoints: on for a long run)
        ast.checkpoint_min = getattr(st, "anuga_checkpoint_min", None)
        ast.checkpoint_steps = int(getattr(st, "anuga_checkpoint_steps", 0) or 0)
        ast.checkpoint_keep = int(getattr(st, "anuga_checkpoint_keep", 2) or 2)
        ast.partial_min = getattr(st, "anuga_partial_min", None)
        ast.resume = bool(getattr(st, "anuga_resume", False))
        ast.resume_with_run = bool(st.resume)
        emb_kw.update(elevation_read=lambda b, c: dem_for("anuga", b, c),
                      elevation_native_m=terrain_native_cell(terrain_src),
                      elevation_label=terrain_label(terrain_src))
        width_m = max(3 * cs, inlet_w)
        hy_a, hy_nb_a = _with_flow(hy, amb_cms, sim_time), _with_flow(hy_nb, amb_cms, sim_time)
        a_runs = []
        an_env = None
        reuse = bool(state.resumed and state.get("anuga_done"))
        for k in range(2):
            def aprog(p, m, k=k):
                progress(60 + 10 * k + 10 * p / 100.0, m)
            an_env, outer, fine = anmod.run(
                os.path.join(model_dir, "anuga"), dem_a, flooded, inflow_xy, direction, width_m,
                hy_a, ast, aprog, cancel, log, structures=st.structures, initial_depth=init_grid,
                refine_lines=st.refine_lines, reuse=reuse and k == 0, **emb_kw)
            a_runs.append({"buffer_m": ast.buffer_m, "stats": an_env.stats})
            warnings += [n for n in an_env.notes if n not in warnings]
            edge_ft = an_env.stats.get("max_edge_depth_ft", 0.0)
            if edge_ft <= st.edge_tolerance_ft or k == 1:
                if edge_ft > st.edge_tolerance_ft:
                    warnings.append("ANUGA: the flood reaches the mesh boundary {:.1f} ft "
                                    "deep; its extent is limited by the LISFLOOD-FP domain."
                                    .format(edge_ft))
                break
            log("ANUGA flood reaches the mesh edge ({:.1f} ft); widening the mesh.".format(
                edge_ft))
            flooded = np.isfinite(an_env.depth.data) & (an_env.depth.data > 0.05) | flooded
            ast.buffer_m *= 2
        state.save(anuga_done=True)
        report["anuga_runs"] = a_runs
        report["anuga_elevation_source"] = an_env.stats.get("elevation_source")
        if an_env.stats.get("checkpoint"):          # only when checkpoints were on
            report["anuga_checkpoint"] = an_env.stats["checkpoint"]
        if an_env.stats.get("threads"):             # 3.10.47: ANUGA's OpenMP threads
            report["anuga_threads"] = an_env.stats["threads"]
        anuga_mesh = _anuga_mesh_report(a_runs)
        report["anuga_mesh"] = anuga_mesh
        if anuga_mesh.get("rejected_line"):
            line = anuga_mesh["rejected_line"]
            if line in warnings:
                warnings.remove(line)
            warnings.insert(0, line)             # first in "Things to check"
        an_final = write_qa("", an_env, "anuga", init_grid)
        report["comparison"] = compare(lf_env, an_env, arrival_m)
        res.qa["diff"] = {}
        dd = np.where(np.isfinite(an_env.depth.data) | np.isfinite(lf_env.depth.data),
                      (np.nan_to_num(an_env.depth.data) - np.nan_to_num(lf_env.depth.data)) / FT,
                      np.nan)
        p = os.path.join(model_dir, "diff_depth_anuga_minus_lisflood_ft.tif")
        writer(dem.like(dd, np.nan), p)
        res.qa["diff"]["depth"] = p
        final_env, final_model, final_arrays = an_env, "ANUGA", an_final
        if has_base:
            progress(82, "ANUGA baseline")
            fl_b = np.isfinite(lf_env.depth.data) & (lf_env.depth.data > 0.05)
            an_base_env, _, _ = anmod.run(
                os.path.join(model_dir, "anuga_base"), dem_a, fl_b, inflow_xy, direction,
                width_m, hy_nb_a, ast, _span(progress, 82, 92), cancel, log,
                structures=st.structures, initial_depth=init_grid, refine_lines=st.refine_lines,
                reuse=reuse, **emb_kw)
            lf_base_arrays = write_qa("_base", an_base_env, "anuga", init_grid)
            bm = (an_base_env.stats or {}).get("mesh") or {}
            report["anuga_mesh"]["baseline"] = bm
            if bm.get("rejected") and bm.get("requested") in anmod.GMSH_MESHERS:
                line = "no-breach baseline: " + anmod.rejected_line(bm["rejected"])
                report["anuga_mesh"].setdefault("rejected_line", line)
                if line not in warnings:
                    warnings.insert(0, line)

    # --- v3.4.0: what the DEM conditioning did (report + manifest) -------------------------
    if isinstance(terrain_src, cond.ConditionedTerrain):
        report["dem_conditioning"] = {
            "edits": {k: int(v) for k, v in terrain_src.last_counts.items()},
            "manning_edits": {k: int(v) for k, v in terrain_src.last_manning_counts.items()},
            "buildings": cond.describe_buildings(st.edits, terrain_src.last_counts
                                                 or terrain_src.last_manning_counts),
            "note": "cell counts are on the last grid the models read; the source DEM is "
                    "never changed"}
    # --- v3.4.0: near-dam refine on a fine FV1 grid -----------------------------------------
    fin = final_arrays
    if getattr(st, "refine_near", False):
        from . import refine as refmod
        decision = refmod.decide(st, final_model, st.receptors, st.structures, dam_xy)
        if not decision["refine"]:
            log("Near-dam refine not applied: {}.".format(decision["reason"]))
            report["refine_near_dam"] = {"applied": False, "reason": decision["reason"],
                                         "dist_mi": decision["dist_mi"],
                                         "fine_cell_m": decision["fine_cell_m"],
                                         "counts": decision.get("counts")}
        else:
            progress(86, "near-dam refine (FV1 fine grid)")
            try:
                fin, base_m, rinfo = refmod.run_refine(
                    dam, dam_xy, st, raw_terrain, writer, model_dir, hy_pre, hy_nb_pre,
                    inflow_xy, path, sim_time, dem, fin, lf_base_arrays, decision,
                    _span(progress, 86, 92), log, cancel)
                if lf_base_arrays is not None and base_m is not None:
                    lf_base_arrays = base_m
                report["refine_near_dam"] = rinfo
                if rinfo.get("warning"):
                    warnings.append("Near-dam refine: " + rinfo["warning"])
                if rinfo.get("applied"):
                    warnings.append("Near-dam refine: " + rinfo["summary"] + " Review the fine "
                                    "run's own report ({}).".format(rinfo["fine_report"]))
            except lfmod.Cancelled:
                raise
            except (lfmod.ModelError, brmod.BreachInputError, ValueError, OSError) as e:
                msg = ("Near-dam refine failed ({}: {}); the ACC results are kept everywhere."
                       .format(type(e).__name__, e))
                log(msg)
                warnings.append(msg)
                report["refine_near_dam"] = {"applied": False, "reason": msg,
                                             "dist_mi": decision["dist_mi"],
                                             "fine_cell_m": decision["fine_cell_m"]}

    # --- final rasters for the classification plugin ------------------------------
    progress(93, "writing rasters")
    if st.write_final:
        # A hazard classification of the previous rasters no longer applies. A rerun that
        # changes scenario (e.g. a sunny-day run into a folder left by a flood run) must not
        # keep the previous run's Baseline / Incremental rasters: they belong to a different
        # scenario and would be read as this run's. Clear them here; they are written again
        # below only when this run actually has a baseline.
        for stale in (list(STALE_ON_RERUN) + list(BASELINE_NAMES.values())
                      + list(INCREMENT_NAMES.values())):
            try:
                os.remove(os.path.join(out_folder, stale))
            except OSError:
                pass
        if st.products:     # v3.2.0: the preview is drawn again below only when wanted
            from .products import EAP_NAMES
            for stale in EAP_NAMES:
                try:
                    os.remove(os.path.join(out_folder, stale))
                except OSError:
                    pass
        for kind, arr in fin.items():
            p = os.path.join(out_folder, FINAL_NAMES[kind])
            writer(dem.like(_to_us(kind, arr), np.nan), p)
            res.final[kind] = p
    incr = None
    if lf_base_arrays is not None:
        thr_m = [t * FT for t in st.incr_thresholds_ft]
        inc_d, inc_w, inc_stats = floodmod.incremental(
            fin["depth"], lf_base_arrays["depth"], fin["wse"], lf_base_arrays["wse"], thr_m)
        incr = {"depth": inc_d, "wse": inc_w}
        if st.write_final:
            for kind, arr in lf_base_arrays.items():
                p = os.path.join(out_folder, BASELINE_NAMES[kind])
                writer(dem.like(_to_us(kind, arr), np.nan), p)
                res.baseline[kind] = p
            for kind, arr in incr.items():
                p = os.path.join(out_folder, INCREMENT_NAMES[kind])
                writer(dem.like(_to_us(kind, arr), np.nan), p)
                res.increment[kind] = p
        cell_ac = dem.dx ** 2 / 4046.8564224
        report["incremental"] = {
            "thresholds_ft": list(st.incr_thresholds_ft),
            "acres_at_or_above": {"{:g} ft".format(t): round(inc_stats[t * FT] * cell_ac, 1)
                                  for t in st.incr_thresholds_ft},
            "note": "incremental = depth (or water-surface elevation) with the breach minus the "
                    "same flood without it; compare with the ND REG_05.2026 6.4 criteria"}
        res.base_si = lf_base_arrays
    res.grid = dem
    if st.animation and os.path.exists(snap_npz):
        progress(94, "animation")
        try:
            from . import animation
            ar = animation.render_run(
                snap_npz, dem, os.path.join(out_folder, "Animation"), t_ref_hr,
                title="{} breach flood".format(dam.dam_id), dam_xy=dam_xy,
                geotiff_writer=writer if st.write_final else None)
            res.animation = ar
            report["animation"] = {"gif": ar.gif, "mp4": ar.mp4, "frames": len(ar.frames),
                                   "geotiff_index": ar.index_json, "notes": ar.notes}
            if final_model == "ANUGA":
                warnings.append("The animation shows the LISFLOOD-FP flood; the final rasters "
                                "come from ANUGA.")
        except Exception as e:
            warnings.append("Could not render the animation: {}: {}".format(type(e).__name__, e))
    if st.keep_arrays:
        res.final_si = fin
    d = fin["depth"]
    cell_ac = dem.dx ** 2 / 4046.8564224

    # --- receptors -----------------------------------------------------------------------
    if st.receptors is not None and len(st.receptors):
        rsum, rrows = impmod.summarize(st.receptors, dem, fin, arrival_m)
        report["receptors"] = rsum
        res.receptor_csv = os.path.join(model_dir, "receptors_flooded.csv")
        impmod.write_rows(res.receptor_csv, rrows)
        if incr is not None:
            inc_rows = {}
            v = st.receptors.sample(dem, incr["depth"])
            inb = st.receptors.in_bounds(dem.bounds)
            for t in st.incr_thresholds_ft:
                m = inb & np.isfinite(v) & (v >= t * FT)
                inc_rows["{:g} ft".format(t)] = impmod.count_text(st.receptors, m)
            report["receptors_incremental"] = inc_rows
        log("Reaches: {}".format(rsum.get("receptors_flooded")))

    summary = {
        "dam_id": dam.dam_id, "final_model": final_model,
        "peak_breach_cfs": round(hy.peak_cfs, 0), "breach_method": hy.method,
        "storage_at_failure_acft": hy.params.get("storage_at_failure_acft"),
        "flooded_acres": round(float(np.isfinite(d).sum()) * cell_ac, 1),
        "flooded_acres_over_arrival_depth": round(float((d >= arrival_m).sum()) * cell_ac, 1),
        "max_depth_ft": round(float(np.nanmax(d)) / FT, 2) if np.isfinite(d).any() else 0,
        "max_velocity_fps": (round(float(np.nanmax(fin["velocity"])) / FT, 2)
                             if np.isfinite(fin["velocity"]).any() else 0),
        "latest_arrival_hr": (round(float(np.nanmax(fin["arrival"])), 2)
                              if np.isfinite(fin["arrival"]).any() else None),
        "lisflood_runs": len(iterations),
        "domain_mi": iterations[-1]["size_mi"] if iterations else None,
        "scenario": st.scenario, "breach_occurs": True,
        "units": "depth ft, velocity ft/s, arrival hours after breach initiation, WSE ft",
    }
    if "receptors" in report:
        summary["receptors_flooded"] = report["receptors"].get("receptors_flooded")
    # v3.10.27 (#91): depth-threshold areas, representative velocities, water-body mask
    water_mask = _report_metrics(summary, report, st, dem, fin, cell_ac, arrival_m, out_folder,
                                 getattr(writer, "work_srs", None), warnings, log)
    if report.get("anuga_mesh"):
        summary["anuga_mesher"] = report["anuga_mesh"]["used"]
        if report["anuga_mesh"].get("rejected_line"):
            summary["anuga_mesh_rejected"] = report["anuga_mesh"]["rejected_line"]
    es = report.get("anuga_elevation_source") or {}
    if es.get("mode") == "fine":
        summary["anuga_elevation"] = "{:g} m terrain at the triangle vertices".format(es["cell_m"])
    elif es.get("mode") == "model_grid":
        summary["anuga_elevation"] = "model grid ({:g} m) at the centroids ({})".format(
            es.get("cell_m", 0), es.get("reason", ""))
    ack = report.get("anuga_checkpoint") or {}
    if ack:
        summary["anuga_checkpoint"] = anuga_checkpoint_line(ack)
    ath = report.get("anuga_threads") or {}
    if ath:
        summary["anuga_threads"] = anuga_threads_line(ath)
    if "incremental" in report:
        summary["incremental_acres"] = report["incremental"]["acres_at_or_above"]
    if report.get("refine_near_dam", {}).get("applied"):
        summary["refine_near_dam"] = report["refine_near_dam"]["summary"]
    if report.get("dem_conditioning"):
        summary["buildings"] = report["dem_conditioning"]["buildings"].split(".")[0]
    # M5 labeling: one authoritative engine name for every report surface (md / HTML / docx /
    # run_report.json / manifest), so a built-in-engine run is never presented as LISFLOOD-FP.
    _builtin_run = lfmod.is_builtin(st.lisflood_exe)
    report["flood_engine"] = ("Built-in engine (local inertial, numpy; NOT LISFLOOD-FP)"
                              if _builtin_run else "LISFLOOD-FP (inertial solver)")
    report["flood_engine_short"] = lfmod.engine_name(st.lisflood_exe)
    summary["flood_engine_short"] = report["flood_engine_short"]
    report["summary"] = summary
    report["warnings"] = warnings
    report["outputs"] = {"final": res.final, "baseline": res.baseline,
                         "increment": res.increment, "qa": res.qa,
                         "hydrograph": res.hydrograph_csv}
    res.summary = summary
    res.warnings = warnings

    # --- products -------------------------------------------------------------------------
    if st.products:
        progress(95, "vector products, EAP map and report")
        try:
            from . import products
            products.write_products(res, report, st, dam, dam_xy, dem, fin, incr, hy, hy_nb,
                                    path, out_folder, model_dir, arrival_m,
                                    crs=getattr(writer, "work_srs", None))
        except Exception as e:          # products are extras; never lose the run for them
            warnings.append("Could not write the vector products / map: {}: {}".format(
                type(e).__name__, e))
    _qa_maps(report, st, dam, dam_xy, inflow_xy, top_m, qa_ctx, path, dem, fin, out_folder,
             warnings, log, water_mask=water_mask)
    if st.dam_record is not None and st.write_final:
        report["dam_record"] = _record_dam(dam, st, res, dam_xy, out_folder, top_from_dem,
                                           summary["final_model"], warnings, log, dem, d,
                                           cut_off=report.get("cut_off"),
                                           crest_from=report.get("dem_crest_from", ""))
    report["run_finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    report["elapsed_min"] = round((time.time() - t_start) / 60.0, 1)
    res.report_json = os.path.join(model_dir, "run_report.json")
    with open(res.report_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=_json_default)
    res.report_md = os.path.join(model_dir, "run_report.md")
    with open(res.report_md, "w", encoding="utf-8") as f:
        f.write(render_report(report))
    if st.products:
        try:
            from . import products
            res.report_html = products.write_html_report(report, model_dir, res)
        except Exception as e:
            warnings.append("Could not write the HTML report: {}".format(e))
    try:
        from . import provenance
        man = provenance.build_manifest(
            report["tool"], report["settings"], report["dam"], dam_xy, report.get("inflow_point"),
            report.get("hydrograph"), st.input_files,
            provenance.flatten_outputs(("", res.final), ("baseline ", res.baseline),
                                       ("incremental ", res.increment),
                                       ("", {"hydrograph": res.hydrograph_csv})),
            st.lisflood_exe, st.anuga_python, started=report["run_started"],
            extra=_manifest_extra(report, summary, warnings))
        res.manifest = provenance.write_manifest(os.path.join(model_dir, provenance.MANIFEST_NAME),
                                                 man)
    except Exception as e:      # the manifest is an extra; never lose the run for it
        warnings.append("Could not write the run manifest: {}: {}".format(type(e).__name__, e))
    if not st.keep_model_files:
        _clean(model_dir)
    progress(100, "done")
    return res


# ============================================================================
# a later final engine (TIER 2: HEC-RAS 2D after LISFLOOD-FP)
# ============================================================================
SCREENING_PREFIX = "LISFLOOD-FP "
FINAL_VECTOR_NAMES = ("Flood Polygons (Max Depth).geojson", "Flood Extent.geojson",
                      "Arrival Zones.geojson", "Velocity Polygons.geojson",
                      "Incremental Depth Polygons.geojson", "Breach Products.gpkg",
                      "EAP Inundation Map (preview).svg")


def final_summary(fin, cell_ac, arrival_m, base=None, final_model=None, grid=None,
                  receptors=None, mask=None, mask_source=None, crs=None):
    """The run summary's flood figures from the FINAL arrays (SI): flooded acres (and over
    the arrival depth), max depth, max velocity, latest arrival. `base` (the old summary) is
    copied first; `final_model` replaces its final_model. v3.10.27 (#91): with `grid`, also
    the report_metrics fields (depth-threshold areas, P95 / P99 velocity, the max-velocity
    cell, velocity at `receptors`, and the masked figures when `mask` is given); the old
    values of those fields are dropped first so none is left over from the screening pass."""
    s = dict(base or {})
    d, v, a = fin["depth"], fin["velocity"], fin["arrival"]
    with np.errstate(invalid="ignore"):
        s.update({
            "flooded_acres": round(float(np.isfinite(d).sum()) * cell_ac, 1),
            "flooded_acres_over_arrival_depth": round(float((d >= arrival_m).sum()) * cell_ac, 1),
            "max_depth_ft": round(float(np.nanmax(d)) / FT, 2) if np.isfinite(d).any() else 0,
            "max_velocity_fps": (round(float(np.nanmax(v)) / FT, 2) if np.isfinite(v).any()
                                 else 0),
            "latest_arrival_hr": (round(float(np.nanmax(a)), 2) if np.isfinite(a).any()
                                  else None)})
    if grid is not None:
        from . import report_metrics as rm
        for k in [k for k in s if rm.is_metric_key(k)]:
            s.pop(k, None)
        s.update(rm.flood_metrics(fin, grid, cell_ac, arrival_m, receptors=receptors, mask=mask,
                                  mask_source=mask_source, crs=crs))
    if final_model:
        s["final_model"] = final_model
    return s


def adopt_final_engine(out_folder, final_model, st=None, dam_xy=None, log=None):
    """After a later engine (core/hecras_engine.run_stage) has written its maxima under
    FINAL_NAMES in `out_folder`: makes every reported figure come from those rasters, as a
    TIER 1 run does for ANUGA inside run_dam.

    - the LISFLOOD-FP vector products (flood polygons, extent, arrival zones, GeoPackage,
      EAP preview) are kept as 'LISFLOOD-FP <name>' and made again from the final rasters
    - run_report.json 'summary' is recomputed from the final rasters (area, max depth, max
      velocity, latest arrival, receptors); the screening figures move to
      'screening_pass_lisflood' (with incremental and near-dam refine results, which only
      LISFLOOD-FP made)
    - run_report.md / .html are written again; dam.json gets a 'modelled' status with the
      final model and its extent
    Returns the new summary. Needs GDAL."""
    from . import gdal_io, products
    import types
    log = log or (lambda m: None)
    model_dir = os.path.join(out_folder, "_breach_model")
    rp = os.path.join(model_dir, "run_report.json")
    with open(rp, encoding="utf-8") as f:
        report = json.load(f)
    if report.get("screening_pass_lisflood"):
        raise ValueError("the run report already went through adopt_final_engine")
    paths = {k: os.path.join(out_folder, v) for k, v in FINAL_NAMES.items()}
    g = gdal_io.read_grid(paths["depth"])
    fin = {"depth": g.data * FT}
    for k in ("velocity", "arrival", "wse"):
        if os.path.exists(paths[k]):
            a = gdal_io.read_like(paths[k], g, "near")
            fin[k] = a if k == "arrival" else a * FT
        else:
            fin[k] = np.full(g.data.shape, np.nan)
    unit = gdal_io.linear_unit_m(g.crs_wkt) or 1.0
    cell_ac = (g.dx * unit) ** 2 / 4046.8564224
    sets = report.get("settings") or {}
    arrival_ft = float(getattr(st, "arrival_depth_ft", None) or sets.get("arrival_depth_ft") or 1.0)
    arrival_m = arrival_ft * FT
    old = dict(report.get("summary") or {})
    # M5: the screening pass may have run on the opt-in built-in engine; label its kept vector
    # products and notes with the engine that actually produced them, never a bare 'LISFLOOD-FP'.
    scr_model = old.get("final_model") or "LISFLOOD-FP"
    scr_prefix = ("built-in engine " if "built-in" in str(scr_model).lower() else SCREENING_PREFIX)
    screening = {"model": scr_model, "summary": old,
                 "note": "The screening pass that laid out the domain and the HEC-RAS model. "
                         "Every figure in 'summary', the flood polygons and the hazard class "
                         "come from {}.".format(final_model),
                 "vectors": {}}
    for k in ("receptors", "receptors_incremental", "incremental", "refine_near_dam",
              "water_mask"):
        if k in report:
            screening[k] = report[k]
    # keep the screening vector products under the LISFLOOD-FP prefix
    for nm in FINAL_VECTOR_NAMES:
        src = os.path.join(out_folder, nm)
        if os.path.exists(src):
            dst = os.path.join(out_folder, scr_prefix + nm)
            os.replace(src, dst)
            screening["vectors"][nm] = dst
    rec = getattr(st, "receptors", None) if st is not None else None
    # v3.10.27 (#91): the depth-threshold / velocity / mask figures from the final rasters too
    from . import report_metrics as rm
    mask, minfo = None, None
    try:
        mask, minfo = rm.build_mask(g, getattr(st, "water_mask", None) if st is not None else None,
                                    g.crs_wkt, out_folder=out_folder, log=log)
    except Exception as e:      # noqa: BLE001 - reporting only
        log("Water-body mask not used: {}: {}".format(type(e).__name__, e))
    msrc = "; ".join(os.path.basename(x) for x in (minfo or {}).get("sources") or [])
    summary = final_summary(fin, cell_ac, arrival_m, old, final_model, grid=g,
                            receptors=rec if rec is not None and len(rec) else None,
                            mask=mask, mask_source=msrc or None, crs=gdal_io.epsg_of(g.crs_wkt))
    if minfo is not None:
        report["water_mask"] = minfo
        if mask is not None:
            gj = rm.write_mask_geojson(g, mask, out_folder, crs=gdal_io.epsg_of(g.crs_wkt))
            if gj:
                minfo["polygons"] = gj
    else:
        report.pop("water_mask", None)
    for k in ("incremental_acres", "refine_near_dam", "receptors_flooded"):
        summary.pop(k, None)
    if rec is not None and len(rec):
        rsum, rrows = impmod.summarize(rec, g, fin, arrival_m)
        report["receptors"] = rsum
        impmod.write_rows(os.path.join(model_dir, "receptors_flooded.csv"), rrows)
        summary["receptors_flooded"] = rsum.get("receptors_flooded")
    else:
        report.pop("receptors", None)
    report.pop("receptors_incremental", None)
    report.pop("refine_near_dam", None)
    if "incremental" in report:
        report["incremental"] = dict(report["incremental"], note=(
            report["incremental"].get("note", "") + ". From the {} screening pass: "
            "{} ran the breach only (no no-breach run).".format(scr_prefix.strip(), final_model)))
    hin = report.get("hecras_inflow") or {}
    fallback = hin.get("note") if hin.get("status") == "fallback_breach" else ""
    if fallback:
        summary["hecras_inflow"] = "breach at the dam (fallback)"
        log("WARNING: " + fallback)
    report["summary"] = summary
    report["screening_pass_lisflood"] = screening
    report["final_engine"] = {"model": final_model, "figures_from": dict(paths),
                              "adopted": datetime.datetime.now().isoformat(timespec="seconds")}
    # vector products from the final rasters
    dxy = dam_xy if dam_xy is not None else report.get("dam_xy")
    dam_ns = types.SimpleNamespace(dam_id=old.get("dam_id") or report.get("dam", {}).get("dam_id"),
                                   name=(report.get("dam") or {}).get("name", ""))
    st_ns = types.SimpleNamespace(
        scenario=getattr(st, "scenario", None) or old.get("scenario"),
        arrival_contours_hr=tuple(getattr(st, "arrival_contours_hr", None)
                                  or RunSettings().arrival_contours_hr),
        receptors=rec, incr_thresholds_ft=(),
        eap_map=getattr(st, "eap_map", None) or products.EAP_AUTO, eap_note=fallback)
    res_ns = types.SimpleNamespace(vectors={}, eap_map="", flood=None, gauges={}, plan={})
    old_vectors = dict((report.get("outputs") or {}).get("vectors") or {})
    warnings = list(report.get("warnings") or [])
    try:
        products.write_products(res_ns, report, st_ns, dam_ns, dxy, g, fin, None, None, None,
                                None, out_folder, model_dir, arrival_m,
                                crs=gdal_io.epsg_of(g.crs_wkt), unit_m=unit, plots=False)
    except Exception as e:
        warnings.append("Could not write the {} vector products: {}: {}".format(
            final_model, type(e).__name__, e))
    msg = ("Every figure in this report, the flood polygons and the hazard class come from {}; "
           "the {} screening pass is under 'screening_pass_lisflood' and its "
           "vector products are kept as '{}...'.".format(final_model, scr_prefix.strip(), scr_prefix))
    if msg not in warnings:
        warnings.append(msg)
    if fallback and fallback not in warnings:
        warnings.insert(0, fallback)
    report["warnings"] = warnings
    vec = {k: v for k, v in old_vectors.items() if os.path.basename(str(v)) not in
           FINAL_VECTOR_NAMES}
    vec.update(res_ns.vectors)
    report.setdefault("outputs", {})["final"] = dict(paths)
    report["outputs"]["vectors"] = vec
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=_json_default)
    with open(os.path.join(model_dir, "run_report.md"), "w", encoding="utf-8") as f:
        f.write(render_report(report))
    try:
        products.write_html_report(report, model_dir, types.SimpleNamespace(
            eap_map=res_ns.eap_map, vectors=vec, dam_id=dam_ns.dam_id))
    except Exception as e:
        log("Could not write the HTML report again: {}".format(e))
    # dam.json: a 'modelled' status with the final model
    try:
        from . import damjson
        result = {"breach_occurs": True, "scenario": summary.get("scenario"),
                  "final_model": final_model,
                  "final_rasters": [os.path.relpath(p, out_folder).replace(os.sep, "/")
                                    for p in paths.values()],
                  "manifest": damjson.MANIFEST_REL, "run_report": damjson.REPORT_REL,
                  "screening_model": screening["model"]}
        if dxy is not None:
            result["extent_ft"] = damjson.extent_ft(g, fin["depth"], dxy, WET_M)
        if summary.get("domain_mi"):
            result["domain_mi"] = summary["domain_mi"]
        if os.path.exists(os.path.join(out_folder, "dam.json")):
            damjson.record_run(out_folder, {}, result)
    except Exception as e:
        log("dam.json not updated for {}: {}: {}".format(final_model, type(e).__name__, e))
    log("Run summary from {}: {:,} ac, max depth {} ft, max velocity {} ft/s (LISFLOOD-FP "
        "screening pass: {} ac, {} ft, {} ft/s).".format(
            final_model, summary["flooded_acres"], summary["max_depth_ft"],
            summary["max_velocity_fps"], old.get("flooded_acres"), old.get("max_depth_ft"),
            old.get("max_velocity_fps")))
    return summary


def preview(dam, dam_xy, st, terrain_src, inflow_xy=None, out_folder=None):
    """What a run would model, without running any flood model: the breach
    hydrograph, the first domain and simulation time. Used for the run-time
    estimate."""
    warnings = []
    cs = st.cell_size_m
    if out_folder:
        attach_proposed(st, out_folder, dam, warnings, lambda _m: None)
    if st.breach_xy is not None:
        dam_xy = (float(st.breach_xy[0]), float(st.breach_xy[1]))
    if st.edits is not None and not st.edits.empty():
        terrain_src = cond.ConditionedTerrain(terrain_src, st.edits)
    near = terrain_src.read((dam_xy[0] - 60, dam_xy[1] - 60, dam_xy[0] + 60, dam_xy[1] + 60),
                            min(cs, 3.0))
    z = near.masked()
    X, Y = np.meshgrid(near.x_centers(), near.y_centers())
    sel = np.hypot(X - dam_xy[0], Y - dam_xy[1]) <= 30.0
    if not np.isfinite(z[sel]).any() and not getattr(st, "proposed", None):
        raise brmod.BreachInputError("{}: no DEM data at the dam point".format(dam.dam_id))
    if getattr(st, "proposed", None) and dam.top_of_dam_ft is None:
        dam.top_of_dam_ft = float(st.proposed["crest_m"]) / FT
    elif dam.top_of_dam_ft is None:
        dam.top_of_dam_ft = float(np.nanmax(z[sel])) / FT
    flood_mode = st.scenario == SCENARIO_FLOOD
    hy_nb = None
    if flood_mode:
        hy, hy_nb, _ = _flood_hydrographs(dam, st, warnings)
        if hy is None:
            return {"breach_occurs": False, "warnings": warnings}
    else:
        hy = brmod.make_hydrograph(dam, st.breach_method, st.overrides, st.user_hydrograph)
    h_m = dam.dam_height_ft * FT
    top_m = dam.top_of_dam_ft * FT
    if inflow_xy is None:
        rad = max(90.0, 8.0 * h_m)
        search_rad = max(rad, 400.0)
        search_cell = min(cs, 20.0)
        local = terrain_src.read((dam_xy[0] - search_rad - 30, dam_xy[1] - search_rad - 30,
                                  dam_xy[0] + search_rad + 30, dam_xy[1] + search_rad + 30),
                                 search_cell)
        ds_info = {}
        inflow_xy, why = _downstream_inflow(terrain_src, local, dam, dam_xy, h_m, top_m, rad,
                                            search_cell, warnings, lambda *_a: None, out=ds_info)
        if inflow_xy is None:
            inflow_xy, crest, why = terrain.place_from_crest(
                local, dam_xy, h_m, top_m, search_rad, downstream=ds_info.get("direction"))
            if inflow_xy is None:
                raise brmod.BreachInputError("{}: could not place the breach inflow: {}".format(
                    dam.dam_id, why))
            if crest is not None:
                dam_xy = crest
                warnings.append("Dam point was not on the embankment; crest moved to the nearest "
                                "ground at the top of dam within {:.0f} m.".format(search_rad))
    L = st.initial_length_mi * MILE
    W = st.half_width_mi * MILE
    half = L + W + 2 * st.planning_cell_m
    pl = terrain_src.read((inflow_xy[0] - half, inflow_xy[1] - half,
                           inflow_xy[0] + half, inflow_xy[1] + half), st.planning_cell_m)
    path, plen = terrain.trace_downstream(pl, inflow_xy, L)
    bounds = terrain.path_bounds(path + [dam_xy], W, snap=cs)
    duration_s = max(hy.duration_s, hy_nb.duration_s if hy_nb is not None else 0.0)
    if st.ambient_cfs and st.ambient_cfs > 0:
        duration_s += max(0.0, float(st.spinup_hr)) * 3600.0
    sim_time = _auto_sim_time(duration_s, bounds, st)
    w, h = terrain.bounds_size(bounds)
    return {"breach_occurs": True, "cells": int(round(w / cs)) * int(round(h / cs)),
            "size_mi": (w / MILE, h / MILE), "cell_m": cs, "sim_time_s": sim_time,
            "peak_cfs": hy.peak_cfs, "inflow_xy": inflow_xy,
            "baseline": bool((flood_mode or (st.ambient_cfs or 0) > 0) and st.baseline
                             and (hy_nb is not None or not flood_mode)),
            "trace_mi": plen / MILE, "warnings": warnings, "path": path, "bounds": bounds,
            "direction": terrain.flow_direction(path)}


def _shift(h, spin_s):
    """The hydrograph delayed by spin_s: its first value is held for that long so the model
    starts from the ambient flow, and the breach initiation moves with it."""
    if h is None or spin_s <= 0:
        return h
    t = np.concatenate([[0.0], np.asarray(h.t_s, float) + spin_s])
    q = np.concatenate([[float(h.q_cms[0])], np.asarray(h.q_cms, float)])
    return brmod.Hydrograph(t, q, h.method, h.params, h.notes, h.t_ref_s + spin_s)


def _zero_hydrograph(h, label):
    """No outflow over the same record: the baseline of a sunny-day run that has ambient flow."""
    return brmod.Hydrograph(np.array([0.0, float(h.t_s[-1])]), np.zeros(2), label,
                            {"scenario": label}, [], t_ref_s=h.t_ref_s)


def _with_flow(h, q_cms, horizon_s):
    """The hydrograph plus a steady flow, held until horizon_s (for models that take a single
    inflow series, ANUGA)."""
    if h is None or q_cms <= 0:
        return h
    t = np.unique(np.concatenate([np.asarray(h.t_s, float), [max(horizon_s, h.t_s[-1] + 1.0)]]))
    q = np.interp(t, h.t_s, h.q_cms, right=0.0) + q_cms
    return brmod.Hydrograph(t, q, h.method, h.params, h.notes, h.t_ref_s)


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))


def _flood_hydrographs(dam, st, warnings):
    """(with-breach hydrograph, no-breach hydrograph, FloodResult). The first is
    None when the dam does not fail."""
    fi = st.flood
    if fi is None:
        raise brmod.BreachInputError("the flood scenario needs the inflow hydrograph")
    if st.breach_method == brmod.METHOD_USER:
        if not st.user_hydrograph or not fi.baseline_csv:
            raise brmod.BreachInputError(
                "with your own hydrographs the flood scenario needs both: the outflow with the "
                "breach and the outflow without it, each starting at breach initiation")
        hy = brmod.read_user_hydrograph(st.user_hydrograph)
        nb = floodmod.hydrograph_from_csv(fi.baseline_csv, "no-breach hydrograph")
        warnings.append("Your own hydrographs are used as given: time zero is breach initiation "
                        "in both.")
        return hy, nb, None
    fr = floodmod.route_flood(dam, st.breach_method, st.overrides, fi)
    if not fr.failed:
        return None, fr.no_breach, fr
    return fr.with_breach, fr.no_breach, fr


def _write_no_breach(res, report, warnings, model_dir, hy_nb):
    report["warnings"] = warnings
    report["run_finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    res.summary = report["summary"]
    res.warnings = warnings
    if hy_nb is not None:
        hy_nb.to_csv(os.path.join(model_dir, "no_breach_hydrograph.csv"))
    res.report_json = os.path.join(model_dir, "run_report.json")
    with open(res.report_json, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=_json_default)
    res.report_md = os.path.join(model_dir, "run_report.md")
    from . import damjson
    with open(res.report_md, "w", encoding="utf-8") as f:
        f.write("# Breach model run: {}\n\nThe dam does not fail in this flood.\n\n{}\n".format(
            res.dam_id, "\n".join("- " + w for w in warnings)))
        if report.get("dam_record"):
            f.write("\n" + "\n".join(damjson.report_lines(report["dam_record"])) + "\n")


def _record_dam(dam, st, res, dam_xy, out_folder, top_from_dem, final_model, warnings, log,
                grid=None, depth=None, cut_off=None, crest_from=""):
    """v3.2.0: write what the run used into <dam folder>/dam.json (damjson.record_run) and add
    its "modelled" status. Conflicts with another tool's values become warnings. Never fails
    the run."""
    from . import damjson
    dr = st.dam_record or {}
    try:
        top_source = "dem" if top_from_dem else dr.get("top_source", "parameter")
        vals = damjson.values_used(dam, st, top_source, dr.get("point"), dr.get("nid_id"))

        def rel(p):
            return os.path.relpath(p, out_folder).replace(os.sep, "/")
        breach = final_model != "none"
        result = {"breach_occurs": breach, "scenario": st.scenario,
                  "final_model": final_model,
                  "final_rasters": [rel(p) for p in res.final.values()],
                  "manifest": damjson.MANIFEST_REL, "run_report": damjson.REPORT_REL}
        if cut_off:
            result["cut_off"] = cut_off
        if res.baseline:
            result["baseline_rasters"] = [rel(p) for p in res.baseline.values()]
        if breach and grid is not None and depth is not None:
            result["extent_ft"] = damjson.extent_ft(grid, depth, dam_xy, WET_M)
            w, h = terrain.bounds_size(grid.bounds)
            result["domain_mi"] = [round(w / MILE, 2), round(h / MILE, 2)]
        # a top of dam this plugin estimated from the DEM is written with a note that marks it
        # as an estimate (and, when damjson supplies it, a "DEM estimate" datum), so a later run
        # re-estimates it instead of trusting one run's guess as a surveyed crest
        dem_note = ""
        if top_from_dem:
            fmt = getattr(damjson, "DEM_ESTIMATE_NOTE",
                          "estimated by this plugin from the {}; not a surveyed or as-built "
                          "crest")
            dem_note = fmt.format(crest_from or "DEM near the dam point")
        info = damjson.record_run(out_folder, vals, result, dr.get("read"),
                                  dem_top_note=dem_note)
    except Exception as e:           # the record is an extra; never lose the run for it
        info = {"error": "dam.json not updated: {}: {}".format(type(e).__name__, e),
                "warnings": [], "read": list(dr.get("read") or [])}
        info["warnings"].append(info["error"])
    for w in info.get("warnings") or []:
        log(w)
        if w not in warnings:
            warnings.append(w)
    return info


def _work_crs_text(writer, terrain_src):
    """v3.10.16: the run's model CRS as text (EPSG code or WKT), or None."""
    for obj in (writer, terrain_src, getattr(terrain_src, "base", None)):
        v = getattr(obj, "work_srs", None) if obj is not None else None
        if v:
            return str(v)
    return None


def _z_to_m_of(terrain_src):
    """v3.10.16: metres per DEM elevation unit, or None when the terrain does not say."""
    v = getattr(terrain_src, "z_to_m", None)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


ROAD_NEAR_M = 30.0      # v3.10.18: a road this close to the dam point may be the embankment


def _check_inflow_site(report, st, dam, dam_xy, inflow_xy, path, downstream, from_plan,
                       warnings, log):
    """v3.10.18: the inflow point against the flow trace (terrain.inflow_side_check) and the
    dam point against the road layer. A disagreement is a prominent warning (the dam point
    or the downstream side is probably wrong) and is recorded in run_report.json:
    inflow_point.side_check and dam_point_checks."""
    if from_plan:
        return
    chk = terrain.inflow_side_check(dam_xy, inflow_xy, path, downstream)
    report.setdefault("inflow_point", {})["side_check"] = chk
    if not chk["ok"]:
        msg = ("{}: the breach inflow point ({:.1f}, {:.1f}) and the flow trace disagree on "
               "the downstream side: {}. The dam point or the downstream side is probably "
               "wrong; give the inflow point (just below the dam) or the DWR dam line "
               "(DAM_LINES).".format(terrain.SIDE_PROBLEM, inflow_xy[0], inflow_xy[1],
                                     "; ".join(chk["problems"])))
        warnings.insert(0, msg)
        log(msg)
    roads = getattr(st, "road_lines", None) or []
    rd = terrain.nearest_line_distance(dam_xy, roads) if roads else None
    line = getattr(dam, "crest_line_xy", None)
    ld = terrain.nearest_line_distance(dam_xy, [line]) if line else None
    report["dam_point_checks"] = {"road_m": None if rd is None else round(rd, 1),
                                  "dam_line_m": None if ld is None else round(ld, 1)}
    if line:
        # v3.10.22: the DWR dam line (run CRS), for the HEC-RAS dam line along the crest
        report["crest_line_xy"] = [[round(float(x), 3), round(float(y), 3)] for x, y in line]
    if rd is not None and rd <= ROAD_NEAR_M:
        if line:
            # v3.10.22: say "on the dam line" only when the dam point is (it may not be)
            where = ("which is on the DWR dam line" if ld is not None and ld <= ROAD_NEAR_M else
                     "{:.0f} m from the DWR dam line".format(ld) if ld is not None else
                     "with a DWR dam line given")
            msg = ("A road runs {:.0f} m from the dam point, {}: the road is on (or beside) "
                   "this embankment; the crest comes from the dam line.".format(rd, where))
        else:
            msg = ("A road runs {:.0f} m from the dam point: the embankment in the DEM may be "
                   "the road's fill rather than the dam (DWR-3161 sits on the ND-23 fill). "
                   "Check the dam point, or give the DWR dam line (DAM_LINES) so the crest "
                   "comes from the dam line.".format(rd))
        report["dam_point_checks"]["note"] = msg
        warnings.append(msg)
        log(msg)


def _downstream_inflow(terrain_src, local, dam, dam_xy, h_m, top_m, rad, cell, warnings, log,
                       out=None):
    """find_inflow_point limited to the downstream side of the crest and outside the pool
    (core/terrain.downstream_direction, pool_mask). The pool needs terrain well past the
    reservoir, so it is read on its own (POOL_HALF_M around the dam, at the search cell)."""
    half = terrain.POOL_HALF_M
    pool_grid = pool = direction = None
    try:
        pool_grid = terrain_src.read((dam_xy[0] - half, dam_xy[1] - half,
                                      dam_xy[0] + half, dam_xy[1] + half), cell)
        pool = pool_grid.like(terrain.pool_mask(pool_grid, h_m).astype(float), nodata=-1.0)
    except Exception as e:                       # noqa: BLE001 - the pool is a refinement
        log("Pool behind the dam not found ({}); the inflow search uses the side test only."
            .format(e))
    try:
        crest_xy = None
        if pool_grid is not None:
            # v3.10.16: the crest of this dam (ground at the top of dam), not the highest
            # ground in the circle (a hill)
            crest_xy = terrain.embankment_crest(local, dam_xy, h_m, top_m, max(rad, 400.0))[0]
        direction, how = terrain.downstream_direction(
            pool_grid, dam_xy, h_m, crest_line=getattr(dam, "crest_line_xy", None),
            crest_xy=crest_xy)
    except Exception as e:                       # noqa: BLE001
        direction, how = None, "downstream direction failed: {}".format(e)
    log("Downstream side of the crest: {}".format(
        "({:.2f}, {:.2f}), {}".format(direction[0], direction[1], how) if direction else how))
    if out is not None:                  # v3.10.18: place_from_crest keeps to this side too
        out["direction"], out["how"] = direction, how
    xy, why = terrain.find_inflow_point(local, dam_xy, h_m, top_m, rad, downstream=direction,
                                        pool=pool)
    if why.startswith(terrain.NO_DOWNSTREAM):
        warnings.append("Breach inflow: {}. Give the inflow point (just below the dam) if this "
                        "is not the downstream toe.".format(why))
        log(warnings[-1])
    elif xy is not None and direction is not None:
        why = "{}, downstream side ({})".format(why, how)
    return xy, why


def _report_metrics(summary, report, st, grid, fin, cell_ac, arrival_m, out_folder, crs,
                    warnings, log):
    """v3.10.27 (#91): the report_metrics fields into `summary` and the water-body mask into
    report['water_mask']. Reporting only, never fails the run. Returns the mask (or None)."""
    from . import report_metrics as rm
    mask = None
    try:
        crs_wkt = getattr(grid, "crs_wkt", None) or crs
        mask, info = rm.build_mask(grid, getattr(st, "water_mask", None), crs_wkt,
                                   out_folder=out_folder, log=log)
        if info is not None:
            report["water_mask"] = info
            for p in info.get("problems") or []:
                warnings.append(p)
        src = "; ".join(os.path.basename(x) for x in (info or {}).get("sources") or [])
        summary.update(rm.flood_metrics(fin, grid, cell_ac, arrival_m,
                                        receptors=getattr(st, "receptors", None), mask=mask,
                                        mask_source=src or None, crs=crs))
        if mask is not None and out_folder and getattr(st, "write_final", True):
            gj = rm.write_mask_geojson(grid, mask, out_folder, crs=crs)
            if gj:
                report["water_mask"]["polygons"] = gj
    except Exception as e:      # noqa: BLE001 - extra figures never cost the run
        msg = "Run-report metrics (#91) not computed: {}: {}".format(type(e).__name__, e)
        warnings.append(msg)
        log(msg)
    return mask


def _qa_maps(report, st, dam, dam_xy, inflow_xy, top_m, qa_ctx, path, dem, fin, out_folder,
             warnings, log, water_mask=None):
    """The QA PNGs (core/qa_maps.py) into the dam's output folder. Never fails the run."""
    try:
        from . import qa_maps
        mode = qa_maps.normalize(getattr(st, "qa_maps", None))
        if mode == qa_maps.QA_OFF or not out_folder:
            report["qa_maps"] = {"mode": mode}
            return
        depth = fin.get("depth") if isinstance(fin, dict) else None
        if depth is not None and dem is not None and np.shape(depth) != dem.data.shape:
            depth = None
        info, problems = qa_maps.write_qa_maps(
            mode, out_folder, getattr(dam, "dam_id", "") or "dam", qa_ctx.get("local") or dem,
            tuple(dam_xy), tuple(inflow_xy) if inflow_xy is not None else None, top_m,
            radius_m=qa_ctx.get("radius_m"), trace=path,
            crest_line=getattr(dam, "crest_line_xy", None), breach_xy=qa_ctx.get("breach_xy"),
            model_grid=dem, depth_m=depth, metrics=report.get("summary"),
            water_mask=water_mask)
        report["qa_maps"] = info
        for p in problems:
            warnings.append(p)
            log(p)
        if info.get("inflow_map") or info.get("depth_map"):
            log("QA maps ({:.1f} s): {}".format(info.get("seconds", 0.0), ", ".join(
                os.path.basename(info[k]) for k in ("inflow_map", "depth_map") if info.get(k))))
    except Exception as e:  # noqa: BLE001 - QA maps are extras
        warnings.append("QA maps not written: {}: {}".format(type(e).__name__, e))


def _dem_resample_of(terrain_src):
    """{"method", "source"} of the DEM warp (GdalTerrain, also inside a ConditionedTerrain)."""
    t = terrain_src
    for _i in range(4):
        if t is None:
            return None
        if getattr(t, "resample", None):
            return {"method": t.resample, "source": getattr(t, "resample_source", "parameter")}
        t = getattr(t, "base", None)
    return None


def _manifest_extra(report, summary, warnings):
    """The manifest's "extra" block. v3.4.0 adds the near-dam refine and the DEM conditioning
    (buildings) so the manifest says what the rasters were made with."""
    extra = {"warnings": len(warnings), "final_model": summary["final_model"]}
    extra["lisflood_solver"] = report["settings"].get("lisflood_solver", lfmod.SOLVER_ACC)
    r = report.get("refine_near_dam")
    if r is not None:
        extra["refine_near_dam"] = {
            "applied": bool(r.get("applied")), "reason": r.get("reason"),
            "dist_mi": r.get("dist_mi"), "fine_cell_m": r.get("fine_cell_m"),
            "solver": r.get("solver"), "cells_replaced": r.get("cells_replaced"),
            "receptors_inside": (r.get("counts") or {}).get("total")}
    if report.get("dem_resample"):
        extra["dem_resample"] = dict(report["dem_resample"])
    if report.get("qa_maps"):
        extra["qa_maps"] = dict(report["qa_maps"])
    c = report.get("dem_conditioning")
    if c is not None:
        extra["dem_conditioning"] = {"buildings": c["buildings"], "edits": c["edits"],
                                     "manning_edits": c["manning_edits"]}
    return extra


def _json_default(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.bool_):
        return bool(o)
    return str(o)


def _clean(model_dir):
    for sub in ("lisflood", "lisflood_base", "anuga", "anuga_base"):
        d = os.path.join(model_dir, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if fn.endswith((".asc", ".npz", ".msh", ".Vx", ".Vy", ".wd", ".Qx", ".Qy",
                                ".start")):
                    try:
                        os.remove(os.path.join(root, fn))
                    except OSError:
                        pass


def _anuga_mesh_report(a_runs):
    """run_report["anuga_mesh"]: the mesher asked for and the one actually used (the last
    ANUGA pass), every pass's mesh, and the 'gmsh mesh rejected: ...' line if any pass fell
    back to the default mesh."""
    meshes = [(r.get("stats") or {}).get("mesh") or {} for r in a_runs]
    last = meshes[-1] if meshes else {}
    out = {"requested": last.get("requested", "default"), "used": last.get("used", "default"),
           "passes": meshes}
    g = last.get("gmsh")
    if g:
        out["gmsh"] = g
    if last.get("embankments"):
        out["embankments"] = last["embankments"]
    rej = [m.get("rejected") for m in meshes if m.get("rejected")
           and m.get("requested") in ("gmsh", "gmsh_or_default")]
    if rej:
        from . import anuga_model as _am
        out["rejected"] = rej[-1]
        out["rejected_line"] = _am.rejected_line(rej[-1])
    return out


def scr_has_final(r):
    """A report whose figures come from a later engine (adopt_final_engine)."""
    return bool(r.get("final_engine") or r.get("screening_pass_lisflood"))


def render_report(r):
    s = r["summary"]
    h = r["hydrograph"]
    lines = ["# Breach model run: {}".format(s["dam_id"]), "",
             "{} · {} · finished {}".format(r["tool"], r["policy"], r["run_finished"]), "",
             "## Result", "",
             "| | |", "|---|---|",
             "| Scenario | {} |".format("flood inflow (PMF) with and without the breach"
                                        if s.get("scenario") == SCENARIO_FLOOD else "sunny day"),
             "| Final rasters from | {} |".format(s["final_model"]),
             "| Peak breach outflow | {:,.0f} cfs ({}) |".format(s["peak_breach_cfs"],
                                                                s["breach_method"]),
             "| Storage at failure | {} ac-ft |".format(s["storage_at_failure_acft"]),
             "| Flooded area | {:,} ac ({:,} ac deeper than the arrival depth) |".format(
                 s["flooded_acres"], s["flooded_acres_over_arrival_depth"]),
             "| Maximum depth / velocity | {} ft / {} ft/s |".format(s["max_depth_ft"],
                                                                  s["max_velocity_fps"]),
             "| Latest arrival | {} h after breach initiation |".format(s["latest_arrival_hr"]),
             "| Domain | {} x {} mi after {} run(s) of {} |".format(
                 s["domain_mi"][0], s["domain_mi"][1], s["lisflood_runs"],
                 r.get("flood_engine_short", "LISFLOOD-FP"))]
    from . import report_metrics as _rm
    lines += _rm.report_lines(s)        # v3.10.27 (#91); nothing for an older report
    if s.get("receptors_flooded"):
        lines.append("| Reaches | {} |".format(s["receptors_flooded"]))
    if s.get("refine_near_dam"):
        lines.append("| Near the dam (v3.4.0) | {} |".format(s["refine_near_dam"]))
    if s.get("buildings"):
        lines.append("| Buildings (v3.4.0) | {} |".format(s["buildings"]))
    if s.get("anuga_mesher"):
        am = r.get("anuga_mesh") or {}
        g = am.get("gmsh") or {}
        txt = s["anuga_mesher"]
        if s["anuga_mesher"] == "gmsh" and g:
            txt = "gmsh ({:,} triangles, smallest {:g} m2, minimum angle {:g} deg)".format(
                g.get("triangles", 0), g.get("min_area_m2", 0), g.get("min_angle_deg", 0))
        elif am.get("requested") not in (None, "default", s["anuga_mesher"]):
            txt += " (asked for {})".format(am.get("requested"))
        lines.append("| ANUGA mesh | {} |".format(txt))
    if s.get("anuga_elevation"):
        lines.append("| ANUGA bed elevation | {} |".format(s["anuga_elevation"]))
    if s.get("anuga_checkpoint"):
        lines.append("| ANUGA checkpoints | {} |".format(s["anuga_checkpoint"]))
    if s.get("anuga_threads"):
        lines.append("| ANUGA threads | {} |".format(s["anuga_threads"]))
    lines.append("")
    if s.get("anuga_mesh_rejected"):
        lines += ["## ANUGA mesh", "", "**" + s["anuga_mesh_rejected"] + "**", ""]
    hin = r.get("hecras_inflow") or {}
    if scr_has_final(r) and hin.get("status") == "fallback_breach" and hin.get("note"):
        lines += ["**HEC-RAS inflow:** " + hin["note"], ""]
    elif scr_has_final(r) and hin.get("status") == "moved":
        lines += ["**HEC-RAS inflow:** the run's inflow point was moved {:,.0f} ft downstream "
                  "along {} (ground {} ft) for the HEC-RAS inflow hydrograph.".format(
                      hin.get("moved_ft") or 0, hin.get("how", "the flow path"),
                      hin.get("ground_ft")), ""]
    scr = r.get("screening_pass_lisflood")
    if scr:
        ss = scr.get("summary") or {}
        lines += ["## Screening pass ({})".format(scr.get("model", "LISFLOOD-FP")), "",
                  scr.get("note", ""), "", "| | |", "|---|---|",
                  "| Flooded area | {:,} ac ({:,} ac deeper than the arrival depth) |".format(
                      ss.get("flooded_acres") or 0, ss.get("flooded_acres_over_arrival_depth") or 0),
                  "| Maximum depth / velocity | {} ft / {} ft/s |".format(
                      ss.get("max_depth_ft"), ss.get("max_velocity_fps")),
                  "| Latest arrival | {} h after breach initiation |".format(
                      ss.get("latest_arrival_hr"))]
        if ss.get("receptors_flooded"):
            lines.append("| Reaches | {} |".format(ss["receptors_flooded"]))
        lines.append("")
    warns = list(r.get("warnings") or [])
    cut = any("still" in w.lower() and ("advanc" in w.lower() or "moving" in w.lower() or "edge" in w.lower())
              for w in warns)
    if cut:
        hole = any("held up by a hole" in w for w in warns)
        cap = any("duration cap" in w for w in warns)
        runs = any("domain runs" in w for w in warns)
        if hole:
            why = "Wet cells are still on DEM no-data after one refill. Depth is held up by a hole, not by the clock."
        elif cap:
            why = "The duration cap stopped the clock while water was still moving. This is not the domain-run limit."
        elif runs:
            why = "The domain-run limit stopped the run while water was still leaving an edge. The duration cap did not stop it."
        else:
            why = "The flood was still moving when the run stopped. The area and depth below are incomplete."
        reason = "DEM hole" if hole else ("duration cap" if cap else ("domain-run limit" if runs else "still moving"))
        r["cut_off"] = {"reason": reason, "detail": why}
        lines += ["## Results cut off", "", why, ""]
    if warns:
        lines += ["## Things to check", ""] + ["- " + w for w in warns] + [""]
    lines += ["## Breach (ND 5.1.1)", ""]
    for k in ("starting_wse_ft", "starting_wse_rule", "breach_bottom_ft", "hw_ft", "hb_ft",
              "storage_at_failure_acft", "storage_exponent_m", "breach_parameters",
              "failure_mode", "avg_breach_width_ft", "bottom_width_ft", "side_slope_h",
              "formation_time_hr", "pipe_initial_dia_ft", "pipe_collapse_dia_ft",
              "pipe_phase_hr", "tr60_peak_cfs", "tr60_rule", "breach_factor_br_ac",
              "embankment_area_ft2", "rise_time_hr", "base_time_hr", "shape",
              "froehlich_1995_peak_cfs", "tr60_peak_cfs_check", "tr60_rule_check", "peak_cfs",
              "time_to_peak_hr", "volume_acft", "source"):
        if k in h and h[k] is not None:
            lines.append("- {}: {}".format(k.replace("_", " "), h[k]))
    if "flood" in r and r["flood"]:
        lines += ["", "## Flood inflow scenario", ""]
        for k, v in r["flood"].items():
            lines.append("- {}: {}".format(k.replace("_", " "), v))
    if "incremental" in r:
        i = r["incremental"]
        lines += ["", "## Incremental effect of the breach", "",
                  "Area where the breach adds at least: " + ", ".join(
                      "{} — {} ac".format(k, v) for k, v in i["acres_at_or_above"].items()),
                  "", i["note"]]
        if "receptors_incremental" in r:
            lines += ["", "Receptors with an incremental depth of at least: " + "; ".join(
                "{} — {}".format(k, v) for k, v in r["receptors_incremental"].items())]
    lines += ["", "Breach inflow point: {:.1f}, {:.1f} ({}).".format(
        r["inflow_point"]["xy"][0], r["inflow_point"]["xy"][1], r["inflow_point"]["how"]), "",
        "## Downstream extent (ND 5.1.1(f))", "",
        "Each run checks the deepest water on every domain edge. An edge deeper than {} ft "
        "moves out by {} mi and the model runs again{}.".format(
            r["settings"]["edge_tolerance_ft"], r["settings"]["expand_step_mi"],
            "; with receptor layers an edge is left alone when no structure, road or rail lies "
            "in the flood's path within {} mi".format(r["settings"]["impact_look_ahead_mi"])
            if r["settings"].get("receptors") else ""), "",
        "| Run | Size (mi) | Cells | Hours | Edge depth N/S/E/W (ft) | Over | Minutes |",
        "|---|---|---|---|---|---|---|"]
    for it in r["lisflood_runs"]:
        e = it["edge_max_depth_ft"]
        lines.append("| {} | {} x {} | {:,} | {} | {}/{}/{}/{} | {} | {} |".format(
            it["run"], it["size_mi"][0], it["size_mi"][1], it["cells"], it["sim_time_hr"],
            e["N"], e["S"], e["E"], e["W"],
            ", ".join(it.get("edges_over_after_class_stop", it.get(
                "edges_over_after_receptors", it["edges_over_tolerance"]))) or "none",
            it["runtime_min"]))
    last = r["lisflood_runs"][-1]["stats"]
    lines += ["", "{} (last run): max volume error {}% of the breach volume, "
              "peak inflow {} cfs.".format(r.get("flood_engine_short", "LISFLOOD-FP"),
                                           last.get("max_volume_error_pct"),
                                           last.get("peak_inflow_cfs")), ""]
    cs = r.get("class_stop_rule")
    if cs:
        lines += ["## Class-stop rule (v3.2.0)", "",
                  "An edge the flood leaves deeper than the tolerance is accepted when, along "
                  "the flow path near it, the breach flood's depth (or, with a no-breach "
                  "baseline, its rise over the baseline) stays below {} ft - ND Dam Hazard's "
                  "termination point (a station below the threshold confirmed by {} more; a "
                  "station every {} mi, deepest wet cell within {} ft) lies inside the domain "
                  "and every station from it to the edge is below - and no structure, road or "
                  "rail lies within the receptor swath beyond the edge. {}".format(
                      cs["threshold_ft"], cs["confirm"], cs["station_mi"], cs["search_ft"],
                      cs["note"]), ""]
        if not cs.get("decisions"):
            lines += ["No edge needed the rule (the flood stayed inside the domain or the "
                      "receptor rule decided).", ""]
        for run in cs.get("decisions") or []:
            for e, dec in sorted(run["edges"].items()):
                lines.append("- Run {}, {} edge: {}.".format(run["run"], e, dec["reason"]))
                tab = stoprule.station_table(dec)
                if tab and dec.get("accept"):
                    lines += [""] + tab + [""]
        lines.append("")
    if r.get("refine_near_dam") is not None:
        from . import refine as refmod
        lines += refmod.report_lines(r["refine_near_dam"])
    if r.get("dem_conditioning"):
        c = r["dem_conditioning"]
        lines += ["## DEM conditioning", "", "- " + c["buildings"]]
        for k, v in sorted(c.get("edits", {}).items()):
            lines.append("- {}: {:,} cells".format(k, v))
        for k, v in sorted(c.get("manning_edits", {}).items()):
            lines.append("- Manning's n {}: {:,} cells".format(k, v))
        lines += ["- " + c["note"], ""]
    if r.get("structures"):
        lines += ["## Structures", ""] + ["- " + x for x in r["structures"]] + [""]
    if r.get("culvert_matches"):
        lines += ["## Culvert matches", "",
                  "Road-crossing guesses sized from an inventory record within the match "
                  "distance. Burned = cut into the model DEM.", "",
                  "| Guess | Source | Match (m) | Span (ft) | Rise (ft) | Barrels | Shape | Burned "
                  "| Inverts up / down (ft) |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for m in r["culvert_matches"]:
            iu, idn = m.get("invert_up_m"), m.get("invert_dn_m")
            inv = "{:.1f} / {:.1f}".format(iu / FT, idn / FT) if (iu is not None and
                                                                   idn is not None) else ""
            lines.append("| {} | {} | {:.1f} | {:.1f} | {:.1f} | {} | {} | {} | {} |".format(
                str(m.get("name", "")).replace("|", "/"), m.get("source", ""),
                float(m.get("match_m") or 0.0), float(m.get("width_m") or 0.0) / FT,
                float(m.get("height_m") or 0.0) / FT, m.get("barrels", ""),
                m.get("shape", ""), "yes" if m.get("burned") else "no", inv))
        plan = [m for m in r["culvert_matches"] if m.get("plan_inverts_ft")]
        if plan:
            lines += ["", "Plan-datum inverts (NDDOT Center Line Pipes, LEFT / RIGHT_ELEV_E as "
                      "recorded): reported only, not used - the inverts above are from the "
                      "LiDAR.", ""]
            for m in plan:
                le, rt = (list(m["plan_inverts_ft"]) + [None, None])[:2]
                lines.append("- {}: left {} ft, right {} ft ({}). {}".format(
                    m.get("source", ""), "?" if le is None else "{:.1f}".format(le),
                    "?" if rt is None else "{:.1f}".format(rt),
                    "datum: " + str(m.get("invert_datum") or "plan"),
                    m.get("plan_datum_note") or ""))
        lines.append("")
    if r.get("receptors"):
        lines += ["## Receptors", ""]
        for k, v in r["receptors"].items():
            if isinstance(v, dict):
                v = ", ".join("{} {}".format(a.replace("_", " "), b) for a, b in v.items())
            lines.append("- {}: {}".format(k.replace("_", " "), v))
        lines.append("")
    if "anuga_runs" in r:
        lines += ["## ANUGA", ""]
        for a in r["anuga_runs"]:
            st = a["stats"]
            lines.append("- mesh buffer {} m: {:,} triangles, {} min, deepest water at the mesh "
                         "edge {} ft".format(a["buffer_m"], st.get("triangles", 0),
                                             st.get("runtime_min"), st.get("max_edge_depth_ft")))
        c = r.get("comparison", {})
        lines += ["", "Comparison with LISFLOOD-FP (cells deeper than the arrival depth): "
                  "LISFLOOD-FP {} ac, ANUGA {} ac, both {} ac, fit index F = {}; mean depth "
                  "difference (ANUGA - LISFLOOD-FP) {} ft, 90th percentile |difference| {} ft."
                  .format(c.get("lisflood_acres"), c.get("anuga_acres"), c.get("both_acres"),
                          c.get("fit_index_F"), c.get("depth_diff_mean_ft"),
                          c.get("depth_diff_abs_p90_ft")), ""]
    lines += ["## Outputs", ""]
    for kind, p in r["outputs"]["final"].items():
        lines.append("- {}: {}".format(kind, p))
    for kind, p in r["outputs"].get("baseline", {}).items():
        lines.append("- baseline {}: {}".format(kind, p))
    for kind, p in r["outputs"].get("increment", {}).items():
        lines.append("- incremental {}: {}".format(kind, p))
    for kind, p in r["outputs"].get("vectors", {}).items():
        lines.append("- {}: {}".format(kind.replace("_", " "), p))
    if r["outputs"].get("eap_map"):
        lines.append("- EAP inundation map preview (the EAP map is ND Dam Hazard's map book on "
                     "aerial imagery): {}".format(r["outputs"]["eap_map"]))
    elif r.get("eap_map_note"):
        lines.append("- EAP map: {}".format(r["eap_map_note"]))
    if r.get("dam_record"):
        from . import damjson
        lines += [""] + damjson.report_lines(r["dam_record"])
    lines += ["", "Units: {}.".format(s["units"]),
              "These rasters are the input for the ND hazard classification; this tool does "
              "not classify the dam.", ""]
    return "\n".join(lines)
