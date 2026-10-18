"""Hand-off from the screening results to ND Dam Breach Runner (tool 7b). Pure Python, no QGIS.

Everything that decides WHAT is sent to 'ndbreach:rundam' and 'ndbreach:ndhazardclass' is
here, so it can be unit-tested and checked against the runner's own parameter definitions
(tests/test_handoff.py imports the runner's algorithms through its fake QGIS harness).
"""
import csv
import json
import math
import os
import re

try:
    from . import hazard as hz
    from . import nd_hazard as nh
except ImportError:                    # core/ itself on sys.path
    import hazard as hz
    import nd_hazard as nh

RUNNER_ALG = "ndbreach:rundam"
HAZARD_ALG = "ndbreach:ndhazardclass"
RUNNER_MIN_VERSION = "3.1"
RUNNER_JSON = "Hazard Classification (ND 5.2).json"       # written by ndhazardclass
RUNNER_HYDRO_CSV = os.path.join("inputs", "breach_hydrograph.csv")   # runner convention

FT = 0.3048
AF_M3 = 1233.4818375
ACRE_M2 = 4046.8564224
CFS_PER_M3S = 1.0 / FT ** 3            # 35.3147

# rundam METHOD enum: index of "My own hydrograph (CSV: time hr, discharge cfs)" in the runner
# v3.0 list (checked against the runner in tests); at run time the index is looked up from the
# installed runner's parameter definition and this is only the fallback.
USER_METHOD_INDEX = 9

# every parameter name this plugin sends (verified against the runner in tests/test_handoff.py)
RUNDAM_KEYS = ("DAM_ID", "DAM_NAME", "DAM_POINT", "HEIGHT_FT", "STORAGE_ACFT", "AREA_AC", "DEM",
               "DAMS_FOLDER", "CELL", "METHOD", "USER_CSV", "RECEPTOR_POINTS", "RECEPTOR_ROADS",
               "RECEPTOR_RAIL", "RECEPTOR_POINTS_MORE", "RECEPTOR_ROADS_MORE",
               "TIER", "FORCE_ANUGA", "REFINE_NEAR", "STRUCTURES", "S_KIND", "S_WIDTH", "S_HEIGHT",
               "S_BARRELS", "S_SHAPE", "S_NAME")
# 7b engine choice (tool parameter -> rundam parameter). The Runner's TIER index 0 is
# LISFLOOD-FP only, 1 is LISFLOOD-FP then ANUGA. "Solver" here is the Runner's near-dam FV1
# refine (REFINE_NEAR); rundam has no other solver switch.
#
# 0.17.22 (QGIS Dev's #22 review): ENGINE is the ONE engine choice. The old FORCE_ANUGA box did
# exactly what option 1 does (the Runner turns FORCE_ANUGA into TIER 1), so it is folded in:
# an old FORCE_ANUGA = True (saved settings, models, histories) still selects option 1, and
# only TIER is sent. ANUGA is the only engine that models culverts / bridges.
ENGINE_LABELS = ["LISFLOOD-FP only (Runner default; culverts / bridges not modelled)",
                 "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA; "
                 "= the Runner's 'force ANUGA')"]
ENGINE_SHORT = ["LISFLOOD-FP only", "LISFLOOD-FP \u2192 ANUGA (final results from ANUGA)"]
ANUGA_SETTING = "NDBREACH_ANUGA_PYTHON"          # the Runner's ANUGA interpreter setting / env
ANUGA_MISSING = ("ANUGA was requested (Runner models = LISFLOOD-FP then ANUGA) but no ANUGA "
                 "environment was found: set 'ANUGA Python interpreter' in Processing > Options > "
                 "Providers > ND Dam Breach Runner (or the NDBREACH_ANUGA_PYTHON environment "
                 "variable), install it with DamSafety Setup, or choose 'LISFLOOD-FP only'. "
                 "Nothing was run.")
CULVERTS_IGNORED = ("Culverts / bridges (CULVERTS) are only modelled by ANUGA; this run uses "
                    "LISFLOOD-FP only, so the culvert layer is ignored. Choose 'LISFLOOD-FP for "
                    "the extent, then ANUGA' to use it.")


def effective_engine(engine=0, force_anuga=False):
    """The one engine choice: 0 = LISFLOOD-FP only, 1 = LISFLOOD-FP then ANUGA. A legacy
    FORCE_ANUGA = True means 1."""
    try:
        e = int(engine or 0)
    except (TypeError, ValueError):
        e = 0
    return 1 if (e >= 1 or force_anuga) else 0


