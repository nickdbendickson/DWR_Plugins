# -*- coding: utf-8 -*-
"""2.18.49: building-outline geometry sources and the FEMA USA Structures scene-offset check.

Why (DWR-8278 hand check, Docs & QA 2026-10-21): FEMA / ORNL USA Structures outlines are
traced per Maxar scene, and one scene (1030010045BF2F00, 2015-06-26) is placed about 14.7 m
south-west of the real buildings (NAIP 2023, NG9-1-1 centrelines). Microsoft's Global ML
Building Footprints were within 1.2 m, NSI 1.3 m, the 911 points 0.4 m and OpenStreetMap
5.3 m. With FEMA's outline a single-wide at 9250 56th Ave SW fell in a dry 40 m cell
next to a cell at 2.66 ft / 4.25 ft/s, and the dam read ND Low instead of High.

What this module gives structures.build_inventory:

* outline_kind()     - which data set an outline layer is (Microsoft, OpenStreetMap,
                       FEMA USA Structures or other), from its family, name and fields.
* scene_offsets()    - per FEMA source scene (IMAGE_NAME), the median vector from the
                       FEMA outline centroids to the reference buildings (Microsoft
                       footprints when loaded, else NSI points, which sit on Bing
                       footprint centroids). A scene whose outlines are consistently
                       off by more than SCENE_FLAG_M (spread at most SCENE_SPREAD_M,
                       at least SCENE_MIN_N pairs) is flagged.
* fetch_ms_footprints() - Microsoft Global ML Building Footprints (ODbL) for the study
                       area: the quadkey files of the public release, cached in the
                       run's outputs/_downloads folder like every other download. They
                       are NOT put in the offline data pack (Hazard's offline_pack stays
                       empty; the shared ND_Dam_Suite_Pack is never written).

Pure-python parts (quadkeys, scene_offsets) need no QGIS, so they are unit-tested alone.
"""

import gzip
import io
import json
import math
import os
import re
import time

KIND_MS = "Microsoft footprints"
KIND_OSM = "OpenStreetMap"
KIND_FEMA = "FEMA USA Structures"
KIND_OTHER = "other outlines"

MS_FAMILY = "MS_BUILDINGS"

# --- scene-offset check (metres; engineering screening values, not standards) -------------
SCENE_MIN_N = 5            # matched outline / reference pairs before a scene is judged
SCENE_FLAG_M = 5.0         # median offset above this: the scene is flagged
SCENE_SPREAD_M = 5.0       # ... when the robust spread of the vectors is at most this
SCENE_MATCH_M = 40.0       # a reference farther than this from an outline is not its building
SCENE_AREA_RATIO = 2.5     # reference / outline area within 1/x .. x when both are known

# --- Microsoft Global ML Building Footprints --------------------------------------------
MS_LINKS_URL = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
MS_ZOOM = 9                 # the release is split by zoom-9 quadkeys
MS_LINKS_MAX_AGE_DAYS = 30
MS_MAX_TOTAL_MB = 250.0     # more than this for one study area: not downloaded (logged)
MS_LOCATIONS = ("UnitedStates",)

_MS_NAME = re.compile(r"microsoft|msft|ms[_ ]?build|mlbuild|ml[_ ]build|global[_ -]?buildings|"
                      r"usbuildings|minedbuildings|northdakota\.geojson", re.I)
_OSM_NAME = re.compile(r"(?<![a-z])osm(?![a-z])|openstreetmap|gis_osm_buildings|osm_build", re.I)


def outline_kind(family, name="", fields=(), fema_parcel=False):
    """KIND_* of an outline layer. `name`: layer name and / or source path; `fields`:
    its field names; fema_parcel: the layer has FEMA USA Structures occupancy fields."""
    fam = (family or "").upper()
    low = {str(f).lower() for f in fields or ()}
    text = str(name or "")
    if fam in (MS_FAMILY, "MICROSOFT", "MS_FOOTPRINTS"):
        return KIND_MS
    if fema_parcel or {"build_id", "image_name"} <= low or "occ_cls" in low:
        return KIND_FEMA
    if _MS_NAME.search(text) or {"release", "capture_dates_range"} <= low:
        return KIND_MS
    if fam == "OSM" or _OSM_NAME.search(text) or "osm_id" in low or \
            ("fclass" in low and "building" not in low and "code" in low):
        return KIND_OSM
    return KIND_OTHER


def _median(v):
    s = sorted(v)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mad_sd(v, med):
    """Robust standard deviation (1.4826 x median absolute deviation)."""
    return 1.4826 * _median([abs(x - med) for x in v]) if v else None


