"""
Pothole (depression) storage and effective contributing area from a DEM + basin polygon.
Pure Python: GDAL/OGR + numpy + the WhiteboxTools command-line binary (whitebox_tools), no QGIS.

Steps
 1. Clip DEM to the basin polygon (cells outside -> NoData; WhiteboxTools treats NoData edges as outlets,
    so the basin boundary acts as the spill boundary).
 2. FillDepressions (fix_flats=False)  -> filled; depth = filled - dem.
 3. Depression cells: depth > min_depth. Clump (8-connected) -> one id per pothole; drop potholes < min_area.
 4. Per pothole: area, max depth, mean depth, volume (sum depth * cell area), spill elevation (filled surface).
 5. Polygonise pothole ids -> GeoPackage.
 6. Contributing area: D8 pointer on a depression-filled surface with flats fixed (FillDepressions fix_flats=True).
    A cell "contributes" if its D8 path leaves the basin at the outlet (the exit cell with the largest D8 flow
    accumulation) without passing through any pothole cell. Pothole cells themselves are non-contributing.
"""
import os
import shutil
import subprocess
import tempfile
import csv

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions(); ogr.UseExceptions()

M3_PER_ACFT = 1233.48183754752
M2_PER_MI2 = 2589988.110336
# WhiteboxTools D8 pointer encoding (non-ESRI): value -> (drow, dcol)
WBT_D8 = {1: (-1, 1), 2: (0, 1), 4: (1, 1), 8: (1, 0), 16: (1, -1), 32: (0, -1), 64: (-1, -1), 128: (-1, 0)}


def find_wbt(explicit=None):
    cands = [explicit, os.environ.get("WBT_PATH"), shutil.which("whitebox_tools"), shutil.which("whitebox_tools.exe")]
    try:
        import whitebox  # pip package 'whitebox' bundles/downloads the binary
        d = os.path.dirname(whitebox.__file__)
        cands += [os.path.join(d, "WBT", "whitebox_tools"), os.path.join(d, "WBT", "whitebox_tools.exe")]
    except ImportError:
        pass
    for c in cands:
        if c and os.path.isfile(c):
            return c
    raise FileNotFoundError("WhiteboxTools binary not found. Install with `conda install -c conda-forge "
                            "whitebox_tools` or `pip install whitebox` (then run whitebox once), or set WBT_PATH.")


def wbt(exe, tool, log=None, **kw):
    args = [exe, "-r=" + tool, "-v=false"] + ["--%s=%s" % (k, v) if v is not True else "--" + k for k, v in kw.items()]
    out = kw.get("output")
    for attempt in range(4):        # WhiteboxTools 2.4 intermittently panics while writing GeoTIFF output; retry
        p = subprocess.run(args, capture_output=True, text=True)
        if p.returncode == 0 and (out is None or os.path.exists(out)):
            break
        if log:
            log("WhiteboxTools %s attempt %d failed, retrying" % (tool, attempt + 1))
    if p.returncode != 0 or (out is not None and not os.path.exists(out)):
        raise RuntimeError("WhiteboxTools %s failed: %s %s" % (tool, p.stdout[-2000:], p.stderr[-2000:]))
    if log:
        log("WhiteboxTools %s OK" % tool)


def _read(path):
    ds = gdal.Open(path); b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype(np.float64); nd = b.GetNoDataValue()
    m = np.isnan(a) if nd is None else (a == nd) | np.isnan(a)
    return a, m, ds.GetGeoTransform(), ds.GetProjection()


def _write(path, arr, gt, wkt, nodata, dtype=gdal.GDT_Float32):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1, dtype, ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform(gt); ds.SetProjection(wkt)
    b = ds.GetRasterBand(1); b.SetNoDataValue(nodata); b.WriteArray(arr); ds = None


