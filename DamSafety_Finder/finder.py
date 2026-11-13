"""Finder runs on files: GDAL / OGR + numpy, no QGIS (Finder 0.1).

The Processing tools (plugin.py) only gather their parameters and call these three functions, so
the same runs work from a script or qgis_process and are tested without QGIS:

    run_embankments(dem, streams, out, ...)   fills across streams in the DEM (core/embankments.py)
    run_ponds(waterbodies, dem, out, ...)     embankment ponds and dugouts (core/ponds.py)
    run_crossings(roads, streams, out, ...)   road / rail x stream crossings (core/crossings.py)
    run_water(image, out, ...)                open water in aerial imagery (core/water.py): the
                                              pond outlines for run_ponds no hydrography layer has

Each writes a GeoPackage (EPSG:5070 points, plus lon / lat fields) and a CSV beside it, and
checks every candidate against the known-structure layers given (core/listed.py): status
listed / unsure / new, with the nearest known id, its layer and the distance.
A layer argument is a path, optionally "path|layername=name". Distances and heights are written
in feet, storage in acre-feet, areas in acres (the US practice); the engine works in metres.

GDAL's exception mode is never switched (it is process-wide in QGIS): every call is checked.
"""
import csv
import json
import math
import os
import re
import time
import zlib

import numpy as np
from osgeo import gdal, ogr, osr
from scipy import ndimage as ndi

from .core import crossings as cx
from .core import evidence as ev
from .core import embankments as em
from .core import lidar as li
from .core import listed as ls
from .core import ponds as pd
from .core import ridges as rd
from .core import terrain as tr
from .core import warp_opts as wo
from .core import water as wt

WORK_EPSG = 5070                     # CONUS Albers equal area: metres, areas true
FT, AF_M3, AC_M2 = 0.3048, 1233.4818375, 4046.8564224
VERSION = "0.6.0"


# ------------------------------------------------------------------------------------- GDAL --
def _checked(res, what):
    if res is None:
        msg = (gdal.GetLastErrorMsg() or "").strip()
        raise RuntimeError("{} failed{}".format(what, ": " + msg if msg else ""))
    return res


