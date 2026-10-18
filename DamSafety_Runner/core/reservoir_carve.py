"""v3.10.20: the reservoir bottom carved into the DEM from the dam's storage curve (opt-in).

LiDAR does not see below a water surface: where the pool was full when the DEM was flown the
reservoir is a flat sheet a few feet below the top of dam and holds a fraction of the dam's
storage (DWR-3161: 30.2 ac below the top of dam against 203.7 ac on the curve, floor never below
1840.4 ft for a 1844.44 ft top of dam). This lowers the cells under that water surface into a
bowl whose elevation-storage curve matches the plugin's storage law at the top of dam
(docs/Reservoir_carve_from_storage.md).

    mode            off (default) | auto (only when the DEM shows the flat-water problem) | on
    plan_carve()    the footprint, the bowl and the curves on one grid (pure numpy)
    run_hook()      workflow.run_dam: plan, write the carved copy, wrap the terrain sources
    CarvedTerrain   terrain source that subtracts the carve depth (area-averaged) from reads
    carve_tif()     lowers a GeoTIFF (the HEC-RAS terrain) by the carve depth raster

The source DEM is never written. Nothing downstream of the dam is carved.
"""
import math
import os
import re

import numpy as np

from .grid import Grid
from .units import ACFT, ACRE, FT

MODE_OFF, MODE_AUTO, MODE_ON = "off", "auto", "on"
MODES = (MODE_OFF, MODE_AUTO, MODE_ON)
LABELS = ("Off", "Auto (only when the DEM shows a water surface)", "On")
DEFAULT_MODE = MODE_OFF
ENV_MODE = "NDBREACH_RESERVOIR_CARVE"
REPORT_KEY = "reservoir_carve"

WARNING = ("Reservoir bottom carved from the storage curve; the lidar showed a water surface")
FLAT_TOL_M = 0.5 * FT          # cells this close to the footprint's low are the water sheet
FLAT_MIN_FRAC = 0.25           # ... and must be this share of the footprint to count as water
# auto: the site checks' note thresholds (hecras_model.STORAGE_NOTE_*)
AUTO_VOL_RATIO = 0.5
AUTO_AREA_RATIO = 0.5
AUTO_DEPTH_FRAC = 0.5
VOL_TOL = 0.02                 # volume at the top of dam within 2 %
P_MIN, P_MAX, P_FALLBACK = 0.3, 8.0, 2.0
RADIUS_MIN_M, RADIUS_MAX_M = 600.0, 5000.0
MAX_CELLS = 6.0e6
CELL_MIN_M, CELL_MAX_M = 2.0, 30.0
US_SLOPE_DEFAULT = 3.0
CREST_WIDTH_DEFAULT_FT = 20.0


def normalize(v):
    """'off' / 'auto' / 'on' from a setting, an enum index or a label; unknown -> off."""
    if v is None or v == "":
        return DEFAULT_MODE
    if isinstance(v, bool):
        return MODE_ON if v else MODE_OFF
    if isinstance(v, (int, np.integer)):
        return MODES[int(v)] if 0 <= int(v) < len(MODES) else DEFAULT_MODE
    s = str(v).strip().lower()
    for m, lab in zip(MODES, LABELS):
        if s == m or s == lab.lower() or s.startswith(m + " "):
            return m
    if s in ("1", "true", "yes"):
        return MODE_ON
    return DEFAULT_MODE


def env_mode(environ=None):
    return normalize((os.environ if environ is None else environ).get(ENV_MODE, "")) \
        if (os.environ if environ is None else environ).get(ENV_MODE) else DEFAULT_MODE


# ----------------------------------------------------------------------------------------
# the target curve (the same law the breach and the HEC-RAS site checks use)
# ----------------------------------------------------------------------------------------
class Curve(object):
    """V(z) = Vt ((z - bottom)/Ht)^m (m3, m), area dV/dz (m2): breach.Reservoir."""

    def __init__(self, dam):
        from . import breach as brmod
        top = float(dam.top_of_dam_ft) * FT
        self.res = brmod.Reservoir(top - float(dam.dam_height_ft) * FT, top,
                                   float(dam.storage_top_acft) * ACFT,
                                   float(dam.surface_area_top_ac) * ACRE
                                   if dam.surface_area_top_ac else None)
        self.top, self.bottom, self.ht, self.vt, self.m = (self.res.top, self.res.bottom,
                                                          self.res.ht, self.res.vt, self.res.m)

    def volume(self, z):
        return float(self.res.volume(z))

    def area(self, z):
        y = max(0.0, float(z) - self.bottom)
        return self.m * self.vt * y ** (self.m - 1.0) / self.ht ** self.m if y > 0 else 0.0


