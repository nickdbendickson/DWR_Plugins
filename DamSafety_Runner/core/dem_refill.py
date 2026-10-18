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
TARGET_EPSG = 5070                                   # fall-back when no grid CRS is given
USER_AGENT = "ND-Dam-Suite (QGIS plugins; dam safety)"


def _sr(crs):
    """The ArcGIS spatial-reference WKID (an int) for `crs`, which may be an int EPSG code, an
    'EPSG:<n>' string, or a CRS WKT. The refill is requested in the grid's own CRS, never silently
    in another, so an unresolved CRS raises instead of defaulting to a wrong SR."""
    if crs is None:
        return TARGET_EPSG
    if isinstance(crs, int):
        return crs
    s = str(crs).strip()
    if s.upper().startswith("EPSG:"):
        s = s.split(":", 1)[1].strip()
    if s.isdigit():
        return int(s)
    try:                                             # a WKT / authority string -> its EPSG code
        from . import gdal_io
        code = gdal_io.epsg_of(str(crs))
    except Exception:                                # noqa: BLE001 - no GDAL, no lookup
        code = None
    if code and str(code).upper().startswith("EPSG:"):
        return int(str(code).split(":", 1)[1])
    raise ValueError("3DEP refill needs the grid's EPSG code; got CRS {!r}".format(crs))


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


def fetch_3dep(bounds, cell_m, out_path, crs=TARGET_EPSG, timeout=120):
    """One GeoTIFF for `bounds`, requested AND returned in the model grid's working CRS (`crs`,
    an EPSG code, an 'EPSG:<n>' string, or a CRS WKT; defaults to EPSG:5070 when none is given).

    `bounds` is in that same CRS, so bboxSR and imageSR both carry it and the tile lines up with
    the grid in `fill_grid`. Hard-coding EPSG:5070 here made an off-5070 run request the wrong
    ground, so the refill silently did nothing.

    The download goes through damsuite.netproxy, so the proxy / SSL set in QGIS (Settings >
    Options > Network) is honoured; a raw urllib download bypasses it and fails behind a proxy.
    Raises if the service fails."""
    sr = _sr(crs)
    xmin, ymin, xmax, ymax = bounds
    w = max(2, int(round((xmax - xmin) / cell_m)))
    h = max(2, int(round((ymax - ymin) / cell_m)))
    if w > 4000 or h > 4000:
        raise ValueError("refill box is too large for one 3DEP request")
    params = {
        "f": "image", "format": "tiff", "pixelType": "F32",
        "bbox": "{},{},{},{}".format(xmin, ymin, xmax, ymax),
        "bboxSR": sr, "imageSR": sr,
        "size": "{},{}".format(w, h),
        "interpolation": "RSP_BilinearInterpolation",
    }
    url = EXPORT_URL + "?" + urllib.parse.urlencode(params)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    from ..damsuite import netproxy
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with netproxy.urlopen(req, timeout=timeout) as r:
        data = r.read()
    if data[:4] not in (b"II*\x00", b"MM\x00*"):     # an ArcGIS JSON error comes back HTTP 200
        raise IOError("3DEP refill did not return a GeoTIFF")
    with open(out_path, "wb") as f:
        f.write(data)
    if os.path.getsize(out_path) < 1000:
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
    kw = dict(format="MEM", outputBounds=(xmin, ymin, xmax, ymax),
              width=grid.nx, height=grid.ny, resampleAlg="bilinear",
              dstNodata=-9999, outputType=gdal.GDT_Float32)
    # Reproject the tile into the grid's CRS so outputBounds (grid coordinates) line up even if
    # the tile is in another CRS; needs the grid's CRS and a tile that carries its own projection.
    crs_wkt = getattr(grid, "crs_wkt", None)
    if crs_wkt and ds.GetProjection():
        kw["dstSRS"] = crs_wkt
    warped = gdal.Warp("", ds, **kw)
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
