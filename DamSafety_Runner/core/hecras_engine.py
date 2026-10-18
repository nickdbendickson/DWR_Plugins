"""HEC-RAS 2D as a Runner engine (design: docs/HEC-RAS_2D_engine_plan.md, PR #68).

OFF BY DEFAULT. Nothing here runs unless the caller asks for it; the LISFLOOD-FP and ANUGA
paths never import this module.

    detect()          what is installed: the Wine prefix with Windows HEC-RAS 6.6, the
                      RasMapperLib driver programs, the official Linux RasUnsteady, h5py
    write_project()   hecras_model.build_model() + (default) the run's breach hydrograph as a
                      2D flow-hydrograph boundary at the run's inflow point (the same forcing
                      LISFLOOD-FP and ANUGA get), to the end of the simulation, with the
                      reservoir dry and no breach in the plan (see "Inflow modes" below)
    preprocess()      terrain HDF; Ras.exe -c pass 1 (the geometry HDF with the 2D mesh);
                      terrain association and 2D property tables (RasMapperLib drivers under
                      Wine); Ras.exe -c pass 2, stopped as soon as the .b##/.x##/.p##.tmp.hdf
                      run inputs exist. Each step's output is checked.
    compute()         the official Linux RasUnsteady on those inputs -> <name>.p01.hdf
    results()         the .p01.hdf maxima on the Runner grid as the workflow's final arrays
                      (SI: m, m/s, hours after breach initiation; NaN where dry)
    write_results()   those arrays under workflow.FINAL_NAMES (ft, ft/s, h) so ND Dam Hazard
                      reads the same files as after a LISFLOOD-FP or ANUGA run

First real dam (Absaraka DWR-2801, TIER 2, 2026-10-18, on c232f65 with the step order now in
preprocess() and Min Courant 0.45): 64,090 computation points (65,739 cells in the results),
100 ft cells, 40 h, ~27 min on 1 core. RasUnsteady: volume error 2.7 ac-ft (0.17 %), max cell
water-surface error 2.27 ft at Downstream cell 6495. That cell is an ordinary interior 100 x
100 ft cell (not a small or boundary cell): ~2,060 ft inside the perimeter, ~7,900 ft from the
inflow BC and ~37,000 ft from the normal-depth outflow, on a sharp terrain step (cell minimum
1020.2 ft, neighbours 1027-1028.6 ft), at the wetting front ~5.1 h in, max depth ~4 ft. Only 10
cells exceed 0.5 ft. No numerics were changed for it; finer cells or breaklines along such
steps are the usual HEC-RAS remedy if it matters.

Linux chain (proven on HEC's Muncie example, 2026-10-16, max WSE within 0.009 ft of HEC's):
Windows HEC-RAS 6.6 installed in a 64-bit Wine prefix with .NET 4.8; `Ras.exe /regserver`
and `RegAsm /codebase RasMapperLib.dll` done once; the VB6 OCXs from the installer
registered; the driver programs terrain.exe, mesh.exe and drv.exe (sources in
tools/hecras_wine/) built and copied INTO the HEC-RAS folder, next to its GDAL.

TERMS AND CONDITIONS (TCU): Ras.exe shows HEC's TCU dialog and Wine does not keep the
acceptance. This module never clicks it. Accepting it is the user's decision; a user who has
accepted it can point NDBREACH_HECRAS_TCU_HOOK at their own script (for example one that
clicks "I agree" with xdotool), which preprocess() starts next to Ras.exe and names in the
log. Without it, Ras.exe waits on the dialog and preprocess() stops at its timeout with a
message saying so.

Settings (environment variables, or the `env` dict of each function):
    NDBREACH_HECRAS_WINEPREFIX   the Wine prefix (required on Linux)
    NDBREACH_HECRAS_WINDOWS_DIR  HEC-RAS folder inside it (default <prefix>/drive_c/HEC/RAS66)
    NDBREACH_HECRAS_LINUX_DIR    the unzipped Linux_RAS_v66 folder (bin/, libs/)
    NDBREACH_HECRAS_DISPLAY      X display for Wine (default :77; an Xvfb is NOT started here)
    NDBREACH_HECRAS_TCU_HOOK     optional, see above
    NDBREACH_HECRAS_TIMEOUT_S    per-step timeout (default 1800)
    NDBREACH_HECRAS_INFLOW_MODE  'hydrograph' (default) or 'breach' (see write_project)
    NDBREACH_HECRAS_BASE_FLOW_CFS  base flow of the inflow hydrograph (default 0, see below)
    NDBREACH_HECRAS_TAIL         'recession' (default) or 'hold' (see inflow_series)

Inflow modes (write_project):
  hydrograph  the run's breach hydrograph is a 2D flow-hydrograph BC at the run's inflow point
              (the forcing LISFLOOD-FP and ANUGA get). The plan has NO breach and the
              reservoir storage area starts DRY (the lowest elevation of its storage curve)
              with no lateral inflow: the reservoir water is already in the hydrograph, so a
              full pool would only be a second copy of it (any base flow into it, or any
              downstream water above the crest, would spill over the weir and be counted
              twice). The dam stays as the weir; water can only reach the dry storage area if
              the 2D water surface rises above the dam crest.
  breach      build_model's own dam-breach model: the pool at the run's starting level, the
              breach on the SA/2D connection with the run's breach parameters and trigger
              (hecras_model.breach_block); no inflow BC.
"""
import json
import math
import os
import shutil
import subprocess
import sys
import time

import numpy as np

from . import hecras

ENV_PREFIX = "NDBREACH_HECRAS_WINEPREFIX"
ENV_WINDIR = "NDBREACH_HECRAS_WINDOWS_DIR"
ENV_LINUX = "NDBREACH_HECRAS_LINUX_DIR"
ENV_DISPLAY = "NDBREACH_HECRAS_DISPLAY"
ENV_TCU = "NDBREACH_HECRAS_TCU_HOOK"
ENV_TIMEOUT = "NDBREACH_HECRAS_TIMEOUT_S"
DRIVERS = ("terrain.exe", "mesh.exe", "drv.exe")
WINDOWS_DEFAULT_SUB = os.path.join("drive_c", "HEC", "RAS66")
INFLOW_BC_NAME = "Breach Inflow"
INFLOW_BC_HALF_FT = 150.0          # half-length of the inflow BC line across the flow
INFLOW_BC_SLOPE = 0.001            # energy slope HEC-RAS distributes the inflow with
ENV_MODE = "NDBREACH_HECRAS_INFLOW_MODE"
ENV_BASE = "NDBREACH_HECRAS_BASE_FLOW_CFS"
ENV_TAIL = "NDBREACH_HECRAS_TAIL"
INFLOW_MODES = ("hydrograph", "breach")
TAILS = ("recession", "hold")
HDF_NAME = "{name}.p01.hdf"