def dem_volume(z, mask, cell_area, elev):
    """Volume (m3) below `elev` over the cells of `mask`."""
    zz = z[mask]
    return float(np.sum(np.clip(elev - zz[np.isfinite(zz)], 0.0, None))) * cell_area


def dem_area(z, mask, cell_area, elev):
    zz = z[mask]
    return float(np.count_nonzero(np.isfinite(zz) & (zz < elev))) * cell_area


# ----------------------------------------------------------------------------------------
# footprint
# ----------------------------------------------------------------------------------------
def _label4(mask, seed):
    """Cells of `mask` 4-connected to seed (scipy when present, else a queue)."""
    try:
        from scipy import ndimage
        lab, _n = ndimage.label(mask)
        k = lab[seed]
        return (lab == k) if k else np.zeros_like(mask, bool)
    except ImportError:
        from collections import deque
        out = np.zeros_like(mask, bool)
        if not mask[seed]:
            return out
        q = deque([seed])
        out[seed] = True
        ny, nx = mask.shape
        while q:
            r, c = q.popleft()
            for rr, cc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if 0 <= rr < ny and 0 <= cc < nx and mask[rr, cc] and not out[rr, cc]:
                    out[rr, cc] = True
                    q.append((rr, cc))
        return out


def _distance(inside, dx):
    """Euclidean distance (m) of each `inside` cell to the nearest cell that is not."""
    try:
        from scipy import ndimage
        return ndimage.distance_transform_edt(inside) * dx
    except ImportError:                       # chamfer-free fallback: erosion steps
        d = np.zeros(inside.shape, float)
        cur = inside.copy()
        k = 0
        while cur.any():
            k += 1
            d[cur] = k
            e = cur.copy()
            e[1:, :] &= cur[:-1, :]
            e[:-1, :] &= cur[1:, :]
            e[:, 1:] &= cur[:, :-1]
            e[:, :-1] &= cur[:, 1:]
            e[0, :] = e[-1, :] = e[:, 0] = e[:, -1] = False
            cur = e
        return d * dx


def _line_mask(X, Y, line, width):
    """Cells within `width` of the polyline (the DWR dam line as a wall)."""
    m = np.zeros(X.shape, bool)
    pts = np.asarray(line, float)
    for a, b in zip(pts[:-1], pts[1:]):
        ab = b - a
        L2 = float(np.dot(ab, ab))
        if L2 <= 0:
            continue
        t = np.clip(((X - a[0]) * ab[0] + (Y - a[1]) * ab[1]) / L2, 0.0, 1.0)
        m |= np.hypot(X - (a[0] + t * ab[0]), Y - (a[1] + t * ab[1])) <= width
    return m


def footprint(grid, dam_xy, downstream, pool_m, radius_m, crest_line=None, clip=None,
              near_m=None):
    """(mask, info): the DEM-connected ground at or below `pool_m` upstream of the dam
    (opposite `downstream`), within `radius_m` of the dam point, the dam line a wall, clipped
    to `clip` (a bool array on the grid) when given, 4-connected to the lowest such cell next
    to the dam."""
    z = grid.masked()
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    P = np.asarray(dam_xy, float)
    d = np.asarray(downstream, float)
    along = (X - P[0]) * d[0] + (Y - P[1]) * d[1]
    dist = np.hypot(X - P[0], Y - P[1])
    can = np.isfinite(z) & (z <= pool_m) & (along < -0.5 * grid.dx) & (dist <= radius_m)
    if crest_line is not None and len(crest_line) >= 2:
        can &= ~_line_mask(X, Y, crest_line, 0.75 * grid.dx)
    if clip is not None:
        can &= np.asarray(clip, bool)
    near_m = near_m or max(60.0, 10.0 * grid.dx)
    near = can & (dist <= near_m)
    info = {"near_m": round(near_m, 1)}
    if not near.any():
        return np.zeros(z.shape, bool), dict(info, reason="no ground at or below the top of dam "
                                             "within {:.0f} m upstream of the dam point".format(near_m))
    cand = np.argwhere(near)
    seed = tuple(cand[int(np.argmin(z[near]))])
    F = _label4(can, seed)
    info["seed_xy"] = [round(float(X[seed]), 1), round(float(Y[seed]), 1)]
    return F, info


