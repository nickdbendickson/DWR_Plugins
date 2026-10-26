"""Hazard 2.18.61: HEC-RAS plan import - arrival on the same depth basis as the depth.

'Import HEC-RAS plan results' (rasimport.import_plan / ndhazard:hecrasplan / plan scenarios):
  * with a terrain raster, depth AND arrival are the cell's water surface minus the terrain
    under each pixel (arrival interpolated between output steps; a pixel reached only between
    steps gets the cell's time of the maximum) - as the Runner 3.10.79 (#173);
  * without one, the Runner's terrain-based rasters of the plan (a re-export, or the dam
    folder's rasters with hecras_stage.json depth_basis 'terrain') are used;
  * else depth from the cell minimum, with CELL_MIN_WARNING;
  * the basis (terrain / runner_reexport / cell_min) is in the result, depth_basis.json and
    the cache manifest.
A 3-cell synthetic plan through a stand-in HDF reader (no h5py, no HEC-RAS); the middle cell
holds a 2 ft wide ditch 10 ft below its banks.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_rasimport_terrain_arrival.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from osgeo import gdal, osr
except ImportError:
    print("SKIP: GDAL is not installed")
    sys.exit(0)
from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.initQgis()

import numpy as np  # noqa: E402

from DamSafety_Hazard import rasimport as RI  # noqa: E402

gdal.UseExceptions()
SRS = osr.SpatialReference()
SRS.ImportFromEPSG(2265)                 # NAD83 / North Dakota North (ftUS)
WKT = SRS.ExportToWkt()
A = "Main"
T_HR = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
# cells at x = 5, 15, 25 (10 ft squares, y 0..10); cell 1 is the ditch cell
ZMIN = np.array([100.0, 90.0, 100.0])
WS = np.array([[100.0, 90.0, 100.0],
               [100.0, 95.0, 100.0],
               [100.05, 99.0, 100.05],
               [100.08, 100.5, 100.08],
               [100.09, 101.0, 100.09]])
# summary maximum (row 0) and its time in days (row 1): cells 0 / 2 peak between steps
MAXWS = np.array([[100.15, 101.0, 100.15], [3.5 / 24, 4.0 / 24, 3.5 / 24]])


class FakePlan(object):
    """The reader interface of rasimport._H5 over a dict."""
    name = "fake"

    def __init__(self, path):
        g, s, t = RI.GEOM + "/" + A, RI.SUMMARY + "/" + A, RI.TSERIES
        self.d = {g + "/Cells Center Coordinate": np.array([[5.0, 5.0], [15.0, 5.0], [25.0, 5.0]]),
                  g + "/Cells Minimum Elevation": ZMIN.copy(),
                  g + "/Cells Surface Area": np.full(3, 100.0),
                  s + "/Maximum Water Surface": MAXWS.copy(),
                  t + "/Time": T_HR / 24.0,
                  t + "/2D Flow Areas/" + A + "/Water Surface": WS.copy()}

    def groups(self, path):
        return [A] if path == RI.GEOM else []

    def has(self, path):
        return path in self.d or path in (RI.GEOM, RI.TSERIES)

    def shape(self, path):
        return self.d[path].shape

    def read(self, path, cols=None):
        a = self.d[path]
        return a.copy() if cols is None else a[:, cols[0]:cols[1]].copy()

    def attr(self, path, name):
        return {"Units System": "US Customary", "Projection": WKT}.get(name) \
            if path == "/" else None

    def field(self, path, name):
        return None

    def close(self):
        pass


RI.open_hdf = FakePlan


def _tif(path, a, gt=(0.0, 1.0, 0.0, 10.0, 0.0, -1.0), nd=-9999.0):
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    ds.SetProjection(WKT)
    ds.GetRasterBand(1).SetNoDataValue(nd)
    ds.GetRasterBand(1).WriteArray(a.astype(np.float32))
    ds = None
    return path


def _read(path):
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype(np.float64)
    a[a == b.GetNoDataValue()] = np.nan
    return a, ds.GetGeoTransform()


def _plan(d, name="dam.p01.hdf"):
    p = os.path.join(d, name)
    with open(p, "wb") as fh:                # a file for the cache key; read through FakePlan
        fh.write(b"\x89HDF\r\n\x1a\n" + b"synthetic plan" * 10)
    return p


def _terrain(d):
    z = np.full((10, 30), 100.0)
    z[:, 14:16] = 90.0                       # the ditch, inside cell 1 (x 10..20)
    return _tif(os.path.join(d, "terrain.tif"), z)


def _cols(a, gt):
    """Pixel-centre x of each column of a raster on its grid."""
    return gt[0] + (np.arange(a.shape[1]) + 0.5) * gt[1]


def test_ditch_cell_terrain(d):
    hdf, dem = _plan(d), _terrain(d)
    out = os.path.join(d, "terrain_out")
    res = RI.import_plan(hdf, out, dem_path=dem, breach_hr=0.5, arrival_ft=0.1)
    assert res["depth_basis"] == RI.DEPTH_BASIS_TERRAIN, res["depth_basis"]
    dep, gt = _read(res["paths"]["depth"])
    arr, gt2 = _read(res["paths"]["arr"])
    assert dep.shape == (10, 30) and gt == gt2
    x = _cols(dep, gt)
    ditch, bank, side = (x > 14) & (x < 16), (x > 10) & (x < 20) & ~((x > 14) & (x < 16)), \
        (x < 10) | (x > 20)
    # depth: the cell's maximum water surface (101) minus the terrain, not the cell minimum
    assert np.allclose(dep[:, ditch], 11.0) and np.allclose(dep[:, bank], 1.0), dep[0]
    assert np.allclose(dep[:, side], 0.15, atol=1e-4), dep[0]
    # arrival per pixel from the same depth, interpolated, minus the 0.5 h breach start:
    # ditch 90 -> 95 crosses 90.1 at 0.02 h (clipped to 0 after the breach shift);
    # banks 99 -> 100.5 cross 100.1 at 2 + 1.1 / 1.5 h (the cell-minimum import said 1 h)
    assert np.allclose(arr[:, ditch], 0.0), arr[0]
    assert np.allclose(arr[:, bank], 2.0 + 1.1 / 1.5 - 0.5, atol=1e-5), arr[0]
    # cells 0 / 2 reach 0.15 ft only between output steps: their time of the maximum
    assert np.allclose(arr[:, side], 3.5 - 0.5, atol=1e-5), arr[0]
    # the arrival mask is the depth >= arrival depth mask (one basis)
    with np.errstate(invalid="ignore"):
        assert np.array_equal(np.isfinite(arr), np.isfinite(dep) & (dep >= 0.1))
    assert any("only between output steps" in n for n in res["notes"]), res["notes"]
    rec = json.load(open(os.path.join(out, RI.BASIS_FILE)))
    assert rec["depth_basis"] == "terrain" and rec["depth_source"] == os.path.abspath(dem)
    assert not [n for n in os.listdir(out) if n.startswith("_")], os.listdir(out)
    # a higher arrival depth: the banks (1 ft) never arrive, the ditch does
    res2 = RI.import_plan(hdf, os.path.join(d, "terrain_2ft"), dem_path=dem, arrival_ft=2.0)
    arr2, _ = _read(res2["paths"]["arr"])
    assert np.isfinite(arr2[:, ditch]).all() and not np.isfinite(arr2[:, bank]).any()
    assert np.allclose(arr2[:, ditch], 0.4, atol=1e-5)     # 90 -> 95 crosses 92 at 0.4 h


def test_cell_min_warning_and_cache(d):
    hdf = _plan(d, "lone.p01.hdf")
    res = RI.import_plan(hdf, os.path.join(d, "cm_out"), arrival_ft=0.1, cell_ft=1.0)
    assert res["depth_basis"] == RI.DEPTH_BASIS_CELL_MIN
    assert RI.CELL_MIN_WARNING in res["notes"]
    dep, gt = _read(res["paths"]["depth"])
    arr, _ = _read(res["paths"]["arr"])
    x = _cols(dep, gt)
    mid = (x > 10.5) & (x < 19.5)
    assert np.allclose(dep[:, mid][np.isfinite(dep[:, mid])], 11.0)
    a = arr[:, mid][np.isfinite(arr[:, mid])]
    assert a.size and np.allclose(a, 0.02, atol=1e-5), a      # 90 -> 95 crosses 90.1
    rec = json.load(open(os.path.join(d, "cm_out", RI.BASIS_FILE)))
    assert rec["depth_basis"] == "cell_min"
    # the plan cache: the basis in the manifest and the result, the warning in the log
    root = os.path.join(d, "cache")
    logs = []
    r1 = RI.plan_to_rasters_cached(hdf, root, {"cell_ft": 1.0}, log=logs.append)
    assert r1["depth_basis"] == "cell_min" and not r1["cached"]
    assert any(RI.CELL_MIN_WARNING in m for m in logs), logs
    m = json.load(open(os.path.join(r1["folder"], RI.MANIFEST)))
    assert m["depth_basis"] == "cell_min"
    logs[:] = []
    r2 = RI.plan_to_rasters_cached(hdf, root, {"cell_ft": 1.0}, log=logs.append)
    assert r2["cached"] and r2["depth_basis"] == "cell_min"
    assert any(RI.CELL_MIN_WARNING in m for m in logs), logs


def _runner_rasters(folder, depth=3.0, arr=2.0):
    os.makedirs(folder, exist_ok=True)
    gt = (0.0, 30.0, 0.0, 30.0, 0.0, -30.0)
    one = np.ones((1, 1))
    _tif(os.path.join(folder, "Depth (Max).tif"), one * depth, gt)
    _tif(os.path.join(folder, "Velocity (Max).tif"), one * 1.5, gt)
    _tif(os.path.join(folder, "Arrival Time.tif"), one * arr, gt)
    _tif(os.path.join(folder, "WSE (Max).tif"), one * 101.0, gt)


def test_runner_reexport_fallback(d):
    dam = os.path.join(d, "DWR-TEST")
    hec = os.path.join(dam, "_hecras")
    os.makedirs(hec)
    hdf = _plan(hec)
    before = RI.plan_key(hdf, {})
    assert RI.find_runner_rasters(hdf) is None
    # a stage record with the cell-minimum basis (a pre-3.10.79 run): not used
    _runner_rasters(dam, depth=9.0)
    json.dump({"results_hdf": hdf, "depth_basis": "cell_min", "t0_hr": 1.0},
              open(os.path.join(hec, "hecras_stage.json"), "w"))
    assert RI.find_runner_rasters(hdf) is None
    assert RI.import_plan(hdf, os.path.join(d, "o0"))["depth_basis"] == "cell_min"
    # a terrain re-export of this plan: used, arrival moved from the Runner's t0 (1 h) to
    # the breach start (0.5 h)
    rx = os.path.join(hec, "reexport_20261025T120000Z")
    _runner_rasters(rx, depth=3.0, arr=2.0)
    json.dump({"results_hdf": hdf, "depth_basis": "terrain", "t0_hr": 1.0,
               "arrival_depth_ft": 0.1}, open(os.path.join(rx, "reexport.json"), "w"))
    assert RI.plan_key(hdf, {}) != before                 # a new re-export: a new conversion
    res = RI.import_plan(hdf, os.path.join(d, "o1"), breach_hr=0.5)
    assert res["depth_basis"] == RI.DEPTH_BASIS_RUNNER, res["notes"]
    assert res["depth_source"] == rx
    assert _read(res["paths"]["depth"])[0][0, 0] == 3.0
    assert abs(_read(res["paths"]["arr"])[0][0, 0] - 2.5) < 1e-6
    assert abs(_read(res["paths"]["vel"])[0][0, 0] - 1.5) < 1e-6
    assert RI.CELL_MIN_WARNING not in res["notes"]
    assert abs(res["pixel_ft"] - 30.0 * 1200.0 / 3937.0 / 0.3048) < 1e-3   # ftUS pixels
    rec = json.load(open(os.path.join(d, "o1", RI.BASIS_FILE)))
    assert rec["depth_basis"] == "runner_reexport" and rec["runner_t0_hr"] == 1.0
    # a copy of the plan elsewhere, the dam folder given (the Hazard tab's dam folder)
    other = os.path.join(d, "elsewhere")
    os.makedirs(other)
    cp = shutil.copy(hdf, os.path.join(other, os.path.basename(hdf)))
    assert RI.find_runner_rasters(cp) is None
    f = RI.find_runner_rasters(cp, [dam])
    assert f and f["folder"] == rx
    # a re-export of ANOTHER plan is not used
    json.dump({"results_hdf": os.path.join(d, "x.p02.hdf"), "depth_basis": "terrain"},
              open(os.path.join(rx, "reexport.json"), "w"))
    assert RI.find_runner_rasters(hdf) is None
    # the dam folder's own rasters when the stage says terrain (and they are not newer)
    json.dump({"results_hdf": hdf, "depth_basis": "terrain", "t0_hr": 0.0},
              open(os.path.join(hec, "hecras_stage.json"), "w"))
    f = RI.find_runner_rasters(hdf)
    assert f and f["kind"] == "stage" and f["folder"] == dam
    t = os.path.getmtime(os.path.join(hec, "hecras_stage.json"))
    os.utime(os.path.join(dam, "Depth (Max).tif"), (t + 60, t + 60))   # a later model run
    assert RI.find_runner_rasters(hdf) is None
    # a terrain raster given: always the terrain, never the Runner rasters
    assert RI.import_plan(hdf, os.path.join(d, "o2"), dem_path=_terrain(d))["depth_basis"] \
        == "terrain"


def test_first_reach_brute_force():
    rng = np.random.default_rng(7)
    T, m, p = 12, 6, 400
    t = np.cumsum(rng.uniform(0.2, 1.0, T))
    ws = np.cumsum(rng.normal(0.05, 0.4, (T, m)), axis=0) + 100.0
    pick = rng.integers(0, m, p)
    ground = rng.uniform(98.0, 102.0, p)
    got = RI._pixel_first_reach(ws, t, pick, ground, 0.1, batch=37)
    for i in range(p):
        dd = ws[:, pick[i]] - ground[i]
        hit = np.flatnonzero(dd >= 0.1)
        if not hit.size:
            assert np.isnan(got[i])
            continue
        j = hit[0]
        exp = t[0] if j == 0 else t[j - 1] + (0.1 - dd[j - 1]) / (dd[j] - dd[j - 1]) * \
            (t[j] - t[j - 1])
        assert abs(got[i] - exp) < 1e-9, (i, got[i], exp)


def test_code_digest_covers_the_new_code():
    for n in ("_pixel_first_reach", "_arrival_from_series", "find_runner_rasters",
              "_import_runner", "import_plan", "_finish"):
        assert n in RI._CODE_FUNCS, n


def main():
    d = tempfile.mkdtemp(prefix="hz_rasarr_")
    try:
        for name, fn in [("ditch cell, terrain", lambda: test_ditch_cell_terrain(d)),
                         ("cell-minimum warning + cache", lambda: test_cell_min_warning_and_cache(d)),
                         ("Runner re-export fallback", lambda: test_runner_reexport_fallback(d)),
                         ("first reach vs brute force", test_first_reach_brute_force),
                         ("code digest", test_code_digest_covers_the_new_code)]:
            fn()
            print("ok   " + name)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("all ok")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
