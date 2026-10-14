"""One 3DEP refill when wet cells sit on no-data. Between runs, not inside the solver.

The footprint of those cells, plus about one mile, is requested at the same cell
size and mosaicked onto the clip. A second hole is not requested again. Nothing
is requested outside the study cap, and dry no-data is left alone.
"""
import os
import urllib.parse
import urllib.request

EXPORT_URL = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
              "3DEPElevation/ImageServer/exportImage")
MILE = 1609.344


def hole_bounds(xs, ys, margin_m, cap):
    """Bounds of the wet no-data cells, plus margin, clipped to the study cap."""
    if not len(xs):
        return None
    box = (min(xs) - margin_m, min(ys) - margin_m, max(xs) + margin_m, max(ys) + margin_m)
    if cap is None:
        return box
    x0 = max(box[0], cap[0])
    y0 = max(box[1], cap[1])
    x1 = min(box[2], cap[2])
    y1 = min(box[3], cap[3])
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return (x0, y0, x1, y1)


def fetch_3dep(bounds, cell_m, out_path, timeout=120):
    """One EPSG:5070 GeoTIFF for bounds. Raises if the service fails."""
    xmin, ymin, xmax, ymax = bounds
    w = max(2, int(round((xmax - xmin) / cell_m)))
    h = max(2, int(round((ymax - ymin) / cell_m)))
    if w > 4000 or h > 4000:
        raise ValueError("refill box is too large for one 3DEP request")
    params = {
        "f": "image", "format": "tiff", "pixelType": "F32",
        "bbox": "{},{},{},{}".format(xmin, ymin, xmax, ymax),
        "bboxSR": 5070, "imageSR": 5070,
        "size": "{},{}".format(w, h),
        "interpolation": "RSP_BilinearInterpolation",
    }
    url = EXPORT_URL + "?" + urllib.parse.urlencode(params)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    urllib.request.urlretrieve(url, out_path)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        raise IOError("3DEP refill wrote no tile")
    return out_path


class PatchedTerrain:
    """Read the original terrain, then fill no-data from a refill grid."""

    def __init__(self, inner, patch):
        self.inner = inner
        self.patch = patch

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def read(self, bounds, cell):
        g = self.inner.read(bounds, cell)
        try:
            p = self.patch.read(bounds, cell)
        except Exception:
            return g
        data = g.data.copy()
        hole = ~g.valid()
        fill = p.valid() & hole
        if fill.any():
            data[fill] = p.data[fill]
            g = g.like(data)
        return g


def fill_grid(grid, tif_path):
    """Fill this grid's no-data from a 3DEP tile. Leaves real cells alone."""
    try:
        from osgeo import gdal
        import numpy as np
    except ImportError:
        return grid
    ds = gdal.Open(tif_path)
    if ds is None:
        return grid
    xmin, ymin, xmax, ymax = grid.bounds
    warped = gdal.Warp(
        "", ds, format="MEM", outputBounds=(xmin, ymin, xmax, ymax),
        width=grid.nx, height=grid.ny, resampleAlg="bilinear",
        dstNodata=-9999, outputType=gdal.GDT_Float32)
    if warped is None:
        return grid
    arr = warped.GetRasterBand(1).ReadAsArray()
    data = grid.data.copy()
    hole = ~grid.valid()
    good = np.isfinite(arr) & (arr > -1000)
    fill = hole & good
    if not fill.any():
        return grid
    data[fill] = arr[fill]
    return grid.like(data)
