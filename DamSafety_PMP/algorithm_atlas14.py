"""NOAA Atlas 14 tools (group "NOAA Atlas 14"): the DamIT PMP Atlas 14 tools on the shared core in noaa14/.

ndpmp:atlas14_point       Atlas 14 estimates at a point (PFDS), table + JSON with source and citation   (DamIT pointpf)
ndpmp:atlas14_basin       the same at sample points in a basin, mean or max                             (DamIT basinpf)
ndpmp:atlas14_fetchgrid   an Atlas 14 GIS grid from NOAA, clipped, GeoTIFF in inches                    (DamIT fetchgrid)
ndpmp:atlas14_gridbasin   basin mean / min / max of an Atlas 14 grid (a file, or fetched)               (DamIT gridbasin)
ndpmp:atlas14_hyetograph  Atlas 14 frequency storms as nd_pmp hyetograph CSVs for inflow_hydrograph      (DamIT hyetograph)

The numbers come only from noaa14/ (byte-identical with DamIT PMP; see noaa14/__init__.py). The network is
noaa14_http.py; the files are atlas14_outputs.py. GDAL work runs inside gdalsafe.gdal_exceptions() so the GDAL
exception mode of QGIS and the other plugins is left as it was (audit R3). Not ported: DamIT's raingrid,
anugarain, lisfloodrain and watershed tools.
"""
import json
import math
import os

from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFeatureSink, QgsField,
                       QgsFields, QgsGeometry, QgsPointXY, QgsProcessing, QgsProcessingAlgorithm,
                       QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputFolder,
                       QgsProcessingOutputNumber, QgsProcessingOutputString, QgsProcessingParameterBoolean,
                       QgsProcessingParameterDefinition, QgsProcessingParameterEnum, QgsProcessingParameterExtent,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterFileDestination,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber,
                       QgsProcessingParameterPoint, QgsProcessingParameterRasterDestination,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterString, QgsProject, QgsSettings,
                       QgsWkbTypes)

from . import atlas14_outputs as O
from . import noaa14_http as http
from .core import gdalsafe as GS
from .noaa14 import atlas14 as A
from .noaa14 import gisgrid as G
from .noaa14 import temporal as T

try:            # Core 1.1.0: number inputs in the chosen display unit system (SI or US)
    from DamSafety_Core.ui.unit_widgets import UnitParamsMixin as _UnitParams
except Exception:                                                 # noqa - Core older than 1.1.0
    class _UnitParams(object):
        pass

SETTING_CACHE = "nd_pmp/atlas14_cache"   # QgsSettings key: the Atlas 14 download / PFDS answer cache folder
SERIES = ["pds", "ams"]
UNITS = ["english", "metric"]
DATA = ["depth", "intensity"]
COMBINE = ["mean", "max"]
WGS84 = QgsCoordinateReferenceSystem("EPSG:4326")
FOLDER = getattr(QgsProcessingParameterFile, "Folder", None) or QgsProcessingParameterFile.Behavior.Folder
DOUBLE = QgsProcessingParameterNumber.Double if hasattr(QgsProcessingParameterNumber, "Double") \
    else QgsProcessingParameterNumber.Type.Double
INTEGER = QgsProcessingParameterNumber.Integer if hasattr(QgsProcessingParameterNumber, "Integer") \
    else QgsProcessingParameterNumber.Type.Integer


def _advanced_flag():
    """The 'advanced parameter' flag: Qgis.ProcessingParameterFlag.Advanced (QGIS 3.36+ / 4), else the old spelling."""
    try:
        from qgis.core import Qgis
        return Qgis.ProcessingParameterFlag.Advanced
    except AttributeError:
        return QgsProcessingParameterDefinition.FlagAdvanced


ADVANCED = _advanced_flag()
try:
    from qgis.core import Qgis
    WKB_POINT = Qgis.WkbType.Point                                # QGIS 3.30+ / 4
except (ImportError, AttributeError):
    WKB_POINT = QgsWkbTypes.Point
NOTE = O.NOTE
FREQ_HELP = "Frequency in years (ARI for PDS: 1, 2, 5, 10, 25, 50, 100, 200, 500, 1000; 1/AEP for AMS: 2 ... 1000)"
BOUNDS = [b for b, _ in G.BOUNDS]
BOUND_LABELS = ["Estimate", "Lower 90% confidence limit", "Upper 90% confidence limit"]
REGION_LABELS = ["Auto (from the point / extent, via PFDS)"] + ["{}: Volume {} - {}".format(c, v, n) for c, v, n in G.REGIONS]
REGION_IDS = [None] + list(G.REGION_CODES)
# the one Atlas 14 duration list (atlas14.DURATION_LABELS): STORM index i is DURATION index i in every tool
DURATION_LABELS = A.DURATION_LABELS
STORM = DURATION_LABELS


def _field_type(kind):
    """QgsField type for 'Double' / 'String' (QMetaType on QGIS >= 3.38 and 4, the legacy variant type before)."""
    try:
        from qgis.PyQt.QtCore import QMetaType
        t = getattr(getattr(QMetaType, "Type", QMetaType), {"Double": "Double", "String": "QString"}[kind])
        probe = QgsField("probe", t)
        if int(getattr(probe.type(), "value", probe.type())) == int(getattr(t, "value", t)):
            return t
    except Exception:                                             # noqa: BLE001
        pass
    from qgis.PyQt.QtCore import QVariant                         # QGIS < 3.38 (Qt5)
    return getattr(QVariant, kind)


def _fields(spec):
    fs = QgsFields()
    for name, kind in spec:
        fs.append(QgsField(name, _field_type(kind)))
    return fs


