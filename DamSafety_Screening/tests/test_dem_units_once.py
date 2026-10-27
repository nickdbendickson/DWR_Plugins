"""Screening 0.17.50, review round 2 of Download DEM's units (Nick, Absaraka DWR-2801):

1. The 1 m lidar VRT converts a feet fallback exactly once. GDAL's default vertical shift had
   already turned a compound-CRS feet fallback (or a band-'ft' one under compound tiles) into
   metres in the warp, and the scale VRT converted it again (951 ft read as 88.4 m). The warps
   keep the values as stored now (warp_opts.no_vshift) - every combination of 2D / compound tiles
   and band-unit / compound-CRS / State Plane fallbacks reads 289.9 m.
2. write_local_dem warps as stored too, so its band tag is the unit of what it wrote.
3. An ND DWR download written by Screening before 0.17.50 in a folder named gft* (DEM_Imagine
   metres scaled x 0.3048 and tagged 'm') is not reused; 0.17.50 records which build wrote a file
   (metadata WRITER, facts 'written_by').
4. Pins the rules the mutation run left open: a bare .asc and a feet grid below a gft* folder of
   the download folder (build_vrts), the ImageServer path passing the area to the request note,
   the ND DWR native units in the facts, the window's ND DWR lines.
Needs GDAL (prints SKIP otherwise). No network."""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _optional  # noqa: E402
_optional.need("osgeo", "writes and reads small GeoTIFFs")
import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402  (the real GDAL, before qgis_mock's stand-in)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import nd_dam_screening  # noqa: E402,F401
from nd_dam_screening.core import dem_download as dd  # noqa: E402
from nd_dam_screening.core import dem_zunit as ZU  # noqa: E402
from nd_dam_screening.core import lidar_tnm as lt  # noqa: E402

gdal.UseExceptions()
TESTS = []
US_FT = 1200.0 / 3937.0


def register(f):
    TESTS.append(f)
    return f


class Feedback:
    def __init__(self):
        self.log, self.warnings = [], []

    def pushInfo(self, m): self.log.append(m)
    def pushWarning(self, m): self.warnings.append(m); self.log.append(m)
    def reportError(self, m, fatal=False): self.log.append(m)
    def setProgress(self, p): pass
    def isCanceled(self): return False


def _to(x, y, src, dst):
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.SetFromUserInput(src)
    b.SetFromUserInput(dst)
    for s in (a, b):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(a, b).TransformPoint(x, y)[:2]


def _ras(path, value, crs="EPSG:26914", box=(599900.0, 5199300.0, 600700.0, 5200100.0), n=80,
         unit="", driver="GTiff"):
    """An n x n raster of `value` in `crs` (compound allowed) covering the UTM 14N `box`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    horiz = crs.split("+")[0]
    x0, y1 = _to(box[0], box[3], "EPSG:26914", horiz)
    x1, y0 = _to(box[2], box[1], "EPSG:26914", horiz)
    s = osr.SpatialReference()
    s.SetFromUserInput(crs)
    mem = gdal.GetDriverByName("MEM").Create("", n, n, 1, gdal.GDT_Float32)
    mem.SetProjection(s.ExportToWkt())
    mem.SetGeoTransform((x0, (x1 - x0) / n, 0.0, y1, 0.0, -(y1 - y0) / n))
    b = mem.GetRasterBand(1)
    b.WriteArray(np.full((n, n), value, dtype="float32"))
    b.SetNoDataValue(-9999.0)
    if unit:
        b.SetUnitType(unit)
    gdal.GetDriverByName(driver).CreateCopy(path, mem)
    mem = None
    return path


def _at(p, x, y):
    ds = gdal.Open(p)
    gt = ds.GetGeoTransform()
    c, r = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
    v = float(ds.GetRasterBand(1).ReadAsArray(c, r, 1, 1)[0, 0])
    ds = None
    return v


def _unit(p):
    ds = gdal.Open(p)
    try:
        return ds.GetRasterBand(1).GetUnitType()
    finally:
        ds = None


@register
def lidar_vrt_converts_the_fallback_once_whatever_the_crs():
    work = tempfile.mkdtemp(prefix="scr_once_")
    try:
        fbs = (("band_ft", "EPSG:26914", "ft", 951.0 * 0.3048),
               ("c26914_6360", "EPSG:26914+6360", "", 951.0 * US_FT),
               ("c2265_6360", "EPSG:2265+6360", "", 951.0 * US_FT),
               ("band_usft_2265", "EPSG:2265", "US survey foot", 951.0 * US_FT))
        for tiles_crs in ("EPSG:26914", "EPSG:26914+5703"):
            for name, crs, unit, want in fbs:
                d = os.path.join(work, tiles_crs.replace(":", "").replace("+", "_"), name)
                tile = _ras(os.path.join(d, "tiles", "USGS_1M_14_x60y520.tif"), 290.0, tiles_crs,
                            box=(600300.0, 5199700.0, 600600.0, 5200000.0), n=300, unit="metre")
                fb = _ras(os.path.join(d, "fb.tif"), 951.0, crs, unit=unit)
                entries = [{"path": tile, "tile": os.path.basename(tile), "project": "P"}]
                vrt, js = lt.build_vrt(entries, os.path.join(d, "D1_1m_lidar.vrt"), fb)
                got = _at(vrt, 600100.0, 5199900.0)      # fallback only
                assert abs(got - want) < 1e-3, (tiles_crs, name, got, want)   # was 88.4
                assert abs(_at(vrt, 600450.0, 5199850.0) - 290.0) < 1e-4, (tiles_crs, name)
                info = json.load(open(js, encoding="utf-8"))
                assert "converted x {:.10g}".format(want / 951.0) in info["fallback_units"], info
                assert ZU.recorded(gdal.Open(vrt), vrt)[0] == ZU.METERS
            # a fallback that records no unit is still left out, under compound tiles too
            d = os.path.join(work, tiles_crs.replace(":", "").replace("+", "_"), "none")
            tile = _ras(os.path.join(d, "tiles", "t.tif"), 290.0, tiles_crs,
                        box=(600300.0, 5199700.0, 600600.0, 5200000.0), n=300, unit="metre")
            fb = _ras(os.path.join(d, "fb.tif"), 951.0)
            vrt, js = lt.build_vrt([{"path": tile, "tile": "t.tif", "project": "P"}],
                                   os.path.join(d, "D1_1m_lidar.vrt"), fb)
            info = json.load(open(js, encoding="utf-8"))
            assert info["fallback"] == "" and fb in info["fallback_refused"], info
    finally:
        shutil.rmtree(work, ignore_errors=True)


@register
def local_dem_keeps_the_values_it_tags():
    work = tempfile.mkdtemp(prefix="scr_once_l_")
    try:
        src = _ras(os.path.join(work, "src.tif"), 1300.0, "EPSG:2265+6360")
        dest, side = dd.write_local_dem(src, os.path.join(work, "o", "x_10m_5070.tif"), 10,
                                        dd.product_info("ndlidar", 10))
        assert dd.recorded_unit(dest)[0] == ZU.US_FEET, (_unit(dest), dd.recorded_unit(dest))
        assert abs(_at(dest, *_to(600300.0, 5199700.0, "EPSG:26914", "EPSG:5070")) - 1300.0) < 0.01
        ds = gdal.Open(dest)
        assert ds.GetMetadataItem("WRITER") == ZU.WRITER, ds.GetMetadata()
        ds = None
        facts = ZU.read_facts(side)
        assert facts["written_by"] == ZU.WRITER and facts["units"] == "US survey feet", facts
        w = dd.warp_to(dest, os.path.join(work, "o", "x_26914.tif"), 26914)
        assert abs(_at(w, 600300.0, 5199700.0) - 1300.0) < 0.01 and \
            dd.recorded_unit(w)[0] == ZU.US_FEET
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _old_nd(out, stem):
    """An ND DWR download as Screening 0.17.19-0.17.49 wrote it under a gft* folder: band unit
    'm', the record and facts file, no WRITER / written_by (here 400 m DEM_Imagine x 0.3048)."""
    src = _ras(os.path.join(out, "_src", stem + ".tif"), 121.92, unit="m")
    dest, side = dd.write_local_dem(src, os.path.join(out, stem + "_10m_5070.tif"), 10,
                                    dd.product_info("ndlidar", 10))
    ds = gdal.Open(dest, gdal.GA_Update)
    md = ds.GetMetadata()
    md.pop("WRITER", None)
    ds.SetMetadata(md)
    ds = None
    with open(side, encoding="utf-8") as fh:
        lines = [x for x in fh.read().splitlines() if not x.startswith("written_by")]
    with open(side, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return dest


@register
def an_old_nd_dwr_download_in_a_gft_folder_is_not_reused():
    from qgis.core import QgsProcessingContext, QgsProcessingException
    from nd_dam_screening.algorithms import download_dem as mod
    work = tempfile.mkdtemp(prefix="scr_once_g_")
    try:
        alg = mod.DownloadDem()
        alg.initAlgorithm()
        base = {"RES": 0, "SCOPE": 0, "DAM_POINT": "-97.39,46.98", "BUFFER_MI": 10.0,
                "COUNTY": 0, "WORK_CRS": "", "SOURCE": 1}
        out = os.path.join(work, "gftaylor", "dems")
        bad = _old_nd(out, "D9")
        assert ZU.old_nd_dwr_problem(gdal.Open(bad), bad)
        try:
            alg.processAlgorithm(dict(base, OUT=out, DAM_ID="D9"), QgsProcessingContext(), Feedback())
            raise AssertionError("an old gft* ND DWR download must not be reused")
        except QgsProcessingException as e:
            assert "Not reusing D9_10m_5070.tif" in str(e) and "'gftaylor'" in str(e) and \
                "run Download DEM again" in str(e), str(e)
        # the same old file outside a gft* folder is right (band 'm'): reused as before
        out2 = os.path.join(work, "plain")
        ok = _old_nd(out2, "D9")
        fbk = Feedback()
        res = alg.processAlgorithm(dict(base, OUT=out2, DAM_ID="D9"), QgsProcessingContext(), fbk)
        assert res["VRT"] == ok and any(m.startswith("reusing") for m in fbk.log), fbk.log
    finally:
        shutil.rmtree(work, ignore_errors=True)


@register
def build_vrts_reads_a_bare_asc_and_a_gft_folder_below_the_download_folder():
    work = tempfile.mkdtemp(prefix="scr_once_b_")
    try:
        root = os.path.join(work, "_dem_tiles_a")
        asc = _ras(os.path.join(root, "t123.asc"), 1000.0, "EPSG:2266", driver="AAIGrid")
        v = dd.build_vrts([asc], root)
        assert os.path.basename(v) == "nd_lidar_ascii_m.vrt", v          # a bare .asc is feet
        x, y = _to(600300.0, 5199700.0, "EPSG:26914", "EPSG:2266")
        assert abs(_at(v, x, y) - 304.8) < 1e-3, _at(v, x, y)
        root = os.path.join(work, "_dem_tiles_b")
        grd = _ras(os.path.join(root, "gft1", "sub", "x.grd"), 1000.0, "EPSG:2266", driver="AAIGrid")
        v = dd.build_vrts([grd], root)                   # gft1 is below the download folder
        assert os.path.basename(v) == "nd_lidar_ascii_m.vrt", v
        assert abs(_at(v, x, y) - 304.8) < 1e-3
    finally:
        shutil.rmtree(work, ignore_errors=True)


@register
def imageserver_download_passes_the_area_to_the_request_note():
    from qgis.core import QgsProcessingContext
    from nd_dam_screening.algorithms import download_dem as mod
    alg = mod.DownloadDem()
    alg.initAlgorithm()
    seen = []
    old = dd.download
    try:
        dd.download = lambda *a, **k: seen.append(k.get("scope")) or "x.vrt"
        params = {"RES": 1, "SCOPE": 1, "COUNTY": 0, "DAM_POINT": "", "BUFFER_MI": 10.0}
        for scope in (0, 1, 2):
            params["DAM_POINT"] = "-97.39,46.98"
            alg._usgs(dict(params, SCOPE=scope), QgsProcessingContext(), Feedback(), "/tmp/x",
                      scope, 1, "s")
        assert seen == [0, 1, 2], seen
    finally:
        dd.download = old


@register
def nd_dwr_facts_name_the_product_build_vrts_used():
    from qgis.core import QgsProcessingContext
    from nd_dam_screening.algorithms import download_dem as mod
    work = tempfile.mkdtemp(prefix="scr_once_n_")
    alg = mod.DownloadDem()
    alg.initAlgorithm()
    old = dd.fetch_dwr
    try:
        img = _ras(os.path.join(work, "i", "dem1", "dem1.img"), 400.0, "EPSG:3721", driver="HFA")
        asc = _ras(os.path.join(work, "a", "gft1", "gft1.asc"), 1300.0, "EPSG:2266", driver="AAIGrid")
        for path, root, units, ftype in (
                (img, os.path.join(work, "i"), "metres (DEM_Imagine)", "Erdas Imagine .img, 1 m"),
                (asc, os.path.join(work, "a"), "feet (ASCII_Grids_ft, converted x 0.3048",
                 "ESRI ASCII grid (.asc), feet")):
            dd.fetch_dwr = lambda package, out, products, feedback=None, p=path, r=root: \
                dd.build_vrts([p], r)
            vrt, native = alg._nd({"IMAGINE": True, "ASCII_FT": True, "DWR_PATH": "/temp/abc"},
                                  QgsProcessingContext(), Feedback(), root, 0)
            assert native["units"].startswith(units) and native["file_type"] == ftype, native
    finally:
        dd.fetch_dwr = old
        shutil.rmtree(work, ignore_errors=True)


@register
def the_window_says_imagine_metres_and_ascii_feet():
    with open(os.path.join(os.path.dirname(HERE), "dialog.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert ("ND DWR LiDAR: DEM_Imagine .img in UTM metres (EPSG:26914 / 3721); ASCII_Grids_ft in "
            "State \"\n                          \"Plane feet, converted.") in src
    assert "ND LiDAR native: DEM_Imagine .img in UTM \"\n            \"metres; ASCII_Grids_ft" in src
    assert "ND DWR LiDAR: feet, often EPSG:3721" not in src and "ND LiDAR native is feet" not in src


def main():
    import traceback
    failed = 0
    for t in TESTS:
        try:
            t()
            print("ok    ", t.__name__)
        except Exception:                                    # noqa: BLE001
            failed += 1
            print("FAIL  ", t.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(TESTS), failed))
    return 1 if failed else 0


def _pytest_names():
    for t in TESTS:
        globals()["test_" + t.__name__] = t


_pytest_names()

if __name__ == "__main__":
    sys.exit(main())
