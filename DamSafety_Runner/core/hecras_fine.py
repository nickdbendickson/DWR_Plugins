"""3.10.92 (review of #204): the HEC-RAS depth at the fine terrain's resolution.

Two depth bases measure every point of the run's fine (1 m) terrain inside each output grid
cell instead of only the cell's centre ('terrain') or its mean ground ('terrain_mean'):

  terrain_max  each fine point takes the max WSE of its nearest HEC-RAS cell (the cell's
               surface held flat, as 'terrain'), depth = WSE - fine ground; the output grid
               cell keeps the MAXIMUM of its fine depths, and arrives the first time ANY of
               its fine points reaches the arrival depth (0.1 ft). Conservative: the deepest
               spot and the earliest arrival in the pixel.
  fine         RAS Mapper style: the max WSE is interpolated between the HEC-RAS cell
               centres - barycentric over the Delaunay triangle of three neighbouring cell
               centres when all three cells are wet, else the nearest cell's WSE held flat -
               never across a dry cell (a point whose nearest cell is dry stays dry) nor into
               ground above the interpolated surface; depth = that WSE - fine ground. Written
               at the fine resolution as extra rasters (FINE_NAMES, ft), and aggregated to
               the output grid cell by 'max' (default) or 'mean' (over the cell's fine points
               inside the mesh, dry counted as 0; FINE_AGG_ENV). The arrival of an output cell
               is that of its deepest fine point on the interpolated surface's time series.
               This APPROXIMATES RAS Mapper's sloped water surface ('Sloped (Cell Corners)'
               interpolates through face and corner values HEC-RAS computes from the face
               hydraulics); it is not identical to it.

The work is tiled (TILE_FINE fine points per tile side), single-threaded numpy, only over
the output cells that a wet HEC-RAS cell can reach, and stops above FINE_MAX_MPX_ENV million
fine points (HecRasError) so a run never runs out of memory here."""
import math
import os

import numpy as np

from . import hecras

FINE_AGG_ENV = "NDBREACH_HECRAS_FINE_AGG"            # 'max' (default) or 'mean'
FINE_AGGS = ("max", "mean")
FINE_AGG_DEFAULT = "max"
FINE_RASTER_ENV = "NDBREACH_HECRAS_FINE_RASTER"      # '0' / 'no': skip the fine rasters
FINE_MAX_MPX_ENV = "NDBREACH_HECRAS_FINE_MAX_MPX"    # million fine points, default below
FINE_MAX_MPX_DEFAULT = 1500.0
FINE_K_MAX = 64                # fine points per output cell side at most
TILE_FINE = 512                # fine points per tile side (about 2 MB per float array)
EDGE_FACTOR = 3.0              # a triangle edge longer than this x its cells' spacing: no slope
FINE_NAMES = {"depth": "Depth_fine (Max).tif", "wse": "WSE_fine (Max).tif"}


def _env(environ):
    return os.environ if environ is None else environ


def fine_agg(value=None, environ=None):
    """How the 'fine' basis makes the output grid cell: 'max' (default) or 'mean'."""
    raw = value if value not in (None, "") else _env(environ).get(FINE_AGG_ENV, "")
    raw = str(raw or FINE_AGG_DEFAULT).strip().lower()
    if raw not in FINE_AGGS:
        raise hecras.HecRasError("{} {!r}: use 'max' (default) or 'mean'".format(
            FINE_AGG_ENV, raw))
    return raw


def fine_rasters_on(value=None, environ=None):
    """Write the fine rasters (FINE_NAMES) of the 'fine' basis? Default yes."""
    raw = value if value is not None else _env(environ).get(FINE_RASTER_ENV, "")
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in ("0", "no", "false", "off", "skip")


def max_points(environ=None):
    raw = _env(environ).get(FINE_MAX_MPX_ENV, "")
    try:
        return int(float(raw) * 1e6) if str(raw).strip() else int(FINE_MAX_MPX_DEFAULT * 1e6)
    except ValueError:
        raise hecras.HecRasError("{} {!r}: a number of million points".format(
            FINE_MAX_MPX_ENV, raw))


def sub_factor(dx_m, ground_cell_m):
    """Fine points per output cell side: the output cell over the fine terrain's cell,
    1..FINE_K_MAX (1 when the terrain's cell size is unknown)."""
    if not ground_cell_m or not dx_m or ground_cell_m <= 0:
        return 1
    return int(min(FINE_K_MAX, max(1, round(float(dx_m) / float(ground_cell_m)))))


