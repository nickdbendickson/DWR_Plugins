"""Opt-in: the dam and a full reservoir are built into the model grid, and a trapezoid
notch is lowered into the bed while the run goes. An alternative to injecting a breach
hydrograph. Off, this module is not called and nothing in the run changes.

The notch is the same linear weir opening the routed hydrograph uses: the invert falls
from the starting water surface (or the top of dam, if the pool is above it) to the
breach bottom, and the bottom width grows from 0, both linear over the formation time.
Elevations are in the DEM's datum. The host writes the bed between batches; no kernel
changes.
"""
import math
import os

import numpy as np

from . import breach as brmod
from .units import ACFT, CFS, FT


UPDATE_S = 10.0
# cells already this close to the search radius are treated as cut off by it
_EDGE_CELLS = 2


def refuse(st):
    """Stop, with what to pick instead, unless this run may lower a notch into the bed.
    Returns at once when the option is off, so a normal run never reaches the checks."""
    if not getattr(st, "breach_in_terrain", False):
        return
    from . import lisflood as lf
    exe = getattr(st, "lisflood_exe", "") or ""
    if not (lf.is_gpu(exe) or lf.is_gpu_fv(exe) or lf.is_gpu_fv2(exe) or lf.is_gpu_dg2(exe)):
        raise brmod.BreachInputError(
            "Breach inside the terrain runs only on Boann ACC, FV1, FV2 or DG2. This engine "
            "does not lower the bed: one builds its tables from the bed at the start, and the "
            "others would ignore the notch and still inject the hydrograph. Pick Boann ACC, "
            "FV1, FV2 or DG2, or turn breach inside the terrain off.")
    if str(getattr(st, "tier", "lisflood") or "lisflood") != "lisflood":
        raise brmod.BreachInputError(
            "Breach inside the terrain runs only on the single flood-model tier. A tier that "
            "also builds a second model would add the breach hydrograph on top of a reservoir "
            "that is already full. Set the tier to the single model, or turn this off.")
    if str(getattr(st, "scenario", "") or "") != "sunny":
        raise brmod.BreachInputError(
            "Breach inside the terrain is a sunny-day run: the reservoir starts full and no "
            "flood hydrograph is added. Pick the sunny-day scenario, or turn this off.")
    if getattr(st, "user_hydrograph_obj", None) is not None or getattr(st, "user_hydrograph", None):
        raise brmod.BreachInputError(
            "Breach inside the terrain needs the opening's shape. A hydrograph you supply has "
            "no invert, bottom width or side slope. Pick a routed weir method, or turn this off.")
    method = getattr(st, "breach_method", "")
    if method not in brmod.ROUTED_METHODS or method == brmod.METHOD_PIPING_ORIFICE:
        raise brmod.BreachInputError(
            "Breach inside the terrain needs a routed weir opening: the invert falls and the "
            "bottom width grows together. A pipe phase, a peak-only curve, or a hydrograph you "
            "supply has no such shape. Pick a routed weir method (not the pipe phase), or turn "
            "this off.")
    if getattr(st, "proposed", None):
        raise brmod.BreachInputError(
            "Breach inside the terrain is not available for a proposed embankment. Run an "
            "existing dam, or turn this off.")


def _lines_of(raw):
    """Polylines with at least two points. None and a one-point fallback are dropped."""
    out = []
    for ln in raw or []:
        if not ln:
            continue
        pts = [(float(p[0]), float(p[1])) for p in ln]
        if len(pts) >= 2:
            out.append(pts)
    return out


def _project(line, xy):
    """(distance m, arc length m from the start of the line) of the closest point on it."""
    xy = np.asarray(xy, float)
    best = None
    acc = 0.0
    pts = np.asarray(line, float)
    for a, b in zip(pts[:-1], pts[1:]):
        ab = b - a
        L = float(np.hypot(ab[0], ab[1]))
        if L <= 0.0:
            continue
        t = max(0.0, min(1.0, float(np.dot(xy - a, ab) / (L * L))))
        p = a + t * ab
        d = float(np.hypot(*(xy - p)))
        if best is None or d < best[0]:
            best = (d, acc + t * L)
        acc += L
    return best


