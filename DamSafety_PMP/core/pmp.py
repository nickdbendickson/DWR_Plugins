"""
Port of the PMP calculation in ND_PMP_Tool.py (AWA, v1, ArcGIS 10.7.1 / Py2.7) to GDAL/OGR + numpy.

Faithfully reproduces (see README "Logic mapping"):
  createPMPfc   - Vector_Grid cells INTERSECTing the basin -> Grid_Points whose centre is in those cells
  getAOIarea    - planar area; geographic basins projected to ESRI:102039; m2/ft2 -> sq mi; rounding chain
  dadLookup     - linear interpolation / odd extrapolation exactly as the original
  storm loop    - adjRain = round(round(DAD,3) * TAF, 1) where TRANS == 1; strict '>' keeps the first max
  basinAve      - arithmetic or area-weighted (clipped Vector_Grid Shape_Area in the grid's CRS) average, round 2
  subbasin averages, depth-duration chart, PMP point layer, 0.025 deg rasters.
Temporal distributions: see temporal.py. NOT ported: checkTemporal table, Spatial Distribution and Snowmelt tools.
"""
import os
import csv
import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()

STORM_TYPES = ("Local", "General", "CoolSeason")
DEFAULT_DURATIONS = {
    "Local": ["01", "02", "03", "04", "05", "06", "12", "24"],
    "General": ["01", "02", "03", "04", "05", "06", "12", "24", "48", "72"],
    "CoolSeason": ["01", "02", "03", "04", "05", "06", "12", "24", "48", "72"],
}
SQM_TO_SQMI = 0.000000386102     # constants copied from the original script
SQFT_TO_SQMI = 0.00000003587
CELL = 0.025


def py2round(x, n=0):
    """Python 2.7 round(): half away from zero on the exact binary value (Py3 round is half-to-even)."""
    if x is None:
        raise TypeError("round(None)")
    q = Decimal(1).scaleb(-n)
    return float(Decimal(x).quantize(q, rounding=ROUND_HALF_UP))


def _log(cb, msg):
    if cb:
        cb(msg)


@dataclass
class PMPConfig:
    basin_path: str                 # any OGR polygon source (shp/gpkg/...)
    data_home: str                  # the 'PMP_Evaluation_Tool' folder (contains Input/)
    out_dir: str
    basin_layer: str = None         # layer name inside basin_path (None = first)
    durations: dict = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_DURATIONS.items()})
    use_basin_area: bool = True     # original param 6; False -> use custom_area (param 7)
    custom_area: float = None
    weighted: bool = True           # param 8
    subbasin_field: str = None      # param 9/10 (None = no subbasin table)
    chart: bool = True              # param 11
    temporal: bool = True           # param 12 (runTemporal)
    write_rasters: bool = True
    basin_name: str = None


# --------------------------------------------------------------------------- data access
def _gdb(home, name):
    p = os.path.join(home, "Input", name)
    if not os.path.isdir(p):
        raise FileNotFoundError("Missing geodatabase: %s" % p)
    return p


def list_storms(home, storm_type):
    """Equivalent of arcpy.ListFeatureClasses('', 'Point', stormType) in Storm_Adj_Factors.gdb."""
    ds = gdal.OpenEx(_gdb(home, "Storm_Adj_Factors.gdb"), gdal.OF_VECTOR)
    grp = ds.GetRootGroup().OpenGroup(storm_type)
    return list(grp.GetVectorLayerNames())


def read_dad(home, storm):
    """Return list of rows (dicts) in table order (OBJECTID order, as the arcpy SearchCursor would)."""
    ds = ogr.Open(_gdb(home, "DAD_Tables.gdb"))
    lyr = ds.GetLayerByName(storm)
    if lyr is None:
        return None
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(i).GetName() for i in range(defn.GetFieldCount())]
    rows = []
    for f in lyr:
        rows.append({n: f.GetField(n) for n in names})
    return rows


