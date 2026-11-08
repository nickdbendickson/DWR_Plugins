"""Hazard 2.23.14 (audit P6, 2026-11-07): the HEC-RAS plan import keeps the per-cell results
(water surface, velocity, times) and the water-surface series blocks in float32 - the precision
HEC-RAS stores - while the cell coordinates, the cell minimum elevation and the depth / wet
arithmetic stay float64. A stand-in HDF reader over arrays (no h5py, no GDAL, no QGIS), so
this runs anywhere; the gridding path is covered by test_rasimport_terrain_arrival.py.

    python3 DamSafety_Hazard/tests/test_rasimport_float32.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from DamSafety_Hazard import rasimport as RI  # noqa: E402

A = "Main"
T_HR = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
ZMIN = np.array([1500.0, 1490.0, 1500.0, 1499.95])          # ND ground, feet
WS = np.array([[1500.0, 1490.0, 1500.0, 1499.95],
               [1500.0, 1495.0, 1500.0, 1499.95],
               [1500.05, 1499.0, 1500.05, 1500.0],
               [1500.08, 1500.5, 1500.08, 1500.2],
               [1500.09, 1501.0, 1500.09, 1500.3]])
MAXWS = np.array([[1500.15, 1501.0, 1500.15, 1500.3], [3.5 / 24, 4.0 / 24, 3.5 / 24, 4.0 / 24]])
XY = np.array([[1234567.5, 345678.25], [1234577.5, 345678.25], [1234587.5, 345678.25],
               [1234597.5, 345678.25]])


class FakePlan(object):
    name = "fake"

    def __init__(self, f32=True):
        g, s, t = RI.GEOM + "/" + A, RI.SUMMARY + "/" + A, RI.TSERIES
        cast = (lambda a: a.astype(np.float32)) if f32 else (lambda a: a.copy())
        self.d = {g + "/Cells Center Coordinate": XY.copy(),
                  g + "/Cells Minimum Elevation": cast(ZMIN),
                  g + "/Cells Surface Area": np.full(4, 100.0),
                  s + "/Maximum Water Surface": cast(MAXWS),
                  s + "/Maximum Face Velocity": cast(np.array([[0.0, 3.5, 7.25, 1.0, 0.5]])),
                  g + "/Cells Face and Orientation Info": np.array([[0, 2], [2, 1], [3, 1], [4, 1]]),
                  g + "/Cells Face and Orientation Values": np.array([[0, 0], [1, 0], [2, 0],
                                                                      [3, 0], [4, 0]]),
                  t + "/Time": T_HR / 24.0,
                  t + "/2D Flow Areas/" + A + "/Water Surface": cast(WS)}

    def has(self, path):
        return path in self.d or path in (RI.GEOM, RI.TSERIES)

    def shape(self, path):
        return self.d[path].shape

    def read(self, path, cols=None):
        a = self.d[path]
        return a.copy() if cols is None else a[:, cols[0]:cols[1]].copy()

    def field(self, path, name):
        return None


def main():
    assert RI.RESULT_DTYPE == np.float32
    c = RI.read_area(FakePlan(), A, False, breach_hr=0.0, arrival_ft=0.1, series=True)
    # dtypes: results float32, coordinates / cell minimum float64
    for f in ("wse", "vel", "peak", "arr"):
        assert c[f].dtype == np.float32, (f, c[f].dtype)
    for f in ("x", "y", "zmin"):
        assert c[f].dtype == np.float64, (f, c[f].dtype)
    assert np.array_equal(c["x"], XY[:, 0]) and np.array_equal(c["y"], XY[:, 1])
    # wet / dry rule in float64: cell 3 is dry at the maximum - 0.35 ft above zmin -> wet;
    # cell 0's maximum 0.15 above ground -> wet; the same answer as the float64 read
    c64 = RI.read_area(FakePlan(f32=False), A, False, series=True)
    old = RI.RESULT_DTYPE
    RI.RESULT_DTYPE = np.float64
    try:
        ref = RI.read_area(FakePlan(f32=False), A, False, series=True)
    finally:
        RI.RESULT_DTYPE = old
    for f in ("wse", "vel", "peak", "arr"):
        a, b = c[f].astype(np.float64), ref[f]
        assert np.array_equal(np.isfinite(a), np.isfinite(b)), f
        m = np.isfinite(a)
        tol = 2e-4 * max(1.0, float(np.nanmax(np.abs(b))))     # float32 of a 1,500 ft WSE
        assert np.all(np.abs(a[m] - b[m]) <= tol), (f, a[m], b[m])
        assert np.array_equal(np.isfinite(c64[f]), np.isfinite(b)), f
    # the arrival index test subtracts float64 zmin from a float32 series: cell 1 passes
    # 0.1 ft of depth at step 1, cell 3 at step 3; cells 0 / 2 never pass it in the series
    # (0.09 ft at most; their maximum falls between output steps)
    first = RI.arrival_days(WS.astype(np.float32), ZMIN, 0.1)
    assert list(first) == [-1, 1, -1, 3], list(first)
    assert np.array_equal(first, RI.arrival_days(WS, ZMIN, 0.1))
    # velocity: cell 0 takes the larger of its two faces (3.5), float32
    assert abs(float(c["vel"][0]) - 3.5) < 1e-6 and abs(float(c["vel"][1]) - 7.25) < 1e-6
    # the code digest names the new fallback writer, so cached imports are rebuilt
    assert "_translate_points" in RI._CODE_FUNCS
    print("ok  rasimport float32 results, float64 elevations and coordinates")


if __name__ == "__main__":
    main()