def scene_offsets(outlines, refs, unit_m=1.0, min_n=SCENE_MIN_N, flag_m=SCENE_FLAG_M,
                  spread_m=SCENE_SPREAD_M, match_m=SCENE_MATCH_M, area_ratio=SCENE_AREA_RATIO,
                  ref_kind="NSI points"):
    """Per-scene offset of FEMA outlines against reference buildings.

    outlines: [(scene, cx, cy, area_m2 or None)] FEMA outline centroids (CRS units)
    refs:     [(x, y, area_m2 or None)] reference building centres (same CRS)
    unit_m:   metres per CRS unit
    Returns {scene: {"scene", "n", "outlines", "dx", "dy" (CRS units, outline -> reference),
    "dx_m", "dy_m", "shift_m", "spread_m", "flagged", "ref"}} for every scene with outlines.
    Each outline is paired with its nearest reference within match_m whose area (when
    both are known) is within 1/area_ratio .. area_ratio of the outline's."""
    unit_m = float(unit_m) if unit_m else 1.0
    reach = match_m / unit_m
    cell = max(reach, 1e-9)
    grid = {}
    for r in refs:
        grid.setdefault((int(math.floor(r[0] / cell)), int(math.floor(r[1] / cell))), []).append(r)
    by_scene = {}
    for scene, cx, cy, area in outlines:
        if not scene:
            continue
        b = by_scene.setdefault(scene, {"vec": [], "outlines": 0})
        b["outlines"] += 1
        ki, kj = int(math.floor(cx / cell)), int(math.floor(cy / cell))
        best = None
        for i in (ki - 1, ki, ki + 1):
            for j in (kj - 1, kj, kj + 1):
                for r in grid.get((i, j), ()):
                    d = math.hypot(r[0] - cx, r[1] - cy)
                    if d > reach:
                        continue
                    if area and r[2] and not (1.0 / area_ratio <= r[2] / area <= area_ratio):
                        continue
                    if best is None or d < best[0]:
                        best = (d, r[0] - cx, r[1] - cy)
        if best is not None:
            b["vec"].append(best[1:])
    out = {}
    for scene, b in by_scene.items():
        vec = b["vec"]
        n = len(vec)
        info = {"scene": scene, "n": n, "outlines": b["outlines"], "dx": 0.0, "dy": 0.0,
                "dx_m": 0.0, "dy_m": 0.0, "shift_m": 0.0, "spread_m": None,
                "flagged": False, "ref": ref_kind}
        if n:
            mdx = _median([v[0] for v in vec])
            mdy = _median([v[1] for v in vec])
            sdx = _mad_sd([v[0] for v in vec], mdx) * unit_m
            sdy = _mad_sd([v[1] for v in vec], mdy) * unit_m
            info.update({"dx": mdx, "dy": mdy, "dx_m": mdx * unit_m, "dy_m": mdy * unit_m,
                         "shift_m": math.hypot(mdx, mdy) * unit_m,
                         "spread_m": max(sdx, sdy)})
            info["flagged"] = (n >= min_n and info["shift_m"] > flag_m
                               and info["spread_m"] <= spread_m)
        out[scene] = info
    return out


def describe_scene(info):
    """One line for the log / QA / judgment item."""
    return ("FEMA USA Structures scene {scene}: {outlines} outline(s), {n} paired with {ref}; "
            "median offset {shift:.1f} m (dx {dx:+.1f} m, dy {dy:+.1f} m, spread {sp})".format(
                scene=info["scene"], outlines=info["outlines"], n=info["n"], ref=info["ref"],
                shift=info["shift_m"], dx=-info["dx_m"], dy=-info["dy_m"],
                sp="{:.1f} m".format(info["spread_m"]) if info["spread_m"] is not None
                else "-"))


# ---------------------------------------------------------------- quadkeys --
def _tile_xy(lon, lat, z):
    n = 2 ** z
    lat = max(-85.05112878, min(85.05112878, lat))
    x = int((lon + 180.0) / 360.0 * n)
    r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def quadkey(x, y, z):
    digits = []
    for i in range(z, 0, -1):
        d = 0
        mask = 1 << (i - 1)
        if x & mask:
            d += 1
        if y & mask:
            d += 2
        digits.append(str(d))
    return "".join(digits)


