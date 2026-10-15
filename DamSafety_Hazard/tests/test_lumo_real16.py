"""Lumo vReal.16 EV.4 / EAP contours / hhmm (Hazard 2.18.27).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real16.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from DamSafety_Hazard import evacuation as EV  # noqa: E402
from DamSafety_Hazard import eap as EAP  # noqa: E402

NODATA = -9999.0


def _raster(arr, cell=1.0):
    p = os.path.join(tempfile.mkdtemp(), "d.tif")
    ds = gdal.GetDriverByName("GTiff").Create(p, arr.shape[1], arr.shape[0], 1,
                                              gdal.GDT_Float32)
    ds.SetGeoTransform((0.0, cell, 0.0, float(arr.shape[0]) * cell, 0.0, -cell))
    s = osr.SpatialReference()
    s.ImportFromEPSG(26914)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(NODATA)
    b.WriteArray(arr.astype(np.float32))
    ds = None
    return p


class _Sampler:
    """Nearest-cell sampler with the RasterSampler interface used by _dry_nodes."""
    def __init__(self, path):
        ds = gdal.Open(path)
        self.gt = ds.GetGeoTransform()
        self.xsize, self.ysize = ds.RasterXSize, ds.RasterYSize
        a = ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
        a[a == NODATA] = np.nan
        self.a = a

    def values_at(self, xs, ys, max_window_cells=None):
        xs, ys = np.atleast_1d(xs), np.atleast_1d(ys)
        c = np.floor((xs - self.gt[0]) / self.gt[1]).astype(int)
        r = np.floor((ys - self.gt[3]) / self.gt[5]).astype(int)
        out = np.full(xs.shape, np.nan)
        ok = (c >= 0) & (c < self.xsize) & (r >= 0) & (r < self.ysize)
        out[ok] = self.a[r[ok], c[ok]]
        return out


def _old_dry(depth, nx, ny, r, kx, ky):
    import math
    ang = np.arange(8) * (math.pi / 4.0)
    xs = [nx] + [nx + r * kx * math.cos(a) for a in ang]
    ys = [ny] + [ny + r * ky * math.sin(a) for a in ang]
    dry = np.ones(len(nx), dtype=bool)
    for x, y in zip(xs, ys):
        d = depth.values_at(x, y)
        with np.errstate(invalid="ignore"):
            dry &= ~(np.isfinite(d) & (d > 0))
    return dry


def test_ev4_wet_between_ring_points():
    a = np.zeros((101, 101))
    # a 2-cell wet tongue at 22.5 deg (between ring points 0 and 45 deg), 20 ft out
    import math
    cx, cy = 50.5, 50.5
    for rr in np.linspace(17, 23, 30):
        x = cx + rr * math.cos(math.radians(22.5))
        y = cy + rr * math.sin(math.radians(22.5))
        a[100 - int(y), int(x)] = 2.0
    s = _Sampler(_raster(a))
    nx, ny = np.array([cx]), np.array([cy])
    assert _old_dry(s, nx, ny, 20.0, 1.0, 1.0)[0]           # the old 8-point ring missed it
    assert not EV._dry_nodes(s, nx, ny, 20.0, 1.0, 1.0)[0]


def test_ev4_nodata_hole_is_wet_inside_extent():
    a = np.zeros((101, 101))
    a[45:48, 52:55] = NODATA                                # hole inside the disk
    s = _Sampler(_raster(a))
    nx, ny = np.array([50.5, 50.5]), np.array([50.5, 90.5])
    got = EV._dry_nodes(s, nx, ny, 10.0, 1.0, 1.0)
    assert not got[0] and got[1], got
    # outside the raster extent stays dry (beyond the model area)
    assert EV._dry_nodes(s, np.array([200.0]), np.array([200.0]), 5.0, 1.0, 1.0)[0]


def test_ev4_default_off():
    import inspect
    src = inspect.getsource(EV)
    assert "if dry_safe_ft and dry_safe_ft > 0:" in src
    assert EV.DEFAULTS["evac_dry_safe_ft"] == 0.0 if hasattr(EV, "DEFAULTS") else True


def test_hhmm_negative():
    assert EAP.hhmm(1.5) == "1:30"
    assert EAP.hhmm(-1.5) == "-1:30"
    assert EAP.hhmm(-0.25) == "-0:15"
    assert EAP.hhmm(0) == "0:00" and EAP.hhmm(None) == ""


def test_contours_reason():
    assert EAP.write_arrival_contours(os.path.join(tempfile.mkdtemp(), "c.gpkg"), None) is None
    assert "no arrival" in EAP.LAST_CONTOUR_PROBLEM[0]
    orig = getattr(gdal, "ContourGenerateEx")
    try:
        delattr(gdal, "ContourGenerateEx")
    except AttributeError:
        orig = None
    if orig is not None:
        try:
            r = EAP.write_arrival_contours(os.path.join(tempfile.mkdtemp(), "c.gpkg"), "/x.tif")
            assert r is None and "ContourGenerateEx" in EAP.LAST_CONTOUR_PROBLEM[0]
        finally:
            gdal.ContourGenerateEx = orig
    p = _raster(np.full((20, 20), 0.1))                     # nothing crosses the levels
    EAP.write_arrival_contours(os.path.join(tempfile.mkdtemp(), "c.gpkg"), p)
    import inspect
    assert "LAST_CONTOUR_PROBLEM" in inspect.getsource(
        __import__("DamSafety_Hazard.pipeline", fromlist=["x"]))


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", n, e)
    sys.exit(1 if fails else 0)