class HecRasEngineError(RuntimeError):
    """A clear, user-facing failure of the HEC-RAS 2D engine."""


def _env(env):
    return os.environ if env is None else env


def _timeout(env):
    try:
        return float(_env(env).get(ENV_TIMEOUT) or 1800)
    except ValueError:
        return 1800.0


# ============================================================================
# detect
# ============================================================================
def detect(env=None, platform=None, isfile=os.path.isfile, isdir=os.path.isdir,
           which=shutil.which):
    """What the HEC-RAS 2D engine can use here. Returns a dict:
    available (bool), platform, wine, prefix, windows_dir, ras_exe, drivers {name: path or ''},
    linux_dir, rasunsteady, h5py (bool), missing [text], reason (text; '' when available)."""
    e = _env(env)
    plat = platform or sys.platform
    out = {"platform": plat, "wine": "", "prefix": "", "windows_dir": "", "ras_exe": "",
           "drivers": {}, "linux_dir": "", "rasunsteady": "", "h5py": False, "missing": []}
    miss = out["missing"]
    try:
        import h5py  # noqa: F401
        out["h5py"] = True
    except ImportError:
        miss.append("h5py in this Python (pip install h5py) - needed to read the results")
    if plat.startswith("win"):
        miss.append("native Windows runs are not wired yet (planned); use the Linux chain "
                    "or the HEC-RAS export tool")
    elif plat == "darwin":
        miss.append("HEC-RAS has no macOS build; use the HEC-RAS export tool")
    else:
        out["wine"] = which("wine") or ""
        if not out["wine"]:
            miss.append("wine (64-bit) on PATH")
        prefix = (e.get(ENV_PREFIX) or "").strip()
        if not prefix or not isdir(prefix):
            miss.append("{} = a Wine prefix with Windows HEC-RAS 6.6".format(ENV_PREFIX))
        else:
            out["prefix"] = prefix
        wdir = (e.get(ENV_WINDIR) or "").strip() or (
            os.path.join(prefix, WINDOWS_DEFAULT_SUB) if prefix else "")
        if wdir and isfile(os.path.join(wdir, "Ras.exe")):
            out["windows_dir"] = wdir
            out["ras_exe"] = os.path.join(wdir, "Ras.exe")
        else:
            miss.append("Ras.exe in {} ({})".format(wdir or "<prefix>/" + WINDOWS_DEFAULT_SUB,
                                                    ENV_WINDIR))
        for d in DRIVERS:
            p = os.path.join(wdir, d) if wdir else ""
            out["drivers"][d] = p if (p and isfile(p)) else ""
            if not out["drivers"][d]:
                miss.append("{} in the HEC-RAS folder (build it from tools/hecras_wine/)".format(d))
        ldir = (e.get(ENV_LINUX) or "").strip()
        exe = os.path.join(ldir, "bin", "RasUnsteady") if ldir else ""
        if exe and isfile(exe):
            out["linux_dir"], out["rasunsteady"] = ldir, exe
        else:
            miss.append("{} = the unzipped HEC-RAS 6.6 Linux folder (bin/RasUnsteady)"
                        .format(ENV_LINUX))
    out["available"] = not miss
    out["reason"] = "" if not miss else "HEC-RAS 2D is not available: " + "; ".join(miss) + "."
    return out


def require(env=None, **kw):
    d = detect(env, **kw)
    if not d["available"]:
        raise HecRasEngineError(d["reason"] + " Nothing was run.")
    return d


# ============================================================================
# project writer
# ============================================================================
def inflow_bc_line(xy, direction, half=INFLOW_BC_HALF_FT):
    """A short straight BC line through `xy` across the flow `direction` (any length)."""
    d = np.asarray(direction, dtype=float)
    n = float(np.hypot(*d))
    if not n:
        raise HecRasEngineError("no flow direction at the inflow point")
    px, py = -d[1] / n, d[0] / n
    x, y = float(xy[0]), float(xy[1])
    return [(x - px * half, y - py * half), (x + px * half, y + py * half)]


def inflow_bc_geometry_lines(line, area, num, lines_xy, name32, name16):
    """The geometry (.g01) lines of the inflow BC line, in build_model's own format."""
    mid = ((line[0][0] + line[-1][0]) / 2.0, (line[0][1] + line[-1][1]) / 2.0)
    return (["BC Line Name=" + name32(INFLOW_BC_NAME), "BC Line Storage Area=" + name16(area),
             "BC Line Start Position= {} , {} ".format(num(line[0][0], 4), num(line[0][1], 4)),
             "BC Line Middle Position= {} , {} ".format(num(mid[0], 4), num(mid[1], 4)),
             "BC Line End Position= {} , {} ".format(num(line[-1][0], 4), num(line[-1][1], 4)),
             "BC Line Arc= {} ".format(len(line))] + lines_xy(line, 2) +
            ["BC Line Text Position= {} , {} ".format(num(mid[0], 4), num(mid[1], 4))])


def add_inflow_to_geometry(text, bc_lines, cr="\r\n"):
    """Inserts the BC line block before the geometry's closing 'LCMann Time=' lines."""
    key = cr + "LCMann Time="
    i = text.find(key)
    if i < 0:
        raise HecRasEngineError("geometry text has no 'LCMann Time=' line")
    return text[:i] + cr + cr.join(bc_lines) + text[i:]


def inflow_unsteady_lines(area, interval, values, lines8, name16, name32,
                          slope=INFLOW_BC_SLOPE):
    blank16, blank8 = " " * 16, " " * 8
    return (["Boundary Location={},{},{},{},{},{},{},{}".format(
        blank16, blank16, blank8, blank8, blank16, name16(area), blank16, name32(INFLOW_BC_NAME)),
        "Interval=" + interval, "Flow Hydrograph= {} ".format(len(values))] + lines8(values) +
        ["Flow Hydrograph Slope= {} ".format(slope), "DSS Path=", "Use DSS=False",
         "Use Fixed Start Time=False", "Fixed Start Date/Time=,", "Is Critical Boundary=False",
         "Critical Boundary Flow="])


