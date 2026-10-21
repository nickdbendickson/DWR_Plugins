"""Cache one DEM clip per dam (EPSG:5070) from 3DEP or a local LiDAR folder."""
import os

from osgeo import gdal, osr
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterEnum, QgsProcessingParameterNumber,
                       QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFile, QgsProcessingException,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem,
                       QgsProject)

from ..core import terrain
from ..core.warp_opts import no_overviews
from ..compat import (FILE_FOLDER, NUM_DOUBLE, SRC_POINT)
from ..damsuite import gdalsafe as GS
from . import _common as cm
from ..core import errors as er


# ----------------------------------------- elevation units and local mosaics (H9 fix) --
# Ported from the sibling nd_dam_screening/algorithms/_common.py (0.17.0): a plain
# gdal.BuildVRT over a local LiDAR folder drops every tile whose CRS differs from the first
# one's AND mosaics feet elevations straight into metres (a feet DEM then reads 3.28x too high,
# giving wrong storage / flood). These helpers scale a feet / US-survey-foot tile to metres and
# warp tiles in a second CRS to EPSG:5070 before the mosaic. 3DEP tiles (already metres,
# EPSG:5070) do not go through this path and are untouched.
ALBERS = "EPSG:5070"
FT = 0.3048                      # metres per international foot
US_FT = 1200.0 / 3937.0          # metres per US survey foot
_FT_WORDS = ("ft", "foot", "feet", "intl foot", "international foot", "ft_intl", "foot_intl")
_M_WORDS = ("m", "metre", "meter", "metres", "meters", "metre (m)")


def z_unit_scale(unit):
    """(metres per elevation unit, kind) for a band's GetUnitType(): kind is "m", "ft" (0.3048),
    "us-ft" (1200/3937), "" (no tag: metres assumed) or "other" (a tag we do not know: metres
    assumed). Read the tag, never the CRS: a State Plane feet CRS often holds metre elevations."""
    u = " ".join(str(unit or "").strip().lower().replace("-", " ").replace(".", " ").split())
    if not u:
        return 1.0, ""
    if u in _M_WORDS:
        return 1.0, "m"
    squashed = u.replace(" ", "").replace("_", "")
    if ("us" in squashed or "survey" in squashed) and any(w in squashed for w in ("ft", "foot", "feet")):
        return US_FT, "us-ft"                                    # us-ft, ftUS, US survey foot, foot_us
    if u in _FT_WORDS or squashed in ("ft", "foot", "feet", "intlfoot", "ftintl", "footintl"):
        return FT, "ft"
    return 1.0, "other"


def dem_z_scale(path):
    """(metres per elevation unit, kind, unit tag) of band 1 of a DEM file (z_unit_scale)."""
    ds = cm.gdal_open(path)
    try:
        unit = ds.GetRasterBand(1).GetUnitType()
    except AttributeError:                                       # a stand-in without unit tags
        unit = ""
    scale, kind = z_unit_scale(unit)
    return scale, kind, unit or ""


def _crs_name(wkt):
    try:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        code = srs.GetAuthorityCode(None)
        name = srs.GetName() or "unnamed CRS"
        return "EPSG:{} {}".format(code, name) if code else name
    except Exception:                                            # noqa
        return str(wkt)[:60] or "no CRS"


def _same_crs(a, b):
    if a == b:
        return True
    if not a or not b:
        return False
    try:
        sa, sb = osr.SpatialReference(), osr.SpatialReference()
        sa.ImportFromWkt(a)
        sb.ImportFromWkt(b)
        return bool(sa.IsSame(sb))
    except Exception:                                            # noqa
        return False


def _cell_m(wkt, gt):
    """A tile's cell size in metres (feet CRSs scaled; degrees taken as 111 km), or None."""
    try:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        f = srs.GetLinearUnits() if srs.IsProjected() else 111320.0
        return abs(gt[1]) * f or None
    except Exception:                                            # noqa
        return None


