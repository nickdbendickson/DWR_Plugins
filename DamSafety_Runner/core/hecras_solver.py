"""3.10.85: tighter HEC-RAS solver and plan settings for the generated model (Nick, 2026-10-26:
"max iterations 40-50, computation step 2-3 s, refine the 2D-to-structure/SA connection and
breach settings, normal-depth outflow where the water reaches the perimeter or clip the 2D area
to the ANUGA mesh").

Every setting comes from (first found) the build option ``solver`` (a dict with the keys
below), the NDBREACH_HECRAS_* environment variable, then the default. ``settings()`` checks
every value before any work and returns ``{key: value}`` plus ``"_source": {key: "option" |
"env" | "default"}``.

key                 variable                           default  before 3.10.85
max_iter            NDBREACH_HECRAS_MAX_ITER           40       20 (tight: 30)
                    2D 'UNET D2 Max Iterations' (both D2 blocks) and the 1D/storage-area
                    'UNET MxIter'. 40 is the most HEC-RAS accepts (its 2D editor takes 0-40),
                    so 50 cannot be written; 1-40 allowed.
dt_mode             NDBREACH_HECRAS_DT_MODE            cap      courant
                    'cap': Computation Interval = the Courant estimate (Courant x cell /
                    velocity) but never above dt_s, and the adaptive step may double only while
                    it stays at or below dt_s (it may still halve); 'fixed': Computation
                    Interval = dt_s, no adaptive step; 'courant': the old rule (no cap).
dt_s                NDBREACH_HECRAS_DT_S               3        (none)
                    the cap / fixed step, seconds (a HEC-RAS interval: 0.1-60 s, snapped down
                    to the nearest one HEC-RAS has, e.g. 2.5 -> 2SEC).
d1d2_maxiter        NDBREACH_HECRAS_D1D2_MAXITER       3        0
                    'UNET D1D2 MaxIter': iterations between the reservoir storage area and the
                    2D area at the dam connection each step (HEC-RAS: 0-20, default 0; HEC
                    suggests 3-4 for submerged or reversing 1D/2D connections - the dam
                    connection is submerged by the breach tailwater). 0 = old.
conn_spacing        NDBREACH_HECRAS_CONN_SPACING       auto     auto (0.5)
                    near point spacing along the dam connection and the breach breaklines, x
                    the 2D cell (0.1-1), Runner grid points only (gmsh sizes its own); the
                    breach row (hecras_breaklines.breach_row_spacing) follows it. auto = the
                    breakline default (0.5 cell). Finer rows were tried as the default: 0.35
                    and 0.4 left cells with more than 8 faces in the uniform-grid test builds
                    (HEC-RAS refuses those), 0.45 passed them, so finer rows are opt-in.
dam_weir_coef       NDBREACH_HECRAS_DAM_WEIR_COEF      2.6      2.6 (US units; SI is x 0.552)
struct_weir_coef    NDBREACH_HECRAS_STRUCT_WEIR_COEF   2.6      2.6 (road overflow at culverts
                                                                and bridges)
culvert_ke_pipe     NDBREACH_HECRAS_CULVERT_KE_PIPE    0.5      0.5 (square edge, headwall)
culvert_ke_box      NDBREACH_HECRAS_CULVERT_KE_BOX     0.4      0.4 (30-75 deg wingwalls)
culvert_exit_loss   NDBREACH_HECRAS_CULVERT_EXIT_LOSS  1.0      1.0
outflow             NDBREACH_HECRAS_OUTFLOW            auto     single
                    'auto': clip the 2D area to the ANUGA mesh outline when the run has one
                    (<model folder>/anuga/mesh_boundary.csv, or anuga_mesh), else keep the
                    buffered flood outline; either way every stretch of the perimeter the
                    run's flood reaches gets a normal-depth outflow line besides the main
                    downstream one. 'anuga': the same, but a WARNING when there is no mesh.
                    'edges': no clip, outflow edges. 'single': the old single downstream line.
outflow_min_cells   NDBREACH_HECRAS_OUTFLOW_MIN_CELLS  1.0      -
                    an outflow stretch shorter than this x the cell is dropped (0.5-20).
anuga_mesh          NDBREACH_HECRAS_ANUGA_MESH         ''       -
                    the ANUGA mesh outline to clip to (mesh_boundary.csv, x,y in the run's
                    work CRS, as ANUGA writes it) when it is not the run's own (e.g. the
                    ANUGA run of a chain).
adaptive            NDBREACH_HECRAS_ADAPTIVE           on       on
                    HEC-RAS's variable time step ('Computation Time Step Use Courant=-1'):
                    the step is halved when the Courant number passes courant_max and doubled
                    after count_to_double steps below courant_min (never above dt_s in 'cap'
                    mode). off = fixed Computation Interval.
courant_max         NDBREACH_HECRAS_COURANT_MAX        target   target (1.0 SWE, 2.0 DWE)
courant_min         NDBREACH_HECRAS_COURANT_MIN        0.45 x max
                    HEC-RAS requires min < 0.5 x max; the pair targets Courant about 1.
count_to_double     NDBREACH_HECRAS_COUNT_TO_DOUBLE    4        4
max_halving         NDBREACH_HECRAS_MAX_HALVING        3        3 (step / 8 at most)
cores               NDBREACH_HECRAS_CORES              auto     all (0)
                    'UNET D2 Cores' (both D2 blocks and the plan line). auto: a mesh under
                    cores_small_cells cells gets cores_small cores (HEC: small meshes run no
                    faster, often slower, on more cores); a larger one gets the PHYSICAL
                    performance cores of the build host (no hyper-threads, no E-cores; Linux
                    /sys topology, else psutil, else half the logical CPUs). 'all' = 0
                    (HEC-RAS uses every logical CPU, the old behaviour) or a number 1-256.
                    Detected on the machine that builds the model: set it when HEC-RAS runs
                    elsewhere.
cores_small         NDBREACH_HECRAS_CORES_SMALL        4        (4-6)
cores_small_cells   NDBREACH_HECRAS_CORES_SMALL_CELLS  10000
ic_time_hr          NDBREACH_HECRAS_IC_TIME_HR         0.5      0 (off)
                    'UNET D2 TotalICTime': the 2D area is run this long before the start
                    with its boundaries ramped from zero (dry) to their first value, so the
                    solver starts from a settled state instead of a jump (HEC: use it when
                    flow enters the 2D area from the start, e.g. the engine's inflow line or
                    a spillway hold). 0 = off.
ic_ramp_frac        NDBREACH_HECRAS_IC_RAMP_FRAC       0.5      0.1
                    'UNET D2 RampUpFraction': the part of ic_time_hr spent ramping (the rest
                    holds the boundaries so the flow settles).
breach_progression  NDBREACH_HECRAS_BREACH_PROGRESSION sine     linear
                    'Breach Progression': sine = HEC's sine-wave curve (slow start, fastest
                    in the middle, slow finish; 11 points 0.5 x (1 - cos(pi t))), so the
                    breach flow starts smoothly instead of a step in growth rate; linear =
                    the old two-point (0,0)-(1,1) table. Same formation time either way.
"""
import math
import os

