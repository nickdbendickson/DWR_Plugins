"""Prefer the configured offline pack DEM for screening terrain and larger clips.

The window / chain default used to be USGS 3DEP (terrain_source 0, larger_clips 1), so a
headless run downloaded fresh clips even when NDSUITE_DATA / NDSUITE_PACK / ~/.ndsuite already
named a pack that covers the dam. This module reads those existing settings (never a hard-coded
path), and only falls back to a 3DEP download when the pack is missing or does not cover the
area. It never writes into the pack or ND_Dam_Suite_Pack.
"""
from __future__ import annotations

import json
import os

RASTER_EXT = (".tif", ".tiff", ".vrt", ".img")
TILE_CACHES = ("lidar_1m",)


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
    found.extend(_config_values(
        (os.path.join("~", ".ndsuite", "config.json"),
         ("ndsuite/pack_path", "ndsuite/data_folder")),
    ))
    try:
        from damsuite import suitedata as SD
        for cand in (SD.pack_location(), SD.data_folder(), SD.active_pack()):
            v = _existing_dir(cand)
            if v:
                found.append(v)
    except Exception:
        pass
    out, seen = [], set()
    for p in found:
        key = os.path.normcase(os.path.realpath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def pack_dem(folder):
    """A DEM mosaic in <folder>/dem or <folder> itself; lidar_1m tiles are not one DEM."""
    folder = _existing_dir(folder)
    if not folder:
        return ""
    candidates = []
    dem_dir = os.path.join(folder, "dem")
    if os.path.isdir(dem_dir):
        candidates.append(dem_dir)
    candidates.append(folder)
    files = []
    for root in candidates:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if name.startswith(".") or name.lower() in TILE_CACHES:
                continue
            path = os.path.join(root, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in RASTER_EXT:
                files.append(path)
    if not files:
        return ""
    vrts = [p for p in files if p.lower().endswith(".vrt")]
    return sorted(vrts or files)[0]


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


def apply_offline_pack_terrain(settings, xy=None, xy_epsg=4326, log=None):
    """Switch 3DEP screening defaults to the configured pack when it exists and covers `xy`.

    If the settings already name a local_dem, larger_clips is set to 2 (local) so growing clips
    do not download 3DEP. Returns the (same) settings dict and a short source string for logs.
    """
    S = settings if isinstance(settings, dict) else {}
    notes = []

    def _log(msg):
        notes.append(msg)
        if log:
            log(msg)

    local = (S.get("local_dem") or "").strip()
    dem, folder = find_pack_dem()
    if local:
        if int(S.get("larger_clips") or 0) != 2:
            S["larger_clips"] = 2
            _log("terrain: local DEM already set ({}); larger clips use that folder, not a 3DEP download".format(local))
        else:
            _log("terrain: local DEM ({})".format(local))
        S["_terrain_source_used"] = "local_dem:{}".format(local)
        S["_terrain_source_notes"] = notes
        return S

    if not dem:
        _log("terrain: USGS 3DEP download (no configured offline pack DEM; set NDSUITE_DATA / NDSUITE_PACK or ~/.ndsuite)")
        S["_terrain_source_used"] = "3dep:no_pack"
        S["_terrain_source_notes"] = notes
        return S

    if not pack_covers(dem, xy, xy_epsg):
        _log("terrain: USGS 3DEP download (offline pack does not cover the area): {}".format(dem))
        S["_terrain_source_used"] = "3dep:pack_does_not_cover:{}".format(dem)
        S["_terrain_source_notes"] = notes
        return S

    S["local_dem"] = os.path.dirname(os.path.abspath(dem))
    S["terrain_source"] = 1
    S["larger_clips"] = 2
    _log("terrain: offline pack ({}); larger clips from {}".format(folder, dem))
    S["_terrain_source_used"] = "offline_pack:{}".format(dem)
    S["_terrain_source_notes"] = notes
    return S