def add_inflow_to_unsteady(text, bc_lines, hold_wse=None, cr="\r\n"):
    """Appends the flow-hydrograph BC; with `hold_wse`, sets the reservoir's initial level
    to it (the weir crest) so the storage area adds nothing and the hydrograph is the only
    breach flow."""
    lines = text.split(cr)
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if hold_wse is not None:
        for k, l in enumerate(lines):
            if l.startswith("Initial Storage Elev="):
                name = l.split("=", 1)[1].split(",")[0]
                lines[k] = "Initial Storage Elev={},{}".format(name, _num3(hold_wse))
    i = next((k for k, l in enumerate(lines) if l.startswith("Met Point Raster Parameters")
              or l.startswith("Precipitation Mode")), len(lines))
    return cr.join(lines[:i] + bc_lines + lines[i:]) + cr


def _num3(v):
    s = "{:.3f}".format(float(v)).rstrip("0").rstrip(".")
    return s or "0"


def drop_breach_from_plan(text, cr="\r\n"):
    """Removes the 'Breach Loc=' ... block so the dam does not breach in the hydrograph mode
    (the breach is in the inflow hydrograph)."""
    lines = text.split(cr)
    i = next((k for k, l in enumerate(lines) if l.startswith("Breach Loc=")), None)
    if i is None:
        return text
    return cr.join(lines[:i]) + cr


def hydrograph_series(hy, units_flow, dt_min=hecras.DEFAULT_STEP_MIN):
    """(interval label, values in project flow units) of the Runner's breach hydrograph,
    on the record only (no extension; see inflow_series)."""
    from .units import CFS
    _t_hr, q_cfs, _info = hecras.resample_hydrograph(hy, dt_min)
    return hecras.ras_interval_label(dt_min), [round(units_flow(float(v) * CFS), 2)
                                               for v in q_cfs]


class _Record:
    def __init__(self, t_s, q_cms, t_ref_s=0.0):
        self.t_s, self.q_cms, self.t_ref_s = t_s, q_cms, t_ref_s


def extended_record(hy, sim_hours, base_flow_cfs=0.0, tail="recession"):
    """The breach hydrograph from time 0 to the end of the simulation (sim_hours), never
    stepping to zero. The record's trailing zero (the Runner ends its hydrograph when the
    reservoir is empty: Absaraka 250 -> 0 cfs at 2.47 h) is dropped and, after the last
    positive value q_last at t_last:
      'recession' (default)  q_last decays to the base flow at the record's own recession
                             rate (k from the falling limb: q halves as it did before t_last),
                             then the base flow is held; adds little volume.
      'hold'                 the base flow is held, or q_last when no base flow is set (adds
                             q_last x the remaining time: Absaraka ~300 cfs x 37.5 h = ~930
                             ac-ft, more than half the breach volume - use it knowingly).
    Every value is at least the base flow. Returns (record, info)."""
    from .units import CFS
    if tail not in TAILS:
        raise HecRasEngineError("tail must be one of " + ", ".join(TAILS))
    base = max(float(base_flow_cfs or 0.0), 0.0) * CFS
    t = np.asarray(hy.t_s, dtype=float)
    q = np.clip(np.asarray(hy.q_cms, dtype=float), 0.0, None)
    end_s = float(sim_hours) * 3600.0
    pos = np.nonzero(q > 0)[0]
    if not len(pos):
        raise HecRasEngineError("the breach hydrograph has no flow")
    il = int(pos[-1])
    t_last, q_last = float(t[il]), float(q[il])
    t, q = t[:il + 1], q[:il + 1]
    ip = int(np.argmax(q))
    k = None
    if tail == "recession" and il > ip:
        # the recession rate over the last halving of the falling limb
        j = ip
        for jj in range(il - 1, ip - 1, -1):
            if q[jj] >= 2.0 * q_last:
                j = jj
                break
        if t_last > t[j] and q[j] > q_last:
            k = math.log(q[j] / q_last) / (t_last - t[j])
    if tail == "recession" and not (k and k > 0):
        k = math.log(2.0) / max(t_last - float(t[ip]), 600.0)
    hold = base if (tail == "recession" or base > 0) else q_last
    if end_s > t_last:
        dt = 60.0
        tt = t_last + np.arange(1, int(math.ceil((end_s - t_last) / dt)) + 1) * dt
        tt[-1] = max(tt[-1], end_s)
        if tail == "recession" and q_last > hold:
            qq = hold + (q_last - hold) * np.exp(-k * (tt - t_last))
        else:
            qq = np.full(len(tt), hold)
        t, q = np.concatenate([t, tt]), np.concatenate([q, qq])
    q = np.maximum(q, base)
    rec = _Record(t, q, float(getattr(hy, "t_ref_s", 0.0) or 0.0))
    vol = (lambda tt_, qq_: float(np.trapezoid(qq_, tt_)) if hasattr(np, "trapezoid")
           else float(np.trapz(qq_, tt_)))
    v0 = vol(np.asarray(hy.t_s, float), np.clip(np.asarray(hy.q_cms, float), 0, None))
    from .units import ACFT
    info = {"tail": tail, "base_flow_cfs": base / CFS, "record_end_hr": t_last / 3600.0,
            "last_flow_cfs": q_last / CFS, "held_cfs": hold / CFS,
            "recession_per_hr": (k * 3600.0 if (tail == "recession" and k) else None),
            "volume_record_acft": v0 / ACFT, "volume_extended_acft": vol(t, q) / ACFT}
    return rec, info


