"""Shared QGIS/GDAL helpers for the processing tools."""
import os
import threading

import numpy as np
from osgeo import gdal, osr
from qgis.core import (QgsFields, QgsField, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsProject, QgsFeatureRequest, QgsRectangle, QgsWkbTypes)

from ..core import terrain, hydrocond, gridutil as gu
from ..core.warp_opts import no_overviews
from ..compat import (GEOM_LINE, GEOM_POINT, GEOM_POLYGON)
from ..damsuite import gdalsafe as GS
from ..damsuite import structure_flags as SF

ALBERS = "EPSG:5070"
FT = 0.3048                      # metres per foot (user-facing depths are in feet since 0.11)
# 0.11.1: no gdal.UseExceptions() - it is process-wide (QGIS and the other plugins); GDAL
# calls go through damsuite.gdalsafe instead, which raises the same errors in both modes.
_lock = threading.Lock()


def hide(param):
    """Mark a Processing parameter hidden (kept for old models / saved histories that still pass
    the metre value; the dialog shows the feet parameter). Works on QGIS 3.x (legacy
    FlagHidden), QGIS 3.36+ / 4 (Qgis.ProcessingParameterFlag.Hidden) and the test mock (no
    flags: ignored)."""
    try:
        from qgis.core import Qgis
        flag = Qgis.ProcessingParameterFlag.Hidden
    except Exception:
        try:
            from qgis.core import QgsProcessingParameterDefinition as D
            flag = D.FlagHidden
        except Exception:
            flag = None
    if flag is not None:
        try:
            param.setFlags(param.flags() | flag)
        except Exception:
            pass
    return param


def feet_or_legacy(alg, params, context, ft_key, m_key, default_ft):
    """The value of a depth threshold in METRES for the model: from the feet parameter
    `ft_key`, unless the caller passed only the old metre parameter `m_key` (a saved model /
    history from before 0.11), which then wins. Returns (metres, feet, source)."""
    legacy = m_key in params and params.get(m_key) not in (None, "") and ft_key not in params
    if legacy:
        m = float(alg.parameterAsDouble(params, m_key, context))
        return m, m / FT, m_key
    ft = alg.parameterAsDouble(params, ft_key, context)
    if ft is None or ft == 0 and ft_key not in params:
        ft = default_ft
    return float(ft) * FT, float(ft), ft_key


def distance_params(add, ft_key, m_key, label_ft, default_ft, min_ft=None, max_ft=None, legacy_label=None):
    """0.13: a user-facing DISTANCE in feet plus its hidden metre twin for saved models / histories
    from before 0.13 (`feet_or_legacy` reads them). `add` is the tool's addParameter; `label_ft`
    should name the unit, e.g. 'Toe search distance beyond the water edge (ft; YOUR value)'."""
    from qgis.core import QgsProcessingParameterNumber
    from ..compat import NUM_DOUBLE
    kw = {}
    if min_ft is not None:
        kw["minValue"] = min_ft
    if max_ft is not None:
        kw["maxValue"] = max_ft
    add(QgsProcessingParameterNumber(ft_key, label_ft, NUM_DOUBLE, default_ft, **kw))
    add(hide(QgsProcessingParameterNumber(m_key, legacy_label or "(before 0.13) {} (m)".format(
        label_ft.split("(")[0].strip()), NUM_DOUBLE, None, optional=True)))


def distance_m(alg, params, context, ft_key, m_key, default_ft):
    """The metres of a distance parameter made by distance_params (feet wins; a saved metre value
    alone is honoured)."""
    m, _ft, _src = feet_or_legacy(alg, params, context, ft_key, m_key, default_ft)
    return m


def no_flood(f, names):
    """(True, reason) when tool 16 marked this dam 'no breach flood' (no_flood = YES: a dugout or
    pond with no water above the natural ground); (False, "") otherwise."""
    if fval(f, names, "no_flood") is None or str(fval(f, names, "no_flood")).strip().upper() != "YES":
        return False, ""
    return True, str(fval(f, names, "no_flood_why") or "no water above the natural ground")