MAX_ITER_LIMIT = 40          # HEC-RAS 2D editor: 0 - 40
D1D2_LIMIT = 20              # HEC-RAS 1D/2D options: 0 - 20
DT_MODES = ("cap", "fixed", "courant")
OUTFLOW_MODES = ("auto", "anuga", "edges", "single")
PROGRESSIONS = ("sine", "linear")
SINE_POINTS = 11
ANUGA_MESH_CSV = os.path.join("anuga", "mesh_boundary.csv")
OUTFLOW_NAME = "Outflow {}"     # the extra normal-depth lines: Outflow 1, Outflow 2, ...
MAX_OUTFLOW_LINES = 20

DEFAULTS = {"max_iter": 40, "dt_mode": "cap", "dt_s": 3.0, "d1d2_maxiter": 3,
            "conn_spacing": None, "dam_weir_coef": 2.6, "struct_weir_coef": 2.6,
            "culvert_ke_pipe": 0.5, "culvert_ke_box": 0.4, "culvert_exit_loss": 1.0,
            "outflow": "auto", "outflow_min_cells": 1.0, "anuga_mesh": "",
            "adaptive": True, "courant_max": None, "courant_min": None, "count_to_double": 4,
            "max_halving": 3, "cores": "auto", "cores_small": 4, "cores_small_cells": 10000,
            "ic_time_hr": 0.5, "ic_ramp_frac": 0.5, "breach_progression": "sine"}