def dad_lookup(rows, duration, area, log=None):
    """Exact port of dadLookup(). Returns None where the original would return None or raise TypeError
    (missing duration field / null values) - the caller then skips the storm for this duration."""
    fld = "H_" + duration
    if not rows or fld not in rows[0]:
        return None
    try:
        x1 = rows[0]["AREASQMI"]; y1 = rows[0][fld]
        xflag = False
        x2 = y2 = None
        for r in rows[1:]:
            if r["AREASQMI"] < area:
                x1 = r["AREASQMI"]; y1 = r[fld]
            else:
                xflag = True
                x2 = r["AREASQMI"]; y2 = r[fld]
                break
        if not xflag:
            _log(log, "    basin area %s > largest DAD area %s: extrapolated" % (area, x1))
            return x1 / area * y1          # sic: same formula as original (y = x1/x2*y1, x2 = area)
        return y1 + (area - x1) * (y2 - y1) / (x2 - x1)
    except TypeError:
        return None


# --------------------------------------------------------------------------- geometry
def _srs4326():
    s = osr.SpatialReference(); s.ImportFromEPSG(4326)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def _open_basin(cfg):
    ds = ogr.Open(cfg.basin_path)
    lyr = ds.GetLayerByName(cfg.basin_layer) if cfg.basin_layer else ds.GetLayer(0)
    if ogr.GT_Flatten(lyr.GetGeomType()) not in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        raise ValueError("Input basin must be a polygon layer")
    return ds, lyr


def _features(lyr, where=None):
    lyr.SetAttributeFilter(where)
    lyr.ResetReading()
    out = [(f, f.GetGeometryRef().Clone()) for f in lyr]
    lyr.SetAttributeFilter(None)
    return out


def _union(geoms):
    u = None
    for g in geoms:
        u = g.Clone() if u is None else u.Union(g)
    return u


def _to(geom, src, dst):
    g = geom.Clone()
    if src is not None and not src.IsSame(dst):
        g.Transform(osr.CoordinateTransformation(src, dst))
    return g


def aoi_area(geoms, srs, log=None):
    """Port of getAOIarea(): returns (raw_sqmi_rounded3)."""
    if srs is None:
        raise ValueError("Basin layer has no CRS")
    srs = srs.Clone(); srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if srs.IsGeographic():
        alb = osr.SpatialReference(); alb.SetFromUserInput("ESRI:102039")
        alb.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        a = sum(_to(g, srs, alb).GetArea() for g in geoms) * SQM_TO_SQMI
        _log(log, "Basin is geographic; projected to ESRI:102039 for area")
    else:
        unit = srs.GetLinearUnitsName()
        f = srs.GetLinearUnits()
        if abs(f - 1.0) < 1e-12:
            a = sum(g.GetArea() for g in geoms) * SQM_TO_SQMI
        else:
            # original: `elif srunitname == "Foot" or "Foot_US"` is always True -> any non-metre unit is treated as feet
            a = sum(g.GetArea() for g in geoms) * SQFT_TO_SQMI
            if unit.lower() not in ("foot", "foot_us", "us survey foot", "foot (international)"):
                _log(log, "WARNING: linear unit '%s' treated as feet (same as original script)" % unit)
    return py2round(a, 3)


class Grid:
    """Vector_Grid cells + Grid_Points, held in memory (68,277 cells)."""

    def __init__(self, home):
        ds = ogr.Open(_gdb(home, "Non_Storm_Data.gdb"))
        self._ds = ds
        self.vg = ds.GetLayerByName("Vector_Grid")
        self.vg_srs = self.vg.GetSpatialRef().Clone()
        self.vg_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        gp = ds.GetLayerByName("Grid_Points")
        self.points = {}   # Id -> (x, y, fid order)
        order = []
        for f in gp:
            g = f.GetGeometryRef()
            pid = int(f.GetField("Id"))
            self.points[pid] = (g.GetX(), g.GetY())
            order.append(pid)
        self.point_order = order

    def cells_intersecting(self, geom4326):
        """Return {cell_id: cell_geometry} for Vector_Grid cells intersecting geom (INTERSECT)."""
        self.vg.SetSpatialFilter(geom4326)
        out = {}
        for f in self.vg:
            g = f.GetGeometryRef()
            if g.Intersects(geom4326):
                out[int(f.GetField("Id"))] = (g.Clone(), f.GetField("TRANS_ZONE"))
        self.vg.SetSpatialFilter(None)
        return out

    def points_in_cells(self, cells):
        """HAVE_THEIR_CENTER_IN: grid points lying inside one of the selected cells. Returns Ids in table order."""
        sel = []
        cell_geoms = [c[0] for c in cells.values()]
        env = None
        for g in cell_geoms:
            e = g.GetEnvelope()
            env = e if env is None else (min(env[0], e[0]), max(env[1], e[1]), min(env[2], e[2]), max(env[3], e[3]))
        if env is None:
            return sel
        u = _union(cell_geoms)
        for pid in self.point_order:
            x, y = self.points[pid]
            if env[0] <= x <= env[1] and env[2] <= y <= env[3]:
                p = ogr.Geometry(ogr.wkbPoint); p.AddPoint_2D(x, y)
                if p.Within(u):
                    sel.append(pid)
        return sel