def _line_length(line):
    pts = np.asarray(line, float)
    return float(sum(np.hypot(*(b - a)) for a, b in zip(pts[:-1], pts[1:])))


def search_radius_m(dam):
    """The same search radius the reservoir carve uses for this dam."""
    from .reservoir_carve import RADIUS_MAX_M, RADIUS_MIN_M, Curve
    cv = Curve(dam)
    return min(max(3.0 * math.sqrt(max(cv.area(cv.top), 0.0)), RADIUS_MIN_M), RADIUS_MAX_M)


def prepare(dam, hy, raw_lines, dam_xy, downstream, inflow_xy):
    """Geometry that does not depend on one pass's grid. BreachInputError when the run
    cannot stage a notch (no crest line, no weir dimensions)."""
    if raw_lines == []:
        raise brmod.BreachInputError(
            "Breach inside the terrain needs a crest line to raise, and this dam's line has "
            "no on-crest part (a spillway or a saddle took all of it). Draw a crest line that "
            "stays on the embankment, or turn this off.")
    lines = _lines_of(raw_lines)
    if not lines:
        line = getattr(dam, "crest_line_xy", None) or None
        lines = _lines_of([line] if line else [])
    if not lines:
        raise brmod.BreachInputError(
            "Breach inside the terrain needs a crest line. This dam has none, so the crest "
            "would be raised at one point and the notch would have no cells. Draw the crest "
            "line, or turn this off.")
    p = getattr(hy, "params", None) or {}
    need = ("bottom_width_ft", "side_slope_h", "formation_time_hr",
            "starting_wse_ft", "breach_bottom_ft")
    missing = [k for k in need if p.get(k) is None]
    if missing:
        raise brmod.BreachInputError(
            "Breach inside the terrain needs the routed opening ({}); this hydrograph has no "
            "such shape. Pick a routed weir method, or turn this off.".format(
                ", ".join(missing)))
    top_ft = float(dam.top_of_dam_ft)
    wse_ft = float(p["starting_wse_ft"])
    bottom_ft = float(p["breach_bottom_ft"])
    invert_start = min(wse_ft, top_ft) * FT
    invert_final = bottom_ft * FT
    if invert_final > invert_start + 1e-6:
        raise brmod.BreachInputError(
            "Breach inside the terrain: the breach bottom ({:.2f} ft) is above the starting "
            "water surface ({:.2f} ft), so there is no notch to lower. Check the breach bottom "
            "and the starting pool.".format(bottom_ft, min(wse_ft, top_ft)))
    from .reservoir_carve import CREST_WIDTH_DEFAULT_FT, US_SLOPE_DEFAULT
    crest_w = dam.crest_width_ft if dam.crest_width_ft else CREST_WIDTH_DEFAULT_FT
    us = dam.us_slope_h if dam.us_slope_h else US_SLOPE_DEFAULT
    ds = dam.ds_slope_h if dam.ds_slope_h else US_SLOPE_DEFAULT
    if downstream is None and inflow_xy is not None:
        v = (float(inflow_xy[0]) - float(dam_xy[0]), float(inflow_xy[1]) - float(dam_xy[1]))
        n = math.hypot(v[0], v[1])
        if n > 1e-6:
            downstream = (v[0] / n, v[1] / n)
    if downstream is None:
        raise brmod.BreachInputError(
            "Breach inside the terrain needs the downstream side of the dam, and it could not "
            "be told from the crest line or the inflow point. Set the inflow point just below "
            "the dam, or turn this off.")
    wall = getattr(dam, "crest_line_xy", None)
    wall = _lines_of([wall])[0] if _lines_of([wall]) else lines[0]
    return {
        "lines": lines,
        "wall": wall,
        "dam_xy": (float(dam_xy[0]), float(dam_xy[1])),
        "downstream": (float(downstream[0]), float(downstream[1])),
        "top_m": top_ft * FT,
        "top_ft": top_ft,
        "bottom_ft": bottom_ft,
        "start_wse_m": wse_ft * FT,
        "invert_start_m": invert_start,
        "invert_final_m": invert_final,
        "bottom_width_m": float(p["bottom_width_ft"]) * FT,
        "side_slope_h": float(p["side_slope_h"]),
        "formation_s": float(p["formation_time_hr"]) * 3600.0,
        "start_s": float(getattr(hy, "t_ref_s", 0.0) or 0.0),
        "crest_width_ft": float(crest_w),
        "us_h": float(us),
        "ds_h": float(ds),
        "crest_length_m": (float(dam.crest_length_ft) * FT
                           if getattr(dam, "crest_length_ft", None) else None),
        "radius_m": search_radius_m(dam),
        "storage_at_failure_acft": p.get("storage_at_failure_acft"),
        "regression_peaks_cfs": dict(p.get("regression_peaks_cfs") or {}),
        "routed_peak_cfs": float(getattr(hy, "peak_cfs", 0.0) or 0.0),
    }


