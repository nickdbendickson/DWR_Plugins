"""Runner 3.10.89 (review of the Download DEM record, Absaraka DWR-2801): what 'Detect from the
raster' may trust (core/dem_zunit.py, byte-identical in Screening).

1. A Download DEM record is trusted only for a USGS 3DEP download: an ND DWR LiDAR one is not
   (Download DEM before Screening 0.17.50 could write feet grids labelled metres), nor the
   SOURCE / UNITS metadata of another tool, nor a <stem>_dem.txt facts file whose file type,
   CRS or cell size is not the raster's.
2. A band unit with a scale / offset on band 1 stops: the unit applies to the scaled values,
   the run reads the raw ones.
3. A 1 m lidar VRT tagged metres that Screening built before 0.17.50 over a feet (or untagged)
   fallback DEM stops; one built by 0.17.50 ('fallback_units' in its json) is trusted.
4. The units note of a detected run says how ("metres (detected: ...)"), not "(chosen)".
Needs GDAL (the QGIS Python); prints SKIP otherwise."""
import json
import os
import shutil
import tempfile

from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.test_dem_z_detect import _dem, _gis
from nd_breach_runner.tests.test_dem_z_download_record import _old_download, NDDWR

try:
    from osgeo import gdal
except ImportError:                                      # pragma: no cover - plain python3
    gdal = None


def _stops(G, path, *words):
    try:
        got = G.resolve_dem_z(path, "auto")
    except ValueError as e:
        msg = str(e)
        assert "'DEM elevation units'" in msg and "Meters or Feet" in msg, msg
        for w in words:
            assert w in msg, (w, msg)
        return msg
    raise AssertionError("{} must not be detected: {}".format(os.path.basename(path), got))


