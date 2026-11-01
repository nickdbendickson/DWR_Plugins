"""AOI layers: the ND offline pack both plugin sets share, or a download for this dam."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

THREEDEP = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
NHD = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"
# Large-scale flowline and waterbody. Public National Map layers, not the DWR inventory.
NHD_LAYERS = ((6, "streams.geojson"), (12, "waterbodies.geojson"))
RASTER_EXT = {".tif", ".tiff", ".vrt", ".img"}
CONFIG_KEY = "ndsuite/data_folder"


def find_suite_folder() -> str | None:
    """The suite data folder. ND Hazard and the DWR plugins write the same place."""
    env = (os.environ.get("NDSUITE_DATA") or "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_dir():
            return str(path.resolve())
    config = Path.home() / ".ndsuite" / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return None
    raw = str(data.get(CONFIG_KEY) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return str(path.resolve()) if path.is_dir() else None


def suite_dem(folder: str | None) -> str | None:
    """A DEM mosaic in <suite>/dem. The lidar_1m tile cache is not one DEM."""
    if not folder:
        return None
    dem_dir = Path(folder) / "dem"
    if not dem_dir.is_dir():
        return None
    found = [p for p in dem_dir.iterdir() if p.is_file() and p.suffix.lower() in RASTER_EXT]
    vrts = [p for p in found if p.suffix.lower() == ".vrt"]
    if vrts:
        return str(sorted(vrts)[0].resolve())
    if not found:
        return None
    return str(sorted(found)[0].resolve())


def pack_note(folder: str) -> str:
    names = ("ND_Dam_Suite_Pack", "nd_offline_pack", "nd_offline_pack.zip")
    for name in names:
        if (Path(folder) / name).exists():
            return f"Offline pack: {Path(folder) / name}"
    return "No ND_Dam_Suite_Pack or nd_offline_pack was sitting in that folder. The dem/, dwr/, and hifld/ folders are still used."


def write_settings(work: Path, suite: str | None, dem_file: str | None, only_ids: str | None = None) -> Path:
    """crest_settings.json for --SETTINGS. only_ids limits the DWR screening chain to that dam:
    DamSafety_Screening's nddam:runchain has no DAM_ID parameter and reads the filter from the
    settings' only_ids (the nd_ runchain takes DAM_ID as well)."""
    data: dict = {}
    if only_ids:
        data["only_ids"] = str(only_ids).strip()
    if suite:
        data["suite_folder"] = suite
    if dem_file:
        data["terrain_source"] = 1
        data["local_dem"] = str(Path(dem_file).resolve().parent)
    path = work / "crest_settings.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _nums(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_nums(item))
        return out
    return []


def aoi_bbox(dam: dict) -> tuple[float, float, float, float] | None:
    """(min lon, min lat, max lon, max lat), capped near 15 km so a download stays local."""
    geom = (dam or {}).get("geometry") if isinstance(dam, dict) else None
    aoi = (geom or {}).get("aoi") if isinstance(geom, dict) else None
    coords = _nums(((aoi or {}).get("geometry") or {}).get("coordinates"))
    lons = coords[0::2]
    lats = coords[1::2]
    if not lons:
        point = None
        for key in ("breachPoint", "reservoirPoint"):
            feature = (geom or {}).get(key) if isinstance(geom, dict) else None
            nums = _nums(((feature or {}).get("geometry") or {}).get("coordinates"))
            if len(nums) >= 2:
                point = nums[0], nums[1]
                break
        if point is None and isinstance(dam, dict):
            lon, lat = dam.get("lon"), dam.get("lat")
            if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
                point = float(lon), float(lat)
        if point is None:
            return None
        lons, lats = [point[0] - 0.04, point[0] + 0.04], [point[1] - 0.04, point[1] + 0.04]
    minx, maxx = min(lons), max(lons)
    miny, maxy = min(lats), max(lats)
    if maxx - minx < 0.01:
        mid = (minx + maxx) / 2
        minx, maxx = mid - 0.02, mid + 0.02
    if maxy - miny < 0.01:
        mid = (miny + maxy) / 2
        miny, maxy = mid - 0.02, mid + 0.02
    if maxx - minx > 0.2:
        mid = (minx + maxx) / 2
        minx, maxx = mid - 0.1, mid + 0.1
    if maxy - miny > 0.2:
        mid = (miny + maxy) / 2
        miny, maxy = mid - 0.1, mid + 0.1
    return minx, miny, maxx, maxy


