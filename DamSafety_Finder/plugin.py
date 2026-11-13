"""QGIS side of the Finder: six Processing tools around finder.py (Finder 0.1).

    embankments  Find fills across streams (dams, dry dams, road fills)   finder.run_embankments
    ponds        Find ponds and dugouts in a water-body layer              finder.run_ponds
    crossings    Find road / rail x stream crossings (culverts)            finder.run_crossings
    water        Find open water in aerial imagery (pond outlines)          finder.run_water
    osm          Dams / dikes / culverts of an OpenStreetMap file           finder.osm_structures
    score        Score fills by how much they look like the listed dams     finder.run_score
    evidence     Add evidence to fills (water years, NWI, LiDAR, below)     finder.run_evidence
    review_batch Pick a review batch (random control, top, near the cut)    finder.run_review_batch

The provider id and names come from brand.py (the file that differs between DamSafety Finder
and DamIT Finder). Input layers must be file-based (GeoPackage, shapefile, ...): the runs
read them with OGR, outside QGIS's layer objects, and a layer's subset filter is not applied.
"""
from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsProcessing,
                       QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingParameterBoolean,
                       QgsProcessingParameterString,
                       QgsProcessingParameterEnum, QgsProcessingParameterExtent,
                       QgsProcessingParameterFile,
                       QgsProcessingParameterField, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterMultipleLayers, QgsProcessingParameterNumber,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
                       QgsProcessingProvider)

from . import brand
from .core import embankments as em
from .core import ponds as pd

FT = 0.3048
WORK = "EPSG:5070"
Z_UNITS = ["From the raster (refused when it records no unit; USGS 3DEP = metres)", "Metres", "Feet"]


def _first(*getters):
    """The first enum spelling this QGIS has: the scoped Qgis.* one (3.36+, the only one in QGIS 4
    / Qt6), then the older class members."""
    for g in getters:
        try:
            return g()
        except AttributeError:
            continue
    raise AttributeError("no spelling of this QGIS enum")


INTEGER = _first(lambda: Qgis.ProcessingNumberParameterType.Integer,
                 lambda: QgsProcessingParameterNumber.Type.Integer,
                 lambda: QgsProcessingParameterNumber.Integer)
DOUBLE = _first(lambda: Qgis.ProcessingNumberParameterType.Double,
                lambda: QgsProcessingParameterNumber.Type.Double,
                lambda: QgsProcessingParameterNumber.Double)
SRC_LINE = _first(lambda: Qgis.ProcessingSourceType.VectorLine,
                  lambda: QgsProcessing.SourceType.TypeVectorLine,
                  lambda: QgsProcessing.TypeVectorLine)
SRC_POLYGON = _first(lambda: Qgis.ProcessingSourceType.VectorPolygon,
                     lambda: QgsProcessing.SourceType.TypeVectorPolygon,
                     lambda: QgsProcessing.TypeVectorPolygon)
SRC_POINT = _first(lambda: Qgis.ProcessingSourceType.VectorPoint,
                   lambda: QgsProcessing.SourceType.TypeVectorPoint,
                   lambda: QgsProcessing.TypeVectorPoint)
SRC_ANY = _first(lambda: Qgis.ProcessingSourceType.VectorAnyGeometry,
                 lambda: QgsProcessing.SourceType.TypeVectorAnyGeometry,
                 lambda: QgsProcessing.TypeVectorAnyGeometry)

KNOWN_HELP = ("Known structures (optional; any point, line or polygon layers): every candidate is "
              "checked against them - listed, unsure or new. Give all the lists you have: {}.")


def _src(alg, params, name, context, required=True):
    """File source of a vector / raster layer parameter ('' when optional and empty)."""
    lyr = alg.parameterAsLayer(params, name, context)
    if lyr is None:
        if required:
            raise QgsProcessingException("{}: no layer".format(name))
        return ""
    prov = lyr.providerType() if hasattr(lyr, "providerType") else ""
    if prov not in ("ogr", "gdal"):
        raise QgsProcessingException(
            "{} ({}) is a '{}' layer: save it to a file (GeoPackage) first - the Finder reads "
            "layers with GDAL / OGR".format(lyr.name(), name, prov))
    if getattr(lyr, "subsetString", lambda: "")():
        alg._feedback.pushWarning("{}: its filter is not applied (the whole file is read)".format(lyr.name()))
    return lyr.source()


def _known(alg, params, context):
    out = []
    for lyr in alg.parameterAsLayerList(params, "KNOWN", context) or []:
        if lyr.providerType() != "ogr":
            raise QgsProcessingException("known layer {} is a '{}' layer: save it to a file first".format(
                lyr.name(), lyr.providerType()))
        out.append((lyr.source(), lyr.name(), None))
    return out


def _bbox(alg, params, context):
    if not params.get("EXTENT"):
        return None
    r = alg.parameterAsExtent(params, "EXTENT", context, QgsCoordinateReferenceSystem(WORK))
    if r is None or r.isNull() or r.isEmpty():
        return None
    return (r.xMinimum(), r.yMinimum(), r.xMaximum(), r.yMaximum())


def _z(alg, params, context):
    return [None, False, True][alg.parameterAsEnum(params, "Z_UNIT", context)]


