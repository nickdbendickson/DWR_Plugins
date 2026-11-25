"""Quick screen (triage): clear dams whose generous flood envelope reaches nothing."""
import dataclasses
import os
import random

import numpy as np
from qgis.core import (QgsProcessingParameterFeatureSource,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterField, QgsProcessingParameterString,
                       QgsProcessingParameterFeatureSink, QgsProcessingParameterFolderDestination,
                       QgsProcessingParameterBoolean, QgsProcessingParameterEnum,
                       QgsFeature, QgsFeatureRequest, QgsRectangle, QgsFields, QgsField,
                       QgsGeometry, QgsPointXY)

from ..core import triage as tg, hazard as hz, gridutil as gu, runner as rn, nd_counties as ndc
from ..core import road_aadt as RA
from . import _common as cm
from ..core import errors as er
from ..core.inventory import DWR_ALIASES, NID_ALIASES
from ..core.more_layers import layer_name
from ..compat import (FILE_FOLDER, FT_DOUBLE, FT_INT, FT_STRING, GEOM_LINE, NUM_DOUBLE, NUM_INT, SRC_LINE,
                      SRC_POINT, SRC_POLYGON, SRC_SKIP_GEOMETRY_CHECKS, WKB_LINESTRING)
from ._common import UnitAlgorithm

AF_M3 = 1233.4818375
FT_M = 0.3048
def _rated_above_low(text):
    """0.52.0 rating guard: an existing class above Low (High / Significant / Medium, NID H / S; DWR
    says Significant where ND rules say Medium). Unknown wording that names one of them counts too,
    so 'High hazard' cannot slip past; Undetermined / Low / blank do not."""
    t = str(text or "").strip().lower()
    c = hz.nh.class_from_text(t)
    if c is not None:
        return c != hz.nh.LOW
    return any(w in t for w in ("high", "significant", "medium", "intermediate"))
NEW = [("tri_result", FT_STRING), ("tri_reason", FT_STRING),
       ("tri_reach_m", FT_DOUBLE), ("tri_hits", FT_INT),
       ("tri_audit", FT_STRING), ("tri_tif", FT_STRING),
       # 0.52.0 downstream corridor: hit | incomplete | none | off | not run | failed
       ("tri_corr", FT_STRING), ("tri_corr_m", FT_DOUBLE), ("tri_corr_hit", FT_STRING),
       # 0.58.0: a clear that rests on traffic counts matched from other layers (review)
       ("tri_aadt", FT_STRING)]
CORR_TERRAIN = ["Cached clips only (tool 2)", "USGS 3DEP service", "Local DEM folder"]
CORR_TERRAIN_SRC = ("cache", "3dep", "local")          # cm.TerrainLoader sources, in that order
CORR_FIELDS = [("dam_id", FT_STRING), ("part", FT_STRING), ("tri_corr", FT_STRING)]
CORR_ON = {"p": "the flow path", "n": "a stream line", "w": "a waterbody shore"}
BASIN_HALF_M = 3000.0          # closed-basin window around an open end (6 km box)
BASIN_MIN_CELL_M = 10.0        # never finer than the envelope's own grid (a 1 m clip: 36 M cells)
OWN_DAM_M = 150.0              # a High dam this close to the dam or its breach point is the dam itself
MI_M = 1609.344



def _wanted(f, names, only, dam_id=None):
    """0.18.1: ONLY_IDS as tool 1 reads it: the dam id OR the NID id. Crest sends
    the NID id (ND00199) while tool 1 names the dam by its DWR id (DWR-2801), so
    matching dam_id alone dropped the one dam of a one-dam chain here."""
    if not only:
        return True
    did = dam_id if dam_id is not None else (cm.fval(f, names, "dam_id") or f.id())
    ids = [did, cm.fval(f, names, "nid_id")]
    # known-class benchmark (2026-11-23): tool 1 also takes the bare DWR structure ID
    # ('6122' for DWR-6122, its parameter help says so); here it matched nothing, so a chain
    # run with DAM_ID=6122 dropped the dam at step 6, simulated 0 dams and reported OK with no class
    sid = str(did).strip()
    if sid.upper().startswith("DWR-"):
        ids.append(sid[4:])
    return any(str(i).strip().upper() in only for i in ids if i not in (None, ""))

def _road_name(r):
    """The road's name and ND NG9-1-1 SHIELD code (as tool 8's _rname), so a US / State
    highway named only there ('HIGHWAY 23', 'US 2', SHIELD USHY / STHY) counts at least
    Medium under 5.2.1.2(b) instead of being dropped as Low on a sub-500 AADT."""
    try:
        low = {n.lower(): n for n in r.fields().names()}
    except Exception:                                    # noqa  (never fails the tool)
        return ""
    got = ""
    for n in ("name", "fullname", "full_name", "road_name", "rte_name", "route_name"):
        v = r[low[n]] if n in low else None
        if v is not None and str(v) not in ("", "NULL"):
            got = str(v)
            break
    sh = r[low["shield"]] if "shield" in low else None
    if sh is not None and str(sh).strip().upper() in ("USHY", "STHY"):
        got = (got + " " + str(sh).strip()).strip()
    return got


class _NoFlood(Exception):
    """A pond marked no_flood = YES by tool 16."""