def _srs(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(int(epsg))
    if hasattr(s, "SetAxisMappingStrategy"):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def _split(src):
    path, _, rest = str(src).partition("|")
    layer = None
    for part in rest.split("|"):
        if part.startswith("layername="):
            layer = part[len("layername="):]
    return path, layer


def open_layer(src):
    """(datasource, layer) of 'path' or 'path|layername=name' (the first layer when no name)."""
    path, name = _split(src)
    try:
        ds = ogr.Open(path, 0)
    except RuntimeError as e:
        raise RuntimeError("OGR cannot open {}: {}".format(path, e)) from None
    _checked(ds, "opening {}".format(path))
    lyr = ds.GetLayerByName(name) if name else ds.GetLayer(0)
    _checked(lyr, "layer {} of {}".format(name or 0, path))
    return ds, lyr


def _transform(lyr, dst):
    src = lyr.GetSpatialRef()
    if src is None:
        raise RuntimeError("layer {} has no CRS".format(lyr.GetName()))
    src = src.Clone()
    if hasattr(src, "SetAxisMappingStrategy"):
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(src, dst)


def _parts(geom):
    """Flat list of single geometries of a (multi / collection) geometry."""
    n = geom.GetGeometryCount()
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t in (ogr.wkbMultiLineString, ogr.wkbMultiPolygon, ogr.wkbMultiPoint, ogr.wkbGeometryCollection):
        out = []
        for i in range(n):
            out.extend(_parts(geom.GetGeometryRef(i)))
        return out
    return [geom]


def read_lines(src, bbox=None):
    """[[(x, y), ...]] in EPSG:5070 of every line part of a layer; bbox (xmin, ymin, xmax, ymax in
    EPSG:5070) keeps the lines whose box meets it."""
    if not src:
        return []
    ds, lyr = open_layer(src)
    ct = _transform(lyr, _srs(WORK_EPSG))
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(ct)
        for p in _parts(g):
            if ogr.GT_Flatten(p.GetGeometryType()) != ogr.wkbLineString:
                continue
            pts = [(p.GetX(i), p.GetY(i)) for i in range(p.GetPointCount())]
            if len(pts) < 2:
                continue
            if bbox is not None:
                xs, ys = [q[0] for q in pts], [q[1] for q in pts]
                if max(xs) < bbox[0] or min(xs) > bbox[2] or max(ys) < bbox[1] or min(ys) > bbox[3]:
                    continue
            out.append(pts)
    ds = None
    return out


def _id_of(f, id_field):
    names = [f.GetFieldDefnRef(i).GetName() for i in range(f.GetFieldCount())]
    want = [id_field] if id_field else []
    want += ["structure_id", "structure_", "nidid", "nid_id", "dam_id", "federal_id", "STRUCTURE_NUMBER_008",
             "BRIDGE_ID", "id", "ID", "name"]
    low = {n.lower(): n for n in names}
    for w in want:
        n = low.get(str(w).lower())
        if n is not None and f.GetField(n) not in (None, ""):
            return str(f.GetField(n))
    return "fid {}".format(f.GetFID())


def read_known(layers):
    """core.listed.Known of every feature of the known-structure layers: [(src, label, id field)]
    or plain paths. Points as they are; lines and polygons by their centroid (a DWR dam line).
    A layer with a 'status' field (the DWR dam list: Intact, Breached, Not Built ...) gives each
    point its state, carried to the fills as known_state."""
    pts = []
    dst = _srs(WORK_EPSG)
    for item in layers or []:
        if isinstance(item, (tuple, list)):
            src, label, idf = (list(item) + [None, None])[:3]
        else:
            src, label, idf = item, None, None
        if not src:
            continue
        ds, lyr = open_layer(src)
        label = label or lyr.GetName()
        ct = _transform(lyr, dst)
        dfn = lyr.GetLayerDefn()
        stf = next((dfn.GetFieldDefn(i).GetName() for i in range(dfn.GetFieldCount())
                    if dfn.GetFieldDefn(i).GetName().lower() == "status"), None)
        for f in lyr:
            g = f.GetGeometryRef()
            if g is None:
                continue
            g = g.Clone()
            g.Transform(ct)
            c = g if ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbPoint else g.Centroid()
            if c is None or c.IsEmpty():
                continue
            state = str(f.GetField(stf) or "").strip() if stf else ""
            pts.append((c.GetX(), c.GetY(), _id_of(f, idf), label, state))
        ds = None
    return ls.Known(pts)


FEET_UNITS = ("ft", "foot", "feet", "us survey foot", "us_survey_foot", "ftus", "us-ft", "us foot",
              "international foot")
METRE_UNITS = ("m", "metre", "meter", "metres", "meters")
ND_GROUND_M = (229.0, 1070.0)          # lowest (Red River at the border) / highest (White Butte) ground in ND


def _unit_kind(name):
    """True if name is a feet unit, False if metres, else None."""
    name = (name or "").strip().lower()
    if not name:
        return None
    if name in FEET_UNITS or "foot" in name or "feet" in name:
        return True
    if name in METRE_UNITS or "metre" in name or "meter" in name:
        return False
    return None


def _horizontal_unit(s):
    """Feet/metres of a projected CRS's linear unit; None for a geographic CRS (degrees)."""
    if s is None or s.IsGeographic():
        return None, ""
    if s.IsCompound():
        try:
            proj = s.CloneGeogCS() and s.GetAttrValue("PROJCS")
        except Exception:
            proj = None
        # Horizontal unit of a compound CRS: PROJCS|UNIT, else the first length unit that is not vertical.
        name = (s.GetAttrValue("PROJCS|UNIT") or s.GetAttrValue("PROJCRS|LENGTHUNIT") or "").lower()
        kind = _unit_kind(name)
        if kind is not None:
            return kind, name
    name = ""
    try:
        name = (s.GetLinearUnitsName() or "").lower()
    except Exception:
        name = ""
    kind = _unit_kind(name)
    return (kind, name) if kind is not None else (None, name)


def _value_unit_hint(ds):
    """('feet' or 'metres' or None, median raw value) from a sample of cell values.

    A median in 1,500-15,000 is treated as feet (CONUS plains / plateau in feet; almost no
    ground is that high in metres). A median in 50-900 is treated as metres. Values in
    between, or outside both, are left unknown - they must be chosen in 'DEM elevation unit'.
    """
    try:
        b = ds.GetRasterBand(1)
        w = max(1, min(ds.RasterXSize, 512))
        h = max(1, min(ds.RasterYSize, 512))
        a = b.ReadAsArray(buf_xsize=w, buf_ysize=h).astype("float64")
        nod = b.GetNoDataValue()
        if nod is not None:
            a[a == nod] = np.nan
        a[(a < -1000) | (a > 30000)] = np.nan
        med = float(np.nanmedian(a))
    except Exception:
        return None, None
    if not np.isfinite(med):
        return None, None
    as_m = 50 <= med <= 900
    as_ft = 1500 <= med <= 15000
    if as_ft and not as_m:
        return "feet", med
    if as_m and not as_ft:
        return "metres", med
    return None, med


def dem_unit(dem):
    """(in_feet, how) of a DEM's elevations: the band's unit type, else a compound CRS's vertical
    unit, else a USGS 3DEP file (every file of it named USGS_*: 3DEP is metres in every product),
    else the horizontal CRS linear unit (a State Plane feet DEM with no Z tag), else a value
    heuristic (a typical ground in 1,200-15,000 is feet). A DEM that records none of these is
    refused (RuntimeError) - choose Metres or Feet: values in feet read as metres make every fill
    3.28 times too high and find fills that are not there."""
    ds = _checked(gdal.Open(str(dem)), "opening the DEM {}".format(dem))
    unit = (ds.GetRasterBand(1).GetUnitType() or "").strip().lower()
    kind = _unit_kind(unit)
    if kind is not None:
        return kind, "the band unit '{}'".format(unit)
    s = osr.SpatialReference()
    if ds.GetProjection() and s.ImportFromWkt(ds.GetProjection()) == 0:
        if s.IsCompound():
            name = (s.GetAttrValue("VERT_CS|UNIT") or s.GetAttrValue("VERTCRS|LENGTHUNIT") or "").lower()
            kind = _unit_kind(name)
            if kind is not None:
                return kind, "the vertical CRS unit '{}'".format(name)
        horiz, hname = _horizontal_unit(s)
    else:
        horiz, hname = None, ""
    files = [f for f in (ds.GetFileList() or []) if not f.lower().endswith((".vrt", ".aux.xml", ".ovr", ".xml"))]
    if files and all(os.path.basename(f).upper().startswith("USGS_") for f in files):
        return False, "USGS 3DEP files (metres)"
    hint, med = _value_unit_hint(ds)
    if horiz is not None:
        how = ("the horizontal CRS unit '{}' (no elevation unit was tagged; confirm with "
               "'DEM elevation unit')".format(hname))
        if hint is not None and ((hint == "feet") != horiz):
            raise RuntimeError(
                "The DEM {} has no elevation unit, and the horizontal CRS unit '{}' disagrees "
                "with the typical stored value ({:.0f} looks like {}). Choose Metres or Feet in "
                "'DEM elevation unit'.".format(os.path.basename(str(dem)), hname, med, hint))
        return horiz, how
    if hint is not None:
        return hint == "feet", (
            "typical ground {:.0f} looks like {} (no elevation unit or CRS unit was tagged; "
            "confirm with 'DEM elevation unit')".format(med, hint))
    extra = ""
    if med is not None:
        extra = " Typical stored value is {:.0f}.".format(med)
    raise RuntimeError(
        "The DEM {} records no elevation unit (no band unit, no vertical CRS, not USGS 3DEP files, "
        "no usable horizontal CRS unit).{} Choose Metres or Feet in 'DEM elevation unit': feet "
        "read as metres make every fill 3.28 times too high. (Or tag it: gdal_edit -units m / "
        "-units ft)".format(os.path.basename(str(dem)), extra))


def dem_in_feet(dem):
    """True when the DEM's elevations are in feet (dem_unit; refuses a DEM that records none)."""
    return dem_unit(dem)[0]


def check_dem_unit(dem, z_feet, log=print):
    """The unit to read the DEM in (z_feet None = what it records, see dem_unit), logged, with a
    warning when a sample of the values in metres lies outside ND's ground (a wrong choice)."""
    if z_feet is None:
        z_feet, how = dem_unit(dem)
        log("DEM elevations read in {} ({})".format("feet" if z_feet else "metres", how))
    else:
        log("DEM elevations read in {} (your choice)".format("feet" if z_feet else "metres"))
    try:
        ds = gdal.Open(str(dem))
        b = ds.GetRasterBand(1)
        w = max(1, min(ds.RasterXSize, 512))
        h = max(1, min(ds.RasterYSize, 512))
        a = b.ReadAsArray(buf_xsize=w, buf_ysize=h).astype("float64")
        nod = b.GetNoDataValue()
        if nod is not None:
            a[a == nod] = np.nan
        a[(a < -1000) | (a > 30000)] = np.nan
        med = float(np.nanmedian(a)) * (FT if z_feet else 1.0)
    except Exception:                                                   # noqa - a check only
        return z_feet
    if np.isfinite(med) and not (ND_GROUND_M[0] - 50 <= med <= ND_GROUND_M[1] + 50):
        log("WARNING: read in {} the DEM's typical ground is {:.0f} m ({:.0f} ft), outside North Dakota's "
            "{:.0f}-{:.0f} m: check 'DEM elevation unit' (outside ND this check does not apply)".format(
                "feet" if z_feet else "metres", med, med / FT, *ND_GROUND_M))
    return z_feet


def warp_dem(dem, ext, cell, z_feet=None):
    """(array m, NaN = no data; geotransform) of the DEM over ext (EPSG:5070) at cell metres,
    averaged from the full-resolution cells (no overviews), values as stored (no vertical shift).
    z_feet None = what the DEM records (dem_unit; refused when it records nothing)."""
    if z_feet is None:
        z_feet = dem_in_feet(dem)
    n_x = max(1, int(math.ceil((ext[2] - ext[0]) / cell)))
    n_y = max(1, int(math.ceil((ext[3] - ext[1]) / cell)))
    ds = _checked(gdal.Open(str(dem)), "opening the DEM {}".format(dem))
    nod = ds.GetRasterBand(1).GetNoDataValue()
    opts = dict(format="MEM", dstSRS="EPSG:{}".format(WORK_EPSG),
                outputBounds=(ext[0], ext[1], ext[0] + n_x * cell, ext[1] + n_y * cell),
                width=n_x, height=n_y, resampleAlg="average", dstNodata=-9999.0,
                outputType=gdal.GDT_Float32)
    if nod is not None:
        opts["srcNodata"] = nod
    wo.no_vshift(gdal, wo.no_overviews(gdal, opts))
    out = _checked(gdal.Warp("", ds, **opts), "warping the DEM")
    a = out.GetRasterBand(1).ReadAsArray().astype("float64")
    a[(a <= -9998.0) | ~np.isfinite(a)] = np.nan
    if z_feet:
        a *= FT
    return a, out.GetGeoTransform()


def write_hillshade(dem, ext, cell, path, z_feet=None, max_cells=400e6):
    """A multi-direction hillshade (gdaldem hillshade -multidirectional) of the DEM over ext
    (EPSG:5070) at cell metres, as a compressed Byte GeoTIFF: the review layer under the
    candidates (a fill parallel to a single sun direction does not show; light from all round
    shows every embankment and pond rim). Streams through a warped VRT, so a county does not
    have to fit in memory. None (nothing written) when it would exceed max_cells."""
    if z_feet is None:
        z_feet = dem_in_feet(dem)
    n_x = int(math.ceil((ext[2] - ext[0]) / cell))
    n_y = int(math.ceil((ext[3] - ext[1]) / cell))
    if n_x * n_y > max_cells:
        return None
    src = _checked(gdal.Open(str(dem)), "opening the DEM {}".format(dem))
    opts = dict(format="VRT", dstSRS="EPSG:{}".format(WORK_EPSG),
                outputBounds=(ext[0], ext[1], ext[0] + n_x * cell, ext[1] + n_y * cell),
                width=n_x, height=n_y, resampleAlg="average", outputType=gdal.GDT_Float32)
    wo.no_vshift(gdal, wo.no_overviews(gdal, opts))
    vrt = path + ".src.vrt"
    _checked(gdal.Warp(vrt, src, **opts), "warping the DEM for the hillshade")
    try:
        hs = gdal.DEMProcessing(path, vrt, "hillshade", multiDirectional=True,
                                zFactor=FT if z_feet else 1.0, computeEdges=True,
                                creationOptions=["COMPRESS=DEFLATE", "TILED=YES"])
        _checked(hs, "the hillshade")
        hs = None
    finally:
        try:
            os.remove(vrt)
        except OSError:
            pass
    return path


def layer_extent(lines):
    """Box of the lines, 1 m larger on each side (a straight north-south line has a box)."""
    xs = [p[0] for ln in lines for p in ln]
    ys = [p[1] for ln in lines for p in ln]
    return (min(xs) - 1.0, min(ys) - 1.0, max(xs) + 1.0, max(ys) + 1.0)


def _lonlat(x, y, ct):
    p = ct.TransformPoint(x, y)
    return p[0], p[1]


# ---------------------------------------------------------------------------------- writing --

def is_new_value(row):
    """'yes' when the candidate is new to the lists given, else 'no' (from finder_status or status)."""
    st = row.get("finder_status") if row.get("finder_status") not in (None, "") else row.get("status")
    return "yes" if st == ls.NEW else "no"


def _row_value(row, name):
    if name == "is_new" and row.get("is_new") in (None, ""):
        return is_new_value(row)
    return row.get(name)


def write_points(path, layer, fields, rows, epsg=WORK_EPSG, overwrite=True):
    """A point layer of rows (dicts with 'x', 'y' in epsg) in a GeoPackage; fields [(name, type)]
    with type 'str' / 'float' / 'int'."""
    drv = _checked(ogr.GetDriverByName("GPKG"), "the GeoPackage driver")
    ds = ogr.Open(path, 1) if os.path.exists(path) else drv.CreateDataSource(path)
    _checked(ds, "opening {} for writing".format(path))
    if ds.GetLayerByName(layer) is not None and overwrite:
        for i in range(ds.GetLayerCount()):
            if ds.GetLayer(i).GetName() == layer:
                ds.DeleteLayer(i)
                break
    lyr = _checked(ds.CreateLayer(layer, _srs(epsg), ogr.wkbPoint), "creating layer {}".format(layer))
    kinds = {"str": ogr.OFTString, "float": ogr.OFTReal, "int": ogr.OFTInteger}
    for name, t in fields:
        lyr.CreateField(ogr.FieldDefn(name, kinds[t]))
    ds.StartTransaction()
    for r in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        for name, t in fields:
            v = _row_value(r, name)
            if v is None or (isinstance(v, float) and not math.isfinite(v)):
                continue
            f.SetField(name, {"str": str, "float": float, "int": int}[t](v))
        g = ogr.Geometry(ogr.wkbPoint)
        g.AddPoint_2D(float(r["x"]), float(r["y"]))
        f.SetGeometry(g)
        lyr.CreateFeature(f)
    ds.CommitTransaction()
    ds = None


def write_csv(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([n for n, _ in fields])
        for r in rows:
            w.writerow(["" if _row_value(r, n) is None else _row_value(r, n) for n, _ in fields])


# Screening hand-off: the DWR export field names DamSafety Screening tool 1 reads
# (structure_id, dam_name, max_dam_height ft, max_pool_vol ac-ft, max_pool_area ac, status).
SCREEN_FIELDS = [("structure_id", "str"), ("dam_name", "str"), ("max_dam_height", "float"),
                 ("max_pool_vol", "float"), ("max_pool_area", "float"), ("status", "str"),
                 ("finder_kind", "str"), ("finder_status", "str"), ("is_new", "str"), ("known_id", "str"),
                 ("latitude", "float"), ("longitude", "float")]


def screening_rows(rows, name_of):
    """Candidates that are not listed, as DWR-style dam records (status 'Candidate'). The dam
    height is the fill's crest height over its toe where there is one (a fill), else the height."""
    out = []
    for r in rows:
        if r["status"] == ls.LISTED or not r.get("height_ft"):
            continue
        out.append({"x": r["x"], "y": r["y"], "structure_id": r["find_id"], "dam_name": name_of(r),
                    "max_dam_height": r.get("crest_height_ft") or r.get("height_ft"), "max_pool_vol": r.get("storage_af"),
                    "max_pool_area": r.get("area_ac"), "status": "Candidate",
                    "finder_kind": r.get("kind"), "finder_status": r["status"],
                    "known_id": r.get("known_id"), "latitude": r.get("lat"), "longitude": r.get("lon")})
    return out


# ------------------------------------------------------------------------------- run record --
RUN_SKIP = ("log", "progress", "cancelled", "opener")


def _setting_text(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return str(v)
    if hasattr(v, "__dict__"):
        return "; ".join("{}={}".format(k, x) for k, x in sorted(vars(v).items()))
    if isinstance(v, (list, tuple)):
        return " ;; ".join(_setting_text(x) for x in v)
    return repr(v)



def _file_of_source(value):
    """The path of a GDAL / OGR source string ('file.gpkg|layername=x'), or None."""
    if isinstance(value, (list, tuple)) and value:
        return _file_of_source(value[0])
    s = str(value or "").strip()
    if not s or s.startswith(("http://", "https://")):
        return None
    path = s.split("|", 1)[0]
    return path if os.path.isfile(path) else None


def _input_checksums(settings):
    """sha256_<setting> rows for each input that is a file on disk."""
    import hashlib
    out = []
    seen = set()
    for k, v in settings.items():
        if k in RUN_SKIP:
            continue
        path = _file_of_source(v)
        if not path or path in seen:
            continue
        seen.add(path)
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    h.update(chunk)
        except OSError:
            continue
        out.append(("sha256_{}".format(k), "{} {}".format(h.hexdigest(), os.path.basename(path))))
    return out


def write_run_info(out_gpkg, tool, settings, result=None):
    """Table 'run_info' (key, value) in the output GeoPackage: the tool, the Finder version, GDAL,
    the time, every setting and input it ran with, and whether it was cancelled - so a result
    says how it was made."""
    import datetime
    if not out_gpkg or not os.path.exists(out_gpkg):
        return
    ds = ogr.Open(out_gpkg, 1)
    if ds is None:
        return
    for i in range(ds.GetLayerCount()):
        if ds.GetLayer(i).GetName() == "run_info":
            ds.DeleteLayer(i)
            break
    lyr = ds.CreateLayer("run_info", None, ogr.wkbNone)
    lyr.CreateField(ogr.FieldDefn("key", ogr.OFTString))
    lyr.CreateField(ogr.FieldDefn("value", ogr.OFTString))
    rows = [("tool", tool), ("finder_version", VERSION), ("gdal", gdal.__version__),
            ("run_at", datetime.datetime.now().astimezone().isoformat(timespec="seconds"))]
    defaults = {"embankments": {"params": em.FinderParams, "ridge_params": rd.RidgeParams},
                "ponds": {"params": pd.PondParams}}.get(tool, {})
    for k, make in defaults.items():                    # None = the defaults: record what they are
        if k in settings and settings[k] is None:
            settings = dict(settings, **{k: make()})
    rows += [(k, _setting_text(v)) for k, v in settings.items() if k not in RUN_SKIP]
    rows += _input_checksums(settings)
    if isinstance(result, dict):
        rows.append(("cancelled", str(bool(result.get("cancelled")))))
    for k, v in rows:
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetField("key", k)
        f.SetField("value", v)
        lyr.CreateFeature(f)
    ds = None


def _records_run(tool, out_arg="out_gpkg"):
    """Decorator: after the run, write_run_info with the call's own arguments."""
    import functools
    import inspect

    def wrap(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def run(*a, **kw):
            bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            res = fn(*a, **kw)
            try:
                write_run_info(bound.arguments.get(out_arg), tool, dict(bound.arguments), res)
            except Exception as e:                                      # noqa - a record only
                (bound.arguments.get("log") or print)("run_info not written: {}".format(e))
            return res
        return run
    return wrap


def find_id(prefix, x, y):
    """Stable id from the point rounded to 1 m (the same structure keeps its id between runs)."""
    return "{}-{:08X}".format(prefix, zlib.crc32("{:.0f},{:.0f}".format(x, y).encode("ascii")) & 0xFFFFFFFF)


def _noop(*_a, **_k):
    return False


def _summary(rows, what):
    c = ls.status_counts(rows)
    return "{} {}: {} new, {} unsure, {} already listed".format(len(rows), what, c[ls.NEW], c[ls.UNSURE],
                                                              c[ls.LISTED])


# ------------------------------------------------------------------------------ embankments --
EMB_FIELDS = [("find_id", "str"), ("kind", "str"), ("status", "str"), ("is_new", "str"), ("known_id", "str"),
              ("known_layer", "str"), ("known_state", "str"), ("known_ft", "float"), ("height_ft", "float"),
              ("crest_height_ft", "float"), ("head_ft", "float"), ("valley_drop_ft", "float"), ("spill_ft", "float"),
              ("crest_ft", "float"), ("toe_ft", "float"), ("length_ft", "float"), ("ridge_len_ft", "float"),
              ("storage_af", "float"), ("area_ac", "float"), ("pool", "str"), ("open_pool", "str"),
              ("on_road", "str"), ("line_source", "str"), ("rescued", "str"), ("spillway", "str"),
              ("spill_dist_ft", "float"), ("spill_width_ft", "float"), ("score", "float"),
              ("score_note", "str"),
              # Finder 0.5 evidence (core/embankments.py steps 7-10)
              ("outlet_dist_ft", "float"), ("spill_on_fill", "str"), ("same_pool", "int"),
              ("drain_ac", "float"), ("drain_ratio", "float"), ("ag_head_ft", "float"),
              ("below_frac", "float"), ("rect", "float"), ("elong", "float"), ("wall_slope", "float"),
              ("mounds", "int"), ("top_w_ft", "float"), ("side_sum", "float"), ("relief_ft", "float"),
              ("slope_pct", "float"), ("fill_road", "str"), ("pool_lat", "float"), ("pool_lon", "float"),
              ("confidence", "str"), ("notes", "str"), ("lat", "float"), ("lon", "float")]
SPILL_FIELDS = [("find_id", "str"), ("kind", "str"), ("spill_ft", "float"), ("width_ft", "float"),
                ("dist_ft", "float"), ("lat", "float"), ("lon", "float")]
RIDGE_FIELDS = [("find_id", "str"), ("kind", "str"), ("status", "str"), ("is_new", "str"), ("known_id", "str"),
                ("known_layer", "str"), ("known_state", "str"), ("known_ft", "float"), ("height_ft", "float"),
                ("length_ft", "float"), ("width_ft", "float"), ("head_ft", "float"),
                ("lat", "float"), ("lon", "float")]
DUG_FIELDS = [("find_id", "str"), ("kind", "str"), ("status", "str"), ("is_new", "str"), ("known_id", "str"),
              ("known_layer", "str"), ("known_state", "str"), ("known_ft", "float"), ("depth_ft", "float"),
              ("area_ac", "float"), ("length_ft", "float"), ("width_ft", "float"),
              ("rectangularity", "float"), ("spoil_ft", "float"), ("volume_af", "float"),
              ("lat", "float"), ("lon", "float")]
LINE_STREAM, LINE_DEM, LINE_POOL = "stream line", "DEM drainage", "DEM pool outlet"


@_records_run("embankments")
def run_embankments(dem, streams, out_gpkg, roads=None, rails=None, known=None, bbox=None, cell=3.0,
                    z_feet=None, params=None, match_ft=500.0, unsure_factor=2.0, tile_m=5000.0,
                    road_buffer_m=6.0, hillshade=True, drainage_ac=0.0, drain_cell=10.0,
                    ridges=False, dugouts=False, dikes=True, dike_near_m=200.0,
                    ridge_params=None, model=None, contrib=True, log=print,
                    progress=None, cancelled=None, pool_lines=None, pool_reach_m=50.0,
                    pool_max_drain_ac=50.0, pool_margin_m=300.0, pool_opts=None):
    """Fills across the stream lines in the DEM, checked against the known structures, and (with
    `hillshade`) a multi-direction hillshade of the area beside the GeoPackage for the review.
    drainage_ac > 0: also walk every draw of the DEM draining that many acres (core/terrain.py,
    traced at drain_cell metres), mapped or not. ridges / dugouts: also look for raised ridges
    (levees, dikes) and dug pits anywhere (core/ridges.py; layers 'ridges' and 'dugouts').
    dikes (on by default): also look for a dike that holds water running BESIDE a stream or draw
    (within dike_near_m), which no line crosses - a pond dam built along a channel, not across it.
    These go into the 'ridges' layer and screening_dams. The whole-DEM ridge search (ridges) finds
    them too, so the dike pass is skipped when ridges is on.
    model: a score model (core/score.py JSON) - each fill gets its score.
    contrib (on by default): the contributing area at each fill (drain_ac, and drain_ratio = acres
    drained per acre-foot stored), routed at drain_cell metres over the tile and its pad (a basin
    larger than the pad reads as a minimum).
    pool_lines (None = on whenever drainage_ac is): also walk a short line out of every pool the
    DEM shows - a closed depression or a flat water surface (core/terrain.py pool_seeds) - through
    its outlet, routed as the draws are, so a pond that drains less than drainage_ac still has its
    fill profiled (line_source "DEM pool outlet"; core/embankments.py step 11). On such a line only
    a hump starting within pool_reach_m below where the line leaves the pool is tested, it must
    hold a pool of params.min_pool_m2 or more, not stand on a road, and drain under
    pool_max_drain_ac at its crest (a bigger draw is walked by the draws); a pool whose seed
    already drains pool_max_drain_ac gets no line. pool_margin_m: pools are looked for this far
    round each tile's core; pool_opts: keyword overrides for terrain.pool_seeds. Where a pool line
    and another line read the same fill, the other line's reading is kept.
    Returns {'rows', 'counts', 'gpkg', 'csv', 'hillshade', ...}."""
    p = params or em.FinderParams()
    rp = ridge_params or rd.RidgeParams()
    if pool_lines is None:
        pool_lines = bool(drainage_ac)
    progress = progress or _noop
    cancelled = cancelled or _noop
    z_feet = check_dem_unit(dem, z_feet, log)
    lines = read_lines(streams, bbox) if streams else []
    dem_only = bool(drainage_ac or ridges or dugouts)
    if not lines and not dem_only:
        raise RuntimeError("no stream lines in the area (or turn on the DEM drainage lines)")
    ext = bbox or (layer_extent(lines) if lines else _image_box(dem))
    road_lines = read_lines(roads, None) + read_lines(rails, None) if (roads or rails) else []
    kn = read_known(known)
    log("{} stream line(s), {} road / rail line(s), {} known structure(s)".format(
        len(lines), len(road_lines), len(kn)))
    pad = p.window_m + p.toe_search_m + p.max_len_m + 2 * cell
    tiles = []
    x = ext[0]
    while x < ext[2]:
        y = ext[1]
        while y < ext[3]:
            tiles.append((x, y, min(x + tile_m, ext[2]), min(y + tile_m, ext[3])))
            y += tile_m
        x += tile_m
    if drainage_ac:
        pad = max(pad, 3000.0)                      # most of a draw's basin is in the tile
    found, ridge_rows, dug_rows, t0 = [], [], [], time.time()
    for ti, core_box in enumerate(tiles):
        if cancelled():
            break
        box = (core_box[0] - pad, core_box[1] - pad, core_box[2] + pad, core_box[3] + pad)
        tl = [ln for ln in lines if _meets(ln, box)]
        if not tl and not dem_only:
            continue
        z, gt = warp_dem(dem, box, cell, z_feet)
        if not np.isfinite(z).any():
            log("tile {}/{}: no DEM data".format(ti + 1, len(tiles)))
            continue
        rmask = None
        if road_lines:
            rmask = np.zeros(z.shape, bool)
            for ln in road_lines:
                if _meets(ln, box):
                    for r, c in em.line_cells(ln, gt, z.shape):
                        rmask[r, c] = True
            k = max(1, int(round(road_buffer_m / cell)))
            rmask = ndi.binary_dilation(rmask, iterations=k)

        def keep(xx, yy, b=core_box):
            return b[0] <= xx < b[2] and b[1] <= yy < b[3]

        dl, deep, darea, pls, pex, pdr = [], None, None, [], [], []
        if drainage_ac or ridges or dikes or contrib or pool_lines:
            z10, gt10 = warp_dem(dem, box, max(drain_cell, cell), z_feet)
            if drainage_ac or contrib or pool_lines:
                # routed over the DEM with narrow raised features opened away, so a draw runs
                # straight through a dam or a road fill (as a stream line does), not round it
                rec, order, _f = tr.flow(tr.open_ridges(z10, abs(gt10[1]), rp.width_m))
                area, _len = tr.accumulate(rec, order, z10.shape, abs(gt10[1]))
                darea = _area_at(area, gt10)
            if drainage_ac:
                dl = tr.drainage_lines(rec, area, gt10, drainage_ac * AC_M2)
            if pool_lines and not cancelled():
                pls, pex, pdr = _pool_lines(z, gt, core_box, pool_margin_m, rec, gt10, area,
                                       pool_max_drain_ac * AC_M2 if pool_max_drain_ac else None,
                                       pool_opts or {})
            if ridges or dikes:
                _r, _o, filled = tr.flow(z10)
                d10 = tr.depth(z10, filled)
                rr = np.clip(((gt[3] + (np.arange(z.shape[0]) + 0.5) * gt[5]) - gt10[3]) / gt10[5],
                             0, z10.shape[0] - 1).astype(int)
                cc = np.clip(((gt[0] + (np.arange(z.shape[1]) + 0.5) * gt[1]) - gt10[0]) / gt10[1],
                             0, z10.shape[1] - 1).astype(int)
                deep = d10[np.ix_(rr, cc)]
        lim = {len(tl) + len(dl) + i: x + pool_reach_m for i, x in enumerate(pex)} if pls else None
        rules = dict(min_area_m2=p.min_pool_m2, skip_road=True,
                     max_drain_m2=pool_max_drain_ac * AC_M2 if pool_max_drain_ac else None,
                     line_area={len(tl) + len(dl) + i: _line_area(ln, area, gt10) for i, ln in enumerate(pls)},
                     pool_drain={len(tl) + len(dl) + i: v for i, v in enumerate(pdr)},
                     inflow=(lambda xs, ys, near=None, rec=rec, area=area, g=gt10, c3=abs(gt[1]):
                             _held_inflow(xs, ys, near, rec, area, g, c3)) if pls else None)
        got = em.find(z, gt, tl + dl + pls, road_mask=rmask, params=p, keep=keep, cancelled=cancelled,
                      drain_area=darea, line_lim=lim, pool_rules=rules)
        for e in got:
            e.line_src = (LINE_STREAM if e.line < len(tl) else
                          LINE_DEM if e.line < len(tl) + len(dl) else LINE_POOL)
        found.extend(got)
        msg = "tile {}/{}: {} line(s){}{}, {} fill(s)".format(
            ti + 1, len(tiles), len(tl), " + {} DEM draw(s)".format(len(dl)) if drainage_ac else "",
            " + {} pool outlet(s)".format(len(pls)) if pool_lines else "", len(got))
        wide = None
        if rmask is not None:                  # a road embankment's base, not just its centreline
            wide = ndi.binary_dilation(rmask, iterations=max(1, int(round(0.5 * rp.width_m / cell))))
        if ridges and not cancelled():
            rg = rd.find_ridges(z, gt, depth=deep, road_mask=wide, params=rp, keep=keep,
                                exclude=[(e.x, e.y) for e in got])
            ridge_rows.extend(rg)
            msg += ", {} ridge(s)".format(len(rg))
        elif dikes and not cancelled() and (tl or dl):
            near = np.zeros(z.shape, bool)     # a buffer around the stream lines and draws
            for ln in tl + dl:
                for r, c in em.line_cells(ln, gt, z.shape):
                    near[r, c] = True
            near = ndi.binary_dilation(near, iterations=max(1, int(round(dike_near_m / cell))))
            dk = rd.find_ridges(z, gt, depth=deep, road_mask=wide, params=rp, keep=keep,
                                exclude=[(e.x, e.y) for e in got], near_mask=near, water_only=True)
            ridge_rows.extend(dk)
            msg += ", {} channel-side dike(s)".format(len(dk))
        if dugouts and not cancelled():
            dg = rd.find_dugouts(z, gt, params=rp, keep=keep)
            dug_rows.extend(dg)
            msg += ", {} dug pit(s)".format(len(dg))
        log(msg + " ({:.0f} s)".format(time.time() - t0))
        progress(100.0 * (ti + 1) / len(tiles))
    if p.one_per_pool:
        found = em.one_per_pool(found, cell)
    found = em.dedupe(found, p.dedupe_m)
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    rows = []
    for e in found:
        st = ls.check(e.x, e.y, kn, match_ft * FT, unsure_factor)
        lon, lat = _lonlat(e.x, e.y, ll)
        plon, plat = _lonlat(e.pool_xy[0], e.pool_xy[1], ll) if e.pool_xy else (None, None)
        rows.append({"x": e.x, "y": e.y, "find_id": find_id("FIND", e.x, e.y), "kind": e.kind,
                     "status": st["status"], "known_id": st["known_id"], "known_layer": st["known_src"],
                     "known_state": st.get("known_state", ""),
                     "known_ft": None if st["known_m"] is None else round(st["known_m"] / FT, 0),
                     "height_ft": round(e.height_m / FT, 1), "crest_height_ft": round(e.crest_height_m / FT, 1),
                     "head_ft": round(e.head_m / FT, 1),
                     "valley_drop_ft": round(e.valley_drop_m / FT, 1),
                     "spill_ft": round(e.spill_m / FT, 2), "crest_ft": round(e.crest_m / FT, 2),
                     "toe_ft": round(e.toe_m / FT, 2), "length_ft": round(e.len_m / FT, 0),
                     "ridge_len_ft": round(e.ridge_len_m / FT, 0),
                     "storage_af": round(e.storage_m3 / AF_M3, 2), "area_ac": round(e.pool_area_m2 / AC_M2, 2),
                     "pool": "yes" if e.pool else "no", "open_pool": "yes" if e.open_pool else "no",
                     "on_road": "yes" if e.on_road else "no", "line_source": e.line_src or LINE_STREAM,
                     "rescued": "yes" if e.rescued else "no",
                     "spillway": _spillway(e), "spill_dist_ft": round(e.spill_dist_m / FT, 0) if e.spill_xy else None,
                     "spill_width_ft": round(e.spill_width_m / FT, 0) if e.spill_xy else None,
                     "outlet_dist_ft": _rnd(e.outlet_dist_m / FT, 0),
                     "spill_on_fill": "yes" if e.spill_on_fill else "no", "same_pool": e.same_pool,
                     "drain_ac": _rnd(e.drain_m2 / AC_M2, 1),
                     "drain_ratio": _rnd((e.drain_m2 / AC_M2) / max(e.storage_m3 / AF_M3, 0.1), 2),
                     "ag_head_ft": _rnd(e.ag_head_m / FT, 1), "below_frac": _rnd(e.below_frac, 2),
                     "rect": _rnd(e.rect, 2), "elong": _rnd(e.elong, 2), "wall_slope": _rnd(e.wall_slope, 3),
                     "mounds": e.mounds, "top_w_ft": _rnd(e.top_w_m / FT, 0), "side_sum": _rnd(e.side_sum, 1),
                     "relief_ft": _rnd(e.relief_m / FT, 1), "slope_pct": _rnd(e.slope_pct, 2),
                     "fill_road": "yes" if e.fill_road else "no",
                     "pool_lat": None if plat is None else round(plat, 6),
                     "pool_lon": None if plon is None else round(plon, 6),
                     "confidence": e.confidence,
                     "notes": "; ".join(e.notes), "lat": round(lat, 6), "lon": round(lon, 6)})
    if model:
        from .core import score as sc
        m = sc.load(model) if isinstance(model, str) else model
        for r, (s_, why) in zip(rows, sc.score_detail(m, rows)):
            r["score"] = round(float(s_), 3)
            r["score_note"] = why
    rows.sort(key=lambda r: ({"new": 0, "unsure": 1, "listed": 2}[r["status"]],
                             -(r.get("score") or 0.0), -r["height_ft"]))
    out_csv = os.path.splitext(out_gpkg)[0] + "_embankments.csv"
    write_points(out_gpkg, "embankments", EMB_FIELDS, rows)
    write_csv(out_csv, EMB_FIELDS, rows)
    spills = []
    for e in found:
        if e.spill_xy:
            lon, lat = _lonlat(e.spill_xy[0], e.spill_xy[1], ll)
            spills.append({"x": e.spill_xy[0], "y": e.spill_xy[1], "find_id": find_id("FIND", e.x, e.y),
                           "kind": _spillway(e), "spill_ft": round(e.spill_m / FT, 2),
                           "width_ft": round(e.spill_width_m / FT, 0), "dist_ft": round(e.spill_dist_m / FT, 0),
                           "lat": round(lat, 6), "lon": round(lon, 6)})
    write_points(out_gpkg, "spillways", SPILL_FIELDS, spills)
    extra = []
    if ridges or dikes:
        extra = _ridge_rows(ridge_rows, kn, match_ft, unsure_factor, ll)
        write_points(out_gpkg, "ridges", RIDGE_FIELDS, extra)
        write_csv(os.path.splitext(out_gpkg)[0] + "_ridges.csv", RIDGE_FIELDS, extra)
        log(_summary(extra, "channel-side dike(s)" if dikes and not ridges
                     else "raised ridge(s) off the stream lines"))
    dugs = []
    if dugouts:
        dugs = _dug_rows(dug_rows, kn, match_ft, unsure_factor, ll)
        write_points(out_gpkg, "dugouts", DUG_FIELDS, dugs)
        write_csv(os.path.splitext(out_gpkg)[0] + "_dugouts.csv", DUG_FIELDS, dugs)
        log(_summary(dugs, "dug pit(s) in the LiDAR"))
    # only dam-kind candidates (and channel-side dikes that hold water): a dry embankment or
    # road fill is never handed to Screening as a dam to protect (Screening cuts a road fill
    # on purpose to open the culvert)
    dams = screening_rows([r for r in rows if r["kind"] == em.K_DAM]
                          + [r for r in extra if r["kind"] == rd.K_LEVEE],
                          lambda r: "{} ({})".format(r["kind"], r["find_id"]))
    write_points(out_gpkg, "screening_dams", SCREEN_FIELDS, dams, epsg=WORK_EPSG)
    counts = {"kinds": em.kind_counts(found), "status": ls.status_counts(rows),
              "ridges": len(extra), "dugouts": len(dugs),
              "dem_drainage": sum(1 for r in rows if r["line_source"] == LINE_DEM),
              "pool_outlet": sum(1 for r in rows if r["line_source"] == LINE_POOL),
              "rescued": sum(1 for r in rows if r["rescued"] == "yes")}
    log(_summary(rows, "fill(s) across streams"))
    hs = None
    if hillshade and not cancelled():
        w = p.window_m if bbox is None else 0.0             # the stream box plus the search window
        hs = write_hillshade(dem, (ext[0] - w, ext[1] - w, ext[2] + w, ext[3] + w), cell,
                             os.path.splitext(out_gpkg)[0] + "_hillshade.tif", z_feet)
        log("review hillshade (light from all round): {}".format(
            hs or "not written - the area is too large at this cell size; make one per county"))
    return {"rows": rows, "counts": counts, "gpkg": out_gpkg, "csv": out_csv, "screening": len(dams),
            "hillshade": hs, "ridges": extra, "dugouts": dugs, "spillways": spills,
            "cancelled": bool(cancelled())}


def _rnd(v, nd):
    """v rounded to nd places, None when not a finite number (not measured)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _pool_lines(z, gt, core_box, margin_m, rec, gt10, area, max_area=None, opts=None):
    """(lines, exits, drains) of one tile: the pools of the DEM z (grid gt) over the core box plus
    margin_m (terrain.pool_seeds), each walked out through its outlet down the opened routing `rec`
    (grid gt10, the draws' receivers); exits = the distance along each line at which it leaves its
    own pool; drains = the ground draining into that pool (m2, terrain.pool_inflow over `area` on
    gt10): flat routing can split a pool's water over many outlet cells, so no one cell on the line
    holds it all. A pool is the seed's own (its flat or depression), on the 10 m cells at least
    40 % of whose 3 m cells it covers, so a creek or another pond beside it is not part of it. A pool
    whose seed already drains max_area gets no line: the draws walk it."""
    c0 = max(0, int((core_box[0] - margin_m - gt[0]) / gt[1]))
    c1 = min(z.shape[1], int(math.ceil((core_box[2] + margin_m - gt[0]) / gt[1])))
    r0 = max(0, int((core_box[3] + margin_m - gt[3]) / gt[5]))
    r1 = min(z.shape[0], int(math.ceil((core_box[1] - margin_m - gt[3]) / gt[5])))
    if r1 <= r0 or c1 <= c0:
        return [], [], []
    sub = z[r0:r1, c0:c1]
    gs = (gt[0] + c0 * gt[1], gt[1], 0.0, gt[3] + r0 * gt[5], 0.0, gt[5])
    seeds, _mask, cells = tr.pool_seeds(sub, gs, cells=True, **(opts or {}))
    H10, W10 = area.shape
    per = (abs(gt10[1]) / abs(gs[1])) ** 2              # 3 m cells in a 10 m cell

    def rc10(x, y):
        return int((y - gt10[3]) / gt10[5]), int((x - gt10[0]) / gt10[1])

    def own10(sd, idx):
        rr, cc = np.divmod(np.asarray(idx, np.int64), sub.shape[1])
        R = np.floor((gs[3] + (rr + 0.5) * gs[5] - gt10[3]) / gt10[5]).astype(np.int64)
        C = np.floor((gs[0] + (cc + 0.5) * gs[1] - gt10[0]) / gt10[1]).astype(np.int64)
        ok = (R >= 0) & (R < H10) & (C >= 0) & (C < W10)
        u, n = np.unique(R[ok] * W10 + C[ok], return_counts=True)
        got = set(u[n >= 0.4 * per].tolist())
        r, c = rc10(sd[0], sd[1])
        if 0 <= r < H10 and 0 <= c < W10:
            got.add(r * W10 + c)
        return got

    pairs = list(zip(seeds, cells))
    if max_area:
        def drains(sd):
            r, c = rc10(sd[0], sd[1])
            return area[r, c] if 0 <= r < H10 and 0 <= c < W10 else 0.0
        pairs = [pc for pc in pairs if drains(pc[0]) < max_area]
    pairs.sort(key=lambda pc: -pc[0][2])          # a seed sharing a cell with a bigger pool's is dropped
    seeds = [pc[0] for pc in pairs]
    pools = [own10(*pc) for pc in pairs]
    lines, exits, index = tr.pool_lines(seeds, rec, gt10, (H10, W10), pools=pools, with_index=True)
    return lines, exits, [tr.pool_inflow(pools[i], rec, area) for i in index]


def _cells10(xs, ys, gt, shape, share=0.0, cell=None):
    """The flat indices of the cells of a grid (geotransform gt, shape) holding the points xs, ys;
    with share and cell (the points' own cell size), only the cells they cover at least that share
    of (a creek cell a pool's edge just reaches is not part of the pool)."""
    c = np.floor((np.asarray(xs, float) - gt[0]) / gt[1]).astype(np.int64)
    r = np.floor((np.asarray(ys, float) - gt[3]) / gt[5]).astype(np.int64)
    ok = (r >= 0) & (r < shape[0]) & (c >= 0) & (c < shape[1])
    u, n = np.unique(r[ok] * shape[1] + c[ok], return_counts=True)
    if share and cell:
        u = u[n * float(cell) ** 2 >= share * abs(gt[1] * gt[5])]
    return u.tolist()


def _held_inflow(xs, ys, near, rec, area, gt10, cell):
    """The ground (m2, `area` on the 10 m routing grid gt10, receivers `rec`) draining into the pool
    at the points xs, ys (cell: their own cell size) whose water the routing takes out of it within
    near = (x, y, radius m): the 10 m cells the pool covers at least 40 % of (a creek cell its edge
    brushes is not part of it)."""
    return tr.pool_inflow(_cells10(xs, ys, gt10, area.shape, 0.4, cell), rec, area, near=_near10(near, gt10))


def _near10(near, gt):
    """(x, y, radius m) -> (row, col, radius) in cells of a grid with geotransform gt (None stays)."""
    if near is None:
        return None
    x, y, rad = near
    return ((y - gt[3]) / gt[5] - 0.5, (x - gt[0]) / gt[1] - 0.5, rad / abs(gt[1]))


def _line_area(line, area, gt):
    """The contributing area (m2) of `area` (grid gt) at each point of `line` (a pool line: the
    centres of the cells of its own routing path)."""
    h, w = area.shape
    out = []
    for x, y in line:
        c = int(math.floor((x - gt[0]) / gt[1]))
        r = int(math.floor((y - gt[3]) / gt[5]))
        out.append(float(area[r, c]) if 0 <= r < h and 0 <= c < w else float("nan"))
    return out


def _area_at(area, gt, snap=2):
    """f(x, y) -> the largest contributing area (m2) of the cells of `area` (grid of geotransform
    gt) within `snap` cells of the point: the draw itself, not a cell just off it."""
    h, w = area.shape

    def at(x, y):
        c = int(math.floor((x - gt[0]) / gt[1]))
        r = int(math.floor((y - gt[3]) / gt[5]))
        if not (0 <= r < h and 0 <= c < w):
            return float("nan")
        return float(area[max(0, r - snap):r + snap + 1, max(0, c - snap):c + snap + 1].max())
    return at


def _spillway(e):
    if not e.spill_xy:
        return "not found in the window"
    if e.spill_dist_m <= 30.0:
        return "over the fill at the stream"
    return "beside the fill (spillway / saddle / end)"


def _status_row(x, y, prefix, kn, match_ft, unsure_factor, ll):
    st = ls.check(x, y, kn, match_ft * FT, unsure_factor)
    lon, lat = _lonlat(x, y, ll)
    return {"x": x, "y": y, "find_id": find_id(prefix, x, y), "status": st["status"],
            "known_id": st["known_id"], "known_layer": st["known_src"],
            "known_state": st.get("known_state", ""),
            "known_ft": None if st["known_m"] is None else round(st["known_m"] / FT, 0),
            "lat": round(lat, 6), "lon": round(lon, 6)}


def _ridge_rows(found, kn, match_ft, unsure_factor, ll):
    out = []
    for g in found:
        r = _status_row(g["x"], g["y"], "RIDGE", kn, match_ft, unsure_factor, ll)
        r.update({"kind": g["kind"], "height_ft": round(g["height_m"] / FT, 1),
                  "crest_height_ft": round(g["height_m"] / FT, 1),
                  "length_ft": round(g["length_m"] / FT, 0), "width_ft": round(g["width_m"] / FT, 0),
                  "head_ft": round(g["head_m"] / FT, 1)})
        out.append(r)
    out.sort(key=lambda r: ({"new": 0, "unsure": 1, "listed": 2}[r["status"]], -r["height_ft"]))
    return out


def _dug_rows(found, kn, match_ft, unsure_factor, ll):
    out = []
    for g in found:
        r = _status_row(g["x"], g["y"], "DUG", kn, match_ft, unsure_factor, ll)
        r.update({"kind": "dugout (LiDAR)", "depth_ft": round(g["depth_m"] / FT, 1),
                  "area_ac": round(g["area_m2"] / AC_M2, 3), "length_ft": round(g["length_m"] / FT, 0),
                  "width_ft": round(g["width_m"] / FT, 0), "rectangularity": round(g["rect"], 2),
                  "spoil_ft": round(g["spoil_m"] / FT, 1), "volume_af": round(g["volume_m3"] / AF_M3, 2)})
        out.append(r)
    out.sort(key=lambda r: ({"new": 0, "unsure": 1, "listed": 2}[r["status"]], -r["volume_af"]))
    return out


def _meets(line, box):
    xs = [q[0] for q in line]
    ys = [q[1] for q in line]
    return not (max(xs) < box[0] or min(xs) > box[2] or max(ys) < box[1] or min(ys) > box[3])


# ------------------------------------------------------------------------------------ ponds --
POND_FIELDS = [("find_id", "str"), ("kind", "str"), ("status", "str"), ("is_new", "str"), ("known_id", "str"),
               ("known_layer", "str"), ("known_state", "str"), ("known_ft", "float"), ("wb_id", "str"),
               ("wb_type", "str"),
               ("height_ft", "float"), ("head_ft", "float"), ("freeboard_ft", "float"),
               ("arc_deg", "float"), ("storage_af", "float"), ("above_grade_af", "float"),
               ("area_ac", "float"), ("no_flood", "str"), ("pool_method", "str"),
               ("nwi_modifier", "str"), ("rectangularity", "float"), ("confidence", "str"), ("notes", "str"), ("lat", "float"),
               ("lon", "float")]
# NWI (Cowardin) special modifiers: the lower-case letters that end a wetland code (PUBHx, PEM1Ch)
NWI_MODIFIERS = {"b": "beaver", "d": "partly drained / ditched", "f": "farmed",
                 "h": "diked / impounded", "r": "artificial substrate", "s": "spoil",
                 "x": "excavated"}
_NWI_CODE = re.compile(r"^[MERLP][0-9A-Z/]*([a-z]+)$")


def nwi_modifiers(code):
    """The special-modifier letters of an NWI wetland code ('' when none or not a code)."""
    m = _NWI_CODE.match(str(code or "").strip())
    return "".join(ch for ch in m.group(1) if ch in NWI_MODIFIERS) if m else ""


def _rings(geom):
    """[[outer, hole, ...], ...] of a (multi)polygon, as lists of (x, y)."""
    polys = []
    for p in _parts(geom):
        if ogr.GT_Flatten(p.GetGeometryType()) != ogr.wkbPolygon:
            continue
        rings = []
        for i in range(p.GetGeometryCount()):
            r = p.GetGeometryRef(i)
            rings.append([(r.GetX(k), r.GetY(k)) for k in range(r.GetPointCount())])
        if rings:
            polys.append(rings)
    return polys


def _rasterize(polys, gt, shape):
    """Bool grid of the cells whose centre is in any polygon (vectorised even-odd rule)."""
    h, w = shape
    xs = gt[0] + (np.arange(w) + 0.5) * gt[1]
    ys = gt[3] + (np.arange(h) + 0.5) * gt[5]
    X, Y = np.meshgrid(xs, ys)
    out = np.zeros(shape, bool)
    for rings in polys:
        inside = np.zeros(shape, bool)
        for ring in rings:
            r = np.asarray(ring, float)
            x0, y0 = r[:, 0], r[:, 1]
            x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
            for a, b, c, d in zip(x0, y0, x1, y1):
                if b == d:
                    continue
                m = (b > Y) != (d > Y)
                xc = a + (Y - b) * (c - a) / (d - b)
                inside ^= m & (X < xc)
        out |= inside
    return out


@_records_run("ponds")
def run_ponds(waterbodies, dem, out_gpkg, known=None, bbox=None, cell=2.0, z_feet=None, params=None,
              id_field=None, type_field=None, dug_words=("excavat", "dugout", "pit"),
              min_ac=0.1, max_ac=50.0, keep_depressions=False, match_ft=300.0, unsure_factor=2.0,
              dug_rect=0.85, dug_max_ac=2.0, water_elev=None, water_elev_band=1,
              log=print, progress=None, cancelled=None):
    """Every water-body polygon classed by core/ponds.py (embankment pond / dugout / natural
    depression) and checked against the known structures. A water body is taken for dug (the
    engine's excavated hint) when its type says so (dug_words, or an NWI code ending in x). A
    natural depression whose outline fills at least dug_rect of its smallest rectangle (and is at
    most dug_max_ac) is called a dugout: potholes are round or ragged, dug pits are rectangles.
    Returns {'rows', 'counts', ...}."""
    p = params or pd.PondParams()
    progress = progress or _noop
    cancelled = cancelled or _noop
    z_feet = check_dem_unit(dem, z_feet, log)
    kn = read_known(known)
    ds, lyr = open_layer(waterbodies)
    if not type_field:                    # an NWI layer: its wetland code carries the modifiers
        names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())]
        type_field = next((n for n in names if n.lower() in ("attribute", "wetland_code")), None)
    ct = _transform(lyr, _srs(WORK_EPSG))
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    feats = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(ct)
        area = g.GetArea() / AC_M2
        if area < min_ac or (max_ac and area > max_ac):
            continue
        env = g.GetEnvelope()                                      # (xmin, xmax, ymin, ymax)
        if bbox is not None and (env[1] < bbox[0] or env[0] > bbox[2] or env[3] < bbox[1] or env[2] > bbox[3]):
            continue
        c = g.PointOnSurface()
        wb_id = str(f.GetField(id_field)) if id_field else str(f.GetFID())
        wb_type = str(f.GetField(type_field) or "") if type_field else ""
        rings = _rings(g)
        rect = None
        if rings:
            ra = rd.rect_of([q for pr in rings for q in pr[0]])[0]
            rect = round(g.GetArea() / ra, 2) if ra > 0 else None
        feats.append((wb_id, wb_type, rings, env, (c.GetX(), c.GetY()), area, rect))
    ds = None
    log("{} water bod(ies) between {:g} and {:g} ac, {} known structure(s)".format(
        len(feats), min_ac, max_ac or float("inf"), len(kn)))
    rows, results, failed = [], [], 0
    pad = p.ring_m + 4 * cell
    for i, (wb_id, wb_type, polys, env, (px, py), area, rect) in enumerate(feats):
        if cancelled():
            break
        box = (env[0] - pad, env[2] - pad, env[1] + pad, env[3] + pad)
        try:
            z, gt = warp_dem(dem, box, cell, z_feet)
            water = _rasterize(polys, gt, z.shape)
            mods = nwi_modifiers(wb_type)
            hint = ("x" in mods or any(w in wb_type.lower() for w in dug_words)) if wb_type else False
            pool_over = None
            if water_elev is not None:
                try:
                    wb, _wgt = read_bands(water_elev, box, cell, (water_elev_band,))
                    we = wb[water_elev_band]
                    wet = water & np.isfinite(we) & (we > -999998.0)
                    if wet.sum() >= 4:
                        pool_over = float(np.median(we[wet]))
                except Exception as e:
                    log("water body {}: could not read the water-elevation mosaic ({})".format(wb_id, e))
            res = pd.analyze_pond(z, water, cell, params=p, excavated_hint=hint, pool_override=pool_over)
        except (RuntimeError, ValueError) as e:
            failed += 1
            log("water body {}: {}".format(wb_id, e))
            continue
        kind = res.pond_type
        shaped = (kind == pd.T_DEPRESSION and rect is not None and rect >= dug_rect and area <= dug_max_ac)
        if shaped:
            kind = res.pond_type = pd.T_DUGOUT
            res.notes.append("dugout by its shape: the outline fills {:.0%} of its smallest "
                             "rectangle".format(rect))
        if kind == pd.T_DEPRESSION and not keep_depressions:
            continue
        results.append(res)
        st = ls.check(px, py, kn, match_ft * FT, unsure_factor, polygons=polys)
        lon, lat = _lonlat(px, py, ll)
        rows.append({"x": px, "y": py, "find_id": find_id("POND", px, py), "kind": kind,
                     "status": st["status"], "known_id": st["known_id"], "known_layer": st["known_src"],
                     "known_state": st.get("known_state", ""),
                     "known_ft": None if st["known_m"] is None else round(st["known_m"] / FT, 0),
                     "wb_id": wb_id, "wb_type": wb_type,
                     "height_ft": round(res.height_m / FT, 1) if not res.no_flood else None,
                     "head_ft": round(res.head_m / FT, 1), "freeboard_ft": round(res.freeboard_m / FT, 1),
                     "arc_deg": round(res.arc_deg, 0), "storage_af": round(res.total_m3 / AF_M3, 2),
                     "above_grade_af": round(res.above_grade_m3 / AF_M3, 2), "area_ac": round(area, 2),
                     "no_flood": "yes" if res.no_flood else "no", "pool_method": res.pool_method,
                     "nwi_modifier": ", ".join(NWI_MODIFIERS[c] for c in mods), "rectangularity": rect,
                     "confidence": res.confidence, "notes": "; ".join(res.notes + (
                         [res.no_flood_reason] if res.no_flood_reason else []) + (
                         ["the NWI marks it diked / impounded (h) but the LiDAR shows no embankment "
                          "holding it: check it"] if "h" in mods and res.pond_type == pd.T_DEPRESSION else [])),
                     "lat": round(lat, 6), "lon": round(lon, 6)})
        progress(100.0 * (i + 1) / max(1, len(feats)))
    rows.sort(key=lambda r: ({"new": 0, "unsure": 1, "listed": 2}[r["status"]], -(r["storage_af"] or 0)))
    out_csv = os.path.splitext(out_gpkg)[0] + "_ponds.csv"
    write_points(out_gpkg, "ponds", POND_FIELDS, rows)
    write_csv(out_csv, POND_FIELDS, rows)
    dams = screening_rows([r for r in rows if r["no_flood"] == "no"],
                          lambda r: "{} {}".format(r["kind"], r["wb_id"]))
    write_points(out_gpkg, "screening_ponds", SCREEN_FIELDS, dams)
    log(_summary(rows, "pond(s) and dugout(s)") + (" ({} failed)".format(failed) if failed else ""))
    return {"rows": rows, "counts": {"kinds": pd.type_counts(results), "status": ls.status_counts(rows)},
            "gpkg": out_gpkg, "csv": out_csv, "failed": failed, "screening": len(dams),
            "cancelled": bool(cancelled())}


# -------------------------------------------------------------------------------- crossings --
CROSS_FIELDS = [("find_id", "str"), ("status", "str"), ("is_new", "str"), ("known_id", "str"), ("known_layer", "str"),
                ("known_state", "str"), ("known_ft", "float"), ("angle_deg", "float"), ("fill_kind", "str"),
                ("fill_height_ft", "float"), ("drainage_ac", "float"), ("flow_len_ft", "float"),
                ("slope_pct", "float"), ("tc_min", "float"), ("q_cfs", "float"), ("pipe_in", "float"),
                ("pipe_size", "str"), ("size_note", "str"), ("lat", "float"), ("lon", "float")]


def _size_crossings(rows, dem, z_feet, c_runoff, intensity, v_fps, drain_cell, tile_m, log, cancelled=None):
    """Drainage area, Kirpich tc, rational Q and the round pipe for Q at each crossing (in place)."""
    if not rows:
        return
    pad = 4000.0
    xs, ys = [r["x"] for r in rows], [r["y"] for r in rows]
    x0 = min(xs)
    y0 = min(ys)
    groups = {}
    for r in rows:
        groups.setdefault((int((r["x"] - x0) // tile_m), int((r["y"] - y0) // tile_m)), []).append(r)
    snap = max(1, int(round(30.0 / drain_cell)))
    cancelled = cancelled or _noop
    for (i, j), rs in groups.items():
        if cancelled():
            return
        box = (x0 + i * tile_m - pad, y0 + j * tile_m - pad, x0 + (i + 1) * tile_m + pad,
               y0 + (j + 1) * tile_m + pad)
        z, gt = warp_dem(dem, box, drain_cell, z_feet)
        if not np.isfinite(z).any():
            continue
        rec, order, _f = tr.flow(tr.open_ridges(z, drain_cell, 60.0))
        area, flen, top = tr.accumulate(rec, order, z.shape, drain_cell, dem=z)
        for r in rs:
            c = int((r["x"] - gt[0]) / gt[1])
            rr = int((r["y"] - gt[3]) / gt[5])
            r0, r1 = max(0, rr - snap), min(z.shape[0], rr + snap + 1)
            c0, c1 = max(0, c - snap), min(z.shape[1], c + snap + 1)
            win = area[r0:r1, c0:c1]
            if win.size == 0:
                continue
            k = np.unravel_index(int(np.argmax(win)), win.shape)
            a, L, t = float(win[k]), float(flen[r0:r1, c0:c1][k]), float(top[r0:r1, c0:c1][k])
            zo = float(z[r0:r1, c0:c1][k])
            ac = a / AC_M2
            slope = max((t - zo) / L, 1e-4) if L > 0 and np.isfinite(t) and np.isfinite(zo) else 1e-3
            tc = cx.kirpich_tc_min(L / FT, slope)
            q = cx.rational_cfs(c_runoff, intensity, ac)
            d_in, size = cx.pipe_for(q, v_fps)
            notes = []
            if ac > 200:
                notes.append("over 200 ac: the rational method is too crude; use a regional regression "
                             "(USGS StreamStats)")
            if L > 0.8 * pad:
                notes.append("the basin may reach past the DEM read: the area is a minimum")
            r.update({"drainage_ac": round(ac, 1), "flow_len_ft": round(L / FT, 0),
                      "slope_pct": round(100 * slope, 2), "tc_min": round(tc, 1), "q_cfs": round(q, 1),
                      "pipe_in": round(d_in, 0),
                      "pipe_size": "{} in round".format(size) if size else "box culvert / bridge",
                      "size_note": "; ".join(notes)})
        log("pipe sizes: {} crossing(s) near ({:.0f}, {:.0f})".format(len(rs), box[0], box[1]))


@_records_run("crossings")
def run_crossings(roads, streams, out_gpkg, known=None, rails=None, bbox=None, fills=None,
                  match_ft=250.0, unsure_factor=2.0, fill_ft=200.0, dem=None, z_feet=None,
                  c_runoff=0.35, intensity_in_hr=2.0, v_fps=8.0, drain_cell=10.0, tile_m=8000.0,
                  log=print, cancelled=None):
    """Every road / rail x stream crossing, checked against the known bridges / culverts /
    crossings. `fills`: an embankments layer from run_embankments; the nearest fill within fill_ft
    is copied (a 'road fill (ponding)' there = the culvert is missing in the DEM, small or
    blocked). With `dem`: the drainage area above each crossing and the round pipe the rational
    method asks for (c_runoff and intensity_in_hr are YOUR values; core/crossings.py).
    Returns {'rows', 'counts', ...}."""
    cancelled = cancelled or _noop
    sl = read_lines(streams, bbox)
    rl = read_lines(roads, bbox) + (read_lines(rails, bbox) if rails else [])
    kn = read_known(known)
    hits = cx.find(rl, sl, cancelled=cancelled)
    fk = None
    if fills:
        ds, lyr = open_layer(fills)
        ct = _transform(lyr, _srs(WORK_EPSG))
        pts = []
        for f in lyr:
            g = f.GetGeometryRef().Clone()
            g.Transform(ct)
            pts.append((g.GetX(), g.GetY(), "{}|{}".format(f.GetField("kind"), f.GetField("height_ft")), "fill"))
        ds = None
        fk = ls.Known(pts)
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    rows = []
    for h in hits:
        st = ls.check(h["x"], h["y"], kn, match_ft * FT, unsure_factor)
        lon, lat = _lonlat(h["x"], h["y"], ll)
        row = {"x": h["x"], "y": h["y"], "find_id": find_id("XING", h["x"], h["y"]), "status": st["status"],
               "known_id": st["known_id"], "known_layer": st["known_src"],
               "known_state": st.get("known_state", ""),
               "known_ft": None if st["known_m"] is None else round(st["known_m"] / FT, 0),
               "angle_deg": h["angle"], "lat": round(lat, 6), "lon": round(lon, 6)}
        if fk is not None:
            near = fk.near(h["x"], h["y"], fill_ft * FT)
            if near:
                kind, _, hft = fk.pts[near[0][1]][2].partition("|")
                row["fill_kind"] = kind
                try:
                    row["fill_height_ft"] = float(hft)
                except ValueError:
                    pass
        rows.append(row)
    if dem and not cancelled():
        z_feet = check_dem_unit(dem, z_feet, log)
        _size_crossings(rows, dem, z_feet, c_runoff, intensity_in_hr, v_fps, drain_cell, tile_m, log, cancelled)
    rows.sort(key=lambda r: {"new": 0, "unsure": 1, "listed": 2}[r["status"]])
    out_csv = os.path.splitext(out_gpkg)[0] + "_crossings.csv"
    write_points(out_gpkg, "crossings", CROSS_FIELDS, rows)
    write_csv(out_csv, CROSS_FIELDS, rows)
    log("{} stream line(s), {} road / rail line(s), {} known structure(s)".format(len(sl), len(rl), len(kn)))
    log(_summary(rows, "road / rail x stream crossing(s)"))
    return {"rows": rows, "counts": {"status": ls.status_counts(rows)}, "gpkg": out_gpkg, "csv": out_csv,
            "cancelled": bool(cancelled())}


# ------------------------------------------------------------------------------------ water --
WATER_FIELDS = [("water_id", "str"), ("area_ac", "float"), ("ndwi", "float"), ("wet_share", "float"),
                ("images", "int"), ("mapped", "str"), ("mapped_share", "float"), ("lat", "float"),
                ("lon", "float")]


def is_service(src):
    s = str(src or "").strip().lower()
    return s.startswith("http://") or s.startswith("https://")


def fetch_imageserver(url, ext, cell, folder, tile_px=2000, log=print, opener=None):
    """Download an ArcGIS ImageServer (exportImage) over ext (EPSG:5070) at cell metres as
    GeoTIFF tiles in folder (2000 px: the ND GIS Hub image services answer 500 to 3000 px);
    returns a VRT of them. `url` is the service address
    (.../ImageServer). Tiles already there are reused (a rerun resumes). urllib honours the
    proxy environment; `opener(url) -> bytes` replaces it in tests."""
    import urllib.parse
    import urllib.request
    base = url.rstrip("/")
    if base.lower().endswith("/exportimage"):
        base = base[: -len("/exportImage")]
    os.makedirs(folder, exist_ok=True)
    step = tile_px * cell
    paths = []
    x = ext[0]
    while x < ext[2]:
        y = ext[1]
        while y < ext[3]:
            x1, y1 = min(x + step, ext[2]), min(y + step, ext[3])
            w = max(1, int(round((x1 - x) / cell)))
            h = max(1, int(round((y1 - y) / cell)))
            path = os.path.join(folder, "img_{:.0f}_{:.0f}_{:g}.tif".format(x, y, cell))
            if not os.path.exists(path):
                q = urllib.parse.urlencode({
                    "bbox": "{},{},{},{}".format(x, y, x + w * cell, y + h * cell), "bboxSR": WORK_EPSG,
                    "imageSR": WORK_EPSG, "size": "{},{}".format(w, h), "format": "tiff",
                    "interpolation": "RSP_BilinearInterpolation", "f": "image"})
                req = base + "/exportImage?" + q
                if opener is not None:
                    data = opener(req)
                else:
                    try:
                        with urllib.request.urlopen(req, timeout=300) as r:
                            data = r.read()
                    except Exception as e:                       # HTTPError, URLError, timeout
                        raise RuntimeError("the image service refused {}: {}".format(req, e)) from None
                if not data[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                    raise RuntimeError("the image service did not return a GeoTIFF for {}: {}".format(
                        req, data[:200]))
                with open(path + ".part", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".part", path)
                log("image tile {} ({} x {} px)".format(os.path.basename(path), w, h))
            paths.append(path)
            y += step
        x += step
    vrt = os.path.join(folder, "image.vrt")
    _checked(gdal.BuildVRT(vrt, paths), "mosaicking the image tiles")
    return vrt


def read_bands(image, ext, cell, bands):
    """{band number: array} of a raster over ext (EPSG:5070) at cell metres (averaged)."""
    n_x = max(1, int(math.ceil((ext[2] - ext[0]) / cell)))
    n_y = max(1, int(math.ceil((ext[3] - ext[1]) / cell)))
    ds = _checked(gdal.Open(str(image)), "opening the image {}".format(image))
    if max(bands) > ds.RasterCount:
        raise RuntimeError("the image has {} band(s); band {} was asked for (a near-infrared band "
                           "is needed: 4-band or colour-infrared imagery)".format(ds.RasterCount, max(bands)))
    opts = dict(format="MEM", dstSRS="EPSG:{}".format(WORK_EPSG),
                outputBounds=(ext[0], ext[1], ext[0] + n_x * cell, ext[1] + n_y * cell),
                width=n_x, height=n_y, resampleAlg="average", outputType=gdal.GDT_Float32,
                dstNodata=-9999.0)
    wo.no_vshift(gdal, wo.no_overviews(gdal, opts))
    # a 4-band image's last band can be tagged alpha (GDAL 3.4 writes and reads a plain 4-band
    # byte GeoTIFF that way): gdalwarp would then use the NIR band as a mask and write 255
    opts["options"] = list(opts.get("options") or []) + ["-nosrcalpha"]
    out = _checked(gdal.Warp("", ds, **opts), "warping the image")
    res = {}
    for b in bands:
        a = out.GetRasterBand(b).ReadAsArray().astype("float64")
        a[a <= -9998.0] = np.nan
        res[b] = a
    return res, out.GetGeoTransform()


def _image_box(image):
    ds = _checked(gdal.Open(str(image)), "opening the image {}".format(image))
    gt = ds.GetGeoTransform()
    x0, y1 = gt[0], gt[3]
    x1, y0 = x0 + ds.RasterXSize * gt[1], y1 + ds.RasterYSize * gt[5]
    s = osr.SpatialReference()
    s.ImportFromWkt(ds.GetProjection())
    if hasattr(s, "SetAxisMappingStrategy"):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(s, _srs(WORK_EPSG))
    pts = [ct.TransformPoint(x, y)[:2] for x in (x0, x1) for y in (y0, y1)]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _mapped_polys(src, box):
    if not src:
        return []
    ds, lyr = open_layer(src)
    ct = _transform(lyr, _srs(WORK_EPSG))
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(ct)
        e = g.GetEnvelope()
        if e[1] < box[0] or e[0] > box[2] or e[3] < box[1] or e[2] > box[3]:
            continue
        out.extend(_rings(g))
    ds = None
    return out


def _built_mask(built, box, gt, shape, built_m, cell):
    """Bool grid True on (and within built_m of) a buildings / pavement / roads layer - buildings
    filled, roads as lines. None when no layer is given."""
    if not built:
        return None
    polys = _mapped_polys(built, box)                  # polygon interiors (buildings, lots)
    mask = _rasterize(polys, gt, shape) if polys else np.zeros(shape, bool)
    for ln in read_lines(built, None):                 # road / rail centrelines, polygon outlines
        if _meets(ln, box):
            for r, c in em.line_cells(ln, gt, shape):
                mask[r, c] = True
    k = max(1, int(round(built_m / cell)))
    return ndi.binary_dilation(mask, iterations=k)


@_records_run("water")
def run_water(image, out_gpkg, bbox=None, cell=1.0, green_band=2, nir_band=4, threshold=0.15,
              open_px=1, min_ac=0.05, max_ac=0.0, min_width_m=10.0, nir_frac=0.25, green_frac=0.35,
              built=None, built_m=5.0, mapped=None, tile_m=4000.0,
              las_src_epsg=None, las_classes=(li.WATER_CLASS,), las_min_pts=1,
              water_raster=None, water_raster_band=1, water_count_min=1,
              persist=0.5, log=print, progress=None, cancelled=None, opener=None):
    """Open-water polygons from 4-band / colour-infrared imagery: a raster file (or VRT) or an
    ArcGIS ImageServer address (then bbox is required; tiles are cached beside the output).
    `image` may also be a list of (source, green band, NIR band), one per year: a cell is then
    water when it is water in at least `persist` of the images that cover it (a pond is wet most
    years, a flooded field or a pothole only in a wet one).
    `mapped`: a water-body layer (3DHP / NHD / NWI); each patch gets mapped = yes when at least
    half of it lies on a mapped water body.
    `green_frac`: a cell dark in near-infrared is water only if its green is at least this much of
    the land's median green - drops the shadows (behind buildings, trees, bins) that are dark in
    every band and otherwise read as water in town. 0 turns the shadow test off.
    `built`: a buildings / pavement / roads layer (lines or polygons); water patches on it, within
    built_m, are dropped (roofs, streets and in-town pools). Writes layer 'water' (polygons,
    EPSG:5070) and a CSV. Returns {'rows', 'counts', 'gpkg', 'csv'}.
    `image` may instead be a classified LiDAR tile (.las/.laz) or a list of them: then there is no
    imagery at all - a cell is water when the point cloud classes it so (ASPRS class 9). The NDWI,
    shadow and built filters don't apply; `las_classes` is the water class set, `las_min_pts` the
    points a cell needs, and `las_src_epsg` the tile CRS when the file carries none.
    `water_raster`: a prebuilt class-9 water-count raster / VRT (band `water_raster_band`), e.g. the
    statewide class-9 LiDAR mosaic - a cell is water where the count is at least `water_count_min`.
    This is the scalable path when reading the raw LAS statewide is too much; no imagery, LAS, NDWI
    or shadow filters apply. Its own nodata (and the -999999 LiDAR nodata) read as no water."""
    progress = progress or _noop
    cancelled = cancelled or _noop
    if water_raster:
        return _run_water_raster(water_raster, int(water_raster_band), float(water_count_min),
                                 out_gpkg, bbox, cell, open_px, min_ac, max_ac, min_width_m,
                                 mapped, tile_m, log, progress, cancelled)
    raw = list(image) if isinstance(image, (list, tuple)) else [image]
    las_sources = [s if isinstance(s, str) else s[0] for s in raw]
    las_mode = bool(las_sources) and all(li.is_las(s) for s in las_sources)
    if las_mode:
        return _run_water_las(las_sources, out_gpkg, bbox, cell, las_src_epsg, tuple(las_classes),
                              las_min_pts, open_px, min_ac, max_ac, min_width_m, mapped, tile_m,
                              log, progress, cancelled)
    srcs = list(image) if isinstance(image, (list, tuple)) else [(image, green_band, nir_band)]
    if not srcs:
        raise RuntimeError("no imagery given")
    got = []
    for k, (src, gb, nb) in enumerate(srcs):
        if is_service(src):
            if bbox is None:
                raise RuntimeError("an image service needs an extent (Only this area)")
            folder = os.path.splitext(out_gpkg)[0] + "_imagery"
            if len(srcs) > 1:
                folder = os.path.join(folder, "{:08x}".format(zlib.crc32(src.encode("utf-8")) & 0xFFFFFFFF))
            log("downloading imagery from {} at {:g} m into {}".format(src, cell, folder))
            src = fetch_imageserver(src, bbox, cell, folder, log=log, opener=opener)
        got.append((src, int(gb), int(nb)))
    srcs = got
    image = srcs[0][0]
    ext = bbox or _image_box(image)
    min_px = max(1, int(round(min_ac * AC_M2 / (cell * cell))))
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    drv = _checked(ogr.GetDriverByName("GPKG"), "the GeoPackage driver")
    ds = ogr.Open(out_gpkg, 1) if os.path.exists(out_gpkg) else drv.CreateDataSource(out_gpkg)
    _checked(ds, "opening {} for writing".format(out_gpkg))
    for i in range(ds.GetLayerCount()):
        if ds.GetLayer(i).GetName() == "water":
            ds.DeleteLayer(i)
            break
    lyr = ds.CreateLayer("water", _srs(WORK_EPSG), ogr.wkbMultiPolygon)
    kinds = {"str": ogr.OFTString, "float": ogr.OFTReal, "int": ogr.OFTInteger}
    for name, t in WATER_FIELDS:
        lyr.CreateField(ogr.FieldDefn(name, kinds[t]))
    tiles = []
    x = ext[0]
    while x < ext[2]:
        y = ext[1]
        while y < ext[3]:
            tiles.append((x, y, min(x + tile_m, ext[2]), min(y + tile_m, ext[3])))
            y += tile_m
        x += tile_m
    rows = []
    mem = ogr.GetDriverByName("Memory") or ogr.GetDriverByName("MEM")      # GDAL < / >= 3.11
    for ti, core in enumerate(tiles):
        if cancelled():
            break
        pad = 300.0                                 # a pond across a tile edge is whole in one tile
        box = (core[0] - pad, core[1] - pad, core[2] + pad, core[3] + pad)
        if len(srcs) == 1:
            bands, gt = read_bands(image, box, cell, (srcs[0][1], srcs[0][2]))
            idx = wt.ndwi(bands[srcs[0][1]], bands[srcs[0][2]])
            dark = wt.dark_in_nir(bands[srcs[0][2]], idx, nir_frac) if nir_frac else None
            bright = wt.bright_in_green(bands[srcs[0][1]], idx, green_frac) if green_frac else None
            reject = _built_mask(built, box, gt, idx.shape, built_m, cell)
            lab, n = wt.water_patches(idx, threshold, open_px, min_px, min_width_m / cell, dark,
                                      bright=bright, reject=reject)
            frac = None
        else:
            idx, frac, gt = wt.persistence([read_bands(sx, box, cell, (gb, nb)) + (gb, nb) for sx, gb, nb in srcs],
                                           threshold, nir_frac, green_frac)
            reject = _built_mask(built, box, gt, frac.shape, built_m, cell)
            lab, n = wt.water_patches(frac, persist, open_px, min_px, min_width_m / cell, None,
                                      reject=reject)
        if n == 0:
            continue
        _emit_water_patches(lab, n, idx, frac, gt, core, cell, mapped, box, max_ac, len(srcs),
                            lyr, mem, ll, rows)
        log("tile {}/{}: {} water patch(es)".format(ti + 1, len(tiles), n))
        progress(100.0 * (ti + 1) / len(tiles))
    ds = None
    out_csv = os.path.splitext(out_gpkg)[0] + "_water.csv"
    write_csv(out_csv, WATER_FIELDS, rows)
    unm = sum(1 for r in rows if r["mapped"] == "no")
    log("{} open-water patch(es) of {:g} ac or more: {} not on the water-body layer{}".format(
        len(rows), min_ac, unm, "" if mapped else " (no water-body layer given)"))
    return {"rows": rows, "counts": {"water": len(rows), "unmapped": unm}, "gpkg": out_gpkg,
            "csv": out_csv, "image": image}


def _emit_water_patches(lab, n, idx, frac, gt, core, cell, mapped, box, max_ac, n_images,
                        lyr, mem, ll, rows):
    """Polygonise the water labels of one tile, keep each patch in the tile whose core holds its
    first cell, and append its feature + row. Shared by the imagery and the LiDAR paths."""
    polys = _mapped_polys(mapped, box)
    mmask = _rasterize(polys, gt, lab.shape) if polys else None
    cells, mean, share = wt.patch_stats(lab, n, idx, mmask)
    wet = wt.patch_stats(lab, n, frac)[1] if frac is not None else None
    first = np.full(n + 1, -1, dtype=np.int64)
    flat = lab.ravel()
    nz = np.nonzero(flat)[0]
    first_idx = np.unique(flat[nz], return_index=True)
    first[first_idx[0]] = nz[first_idx[1]]
    src = gdal.GetDriverByName("MEM").Create("", lab.shape[1], lab.shape[0], 1, gdal.GDT_Int32)
    src.SetGeoTransform(gt)
    src.SetProjection(_srs(WORK_EPSG).ExportToWkt())
    src.GetRasterBand(1).WriteArray(lab)
    mds = mem.CreateDataSource("p")
    ml = mds.CreateLayer("p", _srs(WORK_EPSG), ogr.wkbPolygon)
    ml.CreateField(ogr.FieldDefn("lab", ogr.OFTInteger))
    band = src.GetRasterBand(1)
    gdal.Polygonize(band, band, ml, 0, [], callback=None)
    ml.ResetReading()                  # GDAL < 3.6: the iterator does not rewind after writing
    geoms = {}
    for f in ml:
        k = f.GetField("lab")
        g = f.GetGeometryRef().Clone()
        if k in geoms:
            geoms[k] = geoms[k].Union(g)
        else:
            geoms[k] = g
    for k, g in geoms.items():
        if k <= 0:
            continue
        r, c = divmod(int(first[k]), lab.shape[1])
        fx, fy = gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5]
        if not (core[0] <= fx < core[2] and core[1] <= fy < core[3]):
            continue
        area = cells[k] * cell * cell / AC_M2
        if max_ac and area > max_ac:
            continue
        pt = g.PointOnSurface()
        lon, lat = _lonlat(pt.GetX(), pt.GetY(), ll)
        row = {"water_id": find_id("WATER", pt.GetX(), pt.GetY()), "area_ac": round(area, 3),
               "ndwi": round(float(mean[k]), 3),
               "wet_share": round(float(wet[k]), 2) if wet is not None else 1.0, "images": n_images,
               "mapped": "yes" if share[k] >= 0.5 else "no",
               "mapped_share": round(float(share[k]), 2), "lat": round(lat, 6), "lon": round(lon, 6)}
        f = ogr.Feature(lyr.GetLayerDefn())
        for name, _t in WATER_FIELDS:
            f.SetField(name, row[name])
        f.SetGeometry(ogr.ForceToMultiPolygon(g))
        lyr.CreateFeature(f)
        rows.append(row)


def _run_water_las(sources, out_gpkg, bbox, cell, src_epsg, classes, min_pts, open_px, min_ac,
                   max_ac, min_width_m, mapped, tile_m, log, progress, cancelled):
    """Open water from classified LiDAR tiles (ASPRS class 9), tile by tile. Each spatial tile
    reads only the LAS files that overlap it, ORs their water cells, then runs the same patch
    filters (open, min size, min width) and output as the imagery path."""
    min_px = max(1, int(round(min_ac * AC_M2 / (cell * cell))))
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    bounds = []                                    # (path, (x0,y0,x1,y1) in work CRS)
    for s in sources:
        try:
            bounds.append((s, li.las_bounds(s, WORK_EPSG, src_epsg)))
        except Exception as e:
            log("skipping {}: {}".format(os.path.basename(str(s)), e))
    if not bounds:
        raise RuntimeError("no readable LAS/LAZ tiles")
    if bbox is not None:
        ext = tuple(bbox)
    else:
        ext = (min(b[1][0] for b in bounds), min(b[1][1] for b in bounds),
               max(b[1][2] for b in bounds), max(b[1][3] for b in bounds))
    drv = _checked(ogr.GetDriverByName("GPKG"), "the GeoPackage driver")
    ds = ogr.Open(out_gpkg, 1) if os.path.exists(out_gpkg) else drv.CreateDataSource(out_gpkg)
    _checked(ds, "opening {} for writing".format(out_gpkg))
    for i in range(ds.GetLayerCount()):
        if ds.GetLayer(i).GetName() == "water":
            ds.DeleteLayer(i)
            break
    lyr = ds.CreateLayer("water", _srs(WORK_EPSG), ogr.wkbMultiPolygon)
    kinds = {"str": ogr.OFTString, "float": ogr.OFTReal, "int": ogr.OFTInteger}
    for name, t in WATER_FIELDS:
        lyr.CreateField(ogr.FieldDefn(name, kinds[t]))
    tiles = []
    x = ext[0]
    while x < ext[2]:
        y = ext[1]
        while y < ext[3]:
            tiles.append((x, y, min(x + tile_m, ext[2]), min(y + tile_m, ext[3])))
            y += tile_m
        x += tile_m
    rows = []
    mem = ogr.GetDriverByName("Memory") or ogr.GetDriverByName("MEM")
    for ti, core in enumerate(tiles):
        if cancelled():
            break
        pad = 300.0
        box = (core[0] - pad, core[1] - pad, core[2] + pad, core[3] + pad)
        grid = gt = None
        for path, bb in bounds:
            if bb[2] <= box[0] or bb[0] >= box[2] or bb[3] <= box[1] or bb[1] >= box[3]:
                continue                                    # this tile does not overlap the box
            g, gt = li.water_grid(path, box, cell, WORK_EPSG, classes, min_pts, src_epsg, log)
            grid = g if grid is None else np.fmax(grid, g)  # water in any file wins (NaN ignored)
        if grid is None:
            continue
        idx = np.where(np.isfinite(grid), grid, np.nan)
        lab, n = wt.water_patches(idx, 0.5, open_px, min_px, min_width_m / cell)
        if n == 0:
            continue
        _emit_water_patches(lab, n, idx, None, gt, core, cell, mapped, box, max_ac, len(sources),
                            lyr, mem, ll, rows)
        log("tile {}/{}: {} water patch(es)".format(ti + 1, len(tiles), n))
        progress(100.0 * (ti + 1) / len(tiles))
    ds = None
    out_csv = os.path.splitext(out_gpkg)[0] + "_water.csv"
    write_csv(out_csv, WATER_FIELDS, rows)
    unm = sum(1 for r in rows if r["mapped"] == "no")
    log("{} open-water patch(es) of {:g} ac or more from LiDAR: {} not on the water-body "
        "layer{}".format(len(rows), min_ac, unm, "" if mapped else " (no water-body layer given)"))
    return {"rows": rows, "counts": {"water": len(rows), "unmapped": unm}, "gpkg": out_gpkg,
            "csv": out_csv, "image": sources}


def _run_water_raster(src, band, count_min, out_gpkg, bbox, cell, open_px, min_ac, max_ac,
                      min_width_m, mapped, tile_m, log, progress, cancelled):
    """Open water from a prebuilt class-9 water-count raster / VRT (e.g. the statewide LiDAR water
    mosaic): a cell is water where its count is at least `count_min`. Same tiling, patch filters
    and output as the imagery and LiDAR paths; reuses read_bands (which warps to the work grid and
    reads the source's nodata, so -999999 and the raster's own nodata come through as no water)."""
    min_px = max(1, int(round(min_ac * AC_M2 / (cell * cell))))
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    ext = tuple(bbox) if bbox is not None else _image_box(src)
    drv = _checked(ogr.GetDriverByName("GPKG"), "the GeoPackage driver")
    ds = ogr.Open(out_gpkg, 1) if os.path.exists(out_gpkg) else drv.CreateDataSource(out_gpkg)
    _checked(ds, "opening {} for writing".format(out_gpkg))
    for i in range(ds.GetLayerCount()):
        if ds.GetLayer(i).GetName() == "water":
            ds.DeleteLayer(i)
            break
    lyr = ds.CreateLayer("water", _srs(WORK_EPSG), ogr.wkbMultiPolygon)
    kinds = {"str": ogr.OFTString, "float": ogr.OFTReal, "int": ogr.OFTInteger}
    for name, t in WATER_FIELDS:
        lyr.CreateField(ogr.FieldDefn(name, kinds[t]))
    tiles = []
    x = ext[0]
    while x < ext[2]:
        y = ext[1]
        while y < ext[3]:
            tiles.append((x, y, min(x + tile_m, ext[2]), min(y + tile_m, ext[3])))
            y += tile_m
        x += tile_m
    rows = []
    mem = ogr.GetDriverByName("Memory") or ogr.GetDriverByName("MEM")
    for ti, core in enumerate(tiles):
        if cancelled():
            break
        pad = 300.0
        box = (core[0] - pad, core[1] - pad, core[2] + pad, core[3] + pad)
        bands, gt = read_bands(src, box, cell, (band,))
        counts = bands[band]
        idx = np.where(np.isnan(counts), np.nan, np.where(counts >= count_min, 1.0, 0.0))
        lab, n = wt.water_patches(idx, 0.5, open_px, min_px, min_width_m / cell)
        if n == 0:
            continue
        _emit_water_patches(lab, n, idx, None, gt, core, cell, mapped, box, max_ac, 1,
                            lyr, mem, ll, rows)
        log("tile {}/{}: {} water patch(es)".format(ti + 1, len(tiles), n))
        progress(100.0 * (ti + 1) / len(tiles))
    ds = None
    out_csv = os.path.splitext(out_gpkg)[0] + "_water.csv"
    write_csv(out_csv, WATER_FIELDS, rows)
    unm = sum(1 for r in rows if r["mapped"] == "no")
    log("{} open-water patch(es) of {:g} ac or more from the water-count raster: {} not on the "
        "water-body layer{}".format(len(rows), min_ac, unm, "" if mapped else " (no water-body layer given)"))
    return {"rows": rows, "counts": {"water": len(rows), "unmapped": unm}, "gpkg": out_gpkg,
            "csv": out_csv, "image": src}


# ------------------------------------------------------------------ build the water mosaic --

def _snap_box(box, cell, margin=0):
    """Grow a tile's extent out to the global `cell` grid anchored at the CRS origin, so every
    tile's pixels fall on the same lines and the per-tile GeoTIFFs mosaic into one VRT with no
    seams. `margin` adds that many nodata cells of border on every side (so a water patch never
    sits on the raster's own edge, where the reader's per-tile dedup would drop it). Each edge
    stays a whole multiple of `cell`, so the margin keeps the global grid alignment."""
    x0 = (math.floor(box[0] / cell) - margin) * cell
    y0 = (math.floor(box[1] / cell) - margin) * cell
    # +1 cell so a point exactly on the max edge (col/row == n) still lands inside the grid
    x1 = (math.floor(box[2] / cell) + 1 + margin) * cell
    y1 = (math.floor(box[3] / cell) + 1 + margin) * cell
    return x0, y0, x1, y1


def _write_grid(path, arr, gt, epsg, gdal_type, nodata):
    """One-band GeoTIFF, compressed, with the given geotransform, CRS and nodata."""
    drv = _checked(gdal.GetDriverByName("GTiff"), "the GeoTIFF driver")
    ny, nx = arr.shape
    opts = ["COMPRESS=DEFLATE", "ZLEVEL=9", "PREDICTOR=2", "TILED=YES", "SPARSE_OK=TRUE"]
    out = _checked(drv.Create(path, nx, ny, 1, gdal_type, options=opts),
                   "creating {}".format(path))
    out.SetGeoTransform(gt)
    out.SetProjection(_srs(epsg).ExportToWkt())
    b = out.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    b.WriteArray(arr)
    b.FlushCache()
    out = None


def build_water_mosaic(sources, out_dir, cell=1.0, classes=(li.WATER_CLASS,), mosaic_epsg=WORK_EPSG,
                       src_epsg=None, prefix="water", point_counts=False, log=print, progress=lambda p: None,
                       cancelled=lambda: False):
    """Turn a folder of classified LiDAR tiles (ASPRS class 9) into the prebuilt water mosaic the
    open-water tool reads with its WATER_RASTER input: a per-tile **water-count** GeoTIFF (how many
    class-9 points fall in each `cell`-metre cell) and a **mean-elevation** GeoTIFF (the mean z of
    those points, the pond water surface), then a `water_count.vrt` / `water_elev.vrt` over them.

    This is the scalable path: the reader cannot hold a statewide raw LiDAR point cloud live (it is
    terabytes), but it can read this once-built mosaic a spatial tile at a time. The builder is the
    one place the suite rasterises the LAS water class; it reuses core/lidar (no outside process, so
    DamIT-safe). Every tile snaps to one global grid so the VRT has no seams. `mosaic_epsg` is the
    output CRS (the regional metric grid: ND UTM for DamSafety, CONUS Albers for the U.S.-wide
    DamIT build). `count` 0 is stored as the count raster's nodata and the mean-elevation nodata is
    -999999, so dry land and the gaps between tiles both read back as no water.

    point_counts (Finder 0.5): also a per-tile count of ALL returns per cell and `point_count.vrt`
    over them - water swallows the pulses, so a pond under 2 acres (neither flattened nor classed
    water in 3DEP) shows as a patch of empty cells; the evidence tool reads it (LiDAR drop-out).

    Returns {"count_vrt", "elev_vrt", "tiles": [per-tile dicts], "n_tiles", "n_wet_tiles",
    "points_vrt" (None without point_counts)}.
    """
    os.makedirs(out_dir, exist_ok=True)
    srcs = [sources] if isinstance(sources, str) else list(sources)
    count_tifs, elev_tifs, tiles, point_tifs = [], [], [], []
    for si, path in enumerate(srcs):
        if cancelled():
            break
        name = os.path.splitext(os.path.basename(str(path)))[0]
        try:
            x, y, z, cls = li.tile_points(path, mosaic_epsg, None if point_counts else tuple(classes), src_epsg)
        except Exception as e:
            log("skipping {}: {}".format(os.path.basename(str(path)), e))
            continue
        if point_counts and x.size:
            pbox = _snap_box((float(np.min(x)), float(np.min(y)), float(np.max(x)), float(np.max(y))),
                             cell, margin=2)
            pnx, pny, pgt = li._grid_for(pbox, cell)
            total, _mz = li.rasterize_counts(x, y, z, cls, tuple(int(c) for c in np.unique(cls)), pgt, (pny, pnx))
            p_tif = os.path.join(out_dir, "points_count_{}.tif".format(name))
            _write_grid(p_tif, total, pgt, mosaic_epsg, gdal.GDT_Int32, 0)
            point_tifs.append(p_tif)
            keep = np.isin(cls, np.asarray(list(classes), dtype=cls.dtype))
            x, y, z, cls = x[keep], y[keep], z[keep], cls[keep]
        if x.size == 0:
            log("{}: no class-{} points".format(name, ",".join(str(c) for c in classes)))
            continue
        box = _snap_box((float(np.min(x)), float(np.min(y)), float(np.max(x)), float(np.max(y))),
                        cell, margin=2)
        nx, ny, gt = li._grid_for(box, cell)
        count, meanz = li.rasterize_counts(x, y, z, cls, tuple(classes), gt, (ny, nx))
        wet = int((count > 0).sum())
        if wet == 0:
            log("{}: no water cells".format(name))
            continue
        c_tif = os.path.join(out_dir, "{}_count_{}.tif".format(prefix, name))
        e_tif = os.path.join(out_dir, "{}_elev_{}.tif".format(prefix, name))
        _write_grid(c_tif, count, gt, mosaic_epsg, gdal.GDT_Int32, 0)
        elev = np.where(np.isfinite(meanz), meanz, -999999.0).astype(np.float32)
        _write_grid(e_tif, elev, gt, mosaic_epsg, gdal.GDT_Float32, -999999.0)
        count_tifs.append(c_tif)
        elev_tifs.append(e_tif)
        tiles.append({"tile": name, "wet_cells": wet, "count": c_tif, "elev": e_tif})
        log("{}: {} water cell(s)".format(name, wet))
        progress(100.0 * (si + 1) / max(1, len(srcs)))
    points_vrt = None
    if point_tifs:
        points_vrt = os.path.join(out_dir, "point_count.vrt")
        _checked(gdal.BuildVRT(points_vrt, point_tifs), "mosaicking the return-count tiles")
        log("return-count mosaic: {} tile(s) -> {}".format(len(point_tifs), os.path.basename(points_vrt)))
    if not count_tifs:
        if points_vrt:
            return {"count_vrt": None, "elev_vrt": None, "tiles": tiles, "n_tiles": len(srcs),
                    "n_wet_tiles": 0, "points_vrt": points_vrt}
        raise RuntimeError("no water tiles were built (no readable tiles with class-{} points)".format(
            ",".join(str(c) for c in classes)))
    count_vrt = os.path.join(out_dir, "water_count.vrt")
    elev_vrt = os.path.join(out_dir, "water_elev.vrt")
    _checked(gdal.BuildVRT(count_vrt, count_tifs), "mosaicking the water-count tiles")
    _checked(gdal.BuildVRT(elev_vrt, elev_tifs), "mosaicking the water-elevation tiles")
    log("water mosaic: {} tile(s) with water of {} read; {} -> {} and {}".format(
        len(count_tifs), len(srcs), out_dir, os.path.basename(count_vrt), os.path.basename(elev_vrt)))
    return {"count_vrt": count_vrt, "elev_vrt": elev_vrt, "tiles": tiles,
            "n_tiles": len(srcs), "n_wet_tiles": len(count_tifs), "points_vrt": points_vrt}


# --------------------------------------------------------------------------------- evidence --
EVID_FIELDS = [("img_years", "int"), ("wet_years", "int"), ("wet_share", "float"), ("first_wet", "int"),
               ("wet_list", "str"), ("bare_share", "float"), ("nwi_code", "str"), ("nwi_h", "str"),
               ("nwi_x", "str"), ("nwi_regime", "str"), ("in_quarry", "str"), ("pasture_share", "float"),
               ("soil_rating", "str"), ("lidar_pool_empty", "float"), ("lidar_ring_empty", "float"),
               ("lidar_contrast", "float"), ("lidar_class9", "float"), ("bld_1mi", "int"), ("bld_3mi", "int"),
               ("bld_5mi", "int"), ("roads_1mi", "int"), ("roads_3mi", "int"), ("roads_5mi", "int"),
               ("conseq", "float"), ("permit_check", "str")]
# CDL classes a stock pond sits among: 176 grassland / pasture, 37 other hay, 36 alfalfa,
# 152 shrubland, 195 herbaceous wetlands
PASTURE_CLASSES = (176, 37, 36, 152, 195)
_IMG_KEYS = ("green", "nir", "red", "blue")


def parse_images(text):
    """Imagery for the water history, one per line: 'YEAR | source | green=G nir=N [red=R blue=B]'
    (source = a raster file or an ArcGIS ImageServer address). Returns [{year, src, green, nir,
    red, blue}]. Check a service's band order (its keyProperties): the ND GIS Hub colour-infrared
    years are NIR, green, blue (nir=1 green=2), not the classic NIR, red, green; 4-band NAIP is
    red=1 green=2 blue=3 nir=4."""
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [q.strip() for q in line.split("|")]
        if len(parts) < 3:
            raise ValueError("an image line needs 'YEAR | source | green=G nir=N': {}".format(line))
        d = {"year": int(parts[0]), "src": parts[1]}
        for kv in parts[2].replace(",", " ").split():
            k, _, v = kv.partition("=")
            if k.strip().lower() in _IMG_KEYS:
                d[k.strip().lower()] = int(v)
        if "green" not in d or "nir" not in d:
            raise ValueError("an image line needs green= and nir= bands: {}".format(line))
        out.append(d)
    return out


def _read_grid(src, ext, cell, alg="near"):
    """(band-1 array, NaN = no data; geotransform) of a raster over ext (EPSG:5070) - class
    rasters (land cover) and count rasters keep their values (nearest), not an average."""
    n_x = max(1, int(math.ceil((ext[2] - ext[0]) / cell)))
    n_y = max(1, int(math.ceil((ext[3] - ext[1]) / cell)))
    ds = _checked(gdal.Open(str(src)), "opening {}".format(src))
    nod = ds.GetRasterBand(1).GetNoDataValue()
    opts = dict(format="MEM", dstSRS="EPSG:{}".format(WORK_EPSG),
                outputBounds=(ext[0], ext[1], ext[0] + n_x * cell, ext[1] + n_y * cell),
                width=n_x, height=n_y, resampleAlg=alg, dstNodata=-999999.0,
                outputType=gdal.GDT_Float64)
    if nod is not None:
        opts["srcNodata"] = nod
    wo.no_vshift(gdal, wo.no_overviews(gdal, opts))
    out = _checked(gdal.Warp("", ds, **opts), "warping {}".format(src))
    a = out.GetRasterBand(1).ReadAsArray().astype("float64")
    a[(a <= -999998.0) | ~np.isfinite(a)] = np.nan
    return a, out.GetGeoTransform()


def _data_px(path, depth=0, missing=None):
    """[(x0, y0, x1, y1, leaf)] pixel boxes of a raster that hold data, each with the file that
    holds them: the whole raster (itself), or for a VRT each source's DstRect (the whole raster
    when a source has none) - and for a source that is itself a VRT, its own sources' boxes mapped
    through SrcRect -> DstRect (a VRT of per-project VRTs keeps the gaps between tiles). A VRT
    with no sources (a warped VRT) is one box. A nested VRT that cannot be opened adds no box
    (named in `missing`): the other sources still count."""
    ds = _checked(gdal.Open(str(path)), "opening {}".format(path))
    nx, ny = float(ds.RasterXSize), float(ds.RasterYSize)
    whole = [(0.0, 0.0, nx, ny, str(path))]
    if ds.GetDriver().ShortName != "VRT" or depth > 8:
        return whole
    import xml.etree.ElementTree as ET
    band = ET.fromstring(ds.GetMetadata("xml:VRT")[0]).find("VRTRasterBand")
    srcs = [el for el in (band if band is not None else []) if el.find("SourceFilename") is not None]
    if not srcs:
        return whole
    base, out = os.path.dirname(os.path.abspath(str(path))), []
    for el in srcs:
        fn = el.find("SourceFilename")
        name = (fn.text or "").strip()
        if fn.get("relativeToVRT") == "1":
            name = os.path.join(base, name)
        dr, sr = el.find("DstRect"), el.find("SrcRect")
        dx, dy, dw, dh = ((float(dr.get(k)) for k in ("xOff", "yOff", "xSize", "ySize")) if dr is not None
                          else (0.0, 0.0, nx, ny))
        if not name.lower().endswith(".vrt"):
            out.append((dx, dy, dx + dw, dy + dh, name))
            continue
        try:
            inner = _data_px(name, depth + 1, missing)
            ids = gdal.Open(name)
            iw, ih = float(ids.RasterXSize), float(ids.RasterYSize)
            ids = None
        except Exception:
            if missing is not None:
                missing.add(name)
            continue
        sx, sy, sw, sh = ((float(sr.get(k)) for k in ("xOff", "yOff", "xSize", "ySize")) if sr is not None
                          else (0.0, 0.0, iw, ih))
        if sw <= 0 or sh <= 0:
            continue
        fx, fy = dw / sw, dh / sh
        for x0, y0, x1, y1, leaf in inner:
            x0, y0, x1, y1 = max(x0, sx), max(y0, sy), min(x1, sx + sw), min(y1, sy + sh)
            if x0 < x1 and y0 < y1:
                out.append((dx + (x0 - sx) * fx, dy + (y0 - sy) * fy, dx + (x1 - sx) * fx, dy + (y1 - sy) * fy, leaf))
    return out


def _layout(src, cache):
    """([(box in the raster's CRS (xa, ya, xb, yb), leaf file)], work -> raster CRS transform) of
    a raster, parsed once per run (cache: a dict kept for one run)."""
    key = str(src)
    if key not in cache:
        ds = _checked(gdal.Open(key), "opening {}".format(src))
        gt = ds.GetGeoTransform()
        leaves = []
        for x0, y0, x1, y1, leaf in _data_px(key, missing=cache.setdefault("_missing", set())):
            xa, xb = gt[0] + x0 * gt[1], gt[0] + x1 * gt[1]
            ya, yb = gt[3] + y0 * gt[5], gt[3] + y1 * gt[5]
            leaves.append(((min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)), leaf))
        srs = osr.SpatialReference()
        srs.ImportFromWkt(ds.GetProjection() or _srs(WORK_EPSG).ExportToWkt())
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        cache[key] = (leaves, osr.CoordinateTransformation(_srs(WORK_EPSG), srs))
        ds = None
    return cache[key]


def _cell_points(ext, cell, ct):
    """The cell centres of the grid _read_grid lays over ext, in the raster's CRS, and its shape."""
    n_x = max(1, int(math.ceil((ext[2] - ext[0]) / cell)))
    n_y = max(1, int(math.ceil((ext[3] - ext[1]) / cell)))
    cxs = ext[0] + (np.arange(n_x) + 0.5) * cell
    cys = ext[1] + n_y * cell - (np.arange(n_y) + 0.5) * cell
    gx, gy = np.meshgrid(cxs, cys)
    return np.array(ct.TransformPoints(np.c_[gx.ravel(), gy.ravel()].tolist()))[:, :2], (n_y, n_x)


def _in_box(pts, b):
    return (pts[:, 0] >= b[0]) & (pts[:, 0] <= b[2]) & (pts[:, 1] >= b[1]) & (pts[:, 1] <= b[3])


def _near_leaves(leaves, pts):
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    return [(b, leaf) for b, leaf in leaves if b[2] >= lo[0] and b[0] <= hi[0] and b[3] >= lo[1] and b[1] <= hi[1]]


def _coverage(src, ext, cell, cache=None):
    """Bool grid (as _read_grid lays it out over ext) of the cells a raster has data for: inside
    one of a VRT's source tiles (from the VRT's layout, nested VRTs followed), or inside the
    raster. A count raster stores 0 as nodata (or has none, and a VRT fills its gaps with 0), so
    the values alone cannot tell a cell with no returns from a cell no survey reached. cache: a
    dict kept for one run (the layout is parsed once per source, and a rebuilt VRT is re-read
    on the next run)."""
    leaves, ct = _layout(src, {} if cache is None else cache)
    pts, shape = _cell_points(ext, cell, ct)
    inside = np.zeros(len(pts), bool)
    for b, _leaf in _near_leaves(leaves, pts):
        inside |= _in_box(pts, b)
    return inside.reshape(shape)


def _surveyed(src, box, reach, shape, cache=None):
    """Bool grid (1 m, as _read_grid lays it out over box) of the cells a LiDAR survey covered:
    for each source tile, the cells inside its box AND inside a closing (radius `reach`) of ITS
    OWN cells with returns; any tile's. A tile's box also holds its nodata margin, the
    unsurveyed part of a tile cut by a project edge and the slivers of a tile warped from
    another UTM zone; the closing fills a void (a pond the pool and ring could hold stays
    surveyed-but-empty) and leaves those out - and, taken tile by tile, it never bridges a gap
    between two surveys. Read over a window 2 x reach wider, so the closing is exact on box."""
    cache = {} if cache is None else cache
    g = int(math.ceil(2.0 * reach))
    wbox = (box[0] - g, box[1] - g, box[2] + g, box[3] + g)
    leaves, ct = _layout(src, cache)
    pts, wshape = _cell_points(wbox, 1.0, ct)
    cov = np.zeros(wshape, bool)
    pad = int(math.ceil(2.0 * reach)) + 1
    for b, leaf in _near_leaves(leaves, pts):
        inb = _in_box(pts, b).reshape(wshape)
        if not inb.any():
            continue
        try:
            pw, _gw = _read_grid(leaf, wbox, 1.0)
        except Exception:
            cache.setdefault("_missing", set()).add(leaf)
            continue
        if pw.shape != wshape:
            continue
        occ = np.isfinite(pw) & (pw > 0) & inb
        if not occ.any():
            continue
        rr, cc = np.nonzero(inb)
        r0, r1 = max(0, rr.min() - pad), min(wshape[0], rr.max() + pad + 1)
        c0, c1 = max(0, cc.min() - pad), min(wshape[1], cc.max() + pad + 1)
        o = occ[r0:r1, c0:c1]
        dil = ndi.distance_transform_edt(~o) <= reach
        foot = ndi.distance_transform_edt(np.pad(dil, 1))[1:-1, 1:-1] > reach
        cov[r0:r1, c0:c1] |= foot & inb[r0:r1, c0:c1]
    out = cov[g:g + shape[0], g:g + shape[1]]
    if out.shape != tuple(shape):                       # a rounding cell at the edge
        o2 = np.zeros(shape, bool)
        o2[:out.shape[0], :out.shape[1]] = out[:shape[0], :shape[1]]
        out = o2
    return out


def _polys_with(src, box, field=None):
    """[(rings, value of `field` (or ''), envelope)] of the polygons of a layer meeting box."""
    if not src:
        return []
    ds, lyr = open_layer(src)
    ct = _transform(lyr, _srs(WORK_EPSG))
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())]
    if field is None:
        for cand in ("ATTRIBUTE", "attribute", "WETLAND_TYPE"):
            if cand in names:
                field = cand
                break
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(ct)
        e = g.GetEnvelope()
        if e[1] < box[0] or e[0] > box[2] or e[3] < box[1] or e[2] > box[3]:
            continue
        val = f.GetField(field) if field and field in names else ""
        for rings in _rings(g):
            out.append((rings, "" if val is None else str(val), e))
    ds = None
    return out


def _declared_type(k):
    """The type a field of the Finder's own layers is declared with ('str' when none is)."""
    for fl in ("EMB_FIELDS", "EVID_FIELDS", "REVIEW_FIELDS"):
        t = dict(globals().get(fl, ())).get(k)
        if t:
            return t
    return "str"


def _fields_of(rows, skip=(), first=()):
    """[(name, type)] of every key the rows carry (after `first`), each typed by its first value
    that is not None (a field blank in row 1 is not written as text), or by its declared type
    when no row has a value (a numeric field all blank this run stays numeric)."""
    fields = list(first)
    have = set(skip) | {k for k, _t in fields}
    for r in rows:
        for k in r:
            if k in have:
                continue
            have.add(k)
            v = next((q[k] for q in rows if q.get(k) is not None), None)
            if v is None:                               # blank in every row: the declared type
                fields.append((k, _declared_type(k)))
                continue
            fields.append((k, "float" if isinstance(v, float) else
                           ("int" if isinstance(v, int) and not isinstance(v, bool) else "str")))
    return fields


def _poly_at(polys, x, y, near_m=0.0):
    """The value of the polygon holding (x, y); else of the nearest one whose box is within
    near_m; None when none."""
    best, bd = None, None
    for rings, val, e in polys:
        if e[0] - near_m <= x <= e[1] + near_m and e[2] - near_m <= y <= e[3] + near_m:
            if ls.point_in_polygon(x, y, rings):
                return val
            if near_m > 0:
                d = ls.dist_to_polygon(x, y, rings)
                if d <= near_m and (bd is None or d < bd):
                    best, bd = val, d
    return best


def _points_of(src, box):
    """[(x, y)] in EPSG:5070 of a point / polygon layer (polygons by their centroid) in box."""
    if not src:
        return []
    ds, lyr = open_layer(src)
    ct = _transform(lyr, _srs(WORK_EPSG))
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(ct)
        c = g.Centroid() if ogr.GT_Flatten(g.GetGeometryType()) != ogr.wkbPoint else g
        x, y = c.GetX(), c.GetY()
        if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
            out.append((x, y))
    ds = None
    return out


@_records_run("evidence")
def run_evidence(fills, out_gpkg, images=None, image_cell=1.0, ndwi_threshold=0.15, nwi=None, nwi_field=None,
                 quarries=None, landcover=None, pasture_classes=PASTURE_CLASSES, soils=None, soil_field=None,
                 lidar_points=None, lidar_water=None, dem=None, z_feet=None, buildings=None, roads=None,
                 reach_m=ev.CONSEQ_BINS_M[-1], flow_cell=30.0, cache=None, log=print,
                 progress=None, cancelled=None):
    """Evidence the DEM cannot give for each fill of an 'embankments' layer (core/evidence.py):
    open water in the pool year by year (`images`: parse_images lines or dicts), bright bare
    ground, the NWI code (h / x modifiers, water regime), a quarry layer, the pasture share of a
    land-cover raster (CDL classes) within 250 m, a soil rating, LiDAR return drop-out in the
    pool (`lidar_points`: a per-cell count of all returns; `lidar_water`: the class-9 count
    mosaic) and, with a `dem`, the buildings and roads down the flow path (consequence, a second
    sort). Every source is optional. Writes the fills with the evidence fields to layer 'evidence'
    (and a CSV). Returns {'rows', 'gpkg', 'csv', 'counts'}."""
    progress = progress or _noop
    cancelled = cancelled or _noop
    rows = read_rows(fills)
    if not rows:
        raise RuntimeError("no fills in {}".format(fills))
    imgs = parse_images(images) if isinstance(images, str) else list(images or [])
    cache = cache or (os.path.splitext(out_gpkg)[0] + "_imagery")
    ll = osr.CoordinateTransformation(_srs(4326), _srs(WORK_EPSG))
    xs = [r["x"] for r in rows]
    ys = [r["y"] for r in rows]
    ext = (min(xs) - 1000.0, min(ys) - 1000.0, max(xs) + 1000.0, max(ys) + 1000.0)
    nwi_p = _polys_with(nwi, ext, nwi_field) if nwi else []
    q_p = _polys_with(quarries, ext) if quarries else []
    s_p = _polys_with(soils, ext, soil_field) if soils else []
    log("{} fill(s); {} image year(s), {} NWI polygon(s), {} quarry polygon(s), {} soil polygon(s)".format(
        len(rows), len(imgs), len(nwi_p), len(q_p), len(s_p)))
    cover = {}                                          # LiDAR layouts, parsed once this run
    for i, r in enumerate(rows):
        if cancelled():
            break
        cx, cy = r["x"], r["y"]
        try:
            if r.get("pool_lat") is not None and r.get("pool_lon") is not None:
                cx, cy = ll.TransformPoint(float(r["pool_lon"]), float(r["pool_lat"]))[:2]
        except (TypeError, ValueError):
            pass
        try:
            area = float(r.get("area_ac") or 0.0) * AC_M2
        except (TypeError, ValueError):
            area = 0.0
        rad = min(150.0, max(10.0, math.sqrt(area / math.pi)))
        box = (cx - rad - 40.0, cy - rad - 40.0, cx + rad + 40.0, cy + rad + 40.0)
        seen = {}
        for im in imgs:
            bands = [im[k] for k in _IMG_KEYS if k in im]
            yr = im["year"]
            try:
                src = im["src"]
                if is_service(src):
                    # one cache folder per year AND service: two services of one year (CIR and
                    # 4-band) must not read each other's tiles
                    tag = "{}_{:08x}".format(yr, zlib.crc32(str(src).encode()) & 0xFFFFFFFF)
                    src = fetch_imageserver(src, box, image_cell, os.path.join(cache, tag), log=lambda *a: None)
                b, gt = read_bands(src, box, image_cell, bands)
            except Exception as e:                          # a gap in the service, a dead tile
                log("{} {}: no image ({})".format(r.get("find_id"), yr, e))
                seen.setdefault(yr, None)
                continue
            m = ev.disk(b[im["nir"]].shape, gt, cx, cy, rad)
            w, _sh = ev.wet(ev.ndwi(b[im["green"]], b[im["nir"]]), m, ndwi_threshold)
            if w is not None and not seen.get(yr):         # one year, two images: water in either
                seen[yr] = bool(w)
            else:
                seen.setdefault(yr, None)
            if "red" in im and "blue" in im:
                v = ev.bare_share(b[im["red"]], b[im["green"]], b[im["blue"]], b[im["nir"]],
                                  ev.disk(m.shape, gt, cx, cy, rad + 20.0))
                if math.isfinite(v):                        # an image with no data here keeps the last
                    r["bare_share"] = round(v, 3)
        if imgs:
            h = ev.water_history(seen)
            r.update({"img_years": h["years"], "wet_years": h["wet_years"],
                      "wet_share": None if not h["years"] else round(h["wet_share"], 3),
                      "first_wet": h["first_wet"], "wet_list": h["wet_list"]})
        if nwi:
            code = _poly_at(nwi_p, cx, cy, rad)
            if code is None and (cx, cy) != (r["x"], r["y"]):
                code = _poly_at(nwi_p, r["x"], r["y"], 30.0)
            if code is not None:
                nw = ev.nwi_parse(code)
                r.update({"nwi_code": nw["code"], "nwi_h": "yes" if nw["h"] else "no",
                          "nwi_x": "yes" if nw["x"] else "no", "nwi_regime": nw["regime"]})
            else:
                r.update({"nwi_code": "", "nwi_h": "no", "nwi_x": "no", "nwi_regime": ""})
        if quarries:
            r["in_quarry"] = "yes" if _poly_at(q_p, r["x"], r["y"], 50.0) is not None else "no"
        if soils:
            v = _poly_at(s_p, r["x"], r["y"])
            r["soil_rating"] = v if v is not None else ""
        if landcover:
            try:
                lc, gl = _read_grid(landcover, (r["x"] - 260.0, r["y"] - 260.0, r["x"] + 260.0, r["y"] + 260.0), 10.0)
                r["pasture_share"] = round(ev.share_of_classes(lc, ev.disk(lc.shape, gl, r["x"], r["y"], 250.0)
                                                               & np.isfinite(lc), pasture_classes), 3)
            except Exception as e:
                log("{}: no land cover ({})".format(r.get("find_id"), e))
        if lidar_points:
            try:
                lbox = (cx - rad - 30.0, cy - rad - 30.0, cx + rad + 30.0, cy + rad + 30.0)
                pc, gp = _read_grid(lidar_points, lbox, 1.0)
                cov = _surveyed(lidar_points, lbox, rad + 25.0, pc.shape, cover)
                pc[np.isnan(pc) & cov] = 0.0                # a count raster stores 0 as nodata
                pc[~cov] = np.nan                           # no survey here: no evidence, not 'empty'
                wc = None
                if lidar_water:
                    wc, _g = _read_grid(lidar_water, lbox, 1.0)
                    wc[np.isnan(wc)] = 0.0                  # the water mosaic skips dry tiles
                    wc[~cov] = np.nan
                d = ev.dropout(pc, ev.disk(pc.shape, gp, cx, cy, rad), ev.ring(pc.shape, gp, cx, cy, rad + 5.0, rad + 25.0), wc)
                r.update({"lidar_pool_empty": _rnd(d["pool_empty"], 3), "lidar_ring_empty": _rnd(d["ring_empty"], 3),
                          "lidar_contrast": _rnd(d["contrast"], 3), "lidar_class9": _rnd(d["class9"], 3)})
            except Exception as e:
                log("{}: no LiDAR counts ({})".format(r.get("find_id"), e))
        try:
            st = float(r.get("storage_af"))
            r["permit_check"] = "yes" if st > ev.PERMIT_AF else "no"
        except (TypeError, ValueError):
            pass
        progress(80.0 * (i + 1) / len(rows))
    for name in sorted(cover.get("_missing", ())):
        log("warning: LiDAR source {} could not be read: the fills over it have no LiDAR evidence".format(name))
    if dem and (buildings or roads) and not cancelled():
        _consequences(rows, dem, z_feet, buildings, roads, reach_m, flow_cell, log, cancelled)
    fields = _fields_of(rows, skip={"x", "y"} | set(dict(EVID_FIELDS))) + EVID_FIELDS
    out_csv = os.path.splitext(out_gpkg)[0] + "_evidence.csv"
    write_points(out_gpkg, "evidence", fields, rows)
    write_csv(out_csv, fields, rows)
    counts = {"fills": len(rows),
              "wet_any": sum(1 for r in rows if (r.get("wet_years") or 0) > 0),
              "nwi_h": sum(1 for r in rows if r.get("nwi_h") == "yes"),
              "nwi_x": sum(1 for r in rows if r.get("nwi_x") == "yes"),
              "permit_check": sum(1 for r in rows if r.get("permit_check") == "yes")}
    log("evidence: {} fill(s); water seen in at least one year at {}; NWI impounded (h) {}, excavated (x) {}; "
        "{} over 12.5 ac-ft (check for a water permit)".format(counts["fills"], counts["wet_any"], counts["nwi_h"],
                                                             counts["nwi_x"], counts["permit_check"]))
    progress(100.0)
    return {"rows": rows, "gpkg": out_gpkg, "csv": out_csv, "counts": counts, "cancelled": bool(cancelled())}


def _consequences(rows, dem, z_feet, buildings, roads, reach_m, flow_cell, log, cancelled, tile_m=10000.0):
    """Buildings and roads down the flow path of each fill (core/evidence.py), routed over the
    DEM at flow_cell metres a 10 km tile (plus the reach) at a time."""
    z_feet = check_dem_unit(dem, z_feet, log)
    xs = [r["x"] for r in rows]
    ys = [r["y"] for r in rows]
    groups = {}
    for k, r in enumerate(rows):
        groups.setdefault((int((r["x"] - min(xs)) // tile_m), int((r["y"] - min(ys)) // tile_m)), []).append(k)
    for (gi, gj), ks in groups.items():
        if cancelled():
            break
        x0, y0 = min(xs) + gi * tile_m, min(ys) + gj * tile_m
        box = (x0 - reach_m, y0 - reach_m, x0 + tile_m + reach_m, y0 + tile_m + reach_m)
        z, gt = warp_dem(dem, box, flow_cell, z_feet)
        if not np.isfinite(z).any():
            continue
        rec, _order, _filled = tr.flow(z)
        blds = _points_of(buildings, box) if buildings else []
        bz = []
        for bx, by in blds:
            c = int((bx - gt[0]) / gt[1])
            rr = int((by - gt[3]) / gt[5])
            bz.append((bx, by, float(z[rr, c]) if 0 <= rr < z.shape[0] and 0 <= c < z.shape[1] else float("nan")))
        rl = read_lines(roads, box) if roads else []
        for k in ks:
            r = rows[k]
            c = int((r["x"] - gt[0]) / gt[1])
            rr = int((r["y"] - gt[3]) / gt[5])
            if not (0 <= rr < z.shape[0] and 0 <= c < z.shape[1]):
                continue
            path = ev.flow_path(rec, z.shape, rr * z.shape[1] + c)
            pxy, pd = ev.path_xy(path, z.shape, gt)
            keep = pd <= reach_m
            pxy, pd = pxy[keep], pd[keep]
            pz = z.ravel()[np.asarray(path)[keep]]
            try:
                h = float(r.get("height_ft") or 0.0) * FT
            except (TypeError, ValueError):
                h = 0.0
            bd = ev.flooded_buildings(pxy, pd, pz, bz, h, reach_m) if bz else []
            rd_ = ev.road_crossings(pxy, pd, rl) if rl else []
            try:
                saf = float(r.get("storage_af"))
            except (TypeError, ValueError):
                saf = float("nan")
            c_ = ev.consequence(bd, rd_, saf)
            c_.pop("permit_check", None)                # set from the storage alone, in run_evidence
            r.update(c_)


# ----------------------------------------------------------------------------- review batch --
REVIEW_FIELDS = [("n", "int"), ("find_id", "str"), ("stratum", "str"), ("rank", "int"), ("incl_prob", "float"),
                 ("second", "str"), ("kind", "str"), ("status", "str"), ("score", "float"), ("height_ft", "float"),
                 ("storage_af", "float"), ("area_ac", "float"), ("lat", "float"), ("lon", "float"),
                 ("list_n", "int"), ("pool_n", "int")]


@_records_run("review")
def run_review_batch(fills, out_gpkg, n=90, random_frac=0.18, top_frac=0.37, second_frac=0.12,
                     kinds=(em.K_DAM, em.K_DRY), reviewed=None, tile_m=5000.0, per_tile_cap=3, seed=0,
                     log=print):
    """A review batch (core/review.py) from scored fills: a fixed random control, the top-ranked
    unreviewed cards and cards near the cut, each with the chance it had of being picked, a share
    flagged for a blind second reviewer, in shuffled order. Only unlisted fills of `kinds`.
    reviewed: find_ids already marked (a list, or a CSV / text file of them). Writes layer
    'review_batch' and a CSV. Returns {'rows', 'gpkg', 'csv', 'counts'}."""
    from .core import review as rv
    rows = [r for r in read_rows(fills) if r.get("status") != "listed" and (not kinds or r.get("kind") in kinds)]
    if not rows:
        raise RuntimeError("no unlisted fills of kind {} in {}".format(", ".join(kinds), fills))
    if not any(r.get("score") is not None for r in rows):
        log("the fills have no score: the top and near-the-cut slices are by height")
        for r in rows:
            r["score"] = float(r.get("height_ft") or 0.0) / 100.0
    done = set()
    if isinstance(reviewed, str) and reviewed.strip():
        if not os.path.exists(reviewed):            # not a list of ids: a path that is not there
            raise RuntimeError("the reviewed-cards file {} does not exist".format(reviewed))
        with open(reviewed, encoding="utf-8-sig") as fh:
            done = {m.group(0) for m in re.finditer(r"FIND-[0-9A-F]{8}", fh.read())}
        if not done:
            log("{} holds no card ids (FIND-XXXXXXXX): nothing is left out as reviewed".format(reviewed))
    elif reviewed and not isinstance(reviewed, str):
        done = set(reviewed)
    got = rv.pick_batch(rows, n, random_frac, top_frac, second_frac, tile_m=tile_m, per_tile_cap=per_tile_cap,
                        seed=seed, reviewed=done)
    got.sort(key=lambda r: r["n"])
    for r in got:
        r["list_n"] = len(rows)
    # the batch fields first, then everything else the fills carry (a sheet shows them)
    fields = _fields_of(got, skip={"x", "y"}, first=REVIEW_FIELDS)
    out_csv = os.path.splitext(out_gpkg)[0] + "_review_batch.csv"
    write_points(out_gpkg, "review_batch", fields, got)
    write_csv(out_csv, fields, got)
    counts = {k: sum(1 for r in got if r["stratum"] == k) for k in ("random", "top", "near")}
    counts["second"] = sum(1 for r in got if r["second"] == "yes")
    want = min(int(n), len(rows) - sum(1 for r in rows if r.get("find_id") in done))
    if len(got) < want:
        log("warning: the batch has {} card(s), not {}: too few unreviewed candidates near the cut "
            "(or a per-tile cap of {} too tight for this area)".format(len(got), want, per_tile_cap))
    log("review batch: {} card(s) of {} candidate(s) - {} random control, {} top-ranked, {} near the cut; "
        "{} for a second reviewer".format(len(got), len(rows), counts["random"], counts["top"], counts["near"],
                                          counts["second"]))
    return {"rows": got, "gpkg": out_gpkg, "csv": out_csv, "counts": counts}


# ----------------------------------------------------------------------------- OpenStreetMap --
OSM_FIELDS = [("osm_id", "str"), ("kind", "str"), ("name", "str"), ("tags", "str"),
              ("lat", "float"), ("lon", "float")]
_OSM_HSTORE = re.compile(r'"((?:[^"\\]|\\.)*)"=>"((?:[^"\\]|\\.)*)"')


def osm_kind(tags):
    """The structure an OSM tag set marks, or None: dam / weir / dike / embankment / culvert / ford."""
    ww, mm = tags.get("waterway"), tags.get("man_made")
    if ww in ("dam", "weir"):
        return ww
    if mm == "dyke":
        return "dike"
    if mm == "embankment":
        return "embankment"
    if tags.get("tunnel") == "culvert":
        return "culvert"
    if tags.get("ford") == "yes":
        return "ford"
    return None


@_records_run("osm")
def osm_structures(src, out_gpkg, bbox=None, log=print):
    """Dams, weirs, dikes, embankments, culverts and fords of an OpenStreetMap file (.osm.pbf /
    .osm, the Geofabrik extract) as a point layer 'osm_structures' (EPSG:5070; a line or an area
    at its centre) to give the other tools as known structures. Returns 'out|layername=...'."""
    gdal.SetConfigOption("OGR_INTERLEAVED_READING", "YES")
    ds = _checked(gdal.OpenEx(str(src), gdal.OF_VECTOR), "opening the OpenStreetMap file {}".format(src))
    to5070 = osr.CoordinateTransformation(_srs(4326), _srs(WORK_EPSG))
    ll = osr.CoordinateTransformation(_srs(WORK_EPSG), _srs(4326))
    names = {}
    for i in range(ds.GetLayerCount()):
        lyr = ds.GetLayer(i)
        d = lyr.GetLayerDefn()
        names[lyr.GetName()] = [d.GetFieldDefn(k).GetName() for k in range(d.GetFieldCount())]
    rows, seen, t0 = [], 0, time.time()
    snips = ('"waterway"=>"dam"', '"waterway"=>"weir"', '"man_made"=>"dyke"', '"man_made"=>"embankment"',
             '"tunnel"=>"culvert"', '"ford"=>"yes"')
    while True:
        feat, lyr = ds.GetNextFeature()
        if feat is None:
            break
        seen += 1
        fn = names.get(lyr.GetName(), [])
        tags = {}
        for n in fn:
            if n in ("osm_id", "osm_way_id", "other_tags", "z_order"):
                continue
            v = feat.GetField(n)
            if v not in (None, ""):
                tags[n] = str(v)
        other = feat.GetField("other_tags") if "other_tags" in fn else None
        if other and any(s_ in other for s_ in snips):
            for k, v in _OSM_HSTORE.findall(other):
                tags[k] = v
        kind = osm_kind(tags)
        if not kind:
            continue
        g = feat.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = g.Clone()
        g.AssignSpatialReference(_srs(4326))
        c = g if ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbPoint else g.Centroid()
        c.Transform(to5070)
        x, y = c.GetX(), c.GetY()
        if bbox is not None and not (bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]):
            continue
        oid = feat.GetField("osm_id") if "osm_id" in fn else None
        oid = oid or (feat.GetField("osm_way_id") if "osm_way_id" in fn else None) or str(feat.GetFID())
        lon, lat = _lonlat(x, y, ll)
        rows.append({"x": x, "y": y, "osm_id": "{}/{}".format(lyr.GetName(), oid), "kind": kind,
                     "name": tags.get("name") or "", "tags": ";".join("{}={}".format(k, tags[k]) for k in (
                         "waterway", "man_made", "tunnel", "ford") if k in tags),
                     "lat": round(lat, 6), "lon": round(lon, 6)})
    ds = None
    gdal.SetConfigOption("OGR_INTERLEAVED_READING", None)
    write_points(out_gpkg, "osm_structures", OSM_FIELDS, rows)
    kinds = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    log("{:,} OpenStreetMap features read in {:.0f} s: {}".format(
        seen, time.time() - t0, ", ".join("{} {}".format(v, k) for k, v in sorted(kinds.items())) or "nothing"))
    return "{}|layername=osm_structures".format(out_gpkg)


# ------------------------------------------------------------------------------------ score --
def read_rows(src):
    """Rows (dicts, with x / y in EPSG:5070) of a point layer, e.g. an 'embankments' layer."""
    ds, lyr = open_layer(src)
    ct = _transform(lyr, _srs(WORK_EPSG))
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(ct)
        r = f.items()
        r["x"], r["y"] = g.GetX(), g.GetY()
        out.append(r)
    ds = None
    return out


SCORE_KINDS = ("v1", "0.5", "0.5-terrain", "0.5-trees")


def _marks_of(src):
    """Reviewers' marks: a folder of <find_id>.json ({"mark": ...}) or a CSV with find_id and mark
    columns -> {find_id: {"mark": ...}}."""
    out = {}
    if not src:
        return out
    if os.path.isdir(src):
        for f in os.listdir(src):
            if f.endswith(".json"):
                with open(os.path.join(src, f), encoding="utf-8") as fh:
                    out[f[:-5]] = json.load(fh)
        return out
    if not os.path.exists(src):
        raise RuntimeError("the marks file {} does not exist".format(src))
    with open(src, newline="", encoding="utf-8-sig") as fh:      # -sig: a spreadsheet's BOM
        rd_ = csv.DictReader(fh)
        cols = {c.strip().lower(): c for c in (rd_.fieldnames or [])}
        if "find_id" not in cols or "mark" not in cols:
            raise RuntimeError("the marks file {} needs find_id and mark columns (it has {})".format(
                src, ", ".join(rd_.fieldnames or []) or "none"))
        for r in rd_:
            fid = (r.get(cols["find_id"]) or "").strip()
            if fid:
                out[fid] = {"mark": (r.get(cols["mark"]) or "").strip().lower()}
    return out


@_records_run("score")
def run_score(fills, out_gpkg, positive_words=("dam",), model_in=None, model_out=None, folds=5,
              kind="v1", marks=None, marks_weight=20.0, log=print):
    """Learn a fill score from the fills already on a dam list (core/score.py) and score every
    fill. `fills`: one or more 'embankments' layers (runs over different areas). With model_in
    (a model JSON), score with it instead of learning. kind: 'v1' (the 11 measurements),
    '0.5' (every 0.5 measurement, one fill per listed dam) or '0.5-terrain' (also hinge terms and
    flat / rolling / cut copies) or '0.5-trees' (bagged PU boosted trees on the 0.5
    measurements). marks: reviewers' marks (a folder of <find_id>.json or a CSV of find_id, mark)
    - yes / no / unsure of fills in these layers become trusted labels at weight marks_weight
    (an unsure one as half a dam, half not; a mark replaces the fill's own listed / unlisted
    label; with trees the marks join every bag at that weight); learn them per region (a prairie
    model, a breaks model), not statewide.
    Writes 'scored_fills' and the model JSON (model_out, default beside the GeoPackage). Returns
    {'rows', 'model', 'auc', 'cv_auc', ...}."""
    from .core import score as sc
    srcs = [fills] if isinstance(fills, str) else list(fills)
    rows = []
    for s_ in srcs:
        for r in read_rows(s_):
            r["source"] = s_
            rows.append(r)
    if not rows:
        raise RuntimeError("no fills in the layers given")
    y = sc.labels(rows, positive_words)
    ok = np.array([not sc.off_ground(r, positive_words) for r in rows], bool)  # the list says: not there
    if not ok.all():
        log("{} fill(s) are on listed dams the list says are not there (breached, not built, removed ...): "
            "left out of training and of the checks".format(int((~ok).sum())))
    cv = float("nan")
    if model_in:
        model = sc.load(model_in)
        log("scoring {} fill(s) with {}".format(len(rows), model_in))
    else:
        if kind not in SCORE_KINDS:
            raise RuntimeError("score kind {} is not one of {}".format(kind, ", ".join(SCORE_KINDS)))
        opts = {} if kind == "v1" else {"features": sc.ALL_FEATURES, "bags": True}
        if kind == "0.5-terrain":
            opts.update(splines=True, groups=True)
        if kind == "0.5-trees":
            opts["learner"] = "trees"
        mk = _marks_of(marks)
        extra = []
        by = {r.get("find_id"): r for r in rows}
        for fid, m in mk.items():
            v = (m or {}).get("mark")
            if fid in by and v in ("yes", "no", "unsure"):
                extra.append((by[fid], {"yes": 1.0, "no": 0.0, "unsure": 0.5}[v], float(marks_weight)))
        if mk and not extra:
            log("warning: none of the {} mark(s) in {} is a yes / no / unsure on a fill in these layers: "
                "trained without them".format(len(mk), marks))
        elif mk:
            log("{} of {} mark(s) matched a fill in these layers".format(len(extra), len(mk)))
        if extra:
            opts["extra"] = extra
        note = "trained on {} fill(s) from {} layer(s), kind {}{}".format(
            len(rows), len(srcs), kind, ", {} reviewed mark(s)".format(len(extra)) if extra else "")
        try:
            model = sc.train(rows, positive_words, note=note, **opts)
        except ValueError as e:
            raise RuntimeError(str(e)) from None
        # k-fold cross-validated AUC: how well it ranks listed dams it has not seen
        if folds and y[ok].sum() >= folds:
            rng = np.random.default_rng(0)
            fold = rng.integers(0, folds, len(rows))
            out = np.zeros(len(rows))
            for k in range(folds):
                tr_ = [r for r, f in zip(rows, fold) if f != k]
                te = [i for i, f in enumerate(fold) if f == k]
                try:
                    mf = sc.train(tr_, positive_words, **{k: v for k, v in opts.items() if k != "extra"})
                except ValueError:
                    continue
                out[te] = sc.score(mf, [rows[i] for i in te])
            cv = sc.ranked_auc(out[ok], y[ok])
        model["cv_auc"] = None if not np.isfinite(cv) else round(cv, 3)
        model_out = model_out or os.path.splitext(out_gpkg)[0] + "_score_model.json"
        sc.save(model, model_out)
        log("score model: {} listed dam(s) against {} other fill(s); cross-validated AUC {} -> {}".format(
            model["n_listed"], model["n_other"], "n/a" if not np.isfinite(cv) else "{:.2f}".format(cv), model_out))
    det = sc.score_detail(model, rows)
    s_all = np.array([v for v, _w in det])
    auc = sc.ranked_auc(s_all[ok], y[ok])
    for r, (v, why) in zip(rows, det):
        r["score"] = round(float(v), 3)
        r["score_note"] = why
    rows.sort(key=lambda r: ({"new": 0, "unsure": 1, "listed": 2}.get(r.get("status"), 3), -r["score"]))
    fields = [f for f in EMB_FIELDS] + [("source", "str")]
    write_points(out_gpkg, "scored_fills", fields, rows)
    write_csv(os.path.splitext(out_gpkg)[0] + "_scored_fills.csv", fields, rows)
    return {"rows": rows, "model": model, "model_path": model_out, "auc": auc, "cv_auc": cv,
            "gpkg": out_gpkg}
