"""Thin PyQGIS wrappers: ndpmp:spatial_distribution -> core.spatial, ndpmp:snowmelt -> core.snowmelt."""
import os
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource, QgsProcessingParameterVectorLayer,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber, QgsProcessingParameterFile,
                       QgsProcessingParameterString, QgsProcessingParameterPoint, QgsProcessingParameterBoolean,
                       QgsProcessing, QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputNumber,
                       QgsProcessingUtils, QgsVectorFileWriter, QgsCoordinateTransformContext, QgsFeatureSink,
                       QgsSettings, QgsCoordinateReferenceSystem)

D = QgsProcessingParameterNumber.Double
SETTING_DATA_HOME = "nd_pmp/data_home"


def _basin_to_gpkg(alg, p, context, name="BASIN"):
    src = alg.parameterAsSource(p, name, context)
    tmp = os.path.join(QgsProcessingUtils.tempFolder(), "nd_pmp_%s_%d.gpkg" % (name.lower(), os.getpid()))
    if os.path.exists(tmp):
        os.remove(tmp)
    o = QgsVectorFileWriter.SaveVectorOptions(); o.driverName = "GPKG"; o.layerName = "basin"
    w = QgsVectorFileWriter.create(tmp, src.fields(), src.wkbType(), src.sourceCrs(), QgsCoordinateTransformContext(), o)
    for f in src.getFeatures():
        w.addFeature(f, QgsFeatureSink.FastInsert)
    del w
    return tmp


def _home(alg, p, context):
    h = alg.parameterAsFile(p, "DATA_HOME", context) or QgsSettings().value(SETTING_DATA_HOME, "")
    if not h or not os.path.isdir(os.path.join(h, "Input")):
        raise QgsProcessingException("Set the PMP_Evaluation_Tool folder (contains Input/)")
    QgsSettings().setValue(SETTING_DATA_HOME, h)
    return h


def _wgs84_point(alg, p, name, context):
    if p.get(name) in (None, ""):
        return None
    pt = alg.parameterAsPoint(p, name, context, QgsCoordinateReferenceSystem("EPSG:4326"))
    return (pt.x(), pt.y())


class SpatialDistributionAlgorithm(QgsProcessingAlgorithm):
    def name(self): return "spatial_distribution"
    def displayName(self): return "PMP spatial distribution (SPAS storm patterns)"
    def createInstance(self): return SpatialDistributionAlgorithm()
    def shortHelpString(self):
        return ("Port of Spatial_Distribution_v1_0.py. Input: the <Type>_PMP_Points_<basin>_<area> layer written by "
                "ndpmp:pmp. Shifts each SPAS total-storm raster to the basin centroid (or a given point), flattens "
                "x^(1/5), SAF = value/mean, PMP_xx = mean(PMP_xx)*SAF. Writes points GeoPackage + GeoTIFF per "
                "duration + spatial_distribution_summary.csv. Pattern IDs: comma list of SPAS_xxxx_x; blank = "
                "the original defaults. INTERNAL PROTOTYPE.")

    def initAlgorithm(self, config=None):
        A = self.addParameter
        A(QgsProcessingParameterVectorLayer("PMP_POINTS", "PMP points layer (from ndpmp:pmp)", [QgsProcessing.TypeVectorPoint]))
        A(QgsProcessingParameterFeatureSource("BASIN", "Basin polygon", [QgsProcessing.TypeVectorPolygon]))
        A(QgsProcessingParameterFile("DATA_HOME", "PMP_Evaluation_Tool folder", QgsProcessingParameterFile.Folder,
                                     optional=True, defaultValue=QgsSettings().value(SETTING_DATA_HOME, "")))
        A(QgsProcessingParameterPoint("CENTER", "Target point (blank = basin centroid)", optional=True))
        A(QgsProcessingParameterString("PATTERNS", "Storm patterns (SPAS IDs, comma separated; blank = defaults)",
                                       optional=True))
        A(QgsProcessingParameterFolderDestination("OUTPUT_FOLDER", "Output folder"))
        self.addOutput(QgsProcessingOutputFile("SUMMARY_CSV", "spatial_distribution_summary.csv"))

    def processAlgorithm(self, p, context, feedback):
        from .core.spatial import run_spatial
        lyr = self.parameterAsVectorLayer(p, "PMP_POINTS", context)
        parts = lyr.source().split("|")
        path = parts[0]; lname = None
        for x in parts[1:]:
            if x.startswith("layername="):
                lname = x.split("=", 1)[1]
        lname = lname or lyr.name()
        pats = [s.strip() for s in (self.parameterAsString(p, "PATTERNS", context) or "").split(",") if s.strip()]
        out = self.parameterAsString(p, "OUTPUT_FOLDER", context)
        try:
            rs = run_spatial(path, _basin_to_gpkg(self, p, context), _home(self, p, context), out, points_layer=lname,
                             centre_xy=_wgs84_point(self, p, "CENTER", context), patterns=pats or None,
                             log=feedback.pushInfo)
        except Exception as e:
            raise QgsProcessingException(str(e))
        return {"OUTPUT_FOLDER": out, "SUMMARY_CSV": os.path.join(out, "spatial_distribution_summary.csv") if rs else ""}