# the settings of 3.10.84 (for comparisons and the README)
OLD = {"max_iter": 20, "dt_mode": "courant", "dt_s": None, "d1d2_maxiter": 0,
       "conn_spacing": None, "dam_weir_coef": 2.6, "struct_weir_coef": 2.6,
       "culvert_ke_pipe": 0.5, "culvert_ke_box": 0.4, "culvert_exit_loss": 1.0,
       "outflow": "single", "outflow_min_cells": None, "anuga_mesh": "",
       "adaptive": True, "courant_max": None, "courant_min": None, "count_to_double": 4,
       "max_halving": 3, "cores": "all", "cores_small": 4, "cores_small_cells": 10000,
       "ic_time_hr": 0.0, "ic_ramp_frac": 0.1, "breach_progression": "linear"}
ENV = {k: "NDBREACH_HECRAS_" + k.upper() for k in DEFAULTS}


class SolverSettingError(ValueError):
    pass


def _num(key, v, lo, hi, integer=False):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SolverSettingError("{} must be a number (got {!r})".format(ENV[key], v))
    if integer:
        if x != int(x):
            raise SolverSettingError("{} must be a whole number (got {!r})".format(ENV[key], v))
        x = int(x)
    if not lo <= x <= hi:
        raise SolverSettingError("{} must be {:g} to {:g} (got {!r}){}".format(
            ENV[key], lo, hi, v, "; HEC-RAS accepts at most 40 iterations" if key ==
            "max_iter" else ""))
    return x


def _opt_num(key, v, lo, hi):
    if v is None or str(v).strip() == "":
        return None
    return _num(key, v, lo, hi)


def _bool(key, v):
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t in ("1", "on", "true", "yes"):
        return True
    if t in ("0", "off", "false", "no"):
        return False
    raise SolverSettingError("{} must be on or off (got {!r})".format(ENV[key], v))


def _cores(v):
    t = str(v).strip().lower()
    if t in ("auto", "all"):
        return t
    return _num("cores", v, 1, 256, True)


def _choice(key, v, allowed):
    s = str(v).strip().lower()
    if s not in allowed:
        raise SolverSettingError("{} must be one of {} (got {!r})".format(
            ENV[key], ", ".join(allowed), v))
    return s


CHECKS = {
    "max_iter": lambda v: _num("max_iter", v, 1, MAX_ITER_LIMIT, True),
    "dt_mode": lambda v: _choice("dt_mode", v, DT_MODES),
    "dt_s": lambda v: _num("dt_s", v, 0.1, 60.0),
    "d1d2_maxiter": lambda v: _num("d1d2_maxiter", v, 0, D1D2_LIMIT, True),
    "conn_spacing": lambda v: (None if v is None or str(v).strip().lower() in ("", "auto")
                               else _num("conn_spacing", v, 0.1, 1.0)),
    "dam_weir_coef": lambda v: _num("dam_weir_coef", v, 1.0, 4.0),
    "struct_weir_coef": lambda v: _num("struct_weir_coef", v, 1.0, 4.0),
    "culvert_ke_pipe": lambda v: _num("culvert_ke_pipe", v, 0.0, 1.0),
    "culvert_ke_box": lambda v: _num("culvert_ke_box", v, 0.0, 1.0),
    "culvert_exit_loss": lambda v: _num("culvert_exit_loss", v, 0.0, 1.5),
    "outflow": lambda v: _choice("outflow", v, OUTFLOW_MODES),
    "outflow_min_cells": lambda v: _num("outflow_min_cells", v, 0.5, 20.0),
    "anuga_mesh": lambda v: str(v or "").strip(),
    "adaptive": lambda v: _bool("adaptive", v),
    "courant_max": lambda v: _opt_num("courant_max", v, 0.1, 5.0),
    "courant_min": lambda v: _opt_num("courant_min", v, 0.01, 2.5),
    "count_to_double": lambda v: _num("count_to_double", v, 1, 100, True),
    "max_halving": lambda v: _num("max_halving", v, 0, 10, True),
    "cores": _cores,
    "cores_small": lambda v: _num("cores_small", v, 4, 6, True),
    "cores_small_cells": lambda v: _num("cores_small_cells", v, 100, 10000000, True),
    "ic_time_hr": lambda v: _num("ic_time_hr", v, 0.0, 24.0),
    "ic_ramp_frac": lambda v: _num("ic_ramp_frac", v, 0.0, 1.0),
    "breach_progression": lambda v: _choice("breach_progression", v, PROGRESSIONS),
}

