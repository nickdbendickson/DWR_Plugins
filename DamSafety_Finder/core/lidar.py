"""Open water straight from a classified LiDAR point cloud (Finder 0.4): the LAS/LAZ water class.

The aerial-imagery water tool (core/water.py) guesses water from colour: a positive NDWI that is
dark in the near-infrared. A cast shadow, a wet road or a flooded field can fool it, and a dry
flight year misses a pond. A classified point cloud has already made the call. USGS 3DEP delivers
classified LAS/LAZ, and ASPRS class 9 is **Water** (class 2 is ground). A cell is water when it
holds at least `min_pts` points of the water class - no sun angle, no shadows, no imagery year.

Two ways to read the points:

* **laspy** (if installed): reads LAS **and LAZ** and exposes the tile's CRS. The usual path on a
  machine with the dam suite installed (laspy with its lazrs/laszip backend is the LAZ reader).
* a small **pure-numpy reader** here (`read_points`): uncompressed LAS only (point formats 0-10,
  versions 1.0-1.4). No dependency, so the class filter and the rasteriser are unit-testable
  without laspy or GDAL. It cannot decode LAZ and raises `LasCompressedError` for one; a LAZ with
  no laspy installed is reported with a clear message to install laspy.

The reader never launches an outside process (the DamIT suite forbids it), so LAZ support rests on
laspy rather than a PDAL command line.

Points carry no colour, so the output grid is 1.0 on a water cell and 0.0 on dry land (NaN only
where the tile has no points at all); feed it to core/water.water_patches with threshold 0.5, the
same opening, min-size and min-width filters as the imagery tool. The LAS tile's own CRS places
the points; USGS tiles usually carry it in a WKT VLR this reader understands, otherwise pass
`src_epsg` (the UTM zone of the delivery).
"""
import struct

import numpy as np

try:  # GDAL/OSR ships with the QGIS bundles; only the reprojection to the work grid needs it
    from osgeo import osr
except Exception:  # pragma: no cover
    osr = None


LAS_SIGNATURE = b"LASF"
WATER_CLASS = 9           # ASPRS: 9 = Water, 2 = Ground, 1 = Unclassified


class LasError(RuntimeError):
    """A LAS file that could not be read. A RuntimeError so the tools turn it into a clean
    QgsProcessingException (the plugin converts RuntimeError) instead of leaking a traceback."""


class LasCompressedError(LasError):
    """A LAZ (compressed) file: the pure-numpy reader cannot decode it - use laspy or PDAL."""


# ---------------------------------------------------------------------------- header / points

# byte offset of the classification field inside one point record, per point-data-record format.
# Formats 0-5: a 1-byte bit-field (return/scan) at 14, then classification at 15 (low 5 bits are
# the class, high 3 are flags). Formats 6-10: two bit-field bytes at 14-15, flags at 16, then a
# full-byte classification at 17 (ASPRS extended 0-255, no masking).
_CLASS_OFFSET = {0: 15, 1: 15, 2: 15, 3: 15, 4: 15, 5: 15,
                 6: 17, 7: 17, 8: 17, 9: 17, 10: 17}


def _read_header(buf):
    """Parse the LAS public header block. Returns a dict; raises for a non-LAS or LAZ buffer."""
    if buf[:4] != LAS_SIGNATURE:
        raise LasError("not a LAS file (no LASF signature)")
    ver_major, ver_minor = buf[24], buf[25]
    header_size = struct.unpack_from("<H", buf, 94)[0]
    point_offset = struct.unpack_from("<I", buf, 96)[0]
    n_vlr = struct.unpack_from("<I", buf, 100)[0]
    fmt = buf[104]
    rec_len = struct.unpack_from("<H", buf, 105)[0]
    if fmt & 0x80:                       # the LASzip spec sets bit 7 of the format for LAZ
        raise LasCompressedError("this is a LAZ (compressed) file; use laspy or PDAL")
    fmt &= 0x3F
    if fmt not in _CLASS_OFFSET:
        raise LasError("unsupported point-data-record format {}".format(fmt))
    n_legacy = struct.unpack_from("<I", buf, 107)[0]
    scale = struct.unpack_from("<3d", buf, 131)
    offset = struct.unpack_from("<3d", buf, 155)
    n_points = n_legacy
    if ver_major == 1 and ver_minor >= 4:
        n14 = struct.unpack_from("<Q", buf, 247)[0]
        if n14:
            n_points = n14
    return {"version": (ver_major, ver_minor), "header_size": header_size,
            "point_offset": point_offset, "n_vlr": n_vlr, "format": fmt, "rec_len": rec_len,
            "n_points": n_points, "scale": scale, "offset": offset,
            "class_offset": _CLASS_OFFSET[fmt]}