def flat_water(z, F, tol=FLAT_TOL_M, min_frac=FLAT_MIN_FRAC):
    """(W, z_w, frac): the water sheet in footprint F (cells within `tol` of its low end),
    its surface (median) and its share of F; W is None when it is not a sheet."""
    zf = z[F]
    zf = zf[np.isfinite(zf)]
    if zf.size < 20:
        return None, None, 0.0
    lo = float(np.percentile(zf, 1.0))
    W = F & np.isfinite(z) & (z <= lo + tol)
    frac = float(W.sum()) / float(F.sum())
    if frac < min_frac or W.sum() < 20:
        return None, None, frac
    return W, float(np.median(z[W])), frac


# ----------------------------------------------------------------------------------------
# the bowl
# ----------------------------------------------------------------------------------------
def _bowl(z, C, t, z_w, floor, s, p, face):
    """Each carve cell lowered from its own elevation by s (z_w - floor) t^p, no lower than
    the floor or the dam's upstream face, never raised (t = 0 on the shoreline: unchanged)."""
    out = z.copy()
    zc = z[C] - s * (z_w - floor) * np.power(t[C], p)
    zc = np.maximum(np.maximum(zc, face[C]), floor)
    out[C] = np.minimum(z[C], zc)
    return out


def plan_carve(grid, dam, dam_xy, downstream, mode=MODE_ON, crest_line=None, clip=None,
               radius_m=None):
    """The carve on `grid` (elevations in metres, work CRS). Returns a dict:
    applied (bool), why, carved (Grid or None), depth (Grid or None) and `report` (what
    run_report.json records)."""
    mode = normalize(mode)
    cv = Curve(dam)
    a = grid.dx * grid.dx
    top = cv.top
    H = cv.ht
    rad = radius_m or min(max(3.0 * math.sqrt(cv.area(top)), RADIUS_MIN_M), RADIUS_MAX_M)
    z = grid.masked()
    rep = {"mode": mode, "applied": False, "method": None,
           "pool_elev_ft": round(top / FT, 2), "curve": {
               "bottom_ft": round(cv.bottom / FT, 2), "storage_top_acft": round(cv.vt / ACFT, 1),
               "area_top_ac": round(cv.area(top) / ACRE, 1), "exponent_m": round(cv.m, 3),
               "law": "V = Vt ((z - bottom) / H)^m (breach.Reservoir, as the breach and the "
                      "HEC-RAS site checks)"},
           "cell_m": round(grid.dx, 2), "radius_m": round(rad, 0),
           "downstream": [round(float(downstream[0]), 3), round(float(downstream[1]), 3)],
           "dam_line_wall": bool(crest_line is not None and len(crest_line) >= 2),
           "lake_polygon": bool(clip is not None)}
    F, finfo = footprint(grid, dam_xy, downstream, top, rad, crest_line, clip)
    rep["footprint"] = finfo
    out = {"applied": False, "why": "", "carved": None, "depth": None, "report": rep}
    if not F.any():
        out["why"] = rep["why"] = "no reservoir footprint: " + finfo.get("reason", "")
        return out
    rep["footprint_area_ac"] = round(F.sum() * a / ACRE, 2)
    floor_dem = float(np.nanmin(z[F]))
    v0, a0 = dem_volume(z, F, a, top), dem_area(z, F, a, top)
    W, z_w, frac = flat_water(z, F)
    rep["before"] = {"volume_top_acft": round(v0 / ACFT, 1), "area_top_ac": round(a0 / ACRE, 2),
                     "floor_ft": round(floor_dem / FT, 2),
                     "volume_ratio": round(v0 / cv.vt, 3), "area_ratio": round(a0 / cv.area(top), 3),
                     "depth_frac": round((top - floor_dem) / H, 3)}
    rep["flat_water"] = {"found": W is not None, "share_of_footprint": round(frac, 3),
                         "surface_ft": None if z_w is None else round(z_w / FT, 2),
                         "area_ac": None if W is None else round(W.sum() * a / ACRE, 2)}
    problem = [k for k, bad in (("volume", v0 < AUTO_VOL_RATIO * cv.vt),
                                ("area", a0 < AUTO_AREA_RATIO * cv.area(top)),
                                ("floor", (top - floor_dem) < AUTO_DEPTH_FRAC * H)) if bad]
    rep["flat_water_problem"] = bool(W is not None and problem)
    rep["problem"] = problem
    if mode == MODE_OFF:
        out["why"] = rep["why"] = "off"
        return out
    if mode == MODE_AUTO and not rep["flat_water_problem"]:
        out["why"] = rep["why"] = ("auto: the DEM does not show the flat-water problem ({})".format(
            "no flat water surface in the reservoir" if W is None else
            "volume, area and floor agree with the storage curve"))
        return out
    if v0 >= cv.vt * (1.0 - VOL_TOL):
        out["why"] = rep["why"] = ("the DEM already holds {:,.0f} of the curve's {:,.0f} acre-ft "
                                   "at the top of dam".format(v0 / ACFT, cv.vt / ACFT))
        return out
    floor = cv.bottom
    bb = getattr(dam, "breach_bottom_ft", None)
    if bb is not None and float(bb) * FT > floor:
        floor = float(bb) * FT
    C = W if W is not None else F
    zw = z_w if z_w is not None else float(np.nanmedian(z[F]))
    if zw - floor <= 0.05:
        out["why"] = rep["why"] = "the water surface is not above the floor"
        return out
    # distance from the shoreline (the dam side is not shoreline: deepest water at the dam)
    X, Y = np.meshgrid(grid.x_centers(), grid.y_centers())
    P = np.asarray(dam_xy, float)
    dvec = np.asarray(downstream, float)
    along = (X - P[0]) * dvec[0] + (Y - P[1]) * dvec[1]
    damside = (~C) & (along >= -0.5 * grid.dx) & (np.hypot(X - P[0], Y - P[1]) <= rad)
    D = _distance(C | damside, grid.dx)
    D = np.where(C, D, 0.0)
    dmax = float(D[C].max()) if C.any() else 0.0
    if dmax <= grid.dx * 1.01:
        out["why"] = rep["why"] = "the water surface is too narrow to carve at {:.1f} m".format(grid.dx)
        return out
    t = np.clip((D - grid.dx) / (dmax - grid.dx), 0.0, 1.0)
    # the upstream face of the dam stays: ground no lower than it
    us = float(getattr(dam, "us_slope_h", None) or US_SLOPE_DEFAULT)
    cw = float(getattr(dam, "crest_width_ft", None) or CREST_WIDTH_DEFAULT_FT) * FT
    face = top - np.clip(-along - 0.5 * cw, 0.0, None) / us
    vol = lambda zz: dem_volume(zz, F, a, top)
    s, p = 1.0, None
    vmax = vol(_bowl(z, C, t, zw, floor, 1.0, P_MIN, face))
    vmin_p = vol(_bowl(z, C, t, zw, floor, 1.0, P_MAX, face))
    if vmax < cv.vt * (1.0 - VOL_TOL):
        p, how = P_MIN, "footprint too small: the steepest bowl (p {:g}) to the floor".format(P_MIN)
    elif vmin_p > cv.vt:
        p = P_FALLBACK
        lo, hi = 0.0, 1.0
        for _ in range(60):
            s = 0.5 * (lo + hi)
            if vol(_bowl(z, C, t, zw, floor, s, p, face)) > cv.vt:
                hi = s
            else:
                lo = s
        s = 0.5 * (lo + hi)
        how = "depth scale by bisection (p {:g})".format(p)
    else:
        lo, hi = math.log(P_MIN), math.log(P_MAX)
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if vol(_bowl(z, C, t, zw, floor, 1.0, math.exp(mid), face)) > cv.vt:
                lo = mid
            else:
                hi = mid
        p = math.exp(0.5 * (lo + hi))
        how = "power by bisection, bowl to the floor"
    zc = _bowl(z, C, t, zw, floor, s, p, face)
    depth = np.where(np.isfinite(z), z - zc, 0.0)
    depth = np.where(depth > 1e-6, depth, 0.0)
    v1 = vol(zc)
    err = (v1 - cv.vt) / cv.vt
    rows = []
    levels = [cv.bottom + f * H for f in (0.25, 0.5, 0.75)] + [zw, top]
    for e in sorted(set(round(x, 4) for x in levels)):
        rows.append({"elev_ft": round(e / FT, 2),
                     "target_volume_acft": round(cv.volume(e) / ACFT, 1),
                     "dem_volume_acft": round(dem_volume(z, F, a, e) / ACFT, 1),
                     "carved_volume_acft": round(dem_volume(zc, F, a, e) / ACFT, 1),
                     "target_area_ac": round(cv.area(e) / ACRE, 2),
                     "dem_area_ac": round(dem_area(z, F, a, e) / ACRE, 2),
                     "carved_area_ac": round(dem_area(zc, F, a, e) / ACRE, 2)})
    # shoreline: the carve region's cells on its edge against dry ground (t = 0), and the dry
    # banks between the water line and the top of dam - none of them is lowered
    shore = (C & (t <= 0.0)) | (F & ~C)
    rep.update({
        "applied": True, "why": ("auto: flat water and " + ", ".join(problem) + " below the curve"
                                 if mode == MODE_AUTO else "on"),
        "method": ("bowl under the LiDAR water surface: z = z_w - s (z_w - floor) t^p, t = "
                   "distance from the shoreline / its largest value; {}; the shoreline and the "
                   "upstream face of the dam ({:g}H:1V) unchanged".format(how, us)),
        "power_p": round(float(p), 4), "scale_s": round(float(s), 4),
        "water_surface_ft": round(zw / FT, 2), "floor_ft": round(floor / FT, 2),
        "carve_area_ac": round(float(np.count_nonzero(depth > 0)) * a / ACRE, 2),
        "max_carve_depth_ft": round(float(depth.max()) / FT, 2),
        "lowest_carved_ft": round(float(np.nanmin(zc[F])) / FT, 2),
        "carved_volume_acft": round(float(depth.sum()) * a / ACFT, 1),
        "after": {"volume_top_acft": round(v1 / ACFT, 1),
                  "area_top_ac": round(dem_area(zc, F, a, top) / ACRE, 2),
                  "floor_ft": round(float(np.nanmin(zc[F])) / FT, 2),
                  "volume_error_pct": round(100.0 * err, 2)},
        "volume_within_tolerance": abs(err) <= VOL_TOL,
        "curve_table": rows,
        "shoreline_cells": int(np.count_nonzero(shore)),
        "shoreline_cells_unchanged": int(np.count_nonzero(shore & (depth == 0))),
        "cells_lowered_outside_footprint": int(np.count_nonzero((depth > 0) & ~F)),
    })
    rep["note"] = carve_warning(rep)
    out.update(applied=True, why=rep["why"], carved=grid.like(np.where(np.isfinite(z), zc, np.nan)),
               depth=grid.like(depth, nodata=-9999.0))
    out["masks"] = {"footprint": F, "carve": C}
    return out


