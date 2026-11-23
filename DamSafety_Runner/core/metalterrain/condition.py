"""BoannGrid crossing conditioning in one call, with a keep-out mask built from the layers the
Hazard plugin already uses (Runner 3.58.0).

    from nd_breach_runner.core.metalterrain import condition as C
    out, records, info = C.condition(z, cell_m=30.0, keepout=mask)             # arrays
    info = C.condition_file("dem.tif", "dem_conditioned.tif", dam_folder=".")  # files

The steps, all 4-connected (the way D4 flow, the carve path and the ACC flood engine move
water), from ONE fill:
  1. fill_dem(conn=4)                     the depression-filled surface
  2. audit.depression_census              every filled depression, classed
  3. flow.flow_accum(filled=...)          drainage area; no dead ends on a 4-way fill
  4. burn.reconnect_crossings             crossings only, depth cap, keep-out mask, freeze mask

Before this, a conditioning run was a hand script: it filled 8-way, flow had to fill a second
time 4-way, and the keep-out mask (#483) only worked when the script built and passed it. The
Absaraka #473 rerun had no mask and carved 108 notches through Casselton house lots.

Keep-out layers (any GDAL/OGR vector, in any CRS): building footprint polygons, address points
(E911) and town limits. Each feature is grown by `buffer_m` (25 m by default, about a house lot
around a footprint or an address point) and burned onto the DEM grid with ALL_TOUCHED, so a cell
the lot touches is protected. `find_keepout_layers(folder)` picks them up from a dam folder by
name: the Microsoft footprints Hazard downloads into outputs/_downloads, OpenStreetMap buildings,
E911 / address points, and city / town / municipal limits.
"""
import json
import os
import re

import numpy as np

from . import audit as A
from . import burn as B
from . import delta_log as DL
from . import fill as F
from . import flow as FL

DEFAULT_BUFFER_M = 25.0
VECTOR_EXT = (".geojson", ".json", ".gpkg", ".shp", ".kml", ".fgb", ".geojsonl", ".geojsons")
# file names that are keep-out layers (case-insensitive, matched on the file name only)
KEEPOUT_RE = re.compile(
    r"(?<![a-z])(?:buildings?|footprints?|outlines?|bldgs?|structures?|msft|ms_buildings|"
    r"minedbuildings|global[_-]?buildings|e911|e-911|addresse?s?|address[_-]?points?|"
    r"city[_-]?limits?|town[_-]?limits?|municipal\w*|corporate[_-]?limits?|incorporated)"
    r"(?![a-z])", re.I)
# folders find_keepout_layers looks in, under a dam folder (and the folder itself)
SEARCH_DIRS = ("", "outputs/_downloads", "_downloads", "inputs", "data", "hazard")


def find_keepout_layers(folder, max_depth=3):
    """Vector files under `folder` (and its outputs/_downloads, inputs, data, hazard folders, up
    to `max_depth` levels down) whose NAME says building footprints, address points or town
    limits. Returns a sorted list of paths (duplicates removed)."""
    found = set()
    if not folder or not os.path.isdir(folder):
        return []
    for sub in SEARCH_DIRS:
        top = os.path.join(folder, sub)
        if not os.path.isdir(top):
            continue
        base = top.rstrip(os.sep).count(os.sep)
        for root, dirs, files in os.walk(top):
            if root.count(os.sep) - base >= max_depth:
                dirs[:] = []
            for f in files:
                low = f.lower()
                if low.endswith(VECTOR_EXT) and KEEPOUT_RE.search(os.path.splitext(f)[0]):
                    found.add(os.path.abspath(os.path.join(root, f)))
    return sorted(found)


def _gdal():
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ogr.UseExceptions()
    osr.UseExceptions()
    return gdal, ogr, osr