def band_m(dx, geom):
    """How far from the crest line a cell still belongs to the embankment (m)."""
    face = max(0.0, geom["top_ft"] - geom["bottom_ft"]) * max(geom["us_h"], geom["ds_h"]) * FT
    half_crest = geom["crest_width_ft"] * FT / 2.0
    return max(half_crest + face, max(12.0, float(dx)) / 2.0) + float(dx)


def _pick_line(lines, xy):
    best = None
    for ln in lines:
        hit = _project(ln, xy)
        if hit is not None and (best is None or hit[0] < best[0]):
            best = (hit[0], hit[1], ln)
    return best


def _cell_mask(dem, line, centre_along, band, half_window):
    """Cells within `band` of the polyline and within `half_window` along it of the centre."""
    xs = dem.x_centers()[None, :]
    ys = dem.y_centers()[:, None]
    best_d = np.full(dem.data.shape, np.inf)
    best_a = np.zeros(dem.data.shape, dtype=float)
    acc = 0.0
    pts = np.asarray(line, float)
    for a, b in zip(pts[:-1], pts[1:]):
        ab = b - a
        L2 = float(ab[0] * ab[0] + ab[1] * ab[1])
        L = math.sqrt(L2) if L2 > 0.0 else 0.0
        if L <= 0.0:
            continue
        t = np.clip(((xs - a[0]) * ab[0] + (ys - a[1]) * ab[1]) / L2, 0.0, 1.0)
        d = np.hypot(xs - (a[0] + t * ab[0]), ys - (a[1] + t * ab[1]))
        along = acc + t * L
        take = d < best_d
        best_d = np.where(take, d, best_d)
        best_a = np.where(take, along, best_a)
        acc += L
    sel = ((best_d <= band) & (np.abs(best_a - centre_along) <= half_window) & dem.valid())
    return sel, best_a


def _half_window(geom, line):
    crest_len = geom["crest_length_m"]
    if not crest_len:
        crest_len = _line_length(line)
    height = max(0.0, geom["invert_start_m"] - geom["invert_final_m"])
    top_w = geom["bottom_width_m"] + 2.0 * max(geom["side_slope_h"], 0.0) * height
    return 0.5 * (float(crest_len) + top_w)


def notch_cells(dem, geom, lines):
    """(rows, cols, along_m) of the embankment cells this pass lowers. along_m is the
    distance along the crest from the breach centre (sign does not matter)."""
    picked = _pick_line(lines, geom["dam_xy"])
    if picked is None:
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0, float)
    _dist, centre_along, line = picked
    sel, along = _cell_mask(dem, line, centre_along, band_m(dem.dx, geom), _half_window(geom, line))
    rr, cc = np.nonzero(sel)
    return rr.astype(int), cc.astype(int), (along[rr, cc] - centre_along).astype(float)