def inflow_series(hy, units_flow, sim_hours, dt_min=hecras.DEFAULT_STEP_MIN, base_flow_cfs=0.0,
                  tail="recession"):
    """(interval label, values in project flow units, info): the breach hydrograph on the
    HEC-RAS data interval from the simulation start to its end - exactly
    sim_hours * 60 / dt_min + 1 values - with the tail of extended_record()."""
    from .units import ACFT, CFS
    lab = hecras.ras_interval_label(dt_min)
    if lab is None:
        raise HecRasEngineError("{:g} min is not a HEC-RAS data interval".format(dt_min))
    rec, info = extended_record(hy, sim_hours, base_flow_cfs, tail)
    _t_hr, q_cfs, rinfo = hecras.resample_hydrograph(rec, dt_min)
    n = int(round(float(sim_hours) * 60.0 / float(dt_min))) + 1
    q_cfs = np.asarray(q_cfs, float)[:n]
    if len(q_cfs) < n:
        q_cfs = np.concatenate([q_cfs, np.full(n - len(q_cfs), q_cfs[-1])])
    info.update({"interval": lab, "n": n, "dt_min": float(dt_min),
                 "sim_hours": float(sim_hours), "end_hr": (n - 1) * float(dt_min) / 60.0,
                 "volume_exported_acft": rinfo["volume_exported_acft"],
                 "first_cfs": float(q_cfs[0]), "final_cfs": float(q_cfs[-1])})
    return lab, [round(units_flow(float(v) * CFS), 2) for v in q_cfs], info


def _mode(inflow_mode, env):
    m = (inflow_mode or (_env(env).get(ENV_MODE) or "").strip().lower() or "hydrograph")
    if m not in INFLOW_MODES:
        raise HecRasEngineError("inflow_mode must be 'hydrograph' or 'breach' (got {!r}; "
                                "{})".format(m, ENV_MODE))
    return m


def _float_env(env, key, default):
    v = (_env(env).get(key) or "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        raise HecRasEngineError("{}={!r} is not a number".format(key, v))


def write_project(dam_folder, out_folder=None, options=None, inflow_mode=None,
                  build=None, load=None, log=None, env=None, base_flow_cfs=None, tail=None):
    """Builds the HEC-RAS project of a finished run (hecras_model.build_model). inflow_mode
    (default NDBREACH_HECRAS_INFLOW_MODE, else 'hydrograph'): see the module docstring.
    'hydrograph' forces the 2D area with the run's breach hydrograph at the run's inflow
    point from the simulation start to its end (inflow_series), with a dry reservoir and no
    breach; 'breach' keeps build_model's storage area + SA/2D breach unchanged. Returns
    build_model's dict plus 'inflow_mode' and 'inflow_bc'."""
    from . import hecras_model as hm
    import copy
    build = build or hm.build_model
    load = load or hecras.load_run
    log = log or (lambda m: None)
    inflow_mode = _mode(inflow_mode, env)
    if base_flow_cfs is None:
        base_flow_cfs = _float_env(env, ENV_BASE, None)
    if base_flow_cfs is None:
        base_flow_cfs = float((options or {}).get("base_flow_cfs") or 0.0)
    tail = (tail or (_env(env).get(ENV_TAIL) or "").strip().lower() or "recession")
    if tail not in TAILS:
        raise HecRasEngineError("tail must be 'recession' or 'hold' (got {!r}; {})".format(
            tail, ENV_TAIL))
    res = build(dam_folder, out_folder, options=options, log=log)
    res["inflow_mode"] = inflow_mode
    res["inflow_bc"] = None
    if inflow_mode == "breach":
        b = res["model"].breach or {}
        log("HEC-RAS inflow: dam breach in the plan (SA/2D connection, trigger water surface "
            "{}); the reservoir starts at {}.".format(b.get("trigger_wse"),
                                                       res["model"].initial_wse))
        _write_engine_json(res, inflow_mode, None, None)
        return res
    m, info, paths = res["model"], res["info"], res["paths"]
    xy = info.get("inflow_xy_model")
    if xy is None:
        raise HecRasEngineError("the run has no inflow point (run_report.json 'inflow_point'); "
                                "use inflow_mode='breach'")
    a, b = np.asarray(m.conn_line[0], float), np.asarray(m.conn_line[-1], float)
    along = b - a
    direction = np.asarray(xy, float) - (a + b) / 2.0       # dam -> inflow, downstream
    if float(np.hypot(*direction)) < 1e-6:
        direction = np.array([-along[1], along[0]])
    line = inflow_bc_line(xy, direction)
    hy = load(dam_folder)[0]
    interval, vals, sinfo = inflow_series(hy, m.units.flow, m.sim_hours,
                                          base_flow_cfs=base_flow_cfs, tail=tail)
    with open(paths["g01"], encoding="ascii", newline="") as f:
        g = f.read()
    g = add_inflow_to_geometry(g, inflow_bc_geometry_lines(line, hm.AREA_NAME, hm.num,
                                                           hm.lines_xy, hm.name32, hm.name16))
    # the reservoir: dry, no lateral inflow (its water is in the hydrograph)
    dry = copy.copy(m)
    dry_wse = (min(e for e, _ in m.elev_volume) if m.elev_volume else
               (min(e for _, e in m.weir) if m.weir else m.initial_wse))
    dry.initial_wse = dry_wse
    dry.inflow, dry.base_flow = None, 0.0
    u = hm.unsteady_text(dry)
    u = add_inflow_to_unsteady(u, inflow_unsteady_lines(hm.AREA_NAME, interval, vals, hm.lines8,
                                                        hm.name16, hm.name32))
    with open(paths["p01"], encoding="ascii", newline="") as f:
        p = f.read()
    p = drop_breach_from_plan(p)
    for k, t in (("g01", g), ("u01", u), ("p01", p)):
        with open(paths[k], "w", encoding="ascii", newline="") as f:
            f.write(t)
    res["inflow_bc"] = {"line": [list(map(float, q)) for q in line], "interval": interval,
                        "n": len(vals), "peak": max(vals) if vals else 0.0,
                        "t_ref_hr": float(getattr(hy, "t_ref_s", 0.0) or 0.0) / 3600.0,
                        "reservoir_initial_wse": dry_wse, "series": sinfo}
    _write_engine_json(res, inflow_mode, res["inflow_bc"], info)
    log("HEC-RAS inflow: breach hydrograph ({} values at {}, 0 to {:g} h; after the record "
        "({:.2f} h) '{}' tail to {:.1f} cfs; volume {:.1f} ac-ft vs the record's {:.1f}) on a "
        "BC line at the run's inflow point; the reservoir starts dry ({:.2f}) and the plan has "
        "no breach.".format(len(vals), interval, sinfo["end_hr"], sinfo["record_end_hr"], tail,
                            sinfo["final_cfs"], sinfo["volume_exported_acft"],
                            sinfo["volume_record_acft"], dry_wse))
    return res


def _write_engine_json(res, inflow_mode, inflow_bc, info):
    info = info or res.get("info") or {}
    with open(os.path.join(res["folder"], "hecras_engine.json"), "w", encoding="utf-8") as f:
        json.dump({"inflow_mode": inflow_mode, "inflow_bc": inflow_bc,
                   "time_step": info.get("time_step"), "sim_hours": info.get("sim_hours"),
                   "model_wkt": info.get("model_wkt"), "work_wkt": info.get("work_wkt")},
                  f, indent=1)


# ============================================================================
# preprocess / compute (Linux: Wine + official Linux RasUnsteady)
# ============================================================================
def to_wine_path(path, prefix):
    """C:\\... for a path inside <prefix>/drive_c, else Z:\\... (Wine's root drive)."""
    p = os.path.abspath(path)
    dc = os.path.join(os.path.abspath(prefix), "drive_c")
    if p == dc or p.startswith(dc + os.sep):
        return "C:\\" + os.path.relpath(p, dc).replace("/", "\\").lstrip(".\\") \
            if p != dc else "C:\\"
    return "Z:" + p.replace("/", "\\")


def wine_env(d, env=None):
    e = dict(_env(env))
    for k in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA", "GDAL_DRIVER_PATH", "PYTHONPATH",
              "PYTHONHOME", "LD_LIBRARY_PATH"):
        e.pop(k, None)
    w = to_wine_path(d["windows_dir"], d["prefix"])
    e.update({"WINEPREFIX": d["prefix"], "WINEDEBUG": "-all",
              "DISPLAY": _env(env).get(ENV_DISPLAY) or ":77",
              "WINEPATH": "{0}\\GDAL\\bin64;{0}\\x64".format(w),
              "GDAL_DATA": "{}\\GDAL\\common\\data".format(w)})
    return e


