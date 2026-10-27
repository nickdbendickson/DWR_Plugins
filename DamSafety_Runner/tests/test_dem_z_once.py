"""Runner 3.10.89, review round 2 (Nick, Absaraka DWR-2801): the DEM's unit is applied exactly once,
and every tool reads the unit the run reads.

1. GDAL's default vertical shift: a warp from (or into) a compound CRS converts the values - a
   DEM in 'ND North (ftUS) + NAVD88 height (ftUS)' warped to a 2D UTM work CRS came out in metres,
   and GdalTerrain.read then multiplied by z_to_m again (1300 ftUS read as 120.8 m, not 396.2 m).
   Every warp of DEM values (and of Manning / NLCD / output rasters) now keeps the values as
   stored (warp_opts.no_vshift, gdalwarp -novshift).
2. An explicit Feet / Meters is checked against the unit the DEM records (a Download DEM output
   tagged metres run as Feet read x 0.3048 when the ground was above 656 m); the ND ground check
   samples the whole DEM (it read only the top-left 200 x 200 cells, nodata on a clipped 1 m DEM)
   and works both ways (a feet DEM read as metres at 1100-1200 m).
3. z_to_m_factor('auto') is the run's rule (no guess from the CRS): the culvert pre-check, the
   CLI and the HEC-RAS terrain read the unit the run reads.
4. The run report's units warning comes from a structured flag (z_check), not the word 'check'.
5. Files no unit reads right stop whatever the choice: an ND DWR download from Screening before
   0.17.50 in a gft* folder; a warped VRT whose vertical shift converted the values is refused.
Needs GDAL (the QGIS Python); prints SKIP otherwise."""
import json
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_dem_z_detect import _dem, _gis
from nd_breach_runner.tests.test_dem_z_download_record import _old_download, NDDWR, USGS

try:
    from osgeo import gdal, osr
except ImportError:                                      # pragma: no cover - plain python3
    gdal = None

US_FT = 1200.0 / 3937.0
CX, CY = 600200.0, 5199800.0                             # UTM 14N, the box the tests read
BOX = (CX - 100.0, CY - 100.0, CX + 100.0, CY + 100.0)


def _to(x, y, src, dst):
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.SetFromUserInput(src)
    b.SetFromUserInput(dst)
    for s in (a, b):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(a, b).TransformPoint(x, y)[:2]


def _crs_dem(folder, name, crs, value, unit="", n=60, nodata=-9999.0):
    """An n x n GeoTIFF in `crs` (compound allowed) covering 600 m around (CX, CY), all `value`."""
    horiz = crs.split("+")[0]
    x0, y1 = _to(CX - 300.0, CY + 300.0, "EPSG:26914", horiz)
    x1, y0 = _to(CX + 300.0, CY - 300.0, "EPSG:26914", horiz)
    s = osr.SpatialReference()
    s.SetFromUserInput(crs)
    path = os.path.join(folder, name)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetProjection(s.ExportToWkt())
    ds.SetGeoTransform((x0, (x1 - x0) / n, 0.0, y1, 0.0, -(y1 - y0) / n))
    b = ds.GetRasterBand(1)
    b.WriteArray(np.full((n, n), value, dtype="float32"))
    b.SetNoDataValue(nodata)
    if unit:
        b.SetUnitType(unit)
    b = ds = None
    return path


def _read(path):
    ds = gdal.Open(path)
    a = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    return a


def _med(a):
    a = np.asarray(getattr(a, "data", a), dtype=float)
    return float(np.nanmedian(a[np.isfinite(a) & (a > -9000)]))


def _algs():
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    return algs, fake_qgis


def _stops(fn, *words):
    try:
        got = fn()
    except Exception as e:                               # noqa - ValueError / Qgs / CliError
        for w in words:
            assert w in str(e), (w, str(e))
        return str(e)
    raise AssertionError("must stop, got {}".format(got))


