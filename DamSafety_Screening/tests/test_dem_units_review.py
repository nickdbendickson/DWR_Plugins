"""Screening 0.17.50, review of Download DEM's units (Nick, Absaraka DWR-2801). The Runner's
'Detect from the raster' now trusts the unit a DEM records, so what Download DEM writes must be
right:

1. The 1 m lidar VRT is tagged metres; a FALLBACK_DEM in feet showed through it as metres. It
   is converted (x 0.3048, or x 1200/3937 for US survey feet) before it goes under the tiles,
   and a fallback that records no unit is not used (lidar_tnm.build_vrt).
2. build_vrts took every metre DEM_Imagine tile for a feet grid (x 0.3048, then tagged 'm')
   when '/gft' was anywhere in the path - an output folder D:/gft_dems or C:/Users/gftaylor.
   Only the path below the download folder counts now (_product_of).
3. write_local_dem / warp_to tag the unit the source records, not a blind 'm'.
4. A reused local DEM from before 0.17.50: a USGS 3DEP one is tagged metres; an ND DWR one is
   left as it is, with a warning (never tagged blindly).
5. The request-count note for a county / the state: 'W x H km box', a smaller buffer only for
   a dam, never the native tiles for the state (that source refuses it).
6. DEM_Imagine is UTM metres (EPSG:3721 is UTM 14N) in the help, the IMAGINE label and the
   facts file - as build_vrts reads it; only ASCII_Grids_ft is feet.
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


def _tif(path, value, epsg=26914, gt=(600000.0, 10.0, 0.0, 5200000.0, 0.0, -10.0), n=30,
         unit="", driver="GTiff"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    mem = gdal.GetDriverByName("MEM").Create("", n, n, 1, gdal.GDT_Float32)
    mem.SetProjection(s.ExportToWkt())
    mem.SetGeoTransform(gt)
    b = mem.GetRasterBand(1)
    b.WriteArray(np.full((n, n), value, dtype="float32"))
    b.SetNoDataValue(-9999.0)
    if unit:
        b.SetUnitType(unit)
    gdal.GetDriverByName(driver).CreateCopy(path, mem)
    mem = None
    return path


def _unit(p):
    ds = gdal.Open(p)
    try:
        return ds.GetRasterBand(1).GetUnitType()
    finally:
        ds = None


def _at(p, x, y):
    ds = gdal.Open(p)
    gt = ds.GetGeoTransform()
    c, r = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
    v = float(ds.GetRasterBand(1).ReadAsArray(c, r, 1, 1)[0, 0])
    ds = None
    return v


def _tile_entries(work):
    """One 1 m lidar tile, 290 m (USGS tiles record metres), 100 x 100 m at the box's centre."""
    t = _tif(os.path.join(work, "tiles", "USGS_1M_14_x60y520_P.tif"), 290.0,
             gt=(600100.0, 1.0, 0.0, 5199900.0, 0.0, -1.0), n=100, unit="metre")
    return [{"path": t, "tile": os.path.basename(t), "project": "P"}]


@register
def lidar_vrt_converts_a_feet_fallback_and_refuses_one_without_a_unit():
    work = tempfile.mkdtemp(prefix="scr_fb_")
    try:
        entries = _tile_entries(work)
        outside = (600020.0, 5199980.0)                  # fallback only: no tile there
        inside = (600150.0, 5199850.0)
        cases = (("ft", 951.0, 951.0 * 0.3048, "feet"),
                 ("US survey foot", 951.0, 951.0 * 1200.0 / 3937.0, "US survey feet"),
                 ("m", 290.0, 290.0, "metres"))
        for unit, value, want, words in cases:
            fb = _tif(os.path.join(work, unit.replace(" ", "_"), "fb.tif"), value, unit=unit)
            fbk = Feedback()
            vrt, js = lt.build_vrt(entries, os.path.join(os.path.dirname(fb), "D1_1m_lidar.vrt"),
                                   fb, feedback=fbk)
            assert _unit(vrt) == "m", _unit(vrt)
            assert abs(_at(vrt, *outside) - want) < 0.01, (unit, _at(vrt, *outside), want)
            assert abs(_at(vrt, *inside) - 290.0) < 1e-4            # the tiles on top
            info = json.load(open(js, encoding="utf-8"))
            assert info["fallback"] == fb and info["fallback_units"].startswith(words), info
            assert info["fallback_refused"] == ""
            if unit != "m":
                assert any("converted to metres" in w for w in fbk.warnings), fbk.warnings
        # a Download DEM 10 m GeoTIFF from before 0.17.50 (no band unit, USGS 3DEP record): metres
        old = os.path.join(work, "old", "D1_10m_5070.tif")
        dd.write_local_dem(_tif(os.path.join(work, "old", "src.tif"), 290.0), old, 10,
                           dd.product_info("usgs", 10))
        ds = gdal.Open(old, gdal.GA_Update)
        ds.GetRasterBand(1).SetUnitType("")
        ds = None
        assert _unit(old) == ""
        vrt, js = lt.build_vrt(entries, os.path.join(work, "old", "D1_1m_lidar.vrt"), old)
        assert json.load(open(js, encoding="utf-8"))["fallback_units"].startswith("metres")
        # no unit recorded: not used, the VRT has the tiles only (and says why)
        fb = _tif(os.path.join(work, "none", "fb.tif"), 951.0)
        fbk = Feedback()
        vrt, js = lt.build_vrt(entries, os.path.join(work, "none", "D1_1m_lidar.vrt"), fb,
                               feedback=fbk)
        info = json.load(open(js, encoding="utf-8"))
        assert info["fallback"] == "" and fb in info["fallback_refused"], info
        assert any("is not used" in w and "fb.tif" in w for w in fbk.warnings), fbk.warnings
        assert abs(_at(vrt, *inside) - 290.0) < 1e-4
        ds = gdal.Open(vrt)                                    # the tile only, not 951 'metres'
        files = [os.path.basename(f) for f in ds.GetFileList()]
        assert (ds.RasterXSize, ds.RasterYSize) == (100, 100) and "fb.tif" not in files, files
        ds = None
    finally:
        shutil.rmtree(work, ignore_errors=True)


