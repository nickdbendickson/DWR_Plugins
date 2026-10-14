"""Download a local DEM for one dam, one North Dakota county, or the state.

Resolution is 10 m or 1 m. Source is USGS 3DEP or ND DWR QL2 LiDAR.
The local file is always a tiled GeoTIFF, float32, EPSG:5070, elevations in
metres. A sidecar text file lists source, CRS, units and file type.
"""
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterEnum,
                       QgsProcessingParameterString, QgsProcessingParameterNumber,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterBoolean,
                       QgsProcessingException, QgsProcessingOutputFile)

from ..compat import NUM_DOUBLE
from ..core import dem_download as dd


class DownloadDem(QgsProcessingAlgorithm):
    def name(self):
        return "downloaddem"

    def displayName(self):
        return "Download DEM (dam, county, or state)"

    def group(self):
        return "Dam screening"

    def groupId(self):
        return "dams"

    def createInstance(self):
        return DownloadDem()

    def shortHelpString(self):
        return (
            "Writes one local DEM for a dam, a county, or the state. "
            "Resolution is 10 m or 1 m. "
            "USGS 3DEP: source USGS 3DEP ImageServer, native CRS EPSG:5070, "
            "native units metres, native file type GeoTIFF. "
            "ND DWR LiDAR: source ND QL2 on lidarftp.swc.nd.gov, native CRS "
            "the tile CRS (feet, often EPSG:3721 or EPSG:2266), native units "
            "feet, native file type Erdas Imagine .img at 1 m. "
            "Either source is saved as GeoTIFF float32, tiled, DEFLATE, "
            "CRS EPSG:5070, units metres, at the chosen cell size. "
            "A _dem.txt file next to the raster lists those facts. "
            "10 m is the working copy. 1 m is for one dam or a county you "
            "are about to inspect; a 1 m state is very large. "
            "An existing file of the same name is reused."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            "SOURCE", "Source", ["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)"], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "RES", "Local DEM resolution", ["10 m", "1 m"], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "SCOPE", "Area", ["Dam", "County", "State"], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            "DAM_POINT", "Dam point, longitude,latitude", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "BUFFER_MI", "Buffer around the dam (miles)", NUM_DOUBLE, 15.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterEnum(
            "COUNTY", "County", dd.county_names(), defaultValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            "CELL", "Cell size (m; used only if RES is not set)", NUM_DOUBLE, 10.0, minValue=1.0, optional=True))
        self.addParameter(QgsProcessingParameterString(
            "DWR_PATH", "ND LiDAR FTP link or /temp/<id> path (optional)", optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            "ASCII_FT", "ND LiDAR: also pull ASCII_Grids_ft (not used for the local GeoTIFF if Imagine is present)", False))
        self.addParameter(QgsProcessingParameterBoolean(
            "IMAGINE", "ND LiDAR: DEM_Imagine (1 m feet)", True))
        self.addParameter(QgsProcessingParameterString(
            "WORK_CRS", "Extra CRS (empty = EPSG:5070 only)", optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination("OUT", "Output folder"))
        self.addOutput(QgsProcessingOutputFile("VRT", "Local DEM"))

    def processAlgorithm(self, parameters, context, feedback):
        out = self.parameterAsString(parameters, "OUT", context)
        os.makedirs(out, exist_ok=True)
        feedback.setProgress(1)
        feedback.pushInfo("Download DEM started")
        cell = self._cell(parameters, context)
        scope = self.parameterAsEnum(parameters, "SCOPE", context)
        feedback.pushInfo("source {} | {} m | {}".format(
            "ND DWR LiDAR" if self.parameterAsEnum(parameters, "SOURCE", context) == 1 else "USGS 3DEP",
            cell, ["dam", "county", "state"][scope]))
        if cell == 1 and scope == 2:
            feedback.pushInfo("1 m for the whole state is a very large download. 10 m is the working copy.")
        source_i = self.parameterAsEnum(parameters, "SOURCE", context)
        stem = self._stem(parameters, context, scope)
        dest = os.path.join(out, "{}_{}m_5070.tif".format(stem, int(cell)))
        side = os.path.splitext(dest)[0] + "_dem.txt"
        if os.path.isfile(dest) and os.path.isfile(side):
            feedback.pushInfo("reusing {} ({})".format(dest, open(side, encoding="utf-8").read().replace("\n", "; ")))
            return {"VRT": dest, "OUT": out}
        if source_i == 1:
            vrt, native = self._nd(parameters, context, feedback, out, scope)
            info = dd.product_info("ndlidar", cell, native)
        else:
            vrt = self._usgs(parameters, context, feedback, out, scope, cell, stem)
            info = dd.product_info("usgs", cell)
        feedback.setProgress(90)
        feedback.pushInfo("writing the local GeoTIFF")
        tif, side = dd.write_local_dem(vrt, dest, cell, info, feedback)
        feedback.setProgress(100)
        extra = _warp(tif, out, parameters, context, feedback)
        feedback.pushInfo("local DEM: {}".format(tif))
        feedback.pushInfo("facts: {}".format(side))
        if extra and extra != tif:
            feedback.pushInfo("extra CRS copy: {}".format(extra))
        return {"VRT": tif, "OUT": out}

    def _county(self, parameters, context):
        names = dd.county_names()
        raw = parameters.get("COUNTY")
        if isinstance(raw, str) and raw.strip() in dd.COUNTIES:
            return raw.strip()
        try:
            return names[self.parameterAsEnum(parameters, "COUNTY", context)]
        except Exception:
            return names[0]

    def _cell(self, parameters, context):
        if "RES" in parameters and parameters.get("RES") is not None:
            return dd.cell_from_choice(self.parameterAsEnum(parameters, "RES", context))
        return int(round(self.parameterAsDouble(parameters, "CELL", context) or 10))

    def _stem(self, parameters, context, scope):
        if scope == 0:
            raw = (self.parameterAsString(parameters, "DAM_POINT", context) or "").strip()
            return "dam" if not raw else "dam_{}_{}".format(*[p.strip() for p in raw.split(",")[:2]])
        if scope == 1:
            return self._county(parameters, context).replace(" ", "_")
        return "north_dakota"

    def _usgs(self, parameters, context, feedback, out, scope, cell, stem):
        if scope == 0:
            raw = (self.parameterAsString(parameters, "DAM_POINT", context) or "").strip()
            try:
                lon, lat = [float(x) for x in raw.replace(" ", "").split(",")[:2]]
            except ValueError:
                raise QgsProcessingException("Dam point must be longitude,latitude")
            extent = dd.point_box(lon, lat, self.parameterAsDouble(parameters, "BUFFER_MI", context))
        elif scope == 1:
            name = self._county(parameters, context)
            box = dd.COUNTIES.get(name)
            if box is None:
                raise QgsProcessingException("Unknown county {!r}. One of: {}".format(
                    name, ", ".join(dd.county_names())))
            extent = dd.to_5070(*box)
        else:
            extent = dd.to_5070(*dd.STATE)
        return dd.download(extent, cell, out, stem + "_src", feedback)

    def _nd(self, parameters, context, feedback, out, scope):
        products = []
        if self.parameterAsBool(parameters, "IMAGINE", context):
            products.append("DEM_Imagine")
        if self.parameterAsBool(parameters, "ASCII_FT", context):
            products.append("ASCII_Grids_ft")
        if not products:
            products = ["DEM_Imagine"]
        package = (self.parameterAsString(parameters, "DWR_PATH", context) or "").strip()
        try:
            if package:
                vrt = dd.fetch_dwr(package, out, products, feedback)
            elif scope == 0:
                raw = (self.parameterAsString(parameters, "DAM_POINT", context) or "").strip()
                try:
                    lon, lat = [float(x) for x in raw.replace(" ", "").split(",")[:2]]
                except ValueError:
                    raise QgsProcessingException(
                        "ND LiDAR for a dam needs longitude,latitude, or an FTP package from lidar.dwr.nd.gov.")
                box = dd.point_ll_box(lon, lat, self.parameterAsDouble(parameters, "BUFFER_MI", context))
                cache = os.path.join(out, "_index")
                tiles = dd.tiles_for_box(*box, cache, feedback)
                if feedback is not None:
                    feedback.pushInfo("{} QL2 tiles for the dam box".format(len(tiles)))
                if not tiles:
                    raise QgsProcessingException("No QL2 tiles meet that dam box")
                vrt, _n = dd.fetch_dwr_area(1, "", out, products, feedback, cache_dir=cache, tiles=tiles)
            else:
                vrt, _n = dd.fetch_dwr_area(
                    scope, self._county(parameters, context),
                    out, products, feedback)
        except QgsProcessingException:
            raise
        except Exception as exc:
            raise QgsProcessingException(str(exc))
        native = dd.native_from_raster(vrt)
        native["source"] = "ND DWR LiDAR_NorthDakota_QL2 " + ", ".join(products)
        if not native.get("file_type"):
            native["file_type"] = "Erdas Imagine .img, 1 m" if "DEM_Imagine" in products else "ASCII grid, feet"
        if not native.get("units"):
            native["units"] = "feet"
        return vrt, native


def _warp(tif, out, parameters, context, feedback):
    crs = ""
    try:
        crs = parameters.get("WORK_CRS") or ""
        if not isinstance(crs, str):
            crs = ""
    except Exception:
        crs = ""
    crs = str(crs).strip()
    if not crs or crs.endswith("5070"):
        return tif
    epsg = crs.split(":")[-1]
    dest = os.path.join(out, "dem_{}.tif".format(epsg))
    if feedback is not None:
        feedback.pushInfo("extra copy in {}".format(crs))
    dd.warp_to(tif, dest, epsg)
    return dest
