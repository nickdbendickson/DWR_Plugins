"""Hazard 2.24.7: 20 m NoData-receptor rule (Nick, 12 Nov 2026; damit PR #67 names).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_nodata_receptors.py

Needs PyQGIS (inits a headless QgsApplication) and GDAL. Synthetic rasters only
(cells at 10, 20 and 30 m; finite 0; footprint; peaks from different cells).
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GDAL_PAM_ENABLED", "NO")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature, QgsField,
    QgsGeometry, QgsPointXY, QgsRasterLayer, QgsRectangle,
)

from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard.compat import FT_INT, FT_STRING, WKB_POINT, WKB_POLYGON  # noqa: E402
from DamSafety_Hazard.processing import make_samplers  # noqa: E402
from DamSafety_Core.damsuite import nodata_sample as ND  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")
CTX = QgsCoordinateTransformContext()
X0, Y0 = 100000.0, 2600000.0
NODATA = -9999.0
US_FT_WKT = (
    'PROJCS["test_usft",GEOGCS["GCS",DATUM["D",SPHEROID["S",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
    'PARAMETER["central_meridian",0],PARAMETER["scale_factor",0.9996],'
    'PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
    'UNIT["US survey foot",0.3048006096012192]]')


def _tif(path, arr, cell=10.0, cell_x=None, cell_y=None, wkt=None, epsg=5070):
    dx = cell if cell_x is None else cell_x
    dy = cell if cell_y is None else cell_y
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((X0, dx, 0.0, Y0 + arr.shape[0] * dy, 0.0, -dy))
    srs = osr.SpatialReference()
    if wkt:
        srs.ImportFromWkt(wkt)
    else:
        srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(NODATA)
    ds.GetRasterBand(1).WriteArray(arr.astype(np.float32))
    ds = None
    return path


def _hole_arrays(dry_centre=False, all_nodata=False):
    dep = np.zeros((5, 5), np.float32)
    vel = np.full((5, 5), 0.5, np.float32)
    if all_nodata:
        dep[:] = NODATA
    else:
        if not dry_centre:
            dep[2, 2] = NODATA
        dep[2, 3], vel[2, 3] = 3.0, 1.0
        dep[1, 2], vel[1, 2] = 2.0, 4.0
    return dep, vel


def _xy(cell, cell_x=None, cell_y=None):
    dx = cell if cell_x is None else cell_x
    dy = cell if cell_y is None else cell_y
    ytop = Y0 + 5.0 * dy
    return X0 + 2.5 * dx, ytop - 2.5 * dy


def _point_house(x, y):
    from DamSafety_Hazard.helpers import create_memory_layer
    nena = [("Add_Number", FT_INT), ("St_Name", FT_STRING), ("Placement", FT_STRING),
            ("Place_Type", FT_STRING)]
    pts = create_memory_layer("NDGISHUB_address_points", WKB_POINT, CRS,
                              [QgsField(n, t) for n, t in nena])
    p = QgsFeature(pts.fields())
    p.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    p.setAttributes([9250, "56TH", "Unknown", "Residence"])
    pts.dataProvider().addFeatures([p])
    # no outline: sampled as a point disc
    house = create_memory_layer("FEMA_Structures", WKB_POLYGON, CRS,
                                [QgsField("building", FT_STRING)])
    return house, pts


def _poly_house(x, y, half=1.0):
    from DamSafety_Hazard.helpers import create_memory_layer
    house = create_memory_layer("FEMA_Structures", WKB_POLYGON, CRS,
                                [QgsField("building", FT_STRING)])
    f = QgsFeature(house.fields())
    f.setGeometry(QgsGeometry.fromRect(QgsRectangle(x - half, y - half, x + half, y + half)))
    f.setAttributes(["house"])
    house.dataProvider().addFeatures([f])
    nena = [("Add_Number", FT_INT), ("St_Name", FT_STRING), ("Placement", FT_STRING),
            ("Place_Type", FT_STRING)]
    pts = create_memory_layer("NDGISHUB_address_points", WKB_POINT, CRS,
                              [QgsField(n, t) for n, t in nena])
    p = QgsFeature(pts.fields())
    p.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    p.setAttributes([9250, "56TH", "Unknown", "Residence"])
    pts.dataProvider().addFeatures([p])
    return house, pts


def _run(tmp, cell=30.0, cell_x=None, cell_y=None, wkt=None, dry_centre=False,
         all_nodata=False, footprint=False, crs=None):
    dep, vel = _hole_arrays(dry_centre=dry_centre, all_nodata=all_nodata)
    kw = dict(cell=cell, cell_x=cell_x, cell_y=cell_y, wkt=wkt)
    dpath = _tif(os.path.join(tmp, "d.tif"), dep, **kw)
    vpath = _tif(os.path.join(tmp, "v.tif"), vel, **kw)
    x, y = _xy(cell, cell_x, cell_y)
    house, pts = (_poly_house(x, y, half=0.2 * (cell_x or cell)) if footprint
                  else _point_house(x, y))
    use_crs = crs or CRS
    structs = ST.build_inventory(house, [("NDGISHUB", pts)], use_crs, CTX)
    assert len(structs) == 1, structs
    smp = make_samplers(QgsRasterLayer(dpath, "d"), QgsRasterLayer(vpath, "v"),
                        QgsRasterLayer(vpath, "a"), 0)
    ST.evaluate_structures(structs, smp, use_crs, CTX, point_radius_ft=1.0, edge_ft=0.0)
    return structs[0]


def test_10m_and_20m_score_dry_and_are_flagged():
    for cell in (10.0, 20.0):
        tmp = tempfile.mkdtemp(prefix="nodata_")
        try:
            rec = _run(tmp, cell=cell)
            assert rec.get("sampling") == ND.SAMPLING_CENTRE_NODATA_DRY, rec.get("sampling")
            assert rec.get("nodata_flag") == ND.FLAG_DRY_REVIEW
            assert rec.get("max_depth") in (None, 0.0) or rec.get("max_depth") == 0
            notes = " ".join(ST.structure_notes(rec))
            assert "nodata_dry_review" in notes
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_30m_uses_neighbourhood_option_c():
    tmp = tempfile.mkdtemp(prefix="nodata_")
    try:
        rec = _run(tmp, cell=30.0)
        assert rec.get("sampling") == ND.SAMPLING_NEIGHBOURHOOD, rec.get("sampling")
        assert rec.get("nodata_flag") == ND.FLAG_NEIGHBOURHOOD
        assert rec.get("max_depth") == 3.0, rec.get("max_depth")
        assert rec.get("max_vel") == 4.0, rec.get("max_vel")
        notes = " ".join(ST.structure_notes(rec))
        assert "nodata_neighbourhood" in notes
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_finite_zero_is_not_filled():
    tmp = tempfile.mkdtemp(prefix="nodata_")
    try:
        rec = _run(tmp, cell=30.0, dry_centre=True)
        assert rec.get("nodata_flag") not in (ND.FLAG_NEIGHBOURHOOD, ND.FLAG_DRY_REVIEW)
        assert rec.get("max_depth") in (None, 0.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_footprint_all_nodata_at_30m():
    tmp = tempfile.mkdtemp(prefix="nodata_")
    try:
        rec = _run(tmp, cell=30.0, footprint=True)
        assert rec.get("sampling") == ND.SAMPLING_NEIGHBOURHOOD, rec.get("sampling")
        assert rec.get("max_depth") == 3.0 and rec.get("max_vel") == 4.0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nonsquare_uses_larger_side():
    tmp = tempfile.mkdtemp(prefix="nodata_")
    try:
        rec = _run(tmp, cell=15.0, cell_x=15.0, cell_y=25.0)
        assert rec.get("sampling") == ND.SAMPLING_NEIGHBOURHOOD
        rec = _run(tmp, cell=15.0, cell_x=15.0, cell_y=20.0)
        assert rec.get("sampling") == ND.SAMPLING_CENTRE_NODATA_DRY
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("{} tests ok".format(len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