@register
def nd_lidar_product_is_decided_below_the_download_folder():
    work = tempfile.mkdtemp(prefix="scr_gft_")
    try:
        imgs = {}
        for base in ("plain", "gft_dems", "gftaylor"):
            root = os.path.join(work, base, "_dem_tiles_x")
            img = _tif(os.path.join(root, "dem123", "dem123.img"), 400.0, epsg=3721, driver="HFA")
            imgs[root] = img
            v = dd.build_vrts([img], root)                 # was nd_lidar_ascii_m.vrt, 121.92 'm'
            assert os.path.basename(v) == "nd_lidar_imagine_m.vrt", (base, v)
            assert _unit(v) == "m" and abs(_at(v, 600005.0, 5199995.0) - 400.0) < 1e-4, base
        for root, img in imgs.items():
            assert dd._product_of(img, root) == "DEM_Imagine", (root, img)
        root = os.path.join(work, "gftaylor", "_dem_tiles_y")
        asc = os.path.join(root, "gft123", "gft123.asc")
        assert dd._product_of(asc, root) == "ASCII_Grids_ft"
        assert dd._product_of(os.path.join(root, "pkg", "ASCII_Grids_ft", "t.grd"), root) == \
            "ASCII_Grids_ft"
        assert dd._product_of(os.path.join(root, "gft9", "gft9.grd"), root) == "ASCII_Grids_ft"
        # outside the download folder: only the file's folder and name count
        assert dd._product_of("/data/gftaylor/dem7/dem7.img", root) == "DEM_Imagine"
        assert dd._product_of("/data/gft7/gft7.grd", root) == "ASCII_Grids_ft"
    finally:
        shutil.rmtree(work, ignore_errors=True)


@register
def local_dem_and_extra_copy_carry_the_unit_the_source_records():
    work = tempfile.mkdtemp(prefix="scr_lu_")
    try:
        ft = _tif(os.path.join(work, "src_ft.tif"), 1000.0, unit="ft")
        fbk = Feedback()
        dest, side = dd.write_local_dem(ft, os.path.join(work, "o", "x_10m_5070.tif"), 10,
                                        dd.product_info("ndlidar", 10), fbk)
        assert _unit(dest) == "ft", _unit(dest)
        ds = gdal.Open(dest)
        assert ds.GetMetadataItem("UNITS") == "feet", ds.GetMetadata()
        ds = None
        assert "units: feet" in open(side, encoding="utf-8").read()
        assert any("is feet" in w for w in fbk.warnings), fbk.warnings
        assert dd.recorded_unit(dest)[0] == ZU.FEET
        w = dd.warp_to(dest, os.path.join(work, "o", "dem_26914.tif"), 26914)
        assert _unit(w) == "ft", _unit(w)
        # a metre source from build_vrts: 'm', and the facts say metres
        m = _tif(os.path.join(work, "src_m.tif"), 300.0, unit="m")
        dest, side = dd.write_local_dem(m, os.path.join(work, "o", "y_10m_5070.tif"), 10,
                                        dd.product_info("ndlidar", 10))
        assert _unit(dest) == "m" and "units: metres" in open(side, encoding="utf-8").read()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _old_local(out, stem, info):
    """A local DEM as Screening 0.17.49 wrote it: the record and facts file, no band unit."""
    src = _tif(os.path.join(out, "_src", stem + ".tif"), 320.0, epsg=5070,
               gt=(0.0, 10.0, 0.0, 1500300.0, 0.0, -10.0))
    dest, side = dd.write_local_dem(src, os.path.join(out, stem + "_10m_5070.tif"), 10, info)
    ds = gdal.Open(dest, gdal.GA_Update)
    ds.GetRasterBand(1).SetUnitType("")
    ds = None
    assert _unit(dest) == ""
    return dest