def engine_status(engine, culverts=None):
    """The one-line status the window and the log show, e.g. 'Runner engine: LISFLOOD-FP ->
    ANUGA (final results from ANUGA)'."""
    e = effective_engine(engine)
    s = "Runner engine (7b): " + ENGINE_SHORT[e]
    if culverts not in (None, ""):
        s += "; culverts: " + ("used by ANUGA" if e else "IGNORED (LISFLOOD-FP only)")
    return s


def find_anuga_python(setting=None, env=None, detect=None, isfile=None):
    """The ANUGA interpreter the Runner would use, or ''. setting(): the Runner's Processing
    setting; env: os.environ; detect(): the Runner's own auto-detect (nd_data.detect_anuga_python);
    isfile: os.path.isfile."""
    isfile = isfile or os.path.isfile
    env = os.environ if env is None else env
    for get in (lambda: setting() if setting else "", lambda: env.get(ANUGA_SETTING, "")):
        try:
            v = (get() or "").strip()
        except Exception:                                        # noqa
            v = ""
        if v and isfile(v):
            return v
    if detect is not None:
        try:
            v = detect() or ""
        except Exception:                                        # noqa
            v = ""
        if v and isfile(v):
            return v
    return ""
# 7b's culvert / bridge field parameters -> the Runner's STRUCTURES field parameters.
# (7b's own STRUCTURES-like layer is BLD = buildings, so the culvert layer is CULVERTS here.)
CULVERT_FIELDS = (("C_KIND", "S_KIND", "kind (culvert / bridge / notch)"),
                  ("C_WIDTH", "S_WIDTH", "span or diameter (ft)"),
                  ("C_HEIGHT", "S_HEIGHT", "rise (ft)"),
                  ("C_BARRELS", "S_BARRELS", "barrels"),
                  ("C_SHAPE", "S_SHAPE", "shape (box / pipe)"),
                  ("C_NAME", "S_NAME", "name"))
NDHAZARD_KEYS = ("DAM_FOLDER", "STRUCTURES", "S_HABITABLE", "S_OCC", "S_MOBILE",
                 "UNINHABITED_WORDS", "ROADS", "R_AADT", "R_CLASS", "R_SOLE", "RAIL", "T_TRAINS",
                 "T_PASSENGER", "PASSENGER_CODES", "LIFELINES", "L_KIND", "DOWNSTREAM_DAMS", "D_ID",
                 "D_CLASS", "LETHALITY", "WET_FT", "UNKNOWN_AADT", "UNKNOWN_TRAINS", "CHART_FILE",
                 "STRUCTURES_MORE", "ROADS_MORE", "LIFELINES_MORE")

# which dams to send
SELECT_HIGH_MEDIUM, SELECT_HIGH, SELECT_CHANGED, SELECT_ALL, SELECT_SELECTED, SELECT_CLASSIFIED = range(6)
SELECT_LABELS = ["High and Medium from tool 8 (hz_class), and dams with no class (NoData / empty: "
                 "the simulation failed, so the class is unknown)",
                 "High only (hz_class)",
                 "Borderline (hz_border = yes) or class changed vs the existing class "
                 "(hz_vs_existing)",
                 "All dams",
                 "Selected features only",
                 "High, Medium, Low and unknown (all classified dams)"]
NEEDS_TOOL8 = (SELECT_HIGH_MEDIUM, SELECT_HIGH, SELECT_CHANGED, SELECT_CLASSIFIED)

# hz_class values that mean "no class": the simulation failed or the class was never set. A
# layer edited by hand or from another source can say Unknown / Undetermined / TBD instead of
# leaving the field empty; all of them are treated like an empty class.
UNKNOWN_CLASSES = ("", "nodata", "no data", "null", "none", "nan", "unknown", "undetermined",
                   "not determined", "undet", "tbd", "to be determined", "n/a", "na", "?",
                   "unclassified", "not classified", "pending")
KNOWN_CLASSES = ("High", "Medium", "Low")


def class_of(attrs):
    """(class, unknown?): "High" / "Medium" / "Low" (any letter case) or the raw text, and
    True when it means no class (UNKNOWN_CLASSES)."""
    raw = attrs.get("hz_class")
    cls = "" if raw is None else str(raw).strip()
    for k in KNOWN_CLASSES:
        if cls.lower() == k.lower():
            return k, False
    return cls, cls.lower() in UNKNOWN_CLASSES

