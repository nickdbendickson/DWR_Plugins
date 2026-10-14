"""v3.4.0 near-dam refine: an FV1 fine grid where receptors sit close to the breach.

LISFLOOD-FP's default "acceleration" (local inertial) solver is meant for gradually varied,
subcritical floodplain flow. Just below a breach the flow is fast and often supercritical, so
the velocities it gives there are not reliable (the depth-velocity charts of ND 5.2 then rest on
a weak number). When a structure, road or rail receptor lies within the flagged distance of the
breach point AND the final rasters come from the ACC run, this option re-runs the first stretch
on a fine grid with the FV1 shock-capturing solver and merges the fine results into the final
rasters the way the nested tool does (fine cells upstream of an interface plane across the flow
replace the coarse cells they fall in: maximum depth, speed and WSE, earliest arrival).

The flagged distance and the fine cell size are the engineer's screening values (defaults 1 mi
and half the main cell), not requirements of ND REG_05.2026. Nothing here has been run against
a real LISFLOOD-FP or in a real QGIS: the tests stub the model.
"""
import copy
import math
import os

import numpy as np

from . import nested
from . import lisflood as lfmod
from .units import FT, MILE

DEFAULT_DIST_MI = 1.0        # the engineer's screening distance (YOUR value)
MARGIN = 0.3                 # the fine domain runs this far past the flagged distance (as nested)
MIN_HALF_WIDTH_MI = 0.35     # nested's fine half-width
FOLDER = "refine_near_dam"   # under <dam folder>/_breach_model/
NOTE = ("Near-dam refine is an engineer's choice, not a requirement of ND REG_05.2026: the "
        "flagged distance and the fine cell size are screening values you choose. The ACC "
        "(local inertial) velocities near a breach are not reliable in fast or supercritical "
        "flow; the FV1 solver is shock-capturing. Not run against a real LISFLOOD-FP here.")


def receptors_within(receptors, structures, breach_xy, dist_m):
    """How many receptors lie within `dist_m` of the breach point. Pure.

    receptors: impacts.Receptors or None (points: structures; line samples: road / rail);
    structures: [structures.Structure] (culverts, bridges, notches: lines; counted too).
    Returns {"structures", "road_samples", "rail_samples", "crossings", "total",
    "nearest_ft"} (nearest_ft None when nothing is within the distance)."""
    out = {"structures": 0, "road_samples": 0, "rail_samples": 0, "crossings": 0, "total": 0,
           "nearest_ft": None}
    nearest = np.inf
    bx, by = float(breach_xy[0]), float(breach_xy[1])
    if receptors is not None and len(receptors):
        d = np.hypot(receptors.x - bx, receptors.y - by)
        near = d <= dist_m
        if near.any():
            nearest = min(nearest, float(d[near].min()))
        for key, kind in (("structures", "structure"), ("road_samples", "road"),
                          ("rail_samples", "rail")):
            out[key] = int((near & (receptors.kind == kind)).sum())
    for s in structures or []:
        line = np.asarray(getattr(s, "line", []), float).reshape(-1, 2)
        if not len(line):
            continue
        d = np.hypot(line[:, 0] - bx, line[:, 1] - by)
        if d.min() <= dist_m:
            out["crossings"] += 1
            nearest = min(nearest, float(d.min()))
    out["total"] = out["structures"] + out["road_samples"] + out["rail_samples"] + \
        out["crossings"]
    if np.isfinite(nearest):
        out["nearest_ft"] = round(nearest / FT, 0)
    return out


def fine_cell(st):
    """The fine cell size (m): st.refine_cell_m, or half the main cell when 0 / None."""
    c = float(getattr(st, "refine_cell_m", 0.0) or 0.0)
    return c if c > 0 else 0.5 * float(st.cell_size_m)


def decide(st, final_model, receptors, structures, breach_xy):
    """Whether to refine, and why. Pure. Returns a dict with "refine" (bool), "reason",
    "dist_mi", "fine_cell_m", "solver" and the receptor counts."""
    dist_mi = float(getattr(st, "refine_dist_mi", DEFAULT_DIST_MI) or DEFAULT_DIST_MI)
    info = {"refine": False, "dist_mi": dist_mi, "fine_cell_m": fine_cell(st),
            "solver": lfmod.SOLVER_FV1, "counts": None}
    if not getattr(st, "refine_near", False):
        info["reason"] = "off (REFINE_NEAR not set)"
        return info
    if final_model != "LISFLOOD-FP":
        info["reason"] = ("the final rasters come from {}, not from the LISFLOOD-FP ACC run; "
                          "its velocities near the dam are not the ones in question".format(
                              final_model))
        return info
    if getattr(st, "lisflood_solver", lfmod.SOLVER_ACC) != lfmod.SOLVER_ACC:
        info["reason"] = "the main run already uses the {} solver".format(st.lisflood_solver)
        return info
    if (receptors is None or not len(receptors)) and not structures:
        info["reason"] = ("no structure, road, rail or crossing layer was given, so nothing can "
                          "be flagged within {:g} mi of the breach".format(dist_mi))
        return info
    counts = receptors_within(receptors, structures, breach_xy, dist_mi * MILE)
    info["counts"] = counts
    if counts["total"] == 0:
        info["reason"] = "no receptor lies within {:g} mi of the breach point".format(dist_mi)
        return info
    if info["fine_cell_m"] >= float(st.cell_size_m):
        info["reason"] = ("the fine cell ({:g} m) is not smaller than the main cell ({:g} m)"
                          .format(info["fine_cell_m"], st.cell_size_m))
        return info
    info["refine"] = True
    info["reason"] = ("{} receptor(s) within {:g} mi of the breach (nearest {:,.0f} ft): {} "
                      "structure(s), {} road and {} rail sample(s), {} crossing(s); the ACC "
                      "velocities there are unreliable".format(
                          counts["total"], dist_mi, counts["nearest_ft"] or 0,
                          counts["structures"], counts["road_samples"], counts["rail_samples"],
                          counts["crossings"]))
    return info


