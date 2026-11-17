"""Prefer the configured offline pack DEM for screening terrain and larger clips.

The window / chain default used to be USGS 3DEP (terrain_source 0, larger_clips 1), so a
headless run downloaded fresh clips even when the suite data folder (QGIS setting
ndsuite/data_folder, NDSUITE_DATA / NDSUITE_PACK, ~/.ndsuite/config.json) already held a DEM.
This module reads those existing settings (never a hard-coded path), and only falls back to a
3DEP download when there is no pack DEM, when it does not cover the dam, or when the settings
chose 3DEP on purpose.

0.20.3 review fixes (2026-11-17):
  * the suite helpers are imported from the plugin's own damsuite copy first, so the data
    folder set in QGIS is found (the absolute import only works outside QGIS);
  * only <folder>/dem holds the DEM (the convention of damsuite.suitedata.local_dem); a raster at
    the top of the data folder (NLCD, water counts) is never taken for terrain;
  * local_dem is a folder in the WORK folder (terrain_pack/) holding one VRT that points at the
    pack DEM. Tool 2 mosaics every raster under local_dem recursively, so pointing it at
    <pack>/dem would also mosaic lidar_1m tiles or other rasters kept there;
  * settings that set terrain_source = 0 (USGS 3DEP) are kept as they are, and
    offline_pack_terrain = false turns this module off;
  * the notes are returned in the settings under _terrain_source_notes so the chain can log them.
It never writes into the pack or ND_Dam_Suite_Pack.
"""
from __future__ import annotations

import json
import os

RASTER_EXT = (".tif", ".tiff", ".vrt", ".img")
DEM_DIR = "dem"
WRAP_DIR = "terrain_pack"
WRAP_NAME = "pack_dem.vrt"
OPT_OUT_KEY = "offline_pack_terrain"       # settings key; false = never switch to the pack


def _suitedata():
    try:
        from ..damsuite import suitedata as SD        # the plugin's own copy (QGIS)
        return SD
    except Exception:                                 # noqa
        pass
    try:
        from damsuite import suitedata as SD          # a script with the plugin folder on sys.path
        return SD
    except Exception:                                 # noqa
        return None


def _existing_dir(raw):
    p = os.path.expanduser(str(raw or "").strip())
    return os.path.abspath(p) if p and os.path.isdir(p) else ""


def _config_values(*paths_and_keys):
    """Read keys from JSON config files (e.g. ~/.ndsuite/config.json)."""
    out = []
    for path, keys in paths_and_keys:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                data = json.loads(fh.read())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        for key in keys:
            v = _existing_dir(data.get(key))
            if v:
                out.append(v)
    return out