def read_storm_factors(home, storm, ids, bbox=None):
    """Return {Id: (TAF, TRANS)} for the selected point ids from Storm_Adj_Factors.gdb/<storm>."""
    ds = ogr.Open(_gdb(home, "Storm_Adj_Factors.gdb"))
    lyr = ds.GetLayerByName(storm)
    if bbox:
        lyr.SetSpatialFilterRect(*bbox)
    want = set(ids)
    out = {}
    for f in lyr:
        i = int(f.GetField("ID"))
        if i in want:
            out[i] = (f.GetField("TAF"), f.GetField("TRANS"))
    return out


# --------------------------------------------------------------------------- averages
def weighted_weights(grid, ids, basin4326):
    """Port of basinAve(weighted): WEIGHT = Shape_Area of Vector_Grid cell clipped to basin, in the grid's CRS
    (decimal degrees^2, as ArcGIS Clip output in the scratch GDB inherits the Vector_Grid CRS)."""
    cells = grid.cells_intersecting(basin4326)
    w = {}
    for cid, (g, _z) in cells.items():
        inter = g.Intersection(basin4326)
        w[cid] = inter.GetArea() if inter is not None else 0.0
    # points selected = points intersecting the (re)selected cells
    sel = set(grid.points_in_cells(cells))
    return [(pid, w.get(pid)) for pid in ids if pid in sel]


def basin_average(values_by_id, ids, grid, basin4326, weighted, subbasin_mode):
    if weighted:
        pw = weighted_weights(grid, ids, basin4326)
        v = np.array([values_by_id[p] for p, _ in pw], float)
        wt = np.array([x for _, x in pw], float)
        return py2round(float(np.average(v, weights=wt)), 2)
    if subbasin_mode:
        cells = grid.cells_intersecting(basin4326)
        sel = set(grid.points_in_cells(cells))
        v = np.array([values_by_id[p] for p in ids if p in sel], float)
        return py2round(float(np.average(v)), 2)
    v = np.array([values_by_id[p] for p in ids], float)
    return py2round(float(np.average(v)), 2)


# --------------------------------------------------------------------------- outputs
def _write_points(path, layer_name, grid, ids, durs, pmp, drv):
    drv_ = ogr.GetDriverByName("GPKG")
    ds = ogr.Open(path, 1) if os.path.exists(path) else drv_.CreateDataSource(path)
    if ds.GetLayerByName(layer_name) is not None:
        ds.DeleteLayer(layer_name)
    lyr = ds.CreateLayer(layer_name, _srs4326(), ogr.wkbPoint)
    lyr.CreateField(ogr.FieldDefn("ID", ogr.OFTInteger))
    for d in durs:
        lyr.CreateField(ogr.FieldDefn("PMP_" + d, ogr.OFTReal))
    for d in durs:
        fd = ogr.FieldDefn("STORM_" + d, ogr.OFTString); fd.SetWidth(16); lyr.CreateField(fd)
    lyr.StartTransaction()
    for k, pid in enumerate(ids):
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("ID", pid)
        for d in durs:
            f.SetField("PMP_" + d, pmp[d][k]); f.SetField("STORM_" + d, drv[d][k])
        x, y = grid.points[pid]
        g = ogr.Geometry(ogr.wkbPoint); g.AddPoint_2D(x, y); f.SetGeometry(g)
        lyr.CreateFeature(f)
    lyr.CommitTransaction()
    ds = None