class _Base(QgsProcessingAlgorithm):
    _feedback = None

    def group(self):
        return "Find structures"

    def groupId(self):
        return "find"

    def createInstance(self):
        return type(self)()

    def _num(self, name, label, default, minimum=0.0):
        self.addParameter(QgsProcessingParameterNumber(name, label, DOUBLE, default, minValue=minimum))

    def _common_tail(self, match_default):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            "KNOWN", KNOWN_HELP.format(brand.KNOWN_EXAMPLES), SRC_ANY,
            optional=True))
        self._num("MATCH_FT", "Listed when a known structure is within (ft; YOUR value; unsure up to 2x)",
                  match_default, 1.0)
        self.addParameter(QgsProcessingParameterExtent("EXTENT", "Only this area (empty = the whole layer)",
                                                       optional=True))

    def _log(self, feedback):
        def log(msg):
            feedback.pushInfo(str(msg))
        return log

    def _load(self, context, path, layer, name):
        if context.project() is not None:
            context.addLayerToLoadOnCompletion(
                "{}|layername={}".format(path, layer), context.LayerDetails(name, context.project(), layer))

    @staticmethod
    def _stop_if_cancelled(feedback, out):
        """A cancelled run fails (it used to report success on the tiles done so far)."""
        if feedback.isCanceled():
            raise QgsProcessingException(
                "Cancelled: {} holds only the part done before the cancel - run again for the whole "
                "area".format(out))

    def _report(self, feedback, res, what):
        c = res["counts"]["status"]
        feedback.pushInfo("{}: {} new, {} unsure, {} already listed".format(
            what, c.get("new", 0), c.get("unsure", 0), c.get("listed", 0)))
        if res.get("screening"):
            feedback.pushInfo("{} not-listed candidate(s) written as dam records for screening".format(
                res["screening"]))


class FindEmbankments(_Base):
    def name(self):
        return "embankments"

    def displayName(self):
        return "Find fills across streams and draws (dams, dry dams, road fills, dugouts)"

    def shortHelpString(self):
        return (em.__doc__.split("Every threshold")[0].strip().replace("\n\n", "<br><br>")
                + "<br><br>" + brand.EMB_HELP + "<br><br>Every threshold is YOUR value; the "
                "starting values are not standards. Screening only: check every candidate against "
                "an aerial image.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("DEM", "Bare-earth DEM (LiDAR; a file or a VRT)"))
        self.addParameter(QgsProcessingParameterEnum("Z_UNIT", "DEM elevation unit", Z_UNITS, defaultValue=0))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "STREAMS", "Stream lines (3DHP / NHD flowlines; optional with the DEM drainage lines)",
            [SRC_LINE], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "ROADS", "Road centrelines (optional: tells a road fill from a dam)", [SRC_LINE],
            optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "RAILS", "Railroad lines (optional)", [SRC_LINE], optional=True))
        p = em.FinderParams()
        self._common_tail(500.0)
        self._num("CELL", "Analysis cell size (m; 3 m = 9.8 ft)", 3.0, 0.5)
        self._num("MIN_HEIGHT_FT", "Smallest fill height to report: spill level over the toe (ft; YOUR value)",
                  round(p.min_height_m / FT, 2), 0.5)
        self._num("MIN_HEAD_FT", "Holds water when the upstream level is above the toe by more than the "
                  "valley's own fall, by at least (ft; YOUR value)", round(p.min_head_m / FT, 2), 0.0)
        self._num("MAX_LEN_FT", "Longest fill along the stream (ft; YOUR value)", round(p.max_len_m / FT, 0), 10.0)
        self._num("TOE_FT", "Toe search distance below the fill (ft)", round(p.toe_search_m / FT, 0), 10.0)
        self._num("WINDOW_FT", "Half-width of the spill / storage window (ft)", round(p.window_m / FT, 0), 100.0)
        self._num("DRAIN_AC", "Also walk every draw of the DEM draining at least (acres; 0 = only the "
                  "stream lines; YOUR value)", 10.0, 0.0)
        self.addParameter(QgsProcessingParameterBoolean(
            "DIKES", "Also find dikes that hold water beside a stream (dams built along a channel)", True))
        self.addParameter(QgsProcessingParameterBoolean(
            "DUGOUTS", "Also look for dug pits (dugouts) anywhere in the DEM", True))
        self.addParameter(QgsProcessingParameterBoolean(
            "RIDGES", "Also list raised ridges anywhere (levees, dikes; a review list)", False))
        import os
        dm = getattr(brand, "DEFAULT_MODEL", None)
        dm = os.path.join(os.path.dirname(__file__), dm) if dm else None
        self.addParameter(QgsProcessingParameterFile(
            "MODEL", "Score model (optional; .json from the score tool{})".format(
                "; the shipped one" if dm else ""),
            extension="json", defaultValue=dm if dm and os.path.exists(dm) else None, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            "HILLSHADE", "Also write a review hillshade (<output>_hillshade.tif)", True))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        p = em.FinderParams(
            min_height_m=self.parameterAsDouble(params, "MIN_HEIGHT_FT", context) * FT,
            min_head_m=self.parameterAsDouble(params, "MIN_HEAD_FT", context) * FT,
            max_len_m=self.parameterAsDouble(params, "MAX_LEN_FT", context) * FT,
            toe_search_m=self.parameterAsDouble(params, "TOE_FT", context) * FT,
            window_m=self.parameterAsDouble(params, "WINDOW_FT", context) * FT)
        out = self.parameterAsFileOutput(params, "OUT", context)
        try:
            res = finder.run_embankments(
                _src(self, params, "DEM", context), _src(self, params, "STREAMS", context, False) or None, out,
                roads=_src(self, params, "ROADS", context, False) or None,
                rails=_src(self, params, "RAILS", context, False) or None,
                known=_known(self, params, context), bbox=_bbox(self, params, context),
                cell=self.parameterAsDouble(params, "CELL", context), z_feet=_z(self, params, context),
                params=p, match_ft=self.parameterAsDouble(params, "MATCH_FT", context),
                hillshade=self.parameterAsBool(params, "HILLSHADE", context),
                drainage_ac=self.parameterAsDouble(params, "DRAIN_AC", context),
                dugouts=self.parameterAsBool(params, "DUGOUTS", context),
                dikes=self.parameterAsBool(params, "DIKES", context),
                ridges=self.parameterAsBool(params, "RIDGES", context),
                model=self.parameterAsFile(params, "MODEL", context) or None,
                log=self._log(feedback), progress=feedback.setProgress, cancelled=feedback.isCanceled)
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        self._report(feedback, res, "Fills across streams")
        if res.get("hillshade") and context.project() is not None:
            context.addLayerToLoadOnCompletion(
                res["hillshade"], context.LayerDetails("Review hillshade", context.project(), "hillshade"))
        self._load(context, out, "embankments", "Fills across streams")
        self._load(context, out, "spillways", "Where each pool gets out (spillways)")
        if res.get("dugouts") is not None and self.parameterAsBool(params, "DUGOUTS", context):
            self._load(context, out, "dugouts", "Dug pits (LiDAR)")
        if res["counts"].get("ridges"):
            self._load(context, out, "ridges", "Raised ridges (review)" if self.parameterAsBool(
                params, "RIDGES", context) else "Channel-side dikes (hold water)")
        self._load(context, out, "screening_dams", "Fills for screening (not listed)")
        return {"OUT": out}