def _save_url(url: str, dest: Path, timeout: float, fetch) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if fetch is not None:
        fetch(url, dest)
        return
    request = urllib.request.Request(url, headers={"User-Agent": "Crest-desktop"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(80_000_000 + 1)
    except urllib.error.URLError as exc:
        raise OSError(str(exc)) from exc
    if len(data) > 80_000_000:
        raise OSError("the download was larger than 80 MB and was not saved")
    dest.write_bytes(data)


def download_aoi(work: Path, dam: dict, log, fetch=None) -> dict:
    box = aoi_bbox(dam)
    if not box:
        log("No drawn area and no dam location, so nothing was downloaded.")
        return {"ok": False, "stopped": True}
    minx, miny, maxx, maxy = box
    log(f"Area: longitude {minx:.4f} to {maxx:.4f}, latitude {miny:.4f} to {maxy:.4f}.")
    width_m = max((maxx - minx) * 111320 * 0.7, 500)
    pixels = max(64, min(1500, int(width_m / 10)))
    dem_url = THREEDEP + "?" + urllib.parse.urlencode({
        "bbox": f"{minx},{miny},{maxx},{maxy}",
        "bboxSR": "4326",
        "imageSR": "5070",
        "size": f"{pixels},{pixels}",
        "format": "tiff",
        "pixelType": "F32",
        "f": "image",
    })
    dem = work / "dem.tif"
    try:
        _save_url(dem_url, dem, 180, fetch)
        log(f"DEM: {dem} (3DEP, about {pixels} cells on a side, EPSG:5070).")
    except OSError as exc:
        log(f"DEM was not downloaded: {exc}")
        dem = None
    out_dir = work / "aoi"
    for layer, name in NHD_LAYERS:
        query = urllib.parse.urlencode({
            "where": "1=1",
            "geometry": f"{minx},{miny},{maxx},{maxy}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true",
            "f": "geojson",
        })
        url = f"{NHD}/{layer}/query?{query}"
        dest = out_dir / name
        try:
            _save_url(url, dest, 120, fetch)
            log(f"Wrote {dest}")
        except OSError as exc:
            log(f"{name} was not downloaded: {exc}")
    log("DWR dams, dam lines, and structures are not on that public service. Use the ND offline pack for those.")
    if dem and dem.is_file():
        write_settings(work, find_suite_folder(), str(dem))
        return {"ok": True, "stopped": False, "dem": str(dem)}
    return {"ok": False, "stopped": True}


def use_offline_pack(work: Path, log) -> dict:
    folder = find_suite_folder()
    if not folder:
        log("No ND offline pack folder is set.")
        log("Both plugin sets use the same place: NDSUITE_DATA, or ndsuite/data_folder in ~/.ndsuite/config.json.")
        return {"ok": False, "stopped": True}
    log(f"ND offline pack folder (ND Hazard and the DWR plugins): {folder}")
    log(pack_note(folder))
    dem = suite_dem(folder)
    if dem:
        log(f"Pack DEM: {dem}")
    else:
        log("No DEM mosaic in the pack's dem folder. lidar_1m tiles are not used as the one DEM.")
    settings = write_settings(work, folder, dem)
    log(f"Wrote {settings} so a headless chain can see suite_folder.")
    return {"ok": True, "stopped": False, "dem": dem or "", "suite": folder}