def _run(cmd, cwd, env, timeout, what, ok=None, log=None):
    log = log or (lambda m: None)
    log("HEC-RAS: " + what)
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, capture_output=True,
                           text=True, errors="replace")
    except subprocess.TimeoutExpired:
        raise HecRasEngineError("{} did not finish in {:.0f} s".format(what, timeout))
    except OSError as e:
        raise HecRasEngineError("{} could not start: {}".format(what, e))
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0 or (ok and ok not in text) or "FAIL" in text:
        tail = "\n".join(text.strip().splitlines()[-8:])
        why = ("exit {}".format(r.returncode) if r.returncode != 0 else
               "exit 0 but it printed FAIL" if "FAIL" in text else
               "exit 0 but it never printed '{}'".format(ok))
        raise HecRasEngineError("{} failed ({}):\n{}".format(what, why, tail))
    return text


def write_rasmap_terrain(rasmap_path, terrain_rel="Terrain\\Terrain.hdf", name="Terrain"):
    """Registers the terrain HDF in the project's .rasmap (replaces '<Terrains />')."""
    with open(rasmap_path, encoding="utf-8", newline="") as f:
        t = f.read()
    layer = ('<Terrains Checked="True" Expanded="True">\r\n    <Layer Name="{}" Type="TerrainLayer" '
             'Checked="True" Filename=".\\{}" />\r\n  </Terrains>').format(name, terrain_rel)
    if "<Terrains />" in t:
        t = t.replace("<Terrains />", layer)
    elif "TerrainLayer" not in t:
        raise HecRasEngineError("the .rasmap has no <Terrains /> element")
    with open(rasmap_path, "w", encoding="utf-8", newline="") as f:
        f.write(t)


# ---------------------------------------------------------------------------- output checks
PROPS_DATASETS = ("Cells Volume Elevation Values", "Faces Area Elevation Values")


def geometry_hdf_state(path):
    """What a geometry HDF (<name>.g##.hdf) holds, read-only: {'exists', 'size', 'cells'
    (sum of 'Cell Count' over the 2D areas), 'areas' {name: {'cells', 'props'}}, 'terrain'
    (the associated terrain file, '' if none), 'props' (every 2D area has non-empty
    Cells Volume Elevation / Faces Area Elevation tables), 'error' ('' or why it could not
    be read)}. Never raises."""
    out = {"exists": os.path.isfile(path), "size": 0, "cells": 0, "areas": {}, "terrain": "",
           "props": False, "error": ""}
    if not out["exists"]:
        return out
    out["size"] = os.path.getsize(path)
    if not out["size"]:
        out["error"] = "empty file"
        return out
    try:
        import h5py
    except ImportError:
        out["error"] = "h5py is not installed"
        return out
    try:
        try:
            f = h5py.File(path, "r", locking=False)
        except TypeError:                                  # h5py < 3.5
            f = h5py.File(path, "r")
        with f:
            geo = f.get("Geometry")
            if geo is None:
                out["error"] = "no Geometry group"
                return out
            t = geo.attrs.get("Terrain Filename", b"")
            out["terrain"] = (t.decode("utf-8", "replace") if isinstance(t, bytes) else str(t)).strip()
            areas = geo.get("2D Flow Areas")
            if areas is None or "Attributes" not in areas:
                return out
            att = areas["Attributes"][()]
            names = att.dtype.names or ()
            for row in np.atleast_1d(att):
                nm = row["Name"] if "Name" in names else b""
                nm = (nm.decode("utf-8", "replace") if isinstance(nm, bytes) else str(nm)).strip()
                n = int(row["Cell Count"]) if "Cell Count" in names else 0
                g = areas.get(nm)
                props = bool(g is not None and all(
                    k in g and int(np.prod(g[k].shape)) > 0 for k in PROPS_DATASETS))
                out["areas"][nm] = {"cells": n, "props": props}
            out["cells"] = sum(a["cells"] for a in out["areas"].values())
            out["props"] = bool(out["areas"]) and all(a["props"] for a in out["areas"].values())
    except Exception as e:                                 # a file still being written
        out["error"] = "{}: {}".format(type(e).__name__, e)
    return out


def data_errors(project_dir, name, plan):
    """The text of HEC-RAS's '<name>.p<plan>.data_errors.txt' ('' when there is none)."""
    p = os.path.join(project_dir, "{}.p{}.data_errors.txt".format(name, plan))
    if not os.path.isfile(p):
        return ""
    try:
        with open(p, encoding="latin-1") as f:
            return f.read().strip()
    except OSError:
        return ""