def _dilate(mask, r):
    """Square dilation of a boolean grid by r cells (numpy only)."""
    if r <= 0:
        return mask
    out = mask.copy()
    for ax in (0, 1):
        src = out.copy()
        for s in range(1, r + 1):
            if ax == 0:
                out[s:, :] |= src[:-s, :]
                out[:-s, :] |= src[s:, :]
            else:
                out[:, s:] |= src[:, :-s]
                out[:, :-s] |= src[:, s:]
    return out


def candidates(mesh, wet_cell, x0, ytop, dx, nx, ny, reach=0.75):
    """(ny, nx) the output cells that a wet HEC-RAS cell can reach: those within each wet
    cell's own reach (cell_index) of its centre."""
    rad = hecras.cell_radius(mesh, reach)
    out = np.zeros((ny, nx), dtype=bool)
    ids = np.flatnonzero(wet_cell)
    if not len(ids):
        return out
    col = np.floor((mesh.x[ids] - x0) / dx).astype(np.int64)
    row = np.floor((ytop - mesh.y[ids]) / dx).astype(np.int64)
    rpx = np.ceil(rad[ids] / dx).astype(np.int64) + 1
    for r in np.unique(rpx):
        sel = rpx == r
        c, w = col[sel], row[sel]
        ok = (c >= -r) & (c < nx + r) & (w >= -r) & (w < ny + r)
        m = np.zeros((ny + 2 * r, nx + 2 * r), dtype=bool)
        m[w[ok] + r, c[ok] + r] = True
        out |= _dilate(m, int(r))[r:r + ny, r:r + nx]
    return out


class _Surface:
    """The 'fine' basis's interpolated max WSE (Delaunay of the cell centres)."""

    def __init__(self, mesh, wse, wet_cell):
        try:
            from scipy.spatial import Delaunay
        except ImportError:
            raise hecras.HecRasError(
                "The 'fine' depth basis needs the Python package scipy (for the triangulation "
                "of the HEC-RAS cell centres), which this Python does not have; use the "
                "'terrain' or 'terrain_max' depth basis, or install scipy.")
        pts = np.column_stack([mesh.x, mesh.y])
        self.tri = Delaunay(pts)
        v = self.tri.simplices
        nn = mesh.nn_distance()[0]
        p = pts[v]
        edge = np.max([np.hypot(*(p[:, i] - p[:, (i + 1) % 3]).T) for i in range(3)], axis=0)
        self.ok = wet_cell[v].all(axis=1) & (edge <= EDGE_FACTOR * nn[v].max(axis=1))
        self.wse = wse
        self.v = v

    def at(self, qx, qy, near):
        """(wse, cells (m,3), weights (m,3)) of each point; `near` its nearest cell (>= 0
        and wet, checked by the caller). Outside a usable triangle: the nearest cell's WSE."""
        q = np.column_stack([qx, qy])
        s = self.tri.find_simplex(q)
        use = s >= 0
        use[use] = self.ok[s[use]]
        cells = np.repeat(near[:, None], 3, axis=1)
        w = np.zeros((len(qx), 3))
        w[:, 0] = 1.0
        if use.any():
            T = self.tri.transform[s[use]]
            b = np.einsum("ijk,ik->ij", T[:, :2], q[use] - T[:, 2])
            bw = np.column_stack([b, 1.0 - b.sum(axis=1)])
            bw = np.clip(bw, 0.0, 1.0)
            bw /= bw.sum(axis=1, keepdims=True)
            cells[use] = self.v[s[use]]
            w[use] = bw
        wse = (self.wse[cells] * w).sum(axis=1)
        return wse, cells, w, use