class FindPonds(_Base):
    def name(self):
        return "ponds"

    def displayName(self):
        return "Find ponds and dugouts in a water-body layer"

    def shortHelpString(self):
        return (pd.__doc__.split("Every threshold")[0].strip().replace("\n\n", "<br><br>")
                + "<br><br>" + brand.POND_HELP + "<br><br>Every threshold is YOUR value. Screening "
                "only: the LiDAR cannot see under the water (the greatest depth is your assumption).")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "WB", "Water bodies (polygons: 3DHP / NHD waterbodies, NWI, your own)", [SRC_POLYGON]))
        self.addParameter(QgsProcessingParameterField("ID_FIELD", "Water bodies: ID field (optional)",
                                                      parentLayerParameterName="WB", optional=True))
        self.addParameter(QgsProcessingParameterField(
            "TYPE_FIELD", "Water bodies: type field (optional; 'excavated' / 'dugout' / 'pit' mark a dugout)",
            parentLayerParameterName="WB", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer("DEM", "Bare-earth DEM (LiDAR; a file or a VRT)"))
        self.addParameter(QgsProcessingParameterEnum("Z_UNIT", "DEM elevation unit", Z_UNITS, defaultValue=0))
        self._common_tail(300.0)
        pp = pd.PondParams()
        self._num("CELL", "Analysis cell size (m; 2 m = 6.6 ft)", 2.0, 0.5)
        self._num("MIN_AC", "Smallest water body (acres; YOUR value)", 0.1, 0.0)
        self._num("MAX_AC", "Largest water body (acres; YOUR value; 0 = no limit)", 50.0, 0.0)
        self._num("HEAD_FT", "Water held above the ground when the pool is above the toe by (ft; YOUR value)",
                  round(pp.min_head_m / FT, 2), 0.0)
        self._num("ARC", "Embankment pond when at least this much of the shore holds water (degrees; YOUR value)",
                  pp.min_arc_deg, 0.0)
        self._num("DEPTH_FT", "Greatest water depth to assume (ft; YOUR assumption)", round(pp.max_depth_m / FT, 1), 1.0)
        self._num("DUG_RECT", "A depression whose outline fills this share of its smallest rectangle is "
                  "a dugout (0-1; YOUR value)", 0.85, 0.0)
        self.addParameter(QgsProcessingParameterBoolean(
            "KEEP_DEP", "Also list natural depressions (prairie potholes)", False))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "WATER_ELEV", "Class-9 water-surface mosaic (water_elev.vrt from 'Build a class-9 water "
            "mosaic'; sets the pool level from the LiDAR water, not the DEM; optional)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "WATER_ELEV_BAND", "Band of the water-surface mosaic", INTEGER, 1, minValue=1))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        pp = pd.PondParams(min_head_m=self.parameterAsDouble(params, "HEAD_FT", context) * FT,
                           min_arc_deg=self.parameterAsDouble(params, "ARC", context),
                           max_depth_m=self.parameterAsDouble(params, "DEPTH_FT", context) * FT)
        out = self.parameterAsFileOutput(params, "OUT", context)
        try:
            res = finder.run_ponds(
                _src(self, params, "WB", context), _src(self, params, "DEM", context), out,
                known=_known(self, params, context), bbox=_bbox(self, params, context),
                cell=self.parameterAsDouble(params, "CELL", context), z_feet=_z(self, params, context),
                params=pp, id_field=self.parameterAsString(params, "ID_FIELD", context) or None,
                type_field=self.parameterAsString(params, "TYPE_FIELD", context) or None,
                min_ac=self.parameterAsDouble(params, "MIN_AC", context),
                max_ac=self.parameterAsDouble(params, "MAX_AC", context),
                keep_depressions=self.parameterAsBool(params, "KEEP_DEP", context),
                dug_rect=self.parameterAsDouble(params, "DUG_RECT", context),
                match_ft=self.parameterAsDouble(params, "MATCH_FT", context),
                water_elev=_src(self, params, "WATER_ELEV", context, False) or None,
                water_elev_band=self.parameterAsInt(params, "WATER_ELEV_BAND", context),
                log=self._log(feedback), progress=feedback.setProgress, cancelled=feedback.isCanceled)
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        self._report(feedback, res, "Ponds and dugouts")
        self._load(context, out, "ponds", "Ponds and dugouts")
        self._load(context, out, "screening_ponds", "Ponds for screening (not listed)")
        return {"OUT": out}