class Triage(UnitAlgorithm):
    def name(self): return "triage"
    def displayName(self): return "6 - Quick screen (triage)"
    def group(self): return "Dam screening"
    def groupId(self): return "dams"
    def createInstance(self): return Triage()

    def shortHelpString(self):
        return ("Takes about a second per dam. Builds a deliberately generous flood envelope from "
                "the breach peak: unattenuated peak flow, Manning normal depth in valley cross-"
                "sections along the steepest-descent path (through potholes and, with stream lines, "
                "through road fills), a safety margin, and a reach that stops only when the valley "
                "would hold 'volume factor' x the released volume.\n"
                "tri_result = clear: no habitable structure (tool 8's rule: habitable unless the "
                "occupancy names an uninhabited building), no road that could rate Medium or High "
                "(only a known AADT < 500, not interstate, not a US / State highway, not sole access is left out; an unknown "
                "AADT counts; 0.58.0: a road without AADT first takes the count of the traffic line it runs "
                "along or the count station on it, as tool 8), no railroad, no lifeline facility and no other "
                "dam of the layer lies "
                "inside the envelope, so the dam screens LOW without the 2D model. needs 2D: something is inside, the envelope reached the terrain edge, or the "
                "dam is above the size limit. Tool 7 skips cleared dams.\n"
                "A dam DWR rates above Low (haz_existing High, Significant or Medium; NID H / S) is never cleared.\n"
                "DOWNSTREAM CORRIDOR (0.52.0; the envelope's reach is a cheap estimate that can stop "
                "kilometres short of a real breach flood): a dam the envelope would clear is still sent to "
                "2D when anything that could make it High - a habitable structure, a lifeline facility, a "
                "High-rated dam, a road tool 8 would rate High or a US / State highway without AADT, a "
                "passenger or busy railroad - lies within the corridor width (default 1,640 ft = 500 m) "
                "of its flow path, of the stream lines below it (every branch; one that leaves the "
                "distance is followed 1.5 km further, so a reach that comes back within it is checked) "
                "or of the shore of any lake or river polygon "
                "they touch, out to the corridor distance (default 9.3 mi = 15 km) in a straight line "
                "from the breach toe. Roads, railroads and structures are read with the field choices "
                "tool 8 uses (names matched ignoring case). It is also sent to 2D whenever that corridor "
                "cannot be completed: no stream lines or waterbodies given, no structures layer, an "
                "all-dams layer with no hazard class field, the flow path leaves the terrain clip with "
                "no stream line near its end or ends 'stuck', the streams end before the distance with "
                "no outlet and no closed basin that holds the volume factor x the release (proven on "
                "the cached clip, 3DEP or a local DEM), the channel winds more than 5 x the distance, "
                "more than 25 x the distance of stream lines, the corridor comes within 300 m of the "
                "state line (no receptor data beyond), or any error. Lifeline facilities count although "
                "tool 8 rates them Medium. The corridor can never clear a dam the envelope sends to 2D; "
                "switched off, the results are those of the envelope and the rating guard "
                "(tri_corr = off). tri_corr: hit | incomplete | none | not run | failed; tri_corr_hit "
                "says what and where. Expect most unrated dams in settled valleys to go to 2D: in "
                "testing, 0 of 46 dams the envelope cleared still cleared.\n"
                "TRAFFIC COUNTS (0.58.0): roads without AADT (the pack's NG9-1-1 roads) take the count of the "
                "HPMS / county road line they run along or the NDDOT count station on them (AADT_LAYERS, else the "
                "files next to the roads file, plus ROADS_MORE), in the envelope where the water crosses them "
                "and in the corridor where they lie within its width; one that matches nothing keeps the "
                "'unknown AADT' rule. tri_aadt marks a clear that rests on such a count (to review).\n"
                "AUDIT: a random share of cleared dams is still sent to tool 7 (tri_audit = yes); "
                "tool 8 reports whether the 2D model agreed. Screening shortcut only.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFeatureSource("DAMS", "Dams with breach hydrographs (tool 5)",
                                              [SRC_POINT]))
        P(QgsProcessingParameterFeatureSource("BPTS", "Breach points (tool 4)", [SRC_POINT]))
        P(QgsProcessingParameterFile("DEMDIR", "Folder of cached dam DEMs (tool 2)",
                                     behavior=FILE_FOLDER))
        P(QgsProcessingParameterFeatureSource("BLD", "Structures", [SRC_POINT,
                                              SRC_POLYGON], optional=True))
        P(QgsProcessingParameterField("OCC", "Structures: occupancy class field (as tool 8; a tool-0 "
                                      "'habitable' field wins)", defaultValue="OCC_CLS",
                                      parentLayerParameterName="BLD", optional=True))
        P(QgsProcessingParameterField("PRIM", "Structures: primary occupancy field (as tool 8)",
                                      defaultValue="PRIM_OCC", parentLayerParameterName="BLD", optional=True))
        P(QgsProcessingParameterString("UNINHABITED", "Structures: occupancy words that mark an UNINHABITED "
                                       "building (as tool 8)", defaultValue=", ".join(hz.UNINHABITED)))
        P(QgsProcessingParameterFeatureSource("ROADS", "Roads", [SRC_LINE], optional=True))
        P(QgsProcessingParameterField("ROAD_AADT", "Roads: AADT field", defaultValue="aadt",
                                      parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterField("ROAD_CLASS", "Roads: class field", defaultValue="road_class",
                                      parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterField("R_SOLE", "Roads: sole access to an occupied location field (yes/no)",
                                      defaultValue="sole_access", parentLayerParameterName="ROADS", optional=True))
        P(QgsProcessingParameterFeatureSource("RAIL", "Railroads", [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("LIFELINES", "Lifeline facilities (points or polygons)",
                                              [SRC_POINT, SRC_POLYGON],
                                              optional=True))
        P(QgsProcessingParameterString(
            "ROADS_MORE", "More road GeoPackages, separated by ;; (county roads and any other individual file)",
            defaultValue="", optional=True))
        P(QgsProcessingParameterString(
            "LIFE_MORE", "More lifeline GeoPackages, separated by ;; (nursing, care, assisted living)",
            defaultValue="", optional=True))
        P(QgsProcessingParameterString(
            "BLD_MORE", "More structure GeoPackages, separated by ;; (address points, USA Structures)",
            defaultValue="", optional=True))
        # 0.58.0: traffic counts for roads without one, as tool 8 (defaults: an older saved run still runs)
        P(QgsProcessingParameterBoolean("AADT_JOIN", cm.AADT_JOIN_LABEL, defaultValue=True))
        P(QgsProcessingParameterString("AADT_LAYERS", cm.AADT_LAYERS_LABEL, defaultValue="", optional=True))
        P(QgsProcessingParameterFeatureSource("STREAMS", "Stream lines for road-crossing cuts (optional)",
                                              [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("DAMLINES", "DWR dam lines (protected from cuts, optional)",
                                              [SRC_LINE], optional=True))
        P(QgsProcessingParameterFeatureSource("PROTECT", "More fills to leave uncut (points, lines or polygons; e.g. the Finder's screening_dams)",
                                              [SRC_POINT, SRC_LINE, SRC_POLYGON], optional=True))
        # 0.52.0 downstream corridor (all with defaults: a saved run of an older version still runs)
        P(QgsProcessingParameterBoolean(
            "CORRIDOR", "Downstream corridor check: send to 2D any dam with a house, busy road, passenger "
            "railroad, care facility or High-rated dam along its flow path beyond the envelope (recommended)",
            defaultValue=True))
        P(QgsProcessingParameterNumber("CORR_D_FT", "Corridor: straight-line distance from the dam (ft; 49,213 ft "
                                       "= 15 km = 9.3 mi)", NUM_DOUBLE, 49212.6, minValue=3280.84,
                                       maxValue=164041.99))
        P(QgsProcessingParameterNumber("CORR_W_FT", "Corridor: width each side of the flow path, streams and "
                                       "waterbody shores (ft; 1,640 ft = 500 m; less is not validated)",
                                       NUM_DOUBLE, 1640.42, minValue=0.0))
        P(QgsProcessingParameterFeatureSource("WATERBODIES", "Waterbodies (3DHP / NHD lakes and river polygons; the "
                                              "corridor adds the shore of every one its streams touch)",
                                              [SRC_POLYGON], optional=True))
        P(QgsProcessingParameterFeatureSource("CORR_DAMS", "All dams with their hazard class, for the cascade rule "
                                              "(e.g. the DWR dams)", [SRC_POINT], optional=True))
        P(QgsProcessingParameterField("CORR_DAMS_CLS", "All dams: hazard class field", defaultValue="hazard_class",
                                      parentLayerParameterName="CORR_DAMS", optional=True))
        P(QgsProcessingParameterField("CORR_DAMS_ID", "All dams: structure id field (the dam itself is left out)",
                                      defaultValue="structure_id", parentLayerParameterName="CORR_DAMS",
                                      optional=True))
        P(QgsProcessingParameterEnum("UNKNOWN_AADT", "Corridor: class of a road with no AADT (as tool 8; High makes "
                                     "every road without an AADT a corridor receptor)", list(hz.nh.CLASSES),
                                     defaultValue=hz.nh.CLASSES.index(hz.nh.MEDIUM)))
        P(QgsProcessingParameterEnum("UNKNOWN_TRAINS", "Corridor: class of a railroad with no trains a day (as tool 8)",
                                     list(hz.nh.CLASSES), defaultValue=hz.nh.CLASSES.index(hz.nh.MEDIUM)))
        # as tool 8's, so the corridor rates a railroad as tool 8 would (a train count under another name read
        # as 'unknown': Medium, not a receptor)
        P(QgsProcessingParameterField("RAIL_TRAINS", "Corridor: railroads: trains per day field (as tool 8)",
                                      defaultValue="trains_day", parentLayerParameterName="RAIL", optional=True))
        P(QgsProcessingParameterField("RAIL_PAX", "Corridor: railroads: passenger field (as tool 8; FRA PASSNGR or "
                                      "yes/no)", defaultValue="PASSNGR", parentLayerParameterName="RAIL",
                                      optional=True))
        P(QgsProcessingParameterString("PAXCODES", "Corridor: passenger codes counted as an active passenger line "
                                       "(as tool 8)", defaultValue=", ".join(hz.PAX_CODES), optional=True))
        P(QgsProcessingParameterEnum("CORR_TERRAIN", "Corridor: terrain to prove a closed basin where the streams "
                                     "end (only needed when nothing else sends the dam to 2D)", CORR_TERRAIN,
                                     defaultValue=0))
        P(QgsProcessingParameterFile("CORR_LOCAL", "Corridor: local DEM folder (if selected above)",
                                     behavior=FILE_FOLDER, optional=True))
        P(QgsProcessingParameterString(
            "ONLY_IDS", "Only these dams (comma-separated IDs; empty = every dam in the layer)",
            defaultValue="", optional=True))
        P(QgsProcessingParameterNumber("N", "Manning n for the envelope", NUM_DOUBLE,
                                       0.04, minValue=0.01, maxValue=0.3))
        P(QgsProcessingParameterNumber("MARGIN_FT", "Stage safety margin: at least (ft; the starting value 0.5 "
                                       "ft is a screening value YOU choose)",
                                       NUM_DOUBLE, 0.5, minValue=0.0))
        P(cm.hide(QgsProcessingParameterNumber("MARGIN", "(before 0.11) Stage safety margin: at least (m)",
                                               NUM_DOUBLE, 0.15, minValue=0.0,
                                               optional=True)))
        P(QgsProcessingParameterNumber("MFRAC", "Stage safety margin: share of flow depth",
                                       NUM_DOUBLE, 0.3, minValue=0.0, maxValue=2.0))
        P(QgsProcessingParameterNumber("VF", "Volume factor", NUM_DOUBLE, 2.0,
                                       minValue=1.0, maxValue=10.0))
        P(QgsProcessingParameterNumber("BIG", "Always send to 2D above this storage (ac-ft), 0 = never",
                                       NUM_DOUBLE, 1000.0, minValue=0.0))
        P(QgsProcessingParameterNumber("AUDIT", "Audit share of cleared dams sent to 2D anyway (%)",
                                       NUM_DOUBLE, 5.0, minValue=0.0, maxValue=100.0))
        P(QgsProcessingParameterNumber("SEED", "Random seed for the audit sample",
                                       NUM_INT, 1))
        P(QgsProcessingParameterFolderDestination("OUTDIR", "Folder for envelope rasters"))
        P(QgsProcessingParameterFeatureSink("OUT", "Dams with triage result", SRC_POINT))
        P(QgsProcessingParameterFeatureSink("OUT_CORR", "Downstream corridors (lines)", SRC_LINE,
                                            optional=True, createByDefault=False))

    def processAlgorithm(self, params, context, feedback):
        dams = self.parameterAsSource(params, "DAMS", context)
        bpts = self.parameterAsSource(params, "BPTS", context)
        demdir = self.parameterAsString(params, "DEMDIR", context)
        bld = self.parameterAsSource(params, "BLD", context)
        occ = self.parameterAsString(params, "OCC", context) or None
        prim = self.parameterAsString(params, "PRIM", context) or None
        uninhabited = hz.words(self.parameterAsString(params, "UNINHABITED", context)) or hz.UNINHABITED
        f_sole = self.parameterAsString(params, "R_SOLE", context) or None
        life = self.parameterAsSource(params, "LIFELINES", context)
        roads = self.parameterAsSource(params, "ROADS", context)
        f_aadt = self.parameterAsString(params, "ROAD_AADT", context) or None
        f_rcls = self.parameterAsString(params, "ROAD_CLASS", context) or None
        rail = self.parameterAsSource(params, "RAIL", context)
        streams = self.parameterAsSource(params, "STREAMS", context)
        damlines = self.parameterAsSource(params, "DAMLINES", context)
        keep = self.parameterAsSource(params, "PROTECT", context)
        n = self.parameterAsDouble(params, "N", context)
        margin, margin_ft, _src = cm.feet_or_legacy(self, params, context, "MARGIN_FT", "MARGIN", 0.5)
        feedback.pushInfo("Stage safety margin at least {:.2f} ft ({:.3f} m)".format(margin_ft, margin))
        mfrac = self.parameterAsDouble(params, "MFRAC", context)
        vf = self.parameterAsDouble(params, "VF", context)
        big = self.parameterAsDouble(params, "BIG", context)
        audit = self.parameterAsDouble(params, "AUDIT", context) / 100.0
        rnd = random.Random(self.parameterAsInt(params, "SEED", context))
        outdir = self.parameterAsString(params, "OUTDIR", context)
        os.makedirs(outdir, exist_ok=True)
        loader = cm.TerrainLoader(demdir, "cache")
        corr_on = self.parameterAsBool(params, "CORRIDOR", context)
        corr_d = self.parameterAsDouble(params, "CORR_D_FT", context) * FT_M
        corr_w = self.parameterAsDouble(params, "CORR_W_FT", context) * FT_M
        wbs = self.parameterAsSource(params, "WATERBODIES", context)
        cdams = self.parameterAsSource(params, "CORR_DAMS", context)
        st8 = hz.nh.Settings(unknown_aadt=hz.nh.CLASSES[self.parameterAsEnum(params, "UNKNOWN_AADT", context)],
                             unknown_trains=hz.nh.CLASSES[self.parameterAsEnum(params, "UNKNOWN_TRAINS", context)])
        corr_src = CORR_TERRAIN_SRC[self.parameterAsEnum(params, "CORR_TERRAIN", context)]
        corr_local = self.parameterAsString(params, "CORR_LOCAL", context) or None
        cfields = QgsFields()
        for nm, typ in CORR_FIELDS:
            cfields.append(QgsField(nm, typ))
        csink, cdest = self.parameterAsSink(params, "OUT_CORR", context, cfields, WKB_LINESTRING, cm.albers())

        bnames = bld.fields().names() if bld else []
        occ = occ if occ in bnames else None
        prim = prim if prim in bnames else None
        f_hab = "habitable" if "habitable" in bnames else None
        rnames = roads.fields().names() if roads else []
        f_aadt = f_aadt if f_aadt in rnames else None
        f_rcls = f_rcls if f_rcls in rnames else None
        f_sole = f_sole if f_sole in rnames else None

        b2a = cm.to_albers(bpts)
        bmap = {}
        for b in bpts.getFeatures():
            g = b.geometry(); g.transform(b2a); p = cm.rep_point(g)
            if p is not None and b["flow_az"] not in (None, ""):
                bmap[str(b["dam_id"])] = (p, float(b["flow_az"]))
        names = dams.fields().names()
        only = {x.strip().upper() for x in (self.parameterAsString(params, "ONLY_IDS", context) or "").split(",")
                if x.strip()}
        # every dam of the layer: another dam inside the envelope keeps the dam from 'clear' (5.3)
        d2a = cm.to_albers(dams)
        dam_pts = []
        for f in dams.getFeatures():
            if not _wanted(f, names, only):
                continue
            g = f.geometry()
            if g is not None and not g.isEmpty():
                g.transform(d2a); p = cm.rep_point(g)
                if p is not None:
                    dam_pts.append((str(cm.fval(f, names, "dam_id") or f.id()), p))
        get = lambda f, k, c=None: cm.fval(f, names, k, c)
        fields, put = cm.extend_fields(dams.fields(), NEW)
        sink, dest = self.parameterAsSink(params, "OUT", context, fields, dams.wkbType(), dams.sourceCrs())
        total = dams.featureCount() or 1
        stats = {"clear": 0, "needs 2D": 0, "audit": 0}

        def habitable(feat, occ_f=occ, prim_f=prim, hab_f=f_hab):
            return hz.is_habitable(feat[occ_f] if occ_f else None, feat[prim_f] if prim_f else None,
                                   flag=feat[hab_f] if hab_f else None, uninhabited=uninhabited)

        def _names(src):
            try:
                return list(src.fields().names())
            except Exception:
                return []

        def _open(text, label):
            from ..core.more_layers import open_uris
            layers, bad = open_uris(text)
            for lyr in layers:
                feedback.pushInfo("Extra {}: {}".format(label, lyr.source()))
            for uri in bad:
                feedback.reportError("Could not open extra {} file: {}".format(label, uri))
            return layers

        from ..core.more_layers import pick
        S = lambda k: self.parameterAsString(params, k, context) or ""

        def _cols(src, *cands):
            return pick(_names(src), *cands)

        bld_extra = []
        for lyr in _open(S("BLD_MORE"), "structures"):
            bld_extra.append((lyr, _cols(lyr, occ or "", "OCC_CLS", "occtype", "occ_cls"),
                              _cols(lyr, prim or "", "PRIM_OCC", "prim_occ"),
                              next((n for n in _names(lyr) if str(n).lower() == "habitable"), None)))
        life_extra = _open(S("LIFE_MORE"), "lifelines")
        road_extra = []
        for lyr in _open(S("ROADS_MORE"), "roads"):
            nms = _names(lyr)
            road_extra.append((lyr, nms,
                               _cols(lyr, f_aadt or "", "aadt", "AADT"),
                               _cols(lyr, f_rcls or "", "road_class", "RoadClass",
                                     "FUNCTIONAL_CLASS", "functional_class"),
                               _cols(lyr, f_sole or "", "sole_access")))

        # 0.58.0: traffic counts for roads without AADT (core/road_aadt.py, the Runner's aadt_join), read by area
        join = None
        if self.parameterAsBool(params, "AADT_JOIN", context) and (roads is not None or road_extra):
            apaths = RA.sources(S("AADT_LAYERS"), cm.layer_path(params, "ROADS"), S("ROADS_MORE"))
            if apaths:
                join = cm.aadt_join(apaths)
                feedback.pushInfo("Traffic counts for roads without AADT: {}".format(
                    ", ".join(os.path.basename(p) for p in apaths)))
        rested, said = [], set()                               # per dam: roads rated lower only on a matched count

        def match(parts, is_wet):
            """join.match, the unread files reported once."""
            m = join.match(parts, is_wet=is_wet) if join and parts else None
            for e in (join.errors if join else ()):
                if e not in said:
                    said.add(e)
                    feedback.reportError("Traffic counts: {} was not read (left out)".format(e))
            return m

        def _with_count(rd, m):
            return dataclasses.replace(rd, aadt=hz.nh.aadt_value(m["aadt"]))

        def points_of(kind, parts):
            """Points tested against the envelope: the points; a polygon's vertex mean and vertices;
            a line's vertices (lines are densified before)."""
            if kind == "point":
                return parts
            if kind == "polygon":
                return [(sum(x for x, _ in r) / len(r), sum(y for _, y in r) / len(r)) for r in parts] + \
                    [p for r in parts for p in r]
            return [p for ln in parts for p in ln]

        # ---------------- 0.52.0 downstream corridor: inputs, receptors, the check itself
        # the cascade layer's class / id fields: the chosen name, else any name tool 1 reads in a DWR or NID layer
        cdn = _names(cdams)
        cd_hz = tuple(dict.fromkeys(n for n in (S("CORR_DAMS_CLS"),) + DWR_ALIASES["HZ"] + NID_ALIASES["HZ"] +
                                    ("haz_existing",) if n))
        cd_cls = pick(cdn, *cd_hz)
        cd_id = pick(cdn, S("CORR_DAMS_ID"), *(DWR_ALIASES["ID"] + NID_ALIASES["ID"]))
        corr_missing = None
        if corr_on and (streams is None or wbs is None):
            corr_missing = ("downstream corridor check needs the stream lines (STREAMS) and the waterbodies "
                            "(WATERBODIES)")
        elif corr_on and bld is None and not bld_extra:
            corr_missing = "downstream corridor check needs a structures layer (BLD or BLD_MORE)"
        elif corr_on and cdams is not None and cd_cls is None:
            # its High dams cannot be told apart: dropping the layer could clear a dam above a High one
            corr_missing = ("downstream corridor check cannot read the hazard class of the all-dams layer "
                            "(CORR_DAMS has none of the fields {})".format(", ".join(cd_hz)))
        if corr_missing:
            feedback.pushWarning("The {}: every dam the envelope would clear is sent to 2D.".format(corr_missing))

        def _lname(src):
            try:
                return str(src.sourceName())                   # a Processing feature source
            except Exception:                                  # noqa  (a layer, or a test double)
                return layer_name(src)

        def _text(feat, *flds):
            for k in flds:
                v = feat[k] if k else None
                if v is not None and str(v).strip() not in ("", "NULL"):
                    return str(v).strip()
            return ""

        def _sid(v):
            if isinstance(v, float) and v.is_integer():
                v = int(v)                                     # 2636.0 -> "2636" (DWR-2636)
            s = "" if v is None else str(v).strip()
            return "" if s == "NULL" else s

        cur = {"id": None, "own": []}                          # the dam being checked (cascade rule)

        def _not_self(sid, ta, feat):
            if sid and (sid == cur["id"] or "DWR-" + sid == cur["id"]):
                return False
            g = feat.geometry()
            if g is None or g.isEmpty():
                return False
            g.transform(ta)
            p = cm.rep_point(g)
            return p is not None and all(np.hypot(p[0] - q[0], p[1] - q[1]) > OWN_DAM_M for q in cur["own"])

        def _high(rd, r, ctext, src=""):
            # tool 8 rates it High, or a US / State highway with no AADT (it may carry > 2,000)
            if not (hz.nh.road_class(rd, st8)[0] == hz.nh.HIGH or (rd.us_state and rd.aadt is None)):
                return False, ""
            why = ("interstate" if rd.interstate else "AADT {:,.0f}{}".format(rd.aadt, src) if rd.aadt is not None
                   else "US/State highway, AADT unknown" if rd.us_state else "AADT unknown, assumed High")
            return True, "{} ({})".format(_road_name(r) or ctext or "road", why)

        def _road_high(r, nms, faadt, frcls, fsole):
            ctext = str(r[frcls]) if frcls and r[frcls] else ""
            rd = hz.nd_road("r", 1.0, cm.fval(r, nms, faadt, float), ctext,
                            sole_access=hz.nh.yes_no(r[fsole]) is True if fsole else False, name=_road_name(r))
            if join and rd.aadt is None and not rd.interstate:
                # 0.58.0: its rating rests on a traffic count: decided once the road is known to lie in the
                # corridor, with the count of its part there
                return None, (lambda feat, ta, near: _road_high_joined(feat, ta, near, rd, ctext))
            return _high(rd, r, ctext)

        def _road_high_joined(r, ta, near, rd, ctext):
            kind, parts = cm.albers_parts(r, ta)
            m = match(parts, near) if kind == "line" else None
            if not m:
                return _high(rd, r, ctext)
            ok, label = _high(_with_count(rd, m), r, ctext, " from " + RA.source_label(m["source"]))
            if not ok and _high(rd, r, ctext)[0]:
                rested.append("road {} in the corridor: {} (not High-capable; without it: {})".format(
                    _road_name(r) or ctext or r.id(), m["text"], _high(rd, r, ctext)[1]))
            return ok, label

        rlnames = _names(rail)
        pax_f = pick(rlnames, S("RAIL_PAX"), "PASSNGR", "passenger")
        trn_f = pick(rlnames, S("RAIL_TRAINS"), "trains_day", "trains_per_day", "TRAINS")
        paxcodes = hz.words(S("PAXCODES")) or hz.PAX_CODES

        def _rail_high(r):
            rr = hz.nd_rail("r", 1.0, cm.fval(r, rlnames, trn_f, float),
                            hz.rail_passenger(r[pax_f], paxcodes) if pax_f else False)
            if hz.nh.rail_class(rr, st8)[0] != hz.nh.HIGH:
                return False, ""
            if rr.passenger:
                return True, "passenger railroad"
            if rr.trains_per_day is not None:
                return True, "railroad, {:g} trains a day".format(rr.trains_per_day)
            return True, "railroad, trains a day unknown (assumed High)"

        # High-capable receptor sources, built once: (source, classify(feature) -> (bool, label)). The primary
        # layers' fields are matched ignoring case, as tool 8's pfield does: the envelope's exact match (kept, so
        # CORRIDOR = False is unchanged) read a road's 'AADT' 8,500 as unknown - Medium, no receptor - where
        # tool 8 rates it High
        c_occ, c_prim, c_hab = pick(bnames, S("OCC")), pick(bnames, S("PRIM")), pick(bnames, "habitable")
        c_aadt, c_rcls, c_sole = (pick(rnames, S(k)) for k in ("ROAD_AADT", "ROAD_CLASS", "R_SOLE"))
        rsrc = []
        if bld is not None:
            occ_lab = pick(bnames, "occtype", "OCC_CLS", "occ_cls")       # the label only (NSI's occtype)
            rsrc.append((bld, lambda b: (habitable(b, c_occ, c_prim, c_hab),
                                         "structure " + (_text(b, c_occ, c_prim, occ_lab) or _lname(bld)))))
        for lyr, occ_f, prim_f, hab_f in bld_extra:
            rsrc.append((lyr, lambda b, o=occ_f, p=prim_f, h=hab_f, ly=lyr:
                         (habitable(b, o, p, h), "structure " + (_text(b, o, p) or _lname(ly)))))
        for lyr in ([life] if life is not None else []) + life_extra:
            rsrc.append((lyr, lambda b, ly=lyr: (True, "lifeline facility ({})".format(_lname(ly)))))
        if roads is not None:
            rsrc.append((roads, lambda r: _road_high(r, rnames, c_aadt, c_rcls, c_sole)))
        for lyr, nms, faadt, frcls, fsole in road_extra:
            rsrc.append((lyr, lambda r, a=(nms, faadt, frcls, fsole): _road_high(r, *a)))
        if rail is not None:
            rsrc.append((rail, _rail_high))
        # cascade: every dam of DAMS (ONLY_IDS ignored) DWR rates High, and every High one of CORR_DAMS
        rsrc.append((dams, lambda d: (hz.nh.class_from_text(get(d, "haz_existing")) == hz.nh.HIGH and
                                      _not_self(_sid(get(d, "dam_id")), d2a, d),
                                      "dam {} (High)".format(_sid(get(d, "dam_id")) or d.id()))))
        if cdams is not None and cd_cls is not None:            # (no class field: corr_missing, every dam to 2D)
            c2a = cm.to_albers(cdams)
            rsrc.append((cdams, lambda d: (hz.nh.class_from_text(d[cd_cls]) == hz.nh.HIGH and
                                           _not_self(_sid(d[cd_id]) if cd_id else "", c2a, d),
                                           "dam {} (High)".format((_sid(d[cd_id]) if cd_id else "") or d.id()))))
        rsrc = [(s, cm.to_albers(s), cm.from_albers(s), fn) for s, fn in rsrc]

        def test_points(feat, ta):
            """points_of() in EPSG:5070; a line densified to 10 m first."""
            g = feat.geometry()
            if g is None or g.isEmpty():
                return None
            g.transform(ta)
            if g.type() == GEOM_LINE:
                g = g.densifyByDistance(10.0)
            kind, parts = cm.geom_parts(g)
            pts = points_of(kind, parts) if kind else []
            return np.asarray(pts, float).reshape(-1, 2) if pts else None

        snames = _names(streams)
        fd_f = pick(snames, "flowdirection", "flowdir")
        s2a, a2s = (cm.to_albers(streams), cm.from_albers(streams)) if streams is not None else (None, None)

        def stream_lines(xy, radius):
            """cm.lines_near plus, per part, walk-both-ways (3DHP flowdirection 0 = unknown)."""
            lines, both = [], []
            box = a2s.transformBoundingBox(QgsRectangle(xy[0] - radius, xy[1] - radius,
                                                        xy[0] + radius, xy[1] + radius))
            for s in streams.getFeatures(QgsFeatureRequest().setFilterRect(box)):
                kind, parts = cm.albers_parts(s, s2a)
                if kind != "line":
                    continue
                try:
                    bw = fd_f is not None and int(float(s[fd_f])) == 0
                except (TypeError, ValueError):
                    bw = False
                lines += [np.asarray(p, float) for p in parts]
                both += [bw] * len(parts)
            return lines, both

        w2a, a2w = (cm.to_albers(wbs), cm.from_albers(wbs)) if wbs is not None else (None, None)

        def wb_feats(req):
            """wbs.getFeatures without the context's invalid-geometry check: corridor_axis asks once per 300 axis
            points and round, and in a toolbox run each call ran GEOS isValid on every polygon returned (Lake
            Sakakawea's 91,000 vertices again and again: 25 s instead of 0.6 s for one dam)."""
            if SRC_SKIP_GEOMETRY_CHECKS is not None:
                try:
                    return wbs.getFeatures(req, SRC_SKIP_GEOMETRY_CHECKS)
                except TypeError:                              # a plain layer: no flags argument
                    pass
            return wbs.getFeatures(req)

        def wb_rings():
            """callable(bbox) -> [((fid, part), closed exterior ring)], each polygon converted once per dam."""
            cache = {}

            def get(bbox):
                out = []
                for w in wb_feats(QgsFeatureRequest().setFilterRect(a2w.transformBoundingBox(QgsRectangle(*bbox)))):
                    if w.id() not in cache:
                        kind, parts = cm.albers_parts(w, w2a)
                        cache[w.id()] = [np.asarray(list(r) + ([r[0]] if tuple(r[0]) != tuple(r[-1]) else []), float)
                                         for r in parts] if kind == "polygon" else []
                    out += [((w.id(), k), r) for k, r in enumerate(cache[w.id()])]
                return out
            return get

        terr = {"loader": None, "fails": 0, "off": False}

        def basin_at(dam_id, xy, cell, need_m3):
            """closed_basin() on terrain around xy (conditioned as the clip), or None (no terrain)."""
            if terr["off"]:
                return None
            try:
                if terr["loader"] is None:
                    terr["loader"] = cm.TerrainLoader(demdir, corr_src, corr_local, tmpdir=outdir)
                st = [x for _, arrs in cm.lines_near(streams, xy, BASIN_HALF_M) for x in arrs] if streams else []
                pr = [x for _, arrs in cm.lines_near(damlines, xy, BASIN_HALF_M) for x in arrs] if damlines else []
                pr += cm.protect_near(keep, xy, BASIN_HALF_M)
                win, wgt = terr["loader"].load(dam_id, xy, BASIN_HALF_M, cell, streams=st, protect_lines=pr)
            except Exception as e:                             # noqa  (not proven: needs 2D)
                terr["fails"] += 1
                feedback.pushInfo("Corridor terrain for dam {}: {}".format(dam_id, er.plain(e, dam_id, "6")))
                if corr_src != "cache" and terr["fails"] >= 3:
                    terr["off"] = True                         # a dead service would cost minutes per dam
                    feedback.reportError("Corridor: the {} terrain failed 3 times in a row; none is fetched for "
                                         "the rest of this run (open stream ends stay unproven: needs 2D)"
                                         .format(CORR_TERRAIN[CORR_TERRAIN_SRC.index(corr_src)]))
                return None
            terr["fails"] = 0
            rc = gu.xy_to_rc(wgt, xy[0], xy[1])
            if not gu.inside(win.shape, *rc):
                return None
            return tg.closed_basin(win, rc, need_m3, abs(wgt[1]))

        corr_stats = {}
        outline = []                                           # the ND outline, read when first needed

        def _corridor(dam_id, f, bxy, env, gt, rel, why):
            pxy = np.array([gu.rc_to_xy(gt, r, c) for r, c in env["path"]], float).reshape(-1, 2)
            toe = pxy[0] if len(pxy) else np.asarray(bxy, float)
            g = f.geometry(); g.transform(d2a)
            cur["id"], cur["own"] = dam_id, [p for p in (cm.rep_point(g), bxy) if p is not None]
            net = tg.StreamNet(*stream_lines(toe, corr_d + tg.CORRIDOR_DEFAULTS["beyond_m"]))
            cor = tg.corridor_axis(pxy, env["trace_end"], net, waterbodies=wb_rings(), d_max=corr_d)
            axis, line_m, src = cor["axis"], cor["line_m"], cor["src"]
            O = tg.CORRIDOR_DEFAULTS
            if not outline:
                outline.append(ndc.state_outline_5070())
            # the structure, road and rail layers stop at the state line; a toe far outside ND
            # (a test world, an out-of-state project) skips the rule
            if len(axis) and tg.state_margin(toe[None, :], outline[0])[0] > -O["outside_skip_m"] and \
                    tg.state_margin(np.vstack([axis[::10], axis[-1:]]), outline[0]).min() < O["state_m"]:
                cor["flags"].append("the corridor comes within {:.0f} m of the state line (no receptor data "
                                    "beyond)".format(O["state_m"]))
            hit = None                                         # (label, off m, src, line m)
            if len(axis):
                idx = tg.PointIndex(axis, max(corr_w, 100.0))
                seen = set()

                def near(x, y):
                    return bool(np.isfinite(idx.nearest(np.array([[x, y]]), corr_w + 5.0)[0][0]))
                for b0 in np.arange(0.0, float(line_m.max()) + 1000.0, 1000.0):   # nearest 1 km band first
                    m = (line_m >= b0) & (line_m < b0 + 1000.0)
                    if not m.any():
                        continue
                    lo, hi = axis[m].min(0) - corr_w - 20.0, axis[m].max(0) + corr_w + 20.0
                    rect = QgsRectangle(lo[0], lo[1], hi[0], hi[1])
                    for k, (s, ta, fa, classify) in enumerate(rsrc):
                        for feat in s.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(rect))):
                            if (k, feat.id()) in seen:
                                continue
                            seen.add((k, feat.id()))
                            ok, label = classify(feat)
                            if not ok and not callable(label):
                                continue
                            pts = test_points(feat, ta)
                            if pts is None:
                                continue
                            d, j = idx.nearest(pts, corr_w + 5.0)
                            if not np.isfinite(d).any():
                                continue
                            if callable(label):                # 0.58.0: a road rated on a matched count
                                ok, label = label(feat, ta, near)
                                if not ok:
                                    continue
                            q = int(np.argmin(d)); jj = int(j[q])
                            if hit is None or line_m[jj] < hit[3]:
                                hit = (label, float(d[q]), str(src[jj]), float(line_m[jj]))
                    if hit is not None:
                        break
            if hit is not None:
                txt = "{} {:.0f} m from {}, {:.1f} km from the dam".format(hit[0], hit[1], CORR_ON.get(hit[2], "it"),
                                                                           hit[3] / 1000.0)
                return "needs 2D", "downstream corridor: {}; envelope alone: {}".format(txt, why), "hit", txt, cor
            if cor["flags"]:
                txt = cor["flags"][0]
                return ("needs 2D", "downstream corridor incomplete: {}; envelope alone: {}".format(txt, why),
                        "incomplete", txt, cor)
            for e_xy, e_r, kind in cor["open_ends"]:           # where the streams end: a closed basin?
                b = basin_at(dam_id, e_xy, max(abs(gt[1]), BASIN_MIN_CELL_M), vf * rel)
                if b is None or not b["holds"]:
                    txt = "the {} ends {:.1f} km from the dam with no outlet ({})".format(
                        "flow path" if kind == "path" else "stream network", e_r / 1000.0,
                        b["why"] if b else "no terrain there")
                    return ("needs 2D", "downstream corridor incomplete: {}; envelope alone: {}".format(txt, why),
                            "incomplete", txt, cor)
            return ("clear", "{} and nothing High-capable within {:,.0f} ft of the flow path, streams and waterbody "
                             "shores to {:.1f} mi ({:.1f} km) from the dam".format(why, corr_w / FT_M, corr_d / MI_M,
                                                                                  corr_d / 1000.0),
                    "none", None, cor)

        def corridor_check(dam_id, f, bxy, env, gt, rel, why):
            """(result, reason, tri_corr, reach m, hit / incomplete text) for a dam the envelope would
            clear. Any error: needs 2D - the check never clears on an error."""
            try:
                result, reason, tc, txt, cor = _corridor(dam_id, f, bxy, env, gt, rel, why)
            except Exception as e:                             # noqa  (fail safe)
                msg = er.plain(e, dam_id, "6")
                return ("needs 2D", "downstream corridor check failed ({}); envelope alone: {}".format(msg, why),
                        "failed", None, msg)
            if csink is not None:
                try:
                    for xy, k in cor["parts"]:
                        o = QgsFeature(cfields)
                        o.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(float(x), float(y)) for x, y in xy]))
                        o.setAttributes([dam_id, tg.PART_NAMES.get(k, k), tc])
                        csink.addFeature(o)
                except Exception as e:                         # noqa  (a display layer; the verdict stands)
                    feedback.reportError("Corridor lines of dam {} not written: {}".format(dam_id, e))
            return result, reason, tc, cor["stats"].get("reach_m"), txt

        for i, f in enumerate(dams.getFeatures()):
            if feedback.isCanceled(): break
            feedback.setProgress(100.0 * i / total)
            dam_id = str(get(f, "dam_id") or f.id())
            if not _wanted(f, names, only, dam_id):
                continue
            a = cm.blank_attrs(fields, f)
            result, reason, reach, hits, tif = "needs 2D", "", None, 0, None
            del rested[:]
            tri_corr, corr_m, corr_hit = ("not run" if corr_on else "off"), None, None
            nf, nf_why = cm.no_flood(f, dams.fields().names())
            ex, ex_why = cm.excluded(f, dams.fields().names())
            flag_e, flag_why = cm.flagged(f, dams.fields().names())
            try:
                if flag_e:                      # 0.17.26: a flagged structure -> cleared, with the note
                    raise _NoFlood(flag_why)
                if ex:                      # 0.13: excluded by the reviewer -> cleared, with the reason
                    raise _NoFlood(ex_why)
                if nf:                      # tool 16: nothing above the natural ground -> nothing floods
                    raise _NoFlood(nf_why)
                vw = get(f, "td_vw_m3", float); qp = get(f, "td_qp_m3s", float)
                rel = get(f, "td_rel_m3", float) or vw
                if dam_id not in bmap or vw is None or qp is None:
                    raise LookupError("no breach point or hydrograph")
                bxy, az = bmap[dam_id]
                if big > 0 and vw / AF_M3 > big:
                    reason = f"storage {vw / AF_M3:,.0f} ac-ft above the triage limit"
                else:
                    st = [x for _, arrs in cm.lines_near(streams, bxy, 3000) for x in arrs] if streams else []
                    pr = [x for _, arrs in cm.lines_near(damlines, bxy, 3000) for x in arrs] if damlines else []
                    pr += cm.protect_near(keep, bxy, 3000)
                    dem, gt = cm.load_cached_clip(loader, dam_id, st, pr)
                    half = 0.5 * min(dem.shape) * abs(gt[1])
                    inj = rn.injection_cells(gt, dem.shape, bxy, az, get(f, "td_bb_m", float) or 10.0,
                                             get(f, "td_hb_m", float) or 5.0, abs(gt[1]))
                    if not inj:
                        raise ValueError("breach toe outside the terrain clip")
                    r0 = int(np.mean([c[0] for c in inj])); c0 = int(np.mean([c[1] for c in inj]))
                    env = tg.screen(dem, gt, (r0, c0), qp, rel, n=n, margin_m=margin, margin_frac=mfrac, volume_factor=vf,
                                    half_width_m=min(1500.0, half))
                    reach = env["reach_m"]
                    tif = os.path.join(outdir, f"{dam_id}_triage_depth.tif")
                    cm.write_tif(tif, env["depth"], gt)
                    mask = env["mask"]
                    ext = QgsRectangle(gt[0], gt[3] + dem.shape[0] * gt[5], gt[0] + dem.shape[1] * gt[1], gt[3])

                    def inside(x, y):
                        r, c = gu.xy_to_rc(gt, x, y)
                        return gu.inside(mask.shape, r, c) and mask[r, c]

                    def receptor_hit(feat, tr):
                        # H23: sample a polygon receptor the way tool 8 does, so the triage
                        # shortcut is not less conservative than the 2D tool it stands in for.
                        # The point test (vertices + their mean) covers sub-cell footprints; for
                        # a polygon also apply tool 8's rule - any wet cell centre inside a ring
                        # (hz.nh.sample_footprint over the wet depth, centroid-cell fallback and
                        # all). Vertex-only sampling missed wet cells in a footprint's interior
                        # or along its edges, reading a flooded structure/lifeline as clear.
                        kind_geom, parts = cm.albers_parts(feat, tr)
                        if any(inside(x, y) for x, y in points_of(kind_geom, parts)):
                            return True
                        if kind_geom == "polygon":
                            return any((hz.nh.sample_footprint(env["depth"], None, gt[0], gt[3],
                                                               abs(gt[1]), ring)[0] or 0) > 0
                                       for ring in parts)
                        return False
                    found = []
                    for src, kind in ((bld, "structure"), (life, "lifeline")):
                        if src is None:
                            continue
                        ta, fa = cm.to_albers(src), cm.from_albers(src)
                        for b in src.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if kind == "structure" and not habitable(b):
                                continue
                            if receptor_hit(b, ta):
                                found.append(kind)
                    for lyr, occ_f, prim_f, hab_f in bld_extra:
                        ta, fa = cm.to_albers(lyr), cm.from_albers(lyr)
                        for b in lyr.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if not habitable(b, occ_f, prim_f, hab_f):
                                continue
                            if receptor_hit(b, ta):
                                found.append("structure")
                    for lyr in life_extra:
                        ta, fa = cm.to_albers(lyr), cm.from_albers(lyr)
                        for b in lyr.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if receptor_hit(b, ta):
                                found.append("lifeline")
                    for oid, (x, y) in dam_pts:
                        if oid != dam_id and inside(x, y):
                            found.append("dam")
                    def env_road(r, ta, nms, faadt, frcls, fsole):
                        """A road inside the envelope that could raise the class above Low. Same rule as tool 8
                        (nd_hazard): only a road KNOWN to be low (AADT < 500, not interstate, not US / State, not
                        sole access) cannot; an unknown AADT is assumed Medium there, so it counts here too -
                        0.58.0: unless the road takes a count from the traffic layers where the envelope
                        crosses it."""
                        rd = hz.nd_road("r", 1.0, cm.fval(r, nms, faadt, float),
                                        str(r[frcls]) if frcls and r[frcls] else "",
                                        sole_access=hz.nh.yes_no(r[fsole]) is True if fsole else False,
                                        name=_road_name(r))
                        lvl = hz.nh.road_class(rd, hz.nh.Settings())[0]
                        if lvl == "Low":
                            return False                # cannot raise the class above Low
                        gl = r.geometry(); gl.transform(ta)
                        parts = cm.geom_parts(gl)[1] if join and rd.aadt is None and not rd.interstate else None
                        gl = gl.densifyByDistance(abs(gt[1]))
                        if not any(inside(v.x(), v.y()) for v in gl.vertices()):
                            return False
                        m = match(parts, inside) if parts else None
                        if m and hz.nh.road_class(_with_count(rd, m), hz.nh.Settings())[0] == "Low":
                            rested.append("road {}: {} (Low instead of {})".format(_road_name(r) or r.id(),
                                                                                 m["text"], lvl))
                            return False
                        return True

                    for src, kind in ((roads, "road"), (rail, "railroad")):
                        if src is None:
                            continue
                        ta, fa = cm.to_albers(src), cm.from_albers(src)
                        for r in src.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if kind == "road":
                                if env_road(r, ta, rnames, f_aadt, f_rcls, f_sole):
                                    found.append(kind)
                                continue
                            gl = r.geometry(); gl.transform(ta); gl = gl.densifyByDistance(abs(gt[1]))
                            if any(inside(v.x(), v.y()) for v in gl.vertices()):
                                found.append(kind)
                    for lyr, nms, faadt, frcls, fsole in road_extra:
                        ta, fa = cm.to_albers(lyr), cm.from_albers(lyr)
                        for r in lyr.getFeatures(QgsFeatureRequest().setFilterRect(fa.transformBoundingBox(ext))):
                            if env_road(r, ta, nms, faadt, frcls, fsole):
                                found.append("road")
                    hits = len(found)
                    if env.get("untraced"):
                        reason = (f"no flow path could be traced from the breach toe ({env['end']}); "
                                  f"check the dam point, breach direction and terrain clip")
                    elif hits:
                        reason = ", ".join(f"{found.count(k)} {k}(s)" for k in ("structure", "road", "railroad",
                                                                                 "lifeline", "dam")
                                           if found.count(k)) + " inside the envelope"
                    elif env["edge"]:
                        reason = "envelope reaches the terrain edge"
                    else:
                        result, reason = "clear", f"nothing inside the envelope ({env['end']} limit, {reach:,.0f} m)"
                    rated = str(get(f, "haz_existing") or "").strip()
                    if result == "clear" and _rated_above_low(rated):
                        # 0.52.0 rating guard: the envelope's reach is a cheap estimate that can stop
                        # well short of a real breach flood (DWR-2636 and DWR-10551, both DWR High, were
                        # cleared on a 446 m / 1,568 m envelope while the 2D flood ran 4.9 / 7.1 km).
                        # Triage may send a dam DWR rates above Low to 2D, never clear it.
                        result = "needs 2D"
                        reason = (f"DWR rates it {rated}; triage never clears a dam rated above Low "
                                  f"(envelope alone: {reason})")
                    if result == "clear" and corr_on:
                        # 0.52.0 downstream corridor: only a dam still clear; it can only send it to 2D
                        if corr_missing:
                            result, tri_corr, corr_hit = "needs 2D", "incomplete", corr_missing
                            reason = f"{corr_missing}; envelope alone: {reason}"
                        else:
                            result, reason, tri_corr, corr_m, corr_hit = corridor_check(dam_id, f, bxy, env, gt,
                                                                                        rel, reason)
            except _NoFlood as e:
                result, reason = "clear", "no breach flood: {}".format(e)
            except Exception as e:  # keep the batch going
                reason = "not screened: " + er.plain(e, dam_id, "6")
            tri_aadt = None
            if result == "clear" and rested and not (nf or ex or flag_e):
                # 0.58.0: without the matched counts this dam would go to 2D: flag the clear for review
                tri_aadt = RA.review_text("Triage", "clear", "it needs 2D", rested)
                reason += "; rests on matched traffic counts for {} road(s) (tri_aadt)".format(len(rested))
                stats["aadt"] = stats.get("aadt", 0) + 1
            aud = "no"
            if result == "clear" and not nf and not flag_e and rnd.random() < audit:
                aud = "yes"; stats["audit"] += 1
            stats[result] += 1
            corr_stats[tri_corr] = corr_stats.get(tri_corr, 0) + 1
            for k, v in (("tri_result", result), ("tri_reason", reason), ("tri_reach_m", reach),
                         ("tri_hits", hits), ("tri_audit", aud), ("tri_tif", tif), ("tri_corr", tri_corr),
                         ("tri_corr_m", corr_m), ("tri_corr_hit", corr_hit[:254] if corr_hit else None),
                         ("tri_aadt", tri_aadt)):
                put(a, k, v)
            o = QgsFeature(fields); o.setGeometry(f.geometry()); o.setAttributes(a)
            sink.addFeature(o)
        feedback.pushInfo(f"triage: {stats['clear']} cleared ({stats['audit']} of them audited in 2D), "
                          f"{stats['needs 2D']} need the 2D model")
        if join:
            feedback.pushInfo("traffic counts: {}; {} file read(s) by area; clears that rest on a matched count "
                              "(tri_aadt, to review): {}".format(join.summary(), join.reads, stats.get("aadt", 0)))
        if corr_on:
            feedback.pushInfo("downstream corridor: " + ", ".join(
                "{} {}".format(corr_stats.get(k, 0), k) for k in ("hit", "incomplete", "failed", "none", "not run")) +
                " (none = still clear)")
        res = {"OUT": dest}
        if csink is not None:
            res["OUT_CORR"] = cdest
        return res