def _write_raster(path, grid, ids, values):
    """0.025-deg GeoTIFF with cell centres on the grid points (ArcGIS FeatureToRaster snapping not verified)."""
    xs = np.array([grid.points[p][0] for p in ids]); ys = np.array([grid.points[p][1] for p in ids])
    x0 = xs.min() - CELL / 2; y1 = ys.max() + CELL / 2
    nx = int(round((xs.max() - xs.min()) / CELL)) + 1
    ny = int(round((ys.max() - ys.min()) / CELL)) + 1
    arr = np.full((ny, nx), -9999.0, np.float32)
    for x, y, v in zip(xs, ys, values):
        arr[int(round((y1 - CELL / 2 - y) / CELL)), int(round((x - x0 - CELL / 2) / CELL))] = v
    ds = gdal.GetDriverByName("GTiff").Create(path, nx, ny, 1, gdal.GDT_Float32, ["COMPRESS=DEFLATE"])
    ds.SetGeoTransform((x0, CELL, 0, y1, 0, -CELL))
    ds.SetProjection(_srs4326().ExportToWkt())
    b = ds.GetRasterBand(1); b.SetNoDataValue(-9999.0); b.WriteArray(arr)
    ds = None


def _chart(path, title, durs, vals):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    x = [int(d) for d in durs]
    ax.plot(x, vals); ax.set_xlabel("Storm Duration in Hours"); ax.set_ylabel("Rainfall Depth in Inches")
    ax.set_title(title); ax.grid(True); ax.set_ylim(top=max(vals) + 1); ax.set_xticks(x)
    fig.savefig(path); plt.close(fig)


