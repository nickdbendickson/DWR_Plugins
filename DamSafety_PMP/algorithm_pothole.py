"""Thin PyQGIS wrapper: ndpmp:pothole_storage -> core.pothole (WhiteboxTools)."""
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterRasterLayer, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterNumber, QgsProcessingParameterFile,
                       QgsProcessing, QgsProcessingException, QgsProcessingOutputFile, QgsProcessingOutputNumber,
                       QgsProcessingUtils, QgsVectorFileWriter, QgsCoordinateTransformContext, QgsFeatureSink,
                       QgsSettings)

from .core.pothole import run_pothole

D = QgsProcessingParameterNumber.Double
SETTING_WBT = "nd_pmp/whitebox_tools"


class PotholeStorageAlgorithm(QgsProcessingAlgorithm):
    def name(self): return "pothole_storage"
    def displayName(self): return "Pothole (depression) storage & contributing area"
    def createInstance(self): return PotholeStorageAlgorithm()
    def shortHelpString(self):
        return ("WhiteboxTools FillDepressions/Clump/D8 on a projected DEM clipped to the basin: per-pothole polygons "
                "(area, max depth, volume ac-ft, spill elevation), total depression storage and effective "
                "contributing area. Needs the whitebox_tools binary (conda-forge whitebox_tools or pip whitebox). "
                "INTERNAL PROTOTYPE.")

    def initAlgorithm(self, config=None):
        A = self.addParameter
        A(QgsProcessingParameterRasterLayer("DEM", "DEM (projected CRS)"))
        A(QgsProcessingParameterFeatureSource("BASIN", "Basin polygon", [QgsProcessing.TypeVectorPolygon]))
        A(QgsProcessingParameterNumber("Z_TO_M", "DEM z-unit to metres factor (1 = metres, 0.3048 = feet)", D, 1.0, minValue=0))
        A(QgsProcessingParameterNumber("MIN_DEPTH_M", "Minimum depression depth (m)", D, 0.05, minValue=0))
        A(QgsProcessingParameterNumber("MIN_AREA_M2", "Minimum pothole area (m2)", D, 100, minValue=0))
        A(QgsProcessingParameterFile("WBT_EXE", "whitebox_tools executable (blank = auto-detect)",
                                     optional=True, defaultValue=QgsSettings().value(SETTING_WBT, "")))
        A(QgsProcessingParameterFolderDestination("OUTPUT_FOLDER", "Output folder"))
        for k, lbl in (("POTHOLES", "potholes.gpkg"), ("SUMMARY_CSV", "pothole_summary.csv"),
                       ("CONTRIB_RASTER", "contributing_area.tif"), ("NETWORK_JSON", "pothole_network.json")):
            self.addOutput(QgsProcessingOutputFile(k, lbl))
        for k in ("TOTAL_STORAGE_ACFT", "TOTAL_STORAGE_IN", "CONTRIB_AREA_MI2", "NONCONTRIB_AREA_MI2"):
            self.addOutput(QgsProcessingOutputNumber(k, k))

    def processAlgorithm(self, p, context, feedback):
        dem = self.parameterAsRasterLayer(p, "DEM", context)
        src = self.parameterAsSource(p, "BASIN", context)
        if dem.crs() != src.sourceCrs():
            feedback.pushInfo("Basin CRS differs from DEM CRS; GDAL warps the cutline on the fly")
        tmp = os.path.join(QgsProcessingUtils.tempFolder(), "nd_pmp_pothole_basin_%d.gpkg" % os.getpid())
        opts = QgsVectorFileWriter.SaveVectorOptions(); opts.driverName = "GPKG"; opts.layerName = "basin"
        w = QgsVectorFileWriter.create(tmp, src.fields(), src.wkbType(), src.sourceCrs(), QgsCoordinateTransformContext(), opts)
        for f in src.getFeatures():
            w.addFeature(f, QgsFeatureSink.FastInsert)
        del w
        exe = self.parameterAsFile(p, "WBT_EXE", context) or None
        if exe:
            QgsSettings().setValue(SETTING_WBT, exe)
        out = self.parameterAsString(p, "OUTPUT_FOLDER", context)
        try:
            s, _ = run_pothole(dem.source(), tmp, out, z_factor_to_m=self.parameterAsDouble(p, "Z_TO_M", context),
                               min_depth_m=self.parameterAsDouble(p, "MIN_DEPTH_M", context),
                               min_area_m2=self.parameterAsDouble(p, "MIN_AREA_M2", context), wbt_exe=exe,
                               basin_layer="basin", log=feedback.pushInfo)
        except Exception as e:
            raise QgsProcessingException(str(e))
        for k, v in s.items():
            feedback.pushInfo("%s: %s" % (k, v))
        return {"OUTPUT_FOLDER": out, "POTHOLES": s["potholes_gpkg"], "SUMMARY_CSV": os.path.join(out, "pothole_summary.csv"),
                "CONTRIB_RASTER": os.path.join(out, "contributing_area.tif"),
                "NETWORK_JSON": os.path.join(out, "pothole_network.json"),
                "TOTAL_STORAGE_ACFT": s["total_storage_acft"], "TOTAL_STORAGE_IN": s["total_storage_in_over_basin"],
                "CONTRIB_AREA_MI2": s["contributing_area_mi2"], "NONCONTRIB_AREA_MI2": s["noncontributing_area_mi2"]}
