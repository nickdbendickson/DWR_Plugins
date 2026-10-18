"""Merge ND DWR dams (primary) with the NID (secondary) into one metric layer."""
import os
from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterField, QgsProcessingParameterNumber,
                       QgsProcessingParameterEnum, QgsProcessingParameterString,
                       QgsProcessingParameterFeatureSink,
                       QgsProcessingException, QgsFields, QgsField,
                       QgsFeature, QgsCoordinateTransform,
                       QgsCoordinateReferenceSystem, QgsProject, QgsSpatialIndex,
                       QgsPointXY, QgsGeometry)

from ..core import inventory as inv
from ..compat import (FIELD_ANY, FIELD_NUMERIC, FT_DOUBLE, FT_STRING, NUM_DOUBLE, SRC_POINT, WKB_POINT)
from . import _common as cm

OUT_FIELDS = [("dam_id", FT_STRING), ("name", FT_STRING),
              ("source", FT_STRING), ("nid_id", FT_STRING),
              ("height_m", FT_DOUBLE), ("inv_norm_m3", FT_DOUBLE),
              ("inv_max_m3", FT_DOUBLE), ("inv_area_m2", FT_DOUBLE),
              ("haz_existing", FT_STRING), ("status", FT_STRING),
              ("crest_len_m", FT_DOUBLE), ("shared_vol", FT_STRING)]

# field defaults: DWR shapefile names, and the NID GeoPackage names
FIELD_DEFAULTS = {f"DWR_{k}": v for k, v in inv.DWR_DEFAULTS.items()}
FIELD_DEFAULTS.update({f"NID_{k}": v for k, v in inv.NID_DEFAULTS.items()})

FIELD_KEYS = [("ID", "ID field", FIELD_ANY),
              ("NAME", "name field", FIELD_ANY),
              ("H", "dam height", FIELD_NUMERIC),
              ("NS", "normal storage", FIELD_NUMERIC),
              ("MS", "max storage", FIELD_NUMERIC),
              ("SA", "surface area", FIELD_NUMERIC),
              ("HZ", "hazard class", FIELD_ANY),
              ("STATUS", "status (e.g. Intact / Breached)", FIELD_ANY),
              ("NIDREF", "NID ID (links a DWR dam to the NID layer)", FIELD_ANY),
              ("LEN", "dam length", FIELD_NUMERIC)]


