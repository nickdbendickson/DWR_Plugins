"""Port of Spatial_Distribution_v1_0.py (AWA ND PMP tool) to GDAL/OGR + numpy.

Per storm pattern: shift the SPAS total-storm raster so its storm centre sits on the basin target
point, sample it (bilinear) at the PMP grid points, flatten with x**(1/5), SAF = value / mean,
and set each PMP_* field to (basin mean of that field) * SAF. NoData -> 0 (as the original,
the zeros stay in the mean). Writes a GeoPackage of points and one GeoTIFF per duration.
"""
import os, csv, math
import numpy as np
from osgeo import ogr
from . import gridutil as G

DEFAULT_PATTERNS = {
    "LS": ["SPAS_1209_1", "SPAS_1427_1", "SPAS_1699_1"],
    "GS": ["SPAS_1527_1", "SPAS_1583_1"],
    "CS": ["SPAS_1698_1", "SPAS_1733_1"],
}
TYPE_OF = {"Local": "LS", "General": "GS", "CoolSeason": "CS"}
FLATTEN_X = 5.0


def storm_centres(gdb):
    ds = ogr.Open(gdb); lyr = ds.GetLayerByName("Storm_List")
    return {f.GetField("SPAS_ID"): (f.GetField("Lon"), f.GetField("Lat"), f.GetField("Storm_Name"),
                                    f.GetField("State"), f.GetField("PMP_TYPE")) for f in lyr}


def run_spatial(points_gpkg, basin_path, home, out_dir, points_layer=None, centre_xy=None,
                patterns=None, basin_layer=None, cell_deg=0.025, log=print):
    log = log or (lambda *a: None)
    gdb = os.path.join(home, "Input", "Spatial_Distribution.gdb")
    pds = ogr.Open(points_gpkg)
    plyr = pds.GetLayerByName(points_layer) if points_layer else pds.GetLayer(0)
    pname = plyr.GetName()
    if "_PMP_Points_" not in pname:
        raise ValueError("points layer name must look like <Type>_PMP_Points_<basin>_<area> (from ndpmp:pmp)")
    stype, out_name = pname.split("_PMP_Points_", 1)
    if stype not in TYPE_OF:
        raise ValueError("PMP_Points storm type %s invalid" % stype)
    otype = TYPE_OF[stype]
    defn = plyr.GetLayerDefn()
    pmp_fields = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())
                  if defn.GetFieldDefn(i).GetName().startswith("PMP_")]
    feats = list(plyr)
    xs = np.array([f.GetGeometryRef().GetX() for f in feats]); ys = np.array([f.GetGeometryRef().GetY() for f in feats])
    vals = {k: np.array([f.GetField(k) for f in feats], float) for k in pmp_fields}
    if centre_xy is None:
        c = G.basin_wgs84(basin_path, basin_layer).Centroid()
        tx, ty = c.GetX(), c.GetY()
    else:
        tx, ty = map(float, centre_xy)
    log("Basin target location X=%.3f Y=%.3f" % (tx, ty))
    patterns = patterns or DEFAULT_PATTERNS[otype]
    cent = storm_centres(gdb)
    avail = set(G.gdb_raster_names(gdb))
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for sid in patterns:
        if sid not in cent or sid not in avail:
            log("WARNING: %s not in Storm_List or has no raster; skipped" % sid); continue
        sx, sy = cent[sid][0], cent[sid][1]
        dx, dy = tx - sx, ty - sy
        a, gt = G.read(G.gdb_raster(gdb, sid))
        v = G.bilinear(a, gt, xs - dx, ys - dy)          # == sampling the shifted raster at the points
        v = np.where(np.isnan(v), 0.0, np.power(np.where(np.isnan(v), 0.0, v), 1.0 / FLATTEN_X))
        mean = float(v.mean())
        saf = v / mean if mean > 0 else np.zeros_like(v)
        adj = {k: float(np.mean(vals[k])) * saf for k in pmp_fields}
        sub = os.path.join(out_dir, "%s_PMP_SpatialDist_%s_%s" % (otype, sid, out_name))
        os.makedirs(sub, exist_ok=True)
        gpkg = os.path.join(sub, "%s_PMP_Points_%s_%s_spatial.gpkg" % (stype, out_name, sid))
        if os.path.exists(gpkg):
            os.remove(gpkg)
        ods = ogr.GetDriverByName("GPKG").CreateDataSource(gpkg)
        ol = ods.CreateLayer("%s_PMP_Points_%s_%s_spatial" % (stype, out_name, sid), G.WGS84, ogr.wkbPoint)
        ol.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
        for k in pmp_fields:
            ol.CreateField(ogr.FieldDefn(k, ogr.OFTReal))
        ol.CreateField(ogr.FieldDefn(sid, ogr.OFTReal))
        safn = "SAF_" + sid[5:]
        ol.CreateField(ogr.FieldDefn(safn, ogr.OFTReal))
        for i, f in enumerate(feats):
            o = ogr.Feature(ol.GetLayerDefn())
            o.SetField("ID", f.GetField("ID") if f.GetFieldIndex("ID") >= 0 else i + 1)
            for k in pmp_fields:
                o.SetField(k, float(adj[k][i]))
            o.SetField(sid, float(v[i])); o.SetField(safn, float(saf[i]))
            o.SetGeometry(ogr.CreateGeometryFromWkt("POINT (%.10f %.10f)" % (xs[i], ys[i])))
            ol.CreateFeature(o)
        ods = None
        rasters = []
        x0 = math.floor(xs.min() / cell_deg) * cell_deg; y1 = math.ceil(ys.max() / cell_deg) * cell_deg
        nc = int(round((xs.max() - x0) / cell_deg)) + 1; nr = int(round((y1 - ys.min()) / cell_deg)) + 1
        rgt = (x0, cell_deg, 0, y1, 0, -cell_deg)
        cc = np.floor((xs - x0) / cell_deg).astype(int).clip(0, nc - 1)
        rr = np.floor((y1 - ys) / cell_deg).astype(int).clip(0, nr - 1)
        for k in pmp_fields:
            g = np.full((nr, nc), np.nan); g[rr, cc] = adj[k]
            rasters.append(G.write_tif(os.path.join(sub, "%s_%s_%s_%s_spatial.tif" % (pname[0], k[4:], out_name, sid)), g, rgt))
        row = {"storm_type": stype, "pattern": sid, "storm_name": cent[sid][2], "state": cent[sid][3],
               "storm_lon": sx, "storm_lat": sy, "shift_x_deg": dx, "shift_y_deg": dy, "n_points": len(feats),
               "n_points_nodata": int((v == 0).sum()), "saf_min": float(saf.min()), "saf_max": float(saf.max()),
               "gpkg": gpkg}
        for k in pmp_fields:
            row["mean_" + k] = float(adj[k].mean()); row["max_" + k] = float(adj[k].max())
        results.append(row)
        log("%s: SAF %.3f-%.3f over %d points" % (sid, row["saf_min"], row["saf_max"], len(feats)))
    if results:
        sp = os.path.join(out_dir, "spatial_distribution_summary.csv")
        with open(sp, "w", newline="") as fh:
            w = csv.DictWriter(fh, list(results[0].keys())); w.writeheader(); [w.writerow(r) for r in results]
    return results