@test
def dem_z_detect_trusts_only_a_usgs_download_record_that_describes_the_raster():
    if gdal is None:
        print("SKIP dem_z_trust (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_trust_")
    try:
        # ND DWR LiDAR record, in the file or in the facts file: not trusted
        _stops(G, _old_download(d, "nd_10m_5070.tif", source=NDDWR, sidecar=False),
               "ND DWR QL2 LiDAR", "not trusted")
        _stops(G, _old_download(d, "nds_10m_5070.tif", source=NDDWR, metadata=False),
               "nds_10m_5070_dem.txt", "not trusted")
        # another tool's SOURCE / UNITS (all five keys, but not a Download DEM product)
        _stops(G, _old_download(d, "other.tif", source="My own lidar mosaic", sidecar=False),
               "no Download DEM record")
        # facts files that describe another raster: cell size, CRS, file type
        _stops(G, _old_download(d, "cell_10m_5070.tif", cell=1),
               "does not describe this raster", "cell 1 m")
        _stops(G, _old_download(d, "crs_10m_5070.tif", crs="EPSG:5070"),
               "does not describe this raster", "EPSG:5070")
        tif = _old_download(d, "same_10m_5070.tif")
        assert G.resolve_dem_z(tif, "auto")[0] == G.Z_METERS           # its own record: trusted
        sub = os.path.join(d, "v")
        os.makedirs(sub)
        vrt = os.path.join(sub, "same_10m_5070.vrt")                    # same stem, a VRT
        gdal.BuildVRT(vrt, [tif]).FlushCache()
        shutil.copy(os.path.splitext(tif)[0] + "_dem.txt", os.path.join(sub, "same_10m_5070_dem.txt"))
        _stops(G, vrt, "replaced or re-exported")          # a VRT has no in-file record
        # review round 2: the five keys of write_local_dem alone are not trusted (a copy keeps
        # them whatever it did to the values); with the facts file beside them they are
        _stops(G, _old_download(d, "usgs_10m_5070.tif", sidecar=False), "not trusted alone")
        got, how = G.resolve_dem_z(tif, "auto")
        assert got == G.Z_METERS and "USGS 3DEP ImageServer" in how, how
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def dem_z_detect_stops_on_a_scaled_or_offset_band():
    if gdal is None:
        print("SKIP dem_z_scale (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_scale_")
    try:
        for name, scale, offset in (("scaled.tif", 0.3048, None), ("offset.tif", None, 350.0)):
            p = _dem(d, name, "m", 950.0)
            ds = gdal.Open(p, gdal.GA_Update)
            if scale is not None:
                ds.GetRasterBand(1).SetScale(scale)
            if offset is not None:
                ds.GetRasterBand(1).SetOffset(offset)
            ds = None
            _stops(G, p, "scale", "gdal_translate -unscale")
            # an explicit unit is used as given (as before)
            assert G.resolve_dem_z(p, "meters") == (G.Z_METERS, "chosen")
        # scale 1 / offset 0 (what a scaled-Translate VRT or an unscaled copy reports): fine
        p = _dem(d, "one.tif", "m")
        ds = gdal.Open(p, gdal.GA_Update)
        ds.GetRasterBand(1).SetScale(1.0)
        ds.GetRasterBand(1).SetOffset(0.0)
        ds = None
        assert G.resolve_dem_z(p, "auto")[0] == G.Z_METERS
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _lidar_vrt(folder, fallback, fallback_units=None, unit="m"):
    """A 1 m lidar VRT as Screening's build_vrt writes it: band unit 'm' and <stem>.json (format
    damsuite/lidar_1m_vrt) naming the fallback; fallback_units only from 0.17.50."""
    tile = _dem(folder, "tile.tif", "metre", 290.0)
    vrt = os.path.join(folder, "D1_1m_lidar.vrt")
    ds = gdal.BuildVRT(vrt, [fallback, tile])
    if unit:
        ds.GetRasterBand(1).SetUnitType(unit)
    ds = None
    info = {"format": "damsuite/lidar_1m_vrt", "vrt": vrt, "units": "metres", "fallback": fallback}
    if fallback_units is not None:
        info["fallback_units"] = fallback_units
    with open(os.path.join(folder, "D1_1m_lidar.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    return vrt


@test
def dem_z_detect_stops_on_an_old_lidar_vrt_over_a_feet_fallback():
    if gdal is None:
        print("SKIP dem_z_old_lidar_vrt (no GDAL)")
        return
    from nd_breach_runner.core import gdal_io as G
    d = tempfile.mkdtemp(prefix="demz_oldvrt_")
    try:
        for name, unit, value, words in (("fb_ft.tif", "ft", 951.0, ("built by Screening before 0.17.50",
                                                                         "fb_ft.tif", "in feet")),
                                         ("fb_none.tif", "", 290.0, ("records no unit",))):
            sub = os.path.join(d, name[:-4])
            os.makedirs(sub)
            vrt = _lidar_vrt(sub, _dem(sub, name, unit, value))
            _stops(G, vrt, *words)
            # the same VRT from Screening 0.17.50 (fallback converted, json says so): trusted
            vrt = _lidar_vrt(sub, os.path.join(sub, name), "feet (raster unit 'ft'), converted")
            assert G.resolve_dem_z(vrt, "auto")[0] == G.Z_METERS
        # the same old VRT without its band unit (every lidar VRT has one), beside its USGS 3DEP
        # facts file: stopped - a facts file alone is not trusted (review round 2), with or
        # without the json
        sub = os.path.join(d, "facts")
        os.makedirs(sub)
        vrt = _lidar_vrt(sub, _dem(sub, "fb_ft.tif", "ft", 951.0), unit="")
        with open(os.path.join(sub, "D1_1m_lidar_dem.txt"), "w", encoding="utf-8") as fh:
            fh.write("source: USGS 3DEP 1 m lidar DEM tiles (native, The National Map)\n"
                     "crs: EPSG:26914\nunits: metres\nfile_type: VRT over the native GeoTIFF "
                     "tiles (and the fallback DEM underneath, if any)\ncell_m: 10\n")
        _stops(G, vrt, "D1_1m_lidar_dem.txt", "replaced or re-exported")
        os.remove(os.path.join(sub, "D1_1m_lidar.json"))
        _stops(G, vrt, "D1_1m_lidar_dem.txt", "replaced or re-exported")
        # a metre fallback (the Download DEM 10 m GeoTIFF of before 0.17.50 too): trusted
        sub = os.path.join(d, "m")
        os.makedirs(sub)
        assert G.resolve_dem_z(_lidar_vrt(sub, _old_download(sub, "D1_10m_5070.tif")),
                               "auto")[0] == G.Z_METERS
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def rundam_units_note_says_how_the_unit_was_detected():
    if gdal is None:
        print("SKIP rundam_units_note (no GDAL)")
        return
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    d = tempfile.mkdtemp(prefix="demz_note_")
    try:
        src = _gis(algs, _old_download(d, "DWR-2801_10m_5070.tif"), 0)[0]
        assert src.z_note.startswith("metres (detected: Download DEM record (in the file and "
                                     "DWR-2801_10m_5070_dem.txt): USGS 3DEP"), src.z_note
        assert "chosen" not in src.z_note and "check" not in src.z_note.lower(), src.z_note
        src = _gis(algs, _dem(d, "ft.tif", "ft", 1100.0), 0)[0]
        assert src.z_note == "feet (detected: raster unit 'ft')", src.z_note
        assert _gis(algs, _dem(d, "ft2.tif", "ft", 1100.0), 1)[0].z_note == "feet (chosen)"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def dem_zunit_is_the_same_file_in_runner_and_screening():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    other = os.path.join(os.path.dirname(os.path.realpath(here)), "DamSafety_Screening", "core",
                         "dem_zunit.py")
    if not os.path.isfile(other):
        print("SKIP dem_zunit parity (no DamSafety_Screening next to the Runner)")
        return
    with open(os.path.join(here, "core", "dem_zunit.py"), "rb") as a, open(other, "rb") as b:
        assert a.read() == b.read(), "core/dem_zunit.py differs between Runner and Screening"
