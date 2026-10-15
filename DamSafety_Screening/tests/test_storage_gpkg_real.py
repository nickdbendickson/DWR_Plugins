"""Tool 3 under real QGIS writes 3_dams_storage.gpkg WITH its layer (regression: on Python 3.12
the dry-dam handler's locals() lookups kept the sink referenced and the GeoPackage came out with
no layer), and the shallow-basin guard swaps an Absaraka-like dish for the inventory curve.

Needs real PyQGIS + GDAL + scipy (e.g. the QGIS 3.34 python); prints SKIP otherwise."""
import os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SRC = os.path.join(PLUGIN, "algorithms", "estimate_storage.py")

# the static half runs everywhere: no frame-dict lookups left in tool 3
assert "locals()" not in open(SRC, encoding="utf-8").read(), "estimate_storage.py uses locals()"

try:
    import numpy as np
    import scipy  # noqa: F401
    from osgeo import gdal, ogr, osr
    from qgis.core import QgsApplication, QgsProcessingContext, QgsProcessingFeedback
    if not hasattr(QgsApplication, "initQgis"):
        raise ImportError("mock qgis")
except Exception as e:  # noqa: BLE001
    print("SKIP: real QGIS/GDAL/scipy not available ({})".format(e))
    sys.exit(0)

app = QgsApplication([], False)
app.initQgis()
sys.path.insert(0, os.path.dirname(PLUGIN))
pkg = os.path.basename(PLUGIN)
EstimateStorage = __import__(pkg + ".algorithms.estimate_storage",
                             fromlist=["EstimateStorage"]).EstimateStorage

FT, AF = 0.3048, 1233.48183754752
H = 27.5 * FT
CELL, N, X0, Y0 = 10.0, 81, -100000.0, 2000000.0


def srs():
    s = osr.SpatialReference(); s.ImportFromEPSG(5070); return s


def dem(path, z):
    ds = gdal.GetDriverByName("GTiff").Create(path, N, N, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0, CELL, 0, Y0, 0, -CELL)); ds.SetProjection(srs().ExportToWkt())
    ds.GetRasterBand(1).WriteArray(z); ds.GetRasterBand(1).SetNoDataValue(-9999); ds = None


def xy(r, c):
    return X0 + (c + 0.5) * CELL, Y0 - (r + 0.5) * CELL


def vector(path, gtype, fields, feats):
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("l", srs(), gtype)
    for n, t in fields:
        lyr.CreateField(ogr.FieldDefn(n, t))
    for attrs, wkt in feats:
        f = ogr.Feature(lyr.GetLayerDefn())
        for k, v in attrs.items():
            f.SetField(k, v)
        f.SetGeometry(ogr.CreateGeometryFromWkt(wkt)); lyr.CreateFeature(f)
    ds = None


def main():
    d = tempfile.mkdtemp(prefix="t3gpkg_")
    try:
        demdir = os.path.join(d, "2_terrain"); os.makedirs(demdir)
        rr, cc = np.mgrid[0:N, 0:N]
        z = 300.0 + 0.02 * (N - rr)                                   # gentle valley floor
        dish = (np.abs(rr - 30) <= 8) & (np.abs(cc - 40) <= 8)
        z[dish] -= 1.0                                                # a 1 m dish: Absaraka-like
        dem(os.path.join(demdir, "dam_SHALLOW.tif"), z)
        dem(os.path.join(demdir, "dam_DRY.tif"), 300.0 + 0.02 * (N - rr))
        px, py = xy(40, 40)                                           # dam just below the dish
        x1, y1 = xy(38, 32); x2, y2 = xy(22, 48)
        wb = os.path.join(d, "wb.gpkg")
        vector(wb, ogr.wkbPolygon, [], [({}, "POLYGON(({a} {b},{c} {b},{c} {e},{a} {e},{a} {b}))".format(
            a=x1, b=y1, c=x2, e=y2))])
        dams = os.path.join(d, "1_dams.gpkg")
        fl = [("dam_id", ogr.OFTString), ("height_m", ogr.OFTReal), ("inv_max_m3", ogr.OFTReal),
              ("inv_norm_m3", ogr.OFTReal)]
        vector(dams, ogr.wkbPoint, fl, [
            ({"dam_id": "SHALLOW", "height_m": H, "inv_max_m3": 1615.5 * AF, "inv_norm_m3": 0.0},
             "POINT({} {})".format(px, py)),
            ({"dam_id": "DRY", "height_m": H, "inv_max_m3": 500 * AF, "inv_norm_m3": 0.0},
             "POINT({} {})".format(X0 + 5000, Y0 - 5000))])          # no waterbody within reach
        out = os.path.join(d, "3_dams_storage.gpkg")

        alg = EstimateStorage(); alg.initAlgorithm({})
        ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
        p = {"DAMS": dams, "WB": wb, "DEMDIR": demdir, "SEARCH_FT": 500.0, "SHAPE": 1.0,
             "CURVES": os.path.join(d, "3_stage_storage"), "OUT": out}
        assert alg.prepare(p, ctx, fb)
        alg.processAlgorithm(p, ctx, fb)
        del alg, ctx, fb        # no gc.collect(): a reference cycle must not be what frees the sink

        ds = ogr.Open(out)
        assert ds is not None and ds.GetLayerCount() == 1, "3_dams_storage.gpkg has no layer"
        rows = {f.GetField("dam_id"): f for f in ds.GetLayer(0)}
        assert set(rows) == {"SHALLOW", "DRY"}, list(rows)
        assert rows["DRY"].GetField("stor_conf") == "inventory", rows["DRY"].GetField("stor_conf")
        s = rows["SHALLOW"]
        assert s.GetField("stor_conf") == "inventory", (s.GetField("stor_conf"), s.items())
        assert "not the reservoir" in (s.GetField("stor_notes") or ""), s.GetField("stor_notes")
        assert "no waterbody" in (rows["DRY"].GetField("stor_notes") or "")
        assert abs(s.GetField("stor_used_m3") / AF - 1615.5) < 0.5, s.GetField("stor_used_m3")
        print("ok   gpkg has its layer; dry dam and shallow dish both use the inventory curve")
    finally:
        shutil.rmtree(d, ignore_errors=True)


main()
app.exitQgis()
