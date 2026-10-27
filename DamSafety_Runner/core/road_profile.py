"""Road overtopping on the road centreline (DWR-3034 review, Runner 3.10.90).

The ND 5.2 tool used to read a road's overtopping depth from the model's 30 m 'Depth (Max)'
raster: the deepest cell under the road line. A 30 m cell's ground is an average (or the cell's
lowest ground once a structure is cut through the embankment), not the road surface, so a cell
holding a bridge or culvert trench reports the trench's water depth as road overtopping. In
DWR-3034 that read 4.0 ft on Highway 25 where the 1 m road surface is about 20 ft above the
water.

Here the depth is the water surface minus the ROAD SURFACE along the centreline:

* the centreline is densified to about 1 m;
* road surface = the fine DEM (the run's 1 m terrain) at each centreline point;
* water surface = 'WSE (Max)' of the cell under the point, where that cell is wet;
* points inside a structure cut (the cut corridor from _breach_model/structures.json plus a
  buffer) are left out: water there is flow through the structure, not over the road;
* points in a bare-earth opening (the LiDAR has no bridge deck: the centreline drops steeply
  well below the road on both sides) are left out for the same reason.

The overtopping depth is the largest WSE - road surface over the remaining wet points (0 when
the water stays below the road; the smallest freeboard is reported as well).

Pure numpy; the callers give the samplers (callables taking an (N, 2) array of x, y)."""
import numpy as np

from . import conditioning as cond

FT = 0.3048
STEP_M = 1.0                 # centreline sample spacing
CUT_BUFFER_M = 15.0          # beyond the cut corridor's half width
OPENING_MIN_M = 1.5          # an opening is at least this far below the road on both sides
OPENING_SHOULDER_M = 40.0    # ... looking this far along the road each way
OPENING_GRADE = 0.2          # ... with a side steeper than this (roads stay under ~10 %)
OPENING_PAD_M = 5.0          # mask this much more on each side of an opening

BASIS_CENTRELINE = "centreline"
BASIS_RASTER = "raster"


def densify(line, step=STEP_M):
    return cond.densify(np.asarray(line, dtype=float), step)


def cut_corridors(structs, cell_m, buffer_m=CUT_BUFFER_M):
    """[(line, half_width_m, name)] from structures.json-style dicts (or Structure objects):
    the cut corridor (as structures.Structure.corridor_width) plus `buffer_m`."""
    out = []
    for s in structs or []:
        get = (lambda k, d=None: s.get(k, d)) if isinstance(s, dict) else \
            (lambda k, d=None: getattr(s, k, d))
        try:
            line = np.asarray(get("line"), dtype=float)
            if line.ndim != 2 or len(line) < 1:
                continue
            w = float(get("width_m", 1.0) or 1.0) * max(1, int(get("barrels", 1) or 1)) * 1.1
            half = 0.5 * max(float(cell_m or 0.0), w)
        except (TypeError, ValueError):
            continue
        out.append((line, half + float(buffer_m), str(get("name", "") or get("kind", ""))))
    return out


def in_cuts(pts, cuts):
    """Boolean per point: inside any cut corridor."""
    pts = np.asarray(pts, dtype=float)
    m = np.zeros(len(pts), dtype=bool)
    for line, half, _n in cuts:
        if len(line) == 1:
            d = np.hypot(pts[:, 0] - line[0, 0], pts[:, 1] - line[0, 1])
        else:
            d, _s = cond.polyline_distance(pts[:, 0], pts[:, 1], line)
        m |= d <= half
    return m


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