def excluded(f, names):
    """(True, reason) when the Review tab excluded this dam (rev_decision = exclude); (False, "")
    otherwise. Tools 5, 6, 7, 8 and 12 skip such a dam ("excluded by review")."""
    v = fval(f, names, "rev_decision")
    if v is None or str(v).strip().lower() != "exclude":
        return False, ""
    who, when = fval(f, names, "rev_by"), fval(f, names, "rev_date")
    return True, "excluded by review ({}{})".format(who or "reviewer", ", " + str(when)[:10] if when else "")


def flagged(f, names):
    """0.17.26: (entry, note) when the dam is a flagged structure (damsuite/structure_flags.py,
    e.g. DWR-3161 / ND00597, a road fill over backwater): matched on dam_id, or nid_id.
    (None, "") otherwise. Tools 5, 6, 7, 7b, 8 and 12 list
    such a dam with the note instead of computing a breach flood or a breach hazard."""
    try:
        e = SF.lookup(fval(f, names, "dam_id"), fval(f, names, "nid_id"))
    except Exception:                                      # noqa  (a flag check never fails a tool)
        e = None
    if not e:
        return None, ""
    return e, "flagged structure ({}): {}".format(SF.short_ref(e), SF.note(e))


def albers():
    return QgsCoordinateReferenceSystem(ALBERS)


def to_albers(src):
    return QgsCoordinateTransform(src.sourceCrs(), albers(), QgsProject.instance())


def from_albers(src):
    return QgsCoordinateTransform(albers(), src.sourceCrs(), QgsProject.instance())


def extend_fields(src_fields, new):
    """Copy src fields and add `new` [(name, QVariant type)], reusing names that already exist.

    Returns (fields, setter) where setter(attrs_list, name, value) writes by name.
    """
    fields = QgsFields(src_fields)
    for n, t in new:
        if fields.indexOf(n) < 0:
            fields.append(QgsField(n, t))

    def setter(attrs, name, value):
        attrs[fields.indexOf(name)] = value
    return fields, setter


def blank_attrs(fields, feature):
    a = list(feature.attributes()) + [None] * (fields.count() - len(feature.attributes()))
    return a[:fields.count()]


def fval(f, names, k, cast=None):
    """Feature value by name, None if missing/NULL/blank; optional cast."""
    if k is None or k not in names:
        return None
    v = f[k]
    if v is None or v == "" or (hasattr(v, "isNull") and v.isNull()):
        return None
    if cast is not None:
        try:
            return cast(v)
        except (TypeError, ValueError):
            return None
    return v


def _gdal_msg():
    try:
        return gdal.GetLastErrorMsg()
    except Exception:                                            # noqa
        return ""


def gdal_open(path):
    """gdal.Open, or an error that names the file (GDAL returns None for an unreadable or
    damaged file; v0.11.1 - it used to surface as "'NoneType' object has no attribute ...")."""
    ds = gdal.Open(path)
    if ds is None:
        raise RuntimeError("GDAL cannot open {} ({}) - delete it to have it made again".format(
            path, _gdal_msg() or "missing or damaged file"))
    return ds


def write_tif(path, arr, gt, nodata=None):
    ds = GS.checked(gdal.GetDriverByName("GTiff").Create(path, arr.shape[1], arr.shape[0], 1,
                                               gdal.GDT_Float32, ["COMPRESS=DEFLATE"]), "creating " + path)
    ds.SetGeoTransform(gt)
    srs = osr.SpatialReference(); srs.ImportFromEPSG(5070)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    if nodata is not None:
        b.SetNoDataValue(nodata)
    b.WriteArray(np.asarray(arr, "float32")); ds.FlushCache(); ds = None


def read_tif(path):
    ds = gdal_open(path)
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray().astype("float64")
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = gu.NODATA
    return a, ds.GetGeoTransform()


