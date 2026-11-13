"""Cascade failure: an upstream dam's flood arriving at the next dam down.

Dams are given upstream first. Each dam's own breach model runs as usual.
Between them:

* the upstream run measures the flood's discharge at the next dam (a LISFLOOD-FP
  gauge just upstream of it) and steers its domain toward it, so the wave
  is followed down to that dam;
* the wave is routed through the downstream reservoir. If it overtops the dam
  (or the failure is assumed, `fail_rule="assume"`), that dam is modelled
  failing: in the flood-inflow scenario with the wave added to its own
  inflow when its pool data are given, else as a sunny-day breach starting
  when the wave arrives;
* the downstream reservoir is treated as full ("tailwater") for the
  upstream flood when `pool` polygons are given, so the upstream flood does not
  fill a reservoir that was already at its normal level.

The combined rasters take, cell by cell, the deepest and fastest water of any
run and the earliest arrival, measured from the first dam's breach initiation.
That is conservative; downstream of a failed dam its own run already carries
the water that came from upstream.
"""
import copy
import dataclasses
import os
from typing import Optional

import numpy as np

from . import breach as brmod
from . import flood as floodmod
from . import impacts as impmod
from . import workflow
from .units import FT


@dataclasses.dataclass
class CascadeDam:
    dam: brmod.DamData
    xy: tuple                                   # crest point, model CRS
    inflow_xy: Optional[tuple] = None
    flood: Optional[floodmod.FloodInputs] = None    # its own PMF inflow, spillway, normal pool
    pool: Optional[object] = None               # conditioning.PondLower for its reservoir
    gauge_offset_m: float = 150.0               # gauge this far upstream of the crest


class CascadeResult:
    def __init__(self):
        self.runs = []             # [(CascadeDam, RunResult or None)]
        self.decisions = []        # one dict per downstream dam
        self.combined = {}         # name -> path
        self.warnings = []
        self.folder = ""
        self.report_md = ""


def _gauge_xy(up, down):
    v = np.array(up.xy, float) - np.array(down.xy, float)
    n = float(np.hypot(*v)) or 1.0
    return tuple(np.array(down.xy, float) + v / n * down.gauge_offset_m)


def _sum_inflows(t_a, q_a, t_b, q_b):
    t = np.unique(np.concatenate([t_a, t_b]))
    return t, np.interp(t, t_a, q_a, right=0.0) + np.interp(t, t_b, q_b, right=0.0)


def _first_exceed(t, q, frac=0.05):
    if not len(q) or q.max() <= 0:
        return None
    k = int(np.argmax(q >= frac * q.max()))
    return float(t[k])