@test
def dem_unit_is_applied_once_through_a_compound_crs():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G, hecras_model as hm
    from nd_breach_runner.core.grid import Grid
    d = tempfile.mkdtemp(prefix="demz_once_")
    try:
        want = 1300.0 * US_FT                            # 396.24 m
        for crs in ("EPSG:2265+6360", "EPSG:6318+6360", "EPSG:26914+6360"):
            p = _crs_dem(d, crs.replace(":", "").replace("+", "_") + ".tif", crs, 1300.0)
            assert G.resolve_dem_z(p, "auto")[0] == G.Z_US_FEET, crs
            for z in (G.Z_AUTO, G.Z_US_FEET):
                got = _med(G.GdalTerrain(p, "EPSG:26914", z).read(BOX, 10.0))
                assert abs(got - want) < 0.01, (crs, z, got, want)       # was 120.8
            # the other readers of DEM values keep them as stored; the caller converts once
            s = osr.SpatialReference()
            s.ImportFromEPSG(26914)
            grid = Grid(np.zeros((20, 20)), BOX[0], BOX[3], 10.0, crs_wkt=s.ExportToWkt())
            assert abs(_med(G.read_like(p, grid, "bilinear")) - 1300.0) < 0.01, crs
            a, _x0, _y0 = hm._warp(p, s, 10.0, bounds=BOX)
            assert abs(_med(a) - 1300.0) < 0.01, crs
            t = os.path.join(d, "terrain.tif")
            hm.write_terrain(p, t, s, 10.0, BOX, US_FT / 0.3048, -9999.0, "ft")
            assert abs(_med(_read(t)) - want / 0.3048) < 0.01
        # a band-'ft' DEM (2D) read through a compound metre work CRS: x 0.3048 once
        p = _crs_dem(d, "ft2d.tif", "EPSG:26914", 1300.0, unit="ft")
        work = osr.SpatialReference()
        work.SetFromUserInput("EPSG:26914+5703")
        got = _med(G.GdalTerrain(p, work.ExportToWkt(), G.Z_AUTO).read(BOX, 10.0))
        assert abs(got - 1300.0 * 0.3048) < 0.01, got                    # was 120.8
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def manning_nlcd_and_outputs_keep_their_values_in_a_compound_crs():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G, nd_data
    from nd_breach_runner.core.grid import Grid
    d = tempfile.mkdtemp(prefix="demz_once_n_")
    try:
        dem = _crs_dem(d, "dem.tif", "EPSG:26914+6360", 1300.0)
        work = gdal.Open(dem).GetProjection()            # the DEM's own compound CRS
        n = _crs_dem(d, "n.tif", "EPSG:26914", 0.05)      # 2D, no unit
        t = G.GdalTerrain(dem, work, G.Z_US_FEET, n, 0.06)
        assert abs(_med(t.manning(BOX, 10.0)) - 0.05) < 1e-6, _med(t.manning(BOX, 10.0))  # was 0.164
        assert abs(_med(t.read(BOX, 10.0)) - 1300.0 * US_FT) < 0.01
        codes = _crs_dem(d, "nlcd.tif", "EPSG:26914", 41.0)
        out = os.path.join(d, "n41.tif")
        G.nlcd_to_manning_raster(codes, out, like_path=dem)
        lut = nd_data.manning_lut()
        assert abs(_med(_read(out)) - lut[41]) < 1e-6
        # an output grid (metres) written into the DEM's compound feet CRS stays metres
        s = osr.SpatialReference()
        s.ImportFromEPSG(26914)
        g = Grid(np.full((20, 20), 2.5), BOX[0], BOX[3], 10.0, crs_wkt=s.ExportToWkt())
        path = G.GeoTiffWriter("EPSG:26914", work, 10.0)(g, os.path.join(d, "depth.tif"))
        assert abs(_med(_read(path)) - 2.5) < 1e-6
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def warp_opts_no_vshift_adds_the_flag_once():
    from nd_breach_runner.core import warp_opts as W
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    assert W.no_vshift(gdal, {"format": "MEM"}) == {"format": "MEM", "options": ["-novshift"]}
    kw = W.no_vshift(gdal, W.no_vshift(gdal, {"options": "-q -multi"}))
    assert kw["options"] == ["-q", "-multi", "-novshift"], kw
    gdal.WarpOptions(**W.no_vshift(gdal, W.no_overviews(gdal, {"format": "MEM"})))   # accepted

    class Old(object):                                   # GDAL 3.3: no vertical shift to stop
        @staticmethod
        def VersionInfo(what):
            return "3030000"

        @staticmethod
        def WarpOptions(options=None, format=None):      # noqa: A002
            return options
    assert W.no_vshift(Old, {"format": "MEM"}) == {"format": "MEM"}
    assert W.no_vshift(object(), {"format": "MEM"}) == {"format": "MEM"}      # a test double