class FindCrossings(_Base):
    def name(self):
        return "crossings"

    def displayName(self):
        return "Find road and rail crossings of streams (culverts)"

    def shortHelpString(self):
        from .core import crossings
        return (crossings.__doc__.strip().replace("\n\n", "<br><br>") + "<br><br>" + brand.CROSS_HELP)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "ROADS", "Road centrelines", [SRC_LINE]))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "RAILS", "Railroad lines (optional)", [SRC_LINE], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "STREAMS", "Stream lines (3DHP / NHD flowlines)", [SRC_LINE]))
        self._common_tail(250.0)
        self.addParameter(QgsProcessingParameterVectorLayer(
            "FILLS", "Fills across streams from the first tool (optional: its 'embankments' layer)",
            [SRC_POINT], optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "DEM", "DEM for the drainage area and the expected pipe size (optional)", optional=True))
        self.addParameter(QgsProcessingParameterEnum("Z_UNIT", "DEM elevation unit", Z_UNITS, defaultValue=0))
        self._num("C", "Runoff coefficient C of the rational method (YOUR value)", 0.35, 0.0)
        self._num("INTENSITY", "Rainfall intensity (in/hr; YOUR value: NOAA Atlas 14 at the time of "
                  "concentration and your return period)", 2.0, 0.0)
        self._num("V_FPS", "Velocity in a pipe flowing full (ft/s; YOUR value)", 8.0, 0.5)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        out = self.parameterAsFileOutput(params, "OUT", context)
        try:
            res = finder.run_crossings(
                _src(self, params, "ROADS", context), _src(self, params, "STREAMS", context), out,
                known=_known(self, params, context),
                rails=_src(self, params, "RAILS", context, False) or None,
                bbox=_bbox(self, params, context), fills=_src(self, params, "FILLS", context, False) or None,
                match_ft=self.parameterAsDouble(params, "MATCH_FT", context),
                dem=_src(self, params, "DEM", context, False) or None, z_feet=_z(self, params, context),
                c_runoff=self.parameterAsDouble(params, "C", context),
                intensity_in_hr=self.parameterAsDouble(params, "INTENSITY", context),
                v_fps=self.parameterAsDouble(params, "V_FPS", context), log=self._log(feedback),
                cancelled=feedback.isCanceled)
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        self._report(feedback, res, "Road / rail x stream crossings")
        self._load(context, out, "crossings", "Road and rail crossings")
        return {"OUT": out}


