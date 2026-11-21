"""AOI layers: the ND offline pack both plugin sets share, or a download for this dam."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape, unescape

THREEDEP = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
NHD = "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"
# Large-scale flowline and waterbody. Public National Map layers, not the DWR inventory.
NHD_LAYERS = ((6, "streams.geojson"), (12, "waterbodies.geojson"))
RASTER_EXT = {".tif", ".tiff", ".vrt", ".img"}
CONFIG_KEY = "ndsuite/data_folder"


def default_qgis_profile_root() -> Path:
    """QGIS's own profiles folder when QGIS_CUSTOM_CONFIG_PATH is not set (QgsApplication's default per OS)."""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "QGIS" / "QGIS3"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "QGIS" / "QGIS3"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "QGIS" / "QGIS3"


def qgis_ini_value(ini: Path, key: str) -> str:
    """One value from a QGIS3.ini (QSettings INI file), e.g. "ndsuite/data_folder". "" when absent. Read only."""
    section, _, name = key.partition("/")
    try:
        lines = ini.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    current = None
    for line in lines:
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            continue
        if current != section or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != name:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        return v.replace("\\\\", "\\").strip()             # QSettings writes a backslash as two
    return ""


def qgis_profile_ini() -> Path:
    """QGIS3.ini of the profile Crest's qgis_process reads: the suite launcher's QGIS_CUSTOM_CONFIG_PATH (read from
    the launcher script, as Check QGIS does), else Crest's own QGIS_CUSTOM_CONFIG_PATH, else QGIS's default
    folder; always the "default" profile, which is the one qgis_process opens."""
    from crest.qgis_run import find_qgis_process, qgis_config_path   # here: qgis_run imports this module

    custom = None
    try:
        exe = find_qgis_process()
        custom = qgis_config_path(exe)[0] if exe else os.environ.get("QGIS_CUSTOM_CONFIG_PATH")
    except Exception:                                    # noqa: BLE001  (a lookup, never fatal)
        custom = os.environ.get("QGIS_CUSTOM_CONFIG_PATH")
    root = Path(custom).expanduser() if custom else default_qgis_profile_root()
    return root / "profiles" / "default" / "QGIS" / "QGIS3.ini"


def find_suite_folder_and_source() -> tuple[str | None, str]:
    """(suite data folder, where it came from). The suite's own order (DamSafety_Data settings.suite_folder):
      1. NDSUITE_DATA in Crest's environment;
      2. the QGIS setting ndsuite/data_folder that DamSafety Setup's "Start here" saves, in the profile Crest's
         qgis_process reads (2026-11-21);
      3. "ndsuite/data_folder" in ~/.ndsuite/config.json.
    A folder that does not exist is skipped. Read only."""
    env = (os.environ.get("NDSUITE_DATA") or "").strip()
    if env:
        path = Path(env).expanduser()
        if path.is_dir():
            return str(path.resolve()), "NDSUITE_DATA"
    try:
        ini = qgis_profile_ini()
        raw = qgis_ini_value(ini, CONFIG_KEY)
    except Exception:                                    # noqa: BLE001
        ini, raw = None, ""
    if raw:
        path = Path(raw).expanduser()
        if path.is_dir():
            return str(path.resolve()), f"the QGIS setting {CONFIG_KEY} in {ini}"
    config = Path.home() / ".ndsuite" / "config.json"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if isinstance(data, dict):
        raw = str(data.get(CONFIG_KEY) or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.is_dir():
                return str(path.resolve()), str(config)
    return None, ""


def find_suite_folder() -> str | None:
    """The suite data folder. ND Hazard and the DWR plugins write the same place."""
    return find_suite_folder_and_source()[0]


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


#: <work>/screen_dem: the chosen DEM alone (a link to it, or a copy of a VRT with its tiles made absolute).
STAGED_DEM_DIR = "screen_dem"
_DEM_SIDECARS = (".aux.xml", ".ovr", ".msk")
_WORLD_FILES = (".tfw", ".tifw", ".wld", ".prj")


def _place(src: Path, dst: Path) -> None:
    """dst = src: a symlink, else a hard link, else a copy (Windows without the symlink right, or another drive)."""
    for make in (lambda: os.symlink(src, dst), lambda: os.link(src, dst)):
        try:
            make()
            return
        except (OSError, NotImplementedError, AttributeError):
            continue
    shutil.copy2(src, dst)


def _vrt_with_absolute_sources(vrt: Path) -> str | None:
    """The VRT's text with every relativeToVRT="1" source made absolute (so it still finds its tiles from another
    folder), or None when it has no relative source (a plain link then works)."""
    try:
        text = vrt.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    pattern = re.compile(r'(<SourceFilename[^>]*?)relativeToVRT="1"([^>]*>)([^<]*)(</SourceFilename>)')
    if not pattern.search(text):
        return None

    def absolute(m: re.Match) -> str:
        src = (vrt.resolve().parent / unescape(m.group(3).strip())).resolve()
        return f'{m.group(1)}relativeToVRT="0"{m.group(2)}{escape(str(src))}{m.group(4)}'

    return pattern.sub(absolute, text)


def stage_dem_folder(work: Path, dem_file: str) -> Path:
    """A folder that holds the chosen DEM and nothing else, for the screening's local DEM FOLDER (local_dem with
    terrain_source 1): DamSafety_Screening's tool 2 mosaics every raster under that folder, so the DEM's own folder
    would also bring in the model results (hmax, vmax, arrival) or a second DEM sitting next to it.

    <work>/screen_dem is emptied of what an earlier run put there and gets the DEM (a link; a VRT with relative tile
    paths is written out with absolute ones) and its own sidecars (.aux.xml, .ovr, .msk, world file). The DEM's own
    folder is only read. Same staging as DamIT Crest (damit #163)."""
    dem = Path(dem_file).resolve()
    folder = Path(work) / STAGED_DEM_DIR
    folder.mkdir(parents=True, exist_ok=True)
    if dem.parent == folder.resolve():
        return folder                       # the DEM chosen IS the staged one: keep it as it is
    for old in folder.iterdir():
        if old.is_symlink() or old.is_file():
            old.unlink()
    rewritten = _vrt_with_absolute_sources(dem) if dem.suffix.lower() == ".vrt" else None
    if rewritten is not None:
        (folder / dem.name).write_text(rewritten, encoding="utf-8")
    else:
        _place(dem, folder / dem.name)
    sides = [dem.with_name(dem.name + ext) for ext in _DEM_SIDECARS] + [dem.with_suffix(ext) for ext in _WORLD_FILES]
    for side in sides:
        if side.is_file() and not (folder / side.name).exists():
            _place(side, folder / side.name)
    return folder


def write_settings(work: Path, suite: str | None, dem_file: str | None, only_ids: str | None = None,
                   stage: bool = True) -> Path:
    """crest_settings.json for --SETTINGS. only_ids limits the DWR screening chain to that dam:
    DamSafety_Screening's nddam:runchain has no DAM_ID parameter and reads the filter from the
    settings' only_ids (the nd_ runchain takes DAM_ID as well).

    stage: the local DEM folder is <work>/screen_dem holding dem_file alone (stage_dem_folder). False only for the
    offline pack's own <suite>/dem folder, which by the suite's convention holds the DEM and nothing else."""
    data: dict = {}
    if only_ids:
        data["only_ids"] = str(only_ids).strip()
    if suite:
        data["suite_folder"] = suite
    if dem_file:
        data["terrain_source"] = 1
        data["local_dem"] = str(stage_dem_folder(work, dem_file) if stage else Path(dem_file).resolve().parent)
        # Growing clips must use the same local / pack DEM; the screening default (1) is a fresh 3DEP download.
        data["larger_clips"] = 2
    path = work / "crest_settings.json"
    # Atomic write: a crash mid-write must not leave a truncated settings file for the next run to read.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def _nums(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Python's json accepts NaN/Infinity literals, so a malformed request can carry non-finite
        # coordinates. Drop them here: they would otherwise reach int(width_m / 10) and crash the job.
        return [float(value)] if math.isfinite(value) else []
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
    folder, source = find_suite_folder_and_source()
    if not folder:
        log("No ND offline pack folder is set.")
        log("Crest looks, in order, at: NDSUITE_DATA; the QGIS setting ndsuite/data_folder that DamSafety Setup's "
            f"\"Start here\" saves (read from {qgis_profile_ini()}); ndsuite/data_folder in ~/.ndsuite/config.json. "
            "Point it at the folder that holds ND_Dam_Suite_Pack (that exact name), not at the pack itself.")
        return {"ok": False, "stopped": True}
    log(f"ND offline pack folder (ND Hazard and the DWR plugins): {folder} (from {source})")
    log(pack_note(folder))
    dem = suite_dem(folder)
    if dem:
        log(f"Pack DEM: {dem}")
    else:
        log("No DEM mosaic in the pack's dem folder. lidar_1m tiles are not used as the one DEM.")
    settings = write_settings(work, folder, dem, stage=False)
    log(f"Wrote {settings} so a headless chain can see suite_folder.")
    return {"ok": True, "stopped": False, "dem": dem or "", "suite": folder}