@register
def reused_local_dem_is_tagged_only_when_it_is_a_usgs_download():
    from qgis.core import QgsProcessingContext
    from nd_dam_screening.algorithms import download_dem as mod
    work = tempfile.mkdtemp(prefix="scr_reuse_")
    try:
        out = os.path.join(work, "out")
        usgs = _old_local(out, "D123", dd.product_info("usgs", 10))
        nd = _old_local(out, "D124", dd.product_info("ndlidar", 10))
        alg = mod.DownloadDem()
        alg.initAlgorithm()
        base = {"RES": 0, "SCOPE": 0, "DAM_POINT": "-97.39,46.98", "BUFFER_MI": 10.0, "OUT": out,
                "COUNTY": 0, "WORK_CRS": ""}
        fbk = Feedback()
        res = alg.processAlgorithm(dict(base, SOURCE=0, DAM_ID="D123"), QgsProcessingContext(), fbk)
        assert res["VRT"] == usgs and any(m.startswith("reusing") for m in fbk.log), fbk.log
        assert _unit(usgs) == "m" and any("now tagged metres" in m for m in fbk.log), fbk.log
        fbk = Feedback()
        res = alg.processAlgorithm(dict(base, SOURCE=1, DAM_ID="D124"), QgsProcessingContext(), fbk)
        assert res["VRT"] == nd and _unit(nd) == "", _unit(nd)
        assert any("before Screening 0.17.50" in w and "delete" in w for w in fbk.warnings), \
            fbk.warnings
    finally:
        shutil.rmtree(work, ignore_errors=True)


@register
def request_note_fits_a_county_and_the_state():
    burleigh = dd.to_5070(*dd.COUNTIES["Burleigh"])
    n, km = dd.imageserver_chunks(burleigh, 1)
    w, h = (burleigh[2] - burleigh[0]) / 1000.0, (burleigh[3] - burleigh[1]) / 1000.0
    note = dd.chunk_note(n, 1, km, w, h, scope=1)
    assert "{:.0f} x {:.0f} km box".format(w, h) in note and "square" not in note, note
    assert dd.TNM_SOURCE in note and "buffer" not in note, note
    state = dd.to_5070(*dd.STATE)
    n, km = dd.imageserver_chunks(state, 1)
    note = dd.chunk_note(n, 1, km, (state[2] - state[0]) / 1000.0,
                         (state[3] - state[1]) / 1000.0, scope=2)
    assert "use 10 m" in note and "a county or a box" in note and "buffer" not in note, note
    assert "downloads the 10 km lidar tiles" not in note, note
    # download() passes the area on (the ImageServer stubbed: no network)
    work = tempfile.mkdtemp(prefix="scr_note_")
    old = dd.terrain.fetch_3dep
    try:
        tif = _tif(os.path.join(work, "t.tif"), 300.0, epsg=5070)
        dd.terrain.fetch_3dep = lambda *a, **k: [tif]
        for scope, want, not_want in ((1, dd.TNM_SOURCE, "buffer"), (2, "use 10 m", "lists them")):
            fbk = Feedback()
            dd.download(burleigh if scope == 1 else state, 1, work, "s{}".format(scope), fbk,
                        scope=scope)
            note = [x for x in fbk.warnings if "export requests" in x]
            assert note and want in note[0] and not_want not in note[0], (scope, note)
    finally:
        dd.terrain.fetch_3dep = old
        shutil.rmtree(work, ignore_errors=True)


@register
def nd_dwr_imagine_is_described_as_utm_metres():
    from nd_dam_screening.algorithms import download_dem as mod
    alg = mod.DownloadDem()
    alg.initAlgorithm()
    help_ = alg.shortHelpString()
    assert "native units feet" not in help_ and "(feet, often EPSG:3721" not in help_, help_
    assert "DEM_Imagine is Erdas Imagine .img at 1 m in the tile's UTM CRS" in help_, help_
    assert "metres" in alg._params["IMAGINE"].description and \
        "feet" not in alg._params["IMAGINE"].description, alg._params["IMAGINE"].description
    info = dd.product_info("ndlidar", 10)
    assert info["native_units"].startswith("metres (DEM_Imagine)"), info
    assert "DEM_Imagine UTM metres" in info["native_crs"], info


@register
def dem_zunit_is_the_same_file_as_the_runners():
    mine = os.path.join(os.path.dirname(HERE), "core", "dem_zunit.py")
    other = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(HERE))),
                         "DamSafety_Runner", "core", "dem_zunit.py")
    if not os.path.isfile(other):
        print("SKIP the parity part (no DamSafety_Runner next to Screening)")
        return
    with open(mine, "rb") as a, open(other, "rb") as b:
        assert a.read() == b.read(), "core/dem_zunit.py differs between Screening and the Runner"


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