def pool_on(dem, geom):
    """(mask, depth_m, info) of the reservoir: ground at or below the starting water
    surface, upstream of the crest, inside the carve search radius."""
    from .reservoir_carve import footprint
    rad = search_radius_m_from(geom)
    mask, info = footprint(dem, geom["dam_xy"], geom["downstream"], geom["start_wse_m"], rad,
                           crest_line=geom["wall"])
    z = dem.masked()
    depth = np.zeros(z.shape, dtype=float)
    depth[mask] = np.maximum(0.0, geom["start_wse_m"] - z[mask])
    mask = mask & (depth > 0.0)
    depth = np.where(mask, depth, 0.0)
    return mask, depth, info


def search_radius_m_from(geom):
    """Radius stored on the geometry, or the carve default when a caller only has elevations."""
    if geom.get("radius_m"):
        return float(geom["radius_m"])
    # without a storage curve the pool is still limited; 2 km covers a small dam's basin
    return 2000.0


def stage(dem, geom, raw_lines):
    """(spec, depth_m) for this pass's DEM. spec is what the deck's breach file holds,
    plus pool_volume_m3 and the pool array (those two are not written into the file).
    depth_m is the reservoir depth to add to the pass's initial water. The crest must
    already have been raised on `dem`."""
    lines = _lines_of(raw_lines)
    if not lines:
        raise brmod.BreachInputError(
            "Breach inside the terrain: this pass has no crest line to raise, so the notch "
            "was not cut. Draw a crest line that stays on the embankment, or turn this off.")
    rr, cc, along = notch_cells(dem, geom, lines)
    if rr.size == 0:
        raise brmod.BreachInputError(
            "Breach inside the terrain: the crest line does not cross this grid, so the notch "
            "has no cells. The domain has to cover the crest. Turn this off if the crest is "
            "outside the model on purpose.")
    mask, depth, info = pool_on(dem, geom)
    if not mask.any():
        raise brmod.BreachInputError(
            "Breach inside the terrain found no reservoir upstream of the crest at the "
            "starting water surface ({}). The crest may be below that level, or the domain "
            "does not cover the pool. {}".format(
                info.get("reason", "no cells"), "Turn this off, or check the crest line."))
    vol = float(depth[mask].sum(dtype=np.float64)) * float(dem.dx) ** 2
    spec = {
        "version": 1,
        "start_s": float(geom["start_s"]),
        "formation_s": float(geom["formation_s"]),
        "invert_start_m": float(geom["invert_start_m"]),
        "invert_final_m": float(geom["invert_final_m"]),
        "bottom_width_final_m": float(geom["bottom_width_m"]),
        "side_slope_h": float(geom["side_slope_h"]),
        "update_s": UPDATE_S,
        "cells": [[int(r), int(c), float(a)] for r, c, a in zip(rr, cc, along)],
        "pool_mask": "run.pool.asc",
        "pool_volume_m3": vol,
        "_pool": np.where(mask, 1.0, 0.0),
        "_reached_edge": bool(_reaches_edge(dem, mask, geom)),
        "_routed_peak_cfs": float(geom.get("routed_peak_cfs") or 0.0),
        "_storage_at_failure_acft": geom.get("storage_at_failure_acft"),
        "_regression_peaks_cfs": dict(geom.get("regression_peaks_cfs") or {}),
    }
    return spec, depth


def _reaches_edge(dem, mask, geom):
    """True when the pool touches the grid border or the search radius."""
    if not mask.any():
        return False
    if mask[0, :].any() or mask[-1, :].any() or mask[:, 0].any() or mask[:, -1].any():
        return True
    rad = search_radius_m_from(geom)
    xs = dem.x_centers()[None, :]
    ys = dem.y_centers()[:, None]
    dist = np.hypot(xs - geom["dam_xy"][0], ys - geom["dam_xy"][1])
    return bool((mask & (dist >= rad - _EDGE_CELLS * dem.dx)).any())


def add_depth(dem, base, extra):
    """Initial depth with the reservoir added. `base` is a Grid or None."""
    b = np.zeros(dem.data.shape, dtype=float)
    if base is not None:
        raw = np.asarray(base.data, float)
        b = np.where(np.isfinite(raw), raw, 0.0)
    total = b + np.asarray(extra, float)
    return dem.like(np.where(dem.valid(), total, dem.nodata))