def fine_settings(st, dist_mi, cell_m, sim_time_s, hy, hy_nb):
    """RunSettings copy for the fine FV1 run. Pure (no I/O)."""
    import dataclasses
    f = copy.copy(st)
    f.overrides = dataclasses.replace(st.overrides)
    f.cell_size_m = float(cell_m)
    f.lisflood_solver = lfmod.SOLVER_FV1
    f.tier = "lisflood"
    f.initial_length_mi = dist_mi * (1.0 + MARGIN)
    f.half_width_mi = min(float(st.half_width_mi), max(MIN_HALF_WIDTH_MI, 0.5 * dist_mi))
    f.max_iterations = 1
    f.sim_time_hr = max(1.0, float(sim_time_s) / 3600.0)
    f.receptors = None
    f.gauges = []
    f.products = False
    f.dam_record = None
    f.refine_near = False                 # no recursion
    f.keep_arrays = True
    f.write_final = True                  # fine-resolution rasters in its own folder
    f.resume = False
    f.animation = False
    f.snapshot_frames = 0
    f.class_stop = False
    f.fixed_plan = None
    f.user_hydrograph_obj = hy
    f.user_baseline_obj = hy_nb
    f.user_hydrograph = None
    f.scenario = "sunny"                  # the hydrographs are given; no routing again
    f.flood = None
    f.eap_map = "mapbook"
    return f


def merge_onto(main_grid, main_arrays, fine_grid, fine_arrays, point, direction,
               keys=("depth", "velocity", "wse", "arrival")):
    """Fine results replace the main results where the fine domain covers, upstream of the
    interface plane through `point` across `direction` (nested's scatter rules: max depth,
    speed and WSE, earliest arrival). Everything else keeps the main run's values. Returns
    ({kind: array on main_grid}, cells replaced)."""
    out = {}
    up_f = ~nested._side(fine_grid, point, direction)
    # main cells to clear: inside the fine domain's bounds and upstream of the plane
    X, Y = np.meshgrid(main_grid.x_centers(), main_grid.y_centers())
    fx0, fy0, fx1, fy1 = fine_grid.bounds
    cover = (X >= fx0) & (X <= fx1) & (Y >= fy0) & (Y <= fy1) & ~nested._side(
        main_grid, point, direction)
    for k in keys:
        tgt = main_grid.like(np.array(main_arrays[k], dtype=float, copy=True), np.nan)
        tgt.data[cover] = np.nan
        how = "min" if k == "arrival" else "max"
        nested._scatter(tgt, fine_grid, fine_arrays[k], up_f, how)
        out[k] = tgt.data
    return out, int(cover.sum())


def zone_stats(grid, arrays, breach_xy, dist_m):
    """Maximum depth (ft) and velocity (ft/s) within `dist_m` of the breach. Pure."""
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    zone = np.hypot(X - breach_xy[0], Y - breach_xy[1]) <= dist_m
    d = np.where(zone, arrays["depth"], np.nan)
    v = np.where(zone, arrays["velocity"], np.nan)
    return {"max_depth_ft": round(float(np.nanmax(d)) / FT, 2) if np.isfinite(d).any() else 0.0,
            "max_velocity_fps": (round(float(np.nanmax(v)) / FT, 2)
                                 if np.isfinite(v).any() else 0.0),
            "wet_cells": int(np.isfinite(d).sum())}