# --------------------------------------------------------------------------- main
def run_pmp(cfg: PMPConfig, log=print, is_canceled=lambda: False):
    """Run the PMP analysis for each storm type with a non-empty duration list. Returns a results dict."""
    home = cfg.data_home
    bds, blyr = _open_basin(cfg)
    bsrs = blyr.GetSpatialRef()
    basin_name = cfg.basin_name or os.path.splitext(os.path.basename(cfg.basin_path))[0]
    feats = _features(blyr)
    geoms = [g for _, g in feats]
    grid = Grid(home)
    bsrs_t = bsrs.Clone(); bsrs_t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    basin4326 = _to(_union(geoms), bsrs_t, grid.vg_srs)
    results = {"basin": basin_name, "types": {}}

    raw_area = aoi_area(geoms, bsrs, log)
    _log(log, "Area of interest: %s square miles." % raw_area)
    area = raw_area if cfg.use_basin_area else float(cfg.custom_area)
    area = py2round(py2round(area, 1), 2)
    _log(log, "***Area used for PMP analysis: %s sqmi***" % area)
    results["area_sqmi_raw"] = raw_area; results["area_sqmi"] = area

    cells = grid.cells_intersecting(basin4326)
    ids = grid.points_in_cells(cells)
    if not ids:
        raise RuntimeError("No data present within basin/AOI area.")
    _log(log, "(%d grid points will be analyzed)" % len(ids))

    summary = []
    for stype in STORM_TYPES:
        durs = cfg.durations.get(stype) or []
        if not durs:
            continue
        if area > 100 and stype == "Local":
            _log(log, "***Warning - Local storm PMP depths only valid for basins 100 square miles or smaller***")
        storms = list_storms(home, stype)
        dad = {s: read_dad(home, s) for s in storms}
        xs = [grid.points[i][0] for i in ids]; ys = [grid.points[i][1] for i in ids]
        bbox = (min(xs) - 0.001, min(ys) - 0.001, max(xs) + 0.001, max(ys) + 0.001)
        fac = {s: read_storm_factors(home, s, ids, bbox) for s in storms}
        missing = [s for s in storms if len(fac[s]) != len(ids)]
        if missing:
            raise RuntimeError("Storm factor layers missing grid points: %s" % missing)
        pmp, drv, dadv = {}, {}, {}
        for d in durs:
            if is_canceled():
                raise RuntimeError("Canceled")
            _log(log, "Evaluating %s %s-hour duration..." % (stype, d))
            vals = [0.0] * len(ids); drivers = ["STORM"] * len(ids)
            for s in list(storms):
                y = dad_lookup(dad[s], d, area, log)
                if y is None:
                    _log(log, "  Duration '%s-hour' is not present for %s." % (d, s))
                    continue
                y = py2round(y, 3)
                dadv.setdefault(d, {})[s] = y
                trans = 0
                for k, pid in enumerate(ids):
                    taf, tr = fac[s][pid]
                    if tr == 1:
                        trans += 1
                        adj = py2round(y * taf, 1)
                        if adj > vals[k]:
                            vals[k] = adj; drivers[k] = s
                if trans == 0:
                    storms.remove(s)   # same as original: storm dropped for later durations
            pmp[d] = vals; drv[d] = drivers

        out_type_dir = os.path.join(cfg.out_dir, stype)
        os.makedirs(out_type_dir, exist_ok=True)
        out_area = "%dsqmi" % int(py2round(area, 0))
        gpkg = os.path.join(out_type_dir, "PMP_%s_%s.gpkg" % (basin_name, out_area))
        _write_points(gpkg, "%s_PMP_Points_%s_%s" % (stype, basin_name, out_area), grid, ids, durs, pmp, drv)
        rasters = []
        for d in (durs if cfg.write_rasters else []):
            rp = os.path.join(out_type_dir, "%s_%s_%s_%s.tif" % (stype[0], d, basin_name, out_area))
            _write_raster(rp, grid, ids, pmp[d]); rasters.append(rp)

        tdir = os.path.join(out_type_dir, "CSV_%s_%s" % (basin_name, out_area)); os.makedirs(tdir, exist_ok=True)
        # subbasin averages
        sub_rows = []
        if cfg.subbasin_field:
            for f, g in feats:
                sid = f.GetField(cfg.subbasin_field)
                g4326 = _to(g, bsrs_t, grid.vg_srs)
                row = {"STORM_TYPE": stype, "SUBBASIN": str(sid)}
                for d in durs:
                    vb = dict(zip(ids, pmp[d]))
                    row["PMP_" + d] = basin_average(vb, ids, grid, g4326, cfg.weighted, True)
                sub_rows.append(row)
            _write_csv(os.path.join(tdir, "%s_PMP_Subbasin_Average_%s.csv" % (stype, out_area)), sub_rows)
        bas = {"STORM_TYPE": stype}
        for d in durs:
            vb = dict(zip(ids, pmp[d]))
            bas["PMP_" + d] = basin_average(vb, ids, grid, basin4326, cfg.weighted, bool(cfg.subbasin_field))
        _write_csv(os.path.join(tdir, "%s_PMP_Basin_Average_%s.csv" % (stype, out_area)), [bas])
        chart = None
        if cfg.chart:
            try:
                import matplotlib  # noqa
            except ImportError:
                _log(log, "matplotlib not available - depth-duration chart skipped"); cfg.chart = False
        if cfg.chart:
            chart = os.path.join(out_type_dir, "%s_%s_Depth_Duration_Chart.png" % (basin_name, stype))
            _chart(chart, "%s (%s) %s Storm Basin Average PMP\nDepth Duration Chart" % (basin_name, out_area, stype),
                   durs, [bas["PMP_" + d] for d in durs])
        controlling = {d: sorted(set(drv[d])) for d in durs}
        from . import temporal as T
        for d in durs:
            summary.append([int(d), bas["PMP_" + d], T.TYPE_LABEL[stype], T.controlling_storm(drv[d])])
        pats, notes, hyeto = [], [], []
        if cfg.temporal:
            P = {d: bas["PMP_" + d] for d in durs}
            pats, notes = T.build_patterns(home, stype, durs, P, drv, log)
            for p in pats:
                hyeto.append(T.write_hyetograph(cfg.out_dir, p, area, P[p.duration]))
        results["types"][stype] = {"gpkg": gpkg, "rasters": rasters, "basin_average": bas,
                                   "subbasin_average": sub_rows, "chart": chart, "controlling_storms": controlling,
                                   "dad_values": dadv, "patterns": pats, "temporal_notes": notes, "hyetographs": hyeto, "n_points": len(ids), "point_ids": ids, "pmp": pmp}
        _log(log, "%s basin average: %s" % (stype, bas))
    from . import temporal as T
    os.makedirs(cfg.out_dir, exist_ok=True)
    results["summary_csv"] = T.write_summary(cfg.out_dir, summary)
    return results


def _write_csv(path, rows):
    if not rows:
        return
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