def carve_warning(rep):
    """The report warning for an applied carve."""
    b, f = rep.get("before") or {}, rep.get("after") or {}
    water = (rep.get("flat_water") or {}).get("found")
    head = WARNING if water else ("Reservoir bottom carved from the storage curve; the DEM "
                                  "reservoir held less than the curve")
    txt = ("{} ({} ft). Footprint {:,.1f} ac upstream of the dam; volume at the top of dam "
           "{:,.0f} acre-ft before, {:,.0f} after, curve {:,.0f} ({:+.1f} %); max carve "
           "{:.1f} ft, floor {:.1f} ft. Files: {}.").format(
        head, rep.get("water_surface_ft"), rep.get("footprint_area_ac") or 0.0,
        b.get("volume_top_acft") or 0.0, f.get("volume_top_acft") or 0.0,
        (rep.get("curve") or {}).get("storage_top_acft") or 0.0, f.get("volume_error_pct") or 0.0,
        rep.get("max_carve_depth_ft") or 0.0, rep.get("floor_ft") or 0.0,
        "_breach_model/<dam>_dem_carved.tif")
    if not rep.get("volume_within_tolerance"):
        txt += (" The footprint cannot hold the storage curve's volume above the floor: the "
                "storage, top of dam or dam height may be off, or the reservoir reaches beyond "
                "the DEM below the top of dam.")
    return txt


