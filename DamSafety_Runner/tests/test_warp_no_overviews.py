"""Runner 3.10.43: the DEM is warped to the model grid from its full-resolution cells, never from
the overviews it carries (core/warp_opts.py, gdalwarp -ovr NONE). gdal.Warp's default (-ovr
AUTO) resampled a 1 m lidar tile, or a VRT of them, to a 5 m grid from its 4 m overview.

The synthetic 1 m tiles have NEAREST overviews built over a fine pattern, so an overview read
gives clearly different values than the 1 m cells (on main the 5 m 'average' grid came out
about 5 m lower). Each path must give the same grid as an identical tile WITHOUT overviews,
and 'average' must be the plain mean of the 1 m cells."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

EPSG = 26914
N = 400                    # 1 m cells per tile side


def _gdal():
    from osgeo import gdal, osr
    return gdal, osr


def _surface(x0):
    """A sloping plane plus a 1 m checkerboard of +-5 m: its 5 m block means are the plane
    (+-0.2), a NEAREST overview keeps only one colour of the checkerboard."""
    yy, xx = np.mgrid[0:N, 0:N].astype("float64")
    plane = 300.0 + 0.01 * (xx + x0) - 0.02 * yy
    return (plane + np.where((xx + yy) % 2 == 0, 5.0, -5.0)).astype("float32")


def _tile(path, x0, overviews):
    gdal, osr = _gdal()
    s = osr.SpatialReference()
    s.ImportFromEPSG(EPSG)
    ds = gdal.GetDriverByName("GTiff").Create(path, N, N, 1, gdal.GDT_Float32, ["TILED=YES"])
    ds.SetProjection(s.ExportToWkt())
    ds.SetGeoTransform((500000.0 + x0, 1.0, 0.0, 5200000.0, 0.0, -1.0))
    ds.GetRasterBand(1).WriteArray(_surface(x0))
    ds.GetRasterBand(1).SetNoDataValue(-9999.0)
    if overviews:
        ds.BuildOverviews("NEAREST", [2, 4, 8])
    ds = None
    return path


def _pair(d):
    """(tile with overviews, the same tile without, VRT of two tiles with overviews, VRT of
    the same two without)."""
    gdal, _ = _gdal()
    a = _tile(os.path.join(d, "a_ovr.tif"), 0, True)
    b = _tile(os.path.join(d, "a_plain.tif"), 0, False)
    c = _tile(os.path.join(d, "b_ovr.tif"), N, True)
    e = _tile(os.path.join(d, "b_plain.tif"), N, False)
    v1, v2 = os.path.join(d, "ovr.vrt"), os.path.join(d, "plain.vrt")
    gdal.BuildVRT(v1, [a, c])
    gdal.BuildVRT(v2, [b, e])
    return a, b, v1, v2


def _block_mean(arr, k):
    n = arr.shape[0] // k
    return arr[:n * k, :n * k].reshape(n, k, -1, k).mean(axis=(1, 3))


@test
def warp_dem_to_model_grid_reads_full_resolution_not_overviews():
    """gdal_io.GdalTerrain.read (the DEM -> model grid of every run): 1 m tile and 1 m VRT to
    5 m and 10 m, 'average' and 'bilinear', equal to the overview-free copy; 'average' is the
    mean of the 1 m cells."""
    from nd_breach_runner.core import gdal_io
    d = tempfile.mkdtemp(prefix="ovr_none_")
    try:
        a, b, v1, v2 = _pair(d)
        bounds = (500000.0, 5200000.0 - N, 500000.0 + N, 5200000.0)
        for cell in (5.0, 10.0):
            for alg in ("average", "bilinear"):
                for src, ref in ((a, b), (v1, v2)):
                    got = gdal_io.GdalTerrain(src, "EPSG:{}".format(EPSG), resample=alg).read(
                        bounds, cell).data
                    want = gdal_io.GdalTerrain(ref, "EPSG:{}".format(EPSG), resample=alg).read(
                        bounds, cell).data
                    assert np.allclose(got, want, atol=1e-4), (cell, alg, src,
                                                               float(np.nanmax(abs(got - want))))
            g = gdal_io.GdalTerrain(a, "EPSG:{}".format(EPSG), resample="average").read(
                bounds, cell).data
            assert np.allclose(g, _block_mean(_surface(0).astype("float64"), int(cell)),
                               atol=1e-3), (cell, float(np.nanmax(abs(g - _block_mean(
                                   _surface(0).astype("float64"), int(cell))))))
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def warp_read_like_and_hecras_terrain_read_full_resolution():
    """gdal_io.read_like and the HEC-RAS model's _warp / write_terrain read the 1 m cells too."""
    from nd_breach_runner.core import gdal_io, hecras_model as hm
    from nd_breach_runner.core.grid import Grid
    gdal, osr = _gdal()
    d = tempfile.mkdtemp(prefix="ovr_none_")
    try:
        a, b, v1, v2 = _pair(d)
        s = osr.SpatialReference()
        s.ImportFromEPSG(EPSG)
        grid = Grid(np.zeros((N // 5, N // 5)), 500000.0, 5200000.0, 5.0, crs_wkt=s.ExportToWkt())
        for src, ref in ((a, b), (v1, v2)):
            got, want = gdal_io.read_like(src, grid, "average"), gdal_io.read_like(ref, grid, "average")
            got = getattr(got, "data", got)
            want = getattr(want, "data", want)
            assert np.allclose(got, want, atol=1e-4), ("read_like", src)
            r1 = hm._warp(src, s, 10.0, alg="average")
            r2 = hm._warp(ref, s, 10.0, alg="average")
            assert np.allclose(r1[0], r2[0], atol=1e-4, equal_nan=True), ("hecras _warp", src)
            bounds = (500000.0, 5200000.0 - N, 500000.0 + N, 5200000.0)
            t1, t2 = os.path.join(d, "t1.tif"), os.path.join(d, "t2.tif")
            hm.write_terrain(src, t1, s, 5.0, bounds, 1.0, -9999.0, "metre")
            hm.write_terrain(ref, t2, s, 5.0, bounds, 1.0, -9999.0, "metre")
            d1, d2 = gdal.Open(t1), gdal.Open(t2)
            x1, x2 = d1.GetRasterBand(1).ReadAsArray(), d2.GetRasterBand(1).ReadAsArray()
            d1 = d2 = None
            assert np.allclose(x1, x2, atol=1e-4), ("hecras write_terrain", src)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def warp_opts_no_overviews_keyword_or_cli_flag():
    """overviewLevel='NONE' where the bindings have it, else '-ovr NONE' in options; the
    Runner and Screening copies are the same file."""
    from nd_breach_runner.core import warp_opts as W
    gdal, _ = _gdal()
    kw = W.no_overviews(gdal, {"format": "MEM"})
    assert kw == {"format": "MEM", "overviewLevel": "NONE"}, kw     # GDAL 3.8 / 3.13 here
    gdal.WarpOptions(**kw)                                           # accepted

    class Old(object):
        @staticmethod
        def WarpOptions(options=None, format=None):                 # noqa: A002 - no overviewLevel
            return options
    assert W.no_overviews(Old, {"options": ["-q"]}) == {"options": ["-q", "-ovr", "NONE"]}
    assert W.no_overviews(Old, {"options": "-q -multi"})["options"] == ["-q", "-multi", "-ovr", "NONE"]
    assert W.no_overviews(object(), {}) == {"options": ["-ovr", "NONE"]}
    here = os.path.dirname(os.path.dirname(os.path.abspath(W.__file__)))
    other = os.path.join(os.path.dirname(here), "DamSafety_Screening", "core", "warp_opts.py")
    if os.path.exists(other):
        assert open(other, "rb").read() == open(W.__file__, "rb").read()