@test
def a_warp_that_converted_the_values_is_not_read_by_its_band_unit():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_once_w_")
    try:
        src = _crs_dem(d, "c.tif", "EPSG:2265+6360", 1300.0)
        vrt = os.path.join(d, "w.vrt")                   # gdalwarp without -novshift
        gdal.Warp(vrt, src, format="VRT", dstSRS="EPSG:26914").FlushCache()
        _stops(lambda: G.resolve_dem_z(vrt, "auto"), "vertical shift", "-novshift")
        # the same warp written as a GeoTIFF: GDAL < 3.9 keeps 'US survey foot' on metres with
        # no trace, the ND ground check stops it; GDAL >= 3.9 strips the band unit, so there is
        # no recorded unit and Detect stops for that reason - both are safe
        tif = os.path.join(d, "w.tif")
        gdal.Warp(tif, src, dstSRS="EPSG:26914").FlushCache()
        _ds = gdal.Open(tif)
        kept_unit = bool(_ds.GetRasterBand(1).GetUnitType())
        _ds = None
        if kept_unit:
            msg = _stops(lambda: _gis(algs, tif, 0), "396.2 with units 'US survey feet' "
                         "(detected: raster unit 'us survey foot')", "to Meters", "record is wrong")
            assert "121 m" in msg, msg
        else:
            _stops(lambda: _gis(algs, tif, 0), "no elevation unit is recorded",
                   "Set 'DEM elevation units' to Meters or Feet")
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def an_explicit_unit_the_dem_records_otherwise_stops():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_once_x_")
    try:
        west = _dem(d, "west_10m_5070.tif", "m", 700.0)  # 213 m as feet: passed the ND check
        for z in (1, 2):                                 # Feet, US survey feet
            _stops(lambda: _gis(algs, west, z), "is Feet" if z == 1 else "is US survey feet",
                   "records metres (raster unit 'm')", "set 'DEM elevation units' to Meters",
                   "cannot tell which is right (213 m as ",
                   "choose '{} (the raster's unit tag is wrong)'".format(
                       "Feet" if z == 1 else "US survey feet"), "gdal_edit -units")
        assert _gis(algs, west, 3)[0].z_to_m == 1.0
        ft = _dem(d, "ft.tif", "ft", 1150.0)
        msg = _stops(lambda: _gis(algs, ft, 3), "is Meters", "records feet",
                     "only as feet (351 m as feet, 1150 m as metres)", "to Feet")
        assert "tag is wrong" not in msg and "gdal_edit" not in msg, msg   # the record is right
        assert abs(_gis(algs, ft, 2)[0].z_to_m - US_FT) < 1e-12        # US feet on 'ft': 2 ppm
        plain = _dem(d, "plain.tif", "", 700.0)          # records nothing: the choice is used
        assert abs(_gis(algs, plain, 1)[0].z_to_m - 0.3048) < 1e-12
        # the way out the message gives: correct the record
        ds = gdal.Open(west, gdal.GA_Update)
        ds.GetRasterBand(1).SetUnitType("ft")
        ds = None
        assert abs(_gis(algs, west, 1)[0].z_to_m - 0.3048) < 1e-12
        # a compound CRS's vertical unit is a record too (2300 there is 701 m as feet and not
        # North Dakota as metres: Feet runs, saying the record is wrong); a band unit overrides it
        cm = _crs_dem(d, "c5703.tif", "EPSG:26914+5703", 2300.0)
        _stops(lambda: _gis(algs, cm, 3), "DEM median 2300.0 with units 'Meters'", "to Feet")
        src = _gis(algs, cm, 1)[0]
        assert abs(src.z_to_m - 0.3048) < 1e-12 and src.z_note == "feet (chosen)", src.z_note
        assert "c5703.tif records metres" in src.z_check and "only as feet" in src.z_check, \
            src.z_check
        ds = gdal.Open(cm, gdal.GA_Update)
        ds.GetRasterBand(1).SetUnitType("ft")
        ds = None
        assert abs(_gis(algs, cm, 1)[0].z_to_m - 0.3048) < 1e-12
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def the_nd_ground_check_samples_the_whole_dem_and_says_when_it_cannot():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G, workflow
    algs, fq = _algs()
    d = tempfile.mkdtemp(prefix="demz_once_s_")
    try:
        # Absaraka at 1 m: metres, no unit, the top-left 250 x 250 cells nodata (a clipped box)
        path = os.path.join(d, "clip_1m.tif")
        a = np.full((400, 400), 320.0, dtype="float32")
        a[:250, :250] = -9999.0
        s = osr.SpatialReference()
        s.ImportFromEPSG(26914)
        ds = gdal.GetDriverByName("GTiff").Create(path, 400, 400, 1, gdal.GDT_Float32)
        ds.SetProjection(s.ExportToWkt())
        ds.SetGeoTransform((CX - 200.0, 1.0, 0.0, CY + 200.0, 0.0, -1.0))
        ds.GetRasterBand(1).WriteArray(a)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds = None
        med, cells = G.sample_median(gdal.Open(path))
        assert med == 320.0 and cells > 10, (med, cells)
        # the sample is spread over the raster: a corner unlike the rest (30 there) does not decide
        odd = os.path.join(d, "corner_30.tif")
        a[:250, :250] = 30.0
        ds = gdal.GetDriverByName("GTiff").Create(odd, 400, 400, 1, gdal.GDT_Float32)
        ds.SetProjection(s.ExportToWkt())
        ds.SetGeoTransform((CX - 200.0, 1.0, 0.0, CY + 200.0, 0.0, -1.0))
        ds.GetRasterBand(1).WriteArray(a)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds = None
        assert G.sample_median(gdal.Open(odd))[0] == 320.0
        assert _gis(algs, odd, 3)[0].z_to_m == 1.0       # the top-left read stopped: median 30
        _stops(lambda: _gis(algs, path, 1), "DEM median 320.0 with units 'Feet'",
               "set 'DEM elevation units' to Meters")             # ran x 0.3048 before
        src = _gis(algs, path, 3)[0]
        assert src.z_check == "" and workflow.units_warning(src) == ""
        # every finite non-nodata cell counts (no +-1e5 filter: a raw 130000 is not "no sample")
        big = _dem(d, "big.tif", "", 130000.0)
        _stops(lambda: _gis(algs, big, 3), "DEM median 130000.0", "Neither feet nor metres")
        # no valid cell anywhere: the run goes on, and the report says the unit is not checked
        empty = os.path.join(d, "empty.tif")
        ds = gdal.GetDriverByName("GTiff").Create(empty, 50, 50, 1, gdal.GDT_Float32)
        ds.SetProjection(s.ExportToWkt())
        ds.SetGeoTransform((CX - 25.0, 1.0, 0.0, CY + 25.0, 0.0, -1.0))
        ds.GetRasterBand(1).Fill(-9999.0)
        ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds = None
        assert G.sample_median(gdal.Open(empty)) == (None, 0)
        src = _gis(algs, empty, 3)[0]
        assert src.z_check == G.NO_SAMPLE, src.z_check
        w = workflow.units_warning(src)
        assert "metres (chosen)" in w and "found no valid elevation" in w, w
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def the_nd_ground_check_works_both_ways():
    from nd_breach_runner.core import gdal_io as G
    msg = G.median_unit_problem(1150.0, G.Z_METERS)               # 1150 m: above White Butte
    assert msg and "Read as feet it is 351 m" in msg and "to Feet" in msg, msg
    assert G.median_unit_problem(1190.0, G.Z_METERS)
    assert G.median_unit_problem(1060.0, G.Z_METERS) is None       # west ND, metres
    assert G.median_unit_problem(1150.0, G.Z_FEET) is None         # 351 m as feet
    msg = G.median_unit_problem(1150.0, G.Z_METERS, "detected: raster unit 'm'")
    assert "'Meters' (detected: raster unit 'm')" in msg and "record is wrong" in msg, msg
    assert "record is wrong" not in G.median_unit_problem(1150.0, G.Z_METERS)


