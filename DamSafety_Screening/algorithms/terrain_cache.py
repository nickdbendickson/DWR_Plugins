"""Cache one DEM clip per dam (EPSG:5070) from 3DEP or a local LiDAR folder."""
import os

from osgeo import gdal
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterEnum, QgsProcessingParameterNumber,
                       QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterFile, QgsProcessingException,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem,
                       QgsProject)

from ..core import terrain
from ..compat import (FILE_FOLDER, NUM_DOUBLE, SRC_POINT)
from ..damsuite import gdalsafe as GS
from . import _common as cm
from ..core import errors as er


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
                "so the batch can be re-run or resumed. 3DEP serves the best available "
                "resolution per location; check the resulting cell size where it matters.")

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
            GS.checked(gdal.BuildVRT(vrt, tiles), "building " + vrt)
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
                    GS.checked(gdal.Warp(dst, vrt, dstSRS="EPSG:5070", outputBounds=ext,
                                         xRes=cell, yRes=cell, resampleAlg="bilinear",
                                         outputType=gdal.GDT_Float32, dstNodata=-9999,
                                         creationOptions=["COMPRESS=DEFLATE"]), "clipping the DEM")
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