def lines_near(source, xy, radius, key_field=None):
    """Line geometries (EPSG:5070 numpy arrays) of `source` within radius of xy.

    Returns list of (key, [array, ...]) - one entry per feature, parts separately.
    """
    if source is None:
        return []
    ta, fa = to_albers(source), from_albers(source)
    box = fa.transformBoundingBox(QgsRectangle(xy[0] - radius, xy[1] - radius,
                                               xy[0] + radius, xy[1] + radius))
    out = []
    names = source.fields().names()
    for f in source.getFeatures(QgsFeatureRequest().setFilterRect(box)):
        g = f.geometry()
        if g.isEmpty():
            continue
        g.transform(ta)
        if QgsWkbTypes.geometryType(g.wkbType()) != QgsWkbTypes.LineGeometry:
            continue
        parts = g.asMultiPolyline() if g.isMultipart() else [g.asPolyline()]
        arrs = [np.array([(p.x(), p.y()) for p in pl]) for pl in parts if len(pl) >= 2]
        key = f[key_field] if key_field and key_field in names else f.id()
        if arrs:
            out.append((key, arrs))
    return out


class TerrainLoader:
    """Loads terrain for any extent at any cell size and conditions it.

    Source order: the cached per-dam clip from tool 2 when it covers the extent,
    otherwise the configured source (USGS 3DEP or a local DEM folder); with
    neither, the cached clip is used as far as it reaches (outside = wall).
    Road-fill barriers on stream lines are cut at the model resolution.
    """

    def __init__(self, demdir, source="cache", local_folder=None, tmpdir=None,
                 max_rise_m=8.0, max_len_m=150.0):
        self.demdir, self.source, self.tmpdir = demdir, source, tmpdir
        self.max_rise, self.max_len = max_rise_m, max_len_m
        self.vrt = None
        if source == "local" and local_folder:
            tiles = terrain.list_local_tiles(local_folder)
            if tiles:
                self.vrt = os.path.join(tmpdir or demdir, "_terrain_mosaic.vrt")
                with _lock:
                    if not os.path.exists(self.vrt):
                        GS.checked(gdal.BuildVRT(self.vrt, tiles), "building " + self.vrt)

    def _cached(self, dam_id):
        p = os.path.join(self.demdir, f"dam_{dam_id}.tif") if self.demdir else None
        return p if p and os.path.exists(p) else None

    def _covers(self, path, ext):
        ds = gdal_open(path); gt = ds.GetGeoTransform()
        x0, y1 = gt[0], gt[3]
        x1, y0 = x0 + ds.RasterXSize * gt[1], y1 + ds.RasterYSize * gt[5]
        return x0 <= ext[0] + 1 and y0 <= ext[1] + 1 and x1 >= ext[2] - 1 and y1 >= ext[3] - 1

    def load(self, dam_id, center, half, cell, streams=(), protect_lines=(), roads=None,
             condition=True):
        x0 = np.floor((center[0] - half) / cell) * cell
        y0 = np.floor((center[1] - half) / cell) * cell
        n = int(np.ceil(2 * half / cell))
        ext = (x0, y0, x0 + n * cell, y0 + n * cell)
        src = self._cached(dam_id)
        if src is None or not self._covers(src, ext):
            if self.source == "3dep":
                tmp = os.path.join(self.tmpdir or self.demdir, "_3dep")
                src_list = terrain.fetch_3dep(ext, max(cell, 1.0), tmp, f"x{dam_id}_{int(half)}")
                src = GS.checked(gdal.BuildVRT("", src_list), "mosaicking the 3DEP tiles")
            elif self.vrt:
                src = self.vrt
            elif src is None:
                raise FileNotFoundError("no cached DEM (tool 2) and no terrain source for a larger clip")
        # 0.17.35: averaged from the full-resolution cells, never the source's overviews
        ds = gdal.Warp("", src, **no_overviews(gdal, dict(
            format="MEM", dstSRS=ALBERS, outputBounds=ext, width=n, height=n,
            resampleAlg="average", srcNodata=-9999, dstNodata=-9999,
            outputType=gdal.GDT_Float32)))
        if ds is None:
            raise RuntimeError("GDAL could not warp the terrain for dam {} ({})".format(
                dam_id, _gdal_msg() or "no message"))
        dem = ds.GetRasterBand(1).ReadAsArray().astype("float64")
        dem[~np.isfinite(dem)] = gu.NODATA
        dem[dem < -1000] = gu.NODATA
        gt = ds.GetGeoTransform()
        if condition and streams:
            prot = gu.dilate(gu.rasterize_lines(protect_lines, gt, dem.shape), 2) if protect_lines else None
            rmask = gu.dilate(gu.rasterize_lines(roads, gt, dem.shape), 2) if roads else None
            dem, _info = hydrocond.breach_barriers(dem, streams, gt, protect=prot, roads=rmask,
                                                   max_rise_m=self.max_rise, max_len_m=self.max_len)
        return dem, gt