# the adaptive time step dict keys (hecras_model.COURANT_DEFAULTS) each setting overrides
COURANT_KEYS = {"adaptive": "adaptive", "courant_max": "max", "courant_min": "min",
                "count_to_double": "count_to_double", "max_halving": "max_halving"}


def settings(options=None, environ=None):
    """The solver settings: build option ``solver`` (dict), else NDBREACH_HECRAS_*, else
    DEFAULTS. Raises SolverSettingError naming the variable on a bad value."""
    env = os.environ if environ is None else environ
    opt = (options or {}).get("solver") or {}
    if not isinstance(opt, dict):
        raise SolverSettingError("the 'solver' option must be a dict of {}".format(
            ", ".join(DEFAULTS)))
    bad = sorted(set(opt) - set(DEFAULTS))
    if bad:
        raise SolverSettingError("unknown solver setting(s) {} (known: {})".format(
            ", ".join(bad), ", ".join(DEFAULTS)))
    out, src = {}, {}
    for k, dflt in DEFAULTS.items():
        if opt.get(k) is not None:
            v, s = opt[k], "option"
        elif str(env.get(ENV[k], "")).strip() != "":
            v, s = env[ENV[k]], "env"
        else:
            v, s = dflt, "default"
        out[k] = CHECKS[k](v)
        src[k] = s
    if out["courant_max"] is not None and out["courant_min"] is not None and \
            not out["courant_min"] < 0.5 * out["courant_max"]:
        raise SolverSettingError("{} must be under half of {} (HEC-RAS; got {:g} and {:g})".format(
            ENV["courant_min"], ENV["courant_max"], out["courant_min"], out["courant_max"]))
    out["_source"] = src
    return out


def changed(s):
    """[(key, old, new)] of the settings that differ from 3.10.84."""
    return [(k, OLD[k], s[k]) for k in DEFAULTS if OLD[k] != s[k]]


def time_step(courant_dt_s, s, comp_interval, courant=None):
    """(seconds, label, courant dict, how) of the plan's Computation Interval under the
    settings `s`: comp_interval(dt) -> (seconds, label) is hecras_model's; `courant` the
    adaptive-step dict (COURANT_DEFAULTS merged with the build's time_step), which is
    returned changed (cap: max_doubling limited so the step stays at or below dt_s; fixed:
    adaptive off)."""
    c = dict(courant or {})
    mode = s["dt_mode"]
    if mode == "courant":
        sec, lab = comp_interval(courant_dt_s)
        return sec, lab, c, "Courant estimate {:.2f} s (no cap)".format(courant_dt_s)
    cap_s, cap_lab = comp_interval(float(s["dt_s"]))
    if mode == "fixed":
        c["adaptive"] = False
        return cap_s, cap_lab, c, "fixed {} (NDBREACH_HECRAS_DT_MODE=fixed)".format(cap_lab)
    sec, lab = comp_interval(min(float(courant_dt_s), cap_s))
    how = "Courant estimate {:.2f} s, capped at {}".format(courant_dt_s, cap_lab)
    if c.get("adaptive"):
        k = 0
        while sec * 2 ** (k + 1) <= cap_s + 1e-9:
            k += 1
        asked = int(c.get("max_doubling") or 0)
        if asked > k:
            c["max_doubling"] = k
            how += "; adaptive step may double {} time(s) (was {}) so it stays at or below " \
                   "{}".format(k, asked, cap_lab)
    return sec, lab, c, how