def write_deck(workdir, name, dem_local, spec):
    """Write the breach file and the pool mask next to the deck. Returns the .par line.
    Keys the engine does not read stay out of the file."""
    import json
    pool = np.asarray(spec.get("_pool"), float)
    if pool.shape != dem_local.data.shape:
        raise brmod.BreachInputError(
            "the pool mask is {} and this grid is {}".format(pool.shape, dem_local.data.shape))
    mask_name = "{}.pool.asc".format(name)
    dem_local.like(pool).write_asc(os.path.join(workdir, mask_name), fmt="%.0f")
    body = {
        "version": 1,
        "start_s": spec["start_s"],
        "formation_s": spec["formation_s"],
        "invert_start_m": spec["invert_start_m"],
        "invert_final_m": spec["invert_final_m"],
        "bottom_width_final_m": spec["bottom_width_final_m"],
        "side_slope_h": spec["side_slope_h"],
        "update_s": spec["update_s"],
        "cells": spec["cells"],
        "pool_mask": mask_name,
    }
    path = os.path.join(workdir, "{}.breach.json".format(name))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f)
        f.write("\n")
    return "breachfile {}.breach.json".format(name)


def union_bounds(bounds, terrain_src, geom, dam, cell, snap):
    """(bounds, note). The pool's box, plus 5 model cells, snapped outward, unioned into
    `bounds`. `note` is set when the pool reaches the search radius (storage may be cut
    short). A fixed plan does not call this."""
    from .reservoir_carve import carve_cell
    rad = search_radius_m(dam)
    geom = dict(geom)
    geom["radius_m"] = rad
    step = carve_cell(rad)
    x, y = geom["dam_xy"]
    try:
        g = terrain_src.read((x - rad - 2 * step, y - rad - 2 * step,
                              x + rad + 2 * step, y + rad + 2 * step), step)
    except Exception as e:
        return bounds, "Breach inside the terrain: the reservoir extent was not read ({}: {}). " \
            "The domain was not grown to cover it.".format(type(e).__name__, e)
    data = np.array(g.data, copy=True)
    g2 = g.like(data)
    from . import lisflood as lf
    top = geom["top_m"]
    lf.raise_crest(g2, geom["dam_xy"], None, top, line_xy=geom["wall"])
    try:
        mask, _depth, _info = pool_on(g2, geom)
    except brmod.BreachInputError as e:
        return bounds, str(e)
    if not mask.any():
        return bounds, None
    pad = 5.0 * float(snap)
    cols = g2.x_centers()
    rows = g2.y_centers()
    rr, cc = np.nonzero(mask)
    box = (float(cols[cc].min()) - pad, float(rows[rr].min()) - pad,
           float(cols[cc].max()) + pad, float(rows[rr].max()) + pad)
    merged = (min(bounds[0], box[0]), min(bounds[1], box[1]),
              max(bounds[2], box[2]), max(bounds[3], box[3]))
    snapped = tuple(math.floor(v / snap) * snap if i < 2 else math.ceil(v / snap) * snap
                    for i, v in enumerate(merged))
    note = None
    if _reaches_edge(g2, mask, geom):
        note = ("Breach inside the terrain: the reservoir reaches the search radius "
                "({:.0f} m). Storage beyond that is not in the model.").format(rad)
    return snapped, note


def read_pool(path):
    """Peak outflow and time-to-empty from a .pool file, or None when it is missing.
    time-to-empty is the first time the volume falls to 5% of the volume at the start."""
    try:
        text = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return None
    rows = []
    for ln in text[1:]:
        p = ln.split(",")
        if len(p) < 3:
            continue
        try:
            rows.append((float(p[0]), float(p[1]), float(p[2])))
        except ValueError:
            continue
    if not rows:
        return None
    t, v, q = zip(*rows)
    k = int(np.argmax(q))
    v0 = float(v[0])
    empty = None
    if v0 > 0.0:
        # the 5% crossing, interpolated linearly between the rows on either side of it
        level = 0.05 * v0
        for i in range(1, len(t)):
            if t[i] > float(t[0]) and v[i] <= level:
                ta, va, tb, vb = float(t[i - 1]), float(v[i - 1]), float(t[i]), float(v[i])
                empty = tb if va <= level or va == vb else ta + (va - level) / (va - vb) * (tb - ta)
                break
    return {"peak_outflow_cms": float(q[k]), "time_of_peak_s": float(t[k]),
            "volume_start_m3": v0, "volume_end_m3": float(v[-1]),
            "time_to_empty_s": empty}