def local_mosaic(tiles, vrt, log=None):
    """One VRT in metres (EPSG:5070 when the tiles' CRSs differ) over a local DEM folder.

    gdal.BuildVRT alone drops every tile whose CRS differs from the first one's and mosaics feet
    with metres. Here a tile tagged in feet / US survey feet is scaled to metres (a VRT beside
    the mosaic, tag "m"); an untagged tile is taken as metres, with a warning; when the tiles are
    in more than one CRS each is warped to EPSG:5070 (a warped VRT, no pixels written) before the
    mosaic. The per-tile VRTs go in <mosaic>_tiles/. Returns vrt."""
    say = log or (lambda _m: None)
    groups, feet, untagged, other = [], [], [], []
    scales, cells = [], []
    for t in tiles:
        ds = cm.gdal_open(t)
        wkt = ds.GetProjection() or ""
        cells.append(_cell_m(wkt, ds.GetGeoTransform()))
        ds = None
        scale, kind, unit = dem_z_scale(t)
        scales.append(scale)
        if kind in ("ft", "us-ft"):
            feet.append("{} ({})".format(os.path.basename(t), unit))
        elif kind == "":
            untagged.append(os.path.basename(t))
        elif kind == "other":
            other.append("{} ({!r})".format(os.path.basename(t), unit))
        for g in groups:
            if _same_crs(g[0], wkt):
                g[1].append(t)
                break
        else:
            groups.append((wkt, [t]))
    if len(groups) > 1 and any(not g[0] for g in groups):
        raise RuntimeError("The local DEM folder mixes tiles with and without a CRS; give every tile "
                           "a CRS (a .prj / .aux.xml) or remove these: {}".format(
                               ", ".join(os.path.basename(t) for g in groups if not g[0] for t in g[1][:5])))
    if feet:
        say("Local DEM: {} tile(s) tagged in feet, converted to metres: {}{}".format(
            len(feet), ", ".join(feet[:5]), " ..." if len(feet) > 5 else ""))
    if untagged:
        say("WARNING: local DEM: {} tile(s) have no elevation unit tag; their elevations are assumed to be "
            "METRES (a feet DEM would be read 3.28x too high): {}{}".format(
                len(untagged), ", ".join(untagged[:5]), " ..." if len(untagged) > 5 else ""))
    if other:
        say("WARNING: local DEM: unknown elevation unit, assumed metres: {}".format(", ".join(other[:5])))
    mixed = len(groups) > 1
    if mixed:
        say("Local DEM: tiles in {} CRSs ({}), each warped to EPSG:5070 for the mosaic".format(
            len(groups), "; ".join("{} - {} tile(s)".format(_crs_name(g[0]), len(g[1])) for g in groups)))
    if not mixed and all(sc == 1.0 for sc in scales):
        GS.checked(gdal.BuildVRT(vrt, tiles), "building " + vrt)
        return vrt
    work = os.path.splitext(vrt)[0] + "_tiles"
    os.makedirs(work, exist_ok=True)
    parts = []
    res = min([c for c in cells if c] or [1.0])     # the finest tile's cell, on one aligned grid
    for i, (t, sc) in enumerate(zip(tiles, scales)):
        p = t
        if sc != 1.0:
            p = os.path.join(work, "{:05d}_m.vrt".format(i))
            ds = GS.checked(gdal.Translate(p, t, format="VRT", outputType=gdal.GDT_Float32,
                                           scaleParams=[[0.0, 1.0, 0.0, sc]]), "converting {} to metres".format(t))
            ds = None
            ds = gdal.Open(p, gdal.GA_Update)
            ds.GetRasterBand(1).SetUnitType("m")
            ds = None
        if mixed:
            q = os.path.join(work, "{:05d}_5070.vrt".format(i))
            GS.checked(gdal.Warp(q, p, format="VRT", dstSRS=ALBERS, resampleAlg="bilinear",
                                 xRes=res, yRes=res, targetAlignedPixels=True,
                                 dstNodata=-9999, outputType=gdal.GDT_Float32),
                       "warping {} to EPSG:5070".format(t))
            p = q
        parts.append(p)
    GS.checked(gdal.BuildVRT(vrt, parts), "building " + vrt)
    return vrt


