"""Download every vector layer the three plugins need, plus the raster server list."""

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingException,
                       QgsProcessingParameterEnum, QgsProcessingParameterFileDestination)

from ..core import offline


class DownloadLayers(QgsProcessingAlgorithm):
    def name(self):
        return "downloadlayers"

    def displayName(self):
        return "0b - Download offline layers"

    def group(self):
        return "Dam screening"

    def groupId(self):
        return "dams"

    def createInstance(self):
        return DownloadLayers()

    def shortHelpString(self):
        return ("Downloads the vector layers all three plugins read (dams, flowlines, "
                "waterbodies, roads, railroads, structures, address points) for the county "
                "you pick, writes them to one GeoPackage, and writes the WMS and ArcGIS "
                "raster server list beside it. Selecting that package in the window adds "
                "the layers and the servers to the project.")

    def initAlgorithm(self, config=None):
        names = sorted(offline.COUNTIES)
        self.addParameter(QgsProcessingParameterEnum("COUNTY", "County", names, defaultValue=0))
        self.addParameter(QgsProcessingParameterFileDestination(
            "OUT", "Offline package", "GeoPackage (*.gpkg)"))
        self._names = names

    def processAlgorithm(self, params, context, feedback):
        county = self._names[self.parameterAsEnum(params, "COUNTY", context)]
        out = self.parameterAsFileOutput(params, "OUT", context)
        if not out.lower().endswith(".gpkg"):
            out += ".gpkg"
        try:
            r = offline.download_county(out, county, feedback)
        except Exception as e:
            raise QgsProcessingException(str(e))
        for note in r["notes"]:
            feedback.pushInfo(note)
        feedback.pushInfo("servers: " + r["servers"])
        return {"OUT": r["gpkg"]}
