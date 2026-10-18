"""Thin PyQGIS wrapper around core.run_pmp (all computation lives in core/)."""
import os
import tempfile

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource, QgsProcessingParameterFile,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterString,
                       QgsProcessingParameterBoolean, QgsProcessingParameterNumber, QgsProcessingParameterField,
                       QgsProcessing, QgsProcessingException, QgsSettings, QgsVectorFileWriter,
                       QgsCoordinateTransformContext, QgsProcessingOutputFile, QgsProcessingOutputFolder,
                       QgsProcessingUtils, QgsFeatureSink)

from .core import run_pmp, PMPConfig
from .core.pmp import DEFAULT_DURATIONS

SETTING_DATA_HOME = "nd_pmp/data_home"


def _durs(s):
    return [("%02d" % int(x)) for x in s.replace(";", ",").replace(" ", ",").split(",") if x.strip()]


class NDPMPAlgorithm(QgsProcessingAlgorithm):
    BASIN, HOME, OUT = "BASIN", "DATA_HOME", "OUTPUT_FOLDER"
    LOC, GEN, COOL = "LOCAL_DURATIONS", "GENERAL_DURATIONS", "COOL_DURATIONS"
    USEAREA, AREA, WEIGHTED, SUBFIELD = "USE_BASIN_AREA", "CUSTOM_AREA", "WEIGHTED", "SUBBASIN_FIELD"
    CHART, TEMPORAL, RASTERS = "DD_CHART", "TEMPORAL", "WRITE_RASTERS"

    def name(self): return "pmp"
    def displayName(self): return "PMP Evaluation (main tool)"
    def createInstance(self): return NDPMPAlgorithm()
    def shortHelpString(self):
        return ("Port of ND_PMP_Tool.py (AWA 2021). Data folder = the 'PMP_Evaluation_Tool' folder from the SWC zip "
                "(must contain Input/*.gdb). INTERNAL PROTOTYPE - results not validated against ArcGIS.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(self.BASIN, "Basin polygon(s)",
                                                              [QgsProcessing.TypeVectorPolygon]))
        p = QgsProcessingParameterFile(self.HOME, "PMP_Evaluation_Tool data folder",
                                       behavior=QgsProcessingParameterFile.Folder,
                                       defaultValue=QgsSettings().value(SETTING_DATA_HOME, ""))
        self.addParameter(p)
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUT, "Output folder"))
        for k, lbl, t in ((self.LOC, "Local storm durations (h, comma-separated; blank = skip)", "Local"),
                          (self.GEN, "General storm durations", "General"),
                          (self.COOL, "Cool-season storm durations", "CoolSeason")):
            self.addParameter(QgsProcessingParameterString(k, lbl, ",".join(DEFAULT_DURATIONS[t]), optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.USEAREA, "Use basin area (else custom area)", True))
        self.addParameter(QgsProcessingParameterNumber(self.AREA, "Custom area (sq mi)",
                                                       QgsProcessingParameterNumber.Double, optional=True, minValue=0))
        self.addParameter(QgsProcessingParameterBoolean(self.WEIGHTED, "Area-weighted basin average", True))
        self.addParameter(QgsProcessingParameterField(self.SUBFIELD, "Subbasin ID field (optional)",
                                                      parentLayerParameterName=self.BASIN, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(self.CHART, "Depth-duration chart (PNG)", True))
        self.addParameter(QgsProcessingParameterBoolean(self.TEMPORAL, "Temporal distributions + hyetograph CSVs", True))
        self.addParameter(QgsProcessingParameterBoolean(self.RASTERS, "Write PMP GeoTIFFs", True))
        self.addOutput(QgsProcessingOutputFile("SUMMARY_CSV", "basin_avg_depth_by_duration.csv"))
        self.addOutput(QgsProcessingOutputFolder("HYETOGRAPH_FOLDER", "Hyetograph folder"))

    def processAlgorithm(self, parameters, context, feedback):
        home = self.parameterAsFile(parameters, self.HOME, context)
        if not home or not os.path.isdir(os.path.join(home, "Input")):
            raise QgsProcessingException("Data folder must contain Input/ (the extracted PMP_Evaluation_Tool folder)")
        QgsSettings().setValue(SETTING_DATA_HOME, home)
        out = self.parameterAsString(parameters, self.OUT, context)
        os.makedirs(out, exist_ok=True)
        src = self.parameterAsSource(parameters, self.BASIN, context)
        layer = self.parameterAsVectorLayer(parameters, self.BASIN, context)
        basin_name = layer.name() if layer else "basin"
        # Export basin to a temporary GeoPackage in its own CRS so the core (pure OGR) can read it
        tmp = os.path.join(QgsProcessingUtils.tempFolder(), "nd_pmp_basin_%d.gpkg" % os.getpid())
        opts = QgsVectorFileWriter.SaveVectorOptions(); opts.driverName = "GPKG"; opts.layerName = "basin"
        sink, err = None, None
        writer = QgsVectorFileWriter.create(tmp, src.fields(), src.wkbType(), src.sourceCrs(),
                                            QgsCoordinateTransformContext(), opts)
        for f in src.getFeatures():
            writer.addFeature(f, QgsFeatureSink.FastInsert)
        del writer
        subfield = self.parameterAsString(parameters, self.SUBFIELD, context) or None
        use_area = self.parameterAsBool(parameters, self.USEAREA, context)
        custom = parameters.get(self.AREA)
        if not use_area and custom in (None, ""):
            raise QgsProcessingException("Custom area required when 'Use basin area' is unchecked")
        cfg = PMPConfig(basin_path=tmp, basin_layer="basin", data_home=home, out_dir=out, basin_name=basin_name,
                        durations={"Local": _durs(self.parameterAsString(parameters, self.LOC, context)),
                                   "General": _durs(self.parameterAsString(parameters, self.GEN, context)),
                                   "CoolSeason": _durs(self.parameterAsString(parameters, self.COOL, context))},
                        use_basin_area=use_area,
                        custom_area=self.parameterAsDouble(parameters, self.AREA, context) if not use_area else None,
                        weighted=self.parameterAsBool(parameters, self.WEIGHTED, context),
                        subbasin_field=subfield,
                        chart=self.parameterAsBool(parameters, self.CHART, context),
                        temporal=self.parameterAsBool(parameters, self.TEMPORAL, context),
                        write_rasters=self.parameterAsBool(parameters, self.RASTERS, context))
        try:
            res = run_pmp(cfg, log=feedback.pushInfo, is_canceled=feedback.isCanceled)
        except Exception as e:
            raise QgsProcessingException(str(e))
        result = {self.OUT: out, "SUMMARY_CSV": res["summary_csv"],
                  "HYETOGRAPH_FOLDER": os.path.join(out, "hyetographs")}
        for t, v in res["types"].items():
            feedback.pushInfo("%s: basin average %s" % (t, v["basin_average"]))
            context.addLayerToLoadOnCompletion(
                v["gpkg"], context.LayerDetails("%s PMP points" % t, context.project(), "PMP"))
        return result