class TerrainCache(QgsProcessingAlgorithm):
    DAMS, SRC, LOCAL, RADIUS, CELL, OUT, ID = ("DAMS", "SRC", "LOCAL", "RADIUS",
                                               "CELL", "OUT", "ID")

    def name(self): return "terraincache"
    def displayName(self): return "2 - Cache terrain per dam"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return TerrainCache()

    def shortHelpString(self):
        return ("Writes dam_<ID>.tif (float32 metres, EPSG:5070) for every dam. Source is "
                "either the USGS 3DEP service (needs internet, run once) or a local folder "
                "of ND LiDAR-derived DEM tiles (works offline). Existing files are reused, "
                "so the batch can be re-run or resumed. Local tiles tagged in feet are "
                "converted to metres; untagged tiles are taken as metres (a warning is "
                "logged); tiles in different CRSs are each warped to EPSG:5070. 3DEP serves "
                "the best available resolution per location; check the resulting cell size "
                "where it matters.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DAMS, "Standardized dams", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterEnum(
            self.SRC, "Terrain source", ["USGS 3DEP service", "Local DEM folder"], defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(
            self.LOCAL, "Local DEM folder (if local source)",
            behavior=FILE_FOLDER, optional=True))
        cm.distance_params(self.addParameter, "RADIUS_FT", self.RADIUS,
                           "Half-width of clip around each dam (ft)", 5000.0, min_ft=600.0)
        self.addParameter(QgsProcessingParameterNumber(
            self.CELL, "Cell size (m; the DEM's own unit - 10 m = 32.8 ft)", NUM_DOUBLE, 10.0, minValue=0.5))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUT, "Output folder"))

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, self.DAMS, context)
        use_local = self.parameterAsEnum(params, self.SRC, context) == 1
        radius = cm.distance_m(self, params, context, "RADIUS_FT", self.RADIUS, 5000.0)
        cell = self.parameterAsDouble(params, self.CELL, context)
        out = self.parameterAsString(params, self.OUT, context)
        os.makedirs(out, exist_ok=True)

        vrt = None
        if use_local:
            folder = self.parameterAsString(params, self.LOCAL, context)
            feedback.pushInfo("Listing local DEM tiles in {} ...".format(folder))
            tiles = terrain.list_local_tiles(folder)
            if not tiles:
                raise QgsProcessingException("No DEM tiles found in the local folder")
            feedback.pushInfo("Building a mosaic of {} tile(s). This can sit quiet for several minutes.".format(len(tiles)))
            vrt = os.path.join(out, "_local_mosaic.vrt")
            # H9: feet tiles converted to metres, mixed CRSs warped to EPSG:5070 (no tile dropped)
            try:
                local_mosaic(tiles, vrt, feedback.pushInfo)
            except RuntimeError as e:
                raise QgsProcessingException(str(e))
            feedback.pushInfo("Mosaic ready: {}".format(vrt))

        alb = QgsCoordinateReferenceSystem("EPSG:5070")
        tr = QgsCoordinateTransform(dams.sourceCrs(), alb, QgsProject.instance())
        total = dams.featureCount() or 1
        feedback.pushInfo("Caching terrain for {} dam(s), {:.0f} m cell, {:.0f} ft half-width. "
                          "A reused file is skipped. 3DEP downloads one dam at a time.".format(
                              total, cell, radius / 0.3048))
        ok = fail = skipped = 0
        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(f["dam_id"]) if "dam_id" in dams.fields().names() else str(f.id())
            dst = os.path.join(out, f"dam_{dam_id}.tif")
            if os.path.exists(dst):
                ok += 1
                skipped += 1
                feedback.pushInfo("Dam {} of {}: {} already cached".format(i + 1, total, dam_id))
                continue
            feedback.pushInfo("Dam {} of {}: {} starting".format(i + 1, total, dam_id))
            feedback.setProgressText("Dam {} of {}: {}".format(i + 1, total, dam_id))
            g = f.geometry(); g.transform(tr); p = g.asPoint()
            ext = (p.x() - radius, p.y() - radius, p.x() + radius, p.y() + radius)
            try:
                if use_local:
                    GS.checked(gdal.Warp(dst, vrt, **no_overviews(gdal, dict(   # 0.17.35
                        dstSRS="EPSG:5070", outputBounds=ext, xRes=cell, yRes=cell,
                        resampleAlg="bilinear", outputType=gdal.GDT_Float32, dstNodata=-9999,
                        creationOptions=["COMPRESS=DEFLATE"]))), "clipping the DEM")
                else:
                    tiles = terrain.fetch_3dep(ext, cell, os.path.join(out, "_tmp"), f"d{dam_id}",
                                               log=feedback.pushInfo)
                    GS.checked(gdal.Warp(dst, tiles, dstNodata=-9999,
                                         creationOptions=["COMPRESS=DEFLATE"]), "mosaicking the 3DEP tiles")
                    for t in tiles: os.remove(t)
                ok += 1
                feedback.pushInfo("Dam {} of {}: {} written".format(i + 1, total, dam_id))
            except Exception as e:  # keep the batch going
                fail += 1
                feedback.reportError("dam {}: {}".format(dam_id, er.plain(e, dam_id, "2")))
        feedback.pushInfo("terrain cached: {} written or reused, {} skipped, {} failed".format(ok, skipped, fail))
        return {self.OUT: out}
