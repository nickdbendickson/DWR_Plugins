"""Download a local DEM for one dam, one North Dakota county, or the state.

Resolution is 10 m or 1 m. Source is USGS 3DEP (ImageServer), ND DWR QL2 LiDAR, or (0.17.29)
the USGS 3DEP 1 m lidar project tiles themselves from The National Map (core/lidar_tnm.py).
For the first two the local file is a tiled GeoTIFF, float32, EPSG:5070, elevations in
metres. The native 1 m tiles are kept as they are (UTM, metres) in the suite's lidar tile folder
and the result is a per-dam VRT over them. A sidecar text file lists source, CRS, units and file
type.
"""
import json
import os

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterEnum,
                       QgsProcessingParameterString, QgsProcessingParameterNumber,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterBoolean,
                       QgsProcessingException, QgsProcessingOutputFile, QgsProcessingParameterFile)

from ..compat import FILE_FOLDER as _FOLDER, NUM_DOUBLE
from ..core import dem_download as dd
from ..core import dem_names as dn
from ..core import lidar_tnm as lt
from ..core.tile_workdir import tile_workdir


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
            "An existing file of the same name is reused. "
            "USGS 3DEP 1 m lidar tiles (native, TNM): the lidar project tiles themselves "
            "(10 km GeoTIFFs at 1 m, UTM metres, about 275 MB each) from The National Map, not "
            "a resampled image. Area: the box, else the dam folder (the Runner's model bounds, "
            "else its dam point and the buffer), else the dam point and buffer, else the "
            "county. The first run only lists the tiles (project, collection and publication "
            "dates, sizes), the total download and the free disk space; tick 'Download the "
            "listed tiles' to download them. Tiles go to the tile folder, else <suite data "
            "folder>/dem/lidar_1m/ (never the offline pack or a read-only folder); tiles already "
            "there are skipped and a cut download resumes. Where two projects cover the same "
            "tile the newest collection is used and the log says so. The result is "
            "<DAM_ID>_1m_lidar.vrt in the output folder, with the coarser DEM underneath when "
            "one is given or <DAM_ID>_10m_5070.tif is there, and <DAM_ID>_1m_lidar.json listing "
            "the projects and tiles."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterEnum(
            "SOURCE", "Source", ["USGS 3DEP", "ND DWR LiDAR (lidar.dwr.nd.gov)", lt_label()],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "RES", "Local DEM resolution", ["10 m", "1 m"], defaultValue=0))
        self.addParameter(QgsProcessingParameterEnum(
            "SCOPE", "Area", ["Dam", "County", "State"], defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            "DAM_POINT", "Dam point, longitude,latitude", optional=True))
        self.addParameter(QgsProcessingParameterString(
            "DAM_ID", "Dam ID (names the file <DAM_ID>_10m_5070.tif)", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "BUFFER_MI", "Buffer around the dam (miles)", NUM_DOUBLE, 15.0, minValue=1.0))
        self.addParameter(QgsProcessingParameterString(
            "BEARING_DEG", "Downstream bearing (deg from north; blank = centered on the dam)",
            optional=True))
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
        # 0.17.29: USGS 3DEP 1 m lidar tiles (native, TNM); used only with that source
        self.addParameter(QgsProcessingParameterString(
            "BBOX", "1 m tiles: box west,south,east,north in degrees (optional)", optional=True))
        self.addParameter(QgsProcessingParameterFile(
            "DAM_FOLDER", "1 m tiles: dam folder (the Runner's model bounds, else dam.json's point)",
            behavior=_FOLDER, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            "DOMAIN_PAD_M", "1 m tiles: margin around the Runner's model bounds (m)", NUM_DOUBLE, 500.0,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterFile(
            "TILE_FOLDER", "1 m tiles: tile folder (empty = <suite data folder>/dem/lidar_1m)",
            behavior=_FOLDER, optional=True))
        self.addParameter(QgsProcessingParameterBoolean(
            "LIDAR_DOWNLOAD", "1 m tiles: download the listed tiles (off = list tiles, sizes and free space only)",
            defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            "LIDAR_MAX_GB", "1 m tiles: largest download for one run (GB)", NUM_DOUBLE, 10.0, minValue=0.1))
        self.addParameter(QgsProcessingParameterBoolean(
            "LIDAR_KEEP_OLDER", "1 m tiles: also keep older overlapping projects underneath", defaultValue=False))
        self.addParameter(QgsProcessingParameterFile(
            "FALLBACK_DEM", "1 m tiles: coarser DEM underneath (empty = <DAM_ID>_10m_5070.tif if present)",
            optional=True))
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
            ["USGS 3DEP", "ND DWR LiDAR", lt_label()][min(2, self.parameterAsEnum(parameters, "SOURCE", context))],
            cell if self.parameterAsEnum(parameters, "SOURCE", context) != 2 else 1,
            ["dam", "county", "state"][scope]))
        if cell == 1 and scope == 2 and self.parameterAsEnum(parameters, "SOURCE", context) != 2:
            feedback.pushInfo("1 m for the whole state is a very large download. 10 m is the working copy.")
        source_i = self.parameterAsEnum(parameters, "SOURCE", context)
        if source_i == 2:
            return self._tnm_1m(parameters, context, feedback, out, scope)
        stem = self._stem(parameters, context, scope)
        dest = os.path.join(out, "{}_{}m_5070.tif".format(stem, int(cell)))
        side = os.path.splitext(dest)[0] + "_dem.txt"
        if os.path.isfile(dest) and os.path.isfile(side):
            feedback.pushInfo("reusing {} ({})".format(dest, open(side, encoding="utf-8").read().replace("\n", "; ")))
            return {"VRT": dest, "OUT": out}
        # Source tiles (~30 files, ~170 MB for a dam) go to a temporary folder that is removed
        # after the local GeoTIFF is written, on success and on failure alike.
        with tile_workdir(out) as work:
            if source_i == 1:
                vrt, native = self._nd(parameters, context, feedback, work, scope, index_dir=out)
                info = dd.product_info("ndlidar", cell, native)
            else:
                vrt = self._usgs(parameters, context, feedback, work, scope, cell, stem)
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
            # <DAM_ID>_<cell>m_5070.tif when the dam ID is given: the wizards pick it up.
            dam_id = self.parameterAsString(parameters, "DAM_ID", context) \
                if "DAM_ID" in parameters else ""
            return dn.dam_scope_stem(dam_id, self.parameterAsString(parameters, "DAM_POINT", context))
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
            extent = dd.point_box(lon, lat, self.parameterAsDouble(parameters, "BUFFER_MI", context),
                                  bearing_deg=_bearing(self, parameters, context, feedback))
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

    def _nd(self, parameters, context, feedback, out, scope, index_dir=None):
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
                cache = os.path.join(index_dir or out, "_index")      # the index is kept
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


    # ------------------------------------------------------------- 0.17.29: native 1 m tiles

    def _opt(self, parameters, context, name):
        return (self.parameterAsString(parameters, name, context) or "").strip() \
            if parameters.get(name) not in (None, "") else ""

    def _tnm_area(self, parameters, context, scope):
        """(bbox_ll, dam_id, how) for the native 1 m tiles."""
        try:
            box = lt.parse_bbox(self._opt(parameters, context, "BBOX"))
        except ValueError as e:
            raise QgsProcessingException(str(e))
        buffer_mi = self.parameterAsDouble(parameters, "BUFFER_MI", context)
        dam_folder = self._opt(parameters, context, "DAM_FOLDER")
        dam_id = self._opt(parameters, context, "DAM_ID")
        if box:
            return box, dam_id, "the box given"
        if dam_folder:
            try:
                box, rec_id, how = lt.domain_of_dam_folder(
                    dam_folder, self.parameterAsDouble(parameters, "DOMAIN_PAD_M", context), buffer_mi)
            except ValueError as e:
                raise QgsProcessingException(str(e))
            return box, dam_id or rec_id or os.path.basename(os.path.normpath(dam_folder)), how
        if scope == 0:
            raw = self._opt(parameters, context, "DAM_POINT")
            try:
                lon, lat = [float(x) for x in raw.replace(" ", "").split(",")[:2]]
            except ValueError:
                raise QgsProcessingException(
                    "1 m tiles need a box, a dam folder, or a dam point (longitude,latitude)")
            return lt.point_box_ll(lon, lat, buffer_mi), dam_id, "dam point + {} mi".format(buffer_mi)
        if scope == 1:
            name = self._county(parameters, context)
            return dd.COUNTIES[name], name.replace(" ", "_"), "county {}".format(name)
        raise QgsProcessingException(
            "1 m lidar tiles for the whole state are thousands of tiles (hundreds of GB). "
            "Choose a dam, a county or a box.")

    def _tnm_1m(self, parameters, context, feedback, out, scope):
        box, dam_id, how = self._tnm_area(parameters, context, scope)
        stem = dn.dem_stem(dam_id) or dn.dam_scope_stem("", self._opt(parameters, context, "DAM_POINT"))
        if not dam_id and scope == 0 and not self._opt(parameters, context, "DAM_POINT"):
            stem = "box"
        try:
            from ..damsuite import suitedata as SD
            suite = SD.data_folder()
        except Exception:                                    # noqa - no suite folder lookup here
            suite = ""
        try:
            folder, where, notes = lt.tile_folder(self._opt(parameters, context, "TILE_FOLDER"), suite, out)
        except lt.TileError as e:
            raise QgsProcessingException(str(e))
        for n in notes:
            feedback.pushWarning(n)
        if where == "output":
            feedback.pushWarning("No suite data folder is set, so the tiles go to {}. Set the suite "
                                 "data folder or a tile folder to share them between dams.".format(folder))
        feedback.pushInfo("area: {} ({:.5f}, {:.5f}, {:.5f}, {:.5f})".format(how, *box))
        feedback.pushInfo("tile folder ({}): {}".format(where, folder))
        http = lt.UrlHttp()
        keep = self.parameterAsBool(parameters, "LIDAR_KEEP_OLDER", context)
        try:
            plan = lt.make_plan(box, folder, http, feedback, keep_older=keep)
        except lt.Cancelled:
            feedback.pushInfo("Cancelled. Nothing was downloaded.")
            return {"VRT": "", "OUT": out}
        except Exception as e:                               # noqa - reported as the tool's error
            raise QgsProcessingException("The National Map query failed: {}".format(e))
        feedback.setProgress(5)
        if not plan["tiles"]:
            raise QgsProcessingException("The National Map has no 1 m lidar tiles for that area.")
        for line in lt.describe_plan(plan):
            feedback.pushInfo(line)
        for n in plan["notes"]:
            feedback.pushWarning(n)
        ok, msg = lt.disk_check(plan, int(self.parameterAsDouble(parameters, "LIDAR_MAX_GB", context) * 1024 ** 3))
        feedback.pushInfo(msg)
        download = self.parameterAsBool(parameters, "LIDAR_DOWNLOAD", context)
        if plan["need_bytes"] and not download:
            feedback.pushInfo("Listed only. Tick 'download the listed tiles' and run again to download {}.".format(
                lt.fmt_bytes(plan["need_bytes"])))
            return {"VRT": "", "OUT": out}
        if plan["need_bytes"] and not ok:
            raise QgsProcessingException(msg)
        try:
            entries, failed = lt.fetch(plan, http, feedback)
        except lt.Cancelled:
            feedback.pushInfo("Cancelled. Finished tiles are kept and recorded; the current one "
                              "resumes on the next run.")
            return {"VRT": "", "OUT": out}
        for f in failed:
            feedback.reportError(f, False)
        if not entries:
            raise QgsProcessingException("No 1 m tile could be downloaded.")
        fallback = self._opt(parameters, context, "FALLBACK_DEM")
        if not fallback:
            cand = os.path.join(out, "{}_10m_5070.tif".format(stem))
            fallback = cand if os.path.isfile(cand) else ""
        if fallback and not os.path.isfile(fallback):
            feedback.pushWarning("fallback DEM {} not found; the VRT has the lidar tiles only".format(fallback))
            fallback = ""
        feedback.setProgress(92)
        vrt = os.path.join(out, "{}_1m_lidar.vrt".format(stem))
        try:
            vrt, js = lt.build_vrt(entries, vrt, fallback, box, feedback=feedback, meta={
                "dam_id": dam_id, "area": how, "tile_folder": folder,
                "missing_tiles": failed, "overlap_notes": plan["notes"]})
        except Exception as e:                               # noqa
            raise QgsProcessingException("building the VRT failed: {}".format(e))
        info = dd.product_info("tnm1m", 1, {})
        info["crs"] = json.load(open(js, encoding="utf-8"))["crs"]
        side = dd.write_sidecar(vrt, info)
        feedback.setProgress(100)
        feedback.pushInfo("projects: {}".format(", ".join(plan["projects"])))
        feedback.pushInfo("local DEM: {}{}".format(vrt, " (fallback underneath: {})".format(fallback) if fallback else ""))
        feedback.pushInfo("sources: {}".format(js))
        feedback.pushInfo("facts: {}".format(side))
        if failed:
            feedback.pushWarning("{} tile(s) failed; {}".format(
                len(failed), "the fallback DEM shows there" if fallback else "the VRT has holes there"))
        extra = _warp(vrt, out, parameters, context, feedback)
        if extra and extra != vrt:
            feedback.pushInfo("extra CRS copy: {}".format(extra))
        return {"VRT": vrt, "OUT": out}


def _bearing(alg, parameters, context, feedback=None):
    """The downstream bearing parameter as a float (compass deg from north), or None when blank
    or unparseable (then the DEM box is centred on the dam, as before)."""
    raw = (alg.parameterAsString(parameters, "BEARING_DEG", context) or "").strip()
    if not raw:
        return None
    try:
        return float(raw) % 360.0
    except ValueError:
        if feedback is not None:
            feedback.pushWarning("Downstream bearing {!r} is not a number; centring the DEM on "
                                 "the dam.".format(raw))
        return None


def lt_label():
    return "USGS 3DEP 1 m lidar tiles (native, TNM)"


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