def load_cached_clip(loader, dam_id, streams=(), protect_lines=()):
    """The tool-2 clip on its own grid (no re-centring), conditioned."""
    path = loader._cached(dam_id)
    if not path:
        raise FileNotFoundError("no cached DEM (tool 2)")
    dem, gt = read_tif(path)
    dem[~np.isfinite(dem)] = gu.NODATA
    if streams:
        prot = gu.dilate(gu.rasterize_lines(protect_lines, gt, dem.shape), 2) if protect_lines else None
        dem, _ = hydrocond.breach_barriers(dem, streams, gt, protect=prot,
                                           max_rise_m=loader.max_rise, max_len_m=loader.max_len)
    return dem, gt


def warp_to_grid(path_or_layer_source, gt, shape, resample="near"):
    ds = gdal.Warp("", path_or_layer_source, format="MEM", dstSRS=ALBERS,
                   outputBounds=(gt[0], gt[3] + shape[0] * gt[5], gt[0] + shape[1] * gt[1], gt[3]),
                   width=shape[1], height=shape[0], resampleAlg=resample,
                   outputType=gdal.GDT_Float32)
    if ds is None:
        raise RuntimeError("GDAL could not warp {} to the model grid ({})".format(
            path_or_layer_source, _gdal_msg() or "no message"))
    return ds.GetRasterBand(1).ReadAsArray().astype("float64")


def is_line_source(src):
    return src is not None and QgsWkbTypes.geometryType(src.wkbType()) == GEOM_LINE


def geom_parts(g):
    """(kind, parts) of a geometry in its current CRS, multi-part aware:
    'point' -> [(x, y), ...]; 'line' -> [[(x, y), ...], ...] (parts with >= 2 vertices);
    'polygon' -> [exterior ring [(x, y), ...], ...] (interior rings ignored). (None, []) when
    empty or of another type."""
    if g is None or g.isEmpty():
        return None, []
    t = g.type()
    multi = g.isMultipart()
    if t == GEOM_POINT:
        pts = g.asMultiPoint() if multi else [g.asPoint()]
        return "point", [(p.x(), p.y()) for p in pts]
    if t == GEOM_LINE:
        lines = g.asMultiPolyline() if multi else [g.asPolyline()]
        return "line", [[(p.x(), p.y()) for p in ln] for ln in lines if len(ln) >= 2]
    if t == GEOM_POLYGON:
        polys = g.asMultiPolygon() if multi else [g.asPolygon()]
        return "polygon", [[(p.x(), p.y()) for p in rings[0]] for rings in polys
                           if rings and len(rings[0]) >= 3]
    return None, []


def rep_point(g):
    """(x, y) representing a feature: the point itself, the mean of a MultiPoint's points,
    else the centroid. None when empty."""
    kind, parts = geom_parts(g)
    if kind == "point" and parts:
        return (sum(p[0] for p in parts) / len(parts), sum(p[1] for p in parts) / len(parts))
    if g is None or g.isEmpty():
        return None
    c = g.centroid().asPoint()
    return (c.x(), c.y())


def albers_parts(feature, transform):
    """geom_parts of a feature's geometry transformed to EPSG:5070."""
    g = feature.geometry()
    if g is None or g.isEmpty():
        return None, []
    g.transform(transform)
    return geom_parts(g)