def run_cascade(chain, st, terrain_src, writer, out_root, fail_rule="overtop", progress=None,
                log=None, cancel=None):
    """chain: [CascadeDam] upstream first. fail_rule: "overtop" (a downstream
    dam fails only if the wave overtops it) or "assume" (it always fails)."""
    from . import conditioning as cond
    progress = progress or (lambda p, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or (lambda: False)
    if len(chain) < 2:
        raise brmod.BreachInputError("a cascade needs at least two dams")
    for cd in chain:                 # top of dam from the DEM when it was not given
        if cd.dam.top_of_dam_ft is None:
            near = terrain_src.read((cd.xy[0] - 60, cd.xy[1] - 60, cd.xy[0] + 60, cd.xy[1] + 60),
                                    3.0)
            z = near.masked()
            X, Y = np.meshgrid(near.x_centers(), near.y_centers())
            sel = np.hypot(X - cd.xy[0], Y - cd.xy[1]) <= 30.0
            if not np.isfinite(z[sel]).any():
                raise brmod.BreachInputError("{}: no DEM data at the dam point".format(
                    cd.dam.dam_id))
            cd.dam.top_of_dam_ft = float(np.nanmax(z[sel])) / FT
    out = CascadeResult()
    out.folder = os.path.join(out_root, "Cascade_" + workflow._safe(chain[0].dam.dam_id))
    os.makedirs(out.folder, exist_ok=True)
    n = len(chain)
    wave = None                      # (t_s, q_cms) arriving at the dam being modelled
    t_offset_h = 0.0                 # this run's breach initiation, hours after the first dam's
    failed = True
    first_t_ref_h = 0.0
    for i, cd in enumerate(chain):
        if cancel():
            raise workflow.lfmod.Cancelled("cancelled")
        lo, hi = 100.0 * i / n, 100.0 * (i + 1) / n
        folder = os.path.join(out_root, workflow._safe(cd.dam.dam_id))
        nxt = chain[i + 1] if i + 1 < n else None
        cst = copy.copy(st)
        cst.keep_arrays = True
        cst.write_final = True
        cst.gauges = list(st.gauges)
        cst.receptors = copy.deepcopy(st.receptors) if st.receptors is not None else None
        cst.edits = copy.deepcopy(st.edits) if st.edits is not None else cond.Edits()
        if nxt is not None:
            cst.gauges.append({"name": "to_" + workflow._safe(nxt.dam.dam_id),
                               "xy": _gauge_xy(cd, nxt)})
            if cst.receptors is None:
                cst.receptors = impmod.Receptors()
            cst.receptors.add_points([nxt.xy], ids=["dam " + str(nxt.dam.dam_id)])
        for later in chain[i + 1:]:                 # downstream reservoirs start full
            if later.pool is not None:
                cst.edits.ponds.append(copy.deepcopy(later.pool))
        if wave is not None:
            # this dam is modelled failing under the wave from upstream
            comb = cd.flood
            if comb is not None:
                t, q = _sum_inflows(comb.inflow_t_s, comb.inflow_q_cms, wave[0], wave[1])
                fi = dataclasses.replace(comb, inflow_t_s=t, inflow_q_cms=q)
            else:
                fi = None
            if fi is not None:
                cst.scenario = workflow.SCENARIO_FLOOD
                fi.trigger = floodmod.TRIGGER_WSE if fail_rule == "overtop" else fi.trigger
                if fail_rule == "overtop":
                    fi.trigger_value = cd.dam.top_of_dam_ft
                cst.flood = fi
            else:
                cst.scenario = workflow.SCENARIO_SUNNY
        log("=== cascade dam {}/{}: {}".format(i + 1, n, cd.dam.dam_id))
        # does the wave overtop this dam?
        if wave is not None and cd.flood is not None and fail_rule == "overtop":
            fr = floodmod.route_flood(cd.dam, st.breach_method, st.overrides, cst.flood)
            if not fr.failed:
                out.decisions.append({
                    "dam": cd.dam.dam_id, "fails": False,
                    "reason": "the flood from upstream does not overtop it (peak pool {} ft "
                              "against the top of dam {} ft)".format(
                                  fr.summary.get("peak_pool_ft_no_breach"),
                                  cd.dam.top_of_dam_ft)})
                out.runs.append((cd, None))
                log(out.decisions[-1]["reason"])
                break
        elif wave is not None and cd.flood is None and fail_rule == "overtop":
            out.warnings.append("{}: no pool data (normal level, spillway), so the wave cannot be "
                                "routed through its reservoir; it is assumed to fail when the "
                                "wave arrives.".format(cd.dam.dam_id))
        try:
            res = workflow.run_dam(cd.dam, cd.xy, cst, terrain_src, writer, folder, cd.inflow_xy,
                                   workflow._span(progress, lo, hi), log, cancel)
        except brmod.BreachInputError as e:
            out.warnings.append("{}: {}".format(cd.dam.dam_id, e))
            out.runs.append((cd, None))
            break
        out.runs.append((cd, res))
        if i > 0 and t_offset_h is None:
            t_offset_h = res.t_ref_hr - first_t_ref_h
        if i > 0:
            out.decisions.append({"dam": cd.dam.dam_id, "fails": True, "reason": (
                "assumed to fail" if fail_rule == "assume" else
                "the flood from upstream overtops it" if cd.flood is not None else
                "no pool data: assumed to fail when the flood arrives") +
                "; breach initiation {:.2f} h after the first dam's".format(t_offset_h)})
        res.cascade_offset_h = t_offset_h
        out.warnings += ["{}: {}".format(cd.dam.dam_id, w) for w in res.warnings]
        if i == 0:
            first_t_ref_h = res.t_ref_hr
        this_offset_h = res.cascade_offset_h or 0.0
        this_t_ref_h = res.t_ref_hr
        if nxt is None:
            break
        g = res.gauges.get("to_" + workflow._safe(nxt.dam.dam_id))
        if g is None or not len(g[1]) or g[1].max() <= 0:
            out.decisions.append({"dam": nxt.dam.dam_id, "fails": False,
                                  "reason": "the flood from {} does not reach it within the "
                                            "model domain (no discharge at the gauge)".format(
                                                cd.dam.dam_id)})
            out.runs.append((nxt, None))
            break
        wave = (g[0], g[1])
        arrive_h = (_first_exceed(g[0], g[1]) or 0.0) / 3600.0
        # the next dam's breach initiation, in hours after the first dam's. A dam modelled with
        # its own pool data starts from the flood's clock; without it, it starts when the wave
        # arrives.
        if nxt.flood is not None:
            t_offset_h = None            # known once its run is done (its t_ref)
        else:
            # the wave's clock starts at this run's own breach initiation (t_ref)
            t_offset_h = this_offset_h + arrive_h - this_t_ref_h
    _combine(out, st, writer)
    _report(out, fail_rule, st)
    progress(100, "done")
    return out


def _combine(out, st, writer):
    runs = [(cd, r, off) for (cd, r), off in zip(out.runs, _offsets(out)) if r is not None
            and r.final_si is not None]
    if len(runs) < 2:
        return
    # M21 follow-up: all sub-runs share one LISFLOOD binary, so velocity is all-or-none. When no
    # run produced a maximum-velocity raster (final_si velocity all-NaN), the combined velocity is
    # all zeros; omit the combined velocity product so a Hazard classification of the cascade
    # output refuses rather than reading velocity 0 off the v=0 ACER TM-11 curves.
    no_velocity = not any(np.isfinite(r.final_si["velocity"]).any() for _cd, r, _off in runs)
    g0 = runs[0][1].grid
    # H07 (cascade georef): the combined rasters span the union of every run's extent, each run
    # drawn on its TRUE ground. A dam that auto-refines to a finer cell than another run must not
    # be placed as if every run shared the first run's cell size (otherwise it is half-scale or
    # stretched 2x and the union is truncated). The base cell is the finest run cell, so no run
    # loses resolution; each base cell takes the run cell that contains its centre, which for runs
    # that share a cell size (and so are grid-aligned here) is the former whole-cell placement.
    dx = min(r.grid.dx for _c, r, _o in runs)
    x0 = min(r.grid.x0 for _c, r, _o in runs)
    y0 = max(r.grid.y0 for _c, r, _o in runs)
    x1 = max(r.grid.x0 + r.grid.nx * r.grid.dx for _c, r, _o in runs)
    y1 = min(r.grid.y0 - r.grid.ny * r.grid.dx for _c, r, _o in runs)
    nx, ny = int(round((x1 - x0) / dx)), int(round((y0 - y1) / dx))
    base = type(g0)(np.full((ny, nx), np.nan), x0, y0, dx, g0.nodata, g0.crs_wkt)
    depth = np.zeros(base.data.shape)
    vel = np.zeros(base.data.shape)
    wse = np.full(base.data.shape, -np.inf)
    arr = np.full(base.data.shape, np.inf)
    bx = x0 + (np.arange(nx) + 0.5) * dx          # base cell-centre coordinates (world)
    by = y0 - (np.arange(ny) + 0.5) * dx
    for cd, r, off in runs:
        g = r.grid
        # the run cell that contains each base cell's centre
        src_c = np.floor((bx - g.x0) / g.dx).astype(int)
        src_r = np.floor((g.y0 - by) / g.dx).astype(int)
        jj = np.where((src_c >= 0) & (src_c < g.nx))[0]
        ii = np.where((src_r >= 0) & (src_r < g.ny))[0]
        if ii.size == 0 or jj.size == 0:
            continue
        sl_b = np.ix_(ii, jj)
        gr, gc = src_r[ii][:, None], src_c[jj][None, :]
        d = np.nan_to_num(r.final_si["depth"][gr, gc])
        depth[sl_b] = np.maximum(depth[sl_b], d)
        vel[sl_b] = np.maximum(vel[sl_b], np.nan_to_num(r.final_si["velocity"][gr, gc]))
        w = r.final_si["wse"][gr, gc]
        wse[sl_b] = np.where(np.isfinite(w), np.maximum(wse[sl_b], w), wse[sl_b])
        a = r.final_si["arrival"][gr, gc] + off
        arr[sl_b] = np.where(np.isfinite(a), np.minimum(arr[sl_b], a), arr[sl_b])
    wet = depth > 1e-4
    items = {"Cascade Depth (Max).tif": np.where(wet, depth / FT, np.nan),
             "Cascade Velocity (Max).tif": np.where(wet, vel / FT, np.nan),
             "Cascade WSE (Max).tif": np.where(wet & np.isfinite(wse), wse / FT, np.nan),
             "Cascade Arrival Time.tif": np.where(np.isfinite(arr), arr, np.nan)}
    if no_velocity:                 # M21 follow-up: no run wrote velocity -> omit the zeros product
        del items["Cascade Velocity (Max).tif"]
        try:
            os.remove(os.path.join(out.folder, "Cascade Velocity (Max).tif"))
        except OSError:
            pass
    for name, a in items.items():
        p = os.path.join(out.folder, name)
        writer(base.like(a, np.nan), p)
        out.combined[name] = p
    out.combined_grid = base


def _offsets(out):
    """Breach initiation of each run in hours after the first dam's."""
    return [getattr(r, "cascade_offset_h", 0.0) if r is not None else 0.0 for _cd, r in out.runs]


def _report(out, fail_rule, st):
    lines = ["# Cascade failure", "", "Dams upstream to downstream: " + ", ".join(
        str(cd.dam.dam_id) for cd, _r in out.runs), "",
        "Rule for a downstream dam: " + (
            "fails only if the flood from upstream overtops it" if fail_rule == "overtop"
            else "assumed to fail"), "", "## Decisions", ""]
    for d in out.decisions:
        lines.append("- {}: {} ({})".format(d["dam"], "FAILS" if d["fails"] else "holds",
                                            d["reason"]))
    lines += ["", "## Runs", ""]
    for cd, r in out.runs:
        if r is None:
            lines.append("- {}: not modelled".format(cd.dam.dam_id))
        else:
            s = r.summary
            lines.append("- {}: peak breach {:,.0f} cfs, {:,.0f} ac flooded, deepest {} ft; "
                         "folder {}".format(cd.dam.dam_id, s.get("peak_breach_cfs", 0),
                                            s.get("flooded_acres", 0), s.get("max_depth_ft"),
                                            r.folder))
    if out.combined:
        lines += ["", "## Combined rasters (deepest / fastest / earliest of all runs)", ""]
        lines += ["- " + p for p in out.combined.values()]
    if out.warnings:
        lines += ["", "## Check these", ""] + ["- " + w for w in out.warnings]
    lines += [""]
    out.report_md = os.path.join(out.folder, "Cascade_Report.md")
    with open(out.report_md, "w", encoding="utf-8") as f:
        # 3.20.2: in SI when the suite shows SI (as run_report.md is)
        f.write(workflow._display_units_md("\n".join(lines)))