class SnowmeltAlgorithm(QgsProcessingAlgorithm):
    def name(self): return "snowmelt"
    def displayName(self): return "Snowmelt (100-yr SWE, degree-day, rain-on-snow)"
    def createInstance(self): return SnowmeltAlgorithm()
    def shortHelpString(self):
        return ("Port of Snowmelt_Runoff_Tool.py: daily degree-day melt of ND_Final_100yr_SWE_<mmdd> with Daymet "
                "mean temperature, Ta x1.15 during the rain-on-snow window (x1.075 on the shoulder days), masked to "
                "the basin. Writes Basin_Ave_*.csv (and Sample_Point_*.csv). Optional: writes the rain-window melt "
                "into '# snowmelt_in:' of the cool-season hyetograph CSVs in a folder. Dates are month/day only "
                "(Mar 1 - Jun 14 for SWE). INTERNAL PROTOTYPE.")

    def initAlgorithm(self, config=None):
        A = self.addParameter
        A(QgsProcessingParameterFile("DATA_HOME", "PMP_Evaluation_Tool folder", QgsProcessingParameterFile.Folder,
                                     optional=True, defaultValue=QgsSettings().value(SETTING_DATA_HOME, "")))
        A(QgsProcessingParameterString("START", "Melt start (MM-DD)", "03-15"))
        A(QgsProcessingParameterString("END", "Melt end (MM-DD)", "04-15"))
        A(QgsProcessingParameterNumber("RAIN_DUR", "Rain-on-snow duration (days; 0 = none)",
                                       QgsProcessingParameterNumber.Integer, 3, minValue=0))
        A(QgsProcessingParameterNumber("CM", "Degree-day coefficient Cm (in/degF-day)", D, 0.06, minValue=0))
        A(QgsProcessingParameterFeatureSource("BASIN", "Basin polygon", [QgsProcessing.TypeVectorPolygon]))
        A(QgsProcessingParameterPoint("SAMPLE", "Sample point (optional)", optional=True))
        A(QgsProcessingParameterBoolean("GRIDS", "Save daily GeoTIFFs", False))
        A(QgsProcessingParameterFile("HYETOGRAPH_FOLDER", "Hyetograph folder to fill snowmelt_in (optional)",
                                     QgsProcessingParameterFile.Folder, optional=True))
        A(QgsProcessingParameterFolderDestination("OUTPUT_FOLDER", "Output folder"))
        for k in ("TOTAL_MELT_IN", "RAIN_WINDOW_MELT_IN"):
            self.addOutput(QgsProcessingOutputNumber(k, k))
        self.addOutput(QgsProcessingOutputFile("BASIN_TABLE", "Basin average table CSV"))

    def processAlgorithm(self, p, context, feedback):
        from .core.snowmelt import run_snowmelt, fill_snowmelt
        y = "2001-"   # non-leap year; the grids are keyed by month/day only
        out = self.parameterAsString(p, "OUTPUT_FOLDER", context)
        try:
            r = run_snowmelt(_home(self, p, context), y + self.parameterAsString(p, "START", context).strip(),
                             y + self.parameterAsString(p, "END", context).strip(),
                             self.parameterAsInt(p, "RAIN_DUR", context), self.parameterAsDouble(p, "CM", context),
                             _basin_to_gpkg(self, p, context), out, sample_xy=_wgs84_point(self, p, "SAMPLE", context),
                             grid_output=self.parameterAsBool(p, "GRIDS", context), log=feedback.pushInfo)
        except Exception as e:
            raise QgsProcessingException(str(e))
        hf = self.parameterAsFile(p, "HYETOGRAPH_FOLDER", context)
        if hf and r["rain_window_melt_in"] is not None:
            done = fill_snowmelt(hf, r["rain_window_melt_in"])
            feedback.pushInfo("snowmelt_in=%s written to %d cool-season hyetographs" % (r["rain_window_melt_in"], len(done)))
        bt = [v for k, v in r["tables"].items() if k.startswith("Basin_Ave")]
        return {"OUTPUT_FOLDER": out, "TOTAL_MELT_IN": r["total_melt_in"],
                "RAIN_WINDOW_MELT_IN": r["rain_window_melt_in"] if r["rain_window_melt_in"] is not None else 0.0,
                "BASIN_TABLE": bt[0] if bt else ""}