# ----------------------------------------------------------------------------------------
# terrain wrapper and rasters
# ----------------------------------------------------------------------------------------
def _overlap(t0, tcell, tn, s0, scell, sn, sign=1.0):
    """[tn, sn] share of each target cell covered by each source cell along one axis
    (sign -1: the axis runs downwards from t0 / s0)."""
    te = t0 + sign * tcell * np.arange(tn + 1)
    se = s0 + sign * scell * np.arange(sn + 1)
    if sign < 0:
        te, se = -te, -se
    lo = np.maximum(te[:-1, None], se[None, :-1])
    hi = np.minimum(te[1:, None], se[None, 1:])
    return np.clip(hi - lo, 0.0, None) / tcell


def resample_mean(src, dst):
    """`src` (Grid, same CRS) area-averaged onto `dst`'s cells; 0 where it does not reach
    (a depth: averaging keeps the volume at any cell size)."""
    data = np.where(np.isfinite(src.data) & (src.data != src.nodata), src.data, 0.0)
    xb0, yb0, xb1, yb1 = src.bounds
    out = np.zeros((dst.ny, dst.nx), float)
    cx = (dst.x0 + dst.dx * np.arange(dst.nx + 1))
    cy = (dst.y0 - dst.dx * np.arange(dst.ny + 1))
    ci = np.nonzero((cx[1:] > xb0) & (cx[:-1] < xb1))[0]
    ri = np.nonzero((cy[1:] < yb1) & (cy[:-1] > yb0))[0]
    if not len(ci) or not len(ri):
        return out
    c0, c1, r0, r1 = ci[0], ci[-1] + 1, ri[0], ri[-1] + 1
    wx = _overlap(dst.x0 + c0 * dst.dx, dst.dx, c1 - c0, src.x0, src.dx, src.nx)
    wy = _overlap(dst.y0 - r0 * dst.dx, dst.dx, r1 - r0, src.y0, src.dx, src.ny, sign=-1.0)
    out[r0:r1, c0:c1] = wy.dot(data).dot(wx.T)
    return out