def _require_geometry(ghdf, step, need_cells=True, need_terrain=False, need_props=False,
                      project_dir=None, name=None, plan=None):
    """Raises HecRasEngineError when `step` (exit code 0 or not) left the geometry HDF without
    what the next step needs. Returns the geometry_hdf_state()."""
    st = geometry_hdf_state(ghdf)
    base = os.path.basename(ghdf)
    why = None
    if not st["exists"] or not st["size"]:
        why = "{} was not written".format(base)
    elif st["error"]:
        why = "{} could not be read ({})".format(base, st["error"])
    elif need_cells and st["cells"] <= 0:
        why = "{} has no 2D cells (no mesh)".format(base)
    elif need_terrain and not st["terrain"]:
        why = "{} has no terrain associated".format(base)
    elif need_props and not st["props"]:
        bad = [k for k, a in st["areas"].items() if not a["props"]] or ["the 2D areas"]
        why = "{} has no 2D property tables for {}".format(base, ", ".join(bad))
    if why:
        de = data_errors(project_dir, name, plan) if project_dir else ""
        raise HecRasEngineError("HEC-RAS {}: {} although the step reported success.{}".format(
            step, why, (" HEC-RAS data errors ({}.p{}.data_errors.txt): {}".format(
                name, plan, de[:600]) if de else "")))
    return st


# ---------------------------------------------------------------------------- preprocess
STEPS = ("terrain", "geometry", "assoc", "props", "ras")


def preprocess(project_dir, name, plan="01", geom="01", d=None, env=None, steps=None,
               terrain_tif=None, prj=None, mesh=None, log=None, poll_s=2.0):
    """Makes the run inputs <name>.p<plan>.tmp.hdf, .b<plan>, .x<plan> in `project_dir`
    (which must be inside the Wine prefix's drive_c, as the chain was proven there).

    Default steps, in order (the order proven on Absaraka, 2026-10-18, for a project
    build_model wrote, which has no geometry HDF yet):
      'terrain'   terrain.exe from terrain_tif + prj into Terrain/Terrain.hdf, registered in
                  the .rasmap
      'geometry'  Ras.exe -c (pass 1), stopped as soon as <name>.g<geom>.hdf holds the 2D
                  mesh (Absaraka: 65,739 cells, ~14 s); skipped when that HDF already has
                  cells (e.g. HEC's own examples)
      'assoc'     terrain association (drv.exe assoc = SetGeometryAssociation; ~4 s)
      'props'     2D property tables (drv.exe props; ~22 s)
      'ras'       Ras.exe -c (pass 2), stopped once the run inputs exist (~39 s)
    Optional: 'complete' (drv.exe complete = CompleteGeometryCommand: completes an EXISTING
    geometry HDF from the text; it prints OK but writes nothing when there is none) and
    'mesh' (mesh.exe; `mesh` = (area name, seeds csv path), on an existing geometry HDF).
    After every step the expected output is checked (geometry HDF with 2D cells > 0, terrain
    associated, property tables written, run inputs present) and a missing one fails with a
    clear message even when the program exited 0. Returns {step: seconds}."""
    d = d or require(env)
    e = wine_env(d, env)
    to = _timeout(env)
    W = lambda p: to_wine_path(p, d["prefix"])
    wdir = d["windows_dir"]
    nice = ["nice", "-n", "19"] if shutil.which("nice") else []
    wine = nice + [d["wine"] or "wine"]
    steps = list(steps or STEPS)
    ghdf = os.path.join(project_dir, "{}.g{}.hdf".format(name, geom))
    terrain_hdf = os.path.join(project_dir, "Terrain", "Terrain.hdf")
    chk = dict(project_dir=project_dir, name=name, plan=plan)
    times = {}
    for s in steps:
        t0 = time.time()
        if s == "terrain":
            if not (terrain_tif and prj):
                raise HecRasEngineError("terrain step needs terrain_tif and prj")
            os.makedirs(os.path.dirname(terrain_hdf), exist_ok=True)
            _run(wine + [os.path.join(wdir, "terrain.exe"), W(wdir), W(prj), W(terrain_tif),
                         W(os.path.dirname(terrain_hdf)), "Terrain"], wdir, e, to,
                 "terrain HDF (RasMapperLib, Wine)", ok="OK", log=log)
            if not (os.path.isfile(terrain_hdf) and os.path.getsize(terrain_hdf) > 0):
                raise HecRasEngineError("HEC-RAS terrain HDF: {} was not written although "
                                        "terrain.exe reported OK.".format(terrain_hdf))
            rm = os.path.join(project_dir, name + ".rasmap")
            if os.path.exists(rm):
                write_rasmap_terrain(rm)
        elif s == "geometry":
            st = geometry_hdf_state(ghdf)
            if st["cells"] > 0 and not st["error"]:
                (log or (lambda m: None))("HEC-RAS: {} already has the mesh ({:,} cells); "
                                          "Ras.exe pass 1 skipped".format(
                                              os.path.basename(ghdf), st["cells"]))
            else:
                ras_compile(project_dir, name, plan, d, e, to, log, poll_s,
                            until=geometry_ready(ghdf), what="geometry HDF with the 2D mesh")
            st = _require_geometry(ghdf, "geometry (Ras.exe -c pass 1)", **chk)
            (log or (lambda m: None))("HEC-RAS: geometry HDF with {:,} 2D cells".format(
                st["cells"]))
        elif s == "mesh":
            area, seeds = mesh
            _run(wine + [os.path.join(wdir, "mesh.exe"), W(ghdf), area, "csv", W(seeds)], wdir, e,
                 to, "2D mesh (RasMapperLib, Wine)", log=log)
            _require_geometry(ghdf, "2D mesh (mesh.exe)", **chk)
        elif s == "complete":
            if not os.path.isfile(ghdf):
                raise HecRasEngineError(
                    "drv.exe complete needs an existing {} (it reports OK but writes nothing "
                    "without one); use the 'geometry' step (Ras.exe -c) on a new project."
                    .format(os.path.basename(ghdf)))
            _run(wine + [os.path.join(wdir, "drv.exe"), W(wdir), "complete", W(ghdf),
                         W(os.path.join(project_dir, name + ".rasmap"))], wdir, e, to,
                 "geometry HDF from the text geometry (RasMapperLib, Wine)", ok="OK", log=log)
            _require_geometry(ghdf, "geometry completion (drv.exe complete)", **chk)
        elif s == "assoc":
            _require_geometry(ghdf, "terrain association: input", **chk)
            _run(wine + [os.path.join(wdir, "drv.exe"), W(wdir), "assoc", W(ghdf), W(terrain_hdf)],
                 wdir, e, to, "terrain association (RasMapperLib, Wine)", ok="OK", log=log)
            _require_geometry(ghdf, "terrain association (drv.exe assoc)", need_terrain=True,
                              **chk)
        elif s == "props":
            _require_geometry(ghdf, "2D property tables: input", **chk)
            _run(wine + [os.path.join(wdir, "drv.exe"), W(wdir), "props", W(ghdf)], wdir, e, to,
                 "2D property tables (RasMapperLib, Wine)", ok="tables complete", log=log)
            _require_geometry(ghdf, "2D property tables (drv.exe props)", need_props=True, **chk)
        elif s == "ras":
            ras_compile(project_dir, name, plan, d, e, to, log, poll_s)
        else:
            raise HecRasEngineError("unknown preprocess step " + repr(s))
        times[s] = round(time.time() - t0, 1)
    return times