def _read_wkt_vlr(buf, hdr):
    """The OGC WKT of the coordinate system from a LASF_Projection WKT VLR (record id 2112), or
    None. VLR header: 2 reserved + 16 user id + 2 record id + 2 payload length + 32 description."""
    pos = hdr["header_size"]
    for _ in range(hdr["n_vlr"]):
        if pos + 54 > len(buf):
            break
        user_id = buf[pos + 2:pos + 18].split(b"\x00", 1)[0]
        record_id = struct.unpack_from("<H", buf, pos + 18)[0]
        payload_len = struct.unpack_from("<H", buf, pos + 20)[0]
        body = buf[pos + 54:pos + 54 + payload_len]
        if user_id == b"LASF_Projection" and record_id == 2112:
            return body.split(b"\x00", 1)[0].decode("ascii", "replace") or None
        pos += 54 + payload_len
    return None


def read_points(path, classes=None):
    """x, y (float arrays, LAS units) and classification (uint8) of an uncompressed LAS file, plus
    the header dict (with a 'wkt' key: the CRS WKT if the file carries one). `classes` (a set of
    ASPRS class numbers) keeps only those points, read before scaling for speed. Raises
    `LasCompressedError` for a LAZ file and `LasError` for anything not a readable LAS."""
    with open(path, "rb") as fh:
        buf = fh.read()
    hdr = _read_header(buf)
    hdr["wkt"] = _read_wkt_vlr(buf, hdr)
    n, rec = hdr["n_points"], hdr["rec_len"]
    start = hdr["point_offset"]
    end = start + n * rec
    if end > len(buf):                   # a truncated file: read whole records only
        n = max(0, (len(buf) - start) // rec)
        end = start + n * rec
    co = hdr["class_offset"]
    dt = np.dtype({"names": ["X", "Y", "C"], "formats": ["<i4", "<i4", "u1"],
                   "offsets": [0, 4, co], "itemsize": rec})
    pts = np.frombuffer(buf[start:end], dtype=dt, count=n)
    cls = pts["C"].astype(np.uint8)
    if hdr["format"] < 6:
        cls = cls & 0x1F                 # low 5 bits are the class in formats 0-5
    xi, yi = pts["X"], pts["Y"]
    if classes is not None:
        keep = np.isin(cls, np.asarray(list(classes), dtype=np.uint8))
        xi, yi, cls = xi[keep], yi[keep], cls[keep]
    sx, sy = hdr["scale"][0], hdr["scale"][1]
    ox, oy = hdr["offset"][0], hdr["offset"][1]
    x = xi.astype(np.float64) * sx + ox
    y = yi.astype(np.float64) * sy + oy
    return x, y, cls, hdr


# ---------------------------------------------------------------------------- rasterise

def _grid_for(box, cell):
    """(n_x, n_y, geotransform) of a top-left-origin grid over box=(x0,y0,x1,y1) at `cell` metres,
    laid out exactly as finder.read_bands lays out an imagery tile so the rest of run_water (the
    polygonise + mapped machinery) sees the same grid it always does."""
    import math
    n_x = max(1, int(math.ceil((box[2] - box[0]) / cell)))
    n_y = max(1, int(math.ceil((box[3] - box[1]) / cell)))
    gt = (box[0], cell, 0.0, box[1] + n_y * cell, 0.0, -cell)
    return n_x, n_y, gt


def point_grid(x, y, cls, classes, gt, shape, min_pts=1):
    """A water grid from classified points already in the grid's CRS: 1.0 where a cell holds at
    least `min_pts` points whose class is in `classes`, 0.0 on dry land, NaN where the cell holds
    no points at all (a gap in the tile). `gt` is a top-left-origin geotransform, `shape` is
    (rows, cols). Pure numpy - the testable core."""
    ny, nx = int(shape[0]), int(shape[1])
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    cls = np.asarray(cls)
    col = np.floor((x - gt[0]) / gt[1]).astype(np.int64)
    row = np.floor((y - gt[3]) / gt[5]).astype(np.int64)      # gt[5] < 0
    inside = (col >= 0) & (col < nx) & (row >= 0) & (row < ny)
    flat = row[inside] * nx + col[inside]
    total = np.zeros(ny * nx, np.int64)
    np.add.at(total, flat, 1)
    want = np.isin(cls[inside], np.asarray(list(classes), dtype=cls.dtype if cls.size else np.uint8)) \
        if cls.size else np.zeros(0, bool)
    wet = np.zeros(ny * nx, np.int64)
    if want.size:
        np.add.at(wet, flat[want], 1)
    out = np.where(wet >= max(1, int(min_pts)), 1.0, 0.0)
    out[total == 0] = np.nan
    return out.reshape(ny, nx)


def _reproject(x, y, src_wkt, src_epsg, dst_epsg):
    """Transform x, y into dst_epsg. Source CRS from `src_wkt` (a WKT string) or `src_epsg`; when
    neither is given the points are assumed already in dst_epsg. Needs GDAL/OSR."""
    if src_wkt is None and not src_epsg:
        return x, y
    if osr is None:
        raise LasError("GDAL/OSR is needed to reproject the LAS tile to the work grid")
    s = osr.SpatialReference()
    if src_wkt:
        s.ImportFromWkt(src_wkt)
    else:
        s.ImportFromEPSG(int(src_epsg))
    d = osr.SpatialReference()
    d.ImportFromEPSG(int(dst_epsg))
    for sr in (s, d):
        if hasattr(sr, "SetAxisMappingStrategy"):
            sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s, d)
    pts = ct.TransformPoints(np.column_stack([x, y]).tolist())
    arr = np.asarray(pts, float)
    return arr[:, 0], arr[:, 1]