HYDRO_RUNNER, HYDRO_SCREENING = range(2)
HYDRO_LABELS = ["Runner's own ND method (its default breach method; ND 5.1.1)",
                "Screening tool-5 hydrograph (td_csv), written as the runner's own-hydrograph CSV"]

# storage at the top of dam, in order of preference: (field, unit factor to m3, description)
STORAGE_SOURCES = (("td_vw_m3", 1.0, "tool 5 storage at the top of dam (td_vw_m3)"),
                   ("cal_total_m3", 1.0, "tool 3 calibrated storage at the crest (cal_total_m3)"),
                   ("est_total_m3", 1.0, "tool 3 estimated storage at the crest (est_total_m3)"),
                   ("inv_max_m3", 1.0, "inventory maximum storage (inv_max_m3)"))


class HandoffError(ValueError):
    """A dam cannot be sent to the runner (missing input)."""


def safe_name(s):
    """The runner's folder name for a dam ID (same rule as nd_breach_runner.algorithms)."""
    s = re.sub(r"[^\w\- ]+", "_", str(s)).strip()
    return s or "dam"


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(attrs, key):
    x = _num(attrs.get(key))
    return x if x is not None and x > 0 else None


# ---------------------------------------------------------------------------------- selection
def selected(attrs, mode, is_selected=False):
    """(send?, why) for one dam record; `why` says why it is sent or skipped."""
    cls, unknown = class_of(attrs)
    if mode == SELECT_ALL:
        return True, "all dams"
    if mode == SELECT_SELECTED:
        return (True, "selected") if is_selected else (False, "not selected")
    if mode == SELECT_HIGH_MEDIUM:
        if cls in ("High", "Medium"):
            return True, "screening class {}".format(cls)
        if unknown:
            return True, "no screening class ({}): unknown, so sent".format(cls or "empty")
        return False, "screening class {}".format(cls)
    if mode == SELECT_CLASSIFIED:
        if cls in KNOWN_CLASSES:
            return True, "screening class {}".format(cls)
        if unknown:
            return True, "no screening class ({}): unknown, so sent".format(cls or "empty")
        return False, "screening class {!r} is not High, Medium, Low or unknown".format(cls)
    if mode == SELECT_HIGH:
        return (True, "screening class High") if cls == "High" else \
            (False, "screening class {}".format(cls or "none"))
    if mode == SELECT_CHANGED:
        border = str(attrs.get("hz_border") or "").strip().lower() == "yes"
        vs = str(attrs.get("hz_vs_existing") or "")
        if border:
            return True, "borderline (hz_border = yes)"
        if vs in ("higher than existing", "lower than existing"):
            return True, "screening class {} {}".format(cls, vs)
        return False, "not borderline and agrees with the existing class ({})".format(vs or "n/a")
    raise ValueError("unknown selection mode {}".format(mode))


def agreement(runner_class, screening_class):
    r, s = nh.RANK.get(runner_class), nh.RANK.get(screening_class)
    if r is None or s is None:
        return "n/a"
    return "agree" if r == s else ("runner higher" if r > s else "runner lower")


