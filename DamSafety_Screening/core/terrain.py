"""Terrain acquisition: USGS 3DEP ImageServer or a local folder of ND LiDAR DEMs.

Everything is requested/warped to EPSG:5070 (CONUS Albers, equal-area) so
cell areas are true and volumes are not distorted (Web Mercator would inflate
areas ~1.5x at ND latitudes).
"""
import os
import time
import urllib.parse
import urllib.request

EXPORT_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
              "3DEPElevation/ImageServer/exportImage")
MAX_PX = 8000          # service maxImageWidth / maxImageHeight
TARGET_EPSG = 5070
SPLIT_MIN_PX = 1000    # a failed tile this large is asked for as four quarters


def _quarters(extent):
    """Four boxes that cover extent. Callers already accept a list of tiles."""
    xmin, ymin, xmax, ymax = extent
    mx = (xmin + xmax) / 2.0
    my = (ymin + ymax) / 2.0
    return (
        (xmin, ymin, mx, my),
        (mx, ymin, xmax, my),
        (xmin, my, mx, ymax),
        (mx, my, xmax, ymax),
    )


def tile_extents(xmin, ymin, xmax, ymax, cell, max_px=None):
    """Split an extent into tiles no larger than max_px on a side.

    The default is 1,200 pixels. A 10-mile box at 10 m is about 3,200 pixels
    and the 3DEP export timed out when that was requested as one tile.
    """
    from .solve_gate import TILE_PX
    max_px = TILE_PX if max_px is None else max_px
    step = max_px * cell
    x = xmin
    while x < xmax:
        y = ymin
        while y < ymax:
            yield (x, y, min(x + step, xmax), min(y + step, ymax))
            y += step
        x += step


def fetch_3dep_tile(extent, cell, out_path, retries=3, timeout=120):
    """Download one GeoTIFF tile (float32, metres) in EPSG:5070."""
    xmin, ymin, xmax, ymax = extent
    w = int(round((xmax - xmin) / cell))
    h = int(round((ymax - ymin) / cell))
    if w > MAX_PX or h > MAX_PX:
        raise ValueError("tile exceeds service limit")
    params = {
        "f": "image", "format": "tiff", "pixelType": "F32",
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": TARGET_EPSG, "imageSR": TARGET_EPSG,
        "size": f"{w},{h}", "interpolation": "RSP_BilinearInterpolation",
    }
    url = EXPORT_URL + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read()
            if data[:4] not in (b"II*\x00", b"MM\x00*"):
                raise IOError("response is not a TIFF: %r" % data[:120])
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise IOError(f"3DEP request failed: {last}")


def fetch_3dep(extent, cell, out_dir, stem, max_px=None, log=None, progress=None):
    """Fetch all tiles for an extent; returns list of tile paths."""
    os.makedirs(out_dir, exist_ok=True)
    tiles = list(tile_extents(*extent, cell, max_px))
    paths = []
    for i, ext in enumerate(tiles):
        if progress:
            progress(i, len(tiles))
        if log:
            log("3DEP tile {} of {} for {}".format(i + 1, len(tiles), stem))
        p = os.path.join(out_dir, f"{stem}_t{i}.tif")
        try:
            paths.append(fetch_3dep_tile(ext, cell, p))
        except IOError as err:
            w = int(round((ext[2] - ext[0]) / cell))
            h = int(round((ext[3] - ext[1]) / cell))
            if max(w, h) < SPLIT_MIN_PX:
                raise
            if log:
                log("3DEP tile {} failed ({}); requesting four quarters".format(i + 1, err))
            for q, qext in enumerate(_quarters(ext)):
                qp = os.path.join(out_dir, "{}_t{}q{}.tif".format(stem, i, q))
                paths.append(fetch_3dep_tile(qext, cell, qp))
    return paths


def list_local_tiles(folder, exts=(".tif", ".tiff", ".img", ".vrt")):
    out = []
    for root, _d, files in os.walk(folder):
        out += [os.path.join(root, f) for f in files
                if f.lower().endswith(exts)]
    return sorted(out)