class CarvedTerrain(object):
    """Terrain source: the inner source's elevations minus the carve depth (m), area-averaged
    onto each read grid. Everything else goes to the inner source."""

    def __init__(self, inner, depth):
        self.inner = inner
        self.carve_depth = depth
        self.z_to_m = getattr(inner, "z_to_m", 1.0)
        self.z_note = getattr(inner, "z_note", "")

    def __getattr__(self, name):
        if name in ("inner", "carve_depth"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def read(self, bounds, cell):
        g = self.inner.read(bounds, cell)
        dep = resample_mean(self.carve_depth, g)
        if not (dep > 0).any():
            return g
        ok = g.valid()
        data = g.data.copy()
        data[ok] = data[ok] - dep[ok]
        return g.like(data)


def safe_name(s):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or "dam")).strip("_") or "dam"


def write_tif(grid, path, crs_wkt):
    """A Float32 GeoTIFF of `grid` (work CRS). Returns the path, or None without GDAL."""
    try:
        from osgeo import gdal, osr
    except ImportError:
        return None
    from ..damsuite import gdalsafe as GS
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(path):
        drv.Delete(path)
    ds = GS.checked(drv.Create(path, grid.nx, grid.ny, 1, gdal.GDT_Float32,
                               options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3"]),
                    "creating {}".format(path))
    ds.SetGeoTransform((grid.x0, grid.dx, 0.0, grid.y0, 0.0, -grid.dx))
    if crs_wkt:
        srs = osr.SpatialReference()
        srs.SetFromUserInput(crs_wkt)
        ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.SetUnitType("metre")
    b.WriteArray(np.where(np.isfinite(grid.data), grid.data, -9999.0).astype(np.float32))
    b.FlushCache()
    ds = None
    return path


def write_png(depth, path, masks=None):
    """Preview of the carve depth (white = none, light to dark blue = deeper; the footprint
    outline grey). Returns the path, or None without GDAL's PNG driver."""
    try:
        from osgeo import gdal
    except ImportError:
        return None
    d = np.asarray(depth.data, float)
    d = np.where(np.isfinite(d) & (d > 0), d, 0.0)
    F = masks.get("footprint") if masks else None
    box = F if (F is not None and F.any()) else (d > 0)
    if box.any():                              # crop to the footprint and a margin
        rr, cc = np.nonzero(box)
        pad = max(10, int(0.1 * max(np.ptp(rr) + 1, np.ptp(cc) + 1)))
        r0, r1 = max(0, rr.min() - pad), min(d.shape[0], rr.max() + pad + 1)
        c0, c1 = max(0, cc.min() - pad), min(d.shape[1], cc.max() + pad + 1)
        d = d[r0:r1, c0:c1]
        F = F[r0:r1, c0:c1] if F is not None else None
    step = max(1, int(math.ceil(max(d.shape) / 1200.0)))
    d = d[::step, ::step]
    t = np.clip(d / (d.max() or 1.0), 0.0, 1.0)
    rgb = np.empty(d.shape + (3,), np.uint8)
    rgb[..., 0] = (235 * (1 - t)).astype(np.uint8)
    rgb[..., 1] = (240 * (1 - t) + 40 * t).astype(np.uint8)
    rgb[..., 2] = (255 - 115 * t).astype(np.uint8)
    rgb[d <= 0] = (255, 255, 255)
    if F is not None:
        F = F[::step, ::step]
        edge = F & ~(np.roll(F, 1, 0) & np.roll(F, -1, 0) & np.roll(F, 1, 1) & np.roll(F, -1, 1))
        rgb[edge] = (110, 110, 110)
    mem = gdal.GetDriverByName("MEM").Create("", d.shape[1], d.shape[0], 3, gdal.GDT_Byte)
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(rgb[..., i])
    drv = gdal.GetDriverByName("PNG")
    if drv is None:
        return None
    drv.CreateCopy(path, mem)
    for ext in (".aux.xml",):
        try:
            os.remove(path + ext)
        except OSError:
            pass
    return path


def carve_tif(tif_path, depth_path, z_scale=1.0):
    """Lowers the GeoTIFF `tif_path` (any CRS; elevations in units of `z_scale` m) by the
    carve depth raster (metres), averaged onto its cells; never raises it. Returns
    {'cells', 'max_m', 'volume_m3'} (volume in the tif's CRS units squared x metres)."""
    from osgeo import gdal
    from ..damsuite import gdalsafe as GS
    ds = GS.checked(gdal.Open(tif_path, gdal.GA_Update), "opening {}".format(tif_path))
    gt = ds.GetGeoTransform()
    nx, ny = ds.RasterXSize, ds.RasterYSize
    opts = gdal.WarpOptions(format="MEM", dstSRS=ds.GetProjection(),
                            outputBounds=(gt[0], gt[3] + ny * gt[5], gt[0] + nx * gt[1], gt[3]),
                            width=nx, height=ny, resampleAlg="average", srcNodata=-9999.0,
                            dstNodata=0.0, outputType=gdal.GDT_Float32)
    w = GS.checked(gdal.Warp("", depth_path, options=opts), "warping {}".format(depth_path))
    dep = w.GetRasterBand(1).ReadAsArray().astype(np.float64)
    dep = np.where(np.isfinite(dep) & (dep > 0), dep, 0.0) / float(z_scale)
    b = ds.GetRasterBand(1)
    nd = b.GetNoDataValue()
    a = b.ReadAsArray().astype(np.float64)
    ok = np.isfinite(a) & (a != nd if nd is not None else True) & (dep > 0)
    a[ok] -= dep[ok]
    b.WriteArray(a.astype(np.float32))
    b.FlushCache()
    ds = None
    return {"cells": int(ok.sum()), "max_m": float(dep.max() * z_scale) if ok.any() else 0.0,
            "volume_m3": float(dep[ok].sum() * z_scale * abs(gt[1] * gt[5]))}


def carve_cell(radius_m):
    return min(max(2.0 * radius_m / math.sqrt(MAX_CELLS), CELL_MIN_M), CELL_MAX_M)


def downstream_for(terrain_src, dam, dam_xy, h_m, top_m, inflow_xy, cell, log=None):
    """(unit vector, how): the downstream side the run already uses for the inflow
    (terrain.downstream_direction on the pool grid, as workflow._downstream_inflow), else the
    direction from the dam point to the inflow point."""
    from . import terrain
    log = log or (lambda m: None)
    half = terrain.POOL_HALF_M
    try:
        pool_grid = terrain_src.read((dam_xy[0] - half, dam_xy[1] - half,
                                      dam_xy[0] + half, dam_xy[1] + half), cell)
        d, how = terrain.downstream_direction(pool_grid, dam_xy, h_m,
                                              crest_line=getattr(dam, "crest_line_xy", None))
    except Exception as e:                    # noqa: BLE001
        d, how = None, "downstream direction failed: {}".format(e)
    if d is None and inflow_xy is not None:
        v = (inflow_xy[0] - dam_xy[0], inflow_xy[1] - dam_xy[1])
        n = math.hypot(*v)
        if n > 1e-6:
            d, how = (v[0] / n, v[1] / n), "towards the breach inflow point"
    return d, how


def run_hook(st, dam, dam_xy, inflow_xy, terrain_src, raw_terrain, model_dir, report, warnings,
             log=None, downstream=None, crs_wkt=None):
    """workflow.run_dam, after the inflow point: plan the carve (RunSettings.reservoir_carve),
    write the carved copy and the depth raster into `model_dir`, record
    report['reservoir_carve'], and return (terrain_src, raw_terrain) wrapped in CarvedTerrain
    when the carve was applied (else unchanged). Never stops the run on its own failure."""
    log = log or (lambda m: None)
    mode = normalize(getattr(st, "reservoir_carve", DEFAULT_MODE))
    if mode == MODE_OFF:
        return terrain_src, raw_terrain
    try:
        h_m, top_m = float(dam.dam_height_ft) * FT, float(dam.top_of_dam_ft) * FT
        how = "the run's downstream side"
        if downstream is None:
            downstream, how = downstream_for(terrain_src, dam, dam_xy, h_m, top_m, inflow_xy,
                                             min(float(getattr(st, "cell_size_m", 10.0)), 20.0), log)
        if downstream is None:
            report[REPORT_KEY] = {"mode": mode, "applied": False,
                                  "why": "the downstream side of the dam is not known ({})".format(how)}
            warnings.append("Reservoir carve not applied: the downstream side of the dam is not "
                            "known ({}).".format(how))
            return terrain_src, raw_terrain
        cv = Curve(dam)
        rad = min(max(3.0 * math.sqrt(cv.area(cv.top)), RADIUS_MIN_M), RADIUS_MAX_M)
        cell = carve_cell(rad)
        g = terrain_src.read((dam_xy[0] - rad - 2 * cell, dam_xy[1] - rad - 2 * cell,
                              dam_xy[0] + rad + 2 * cell, dam_xy[1] + rad + 2 * cell), cell)
        if crs_wkt and not g.crs_wkt:
            g.crs_wkt = crs_wkt
        res = plan_carve(g, dam, dam_xy, downstream, mode,
                         crest_line=getattr(dam, "crest_line_xy", None), radius_m=rad)
        rep = res["report"]
        rep["downstream_how"] = how
        if inflow_xy is not None:
            v = (inflow_xy[0] - dam_xy[0], inflow_xy[1] - dam_xy[1])
            rep["inflow_on_downstream_side"] = bool(v[0] * downstream[0] + v[1] * downstream[1] > 0)
        report[REPORT_KEY] = rep
        if not res["applied"]:
            log("Reservoir carve ({}) not applied: {}".format(mode, res["why"]))
            return terrain_src, raw_terrain
        name = safe_name(getattr(dam, "dam_id", "dam"))
        os.makedirs(model_dir, exist_ok=True)
        wkt = g.crs_wkt or crs_wkt
        files = {}
        for key, grid, fn in (("dem", res["carved"], name + "_dem_carved.tif"),
                              ("depth", res["depth"], name + "_carve_depth.tif")):
            try:
                pth = write_tif(grid, os.path.join(model_dir, fn), wkt)
            except Exception as e:            # noqa: BLE001
                pth = None
                log("Reservoir carve: {} not written ({})".format(fn, e))
            if pth:
                files[key] = fn
        try:
            pth = write_png(res["depth"], os.path.join(model_dir, name + "_carve_depth.png"),
                            res.get("masks"))
            if pth:
                files["png"] = os.path.basename(pth)
        except Exception as e:                # noqa: BLE001
            log("Reservoir carve preview not written ({})".format(e))
        rep["files"] = files
        rep["source_dem_unchanged"] = True
        rep["note"] = carve_warning(rep).replace("_breach_model/<dam>_dem_carved.tif",
                                                 "_breach_model/" + files.get("dem", "(not written)"))
        warnings.insert(0, rep["note"])
        log(rep["note"])
        terrain_src, raw_terrain = wrap_sources(terrain_src, raw_terrain, res["depth"])
    except Exception as e:                    # noqa: BLE001 - the carve is an option
        report[REPORT_KEY] = {"mode": mode, "applied": False,
                              "why": "failed: {}: {}".format(type(e).__name__, e)}
        warnings.append("Reservoir carve failed ({}: {}); the run used the DEM as it is."
                        .format(type(e).__name__, e))
        log(warnings[-1])
    return terrain_src, raw_terrain


def wrap_sources(terrain_src, raw_terrain, depth):
    """(terrain_src, raw_terrain) reading the carved terrain. A ConditionedTerrain keeps its
    type (the run reports its edits): the carve goes under it, so the edits (channels,
    ponds, an embankment) apply on top of the carved ground."""
    from . import conditioning as cond
    if isinstance(terrain_src, cond.ConditionedTerrain):
        if terrain_src.base is raw_terrain:
            terrain_src.base = CarvedTerrain(raw_terrain, depth)
            return terrain_src, terrain_src.base
        terrain_src.base = CarvedTerrain(terrain_src.base, depth)
        return terrain_src, CarvedTerrain(raw_terrain, depth)
    wrapped = CarvedTerrain(terrain_src, depth)
    return wrapped, (wrapped if raw_terrain is terrain_src else CarvedTerrain(raw_terrain, depth))


def hecras_terrain_note(report, model_dir, terrain_tif, z_scale):
    """hecras_model.build_model: lower its terrain GeoTIFF by the run's carve depth raster.
    Returns a note (or '' when the run has no carve)."""
    rc = report.get(REPORT_KEY) or {}
    fn = (rc.get("files") or {}).get("depth")
    if not rc.get("applied") or not fn:
        return ""
    path = fn if os.path.isabs(fn) else os.path.join(model_dir, fn)
    if not os.path.exists(path):
        return ("The run carved the reservoir bottom but its depth raster {} is missing; the "
                "HEC-RAS terrain is the DEM as it is.".format(fn))
    got = carve_tif(terrain_tif, path, z_scale)
    return ("Reservoir bottom carved from the storage curve (the run's {}; the lidar showed a "
            "water surface): the terrain was lowered in {:,} cells, by up to {:.1f} ft.".format(
                fn, got["cells"], got["max_m"] / FT))
