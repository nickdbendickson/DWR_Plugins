"""3.10.56: the HEC-RAS package's terrain GeoTIFF must keep its coordinate system
(core/hecras_terrain_crs): a good terrain passes; one that lost its EPSG code (an unnamed metre
CRS, as a rewrite without the PROJ database gave) or has none, one that does not match
GIS/Projection.prj, and a missing PROJ database all stop the package with a plain reason;
add_to_package leaves no half package behind. Synthetic GeoTIFFs, no HEC-RAS, no solver."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import hecras_structures as hs
from nd_breach_runner.core import hecras_terrain_crs as htc
from nd_breach_runner.tests import test_hecras_structures as ths
from nd_breach_runner.tests.registry import test


def _osr():
    from osgeo import gdal, osr
    gdal.UseExceptions()
    return gdal, osr


def _srs(epsg):
    _, osr = _osr()
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s


def _tif(path, wkt):
    """A 4 x 4 Float32 GeoTIFF with the CRS `wkt` ('' = none)."""
    gdal, _ = _osr()
    ds = gdal.GetDriverByName("GTiff").Create(path, 4, 4, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((2700000.0, 10.0, 0.0, 480000.0, 0.0, -10.0))
    if wkt:
        ds.SetProjection(wkt)
    ds.GetRasterBand(1).WriteArray(np.full((4, 4), 1000.0, np.float32))
    ds = None
    return path


def _prj(path, epsg):
    s = _srs(epsg)
    s.MorphToESRI()                       # as build_model writes GIS/Projection.prj
    with open(path, "w") as f:
        f.write(s.ExportToWkt())
    return path


def _unnamed_metre_wkt():
    """EPSG:2266's projection in metres with no name or authority: what a rewrite without the
    PROJ database produced."""
    _, osr = _osr()
    p4 = _srs(2266).ExportToProj4().replace("+units=ft", "+units=m")
    s = osr.SpatialReference()
    s.ImportFromProj4(p4)
    return s.ExportToWkt()


def _raises(fn, *words):
    try:
        fn()
    except htc.TerrainCRSError as e:
        msg = str(e)
        assert isinstance(e, hm.HecRasModelError)
        for w in words:
            assert w.lower() in msg.lower(), (w, msg)
        return msg
    raise AssertionError("expected TerrainCRSError ({})".format(words))


@test
def hecras_terrain_crs_good_tif_passes():
    tmp = tempfile.mkdtemp()
    try:
        t = _tif(os.path.join(tmp, "T.tif"), _srs(2266).ExportToWkt())
        r = htc.check_terrain(t, _prj(os.path.join(tmp, "Projection.prj"), 2266))
        assert r["epsg"] == 2266 and abs(r["unit_m"] - 0.3048) < 1e-12, r
        assert htc.proj_db_problem() is None
    finally:
        shutil.rmtree(tmp)


@test
def hecras_terrain_crs_lost_epsg_or_metre_or_none_fails():
    tmp = tempfile.mkdtemp()
    try:
        prj = _prj(os.path.join(tmp, "Projection.prj"), 2266)
        t = _tif(os.path.join(tmp, "M.tif"), _unnamed_metre_wkt())
        msg = _raises(lambda: htc.check_terrain(t, prj), "metre", "EPSG:2266")
        assert "PROJ_DATA" in msg            # says what to do
        t0 = _tif(os.path.join(tmp, "N.tif"), "")
        _raises(lambda: htc.check_terrain(t0, prj), "no coordinate system")
    finally:
        shutil.rmtree(tmp)


@test
def hecras_terrain_crs_mismatch_with_projection_prj_fails():
    tmp = tempfile.mkdtemp()
    try:
        prj = _prj(os.path.join(tmp, "Projection.prj"), 2266)
        t = _tif(os.path.join(tmp, "U.tif"), _srs(32121).ExportToWkt())   # ND South, metre
        _raises(lambda: htc.check_terrain(t, prj), "does not match", "EPSG:32121", "EPSG:2266")
        t2 = _tif(os.path.join(tmp, "N.tif"), _srs(2265).ExportToWkt())   # ND North, ft
        _raises(lambda: htc.check_terrain(t2, prj), "does not match", "EPSG:2265")
        _raises(lambda: htc.check_terrain(t2, os.path.join(tmp, "none.prj")), "no ")
    finally:
        shutil.rmtree(tmp)


@test
def hecras_terrain_crs_missing_proj_database_fails():
    """A fresh Python with PROJ_DATA on an empty folder (PROJ_LIB unset): GDAL cannot reach
    proj.db, and the check says so (GDAL caches the database in this process, hence the child)."""
    import subprocess
    import sys
    import nd_breach_runner
    tmp = tempfile.mkdtemp()
    try:
        t = _tif(os.path.join(tmp, "T.tif"), _srs(2266).ExportToWkt())
        prj = _prj(os.path.join(tmp, "Projection.prj"), 2266)
        empty = os.path.join(tmp, "no_proj")
        os.makedirs(empty)
        env = {k: v for k, v in os.environ.items() if k not in ("PROJ_LIB", "PROJ_DATA")}
        env["PROJ_DATA"] = empty
        env["PROJ_LIB"] = empty
        root = os.path.dirname(os.path.dirname(os.path.abspath(nd_breach_runner.__file__)))
        env["PYTHONPATH"] = os.pathsep.join([root] + [p for p in sys.path if p])
        code = ("import sys\n"
                "from nd_breach_runner.core import hecras_terrain_crs as htc\n"
                "try:\n"
                "    htc.check_terrain(sys.argv[1], sys.argv[2])\n"
                "except htc.TerrainCRSError as e:\n"
                "    print('ERR', e)\n"
                "else:\n"
                "    print('PASSED')\n")
        r = subprocess.run([sys.executable, "-c", code, t, prj], env=env, capture_output=True,
                           text=True, timeout=120)
        out = r.stdout + r.stderr
        assert "ERR" in r.stdout and "proj.db" in r.stdout and "PROJ_DATA" in r.stdout, out[-2000:]
        assert htc.check_terrain(t, prj)["epsg"] == 2266      # this process is unaffected
    finally:
        shutil.rmtree(tmp)


@test
def hecras_terrain_crs_add_to_package_refuses_a_bad_terrain():
    tmp = tempfile.mkdtemp()
    try:
        tif = ths._terrain(os.path.join(tmp, "t.tif"))
        pk = ths._package(tmp, tif)
        _tif(os.path.join(pk, "Terrain", "T Terrain.tif"), _unnamed_metre_wkt())
        out = os.path.join(tmp, "OUT")
        _raises(lambda: hs.add_to_package(pk, os.path.join(tmp, "RUN"), out), "lost its EPSG")
        assert not os.path.exists(out)                # no half package
        _raises(lambda: htc.check_package(pk), "lost its EPSG")
    finally:
        shutil.rmtree(tmp)