@test
def the_units_warning_is_a_flag_not_a_word_in_the_note():
    from nd_breach_runner.core import workflow
    note = ("metres (detected: Download DEM facts file dam_check_10m_5070_dem.txt: USGS 3DEP "
            "ImageServer (elevation.nationalmap.gov), units metres)")
    assert workflow.units_warning(types.SimpleNamespace(z_note=note)) == ""
    assert workflow.units_warning(types.SimpleNamespace(z_note=note, z_check="")) == ""
    w = workflow.units_warning(types.SimpleNamespace(z_note="metres (chosen)", z_check="why"))
    assert w.startswith("DEM elevation units: metres (chosen) - why."), w


@test
def every_tool_reads_the_unit_the_run_reads():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G, culvert_precheck as cp, hecras_engine as he
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.scripts import ndbreach_cli as cli
    d = tempfile.mkdtemp(prefix="demz_once_t_")
    try:
        s = osr.SpatialReference()
        s.ImportFromEPSG(26914)
        # a 0.17.49 download (no band unit, its USGS 3DEP record): metres, as the run reads it
        old = _old_download(d, "D1_10m_5070.tif", value=700.0)
        f, note = G.z_to_m_factor(old, G.Z_AUTO)
        assert f == 1.0 and note.startswith("metres (detected: Download DEM record"), note
        fine = cp.FineRaster(old, s.ExportToWkt(), G.Z_AUTO)
        assert fine.z_to_m == 1.0 and fine.z_choice == G.Z_METERS, (fine.z_to_m, fine.z_note)
        # its copy warped to ND South feet keeps only the metadata: the run stops, and so does
        # every tool (z_to_m_factor('auto') guessed from the feet CRS and the median)
        ft_crs = os.path.join(d, "D1_2266.tif")
        gdal.Warp(ft_crs, old, dstSRS="EPSG:2266").FlushCache()
        for fn in (lambda: G.resolve_dem_z(ft_crs, "auto"), lambda: G.z_to_m_factor(ft_crs),
                   lambda: cp.FineRaster(ft_crs, s.ExportToWkt(), G.Z_AUTO)):
            _stops(fn, "'DEM elevation units'", "not trusted alone")
        # nothing recorded, in a feet CRS or a metre one: refused with the run's message
        for plain in (_crs_dem(d, "plain_2266.tif", "EPSG:2266", 1050.0), _dem(d, "plain.tif", "", 320.0)):
            _stops(lambda: G.z_to_m_factor(plain, G.Z_AUTO), "'DEM elevation units'",
                   "no elevation unit is recorded")
            _stops(lambda: cp.FineRaster(plain, s.ExportToWkt(), G.Z_AUTO), "no elevation unit")
        # an explicit unit the record contradicts stops the pre-check as it stops the run
        tagged = _dem(d, "m.tif", "m", 700.0)
        _stops(lambda: cp.FineRaster(tagged, s.ExportToWkt(), G.Z_FEET), "records metres")
        # HEC-RAS terrain: its tag decides; an untagged one (review round 3: refused, naming a
        # 'DEM elevation units' the HEC-RAS tools do not have) is read in its CRS's linear unit
        # as before 3.10.88, with a warning naming the unit used and the tools' own setting
        grid = Grid(np.zeros((10, 10)), 600000.0, 5200000.0, 10.0, crs_wkt=s.ExportToWkt())
        t_ft = _dem(d, "t_ft.tif", "ft", 1300.0)
        assert abs(_med(he.terrain_on_grid(t_ft, grid)) - 1300.0) < 0.01
        notes = []
        assert abs(_med(he.terrain_on_grid(plain, grid, notes=notes)) - 320.0 / 0.3048) < 0.01
        assert "metres (assumed" in notes[0] and "'Terrain elevation units'" in notes[1], notes
        # the CLI: an ESRI ASCII grid records no unit, so auto stops (it guessed metres)
        asc = os.path.join(d, "dem.asc")
        with open(asc, "w") as fh:
            fh.write("ncols 3\nnrows 3\nxllcorner 0\nyllcorner 0\ncellsize 10\n"
                     "NODATA_value -9999\n1 2 3\n4 5 6\n7 8 9\n")
        _stops(lambda: cli.AscTerrain(asc, G.Z_AUTO), "dem.z_units is auto",
               "records no elevation unit")
        assert cli.AscTerrain(asc, G.Z_METERS).z_to_m == 1.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _nd_download(folder, name, writer=False, metadata=True, value=121.92):
    """An ND DWR LiDAR download as Screening 0.17.19-0.17.49 wrote it: band unit 'm' (build_vrts
    tagged its VRTs), the record and facts file; `writer` adds the 0.17.50 WRITER / written_by."""
    os.makedirs(folder, exist_ok=True)
    path = _old_download(folder, name, value=value, source=NDDWR, metadata=metadata)
    ds = gdal.Open(path, gdal.GA_Update)
    ds.GetRasterBand(1).SetUnitType("m")
    if writer and metadata:
        ds.SetMetadataItem("WRITER", "Screening 0.17.50 Download DEM")
    ds = None
    if writer:
        with open(os.path.splitext(path)[0] + "_dem.txt", "a", encoding="utf-8") as fh:
            fh.write("written_by: Screening 0.17.50 Download DEM\n")
    return path