class PrepareDams(QgsProcessingAlgorithm):
    DWR, NID, OUT, DIST, KEEP, MINH, MINV = ("DWR", "NID", "OUT", "DIST", "KEEP", "MINH", "MINV")
    UNITS = ["Feet / acre-feet / acres", "Metres / m3 / m2"]

    def name(self): return "preparedams"
    def displayName(self): return "1 - Prepare dam inventory"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return PrepareDams()

    def shortHelpString(self):
        return ("Builds one metric dam layer. Defaults match the ND DWR 'Dams by Hazard Class' "
                "export (feet, acre-feet, acres; 0 = not recorded).\n"
                "Status filter: only dams whose status is in the list are kept (default 'Intact'); "
                "breached, removed, not-built, submerged and location-inconsistent dams are "
                "dropped. Leave empty to keep all.\n"
                "Size filter (optional): a dam is skipped only when BOTH its height and its storage "
                "are known and BOTH are below your limits, so dams with missing data are never "
                "skipped by accident. Use it to run the ~1000 larger dams first.\n"
                "NID (optional): matched to DWR dams first by NID ID, then by distance; matched "
                "values fill gaps in the DWR record (source = DWR+NID). NID dams with no DWR dam "
                "are added (source = NID).\n"
                "A blank field box is filled from the layer when the tool runs (DWR and NID "
                "names both match). NID boxes start from NIDID, NAME, DAM_HEIGHT, "
                "NORMAL_STORAGE, MAX_STORAGE, SURFACE_AREA, HAZARD_POTENTIAL, "
                "OPERATIONAL_STATUS and DAM_LENGTH when that layer is picked.\n"
                "shared_vol = YES marks records that share an identical large volume (e.g. levee "
                "'reach' segments of one reservoir): they are not separate reservoirs and need "
                "manual handling. Existing hazard text is kept as-is; DWR says 'Significant' where "
                "the ND policy says 'Medium'.")

    def initAlgorithm(self, config=None):
        for src, label in ((self.DWR, "ND DWR dams"), (self.NID, "NID dams")):
            self.addParameter(QgsProcessingParameterFeatureSource(
                src, f"{label} (points)", [SRC_POINT], optional=True))
            self.addParameter(QgsProcessingParameterEnum(
                f"{src}_UNITS", f"{label}: units", self.UNITS, defaultValue=0))
            for key, lab, kind in FIELD_KEYS:
                self.addParameter(QgsProcessingParameterField(
                    f"{src}_{key}", f"{label}: {lab}",
                    defaultValue=FIELD_DEFAULTS.get(f"{src}_{key}"),
                    parentLayerParameterName=src, type=kind, optional=True))
        self.addParameter(QgsProcessingParameterString(
            "COUNTY", "Only this North Dakota county (empty = all dams)",
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterString(
            self.KEEP, "Keep only these DWR statuses (comma-separated; empty = all)",
            defaultValue="Intact", optional=True))
        self.addParameter(QgsProcessingParameterString(
            "ONLY_IDS", "Only these dams (comma-separated DWR structure IDs, NID IDs or dam IDs such as "
            "DWR-5289; empty = all) - 'Run one dam' uses this", defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.MINH, "Size filter: skip dams lower than (ft)\u2026",
            NUM_DOUBLE, 0.0, minValue=0))
        self.addParameter(QgsProcessingParameterNumber(
            self.MINV, "\u2026AND holding less than (acre-ft). 0 and 0 = no size filter",
            NUM_DOUBLE, 0.0, minValue=0))
        cm.distance_params(self.addParameter, "DIST_FT", self.DIST,
                           "Match distance (ft) for DWR/NID pairs without a shared NID ID", 1600.0, min_ft=0.0)
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUT, "Standardized dams", SRC_POINT))

    # ------------------------------------------------------------------
    def _read(self, params, context, key, tag):
        """Return [(wgs_geometry, record dict)] for one source layer."""
        src = self.parameterAsSource(params, key, context)
        if src is None:
            return []
        metric = self.parameterAsEnum(params, f"{key}_UNITS", context) == 1
        fld = {k: (self.parameterAsString(params, f"{key}_{k}", context) or None)
               for k, _l, _t in FIELD_KEYS}
        names = [f.name() for f in src.fields()]
        aliases = inv.DWR_ALIASES if tag == "DWR" else inv.NID_ALIASES
        found = inv.match_fields(names, aliases)
        for k, _l, _t in FIELD_KEYS:
            if not fld.get(k):
                fld[k] = found.get(k)
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        tr = QgsCoordinateTransform(src.sourceCrs(), wgs, QgsProject.instance())
        out = []
        county = self.parameterAsString(params, "COUNTY", context)
        for i, f in enumerate(src.getFeatures()):
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                continue
            if county:
                gp = QgsGeometry(g)
                try:
                    gp.transform(tr)
                except Exception:
                    gp = None
                pt = None if gp is None or gp.isEmpty() else gp.centroid().asPoint()
                if not inv.feature_in_county(f, county, point=pt):
                    continue
            g.transform(tr)
            if g.isMultipart():                  # DWR points are stored as multipoints
                g = g.centroid()
            out.append((g, inv.standardize(lambda n, _f=f: _f[n], fld, metric, tag, i)))
        return out

    def processAlgorithm(self, params, context, feedback):
        dist = cm.distance_m(self, params, context, "DIST_FT", self.DIST, 1600.0)
        keep = inv.parse_status_list(self.parameterAsString(params, self.KEEP, context))
        min_h = self.parameterAsDouble(params, self.MINH, context)
        min_v = self.parameterAsDouble(params, self.MINV, context)
        dwr = self._read(params, context, self.DWR, "DWR")
        nid = self._read(params, context, self.NID, "NID")
        if not dwr and not nid:
            raise QgsProcessingException("Provide a DWR layer, an NID layer, or both.")

        fields = QgsFields()
        for n, t in OUT_FIELDS:
            fields.append(QgsField(n, t))
        wgs = QgsCoordinateReferenceSystem("EPSG:4326")
        sink, dest = self.parameterAsSink(params, self.OUT, context, fields,
                                          WKB_POINT, wgs)
        to_alb = QgsCoordinateTransform(wgs, QgsCoordinateReferenceSystem("EPSG:5070"),
                                        QgsProject.instance())

        def albers(g):
            g2 = QgsGeometry(g); g2.transform(to_alb)
            return g2.asPoint()

        # --- match NID dams to DWR dams (against ALL DWR records, so that a NID dam whose
        # --- DWR twin is breached is dropped with it instead of coming back as "NID only")
        fill, used = {}, set()
        nid_by_id = {a["id"].strip().upper(): j for j, (_g, a) in enumerate(nid)}
        for i, (_g, a) in enumerate(dwr):
            j = nid_by_id.get(a["nidref"]) if a["nidref"] else None
            if j is not None and j not in used:
                fill[i] = nid[j][1]; used.add(j)
        n_by_id = len(fill)
        if dwr and nid:
            index, dwr_pts = QgsSpatialIndex(), {}
            for i, (g, _a) in enumerate(dwr):
                if i in fill:
                    continue
                p = albers(g); dwr_pts[i] = p
                ft = QgsFeature(i)
                ft.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p.x(), p.y())))
                index.addFeature(ft)
            for j, (g, _a) in enumerate(nid):
                if j in used:
                    continue
                p = albers(g)
                hit = index.nearestNeighbor(QgsPointXY(p.x(), p.y()), 1, dist)
                if hit and hit[0] in dwr_pts and dwr_pts[hit[0]].distance(p) <= dist \
                        and hit[0] not in fill:
                    fill[hit[0]] = nid[j][1]; used.add(j)

        # --- assemble the output records
        only = {x.strip().upper() for x in (self.parameterAsString(params, "ONLY_IDS", context) or "").split(",")
                if x.strip()}

        def wanted(*ids):
            return not only or any(str(i).strip().upper() in only for i in ids if i not in (None, ""))

        recs, n_status, n_size, n_only = [], 0, 0, 0
        for i, (g, a) in enumerate(dwr):
            if not inv.status_ok(a["status"], keep):
                n_status += 1
                continue
            a = dict(a); a["id"] = f"DWR-{a['id']}"
            if not wanted(a["id"], a["id"][4:], a["nidref"]):
                n_only += 1
                continue
            src, nid_id = "DWR", a["nidref"]
            if i in fill:
                n = fill[i]; src = "DWR+NID"; nid_id = nid_id or n["id"]
                for k in ("h", "ns", "ms", "sa", "len"):
                    if a[k] is None: a[k] = n[k]
                if not a["name"]: a["name"] = n["name"]
                if not a["hz"]: a["hz"] = n["hz"]
            if inv.size_skip(a["h"], a["ns"] or a["ms"], min_h, min_v):
                n_size += 1
                continue
            recs.append((g, a, src, nid_id))
        for j, (g, a) in enumerate(nid):
            if j in used:
                continue
            if inv.size_skip(a["h"], a["ns"] or a["ms"], min_h, min_v):
                n_size += 1
                continue
            a = dict(a); a["id"] = f"NID-{a['id']}"
            if not wanted(a["id"], a["id"][4:]):
                n_only += 1
                continue
            recs.append((g, a, "NID", a["id"][4:]))

        shared = inv.shared_volume_flags([r[1]["ms"] or r[1]["ns"] for r in recs])
        for (g, a, src, nid_id), sh in zip(recs, shared):
            o = QgsFeature(fields); o.setGeometry(g)
            o.setAttributes([a["id"], a["name"], src, nid_id, a["h"], a["ns"], a["ms"], a["sa"],
                             a["hz"], a["status"], a["len"], "YES" if sh else ""])
            sink.addFeature(o)
        feedback.pushInfo(
            f"{len(recs)} dams written | dropped by status: {n_status}, by size: {n_size} | "
            f"NID matched: {len(used)} ({n_by_id} by ID) | flagged shared volume: {sum(shared)}" +
            (f" | not in the chosen IDs: {n_only}" if only else ""))
        if only and not recs:
            raise QgsProcessingException("None of the dams matches the chosen IDs ({}); check the ID "
                                         "(DWR structure ID such as 5289, or DWR-5289).".format(", ".join(sorted(only))))
        return {self.OUT: dest}

    def _write_field_map(self, parameters, context):
        """Keep the fields the user set so a later chain run does not re-detect them."""
        out = parameters.get(self.OUT)
        if not isinstance(out, str) or not out:
            return
        folder = os.path.dirname(out)
        if not folder:
            return
        fields = {}
        for tag in ("DWR", "NID"):
            for key, _lab, _kind in FIELD_KEYS:
                val = self.parameterAsString(parameters, "{}_{}".format(tag, key), context)
                if val:
                    fields["{}_{}".format(tag, key)] = val
        if not fields:
            return
        try:
            import json
            with open(os.path.join(folder, "1_fields.json"), "w", encoding="utf-8") as fh:
                json.dump(fields, fh, indent=1, sort_keys=True)
        except OSError:
            pass
