"""Estimate reservoir storage per dam from cached DEM + NHD HR waterbodies."""
import os

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink,
                       QgsFields, QgsField, QgsFeature,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject,
                       QgsFeatureRequest, QgsRectangle)

from ..core import storage
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_POINT, SRC_POLYGON)
from . import _common as cm
from ..core import errors as er
from ..damsuite import gdalsafe as GS

NEW_FIELDS = [("est_pool_m", FT_DOUBLE), ("est_crest_m", FT_DOUBLE),
              ("est_area_m2", FT_DOUBLE), ("est_below_m3", FT_DOUBLE),
              ("est_above_m3", FT_DOUBLE), ("est_total_m3", FT_DOUBLE),
              ("est_maxdep_m", FT_DOUBLE), ("shore_deg", FT_DOUBLE),
              ("stor_used_m3", FT_DOUBLE), ("stor_method", FT_STRING),
              ("stor_conf", FT_STRING), ("inv_vs_est", FT_DOUBLE),
              ("stor_notes", FT_STRING),
              ("below_scale", FT_DOUBLE), ("cal_total_m3", FT_DOUBLE)]


class EstimateStorage(QgsProcessingAlgorithm):
    DAMS, WB, DEMDIR, SEARCH, SHAPE, OUT, CURVES = ("DAMS", "WB", "DEMDIR", "SEARCH",
                                                    "SHAPE", "OUT", "CURVES")

    def name(self): return "estimatestorage"
    def displayName(self): return "3 - Estimate reservoir storage"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return EstimateStorage()

    def shortHelpString(self):
        return ("For each dam, finds the nearest NHD HR waterbody, reads the pool elevation "
                "from the hydro-flattened DEM, measures storage between pool and crest "
                "directly from the DEM, and ESTIMATES storage below pool by projecting the "
                "shoreline slope inward (capped by dam height). The below-pool part is the "
                "uncertain one: on a synthetic rounded pond it overestimated by ~40%. If the "
                "dam has inventory normal storage (DWR or NID), that value is used "
                "(stor_method = inventory): it calibrates the below-pool part of the curve "
                "(below_scale) while the above-pool part stays DEM-measured. inv_vs_est is "
                "inventory normal storage / estimated storage at normal pool. Use the shape factor to "
                "calibrate against dams with known storage.")

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.DAMS, "Standardized dams", [SRC_POINT]))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.WB, "NHD HR waterbodies (polygons)", [SRC_POLYGON]))
        self.addParameter(QgsProcessingParameterFile(
            self.DEMDIR, "Folder of cached dam DEMs", behavior=FILE_FOLDER))
        cm.distance_params(self.addParameter, "SEARCH_FT", self.SEARCH,
                           "Waterbody search distance from dam point (ft)", 500.0, min_ft=30.0)
        self.addParameter(QgsProcessingParameterNumber(
            self.SHAPE, "Below-pool shape factor (calibration)",
            NUM_DOUBLE, 1.0, minValue=0.1, maxValue=1.5))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.CURVES, "Folder for stage-storage curves (CSV)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT, "Dams with storage", SRC_POINT))

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _rasterize(geoms_wkb, ds):
        gt = ds.GetGeoTransform()
        mem = GS.checked(gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte),
                         "making the water-body mask")
        mem.SetGeoTransform(gt); mem.SetProjection(ds.GetProjection())
        srs = osr.SpatialReference(); srs.ImportFromEPSG(5070)
        vds = (ogr.GetDriverByName("MEM") or ogr.GetDriverByName("Memory")).CreateDataSource("wb")
        lyr = vds.CreateLayer("wb", srs, ogr.wkbPolygon)
        for w in geoms_wkb:
            ft = ogr.Feature(lyr.GetLayerDefn()); ft.SetGeometry(ogr.CreateGeometryFromWkb(w))
            lyr.CreateFeature(ft)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1])
        return mem.ReadAsArray().astype(bool)

    def _inventory_curve(self, f, names, arr, row, col, params, context, why):
        """The assumed full-height curve of a dam from its inventory storage and height
        (storage ~ depth squared from the ground at the dam to ground + height), or None when
        the record lacks a usable storage (> 1 m3) or height (> 0.5 m)."""
        def num(k):
            v = f[k] if k in names else None
            try:
                return float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                return 0.0
        inv_max, inv_norm, hh = num("inv_max_m3"), num("inv_norm_m3"), num("height_m")
        choice = self.parameterAsEnum(params, "STORAGE_CHOICE", context) \
            if self.parameterDefinition("STORAGE_CHOICE") else 0
        vol, how = storage.storage_choice_volume(inv_norm, inv_max, choice)
        if not (vol > 1.0 and hh > 0.5):
            return None
        ground = None
        if arr is not None and row is not None and col is not None:
            try:
                ground = float(np.nanmedian(arr[max(0, row - 2):row + 3, max(0, col - 2):col + 3]))
            except Exception:                                  # noqa
                ground = None
        if ground is None or ground != ground:
            ground = 0.0
        curve = [(ground + i * hh / 10.0, vol * (i / 10.0) ** 2) for i in range(11)]
        note = ("dry-dam inventory curve: assumption, using {} {:.0f} m3 (normal {:.0f}, maximum "
                "{:.0f}) and height {:.1f} m; {}").format(how, vol, inv_norm, inv_max, hh, why)
        return storage.StorageResult(ground, ground + hh, 0.0, 0.0, vol, vol, hh, 0.0,
                                     "inventory", curve, [note])

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, self.DAMS, context)
        wbs = self.parameterAsSource(params, self.WB, context)
        demdir = self.parameterAsString(params, self.DEMDIR, context)
        search = cm.distance_m(self, params, context, "SEARCH_FT", self.SEARCH, 500.0)
        shape = self.parameterAsDouble(params, self.SHAPE, context)
        curves = self.parameterAsString(params, self.CURVES, context)
        os.makedirs(curves, exist_ok=True)

        fields = QgsFields(dams.fields())
        for n, t in NEW_FIELDS: fields.append(QgsField(n, t))
        sink, dest = self.parameterAsSink(params, self.OUT, context, fields,
                                          dams.wkbType(), dams.sourceCrs())
        alb = QgsCoordinateReferenceSystem("EPSG:5070")
        d2a = QgsCoordinateTransform(dams.sourceCrs(), alb, QgsProject.instance())
        a2w = QgsCoordinateTransform(alb, wbs.sourceCrs(), QgsProject.instance())
        w2a = QgsCoordinateTransform(wbs.sourceCrs(), alb, QgsProject.instance())
        names = dams.fields().names()
        total = dams.featureCount() or 1

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(f["dam_id"]) if "dam_id" in names else str(f.id())
            out = QgsFeature(fields); out.setGeometry(f.geometry())
            vals = list(f.attributes()) + [None] * len(NEW_FIELDS)
            res, note = None, ""
            # reset per dam: the dry-dam fallback must not see the previous dam's raster, and the
            # frame-dict lookups it used kept the frame (the output sink too) referenced on
            # Python 3.12, so 3_dams_storage.gpkg was left without its layer
            arr = row = col = None
            try:
                dem_path = os.path.join(demdir, f"dam_{dam_id}.tif")
                if not os.path.exists(dem_path):
                    raise FileNotFoundError("no cached DEM")
                ds = cm.gdal_open(dem_path)
                arr = ds.GetRasterBand(1).ReadAsArray().astype("float64")
                nd = ds.GetRasterBand(1).GetNoDataValue()
                if nd is not None: arr[arr == nd] = np.nan
                gt = ds.GetGeoTransform(); cell = abs(gt[1])

                g = f.geometry(); g.transform(d2a); p = g.asPoint()
                # grid position first: the no-waterbody (dry-dam) fallback samples the ground here;
                # set after the waterbody lookup it stayed None and the curve started at 0 m
                col = int((p.x() - gt[0]) / gt[1]); row = int((p.y() - gt[3]) / gt[5])
                box = QgsRectangle(p.x() - search, p.y() - search, p.x() + search, p.y() + search)
                box_w = a2w.transformBoundingBox(box)
                cands = []
                for w in wbs.getFeatures(QgsFeatureRequest().setFilterRect(box_w)):
                    wg = w.geometry(); wg.transform(w2a)
                    dist = wg.distance(g)
                    if dist <= search: cands.append((dist, -wg.area(), wg))
                if not cands: raise LookupError("no waterbody within search distance")
                cands.sort(key=lambda c: (round(c[0]), c[1]))
                mask = self._rasterize([cands[0][2].asWkb()], ds)

                h = f["height_m"] if "height_m" in names else None
                h = float(h) if h not in (None, "") else None
                res = storage.estimate_storage(arr, mask, cell, (row, col),
                                               known_height_m=h, shape_factor=shape)
                note = "; ".join(res.notes)
                # shallow-basin guard (ported from nd_ Screening 0.28.3): a basin whose whole
                # curve, bed to crest, spans far less than the dam height is not the dam's
                # reservoir (a dish on the floor of a dry flood-control dam). Stretching it to
                # the inventory maximum puts nearly all the volume in the top few feet and hands
                # the Runner a storage-area exponent at its cap; the full-height inventory curve
                # is used instead when the record allows it.
                span = storage.curve_span_m(res)
                if storage.is_shallow_basin(span, h):
                    alt = self._inventory_curve(f, names, arr, row, col, params, context,
                                                storage.SHALLOW_BASIN_WHY.format(span / storage.FT_M,
                                                                                 h / storage.FT_M))
                    if alt is not None:
                        res, note = alt, "{} (water body: {})".format(alt.notes[0], note)
                        feedback.pushWarning("dam {}: {}".format(dam_id, alt.notes[0]))
            except Exception as e:  # keep going, record why
                note = "FAILED: " + er.plain(e, dam_id, "3")
                # Dry dam: no pool polygon. Use inventory max storage and height.
                alt = None
                if "no waterbody" in note:
                    alt = self._inventory_curve(f, names, arr, row, col, params, context,
                                                "no waterbody polygon")
                if alt is not None:
                    res, note = alt, alt.notes[0]
                    feedback.pushWarning("dam {}: {}".format(dam_id, note))
                else:
                    feedback.reportError("dam {}: {}".format(dam_id, er.plain(e, dam_id, "3")))

            k = len(f.attributes())
            if res is not None:
                inv = f["inv_norm_m3"] if "inv_norm_m3" in names else None
                inv = float(inv) if inv not in (None, "") else None
                curve, scale, cnote = storage.calibrate_below_pool(
                    res.curve, res.below_pool_m3, inv)
                if cnote: note = (note + "; " if note else "") + cnote
                choice = self.parameterAsEnum(params, "STORAGE_CHOICE", context) if self.parameterDefinition("STORAGE_CHOICE") else 0
                inv_max = f["inv_max_m3"] if "inv_max_m3" in names else None
                try:
                    inv_max = float(inv_max) if inv_max not in (None, "") else 0.0
                except (TypeError, ValueError):
                    inv_max = 0.0
                inv_norm = inv or 0.0
                if int(choice or 0) != 1:
                    curve, tnote = storage.ensure_top_of_dam(
                        curve, inv_max, getattr(res, "crest_elev", None))
                    if tnote:
                        note = (note + "; " if note else "") + tnote
                if inv_max > 1.0 and inv_norm > 1.0 and inv_max > 3.0 * inv_norm:
                    note = (note + "; " if note else "") + "assumption: maximum storage is more than 3 times normal"
                purpose = ""
                for key in ("purpose", "purposes", "dam_purpose"):
                    if key in names and f[key]:
                        purpose = str(f[key])
                        break
                if "flood" in purpose.lower():
                    note = (note + "; " if note else "") + "assumption: purpose is flood control; used {}".format(
                        "normal storage" if int(choice or 0) == 1 else "maximum storage")
                cal_total = curve[-1][1]
                used, method, conf = ((inv, "inventory", "high") if inv else
                                      (res.total_m3, "estimated", res.confidence))
                vals[k:] = [res.pool_elev, res.crest_elev, res.surface_area_m2, res.below_pool_m3,
                            res.above_pool_m3, res.total_m3, res.max_depth_m, res.shore_slope_deg,
                            used, method, conf,
                            (inv / res.below_pool_m3) if inv and res.below_pool_m3 else None,
                            note, scale, cal_total]
                with open(os.path.join(curves, f"curve_{dam_id}.csv"), "w") as fh:
                    fh.write("elev_m,storage_m3\n")     # calibrated curve
                    for e, v in curve: fh.write(f"{e:.3f},{v:.1f}\n")
            else:
                vals[k + 9] = "none"; vals[k + 10] = "none"; vals[k + 12] = note
            out.setAttributes(vals)
            sink.addFeature(out)
        return {self.OUT: dest}
