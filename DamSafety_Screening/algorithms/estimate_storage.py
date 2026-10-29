"""Estimate reservoir storage per dam from cached DEM + NHD HR waterbodies."""
import os

import numpy as np
from osgeo import gdal, ogr, osr
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFolderDestination, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink,
                       QgsProcessingParameterEnum, QgsProcessingParameterField,
                       QgsFields, QgsField, QgsFeature,
                       QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject,
                       QgsFeatureRequest, QgsRectangle)

from ..core import storage
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_LINE, SRC_POINT,
                      SRC_POLYGON)
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
# 0.17.47: where the inventory storage used sits on the curve (core/storage.py
# anchor_inventory_storage). Kept out of NEW_FIELDS, which tools 17/18 (find ponds, propose dam)
# build their layers on and fill by position.
ANCHOR_FIELDS = [("stor_anchor", FT_STRING), ("stor_anchor_m", FT_DOUBLE)]


def _storage_choice(alg, params, context):
    """STORAGE_CHOICE of `alg`: 0 (Max) when it has no such parameter (a model or script from
    before 0.17.27, or a stand-in object in a test)."""
    if alg.parameterDefinition("STORAGE_CHOICE") is None:
        return storage.STORAGE_MAX
    return alg.parameterAsEnum(params, "STORAGE_CHOICE", context)