def openings(s, z, min_m=OPENING_MIN_M, shoulder_m=OPENING_SHOULDER_M, grade=OPENING_GRADE,
             pad_m=OPENING_PAD_M):
    """Bare-earth openings along a centreline profile (s along-road distance, z ground, both m,
    evenly spaced). Returns (mask, runs): mask per sample, runs = [(i_low, depth_m, width_m)]
    where depth is below the lower shoulder. An opening: z at least min_m below the highest
    ground within shoulder_m on BOTH sides, with a side steeper than `grade` (a road sag is
    gentle; an opening under a removed deck is steep)."""
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
    with np.errstate(invalid="ignore", divide="ignore"):
        g = np.abs(np.diff(z)) / np.maximum(np.diff(s), 1e-9)
    g = np.where(np.isfinite(g), g, 0.0)
    runs = []
    pad = max(0, int(round(pad_m / step)))
    look = max(1, int(round(10.0 / step)))
    i = 0
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and cand[j + 1]:
            j += 1
        a, b = max(0, i - look), min(n - 1, j + look)
        if b > a and float(np.max(g[a:b])) > grade:
            dd = depth[i:j + 1]
            deep = np.flatnonzero(dd >= np.nanmax(dd) - 0.1)    # a flat bottom: its middle
            k = i + int(deep[len(deep) // 2])
            mask[max(0, i - pad):min(n, j + pad + 1)] = True
            runs.append((k, float(depth[k]), float(s[j] - s[i] + step)))
        i = j + 1
    return mask, runs


def road_crown_depth(parts, wse_ft, ground_m, wet=None, cuts=(), step=STEP_M,
                     mask_openings=True):
    """Overtopping depth of one road over its centreline.

    parts: polylines (N, 2) in the raster CRS. wse_ft(xy) -> water surface (ft; NaN dry or no
    data). ground_m(xy) -> road surface (m; NaN no data). wet(xy) -> bool, the cell is wet
    (default: WSE finite). cuts: cut_corridors(...).
    Returns None when no wet centreline point has a road surface (the caller keeps the raster
    depth), else a dict: depth_ft (>= 0), x, y (deepest point, or the least freeboard),
    freeboard_ft (min road surface - WSE over the wet points; negative when overtopped),
    n_wet, n_cut, n_opening, n_nodem, how."""
    best = None
    n_wet = n_cut = n_open = n_nodem = n_used = 0
    for part in parts:
        pts = densify(part, step)
        if len(pts) < 1:
            continue
        w = np.asarray(wse_ft(pts), dtype=float)
        wt = np.isfinite(w) if wet is None else (np.asarray(wet(pts), dtype=bool)
                                                 & np.isfinite(w))
        if not wt.any():
            continue
        n_wet += int(wt.sum())
        zc = in_cuts(pts, cuts) if cuts else np.zeros(len(pts), dtype=bool)
        g = np.asarray(ground_m(pts), dtype=float)
        op = np.zeros(len(pts), dtype=bool)
        if mask_openings and len(pts) >= 3:
            s = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
            op, _r = openings(s, g)
        n_cut += int((wt & zc).sum())
        n_open += int((wt & ~zc & op).sum())
        n_nodem += int((wt & ~zc & ~op & ~np.isfinite(g)).sum())
        use = wt & ~zc & ~op & np.isfinite(g)
        if not use.any():
            continue
        n_used += int(use.sum())
        d = w[use] - g[use] / FT
        k = int(np.argmax(d))
        if best is None or d[k] > best[0]:
            best = (float(d[k]), float(pts[use][k, 0]), float(pts[use][k, 1]))
    if n_wet == 0:
        return {"depth_ft": 0.0, "x": None, "y": None, "freeboard_ft": None, "n_wet": 0,
                "n_cut": 0, "n_opening": 0, "n_nodem": 0,
                "how": "road centreline: no wet point"}
    if best is None:
        if n_nodem:
            return None
        return {"depth_ft": 0.0, "x": None, "y": None, "freeboard_ft": None, "n_wet": n_wet,
                "n_cut": n_cut, "n_opening": n_open, "n_nodem": n_nodem,
                "how": "road centreline: wet only in structure cuts or bare-earth openings"}
    return {"depth_ft": max(0.0, best[0]), "x": best[1], "y": best[2],
            "freeboard_ft": -best[0], "n_wet": n_wet, "n_cut": n_cut, "n_opening": n_open,
            "n_nodem": n_nodem,
            "how": "road centreline: WSE - road surface ({:g} m DEM, {} wet point(s), {} in "
                   "structure cuts and {} in bare-earth openings left out)".format(
                       step, n_used, n_cut, n_open)}


class PointDEM:
    """Point reads of a DEM file (GDAL) in metres, from points in another CRS (wkt or
    'EPSG:n'). Windows of `tile` pixels are cached. Nearest pixel."""

    def __init__(self, path, z_to_m=1.0, points_crs=None, tile=512):
        from osgeo import osr
        from ..damsuite import gdalsafe as GS
        self.ds = GS.open_raster(path)        # raises with GDAL's reason; no process-wide switch
        if self.ds is None:
            raise OSError("cannot open " + path)
        self.band = self.ds.GetRasterBand(1)
        self.nodata = self.band.GetNoDataValue()
        self.gt = self.ds.GetGeoTransform()
        self.z = float(z_to_m or 1.0)
        self.tile = int(tile)
        self.cache = {}
        self.tf = None
        if points_crs:
            src = osr.SpatialReference()
            if str(points_crs).upper().startswith("EPSG:"):
                src.ImportFromEPSG(int(str(points_crs).split(":")[1]))
            else:
                src.ImportFromWkt(points_crs)
            dst = osr.SpatialReference()
            dst.ImportFromWkt(self.ds.GetProjection() or "")
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
        xy = np.asarray(xy, dtype=float)
        if self.tf is not None and len(xy):
            xy = np.asarray([p[:2] for p in self.tf.TransformPoints(xy[:, :2].tolist())])
        gt = self.gt
        c = np.floor((xy[:, 0] - gt[0]) / gt[1]).astype(int)
        r = np.floor((xy[:, 1] - gt[3]) / gt[5]).astype(int)
        out = np.full(len(xy), np.nan)
        ok = (r >= 0) & (c >= 0) & (r < self.ds.RasterYSize) & (c < self.ds.RasterXSize)
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
        xy = np.asarray(xy, dtype=float)
        c = np.floor((xy[:, 0] - grid.x0) / grid.dx).astype(int)
        r = np.floor((grid.y0 - xy[:, 1]) / grid.dx).astype(int)
        ok = (r >= 0) & (c >= 0) & (r < a.shape[0]) & (c < a.shape[1])
        out = np.full(len(xy), np.nan)
        out[ok] = a[r[ok], c[ok]]
        return out
    return f