def fine_on_grid(mesh, x0, ytop, dx, nx, ny, ground, k, basis, wet_ft=0.01,
                 arrival_depth_ft=hecras.ARRIVAL_DEPTH_FT, t0_hr=0.0, want_arrival=True,
                 reach=0.75, agg=None, rasters=None, limit=None, environ=None):
    """The terrain_max / fine depth basis on the output grid (x0, ytop, dx, nx, ny), ft.

    mesh: hecras.Mesh in the grid's CRS, with raw 'wse' / 'min_elev' (and the series while
    the HDF is open). ground(x0f, ytopf, dxf, nxf, nyf) -> (nyf, nxf) fine ground in ft (NaN
    where none: the cell's minimum elevation is used). k: fine points per output cell side
    (sub_factor). rasters: None, or a factory (x0w, ytopw, dxf, nxw, nyw) -> an object with
    write(kind, array_ft, xoff, yoff) and close() -> {kind: path} for the 'fine' rasters.
    Returns ({'depth', 'wse', 'velocity', 'time_of_max', 'arrival'} on the output grid,
    info)."""
    basis = hecras.depth_basis(basis, environ)
    if basis not in hecras.FINE_BASES:
        raise hecras.HecRasError("fine_on_grid is for {}".format(" / ".join(hecras.FINE_BASES)))
    agg = fine_agg(agg, environ) if basis == hecras.DEPTH_BASIS_FINE else "max"
    k = max(1, int(k))
    lim = max_points(environ) if limit is None else int(limit)
    raw = mesh.raw
    wse_c, cmin_c = raw["wse"], raw["min_elev"]
    with np.errstate(invalid="ignore"):
        wet_cell = np.isfinite(wse_c) & np.isfinite(cmin_c) & (wse_c - cmin_c >= wet_ft)
    cand = candidates(mesh, wet_cell, x0, ytop, dx, nx, ny, reach)
    n_cand = int(cand.sum())
    n_pts = n_cand * k * k
    info = {"depth_basis": basis, "fine_points_per_cell_side": k, "fine_cell": dx / k,
            "aggregate": agg if basis == hecras.DEPTH_BASIS_FINE else "max",
            "candidate_cells": n_cand, "fine_points": n_pts, "fine_limit_points": lim,
            "interpolated_points": 0, "flat_points": 0, "rasters": {}, "notes": []}
    if n_pts > lim:
        raise hecras.HecRasError(
            "The '{}' depth basis would read {:,} fine terrain points ({:,} output cells x {} x "
            "{}), more than {:,} ({}): use the 'terrain' basis, or raise the limit.".format(
                basis, n_pts, n_cand, k, k, lim, FINE_MAX_MPX_ENV))
    surf = _Surface(mesh, wse_c, wet_cell) if basis == hecras.DEPTH_BASIS_FINE and n_cand \
        else None
    shape = (ny, nx)
    depth = np.full(shape, np.nan)
    wse = np.full(shape, np.nan)
    rep_cell = np.full(shape, -1, dtype=np.int64)
    rep_ground = np.full(shape, np.nan)
    rep_cells3 = np.zeros(shape + (3,), dtype=np.int64)
    rep_w3 = np.zeros(shape + (3,))
    pair_px, pair_cell, pair_g = [], [], []
    thr = float(arrival_depth_ft)
    cache = {}
    dxf = dx / k
    writer = None
    if rasters is not None and n_cand:
        rr = np.flatnonzero(cand.any(axis=1))
        cc = np.flatnonzero(cand.any(axis=0))
        wr0, wc0 = int(rr[0]), int(cc[0])
        writer = rasters(x0 + wc0 * dx, ytop - wr0 * dx, dxf, (int(cc[-1]) - wc0 + 1) * k,
                         (int(rr[-1]) - wr0 + 1) * k)
        info["fine_raster_shape"] = [(int(rr[-1]) - wr0 + 1) * k, (int(cc[-1]) - wc0 + 1) * k]
    B = max(1, TILE_FINE // k)
    tiles = 0
    try:
        for r0 in range(0, ny, B):
            r1 = min(ny, r0 + B)
            band = cand[r0:r1]
            if not band.any():
                continue
            for c0 in range(0, nx, B):
                c1 = min(nx, c0 + B)
                if not band[:, c0:c1].any():
                    continue
                tiles += 1
                nyb, nxb = r1 - r0, c1 - c0
                nyf, nxf = nyb * k, nxb * k
                x0f, ytopf = x0 + c0 * dx, ytop - r0 * dx
                ci = hecras.cell_index(mesh, x0f, ytopf, dxf, nxf, nyf, reach=reach,
                                       cache=cache, allow_empty=True)
                inside = ci >= 0
                if not inside.any():
                    continue
                cs = np.maximum(ci, 0)
                g = np.asarray(ground(x0f, ytopf, dxf, nxf, nyf), dtype=np.float64)
                g = np.where(np.isfinite(g), g, cmin_c[cs])
                near_wet = inside & wet_cell[cs]
                ws = np.where(near_wet, wse_c[cs], np.nan)
                c3 = w3 = None
                if surf is not None:
                    c3 = np.repeat(cs[..., None], 3, axis=2)
                    w3 = np.zeros((nyf, nxf, 3))
                    w3[..., 0] = 1.0
                    if near_wet.any():
                        xs = x0f + (np.arange(nxf) + 0.5) * dxf
                        ys = ytopf - (np.arange(nyf) + 0.5) * dxf
                        jj, ii = np.nonzero(near_wet)
                        wi, cells, wts, used = surf.at(xs[ii], ys[jj], cs[jj, ii])
                        ws[jj, ii] = wi
                        c3[jj, ii] = cells
                        w3[jj, ii] = wts
                        info["interpolated_points"] += int(used.sum())
                        info["flat_points"] += int((~used).sum())
                with np.errstate(invalid="ignore"):
                    d = np.where(near_wet, np.clip(ws - g, 0.0, None), np.nan)
                    wetf = np.isfinite(d) & (d >= wet_ft)
                if writer is not None:
                    writer.write("depth", np.where(wetf, d, np.nan), (c0 - wc0) * k,
                                 (r0 - wr0) * k)
                    writer.write("wse", np.where(wetf, ws, np.nan), (c0 - wc0) * k,
                                 (r0 - wr0) * k)

                def blocks(a):
                    return a.reshape(nyb, k, nxb, k).swapaxes(1, 2).reshape(nyb, nxb, k * k)
                db = blocks(np.where(wetf, d, -1.0))
                j = db.argmax(axis=2)
                pick = (np.arange(nyb)[:, None], np.arange(nxb)[None, :], j)
                dmax = db[pick]
                if agg == "mean":
                    n_in = blocks(inside.astype(float)).sum(axis=2)
                    with np.errstate(invalid="ignore", divide="ignore"):
                        dv = blocks(np.where(wetf, d, 0.0)).sum(axis=2) / n_in
                else:
                    dv = dmax
                wetp = (dmax >= wet_ft) & np.isfinite(dv) & (dv >= wet_ft)
                sl = (slice(r0, r1), slice(c0, c1))
                depth[sl] = np.where(wetp, dv, np.nan)
                wse[sl] = np.where(wetp, blocks(ws)[pick], np.nan)
                rep_cell[sl] = np.where(wetp, blocks(cs)[pick], -1)
                rep_ground[sl] = blocks(g)[pick]
                if surf is not None:
                    for q in range(3):
                        rep_cells3[sl + (q,)] = blocks(c3[..., q])[pick]
                        rep_w3[sl + (q,)] = blocks(w3[..., q])[pick]
                if want_arrival and basis == hecras.DEPTH_BASIS_TERRAIN_MAX:
                    with np.errstate(invalid="ignore"):
                        hit = wetf & (d >= thr)
                    if hit.any():
                        fr, fc = np.nonzero(hit)
                        px = (r0 + fr // k) * nx + (c0 + fc // k)
                        cell = cs[fr, fc]
                        gg = g[fr, fc]
                        key = px * mesh.n + cell
                        o = np.lexsort((gg, key))
                        first = np.ones(len(o), dtype=bool)
                        first[1:] = key[o][1:] != key[o][:-1]
                        o = o[first]
                        pair_px.append(px[o])
                        pair_cell.append(cell[o])
                        pair_g.append(gg[o])
    finally:
        if writer is not None:
            info["rasters"] = writer.close() or {}
    info["tiles"] = tiles
    wetp = np.isfinite(depth)
    out = {"depth": depth, "wse": wse}
    rc = np.maximum(rep_cell, 0)
    for kk in ("velocity", "time_of_max"):
        if kk in raw:
            out[kk] = np.where(wetp, raw[kk][rc], np.nan)
    if want_arrival and mesh._series is not None:
        ts, t_hr, fac = mesh._series
        arr = np.full(ny * nx, np.nan)
        with np.errstate(invalid="ignore"):
            reach_px = np.flatnonzero((wetp & (depth >= thr)).ravel())
        if basis == hecras.DEPTH_BASIS_TERRAIN_MAX and pair_px:
            px = np.concatenate(pair_px)
            t = hecras._first_reach(ts, t_hr, np.concatenate(pair_g) / fac, thr / fac, mesh.n,
                                    cols=np.concatenate(pair_cell))
            best = np.full(ny * nx, np.inf)
            ok = np.isfinite(t)
            np.minimum.at(best, px[ok], t[ok])
            arr = np.where(np.isfinite(best), best, np.nan)
            info["arrival_pairs"] = int(len(px))
        elif basis == hecras.DEPTH_BASIS_FINE and len(reach_px):
            r, c = np.unravel_index(reach_px, shape)
            arr[reach_px] = hecras._first_reach(ts, t_hr, rep_ground[r, c] / fac, thr / fac,
                                                mesh.n, cols=rep_cells3[r, c],
                                                weights=rep_w3[r, c])
        missed = np.zeros(ny * nx, dtype=bool)
        missed[reach_px] = ~np.isfinite(arr[reach_px])
        if missed.any() and "time_of_max" in raw:
            arr[missed] = raw["time_of_max"][rc.ravel()[missed]]
            info["notes"].append("{:,} grid cells reach the arrival depth only between output "
                                 "steps; their arrival is the cell's time of the maximum."
                                 .format(int(np.isfinite(arr[missed]).sum())))
        keep = np.zeros(ny * nx, dtype=bool)
        keep[reach_px] = True
        arr = np.where(keep, np.clip(arr - float(t0_hr), 0.0, None), np.nan)
        out["arrival"] = arr.reshape(shape)
    elif want_arrival:
        info["notes"].append("No usable Water Surface time series in the file: no arrival "
                             "time.")
    info["notes"].append(
        "Depth basis '{}': {} fine points per output cell ({} x {}, {:.2f} grid units each) "
        "over {:,} output cells{}.".format(
            basis, k * k, k, k, dxf, n_cand,
            "" if basis == hecras.DEPTH_BASIS_TERRAIN_MAX else
            "; WSE interpolated at {:,} points, held flat at {:,}; aggregated by {}".format(
                info["interpolated_points"], info["flat_points"], agg)))
    return out, info


def clip_window(a, xoff, yoff, nxw, nyw):
    """The part of tile `a` placed at (xoff, yoff) that lies in a window nxw x nyw, and its
    offsets there."""
    xa, ya = max(0, -int(xoff)), max(0, -int(yoff))
    xb = min(a.shape[1], int(nxw) - int(xoff))
    yb = min(a.shape[0], int(nyw) - int(yoff))
    if xb <= xa or yb <= ya:
        return a[:0, :0], 0, 0
    return a[ya:yb, xa:xb], int(xoff) + xa, int(yoff) + ya


def gdal_rasters(folder, crs_wkt, names=None):
    """A `rasters` factory for fine_on_grid writing tiled, DEFLATE-compressed, sparse
    Float32 GeoTIFFs (ft) into `folder`."""
    names = names or FINE_NAMES

    def factory(x0w, ytopw, dxf, nxw, nyw):
        from . import gdal_io
        gdal = gdal_io._gdal()[0]
        drv = gdal.GetDriverByName("GTiff")
        os.makedirs(folder, exist_ok=True)
        dss, paths = {}, {}
        for kind, n in names.items():
            p = os.path.join(folder, n)
            ds = drv.Create(p, int(nxw), int(nyw), 1, gdal.GDT_Float32,
                            options=["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256",
                                     "COMPRESS=DEFLATE", "PREDICTOR=3", "SPARSE_OK=TRUE",
                                     "BIGTIFF=IF_SAFER", "NUM_THREADS=1"])
            ds.SetGeoTransform((x0w, dxf, 0.0, ytopw, 0.0, -dxf))
            if crs_wkt:
                ds.SetProjection(crs_wkt)
            ds.GetRasterBand(1).SetNoDataValue(float("nan"))
            dss[kind], paths[kind] = ds, p

        class W:
            def write(self, kind, a, xoff, yoff):
                ds = dss.get(kind)
                if ds is None:
                    return
                a, xoff, yoff = clip_window(np.asarray(a, dtype=np.float32), xoff, yoff,
                                            ds.RasterXSize, ds.RasterYSize)
                if a.size and np.isfinite(a).any():
                    ds.GetRasterBand(1).WriteArray(a, int(xoff), int(yoff))

            def close(self):
                for kk in list(dss):
                    dss[kk].FlushCache()
                    dss[kk] = None
                dss.clear()
                return dict(paths)
        return W()
    return factory
