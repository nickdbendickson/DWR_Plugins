"""QGIS side of DamSafety Hazard Lite: one Processing tool around core.run()."""
import os

from qgis.core import (QgsApplication, QgsProcessingAlgorithm, QgsProcessingProvider,
                       QgsProcessingParameterRasterLayer, QgsProcessingParameterFile,
                       QgsProcessingParameterBoolean, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterString, QgsProcessingOutputString, QgsProcessingException,
                       QgsSettings)

from . import core

LAYER_PARAMS = [("address", "911 address points"), ("outlines", "Structure outlines (FEMA USA Structures)"),
                ("roads", "Road centerlines (NG9-1-1)"), ("counts", "NDDOT traffic counts"),
                ("rail", "Rail lines (NARN)"), ("crossings", "FRA grade crossings")]


class Classify(QgsProcessingAlgorithm):
    def name(self):
        return "classify"

    def displayName(self):
        return "Classify dam hazard (REG_05.2026 5.2)"

    def shortHelpString(self):
        return core.__doc__.replace("\n", "<br>")

    def createInstance(self):
        return Classify()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer("DEPTH", "Max depth raster"))
        self.addParameter(QgsProcessingParameterRasterLayer("VELOCITY", "Max velocity raster"))
        self.addParameter(QgsProcessingParameterBoolean("METRES", "Rasters are in m and m/s", False))
        self.addParameter(QgsProcessingParameterBoolean(
            "JUDGMENT_HIGH", "Count the judgment zone as High (5.3 borderline rule)", True))
        pack = QgsSettings().value("ndsuite/pack_path", os.environ.get("NDSUITE_PACK", ""))
        self.addParameter(QgsProcessingParameterFile(
            "PACK", "ND Dam Suite offline pack folder", QgsProcessingParameterFile.Folder,
            defaultValue=pack or None, optional=True))
        for key, label in LAYER_PARAMS:
            self.addParameter(QgsProcessingParameterFile(
                key.upper(), label + " (blank = from the pack)", extension="gpkg", optional=True))
        self.addParameter(QgsProcessingParameterString("DAM", "Dam name", optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination("OUT", "Output folder"))
        self.addOutput(QgsProcessingOutputString("CLASS", "ND hazard class"))

    def processAlgorithm(self, p, context, feedback):
        pack = self.parameterAsFile(p, "PACK", context)
        layers = core.pack_paths(pack) if pack else {}
        for key, _ in LAYER_PARAMS:
            path = self.parameterAsFile(p, key.upper(), context)
            if path:
                layers[key] = path
            if not os.path.exists(layers.get(key, "").partition("|")[0]):
                raise QgsProcessingException("No {} layer: set the pack folder or the file.".format(key))
        out = self.parameterAsString(p, "OUT", context)
        res = core.run(self.parameterAsRasterLayer(p, "DEPTH", context).source(),
                       self.parameterAsRasterLayer(p, "VELOCITY", context).source(), out, layers,
                       metres=self.parameterAsBool(p, "METRES", context),
                       judgment_high=self.parameterAsBool(p, "JUDGMENT_HIGH", context),
                       dam_name=self.parameterAsString(p, "DAM", context), log=feedback.pushInfo)
        for cls, sec, why in res["reasons"]:
            feedback.pushInfo("{} [{}] {}".format(cls, sec, why))
        for v in res["verify"]:
            feedback.pushWarning("Verify: " + v)
        if context.project() is not None:
            for lyr in ("structures", "road_spots", "rail_spots"):
                context.addLayerToLoadOnCompletion(
                    "{}|layername={}".format(res["gpkg"], lyr),
                    context.LayerDetails(lyr, context.project(), lyr))
        return {"OUT": out, "CLASS": res["class"]}


class Provider(QgsProcessingProvider):
    def id(self):
        return "ndlite"

    def name(self):
        return "DamSafety Hazard Lite"

    def loadAlgorithms(self):
        self.addAlgorithm(Classify())


class Plugin:
    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.action = None
        self.dialog = None

    def initProcessing(self):
        self.provider = Provider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def initGui(self):
        self.initProcessing()
        from qgis.PyQt.QtGui import QIcon
        from qgis.PyQt.QtWidgets import QAction
        here = os.path.dirname(__file__)
        self.action = QAction(QIcon(os.path.join(here, "icon.svg")),
                              "DamSafety Hazard Lite…", self.iface.mainWindow())
        self.action.triggered.connect(self.open)
        self.iface.addPluginToMenu("DamSafety Hazard Lite", self.action)

    def open(self):
        from .dialog import HazardLiteDialog
        if self.dialog is None:
            self.dialog = HazardLiteDialog(self.iface.mainWindow(), self.iface)
        self.dialog.show()
        self.dialog.raise_()

    def unload(self):
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        if self.action is not None:
            self.iface.removePluginMenu("DamSafety Hazard Lite", self.action)
