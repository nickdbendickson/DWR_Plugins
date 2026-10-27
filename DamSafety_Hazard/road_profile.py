"""Vendored copy of DamSafety_Runner/core/road_profile.py (Runner 3.10.90, PR #198) for the
Hazard plugin's road / rail crown check (crown.py, Hazard 2.18.65): kept in step with the
Runner's by tests/test_line_crown_check.py (the same results on the same inputs when both
plugins are installed side by side). Only the imports differ: conditioning.densify /
polyline_distance are inlined (_cond below) and gdalsafe is this plugin's damsuite copy.

Road overtopping on the road centreline (DWR-3034 review, Runner 3.10.90).

The ND 5.2 tool used to read a road's overtopping depth from the model's 30 m 'Depth (Max)'
raster: the deepest cell under the road line. A 30 m cell's ground is an average (or the cell's
lowest ground once a structure is cut through the embankment), not the road surface, so a cell
holding a bridge or culvert trench reports the trench's water depth as road overtopping. In
DWR-3034 that read 4.0 ft on Highway 25 where the 1 m road surface is about 20 ft above the
water.

Here the depth is the water surface minus the ROAD SURFACE along the centreline:

* the centreline is densified to about 1 m (STEP_M, metres whatever the CRS unit);
* road surface = the fine DEM (the run's 1 m terrain) at each centreline point;
* water surface = 'WSE (Max)' of the cell under the point, where that cell is wet;
* culverts are NOT masked: over a buried culvert the fine DEM is the road surface, and the
  trench cell's water surface reads dry against it unless the road really is overtopped;
* bridges and notches (cut as open channels, no deck in the bare-earth DEM): the points in
  the cut corridor (structures.Structure.corridor_width, no extra buffer) are read against a
  DECK STAND-IN, the lower of the road surfaces just outside the corridor (the abutments /
  approaches), never against the trench or the bare earth under the removed deck. A point
  whose own road surface is lower than the stand-in but within OPENING_MIN_M of it (a deck in
  the LiDAR, a sag) is read on its own surface, so the stand-in never hides water;
* bare-earth openings (the centreline drops steeply well below the road on both sides, the
  steep side measured over a baseline of several metres scaled by the DEM pixel) take the
  same deck stand-in ONLY where they line up with a mapped bridge or notch (within
  OPENING_MATCH_M of its corridor); any other opening is read as it is (a road sag over a
  coulee looks the same and its overtopping must stay);
* wet points with no road surface (DEM no-data, outside the DEM, a refilled area) or no water
  surface take the depth raster there, so the road depth is never below the raster at them.

The overtopping depth is the largest of those per-point depths (0 when the water stays below
the road; the smallest freeboard is reported as well).

Pure numpy; the callers give the samplers (callables taking an (N, 2) array of x, y)."""
import numpy as np

import math