class EstimateStorage(QgsProcessingAlgorithm):
    DAMS, WB, DEMDIR, SEARCH, SHAPE, OUT, CURVES = ("DAMS", "WB", "DEMDIR", "SEARCH",
                                                    "SHAPE", "OUT", "CURVES")
    STORAGE_CHOICE = "STORAGE_CHOICE"      # 0.17.27: the window's storage choice (0 Max)
    LINES = "LINES"      # 0.17.56: DWR dam lines (optional) - the crest at mid-crest (damsuite.midcrest)

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
                "calibrate against dams with known storage.\n\n"
                "Storage to use (default Max storage): Max places the inventory maximum at the "
                "max pool - (DWR max_pool_elev - normal_elev) above the lidar water surface, which "
                "is the normal pool - with the normal storage at the normal pool and only the DEM "
                "fill above the max pool added on top (stor_anchor, stor_anchor_m). A max pool "
                "above the DEM crest is placed at the crest (MAXPOOL-ABOVE-CREST): the storage at "
                "the top of dam is set to the inventory maximum (never below it), which can be less "
                "than the normal storage plus the DEM fill to the crest (the inventory difference "
                "replaces that fill, as it does between the two pools). Without a usable normal "
                "storage, a DEM fill from the pool to the max pool larger than the maximum keeps "
                "the DEM curve (FILL-EXCEEDS-MAX; the storage at the top of dam is then above the "
                "maximum); with one, the DEM fill between the two pools is scaled to fit between "
                "them. "
                "Without the pool elevations (NID-only records), or with a max pool below the "
                "normal pool or at it with a larger maximum (MAXPOOL-ELEV-BAD), it raises the "
                "storage at the top of dam to the inventory maximum as before, flagged FALLBACK in "
                "stor_notes. A normal storage equal to the maximum (both read from the max-storage "
                "field) is flagged NORMAL-EQUALS-MAX and not used at the normal pool; one above "
                "the maximum is flagged NORMAL-NOT-BELOW-MAX (not used at the normal pool when the "
                "pool elevations are known, kept there as before in the fallback). Normal keeps "
                "the DEM-measured part above the normal "
                "pool (a sunny-day check); Larger of the two uses whichever inventory value is "
                "larger. A missing or zero value falls back to the other one (Normal without a "
                "normal storage uses the maximum, Max without a maximum uses the normal); with "
                "neither, the DEM estimate is kept. Dams without a water body use the chosen "
                "value for their assumed full-height curve. stor_notes and the log start with "
                "the storage used and any fallback.")

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
        self.addParameter(QgsProcessingParameterEnum(
            self.STORAGE_CHOICE, "Storage to use", list(storage.STORAGE_CHOICES),
            defaultValue=storage.STORAGE_MAX))
        # 0.17.56 (Nick 2026-10-28): the crest is read at mid-crest of the dam's line (its
        # midpoint by length, the highest DEM point across it +-25 m), not the 98th percentile
        # near the dam point; dams without a line keep the old read (logged)
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.LINES, "DWR dam lines (optional: the crest at mid-crest)", [SRC_LINE],
            optional=True))
        self.addParameter(QgsProcessingParameterField(
            "LID", "Dam lines: structure ID field", defaultValue="structure_id",
            parentLayerParameterName=self.LINES, optional=True))
        self.addParameter(QgsProcessingParameterFolderDestination(
            self.CURVES, "Folder for stage-storage curves (CSV)"))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT, "Dams with storage", SRC_POINT))

    # -- helpers ---------------------------------------------------------
    def _choice(self, params, context):
        """STORAGE_CHOICE (0 Max when the parameter is absent: older models / scripts)."""
        return _storage_choice(self, params, context)

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
        pick = storage.resolve_storage_choice(inv_norm, inv_max, _storage_choice(self, params, context))
        vol = pick["volume_m3"]
        how = "{} storage".format(pick["kind"])
        if vol is None or not hh > 0.5:
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
        choice = self._choice(params, context)
        feedback.pushInfo("Storage to use: {}".format(
            storage.resolve_storage_choice(0, 0, choice)["label"]))

        fields = QgsFields(dams.fields())
        for n, t in NEW_FIELDS + ANCHOR_FIELDS: fields.append(QgsField(n, t))
        sink, dest = self.parameterAsSink(params, self.OUT, context, fields,
                                          dams.wkbType(), dams.sourceCrs())
        alb = QgsCoordinateReferenceSystem("EPSG:5070")
        d2a = QgsCoordinateTransform(dams.sourceCrs(), alb, QgsProject.instance())
        a2w = QgsCoordinateTransform(alb, wbs.sourceCrs(), QgsProject.instance())
        w2a = QgsCoordinateTransform(wbs.sourceCrs(), alb, QgsProject.instance())
        names = dams.fields().names()
        total = dams.featureCount() or 1
        lines = (self.parameterAsSource(params, self.LINES, context)
                 if self.parameterDefinition(self.LINES) is not None else None)
        l2a = a2l = None
        by_id = {}
        if lines is not None:
            l2a = QgsCoordinateTransform(lines.sourceCrs(), alb, QgsProject.instance())
            a2l = QgsCoordinateTransform(alb, lines.sourceCrs(), QgsProject.instance())
            by_id = cm.dam_line_index(lines, self.parameterAsString(params, "LID", context)
                                      or "structure_id")
        n_mid = 0

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100 * i / total)
            dam_id = str(f["dam_id"]) if "dam_id" in names else str(f.id())
            out = QgsFeature(fields); out.setGeometry(f.geometry())
            vals = list(f.attributes()) + [None] * (len(NEW_FIELDS) + len(ANCHOR_FIELDS))
            res, note = None, ""
            # M19: True when `res` is the assumed quadratic inventory curve from _inventory_curve
            # (a guess from inventory volume + height, not a DEM measurement); it must be reported
            # low confidence and labelled as the assumed kind, never high-confidence 'inventory'
            assumed = False
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
                mc = mc_note = None
                if lines is not None:
                    verts, how = cm.dam_line_verts(lines, by_id, dam_id, g, l2a, a2l, search)
                    mc = cm.midcrest_on_dem(verts, arr, gt, (p.x(), p.y())) if verts else None
                    if mc is not None:
                        n_mid += 1
                        mc_note = ("crest at mid-crest ({}): {:.2f} m NAVD88 at ({:.1f}, {:.1f}), "
                                   "{:.1f} m from the dam point").format(
                            how, mc["z_m"], mc["xy"][0], mc["xy"][1], mc["moved_m"])
                    else:
                        mc_note = "crest not at mid-crest: {}".format(
                            how if not verts else "no DEM across the dam line's midpoint")
                        feedback.pushWarning("dam {}: {}; the crest is read near the dam "
                                             "point".format(dam_id, mc_note))
                res = storage.estimate_storage(arr, mask, cell, (row, col),
                                               known_height_m=h, shape_factor=shape,
                                               crest_z_m=mc["_z_m"] if mc else None)
                if mc_note:
                    res.notes.append(mc_note)
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
                        assumed = True
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
                    assumed = True
                    feedback.pushWarning("dam {}: {}".format(dam_id, note))
                else:
                    feedback.reportError("dam {}: {}".format(dam_id, er.plain(e, dam_id, "3")))

            k = len(f.attributes())
            if res is not None:
                inv = f["inv_norm_m3"] if "inv_norm_m3" in names else None
                inv = float(inv) if inv not in (None, "") else None
                inv_max = f["inv_max_m3"] if "inv_max_m3" in names else None
                try:
                    inv_max = float(inv_max) if inv_max not in (None, "") else 0.0
                except (TypeError, ValueError):
                    inv_max = 0.0
                inv_norm = inv or 0.0
                # the storage choice (0.17.27). resolve_storage_choice says which inventory
                # value is used (Max; Larger when the maximum is larger; Normal without a normal
                # storage -> the maximum; Normal; Max without a maximum -> the normal), and any
                # fallback. 0.17.47: each storage sits at its own elevation
                # (storage.anchor_inventory_storage): the normal storage at the normal pool (the
                # lidar water surface), the maximum at the max pool (inventory max-pool minus
                # normal-pool elevation above the water surface) with only the DEM fill above
                # the max pool added on top. Before, the "normal" storage was always put at the
                # water surface and the DEM fill to the spill or crest added on top, so a record
                # whose normal box held the maximum (DWR-2865: 167 ac-ft at 1870 ft) came out
                # 237.6 ac-ft. Without the max-pool elevation the maximum is placed at the top of
                # dam as before (flagged FALLBACK in stor_notes); a max pool above the crest puts
                # the maximum at the crest (MAXPOOL-ABOVE-CREST), so the storage at the top of dam
                # is never below the maximum.
                pick = storage.resolve_storage_choice(inv_norm, inv_max, choice)
                anchor_m = None
                if assumed:
                    # the assumed full-height curve already holds the inventory volume at its top
                    curve, scale, cnote = storage.calibrate_below_pool(
                        res.curve, res.below_pool_m3, inv)
                    if cnote: note = (note + "; " if note else "") + cnote
                    anchor = "assumed inventory curve"
                    if pick["kind"] == "maximum":
                        curve, tnote = storage.ensure_top_of_dam(
                            curve, pick["volume_m3"], getattr(res, "crest_elev", None))
                        if tnote:
                            note = (note + "; " if note else "") + tnote
                else:
                    ne = f["inv_norm_elev_m"] if "inv_norm_elev_m" in names else None
                    me = f["inv_max_elev_m"] if "inv_max_elev_m" in names else None
                    curve, scale, ainfo = storage.anchor_inventory_storage(
                        res.curve, res.below_pool_m3, res.pool_elev, inv_norm, inv_max,
                        pick["kind"], ne, me, getattr(res, "crest_elev", None))
                    anchor, anchor_m = ainfo["anchor"], ainfo["anchor_m"]
                    extra = ainfo["notes"] + ainfo["flags"]
                    if extra:
                        note = (note + "; " if note else "") + "; ".join(extra)
                    for fl in ainfo["flags"]:
                        # a record without pool elevations (every NID-only dam) is the documented
                        # fallback: logged, not warned; every other flag is a warning
                        (feedback.pushInfo if fl.startswith("MAXPOOL-ELEV-MISSING")
                         else feedback.pushWarning)("dam {}: {}".format(dam_id, fl))
                note = pick["text"] + ("; " + note if note else "")
                (feedback.pushWarning if pick["fallback"] else feedback.pushInfo)(
                    "dam {}: {}".format(dam_id, pick["text"]))
                if inv_max > 1.0 and inv_norm > 1.0 and inv_max > 3.0 * inv_norm:
                    note = (note + "; " if note else "") + "assumption: maximum storage is more than 3 times normal"
                purpose = ""
                for key in ("purpose", "purposes", "dam_purpose"):
                    if key in names and f[key]:
                        purpose = str(f[key])
                        break
                if "flood" in purpose.lower():
                    note = (note + "; " if note else "") + "assumption: purpose is flood control; used {}".format(
                        "{} storage".format(pick["kind"]) if pick["kind"] != "none"
                        else "the DEM estimate")
                cal_total = curve[-1][1]
                used, method, conf = ((inv, "inventory", "high") if inv else
                                      (res.total_m3, "estimated", res.confidence))
                if assumed:
                    # M19 (ported from nd_ 0.26.0 estimate_storage.py): the assumed quadratic curve
                    # already holds the inventory volume at its top, but it is a GUESS whatever the
                    # inventory fields say - report it low confidence and labelled as the assumed
                    # kind, not a high-confidence 'inventory' measurement.
                    used, method, conf = res.total_m3, "inventory curve", "low"
                vals[k:] = [res.pool_elev, res.crest_elev, res.surface_area_m2, res.below_pool_m3,
                            res.above_pool_m3, res.total_m3, res.max_depth_m, res.shore_slope_deg,
                            used, method, conf,
                            (inv / res.below_pool_m3) if inv and res.below_pool_m3 else None,
                            note, scale, cal_total, anchor, anchor_m]
                with open(os.path.join(curves, f"curve_{dam_id}.csv"), "w", encoding="utf-8") as fh:
                    fh.write("elev_m,storage_m3\n")     # calibrated curve
                    for e, v in curve: fh.write(f"{e:.3f},{v:.1f}\n")
            else:
                vals[k + 9] = "none"; vals[k + 10] = "none"; vals[k + 12] = note
            out.setAttributes(vals)
            sink.addFeature(out)
        if lines is not None:
            feedback.pushInfo("crest at mid-crest of the dam line: {} of {} dams".format(
                n_mid, dams.featureCount()))
        else:
            # info, not a warning: without the optional layer the tool works as before 0.17.56
            feedback.pushInfo("no dam lines given: the crest is read near each dam point (not "
                              "at mid-crest)")
        return {self.OUT: dest}