# ---------------------------------------------------------------------------------- inputs
def area_at_top_m2(curve_csv, crest_m=None, dz=0.3):
    """Surface area near the top of dam from a tool-3 stage-storage curve (elev_m, storage_m3):
    mean area over the top `dz` metres below the crest (the last point when no crest is given).
    None when the file is missing or unusable."""
    if not curve_csv or not os.path.exists(curve_csv):
        return None
    e, v = [], []
    with open(curve_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            a, b = _num(row.get("elev_m")), _num(row.get("storage_m3"))
            if a is not None and b is not None:
                e.append(a)
                v.append(b)
    if len(e) < 2:
        return None
    pts = sorted(zip(e, v))
    e, v = [p[0] for p in pts], [p[1] for p in pts]
    top = min(crest_m, e[-1]) if crest_m is not None else e[-1]
    lo = max(top - dz, e[0])
    if top - lo <= 1e-6:
        return None

    def interp(z):
        for (z0, v0), (z1, v1) in zip(pts[:-1], pts[1:]):
            if z0 <= z <= z1:
                return v0 if z1 == z0 else v0 + (v1 - v0) * (z - z0) / (z1 - z0)
        return v[-1] if z > e[-1] else v[0]
    a = (interp(top) - interp(lo)) / (top - lo)
    return a if a > 0 else None


# ported from nd_ Screening 0.28.3 (core/handoff.py SHALLOW_CURVE / area_for_runner): a curve
# that spans less than this fraction of the dam height does not describe the whole reservoir,
# so its area at the top is not the area at the top of dam
SHALLOW_CURVE = 0.5


def curve_span_m(curve_csv, crest_m=None):
    """Elevation span (m) of a tool-3 curve up to the crest, or None."""
    if not curve_csv or not os.path.exists(curve_csv):
        return None
    e = []
    with open(curve_csv, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            a, b = _num(row.get("elev_m")), _num(row.get("storage_m3"))
            if a is not None and b is not None:
                e.append(a)
    if len(e) < 2:
        return None
    e.sort()
    top = min(crest_m, e[-1]) if crest_m is not None else e[-1]
    return top - e[0]


def area_for_runner(curve_csv, crest_m=None, height_m=None, dz=0.3):
    """(surface area at the top of dam in m2 or None, note) for the Runner's AREA_AC.

    None when the curve spans much less than the dam height: the Runner turns the area into
    its storage-area exponent (area * height / storage), so an area measured over a 4 ft-deep
    basin of a 27 ft dam gave an exponent at the Runner's cap of 6 and a peak breach flow about
    a third too high. With no area the Runner assumes storage ~ depth squared and says so."""
    span = curve_span_m(curve_csv, crest_m)
    if span is None:
        return None, ""
    try:
        h = float(height_m) if height_m not in (None, "") else None
    except (TypeError, ValueError):
        h = None
    if h and span < SHALLOW_CURVE * h:
        return None, ("no surface area sent: the stage-storage curve spans {:.1f} ft of the "
                      "{:.1f} ft dam height, too little to give the area at the top of dam (the "
                      "Runner assumes storage varies with depth squared)".format(span / FT, h / FT))
    return area_at_top_m2(curve_csv, crest_m, dz), ""


def write_runner_hydrograph(src_csv, dst_csv):
    """Tool-5 hydrograph (time_s, discharge_m3s) -> the runner's own-hydrograph CSV
    (time hr, discharge cfs). Returns (dst_csv, peak_cfs)."""
    rows = []
    with open(src_csv, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            t, q = _num(parts[0]), _num(parts[1])
            if t is None or q is None:
                if i == 0:
                    continue                      # header
                raise HandoffError("bad row {} in {}".format(i + 1, src_csv))
            rows.append((t / 3600.0, max(q, 0.0) * CFS_PER_M3S))
    if len(rows) < 2:
        raise HandoffError("the tool-5 hydrograph {} has fewer than two rows".format(src_csv))
    d = os.path.dirname(dst_csv)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(dst_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time_hr", "discharge_cfs"])
        for t, q in rows:
            w.writerow(["{:.5f}".format(t), "{:.3f}".format(q)])
    return dst_csv, max(q for _, q in rows)


def rundam_params(attrs, point_xy, crs_authid, dem, dams_folder, cell_m=None, user_csv=None,
                  area_top_m2=None, user_method_index=USER_METHOD_INDEX):
    """(parameters for 'ndbreach:rundam', notes) for one screening dam record.

    attrs: the dam's fields (screening names, metric units). point_xy: the tool-4 breach point
    (on the DWR dam line, i.e. on the crest) in `crs_authid`. No DWR elevations are passed
    (local datum): TOP_FT is left empty so the runner takes the highest DEM point within 30 m.
    Raises HandoffError when the dam height or the storage is missing.
    """
    dam_id = str(attrs.get("dam_id") or "").strip()
    if not dam_id:
        raise HandoffError("no dam_id")
    notes = []
    h_m = _pos(attrs, "height_m")
    if h_m is None:
        h_m = _pos(attrs, "td_hb_m")
        if h_m is None:
            raise HandoffError("no dam height (height_m, or tool-5 td_hb_m)")
        notes.append("height from the tool-5 breach height td_hb_m (no inventory height)")
    vol, how = None, ""
    for key, k, desc in STORAGE_SOURCES:
        x = _pos(attrs, key)
        if x is not None:
            vol, how = x * k, desc
            break
    if vol is None:
        raise HandoffError("no storage (td_vw_m3, cal_total_m3, est_total_m3 or inv_max_m3)")
    height_ft = h_m / FT
    storage_acft = vol / AF_M3
    if height_ft < 0.1 or storage_acft < 0.01:
        raise HandoffError("height {:.2f} ft or storage {:.3f} ac-ft below the runner's "
                           "minimum".format(height_ft, storage_acft))
    notes.append("height {:.1f} ft; storage {:,.1f} ac-ft from {}".format(height_ft,
                                                                        storage_acft, how))
    p = {"DAM_ID": dam_id,
         "DAM_POINT": "{:.3f},{:.3f} [{}]".format(point_xy[0], point_xy[1], crs_authid),
         "HEIGHT_FT": round(height_ft, 3),
         "STORAGE_ACFT": round(storage_acft, 3),
         "DEM": dem,
         "DAMS_FOLDER": dams_folder}
    name = attrs.get("name")
    if name not in (None, ""):
        p["DAM_NAME"] = str(name)
    if area_top_m2 is not None and area_top_m2 > 0:
        p["AREA_AC"] = round(area_top_m2 / ACRE_M2, 3)
        notes.append("surface area {:,.1f} ac near the top of dam from the tool-3 curve".format(
            p["AREA_AC"]))
    else:
        notes.append("no surface area at the top of dam (normal-pool areas are not passed)")
    if cell_m is not None and cell_m > 0:
        p["CELL"] = float(cell_m)
    if user_csv:
        p["METHOD"] = int(user_method_index)
        p["USER_CSV"] = user_csv
        notes.append("breach hydrograph: screening tool 5 ({})".format(os.path.basename(user_csv)))
    else:
        notes.append("breach hydrograph: runner's own ND method")
    return p, notes


def engine_params(tier=0, force_anuga=False, refine_near=False, culverts=None,
                  culvert_fields=None):
    # force_anuga: the legacy box, folded into tier (effective_engine); FORCE_ANUGA is no
    # longer sent - TIER 1 is what the Runner does with it.
    """(extra 'ndbreach:rundam' parameters, notes) for 7b's engine and culvert choices.

    Only what differs from the Runner's own defaults is sent, so the defaults (tier 0, no
    force, no refine, no culverts) add nothing and the run is the same as before.
    culvert_fields: {C_KIND, ...: field name or ''}; empty names are left out so the Runner
    matches its own synonyms (kind, width_ft, height_ft, barrels, shape, name ...)."""
    p, notes = {}, []
    tier = effective_engine(tier, force_anuga)
    if tier:
        p["TIER"] = tier
        notes.append("models: " + ENGINE_LABELS[tier])
    if refine_near:
        p["REFINE_NEAR"] = True
        notes.append("near-dam refine with the FV1 solver")
    if culverts not in (None, ""):
        p["STRUCTURES"] = culverts
        for key, rkey, _d in CULVERT_FIELDS:
            v = (culvert_fields or {}).get(key)
            if v:
                p[rkey] = str(v)
        notes.append("culverts / bridges: {}".format(culverts))
        if not tier:
            # the Runner uses the culvert layer only in ANUGA; say so instead of dropping it quietly
            notes.append(CULVERTS_IGNORED)
    return p, notes


def ndhazard_params(dam_folder, layers=None, fields=None, lethality=0, unknown_aadt=None,
                    chart_file="", passenger_codes="", unknown_trains=None, wet_m=None,
                    uninhabited_words=""):
    """Parameters for 'ndbreach:ndhazardclass'. layers: {STRUCTURES, ROADS, RAIL, LIFELINES,
    DOWNSTREAM_DAMS: value or None}; fields: {S_OCC, D_ID, ...: name or ''} - empty names are left
    out so the runner matches its own synonyms. wet_m: the screening's 'any depth' (m), sent as
    WET_FT."""
    p = {"DAM_FOLDER": dam_folder, "LETHALITY": int(lethality)}
    if unknown_trains is not None:
        p["UNKNOWN_TRAINS"] = int(unknown_trains)
    if wet_m is not None:
        p["WET_FT"] = round(float(wet_m) / FT, 6)
    if uninhabited_words:
        p["UNINHABITED_WORDS"] = uninhabited_words
    for k, v in (layers or {}).items():
        if v not in (None, ""):
            p[k] = v
    for k, v in (fields or {}).items():
        if v:
            p[k] = v
    if unknown_aadt is not None:
        p["UNKNOWN_AADT"] = int(unknown_aadt)
    if chart_file:
        p["CHART_FILE"] = chart_file
    if passenger_codes:
        p["PASSENGER_CODES"] = passenger_codes
    return p


def read_runner_json(path):
    """{class, border, gaps} from the runner's 'Hazard Classification (ND 5.2).json'."""
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    cls = d.get("hazard_class")
    if cls not in nh.RANK:
        raise HandoffError("no hazard class in {}".format(path))
    return {"class": cls, "border": "yes" if d.get("borderline_raised") else "no",
            "gaps": hz.limit_text(d.get("data_gaps") or []),
            "lethality": d.get("lethality_label") or (d.get("settings") or {}).get("lethality")}