def quadkeys_for_extent(extent_4326, z=MS_ZOOM):
    """Zoom-z quadkeys covering (xmin, ymin, xmax, ymax) in degrees."""
    xmin, ymin, xmax, ymax = extent_4326
    x0, y1 = _tile_xy(xmin, ymin, z)
    x1, y0 = _tile_xy(xmax, ymax, z)
    return sorted({quadkey(x, y, z) for x in range(min(x0, x1), max(x0, x1) + 1)
                   for y in range(min(y0, y1), max(y0, y1) + 1)})


def parse_size_mb(text):
    m = re.match(r"\s*([\d.]+)\s*([KMG]?B)", str(text or ""), re.I)
    if not m:
        return 0.0
    v = float(m.group(1))
    return v * {"B": 1e-6, "KB": 1e-3, "MB": 1.0, "GB": 1e3}[m.group(2).upper()]


def links_for(csv_text, keys, locations=MS_LOCATIONS):
    """[(quadkey, url, size_mb)] of the dataset-links.csv rows for `keys`."""
    import csv
    want = set(keys)
    out = []
    for row in csv.DictReader(io.StringIO(csv_text)):
        if row.get("QuadKey") in want and (not locations or row.get("Location") in locations):
            out.append((row["QuadKey"], row["Url"], parse_size_mb(row.get("Size"))))
    return out


def features_in_extent(lines, extent_4326):
    """GeoJSON features (one JSON object per line, Microsoft's csv.gz) whose first ring's
    bounding box meets the extent."""
    xmin, ymin, xmax, ymax = extent_4326
    feats = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            f = json.loads(line)
            ring = f["geometry"]["coordinates"][0]
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
        except Exception:
            continue
        if max(xs) < xmin or min(xs) > xmax or max(ys) < ymin or min(ys) > ymax:
            continue
        props = f.get("properties") or {}
        f["properties"] = {"ms_source": "Microsoft Global ML Building Footprints (ODbL)",
                           "height": props.get("height"),
                           "confidence": props.get("confidence")}
        feats.append(f)
    return feats


def fetch_ms_footprints(extent_4326, output_folder, progress_cb=None):
    """Microsoft Global ML Building Footprints in the extent (EPSG:4326 GeoJSON in the
    run's outputs/_downloads folder). Returns (layer, count, path). Never touches the
    offline data pack."""
    from . import download as DL
    DL._check_extent(extent_4326)
    folder = DL.download_folder(output_folder)
    path = DL._cache_path(folder, "MS_building_footprints", extent_4326)
    lyr = DL._todays_file(path, "Microsoft building footprints")
    if lyr is not None:
        return lyr, lyr.featureCount(), path

    def live():
        links_path = os.path.join(folder, "MS_dataset_links.csv")
        fresh = os.path.exists(links_path) and \
            time.time() - os.path.getmtime(links_path) < MS_LINKS_MAX_AGE_DAYS * 86400
        if fresh:
            with open(links_path, encoding="utf-8") as fh:
                text = fh.read()
        else:
            try:
                text = DL.http_get_bytes(MS_LINKS_URL, DL.HTTP_USER_AGENT).decode("utf-8")
                with open(links_path + ".part", "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(links_path + ".part", links_path)
            except DL.CancelledByUser:
                raise
            except Exception:
                if not os.path.exists(links_path):
                    raise
                with open(links_path, encoding="utf-8") as fh:
                    text = fh.read()
        keys = quadkeys_for_extent(extent_4326)
        links = links_for(text, keys)
        total = sum(s for _k, _u, s in links)
        if total > MS_MAX_TOTAL_MB:
            raise DL.DownloadError("{:.0f} MB of footprint files for {} quadkey(s) - over the "
                                   "{:.0f} MB limit; load the footprints as a layer instead"
                                   .format(total, len(links), MS_MAX_TOTAL_MB))
        feats = []
        for i, (key, url, _size) in enumerate(links):
            if progress_cb:
                progress_cb(100.0 * i / max(1, len(links)),
                            "Microsoft footprints: quadkey {}".format(key))
            raw = DL.http_get_bytes(url, DL.HTTP_USER_AGENT)
            with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8") as fh:
                feats.extend(features_in_extent(fh, extent_4326))
        DL._write_fc(path, feats)
        DL.log_message("Microsoft building footprints: {} in the area from {} quadkey "
                       "file(s) ({})".format(len(feats), len(links), ", ".join(keys)))
        if not feats:
            from qgis.core import QgsVectorLayer
            return QgsVectorLayer("Polygon?crs=EPSG:4326", "Microsoft building footprints",
                                  "memory"), 0, path
        return DL._load(path, "Microsoft building footprints"), len(feats), path
    return DL._with_stale_fallback(path, "Microsoft building footprints", live)