def _points_via_laspy(path, classes):
    """x, y, classification, crs-wkt using laspy (reads LAZ too), or None if laspy is absent."""
    try:
        import laspy
    except Exception:
        return None
    las = laspy.read(path)
    cls = np.asarray(las.classification, np.uint8)
    x = np.asarray(las.x, float)
    y = np.asarray(las.y, float)
    if classes is not None:
        keep = np.isin(cls, np.asarray(list(classes), np.uint8))
        x, y, cls = x[keep], y[keep], cls[keep]
    wkt = None
    try:
        crs = las.header.parse_crs()
        wkt = crs.to_wkt() if crs is not None else None
    except Exception:
        wkt = None
    return x, y, cls, wkt


def water_grid(path, box, cell, work_epsg=5070, classes=(WATER_CLASS,), min_pts=1,
               src_epsg=None, log=print):
    """(grid, geotransform) of water over `box` (in work_epsg) at `cell` metres from a LAS/LAZ
    tile: 1.0 water, 0.0 dry, NaN no-data. Reads with laspy (LAS and LAZ), else the pure-numpy
    reader (uncompressed LAS only). `src_epsg` names the tile's CRS when the file carries none."""
    nx, ny, gt = _grid_for(box, cell)
    low = str(path).lower()
    got = _points_via_laspy(path, set(classes))
    if got is not None:
        x, y, cls, wkt = got
        x, y = _reproject(x, y, wkt, src_epsg, work_epsg)
        return point_grid(x, y, cls, classes, gt, (ny, nx), min_pts), gt
    if low.endswith(".laz"):
        raise LasError("{} is a LAZ (compressed) file; install laspy to read it".format(path))
    x, y, cls, hdr = read_points(path, set(classes))
    x, y = _reproject(x, y, hdr.get("wkt"), src_epsg, work_epsg)
    return point_grid(x, y, cls, classes, gt, (ny, nx), min_pts), gt


def _header_bounds(path):
    """(minx, miny, maxx, maxy, wkt) in the file's own CRS from the LAS header, without reading
    the points. None for a LAZ the pure reader can't open (its header still parses, so this works
    for LAZ too: the min/max are plain header fields)."""
    with open(path, "rb") as fh:
        buf = fh.read(400)
    if buf[:4] != LAS_SIGNATURE:
        raise LasError("not a LAS file (no LASF signature)")
    maxx, minx = struct.unpack_from("<2d", buf, 179)
    maxy, miny = struct.unpack_from("<2d", buf, 195)
    with open(path, "rb") as fh:
        full = fh.read()
    hdr = {"header_size": struct.unpack_from("<H", full, 94)[0],
           "n_vlr": struct.unpack_from("<I", full, 100)[0]}
    wkt = _read_wkt_vlr(full, hdr)
    return minx, miny, maxx, maxy, wkt


def las_bounds(path, work_epsg=5070, src_epsg=None):
    """The tile's extent (x0, y0, x1, y1) in work_epsg, from the header min/max corners. Lets a
    run skip a LAS tile that doesn't overlap the area being scanned without reading its points."""
    minx, miny, maxx, maxy, wkt = _header_bounds(path)
    xs = [minx, maxx, minx, maxx]
    ys = [miny, miny, maxy, maxy]
    x, y = _reproject(np.asarray(xs, float), np.asarray(ys, float), wkt, src_epsg, work_epsg)
    return float(np.min(x)), float(np.min(y)), float(np.max(x)), float(np.max(y))


def is_las(src):
    """Whether a source string is a LAS/LAZ point-cloud file (by extension)."""
    return str(src or "").lower().endswith((".las", ".laz"))