class _cond:
    """conditioning.densify / polyline_distance of the Runner (core/conditioning.py)."""

    @staticmethod
    def polyline_distance(px, py, line):
        line = np.asarray(line, dtype=float)
        px = np.asarray(px, dtype=float)
        py = np.asarray(py, dtype=float)
        best = np.full(px.shape, np.inf)
        along = np.zeros(px.shape)
        s0 = 0.0
        for i in range(len(line) - 1):
            ax, ay = line[i]
            bx, by = line[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            seg = math.sqrt(seg2)
            if seg2 == 0:
                continue
            t = np.clip(((px - ax) * dx + (py - ay) * dy) / seg2, 0.0, 1.0)
            d = np.hypot(px - (ax + t * dx), py - (ay + t * dy))
            better = d < best
            best = np.where(better, d, best)
            along = np.where(better, s0 + t * seg, along)
            s0 += seg
        return best, along

    @staticmethod
    def densify(line, spacing):
        line = np.asarray(line, dtype=float)
        if len(line) < 2:
            return line.copy()
        seg = np.hypot(*np.diff(line, axis=0).T)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        n = max(2, int(math.ceil(s[-1] / spacing)) + 1)
        ss = np.linspace(0.0, s[-1], n)
        return np.column_stack([np.interp(ss, s, line[:, 0]), np.interp(ss, s, line[:, 1])])


cond = _cond

FT = 0.3048
STEP_M = 1.0                 # centreline sample spacing (m)
CUT_BUFFER_M = 0.0           # 3.10.90 review: no buffer beyond the cut corridor
OPENING_MIN_M = 1.5          # an opening is at least this far below the road on both sides
OPENING_SHOULDER_M = 40.0    # ... looking this far along the road each way
OPENING_GRADE = 0.2          # ... with a side steeper than this (roads stay under ~10 %)
OPENING_PAD_M = 5.0          # mask this much more on each side of an opening
OPENING_BASELINE_M = 5.0     # the steep side is measured over at least this (and 2 DEM pixels)
OPENING_MATCH_M = 15.0       # an opening counts only this close to a bridge / notch corridor
STANDIN_REACH_M = 3.0        # the deck stand-in: lowest road surface this far outside the run
STANDIN_GRADE_BASE_M = 10.0  # ... and the approach's grade over this, carried into a sag
STANDIN_GRADE_MAX = 0.10     # ... at most (roads stay under ~10 %)

MASK_KINDS = ("bridge", "notch")     # cut as open channels with no deck in the bare-earth DEM

BASIS_CENTRELINE = "centreline"
BASIS_RASTER = "raster"


def densify(line, step=STEP_M):
    return cond.densify(np.asarray(line, dtype=float), step)


def _get(s):
    return (lambda k, d=None: s.get(k, d)) if isinstance(s, dict) else \
        (lambda k, d=None: getattr(s, k, d))


def cut_corridors(structs, cell_m, buffer_m=CUT_BUFFER_M, unit_m=1.0, kinds=MASK_KINDS):
    """[(line, half_width, name)] from structures.json-style dicts (or Structure objects) of
    the `kinds` given (bridges and notches: culverts are buried, so the fine DEM is the road
    over them). half_width = the cut corridor's (as structures.Structure.corridor_width, with
    the model cell `cell_m` in metres) plus `buffer_m`, in the line's units (metres /
    `unit_m`, the metres per CRS unit of the lines)."""
    out = []
    u = float(unit_m or 1.0)
    for s in structs or []:
        get = _get(s)
        kind = str(get("kind", "") or "").lower()
        if kinds is not None and kind not in kinds:
            continue
        try:
            line = np.asarray(get("line"), dtype=float)
            if line.ndim != 2 or len(line) < 1 or line.shape[1] < 2:
                continue
            line = line[:, :2]
            if not np.all(np.isfinite(line)):
                continue
            w = float(get("width_m", 1.0) or 1.0) * max(1, int(get("barrels", 1) or 1)) * 1.1
            half = 0.5 * max(float(cell_m or 0.0), w)
        except (TypeError, ValueError):
            continue
        out.append((line, (half + float(buffer_m or 0.0)) / u,
                    str(get("name", "") or get("kind", ""))))
    return out


def corridor_gap(pts, cuts):
    """Per point: the distance outside the nearest cut corridor (<= 0 inside it), in the
    points' units; +inf with no corridor near."""
    pts = np.asarray(pts, dtype=float)
    out = np.full(len(pts), np.inf)
    if not len(pts):
        return out
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    for line, half, _n in cuts:
        reach = half + OPENING_MATCH_M * 10.0     # bbox prefilter only (generous)
        if (line[:, 0].min() - reach > hi[0] or line[:, 0].max() + reach < lo[0] or
                line[:, 1].min() - reach > hi[1] or line[:, 1].max() + reach < lo[1]):
            continue
        if len(line) == 1:
            d = np.hypot(pts[:, 0] - line[0, 0], pts[:, 1] - line[0, 1])
        else:
            d, _s = cond.polyline_distance(pts[:, 0], pts[:, 1], line)
        out = np.minimum(out, d - half)
    return out


def in_cuts(pts, cuts):
    """Boolean per point: inside any cut corridor."""
    return corridor_gap(pts, cuts) <= 0.0


def _run_max(z, w, side):
    """Max of z over the w samples before (side -1) or after (+1) each sample, NaN-aware;
    -inf where there are none."""
    n = len(z)
    zz = np.where(np.isfinite(z), z, -np.inf)
    out = np.full(n, -np.inf)
    for k in range(1, min(w, n - 1) + 1):
        if side < 0:
            out[k:] = np.maximum(out[k:], zz[:n - k])
        else:
            out[:n - k] = np.maximum(out[:n - k], zz[k:])
    return out


def _grade(s, z, baseline_m=None):
    """Per sample, the steepest grade touching it: over one step (baseline_m None, the
    3.10.90 first version) or over every baseline of `baseline_m` that spans it (pixel steps,
    LiDAR noise and seams average out over several metres; the road depth and core/bridge_snap
    pass OPENING_BASELINE_M or more)."""
    n = len(z)
    step = float(np.median(np.diff(s))) or 1.0
    if not baseline_m:
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.abs(np.diff(z)) / np.maximum(np.diff(s), 1e-9)
        return np.where(np.isfinite(g), g, 0.0)
    b = max(1, int(round(float(baseline_m) / step)))
    out = np.zeros(n)
    if n <= b:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        gb = np.abs(z[b:] - z[:-b]) / np.maximum(s[b:] - s[:-b], 1e-9)
    gb = np.where(np.isfinite(gb), gb, 0.0)
    for o in range(b + 1):
        out[o:o + len(gb)] = np.maximum(out[o:o + len(gb)], gb)
    return out


def openings(s, z, min_m=OPENING_MIN_M, shoulder_m=OPENING_SHOULDER_M, grade=OPENING_GRADE,
             pad_m=OPENING_PAD_M, baseline_m=None):
    """Bare-earth openings along a centreline profile (s along-road distance, z ground, both m,
    evenly spaced). Returns (mask, runs): mask per sample, runs = [(i_low, depth_m, width_m)]
    where depth is below the lower shoulder. An opening: z at least min_m below the highest
    ground within shoulder_m on BOTH sides, with a side steeper than `grade` (a road sag is
    gentle; an opening under a removed deck is steep). `baseline_m`: measure the steep side over
    that many metres (the road-depth reading passes max(OPENING_BASELINE_M, 2 DEM pixels));
    None keeps the single-step grade."""
    s = np.asarray(s, dtype=float)
    z = np.asarray(z, dtype=float)
    n = len(z)
    mask = np.zeros(n, dtype=bool)
    if n < 3:
        return mask, []
    step = float(np.median(np.diff(s))) or 1.0
    w = max(1, int(round(shoulder_m / step)))
    lo = np.minimum(_run_max(z, w, -1), _run_max(z, w, +1))
    depth = lo - z
    cand = np.isfinite(depth) & (depth >= min_m)
    g = _grade(s, z, baseline_m)
    runs = []
    pad = max(0, int(round(pad_m / step)))
    look = max(1, int(round(max(10.0, float(baseline_m or 0.0)) / step)))
    i = 0
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and cand[j + 1]:
            j += 1
        a, b = max(0, i - look), min(n - 1, j + look)
        if baseline_m:
            steep = float(np.max(g[a:b + 1])) > grade
        else:                                   # as the 3.10.90 first draft had it
            steep = b > a and float(np.max(g[a:b])) > grade
        if steep:
            dd = depth[i:j + 1]
            deep = np.flatnonzero(dd >= np.nanmax(dd) - 0.1)    # a flat bottom: its middle
            k = i + int(deep[len(deep) // 2])
            mask[max(0, i - pad):min(n, j + pad + 1)] = True
            runs.append((k, float(depth[k]), float(s[j] - s[i] + step)))
        i = j + 1
    return mask, runs


def _segments(mask):
    """[(i, j)] inclusive index ranges of the True runs of `mask`."""
    out = []
    n = len(mask)
    i = 0
    while i < n:
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and mask[j + 1]:
            j += 1
        out.append((i, j))
        i = j + 1
    return out


def _approach(s, z, sel_edge, sel_grade, s_edge, away):
    """(surface, rise) of one approach of a masked run: the lowest surface of `sel_edge` (just
    outside the run), and the road's rise going AWAY from the run (least squares over
    `sel_grade`), clipped to [0, STANDIN_GRADE_MAX]; (None, 0) without a surface."""
    v = z[sel_edge]
    v = v[np.isfinite(v)]
    if not len(v):
        return None, 0.0
    zz, ss = z[sel_grade], s[sel_grade]
    ok = np.isfinite(zz)
    rise = 0.0
    if ok.sum() >= 3 and np.ptp(ss[ok]) > 0:
        try:
            rise = float(np.polyfit((ss[ok] - s_edge) * away, zz[ok], 1)[0])
        except (ValueError, np.linalg.LinAlgError):
            rise = 0.0
    if not np.isfinite(rise):
        rise = 0.0
    return float(v.min()), float(np.clip(rise, 0.0, STANDIN_GRADE_MAX))


def deck_standin(s, z, mask, reach_m=STANDIN_REACH_M, grade_base_m=STANDIN_GRADE_BASE_M):
    """Per sample of each masked run, the deck stand-in: the road surface of the LOWER approach
    (the lowest unmasked surface within `reach_m` of each end), and, where the road rises away
    from the run on a side (a sag), that side's grade carried into the run (at most
    STANDIN_GRADE_MAX), so the stand-in is never above either approach and never hides water;
    NaN where neither side has a surface. s in metres along the road, z road surface (m)."""
    s = np.asarray(s, dtype=float)
    z = np.asarray(z, dtype=float)
    out = np.full(len(z), np.nan)
    idx = np.arange(len(z))
    free = ~np.asarray(mask, dtype=bool)
    for i, j in _segments(mask):
        lines = []
        for side, edge_i, away in ((idx < i, i - 1, -1.0), (idx > j, j + 1, +1.0)):
            if edge_i < 0 or edge_i >= len(z) or not free[edge_i]:
                continue
            e = s[edge_i]
            dist = (s - e) * away
            near = side & free & (dist >= -1e-9) & (dist <= reach_m + 1e-9)
            span = side & free & (dist >= -1e-9) & (dist <= grade_base_m + 1e-9)
            zz, rise = _approach(s, z, near, span, e, away)
            if zz is not None:
                lines.append((zz, rise, e))
        if not lines:
            continue
        ss = s[i:j + 1]
        low = min(zz for zz, _r, _e in lines)
        # each approach's line falls into the run at its rise away from it; the stand-in is the
        # higher line (a V across a sag), capped at the lower approach
        cand = np.max([zz - r * np.abs(ss - e) for zz, r, e in lines], axis=0)
        out[i:j + 1] = np.minimum(low, cand)
    return out


def _empty(n_wet=0, how="road centreline: no wet point"):
    return {"depth_ft": 0.0, "x": None, "y": None, "freeboard_ft": None, "n_wet": n_wet,
            "n_cut": 0, "n_opening": 0, "n_nodem": 0, "n_nowse": 0, "n_no_approach": 0,
            "n_opening_unmatched": 0, "raster_points_ft": None, "how": how}


def road_crown_depth(parts, wse_ft, ground_m, wet=None, cuts=(), step=STEP_M,
                     mask_openings=True, depth_ft=None, unit_m=1.0, pixel_m=None):
    """Overtopping depth of one road over its centreline.

    parts: polylines (N, 2) in the raster CRS (`unit_m` metres per unit). wse_ft(xy) -> water
    surface (ft; NaN dry or no data). ground_m(xy) -> road surface (m; NaN no data). wet(xy) ->
    bool, the cell is wet (default: WSE finite). depth_ft(xy) -> the depth raster (ft), used at
    wet points with no road surface or no water surface. cuts: cut_corridors(...) of the
    bridges and notches, in the parts' units. pixel_m: the DEM pixel (m; default
    ground_m.pixel_m, else 1).
    Returns None when no wet centreline point has both a water surface and a road surface, or
    when a wet point has neither and no depth_ft is given (the caller keeps the raster depth);
    else a dict: depth_ft (>= 0), x, y (deepest point, or the least freeboard), freeboard_ft
    (min road surface - WSE over the points read on a surface; negative when overtopped),
    raster_points_ft (deepest raster depth at the points with no surface / WSE, or None),
    n_wet, n_cut (on a bridge / notch deck stand-in), n_opening (on a mapped opening's
    stand-in), n_opening_unmatched (detected openings with no mapped bridge / notch: read as
    they are), n_nodem, n_nowse, n_no_approach (in a cut / opening with no approach surface on
    the line, e.g. a road layer split at the bridge: the raster depth there), how."""
    u = float(unit_m or 1.0)
    px = float(pixel_m or getattr(ground_m, "pixel_m", None) or 1.0)
    base_m = max(OPENING_BASELINE_M, 2.0 * px)
    reach_m = max(STANDIN_REACH_M, 2.0 * px)
    best = rbest = None
    n_wet = n_cut = n_open = n_unm = n_nodem = n_nowse = n_noap = n_used = 0
    unresolved = False
    for part in parts:
        pts = densify(part, step / u)
        if len(pts) < 1:
            continue
        w = np.asarray(wse_ft(pts), dtype=float)
        has_w = np.isfinite(w)
        wt = has_w.copy() if wet is None else np.asarray(wet(pts), dtype=bool)
        if not wt.any():
            continue
        n_wet += int(wt.sum())
        g = np.asarray(ground_m(pts), dtype=float)
        s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]) * u \
            if len(pts) > 1 else np.zeros(1)
        gap = corridor_gap(pts, cuts) if cuts else np.full(len(pts), np.inf)
        zc = gap <= 0.0
        op = np.zeros(len(pts), dtype=bool)
        if mask_openings and len(pts) >= 3:
            m_all, _r = openings(s, g, baseline_m=base_m)
            for i, j in _segments(m_all):
                if float(np.min(gap[i:j + 1])) <= OPENING_MATCH_M / u:
                    op[i:j + 1] = True
                elif wt[i:j + 1].any():
                    n_unm += 1
        masked = zc | op
        surf = g.copy()
        noap = np.zeros(len(pts), dtype=bool)
        if masked.any():
            stand = deck_standin(s, g, masked, reach_m, max(STANDIN_GRADE_BASE_M, 3.0 * px))
            own = np.isfinite(g) & (g >= stand - OPENING_MIN_M)
            surf = np.where(masked, np.where(own, np.minimum(g, stand), stand), g)
            noap = masked & ~np.isfinite(stand)
        has_s = np.isfinite(surf)
        n_cut += int((wt & zc).sum())
        n_open += int((wt & op & ~zc).sum())
        use = wt & has_w & has_s
        n_noap += int((wt & has_w & ~has_s & noap).sum())
        n_nodem += int((wt & has_w & ~has_s & ~noap).sum())
        n_nowse += int((wt & ~has_w).sum())
        ras = wt & ~use
        if use.any():
            n_used += int(use.sum())
            d = w[use] - surf[use] / FT
            k = int(np.argmax(d))
            if best is None or d[k] > best[0]:
                best = (float(d[k]), float(pts[use][k, 0]), float(pts[use][k, 1]))
        if ras.any():
            if depth_ft is None:
                unresolved = True
                continue
            dr = np.nan_to_num(np.asarray(depth_ft(pts[ras]), dtype=float))
            k = int(np.argmax(dr))
            if rbest is None or dr[k] > rbest[0]:
                rbest = (float(dr[k]), float(pts[ras][k, 0]), float(pts[ras][k, 1]))
    if n_wet == 0:
        return _empty()
    if best is None or unresolved:
        return None
    depth, x, y = best
    if rbest is not None and rbest[0] > depth:
        depth, x, y = rbest
    return {"depth_ft": max(0.0, depth), "x": x, "y": y, "freeboard_ft": -best[0],
            # Hazard addition (not in the Runner copy): where the least freeboard is
            "best_x": best[1], "best_y": best[2],
            "n_wet": n_wet, "n_cut": n_cut, "n_opening": n_open, "n_nodem": n_nodem,
            "n_nowse": n_nowse, "n_no_approach": n_noap, "n_opening_unmatched": n_unm,
            "raster_points_ft": None if rbest is None else rbest[0],
            "how": "road centreline: WSE - road surface ({:g} m steps, {} wet point(s) on a "
                   "road surface: {} on a bridge / notch deck stand-in, {} on a mapped "
                   "opening's stand-in; {} from the depth raster (no road surface {}, no "
                   "water surface {}, in a cut with no approach on the line {}); {} bare-earth "
                   "opening(s) with no mapped bridge read as they are)".format(
                       step, n_used, n_cut, n_open, n_nodem + n_nowse + n_noap, n_nodem,
                       n_nowse, n_noap, n_unm)}


class PointDEM:
    """Point reads of a DEM file (GDAL) in metres, from points in another CRS (wkt or
    'EPSG:n'). Windows of `tile` pixels are cached. Nearest pixel. A block that cannot be read
    raises OSError / RuntimeError (the caller falls back to the depth raster)."""

    def __init__(self, path, z_to_m=1.0, points_crs=None, tile=512):
        from osgeo import osr
        from .damsuite import gdalsafe as GS
        self.ds = GS.open_raster(path)        # raises with GDAL's reason; no process-wide switch
        if self.ds is None:
            raise OSError("cannot open " + path)
        self.path = path
        self.band = self.ds.GetRasterBand(1)
        self.nodata = self.band.GetNoDataValue()
        self.gt = self.ds.GetGeoTransform()
        self.z = float(z_to_m or 1.0)
        self.tile = int(tile)
        self.cache = {}
        self.tf = None
        dst = osr.SpatialReference()
        dst.ImportFromWkt(self.ds.GetProjection() or "")
        self.srs = dst
        if points_crs:
            src = osr.SpatialReference()
            if str(points_crs).upper().startswith("EPSG:"):
                src.ImportFromEPSG(int(str(points_crs).split(":")[1]))
            else:
                src.SetFromUserInput(str(points_crs))
            for sr in (src, dst):
                try:
                    sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                except AttributeError:
                    pass
            if (self.ds.GetProjection() or "") and not src.IsSame(dst):
                self.tf = osr.CoordinateTransformation(src, dst)

    @property
    def cell_m(self):
        return abs(self.gt[1])

    @property
    def pixel_m(self):
        """The pixel size in metres (the CRS unit converted; degrees ~ 111 km)."""
        try:
            if self.srs.IsProjected():
                return abs(self.gt[1]) * float(self.srs.GetLinearUnits() or 1.0)
            if self.srs.IsGeographic():
                return abs(self.gt[1]) * 111320.0
        except Exception:
            pass
        return abs(self.gt[1])

    def _block(self, br, bc):
        key = (br, bc)
        if key not in self.cache:
            if len(self.cache) > 64:
                self.cache.clear()
            r0, c0 = br * self.tile, bc * self.tile
            nr = min(self.tile, self.ds.RasterYSize - r0)
            nc = min(self.tile, self.ds.RasterXSize - c0)
            a = self.band.ReadAsArray(c0, r0, nc, nr)
            if a is None:
                raise OSError("cannot read the road surface DEM block {} {}".format(r0, c0))
            a = a.astype(np.float64)
            if self.nodata is not None:
                a[a == self.nodata] = np.nan
            self.cache[key] = a * self.z
        return self.cache[key]

    def __call__(self, xy):
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        if self.tf is not None and len(xy):
            xy = np.asarray([p[:2] for p in self.tf.TransformPoints(xy[:, :2].tolist())])
        gt = self.gt
        out = np.full(len(xy), np.nan)
        fin = np.all(np.isfinite(xy), axis=1)
        c = np.floor(np.where(fin, (xy[:, 0] - gt[0]) / gt[1], -1)).astype(int)
        r = np.floor(np.where(fin, (xy[:, 1] - gt[3]) / gt[5], -1)).astype(int)
        ok = fin & (r >= 0) & (c >= 0) & (r < self.ds.RasterYSize) & (c < self.ds.RasterXSize)
        if not ok.any():
            return out
        br, bc = r // self.tile, c // self.tile
        for key in set(zip(br[ok].tolist(), bc[ok].tolist())):
            sel = ok & (br == key[0]) & (bc == key[1])
            a = self._block(*key)
            out[sel] = a[r[sel] - key[0] * self.tile, c[sel] - key[1] * self.tile]
        return out


def grid_sampler(grid, arr):
    """Nearest-cell values of an array on `grid` at (N, 2) points; NaN outside."""
    a = np.asarray(arr, dtype=float)

    def f(xy):
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        c = np.floor((xy[:, 0] - grid.x0) / grid.dx).astype(int)
        r = np.floor((grid.y0 - xy[:, 1]) / grid.dx).astype(int)
        ok = (r >= 0) & (c >= 0) & (r < a.shape[0]) & (c < a.shape[1])
        out = np.full(len(xy), np.nan)
        out[ok] = a[r[ok], c[ok]]
        return out
    return f