@test
def an_old_nd_dwr_download_in_a_gft_folder_is_refused_whatever_the_choice():
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_once_g_")
    try:
        for sub, meta in (("gftaylor", True), (os.path.join("GFT_dems", "x"), False)):
            bad = _nd_download(os.path.join(d, sub), "D7_10m_5070.tif", metadata=meta)
            for z in ("auto", "meters", "feet"):
                _stops(lambda: G.resolve_dem_z(bad, z), "before 0.17.50",
                       "'{}'".format(sub.split(os.sep)[0]), "x 0.3048",
                       "Delete D7_10m_5070.tif and D7_10m_5070_dem.txt")
        ok = _nd_download(os.path.join(d, "plain"), "D7_10m_5070.tif")
        assert G.resolve_dem_z(ok, "auto")[0] == G.Z_METERS             # band 'm', values right
        new = _nd_download(os.path.join(d, "gft2"), "D7_10m_5070.tif", writer=True)
        assert G.resolve_dem_z(new, "auto")[0] == G.Z_METERS             # written by 0.17.50
        # 'gft' only in the file name, or below another folder name: not the old rule's path
        ok2 = _nd_download(os.path.join(d, "dems"), "gft_D7_10m_5070.tif")
        assert G.resolve_dem_z(ok2, "auto")[0] == G.Z_METERS
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def a_download_record_is_trusted_only_whole():
    """The trust rules the mutation run showed untested (F1b/c/k/m/n)."""
    if gdal is None:
        print("SKIP dem_z_once (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_once_r_")
    try:
        # SOURCE + UNITS alone (the 24137fe rule) are not the five write_local_dem keys
        p = _dem(d, "two_keys.tif", "", 320.0)
        ds = gdal.Open(p, gdal.GA_Update)
        ds.SetMetadataItem("SOURCE", USGS)
        ds.SetMetadataItem("UNITS", "metres")
        ds = None
        _stops(lambda: G.resolve_dem_z(p, "auto"), "no elevation unit is recorded")
        # a facts file without the five keys: refused, not a KeyError
        p = _dem(d, "short_10m_5070.tif", "", 320.0)
        with open(os.path.splitext(p)[0] + "_dem.txt", "w", encoding="utf-8") as fh:
            fh.write("source: {}\nunits: metres\n".format(USGS))
        _stops(lambda: G.resolve_dem_z(p, "auto"), "no elevation unit is recorded")
        # review round 2: both halves of the record, agreeing, and the raster not replaced later
        both = _old_download(d, "both_10m_5070.tif")
        assert G.resolve_dem_z(both, "auto")[0] == G.Z_METERS
        ft = os.path.join(d, "both_ft.tif")                 # a feet copy keeps the metadata
        gdal.Translate(ft, both, scaleParams=[[0, 1, 0, 1 / 0.3048]],
                       outputType=gdal.GDT_Float32).FlushCache()
        _stops(lambda: G.resolve_dem_z(ft, "auto"), "not trusted alone")
        side = os.path.splitext(both)[0] + "_dem.txt"
        t = os.path.getmtime(side)
        os.utime(both, (t + 3600.0, t + 3600.0))             # rewritten an hour after its facts
        _stops(lambda: G.resolve_dem_z(both, "auto"), "60 min after its facts file")
        os.utime(both, (t, t))
        with open(side, encoding="utf-8") as fh:
            text = fh.read()
        with open(side, "w", encoding="utf-8") as fh:
            fh.write(text.replace("source: " + USGS, "source: USGS 3DEP 1 m lidar tiles (native, TNM)"))
        os.utime(side, (t, t))
        _stops(lambda: G.resolve_dem_z(both, "auto"), "is not the raster's record")
        # a USGS record that says feet is not read as metres
        p = _old_download(d, "usgs_ft_10m_5070.tif", sidecar=False)
        ds = gdal.Open(p, gdal.GA_Update)
        ds.SetMetadataItem("UNITS", "feet")
        ds = None
        _stops(lambda: G.resolve_dem_z(p, "auto"), "no elevation unit is recorded")
        # an old lidar VRT whose json names its feet fallback relative to the json
        sub = os.path.join(d, "lidar")
        os.makedirs(sub)
        tile = _dem(sub, "tile.tif", "m", 290.0)
        fb = _dem(sub, "fb.tif", "ft", 951.0)
        vrt = os.path.join(sub, "D1_1m_lidar.vrt")
        v = gdal.BuildVRT(vrt, [fb, tile])
        v.GetRasterBand(1).SetUnitType("m")
        v = None
        with open(os.path.join(sub, "D1_1m_lidar.json"), "w", encoding="utf-8") as fh:
            json.dump({"format": "damsuite/lidar_1m_vrt", "fallback": "fb.tif"}, fh)
        cwd = os.getcwd()
        try:
            os.chdir(d)                                  # not the json's folder
            _stops(lambda: G.resolve_dem_z(vrt, "auto"), "fb.tif", "in feet")
        finally:
            os.chdir(cwd)
        # another tool's json beside a VRT is not a lidar VRT record
        with open(os.path.join(sub, "D1_1m_lidar.json"), "w", encoding="utf-8") as fh:
            json.dump({"fallback": fb}, fh)
        assert G.resolve_dem_z(vrt, "auto")[0] == G.Z_METERS
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def nd_dwr_guidance_says_imagine_metres_and_ascii_feet():
    from nd_breach_runner.core import gdal_io as G
    h = G.DEM_Z_HELP
    assert "ND DWR DEM_Imagine (.img, UTM metres)" in h, h
    assert "Feet: the raw ND DWR QL2 ASCII_Grids_ft grids (.asc, ND State Plane feet)" in h, h
    assert "also DEM_Imagine feet" not in h and "EPSG:3721" not in h, h
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "wizard.py"), encoding="utf-8") as fh:
        wiz = fh.read()
    assert "ND DWR LiDAR: DEM_Imagine .img in UTM metres" in wiz
    assert "(ASCII_Grids_ft: State Plane feet, converted)" in wiz
    assert "ND DWR LiDAR: feet, often EPSG:3721" not in wiz
