"""Three-way classification parity, whole-plugin leg (audit P1, 2026-11-08).

DamSafety_Runner/tests/test_classification_parity.py proves on numpy arrays that the Hazard's
array-level rules (fastsample cell selection, peak pair, the digitized table), the Runner's
Sampler + core/nd_hazard and the Screening's core/hazard give the same zone for the same
structures on the same rasters. This script runs the SAME synthetic flood and structure set
through the Hazard plugin's real path - GeoTIFFs, QgsGeometry footprints, RasterSampler,
structures.evaluate_structures (GEOS regions and the fast path) - and checks that its zones
equal the numpy legs' expected zones, so nothing between the array-level rule and the plugin
(the GEOS buffer, the rasteriser, the depth / velocity readers) breaks the parity.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_classification_parity.py

Needs PyQGIS and GDAL; without them it prints SKIP and exits 0 (CI's plugin-suites job runs it
through DamSafety_Hazard/tests/run_all.py).
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

try:
    from osgeo import gdal, osr  # noqa: E402
    from qgis.core import QgsApplication  # noqa: E402
except ImportError as e:
    print("SKIP: {} (needs PyQGIS and GDAL; CI's plugin-suites job runs this)".format(e))
    sys.exit(0)

_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsFeature, QgsField,
    QgsGeometry, QgsPointXY, QgsRasterLayer,
)

from DamSafety_Hazard import structures as ST  # noqa: E402
from DamSafety_Hazard.compat import FT_STRING, WKB_POINT, WKB_POLYGON  # noqa: E402
from DamSafety_Hazard.constants import CLASS_HIGH, CLASS_JUDGMENT, CLASS_LOW  # noqa: E402
from DamSafety_Hazard.helpers import create_memory_layer  # noqa: E402
from DamSafety_Hazard.processing import make_samplers  # noqa: E402
from nd_breach_runner.tests import test_classification_parity as P  # noqa: E402

CRS = QgsCoordinateReferenceSystem("EPSG:5070")                 # metres, as the fixture
CTX = QgsCoordinateTransformContext()
NODATA = -9999.0
ZONE_NAME = {CLASS_HIGH: "high danger", CLASS_JUDGMENT: "judgment", CLASS_LOW: "low danger"}


def _tif(path, arr, gt):
    ds = gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).SetNoDataValue(NODATA)
    ds.GetRasterBand(1).WriteArray(arr.astype(np.float32))
    ds = None


def _layers(structs):
    """A FEMA-style outline layer and an NSI-style point layer of the fixture's structures;
    the mobile homes carry an occupancy the inventory reads as a manufactured home."""
    polys = create_memory_layer("FEMA_Structures", WKB_POLYGON, CRS,
                                [QgsField("OCC_CLS", FT_STRING), QgsField("PRIM_OCC", FT_STRING),
                                 QgsField("BUILD_ID", FT_STRING)])
    pts = create_memory_layer("NSI", WKB_POINT, CRS,
                              [QgsField("occtype", FT_STRING), QgsField("fd_id", FT_STRING)])
    for sid, kind, geom, mobile, _note in structs:
        if kind == "polygon":
            f = QgsFeature(polys.fields())
            f.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in geom]]))
            f.setAttributes(["Residential", "Manufactured Home" if mobile else
                             "Single Family Dwelling", sid])
            polys.dataProvider().addFeatures([f])
        else:
            f = QgsFeature(pts.fields())
            f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*geom)))
            f.setAttributes(["RES2" if mobile else "RES1-1SNB", sid])
            pts.dataProvider().addFeatures([f])
    return polys, pts


def _by_name(structs, recs):
    """{fixture id: rec} by location (the inventory renumbers ids)."""
    out = {}
    for sid, kind, geom, _m, _n in structs:
        if kind == "polygon":
            cx = sum(p[0] for p in geom) / len(geom)
            cy = sum(p[1] for p in geom) / len(geom)
        else:
            cx, cy = geom
        best = min(recs, key=lambda r: r["geom"].distance(QgsGeometry.fromPointXY(
            QgsPointXY(cx, cy))))
        out[sid] = best
    return out


def main():
    d, v = P.rasters()
    gt = (0.0, 1.0, 0.0, 60.0, 0.0, -1.0)
    structs = P.structures()
    # The plugin reads the float32 GeoTIFFs written below; sample the numpy reference leg from the
    # SAME float32 values. Otherwise a fixture sitting exactly on a zone boundary disagrees for
    # identical data: on_line's 2.916 ft is 2.9159998 in float32, just below the 2.916 ft judgment
    # threshold, so a float64 reference reads 'judgment' while the float32 plugin reads 'low'. In
    # production every leg (Hazard, Runner, Screening) reads float32 rasters and agrees.
    d, v = d.astype(np.float32), v.astype(np.float32)
    expected = P.hazard_leg(d, v, gt, structs)             # the numpy leg (== Runner, Screening)
    tmp = tempfile.mkdtemp(prefix="parity_")
    fails = []
    try:
        dpath, vpath = os.path.join(tmp, "d.tif"), os.path.join(tmp, "v.tif")
        _tif(dpath, d, gt)
        _tif(vpath, v, gt)
        polys, pts = _layers(structs)
        for fast in (False, True):
            recs = ST.build_inventory(polys, [("NSI", pts)], CRS, CTX)
            assert len(recs) == len(structs), (len(recs), len(structs))
            smp = make_samplers(QgsRasterLayer(dpath, "d"), QgsRasterLayer(vpath, "v"),
                                QgsRasterLayer(vpath, "a"), 0)
            ST.evaluate_structures(recs, smp, CRS, CTX, fast_sampling=fast)
            got = _by_name(structs, recs)
            for sid, kind, geom, mobile, note in structs:
                rec = got[sid]
                want_zone, want_d, want_v = expected[sid]
                zone = None if rec.get("max_depth") in (None, 0) or not rec["max_depth"] > 0 \
                    else ZONE_NAME.get(rec.get("zone"))
                if rec.get("acer_type") != (ST.TYPE_MOBILE if mobile else ST.TYPE_HOUSE):
                    fails.append("{} fast={}: type {}".format(sid, fast, rec.get("acer_type")))
                if zone != want_zone:
                    fails.append("{} fast={} ({}): Hazard plugin zone {} != {}".format(
                        sid, fast, note, zone, want_zone))
                # Exact depth / velocity on the fast (fastsample, analytic-disc) path, which the
                # reference leg mirrors. The default GEOS path differs from the analytic disc by the
                # documented arc/chord gap of GEOS buffers (structures.evaluate_structures, v2.9):
                # the buffer polygon lies INSIDE the true circle, so it reads a conservative SUBSET
                # of the edge cells (point_disc: GEOS reaches 0.92 ft/s where the disc reaches
                # 0.94). On the GEOS path require only that it did not read MORE than the analytic
                # disc; the zone, checked above on both paths, is what must agree.
                if want_d is not None:
                    if fast:
                        off = (abs(rec["max_depth"] - want_d) > 1e-4 or
                               abs(rec["max_vel"] - want_v) > 1e-4)
                    else:
                        off = (rec["max_depth"] > want_d + 1e-4 or rec["max_vel"] > want_v + 1e-4)
                    if off:
                        fails.append("{} fast={}: {} ft / {} ft/s vs {} / {}".format(
                            sid, fast, rec["max_depth"], rec["max_vel"], want_d, want_v))
                print("{:4} {:12s} fast={!s:5} zone {!s:12} {} ft {} ft/s".format(
                    "ok" if not fails or not fails[-1].startswith(sid) else "FAIL", sid, fast,
                    zone, rec.get("max_depth"), rec.get("max_vel")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("\n".join(fails))
        print("\n{} failure(s)".format(len(fails)))
        return 1
    print("ALL OK: the Hazard plugin's zones equal the Runner / Screening legs on the fixture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