def run_refine(dam, dam_xy, st, raw_terrain, writer, model_dir, hy, hy_nb, inflow_xy, path,
               sim_time_s, dem, fin, base_arrays, decision, progress=None, log=None,
               cancel=None):
    """Run the fine FV1 stretch and merge it into `fin` (and `base_arrays`). Returns
    (fin_merged, base_merged or None, info). `raw_terrain` is the terrain BEFORE the run's
    DEM edits were wrapped on (run_dam wraps them again). Raises what workflow.run_dam raises."""
    from . import workflow
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    dist_mi = decision["dist_mi"]
    cell_m = decision["fine_cell_m"]
    fst = fine_settings(st, dist_mi, cell_m, sim_time_s, hy, hy_nb if base_arrays is not None
                        else None)
    folder = os.path.join(model_dir, FOLDER)
    log("Near-dam refine: {}. Running the first {:.2f} mi below the breach on a {:g} m grid "
        "with the FV1 solver.".format(decision["reason"], fst.initial_length_mi, cell_m))
    fr = workflow.run_dam(dam, dam_xy, fst, raw_terrain, writer, folder, inflow_xy,
                          progress, log, cancel)
    info = {"applied": False, "reason": decision["reason"], "dist_mi": dist_mi,
            "fine_cell_m": cell_m, "main_cell_m": float(st.cell_size_m),
            "solver": lfmod.SOLVER_FV1, "counts": decision.get("counts"), "folder": folder,
            "note": NOTE}
    if fr.final_si is None or fr.grid is None:
        info["warning"] = "the fine run gave no results; the ACC results are kept"
        return fin, base_arrays, info
    ipoint, idir, plen = nested.interface_point(path, dist_mi * MILE)
    # LISFLOOD-FP 8.1 writes no FV1 .maxVc. Merging that as 0 ft/s lowered hazard
    # next to the dam. Keep the ACC velocities and take depth, WSE and arrival
    # from the fine run.
    fstats = dict(getattr(fr, "lisflood_stats", None) or {})
    keys = ("depth", "velocity", "wse", "arrival")
    if fstats.get("velocity_written") is False:
        keys = ("depth", "wse", "arrival")
        info["velocity_kept_acc"] = True
        info["warning"] = ("LISFLOOD-FP wrote no maximum velocity for the FV1 fine run, so "
                           "the ACC velocities are kept near the dam; depth, water surface and "
                           "arrival come from the fine run")
        log("Near-dam refine: " + info["warning"])
    merged, replaced = merge_onto(dem, fin, fr.grid, fr.final_si, ipoint, idir, keys)
    before = zone_stats(dem, fin, dam_xy, dist_mi * MILE)
    after = zone_stats(dem, merged, dam_xy, dist_mi * MILE)
    base_m = None
    if base_arrays is not None and fr.base_si is not None:
        base_m, _ = merge_onto(dem, base_arrays, fr.grid, fr.base_si, ipoint, idir, keys)
    elif base_arrays is not None:
        info["warning"] = ("the fine run has no no-breach baseline; the baseline and "
                           "incremental rasters keep the ACC results near the dam")
        base_m = base_arrays
    # the fine domain is deliberately short: the flood leaving its downstream edge is expected
    fine_warn = [w for w in fr.warnings if not w.startswith("Stopped after 1 runs")
                 and "handed over" not in w]
    info.update({"applied": True, "interface_xy": [round(ipoint[0], 1), round(ipoint[1], 1)],
                 "interface_direction": [round(idir[0], 3), round(idir[1], 3)],
                 "fine_cells": int(fr.grid.data.size), "cells_replaced": replaced,
                 "zone_before_acc": before, "zone_after_fv1": after,
                 "fine_warnings": fine_warn, "fine_report": fr.report_json,
                 "summary": "ACC velocities within {:g} mi of the breach replaced by an FV1 "
                            "fine-grid run ({:g} m cells); {} receptors inside. Zone maximum "
                            "velocity {} -> {} ft/s, depth {} -> {} ft.".format(
                                dist_mi, cell_m, (decision.get("counts") or {}).get("total", 0),
                                before["max_velocity_fps"], after["max_velocity_fps"],
                                before["max_depth_ft"], after["max_depth_ft"])})
    log("Near-dam refine: " + info["summary"])
    return merged, base_m, info


def report_lines(info):
    """Markdown lines for the run report."""
    if not info:
        return []
    lines = ["## Near-dam refine (v3.4.0)", ""]
    if not info.get("applied"):
        lines += ["Not applied: {}.".format(info.get("reason", "")), ""]
        if info.get("warning"):
            lines += ["- " + info["warning"], ""]
        return lines
    b, a = info["zone_before_acc"], info["zone_after_fv1"]
    lines += [info["summary"], "",
              "| | ACC main run | FV1 fine run (merged) |", "|---|---|---|",
              "| Cell size | {:g} m | {:g} m |".format(info["main_cell_m"], info["fine_cell_m"]),
              "| Max depth within {:g} mi | {} ft | {} ft |".format(info["dist_mi"],
                                                                     b["max_depth_ft"],
                                                                     a["max_depth_ft"]),
              "| Max velocity within {:g} mi | {} ft/s | {} ft/s |".format(
                  info["dist_mi"], b["max_velocity_fps"], a["max_velocity_fps"]),
              "| Cells replaced on the main grid | | {:,} |".format(info["cells_replaced"]),
              "", "Interface plane at {:.0f}, {:.0f}; fine results upstream of it, main results "
              "downstream (nested merge rules). Fine-resolution rasters: {}.".format(
                  info["interface_xy"][0], info["interface_xy"][1], info["folder"]),
              "", info["note"], ""]
    if info.get("warning"):
        lines += ["- " + info["warning"], ""]
    return lines