def anuga_outline(model_dir, s):
    """The ANUGA mesh outline file to clip to, or None: anuga_mesh, else the run's own
    <model folder>/anuga/mesh_boundary.csv."""
    if s.get("anuga_mesh"):
        return s["anuga_mesh"]
    p = os.path.join(model_dir, ANUGA_MESH_CSV)
    return p if os.path.isfile(p) else None


def read_outline(path):
    """(n, 2) x, y of a mesh_boundary.csv (header 'x,y')."""
    import numpy as np
    a = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    if a.shape[0] < 3 or a.shape[1] < 2:
        raise SolverSettingError("{}: fewer than 3 outline points".format(path))
    return a[:, :2]


def wet_runs(reached, blocked, min_pts):
    """Index runs (lists, in ring order) of `reached` and not `blocked` points of a closed
    ring of n points, each at least min_pts long; at most MAX_OUTFLOW_LINES, longest first."""
    n = len(reached)
    good = [bool(reached[i]) and not bool(blocked[i]) for i in range(n)]
    if all(good):
        return [list(range(n))[:n - 1]]          # never the whole ring: leave one point
    if not any(good):
        return []
    start = next(i for i in range(n) if not good[i])
    runs, cur = [], []
    for k in range(1, n + 1):
        i = (start + k) % n
        if good[i]:
            cur.append(i)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    runs = [r for r in runs if len(r) >= max(2, int(min_pts))]
    runs.sort(key=len, reverse=True)
    return runs[:MAX_OUTFLOW_LINES]


def apply_courant(ts, s):
    """The adaptive time step dict `ts` (COURANT_DEFAULTS merged with the build option
    time_step) with the settings set by option or environment put over it."""
    out = dict(ts)
    for k, tk in COURANT_KEYS.items():
        if s["_source"].get(k) != "default":
            out[tk] = s[k]
    return out


def sine_progression(n=SINE_POINTS):
    """[(time fraction, breach fraction)] of HEC-RAS's sine-wave breach progression."""
    out = []
    for i in range(n):
        t = i / float(n - 1)
        out.append((round(t, 4), round(0.5 * (1.0 - math.cos(math.pi * t)), 4)))
    return out


def _cpulist(text):
    out = set()
    for part in str(text).strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def _read(path):
    with open(path, encoding="ascii") as f:
        return f.read().strip()


def physical_cores(sys_root="/sys/devices", logical=None):
    """(count, how) of the host's physical performance cores: hyper-thread siblings counted
    once and, on a hybrid CPU (Linux /sys/devices/cpu_core), only the P-cores. Linux /sys
    topology, else psutil's physical count, else half the logical CPUs."""
    cpu = os.path.join(sys_root, "system", "cpu")
    try:
        online = _cpulist(_read(os.path.join(cpu, "online")))
        how = "physical cores"
        pc = os.path.join(sys_root, "cpu_core", "cpus")
        if os.path.isfile(pc):
            online &= _cpulist(_read(pc))
            how = "physical P-cores (E-cores left out)"
        ids = set()
        for i in online:
            t = os.path.join(cpu, "cpu{}".format(i), "topology")
            ids.add((_read(os.path.join(t, "physical_package_id")),
                     _read(os.path.join(t, "core_id"))))
        if ids:
            return len(ids), how + " (Linux /sys topology)"
    except (OSError, ValueError):
        pass
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n), "physical cores (psutil; cannot tell E-cores apart)"
    except Exception:
        pass
    n = logical if logical is not None else (os.cpu_count() or 2)
    return max(1, int(n) // 2), "half the {} logical CPUs (hyper-threading assumed)".format(n)


def cores(n_cells, s, host=None):
    """(UNET D2 Cores value, how): 0 = all logical CPUs."""
    c = s["cores"]
    if c == "all":
        return 0, "all available (HEC-RAS default; NDBREACH_HECRAS_CORES=all)"
    if c != "auto":
        return int(c), "set ({}={})".format(ENV["cores"], c)
    phys, phow = host if host is not None else physical_cores()
    if n_cells < s["cores_small_cells"]:
        n = max(1, min(int(s["cores_small"]), phys))
        return n, "{:,} cells < {:,}: {} cores ({} {} on the build host)".format(
            n_cells, s["cores_small_cells"], n, phys, phow)
    return max(1, phys), "{:,} cells: all {} {} of the build host".format(n_cells, phys, phow)