def _to_wgs84(pt, crs):
    if crs.isValid() and crs != WGS84:
        return QgsCoordinateTransform(crs, WGS84, QgsProject.instance()).transform(pt)
    return pt


def _geom_wgs84(src_geom, crs):
    g = QgsGeometry(src_geom)
    if crs.isValid() and crs != WGS84:
        g.transform(QgsCoordinateTransform(crs, WGS84, QgsProject.instance()))
    return g


class _Base(_UnitParams, QgsProcessingAlgorithm):
    http_client = None                    # tests set a fake: get_bytes(url, timeout)
    downloader = None                     # tests set a fake: download(url, dest) (a plain function on the class)
    pause_s = 1.0

    def group(self):
        return "NOAA Atlas 14"

    def groupId(self):  # noqa: N802
        return "atlas14"

    def createInstance(self):  # noqa: N802
        return type(self)()

    def _cache(self, P, context, out_path):
        """CACHE_DIR (remembered in nd_pmp/atlas14_cache), else the remembered folder, else pfds_cache next to the output."""
        given = self.parameterAsString(P, "CACHE_DIR", context)
        if given:
            QgsSettings().setValue(SETTING_CACHE, given)
            return given
        return QgsSettings().value(SETTING_CACHE, "") or \
            os.path.join(os.path.dirname(os.path.abspath(out_path)), "pfds_cache")

    def _client(self):
        return self.http_client or http.Client()

    def _download(self, feedback, timeout, max_bytes):
        fake = type(self).downloader

        def download(url, dest):
            feedback.pushInfo("Downloading {}".format(url))
            if fake is not None:
                return fake(url, dest)
            return http.download_file(url, dest, timeout=timeout, max_bytes=max_bytes, cancelled=feedback.isCanceled)
        return download

    def _pfds_params(self):
        self.addParameter(QgsProcessingParameterEnum("SERIES", "Series (pds = partial duration / ARI, ams = annual maximum / AEP)",
                                                     SERIES, defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum("UNITS", "Units", UNITS, defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum("DATA", "Estimate", DATA, defaultValue=0))
        self._cache_params("PFDS answer cache folder")

    def _cache_params(self, what, advanced=False):
        defs = [QgsProcessingParameterFile("CACHE_DIR", "{} (empty = the last one used, else pfds_cache next to the "
                                           "outputs)".format(what), behavior=FOLDER, optional=True),
                QgsProcessingParameterBoolean("OFFLINE", "Offline (cached downloads / PFDS answers only)", defaultValue=False)]
        for d in defs:
            if advanced:
                d.setFlags(d.flags() | ADVANCED)
            self.addParameter(d)

    def _pfds(self, P, context, out_path):
        return {"http": self._client(), "cache_dir": self._cache(P, context, out_path),
                "data": DATA[self.parameterAsEnum(P, "DATA", context)],
                "units": UNITS[self.parameterAsEnum(P, "UNITS", context)],
                "series": SERIES[self.parameterAsEnum(P, "SERIES", context)],
                "offline": self.parameterAsBool(P, "OFFLINE", context)}

    def _run_fetch(self, points, kw):
        try:
            return A.fetch_many(points, kw["http"], kw["cache_dir"], pause_s=self.pause_s, data=kw["data"],
                                units=kw["units"], series=kw["series"], offline=kw["offline"])
        except A.Atlas14Error as e:
            raise QgsProcessingException(str(e))
        except IOError as e:
            raise QgsProcessingException("PFDS request failed: {}".format(e))


class Atlas14PointAlgorithm(_Base):
    def name(self):
        return "atlas14_point"

    def displayName(self):  # noqa: N802
        return "Atlas 14 estimates at a point"

    def shortHelpString(self):  # noqa: N802
        return ("NOAA Atlas 14 precipitation-frequency estimates (19 durations, 5 min - 60 days; PDS 1 - 1000-year "
                "ARI or AMS 1/2 - 1/1000 AEP; 90% bounds) at one point, from PFDS. Writes a CSV table and a JSON "
                "file with the Atlas 14 volume, version, region and citation; atlas14_hyetograph reads the JSON. "
                "The PFDS query goes through QGIS networking (Settings > Options > Network proxy); answers are cached. "
                + NOTE)

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterPoint("POINT", "Point"))
        self._pfds_params()
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT_CSV", "Table (CSV)", "CSV files (*.csv)"))
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT_JSON", "Estimates and source (JSON)", "JSON files (*.json)"))
        self.addOutput(QgsProcessingOutputString("VOLUME", "Atlas 14 volume and version"))
        self.addOutput(QgsProcessingOutputNumber("DEPTH_100YR_24HR", "100-year 24-hour estimate (PDS / AMS 1%)"))

    def processAlgorithm(self, P, context, feedback):  # noqa: N802
        pt = _to_wgs84(self.parameterAsPoint(P, "POINT", context), self.parameterAsPointCrs(P, "POINT", context))
        csv_path = self.parameterAsFileOutput(P, "OUTPUT_CSV", context)
        json_path = self.parameterAsFileOutput(P, "OUTPUT_JSON", context)
        kw = self._pfds(P, context, json_path)
        (pf, src), = self._run_fetch([(pt.y(), pt.x())], kw)
        O.write_outputs(csv_path, json_path, pf, [src], {"location": {"lat": pt.y(), "lon": pt.x()}})
        feedback.pushInfo(A.citation(pf))
        v = pf["quantiles"][A.duration_index("24-hr")][A.freq_index(pf, 100)]
        return {"OUTPUT_CSV": csv_path, "OUTPUT_JSON": json_path,
                "VOLUME": "Volume {} Version {} ({})".format(pf["volume"], pf["version"], pf["region"]),
                "DEPTH_100YR_24HR": v}


def sample_points(geom_wgs84, max_points):
    """pointOnSurface plus up to max_points-1 grid points inside the basin (WGS84 geometry)."""
    pts = [geom_wgs84.pointOnSurface().asPoint()]
    if max_points <= 1:
        return pts
    bb = geom_wgs84.boundingBox()
    n = 2
    inside = []
    while n <= 12:
        inside = []
        for i in range(n):
            for j in range(n):
                p = QgsPointXY(bb.xMinimum() + (i + 0.5) * bb.width() / n, bb.yMinimum() + (j + 0.5) * bb.height() / n)
                if geom_wgs84.contains(p):
                    inside.append(p)
        if len(inside) >= max_points - 1:
            break
        n += 1
    if len(inside) > max_points - 1:
        step = len(inside) / float(max_points - 1)
        inside = [inside[int(k * step)] for k in range(max_points - 1)]
    return pts + inside


class Atlas14BasinAlgorithm(_Base):
    def name(self):
        return "atlas14_basin"

    def displayName(self):  # noqa: N802
        return "Atlas 14 estimates for a basin (sampled points)"

    def shortHelpString(self):  # noqa: N802
        return ("Queries PFDS at a point on the basin and up to MAX_POINTS - 1 evenly spread points inside it (one "
                "polite request per second, answers cached) and combines the Atlas 14 tables cell by cell (mean or "
                "max). Point estimates are not areally reduced: Atlas 14 publishes point values only. For a basin "
                "mean over the Atlas 14 grid use atlas14_gridbasin. " + NOTE)

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource("BASIN", "Basin (polygons; all features together)",
                                                              [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterNumber("MAX_POINTS", "Sample points (max)", INTEGER, defaultValue=9,
                                                       minValue=1, maxValue=50))
        self.addParameter(QgsProcessingParameterEnum("COMBINE", "Combine", COMBINE, defaultValue=0))
        self._pfds_params()
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT_CSV", "Table (CSV)", "CSV files (*.csv)"))
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT_JSON", "Estimates and sources (JSON)", "JSON files (*.json)"))
        self.addParameter(QgsProcessingParameterFeatureSink("POINTS", "Sample points", QgsProcessing.TypeVectorPoint,
                                                            optional=True, createByDefault=True))
        self.addOutput(QgsProcessingOutputString("VOLUME", "Atlas 14 volume(s)"))
        self.addOutput(QgsProcessingOutputNumber("DEPTH_100YR_24HR", "100-year 24-hour estimate (combined)"))

    def processAlgorithm(self, P, context, feedback):  # noqa: N802
        src = self.parameterAsSource(P, "BASIN", context)
        geoms = [f.geometry() for f in src.getFeatures() if not f.geometry().isEmpty()]
        if not geoms:
            raise QgsProcessingException("The basin layer has no polygon.")
        g = _geom_wgs84(QgsGeometry.unaryUnion(geoms), src.sourceCrs())
        pts = sample_points(g, self.parameterAsInt(P, "MAX_POINTS", context))
        csv_path = self.parameterAsFileOutput(P, "OUTPUT_CSV", context)
        json_path = self.parameterAsFileOutput(P, "OUTPUT_JSON", context)
        kw = self._pfds(P, context, json_path)
        feedback.pushInfo("{} PFDS point(s)".format(len(pts)))
        got = self._run_fetch([(p.y(), p.x()) for p in pts], kw)
        how = COMBINE[self.parameterAsEnum(P, "COMBINE", context)]
        try:
            pf = A.combine([x[0] for x in got], how)
        except A.Atlas14Error as e:
            raise QgsProcessingException(str(e))
        O.write_outputs(csv_path, json_path, pf, [x[1] for x in got],
                        {"basin": {"points": [{"lat": p.y(), "lon": p.x()} for p in pts], "combine": how,
                                   "areal_reduction": "none (Atlas 14 point estimates)"}})
        di, fi = A.duration_index("24-hr"), A.freq_index(pf, 100)
        out = {"OUTPUT_CSV": csv_path, "OUTPUT_JSON": json_path, "VOLUME": ", ".join(pf["volumes"]),
               "DEPTH_100YR_24HR": pf["quantiles"][di][fi]}
        sink, dest = self.parameterAsSink(P, "POINTS", context, _fields(
            [("lat", "Double"), ("lon", "Double"), ("volume", "String"), ("d100_24h", "Double")]), WKB_POINT, WGS84)
        if sink is not None:
            for p, (one, s) in zip(pts, got):
                f = QgsFeature()
                f.setGeometry(QgsGeometry.fromPointXY(p))
                f.setAttributes([p.y(), p.x(), "{} v{}".format(one["volume"], one["version"]), one["quantiles"][di][fi]])
                sink.addFeature(f, QgsFeatureSink.FastInsert)
            out["POINTS"] = dest
        return out


# ---------------------------------------------------------------------------------------------------------
# Atlas 14 GIS grids

class _GridChoice(_Base):
    """The region / series / frequency / duration / bound choices shared by fetchgrid and gridbasin."""

    def _choice_params(self, advanced=False):
        defs = [QgsProcessingParameterEnum("REGION", "Atlas 14 volume folder (Auto = from the point / extent, asks PFDS)",
                                           REGION_LABELS, defaultValue=0),
                QgsProcessingParameterEnum("SERIES", "Series (pds = partial duration / ARI, ams = annual maximum / AEP)",
                                           SERIES, defaultValue=0),
                QgsProcessingParameterNumber("FREQUENCY", FREQ_HELP, INTEGER, defaultValue=100, minValue=1, maxValue=1000),
                QgsProcessingParameterEnum("DURATION", "Duration (the 19 Atlas 14 durations, 5 minutes - 60 days)",
                                           DURATION_LABELS, defaultValue=A.DEFAULT_DURATION_INDEX),
                QgsProcessingParameterEnum("BOUND", "Estimate or 90% confidence bound", BOUND_LABELS, defaultValue=0)]
        for d in defs:
            if advanced and d.name() in ("REGION", "SERIES", "BOUND"):
                d.setFlags(d.flags() | ADVANCED)
            self.addParameter(d)
        self._cache_params("Download cache folder", advanced)

    def _choice(self, P, context):
        return {"region": REGION_IDS[self.parameterAsEnum(P, "REGION", context)],
                "series": SERIES[self.parameterAsEnum(P, "SERIES", context)],
                "years": self.parameterAsInt(P, "FREQUENCY", context),
                "duration": DURATION_LABELS[self.parameterAsEnum(P, "DURATION", context)],
                "bound": BOUNDS[self.parameterAsEnum(P, "BOUND", context)],
                "offline": self.parameterAsBool(P, "OFFLINE", context)}

    def _fetch_grid(self, ch, cache, det_point, window, mask_wkt, feedback):
        """Fetch (or reuse) the zip and clip it. det_point = (lat, lon) used only to find the region.
        Returns (array, gt, record). Raises QgsProcessingException with a plain message."""
        region, pf = ch["region"], None
        try:
            G.check_choice(region or "mw", ch["years"], ch["duration"], ch["bound"], ch["series"])
            if region is None:
                if det_point is None:
                    raise QgsProcessingException("Give a point, an extent or a basin so the Atlas 14 region can be found, "
                                                 "or choose the region.")
                pf, _ = A.fetch(det_point[0], det_point[1], self._client(), cache, offline=ch["offline"])
                region = G.region_from_pfds(pf)
                feedback.pushInfo("PFDS: {} (volume {} version {}) -> GIS folder {}".format(
                    pf["region"], pf["volume"], pf["version"], region))
                G.check_choice(region, ch["years"], ch["duration"], ch["bound"], ch["series"])
            zpath, rec = G.fetch_zip(region, ch["years"], ch["duration"], ch["bound"], ch["series"], cache,
                                     self._download(feedback, 300, 300 * 10 ** 6), offline=ch["offline"])
            feedback.pushInfo("{} ({} bytes, {})".format(rec["url"], rec["bytes"], "cached" if rec["from_cache"] else "downloaded"))
            with GS.gdal_exceptions():
                arr, gt, _ = G.read_clip(zpath, window, mask_wkt)
            meta = G.zip_metadata(zpath)
        except A.Atlas14Error as e:
            raise QgsProcessingException(str(e))
        except (IOError, RuntimeError) as e:
            raise QgsProcessingException("Atlas 14 GIS download or read failed ({}). Use OFFLINE with a cached zip, or "
                                         "check the network.".format(e))
        rec.update({"region": region, "series": ch["series"], "frequency_years": ch["years"], "duration": ch["duration"],
                    "bound": ch["bound"], "zip_metadata": meta})
        if pf is not None:
            rec["pfds_region"] = {"region": pf["region"], "volume": pf["volume"], "version": pf["version"],
                                  "citation": A.citation(pf)}
        return arr, gt, rec


class Atlas14FetchGridAlgorithm(_GridChoice):
    def name(self):
        return "atlas14_fetchgrid"

    def displayName(self):  # noqa: N802
        return "Fetch an Atlas 14 precipitation-frequency grid (GIS format)"

    def shortHelpString(self):  # noqa: N802
        return ("Downloads the NOAA Atlas 14 'PF in GIS format' grid for one frequency and duration from the PFDS GIS data "
                "source (https://hdsc.nws.noaa.gov/pub/hdsc/data/<region>/<region><ARI>yr<dur><a|al|au>[_ams].zip, "
                "the same files as https://hdsc.nws.noaa.gov/pfds/pfds_gis.html), clips it to the point buffer, extent "
                "or basin you give, and writes a GeoTIFF in INCHES (NOAA's integer thousandths of an inch x 0.001; "
                "Float32, NAD83 geographic, nodata -9999) plus a JSON sidecar (<tif>.json, always written; OUTPUT_JSON "
                "adds a copy) with the source URL, fetch time, SHA-256, Atlas 14 volume and citation; atlas14_gridbasin "
                "reads the GeoTIFF in inches only when the sidecar sits beside it. With MASK_TO_BASIN, every cell the "
                "basin touches is kept. Choose any of the 10 ARIs (PDS) or 9 AEPs (AMS), any of "
                "the 19 durations, and the estimate or its 90% lower / upper bound. The region comes from the point / "
                "extent (via a PFDS point query) unless you choose it. Without a clip area the whole regional grid "
                "(about 25 MB of cells) is written. Downloads (3-4 MB zips) are cached; OFFLINE uses the cache only. "
                "Outside Atlas 14 coverage, or for a file NOAA does not publish, it stops with a message. " + NOTE)

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterPoint("POINT", "Point (finds the region; clipped to a buffer when no "
                                                      "extent or basin is given)", optional=True))
        self.addParameter(QgsProcessingParameterExtent("EXTENT", "Clip extent", optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource("BASIN", "Clip to basin (polygons)",
                                                              [QgsProcessing.TypeVectorPolygon], optional=True))
        self.addParameter(QgsProcessingParameterBoolean("MASK_TO_BASIN", "Blank cells outside the basin polygons "
                                                        "(cells the basin touches are kept)",
                                                        defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber("BUFFER_KM", "Buffer around the point (km)", DOUBLE,
                                                       defaultValue=5.0, minValue=0.0, maxValue=500.0))
        self._choice_params(advanced=False)
        self.addParameter(QgsProcessingParameterRasterDestination("OUTPUT", "Atlas 14 grid (GeoTIFF, inches)"))
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT_JSON", "Copy of the source record (JSON; <GeoTIFF>.json "
                                                                "is always written)",
                                                                "JSON files (*.json)", optional=True, createByDefault=False))
        self.addOutput(QgsProcessingOutputNumber("MEAN", "Mean of the clipped grid (in)"))
        self.addOutput(QgsProcessingOutputNumber("POINT_VALUE", "Value at the point (in), -1 if no point"))
        self.addOutput(QgsProcessingOutputString("URL", "NOAA file"))

    def processAlgorithm(self, P, context, feedback):  # noqa: N802
        ch = self._choice(P, context)
        out = self.parameterAsOutputLayer(P, "OUTPUT", context)
        side = G.sidecar_path(out)              # always: gridbasin reads the GeoTIFF in inches only with it
        copy = self.parameterAsFileOutput(P, "OUTPUT_JSON", context)
        cache = self._cache(P, context, out)
        pt = None
        if P.get("POINT") not in (None, ""):
            pt = _to_wgs84(self.parameterAsPoint(P, "POINT", context), self.parameterAsPointCrs(P, "POINT", context))
        window, det, mask_wkt = None, None, None
        basin = self.parameterAsSource(P, "BASIN", context) if P.get("BASIN") not in (None, "") else None
        if basin is not None:
            geoms = [f.geometry() for f in basin.getFeatures() if not f.geometry().isEmpty()]
            if not geoms:
                raise QgsProcessingException("The basin layer has no polygon.")
            g = _geom_wgs84(QgsGeometry.unaryUnion(geoms), basin.sourceCrs())
            bb = g.boundingBox()
            window = (bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum())
            c = g.pointOnSurface().asPoint()
            det = (c.y(), c.x())
            if self.parameterAsBool(P, "MASK_TO_BASIN", context):
                mask_wkt = g.asWkt()
        elif P.get("EXTENT") not in (None, ""):
            bb = self.parameterAsExtent(P, "EXTENT", context, WGS84)
            if bb.isNull() or bb.isEmpty():
                raise QgsProcessingException("The clip extent is empty.")
            window = (bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum())
            det = (bb.center().y(), bb.center().x())
        elif pt is not None:
            km = self.parameterAsDouble(P, "BUFFER_KM", context)
            dlat = km / 111.32
            dlon = km / (111.32 * max(math.cos(math.radians(pt.y())), 0.01))
            window = (pt.x() - dlon, pt.y() - dlat, pt.x() + dlon, pt.y() + dlat)
        if pt is not None:
            det = (pt.y(), pt.x())                       # an explicit point is the best region probe
        arr, gt, rec = self._fetch_grid(ch, cache, det, window, mask_wkt, feedback)
        with GS.gdal_exceptions():
            G.write_geotiff(out, arr, gt)
        st = G.array_stats(arr)
        pv = G.value_at(arr, gt, pt.x(), pt.y()) if pt is not None else None
        vol = rec.get("pfds_region") or {}
        zm = rec.get("zip_metadata") or {}
        doc = {"schema": "damit.pmp.atlas14_gis", "schema_version": 1, "units": "in", "value_scale": 1.0,
               "grid_values": "NOAA integer thousandths of an inch x 0.001 = inches", "crs": "EPSG:4269 (NAD83 geographic)",
               "nodata": G.NODATA_OUT, "raster": out,
               "source": "NOAA Atlas 14 GIS grid, NOAA/NWS Hydrometeorological Design Studies Center ({})".format(G.GIS_PAGE),
               "url": rec["url"], "fetched": rec["fetched"], "from_cache": rec["from_cache"], "zip": rec["zip"],
               "zip_bytes": rec["bytes"], "sha256": rec["sha256"], "region": rec["region"], "series": ch["series"],
               "frequency_years": ch["years"], "duration": ch["duration"], "bound": ch["bound"],
               "clip": {"window_lon_lat": window, "masked_to_basin": bool(mask_wkt),
                        "buffer_km": self.parameterAsDouble(P, "BUFFER_KM", context) if (window and basin is None
                                                                                          and P.get("EXTENT") in (None, "")) else None},
               "stats_in": st, "point": ({"lon": pt.x(), "lat": pt.y(), "value_in": pv} if pt is not None else None),
               "atlas14_volume": vol or None, "grid_metadata": zm,
               "citation": vol.get("citation") or G.citation(rec["region"], zm.get("pubdate")),
               "written_by": "ndpmp:atlas14_fetchgrid", "note": NOTE}
        for path in [side] + ([copy] if copy and os.path.abspath(copy) != os.path.abspath(side) else []):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1)
        feedback.pushInfo("mean {} in, min {}, max {} ({} cells){}".format(
            st["mean"], st["min"], st["max"], st["cells"], "; point {} in".format(pv) if pv is not None else ""))
        return {"OUTPUT": out, "OUTPUT_JSON": copy or side, "MEAN": st["mean"],
                "POINT_VALUE": pv if pv is not None else -1.0,
                "URL": rec["url"]}


class Atlas14GridBasinAlgorithm(_GridChoice):
    def name(self):
        return "atlas14_gridbasin"

    def displayName(self):  # noqa: N802
        return "Basin statistics of an Atlas 14 grid"

    def shortHelpString(self):  # noqa: N802
        return ("Mean, minimum and maximum of a NOAA Atlas 14 GIS grid over the basin. Give a grid you have (NOAA's file, "
                "values in thousandths of an inch, SCALE 0.001; the frequency and duration are read from the standard "
                "file name, e.g. mw100yr24ha.asc, or an atlas14_fetchgrid GeoTIFF, already in inches) OR leave GRID "
                "empty and choose the frequency, duration, series, bound and region (as in atlas14_fetchgrid): the "
                "tool then fetches the NOAA grid itself (cached) and reports the basin statistics. The basin is a "
                "polygon you supply; every cell it touches counts. Atlas 14 values are point estimates (no areal "
                "reduction). " + NOTE)

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterRasterLayer("GRID", "NOAA Atlas 14 grid (empty = fetch from NOAA with the "
                                                            "choices below)", optional=True))
        self.addParameter(QgsProcessingParameterFeatureSource("BASIN", "Basin (polygons)", [QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterNumber("SCALE", "Grid value to inches (NOAA files 0.001; fetchgrid "
                                                       "GeoTIFFs are read as 1)", DOUBLE, defaultValue=0.001))
        self._choice_params(advanced=False)
        self.addParameter(QgsProcessingParameterFileDestination("OUTPUT_JSON", "Statistics (JSON)", "JSON files (*.json)"))
        self.addOutput(QgsProcessingOutputNumber("MEAN", "Basin mean (in)"))

    def processAlgorithm(self, P, context, feedback):  # noqa: N802
        src = self.parameterAsSource(P, "BASIN", context)
        geoms = [f.geometry() for f in src.getFeatures() if not f.geometry().isEmpty()]
        if not geoms:
            raise QgsProcessingException("The basin layer has no polygon.")
        g = QgsGeometry.unaryUnion(geoms)
        out = self.parameterAsFileOutput(P, "OUTPUT_JSON", context)
        grid = self.parameterAsRasterLayer(P, "GRID", context) if P.get("GRID") not in (None, "") else None
        fetch = None
        if grid is not None:
            try:
                with GS.gdal_exceptions():
                    from .noaa14 import gridstats            # 0.7.1: no longer changes the GDAL mode at import
                    scale = gridstats.grid_scale(grid.source(), self.parameterAsDouble(P, "SCALE", context))
                    st = gridstats.basin_stats(grid.source(), g.asWkt(), src.sourceCrs().toWkt(), scale)
            except (ValueError, RuntimeError) as e:
                raise QgsProcessingException(str(e))
            name = gridstats.parse_name(grid.source())
            side = G.read_sidecar(grid.source())
            if side:
                name = dict(name, **{k: side[k] for k in ("region", "frequency_years", "duration", "series", "bound")
                                     if k in side})
            gname, gsrc = grid.source(), ("NOAA Atlas 14 GIS grid supplied by the user (https://hdsc.nws.noaa.gov/pfds/)"
                                          if not side else "atlas14_fetchgrid GeoTIFF; NOAA file " + str(side.get("url")))
        else:
            ch = self._choice(P, context)
            wg = _geom_wgs84(g, src.sourceCrs())
            bb = wg.boundingBox()
            c = wg.pointOnSurface().asPoint()
            arr, gt, fetch = self._fetch_grid(ch, self._cache(P, context, out), (c.y(), c.x()),
                                              (bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum()), wg.asWkt(),
                                              feedback)
            try:
                st = G.array_stats(arr)
            except ValueError:
                raise QgsProcessingException("no Atlas 14 grid cells with values inside the basin (outside Atlas 14 coverage?)")
            name = {"region_code": fetch["region"], "frequency_years": ch["years"], "duration": ch["duration"],
                    "series": ch["series"]}
            if ch["bound"] != "estimate":
                name["bound"] = ch["bound"]
            gname, gsrc = fetch["url"], "NOAA Atlas 14 GIS grid fetched by ndpmp:atlas14_gridbasin ({})".format(G.GIS_PAGE)
        doc = {"schema": "damit.pmp.atlas14_grid", "schema_version": 1, "grid": gname,
               "grid_name": name, "units": "in", "stats": st, "source": gsrc,
               "areal_reduction": "none (Atlas 14 point estimates)", "note": NOTE}
        if fetch:
            doc["fetch"] = {k: fetch[k] for k in ("url", "fetched", "from_cache", "sha256", "zip") if k in fetch}
            doc["fetch"]["pfds_region"] = fetch.get("pfds_region")
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        feedback.pushInfo("basin mean {} in, min {}, max {} ({} cells)".format(st["mean"], st["min"], st["max"], st["cells"]))
        return {"OUTPUT_JSON": out, "MEAN": st["mean"]}


# ---------------------------------------------------------------------------------------------------------
# Frequency storms for the DWR chain: atlas14_hyetograph -> inflow_hydrograph -> ndbreach:spillwayadequacy

class Atlas14HyetographAlgorithm(_Base):
    def name(self):
        return "atlas14_hyetograph"

    def displayName(self):  # noqa: N802
        return "Atlas 14 design storms (hyetographs for inflow_hydrograph)"

    def shortHelpString(self):  # noqa: N802
        return ("Builds NOAA Atlas 14 frequency storms and writes each one as an nd_pmp hyetograph CSV in the "
                "hyetographs folder of the output folder, named by its storm id (e.g. "
                "atlas14_pds_0100yr_24hr_alternating.csv: time_hr, incr_depth_in, cum_depth_in, with '# key: value' "
                "header lines). That is the format ndpmp:inflow_hydrograph reads: run it on the hyetographs folder, "
                "then ndbreach:spillwayadequacy on its output. One storm per frequency x duration x pattern. Also "
                "writes atlas14_storms_summary.csv and atlas14_storms.json.<br><br>"
                "Atlas 14 depths come from an atlas14_point / atlas14_basin JSON (DamIT PMP's pointpf / basinpf JSON "
                "also works) or, without one, from a PFDS query at POINT. FREQUENCY is a list of whole years, e.g. "
                "100,500,1000. Storm total = the Atlas 14 depth for the duration and frequency (sub-durations log-log "
                "interpolated). PATTERN spreads it in time: alternating block (default; not an Atlas 14 temporal "
                "distribution), the NOAA Atlas 14 temporal distribution (Volume 8 quartile curves, 6-, 12-, 24- or "
                "96-hour storms; QUARTILE, PERCENT, area found from the point) or NRCS Type I / IA / II / III (24-hour "
                "only). A duration and pattern pair with no curve, or a duration that is not a whole number of "
                "STEP_MIN steps, is skipped with a warning.<br><br>"
                "STEP_MIN (default 15 minutes, the step of the PMP 24-hour storms): use a step no longer than about "
                "0.29 x the basin lag (Tc / 5). A longer block lowers the inflow peak of a small basin; the depth "
                "stays the same.<br><br>"
                "GROSS rain: there are no loss parameters here. ndpmp:inflow_hydrograph applies the losses (CN or "
                "initial + constant). AREA_MI2 goes into each header; inflow_hydrograph uses it unless you override "
                "the area there.<br><br>"
                "Atlas 14 values are point values. AREAL_FACTOR multiplies every depth (1 = none): give your own areal "
                "reduction factor for the basin; this tool computes none.<br><br>"
                "ND REG_05.2026 Table 6.1: the Inflow Design Flood is the PMF for a high hazard dam, the 0.1% AEP "
                "(1,000-year) flood for medium and the 1% AEP (100-year) flood for low hazard. Where an incremental "
                "damage analysis (Section 6.4) sets a smaller IDF, the dam must still pass the 24-hour, 500-year "
                "rainstorm without overtopping (the overtops column of ndbreach:spillwayadequacy). This tool gives the "
                "frequency storms; ndpmp:pmp gives the PMP storms.<br><br>"
                "ndbreach:spillwayadequacy takes every inflow it is given as the IDF, so give it only the storms of "
                "one frequency: set PATTERN in inflow_hydrograph, e.g. *_0100yr_* (low hazard), *_1000yr_* (medium) "
                "or *_0500yr_24hr_* (the 500-year check). The StreamStats check of inflow_hydrograph "
                "(STREAMSTATS_JSON) compares PMF peaks only and leaves Atlas 14 storms out.<br><br>"
                "A rerun into the same output folder removes the Atlas 14 hyetographs of the earlier run that it "
                "does not write again.<br><br>" + NOTE)

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFile("INPUT_JSON", "Atlas 14 estimates (JSON from atlas14_point / "
                                                     "atlas14_basin; empty = query PFDS at the point below)",
                                                     extension="json", optional=True))
        self.addParameter(QgsProcessingParameterPoint("POINT", "Point (dam or basin outlet; used when no JSON is given)",
                                                      optional=True))
        series = QgsProcessingParameterEnum("SERIES", "Series for the point query (pds = partial duration / ARI, ams = "
                                            "annual maximum / AEP)", SERIES, defaultValue=0)
        series.setFlags(series.flags() | ADVANCED)
        self.addParameter(series)
        self.addParameter(QgsProcessingParameterString("FREQUENCY", "Frequencies in years, comma separated (PDS ARI 1, 2, "
                                                       "5, 10, 25, 50, 100, 200, 500, 1000; AMS 1/AEP 2 ... 1000)",
                                                       defaultValue="100,500,1000"))
        self.addParameter(QgsProcessingParameterEnum("STORM", "Storm durations (the 19 Atlas 14 durations, same list "
                                                     "and indices as DURATION in atlas14_fetchgrid)", STORM,
                                                     allowMultiple=True, defaultValue=[A.DEFAULT_DURATION_INDEX]))
        self.addParameter(QgsProcessingParameterNumber("STEP_MIN", "Time step (minutes)", INTEGER, defaultValue=15,
                                                       minValue=5, maxValue=1440))
        self.addParameter(QgsProcessingParameterEnum("PATTERN", "Temporal patterns (the depth is always the Atlas 14 depth)",
                                                     T.PATTERN_LABELS, allowMultiple=True, defaultValue=[0]))
        adv = [QgsProcessingParameterEnum("QUARTILE", "Atlas 14 temporal distribution: quartile case",
                                          T.QUARTILE_LABELS, defaultValue=T.QUARTILES.index(T.DEFAULT_QUARTILE)),
               QgsProcessingParameterNumber("PERCENT", "Atlas 14 temporal distribution: percent of occurrence (10 - 90; "
                                            "50 = median curve)", INTEGER, defaultValue=T.DEFAULT_PERCENT, minValue=10,
                                            maxValue=90),
               QgsProcessingParameterNumber("TDA", "Atlas 14 temporal distribution area (0 = find it from the point; "
                                            "Volume 8 areas 1 - 4)", INTEGER, defaultValue=0, minValue=0, maxValue=4)]
        for d in adv:
            d.setFlags(d.flags() | ADVANCED)
            self.addParameter(d)
        self.addParameter(QgsProcessingParameterNumber("AREA_MI2", "Drainage area (sq mi; written to each hyetograph "
                                                       "header for inflow_hydrograph; 0 = not set)", DOUBLE,
                                                       defaultValue=0.0, minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber("AREAL_FACTOR", "Areal reduction factor (multiplies every Atlas 14 "
                                                       "point depth; 1 = none)", DOUBLE, defaultValue=1.0,
                                                       minValue=0.01, maxValue=1.0))
        self._cache_params("Download and PFDS answer cache folder", advanced=True)
        self.addParameter(QgsProcessingParameterFolderDestination("OUTPUT_FOLDER", "Output folder"))
        self.addOutput(QgsProcessingOutputFolder("HYETOGRAPH_FOLDER", "Hyetograph folder (input of inflow_hydrograph)"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY_CSV", "atlas14_storms_summary.csv"))
        self.addOutput(QgsProcessingOutputFile("OUTPUT_JSON", "atlas14_storms.json"))
        self.addOutput(QgsProcessingOutputNumber("STORMS", "Storms written"))

    def _pf(self, P, context, out, cache, feedback):
        """(pf, sources, location, estimates JSON or None) from INPUT_JSON or a PFDS query at POINT."""
        path = self.parameterAsFile(P, "INPUT_JSON", context) if P.get("INPUT_JSON") not in (None, "") else ""
        if path:
            try:
                pf, doc = O.read_atlas14_json(path)
            except A.Atlas14Error as e:
                raise QgsProcessingException(str(e))
            return pf, doc.get("sources"), doc.get("location") or doc.get("basin"), path
        if P.get("POINT") in (None, ""):
            raise QgsProcessingException("Give an Atlas 14 estimates JSON (atlas14_point / atlas14_basin) or a point.")
        pt = _to_wgs84(self.parameterAsPoint(P, "POINT", context), self.parameterAsPointCrs(P, "POINT", context))
        kw = {"http": self._client(), "cache_dir": cache, "data": "depth", "units": "english",
              "series": SERIES[self.parameterAsEnum(P, "SERIES", context)],
              "offline": self.parameterAsBool(P, "OFFLINE", context)}
        (pf, src), = self._run_fetch([(pt.y(), pt.x())], kw)
        loc = {"lat": pt.y(), "lon": pt.x()}
        jp = os.path.join(out, "atlas14_estimates.json")
        O.write_outputs(os.path.join(out, "atlas14_estimates.csv"), jp, pf, [src], {"location": loc})
        feedback.pushInfo("PFDS: Volume {} Version {} ({}); {}".format(pf["volume"], pf["version"], pf["region"],
                                                                        "cached answer" if src["from_cache"] else "queried"))
        return pf, [src], loc, jp

    def processAlgorithm(self, P, context, feedback):  # noqa: N802
        out = self.parameterAsString(P, "OUTPUT_FOLDER", context)
        os.makedirs(out, exist_ok=True)
        cache = self._cache(P, context, os.path.join(out, "x"))
        pf, sources, loc, est = self._pf(P, context, out, cache, feedback)
        try:
            freqs = O.parse_frequencies(self.parameterAsString(P, "FREQUENCY", context), pf.get("series", "pds"))
            durations = [STORM[i] for i in self.parameterAsEnums(P, "STORM", context)] or [A.DEFAULT_DURATION]
            patterns = [T.PATTERNS[i] for i in self.parameterAsEnums(P, "PATTERN", context)] or [T.PATTERNS[0]]
            rec = O.make_storms(
                pf, freqs, durations, patterns, self.parameterAsInt(P, "STEP_MIN", context), out,
                area_mi2=self.parameterAsDouble(P, "AREA_MI2", context),
                areal_factor=self.parameterAsDouble(P, "AREAL_FACTOR", context),
                quartile=T.QUARTILES[self.parameterAsEnum(P, "QUARTILE", context)],
                percent=self.parameterAsInt(P, "PERCENT", context),
                temporal_area=self.parameterAsInt(P, "TDA", context) or None, cache_dir=cache,
                download=self._download(feedback, 120, 20 * 10 ** 6), offline=self.parameterAsBool(P, "OFFLINE", context),
                sources=sources, location=loc, log=feedback.pushInfo, warn=feedback.pushWarning)
        except A.Atlas14Error as e:
            raise QgsProcessingException(str(e))
        except (IOError, RuntimeError) as e:
            raise QgsProcessingException("Atlas 14 temporal distribution download or read failed ({}). Use OFFLINE with "
                                         "cached files, another pattern, or check the network.".format(e))
        if est:
            rec["estimates_json"] = est
        if self.parameterAsDouble(P, "AREA_MI2", context) <= 0:
            feedback.pushWarning("AREA_MI2 is 0: set the drainage area in inflow_hydrograph (AREA_MI2 there).")
        held = O.folder_hyetographs(rec["hyetograph_folder"])
        feedback.pushInfo("{} storm(s) written; {} hyetograph CSV(s) in {}. Next: ndpmp:inflow_hydrograph on that "
                          "folder.".format(len(rec["storms"]), len(held), rec["hyetograph_folder"]))
        others = [n for n in held if not n.startswith(O.STORM_PREFIX)]
        if others:
            feedback.pushWarning("The hyetograph folder also holds {} CSV(s) this tool did not write ({}); "
                                 "inflow_hydrograph will route them too unless its PATTERN leaves them out.".format(
                                     len(others), ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")))
        feedback.pushInfo(A.citation(pf))
        return {"OUTPUT_FOLDER": out, "HYETOGRAPH_FOLDER": rec["hyetograph_folder"], "SUMMARY_CSV": rec["summary_csv"],
                "OUTPUT_JSON": rec["record_json"], "STORMS": len(rec["storms"])}


ALGORITHMS = (Atlas14PointAlgorithm, Atlas14BasinAlgorithm, Atlas14FetchGridAlgorithm, Atlas14GridBasinAlgorithm,
              Atlas14HyetographAlgorithm)