class FindWater(_Base):
    def name(self):
        return "water"

    def displayName(self):
        return "Find open water in aerial imagery (pond outlines)"

    def shortHelpString(self):
        from .core import water
        return (water.__doc__.strip().replace("\n\n", "<br><br>") + "<br><br>" + brand.WATER_HELP
                + "<br><br>An image service is downloaded for the extent only (tiles are kept beside "
                "the output and reused). Every threshold is YOUR value.")

    def initAlgorithm(self, config=None):
        names = [s[0] for s in brand.IMAGE_SERVICES] + ["The image layer below",
                                                        "The image service address below"]
        self.addParameter(QgsProcessingParameterEnum(
            "SOURCE", "Imagery (pick several years to keep only the water of most of them)", names,
            allowMultiple=True, defaultValue=[0]))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "IMAGE", "Image layer (4-band or colour-infrared GeoTIFF / VRT; optional)", optional=True))
        self.addParameter(QgsProcessingParameterString(
            "URL", "Image service address (ArcGIS .../ImageServer; optional)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "GREEN", "Green band of the layer / address (a preset knows its own)", INTEGER, 2, minValue=1))
        self.addParameter(QgsProcessingParameterNumber(
            "NIR", "Near-infrared band of the layer / address (a preset knows its own)", INTEGER, 4, minValue=1))
        self.addParameter(QgsProcessingParameterExtent(
            "EXTENT", "Only this area (needed for an image service)", optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "MAPPED", "Mapped water bodies (3DHP / NHD waterbodies, NWI; optional)", [SRC_POLYGON],
            optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "BUILT", "Buildings / streets in town (drops roofs, pavement, pools; optional)", [SRC_ANY],
            optional=True))
        _folder = (QgsProcessingParameterFile.Behavior.Folder
                   if hasattr(QgsProcessingParameterFile, "Behavior")
                   else QgsProcessingParameterFile.Folder)
        self.addParameter(QgsProcessingParameterFile(
            "LAS", "Classified LiDAR tile (.las/.laz; ASPRS class 9 = water, instead of imagery)",
            extension="", optional=True))
        self.addParameter(QgsProcessingParameterFile(
            "LAS_DIR", "...or a folder of classified LiDAR tiles", behavior=_folder, optional=True))
        self.addParameter(QgsProcessingParameterString(
            "LAS_CLASSES", "LiDAR water classes (ASPRS numbers, comma-separated)", "9", optional=True))
        self.addParameter(QgsProcessingParameterString(
            "LAS_EPSG", "LiDAR tile EPSG (only if the file carries no CRS; e.g. 26914)", "",
            optional=True))
        self._num("LAS_MIN_PTS", "LiDAR: points a cell needs to count as water", 1.0, 1.0)
        self.addParameter(QgsProcessingParameterRasterLayer(
            "WATER_RASTER", "Prebuilt class-9 water-count raster / VRT (e.g. the statewide LiDAR "
            "water mosaic; instead of imagery or LAS)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "WATER_BAND", "Band of the water-count raster", INTEGER, 1, minValue=1))
        self._num("WATER_COUNT_MIN", "Water-count raster: count a cell needs to be water", 1.0, 0.0)
        self._num("CELL", "Analysis cell size (m)", 1.0, 0.1)
        self._num("THRESHOLD", "Water when NDWI is at least (YOUR value; check it on a known pond)", 0.15, -1.0)
        self._num("NIR_FRAC", "and the near-infrared is at most this share of the land's (0 = no test)", 0.25, 0.0)
        self._num("GREEN_FRAC", "and the green is at least this share of the land's (drops shadows in town; "
                  "0 = no test)", 0.35, 0.0)
        self._num("MIN_AC", "Smallest water patch (acres; YOUR value)", 0.05, 0.0)
        self._num("MAX_AC", "Largest water patch (acres; 0 = no limit)", 0.0, 0.0)
        self._num("MIN_WIDTH", "Narrowest water patch (m; drops road and rail ditches)", 10.0, 0.0)
        self._num("PERSIST", "With several years: water in at least this share of them (0-1; YOUR value)",
                  0.5, 0.0)
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def _las_sources(self, params, context):
        """The LAS/LAZ tiles to read: the single file and/or every .las/.laz in the folder."""
        import glob
        import os
        out = []
        one = (self.parameterAsString(params, "LAS", context) or "").strip()
        if one:
            out.append(one)
        folder = (self.parameterAsString(params, "LAS_DIR", context) or "").strip()
        if folder and os.path.isdir(folder):
            for pat in ("*.las", "*.laz", "*.LAS", "*.LAZ"):
                out.extend(sorted(glob.glob(os.path.join(folder, pat))))
        return out

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        import glob
        import os
        self._feedback = feedback
        bbox = _bbox(self, params, context)
        out = self.parameterAsFileOutput(params, "OUT", context)
        wr = _src(self, params, "WATER_RASTER", context, False)
        if wr:
            try:
                res = finder.run_water(
                    None, out, bbox=bbox, cell=self.parameterAsDouble(params, "CELL", context),
                    min_ac=self.parameterAsDouble(params, "MIN_AC", context),
                    max_ac=self.parameterAsDouble(params, "MAX_AC", context),
                    min_width_m=self.parameterAsDouble(params, "MIN_WIDTH", context),
                    water_raster=wr, water_raster_band=self.parameterAsInt(params, "WATER_BAND", context),
                    water_count_min=self.parameterAsDouble(params, "WATER_COUNT_MIN", context),
                    mapped=_src(self, params, "MAPPED", context, False) or None,
                    log=self._log(feedback), progress=feedback.setProgress,
                    cancelled=feedback.isCanceled)
            except RuntimeError as e:
                raise QgsProcessingException(str(e))
            self._stop_if_cancelled(feedback, out)
            feedback.pushInfo("Open water from the water-count raster: {} patch(es), {} not on the "
                              "mapped water bodies".format(res["counts"]["water"], res["counts"]["unmapped"]))
            self._load(context, out, "water", "Open water (LiDAR water mosaic)")
            return {"OUT": out}
        las = self._las_sources(params, context)
        if las:
            classes = tuple(int(c) for c in
                            (self.parameterAsString(params, "LAS_CLASSES", context) or "9").replace(
                                ";", ",").split(",") if c.strip())
            epsg = (self.parameterAsString(params, "LAS_EPSG", context) or "").strip()
            try:
                res = finder.run_water(
                    las if len(las) > 1 else las[0], out, bbox=bbox,
                    cell=self.parameterAsDouble(params, "CELL", context),
                    min_ac=self.parameterAsDouble(params, "MIN_AC", context),
                    max_ac=self.parameterAsDouble(params, "MAX_AC", context),
                    min_width_m=self.parameterAsDouble(params, "MIN_WIDTH", context),
                    las_classes=classes or (9,),
                    las_min_pts=int(self.parameterAsDouble(params, "LAS_MIN_PTS", context)),
                    las_src_epsg=int(epsg) if epsg else None,
                    mapped=_src(self, params, "MAPPED", context, False) or None,
                    log=self._log(feedback), progress=feedback.setProgress,
                    cancelled=feedback.isCanceled)
            except RuntimeError as e:
                raise QgsProcessingException(str(e))
            self._stop_if_cancelled(feedback, out)
            feedback.pushInfo("Open water from LiDAR: {} patch(es), {} not on the mapped water "
                              "bodies".format(res["counts"]["water"], res["counts"]["unmapped"]))
            self._load(context, out, "water", "Open water (LiDAR class 9)")
            return {"OUT": out}
        green = self.parameterAsInt(params, "GREEN", context)
        nir = self.parameterAsInt(params, "NIR", context)
        images = []
        for k in self.parameterAsEnums(params, "SOURCE", context) or [0]:
            if k < len(brand.IMAGE_SERVICES):
                _label, src, gb, nb = brand.IMAGE_SERVICES[k]
                images.append((src, gb, nb))
            elif k == len(brand.IMAGE_SERVICES):
                images.append((_src(self, params, "IMAGE", context), green, nir))
            else:
                src = (self.parameterAsString(params, "URL", context) or "").strip()
                if not finder.is_service(src):
                    raise QgsProcessingException("give an image service address (https://.../ImageServer)")
                images.append((src, green, nir))
        bbox = _bbox(self, params, context)
        if any(finder.is_service(i[0]) for i in images) and bbox is None:
            raise QgsProcessingException("an image service needs 'Only this area': the state at 1 m is "
                                         "far too much to download")
        out = self.parameterAsFileOutput(params, "OUT", context)
        try:
            res = finder.run_water(
                images if len(images) > 1 else images[0][0], out, bbox=bbox,
                cell=self.parameterAsDouble(params, "CELL", context),
                green_band=images[0][1], nir_band=images[0][2],
                persist=self.parameterAsDouble(params, "PERSIST", context),
                threshold=self.parameterAsDouble(params, "THRESHOLD", context),
                min_ac=self.parameterAsDouble(params, "MIN_AC", context),
                max_ac=self.parameterAsDouble(params, "MAX_AC", context),
                min_width_m=self.parameterAsDouble(params, "MIN_WIDTH", context),
                nir_frac=self.parameterAsDouble(params, "NIR_FRAC", context),
                green_frac=self.parameterAsDouble(params, "GREEN_FRAC", context),
                mapped=_src(self, params, "MAPPED", context, False) or None,
                built=_src(self, params, "BUILT", context, False) or None,
                log=self._log(feedback), progress=feedback.setProgress, cancelled=feedback.isCanceled)
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        feedback.pushInfo("Open water: {} patch(es), {} not on the mapped water bodies".format(
            res["counts"]["water"], res["counts"]["unmapped"]))
        self._load(context, out, "water", "Open water (aerial imagery)")
        return {"OUT": out}


class BuildWaterMosaic(_Base):
    def name(self):
        return "water_mosaic"

    def displayName(self):
        return "Build a class-9 water mosaic from LiDAR tiles"

    def shortHelpString(self):
        return (brand.MOSAIC_HELP + "<br><br>Output CRS: " + brand.MOSAIC_REGION
                + ". Two rasters are written: water_count (class-9 points per cell) and water_elev "
                "(their mean elevation, the pond water surface). Give the count VRT to 'Find open "
                "water' as its 'Prebuilt class-9 water-count raster'.")

    def initAlgorithm(self, config=None):
        _folder = (QgsProcessingParameterFile.Behavior.Folder
                   if hasattr(QgsProcessingParameterFile, "Behavior")
                   else QgsProcessingParameterFile.Folder)
        self.addParameter(QgsProcessingParameterFile(
            "LAS", "A single classified LiDAR tile (.las/.laz; optional)", extension="", optional=True))
        self.addParameter(QgsProcessingParameterFile(
            "LAS_DIR", "...or a folder of classified LiDAR tiles", behavior=_folder, optional=True))
        self.addParameter(QgsProcessingParameterString(
            "LAS_CLASSES", "Water classes (ASPRS numbers, comma-separated)", "9", optional=True))
        self.addParameter(QgsProcessingParameterString(
            "LAS_EPSG", "LiDAR tile EPSG (only if the file carries no CRS; e.g. 26914)", "",
            optional=True))
        self._num("CELL", "Cell size of the mosaic (m)", 1.0, 0.1)
        self.addParameter(QgsProcessingParameterBoolean(
            "POINTS", "Also count every return per cell (point_count.vrt: LiDAR drop-out evidence for "
            "'Add evidence to fills')", False))
        self.addParameter(QgsProcessingParameterFolderDestination(
            "OUT", "Output folder (water_count.vrt + water_elev.vrt + per-tile GeoTIFFs)"))

    def _las_sources(self, params, context):
        import glob
        import os
        out = []
        one = (self.parameterAsString(params, "LAS", context) or "").strip()
        if one:
            out.append(one)
        folder = (self.parameterAsString(params, "LAS_DIR", context) or "").strip()
        if folder and os.path.isdir(folder):
            for pat in ("*.las", "*.laz", "*.LAS", "*.LAZ"):
                out.extend(sorted(glob.glob(os.path.join(folder, pat))))
        return out

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        srcs = self._las_sources(params, context)
        if not srcs:
            raise QgsProcessingException("give a LiDAR tile or a folder of tiles")
        classes = tuple(int(c) for c in
                        (self.parameterAsString(params, "LAS_CLASSES", context) or "9").replace(
                            ";", ",").split(",") if c.strip()) or (9,)
        epsg = (self.parameterAsString(params, "LAS_EPSG", context) or "").strip()
        out_dir = self.parameterAsString(params, "OUT", context)
        try:
            res = finder.build_water_mosaic(
                srcs, out_dir, cell=self.parameterAsDouble(params, "CELL", context),
                classes=classes, mosaic_epsg=brand.MOSAIC_EPSG,
                src_epsg=int(epsg) if epsg else None,
                point_counts=self.parameterAsBool(params, "POINTS", context),
                log=self._log(feedback), progress=feedback.setProgress,
                cancelled=feedback.isCanceled)
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out_dir)
        feedback.pushInfo("Water mosaic: {} of {} tile(s) held water -> {} and {}".format(
            res["n_wet_tiles"], res["n_tiles"], res["count_vrt"], res["elev_vrt"]))
        if context.project() is not None:
            context.addLayerToLoadOnCompletion(
                res["count_vrt"], context.LayerDetails("Water-count mosaic", context.project()))
        if res.get("points_vrt"):
            feedback.pushInfo("Return-count mosaic -> {}".format(res["points_vrt"]))
        return {"OUT": out_dir, "COUNT_VRT": res["count_vrt"], "ELEV_VRT": res["elev_vrt"],
                "POINTS_VRT": res.get("points_vrt")}


class OsmStructures(_Base):
    def name(self):
        return "osm"

    def displayName(self):
        return "Extract dams, dikes and culverts from OpenStreetMap"

    def shortHelpString(self):
        from . import finder
        return (finder.osm_structures.__doc__.strip().replace("\n\n", "<br><br>") + "<br><br>"
                "Tags read: waterway=dam / weir, man_made=dyke / embankment, tunnel=culvert, ford=yes. "
                + brand.OSM_HELP)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFile(
            "OSM", "OpenStreetMap file (.osm.pbf or .osm)", extension=""))
        self.addParameter(QgsProcessingParameterExtent("EXTENT", "Only this area (empty = all of it)",
                                                       optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        out = self.parameterAsFileOutput(params, "OUT", context)
        try:
            finder.osm_structures(self.parameterAsFile(params, "OSM", context), out,
                                  bbox=_bbox(self, params, context), log=self._log(feedback))
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        self._load(context, out, "osm_structures", "OpenStreetMap dams, dikes, culverts")
        return {"OUT": out}


class ScoreFills(_Base):
    def name(self):
        return "score"

    def displayName(self):
        return "Score fills by how much they look like the listed dams"

    def shortHelpString(self):
        from .core import score
        return score.__doc__.strip().replace("\n\n", "<br><br>")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterMultipleLayers(
            "FILLS", "'embankments' layers from 'Find fills' runs (more areas = a better model)", SRC_POINT))
        self.addParameter(QgsProcessingParameterString(
            "WORDS", "A fill is a listed dam when it is listed on a layer whose name holds (comma-separated)",
            "dam"))
        self.addParameter(QgsProcessingParameterFile(
            "MODEL", "Use this score model instead of learning one (optional)", extension="json",
            optional=True))
        self.addParameter(QgsProcessingParameterEnum(
            "KIND", "What the model learns from", ["The 11 measurements of Finder 0.4 (v1)",
                                                   "Every 0.5 measurement, one fill per listed dam",
                                                   "As above, terrain-aware (flat / rolling / cut)",
                                                   "As above, boosted trees (bagged, positive-unlabelled)"],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterFile(
            "MARKS", "Reviewers' marks of these fills, as trusted labels (a CSV of find_id, mark; optional). "
            "Use per region: marks from one terrain can misrank another", extension="csv", optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        out = self.parameterAsFileOutput(params, "OUT", context)
        layers = []
        for lyr in self.parameterAsLayerList(params, "FILLS", context) or []:
            if lyr.providerType() != "ogr":
                raise QgsProcessingException("{} is a '{}' layer: save it to a file first".format(
                    lyr.name(), lyr.providerType()))
            layers.append(lyr.source())
        words = [w.strip() for w in (self.parameterAsString(params, "WORDS", context) or "dam").split(",")
                 if w.strip()]
        try:
            res = finder.run_score(layers, out, positive_words=words or ["dam"],
                                   model_in=self.parameterAsFile(params, "MODEL", context) or None,
                                   kind=finder.SCORE_KINDS[self.parameterAsEnum(params, "KIND", context)],
                                   marks=self.parameterAsFile(params, "MARKS", context) or None,
                                   log=self._log(feedback))
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        self._load(context, out, "scored_fills", "Fills, scored")
        return {"OUT": out, "MODEL": res.get("model_path")}


class AddEvidence(_Base):
    def name(self):
        return "evidence"

    def displayName(self):
        return "Add evidence to fills (water by year, NWI, LiDAR drop-out, what is below)"

    def shortHelpString(self):
        from .core import evidence
        return (evidence.__doc__.strip().replace("\n\n", "<br><br>") + "<br><br>Imagery, one per line: "
                "<tt>YEAR | file or ImageServer address | green=G nir=N [red=R blue=B]</tt>. "
                + brand.EVIDENCE_IMAGES_HELP)

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "FILLS", "'embankments' (or 'scored_fills') layer from 'Find fills'", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterString(
            "IMAGES", "Imagery with a near-infrared band, one per line (YEAR | source | green=G nir=N "
            "[red=R blue=B]); empty = no water history", brand.EVIDENCE_IMAGES, multiLine=True, optional=True))
        self._num("NDWI", "Open water where NDWI is over (YOUR value)", 0.15, -1.0)
        self.addParameter(QgsProcessingParameterVectorLayer(
            "NWI", "NWI wetlands (polygons with ATTRIBUTE codes; optional)", [SRC_POLYGON], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "QUARRIES", "Quarries / gravel pits (polygons, e.g. OSM landuse=quarry; optional)", [SRC_POLYGON],
            optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "LANDCOVER", "Land cover raster (USDA CDL classes; optional) - pasture share within 250 m",
            optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "SOILS", "Soils (polygons; optional)", [SRC_POLYGON], optional=True))
        self.addParameter(QgsProcessingParameterString(
            "SOIL_FIELD", "Soil field to report (e.g. drainagecl)", "drainagecl", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "LIDAR_POINTS", "LiDAR return-count raster (point_count.vrt from 'Build a class-9 water mosaic'; "
            "optional)", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "LIDAR_WATER", "LiDAR class-9 water-count raster (water_count.vrt; optional)", optional=True))
        self.addParameter(QgsProcessingParameterRasterLayer(
            "DEM", "DEM for the flow path below each fill (optional; with buildings and / or roads)",
            optional=True))
        self.addParameter(QgsProcessingParameterEnum("Z_UNIT", "DEM elevation unit", Z_UNITS, defaultValue=0))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "BUILDINGS", "Buildings (points or footprints; optional)", [SRC_ANY], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            "ROADS", "Roads / railroads (lines; optional)", [SRC_LINE], optional=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        out = self.parameterAsFileOutput(params, "OUT", context)
        opt = lambda n: _src(self, params, n, context, required=False) or None  # noqa: E731
        try:
            res = finder.run_evidence(
                _src(self, params, "FILLS", context), out,
                images=self.parameterAsString(params, "IMAGES", context) or None,
                ndwi_threshold=self.parameterAsDouble(params, "NDWI", context),
                nwi=opt("NWI"), quarries=opt("QUARRIES"), landcover=opt("LANDCOVER"), soils=opt("SOILS"),
                soil_field=(self.parameterAsString(params, "SOIL_FIELD", context) or None),
                lidar_points=opt("LIDAR_POINTS"), lidar_water=opt("LIDAR_WATER"), dem=opt("DEM"),
                z_feet=_z(self, params, context), buildings=opt("BUILDINGS"), roads=opt("ROADS"),
                log=self._log(feedback), progress=feedback.setProgress, cancelled=feedback.isCanceled)
        except (RuntimeError, ValueError) as e:
            raise QgsProcessingException(str(e))
        self._stop_if_cancelled(feedback, out)
        self._load(context, out, "evidence", "Fills with evidence")
        return {"OUT": out}


class ReviewBatch(_Base):
    def name(self):
        return "review_batch"

    def displayName(self):
        return "Pick a review batch (random control, top-ranked, near the cut)"

    def shortHelpString(self):
        from .core import review
        return review.__doc__.strip().replace("\n\n", "<br><br>")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            "FILLS", "Scored fills ('embankments', 'scored_fills' or 'evidence' layer)", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterNumber("N", "Cards in the batch", INTEGER, 90, minValue=5))
        self._num("RANDOM", "Share that is a fixed random control", 0.18, 0.0)
        self._num("TOP", "Share from the top of the ranking", 0.37, 0.0)
        self._num("SECOND", "Share flagged for a blind second reviewer", 0.12, 0.0)
        self.addParameter(QgsProcessingParameterNumber("CAP", "At most this many near-the-cut cards from one "
                                                       "5 km tile", INTEGER, 3, minValue=1))
        self.addParameter(QgsProcessingParameterFile(
            "REVIEWED", "Cards already reviewed (any text / CSV file holding their FIND- ids; optional)",
            extension="", optional=True))
        self.addParameter(QgsProcessingParameterNumber("SEED", "Random seed (keep it: the control stays put, and a later round takes the next cards)",
                                                       INTEGER, 0, minValue=0))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Output GeoPackage", "GeoPackage (*.gpkg)"))

    def processAlgorithm(self, params, context, feedback):
        from . import finder
        self._feedback = feedback
        out = self.parameterAsFileOutput(params, "OUT", context)
        rnd = self.parameterAsDouble(params, "RANDOM", context)
        top = self.parameterAsDouble(params, "TOP", context)
        if rnd + top > 1.0:
            raise QgsProcessingException("the random and top shares add up to more than the batch")
        try:
            finder.run_review_batch(
                _src(self, params, "FILLS", context), out, n=self.parameterAsInt(params, "N", context),
                random_frac=rnd, top_frac=top, second_frac=self.parameterAsDouble(params, "SECOND", context),
                per_tile_cap=self.parameterAsInt(params, "CAP", context),
                reviewed=self.parameterAsFile(params, "REVIEWED", context) or None,
                seed=self.parameterAsInt(params, "SEED", context), log=self._log(feedback))
        except RuntimeError as e:
            raise QgsProcessingException(str(e))
        self._load(context, out, "review_batch", "Review batch")
        return {"OUT": out}


class Provider(QgsProcessingProvider):
    def id(self):
        return brand.PROVIDER_ID

    def name(self):
        return brand.NAME

    def longName(self):
        return brand.NAME + " - structures not on the lists"

    def icon(self):
        from qgis.PyQt.QtGui import QIcon
        import os
        return QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))

    def loadAlgorithms(self):
        for alg in (FindEmbankments(), FindPonds(), FindCrossings(), FindWater(), BuildWaterMosaic(),
                    OsmStructures(), ScoreFills(), AddEvidence(), ReviewBatch()):
            self.addAlgorithm(alg)


class Plugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initProcessing(self):
        self.provider = Provider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None