def run_pothole(dem_path, basin_path, out_dir, z_factor_to_m=1.0, min_depth_m=0.05, min_area_m2=100.0,
                wbt_exe=None, basin_layer=None, log=print, keep_temp=False):
    exe = find_wbt(wbt_exe)
    dem_path, basin_path, out_dir = map(os.path.abspath, (dem_path, basin_path, out_dir))
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="ndpmp_pothole_")
    # 1. clip (keep DEM CRS; must be projected with metre/ft units)
    src = gdal.Open(dem_path)
    srs = osr.SpatialReference(wkt=src.GetProjection())
    if not srs.IsProjected():
        raise ValueError("DEM must be in a projected CRS (equal-area or UTM) so cell areas are meaningful")
    lin = srs.GetLinearUnits()   # metres per CRS unit
    clip = os.path.join(tmp, "dem_clip.tif")
    gdal.Warp(clip, src, cutlineDSName=basin_path, cutlineLayer=basin_layer, cropToCutline=True,
              dstNodata=-32768.0, outputType=gdal.GDT_Float32, warpOptions=["CUTLINE_ALL_TOUCHED=FALSE"])
    dem, ndm, gt, wkt = _read(clip)
    cell_m2 = abs(gt[1] * gt[5]) * lin * lin
    # 2-3. fill and depressions
    filled = os.path.join(tmp, "filled.tif")
    wbt(exe, "FillDepressions", log, dem=clip, output=filled)                # fix_flats off by default
    fz, _, _, _ = _read(filled)
    depth = np.where(ndm, 0.0, (fz - dem) * z_factor_to_m)
    depth[depth < 0] = 0.0
    sink = (depth > min_depth_m) & ~ndm
    mask = os.path.join(tmp, "sink.tif")
    _write(mask, sink.astype(np.float32), gt, wkt, -1.0)
    clump = os.path.join(tmp, "clump.tif")
    wbt(exe, "Clump", log, i=mask, output=clump, diag=True, zero_back=True)
    lab, _, _, _ = _read(clump)
    lab = np.where(sink, lab, 0).astype(np.int64)
    ids, inv = np.unique(lab, return_inverse=True)
    inv = inv.reshape(lab.shape)
    n = len(ids)
    cnt = np.bincount(inv.ravel(), minlength=n)
    vol = np.bincount(inv.ravel(), weights=depth.ravel(), minlength=n) * cell_m2
    maxd = np.zeros(n); np.maximum.at(maxd, inv.ravel(), depth.ravel())
    spill = np.full(n, -np.inf); np.maximum.at(spill, inv.ravel(), np.where(ndm, -np.inf, fz).ravel())
    keep = (ids != 0) & (cnt * cell_m2 >= min_area_m2)
    newid = np.zeros(n, np.int64); newid[keep] = np.arange(1, keep.sum() + 1)
    pid = newid[inv]                                   # 0 = not a (kept) pothole
    pot_mask = pid > 0
    # 5. polygons
    idr = os.path.join(tmp, "pid.tif"); _write(idr, pid.astype(np.int32), gt, wkt, 0, gdal.GDT_Int32)
    gpkg = os.path.join(out_dir, "potholes.gpkg")
    if os.path.exists(gpkg):
        os.remove(gpkg)
    stats = {}
    for i in np.nonzero(keep)[0]:
        k = int(newid[i])
        stats[k] = dict(pothole_id=k, n_cells=int(cnt[i]), area_m2=float(cnt[i] * cell_m2),
                        area_ac=float(cnt[i] * cell_m2 / 4046.8564224), max_depth_m=float(maxd[i]),
                        mean_depth_m=float(vol[i] / (cnt[i] * cell_m2)), volume_m3=float(vol[i]),
                        volume_acft=float(vol[i] / M3_PER_ACFT), spill_elev=float(spill[i]))
    s = osr.SpatialReference(wkt=wkt)
    mds = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("m")  # "Memory" before GDAL 3.11
    ml = mds.CreateLayer("p", s, ogr.wkbPolygon); ml.CreateField(ogr.FieldDefn("pothole_id", ogr.OFTInteger))
    rds = gdal.Open(idr); band = rds.GetRasterBand(1)
    gdal.Polygonize(band, band, ml, 0, [], callback=None)
    geoms = {}
    for f in ml:
        g = f.GetGeometryRef().Clone()
        geoms.setdefault(f.GetField("pothole_id"), ogr.Geometry(ogr.wkbMultiPolygon)).AddGeometry(g)
    ods = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
    lyr = ods.CreateLayer("potholes", s, ogr.wkbMultiPolygon)
    cols = [("pothole_id", ogr.OFTInteger), ("area_m2", ogr.OFTReal), ("area_ac", ogr.OFTReal),
            ("max_depth_m", ogr.OFTReal), ("mean_depth_m", ogr.OFTReal), ("volume_m3", ogr.OFTReal),
            ("volume_acft", ogr.OFTReal), ("spill_elev", ogr.OFTReal), ("n_cells", ogr.OFTInteger)]
    for fname, ft in cols:
        lyr.CreateField(ogr.FieldDefn(fname, ft))
    lyr.StartTransaction()
    for k in sorted(stats):
        f = ogr.Feature(lyr.GetLayerDefn())
        for fname, _ in cols:
            f.SetField(fname, stats[k][fname])
        f.SetGeometry(geoms[k]); lyr.CreateFeature(f)
    lyr.CommitTransaction()
    ods = None; rds = None
    # 6. contributing area
    filled2 = os.path.join(tmp, "filled_flat.tif"); pntr = os.path.join(tmp, "d8.tif"); acc = os.path.join(tmp, "acc.tif")
    wbt(exe, "FillDepressions", log, dem=clip, output=filled2, fix_flats=True)
    wbt(exe, "D8Pointer", log, dem=filled2, output=pntr)
    wbt(exe, "D8FlowAccumulation", log, i=pntr, output=acc, pntr=True, out_type="cells")
    pv, _, _, _ = _read(pntr); av, _, _, _ = _read(acc)
    nr, nc = dem.shape
    valid = ~ndm
    down = np.full(nr * nc, -1, np.int64)        # -1 = leaves basin / grid (exit), -2 = no direction (pit/flat)
    rr, cc = np.nonzero(valid)
    for code, (dr, dc) in WBT_D8.items():
        sel = (pv[rr, cc] == code)
        r2 = rr[sel] + dr; c2 = cc[sel] + dc
        inside = (r2 >= 0) & (r2 < nr) & (c2 >= 0) & (c2 < nc)
        tgt = np.full(sel.sum(), -1, np.int64)
        ok = inside.copy(); ok[inside] = valid[r2[inside], c2[inside]]
        tgt[ok] = r2[ok] * nc + c2[ok]
        down[rr[sel] * nc + cc[sel]] = tgt
    nodir = valid & ~np.isin(pv, list(WBT_D8.keys()))
    # WhiteboxTools gives edge cells pointer 0 (no outward direction): a no-direction cell touching NoData or the
    # grid edge is an exit (-1); an interior no-direction cell is a pit (-2).
    pad = np.pad(valid, 1, constant_values=False)
    allnb = np.ones_like(valid)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            allnb &= pad[1 + dr:1 + dr + nr, 1 + dc:1 + dc + nc]
    boundary = valid & ~allnb
    down[np.nonzero((nodir & boundary).ravel())[0]] = -1
    down[np.nonzero((nodir & ~boundary).ravel())[0]] = -2
    # exit cells (valid, flow leaves basin); outlet = exit cell with max accumulation
    flat_valid = valid.ravel()
    exits = np.nonzero(flat_valid & (down == -1))[0]
    if len(exits) == 0:
        raise RuntimeError("No cell drains out of the basin (closed basin?)")
    outlet = int(exits[np.argmax(av.ravel()[exits])])
    # Walk downstream from every cell with memoisation: state 1 = reaches outlet clean, 0 = not
    state = np.full(nr * nc, -1, np.int8)
    potf = pot_mask.ravel()
    state[~flat_valid] = 0
    state[potf] = 0
    any_exit = np.zeros(nr * nc, np.int8)   # reaches ANY exit cleanly
    any_state = state.copy()
    for start in np.nonzero(flat_valid)[0]:
        if state[start] != -1:
            continue
        path = []; c = start
        while True:
            if state[c] != -1:
                res, res_any = state[c], any_state[c]; break
            path.append(c)
            d = down[c]
            if d == -1:
                res = 1 if c == outlet else 0; res_any = 1; break
            if d == -2 or len(path) > nr * nc:
                res = 0; res_any = 0; break
            c = d
        for p in path:
            state[p] = res; any_state[p] = res_any
    # ---- pothole network for the fill-and-spill cascade ---------------------------------
    # first[c] = id of the first pothole a drop starting at c enters (its own if c is a pothole cell),
    #            0 = reaches the main outlet without entering one, -1 = another exit / pit.
    first = np.full(nr * nc, -9, np.int64)
    pidf = pid.ravel()
    first[~flat_valid] = -1
    first[potf] = pidf[potf]
    for start in np.nonzero(flat_valid)[0]:
        if first[start] != -9:
            continue
        path = []; c = start
        while True:
            if first[c] != -9:
                res = first[c]; break
            path.append(c)
            d = down[c]
            if d == -1:
                res = 0 if c == outlet else -1; break
            if d == -2 or len(path) > nr * nc:
                res = -1; break
            c = d
        for p_ in path:
            first[p_] = res
    # spill target of each pothole: follow D8 from its cells until leaving it
    target = {}
    for k in stats:
        c = int(np.nonzero(pidf == k)[0][0])
        tgt, steps = -1, 0
        while steps <= nr * nc:
            steps += 1
            if pidf[c] > 0 and pidf[c] != k:
                tgt = int(pidf[c]); break
            d = down[c]
            if d == -1:
                tgt = 0 if c == outlet else -1; break
            if d == -2:
                tgt = -1; break
            c = int(d)
        target[k] = tgt
    catch = np.bincount(np.where(first > 0, first, 0), minlength=len(stats) + 1) * cell_m2
    network = {"cell_m2": cell_m2, "basin_m2": float(valid.sum() * cell_m2),
               "direct_to_outlet_m2": float((first == 0).sum() * cell_m2),
               "to_other_exit_m2": float(((first == -1) & flat_valid).sum() * cell_m2),
               "potholes": [{"id": k, "volume_m3": stats[k]["volume_m3"], "area_m2": stats[k]["area_m2"],
                             "catchment_m2": float(catch[k]), "target": target[k]} for k in sorted(stats)]}
    import json
    with open(os.path.join(out_dir, "pothole_network.json"), "w") as fh:
        json.dump(network, fh, indent=0)
    contrib = (state == 1).reshape(dem.shape) & valid
    contrib_any = (any_state == 1).reshape(dem.shape) & valid
    _write(os.path.join(out_dir, "contributing_area.tif"), contrib.astype(np.uint8), gt, wkt, 255, gdal.GDT_Byte)
    _write(os.path.join(out_dir, "depression_depth_m.tif"), np.where(valid, depth, -9999).astype(np.float32), gt, wkt, -9999)
    basin_m2 = valid.sum() * cell_m2
    tot_m3 = sum(s_["volume_m3"] for s_ in stats.values())
    orow, ocol = divmod(outlet, nc)
    ox = gt[0] + (ocol + 0.5) * gt[1]; oy = gt[3] + (orow + 0.5) * gt[5]
    summ = {"basin_area_mi2": basin_m2 / M2_PER_MI2, "basin_area_ac": basin_m2 / 4046.8564224,
            "cell_size_m": abs(gt[1]) * lin, "n_potholes": len(stats),
            "pothole_area_ac": sum(s_["area_ac"] for s_ in stats.values()),
            "total_storage_acft": tot_m3 / M3_PER_ACFT, "total_storage_in_over_basin": tot_m3 / basin_m2 / 0.0254,
            "max_pothole_depth_m": max([s_["max_depth_m"] for s_ in stats.values()] or [0]),
            "contributing_area_mi2": contrib.sum() * cell_m2 / M2_PER_MI2,
            "contributing_fraction": float(contrib.sum() / valid.sum()),
            "noncontributing_area_mi2": (valid.sum() - contrib.sum()) * cell_m2 / M2_PER_MI2,
            "contributing_any_exit_mi2": contrib_any.sum() * cell_m2 / M2_PER_MI2,
            "outlet_x": ox, "outlet_y": oy, "outlet_accum_cells": float(av.ravel()[outlet]),
            "min_depth_m": min_depth_m, "min_area_m2": min_area_m2, "dem": dem_path}
    summ = {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in summ.items()}
    with open(os.path.join(out_dir, "pothole_summary.csv"), "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["metric", "value"])
        for k, v in summ.items():
            w.writerow([k, v])
    with open(os.path.join(out_dir, "potholes.csv"), "w", newline="") as fh:
        cols = ["pothole_id", "n_cells", "area_m2", "area_ac", "max_depth_m", "mean_depth_m", "volume_m3",
                "volume_acft", "spill_elev"]
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for k in sorted(stats):
            w.writerow(stats[k])
    if not keep_temp:
        shutil.rmtree(tmp, ignore_errors=True)
    summ["potholes_gpkg"] = gpkg
    return summ, stats