def configured_pack_folders():
    """Existing folders the suite already uses for the offline pack / suite data. No box paths."""
    found = []
    for env in ("NDSUITE_PACK", "NDSUITE_DATA"):
        v = _existing_dir(os.environ.get(env, ""))
        if v:
            found.append(v)
    SD = _suitedata()
    if SD is not None:
        for name in ("data_folder", "pack_location", "active_pack"):
            try:
                v = _existing_dir(getattr(SD, name)())
            except Exception:                         # noqa
                v = ""
            if v:
                found.append(v)
    found.extend(_config_values(
        (os.path.join("~", ".ndsuite", "config.json"),
         ("ndsuite/data_folder", "ndsuite/pack_path")),
    ))
    out, seen = [], set()
    for p in found:
        key = os.path.normcase(os.path.realpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def pack_dem(folder):
    """The DEM in <folder>/dem: a .vrt first (a mosaic), else the only raster, else ''.
    Same rule as damsuite.suitedata.local_dem; files directly in <folder> are never used."""
    folder = _existing_dir(folder)
    if not folder:
        return ""
    dem_dir = os.path.join(folder, DEM_DIR)
    try:
        names = sorted(os.listdir(dem_dir))
    except OSError:
        return ""
    files = [os.path.join(dem_dir, n) for n in names
             if not n.startswith(".") and os.path.splitext(n)[1].lower() in RASTER_EXT
             and os.path.isfile(os.path.join(dem_dir, n))]
    vrts = [p for p in files if p.lower().endswith(".vrt")]
    if vrts:
        return vrts[0]
    return files[0] if len(files) == 1 else ""


def find_pack_dem():
    """(dem path, folder it came from) for the first configured pack that has a DEM, else ('', '')."""
    for folder in configured_pack_folders():
        dem = pack_dem(folder)
        if dem:
            return dem, folder
    return "", ""


def raster_bounds(path):
    """(minx, miny, maxx, maxy, epsg_or_None) of a raster, or None when it cannot be read."""
    try:
        from osgeo import gdal, osr
    except ImportError:
        return None
    ds = gdal.Open(path)
    if ds is None:
        return None
    try:
        gt = ds.GetGeoTransform()
        w, h = ds.RasterXSize, ds.RasterYSize
        xs = (gt[0], gt[0] + w * gt[1] + h * gt[2])
        ys = (gt[3], gt[3] + w * gt[4] + h * gt[5])
        epsg = None
        wkt = ds.GetProjection()
        if wkt:
            srs = osr.SpatialReference()
            if srs.ImportFromWkt(wkt) == 0:
                try:
                    srs.AutoIdentifyEPSG()
                    code = srs.GetAuthorityCode(None)
                    epsg = int(code) if code else None
                except Exception:
                    epsg = None
        return (min(xs), min(ys), max(xs), max(ys), epsg)
    finally:
        ds = None


def _point_in_bounds(bounds, xy, xy_epsg=4326):
    if not bounds or not xy or len(xy) < 2:
        return True
    minx, miny, maxx, maxy, epsg = bounds
    x, y = float(xy[0]), float(xy[1])
    if epsg and xy_epsg and int(epsg) != int(xy_epsg):
        try:
            from osgeo import osr
            src, dst = osr.SpatialReference(), osr.SpatialReference()
            src.ImportFromEPSG(int(xy_epsg))
            dst.ImportFromEPSG(int(epsg))
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            t = osr.CoordinateTransformation(src, dst)
            x, y, _ = t.TransformPoint(x, y)
        except Exception:
            return True  # cannot compare CRSs: do not reject the pack
    pad_x = max(1.0, 0.01 * (maxx - minx))
    pad_y = max(1.0, 0.01 * (maxy - miny))
    return (minx - pad_x) <= x <= (maxx + pad_x) and (miny - pad_y) <= y <= (maxy + pad_y)


def pack_covers(dem, xy=None, xy_epsg=4326):
    """True when the pack DEM can be used. Unknown bounds (no GDAL) count as covering; missing file does not."""
    if not dem or not os.path.isfile(dem):
        return False
    if xy is None:
        return True
    bounds = raster_bounds(dem)
    if bounds is None:
        return True
    return _point_in_bounds(bounds, xy, xy_epsg)


def wrap_pack_dem(dem, work):
    """<work>/terrain_pack/pack_dem.vrt, one VRT that points at the pack DEM, so tool 2 and tool 7
    mosaic exactly that DEM. Returns the folder, or '' when it cannot be written (no GDAL)."""
    if not work:
        return ""
    folder = os.path.join(work, WRAP_DIR)
    vrt = os.path.join(folder, WRAP_NAME)
    src_txt = vrt + ".source"
    want = os.path.abspath(dem)
    try:
        with open(src_txt, encoding="utf-8") as fh:
            if fh.read().strip() == want and os.path.isfile(vrt):
                return folder
    except OSError:
        pass
    try:
        from osgeo import gdal
    except ImportError:
        return ""
    try:
        os.makedirs(folder, exist_ok=True)
        ds = gdal.BuildVRT(vrt, [want])
        if ds is None:
            return ""
        ds = None
        with open(src_txt, "w", encoding="utf-8") as fh:
            fh.write(want + "\n")
    except Exception:                                 # noqa
        return ""
    return folder


def _off(v):
    return v is False or str(v).strip().lower() in ("false", "no", "0", "off")


def apply_offline_pack_terrain(settings, xy=None, xy_epsg=4326, log=None, work=None, explicit=()):
    """Switch 3DEP screening defaults to the configured pack when it exists and covers `xy`.

    `explicit` holds the settings keys the user's settings file / JSON set. terrain_source = 0
    set there is a choice of USGS 3DEP and is kept; larger_clips set there is kept too.
    `work` is the work folder, where terrain_pack/pack_dem.vrt is written; without it the
    settings are left on 3DEP. Returns the same settings dict; the notes are in
    _terrain_source_notes and the source in _terrain_source_used.
    """
    S = settings if isinstance(settings, dict) else {}
    explicit = set(explicit or ())
    work = work or S.get("work") or ""
    notes = []

    def _log(msg):
        notes.append(msg)
        if log:
            log(msg)

    def _done(used):
        S["_terrain_source_used"] = used
        S["_terrain_source_notes"] = notes
        return S

    if _off(S.get(OPT_OUT_KEY, True)):
        _log("terrain: offline pack default is turned off ({} = false); the settings are used as they are"
             .format(OPT_OUT_KEY))
        return _done("settings:pack_default_off")

    local = (S.get("local_dem") or "").strip()
    if local:
        if int(S.get("larger_clips") or 0) == 1 and "larger_clips" not in explicit:
            S["larger_clips"] = 2
            _log("terrain: local DEM already set ({}); larger clips use it too, not a 3DEP download".format(local))
        else:
            _log("terrain: local DEM ({}); larger clips {}".format(local, S.get("larger_clips")))
        return _done("local_dem:{}".format(local))

    if "terrain_source" in explicit and int(S.get("terrain_source") or 0) == 0:
        _log("terrain: USGS 3DEP download (chosen in the settings: terrain_source = 0)")
        return _done("3dep:chosen")

    dem, folder = find_pack_dem()
    if not dem:
        _log("terrain: USGS 3DEP download (no DEM in <data folder>/dem of a configured offline pack; "
             "set the suite data folder, NDSUITE_DATA / NDSUITE_PACK or ~/.ndsuite)")
        return _done("3dep:no_pack")

    if not pack_covers(dem, xy, xy_epsg):
        _log("terrain: USGS 3DEP download (offline pack does not cover the dam): {}".format(dem))
        return _done("3dep:pack_does_not_cover:{}".format(dem))

    wrap = wrap_pack_dem(dem, work)
    if not wrap:
        _log("terrain: USGS 3DEP download (pack DEM {} found, but {} could not be written in the work "
             "folder)".format(dem, os.path.join(WRAP_DIR, WRAP_NAME)))
        return _done("3dep:no_wrapper:{}".format(dem))

    S["local_dem"] = wrap
    S["terrain_source"] = 1
    if "larger_clips" not in explicit:
        S["larger_clips"] = 2
    _log("terrain: offline pack DEM {} (from {}), through {}; larger clips {}".format(
        dem, folder, os.path.join(wrap, WRAP_NAME), S.get("larger_clips")))
    return _done("offline_pack:{}".format(dem))


def pop_notes(settings):
    """The notes apply_offline_pack_terrain left in `settings`, removed from it."""
    if not isinstance(settings, dict):
        return []
    settings.pop("_terrain_source_used", None)
    return list(settings.pop("_terrain_source_notes", None) or [])