def report_entry(spec, pool, dx):
    """The run_report block: the in-terrain peak beside the routed peak, the regression
    range (not recomputed), the pool volume, and whether the notch is at least 3 cells."""
    peak_cms = None if pool is None else pool["peak_outflow_cms"]
    peak_cfs = None if peak_cms is None else peak_cms / CFS
    routed = spec.get("_routed_peak_cfs")
    peaks = spec.get("_regression_peaks_cfs") or {}
    vals = [float(v) for v in peaks.values() if v is not None]
    lo = min(vals) if vals else None
    hi = max(vals) if vals else None
    inside = None
    if peak_cfs is not None and lo is not None:
        inside = bool(lo - 1.0 <= peak_cfs <= hi + 1.0)
    width_m = float(spec.get("bottom_width_final_m") or 0.0)
    cells = (width_m / float(dx)) if dx else None
    stor = spec.get("_storage_at_failure_acft")
    vol = spec.get("pool_volume_m3")
    vol_acft = None if vol is None else float(vol) / ACFT
    return {
        "on": True,
        "pool_volume_acft": None if vol_acft is None else round(vol_acft, 2),
        "storage_at_failure_acft": stor,
        "pool_vs_storage": _ratio(vol_acft, stor),
        "peak_outflow_cfs": None if peak_cfs is None else round(peak_cfs, 0),
        "time_of_peak_hr": None if pool is None else round(pool["time_of_peak_s"] / 3600.0, 3),
        "time_to_empty_hr": (None if pool is None or pool["time_to_empty_s"] is None
                             else round(pool["time_to_empty_s"] / 3600.0, 3)),
        "routed_peak_cfs_not_injected": None if not routed else round(float(routed), 0),
        "regression_peak_min_cfs": None if lo is None else round(lo, 0),
        "regression_peak_max_cfs": None if hi is None else round(hi, 0),
        "peak_inside_regression_range": inside,
        "bottom_width_m": round(width_m, 2),
        "bottom_width_cells": None if cells is None else round(cells, 2),
        "resolved": None if cells is None else bool(cells >= 3.0),
        "note": ("The in-terrain outflow includes any flow over the crest from the start, "
                 "which the routed hydrograph does not. The routed peak was computed for the "
                 "report and was not injected."),
    }


def _ratio(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if b <= 0.0:
        return None
    return round(a / b, 3)


def storage_warning(spec):
    """A sentence when the DEM pool and the storage curve differ by more than 25%, else None."""
    vol = spec.get("pool_volume_m3")
    stor = spec.get("_storage_at_failure_acft")
    try:
        vol_ac, stor = float(vol) / ACFT, float(stor)
    except (TypeError, ValueError):
        return None
    if stor <= 0.0 or abs(vol_ac - stor) <= 0.25 * stor:
        return None
    return ("Breach inside the terrain: the reservoir in the DEM holds {:,.0f} ac-ft at the "
            "starting water surface; the storage curve holds {:,.0f} ac-ft at failure "
            "({:+.0f}%). The two are not the same pool. Check the crest line and the storage."
            .format(vol_ac, stor, 100.0 * (vol_ac / stor - 1.0)))


def resolution_warning(spec, dx):
    width = float(spec.get("bottom_width_final_m") or 0.0)
    if dx and width < 3.0 * float(dx):
        return ("Breach inside the terrain: the final breach bottom is {:.1f} m, under 3 cells "
                "of {:.1f} m, so the notch is not resolved. Use a finer cell.".format(width, dx))
    return None
