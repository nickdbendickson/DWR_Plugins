"""Hazard 2.26.0: depth on the UNCONDITIONED ground (rawground.py) and the model gate's "terrain"
row. Absaraka (DWR-2801, November 2026): a conditioner notched up to 19.5 m through Casselton and
the model's depth there was water in the notch, not on the ground. Synthetic grids only.

    python3 DamSafety_Hazard/tests/test_rawground.py

numpy + GDAL; no QGIS.
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from DamSafety_Hazard import rawground as RG  # noqa: E402
from DamSafety_Hazard import modelgate as MG  # noqa: E402

TMP = tempfile.mkdtemp(prefix="hz_rawground_")
FT = RG.FT_PER_M


def _tif(name, arr, cell=30.0, nodata=-9999.0):
    from osgeo import gdal, osr
    a = np.asarray(arr, dtype=np.float64)
    path = os.path.join(TMP, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32)
    ds.SetGeoTransform((500000.0, cell, 0.0, 5200000.0, 0.0, -cell))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(26914)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(np.where(np.isfinite(a), a, nodata).astype(np.float32))
    ds = None
    return path


def _case():
    """10x10 m-DEM: a 3 m notch along row 5; the water surface stands 1 ft above raw grade on the
    left half (cells stay wet on raw ground) and 2 ft below it on the right half (cells dry)."""
    raw = np.full((10, 10), 300.0)
    run = raw.copy()
    run[5, :] -= 3.0
    depth = np.zeros((10, 10))
    depth[5, :5] = 3.0 * FT + 1.0         # 1 ft above the real ground
    depth[5, 5:] = 3.0 * FT - 2.0         # 2 ft below the real ground
    depth[2, 2] = 4.0                      # wet, not lowered: untouched
    vel = np.where(depth > 0, 2.5, 0.0)
    return raw, run, depth, vel


def test_ground_lowering_only_counts_lowering():
    raw = np.array([[10.0, 10.0, np.nan], [10.0, 10.0, 10.0]])
    run = np.array([[9.0, 11.0, 5.0], [10.0, np.nan, 7.5]])
    low = RG.ground_lowering(run, raw)
    assert np.allclose(low, [[1.0, 0.0, 0.0], [0.0, 0.0, 2.5]]), low
    try:
        RG.ground_lowering(run[:, :2], raw)
    except ValueError:
        pass
    else:
        raise AssertionError("shape mismatch accepted")


def test_depth_on_raw_ground_math():
    raw, run, depth, vel = _case()
    low = RG.ground_lowering(run, raw)
    dr, vr, st = RG.depth_on_raw_ground(depth, low, vel)
    assert np.allclose(dr[5, :5], 1.0), dr[5, :5]
    assert (dr[5, 5:] == 0).all() and (vr[5, 5:] == 0).all(), "dry cells kept depth / velocity"
    assert (vr[5, :5] == 2.5).all(), "velocity changed on a cell still wet"
    assert dr[2, 2] == 4.0 and vr[2, 2] == 2.5, "an unlowered cell changed"
    assert st["cells_lowered"] == 10 and st["wet_cells_on_lowered_ground"] == 10
    assert st["cells_dried"] == 5, st
    assert abs(st["max_lowering_wet"] - 3.0 * FT) < 1e-9
    # no lowering -> identical
    d2, v2, s2 = RG.depth_on_raw_ground(depth, np.zeros_like(depth), vel)
    assert np.array_equal(d2, depth) and np.array_equal(v2, vel) and s2["cells_lowered"] == 0


def test_receptor_report_flags_receptors_on_a_carve():
    raw, run, depth, _v = _case()
    low = RG.ground_lowering(run, raw)
    rep = RG.receptor_report([(4, 1), (2, 2), (5, 8)], low, depth)
    idx = sorted(r["index"] for r in rep)
    assert idx == [0, 2], rep                 # (2,2) is 3 rows from the notch
    r0 = [r for r in rep if r["index"] == 0][0]
    assert abs(r0["max_lowering"] - 3.0 * FT) < 1e-9 and abs(r0["depth_raw"] - 1.0) < 1e-6, r0
    r2 = [r for r in rep if r["index"] == 2][0]
    assert r2["depth_raw"] == 0.0, r2


def test_write_raw_ground_rasters():
    from osgeo import gdal
    raw, run, depth, vel = _case()
    out = os.path.join(TMP, "out")
    st = RG.write_raw_ground(_tif("d.tif", depth), _tif("run.tif", run), _tif("raw.tif", raw),
                             out, vel_path=_tif("v.tif", vel))
    assert os.path.basename(st["depth"]) == RG.DEPTH_NAME and os.path.isfile(st["vel"])
    d = gdal.Open(st["depth"]).ReadAsArray()
    v = gdal.Open(st["vel"]).ReadAsArray()
    assert np.allclose(d[5, :5], 1.0, atol=1e-4) and (d[5, 5:] == 0).all(), d[5]
    assert (v[5, 5:] == 0).all() and np.allclose(v[5, :5], 2.5)
    assert st["cells_dried"] == 5


def test_model_gate_terrain_row():
    raw, run, depth, _v = _case()
    d, r_raw, r_run = _tif("gd.tif", depth), _tif("graw.tif", raw), _tif("grun.tif", run)
    item = {"name": "Breach", "depth": d}
    skip = MG.check_terrain(item, {})
    assert skip[0]["Status"] == MG.SKIP, skip
    miss = MG.check_terrain(item, {"raw_dem": r_raw, "run_dem": os.path.join(TMP, "none.tif")})
    assert miss[0]["Status"] == MG.SKIP and "not found" in miss[0]["Reason"], miss
    warn = MG.check_terrain(item, {"raw_dem": r_raw, "run_dem": r_run})
    assert warn[0]["Status"] == MG.WARN and warn[0]["Value"] == "10", warn
    assert "5 of them are dry" in warn[0]["Reason"], warn[0]["Reason"]
    same = MG.check_terrain(item, {"raw_dem": r_raw, "run_dem": r_raw})
    assert same[0]["Status"] == MG.PASS, same
    # the terrain row is never FAIL and the gate defaults carry the keys
    for k in ("raw_dem", "run_dem", "terrain_warn_ft"):
        assert k in MG.DEFAULTS, k


def main():
    tests = [test_ground_lowering_only_counts_lowering, test_depth_on_raw_ground_math,
             test_receptor_report_flags_receptors_on_a_carve, test_write_raw_ground_rasters,
             test_model_gate_terrain_row]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Exception as e:
            failed += 1
            import traceback
            traceback.print_exc()
            print("FAIL ", fn.__name__, ":", e)
    print("\n{} tests, {} failed".format(len(tests), failed))
    shutil.rmtree(TMP, ignore_errors=True)
    return failed


if __name__ == "__main__":
    rc = main()
    sys.stdout.flush()
    os._exit(1 if rc else 0)