def keepout_mask(shape, geotransform, wkt, layers, buffer_m=DEFAULT_BUFFER_M):
    """Burn the features of `layers` (vector paths) onto a grid of `shape` / `geotransform` /
    `wkt`, each grown by `buffer_m` metres (in the grid's CRS, so the grid must be projected in
    metres). Returns (mask bool array, n_features, per-layer counts dict)."""
    gdal, ogr, osr = _gdal()
    ny, nx = shape
    mem = gdal.GetDriverByName("MEM").Create("", int(nx), int(ny), 1, gdal.GDT_Byte)
    mem.SetGeoTransform(tuple(geotransform))
    mem.SetProjection(wkt)
    dst = osr.SpatialReference()
    dst.ImportFromWkt(wkt)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    x0, dx, _r1, y0, _r2, dy = geotransform
    xa, xb = sorted((x0, x0 + dx * nx))
    ya, yb = sorted((y0, y0 + dy * ny))
    pad = buffer_m + abs(dx)
    box = ogr.CreateGeometryFromWkt("POLYGON(({a} {c},{b} {c},{b} {d},{a} {d},{a} {c}))".format(
        a=xa - pad, b=xb + pad, c=ya - pad, d=yb + pad))
    total, per = 0, {}
    for path in layers:
        src = ogr.Open(path)
        if src is None:
            raise IOError("cannot open keep-out layer {}".format(path))
        n_here = 0
        for li in range(src.GetLayerCount()):
            lyr = src.GetLayer(li)
            srs = lyr.GetSpatialRef()
            tr = None
            if srs is not None and not srs.IsSame(dst):
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                tr = osr.CoordinateTransformation(srs, dst)
            tmp = ogr.GetDriverByName("Memory").CreateDataSource("keepout")
            out = tmp.CreateLayer("keepout", dst, ogr.wkbUnknown)
            for feat in lyr:
                g = feat.GetGeometryRef()
                if g is None or g.IsEmpty():
                    continue
                g = g.Clone()
                if tr is not None:
                    g.Transform(tr)
                if not g.Intersects(box):
                    continue
                if buffer_m > 0:
                    g = g.Buffer(float(buffer_m), 4)
                nf = ogr.Feature(out.GetLayerDefn())
                nf.SetGeometry(g)
                out.CreateFeature(nf)
                n_here += 1
            if n_here:
                gdal.RasterizeLayer(mem, [1], out, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        per[path] = n_here
        total += n_here
    return mem.GetRasterBand(1).ReadAsArray().astype(bool), total, per


def condition(dem, cell_m, keepout=None, freeze_mask=None, min_accum_cells=20,
              classes=B.DEFAULT_CLASSES, max_cut_m=B.DEFAULT_MAX_CUT_M, eps=1e-5,
              backend="auto", log=None, refuse_freeze="record"):
    """Condition `dem` (2-D, NaN / -9999 = NoData) for crossings, 4-connected throughout from one
    fill. `keepout` is the protect mask (True = never lower), `freeze_mask` the dam-safety freeze
    (embankments / dam crests). Returns (conditioned, records, info); records are
    burn.reconnect_crossings records, info has the step counts."""
    z = np.asarray(dem, dtype=np.float64)
    say = log or (lambda _m: None)
    filled, fi = F.fill_dem(z, eps=eps, backend=backend, log=log, conn=4)
    say("[boanngrid] fill (4-way): {} sweeps, converged {}".format(fi["sweeps"], fi["converged"]))
    labels, items = A.depression_census(z, filled, cell_m=cell_m)
    kinds = {}
    for it in items:
        kinds[it["klass"]] = kinds.get(it["klass"], 0) + 1
    say("[boanngrid] census: {:,} depressions {}".format(len(items), kinds))
    _d, accum, finfo = FL.flow_accum(z, filled=filled, backend=backend, conn=4, log=log)
    say("[boanngrid] flow: {:,} dead ends, largest drainage area {:,} cells".format(
        finfo["interior_sinks"], int(np.nanmax(accum)) if np.isfinite(accum).any() else 0))
    out, records = B.reconnect_crossings(
        z, min_accum_cells=min_accum_cells, cell_m=cell_m, eps=eps, backend=backend,
        filled=filled, labels=labels, items=items, accum=accum, freeze_mask=freeze_mask,
        refuse_freeze=refuse_freeze, log=log, classes=classes, max_cut_m=max_cut_m,
        protect_mask=keepout)
    low = np.isfinite(z) & ((z - out) > 1e-9)
    lim = B.limit_refusals(records)
    info = {
        "fill_sweeps": fi["sweeps"], "fill_converged": bool(fi["converged"]),
        "depressions": len(items), "classes": kinds,
        "flow_dead_ends": int(finfo["interior_sinks"]),
        "flow_refilled_d4": bool(finfo.get("refilled_d4")),
        "max_accum_cells": int(np.nanmax(accum)) if np.isfinite(accum).any() else 0,
        "carved": sum(1 for r in records if "refused" not in r),
        "refused_max_cut": sum(1 for r in lim if r["refused"] == "max_cut"),
        "refused_keepout": sum(1 for r in lim if r["refused"] == "protected"),
        "refused_freeze": len(B.freeze_refusals(records)),
        "cells_lowered": int(low.sum()),
        "max_cut_m": float((z - out)[low].max()) if low.any() else 0.0,
        "keepout_cells": int(np.asarray(keepout).sum()) if keepout is not None else 0,
        "min_accum_cells": int(min_accum_cells),
        "classes_carved": list(classes) if classes is not None else None,
        "max_cut_cap_m": max_cut_m,
    }
    say("[boanngrid] reconnect: {carved:,} carved; refused {refused_max_cut:,} past the depth cap, "
        "{refused_keepout:,} through the keep-out mask, {refused_freeze:,} by the freeze mask; "
        "{cells_lowered:,} cells lowered, deepest cut {max_cut_m:.2f} m".format(**info))
    return out, records, info


def _read_raster(path):
    gdal, _ogr, _osr = _gdal()
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype(np.float64)
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    a[a <= F.NODATA + 1] = np.nan
    return a, ds.GetGeoTransform(), ds.GetProjection()


def _write_geotiff(path, a, gt, wkt, nodata=-9999.0):
    gdal, _ogr, _osr = _gdal()
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32,
                                              options=["COMPRESS=DEFLATE", "TILED=YES"])
    ds.SetGeoTransform(gt)
    ds.SetProjection(wkt)
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isfinite(a), a, nodata).astype(np.float32))
    ds.FlushCache()
    ds = None
    return path


