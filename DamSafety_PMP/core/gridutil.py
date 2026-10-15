"""Small GDAL helpers shared by the spatial-distribution and snowmelt ports."""
import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions(); ogr.UseExceptions()
WGS84 = osr.SpatialReference(); WGS84.ImportFromEPSG(4326)
WGS84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)


def gdb_raster(gdb, name):
    return "OpenFileGDB:%s:%s" % (gdb, name)


def gdb_raster_names(gdb):
    ds = gdal.Open(gdb)
    md = ds.GetMetadata("SUBDATASETS") or {}
    return sorted(v.rsplit(":", 1)[1] for k, v in md.items() if k.endswith("_NAME"))


def read(path):
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    if nd is not None:
        a[(a == nd) | (np.abs(a) > 1e37)] = np.nan
    return a, ds.GetGeoTransform()


def basin_wgs84(basin_path, layer=None):
    """Union of all basin polygons as an OGR geometry in WGS84 lon/lat."""
    ds = ogr.Open(basin_path)
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    srs = lyr.GetSpatialRef()
    g = None
    for f in lyr:
        gg = f.GetGeometryRef().Clone()
        g = gg if g is None else g.Union(gg)
    if srs is not None:
        srs = srs.Clone(); srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        g.Transform(osr.CoordinateTransformation(srs, WGS84))
    return g


def mask_on_grid(geom, gt, shape):
    """Boolean mask of cells whose centres fall in geom (arcpy env.mask behaviour); if no centre
    falls inside (basin smaller than a cell) fall back to every touched cell."""
    drv = gdal.GetDriverByName("MEM")
    out = None
    for all_touched in (False, True):
        r = drv.Create("", shape[1], shape[0], 1, gdal.GDT_Byte)
        r.SetGeoTransform(gt); r.SetProjection(WGS84.ExportToWkt())
        mds = ogr.GetDriverByName("Memory").CreateDataSource("m")
        ml = mds.CreateLayer("b", WGS84, ogr.wkbMultiPolygon)
        f = ogr.Feature(ml.GetLayerDefn()); f.SetGeometry(geom); ml.CreateFeature(f)
        gdal.RasterizeLayer(r, [1], ml, burn_values=[1],
                            options=["ALL_TOUCHED=TRUE"] if all_touched else [])
        out = r.GetRasterBand(1).ReadAsArray().astype(bool)
        if out.any():
            return out, all_touched
    return out, True


def bilinear(a, gt, x, y):
    """Bilinear sample at lon/lat arrays (ArcGIS ExtractMultiValuesToPoints BILINEAR); NaN outside/NoData."""
    x = np.asarray(x, float); y = np.asarray(y, float)
    c = (x - gt[0]) / gt[1] - 0.5
    r = (y - gt[3]) / gt[5] - 0.5
    c0 = np.floor(c).astype(int); r0 = np.floor(r).astype(int)
    fc, fr = c - c0, r - r0
    out = np.full(x.shape, np.nan)
    H, W = a.shape
    for i in range(x.size):
        if not (0 <= r0.flat[i] < H - 1 and 0 <= c0.flat[i] < W - 1):
            # edge: nearest cell if inside
            rr, cc = int(np.floor((y.flat[i] - gt[3]) / gt[5])), int(np.floor((x.flat[i] - gt[0]) / gt[1]))
            if 0 <= rr < H and 0 <= cc < W:
                out.flat[i] = a[rr, cc]
            continue
        q = a[r0.flat[i]:r0.flat[i] + 2, c0.flat[i]:c0.flat[i] + 2]
        w = np.array([[(1 - fr.flat[i]) * (1 - fc.flat[i]), (1 - fr.flat[i]) * fc.flat[i]],
                      [fr.flat[i] * (1 - fc.flat[i]), fr.flat[i] * fc.flat[i]]])
        ok = ~np.isnan(q)
        if ok.any():
            out.flat[i] = float((q[ok] * w[ok]).sum() / w[ok].sum())
    return out


def write_tif(path, a, gt, nodata=-9999.0):
    d = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32,
                                             ["COMPRESS=DEFLATE"])
    d.SetGeoTransform(gt); d.SetProjection(WGS84.ExportToWkt())
    b = d.GetRasterBand(1); b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isnan(a), nodata, a)); d.FlushCache(); d = None
    return path
