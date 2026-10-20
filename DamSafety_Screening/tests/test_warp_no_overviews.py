"""Screening 0.17.35: terrain downsampled with gdal.Warp is computed from the full-resolution
cells, never from the overviews the source carries (core/warp_opts.py, gdalwarp -ovr NONE).
gdal.Warp's default (-ovr AUTO) resampled a 1 m lidar tile, or a VRT of them, to a 5 m grid
from its 4 m overview.

The synthetic 1 m tiles have NEAREST overviews built over a fine checkerboard, so an overview
read gives clearly different values than the 1 m cells (about 5 m lower on main). Each path
must give the same grid as identical tiles WITHOUT overviews. Needs GDAL (the QGIS Python);
the TerrainLoader part also needs QGIS. Prints SKIP otherwise."""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

import _optional  # noqa: E402
_optional.need("osgeo", "the overview regression warps real GeoTIFFs")
import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402
from nd_dam_screening.core import dem_download as dd, warp_opts as W  # noqa: E402

N = 400                              # 1 m cells per tile side
X0, Y0 = 0.0, 1500000.0              # EPSG:5070 (Albers), no reprojection in the comparison
WORK = tempfile.mkdtemp(prefix="scr_ovr_none_")


def surface(x0):
    yy, xx = np.mgrid[0:N, 0:N].astype("float64")
    plane = 400.0 + 0.01 * (xx + x0) - 0.02 * yy
    return (plane + np.where((xx + yy) % 2 == 0, 5.0, -5.0)).astype("float32")


def tile(folder, name, x0, overviews):
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name)
    s = osr.SpatialReference()
    s.ImportFromEPSG(5070)
    ds = gdal.GetDriverByName("GTiff").Create(path, N, N, 1, gdal.GDT_Float32, ["TILED=YES"])
    ds.SetProjection(s.ExportToWkt())
    ds.SetGeoTransform((X0 + x0, 1.0, 0.0, Y0, 0.0, -1.0))
    ds.GetRasterBand(1).WriteArray(surface(x0))
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    if overviews:
        ds.BuildOverviews("NEAREST", [2, 4, 8])
    ds = None
    return path


OVR, PLAIN = os.path.join(WORK, "ovr"), os.path.join(WORK, "plain")
A_OVR = tile(OVR, "a.tif", 0, True)
tile(OVR, "b.tif", N, True)
A_PLAIN = tile(PLAIN, "a.tif", 0, False)
tile(PLAIN, "b.tif", N, False)


def read(path):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(1).ReadAsArray().astype("float64")
    ds = None
    return a


def test_helper():
    kw = W.no_overviews(gdal, {"format": "MEM"})
    assert kw.get("overviewLevel") == "NONE" or "-ovr" in kw.get("options", []), kw
    gdal.WarpOptions(**kw)
    other = os.path.join(os.path.dirname(os.path.dirname(HERE)), "DamSafety_Runner", "core",
                         "warp_opts.py")
    if os.path.exists(other):
        assert open(other, "rb").read() == open(W.__file__, "rb").read(), "warp_opts copies differ"
    print("OK helper")


def test_write_local_dem():
    """core.dem_download.write_local_dem (tool 2's local DEM): 1 m -> 5 m and 10 m bilinear."""
    info = {"source": "local", "crs": "EPSG:5070", "units": "metre", "file_type": "GTiff",
            "cell_m": 0, "native_source": "", "native_crs": "", "native_units": "",
            "native_file_type": ""}
    for cell in (5.0, 10.0):
        info["cell_m"] = cell
        got, want = os.path.join(WORK, "got_%d.tif" % cell), os.path.join(WORK, "want_%d.tif" % cell)
        dd.write_local_dem(A_OVR, got, cell, info)
        dd.write_local_dem(A_PLAIN, want, cell, info)
        g, w = read(got), read(want)
        assert g.shape == w.shape and np.allclose(g, w, atol=1e-4), (
            "write_local_dem read the overviews at %s m: max diff %.3f" % (cell, float(np.max(abs(g - w)))))
        # the plane (+-0.2 m of checkerboard residue), not one colour of it (5 m off)
        assert abs(float(g[2:-2, 2:-2].mean()) - float(surface(0).mean())) < 0.5, float(g.mean())
    print("OK write_local_dem")


def test_terrain_loader():
    """algorithms._common.TerrainLoader.load from a local DEM folder (the VRT of the tiles),
    'average' to 5 m and 10 m: the mean of the 1 m cells."""
    try:
        import qgis.core  # noqa: F401
    except Exception as e:  # noqa: BLE001 - no QGIS in this Python
        print("SKIP terrain loader: QGIS is not importable ({})".format(e))
        return
    from nd_dam_screening.algorithms import _common as cm
    centre, half = (X0 + N, Y0 - N / 2.0), 150.0
    for cell in (5.0, 10.0):
        out = []
        for folder in (OVR, PLAIN):
            tmp = tempfile.mkdtemp(dir=WORK)
            ld = cm.TerrainLoader(None, source="local", local_folder=folder, tmpdir=tmp)
            res = ld.load("t", centre, half, cell, condition=False)
            dem = res[0] if isinstance(res, tuple) else res
            out.append(np.asarray(dem, dtype="float64"))
        g, w = out
        assert g.shape == w.shape and np.allclose(g, w, atol=1e-4), (
            "TerrainLoader read the overviews at %s m: max diff %.3f" % (cell, float(np.max(abs(g - w)))))
    print("OK terrain loader")


if __name__ == "__main__":
    try:
        test_helper()
        test_write_local_dem()
        test_terrain_loader()
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print("ALL OK")