def condition_file(dem_path, out_path, keepout_layers=(), dam_folder=None,
                   buffer_m=DEFAULT_BUFFER_M, freeze_path=None, min_accum_cells=20,
                   classes=B.DEFAULT_CLASSES, max_cut_m=B.DEFAULT_MAX_CUT_M, backend="auto",
                   acknowledge=None, log=None):
    """Condition the GeoTIFF `dem_path` and write `out_path` (GeoTIFF), plus beside it
    <out>.deltalog.sqlite (every carve and refusal) and <out>.json (the counts, the keep-out
    layers used). Keep-out layers are `keepout_layers` plus, when `dam_folder` is given, what
    find_keepout_layers finds there. `freeze_path` is an optional raster (non-zero = frozen
    embankment / dam crest) on the same grid. Writing the DEM goes through the delta-log export
    gate: it is refused while a freeze refusal is unresolved, unless `acknowledge` says why it is
    acceptable. Returns the info dict."""
    say = log or (lambda _m: None)
    z, gt, wkt = _read_raster(dem_path)
    cell = abs(gt[1])
    layers = list(keepout_layers or ())
    if dam_folder:
        auto = [p for p in find_keepout_layers(dam_folder) if p not in layers]
        layers += auto
        say("[boanngrid] keep-out layers found in {}: {}".format(
            dam_folder, ", ".join(os.path.basename(p) for p in auto) or "none"))
    keep, per = None, {}
    if layers:
        keep, n_feat, per = keepout_mask(z.shape, gt, wkt, layers, buffer_m=buffer_m)
        say("[boanngrid] keep-out mask: {:,} features, {:,} cells ({:g} m margin)".format(
            n_feat, int(keep.sum()), buffer_m))
    else:
        say("[boanngrid] WARNING: no keep-out layers (building footprints / address points / "
            "town limits); a carve can cross a house lot")
    freeze = None
    if freeze_path:
        fz, _g, _w = _read_raster(freeze_path)
        if fz.shape != z.shape:
            raise ValueError("freeze raster {} is {} cells, the DEM {}".format(
                freeze_path, fz.shape, z.shape))
        freeze = np.isfinite(fz) & (fz != 0)
    out, records, info = condition(z, cell, keepout=keep, freeze_mask=freeze,
                                   min_accum_cells=min_accum_cells, classes=classes,
                                   max_cut_m=max_cut_m, backend=backend, log=log)
    run_id = "boanngrid:" + os.path.basename(dem_path)
    db = out_path + ".deltalog.sqlite"
    if os.path.exists(db):
        os.remove(db)                      # the ledger belongs to this output; rewrite it
    with DL.DeltaLog(run_id=run_id, path=db) as dlog:
        DL.log_reconnect(dlog, records, cell_m=cell)
        dlog.assert_export_allowed(acknowledge=acknowledge)
        _write_geotiff(out_path, out, gt, wkt)
        summary = dlog.summary()
    info.update({"dem": os.path.abspath(dem_path), "out": os.path.abspath(out_path),
                 "keepout_layers": {os.path.abspath(k): v for k, v in per.items()},
                 "buffer_m": buffer_m, "deltalog": os.path.abspath(db)})
    with open(out_path + ".json", "w") as fh:
        json.dump(info, fh, indent=1, default=float)
    with open(out_path + ".deltalog.md", "w") as fh:
        fh.write(summary)
    say("[boanngrid] wrote {}".format(out_path))
    return info