def run_inputs(project_dir, name, plan):
    return [os.path.join(project_dir, "{}.{}".format(name, x)) for x in
            ("b" + plan, "x" + plan, "p{}.tmp.hdf".format(plan))]


def geometry_ready(ghdf):
    """A poll function: true once the geometry HDF has 2D cells and its size did not change
    since the previous poll (Ras.exe may still be writing it)."""
    last = [None]

    def ready():
        st = geometry_hdf_state(ghdf)
        prev, last[0] = last[0], st["size"]
        return st["cells"] > 0 and not st["error"] and prev == st["size"]
    return ready


def ras_compile(project_dir, name, plan, d, e, timeout, log=None, poll_s=2.0, until=None,
                what=None):
    """Ras.exe -c through a .bat (Wine mangles quoted arguments otherwise); stopped
    (wineserver -k) as soon as `until()` is true - by default: the run inputs exist and
    RasUnsteady.exe has started. A HEC-RAS data-errors file is reported in the error."""
    log = log or (lambda m: None)
    inputs_ok = (lambda: all(os.path.exists(p) and os.path.getsize(p) > 0
                             for p in run_inputs(project_dir, name, plan)))
    after_exit = until
    if until is None:
        for p in run_inputs(project_dir, name, plan):
            if os.path.exists(p):
                os.remove(p)
        until = (lambda: inputs_ok() and _rasunsteady_running())
        after_exit = inputs_ok
        what = what or "run inputs ({})".format(", ".join(
            os.path.basename(p) for p in run_inputs(project_dir, name, plan)))
        final = True
    else:
        final = False
    de_path = os.path.join(project_dir, "{}.p{}.data_errors.txt".format(name, plan))
    if os.path.exists(de_path):
        os.remove(de_path)
    bat = os.path.join(project_dir, "_ndbreach_ras_c.bat")
    with open(bat, "w", newline="") as f:
        f.write('"{}" -c "{}" "{}"\r\n'.format(
            to_wine_path(d["ras_exe"], d["prefix"]),
            to_wine_path(os.path.join(project_dir, name + ".prj"), d["prefix"]),
            to_wine_path(os.path.join(project_dir, "{}.p{}".format(name, plan)), d["prefix"])))
    hook = None
    tcu = (e.get(ENV_TCU) or "").strip()
    log("HEC-RAS: Ras.exe -c ({})".format("geometry and plan compile" if final else
                                          "pass 1: " + what))
    proc = subprocess.Popen([d["wine"] or "wine", "cmd", "/c", to_wine_path(bat, d["prefix"])],
                            cwd=project_dir, env=e, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    t0, done = time.time(), False
    try:
        if tcu:
            log("HEC-RAS: starting the user's TCU hook " + tcu)
            cmd = ["/bin/bash", tcu] if tcu.endswith(".sh") else [tcu]
            try:
                hook = subprocess.Popen(cmd, env=e, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            except OSError as x:
                raise HecRasEngineError("the TCU hook {} could not start: {}".format(tcu, x))
        while time.time() - t0 < timeout:
            time.sleep(poll_s)
            if until():
                time.sleep(2.0 if final else 0.0)
                done = True
                break
            if proc.poll() is not None:
                done = bool(after_exit())
                break
    finally:
        subprocess.run(["wineserver", "-k"], env=e, timeout=60, capture_output=True)
        for p in (proc, hook):
            if p is not None and p.poll() is None:
                p.kill()
    de = data_errors(project_dir, name, plan)
    if final and de:
        raise HecRasEngineError("Ras.exe -c reported HEC-RAS data errors ({}): {}".format(
            os.path.basename(de_path), de[:800]))
    if not done:
        if de:
            raise HecRasEngineError("Ras.exe -c stopped on HEC-RAS data errors ({}): {}".format(
                os.path.basename(de_path), de[:800]))
        raise HecRasEngineError(
            "Ras.exe -c wrote no {} within {:.0f} s. If HEC's Terms and Conditions dialog is "
            "waiting, it must be accepted by the user (see {}).".format(what, timeout, ENV_TCU))


def _rasunsteady_running():
    try:
        r = subprocess.run(["pgrep", "-fi", "rasunsteady.exe"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return True


def strip_cr(path):
    with open(path, "rb") as f:
        b = f.read()
    with open(path, "wb") as f:
        f.write(b.replace(b"\r\n", b"\n"))


def compute(project_dir, name, plan="01", d=None, env=None, work_dir=None, log=None):
    """Official Linux RasUnsteady on the preprocessed inputs; returns the results HDF path
    (<name>.p<plan>.hdf in work_dir, default <project>/linux_run)."""
    d = d or require(env)
    log = log or (lambda m: None)
    work = work_dir or os.path.join(project_dir, "linux_run")
    os.makedirs(work, exist_ok=True)
    ins = run_inputs(project_dir, name, plan)
    for p in ins:
        if not os.path.exists(p):
            raise HecRasEngineError("missing run input {} (run preprocess first)".format(p))
        shutil.copy2(p, work)
    for p in ins[:2]:
        strip_cr(os.path.join(work, os.path.basename(p)))
    L = d["linux_dir"]
    e = dict(_env(env))
    e["LD_LIBRARY_PATH"] = ":".join(os.path.join(L, s) for s in ("libs", "libs/mkl", "libs/rhel_8"))
    tmp = os.path.basename(ins[2])
    nice = ["nice", "-n", "19"] if shutil.which("nice") else []
    text = _run(nice + [d["rasunsteady"], tmp, "x" + plan], work, e, _timeout(env),
                "Linux RasUnsteady", ok="Finished Unsteady Flow Simulation", log=log)
    with open(os.path.join(work, "run.log"), "w") as f:
        f.write(text)
    out = os.path.join(work, "{}.p{}.hdf".format(name, plan))
    os.replace(os.path.join(work, tmp), out)
    return out


# ============================================================================
# results -> the Runner grid
# ============================================================================
def results(hdf, like_grid, arrival_m=0.3048, t0_hr=0.0, to_grid_xy=None, area=None,
            units=None, wet_m=0.003):
    """The HEC-RAS maxima on the Runner grid as the workflow's final arrays, SI:
    {'depth' m, 'velocity' m/s, 'wse' m, 'arrival' h after breach initiation}, NaN where
    dry or outside the mesh. to_grid_xy(xy (n,2)) -> (n,2) maps the HEC-RAS model CRS to the
    grid's (None when they are the same)."""
    FT = 0.3048
    mesh = hecras.read_mesh(hdf, area=area, units=units, arrival_depth_ft=arrival_m / FT,
                            t0_hr=t0_hr, wet_ft=wet_m / FT)
    if to_grid_xy is not None:
        xy = np.asarray(to_grid_xy(np.column_stack([mesh.x, mesh.y])), dtype=float)
        mesh.x, mesh.y = xy[:, 0], xy[:, 1]
        mesh._nn = None
    a = hecras.rasterise(mesh, like_grid.x0, like_grid.y0, like_grid.dx, like_grid.nx,
                         like_grid.ny, names=("depth", "wse", "velocity", "arrival"))
    shape = (like_grid.ny, like_grid.nx)
    nan = np.full(shape, np.nan)
    d = np.asarray(a.get("depth", nan), float) * FT
    wet = np.isfinite(d) & (d > wet_m)
    arr = np.asarray(a.get("arrival", nan), float)
    return {"depth": np.where(wet, d, np.nan),
            "velocity": np.where(wet, np.asarray(a.get("velocity", nan), float) * FT, np.nan),
            "arrival": np.where(wet & np.isfinite(arr), np.clip(arr, 0.0, None), np.nan),
            "wse": np.where(wet, np.asarray(a.get("wse", nan), float) * FT, np.nan)}


def write_results(arrays, like_grid, writer, folder, names=None):
    """Writes the arrays under workflow.FINAL_NAMES in ft, ft/s and hours (what ND Dam Hazard
    reads). Returns {kind: path}."""
    from . import workflow
    names = names or workflow.FINAL_NAMES
    out = {}
    for kind, arr in arrays.items():
        p = os.path.join(folder, names[kind])
        writer(like_grid.like(workflow._to_us(kind, arr), np.nan), p)
        out[kind] = p
    return out


def transformer(src_wkt, dst_wkt):
    """xy (n,2) from one CRS to another with OSR (GDAL), or None when they are the same."""
    if not src_wkt or not dst_wkt or src_wkt == dst_wkt:
        return None
    from . import hecras_model as hm
    return hm.transformer(hm.srs_from(src_wkt), hm.srs_from(dst_wkt))


# ============================================================================
# the Runner stage (TIER_HECRAS)
# ============================================================================
LISFLOOD_PREFIX = "LISFLOOD-FP "


def run_stage(dam_folder, final_paths, writer, st, log=None, env=None, d=None):
    """After a LISFLOOD-FP run (workflow.run_dam with TIER_HECRAS): keeps its final rasters
    as 'LISFLOOD-FP <name>', builds, preprocesses and computes the HEC-RAS 2D model, and
    writes the HEC-RAS maxima under FINAL_NAMES on the same grid. Returns {'final': {kind:
    path}, 'warnings': [...], 'hdf': path, 'project': folder}."""
    from . import gdal_io
    log = log or (lambda m: None)
    d = d or require(env)
    dp = final_paths.get("depth")
    if not dp or not os.path.exists(dp):
        raise HecRasEngineError("the LISFLOOD-FP run wrote no final depth raster (write_final)")
    like = gdal_io.read_grid(dp)
    for kind, p in final_paths.items():
        if p and os.path.exists(p):
            shutil.copy2(p, os.path.join(os.path.dirname(p),
                                         LISFLOOD_PREFIX + os.path.basename(p)))
    stamp = time.strftime("%Y%m%d_%H%M%S")
    proj = os.path.join(d["prefix"], "drive_c", "ndbreach",
                        "{}_{}".format(os.path.basename(os.path.normpath(dam_folder)), stamp))
    r = write_project(dam_folder, proj, log=log, env=env)      # NDBREACH_HECRAS_INFLOW_MODE
    name = r["model"].name
    times = preprocess(proj, name, d=d, env=env, terrain_tif=r["paths"]["terrain_tif"],
                       prj=r["paths"]["projection"], log=log)
    hdf = compute(proj, name, d=d, env=env, log=log)
    keep = os.path.join(dam_folder, "_hecras")
    os.makedirs(keep, exist_ok=True)
    shutil.copy2(hdf, keep)
    tr = transformer(r["info"].get("model_wkt"), like.crs_wkt or r["info"].get("work_wkt"))
    arr = results(hdf, like, arrival_m=float(getattr(st, "arrival_depth_ft", 1.0)) * 0.3048,
                  t0_hr=(r.get("inflow_bc") or {}).get("t_ref_hr", 0.0), to_grid_xy=tr)
    final = write_results(arr, like, writer, dam_folder)
    rep = {"engine": "hecras", "hecras_version": "6.6", "platform": d["platform"],
           "project": proj, "results_hdf": os.path.join(keep, os.path.basename(hdf)),
           "preprocess_s": times, "inflow_mode": r["inflow_mode"],
           "inflow_series": (r.get("inflow_bc") or {}).get("series"),
           "time_step": r["info"].get("time_step"),
           "lisflood_rasters_kept_as": LISFLOOD_PREFIX + "<name>"}
    with open(os.path.join(keep, "hecras_stage.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1)
    return {"final": final, "hdf": hdf, "project": proj, "report": rep,
            "warnings": ["Final rasters are from HEC-RAS 2D (preliminary engine; LISFLOOD-FP "
                         "rasters kept as '{}...'). Review _hecras/hecras_stage.json."
                         .format(LISFLOOD_PREFIX)]}
