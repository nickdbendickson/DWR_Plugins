"""A complete HEC-RAS 2D dam-breach model from a finished breach run.

The model is laid out the way HEC's tutorial "Dam Breach - Simple 2D Geometry" and HEC's own
example project (BaldEagleCrkMulti2D, plan "SA to 2D Dam Break Run") lay it out:

* the reservoir is a STORAGE AREA with an elevation-volume curve (the plugin's own storage
  curve, `breach.Reservoir`: the same storage at the top of dam, the same surface area);
* the dam is an SA/2D AREA CONNECTION between the storage area and the 2D area: a weir
  along the dam line at the top of dam (or the ground where that is higher), with the breach
  in the plan (centre station, final bottom width and elevation, side slopes, formation time,
  failure mode, water-surface trigger);
* downstream is a 2D FLOW AREA whose perimeter is the run's flooded extent, widened by a side
  buffer, with an extra buffer at the downstream end of the inundation, and cut off straight
  along the dam line;
* a NORMAL DEPTH boundary where the valley leaves the 2D area, with the slope of the
  valley there;
* v3.12.0: the run's road culverts, bridges and notches that lie in the 2D area are SA/2D
  AREA CONNECTIONS INSIDE the 2D area (both sides the same 2D area, the way HEC-RAS models a
  road embankment with culverts in a 2D area): a weir along the road from the terrain, a
  culvert group for a culvert, a gap in the weir for a bridge or notch (see
  `structure_connection`). NOT opened in HEC-RAS; the README lists what to check;
* the plan starts the reservoir at the run's starting water surface and breaches at once
  (sunny day), or routes the run's inflow hydrograph and breaches at the run's trigger level
  (flood scenario).

Written (text files in the HEC-RAS 6.x format, CRLF line ends):

    <name>.prj         project (units, files)
    <name>.g01         geometry: storage area, 2D area perimeter and its computation points,
                       SA/2D connection (weir profile), normal-depth BC line; v3.12.0: a
                       connection inside the 2D area for each culvert, bridge and notch
    <name>.u01         unsteady flow: initial reservoir level, inflow, normal depth
    <name>.p01         plan: simulation window, time step, 2D equation set, breach
    <name>.rasmap      RAS Mapper file pointing at the projection
    GIS/Projection.prj the model's coordinate system (ESRI WKT)
    Terrain/<name> Terrain.tif   the DEM in the model's coordinate system and vertical units
    GIS/HECRAS Layout.gpkg       the same geometry as GIS layers, to check in QGIS
    README_HECRAS.md   what was built, the values and where they came from, and the steps
                       left to do in HEC-RAS

What only HEC-RAS can make (it is a proprietary, versioned binary format): the RAS terrain
(Terrain/<name>.hdf, made in RAS Mapper from the GeoTIFF), and the preprocessed geometry
(<name>.g01.hdf: the mesh faces and the hydraulic property tables, computed from the
computation points and the terrain). The README lists those steps.

Units follow the model's coordinate system: a CRS in feet (US survey or international) gives
an English-units model (ft, acre-ft, cfs), a CRS in metres an SI model (m, 1000 m3, m3/s).
By default the model uses the DEM's CRS when that is in feet, else North Dakota State Plane
(NAD83, international feet, North or South zone by latitude) for a dam in North Dakota, else
the run's metric CRS. Everything (dam point, flood extent, DEM) is transformed into it.

The pure-text writers at the top use the standard library and numpy only; `build_model`
needs GDAL/OGR (part of QGIS).
"""
import datetime
import json
import math
import os
import re

import numpy as np

from . import breach as brmod
from . import conditioning as cond             # v3.12.0: polygon and polyline helpers
from . import hecras
from . import structures as structmod          # v3.12.0: the run's culverts and bridges
from .units import ACFT, CFS, FT, US_FT, MILE

PROGRAM_VERSION = "6.10"
SA_NAME = "Reservoir"
AREA_NAME = "Downstream"
CONN_NAME = "Dam"
BC_NAME = "DS Normal Depth"
START_DATE = "01JAN2000"
MAX_WEIR_POINTS = 450          # HEC-RAS allows 500 station-elevation points on a weir
MAX_ARC_POINTS = 400
# 3.10.85: HEC-RAS stops when the normal-depth BC line touches the 2D perimeter ("BC Line
# intersects the perimeter ... it must be entirely outside"), so it is drawn this many cells
# outside the perimeter, along it (found running the export in HEC-RAS 6.6 itself)
BC_OFFSET_CELLS = 0.25
CR = "\r\n"

# v3.12.0: culverts, bridges and notches as connections inside the 2D area. The keyword lines
# are copied from HEC's own example geometries (HEC-RAS 6.6 example projects):
#   internal connection (both sides the 2D area, weir, routing type, outlet rating curve line):
#     2D Unsteady Flow Hydraulics/BaldEagleCrkMulti2D/BaldEagleDamBrk.g09 'Sayers Dam', and
#     BaldEagleDamBrk.g03 'Lower Levee' (weir equation for the overflow: 'Conn OverFlow
#     Method 2D=False', 'Conn Weir WD=10', 'Conn Weir Coef=2.6', 'Conn Weir Is Ogee= 0 ')
#   culvert group 'Connection Culv=', barrel stations, 'Conn Culv Bottom n=' (the routing type
#   there, 3, is NOT for a connection inside a 2D area - see ROUTING_WEIR_CULVERTS below):
#     Applications Guide/Example 17 - Unsteady Flow Application/Diamond.g01 'Pyramid to Eagle'
#     1D Unsteady Flow Hydraulics/Storage Area Hydraulic Connection/Beaver_LS_SAConn.g01
# The fields of 'Connection Culv=' were read against the geometry HDF HEC-RAS 6.3 wrote for
# Diamond and Beaver_LS_SAConn: shape, RISE, SPAN (blank for a circular pipe), length,
# Manning's n, entrance loss, exit loss, FHWA chart, FHWA scale, upstream invert, downstream
# invert, number of barrels, group name (12 characters), solution flag (0), upstream distance
# (blank on a connection); then one upstream / downstream station pair per barrel in
# 8-character fields. In those storage-area files a culvert counts only with 'Conn Routing
# Type= 3' (OrleansEx.g04 has a culvert with type 1 and HEC-RAS 6.3 left it out of the HDF);
# inside a 2D area type 3 stops HEC-RAS 6.6, see ROUTING_WEIR_CULVERTS below.
CULVERT_GROUP = "Culvert #1"
# 3.10.56 fix (Absaraka chain abs2801v2egrcv: "The SA 2D Connection at BNSF KO KO-01 cu has an
# error with the connection. Check that the "To:" is set correctly."): a connection inside a 2D
# area is 'Conn Routing Type= 1' (weir, gates, culverts) WITH or without culverts, as HEC's
# BaldEagleDamBrk internal levees and HEC-RAS-computed culverts on them are. Type 3 (copied from
# the storage-area examples above) made HEC-RAS 6.6 read every culvert connection as a family of
# rating curves ('Use RC Family' 1 in the geometry HDF) with no curves, and stop. The barrels
# keep HEC-RAS's default centerline (across the line at the barrel's station, the barrel length
# long, headwater end on the right of the line: barrel_ends(), which the cell checks use; it
# matched the centerlines HEC-RAS 6.6 wrote for that run to the hundredth of a foot).
ROUTING_WEIR_CULVERTS = 1
# Runner shape -> (HEC-RAS shape code, FHWA chart, FHWA scale, entrance loss coefficient):
# a concrete pipe with a square edge in a headwall (chart 1 scale 1, Ke 0.5) and a box with
# wingwalls flared 30 to 75 degrees (chart 8 scale 1, Ke 0.4), HEC-RAS's typical values
CULVERT_TYPES = {"pipe": (1, 1, 1, 0.5), "box": (2, 8, 1, 0.4)}
CULVERT_EXIT_LOSS = 1.0
STRUCT_WEIR_COEF = 2.6         # road overflow, US units (HEC-RAS's default weir coefficient)
STRUCT_RISE_FT = 10.0          # the connection line runs along the road until the ground is
STRUCT_MAX_HALF_FT = 1000.0    # this far above the road at the structure, or this far each side
STRUCT_EDGE_CELLS = 2.0        # 3.10.56: both ends of a connection line at least this many cells
#                                inside the 2D perimeter (KO-01 at Absaraka ended 114 ft = about
#                                one cell from it and HEC-RAS stopped: "check that the To: is set")
STRUCT_ADVERSE_FT = 0.5        # a culvert whose outlet invert is this much above its inlet is noted

# computation intervals HEC-RAS offers (seconds), and their labels
COMP_INTERVALS = ((0.1, "0.1SEC"), (0.2, "0.2SEC"), (0.25, "0.25SEC"), (0.3, "0.3SEC"),
                  (0.4, "0.4SEC"), (0.5, "0.5SEC"), (1, "1SEC"), (2, "2SEC"), (3, "3SEC"),
                  (4, "4SEC"), (5, "5SEC"), (6, "6SEC"), (10, "10SEC"), (12, "12SEC"),
                  (15, "15SEC"), (20, "20SEC"), (30, "30SEC"), (60, "1MIN"), (120, "2MIN"),
                  (180, "3MIN"), (240, "4MIN"), (300, "5MIN"), (600, "10MIN"))
NICE_CELLS = (5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 200, 250, 300,
              400, 500)

# Adaptive (Courant) time step of the plan, HEC-RAS 6.x 'Computation Time Step' lines. The
# keywords, their order and the 'Use Courant=' layout are copied from HEC's own 6.x example
# BaldEagleCrkMulti2D/BaldEagleDamBrk.p06 (Example_Projects_6_6; Max Courant=1, Min
# Courant=0.45, Count To Double=4, Max Doubling=1, Max Halving=0). The Computation Interval is
# the base step; HEC-RAS halves it (at most max_halving times) when the Courant number
# passes max, and doubles it (at most max_doubling times) after count_to_double steps below
# min. max None = the equation's Courant target (1 SWE, 2 DWE); min None = 0.45 x max
# (0.45 SWE, 0.9 DWE). HEC-RAS 6.6 refuses min >= 0.5 x max ('The advanced time step control
# requires that the Minimum Courant is < 0.5 Maximum Courant'; Absaraka, max 1 / min 0.5),
# so courant_lines() raises for it.
COURANT_MIN_RATIO = 0.45
COURANT_DEFAULTS = {"adaptive": True, "max": None, "min": None, "count_to_double": 4,
                    "max_doubling": 3, "max_halving": 3, "residence": 0}

EQ_SWE, EQ_DWE = "swe", "dwe"
EQUATIONS = {EQ_SWE: (1, "Shallow water equations (SWE-ELM)"),
             EQ_DWE: (0, "Diffusion wave")}


# v3.10.22: solver tolerances written in the plan. "default" is what 3.10.20 wrote; "tight"
# halves the water-surface and volume tolerances and allows more iterations (option
# tolerances), to bring the volume accounting error down (Absaraka 3.10.17 package: 1.05 %)
TOLERANCES = {"default": {"ztol": 0.02, "zsatol": 0.02, "d2_ztol": 0.01, "d2_voltol": 0.01,
                          "d2_maxiter": 20},
              "tight": {"ztol": 0.01, "zsatol": 0.01, "d2_ztol": 0.005, "d2_voltol": 0.005,
                        "d2_maxiter": 30}}
# v3.10.22: sunny day: the breach starts at a set time after a short quiet warm-up (the pool at
# its starting level), not by a trigger level a hundredth below the starting pool
BREACH_WARMUP_HR = 0.25
BREACH_START_MODES = ("time", "wse")
HEC_BREACH_MODES = ("match_plugin", "failure_mode")
SPILLWAY_WEIR_COEF = 2.6        # US units; the dam connection's own weir coefficient
SPILLWAY_INTERVAL_MIN = 5       # the reservoir's warm-up inflow series


class HecRasModelError(ValueError):
    pass


# ============================================================================
# Units
# ============================================================================
class Units:
    """Project units of the HEC-RAS model, from the linear unit of its CRS."""

    def __init__(self, si):
        self.si = bool(si)
        self.L = 1.0 if self.si else FT            # metres per project length unit

    @property
    def label(self):
        return "SI Units" if self.si else "English Units"

    @property
    def len_name(self):
        return "m" if self.si else "ft"

    @property
    def vol_name(self):
        return "1000 m3" if self.si else "acre-ft"

    @property
    def q_name(self):
        return "m3/s" if self.si else "cfs"

    def length(self, m):
        return float(m) / self.L

    def from_ft(self, ft):
        return None if ft is None else float(ft) * FT / self.L

    def volume(self, m3):
        return float(m3) / (1000.0 if self.si else ACFT)

    def flow(self, cms):
        return float(cms) if self.si else float(cms) / CFS

    def weir_coef(self, c_us):
        """A US-units weir coefficient (ft^0.5/s) in the project units."""
        return float(c_us) * math.sqrt(FT) if self.si else float(c_us)


# ============================================================================
# Text formatting
# ============================================================================
def f8(v):
    """A number in an 8-character field (hecras.field8: decimals dropped to fit; a value that
    cannot fit raises HecRasModelError). None or NaN is a blank field."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return " " * 8
    try:
        return hecras.field8(v, 8)
    except hecras.FixedWidthError as e:
        raise HecRasModelError(str(e))


def f16(v):
    """A coordinate in a 16-character field, with as many decimals as fit in 15 so that
    neighbouring numbers never run together."""
    v = float(v)
    for d in (8, 7, 6, 5, 4, 3, 2, 1, 0):
        s = "{:.{}f}".format(v, d)
        if len(s) <= 15:
            return s.rjust(16)
    raise HecRasModelError("coordinate {} is too large for HEC-RAS".format(v))


def num(v, nd=3):
    """Short number text for 'key=value' lines."""
    if v is None:
        return ""
    s = "{:.{}f}".format(float(v), nd).rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def name16(s):
    s = str(s)
    if len(s) > 16 or not s.strip() or any(c in s for c in ",=\r\n"):
        raise HecRasModelError("HEC-RAS names are 1 to 16 characters without , or =: "
                               "{!r}".format(s))
    return s.ljust(16)


def name32(s):
    s = str(s)
    if len(s) > 32 or any(c in s for c in ",=\r\n"):
        raise HecRasModelError("bad boundary-line name {!r}".format(s))
    return s.ljust(32)


def lines8(values, per_line=10):
    vals = list(values)
    return ["".join(f8(x) for x in vals[i:i + per_line]) for i in range(0, len(vals), per_line)]


def lines_xy(points, per_line=2):
    """Coordinate pairs, `per_line` points to a line, 16-character fields."""
    pts = [(float(x), float(y)) for x, y in points]
    out = []
    for i in range(0, len(pts), per_line):
        out.append("".join(f16(x) + f16(y) for x, y in pts[i:i + per_line]))
    return out


def comp_interval(dt_s):
    """The largest HEC-RAS computation interval not above dt_s: (seconds, label)."""
    best = COMP_INTERVALS[0]
    for s, lab in COMP_INTERVALS:
        if s <= dt_s + 1e-9:
            best = (s, lab)
    return best


def nice_cell(c):
    for v in NICE_CELLS:
        if v >= c - 1e-9:
            return float(v)
    return float(NICE_CELLS[-1])


def ras_date(hours, start=START_DATE):
    """('01JAN2000', '0000') for a time `hours` after the start date at 00:00."""
    t0 = datetime.datetime.strptime(start, "%d%b%Y")
    t = t0 + datetime.timedelta(hours=float(hours))
    return t.strftime("%d%b%Y").upper(), t.strftime("%H%M")


def safe_name(s, n=24):
    s = re.sub(r"[^A-Za-z0-9_]+", "_", str(s)).strip("_") or "Dam"
    return s[:n]


# ============================================================================
# Model description (filled by build_model, or by hand in the tests)
# ============================================================================
class Model:
    """Everything the writers need, in project units and the model CRS."""

    def __init__(self):
        self.name = "Dam"
        self.title = "Dam breach"
        self.units = Units(False)
        self.perimeter = []          # 2D area perimeter, not closed, counter-clockwise
        self.points = np.zeros((0, 2))   # 2D computation points
        self.cell = 100.0
        self.nominal_cell = None     # Point Generation Data; None = cell
        self.manning_n = 0.06
        self.reservoir = []          # storage-area outline (display only)
        self.elev_volume = []        # [(elevation, volume)]
        self.conn_line = []          # connection line, left to right looking downstream
        self.weir = []               # [(station, elevation)] along conn_line
        self.weir_width = 20.0
        self.weir_coef = 2.6
        self.bc_line = []            # normal-depth BC line along the perimeter
        self.bc_slope = 0.001
        self.initial_wse = None
        self.inflow = None           # (interval label, [values]) or None for base flow
        self.base_flow = 0.0
        self.sim_hours = 24.0
        self.comp_interval = "5SEC"
        # adaptive time step (see courant_lines()); None = fixed Computation Interval
        self.courant = dict(COURANT_DEFAULTS)
        self.output_interval = "1MIN"
        self.mapping_interval = "5MIN"
        self.detail_interval = "1HOUR"
        self.equation = EQ_SWE
        self.breach = {}             # see breach_block()
        self.projection_name = "Projection.prj"
        self.terrain_name = ""
        self.description = []
        self.structures = []         # v3.12.0: structure_connection() dicts
        self.tolerances = dict(TOLERANCES["default"])   # v3.10.22: solver tolerances (plan)
        self.breaklines = []         # v3.10.22: [{'name', 'line', 'near', 'far'}] in the 2D area
        self.conn_cellsize = None    # v3.10.22: (near, far) along the dam connection

    def bounds(self):
        pts = list(self.perimeter) + list(self.reservoir)
        a = np.asarray(pts, dtype=float)
        return float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), \
            float(a[:, 1].max())


# ============================================================================
# Writers
# ============================================================================
def _centroid(pts):
    a = np.asarray(pts, dtype=float)
    return float(a[:, 0].mean()), float(a[:, 1].mean())


def project_text(m):
    L = ["Proj Title=" + m.title, "Current Plan=p01", "Default Exp/Contr=0.3,0.1", m.units.label,
         "Geom File=g01", "Unsteady File=u01", "Plan File=p01", "Y Axis Title=Elevation",
         "X Axis Title(PF)=Main Channel Distance", "X Axis Title(XS)=Station",
         "BEGIN DESCRIPTION:"] + list(m.description) + [
        "END DESCRIPTION:", "DSS Start Date=", "DSS Start Time=", "DSS End Date=",
        "DSS End Time=", "DSS File=dss", "DSS Export Filename=", "DSS Export Rating Curves= 0 ",
        "DSS Export Rating Curve Sorted= 0 ", "DSS Export Volume Flow Curves= 0 ",
        "DXF Filename=", "DXF OffsetX= 0 ", "DXF OffsetY= 0 ", "DXF ScaleX= 1 ",
        "DXF ScaleY= 10 ", "GIS Export Profiles= 0 "]
    return CR.join(L) + CR


def geometry_text(m, now=None):
    now = now or datetime.datetime.now()
    xmin, ymin, xmax, ymax = m.bounds()
    pad = 0.05 * max(xmax - xmin, ymax - ymin)
    L = ["Geom Title=" + m.title, "Program Version=" + PROGRAM_VERSION,
         "Viewing Rectangle= {} , {} , {} , {} ".format(num(xmin - pad, 2), num(xmax + pad, 2),
                                                       num(ymax + pad, 2), num(ymin - pad, 2)),
         ""]
    # --- 2D flow area ------------------------------------------------------------------
    cx, cy = _centroid(m.perimeter)
    L.append("Storage Area={},{},{}".format(name16(AREA_NAME), num(cx, 4), num(cy, 4)))
    L.append("Storage Area Surface Line= {} ".format(len(m.perimeter)))
    L += [s.ljust(48) for s in lines_xy(m.perimeter, 1)]
    L += ["Storage Area Type= 0 ", "Storage Area Area=", "Storage Area Min Elev=",
          "Storage Area Is2D=-1",
          "Storage Area Point Generation Data=,,{0},{0}".format(
              num(m.nominal_cell if getattr(m, "nominal_cell", None) else m.cell)),
          "Storage Area 2D Points= {} ".format(len(m.points))]
    L += lines_xy(m.points, 2)
    L += ["Storage Area 2D PointsPerimeterTime=" + now.strftime("%d%b%Y %H:%M:%S"),
          "Storage Area Mannings={}".format(num(m.manning_n, 4)),
          "2D Cell Volume Filter Tolerance=0.01", "2D Cell Minimum Area Fraction=0.01",
          "2D Face Profile Filter Tolerance=0.01",
          "2D Face Area Elevation Profile Filter Tolerance=0.01",
          "2D Face Area Elevation Conveyance Ratio=0.02", "2D Face Area Laminar Depth=0.2", ""]
    # --- reservoir storage area ----------------------------------------------------------
    cx, cy = _centroid(m.reservoir)
    L.append("Storage Area={},{},{}".format(name16(SA_NAME), num(cx, 4), num(cy, 4)))
    L.append("Storage Area Surface Line= {} ".format(len(m.reservoir)))
    L += [s.ljust(48) for s in lines_xy(m.reservoir, 1)]
    L += ["Storage Area Type= 1 ", "Storage Area Area=", "Storage Area Min Elev=",
          "Storage Area Vol Elev= {} ".format(len(m.elev_volume))]
    flat = []
    for e, v in m.elev_volume:
        flat += [e, v]
    L += lines8(flat)
    L += ["Storage Area Is2D=0", ""]
    # --- v3.10.22: 2D breaklines (BaldEagleDamBrk.g03 / Muncie.g04 layout); the computation
    # points already follow them (hecras_breaklines.enforce) ------------------------------
    for b in m.breaklines:
        L += ["BreakLine Name=" + str(b["name"]),
              "BreakLine CellSize Min={}".format(num(b["near"], 2)),
              "BreakLine CellSize Max={}".format(num(b["far"], 2)),
              "BreakLine Near Repeats={}".format(int(b.get("repeats", 0))),
              "BreakLine Protection Radius=-1",
              "BreakLine Polyline= {} ".format(len(b["line"]))]
        L += lines_xy(b["line"], 2)
    # --- SA/2D connection: the dam ------------------------------------------------------
    cx, cy = _centroid(m.conn_line)
    L.append("Connection={},{},{}".format(name16(CONN_NAME), num(cx, 4), num(cy, 4)))
    L.append("Connection Desc=Dam crest from the ND Dam Breach Runner")
    L.append("Connection Line={}".format(len(m.conn_line)))
    L += lines_xy(m.conn_line, 2)
    L.append("Connection Last Edited Time=" + now.strftime("%b/%d/%Y %H:%M:%S"))
    if m.conn_cellsize:
        L += ["Conn CellSize Min={}".format(num(m.conn_cellsize[0], 2)),
              "Conn CellSize Max={}".format(num(m.conn_cellsize[1], 2))]
    L += ["Conn Near Repeats=1",
          "Connection Up SA=" + name16(SA_NAME), "Connection Dn SA=" + name16(AREA_NAME),
          "Conn Routing Type= 1 ", "Conn Use RC Family=False", "Conn OverFlow Method 2D=True",
          "Conn Weir WD={}".format(num(m.weir_width, 2)),
          "Conn Weir Coef={}".format(num(m.weir_coef, 3)),
          "Conn Simple Spill Pos Coef=0.05", "Conn Simple Spill Neg Coef=0.05",
          "Conn Weir SE= {} ".format(len(m.weir))]
    flat = []
    for s, e in m.weir:
        flat += [s, e]
    L += lines8(flat)
    L += ["Conn Outlet Rating Curve= 0 ,False,,"]
    # --- v3.12.0: culverts, bridges and notches inside the 2D area ----------------------
    for c in m.structures:
        L += structure_block(c, now)
    # --- normal-depth boundary line -----------------------------------------------------
    bc = m.bc_line
    mid = bc[len(bc) // 2]
    L += ["BC Line Name=" + name32(BC_NAME), "BC Line Storage Area=" + name16(AREA_NAME),
          "BC Line Start Position= {} , {} ".format(num(bc[0][0], 4), num(bc[0][1], 4)),
          "BC Line Middle Position= {} , {} ".format(num(mid[0], 4), num(mid[1], 4)),
          "BC Line End Position= {} , {} ".format(num(bc[-1][0], 4), num(bc[-1][1], 4)),
          "BC Line Arc= {} ".format(len(bc))]
    L += lines_xy(bc, 2)
    L += ["BC Line Text Position= {} , {} ".format(num(mid[0], 4), num(mid[1], 4)),
          "LCMann Time=Dec/30/1899 00:00:00", "LCMann Region Time=Dec/30/1899 00:00:00",
          "LCMann Table=0", "Chan Stop Cuts=-1 ", "", "", "",
          "Use User Specified Reach Order=0", "GIS Ratio Cuts To Invert=-1",
          "GIS Limit At Bridges=0", "Composite Channel Slope=5"]
    return CR.join(L) + CR


def barrel_ends(c):
    """3.10.56: the (headwater end, tailwater end) of each culvert barrel of connection `c`, as
    HEC-RAS lays a barrel with no centerline of its own: at the barrel's station on the
    connection line, across it, the barrel length long. The line is drawn left to right looking
    downstream, so the headwater side is on its right."""
    cv = c.get("culvert") or {}
    A = np.asarray(c["line"][0], float)
    B = np.asarray(c["line"][-1], float)
    L = float(np.hypot(*(B - A))) or 1.0
    t = (B - A) / L
    down = np.array([-t[1], t[0]])                 # left of the line = downstream
    h = 0.5 * float(cv.get("length") or 0.0)
    out = []
    for sb in cv.get("stations", []):
        P = A + t * float(sb)
        out.append((tuple(P - down * h), tuple(P + down * h)))
    return out


def structure_block(c, now):
    """v3.12.0: the geometry lines of one structure inside the 2D area (a dict from
    `structure_connection`): an SA/2D area connection from the 2D area to itself, laid out as
    HEC's internal structure 'Sayers Dam' (BaldEagleDamBrk.g09) - connection line, weir
    station-elevation, outlet rating curve line - with, for a culvert, the culvert group as
    HEC's Diamond.g01 and Beaver_LS_SAConn.g01 write it (see CULVERT_GROUP above for the
    fields). NOT opened in HEC-RAS: the README lists what to check there."""
    cx, cy = _centroid(c["line"])
    culv = c.get("culvert")
    L = ["Connection={},{},{}".format(name16(c["name"]), num(cx, 4), num(cy, 4)),
         "Connection Desc=" + c["desc"],
         "Connection Line={}".format(len(c["line"]))]
    L += lines_xy(c["line"], 2)
    L += ["Connection Last Edited Time=" + now.strftime("%b/%d/%Y %H:%M:%S"),
          "Conn Near Repeats=1",
          "Connection Up SA=" + name16(AREA_NAME), "Connection Dn SA=" + name16(AREA_NAME),
          # 1 = weir, gates and culverts, inside a 2D area (BaldEagleDamBrk.g03 'Lower Levee');
          # NOT 3 (a family of rating curves there: HEC-RAS stops, "check the To:")
          "Conn Routing Type= {} ".format(ROUTING_WEIR_CULVERTS),
          "Conn Use RC Family=False", "Conn OverFlow Method 2D=False",
          "Conn Weir WD={}".format(num(c["weir_width"], 2)),
          "Conn Weir Coef={}".format(num(c["weir_coef"], 3)),
          "Conn Weir Is Ogee= 0 ",
          "Conn Simple Spill Pos Coef=0.05", "Conn Simple Spill Neg Coef=0.05",
          "Conn Weir SE= {} ".format(len(c["weir"]))]
    flat = []
    for s, e in c["weir"]:
        flat += [s, e]
    L += lines8(flat)
    if culv:
        pipe = culv["shape"] == CULVERT_TYPES["pipe"][0]
        L.append("Connection Culv={},{},{},{},{},{},{},{},{},{},{}, {} ,{}, 0 ,".format(
            culv["shape"], num(culv["rise"], 3), "" if pipe else num(culv["span"], 3),
            num(culv["length"], 2), num(culv["n"], 4), num(culv["entrance"], 3),
            num(culv["exit"], 3), culv["chart"], culv["scale"], num(culv["inv_up"], 2),
            num(culv["inv_dn"], 2), int(culv["barrels"]), CULVERT_GROUP.ljust(12)))
        st = []
        for s in culv["stations"]:
            st += [s, s]                         # upstream and downstream station of a barrel
        L += lines8(st)
        L.append("Conn Culv Bottom n={}".format(num(culv["n"], 4)))
    L.append("Conn Outlet Rating Curve= 0 ,False,,")
    return L


_MET = """Met Point Raster Parameters=,,,,
Precipitation Mode=Disable
Wind Mode=No Wind Forces
Air Density Mode=
Met BC=Precipitation|Expanded View=0
Met BC=Precipitation|Point Interpolation=Nearest
Met BC=Precipitation|Gridded Source=DSS
Met BC=Evapotranspiration|Expanded View=0
Met BC=Evapotranspiration|Point Interpolation=Nearest
Met BC=Evapotranspiration|Gridded Source=DSS
Met BC=Wind Speed|Expanded View=0
Met BC=Wind Speed|Point Interpolation=Nearest
Met BC=Wind Speed|Gridded Source=DSS
Met BC=Wind Direction|Expanded View=0
Met BC=Wind Direction|Point Interpolation=Nearest
Met BC=Wind Direction|Gridded Source=DSS
Met BC=Wind Velocity X|Expanded View=0
Met BC=Wind Velocity X|Point Interpolation=Nearest
Met BC=Wind Velocity X|Gridded Source=DSS
Met BC=Wind Velocity Y|Expanded View=0
Met BC=Wind Velocity Y|Point Interpolation=Nearest
Met BC=Wind Velocity Y|Gridded Source=DSS
Met BC=Air Density|Mode=Constant
Met BC=Air Density|Expanded View=0
Met BC=Air Density|Constant Value=1.225
Met BC=Air Density|Constant Units=kg/m3
Met BC=Air Density|Point Interpolation=Nearest
Met BC=Air Density|Gridded Source=DSS
Met BC=Air Temperature|Expanded View=0
Met BC=Air Temperature|Point Interpolation=Nearest
Met BC=Air Temperature|Gridded Source=DSS
Met BC=Humidity|Expanded View=0
Met BC=Humidity|Point Interpolation=Nearest
Met BC=Humidity|Gridded Source=DSS
Met BC=Air Pressure|Expanded View=0
Met BC=Air Pressure|Point Interpolation=Nearest
Met BC=Air Pressure|Gridded Source=DSS
Non-Newtonian Method= 0 ,
Non-Newtonian Constant Vol Conc=0
Non-Newtonian Yield Method= 0 ,
Non-Newtonian Yield Coef=0, 0
User Yeild=   0
Non-Newtonian Sed Visc= 0 ,
Non-Newtonian Obrian B=0
User Viscosity=0
User Viscosity Ratio=0
Herschel-Bulkley Coef=0, 0
Clastic Method= 0 ,
Coulomb Phi=0
Voellmy X=0
Non-Newtonian Hindered FV= 0
Non-Newtonian FV K=0
Non-Newtonian ds=0
Non-Newtonian Max Cv=0
Non-Newtonian Bulking Method= 0 ,
Non-Newtonian High C Transport= 0 , """


def _bc_tail():
    return ["DSS Path=", "Use DSS=False", "Use Fixed Start Time=False",
            "Fixed Start Date/Time=,", "Is Critical Boundary=False", "Critical Boundary Flow="]


def unsteady_text(m):
    blank16, blank8, blank32 = " " * 16, " " * 8, " " * 32
    L = ["Flow Title=" + m.title, "Program Version=" + PROGRAM_VERSION, "Use Restart= 0 ",
         "Initial Storage Elev={},{}".format(name16(SA_NAME), num(m.initial_wse, 3))]
    # inflow to the reservoir
    L.append("Boundary Location={},{},{},{},{},{},{},{}".format(
        blank16, blank16, blank8, blank8, blank16, name16(SA_NAME), blank16, blank32))
    if m.inflow:
        interval, vals = m.inflow
    else:
        interval = "1HOUR"
        vals = [m.base_flow] * (int(math.ceil(m.sim_hours)) + 2)
    L.append("Interval=" + interval)
    L.append("Lateral Inflow Hydrograph= {} ".format(len(vals)))
    L += lines8(vals)
    L += _bc_tail()
    # normal depth where the valley leaves the 2D area
    L.append("Boundary Location={},{},{},{},{},{},{},{}".format(
        blank16, blank16, blank8, blank8, blank16, name16(AREA_NAME), blank16, name32(BC_NAME)))
    L.append("Friction Slope={},0".format(num(m.bc_slope, 6)))
    L += _MET.split("\n")
    return CR.join(L) + CR


def breach_block(m):
    b = m.breach
    piping = bool(b.get("piping"))
    geom = [num(b["center_station"], 2), num(b["bottom_width"], 2), num(b["bottom_elev"], 2),
            num(b["left_slope"], 3), num(b["right_slope"], 3), "True" if piping else "False",
            num(b.get("piping_coef", 0.5), 3),
            num(b["piping_elev"], 2) if piping and b.get("piping_elev") is not None else "",
            num(b["formation_hr"], 3), num(b["weir_coef"], 3)]
    blank16, blank8 = " " * 16, " " * 8
    return ["Breach Loc={},{},{},True,{}".format(blank16, blank16, blank8, name16(CONN_NAME)),
            "Breach Method= 0 ",
            "Breach Geom=" + ",".join(geom),
            breach_start_line(b),
            "Breach Progression= 2 ", f8(0) + f8(0) + f8(1) + f8(1),
            "Simplified Physical Breach Downcutting= 2 ", f8(0) + f8(0) + f8(1) + f8(1),
            "Simplified Physical Breach Widening= 2 ", f8(0) + f8(0) + f8(1) + f8(1),
            "Starting Notch Depth= 3.402823E+38 ", "Initial Piping Diameter= 3.402823E+38 ",
            "Mass Wasting Options= 0 ", "Mass Wasting Width= 3.402823E+38 ",
            "Mass Wasting Duration= 3.402823E+38 ",
            "Mass Wasting Final Bottom Elevation= 3.402823E+38 ",
            "Breach Use User Defined Growth Ratio=0", "Breach User Defined Growth Ratio=1"]


def breach_start_line(b):
    """v3.10.22: 'Breach Start=' - a set time (HEC's L40_Levee_Outflow_H.p06 layout:
    False,,<date>,<HHMM>,...) when b['start_mode'] is 'time', else the water-surface trigger
    (BaldEagleDamBrk layout)."""
    if b.get("start_mode") == "time":
        d, t = ras_date(float(b.get("start_hr") or 0.0))
        return "Breach Start=False,,{},{},False,,,0".format(d, t)
    return "Breach Start=True,{},,,False,,,0".format(num(b["trigger_wse"], 3))


def _g(v):
    return "{:g}".format(float(v))


def _d2_block(m, eq):
    tol = getattr(m, "tolerances", None) or TOLERANCES["default"]
    return ["UNET D2 Name=" + name16(AREA_NAME), "UNET D2 Theta= 1 ", "UNET D2 Theta Warmup= 1 ",
            "UNET D2 Z Tol= {} ".format(_g(tol["d2_ztol"])),
            "UNET D2 Volume Tol= {} ".format(_g(tol["d2_voltol"])),
            "UNET D2 Max Iterations= {} ".format(int(tol["d2_maxiter"])),
            "UNET D2 Equation= {} ".format(eq), "UNET D2 TotalICTime=",
            "UNET D2 RampUpFraction=0.1", "UNET D2 TimeSlices= 1 ",
            "UNET D2 Turbulence Formulation=Non-Conservative (original)",
            "UNET D2 Eddy Viscosity=", "UNET D2 Transverse Eddy Viscosity=",
            "UNET D2 Smagorinsky Mixing=0", "UNET D2 BCVolumeCheck=0", "UNET D2 Latitude=",
            "UNET D2 Cores=0", "UNET D2 SolverType=PARDISO (Direct)"]


def courant_lines(c, equation=EQ_SWE):
    """The plan's 'Computation Time Step' lines (HEC's BaldEagleDamBrk.p06 layout). With
    c['adaptive'] false (or c None) they are HEC's fixed-step lines (Muncie.p03)."""
    if not c or not c.get("adaptive"):
        return ["Computation Time Step Use Courant=        0",
                "Computation Time Step Use Time Series=    0",
                "Computation Time Step Max Courant=", "Computation Time Step Min Courant=",
                "Computation Time Step Count To Double=0", "Computation Time Step Max Doubling=0",
                "Computation Time Step Max Halving=0",
                "Computation Time Step Residence Courant=0"]
    cmax = float(c.get("max") or (1.0 if equation == EQ_SWE else 2.0))
    cmin = float(c.get("min") if c.get("min") is not None else COURANT_MIN_RATIO * cmax)
    if not (0 < cmin < 0.5 * cmax - 1e-12):
        raise HecRasModelError("adaptive time step: HEC-RAS needs 0 < min Courant < 0.5 x max "
                               "Courant (got min {:g}, max {:g}; e.g. min {:g})".format(
                                   cmin, cmax, COURANT_MIN_RATIO * cmax))
    ints = []
    for k in ("count_to_double", "max_doubling", "max_halving"):
        v = int(c.get(k) or 0)
        if v < 0:
            raise HecRasModelError("adaptive time step: {} must be 0 or more".format(k))
        ints.append(v)
    return ["Computation Time Step Use Courant=        -1",
            "Computation Time Step Use Time Series=    0",
            "Computation Time Step Max Courant=" + num(cmax, 3),
            "Computation Time Step Min Courant=" + num(cmin, 3),
            "Computation Time Step Count To Double={}".format(ints[0]),
            "Computation Time Step Max Doubling={}".format(ints[1]),
            "Computation Time Step Max Halving={}".format(ints[2]),
            "Computation Time Step Residence Courant={}".format(num(c.get("residence") or 0, 3))]


def plan_text(m):
    eq = EQUATIONS[m.equation][0]
    tol = getattr(m, "tolerances", None) or TOLERANCES["default"]
    d1, t1 = ras_date(0.0)
    d2, t2 = ras_date(m.sim_hours)
    L = ["Plan Title=" + m.title, "Program Version=" + PROGRAM_VERSION,
         "Short Identifier=" + "Breach".ljust(64),
         "Simulation Date={},{},{},{}".format(d1, t1, d2, t2),
         "Geom File=g01", "Flow File=u01", "Subcritical Flow", "K Sum by GR= 0 ",
         "Std Step Tol= 0.01 ", "Critical Tol= 0.01 ", "Num of Std Step Trials= 20 ",
         "Max Error Tol= 0.3 ", "Flow Tol Ratio= 0.001 ", "Split Flow NTrial= 30 ",
         "Split Flow Tol= 0.02 ", "Split Flow Ratio= 0.02 ", "Log Output Level= 0 ",
         "Friction Slope Method= 1 ", "Unsteady Friction Slope Method= 2 ",
         "Unsteady Bridges Friction Slope Method= 1 ", "Parabolic Critical Depth",
         "Global Vel Dist= 0 , 0 , 0 ", "Global Log Level= 0 ", "CheckData=True",
         "Encroach Param=-1 ,0,0, 0 ", "BEGIN DESCRIPTION:"] + list(m.description) + [
        "END DESCRIPTION:",
        "Computation Interval=" + m.comp_interval, "Output Interval=" + m.output_interval,
        "Instantaneous Interval=" + m.detail_interval, "Mapping Interval=" + m.mapping_interval,
        ] + courant_lines(m.courant, m.equation) + [
        "Run HTab=-1 ", "Run UNet=-1 ", "Run Sediment= 0 ", "Run PostProcess=-1 ",
        "Run WQNet= 0 ", "Run RASMapper= 0 ",
        "UNET Theta= 1 ", "UNET Theta Warmup= 1 ",
        "UNET ZTol= {} ".format(_g(tol["ztol"])), "UNET ZSATol= {} ".format(_g(tol["zsatol"])),
        "UNET QTol=", "UNET MxIter= 20 ", "UNET Max Iter WO Improvement= 0 ",
        "UNET MaxInSteps= 0 ", "UNET DtIC= 0 ", "UNET DtMin= 0 ", "UNET MaxCRTS= 20 ",
        "UNET WFStab= 2 ", "UNET SFStab= 1 ", "UNET WFX= 1 ", "UNET SFX= 1 ",
        "UNET Gravity={}".format("9.80665" if m.units.si else "32.174"),
        "UNET 1D Methodology=Finite Difference", "UNET DSS MLevel= 4 ", "UNET Pardiso=0",
        "UNET DZMax Abort= 100 ", "UNET Use Existing IB Tables=-1 ",
        "UNET Froude Reduction=False", "UNET Froude Limit= 0.8 ", "UNET Froude Power= 4 ",
        "UNET D1 Cores= 0 ", "UNET D2 Coriolis=0", "UNET D2 Cores= 0 "]
    L += _d2_block(m, eq)[1:]
    L += _d2_block(m, eq)
    L += ["UNET D1D2 MaxIter= 0 ", "UNET D1D2 ZTol=0.01", "UNET D1D2 QTol=0.1",
          "UNET D1D2 MinQTol=1", "DSS File=dss", "Write IC File= 0 ",
          "Write IC File at Fixed DateTime=0", "IC Time=,,", "Write IC File Reoccurance=",
          "Write IC File at Sim End=0", "Echo Input=False", "Echo Parameters=False",
          "Echo Output=False", "Write Detailed= 0 ", "HDF Write Warmup=0",
          "HDF Write Time Slices=0", "HDF Flush=0", "HDF Compression= 1 ", "HDF Chunk Size= 1 ",
          "HDF Spatial Parts= 1 ", "HDF Use Max Rows=0", "HDF Fixed Rows= 1 "]
    L += breach_block(m)
    return CR.join(L) + CR


def rasmap_text(m):
    """RAS Mapper file: the projection and the geometry. The terrain is added in RAS Mapper
    (its terrain HDF is made there from the GeoTIFF in Terrain/)."""
    return ("<RASMapper>\n  <Version>2.0.0</Version>\n"
            '  <RASProjectionFilename Filename=".\\GIS\\{proj}" />\n'
            '  <Geometries Checked="True" Expanded="True">\n'
            '    <Layer Name="{title}" Type="RASGeometry" Checked="True" Expanded="True" '
            'Filename=".\\{geo}">\n'
            '      <Layer Type="RASStorageArea" Checked="True" />\n'
            '      <Layer Type="RASD2FlowArea" Checked="True" />\n'
            '      <Layer Type="StructureLayer" Checked="True" />\n'
            '      <Layer Type="RASBoundaryConditionLines" Checked="True" />\n'
            "    </Layer>\n  </Geometries>\n  <Terrains />\n"
            "  <Units>{units}</Units>\n</RASMapper>\n").format(
        proj=m.projection_name, title=xml_escape(m.title), geo="{}.g01.hdf".format(m.name),
        units=m.units.label).replace("\n", CR)


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def write_text_files(m, folder, now=None):
    """Writes the project, geometry, unsteady, plan and RAS Mapper files. Returns paths.

    now: optional fixed timestamp for the geometry edit-time stamps (Storage Area 2D
    PointsPerimeterTime / Connection Last Edited Time, incl. per-structure). It defaults to
    the wall clock, as before; a fixed value makes a build byte-reproducible (HEC-RAS
    overwrites these stamps on first open, so there is no operational difference)."""
    os.makedirs(folder, exist_ok=True)
    out = {}
    for ext, fn in (("prj", project_text), ("g01", geometry_text), ("u01", unsteady_text),
                    ("p01", plan_text), ("rasmap", rasmap_text)):
        p = os.path.join(folder, "{}.{}".format(m.name, ext))
        text = geometry_text(m, now) if fn is geometry_text else fn(m)
        with open(p, "w", newline="", encoding="ascii", errors="replace") as f:
            f.write(text)
        out[ext] = p
    return out


# ============================================================================
# Pure geometry helpers (numpy)
# ============================================================================
def ring_area(pts):
    a = np.asarray(pts, dtype=float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _offset_outward(ring, idx, dist):
    """3.10.85: the points ring[idx] of a closed ring moved `dist` outward, along the ring's
    normal there, so a line drawn along the perimeter lies just outside it."""
    ring = np.asarray(ring, float)
    n = len(ring)
    x, y = ring[:, 0], ring[:, 1]
    ccw = (np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)) > 0
    out = []
    for i in idx:
        t = ring[(i + 1) % n] - ring[(i - 1) % n]
        L = math.hypot(t[0], t[1]) or 1.0
        # outward normal: right of the direction of travel on a counter-clockwise ring
        nx, ny = (t[1] / L, -t[0] / L) if ccw else (-t[1] / L, t[0] / L)
        out.append((ring[i][0] + nx * dist, ring[i][1] + ny * dist))
    return np.asarray(out)


def densify(pts, step, closed=False):
    a = [tuple(map(float, p)) for p in pts]
    if closed:
        a = a + [a[0]]
    out = []
    for p, q in zip(a[:-1], a[1:]):
        d = math.hypot(q[0] - p[0], q[1] - p[1])
        n = max(1, int(math.ceil(d / step)))
        for k in range(n):
            out.append((p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n))
    if not closed:
        out.append(a[-1])
    return out


def thin_ring(ring, min_step, keep=()):
    """Drops ring vertices closer than `min_step` to the last kept one (HEC-RAS cells along
    the perimeter get a face per perimeter segment, and at most 8 faces are allowed).
    Vertices in `keep` stay."""
    ring = np.asarray(ring, float)
    keep = set(int(k) for k in keep)
    out = [0]
    for i in range(1, len(ring)):
        if i in keep or np.hypot(*(ring[i] - ring[out[-1]])) >= min_step:
            out.append(i)
    # the closing segment
    while len(out) > 3 and out[-1] not in keep and \
            np.hypot(*(ring[out[-1]] - ring[out[0]])) < min_step:
        out.pop()
    return ring[out]


def point_segment_distance(px, py, seg):
    """Distance from points (arrays) to a polyline (list of (x, y)), vectorised."""
    s = np.asarray(seg, dtype=float)
    best = np.full(np.shape(px), np.inf)
    for (x1, y1), (x2, y2) in zip(s[:-1], s[1:]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = np.zeros_like(px) if L2 == 0 else np.clip(((px - x1) * dx + (py - y1) * dy) / L2,
                                                       0.0, 1.0)
        d = np.hypot(px - (x1 + t * dx), py - (y1 + t * dy))
        best = np.minimum(best, d)
    return best


def simplify_profile(st, el, tol, max_points):
    """Douglas-Peucker on a station-elevation profile (keeps the end points)."""
    st = np.asarray(st, float)
    el = np.asarray(el, float)
    while True:
        keep = np.zeros(len(st), bool)
        keep[0] = keep[-1] = True
        stack = [(0, len(st) - 1)]
        while stack:
            i, j = stack.pop()
            if j <= i + 1:
                continue
            x0, y0, x1, y1 = st[i], el[i], st[j], el[j]
            seg = st[i + 1:j]
            yi = y0 + (y1 - y0) * (seg - x0) / ((x1 - x0) or 1.0)
            err = np.abs(el[i + 1:j] - yi)
            k = int(np.argmax(err))
            if err[k] > tol:
                keep[i + 1 + k] = True
                stack += [(i, i + 1 + k), (i + 1 + k, j)]
        if keep.sum() <= max_points:
            return st[keep], el[keep]
        tol *= 1.5


def storage_curve(dam, units, n=40, above_ft=None):
    """[(elevation, volume)] of the plugin's storage law (breach.Reservoir), from the
    reservoir bottom to above the top of dam, in project units."""
    top_m = dam.top_of_dam_ft * FT
    bot_m = top_m - dam.dam_height_ft * FT
    res = brmod.Reservoir(bot_m, top_m, dam.storage_top_acft * ACFT,
                          dam.surface_area_top_ac * 4046.8564224
                          if dam.surface_area_top_ac else None)
    extra_ft = above_ft if above_ft is not None else max(5.0, 0.25 * dam.dam_height_ft)
    hi_m = top_m + extra_ft * FT
    if dam.pmf_max_wse_ft is not None:
        hi_m = max(hi_m, dam.pmf_max_wse_ft * FT + 2 * FT)
    # finer near the bottom, where the power law curves most
    f = np.linspace(0.0, 1.0, n) ** 1.5
    z = bot_m + f * (hi_m - bot_m)
    z = np.unique(np.concatenate([z, [top_m]]))
    out = []
    for zz in z:
        out.append((round(units.length(zz), 2), round(units.volume(float(res.volume(zz))), 3)))
    return out, res


def breach_values(dam, params, units, crest_length_u):
    """Breach editor values (project units) from the run's parameters; same rules as the
    breach table of `hecras.breach_parameter_rows`. Returns (dict, notes)."""
    rows = {r["item"]: r for r in hecras.breach_parameter_rows(dam, params)}
    p = dict(params or {})
    notes = []
    bottom_ft = rows["Final bottom elevation"]["value"]
    width_ft = rows["Final bottom width"]["value"]
    z = rows["Left side slope"]["value"]
    tf = rows["Full formation time"]["value"]
    if z is None:
        z = 0.5
        notes.append("The breach method gives no side slope; 0.5H:1V was used (HEC-RAS "
                     "practice for earthen dams is 0.5 to 1).")
    if width_ft is None:
        width_ft = 3.0 * dam.dam_height_ft
        notes.append("The breach method gives no width; 3 x dam height ({:.0f} {}) was used."
                     .format(units.from_ft(width_ft), units.len_name))
    if tf is None:
        tf = p.get("rise_time_hr") or 1.0
        notes.append("The breach method gives no formation time; {:.2f} h (the plugin's "
                     "hydrograph rise) was used.".format(tf))
    mode = str(p.get("failure_mode") or "").lower()
    piping = mode == "piping"
    trig = p.get("breach_trigger")
    if trig in ("peak", "wse", "time"):
        trig_ft = p.get("pool_at_breach_ft")
        if trig == "time":
            notes.append("The run breached at a set time; HEC-RAS here breaches when the pool "
                         "reaches the level it had at that time ({} ft).".format(trig_ft))
    else:
        trig_ft = p.get("starting_wse_ft") or dam.top_of_dam_ft
    if trig_ft is None:
        trig_ft = dam.top_of_dam_ft
    wse0_ft = p.get("starting_wse_ft") or brmod.starting_wse_ft(dam)[0]
    b = {"bottom_elev": units.from_ft(bottom_ft), "bottom_width": units.from_ft(width_ft),
         "left_slope": float(z), "right_slope": float(z), "formation_hr": float(tf),
         "piping": piping, "piping_coef": float(p.get("pipe_cd") or 0.5),
         "piping_elev": None, "weir_coef": units.weir_coef(hecras.BREACH_WEIR_COEF),
         "trigger_wse": None}
    if piping:
        pe = rows.get("Initial piping elevation", {}).get("value")
        if pe is None:
            hb = dam.top_of_dam_ft - bottom_ft
            pe = bottom_ft + 0.5 * hb
            notes.append("Initial piping elevation: half way up the breach ({:.1f} {}).".format(
                units.from_ft(pe), units.len_name))
        b["piping_elev"] = units.from_ft(pe)
    top_u = units.from_ft(dam.top_of_dam_ft)
    hb_u = top_u - b["bottom_elev"]
    max_bottom = crest_length_u - 2.0 * z * hb_u
    if crest_length_u > 0 and b["bottom_width"] > max_bottom:
        notes.append("The breach bottom width ({:.0f}) does not fit in the dam line ({:.0f}) "
                     "with its side slopes; it was limited to {:.0f}.".format(
                         b["bottom_width"], crest_length_u, max(max_bottom, 0.0)))
        b["bottom_width"] = max(max_bottom, 0.0)
    return b, wse0_ft, trig_ft, notes


# ============================================================================
# v3.12.0: culverts, bridges and notches inside the 2D area (numpy only)
# ============================================================================
STRUCT_FIELDS = ("width_m", "height_m", "barrels", "shape", "invert_up_m", "invert_dn_m",
                 "manning", "losses", "name")


def run_structures(report):
    """The run's culverts, bridges and notches as structures.Structure (line in the run's
    model CRS, upstream end first; sizes and inverts in metres) and where they come from:
    as the run used them ('structures_used', written by 3.12.0 runs after the inventory
    inverts were checked against the LiDAR), else as they were given to the run
    ('settings' > 'structures', in every run report since structures came in)."""
    raw = report.get("structures_used")
    src = "the run's culvert / bridge layer, as the run used it (inverts after its LiDAR check)"
    if not raw:
        raw = (report.get("settings") or {}).get("structures")
        src = ("the run's culvert / bridge layer as it was given to the run (a run before "
               "3.12.0 does not record the inverts its LiDAR check settled on)")
    out = []
    for d in raw or []:
        d = d if isinstance(d, dict) else {}
        try:
            line = np.asarray(d.get("line"), dtype=float).reshape(-1, 2)
        except (TypeError, ValueError):
            line = np.zeros((0, 2))
        out.append(structmod.Structure(str(d.get("kind") or ""), line,
                                       **{k: d[k] for k in STRUCT_FIELDS if k in d}))
    return out, (src if out else "")


def _fill_nan(v):
    v = np.asarray(v, dtype=float)
    ok = np.isfinite(v)
    if ok.all() or not ok.any():
        return v
    i = np.arange(len(v))
    return np.interp(i, i[ok], v[ok])


def _profile(st, el, tol, max_points):
    s2, e2 = simplify_profile(st, el, tol, max_points)
    return [(round(float(a), 2), round(float(b), 2)) for a, b in zip(s2, e2)]


def structure_crest(line, ground, step, flat=0.0):
    """Where a structure line crosses its embankment: the highest ground along it (sampled
    every `step`; the middle of the top where it is level within `flat`). Returns (point,
    index, along, z, dense points, notes), or None without terrain under the line."""
    dense = cond.densify(np.asarray(line, dtype=float).reshape(-1, 2), step)
    along = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(dense, axis=0).T))])
    zl = ground(dense)
    if not np.isfinite(zl).any():
        return None
    notes = []
    k = int(np.nanargmax(zl))
    top = np.isfinite(zl) & (zl >= zl[k] - flat)
    i0 = i1 = k
    while i0 > 0 and top[i0 - 1]:
        i0 -= 1
    while i1 < len(top) - 1 and top[i1 + 1]:
        i1 += 1
    k = int(np.argmin(np.abs(along - 0.5 * (along[i0] + along[i1]))))
    if along[k] < step or along[k] > along[-1] - step:
        k = int(np.argmin(np.abs(along - 0.5 * along[-1])))
        notes.append("its line crosses no high ground (embankment) in the terrain; the "
                     "connection is at the middle of the line")
    return dense[k], k, along, zl, dense, notes


def structure_connection(s, line, ground, perimeter, cell, step, units, name, avoid=(),
                         neighbours=()):
    """One Runner structure (structures.Structure) as a connection inside the 2D area.

    `line` is the structure's line in the model CRS (upstream end first), `ground(xy)` the
    terrain the model uses (project units, NaN where there is none), `perimeter` the 2D area,
    `cell` its cell size, `step` the terrain cell (sampling step), `avoid` [(name, line)] the
    connection lines already placed, `neighbours` [(name, crest point)] of the other
    structures. Returns (dict, None), or (None, why it is left out).

    * The embankment crest is the highest ground along the structure line; the connection
      line crosses it there, perpendicular to the structure line, drawn left to right looking
      downstream, and runs along the road each way until the ground is STRUCT_RISE_FT above
      the crest at the structure, or STRUCT_MAX_HALF_FT, keeping STRUCT_EDGE_CELLS cells inside
      the 2D area
      and half way (less half a cell) from the crest of any other structure, so that two
      structures on one road get two connections that do not touch.
    * The weir is the terrain along that line (the road crest; HEC-RAS's weir equation, not
      the 2D equations, takes the flow over it).
    * A culvert is one culvert group of the Runner's size, in project units: a box (shape 2,
      chart 8 scale 1) or a concrete pipe (shape 1, chart 1 scale 1), its line's length,
      Manning's n, the inverts the run used or else the terrain at the line's ends, and
      barrels side by side about the crest station.
    * A bridge or a notch is a gap in the weir as wide as its span (times barrels) down to the
      invert (the structure's, else the lowest ground near each end of its line, as the
      Runner's notch cut): an open cut, as LISFLOOD-FP had it - no deck, piers or pressure
      flow."""
    line = np.asarray(line, dtype=float).reshape(-1, 2)
    kind = str(s.kind)
    culvert = kind == structmod.KIND_CULVERT
    if kind not in structmod.KINDS:
        return None, "unknown kind '{}'".format(kind)
    if len(line) < 2 or not np.isfinite(line).all() or cond.polyline_length(line) <= 0:
        return None, "needs a line with two ends"
    pipe = str(s.shape or "").lower() == "pipe"
    sizes = [s.width_m] + ([s.height_m] if culvert and not pipe else [])
    if any(v is None or not math.isfinite(float(v)) or float(v) <= 0 for v in sizes):
        return None, "no size (span{})".format(" / rise" if culvert and not pipe else "")
    nb = max(1, int(s.barrels or 1))
    per = np.asarray(perimeter, dtype=float)
    closed = list(map(tuple, per)) + [tuple(per[0])]
    if not cond.points_in_polygon(line[:, 0], line[:, 1], per).all():
        return None, "outside the downstream 2D area"
    step = max(float(step), 1e-6)
    span = units.length(s.width_m)
    group = nb * span * (1.1 if culvert else 1.0)      # width of the barrels or of the opening
    # --- the embankment crest along the structure line -----------------------------
    tol = units.from_ft(0.1)
    crest = structure_crest(line, ground, step, tol)
    if crest is None:
        return None, "no terrain under its line"
    C, k, along, zl, dense, notes = crest
    total = float(along[-1])
    zc = float(zl[k]) if np.isfinite(zl[k]) else float(np.nanmax(zl))
    t = dense[min(k + 1, len(dense) - 1)] - dense[max(k - 1, 0)]
    d = t / (np.hypot(*t) or 1.0)
    n_left = np.array([-d[1], d[0]])
    # --- connection line: along the road each way ------------------------------------
    rise = units.from_ft(STRUCT_RISE_FT)
    min_half = 0.5 * group + step
    ts = np.arange(step, max(units.from_ft(STRUCT_MAX_HALF_FT), min_half) + 0.5 * step, step)
    halves = []
    for side in (1.0, -1.0):
        pts = C[None, :] + side * n_left[None, :] * ts[:, None]
        z = ground(pts)
        ok = cond.points_in_polygon(pts[:, 0], pts[:, 1], per) & np.isfinite(z)
        ok &= point_segment_distance(pts[:, 0], pts[:, 1], closed) >= STRUCT_EDGE_CELLS * cell
        bad = np.nonzero(~ok)[0]
        end = int(bad[0]) if len(bad) else len(ts)            # points before this are usable
        why = "too close to the edge of the 2D area for a connection line across it"
        for other, q in neighbours:                         # stay on this structure's side
            near = np.nonzero(np.hypot(pts[:, 0] - q[0], pts[:, 1] - q[1]) <= ts + cell)[0]
            if len(near) and near[0] < end:
                end = int(near[0])
                why = ("too close to {} for a connection line of its own; add it to that "
                       "connection in HEC-RAS".format(other))
        if end == 0 or ts[end - 1] < min_half - 1e-9:
            return None, why
        high = np.nonzero(z[:end] >= zc + rise)[0]
        half = float(ts[high[0]]) if len(high) else float(ts[end - 1])
        halves.append(max(half, min_half))
    A = C + n_left * halves[0]                 # left end looking downstream
    B = C - n_left * halves[1]
    length = halves[0] + halves[1]
    sc = halves[0]                             # station of the crest point
    probe = A[None, :] + (B - A)[None, :] * np.linspace(0.0, 1.0, 64)[:, None]
    for other, oline in avoid:
        if point_segment_distance(probe[:, 0], probe[:, 1], oline).min() < cell:
            return None, ("its connection line would cross or run along the one of {} (the same "
                          "road?); add it to that connection in HEC-RAS".format(other))
    # --- weir: the terrain along the connection line --------------------------------
    n = int(min(MAX_WEIR_POINTS * 8, max(10, math.ceil(length / step) + 1)))
    st = np.linspace(0.0, length, n)
    el = _fill_nan(ground(A[None, :] + (B - A)[None, :] * (st / length)[:, None]))
    # top width of the road: the crest along the structure line, within 1 ft of its top
    top = np.isfinite(zl) & (zl >= zc - units.from_ft(1.0))
    i0 = i1 = k
    while i0 > 0 and top[i0 - 1]:
        i0 -= 1
    while i1 < len(top) - 1 and top[i1 + 1]:
        i1 += 1
    weir_width = min(max(float(along[i1] - along[i0]), units.from_ft(10.0)), total)
    out = {"name": name, "kind": kind, "source_name": s.name or kind, "line": [tuple(A), tuple(B)],
           "structure_line": [tuple(p) for p in line], "crest": tuple(C), "crest_elev": zc,
           "weir_width": weir_width, "weir_coef": units.weir_coef(STRUCT_WEIR_COEF),
           "culvert": None, "gap": None, "notes": notes}
    label = re.sub(r"[\s,=]+", " ", str(s.name or kind)).strip()
    if culvert:
        out["weir"] = _profile(st, el, tol, MAX_WEIR_POINTS)
        zends = ground(line[[0, -1]])
        inv = [units.length(v) if v is not None else float(z)
               for v, z in zip((s.invert_up_m, s.invert_dn_m), zends)]
        if not all(math.isfinite(v) for v in inv):
            return None, "no terrain at an end of its line and no invert given"
        given = [v is not None for v in (s.invert_up_m, s.invert_dn_m)]
        shape, chart, scale, ke = CULVERT_TYPES["pipe" if pipe else "box"]
        rise_u = span if pipe else units.length(s.height_m)
        spacing = 1.1 * span
        stations = [round(sc + (i - 0.5 * (nb - 1)) * spacing, 2) for i in range(nb)]
        out["culvert"] = {"shape": shape, "chart": chart, "scale": scale, "entrance": ke,
                          "exit": CULVERT_EXIT_LOSS, "rise": rise_u, "span": span,
                          "length": round(cond.polyline_length(line), 2),
                          "n": float(s.manning or 0.013), "inv_up": round(inv[0], 2),
                          "inv_dn": round(inv[1], 2), "barrels": nb, "stations": stations,
                          "inverts_from": " / ".join("run" if g else "terrain" for g in given)}
        road = el[np.abs(st - sc) <= 0.5 * group + step]
        if len(road) and float(road.min()) < max(inv) + rise_u - tol:
            notes.append("the road in the terrain at the culvert ({:.1f} {u}) is lower than the "
                         "top of the barrel ({:.1f} {u}): the DEM may be cut through the road "
                         "here, or the size or inverts are off; check the weir and the culvert"
                         .format(float(road.min()), max(inv) + rise_u, u=units.len_name))
        if inv[1] - inv[0] >= units.from_ft(STRUCT_ADVERSE_FT):
            notes.append("adverse culvert: the outlet invert ({:.2f} {u}) is {:.2f} {u} above the "
                         "inlet ({:.2f} {u}) as the run had it (inverts from: {}); kept, the "
                         "headwater side is the first end of the structure line; check the "
                         "inverts in the field"
                         .format(inv[1], inv[1] - inv[0], inv[0], out["culvert"]["inverts_from"],
                                 u=units.len_name))
        out["desc"] = "Culvert {} from the ND Dam Breach Runner".format(label)
    else:
        # the gap floor: the invert at the crest, from the structure's inverts or the lowest
        # ground near each end of its line (the Runner's notch rule)
        near = max(span, 2.0 * step)
        lo, hi = zl[along <= near], zl[along >= total - near]
        inv = [units.length(v) if v is not None else (float(np.nanmin(z)) if np.isfinite(z).any()
                                                       else float("nan"))
               for v, z in zip((s.invert_up_m, s.invert_dn_m), (lo, hi))]
        if not all(math.isfinite(v) for v in inv):
            return None, "no terrain near an end of its line and no invert given"
        floor = inv[0] + (inv[1] - inv[0]) * float(along[k]) / (total or 1.0)
        a, b = sc - 0.5 * group, sc + 0.5 * group
        za, zb = float(np.interp(a, st, el)), float(np.interp(b, st, el))
        mid = (st > a) & (st < b)
        per_part = MAX_WEIR_POINTS // 3
        left = _profile(np.r_[st[st < a], a], np.r_[el[st < a], za], tol, per_part)
        gap = _profile(np.r_[a, st[mid], b], np.minimum(np.r_[za, el[mid], zb], floor), tol,
                       per_part)
        right = _profile(np.r_[b, st[st > b]], np.r_[zb, el[st > b]], tol, per_part)
        weir = []
        for p in left + gap + right:                # vertical walls: two points at a station
            if not weir or p != weir[-1]:
                weir.append(p)
        out["weir"] = weir
        out["gap"] = {"width": group, "floor": round(floor, 2),
                      "inverts_from": " / ".join("run" if v is not None else "terrain"
                                                 for v in (s.invert_up_m, s.invert_dn_m))}
        out["desc"] = "{} {} from the ND Dam Breach Runner (open gap)".format(
            kind.capitalize(), label)
    return out, None


# 3.10.75: the characters a connection name may have. HEC-RAS 6.6 refuses "," and "=" (the
# .g01 record separators) and "/" (DWR-2801 NickReview, 2026-10-24: 57 of 126 connections
# named from OpenStreetMap ids, 'OSM way/254643...', 'OSM node/1185946', stopped the plan with
# "the connection name has fore slashes(/) in it"); the names it took there had only letters,
# digits, spaces and ( ) - . so anything else becomes "_" (as the hand fix there did).
CONN_NAME_MAX = 16
CONN_NAME_BAD = re.compile(r"[^A-Za-z0-9 ()._\-]")


def connection_name_base(text):
    """A structure name as HEC-RAS takes it (no length limit yet): whitespace runs to one space,
    each run of other refused characters ("/", ",", "=", ...) to one "_"."""
    base = re.sub(r"\s+", " ", str(text or "")).strip()
    base = re.sub(r"_+", "_", CONN_NAME_BAD.sub("_", base)).strip(" _")
    return base or "Structure"


def connection_name(text, used):
    """A unique HEC-RAS connection name (at most CONN_NAME_MAX = 16 characters, only those
    CONN_NAME_BAD allows) from a structure name; a name already in `used` (case-insensitive,
    as HEC-RAS compares them) gets " 2", " 3", ... in its last characters. The original stays
    in the connection's "source_name" (README table, layout layer, report)."""
    base = connection_name_base(text)
    taken = {str(u).strip().lower() for u in used}
    nm, i = base[:CONN_NAME_MAX].strip(), 1
    while nm.lower() in taken:
        i += 1
        suffix = " {}".format(i)
        nm = base[:CONN_NAME_MAX - len(suffix)].rstrip() + suffix
    used.add(nm)
    return nm


# ============================================================================
# Building the model from a run (GDAL / OGR)
# ============================================================================
ND_LON = (-104.06, -96.55)
ND_LAT = (45.93, 49.01)
# NAD83 / North Dakota North (ft) and South (ft): North Dakota's State Plane is in
# international feet
ND_NORTH_FT, ND_SOUTH_FT = "EPSG:2265", "EPSG:2266"
ND_ZONE_SPLIT_LAT = 47.33     # approximate; the zone line follows county lines

DEFAULTS = {"side_buffer_ft": 1000.0, "end_buffer_ft": 2640.0, "cell_ft": 0.0,
            "target_cells": 100000, "equation": EQ_SWE, "base_flow_cfs": 0.0,
            "terrain_cell": 0.0, "max_terrain_cells": 150e6, "extent_depth_ft": 0.0,
            "manning_n": None, "courant": None,
            # adaptive time step, merged over COURANT_DEFAULTS ({"adaptive": False} = the
            # fixed Computation Interval); its 'max' defaults to the Courant target above
            "time_step": None,
            "structures": True,                 # v3.12.0: the run's culverts and bridges
            # 3.10.56: the culverts and bridges of another run (its dam folder or
            # run_report.json, e.g. the ANUGA run), faces on the connection lines, and the
            # weirs / culvert inverts no lower than their cells
            "structures_from": None, "structure_breaklines": True, "structure_cell_fit": True,
            # 3.10.75: a connection line with (almost) no cell faces along it stops the build
            # ('fail'), or is only reported ('warn'); under 90 % is always reported
            "structure_align_check": "fail",
            # TIER 2 fix: how far a rejected inflow point may be walked downstream
            "inflow_search_ft": 3000.0,
            # v3.10.22 (see the constants above build_model's helpers)
            "breach_start": None,               # None: 'time' (sunny day), 'wse' (flood)
            "breach_warmup_hr": BREACH_WARMUP_HR,
            "hec_breach_mode": "match_plugin",  # or 'failure_mode' (HEC Piping for piping)
            "warmup_hold": True,                # spillway: hold the pool during the warm-up
            "tolerances": "default",            # or 'tight', or a dict (TOLERANCES keys)
            "mesh_seeds": None,                 # None: NDBREACH_HECRAS_MESH_SEEDS, else 'grid'
            "breaklines": True,                 # dam, breach and channel breaklines (in the points)
            # v3.10.22: the dam line along the crest traced from the DEM ('crest') or the
            # straight line through the dam point across the flow ('straight', the old line);
            # the breach centre along it: breach_at ('auto' = the deepest dam section, 'toe',
            # 'lowest_crest', 'inflow' = the run's inflow point projected, the old rule) or
            # breach_station_ft (from the dam line's first end); dam_line_xy: a dam line of
            # your own (model CRS), which wins over the trace (see hecras_crest)
            "dam_axis": "crest", "breach_at": "auto", "breach_station_ft": None,
            "dam_line_xy": None,
            # 3.10.76: bands of small cells along roads, rail, structures and breaklines
            # (hecras_bands; None = the NDBREACH_BAND* variables, else hecras_bands.DEFAULTS:
            # on, 3 rows of 20 ft each side, graded 1.5:1, wet area + 500 ft)
            "line_bands": None, "band_cell_ft": None, "band_rows": None, "band_grading": None,
            "band_classes": None, "band_extent": None, "band_wet_pad_ft": None,
            "band_lines": None}


def _ogr():
    from osgeo import gdal, ogr, osr
    return gdal, ogr, osr


def srs_from(text):
    _, _, osr = _ogr()
    s = osr.SpatialReference()
    if s.SetFromUserInput(str(text)) != 0:
        raise HecRasModelError("not a coordinate system: {}".format(str(text)[:80]))
    try:
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return s


def transformer(src, dst):
    """f(xy array (n, 2)) -> (n, 2) from srs `src` to srs `dst`."""
    _, _, osr = _ogr()
    if src.IsSame(dst):
        return lambda a: np.asarray(a, dtype=float).reshape(-1, 2)
    ct = osr.CoordinateTransformation(src, dst)

    def f(a):
        a = np.asarray(a, dtype=float).reshape(-1, 2)
        if not len(a):
            return a
        r = ct.TransformPoints([(float(x), float(y)) for x, y in a])
        return np.asarray([(p[0], p[1]) for p in r], dtype=float)
    return f


def crs_units(srs):
    """(Units, metres per CRS unit) of a projected CRS; error for a geographic one."""
    if not srs.IsProjected():
        raise HecRasModelError("the HEC-RAS model needs a projected coordinate system "
                               "(feet or metres), not a geographic one")
    u = srs.GetLinearUnits()
    if abs(u - FT) < 1e-6 or abs(u - US_FT) < 1e-6:
        return Units(False), u
    if abs(u - 1.0) < 1e-9:
        return Units(True), u
    raise HecRasModelError("the model coordinate system must be in feet or metres (its unit "
                           "is {} m)".format(u))


def choose_crs(requested, dem_wkt, lonlat, work_wkt):
    """(srs, why). requested > the DEM's CRS when it is in feet > ND State Plane (US ft) for
    a dam in North Dakota > the run's metric CRS."""
    if requested:
        return srs_from(requested), "chosen"
    if dem_wkt:
        s = srs_from(dem_wkt)
        if s.IsProjected() and abs(s.GetLinearUnits() - 1.0) > 1e-6:
            try:
                crs_units(s)
                return s, "the DEM's coordinate system (feet)"
            except HecRasModelError:
                pass
    if lonlat is not None:
        lon, lat = lonlat
        if ND_LON[0] <= lon <= ND_LON[1] and ND_LAT[0] <= lat <= ND_LAT[1]:
            code = ND_NORTH_FT if lat >= ND_ZONE_SPLIT_LAT else ND_SOUTH_FT
            return srs_from(code), ("North Dakota State Plane {} zone, NAD83, international feet "
                                    "({}); "
                                    "the zone is picked by latitude, choose the other one "
                                    "if your county is in it".format(
                                        "North" if code == ND_NORTH_FT else "South", code))
    if work_wkt:
        return srs_from(work_wkt), "the run's model coordinate system (metres)"
    raise HecRasModelError("no coordinate system for the HEC-RAS model: choose one")


def _epsg_text(srs):
    code = srs.GetAuthorityCode(None)
    name = srs.GetAuthorityName(None)
    return "{}:{}".format(name, code) if code and name else srs.GetName()


def _poly_from_mask(mask, x0, y0, cell, srs):
    """OGR (multi)polygon of the True cells of a north-up mask."""
    gdal, ogr, _ = _ogr()
    ny, nx = mask.shape
    ds = gdal.GetDriverByName("MEM").Create("", nx, ny, 1, gdal.GDT_Byte)
    ds.SetGeoTransform((x0, cell, 0.0, y0, 0.0, -cell))
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(mask.astype(np.uint8))
    vds = ogr.GetDriverByName("Memory").CreateDataSource("m")
    lyr = vds.CreateLayer("p", srs, ogr.wkbPolygon)
    lyr.CreateField(ogr.FieldDefn("v", ogr.OFTInteger))
    gdal.Polygonize(ds.GetRasterBand(1), ds.GetRasterBand(1), lyr, 0, [], callback=None)
    out = ogr.Geometry(ogr.wkbMultiPolygon)
    for feat in lyr:
        if feat.GetField(0) == 1:
            out.AddGeometry(feat.GetGeometryRef().Clone())
    return out.UnionCascaded() if out.GetGeometryCount() else None


def _rings(geom):
    """Exterior rings of a (multi)polygon as numpy arrays (not closed), largest first."""
    from osgeo import ogr
    polys = []
    t = ogr.GT_Flatten(geom.GetGeometryType())
    if t == ogr.wkbPolygon:
        polys = [geom]
    elif t in (ogr.wkbMultiPolygon, ogr.wkbGeometryCollection):
        polys = [geom.GetGeometryRef(i) for i in range(geom.GetGeometryCount())
                 if ogr.GT_Flatten(geom.GetGeometryRef(i).GetGeometryType()) == ogr.wkbPolygon]
    out = []
    for p in polys:
        r = p.GetGeometryRef(0)
        a = np.asarray(r.GetPoints(), dtype=float)[:, :2]
        if len(a) > 1 and np.allclose(a[0], a[-1]):
            a = a[:-1]
        out.append((abs(p.GetArea()), a, p))
    out.sort(key=lambda t: -t[0])
    return out


def _polygon(pts):
    from osgeo import ogr
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in pts:
        ring.AddPoint_2D(float(x), float(y))
    ring.AddPoint_2D(float(pts[0][0]), float(pts[0][1]))
    p = ogr.Geometry(ogr.wkbPolygon)
    p.AddGeometry(ring)
    return p


def _line(pts):
    from osgeo import ogr
    g = ogr.Geometry(ogr.wkbLineString)
    for x, y in pts:
        g.AddPoint_2D(float(x), float(y))
    return g


def _point(x, y):
    from osgeo import ogr
    g = ogr.Geometry(ogr.wkbPoint)
    g.AddPoint_2D(float(x), float(y))
    return g


def _warp(path, srs, cell, bounds=None, alg="bilinear", src_nodata=None):
    """A raster in the model CRS on a north-up grid: (array with NaN, x0, ytop)."""
    gdal, _, _ = _ogr()
    from ..damsuite import gdalsafe as GS
    kw = dict(format="MEM", dstSRS=srs.ExportToWkt(), xRes=cell, yRes=cell,
              resampleAlg=alg, dstNodata=-9999.0, outputType=gdal.GDT_Float32,
              multithread=True, targetAlignedPixels=True)
    if bounds is not None:
        kw["outputBounds"] = bounds
    if src_nodata is not None:
        kw["srcNodata"] = src_nodata
    from .warp_opts import no_overviews, no_vshift   # 3.10.43: no source overviews
    # 3.10.89: values as stored (no vertical shift); the caller applies the DEM's unit
    out = GS.checked(gdal.Warp("", path, options=gdal.WarpOptions(
        **no_vshift(gdal, no_overviews(gdal, kw)))), "reprojecting {}".format(path))
    a = out.GetRasterBand(1).ReadAsArray().astype(np.float64)
    a[a == -9999.0] = np.nan
    gt = out.GetGeoTransform()
    return a, gt[0], gt[3]


def _tif_window(path, bounds, srs, max_cells=2000):
    """v3.12.0: the model's terrain GeoTIFF over `bounds` as (array with NaN, x0, ytop, cell),
    read as it is (the terrain HEC-RAS will have, not resampled again) when the window is at
    most `max_cells` across, else warped to a coarser cell."""
    gdal, _, _ = _ogr()
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    cell = abs(gt[1])
    if max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / cell > max_cells:
        ds = None
        scs = max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / max_cells
        a, x0, y0 = _warp(path, srs, scs, bounds=bounds, alg="bilinear")
        return a, x0, y0, scs
    c0 = max(0, int(math.floor((bounds[0] - gt[0]) / cell)))
    c1 = min(ds.RasterXSize, int(math.ceil((bounds[2] - gt[0]) / cell)))
    r0 = max(0, int(math.floor((gt[3] - bounds[3]) / cell)))
    r1 = min(ds.RasterYSize, int(math.ceil((gt[3] - bounds[1]) / cell)))
    if c1 - c0 < 2 or r1 - r0 < 2:
        ds = None
        return np.full((2, 2), np.nan), bounds[0], bounds[3], cell
    b = ds.GetRasterBand(1)
    a = b.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float64)
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    ds = None
    return a, gt[0] + c0 * cell, gt[3] - r0 * cell, cell


def _sample(a, x0, y0, cell, xy):
    """Bilinear sample of a north-up array at points (NaN outside)."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    fx = (xy[:, 0] - x0) / cell - 0.5
    fy = (y0 - xy[:, 1]) / cell - 0.5
    ny, nx = a.shape
    c0 = np.clip(np.floor(fx).astype(int), 0, nx - 2) if nx > 1 else np.zeros(len(xy), int)
    r0 = np.clip(np.floor(fy).astype(int), 0, ny - 2) if ny > 1 else np.zeros(len(xy), int)
    tx = np.clip(fx - c0, 0, 1)
    ty = np.clip(fy - r0, 0, 1)
    c1 = np.minimum(c0 + 1, nx - 1)
    r1 = np.minimum(r0 + 1, ny - 1)
    v = (a[r0, c0] * (1 - tx) * (1 - ty) + a[r0, c1] * tx * (1 - ty) +
         a[r1, c0] * (1 - tx) * ty + a[r1, c1] * tx * ty)
    # where a corner is NaN, fall back to the nearest cell
    bad = ~np.isfinite(v)
    if bad.any():
        rr = np.clip(np.round(fy).astype(int), 0, ny - 1)
        cc = np.clip(np.round(fx).astype(int), 0, nx - 1)
        v[bad] = a[rr[bad], cc[bad]]
    out = (fx < -0.5) | (fy < -0.5) | (fx > nx - 0.5) | (fy > ny - 0.5)
    v[out] = np.nan
    return v


def _flood_fill(mask, seed_rc):
    """Cells of `mask` 4-connected to the seed (numpy only)."""
    r, c = seed_rc
    reach = np.zeros_like(mask, dtype=bool)
    if not mask[r, c]:
        return reach
    reach[r, c] = True
    while True:
        grown = reach.copy()
        grown[1:, :] |= reach[:-1, :]
        grown[:-1, :] |= reach[1:, :]
        grown[:, 1:] |= reach[:, :-1]
        grown[:, :-1] |= reach[:, 1:]
        grown &= mask
        if (grown == reach).all():
            return reach
        reach = grown


def _z_factor(report, dem_path):
    """Metres per DEM elevation unit: the run's record, else the unit the DEM records
    (gdal_io.z_to_m_factor 'auto' = resolve_dem_z; 3.10.89: no guess from the CRS)."""
    note = str(report.get("dem_elevation_units") or "").lower()
    if report.get("dem_z_to_m"):
        return float(report["dem_z_to_m"]), report.get("dem_elevation_units") or ""
    if note.startswith("us survey feet"):
        return US_FT, note
    if note.startswith("feet"):
        return FT, note
    if note.startswith("metres"):
        return 1.0, note
    from . import gdal_io
    return gdal_io.z_to_m_factor(dem_path)


def _work_crs(report, model_dir):
    if report.get("work_crs"):
        return report["work_crs"]
    qa = os.path.join(model_dir, "qa_dam_inflow_and_flow_path.geojson")
    if os.path.exists(qa):
        try:
            with open(qa, encoding="utf-8") as f:
                return json.load(f)["crs"]["properties"]["name"]
        except (KeyError, ValueError, TypeError, OSError):
            pass
    return None


def _flow_path(model_dir):
    qa = os.path.join(model_dir, "qa_dam_inflow_and_flow_path.geojson")
    try:
        with open(qa, encoding="utf-8") as f:
            for ft in json.load(f)["features"]:
                if ft["geometry"]["type"] == "LineString":
                    return np.asarray(ft["geometry"]["coordinates"], dtype=float)
    except (KeyError, ValueError, TypeError, OSError):
        pass
    return None


def _dam_lonlat(dam_folder):
    p = os.path.join(dam_folder, "dam.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        pt = d.get("point") or d.get("location") or {}
        if pt.get("lon") is not None and pt.get("lat") is not None:
            return float(pt["lon"]), float(pt["lat"])
    except (ValueError, OSError, AttributeError, TypeError):
        pass
    return None


def inflow_check(ground_m, top_m, height_m):
    """v3.10.16: why a run's breach inflow point is not below the dam ('' when it is). The
    inflow must sit at least a quarter of the dam height below the top of dam (the Runner's own
    rule is half; a quarter allows for a coarse DEM). Unknown ground is accepted."""
    if ground_m is None or not np.isfinite(ground_m) or not height_m:
        return ""
    if ground_m > top_m - 0.25 * height_m:
        return ("the ground there is {:.1f} ft, {} the {:.1f} ft top of dam".format(
            ground_m / FT, "{:.1f} ft above".format((ground_m - top_m) / FT) if ground_m >= top_m
            else "only {:.1f} ft below".format((top_m - ground_m) / FT), top_m / FT))
    return ""


INFLOW_FAR_FT = 1312.0       # 400 m: the Runner never places the inflow farther from the dam


def inflow_side_problems(dam_xy, inflow_xy, path=None, far=None, unit=""):
    """Why the run's inflow point looks like a dam-point / downstream-side problem: the flow
    line from it runs back over the dam point, the point is behind the dam relative to the
    flow line's direction, or it is farther from the dam point than the Runner ever puts it
    (`far`, model units). [] when none."""
    out = []
    P, Q = np.asarray(dam_xy, float), np.asarray(inflow_xy, float)
    d0 = float(np.hypot(*(Q - P)))
    if far is not None and d0 > far:
        out.append("the inflow point is {:,.0f} {u} from the dam point (the Runner places it "
                   "within {:,.0f} {u})".format(d0, far, u=unit))
    if path is not None and len(path) >= 2 and d0 > 1e-6:
        pa = np.asarray(path, float)
        dmin = float(np.hypot(pa[1:, 0] - P[0], pa[1:, 1] - P[1]).min())
        if dmin < 0.5 * d0:
            out.append("the flow line from the inflow point runs back over the dam point "
                       "({:,.0f} {u} from it)".format(dmin, u=unit))
        seg = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pa, axis=0).T))])
        far_i = int(np.searchsorted(seg, min(seg[-1], 3.0 * d0 + 1e-9)))
        v = pa[min(far_i, len(pa) - 1)] - pa[0]
        if float(np.hypot(*v)) > 1e-6 and float(np.dot(Q - P, v)) < 0:
            out.append("the inflow point is behind the dam relative to the flow line's "
                       "direction")
    return out


def inflow_drop_needed_m(top_m, height_m):
    """The highest ground (m) an inflow point may have: a quarter of the dam height below the
    top of dam (inflow_check)."""
    return float(top_m) - 0.25 * float(height_m)


def _walk_line(pts, start, step):
    """Points every `step` along polyline `pts` downstream of its point nearest `start`, with
    their distance along the line from there: [(xy, dist)]."""
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return []
    dens = np.asarray(densify([tuple(p) for p in pts], step))
    k = int(np.argmin(np.hypot(dens[:, 0] - start[0], dens[:, 1] - start[1])))
    seg = np.hypot(*np.diff(dens[k:], axis=0).T) if len(dens) - k > 1 else np.zeros(0)
    dist = np.concatenate([[0.0], np.cumsum(seg)])
    return [(dens[k + i], float(dist[i])) for i in range(len(dist))]


def _steepest_descent(start, ground, step, max_dist, n_dirs=16):
    """[(xy, dist)] from `start` downhill on `ground(xy (n,2)) -> z` in steps of `step`
    (looking up to 4 steps away across flats), until max_dist or no lower ground."""
    ang = np.linspace(0.0, 2 * np.pi, n_dirs, endpoint=False)
    unit = np.column_stack([np.cos(ang), np.sin(ang)])
    p = np.asarray(start, float)
    z = float(ground(p[None, :])[0])
    out, dist = [(p, 0.0)], 0.0
    while dist < max_dist and np.isfinite(z):
        moved = False
        for r in (step, 2 * step, 4 * step):
            cand = p + unit * r
            zc = ground(cand)
            if not np.isfinite(zc).any():
                continue
            i = int(np.nanargmin(zc))
            if zc[i] < z - 1e-6:
                p, z, dist, moved = cand[i], float(zc[i]), dist + r, True
                out.append((p, dist))
                break
        if not moved:
            break
    return out


def relocate_inflow(start, ground, need_m, dam_xy, min_off, max_dist, step, path=None,
                    diag=None):
    """TIER 2 fix: an inflow point downstream of a rejected one. Walks from `start` along the
    run's flow path (`path`, the Runner's downstream trace), else down the steepest descent
    of the DEM, to the first point whose ground (m, `ground(xy)`) is at most `need_m` and
    that is at least `min_off` from the dam point (off the embankment), within `max_dist`
    of walking. Returns {'xy', 'dist', 'ground_m', 'how'} or None; `diag` (a dict) gets the
    lowest ground seen off the embankment ('lowest_m', 'lowest_xy', 'lowest_dist')."""
    tries = []
    if path is not None and len(path) > 1:
        tries.append(("the run's downstream flow path", _walk_line(path, start, step)))
    tries.append(("the steepest descent of the DEM", _steepest_descent(start, ground, step,
                                                                        max_dist)))
    P = np.asarray(dam_xy, float)
    for how, cand in tries:
        cand = [(xy, d) for xy, d in cand if d <= max_dist + 1e-9]
        if not cand:
            continue
        z = ground(np.asarray([xy for xy, _ in cand], float))
        for (xy, d), zz in zip(cand, z):
            off = float(np.hypot(*(xy - P))) >= min_off
            if (diag is not None and off and np.isfinite(zz) and
                    zz < diag.get("lowest_m", np.inf)):
                diag.update(lowest_m=float(zz), lowest_xy=[float(v) for v in xy],
                            lowest_dist=float(d))
            if np.isfinite(zz) and zz <= need_m and off:
                return {"xy": np.asarray(xy, float), "dist": float(d), "ground_m": float(zz),
                        "how": how}
    return None


def record_inflow_status(report_path, status, srs, work, units, log=None):
    """TIER 2 fix: the HEC-RAS inflow point (used, moved downstream, rejected or missing) in the
    run's run_report.json as 'hecras_inflow', with the moved point in the run's CRS too."""
    rec = dict(status)
    if rec.get("xy_model") and work is not None:
        rec["xy"] = [round(float(v), 3) for v in transformer(srs, work)([rec["xy_model"]])[0]]
    rec["model_crs"] = _epsg_text(srs)
    rec["model_units"] = units.len_name
    try:
        with open(report_path, encoding="utf-8") as f:
            rep = json.load(f)
        rep["hecras_inflow"] = rec
        tmp = report_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, default=str)
        os.replace(tmp, report_path)
    except (OSError, ValueError) as e:
        if log:
            log("Could not record the HEC-RAS inflow point in {}: {}".format(report_path, e))
    return rec


def dam_length_ft(dam, dam_folder=None, override=None):
    """v3.10.16: (dam crest length in ft, where it came from) or (None, ''): the option, the
    run's crest length, else dam.json 'dam_length_ft'."""
    for v, src in ((override, "chosen"),
                   (getattr(dam, "crest_length_ft", None), "the run's crest length")):
        try:
            if v is not None and float(v) > 0:
                return float(v), src
        except (TypeError, ValueError):
            pass
    if dam_folder:
        try:
            with open(os.path.join(dam_folder, "dam.json"), encoding="utf-8") as f:
                v = (json.load(f).get("fields") or {}).get("dam_length_ft")
            if v is not None and float(v) > 0:
                return float(v), "dam.json dam_length_ft"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return None, ""


def trim_dam_line(A, B, P, length, min_length=0.0):
    """v3.10.16: the part of the edge A-B that is `length` long, centred on the dam point P
    projected onto it (moved along so it stays on A-B). Returns (A', B', length', note);
    the edge unchanged and note '' when no length is given or it is not shorter."""
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    full = float(np.hypot(*(B - A)))
    if not length or full <= 0 or length >= full:
        return A, B, full, ""
    L = min(full, max(float(length), float(min_length)))
    ab = (B - A) / full
    s = float(np.dot(np.asarray(P, float) - A, ab))
    s = min(max(s, 0.5 * L), full - 0.5 * L)
    A2 = A + ab * (s - 0.5 * L)
    B2 = A + ab * (s + 0.5 * L)
    return A2, B2, L, ("{:,.0f} of the {:,.0f} long edge of the 2D area across the valley, "
                       "centred on the dam point".format(L, full))


def insert_on_ring(ring, pts, tol=None):
    """v3.10.16: the ring (not closed, (n, 2)) with each point inserted as a vertex on the
    segment it lies on (within `tol`, default 1e-6 of the ring's size); a point that is
    already a vertex is not repeated."""
    ring = [tuple(map(float, p)) for p in np.asarray(ring, float)]
    a = np.asarray(ring)
    size = float(np.ptp(a, axis=0).max()) if len(a) else 1.0
    tol = tol if tol is not None else max(size * 1e-6, 1e-6)
    for q in pts:
        q = (float(q[0]), float(q[1]))
        if any(math.hypot(q[0] - p[0], q[1] - p[1]) <= tol for p in ring):
            continue
        n = len(ring)
        best = None
        for i in range(n):
            p0, p1 = ring[i], ring[(i + 1) % n]
            dd = point_segment_distance(np.array([q[0]]), np.array([q[1]]), [p0, p1])[0]
            if best is None or dd < best[0]:
                best = (dd, i)
        if best is not None and best[0] <= max(tol, size * 1e-4):
            ring.insert(best[1] + 1, q)
    return np.asarray(ring, float)


def _dem_value_m(dem_path, srs, xy, z_to_m):
    """v3.10.16: the DEM elevation (m) at a point in the model CRS, or None."""
    try:
        from ..damsuite import gdalsafe as GS
        ds = GS.open_raster(dem_path)
        dsrs = srs_from(ds.GetProjection())
        x, y = transformer(srs, dsrs)([xy])[0]
        gt = ds.GetGeoTransform()
        c = int(math.floor((x - gt[0]) / gt[1]))
        r = int(math.floor((y - gt[3]) / gt[5]))
        if not (0 <= c < ds.RasterXSize and 0 <= r < ds.RasterYSize):
            return None
        b = ds.GetRasterBand(1)
        v = float(b.ReadAsArray(c, r, 1, 1)[0, 0])
        nd = b.GetNoDataValue()
        if (nd is not None and v == nd) or not np.isfinite(v):
            return None
        return v * float(z_to_m)
    except Exception:                       # noqa: BLE001 - a check, never a failure
        return None


# ============================================================================
# v3.10.17: site checks, breach invert vs the ground below the dam, dam line without a
# recorded length, simulation window (DWR-3161 HEC-RAS test: 36 % volume error)
# ============================================================================
# A run whose dam point, dam height or storage curve do not fit the terrain gives a HEC-RAS
# model that runs but means nothing (DWR-3161: breach bottom 1816.3 ft connected to cells at
# 1840.5 ft, 4 ft of relief for a 28 ft dam, storage curve to 1816 ft over 201 ac against a
# 30 ac outline with ground no lower than 1840.4 ft). build_model stops with HecRasSiteError
# on a gross mismatch; options={"allow_bad_site": True} or the environment variable
# ALLOW_BAD_SITE=1 builds it anyway and writes the problems into the notes.
ENV_ALLOW_BAD_SITE = "ALLOW_BAD_SITE"
SITE_BREACH_BELOW_TOE_FT = 5.0     # breach bottom more than this below the toe ground: stop
SITE_MIN_RELIEF_FRAC = 0.5         # top of dam - toe ground under half the dam height: stop
SITE_NOTE_RELIEF_FRAC = (0.8, 1.5)  # outside this (but not a stop): a note
STORAGE_STOP_DEPTH_FRAC = 0.25     # top of dam - lowest ground in the outline < 1/4 H: stop
STORAGE_NOTE_DEPTH_FRAC = 0.5      # ... < 1/2 H: a note (LiDAR sees a normal pool's surface)
STORAGE_STOP_AREA_RATIO = 0.2      # outline area / curve area at the top < 0.2: stop
STORAGE_NOTE_AREA_RATIO = (0.5, 2.0)
# dam line when no length is on record: the embankment crest along the 2D area's edge across
# the valley, i.e. where the ground is within max(3 ft, 10 % of H) of the top of dam, walking
# out from the dam point and stepping over gaps up to max(2 cells, DAM_LINE_GAP_FT) (an
# emergency spillway, a low spot), at most DAM_LINE_CAP_FT
DAM_LINE_CAP_FT = 3000.0
DAM_LINE_GAP_FT = 300.0            # stretches off the crest stepped over (spillway, low spot)
# simulation window: (the later of the end of the breach hydrograph and the run's latest
# arrival) + a drain allowance of 25 % of that (at least 2 h), at most SIM_CAP_HR
SIM_CAP_HR = 48.0
SIM_MIN_HR = 6.0
SIM_DRAIN_FRAC = 0.25
SIM_DRAIN_MIN_HR = 2.0
# The site checks guard the breach model (storage area + SA/2D breach + its storage curve).
# options={"site_scope": "hydrograph"} (hecras_engine's hydrograph-inflow plan: dry reservoir,
# no breach, the run's hydrograph on a BC line at the inflow point, which has its own check)
# turns them into notes; "breach" (default) stops on them.
SITE_SCOPES = ("breach", "hydrograph")
HYDRO_END_FRAC = 0.02              # the hydrograph 'ends' when it falls below 2 % of its peak
# v3.10.17: HEC-RAS's breach shape is not the plugin's (README and manifest say so)
BREACH_SHAPE_NOTE = (
    "Breach shape: the plugin's routed breach opens as a trapezoid that deepens from the crest "
    "and widens linearly over the formation time (an orifice phase first only for the "
    "'piping with a pipe phase' method). HEC-RAS's 'Piping' failure mode in this plan starts "
    "as a pipe at the piping elevation that grows until the top collapses, then becomes the "
    "trapezoid; 'Overtopping' deepens from the top like the plugin. Expect the HEC-RAS peak and "
    "its timing to differ from the plugin's. Since Runner 3.10.22 the plan uses Overtopping for "
    "the plugin's routed (weir) methods so that the peaks compare (option hec_breach_mode; "
    "Piping only for the pipe-phase method or when asked).")


class HecRasSiteError(HecRasModelError):
    """The run's dam, terrain and storage do not fit together; `problems` lists why."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(
            "The HEC-RAS model was not built because the dam data do not match the ground: "
            + " ".join("({}) {}".format(i + 1, p) for i, p in enumerate(self.problems))
            + " Check the dam point, top of dam, dam height and storage, then re-run the "
            "Runner. To build the model anyway (for testing only), set {}=1 or the option "
            "allow_bad_site.".format(ENV_ALLOW_BAD_SITE))


def allow_bad_site(options=None, env=None):
    """True when the site checks may be overridden (option or ALLOW_BAD_SITE=1)."""
    if options and options.get("allow_bad_site"):
        return True
    v = str((os.environ if env is None else env).get(ENV_ALLOW_BAD_SITE, "")).strip().lower()
    return v in ("1", "true", "yes", "on")


def breach_site_check(bottom_ft, toe_ft, top_ft, height_ft):
    """The breach and the ground just below the dam. Returns (problems, notes), plain text.
    A problem stops the build: the breach bottom more than SITE_BREACH_BELOW_TOE_FT below the
    toe ground, or the top of dam less than SITE_MIN_RELIEF_FRAC x H above it."""
    problems, notes = [], []
    if toe_ft is None or not np.isfinite(toe_ft) or not height_ft:
        return problems, ["The ground below the dam could not be read; the breach was not "
                          "checked against it."]
    below = toe_ft - bottom_ft
    relief = top_ft - toe_ft
    frac = relief / float(height_ft)
    if below > SITE_BREACH_BELOW_TOE_FT:
        problems.append(
            "The breach bottom ({:.1f} ft) is {:.1f} ft below the ground just downstream of the "
            "dam ({:.1f} ft); HEC-RAS would connect it to cells far above it, the water cannot "
            "drain that low and the volume balance fails.".format(bottom_ft, below, toe_ft))
    if frac < SITE_MIN_RELIEF_FRAC:
        problems.append(
            "The top of dam ({:.1f} ft) is only {:.1f} ft above the ground just downstream of "
            "the dam ({:.1f} ft), {:.0f} % of the {:.1f} ft dam height: the DEM shows no "
            "embankment at the dam point.".format(top_ft, relief, toe_ft, 100 * frac, height_ft))
    elif not (SITE_NOTE_RELIEF_FRAC[0] <= frac <= SITE_NOTE_RELIEF_FRAC[1]):
        notes.append("The top of dam is {:.1f} ft above the ground just downstream of the dam "
                     "({:.0f} % of the {:.1f} ft dam height); check the dam height and the dam "
                     "point.".format(relief, 100 * frac, height_ft))
    return problems, notes


def storage_site_check(curve_min_ft, curve_area_ac, ground_min_ft, outline_area_ac, top_ft,
                       height_ft):
    """The storage curve against the terrain inside the reservoir outline (ground below the top
    of dam reached from just upstream). Returns (problems, notes, stats)."""
    problems, notes = [], []
    stats = {"curve_min_ft": round(float(curve_min_ft), 2),
             "curve_area_top_ac": (round(float(curve_area_ac), 1) if curve_area_ac else None),
             "ground_min_ft": (None if ground_min_ft is None else round(float(ground_min_ft), 2)),
             "outline_area_ac": (None if outline_area_ac is None else
                                 round(float(outline_area_ac), 1))}
    if ground_min_ft is None or not height_ft:
        notes.append("The reservoir is not visible in the DEM below the top of dam; the storage "
                     "curve was not checked against the terrain.")
        return problems, notes, stats
    depth = top_ft - ground_min_ft
    frac = depth / float(height_ft)
    stats["depth_frac"] = round(frac, 3)
    if frac < STORAGE_STOP_DEPTH_FRAC:
        problems.append(
            "Inside the reservoir outline the ground is never lower than {:.1f} ft, only {:.1f} "
            "ft below the top of dam, but the storage curve goes down to {:.1f} ft ({:.1f} ft "
            "dam height): the dam point does not sit at this reservoir.".format(
                ground_min_ft, depth, curve_min_ft, height_ft))
    elif frac < STORAGE_NOTE_DEPTH_FRAC:
        notes.append("The lowest ground inside the reservoir outline is {:.1f} ft, {:.1f} ft "
                     "above the bottom of the storage curve ({:.1f} ft); LiDAR shows a water "
                     "surface there, or the dam data are off.".format(
                         ground_min_ft, ground_min_ft - curve_min_ft, curve_min_ft))
    if curve_area_ac and outline_area_ac is not None:
        ratio = outline_area_ac / float(curve_area_ac)
        stats["area_ratio"] = round(ratio, 3)
        if ratio < STORAGE_STOP_AREA_RATIO:
            problems.append(
                "The ground below the top of dam upstream covers {:.1f} ac, against {:.1f} ac "
                "at the top of the storage curve ({:.0f} %).".format(
                    outline_area_ac, curve_area_ac, 100 * ratio))
        elif not (STORAGE_NOTE_AREA_RATIO[0] <= ratio <= STORAGE_NOTE_AREA_RATIO[1]):
            notes.append("The ground below the top of dam upstream covers {:.1f} ac, against "
                         "{:.1f} ac at the top of the storage curve; check the surface area "
                         "and the top of dam.".format(outline_area_ac, curve_area_ac))
    return problems, notes, stats


def site_gate(problems, allow, notes):
    """Raises HecRasSiteError for `problems` unless `allow`; then they go into `notes`."""
    if not problems:
        return
    if not allow:
        raise HecRasSiteError(problems)
    for p in problems:
        notes.append("SITE CHECK OVERRIDDEN ({}=1 / allow_bad_site): {} The results of this "
                     "model are not meaningful.".format(ENV_ALLOW_BAD_SITE, p))


def crest_segment(st, z, s0, top, tol, gap=0.0):
    """(s_lo, s_hi, ok): the embankment crest along the dam line: the stations around s0 where
    the ground (z) is within `tol` of the top of dam (NaN = unknown, counts as crest), walking
    out from s0 and stepping over stretches up to `gap` long that are not (an emergency
    spillway, a low spot, a road cut); one station more at each end ties it in. ok is False
    when no crest ground lies within `gap` of s0 (then s_lo = s_hi = s0's station)."""
    st = np.asarray(st, float)
    z = np.asarray(z, float)
    n = len(st)
    if n == 0:
        return 0.0, 0.0, False
    inb = ~(np.abs(z - top) > tol)
    k0 = int(np.argmin(np.abs(st - s0)))
    if not inb[k0]:
        cand = np.nonzero(inb & (np.abs(st - st[k0]) <= gap))[0]
        if not len(cand):
            return float(st[k0]), float(st[k0]), False
        k0 = int(cand[np.argmin(np.abs(st[cand] - st[k0]))])
    last = k0
    for i in range(k0 + 1, n):
        if inb[i]:
            last = i
        elif st[i] - st[last] > gap:
            break
    hi = min(last + 1, n - 1)
    last = k0
    for i in range(k0 - 1, -1, -1):
        if inb[i]:
            last = i
        elif st[last] - st[i] > gap:
            break
    lo = max(last - 1, 0)
    return float(st[lo]), float(st[hi]), True


def dam_line_part(A, B, s_lo, s_hi, s0, cap, min_length=0.0):
    """(A', B', length): the stations s_lo..s_hi of the edge A-B, at most `cap` long (centred
    on s0, kept inside s_lo..s_hi) and at least `min_length` (centred on s0, kept on A-B)."""
    A = np.asarray(A, float)
    B = np.asarray(B, float)
    full = float(np.hypot(*(B - A)))
    if full <= 0:
        return A, B, 0.0
    ab = (B - A) / full
    lo, hi = max(0.0, float(s_lo)), min(full, float(s_hi))
    if cap and hi - lo > cap:
        c = min(max(float(s0), lo + 0.5 * cap), hi - 0.5 * cap)
        lo, hi = c - 0.5 * cap, c + 0.5 * cap
    if hi - lo < min_length:
        L = min(full, float(min_length))
        c = min(max(float(s0), 0.5 * L), full - 0.5 * L)
        lo, hi = c - 0.5 * L, c + 0.5 * L
    return A + ab * lo, A + ab * hi, hi - lo


def hydrograph_end_hr(t_s, q, t_ref_s=0.0, frac=HYDRO_END_FRAC):
    """Hours after breach initiation at which the hydrograph has fallen below `frac` of its
    peak for good (its last time when it never does)."""
    t = np.asarray(t_s, float)
    q = np.asarray(q, float)
    if len(t) == 0:
        return 0.0
    pk = float(np.nanmax(q)) if np.isfinite(q).any() else 0.0
    if pk <= 0:
        return max(0.0, (float(t[-1]) - t_ref_s) / 3600.0)
    above = np.nonzero(q >= frac * pk)[0]
    k = int(above[-1]) if len(above) else len(t) - 1
    k = min(k + 1, len(t) - 1)          # the first sample below it
    return max(0.0, (float(t[k]) - t_ref_s) / 3600.0)


def simulation_hours(hydro_end_hr, arrival_hr, formation_hr=0.0, cap=SIM_CAP_HR,
                     minimum=SIM_MIN_HR):
    """(hours, how): the plan's simulation window. The later of the end of the breach
    hydrograph, the run's latest arrival and the breach formation time, plus a drain allowance
    of SIM_DRAIN_FRAC of that (at least SIM_DRAIN_MIN_HR); whole hours, between `minimum` and
    `cap`."""
    base = max(float(hydro_end_hr or 0.0), float(arrival_hr or 0.0), float(formation_hr or 0.0))
    drain = max(SIM_DRAIN_MIN_HR, SIM_DRAIN_FRAC * base)
    want = base + drain
    sim = float(min(max(math.ceil(want - 1e-9), minimum), cap))
    how = ("the later of the end of the breach hydrograph ({:.1f} h) and the run's latest "
           "arrival ({:.1f} h), plus {:.1f} h to drain".format(
               float(hydro_end_hr or 0.0), float(arrival_hr or 0.0), drain))
    if want > cap:
        how += ("; capped at {:g} h (the flood front may not reach the end of the 2D area; set "
                "the option sim_hours to run longer)".format(cap))
    return sim, how


def cut_breach_channel(tif_path, A, ab, d, s_lo, s_hi, t_max, z_cut, side_slope=None,
                       z_top=None, t_min=0.0):
    """Lowers the terrain GeoTIFF (never raises it) to z_cut in the rectangle from the dam line
    A + ab*s (s_lo..s_hi) downstream (d) for 0..t_max: the breach opening through the
    downstream half of the embankment, so the 2D cells along the breach reach the breach
    bottom. v2d: with side_slope (H:V) and z_top, the sides rise from z_cut at that slope
    beyond s_lo / s_hi up to z_top (the breach's own side slopes), lowered only too; t_min < 0
    starts the cut upstream of the dam line (through the whole embankment), so that the
    interpolated terrain at the dam line is the breach bottom too.
    Returns {'cells', 'area', 'max_cut'} (area in the GeoTIFF's units squared)."""
    gdal, ogr, osr = _ogr()
    ds = gdal.Open(tif_path, gdal.GA_Update)
    gt = ds.GetGeoTransform()
    b = ds.GetRasterBand(1)
    nd = b.GetNoDataValue()
    A = np.asarray(A, float)
    ab = np.asarray(ab, float)
    d = np.asarray(d, float)
    wside = 0.0
    if side_slope and z_top is not None and z_top > z_cut:
        wside = float(side_slope) * (float(z_top) - float(z_cut))
    lo_, hi_ = s_lo - wside, s_hi + wside
    corners = np.array([A + ab * lo_ + d * t_min, A + ab * hi_ + d * t_min,
                        A + ab * hi_ + d * t_max, A + ab * lo_ + d * t_max])
    c0 = max(0, int(math.floor((corners[:, 0].min() - gt[0]) / gt[1])) - 1)
    c1 = min(ds.RasterXSize, int(math.ceil((corners[:, 0].max() - gt[0]) / gt[1])) + 1)
    r0 = max(0, int(math.floor((corners[:, 1].max() - gt[3]) / gt[5])) - 1)
    r1 = min(ds.RasterYSize, int(math.ceil((corners[:, 1].min() - gt[3]) / gt[5])) + 1)
    out = {"cells": 0, "area": 0.0, "max_cut": 0.0}
    if c1 <= c0 or r1 <= r0:
        ds = None
        return out
    a = b.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float64)
    xs = gt[0] + (np.arange(c0, c1) + 0.5) * gt[1]
    ys = gt[3] + (np.arange(r0, r1) + 0.5) * gt[5]
    X, Y = np.meshgrid(xs, ys)
    s = (X - A[0]) * ab[0] + (Y - A[1]) * ab[1]
    t = (X - A[0]) * d[0] + (Y - A[1]) * d[1]
    valid = np.isfinite(a)
    if nd is not None:
        valid &= a != nd
    zt = np.full(a.shape, float(z_cut))
    if wside > 0:
        off = np.maximum(s_lo - s, s - s_hi)
        zt = np.where(off > 0, z_cut + off / float(side_slope), z_cut)
    m = (s >= lo_) & (s <= hi_) & (t >= t_min) & (t <= t_max) & valid & (a > zt + 1e-9)
    if m.any():
        out = {"cells": int(m.sum()), "area": float(m.sum() * abs(gt[1] * gt[5])),
               "max_cut": float((a[m] - zt[m]).max())}
        a[m] = zt[m]
        b.WriteArray(a.astype(np.float32), c0, r0)
        b.FlushCache()
    ds = None
    return out


def breach_corner_margin(tcell, sb):
    """v2d: how far the flat breach cut runs past each bottom corner so that the dam-row cell
    centred on the corner (width sb) reaches the invert in HEC-RAS's interpolated terrain: the
    four terrain cells around a point span up to sqrt(2) x tcell across the dam, the corner
    cell reaches sb / 2 inside the corner, plus a quarter terrain cell."""
    return max(0.25 * tcell, math.sqrt(2.0) * tcell - 0.5 * sb + 0.25 * tcell)


# ============================================================================
# v3.10.22: breach start, spillway, the plugin's hydrograph on HEC-RAS's breach, solver
# tolerances, gmsh computation points
# ============================================================================
SPILLWAY_KEYS = (("spillway_crest_ft", "spillway_width_ft"),
                 ("emergency_spillway_crest_ft", "emergency_spillway_width_ft"))


def spillway_spec(dam_folder=None, options=None):
    """(crest ft, width ft, centre station ft or None, where from) or None: the options
    spillway_crest_ft / spillway_width_ft (/ spillway_station_ft), else dam.json fields
    spillway_crest_ft + spillway_width_ft (or emergency_spillway_...). The option spillway=False
    leaves it out."""
    o = options or {}
    if o.get("spillway") is False:
        return None
    if o.get("spillway_crest_ft") is not None and o.get("spillway_width_ft") is not None:
        return (float(o["spillway_crest_ft"]), float(o["spillway_width_ft"]),
                None if o.get("spillway_station_ft") is None else float(o["spillway_station_ft"]),
                "chosen")
    if dam_folder:
        try:
            with open(os.path.join(dam_folder, "dam.json"), encoding="utf-8") as f:
                fl = json.load(f).get("fields") or {}
        except (OSError, ValueError, AttributeError):
            fl = {}
        for kc, kw in SPILLWAY_KEYS:
            try:
                c, w = fl.get(kc), fl.get(kw)
                if c is not None and w is not None and float(w) > 0:
                    st = fl.get("spillway_station_ft")
                    return (float(c), float(w), None if st is None else float(st),
                            "dam.json {} / {}".format(kc, kw))
            except (TypeError, ValueError):
                pass
    return None


def spillway_stations(dam_len, width, breach_lo, breach_hi, station=None, margin=0.0):
    """(s1, s2) of a spillway `width` wide on the dam line (stations from its left end):
    centred on `station` when given, else at the end of the line farther from the breach
    (breach_lo..breach_hi, its top width), `margin` in from the end. (None, reason) when it
    does not fit without touching the breach."""
    width, dam_len = float(width), float(dam_len)
    if width <= 0:
        return None, "the spillway width is not above 0"
    if station is not None:
        s1, s2 = float(station) - 0.5 * width, float(station) + 0.5 * width
    elif (dam_len - breach_hi) >= breach_lo:
        s2 = dam_len - margin
        s1 = s2 - width
    else:
        s1 = margin
        s2 = s1 + width
    if s1 < 0 or s2 > dam_len:
        return None, ("the spillway ({:.0f} wide) does not fit on the {:.0f} dam line"
                      .format(width, dam_len))
    if s2 > breach_lo and s1 < breach_hi:
        return None, ("the spillway (stations {:.0f} to {:.0f}) would overlap the breach "
                      "({:.0f} to {:.0f})".format(s1, s2, breach_lo, breach_hi))
    return (s1, s2), ""


def add_spillway_notch(weir, s1, s2, crest, wall=0.25):
    """The weir profile [(station, elevation)] with a notch at `crest` from s1 to s2 (the
    profile elsewhere unchanged; `wall` sets how steep its sides are)."""
    st = np.array([a for a, _ in weir], float)
    el = np.array([b for _, b in weir], float)
    out = [(a, b) for a, b in weir if a < s1 - wall - 1e-9]
    lo = max(s1 - wall, 0.0)
    out.append((round(lo, 2), round(float(np.interp(lo, st, el)), 2)))
    out += [(round(s1, 2), round(float(crest), 2)), (round(s2, 2), round(float(crest), 2))]
    hi = min(s2 + wall, float(st[-1]))
    out.append((round(hi, 2), round(float(np.interp(hi, st, el)), 2)))
    out += [(a, b) for a, b in weir if a > s2 + wall + 1e-9]
    clean = []
    for a, b in out:
        if clean and a <= clean[-1][0] + 1e-9:
            continue
        clean.append((a, b))
    return clean


def weir_flow(coef, width, head):
    """Q = C L H^1.5 (project units; 0 for no head)."""
    return float(coef) * float(width) * max(float(head), 0.0) ** 1.5


def warmup_inflow(sim_hours, start_hr, hold, base=0.0, step_min=SPILLWAY_INTERVAL_MIN):
    """(interval label, values): `hold` + base until the breach starts (it keeps the pool at
    its starting level while the spillway runs), then the base flow only."""
    dt = step_min / 60.0
    n = int(math.ceil(float(sim_hours) / dt)) + 2
    t = np.arange(n) * dt
    q = np.where(t <= float(start_hr) + 1e-9, float(hold) + float(base), float(base))
    return hecras.ras_interval_label(step_min), [round(float(v), 2) for v in q]


def plugin_reference(dam, params, bvals, units, start_hr=0.0):
    """The plugin's own breach routing (breach.routed_hydrograph) on HEC-RAS's breach: the
    bottom elevation, bottom width, side slopes and formation time of this plan (the bottom
    may have been raised to the toe ground), so the HEC-RAS breach flow can be compared with
    like. Returns a dict (t_hr from the breach start, q_cfs, peak, times) or None."""
    method = str(params.get("method") or "")
    if method not in brmod.ROUTED_METHODS:
        return None
    import dataclasses
    bottom_ft = float(bvals["bottom_elev"]) * units.L / FT
    z = float(bvals["left_slope"])
    hb_ft = float(dam.top_of_dam_ft) - bottom_ft
    avg_w = float(bvals["bottom_width"]) * units.L / FT + z * hb_ft
    d2 = dataclasses.replace(dam, breach_bottom_ft=bottom_ft)
    ov = brmod.BreachOverrides(avg_width_ft=avg_w, formation_hr=float(bvals["formation_hr"]),
                               side_slope_h=z)
    hy = brmod.routed_hydrograph(d2, method, ov)
    t_hr = np.asarray(hy.t_s, float) / 3600.0
    q = np.asarray(hy.q_cms, float) / CFS
    k = int(np.argmax(q))
    return {"method": method, "bottom_ft": round(bottom_ft, 2), "avg_width_ft": round(avg_w, 1),
            "side_slope": z, "formation_hr": float(bvals["formation_hr"]),
            "peak_cfs": round(float(q[k]), 1), "peak_hr_after_breach": round(float(t_hr[k]), 3),
            "peak_hr_in_plan": round(float(t_hr[k]) + float(start_hr), 3),
            "volume_acft": round(float(hy.volume_m3) / ACFT, 1),
            "run_peak_cfs": params.get("peak_cfs"), "start_hr": float(start_hr),
            "t_hr": t_hr, "q_cfs": q}


def record_run_report(report_path, key, value, log=None):
    """v3.10.22: one entry of the run's run_report.json (written atomically)."""
    try:
        with open(report_path, encoding="utf-8") as f:
            rep = json.load(f)
        rep[key] = value
        tmp = report_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rep, f, indent=2, default=str)
        os.replace(tmp, report_path)
    except (OSError, ValueError) as e:
        if log:
            log("Could not record {} in {}: {}".format(key, report_path, e))


def dam_axis_of(options=None):
    """v3.10.22: 'crest' (default) or 'straight' from the option dam_axis."""
    from . import hecras_crest as hcrest
    v = (options or {}).get("dam_axis")
    v = "crest" if v in (None, "") else str(v).strip().lower()
    if v not in hcrest.DAM_AXES:
        raise HecRasModelError("dam_axis must be one of {} (got {!r})".format(
            ", ".join(hcrest.DAM_AXES), v))
    return v


def _dam_axis_rows(ax, L):
    """README rows for the dam line along the crest (or why it is straight)."""
    if not ax:
        return []
    if ax.get("used") != "crest":
        return ["| Dam line along the crest | not used | {} |".format(
            "the trace failed: " + ax["why"] + " (the straight line is used)" if ax.get("why")
            else "dam_axis='straight' (the straight line across the flow)")]
    return ["| Breach location | station {:,.1f} {} | {}; the crest there runs {:.0f} degrees "
            "off the straight line; the breach is cut at right angles to the crest (toward "
            "bearing {:.0f}) |".format(ax.get("final_breach_station", ax.get("breach_station", 0)),
                                       L, ax.get("breach_how", ""), ax.get("skew_deg") or 0.0,
                                       (ax.get("breach_frame") or {}).get(
                                           "downstream_bearing_deg",
                                           ax.get("downstream_bearing_deg") or 0.0))]


def crest_dam_line(dem_path, srs, zf, units, dam, params, P, d, n_left, cell, tcell, o,
                   user_line=None, inflow_m=None, dam_folder=None):
    """v3.10.22: the dam line along the embankment crest (model units, from A, the left end
    looking downstream, to B), the breach centre along it and how both were found. A user's
    dam line (user_line, model CRS) wins over the trace. Raises hecras_crest.CrestTraceError
    when the crest cannot be traced (build_model then uses the straight line)."""
    from . import hecras_crest as hc
    axis = -np.asarray(n_left, float)                  # A -> B
    ft = units.from_ft(1.0)
    top_u = units.from_ft(dam.top_of_dam_ft)
    H = units.from_ft(dam.dam_height_ft)
    toe = units.from_ft((dam.ds_slope_h or 3.0) * dam.dam_height_ft +
                        0.5 * (dam.crest_width_ft or 20.0))
    want_ft, want_src = dam_length_ft(dam, dam_folder, o.get("dam_length_ft"))
    cap_ft = float(o.get("max_dam_line_ft") or DAM_LINE_CAP_FT)
    max_len = units.from_ft(want_ft) if want_ft else units.from_ft(cap_ft)
    step = max(float(tcell), units.from_ft(5.0))
    pad = max_len + 2.0 * toe + 4.0 * step + cell
    while (2 * pad / step) ** 2 > 6e6:
        step *= 1.5
    b = (P[0] - pad, P[1] - pad, P[0] + pad, P[1] + pad)
    za, zx0, zy0 = _warp(dem_path, srs, step, bounds=b, alg="bilinear")
    za = za * float(zf) / units.L

    def sample(xy, za=za, zx0=zx0, zy0=zy0, c=step):
        return _sample(za, zx0, zy0, c, xy)

    gap = max(2.0 * cell, units.from_ft(DAM_LINE_GAP_FT))
    rec = {"asked": "crest", "step": round(step, 2), "dam_length": round(max_len, 1),
           "dam_length_source": want_src or "the cap max_dam_line_ft ({:g} ft)".format(cap_ft),
           "dam_length_on_record": bool(want_ft)}
    if user_line is not None and len(user_line) >= 2:
        line = np.asarray(user_line, float).reshape(-1, 2)
        if (line[-1] - line[0]) @ axis < 0:
            line = line[::-1]
        line = hc.min_spacing(line, 0.5 * cell)
        rec.update(source="your dam line", raw_points=int(len(user_line)))
        p0 = line[int(np.argmin(np.hypot(*(line - P).T)))]
    else:
        tr = hc.trace(sample, P, axis, top_u, H, step, toe, max_len, gap, ft=ft)
        line = hc.tidy(tr["line"], step, cell, keep_len=max_len, centre=tr["p0"])
        p0 = tr["p0"]
        rec.update(source="traced from the DEM", raw_points=int(len(tr["line"])),
                   raw_length=round(float(hc.cumlen(tr["line"])[-1]), 1),
                   snap=round(tr["snap"], 2), tol=round(tr["tol"], 2), prom=round(tr["prom"], 2),
                   ends={k: {"stop": v[0], "walked": round(v[1], 1)} for k, v in tr["ends"].items()},
                   raw_line=[[round(float(x), 2), round(float(y), 2)] for x, y in tr["line"]])
    L = float(hc.cumlen(line)[-1])
    if L < 2.0 * cell:
        raise hc.CrestTraceError("the dam line along the crest is only {:,.0f} {} long".format(
            L, units.len_name))
    pb = breach_values(dam, params, units, 0.0)[0]
    half_b = 0.5 * float(pb["bottom_width"])
    half_t = half_b + float(pb["left_slope"]) * max(0.0, top_u - float(pb["bottom_elev"]))
    tol = max(units.from_ft(3.0), 0.1 * H)
    method = str(o.get("breach_at") or "auto").strip().lower()
    if method not in hc.BREACH_AT:
        raise HecRasModelError("breach_at must be one of {} (got {!r})".format(
            ", ".join(hc.BREACH_AT), method))
    detail = {}
    if o.get("breach_station_ft") is not None:
        sc = min(max(units.from_ft(float(o["breach_station_ft"])), 0.0), L)
        how, method = "chosen (breach_station_ft)", "station"
    elif method == "inflow" and inflow_m is not None:
        sc = hc.station_of(line, inflow_m)
        how = "the run's breach inflow point projected onto the dam line"
    else:
        if method == "inflow":
            method = "auto"
        sc, how, detail = hc.pick_breach(line, sample, toe, cell, half_b, half_t, top_u, tol,
                                         method, step)
    bxy = hc.point_at(line, sc)
    # straight across the breach (its top width plus half a cell each side), so that the
    # breach is a rectangle in the crest's own frame there
    hw = half_t + 0.5 * cell
    s0, s1 = sc - hw, sc + hw
    cv = hc.cumlen(line)
    # a vertex just outside the span becomes its end (no sliver segment beside the breach)
    near0 = [c for c in cv[1:-1] if s0 - 0.5 * cell < c <= s0]
    near1 = [c for c in cv[1:-1] if s1 <= c < s1 + 0.5 * cell]
    s0 = min(near0) if near0 else s0
    s1 = max(near1) if near1 else s1
    line2 = hc.straighten(line, s0, s1)
    ins = [i for i, q in enumerate(line2) if min(np.hypot(*(q - hc.point_at(line, s0))),
                                                  np.hypot(*(q - hc.point_at(line, s1)))) < 1e-6]
    line2 = hc.min_spacing(line2, 0.5 * cell, keep=ins)
    if not hc.monotonic(line2, axis):
        raise hc.CrestTraceError("the dam line along the crest turns back across the valley "
                                 "(it does not advance from one end to the other)")
    sc = hc.station_of(line2, bxy)
    span_xy = (hc.point_at(line, s0), hc.point_at(line, s1))
    rec["straight_span"] = [round(hc.station_of(line2, q), 1) for q in span_xy]
    t_b = hc.tangent_at(line2, sc)
    skew = hc.angle_deg(t_b, axis)
    rec.update(used="crest", line=[[round(float(x), 2), round(float(y), 2)] for x, y in line2],
               vertices=int(len(line2)), length=round(float(hc.cumlen(line2)[-1]), 1),
               breach_station=round(sc, 1), breach_xy=[round(float(v), 2) for v in bxy],
               breach_how=how, breach_method=method, breach_detail=detail,
               skew_deg=round(skew, 1), crest_bearing_deg=round(hc.bearing_deg(t_b), 1),
               straight_bearing_deg=round(hc.bearing_deg(axis), 1),
               downstream_bearing_deg=round(hc.bearing_deg(hc.left_normal(t_b)), 1),
               p0=[round(float(v), 2) for v in p0])
    return line2, sc, rec


def build_model(dam_folder, out_folder=None, dem_path=None, crs=None, options=None,
                log=None, progress=None):
    """Builds the HEC-RAS model of a finished run in `out_folder` (default
    <dam folder>/_hecras/model). Returns a dict with the paths, the Model and notes."""
    log = log or (lambda m: None)
    progress = progress or (lambda p, m="": None)
    o = dict(DEFAULTS)
    o.update({k: v for k, v in (options or {}).items() if v is not None})
    notes = []
    progress(2, "reading the run")
    hy, dam, params = hecras.load_run(dam_folder)
    model_dir = os.path.join(dam_folder, hecras.MODEL_FOLDER)
    with open(os.path.join(model_dir, hecras.RUN_REPORT_JSON), encoding="utf-8") as f:
        report = json.load(f)
    settings = report.get("settings") or {}
    depth_path = os.path.join(dam_folder, "Depth (Max).tif")
    if not os.path.exists(depth_path):
        raise HecRasModelError("the dam folder has no 'Depth (Max).tif' (run with the final "
                               "rasters written)")
    dem_path = dem_path or (settings.get("input_files") or {}).get("DEM")
    if not dem_path or not os.path.exists(dem_path):
        raise HecRasModelError("the DEM of the run was not found ({}); choose it".format(
            dem_path or "not recorded"))

    # --- coordinate systems --------------------------------------------------------
    gdal, ogr, osr = _ogr()
    from ..damsuite import gdalsafe as GS
    from . import hecras_seeds as hseeds
    from . import hecras_breaklines as hbl
    seeds_mode = hseeds.mode_of(o)               # v3.10.22: raises on a bad value, before work
    from . import hecras_bands as hband
    try:
        bcfg = hband.settings(o)                 # 3.10.76: raises on a bad value, before work
    except hband.BandError as e:
        raise HecRasModelError(str(e))
    band_channel = bcfg["on"] and "channel" in bcfg["classes"]
    from . import hecras_crest as hcrest
    axis_mode = dam_axis_of(o)                   # v3.10.22: raises on a bad value, before work
    dem_ds = GS.open_raster(dem_path)
    dem_wkt = dem_ds.GetProjection()
    dem_gt = dem_ds.GetGeoTransform()
    dem_nodata = dem_ds.GetRasterBand(1).GetNoDataValue()
    dem_ds = None
    work_text = _work_crs(report, model_dir)
    lonlat = _dam_lonlat(dam_folder)
    dam_xy_work = report.get("dam_xy")
    geo = srs_from("EPSG:4269")
    if work_text and dam_xy_work:
        work = srs_from(work_text)
        lonlat = tuple(transformer(work, geo)([dam_xy_work])[0])
    else:
        work = None
        notes.append("The run did not record its model coordinate system; the dam point "
                     "comes from dam.json and the flow direction from the flooded area.")
    if lonlat is None and work is None:
        raise HecRasModelError("the run records no coordinate system for the dam point and "
                               "there is no dam.json point")
    srs, why = choose_crs(crs, dem_wkt, lonlat, work_text)
    units, u_m = crs_units(srs)
    log("HEC-RAS model coordinate system: {} ({}); {}".format(_epsg_text(srs), why,
                                                              units.label))
    to_model = transformer(work, srs) if work is not None else None
    P = (to_model([dam_xy_work])[0] if to_model is not None else
         transformer(geo, srs)([lonlat])[0])
    if settings.get("breach_xy") and to_model is not None:
        P = to_model([settings["breach_xy"]])[0]
    elif report.get("crest_xy") and to_model is not None:
        P = to_model([report["crest_xy"]])[0]          # v3.10.16: the run moved the crest
    inflow = (report.get("inflow_point") or {}).get("xy")
    inflow_m = to_model([inflow])[0] if (inflow and to_model is not None) else None
    path_m = None
    fp = _flow_path(model_dir)
    if fp is not None and to_model is not None:
        path_m = to_model(fp)
    # v3.10.16: an inflow point that is not below the dam (runs before 3.10.16 could put it on
    # a hill next to the dam, DWR-3161) must not set the flow direction or the breach centre
    use_path_dir = True
    inflow_status = {"status": "missing" if inflow is None else "used",
                     "run_xy": list(inflow) if inflow else None}
    if inflow is not None and to_model is None:
        inflow_status.update(status="rejected", why="the run records no coordinate system")
    if inflow_m is not None:
        # the run's point against the run's flow line: a point behind the dam (DWR-3161,
        # Runner 3.10.16) means the dam point or the downstream side is wrong, not only that
        # the ground is too high
        side = inflow_side_problems(P, inflow_m, path_m, units.from_ft(INFLOW_FAR_FT),
                                    units.len_name)
        if side:
            inflow_status["side_problems"] = side
            notes.append("CHECK THE DAM POINT: the run's breach inflow point and its flow "
                         "line disagree: {}. The dam point or the downstream side the Runner "
                         "chose is probably wrong (Runner 3.10.18 checks this); the point is "
                         "not used and not walked, and the flow direction comes from the "
                         "flooded area.".format("; ".join(side)))
            log(notes[-1])
            inflow_status.update(status="rejected", why="CHECK THE DAM POINT: " +
                                 "; ".join(side))
            inflow_m = None
            use_path_dir = False
    if inflow_m is not None:
        zf = _z_factor(report, dem_path)[0]
        g_m = _dem_value_m(dem_path, srs, inflow_m, zf)
        top_m, h_m = dam.top_of_dam_ft * FT, dam.dam_height_ft * FT
        why_bad = inflow_check(g_m, top_m, h_m)
        if why_bad:
            inflow_status.update(status="rejected", why=why_bad)
            log("Inflow point not used: " + why_bad)
            use_path_dir = False          # the flow trace started at that point too
            # TIER 2 fix: walk it downstream (flow path, else steepest descent) to ground at
            # least a quarter of the dam height below the top of dam, off the embankment
            mx = units.from_ft(float(o["inflow_search_ft"]))
            step = max(units.from_ft(5.0), mx / 600.0)
            off = units.from_ft(0.5 * float(dam.crest_width_ft or 20.0) +
                               (dam.ds_slope_h or 3.0) * dam.dam_height_ft)
            moved, diag = None, {}
            try:
                b = (inflow_m[0] - mx - 4 * step, inflow_m[1] - mx - 4 * step,
                     inflow_m[0] + mx + 4 * step, inflow_m[1] + mx + 4 * step)
                wc = max(step, 2.0 * mx / 1500.0)
                ga, gx0, gy0 = _warp(dem_path, srs, wc, bounds=b, alg="bilinear")
                ground = (lambda xy, ga=ga, gx0=gx0, gy0=gy0, wc=wc:
                          _sample(ga, gx0, gy0, wc, xy) * zf)
                moved = relocate_inflow(inflow_m, ground, inflow_drop_needed_m(top_m, h_m), P,
                                        off, mx, step, path_m, diag)
            except Exception as e:            # noqa: BLE001 - reported as not found
                inflow_status["search_error"] = "{}: {}".format(type(e).__name__, e)
            if moved is not None:
                inflow_status.update(
                    status="moved", xy_model=[float(v) for v in moved["xy"]],
                    moved=round(moved["dist"], 1), moved_ft=round(moved["dist"] * units.L / FT, 1),
                    ground_ft=round(moved["ground_m"] / FT, 2), how=moved["how"],
                    original_ground_ft=(round(g_m / FT, 2) if g_m is not None else None))
                msg = ("The run's breach inflow point was moved {:,.0f} ft downstream along {} "
                       "to {:.1f}, {:.1f} ({}), ground {:.1f} ft ({:.1f} ft below the top of "
                       "dam): at the run's point {}.".format(
                           inflow_status["moved_ft"], moved["how"], moved["xy"][0],
                           moved["xy"][1], units.len_name, moved["ground_m"] / FT,
                           (top_m - moved["ground_m"]) / FT, why_bad))
                notes.append(msg)
                log("Inflow point moved: " + msg)
                inflow_m = np.asarray(moved["xy"], float)
            else:
                inflow_status["searched_ft"] = float(o["inflow_search_ft"])
                inflow_status["need_ground_ft"] = round(inflow_drop_needed_m(top_m, h_m) / FT, 2)
                inflow_status["need_drop_ft"] = round(0.25 * h_m / FT, 2)
                if "lowest_m" in diag:
                    inflow_status.update(lowest_ground_ft=round(diag["lowest_m"] / FT, 2),
                                         lowest_xy_model=diag["lowest_xy"],
                                         lowest_at_ft=round(diag["lowest_dist"] * units.L / FT,
                                                            1))
                notes.append("The run's breach inflow point was not used: {}; no point at "
                             "least a quarter of the dam height below the top of dam was found "
                             "within {:,.0f} ft downstream. The flow direction comes from the "
                             "flooded area and the breach centre from the lowest ground at the "
                             "toe. Check the dam point and re-run the Runner.".format(
                                 why_bad, float(o["inflow_search_ft"])))
                inflow_m = None

    # --- flooded extent in the model CRS -----------------------------------------
    progress(10, "flooded extent")
    side = units.from_ft(o["side_buffer_ft"])
    end = units.from_ft(o["end_buffer_ft"])
    run_cell_u = units.length(float(settings.get("cell_size_m") or 10.0))
    acell = max(run_cell_u, side / 8.0, units.from_ft(20.0))
    dep, dx0, dy0 = _warp(depth_path, srs, acell, alg="max")
    thr = o["extent_depth_ft"]
    wet = np.isfinite(dep) & (dep > thr)
    band_wet = (wet, dx0, dy0, acell)             # 3.10.76: the line bands' 'wet' extent
    if not wet.any():
        raise HecRasModelError("the run's Depth (Max).tif has no flooded cells")
    ny, nx = wet.shape
    X = dx0 + (np.arange(nx) + 0.5) * acell
    Y = dy0 - (np.arange(ny) + 0.5) * acell
    # flow direction at the dam: towards the inflow point, else the path, else the flood
    d = None
    for q in ([inflow_m] if inflow_m is not None else []) + (
            [path_m[min(len(path_m) - 1, 5)]] if (path_m is not None and len(path_m) > 1
                                                  and use_path_dir) else []):
        v = np.asarray(q, float) - P
        if np.hypot(*v) > 1e-6:
            d = v / np.hypot(*v)
            break
    rr, cc = np.nonzero(wet)
    wx, wy = X[cc], Y[rr]
    dist = np.hypot(wx - P[0], wy - P[1])
    if d is None:
        near = dist <= np.percentile(dist, 10)
        v = np.array([wx[near].mean() - P[0], wy[near].mean() - P[1]])
        d = v / (np.hypot(*v) or 1.0)
        notes.append("Flow direction at the dam taken from the flooded area near it.")
    n_left = np.array([-d[1], d[0]])
    wet_poly = _poly_from_mask(wet, dx0, dy0, acell, srs)
    # the end of the inundation: the flooded cells farthest from the dam
    far = dist >= dist.max() - max(end, 3 * acell)
    endmask = np.zeros_like(wet)
    endmask[rr[far], cc[far]] = True
    end_poly = _poly_from_mask(endmask, dx0, dy0, acell, srs)
    progress(20, "2D area perimeter")
    dom = wet_poly.Buffer(side, 4)
    if end_poly is not None and end > side:
        dom = dom.Union(end_poly.Buffer(end, 4))
    # cell size of the 2D area
    area_u2 = dom.GetArea()
    cell = units.from_ft(float(o["cell_ft"] or 0.0))
    if not cell:
        lo, hi = (units.from_ft(25.0), units.from_ft(500.0))
        cell = nice_cell(min(hi, max(lo, math.sqrt(area_u2 / float(o["target_cells"])))))
    dom = dom.Simplify(cell / 4.0)
    # v3.10.22: the dam line along the embankment crest, traced from the DEM (dam_axis='crest'),
    # else (or when the trace fails) the straight line through the dam point across the flow
    crest_line, crest_sc, crest_rec = None, None, {"asked": axis_mode, "used": "straight"}
    if axis_mode == "crest":
        zf_c = _z_factor(report, dem_path)[0]
        dsrs_c = srs_from(dem_wkt) if dem_wkt else srs
        tcell_c = float(o["terrain_cell"] or 0.0) or abs(dem_gt[1]) * (
            dsrs_c.GetLinearUnits() if dsrs_c.IsProjected() else 111320.0) / units.L
        user_line = None
        if o.get("dam_line_xy"):
            user_line = np.asarray(o["dam_line_xy"], float).reshape(-1, 2)
        elif report.get("crest_line_xy") and to_model is not None:
            user_line = to_model(report["crest_line_xy"])
        try:
            crest_line, crest_sc, crest_rec = crest_dam_line(
                dem_path, srs, zf_c, units, dam, params, P, d, n_left, cell, tcell_c, o,
                user_line=user_line, inflow_m=inflow_m, dam_folder=dam_folder)
            # the part inside the 2D area (the flooded area plus its buffer) only
            cl_geom = dom.Intersection(_line([tuple(q) for q in crest_line]))
            pieces = []
            for g_ in ([cl_geom] if ogr.GT_Flatten(cl_geom.GetGeometryType()) == ogr.wkbLineString
                       else [cl_geom.GetGeometryRef(i) for i in range(cl_geom.GetGeometryCount())]):
                if g_ is not None and g_.GetPointCount() >= 2:
                    pieces.append(np.asarray(g_.GetPoints(), float)[:, :2])
            full_c = float(hcrest.cumlen(crest_line)[-1])
            bxy_c = hcrest.point_at(crest_line, crest_sc)
            if not pieces:
                raise hcrest.CrestTraceError("the traced crest is outside the 2D area")
            piece = min(pieces, key=lambda q: float(point_segment_distance(
                np.array([bxy_c[0]]), np.array([bxy_c[1]]), q)[0]))
            if float(hcrest.cumlen(piece)[-1]) < full_c - 0.5 * cell:
                if (piece[-1] - piece[0]) @ (crest_line[-1] - crest_line[0]) < 0:
                    piece = piece[::-1]
                notes.append("Dam line: the traced crest reaches beyond the 2D area; {:,.0f} of "
                             "its {:,.0f} {} are used.".format(float(hcrest.cumlen(piece)[-1]),
                                                               full_c, units.len_name))
                crest_line = hcrest.min_spacing(piece, 0.5 * cell)
                crest_sc = hcrest.station_of(crest_line, bxy_c)
                crest_rec["clipped_to_2d_area"] = True
        except hcrest.CrestTraceError as e:
            crest_line, crest_sc = None, None
            crest_rec = {"asked": "crest", "used": "straight", "why": str(e),
                         "fallback": True}
            notes.append("WARNING: the dam line could not be traced along the crest ({}); the "
                         "straight line through the dam point across the flow is used "
                         "(dam_axis='straight'). Check the breach location and the dam line in "
                         "RAS Mapper, or give a dam line (dam_line_xy).".format(e))
            log(notes[-1])
    span = 4.0 * math.sqrt(max(area_u2, 1.0)) + 10 * side
    cut = _line([P - n_left * span, P + n_left * span])
    inter = dom.Intersection(cut)
    segs = []
    for g in ([inter] if ogr.GT_Flatten(inter.GetGeometryType()) == ogr.wkbLineString
              else [inter.GetGeometryRef(i) for i in range(inter.GetGeometryCount())]):
        pts = np.asarray(g.GetPoints(), float)[:, :2] if g.GetPointCount() else None
        if pts is not None and len(pts) >= 2:
            segs.append(pts)
    if not segs:
        raise HecRasModelError("the dam line does not cross the flooded area; check the dam "
                               "point")
    t_of = lambda p: float(np.dot(np.asarray(p) - P, n_left))
    seg = min(segs, key=lambda s: 0.0 if min(t_of(s[0]), t_of(s[-1])) <= 0 <= max(
        t_of(s[0]), t_of(s[-1])) else min(abs(t_of(s[0])), abs(t_of(s[-1]))))
    ta, tb = sorted((t_of(seg[0]), t_of(seg[-1])))
    A = P + n_left * tb           # left end looking downstream
    B = P + n_left * ta
    eps = cell * 0.01
    up_len = span
    edge_line = None
    if crest_line is not None:
        # v3.10.22: cut along the crest, extended straight past its ends across the valley;
        # everything upstream of it (the pool) is left out of the 2D area
        ext = hcrest.extend(crest_line, span, -n_left)
        up2 = 2.0 * span
        cutter = _polygon([tuple(q) for q in ext] + [tuple(ext[-1] - d * up2),
                                                      tuple(ext[0] - d * up2)])
        bxy_c = hcrest.point_at(crest_line, crest_sc)
        probe = bxy_c + hcrest.left_normal(hcrest.tangent_at(crest_line, crest_sc)) * \
            max(2 * acell, cell)
        ei = dom.Intersection(_line([tuple(q) for q in ext]))
        epieces = []
        for g_ in ([ei] if ogr.GT_Flatten(ei.GetGeometryType()) == ogr.wkbLineString
                   else [ei.GetGeometryRef(i) for i in range(ei.GetGeometryCount())]):
            if g_ is not None and g_.GetPointCount() >= 2:
                epieces.append(np.asarray(g_.GetPoints(), float)[:, :2])
        if epieces:
            edge_line = min(epieces, key=lambda q: float(point_segment_distance(
                np.array([bxy_c[0]]), np.array([bxy_c[1]]), q)[0]))
        A, B = crest_line[0].copy(), crest_line[-1].copy()
    else:
        cutter = _polygon([A + n_left * eps, B - n_left * eps, B - n_left * eps - d * up_len,
                           A + n_left * eps - d * up_len])
        probe = P + d * max(2 * acell, cell)
    down = dom.Difference(cutter)
    parts = _rings(down)
    if not parts:
        raise HecRasModelError("nothing of the 2D area is left below the dam")
    chosen = None
    for _, ring, poly in parts:
        if poly.Contains(_point(*probe)) or poly.Distance(_point(*probe)) < cell:
            chosen = ring
            break
    chosen = chosen if chosen is not None else parts[0][1]
    perim = chosen
    if ring_area(perim) < 0:
        perim = perim[::-1]
    # make A and B perimeter vertices (they are, from the cut) and find the dam edge
    dA = np.hypot(*(perim - A).T)
    dB = np.hypot(*(perim - B).T)
    if dA.min() > cell or dB.min() > cell:
        notes.append("The 2D area's edge at the dam is not straight; check the connection "
                     "line in RAS Mapper.")
    iA, iB = int(np.argmin(dA)), int(np.argmin(dB))
    if crest_line is not None:
        # every crest vertex is a perimeter vertex (from the cut) and stays one
        perim = insert_on_ring(perim, crest_line)
        kv = [int(np.argmin(np.hypot(*(perim - q).T))) for q in crest_line]
        perim = thin_ring(perim, 0.9 * cell, keep=kv)
        crest_line = np.array([perim[int(np.argmin(np.hypot(*(perim - q).T)))]
                               for q in crest_line])
        A, B = crest_line[0], crest_line[-1]
        dam_len = float(hcrest.cumlen(crest_line)[-1])
        crest_sc = min(max(hcrest.station_of(crest_line, bxy_c), 0.0), dam_len)
    else:
        perim = thin_ring(perim, 0.9 * cell, keep=(iA, iB))
        A = perim[int(np.argmin(np.hypot(*(perim - A).T)))]
        B = perim[int(np.argmin(np.hypot(*(perim - B).T)))]
        dam_len = float(np.hypot(*(A - B)))
    log("2D area: {:,.0f} {}2, dam line {:,.0f} {}".format(abs(ring_area(perim)),
                                                           units.len_name, dam_len,
                                                           units.len_name))

    # --- terrain in the model CRS --------------------------------------------------
    progress(35, "terrain")
    zf, znote = _z_factor(report, dem_path)
    dem_srs = srs_from(dem_wkt) if dem_wkt else srs
    dem_cell_u = abs(dem_gt[1]) * (dem_srs.GetLinearUnits() if dem_srs.IsProjected() else
                                   111320.0) / units.L
    # reservoir search window, upstream of the dam
    if dam.surface_area_top_ac:
        res_r = 3.0 * math.sqrt(dam.surface_area_top_ac * 4046.8564224) / units.L
    else:
        res_r = 3.0 * math.sqrt(dam.storage_top_acft * ACFT / max(dam.dam_height_ft * FT * 0.4,
                                                                 0.1)) / units.L
    res_r = min(max(res_r, units.from_ft(2000.0)), units.length(8 * MILE))
    xs = list(perim[:, 0]) + [P[0] - res_r, P[0] + res_r]
    ys = list(perim[:, 1]) + [P[1] - res_r, P[1] + res_r]
    margin = 2 * cell
    tb_ = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    tcell = float(o["terrain_cell"] or 0.0) or dem_cell_u
    ncell = (tb_[2] - tb_[0]) * (tb_[3] - tb_[1]) / tcell ** 2
    if ncell > o["max_terrain_cells"]:
        tcell = math.sqrt((tb_[2] - tb_[0]) * (tb_[3] - tb_[1]) / o["max_terrain_cells"])
        tcell = math.ceil(tcell * 10) / 10.0
        notes.append("Terrain written at {:g} {} cells (the DEM's would make more than "
                     "{:,.0f} cells).".format(tcell, units.len_name, o["max_terrain_cells"]))
    out = out_folder or os.path.join(dam_folder, "_hecras", "model")
    os.makedirs(os.path.join(out, "GIS"), exist_ok=True)
    os.makedirs(os.path.join(out, "Terrain"), exist_ok=True)
    terrain_name = "{} Terrain".format(safe_name(dam.dam_id))
    terrain_tif = os.path.join(out, "Terrain", terrain_name + ".tif")
    write_terrain(dem_path, terrain_tif, srs, tcell, tb_, zf / units.L, dem_nodata,
                  "ft" if not units.si else "metre")
    # v3.10.20: the run carved the reservoir bottom from the storage curve (RESERVOIR_CARVE):
    # the same depth raster lowers this terrain, so the model and the site checks below see it
    from . import reservoir_carve as rcarve
    carve_note = rcarve.hecras_terrain_note(report, model_dir, terrain_tif, units.L)
    if carve_note:
        notes.append(carve_note)
        log(carve_note)
    # the terrain at a coarser cell for the layout (at most about 20 million cells), and at
    # its own cell around the dam for the weir profile
    scell = max(tcell, math.sqrt((tb_[2] - tb_[0]) * (tb_[3] - tb_[1]) / 20e6))
    z, zx0, zy0 = _warp(terrain_tif, srs, scell, bounds=tb_, alg="average")
    top_u = units.from_ft(dam.top_of_dam_ft)

    # --- reservoir outline: ground below the top of dam reached from just upstream ---
    progress(50, "reservoir")
    rcell = max(scell, res_r / 300.0)
    k = max(1, int(round(rcell / scell)))
    zc = z[:z.shape[0] // k * k, :z.shape[1] // k * k]
    zc = np.nanmin(zc.reshape(zc.shape[0] // k, k, zc.shape[1] // k, k), axis=(1, 3)) \
        if k > 1 else zc
    rc_cell = scell * k
    RX = zx0 + (np.arange(zc.shape[1]) + 0.5) * rc_cell
    RY = zy0 - (np.arange(zc.shape[0]) + 0.5) * rc_cell
    GX, GY = np.meshgrid(RX, RY)
    upstream = ((GX - P[0]) * d[0] + (GY - P[1]) * d[1]) < -rc_cell
    lat = np.abs((GX - P[0]) * n_left[0] + (GY - P[1]) * n_left[1])
    within = np.hypot(GX - P[0], GY - P[1]) <= res_r
    can = upstream & within & np.isfinite(zc) & (zc <= top_u)
    near = can & (lat <= max(dam_len, 4 * rc_cell)) & (np.hypot(GX - P[0], GY - P[1]) <=
                                                        max(dam_len, 10 * rc_cell))
    reservoir = None
    res_obs = None          # v3.10.17: (lowest ground, area) inside the outline, model units
    if near.any():
        cand = np.argwhere(near)
        zz = zc[near]
        seed = tuple(cand[int(np.argmin(zz))])
        reach = _flood_fill(can, seed)
        if reach.sum() >= 4:
            rp = _poly_from_mask(reach, zx0, zy0, rc_cell, srs)
            if rp is not None:
                rp = rp.Simplify(rc_cell)
                rings = _rings(rp)
                if rings:
                    reservoir = rings[0][1]
                    res_obs = (float(np.nanmin(zc[reach])), float(reach.sum()) * rc_cell ** 2)
    if reservoir is None or len(reservoir) < 3:
        sa = (dam.surface_area_top_ac * 4046.8564224 / units.L ** 2 if dam.surface_area_top_ac
              else dam_len ** 2)
        Lr = max(sa / max(dam_len, 1.0), cell)
        reservoir = np.array([A - d * cell, A - d * (cell + Lr), B - d * (cell + Lr),
                              B - d * cell])
        notes.append("The reservoir outline is a rectangle with the surface area at the top "
                     "of dam (the DEM did not show it); it is for display only.")
        res_obs = None
    if ring_area(reservoir) < 0:
        reservoir = reservoir[::-1]
    if len(reservoir) > 500:
        reservoir = reservoir[np.linspace(0, len(reservoir) - 1, 500).astype(int)]

    # --- dam: connection line and weir profile ------------------------------------
    progress(60, "dam crest")
    # v3.10.16: the connection is the dam's own length (dam.json / the run) when that is
    # shorter than the 2D area's edge across the valley, centred on the dam point; the rest of
    # that edge is a closed boundary (the reservoir reaches the 2D area over the dam only)
    edge_A, edge_B = A, B
    if crest_line is not None:
        if edge_line is None:
            edge_line = crest_line
        dam_line_how = ("{:,.0f} {} along the embankment crest, {} ({} vertices; the crest at "
                        "the breach runs {:.0f} degrees off the straight line across the flow)"
                        .format(dam_len, units.len_name, crest_rec.get("source", "traced"),
                                len(crest_line), crest_rec.get("skew_deg") or 0.0))
        notes.append("Dam connection: " + dam_line_how + "; ends: " + "; ".join(
            "{} {}".format(k, v["stop"]) for k, v in (crest_rec.get("ends") or {}).items()) +
            (" (dam length {:,.0f} {} from {})".format(crest_rec["dam_length"], units.len_name,
                                                      crest_rec["dam_length_source"])
             if crest_rec.get("dam_length_on_record") else
             " (no dam length on record; give dam_length_ft in dam.json; the crest is followed "
             "to its ends, at most {:,.0f} {} (option max_dam_line_ft))".format(
                 crest_rec["dam_length"], units.len_name)) + ".")
        if (not crest_rec.get("dam_length_on_record") and crest_rec.get("dam_length")
                and (dam_len >= crest_rec["dam_length"] - 2.0 * cell or any(
                    v.get("stop") == "the dam length"
                    for v in (crest_rec.get("ends") or {}).values()))):
            dam_line_how += "; capped at {:,.0f} ft".format(
                crest_rec["dam_length"] / units.from_ft(1.0))
    else:
        want_ft, want_src = dam_length_ft(dam, dam_folder, o.get("dam_length_ft"))
        A, B, dam_len, tnote = trim_dam_line(edge_A, edge_B, P, units.from_ft(want_ft)
                                             if want_ft else None, 2.0 * cell)
        if tnote:
            notes.append("Dam connection: {} ({}).".format(tnote, want_src))
            perim = insert_on_ring(perim, [A, B])
        dam_line_how = ("{} ft from {}".format(round(want_ft), want_src) if want_ft else
                        "the 2D area's edge across the valley")
        if not want_ft:
            # v3.10.17: no length on record - only the part of the edge where the ground stays
            # near the top of dam around the dam point (DWR-3161: a 4,612 ft line over hills up to
            # 2043 ft for a 1844 ft dam), at most DAM_LINE_CAP_FT
            edge_len = float(np.hypot(*(edge_B - edge_A)))
            ab_e = (edge_B - edge_A) / (edge_len or 1.0)
            st_e = np.linspace(0.0, edge_len, max(2, int(math.ceil(edge_len / scell)) + 1))
            z_e = _sample(z, zx0, zy0, scell, edge_A[None, :] + st_e[:, None] * ab_e[None, :])
            s0 = min(max(float(np.dot(P - edge_A, ab_e)), 0.0), edge_len)
            tol_ft = max(3.0, 0.1 * dam.dam_height_ft)
            gap = max(2.0 * cell, units.from_ft(DAM_LINE_GAP_FT))
            s_lo, s_hi, ok_c = crest_segment(st_e, z_e, s0, top_u, units.from_ft(tol_ft), gap)
            cap_ft = float(o.get("max_dam_line_ft") or DAM_LINE_CAP_FT)
            # at least the breach's top width plus a cell each side
            pb = breach_values(dam, params, units, 0.0)[0]
            min_len = max(2.0 * cell, pb["bottom_width"] + 2.0 * pb["left_slope"] *
                          (top_u - pb["bottom_elev"]) + 2.0 * cell)
            A2, B2, L2 = dam_line_part(edge_A, edge_B, s_lo, s_hi, s0, units.from_ft(cap_ft),
                                       min_len)
            if L2 < edge_len - 1e-6:
                A, B, dam_len = A2, B2, L2
                perim = insert_on_ring(perim, [A, B])
                if not ok_c:
                    dam_line_how = ("{:,.0f} {} around the dam point: no ground within {:.1f} ft of "
                                    "the top of dam (no embankment crest) near the dam point".format(
                                        L2, units.len_name, tol_ft))
                else:
                    dam_line_how = ("{:,.0f} of the {:,.0f} {} edge of the 2D area across the valley, "
                                    "where the ground is within {:.1f} ft of the top of dam (the "
                                    "embankment crest) around the dam point{}".format(
                                        L2, edge_len, units.len_name, tol_ft,
                                        "; capped at {:,.0f} ft".format(cap_ft)
                                        if s_hi - s_lo > units.from_ft(cap_ft) + 1e-6 else ""))
                notes.append("Dam connection: {} (no dam length on record; give dam_length_ft in "
                             "dam.json or the option dam_length_ft).".format(dam_line_how))
    toe_off = units.from_ft((dam.ds_slope_h or 3.0) * dam.dam_height_ft +
                            0.5 * (dam.crest_width_ft or 20.0))
    wcell = tcell if dam_len / tcell <= 20000 else dam_len / 20000.0
    pad = toe_off + max(4 * wcell, 1.5 * cell)
    dam_poly = np.asarray(crest_line if crest_line is not None else [A, B], float)  # v3.10.22
    if edge_line is None:
        edge_line = np.asarray([edge_A, edge_B], float)
    wb = (dam_poly[:, 0].min() - pad, dam_poly[:, 1].min() - pad, dam_poly[:, 0].max() + pad,
          dam_poly[:, 1].max() + pad)
    zw, wx0, wy0 = _warp(terrain_tif, srs, wcell, bounds=wb, alg="bilinear")
    npts = int(min(MAX_WEIR_POINTS * 8, max(10, dam_len / max(wcell, 1e-9))))
    if crest_line is not None:
        st = np.linspace(0.0, dam_len, npts)
        line = hcrest.point_at(dam_poly, st)
    else:
        line = np.linspace(0.0, 1.0, npts)[:, None] * (B - A)[None, :] + A[None, :]
        st = np.hypot(*(line - A).T)
    zl = _sample(zw, wx0, wy0, wcell, line)
    weir_z = np.where(np.isfinite(zl), np.maximum(zl, top_u), top_u)
    s2, e2 = simplify_profile(st, weir_z, units.from_ft(0.1), MAX_WEIR_POINTS)
    weir = [(round(float(a), 2), round(float(b), 2)) for a, b in zip(s2, e2)]
    if crest_line is not None:
        # the crest vertices (perimeter vertices) and points on the segments between them
        conn_line = [tuple(map(float, q)) for q in densify(dam_poly, max(dam_len / 16.0, cell))]
    else:
        conn_line = [tuple(A)] + [tuple(p) for p in densify([A, B], max(dam_len / 16.0, cell))[1:-1]] \
            + [tuple(B)]
    # TIER 2 fix: a moved inflow point must lie inside the 2D area
    if inflow_status["status"] == "moved" and inflow_m is not None:
        if not _polygon([tuple(q) for q in perim]).Contains(_point(*inflow_m)):
            inflow_status.update(status="rejected", why=inflow_status.get("why", "") +
                                 "; the point found downstream lies outside the 2D area")
            notes.append("The moved inflow point lies outside the 2D area; it is not used.")
            inflow_m = None

    # breach centre: where the run's breach inflow point (the low ground at the toe that
    # the runner found) projects onto the dam line; without it, the lowest ground along the
    # toe within the crest around the dam point
    ab = (B - A) / (dam_len or 1.0)
    sP = float(np.dot(P - A, ab))
    # TIER 2 fix: a point walked down the DEM (no flow path) may sit at the foot of a hill to
    # one side, so it moves the inflow only, not the breach centre
    if crest_line is not None:
        # v3.10.22: chosen along the traced crest (hecras_crest.pick_breach)
        sc = float(crest_sc)
        O_, ab_, d_ = hcrest.frame_at(dam_poly, sc)
        toe_xy = O_ + ab_ * sc + d_ * toe_off
        centre_how = crest_rec.get("breach_how") or "along the traced crest"
    elif inflow_m is not None and inflow_status.get("how") != "the steepest descent of the DEM":
        sc = float(np.dot(inflow_m - A, ab))
        toe_xy = inflow_m
        centre_how = "the run's breach inflow point projected onto the dam line"
    else:
        toe = line + d[None, :] * toe_off
        zt = _sample(zw, wx0, wy0, wcell, toe)
        crest_len_u = units.from_ft(dam.crest_length_ft) if dam.crest_length_ft else dam_len
        win = np.abs(st - sP) <= max(0.5 * crest_len_u, 3 * tcell)
        if np.isfinite(zt[win]).any():
            k_ = int(np.nanargmin(np.where(np.isfinite(zt[win]), zt[win], np.inf)))
            sc = float(st[win][k_])
            toe_xy = toe[win][k_]
        else:
            sc, toe_xy = sP, P + d * toe_off
        centre_how = "lowest ground at the toe near the dam point"
    bvals, wse0_ft, trig_ft, bnotes = breach_values(dam, params, units, dam_len)
    notes += bnotes
    # v3.10.22: the HEC-RAS failure mode that opens the breach the way the plugin routed it
    hmode = str(o.get("hec_breach_mode") or "match_plugin").strip().lower()
    if hmode not in HEC_BREACH_MODES:
        raise HecRasModelError("hec_breach_mode must be one of {} (got {!r})".format(
            ", ".join(HEC_BREACH_MODES), hmode))
    method = str(params.get("method") or "")
    if hmode == "match_plugin" and bvals.get("piping") and method != brmod.METHOD_PIPING_ORIFICE:
        bvals["piping"] = False
        bvals["piping_elev"] = None
        notes[:] = [n for n in notes if not n.startswith("Initial piping elevation")]
        notes.append("Breach: HEC-RAS failure mode 'Overtopping' (the breach deepens from the "
                     "top and widens linearly), because the plugin's '{}' breach is routed that "
                     "way; HEC-RAS 'Piping' (a pipe that grows, then collapses) gives a later, "
                     "lower peak (option hec_breach_mode='failure_mode').".format(method))
    bvals["hec_mode"] = "piping" if bvals.get("piping") else "overtopping"
    half = 0.5 * bvals["bottom_width"] + bvals["left_slope"] * (top_u - bvals["bottom_elev"])
    sc = min(max(sc, half), max(dam_len - half, half))
    bvals["center_station"] = round(sc, 1)
    # v3.10.22: the straight frame at the breach (origin, along, downstream): the dam line's
    # own for the straight line; the crest segment's at the breach for the traced crest
    Ob, abb, db = hcrest.frame_at(dam_poly, sc) if crest_line is not None else (A, ab, d)
    bvals["trigger_wse"] = units.from_ft(trig_ft)
    if bvals["trigger_wse"] is not None and abs(trig_ft - wse0_ft) < 1e-6:
        # sunny day: breach as soon as the run starts (pool starts at the trigger)
        bvals["trigger_wse"] = units.from_ft(wse0_ft - 0.01)
    # --- v3.10.17: site checks; the breach invert against the ground below the dam -----
    # toe ground: the lowest ground just past the embankment toe across the breach (the 2D
    # cells the breach flows into), else the ground at the toe point
    bw = float(bvals["bottom_width"])
    half_w = max(0.5 * bw, cell)
    ss = np.linspace(sc - half_w, sc + half_w, max(3, int(math.ceil(2 * half_w / wcell)) + 1))
    tt = np.linspace(toe_off, toe_off + cell, max(3, int(math.ceil(cell / wcell)) + 1))
    SS, TT = np.meshgrid(ss, tt)
    band = (Ob[None, :] + SS.reshape(-1)[:, None] * abb[None, :] +
            TT.reshape(-1)[:, None] * db[None, :])
    zb_ = _sample(zw, wx0, wy0, wcell, band)
    gb = float(_sample(zw, wx0, wy0, wcell, [toe_xy])[0])
    toe_u = (float(np.nanmin(zb_)) if np.isfinite(zb_).any() else
             (gb if np.isfinite(gb) else None))
    to_ft = (lambda v: None if v is None else float(v) * units.L / FT)
    problems, snotes = breach_site_check(to_ft(bvals["bottom_elev"]), to_ft(toe_u),
                                         dam.top_of_dam_ft, dam.dam_height_ft)
    ev, res = storage_curve(dam, units)
    curve_area_ac = res.m * res.vt / res.ht / 4046.8564224
    if res_obs is not None:
        g_ft, a_ac = to_ft(res_obs[0]), res_obs[1] * units.L ** 2 / 4046.8564224
    else:
        g_ft = a_ac = None
    p2, n2, sstats = storage_site_check(res.bottom / FT, curve_area_ac, g_ft, a_ac,
                                        dam.top_of_dam_ft, dam.dam_height_ft)
    allow = allow_bad_site(o)
    scope = str(o.get("site_scope") or "breach").strip().lower()
    if scope not in SITE_SCOPES:
        raise HecRasModelError("site_scope must be one of {} (got {!r})".format(
            ", ".join(SITE_SCOPES), scope))
    if scope == "hydrograph":
        # the plan has no breach and a dry reservoir: these checks judge a model that is not
        # built; the inflow point has its own check (inflow_check)
        for q in problems + p2:
            notes.append("Site check for the breach model (not used: hydrograph-inflow plan, "
                         "no breach, dry reservoir): " + q)
    else:
        site_gate(problems + p2, allow, notes)
    notes += snotes + n2
    site = {"toe_ground_ft": None if toe_u is None else round(to_ft(toe_u), 2),
            "breach_bottom_run_ft": round(to_ft(bvals["bottom_elev"]), 2),
            "top_of_dam_ft": round(float(dam.top_of_dam_ft), 2),
            "dam_height_ft": round(float(dam.dam_height_ft), 2),
            "storage": sstats, "problems": problems + p2, "scope": scope,
            "overridden": bool(problems + p2) and scope == "breach",
            "breach_bottom_raised_ft": 0.0}
    # the breach must not open below the cells it flows into (HEC-RAS: 'weir breach lower than
    # the cells they are connected to'): raise its bottom to the toe ground. The storage below
    # cannot drain onto the ground there anyway; the note says how much stays behind.
    if toe_u is not None and bvals["bottom_elev"] < toe_u - 1e-6:
        old_b = bvals["bottom_elev"]
        new_b = math.ceil(toe_u * 100.0) / 100.0
        kept = float(res.volume(new_b * units.L)) / ACFT
        bvals["bottom_elev"] = new_b
        if bvals.get("piping") and bvals.get("piping_elev") is not None and \
                bvals["piping_elev"] <= new_b:
            bvals["piping_elev"] = round(new_b + 0.5 * (top_u - new_b), 2)
            notes.append("Initial piping elevation moved to {:.2f} {}, half way up the raised "
                         "breach.".format(bvals["piping_elev"], units.len_name))
        site["breach_bottom_raised_ft"] = round(to_ft(new_b - old_b), 2)
        notes.append("Breach bottom raised {:.1f} ft, from {:.2f} to {:.2f} {u}: the lowest "
                     "ground just downstream of the dam in the breach. HEC-RAS cannot drain the "
                     "reservoir below the ground the breach flows onto; {:,.0f} acre-ft ({:.0f} % "
                     "of the {:,.0f} acre-ft at the top of dam) stays in the reservoir.".format(
                         to_ft(new_b - old_b), old_b, new_b, kept,
                         100.0 * kept / max(dam.storage_top_acft, 1e-9), dam.storage_top_acft,
                         u=units.len_name))
        half = 0.5 * bvals["bottom_width"] + bvals["left_slope"] * (top_u - new_b)
        sc = min(max(sc, half), max(dam_len - half, half))
        bvals["center_station"] = round(sc, 1)
        if crest_line is not None:
            Ob, abb, db = hcrest.frame_at(dam_poly, sc)
    # the breach opening through the downstream half of the embankment, cut into the terrain
    # to the breach bottom (lowered only), so the 2D cells along the breach reach its bottom
    cut = None
    if o.get("breach_cut", True):
        cw = 0.5 * max(bw, 2.0 * tcell)
        cut = cut_breach_channel(terrain_tif, Ob, abb, db, sc - cw, sc + cw, toe_off + cell,
                                 bvals["bottom_elev"])
        cut.update(width=round(2 * cw, 1), length=round(toe_off + cell, 1),
                   z=round(bvals["bottom_elev"], 2))
        if cut["cells"]:
            notes.append("Terrain: the breach opening ({:,.0f} {u} wide, from the dam line "
                         "{:,.0f} {u} downstream) is cut to the breach bottom {:.2f} {u} in the "
                         "terrain GeoTIFF ({:,} cells, lowered by up to {:.1f} {u}), so the 2D "
                         "cells at the breach reach its bottom.".format(
                             2 * cw, toe_off + cell, bvals["bottom_elev"], cut["cells"],
                             cut["max_cut"], u=units.len_name))
    site["breach_cut"] = cut
    # --- v3.10.22: the spillway as a notch in the dam weir ------------------------------
    spill_info = None
    sp = spillway_spec(dam_folder, o)
    if sp is not None:
        crest_u, width_u = units.from_ft(sp[0]), units.from_ft(sp[1])
        st_u = None if sp[2] is None else units.from_ft(sp[2])
        half_t = 0.5 * bvals["bottom_width"] + bvals["left_slope"] * (top_u - bvals["bottom_elev"])
        if crest_u >= top_u - 1e-6:
            notes.append("Spillway not added: its crest {:.2f} {} is not below the top of dam."
                         .format(crest_u, units.len_name))
        else:
            ss_, why_ = spillway_stations(dam_len, width_u, sc - half_t, sc + half_t, st_u,
                                          margin=min(cell, max(0.0, 0.1 * dam_len)))
            if ss_ is None:
                notes.append("Spillway not added: {}.".format(why_))
            else:
                weir = add_spillway_notch(weir, ss_[0], ss_[1], crest_u)
                spill_info = {"crest": round(crest_u, 2), "width": round(width_u, 2),
                              "stations": [round(ss_[0], 1), round(ss_[1], 1)],
                              "source": sp[3], "as": "notch in the dam connection's weir",
                              "weir_coef": units.weir_coef(SPILLWAY_WEIR_COEF)}
                notes.append("Spillway: a {:,.0f} {u} notch at {:.2f} {u} in the dam weir, "
                             "stations {:,.0f} to {:,.0f} ({}; {}). HEC-RAS counts the breach "
                             "flow inside the connection's 'Weir Flow' (Total - Weir is 0): "
                             "read the breach flow from the connection's 'Breaching Variables' "
                             "('Breach Flow'); the spillway is 'Total Flow' minus 'Breach "
                             "Flow'.".format(width_u, crest_u, ss_[0], ss_[1], sp[3],
                                                   "centred where given" if st_u is not None
                                                   else "at the end of the dam line farther "
                                                   "from the breach", u=units.len_name))

    # --- boundary: normal depth where the valley leaves the 2D area ---------------
    progress(70, "downstream boundary")
    dens = np.asarray(densify(perim, cell / 2.0, closed=True))
    zp = _sample(z, zx0, zy0, scell, dens)
    dam_seg_d = point_segment_distance(dens[:, 0], dens[:, 1], edge_line)
    ok = np.isfinite(zp) & (dam_seg_d > side + cell)
    if not ok.any():
        raise HecRasModelError("no ground along the 2D perimeter away from the dam")
    zq = np.where(ok, zp, np.inf)
    i0 = int(np.argmin(zq))
    zmin = float(zq[i0])
    dmax = float(np.nanmax(dep[np.isfinite(dep)])) if np.isfinite(dep).any() else 1.0
    end_depth = dep[rr[far], cc[far]]
    h_bc = units.from_ft(max(10.0, 1.5 * float(np.nanmax(end_depth)) if len(end_depth) else 10.0))
    n = len(dens)
    lo = hi = i0
    lim = n // 3
    while hi - i0 < lim and ok[(hi + 1) % n] and zp[(hi + 1) % n] <= zmin + h_bc:
        hi += 1
    while i0 - lo < lim and ok[(lo - 1) % n] and zp[(lo - 1) % n] <= zmin + h_bc:
        lo -= 1
    idx = [(i % n) for i in range(lo, hi + 1)]
    if len(idx) < 2:
        idx = [(i0 - 1) % n, i0, (i0 + 1) % n]
    bc = _offset_outward(dens, idx, BC_OFFSET_CELLS * cell)
    if len(bc) > MAX_ARC_POINTS:
        bc = bc[np.linspace(0, len(bc) - 1, MAX_ARC_POINTS).astype(int)]
    exit_xy = dens[i0]
    # valley slope at the exit: along the run's flow path when known, else dam toe to exit
    slope, how = None, ""
    if path_m is not None and len(path_m) > 2:
        j = int(np.argmin(np.hypot(*(path_m - exit_xy).T)))
        if np.hypot(*(path_m[j] - exit_xy)) < 3 * side + end:
            along = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(path_m, axis=0).T))])
            back = units.from_ft(2640.0)
            k2 = int(np.searchsorted(along, along[j] - back))
            k2 = max(0, min(k2, j - 1))
            zz2 = _sample(z, zx0, zy0, scell, path_m[[k2, j]])
            Lp = along[j] - along[k2]
            if np.isfinite(zz2).all() and Lp > 0:
                slope, how = (zz2[0] - zz2[1]) / Lp, "along the run's flow path, the last " \
                    "{:,.0f} {} before the boundary".format(Lp, units.len_name)
    if slope is None or not slope > 0:
        ref = inflow_m if inflow_m is not None else (Ob + abb * sc + db * toe_off)
        zr = _sample(z, zx0, zy0, scell, [ref])[0]
        Lr2 = float(np.hypot(*(exit_xy - ref)))
        slope = (zr - zmin) / Lr2 if (np.isfinite(zr) and Lr2 > 0) else 0.001
        how = "average fall from the dam toe to the boundary (straight line)"
    slope = float(min(max(slope, 1e-4), 0.05))

    # --- 2D computation points -----------------------------------------------------
    progress(80, "2D computation points")
    pxmin, pymin = perim.min(axis=0)
    pxmax, pymax = perim.max(axis=0)
    gx0 = math.floor(pxmin / cell) * cell + cell / 2.0
    gy1 = math.ceil(pymax / cell) * cell - cell / 2.0
    gnx = int(math.ceil((pxmax - gx0) / cell)) + 1
    gny = int(math.ceil((gy1 - pymin) / cell)) + 1
    mds = gdal.GetDriverByName("MEM").Create("", gnx, gny, 1, gdal.GDT_Byte)
    mds.SetGeoTransform((gx0 - cell / 2.0, cell, 0.0, gy1 + cell / 2.0, 0.0, -cell))
    vds = ogr.GetDriverByName("Memory").CreateDataSource("p")
    lyr = vds.CreateLayer("p", srs, ogr.wkbPolygon)
    f_ = ogr.Feature(lyr.GetLayerDefn())
    f_.SetGeometry(_polygon(perim))
    lyr.CreateFeature(f_)
    gdal.RasterizeLayer(mds, [1], lyr, burn_values=[1])
    inside = mds.GetRasterBand(1).ReadAsArray().astype(bool)
    pr, pc = np.nonzero(inside)
    px = gx0 + pc * cell
    py = gy1 - pr * cell
    # drop points too close to the perimeter (they would make sliver cells)
    edge = np.zeros_like(inside)
    edge[1:-1, 1:-1] = ~(inside[:-2, 1:-1] & inside[2:, 1:-1] & inside[1:-1, :-2] &
                         inside[1:-1, 2:])
    edge |= inside & False
    edge[[0, -1], :] = True
    edge[:, [0, -1]] = True
    near_edge = edge[pr, pc]
    keep = np.ones(len(px), bool)
    if near_edge.any():
        dd = point_segment_distance(px[near_edge], py[near_edge],
                                    list(map(tuple, perim)) + [tuple(perim[0])])
        keep[np.nonzero(near_edge)[0][dd < 0.3 * cell]] = False
    pts = np.column_stack([px[keep], py[keep]])
    if len(pts) < 10:
        raise HecRasModelError("the 2D area has fewer than 10 cells at {} {}; choose a "
                               "smaller cell size".format(cell, units.len_name))

    # v3.10.22: gmsh point layout instead of the grid (optional; HEC-RAS still makes the cells)
    seeds_info = {"asked": seeds_mode, "used": "grid", "grid_points": int(len(pts))}
    if seeds_mode == "gmsh":
        progress(82, "gmsh computation points")
        try:
            spts, sinf = hseeds.make_seeds(
                perim, cell, options=o, dam_line=[tuple(q) for q in dam_poly], downstream=d,
                breach_xy=Ob + abb * sc, breach_len=toe_off + 2.0 * cell, breach_dir=db,
                channel=(path_m if (path_m is not None and use_path_dir) else None),
                structures=_seed_structures(o, report, to_model, perim, cell, terrain_tif, srs,
                                            units, log))
        except hseeds.SeedError as e:
            seeds_info["why"] = str(e)
            notes.append("gmsh computation points were asked for but not used ({}); the regular "
                         "{:g} {} grid was used.".format(e, cell, units.len_name))
            log("WARNING: " + notes[-1])
        else:
            pts = spts
            seeds_info.update(used="gmsh", **sinf)
            notes.append("Computation points from gmsh {} ({:,} points; spacing {:g} {u} along "
                         "the dam and breach, {:g} along the channel, {:g} elsewhere). HEC-RAS "
                         "makes the cells from them.".format(
                             sinf["gmsh_version"], len(pts), sinf["sizes"]["dam_spacing"],
                             sinf["sizes"]["channel_spacing"], cell, u=units.len_name))
            log(notes[-1])
    # v3.10.22: breaklines along the breach cut and the channel, an even row along the dam,
    # built into the points (HEC-RAS keeps a given point layout as it is and does not enforce
    # breaklines in it; see hecras_breaklines)
    bl_info, bl_lines, bl_dam = None, [], None
    if o.get("breaklines", True):
        if seeds_info.get("used") == "gmsh":
            zs = seeds_info.get("sizes") or {}
            bsp = {"dam": zs.get("dam_spacing"), "breach": zs.get("breach_spacing"),
                   "channel": zs.get("channel_spacing")}
        else:
            bsp = None
        bcw = 0.5 * max(bw, 2.0 * tcell)
        bplan = hbl.plan(perim, cell, [tuple(q) for q in dam_poly], db,
                         breach=(Ob + abb * sc, bcw, toe_off + cell, 0.5 * bw)
                         if cut is not None else None,
                         # 3.10.76: the channel band (class 'channel') takes the path
                         path=(path_m if (path_m is not None and use_path_dir
                                          and not band_channel) else None),
                         spacing=bsp)
        n0 = len(pts)
        pts, bst = hbl.enforce(pts, perim, bplan)
        prot = bst.pop("_protected")
        bl_lines = [{"name": b["name"], "line": [tuple(map(float, q)) for q in b["line"]],
                     "near": float(b["near"]), "far": float(b["far"])} for b in bplan["lines"]]
        bl_dam = (float(bplan["dam"]["near"]), float(bplan["dam"]["far"]))
        bl_info = dict(bst, points_before=int(n0), dam_spacing=round(bl_dam[0], 2),
                       lines=[{"name": b["name"], "near": round(b["near"], 2),
                               "far": round(b["far"], 2),
                               "length": round(hbl.length(b["line"]), 1)} for b in bl_lines],
                       skipped=bplan["skipped"],
                       channel_length=bplan.get("channel_length"))
        bdam = (bplan["dam"] or {}).get("breach")
        if bdam:
            bl_info["breach_dam_spacing"] = round(float(bdam["near"]), 2)
            # v2d: the dam-row cells centred on the bottom corners straddle them; the flat cut
            # runs a little past each corner (breach_corner_margin), then the breach's side
            # slopes, so those cells reach the invert (v2c: a face on the corner, the cell
            # beside it 2.8-3.4 ft above the invert in HEC-RAS)
            if cut is not None:
                cm = breach_corner_margin(tcell, float(bdam["near"]))
                fh = max(bcw, float(bdam["half"]) + cm)
                cut2 = cut_breach_channel(terrain_tif, Ob, abb, db, sc - fh, sc + fh,
                                          toe_off + cell, bvals["bottom_elev"],
                                          side_slope=max(float(bvals.get("left_slope") or 0),
                                                         float(bvals.get("right_slope") or 0))
                                          or None, z_top=top_u, t_min=-1.5 * tcell)
                cut.update(corner_margin=round(cm, 2), flat_width=round(2 * fh, 1),
                           upstream=round(1.5 * tcell, 1),
                           side_slope=bvals.get("left_slope"),
                           corner_cells=cut2["cells"],
                           corner_max_cut=round(cut2["max_cut"], 2))
                notes.append("Terrain: the breach cut runs {:.1f} {u} past each bottom corner "
                             "({:,.0f} {u} flat), then rises at the breach's side slope {:g}:1, "
                             "and starts {:.0f} {u} upstream of the dam line (outside the 2D "
                             "area), so the dam-row cells centred on the corners reach the "
                             "breach bottom ({:,} more terrain cells lowered, by up to {:.1f} {u})."
                             .format(cm, 2 * fh, float(bvals.get("left_slope") or 0),
                                     1.5 * tcell, cut2["cells"], cut2["max_cut"],
                                     u=units.len_name))
                log(notes[-1])
        notes.append("Breaklines: {} (length, cell size along it), and an even row of points "
                     "{:g} {u} apart along the dam{}; built into the computation points ({:,} "
                     "removed, {:,} added), because HEC-RAS keeps the points it is given and "
                     "does not enforce breaklines in them.{}".format(
                         "; ".join("{} {:,.0f} {u}, {:g} {u}".format(
                             b["name"], hbl.length(b["line"]), round(b["near"], 1),
                             u=units.len_name) for b in bl_lines) or "none",
                         bl_dam[0],
                         " ({:g} {u} across the breach, cells centred on its centre and on both "
                         "bottom corners, so that no cell face lies on a corner)".format(
                             round(float(bdam["near"]), 1), u=units.len_name) if bdam else "",
                         bst["points_removed"], bst["points_added"],
                         "".join(" No {}.".format(w) for w in bplan["skipped"]),
                         u=units.len_name))
        log(notes[-1])
    else:
        prot = None
        bplan = None
    # 3.10.76: bands of small cells along roads, rail, structures and breaklines (hecras_bands)
    band_info, band_g, banded = None, [], {}
    if bcfg["on"]:
        progress(83, "line bands")
        n0 = len(pts)
        pts, prot, band_info, band_g, banded = _line_bands(
            bcfg, o, report, settings, to_model, srs, units, perim, cell, pts, prot, dam_poly,
            [b["line"] for b in ((bplan or {}).get("lines") or []) if b.get("kind") == "breach"],
            band_wet, (path_m if (path_m is not None and use_path_dir) else None), terrain_tif,
            log)
        band_info["points_before"] = int(n0)
        if band_info.get("note"):
            notes.append(band_info["note"])
            log(notes[-1])
    # v3.10.22: no cell with more than 8 faces (HEC-RAS's limit; Absaraka v2_gmsh failed its
    # property tables with "48 cell(s) with more than 8 sides"): the Voronoi pre-check on
    # every cell (clipped to the perimeter where it reaches it) and every face point, with a
    # nudge, then local removal; a layout that still fails stops the build here
    # v3.10.22 (v2c; v2b_gmsh: "1 face errors detected on the perimeter of the mesh"): and
    # on the perimeter, no cell cut in two, no sliver perimeter face, no perimeter corner on
    # a cell edge; for the grid and the gmsh points alike
    ptol = (units.from_ft(hbl.PERIM_SHORT_MIN), units.from_ft(hbl.PERIM_VERTEX_MIN))
    try:
        pts, _, vfix = hbl.fix_faces(pts, perim, prot, clip=perimeter_clip(perim),
                                     perimeter_tol=ptol)
    except ImportError:                                 # pragma: no cover - no scipy
        vfix = None
    if vfix is not None:
        if vfix["nudged"] or vfix["removed"] or vfix.get("added"):
            notes.append("Voronoi pre-check: {:,} computation points nudged, {:,} removed "
                         "and {:,} added (a big cell split in two) that would have made cells "
                         "with more than {} faces (HEC-RAS's limit).".format(
                             vfix["nudged"], vfix["removed"], vfix.get("added", 0),
                             hbl.MAX_FACES))
            log(notes[-1])
        pf = vfix["perimeter_found"]
        if vfix["perimeter_nudged"] or vfix["perimeter_removed"]:
            notes.append("Perimeter pre-check: found {} cell(s) cut in two by the perimeter, "
                         "{} perimeter face(s) shorter than {:g} {u} (or 2 % of the spacing) "
                         "and {} perimeter corner(s) within {:g} {u} of a cell edge; {:,} "
                         "point(s) nudged and {:,} removed to clear them (HEC-RAS stops with "
                         "'face errors detected on the perimeter of the mesh').".format(
                             pf.get("split_cells", 0), pf.get("short_perimeter_faces", 0),
                             round(ptol[0], 2), pf.get("perimeter_corners_near_edge", 0),
                             round(ptol[1], 2), vfix["perimeter_nudged"],
                             vfix["perimeter_removed"], u=units.len_name))
            log(notes[-1])
        vc = vfix["check"]
        if vfix.get("perimeter_corners_left"):
            notes.append("WARNING: Perimeter pre-check: {} perimeter corner(s) still within {:g} "
                         "{} of a cell edge; HEC-RAS may stop with 'face errors detected on the "
                         "perimeter of the mesh'.".format(vfix["perimeter_corners_left"],
                                                          round(ptol[1], 2), units.len_name))
            log(notes[-1])
        if vfix["left"] or vc["facepoints_over"] or vfix["perimeter_left"]:
            raise HecRasModelError(
                "the computation points still make {:,} cells with more than {} faces, "
                "{:,} face points shared by more than 8 cells and {:,} perimeter cells that "
                "are cut in two or have a sliver perimeter face, after {:,} were nudged and "
                "{:,} removed; HEC-RAS would refuse this mesh. Build with breaklines=False or "
                "mesh_seeds='grid', or a larger cell size.".format(
                    vfix["left"], hbl.MAX_FACES, vc["facepoints_over"], vfix["perimeter_left"],
                    vfix["nudged"] + vfix["perimeter_nudged"],
                    vfix["removed"] + vfix["perimeter_removed"]))
        notes.append("Voronoi pre-check: {:,} cells ({:,} inside, {:,} clipped to the "
                     "perimeter), most sides {}, 0 over {}; face points: most {} cells, 0 over "
                     "8; perimeter: 0 cells cut in two, shortest perimeter face {} {u}, "
                     "nearest corner to a cell edge {} {u}.".format(
                         vc["cells"], vc["interior"], vc["boundary"], vc["max_sides"],
                         hbl.MAX_FACES, vc["max_cells_per_facepoint"],
                         vc.get("min_perimeter_face"), vc.get("min_corner_to_edge"),
                         u=units.len_name))
        log(notes[-1])
    # v2d: HEC-RAS's 'weir breach lower than the cells they are connected to', checked before
    # HEC-RAS (Absaraka v2c / v2c_gmsh: cell 62974 / 64879 above the invert at station 809.6,
    # volume errors 38.7 % / >= 12 %)
    # (not for hecras_engine's hydrograph-inflow plan, which has no breach)
    bcc = None if scope == "hydrograph" else breach_cells_precheck(
        pts, perim, conn_line, sc, bvals, top_u, terrain_tif, srs, cell, o,
        bool(cut and "corner_margin" in cut), units)
    if bcc is not None:
        site["breach_cells"] = {k: v for k, v in bcc.items() if k != "cells"}
        notes.append(bcc["note"])
        log(notes[-1])
        record_run_report(os.path.join(model_dir, hecras.RUN_REPORT_JSON),
                          "hecras_breach_cells", bcc, log)
        if bcc["action"] == "fail":
            raise HecRasModelError(bcc["note"] + " Build with breach_cell_check='warn' to write "
                                   "the model anyway.")
    # 3.10.54: HEC-RAS's 'SA 2D Connection ... has a weir elevation lower than the cells', checked
    # before HEC-RAS (check only: v2e_gmsh_r's notch at 1066.0 ft fronted cells at 1066.61 /
    # 1066.25 ft; the terrain and the notch are not changed here)
    # 3.10.54 review: never aborts the build in 'warn' or 'auto' mode; an error in the check itself
    # is a note and run_report.json 'hecras_weir_cells' {'action': 'not run', 'error': ...}
    wmode = weir_cell_check_mode(o)
    # 3.10.54 review 2: a bad weir_cell_fail_ft is a settings error (raised here, before the
    # guarded check), not "pre-check not run"
    if wmode != "off" and o.get("weir_cell_fail_ft") not in (None, ""):
        weir_cell_fail_ft(o)
    try:
        wcc = weir_cells_precheck(pts, perim, conn_line, weir, top_u, terrain_tif, srs, cell, o,
                                  units)
    except Exception as e:                                       # noqa: BLE001
        if wmode == "fail":
            raise
        wcc = {"ok": None, "mode": wmode, "action": "not run",
               "error": "{}: {}".format(type(e).__name__, e)[:500],
               "note": "Weir-cell pre-check not run ({}: {}); the build carries on. HEC-RAS "
                       "still checks the dam connection itself.".format(type(e).__name__,
                                                                       str(e)[:200])}
    if wcc is not None:
        site["weir_cells"] = {k: v for k, v in wcc.items() if k != "cells"}
        notes.append(wcc["note"])
        log(notes[-1])
        record_run_report(os.path.join(model_dir, hecras.RUN_REPORT_JSON),
                          "hecras_weir_cells", wcc, log)
        if wcc["action"] == "fail":
            raise HecRasModelError(wcc["note"] + " Build with weir_cell_check='warn' to write "
                                   "the model anyway, or raise weir_cell_fail_ft (now {} ft) if "
                                   "this margin is acceptable.".format(weir_cell_fail_ft(o)))
    n_faces = vfix["removed"] if vfix else 0
    seeds_info["removed_for_faces"] = int(n_faces)
    seeds_info["nudged_for_faces"] = int(vfix["nudged"]) if vfix else 0
    seeds_info["over_8_faces_left"] = int(vfix["left"]) if vfix else None
    if bl_info is not None:
        bl_info["points"] = int(len(pts))
    mc = mesh_check(perim, pts, cell)
    if mc is not None and vfix is not None:
        mc = dict(mc, voronoi_precheck=vfix["check"], nudged=vfix["nudged"],
                  removed=vfix["removed"], perimeter_found=vfix["perimeter_found"],
                  perimeter_nudged=vfix["perimeter_nudged"],
                  perimeter_removed=vfix["perimeter_removed"])
    # v3.10.22: the 2D area's nominal cell size is the median spacing of the points (gmsh)
    nominal_cell = cell
    if seeds_info.get("used") == "gmsh":
        sp_ = hseeds.spacing_stats(pts, perim)
        if sp_:
            seeds_info["spacing"] = sp_
            nominal_cell = sp_["median"]

    # --- v3.12.0: the run's culverts, bridges and notches inside the 2D area -------
    # 3.10.56: or those of another run (structures_from, e.g. the ANUGA run of the dam); the
    # loop lives in hecras_structures.connections (shared with add_to_package)
    from . import hecras_structures as hstr
    structs, ssrc, s_to_model = _model_structures(o, report, to_model, srs, log)
    if structs:
        progress(84, "culverts and bridges")
    sinfo = dict(hstr.connections(structs, s_to_model, perim, cell, terrain_tif, srs, units, log),
                 source=ssrc)
    # 3.10.56: faces on every connection line (breaklines built into the points), then the
    # weir and culvert inverts no lower than the cells they connect (HEC-RAS refuses them)
    if sinfo["exported"] and o.get("structure_breaklines", True):
        pts, sbl = hstr.connection_breaklines(pts, perim, sinfo["exported"], cell, units,
                                              conn_line, clip=perimeter_clip(perim),
                                              banded=banded)
        sinfo["breaklines"] = sbl
        mc = mesh_check(perim, pts, cell)
        notes.append("Structure breaklines: {} connection line(s) built into the computation "
                     "points ({:,} removed, {:,} added; {:,} points now){}.".format(
                         sbl["lines"], sbl.get("points_removed", 0), sbl.get("points_added", 0),
                         sbl["points"], "".join("; no breakline on {} ({})".format(
                             k["name"], k["why"]) for k in sbl["skipped"])))
        log(notes[-1])
    if sinfo.get("renamed"):
        notes.append("Structures: {} connection name(s) had characters HEC-RAS refuses (\"/\" "
                     "in OpenStreetMap ids) replaced: {}.".format(len(sinfo["renamed"]), "; ".join(
                         "'{}' = {}".format(x["name"], x["source_name"])
                         for x in sinfo["renamed"])))
        log(notes[-1])
    if sinfo["exported"]:
        # 3.10.75: each connection line on the cell faces HEC-RAS would make (DWR-2801: lines
        # 10-90 % on its faces where the old check said 100 %)
        aligned = (sinfo.get("breaklines") or {}).get("aligned") or hstr.alignment(
            pts, sinfo["exported"], units, cell=cell)
        sinfo["aligned"] = aligned
        a_warn, a_fail = hstr.alignment_notes(aligned)
        if a_warn:
            notes.append("Structures: connection line(s) under {:.0%} on cell faces (HEC-RAS "
                         "may refuse them or move flow off the line; check them in RAS Mapper): "
                         "{}.".format(hstr.ALIGN_WARN, ", ".join(a_warn)))
            log(notes[-1])
        if a_fail:
            # without the structure breaklines (structure_breaklines=False, the user's own
            # choice) it is only reported
            if (str(o.get("structure_align_check", "fail")).lower() == "warn"
                    or not o.get("structure_breaklines", True)):
                notes.append("Structures: NO cell faces along: " + "; ".join(a_fail) + ".")
                log(notes[-1])
            else:
                raise HecRasModelError(hstr.PROBLEMS_HEAD + "; ".join(a_fail) + " (build with "
                                       "structure_align_check='warn' to write it anyway)")
    if sinfo["exported"] and o.get("structure_cell_fit", True):
        traster = hstr.read_raster(terrain_tif)
        sck = hstr.cells_check(pts, perim, sinfo["exported"], traster)
        sinfo["fit"] = hstr.fit_to_cells(sinfo["exported"], sck, units)
        sck2 = hstr.cells_check(pts, perim, sinfo["exported"], traster) if sinfo["fit"] else sck
        sinfo["cells_check"] = hstr.check_summary(sck2)
        # 3.10.56: what HEC-RAS would refuse (names, line, barrel ends, inverts) stops the build
        probs = hstr.connection_problems(sinfo["exported"], sck2, perim,
                                         other_names=[AREA_NAME, SA_NAME, CONN_NAME], cell=cell)
        if probs:
            raise HecRasModelError(hstr.PROBLEMS_HEAD + "; ".join(probs))
        if sinfo["fit"]:
            notes.append("Structures: {} weir stretch(es) / culvert invert(s) raised to the "
                         "lowest ground of the cell they connect (HEC-RAS refuses a weir or a "
                         "culvert below its cells): {}.".format(len(sinfo["fit"]), "; ".join(
                             hstr.change_text(x, units) for x in sinfo["fit"])))
            log(notes[-1])

    # --- simulation timing ---------------------------------------------------------
    summ = report.get("summary") or {}
    arr = summ.get("latest_arrival_hr") or 0.0
    # v3.10.17: from the end of the breach hydrograph and the latest arrival, plus a drain
    # allowance, capped (was 1.25 x the longer of the record and arrival + formation + 2 h)
    t_end = hydrograph_end_hr(hy.t_s, hy.q_cms, hy.t_ref_s) if len(hy.t_s) else 6.0
    if o.get("sim_hours"):
        sim, sim_how = float(o["sim_hours"]), "chosen"
    else:
        sim, sim_how = simulation_hours(t_end, float(arr), bvals["formation_hr"],
                                        cap=float(o.get("sim_cap_hr") or SIM_CAP_HR))
    vmax_fps = float(summ.get("max_velocity_fps") or 10.0)
    hmax_ft = float(summ.get("max_depth_ft") or dmax or 10.0)
    cour = o["courant"] or (1.0 if o["equation"] == EQ_SWE else 2.0)
    # HEC-RAS's Courant number is V dt / dx (velocity, not wave celerity)
    cell_ft = cell * units.L / FT
    dt = cour * cell_ft / max(vmax_fps, 5.0)
    dt_s, dt_lab = comp_interval(dt)
    # v3.10.22: when the breach starts. Sunny day: at a set time after a short warm-up with the
    # pool at its starting level (the plugin's breach starts at its t = 0 with the pool there,
    # so the HEC-RAS times are the plugin's plus the warm-up); flood: the run's trigger level
    scen = report.get("scenario")
    smode = str(o.get("breach_start") or ("wse" if scen == "flood" else "time")).strip().lower()
    if smode not in BREACH_START_MODES:
        raise HecRasModelError("breach_start must be 'time' or 'wse' (got {!r})".format(smode))
    start_hr = None
    if smode == "time":
        if scen == "flood":
            start_hr = round(float(hy.t_ref_s or 0.0) / 60.0) / 60.0
        else:
            w = float(o.get("breach_warmup_hr") if o.get("breach_warmup_hr") is not None
                      else BREACH_WARMUP_HR)
            if not 0.0 <= w <= 6.0:
                raise HecRasModelError("breach_warmup_hr must be 0 to 6 h (got {:g})".format(w))
            start_hr = round(w * 60.0) / 60.0
            if not o.get("sim_hours"):
                sim += start_hr
                sim_how = (sim_how or "") + "; plus the {:g} h warm-up before the breach".format(
                    start_hr)
        if o.get("sim_hours") and sim <= start_hr:
            raise HecRasModelError("sim_hours ({:g}) must be longer than the breach start "
                                   "({:g} h)".format(sim, start_hr))
    bvals["start_mode"], bvals["start_hr"] = smode, start_hr

    # --- inflow (flood scenario) ---------------------------------------------------
    inflow_series = None
    flood = settings.get("flood")
    if report.get("scenario") == "flood":
        src = (settings.get("input_files") or {}).get("PMF inflow hydrograph")
        if src and os.path.exists(src):
            t_h, q_cfs = brmod.read_csv_series(src, "inflow hydrograph")
            step_min = 15 if (t_h[-1] - t_h[0]) <= 120 else 60
            tt = np.arange(0.0, sim + step_min / 60.0, step_min / 60.0)
            qq = np.interp(tt, t_h - t_h[0], q_cfs, right=float(q_cfs[-1]))
            inflow_series = (hecras.ras_interval_label(step_min),
                             [round(units.flow(v * CFS), 2) for v in qq])
        else:
            notes.append("Flood scenario: the inflow hydrograph file was not found; the "
                         "reservoir gets the base flow only. Enter the inflow in HEC-RAS.")
        notes.append("Flood scenario: the outlets are not in the HEC-RAS dam; the spillway "
                     "is the notch in the dam weir." if spill_info else
                     "Flood scenario: the spillway and outlets are not in the HEC-RAS dam "
                     "(the weir is the top of dam); add them to the connection, or give "
                     "spillway_crest_ft and spillway_width_ft.")
        if flood and flood.get("normal_pool_ft") is not None and inflow_series:
            wse0_ft = float(flood["normal_pool_ft"])

    hold = None
    if (spill_info and inflow_series is None and smode == "time" and start_hr and
            o.get("warmup_hold", True)):
        # the spillway runs from the start; an inflow equal to its flow at the starting pool
        # until the breach starts keeps the pool there (and wets the channel below it); then
        # no inflow (sunny day)
        hold = weir_flow(spill_info["weir_coef"], spill_info["width"],
                         units.from_ft(wse0_ft) - spill_info["crest"])
        inflow_series = warmup_inflow(sim, start_hr, hold,
                                      units.flow(o["base_flow_cfs"] * CFS))
        spill_info["warmup_inflow"] = round(hold, 1)
        notes.append("Reservoir inflow {:,.0f} {} (the spillway's flow at the starting pool) "
                     "until the breach starts at {:g} h, then none: the pool stays at its "
                     "starting level through the warm-up.".format(hold, units.q_name, start_hr))

    # --- the model -----------------------------------------------------------------
    m = Model()
    m.name = safe_name(dam.dam_id)
    m.title = "{} breach".format(dam.dam_id)[:60]
    m.units = units
    m.perimeter = [tuple(p) for p in perim]
    m.points = pts
    m.breaklines = bl_lines + band_g            # v3.10.22; 3.10.76: the line bands
    m.conn_cellsize = bl_dam
    m.cell = cell
    m.nominal_cell = nominal_cell                 # v3.10.22: median point spacing (gmsh)
    m.manning_n = float(o["manning_n"] or settings.get("manning_n") or 0.06)
    m.reservoir = [tuple(p) for p in reservoir]
    ev, res = storage_curve(dam, units)
    m.elev_volume = ev
    m.conn_line = conn_line
    m.weir = weir
    m.weir_width = units.from_ft(dam.crest_width_ft or 20.0)
    m.weir_coef = units.weir_coef(2.6)
    m.bc_line = [tuple(p) for p in bc]
    m.bc_slope = slope
    m.initial_wse = units.from_ft(wse0_ft)
    m.inflow = inflow_series
    m.base_flow = units.flow(o["base_flow_cfs"] * CFS)
    m.sim_hours = sim
    m.comp_interval = dt_lab
    ts = dict(COURANT_DEFAULTS)
    ts.update({k: v for k, v in (o.get("time_step") or {}).items() if v is not None})
    if ts["max"] is None:
        ts["max"] = cour
    if ts["min"] is None:
        ts["min"] = round(COURANT_MIN_RATIO * float(ts["max"]), 4)
    m.courant = ts
    m.equation = o["equation"]
    m.breach = bvals
    m.terrain_name = terrain_name
    m.description = ["Built by the ND Dam Breach Runner from the run in {}.".format(
        os.path.basename(os.path.normpath(dam_folder)))]
    m.structures = sinfo["exported"]            # v3.12.0
    tol = o.get("tolerances") or "default"      # v3.10.22
    if isinstance(tol, dict):
        tname, tvals = "custom", dict(TOLERANCES["default"], **tol)
    elif str(tol).lower() in TOLERANCES:
        tname, tvals = str(tol).lower(), dict(TOLERANCES[str(tol).lower()])
    else:
        raise HecRasModelError("tolerances must be one of {} or a dict (got {!r})".format(
            ", ".join(TOLERANCES), tol))
    m.tolerances = tvals
    # v3.10.22: the plugin's routing on this plan's breach, to compare the breach flow with
    try:
        pref = plugin_reference(dam, params, bvals, units, start_hr or 0.0)
    except (brmod.BreachInputError, ValueError, TypeError, KeyError) as e:
        pref = None
        notes.append("The plugin's hydrograph on the HEC-RAS breach was not computed: {}"
                     .format(e))

    # --- write -------------------------------------------------------------------
    progress(88, "writing files")
    paths = write_text_files(m, out)
    esri = srs.Clone()
    esri.MorphToESRI()
    paths["projection"] = os.path.join(out, "GIS", m.projection_name)
    with open(paths["projection"], "w", encoding="utf-8") as f:
        f.write(esri.ExportToWkt())
    # terrain GeoTIFF: only what the model needs (2D area and reservoir) plus a margin
    paths["terrain_tif"] = terrain_tif
    # v3.10.56: the terrain must still carry the model's EPSG code and unit (a rewrite without
    # the PROJ database gave an unnamed metre CRS and HEC-RAS's terrain step failed)
    from . import hecras_terrain_crs as htc
    htc.check_terrain(terrain_tif, paths["projection"])
    paths["layout"] = write_layout(os.path.join(out, "GIS", "HECRAS Layout.gpkg"), m, srs,
                                   bc_slope_how=how, P=P, skipped=sinfo["skipped"])
    # v3.10.22: the dam line and the breach along it, in the README and run_report.json
    crest_rec.update(final_breach_station=round(float(sc), 1),
                     final_breach_xy=[round(float(v), 2) for v in (Ob + abb * sc)],
                     breach_frame={"along_bearing_deg": round(hcrest.bearing_deg(abb), 1),
                                   "downstream_bearing_deg": round(hcrest.bearing_deg(db), 1)},
                     connection_length=round(float(dam_len), 1), units=units.len_name,
                     model_crs=_epsg_text(srs))
    if crest_line is not None:
        notes.append("Breach centre at station {:,.1f} {u} of the dam line: {}. The crest there "
                     "runs {:.0f} degrees off the straight line across the flow (bearing {:.0f} "
                     "against {:.0f}); the breach cut is a rectangle across the crest, "
                     "downstream at bearing {:.0f}.".format(
                         sc, crest_rec.get("breach_how"), crest_rec.get("skew_deg") or 0.0,
                         crest_rec.get("crest_bearing_deg") or 0.0,
                         crest_rec.get("straight_bearing_deg") or 0.0,
                         hcrest.bearing_deg(db), u=units.len_name))
        log(notes[-1])
    record_run_report(os.path.join(model_dir, hecras.RUN_REPORT_JSON), "hecras_dam_line",
                      dict(crest_rec, folder=out), log)
    info = {"crs": _epsg_text(srs), "crs_why": why, "units": units.label,
            "len_name": units.len_name, "cell": cell, "cell_chosen": bool(o["cell_ft"]),
            "cells": int(len(pts)), "area": abs(ring_area(perim)),
            "dam_line": dam_len, "terrain_cell": tcell, "dem_z": znote,
            "comp_interval": dt_lab, "time_step": dict(m.courant), "sim_hours": sim, "bc_slope": slope,
            "bc_slope_how": how, "centre_how": centre_how, "initial_wse": m.initial_wse,
            "breach": bvals,
            "side_buffer": side, "end_buffer": end, "weir_points": len(weir),
            "storage_exponent": res.m, "manning_n": m.manning_n,
            "equation": EQUATIONS[m.equation][1], "scenario": report.get("scenario"),
            "edits": settings.get("edits"), "dem": dem_path, "mesh_check": mc,
            "structures": sinfo,                # v3.12.0
            # HEC-RAS 2D engine (hecras_engine): the inflow point and the CRSs, for the
            # hydrograph-inflow mode and for putting the results back on the run's grid
            "inflow_xy_model": (None if inflow_m is None else [float(v) for v in inflow_m]),
            "inflow_status": inflow_status,
            # v3.10.17
            "site": site, "sim_how": sim_how, "hydrograph_end_hr": round(t_end, 2),
            "dam_line_how": dam_line_how, "breach_shape_note": BREACH_SHAPE_NOTE,
            "dam_axis": crest_rec,              # v3.10.22
            # v3.10.22
            "breach_start": {"mode": smode, "start_hr": start_hr,
                             "trigger_wse": bvals.get("trigger_wse")},
            "hec_breach_mode": hmode, "spillway": spill_info,
            "tolerances": {"name": tname, "values": tvals}, "mesh_seeds": seeds_info,
            "breaklines": bl_info,
            "line_bands": band_info,            # 3.10.76
            "plugin_reference": (None if pref is None else
                                 {k: v for k, v in pref.items() if k not in ("t_hr", "q_cfs")}),
            "model_wkt": srs.ExportToWkt(),
            "work_wkt": (work.ExportToWkt() if work is not None else None)}
    paths["readme"] = write_readme(os.path.join(out, "README_HECRAS.md"), m, info, notes, dam,
                                   params)
    record_inflow_status(os.path.join(model_dir, hecras.RUN_REPORT_JSON), inflow_status,
                         srs, work, units, log)
    # v3.10.22: the mesh-point setting (and the gmsh version) in the run's run_report.json
    record_run_report(os.path.join(model_dir, hecras.RUN_REPORT_JSON), "hecras_mesh_seeds",
                      dict(seeds_info, folder=out), log)
    progress(100, "done")
    return {"folder": out, "paths": paths, "model": m, "info": info, "notes": notes,
            "plugin_reference": pref}


BREACH_CELL_CHECK_MODES = ("auto", "fail", "warn", "off")


def _breach_cells_row(bc, L):
    if not bc:
        return "not run"
    return "{}: {} cell(s) on the bottom, stations {:,.1f} to {:,.1f}, invert {:.2f} {u}, " \
        "highest {:+.2f} {u}; {} above it".format(
            bc.get("action", "?").upper(), bc.get("bottom_cells"), bc["reach"][0], bc["reach"][1],
            bc["invert"], bc.get("worst_above") or 0.0, bc.get("bad"), u=L)


def breach_cells_precheck(pts, perim, conn_line, sc, bvals, top_u, terrain_tif, srs, cell, o,
                          cut_on, units):
    """v2d: every cell fronting the final breach bottom on the dam connection must reach the
    final invert (hecras_breaklines.breach_cell_check on the written terrain). Option
    breach_cell_check: 'auto' (default: fail when the corner-centred dam row and the corner
    cut were built, which should always make them reach it; warn otherwise, e.g. without
    breaklines or the cut), 'fail', 'warn' or 'off'. Returns the result
    with 'action' ('ok', 'warn' or 'fail') and 'note', or None when off or not possible."""
    from . import hecras_breaklines as hbl
    mode = str(o.get("breach_cell_check") or "auto").strip().lower()
    if mode not in BREACH_CELL_CHECK_MODES:
        raise HecRasModelError("breach_cell_check must be one of {} (got {!r})".format(
            ", ".join(BREACH_CELL_CHECK_MODES), mode))
    if mode == "off" or not bvals or bvals.get("bottom_width") is None:
        return None
    half = 0.5 * float(bvals["bottom_width"])
    pad = 4.0 * cell + float(bvals.get("left_slope") or 0) * max(0.0, top_u - bvals["bottom_elev"])
    cl = np.asarray(conn_line, float)
    lo_xy = cl.min(axis=0) - pad
    hi_xy = cl.max(axis=0) + pad
    try:
        ras = _tif_window(terrain_tif, (lo_xy[0], lo_xy[1], hi_xy[0], hi_xy[1]), srs,
                          max_cells=6000)
        r = hbl.breach_cell_check(pts, perim, cl, sc - half, sc + half, bvals["bottom_elev"],
                                  ras, top=top_u,
                                  side_slope=float(bvals.get("left_slope") or 0) or None,
                                  tol=units.from_ft(hbl.BREACH_CELL_TOL),
                                  station_tol=units.from_ft(hbl.BREACH_STATION_TOL),
                                  step=units.from_ft(0.1))
    except ImportError:                                 # pragma: no cover - no scipy
        return None
    r["terrain_cell"] = round(float(ras[3]), 3)
    r["mode"] = mode
    if r["ok"]:
        r["action"] = "ok"
    elif mode == "fail" or (mode == "auto" and cut_on):
        r["action"] = "fail"
    else:
        r["action"] = "warn"
    L = units.len_name
    if r["ok"]:
        r["note"] = ("Breach-cell pre-check: OK, the {} cell(s) fronting the final breach bottom "
                     "(stations {:,.1f} to {:,.1f}) reach the invert {:.2f} {u} (highest {:+.2f} "
                     "{u}); HEC-RAS should not warn 'weir breach lower than the cells they are "
                     "connected to'.".format(r["bottom_cells"], r["reach"][0], r["reach"][1],
                                             r["invert"], r["worst_above"] or 0.0, u=L))
    else:
        r["note"] = ("Breach-cell pre-check {}: {} cell(s) fronting the final breach bottom "
                     "(stations {:,.1f} to {:,.1f}) sit above the invert {:.2f} {u}: {}. HEC-RAS "
                     "warns 'weir breach lower than the cells they are connected to' and the run "
                     "can lose or make water (Absaraka v2c: 38.7 % volume error).".format(
                         "FAILED" if r["action"] == "fail" else "WARNING", r["bad"],
                         r["reach"][0], r["reach"][1], r["invert"],
                         "; ".join("cell {} (stations {:,.1f}-{:,.1f}) lowest ground {:.2f}, "
                                   "{:+.2f} {u}".format(c["cell"], c["from"], c["to"],
                                                        c["min_elev"], c["above"], u=L)
                                   for c in r["bad_cells"]), u=L))
    r.pop("bad_cells", None)
    return r


WEIR_CELL_CHECK_MODES = ("warn", "auto", "fail", "off")
WEIR_CELL_TOL_FT = 0.01
WEIR_CELL_AUTO_FAIL_FT = 0.1     # 'auto' fails above this (c4: +0.25 / +0.61 ft), warns below


def weir_cell_check_mode(o):
    """The weir_cell_check option, validated ('warn' when not set)."""
    mode = str(o.get("weir_cell_check") or "warn").strip().lower()
    if mode not in WEIR_CELL_CHECK_MODES:
        raise HecRasModelError("weir_cell_check must be one of {} (got {!r})".format(
            ", ".join(WEIR_CELL_CHECK_MODES), mode))
    return mode


def weir_cell_fail_ft(o):
    """'auto' threshold in ft (option weir_cell_fail_ft, default WEIR_CELL_AUTO_FAIL_FT)."""
    v = o.get("weir_cell_fail_ft")
    if v is None or v == "":
        return WEIR_CELL_AUTO_FAIL_FT
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise HecRasModelError("weir_cell_fail_ft must be a number of feet (got {!r})".format(v))
    if not (v >= 0.0 and np.isfinite(v)):
        raise HecRasModelError("weir_cell_fail_ft must be >= 0 ft (got {!r})".format(v))
    return v


def weir_cell_action(mode, bad, fail_above):
    """Pure: 'ok' / 'warn' / 'fail' for the bad rows ('above' in model units). 'auto' fails only
    when a cell sits more than `fail_above` (model units) above the weir."""
    if not bad:
        return "ok"
    if mode == "fail":
        return "fail"
    if mode == "auto" and max(r["above"] for r in bad) > fail_above:
        return "fail"
    return "warn"


def weir_cell_rows(runs, zmin, weir, tol=0.0):
    """Pure: for each connection cell run (cell, station from, station to) with a lowest-ground
    value in `zmin`, the lowest weir elevation over its face (the profile interpolated at both
    ends and any vertex between) and how far the cell sits above it. HEC-RAS refuses a dam
    connection whose weir is lower than a cell it connects to ('SA 2D Connection ... has a weir
    elevation lower than the cells ... This is not allowed', run queue c4_hecras_v2e_gmsh_r).
    Returns (rows, bad rows)."""
    st = np.array([a for a, _ in weir], float)
    el = np.array([b for _, b in weir], float)
    rows = []
    for c, a, b in runs:
        z = zmin.get(int(c))
        if z is None or not np.isfinite(z):
            continue
        inner = el[(st > a) & (st < b)]
        w = float(min(np.interp([a, b], st, el).min(), inner.min() if len(inner) else np.inf))
        rows.append({"cell": int(c), "from": round(float(a), 2), "to": round(float(b), 2),
                     "weir_min": round(w, 3), "min_elev": round(float(z), 3),
                     "above": round(float(z) - w, 3)})
    bad = [r for r in rows if r["above"] > tol]
    return rows, bad


def weir_cells_precheck(pts, perim, conn_line, weir, top_u, terrain_tif, srs, cell, o, units):
    """3.10.54, CHECK ONLY (the terrain and the weir are not changed): the cells along the dam
    connection where the weir is below the top of dam (the spillway notch; elsewhere the weir is
    the top of dam or the ground above it) against their lowest ground on the written terrain,
    as HEC-RAS checks them. Option weir_cell_check: 'warn' (default; a note and
    run_report.json 'hecras_weir_cells'), 'auto' (fail only when a cell is more than
    weir_cell_fail_ft, default 0.1 ft, above the weir; warn below that), 'fail' (stop the
    build) or 'off'. Returns the result
    with 'action' and 'note', or None when off, without a notch or not possible."""
    from . import hecras_breaklines as hbl
    mode = weir_cell_check_mode(o)
    if mode == "off" or not weir:
        return None
    st = np.array([a for a, _ in weir], float)
    el = np.array([b for _, b in weir], float)
    below = top_u - units.from_ft(0.05)       # the weir is written to 0.01; notches are far lower
    if not (el < below).any():
        return None
    try:
        runs = hbl.connection_cells(pts, perim, conn_line, units.from_ft(0.1))
    except ImportError:                                 # pragma: no cover - no scipy
        return None
    low = []
    for c, a, b in runs:
        inner = el[(st > a) & (st < b)]
        w = min(np.interp([a, b], st, el).min(), inner.min() if len(inner) else np.inf)
        if w < below:
            low.append((c, a, b))
    if not low:
        return None
    cl = np.asarray(conn_line, float)
    seg = np.hypot(*np.diff(cl, axis=0).T)
    along = np.r_[0.0, np.cumsum(seg)]
    ss = np.array([x for _, a, b in low for x in (a, b)])
    xy = np.column_stack([np.interp(ss, along, cl[:, 0]), np.interp(ss, along, cl[:, 1])])
    pad = 6.0 * cell
    lo_xy, hi_xy = xy.min(axis=0) - pad, xy.max(axis=0) + pad
    ras = _tif_window(terrain_tif, (lo_xy[0], lo_xy[1], hi_xy[0], hi_xy[1]), srs, max_cells=6000)
    zmin = hbl.cell_min_elevations(pts, [c for c, _, _ in low], ras, perim)
    tol = units.from_ft(WEIR_CELL_TOL_FT)
    fail_above = units.from_ft(weir_cell_fail_ft(o))
    rows, bad = weir_cell_rows(low, zmin, weir, tol)
    L = units.len_name
    r = {"ok": not bad, "mode": mode, "cells": rows, "bad": len(bad), "tol": tol,
         "terrain_cell": round(float(ras[3]), 3),
         "action": weir_cell_action(mode, bad, fail_above)}
    if mode == "auto":
        r["fail_above"] = round(float(fail_above), 3)
    if not bad:
        r["note"] = ("Weir-cell pre-check: OK, the {} cell(s) along the dam connection where the "
                     "weir is below the top of dam (the spillway notch) reach the weir (highest "
                     "{:+.2f} {u}).".format(len(rows), max(x["above"] for x in rows) if rows
                                             else 0.0, u=L))
    else:
        r["note"] = ("Weir-cell pre-check {}: {} cell(s) along the dam connection sit above the "
                     "weir: {}. HEC-RAS stops with 'SA 2D Connection at Dam has a weir elevation "
                     "lower than the cells ... This is not allowed' (run queue "
                     "c4_hecras_v2e_gmsh_r). Lower the terrain in those cells to the weir, or "
                     "check the spillway crest and stations; the builder does not change "
                     "either.".format(
                         "FAILED" if r["action"] == "fail" else "WARNING", len(bad),
                         "; ".join("cell {} (stations {:,.1f}-{:,.1f}) lowest ground {:.2f}, "
                                   "weir {:.2f}, {:+.2f} {u}".format(
                                       x["cell"], x["from"], x["to"], x["min_elev"],
                                       x["weir_min"], x["above"], u=L) for x in bad), u=L))
    return r


def mesh_check(perimeter, points, cell):
    """A stand-in for HEC-RAS's mesh checks on the cells along the perimeter: the Voronoi
    cell of each point clipped to the perimeter. Returns {'cells_checked', 'max_faces',
    'over_8_faces', 'smallest_fraction'} or None without scipy."""
    try:
        from scipy.spatial import Voronoi
    except ImportError:
        return None
    pts = np.asarray(points, float)
    per = np.asarray(perimeter, float)
    poly = _polygon(per)
    # far-away mirror points close every region
    xmin, ymin = per.min(axis=0) - 10 * cell
    xmax, ymax = per.max(axis=0) + 10 * cell
    frame = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
    from . import hecras_breaklines as _hbl
    vor = _hbl.centred_voronoi(np.vstack([pts, frame]))  # 3.10.82: qhull round-off
    d = point_segment_distance(pts[:, 0], pts[:, 1], list(map(tuple, per)) + [tuple(per[0])])
    edge = np.nonzero(d < 1.5 * cell)[0]
    worst, over, small = 0, 0, 1.0
    for i in edge:
        reg = vor.regions[vor.point_region[i]]
        if not reg or -1 in reg:
            continue
        cellpoly = _polygon(vor.vertices[reg]).Intersection(poly)
        if cellpoly is None or cellpoly.IsEmpty():
            continue
        rings = _rings(cellpoly)
        if not rings:
            continue
        nf = len(rings[0][1])
        worst = max(worst, nf)
        over += nf > 8
        small = min(small, cellpoly.GetArea() / cell ** 2)
    return {"cells_checked": int(len(edge)), "max_faces": int(worst), "over_8_faces": int(over),
            "smallest_fraction": round(float(small), 3)}


def write_terrain(dem_path, path, srs, cell, bounds, factor, src_nodata, zunit, rows=512):
    """The DEM warped into the model CRS on a `cell` grid over `bounds`, elevations times
    `factor` (to project units), as a tiled Float32 GeoTIFF. The warp is a virtual raster
    read in blocks of rows, so a large terrain never has to fit in memory."""
    gdal, _, _ = _ogr()
    from ..damsuite import gdalsafe as GS
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(path):
        drv.Delete(path)
    kw = dict(format="VRT", dstSRS=srs.ExportToWkt(), xRes=cell, yRes=cell,
              outputBounds=bounds, resampleAlg="bilinear", dstNodata=-9999.0,
              outputType=gdal.GDT_Float32)
    if src_nodata is not None:
        kw["srcNodata"] = src_nodata
    from .warp_opts import no_overviews, no_vshift   # 3.10.43: the terrain from the full-res DEM
    # 3.10.89: values as stored (no vertical shift): `factor` is the only unit conversion
    vrt = GS.checked(gdal.Warp("", dem_path, options=gdal.WarpOptions(
        **no_vshift(gdal, no_overviews(gdal, kw)))), "reprojecting the DEM {}".format(dem_path))
    nx, ny = vrt.RasterXSize, vrt.RasterYSize
    ds = GS.checked(drv.Create(path, nx, ny, 1, gdal.GDT_Float32,
                               options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3",
                                        "BIGTIFF=IF_SAFER"]), "creating {}".format(path))
    ds.SetGeoTransform(vrt.GetGeoTransform())
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    src = vrt.GetRasterBand(1)
    for r0 in range(0, ny, rows):
        n = min(rows, ny - r0)
        a = src.ReadAsArray(0, r0, nx, n).astype(np.float32)
        ok = a != -9999.0
        if abs(factor - 1.0) > 1e-12:
            a[ok] = a[ok] * np.float32(factor)
        b.WriteArray(a, 0, r0)
    b.SetUnitType(zunit)
    b.FlushCache()
    ds = vrt = None
    return path


def _write_tif(path, a, x0, y0, cell, srs, zunit):
    gdal, _, _ = _ogr()
    from ..damsuite import gdalsafe as GS
    drv = gdal.GetDriverByName("GTiff")
    if os.path.exists(path):
        drv.Delete(path)
    ds = GS.checked(drv.Create(path, a.shape[1], a.shape[0], 1, gdal.GDT_Float32,
                               options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=3",
                                        "BIGTIFF=IF_SAFER"]), "creating {}".format(path))
    ds.SetGeoTransform((x0, cell, 0.0, y0, 0.0, -cell))
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.SetUnitType(zunit)
    b.WriteArray(np.where(np.isfinite(a), a, -9999.0).astype(np.float32))
    b.FlushCache()
    ds = None
    return path


def write_layout(path, m, srs, bc_slope_how="", P=None, skipped=()):
    """The model's geometry as GeoPackage layers in the model CRS, to check in QGIS.
    v3.12.0: with structures, 'Structure connections' (the connection lines in the 2D area)
    and 'Structures from the run' (each run structure's line, exported or why not)."""
    _, ogr, _ = _ogr()
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)

    def layer(name, gtype, rows):
        lyr = ds.CreateLayer(name, srs, gtype)
        keys = []
        for _, props in rows:
            for k, v in props.items():
                if k not in keys:
                    keys.append(k)
                    lyr.CreateField(ogr.FieldDefn(k, ogr.OFTReal if isinstance(v, float)
                                                  else ogr.OFTString))
        for geom, props in rows:
            f = ogr.Feature(lyr.GetLayerDefn())
            for k, v in props.items():
                f.SetField(k, v)
            f.SetGeometry(geom)
            lyr.CreateFeature(f)
    u = m.units.len_name
    layer("2D flow area", ogr.wkbPolygon, [(_polygon(m.perimeter), {
        "name": AREA_NAME, "cell_size": float(m.cell), "cells": str(len(m.points)),
        "manning_n": float(m.manning_n), "units": u})])
    layer("Reservoir storage area", ogr.wkbPolygon, [(_polygon(m.reservoir), {
        "name": SA_NAME, "note": "display only; HEC-RAS uses the elevation-volume curve"})])
    b = m.breach
    layer("Dam connection", ogr.wkbLineString, [(_line(m.conn_line), {
        "name": CONN_NAME, "breach_center_sta": float(b["center_station"]),
        "breach_bottom_width": float(b["bottom_width"]),
        "breach_bottom_elev": float(b["bottom_elev"]), "units": u})])
    layer("Normal depth boundary", ogr.wkbLineString, [(_line(m.bc_line), {
        "name": BC_NAME, "friction_slope": float(m.bc_slope), "slope_from": bc_slope_how})])
    # breach centre on the line
    A = np.asarray(m.conn_line[0])
    B = np.asarray(m.conn_line[-1])
    L = float(np.hypot(*(B - A))) or 1.0
    c = A + (B - A) * float(b["center_station"]) / L
    rows = [(_point(*c), {"what": "breach centre"})]
    if P is not None:
        rows.append((_point(*P), {"what": "dam point of the run"}))
    layer("Breach centre", ogr.wkbPoint, rows)
    # v3.12.0: culverts, bridges and notches
    if m.structures:
        layer("Structure connections", ogr.wkbLineString, [(_line(c["line"]), {
            "name": c["name"], "kind": c["kind"], "structure": c["source_name"],
            "hecras": "culvert group" if c["culvert"] else "weir with an open gap",
            "units": u}) for c in m.structures])
    rows = [(_line(c["structure_line"]), {"name": c["source_name"], "kind": c["kind"],
                                          "exported": "yes", "connection": c["name"]})
            for c in m.structures]
    rows += [(_line(sk["line"]), {"name": sk["name"], "kind": sk["kind"], "exported": "no",
                                  "connection": sk["why"]})
             for sk in skipped if len(sk["line"]) >= 2]
    if rows:
        layer("Structures from the run", ogr.wkbLineString, rows)
    ds = None
    return path


def _time_step_text(m):
    c = m.courant or {}
    base = "base step: Courant number (V dt / dx) about {:g} at the run's peak velocity".format(
        float(c.get("max") or (1 if m.equation == EQ_SWE else 2)))
    if not c.get("adaptive"):
        return base + "; fixed step"
    cmax = float(c.get("max") or (1.0 if m.equation == EQ_SWE else 2.0))
    cmin = float(c["min"]) if c.get("min") is not None else COURANT_MIN_RATIO * cmax
    return (base + "; adaptive (Courant {:g} to {:g}, halved up to {} times, doubled up to {} "
            "times after {} steps below the minimum)".format(
                cmin, cmax, int(c.get("max_halving") or 0), int(c.get("max_doubling") or 0),
                int(c.get("count_to_double") or 0)))


def _site_rows(site, L):
    """v3.10.17: README rows for the site checks."""
    if not site:
        return []
    st = site.get("storage") or {}
    rows = ["| Ground just below the dam (breach) | {} ft | lowest ground just past the toe "
            "across the breach; top of dam {} ft, dam height {} ft |".format(
                site.get("toe_ground_ft"), site.get("top_of_dam_ft"), site.get("dam_height_ft")),
            "| Reservoir in the DEM | lowest ground {} ft, {} ac below the top of dam | storage "
            "curve: bottom {} ft, {} ac at the top |".format(
                st.get("ground_min_ft"), st.get("outline_area_ac"), st.get("curve_min_ft"),
                st.get("curve_area_top_ac"))]
    if site.get("breach_bottom_raised_ft"):
        rows.append("| Breach bottom raised | {} ft | to the ground the breach flows onto |"
                    .format(site["breach_bottom_raised_ft"]))
    cut = site.get("breach_cut") or {}
    if cut.get("cells"):
        rows.append("| Terrain cut at the breach | {} x {} {}, to {} | so the 2D cells at the "
                    "breach reach its bottom |".format(cut.get("width"), cut.get("length"), L,
                                                       cut.get("z")))
        if cut.get("corner_margin") is not None:
            rows.append("| Breach cut past the corners | {} {} each side ({} {} flat), then "
                        "side slope {}:1 | so the dam-row cells centred on the bottom corners "
                        "reach the invert |".format(cut["corner_margin"], L, cut.get("flat_width"),
                                                   L, cut.get("side_slope")))
    if site.get("problems") and site.get("scope") == "hydrograph":
        rows.append("| Site checks (breach model) | {} problem(s), not applied | hydrograph-"
                    "inflow plan: no breach, dry reservoir |".format(len(site["problems"])))
    if site.get("overridden"):
        rows.append("| **Site checks** | **FAILED, overridden** | ALLOW_BAD_SITE / allow_bad_site;"
                    " the results are not meaningful |")
    return rows


def write_readme(path, m, info, notes, dam, params):
    u = m.units
    L = u.len_name
    b = info["breach"]
    lines = [
        "# HEC-RAS model: {}".format(m.title), "",
        "Built by the ND Dam Breach Runner the way HEC's tutorial *Dam Breach - Simple 2D "
        "Geometry* builds a model: the reservoir is a storage area, the dam is an SA/2D "
        "connection with the breach, and the valley below is a 2D flow area. **The values "
        "are the plugin's screening estimates; review them before a submittal.**", "",
        "## Files", "",
        "| File | What |", "|---|---|",
        "| `{0}.prj` | HEC-RAS project ({1}) |".format(m.name, u.label),
        "| `{0}.g01` | Geometry: storage area, 2D area ({1:,} cells of {2:g} {3}), dam "
        "connection, boundary line |".format(m.name, info["cells"], info["cell"], L),
        "| `{0}.u01` | Unsteady flow: initial pool, inflow, normal depth |".format(m.name),
        "| `{0}.p01` | Plan: {1:g} h, {2} time step, {3}, the breach |".format(
            m.name, info["sim_hours"], info["comp_interval"], info["equation"]),
        "| `{0}.rasmap` | RAS Mapper: projection and geometry |".format(m.name),
        "| `GIS/Projection.prj` | Coordinate system {} |".format(info["crs"]),
        "| `Terrain/{0}.tif` | DEM in that system, elevations in {1}, {2:g} {1} cells |".format(
            m.terrain_name, L, info["terrain_cell"]),
        "| `GIS/HECRAS Layout.gpkg` | The same geometry as layers, to check in QGIS |", "",
        "## Steps left to do in HEC-RAS (6.x)", "",
        "HEC-RAS makes two files itself, in its own binary format: the terrain and the "
        "preprocessed geometry.", "",
        "1. Open `{}.prj` in HEC-RAS. Open RAS Mapper; if it asks for the projection, choose "
        "`GIS/Projection.prj`.".format(m.name),
        "2. RAS Mapper: *Project > Create a New RAS Terrain*, add `Terrain/{}.tif`, save it in "
        "`Terrain/` (vertical units {}).".format(m.terrain_name, L),
        "3. Right-click the geometry > *Manage Geometry Associations* (6.x: Geometry > "
        "Terrain) and pick that terrain.",
        "4. Still in RAS Mapper, right-click the 2D flow area > *Edit 2D Area Properties* and "
        "press *Generate Computation Points* only if you want a different cell size; the "
        "points are already there. Then *Geometry > Compute Property Tables* (or let the "
        "plan's geometry preprocessor do it). Check the mesh messages.",
        "5. (Optional) Land cover: the 2D area uses one Manning's n ({:g}). If the run used "
        "an NLCD or Manning raster, make a RAS land cover layer and associate it.".format(
            m.manning_n),
        "6. Open the plan `{}.p01` and *Compute*. Then compare the HEC-RAS results with the "
        "plugin's: *Import HEC-RAS 2D results as rasters* reads the `.p01.hdf`.".format(m.name),
        "",
        "## Values and where they come from", "",
        "| Item | Value | From |", "|---|---|---|",
        "| Coordinate system | {} | {} |".format(info["crs"], info["crs_why"]),
        "| DEM elevations | {} | {} |".format(L, info["dem_z"]),
        "| 2D area | {:,.0f} {}2 | flooded extent of the run + {:,.0f} {} each side + {:,.0f} {} "
        "at the end of the inundation, cut along the dam line |".format(
            info["area"], L, info["side_buffer"], L, info["end_buffer"], L),
        "| Cell size | {:g} {} | {} |".format(info["cell"], L,
                                             "chosen" if info.get("cell_chosen") else
                                             "about {:,} cells".format(DEFAULTS["target_cells"])),
        "| Manning's n | {:g} | the run's default n |".format(m.manning_n),
        "| Reservoir curve | {} points | the plugin's storage law: storage at the top of dam, "
        "surface area, dam height (exponent {:.2f}) |".format(len(m.elev_volume),
                                                             info["storage_exponent"]),
        "| Initial pool | {:.2f} {} | the run's starting water surface |".format(
            info["initial_wse"], L),
        "| Dam weir | {} points along {:,.0f} {} | top of dam {:.2f}, or the ground where "
        "higher; coefficient {:g} |".format(info["weir_points"], info["dam_line"], L,
                                            u.from_ft(dam.top_of_dam_ft), m.weir_coef),
        "| Breach centre station | {:.1f} {} | {} |".format(b["center_station"], L,
                                                          info["centre_how"]),
        "| Breach bottom width / elevation | {:.1f} / {:.2f} {} | the run's breach "
        "parameters |".format(b["bottom_width"], b["bottom_elev"], L),
        "| Side slopes | {:g} H:1V | the run |".format(b["left_slope"]),
        "| Formation time | {:g} h, linear | the run |".format(b["formation_hr"]),
        "| Failure mode | {} | {} |".format(
            "Piping" if b["piping"] else "Overtopping",
            "the plugin's breach shape (hec_breach_mode match_plugin)"
            if info.get("hec_breach_mode", "match_plugin") == "match_plugin" else "the run"),
        "| Breach weir coefficient | {:g} | the plugin's breach weir (HEC-RAS default 2.6) |"
        .format(b["weir_coef"]),
        breach_start_row(b, info, L),
        "| Spillway | {} | {} |".format(
            "not modelled" if not info.get("spillway") else
            "{width:g} {L} notch at {crest:.2f} {L}, stations {s0:,.0f}-{s1:,.0f}".format(
                L=L, s0=info["spillway"]["stations"][0], s1=info["spillway"]["stations"][1],
                **info["spillway"]),
            "give spillway_crest_ft and spillway_width_ft (options or dam.json)"
            if not info.get("spillway") else info["spillway"]["source"] + (
                "; inflow {:,.0f} {} until the breach starts keeps the pool".format(
                    info["spillway"]["warmup_inflow"], u.q_name)
                if info["spillway"].get("warmup_inflow") else "")),
        "| Downstream boundary | normal depth, slope {:.5f} | {} |".format(
            info["bc_slope"], info["bc_slope_how"]),
        "| Time step | {} | {} |".format(info["comp_interval"], _time_step_text(m)),
        "| Solver tolerances | {} | UNET ZTol {ztol:g}, ZSATol {zsatol:g}; 2D Z {d2_ztol:g}, "
        "volume {d2_voltol:g}, {d2_maxiter} iterations |".format(
            (info.get("tolerances") or {}).get("name", "default"),
            **((info.get("tolerances") or {}).get("values") or TOLERANCES["default"])),
        "| Computation points | {} | {} |".format(
            "{:,} from gmsh {}".format(info["cells"], (info.get("mesh_seeds") or {}).get(
                "gmsh_version")) if (info.get("mesh_seeds") or {}).get("used") == "gmsh" else
            "regular {:g} {} grid".format(info["cell"], L),
            "gmsh asked for, not available: " + info["mesh_seeds"].get("why", "")
            if (info.get("mesh_seeds") or {}).get("asked") == "gmsh" and
            info["mesh_seeds"].get("used") != "gmsh" else
            "refined along the dam, breach and channel; HEC-RAS makes the cells"
            if (info.get("mesh_seeds") or {}).get("used") == "gmsh" else "the default"),
        "| Breaklines | {} | {} |".format(
            ", ".join(b["name"] for b in (info.get("breaklines") or {}).get("lines", []))
            or "none", "built into the computation points: pairs of points across each line "
            "(faces on the line) and an even row along the dam; re-enforce them in RAS Mapper "
            "only if you move them" if info.get("breaklines") else "off (option breaklines)"),
        "| Plugin breach peak on this breach | {} | {} |".format(
            "{:,.0f} cfs at {:.2f} h after the breach starts".format(
                info["plugin_reference"]["peak_cfs"],
                info["plugin_reference"]["peak_hr_after_breach"])
            if info.get("plugin_reference") else "not computed",
            "the plugin's routing with this plan's breach bottom, width, slopes and time "
            "(compare with the HEC-RAS breach flow); the run's own peak was {}".format(
                (info.get("plugin_reference") or {}).get("run_peak_cfs"))),
        "| Mesh check (cells along the perimeter) | {} |  Voronoi cells clipped to the "
        "perimeter, as HEC-RAS builds them |".format(
            "not run (no scipy)" if not info.get("mesh_check") else
            "{cells_checked} checked, most faces {max_faces}, over 8 faces {over_8_faces}, "
            "smallest {smallest_fraction:g} of a full cell".format(**info["mesh_check"])),
        "| Voronoi pre-check (every cell) | {} | HEC-RAS refuses a cell with more than 8 "
        "sides; points nudged or removed locally before writing |".format(
            "not run (no scipy)" if not (info.get("mesh_check") or {}).get("voronoi_precheck")
            else "{cells:,} cells, most sides {max_sides}, over 8: {over_interior} inside + "
            "{over_boundary} on the perimeter; face points: most {max_cells_per_facepoint} "
            "cells, over 8: {facepoints_over}".format(**info["mesh_check"]["voronoi_precheck"])
            + "; {} nudged, {} removed".format(info["mesh_check"]["nudged"],
                                               info["mesh_check"]["removed"])),
        "| Perimeter pre-check | {} | HEC-RAS stops on a perimeter cell cut in two or a "
        "sliver perimeter face ('face errors detected on the perimeter of the mesh'); "
        "points nudged or removed locally before writing |".format(
            "not run (no scipy)" if not (info.get("mesh_check") or {}).get("voronoi_precheck")
            else "found {f[split_cells]} cut in two, {f[short_perimeter_faces]} sliver faces, "
            "{f[perimeter_corners_near_edge]} corners on a cell edge; {n} nudged, {r} removed; "
            "now {c[split_cells]} / {c[short_perimeter_faces]} / "
            "{c[perimeter_corners_near_edge]}, shortest perimeter face {c[min_perimeter_face]}, "
            "nearest corner to an edge {c[min_corner_to_edge]}".format(
                f=info["mesh_check"].get("perimeter_found") or {
                    "split_cells": 0, "short_perimeter_faces": 0,
                    "perimeter_corners_near_edge": 0},
                c=info["mesh_check"]["voronoi_precheck"],
                n=info["mesh_check"].get("perimeter_nudged", 0),
                r=info["mesh_check"].get("perimeter_removed", 0))),
        "| Breach-cell pre-check | {} | every cell fronting the final breach bottom on the "
        "dam connection reaches the invert (HEC-RAS: 'weir breach lower than the cells they "
        "are connected to'; Absaraka v2c: 38.7 % volume error) |".format(
            _breach_cells_row((info.get("site") or {}).get("breach_cells"), L)),
        "| Simulation | {:g} h from {} 00:00 | {} |".format(
            info["sim_hours"], START_DATE, info.get("sim_how") or ""),
        "| Dam line | {:,.0f} {} | {} |".format(info["dam_line"], L,
                                              info.get("dam_line_how") or ""),
        ] + _dam_axis_rows(info.get("dam_axis"), L) + _site_rows(info.get("site"), L) + ["",
        "## Not carried over from the run", "",
        "- DEM edits made for the run (channels, ponds, notches, buildings, a proposed "
        "embankment) are not in the terrain: add them in RAS Mapper as terrain "
        "modifications if they matter.",
        ("- Outlets: not modelled; the spillway is the notch above." if info.get("spillway") else
         "- Spillways and outlets: the dam is a weir at the top of dam. Give the spillway crest "
         "and width (spillway_crest_ft, spillway_width_ft) to add it as a notch."),
        "- HEC-RAS routes the breach itself (weir flow through the opening); its peak will "
        "differ from the plugin's {:,.0f} cfs.".format(float(params.get("peak_cfs") or 0)),
        "- " + BREACH_SHAPE_NOTE,
        ""]
    # v3.12.0: culverts, bridges and notches (nothing is added when the run had none)
    k = lines.index("## Not carried over from the run")
    lines[k:k] = structures_readme(m, info.get("structures"))
    if notes:
        lines += ["## Notes", ""] + ["- " + n for n in notes] + [""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def breach_start_row(b, info, L):
    """v3.10.22: the README row on when the breach starts."""
    if b.get("start_mode") == "time":
        return ("| Breach start | at {:g} h (set time) | {} |".format(
            b.get("start_hr") or 0.0,
            "the run's breach initiation in the flood record" if info["scenario"] == "flood"
            else "sunny day: a {:g} h warm-up with the pool at its starting level, then the "
            "breach; add {:g} h to the plugin's times to compare".format(
                b.get("start_hr") or 0.0, b.get("start_hr") or 0.0)))
    return "| Breach trigger | water surface {:.2f} {} | {} |".format(
        b["trigger_wse"], L, "the run's trigger" if info["scenario"] == "flood" else
        "sunny day: the breach starts with the run")


def _model_structures(o, report, to_model, srs, log):
    """The structures build_model exports: the run's, or those of o['structures_from'] (3.10.56).
    Returns (structs, source text, their transformer to the model CRS)."""
    from . import hecras_structures as hstr
    if o.get("structures") and o.get("structures_from"):
        structs, ssrc, s_wtext, snotes = hstr.load_run_structures(o["structures_from"])
        for n_ in snotes:
            log("Structures: " + n_)
        return structs, ssrc, (transformer(srs_from(s_wtext), srs) if s_wtext else None)
    structs, ssrc = run_structures(report) if o["structures"] else ([], "")
    return structs, ssrc, to_model


def _seed_structures(o, report, to_model, perim, cell, terrain_tif, srs, units, log):
    """3.10.75: the connection lines for the gmsh seeds ([{'line', 'size'}], size =
    hecras_structures.structure_spacing of the line), so the points are graded down to the
    breakline spacing at every culvert / bridge. The same connections as the build makes later
    (hecras_structures.connections; the terrain may differ only by the breach cut), found here
    silently; [] without structures or structure breaklines, or on any error (the seeds are
    then as before)."""
    if not (o.get("structures") and o.get("structure_breaklines", True)):
        return []
    from . import hecras_structures as hstr
    try:
        structs, _, s_to_model = _model_structures(o, report, to_model, srs, lambda m: None)
        if not structs:
            return []
        sinfo = hstr.connections(structs, s_to_model, perim, cell, terrain_tif, srs, units)
    except Exception as e:                             # noqa: BLE001 - seeds then as before
        log("gmsh seeds: no structure refinement ({}: {})".format(type(e).__name__, e))
        return []
    out = []
    for c in sinfo["exported"]:
        ln = np.asarray(c["line"], float)
        out.append({"name": c["name"], "line": ln.tolist(), "size": hstr.structure_spacing(
            float(np.hypot(*(ln[-1] - ln[0]))), cell, units)})
    return out


def structures_readme(m, sinfo):
    """v3.12.0: the README section on the run's culverts, bridges and notches: what became of
    each one and what to check in HEC-RAS. [] when the run had none."""
    if not sinfo or not (sinfo.get("exported") or sinfo.get("skipped")):
        return []
    u = m.units
    L = u.len_name
    out = [
        "## Culverts, bridges and notches", "",
        "From {}. Each one that lies in the 2D area is an SA/2D area connection *inside* the "
        "2D area (upstream and downstream both `{}`), the way HEC-RAS models a road embankment "
        "with culverts in a 2D area. Its connection line crosses the embankment where the "
        "structure line is highest, perpendicular to that line, and runs along the road each "
        "way until the ground is {:g} ft above the road at the structure (at most {:,.0f} ft "
        "each side, and one cell inside the 2D area). The weir is the terrain along that line "
        "(the road crest), with the weir equation (coefficient {:g}) for flow over the road."
        .format(sinfo["source"], AREA_NAME, STRUCT_RISE_FT, STRUCT_MAX_HALF_FT,
                u.weir_coef(STRUCT_WEIR_COEF)), "",
        "**These connections follow the keyword lines of HEC's own example geometries but have "
        "NOT been opened in HEC-RAS. Check every one (below) before you run the plan.**", ""]
    ex = sinfo.get("exported") or []
    if ex:
        out += ["| Connection | Structure | In HEC-RAS | Opening | Inverts US / DS ({0}) | "
                "Length ({0}) | Weir |".format(L), "|---|---|---|---|---|---|---|"]
        for c in ex:
            weir = "{} points along {:,.0f} {}, road top {:,.0f} {} wide".format(
                len(c["weir"]), float(np.hypot(*np.subtract(c["line"][1], c["line"][0]))), L,
                c["weir_width"], L)
            cv, gp = c["culvert"], c["gap"]
            if cv:
                pipe = cv["shape"] == CULVERT_TYPES["pipe"][0]
                how = ("culvert group: {} (shape {}), FHWA chart {} scale {}, n {:g}, entrance "
                       "loss {:g}, exit loss {:g}".format(
                           "concrete pipe" if pipe else "box", cv["shape"], cv["chart"],
                           cv["scale"], cv["n"], cv["entrance"], cv["exit"]))
                size = ("{} x {} {} diameter".format(cv["barrels"], num(cv["rise"], 2), L) if pipe
                        else "{} x {} x {} {} (span x rise)".format(
                            cv["barrels"], num(cv["span"], 2), num(cv["rise"], 2), L))
                inv = "{:.2f} / {:.2f} ({})".format(cv["inv_up"], cv["inv_dn"], cv["inverts_from"])
                length = "{:,.0f}".format(cv["length"])
            else:
                how = "weir with an open gap (no deck, piers or pressure flow)"
                size = "gap {} {} wide".format(num(gp["width"], 1), L)
                inv = "gap floor {:.2f} ({})".format(gp["floor"], gp["inverts_from"])
                length = ""
            out.append("| {} | {} `{}` | {} | {} | {} | {} | {} |".format(
                c["name"], c["kind"], c["source_name"], how, size, inv, length, weir))
        out.append("")
        for c in ex:
            out += ["- {}: {}.".format(c["name"], n) for n in c["notes"]]
        out += ["- Inverts: 'run' is the structure's invert as the run used it, 'terrain' the "
                "ground at that end of its line (a culvert) or the lowest ground near that end "
                "(a gap).", "- The barrels sit side by side about the crossing of the two "
                "lines, {:g} x the span apart, on HEC-RAS's default barrel centrelines (across "
                "the connection line). The Runner's own lines are in the layout GeoPackage "
                "(layer 'Structures from the run').".format(1.1), ""]
    ren = sinfo.get("renamed") or []
    if ren:
        out += ["Renamed for HEC-RAS (3.10.75; HEC-RAS refuses \"/\", \",\" and \"=\" in a "
                "connection name and takes at most {} characters; the original name, with its "
                "OpenStreetMap / NBI id, is in the Structure column above and here):".format(
                    CONN_NAME_MAX), ""]
        out += ["- `{}` = `{}`".format(x["name"], x["source_name"]) for x in ren] + [""]
    al = (sinfo.get("breaklines") or {}).get("aligned") or sinfo.get("aligned") or {}
    if al:
        low = sorted((v, k) for k, v in al.items() if v < 0.90)
        out += ["Connection lines on cell faces (3.10.75; the share of each line on the faces "
                "HEC-RAS makes from these points, faces shorter than a tenth of the spacing "
                "merged as HEC-RAS does): {} of {} at 90 % or more{}.".format(
                    len(al) - len(low), len(al), "; under 90 %: " + ", ".join(
                        "{} {:.0%}".format(k, v) for v, k in low) if low else ""), ""]
    fit = sinfo.get("fit") or []
    if fit:
        out += ["Raised to the cells they connect (3.10.56; HEC-RAS refuses a weir or a culvert "
                "invert below the lowest ground of a connected cell; the terrain is unchanged):",
                ""]
        out += ["- " + _hstr_change_text(x, u) + "." for x in fit] + [""]
    if ex:
        out += ["Mapping limits (3.10.56): HEC-RAS's culvert group is FHWA inlet / outlet "
                "control with entrance and exit losses; ANUGA's Boyd operator uses one loss "
                "coefficient (the run's `losses`) and its own inverts (used here). A bridge is "
                "an open gap in the road weir down to its invert, as ANUGA and LISFLOOD-FP cut "
                "it into the DEM: no deck, low chord, piers or pressure flow. Flow over a road "
                "is the weir equation (coefficient {:g}), not the 2D equations.".format(
                    u.weir_coef(STRUCT_WEIR_COEF)), ""]
    sk = sinfo.get("skipped") or []
    if sk:
        out += ["Not exported (add them in HEC-RAS if they matter):", ""]
        out += ["- {} ({}): {}.".format(x["name"], x["kind"], x["why"]) for x in sk] + [""]
    if ex:
        out += [
            "What to check in HEC-RAS:", "",
            "1. Geometric Data: open each connection above in the SA/2D Area Connection editor. "
            "Check the connection line (it should cross the road at the structure), the weir "
            "station-elevation (the road crest, with no cut at a culvert: see the notes), the "
            "weir width and coefficient, and the culvert data: shape, span, rise, length, "
            "Manning's n (top and bottom), entrance and exit loss, FHWA chart and scale (set "
            "for a concrete pipe or a box with flared wingwalls; change them for corrugated "
            "metal or another inlet), upstream and downstream inverts, number of barrels and "
            "their centreline stations. Make sure the culvert group is listed for the "
            "connection. Replace an open gap with a bridge or culverts if you have the data.",
            ("2. RAS Mapper: the 2D cells need faces along every connection line. "
             "3.10.56: the connection lines are breaklines built into the computation points "
             "(a pair of points half a spacing each side of each line, so the faces lie on "
             "it; {}). Do not regenerate the points; if you move a connection, enforce its "
             "breakline in RAS Mapper.".format("; ".join(
                 "{} {:.0%} on faces".format(k, v) for k, v in
                 ((sinfo.get("breaklines") or {}).get("on_faces") or {}).items()) or "see the notes")
             if (sinfo.get("breaklines") or {}).get("lines") else
             "2. RAS Mapper: the 2D cells need faces along every connection line. The "
             "computation points here were laid out without the structures: put breaklines "
             "on the connection lines (or regenerate the points) so the mesh follows them."),
            "3. Compute the property tables (Geometry > Compute Property Tables, or the plan's "
            "geometry preprocessor) and read the messages for each connection.",
            "4. After the run, compare the water upstream of each road with the run's maximum "
            "depth there.", ""]
    return out


def _hstr_change_text(x, units):
    from . import hecras_structures as hstr
    return hstr.change_text(x, units)


def perimeter_clip(perimeter):
    """v3.10.22: clip(corners) for hecras_breaklines.voronoi_precheck: a Voronoi cell clipped
    to the 2D area as HEC-RAS builds it -> {'sides', 'pieces' (polygons it falls into; HEC-RAS
    refuses a cell split in two), 'perimeter_faces' [lengths of the pieces of the perimeter
    inside it: HEC-RAS's perimeter faces]}."""
    from osgeo import ogr
    per = np.asarray(perimeter, float)
    poly = _polygon(per)
    ring = ogr.Geometry(ogr.wkbLineString)
    for x, y in list(per) + [per[0]]:
        ring.AddPoint_2D(float(x), float(y))

    def _parts(g):
        if g is None or g.IsEmpty():
            return []
        t = ogr.GT_Flatten(g.GetGeometryType())
        if t == ogr.wkbLineString:
            return [np.asarray(g.GetPoints(), float)[:, :2]]
        if t in (ogr.wkbMultiLineString, ogr.wkbGeometryCollection):
            out = []
            for i in range(g.GetGeometryCount()):
                out += _parts(g.GetGeometryRef(i))
            return out
        return []

    def clip(corners):
        cell = _polygon(corners)
        cp = cell.Intersection(poly)
        if cp is None or cp.IsEmpty():
            return {"sides": 0, "pieces": 0, "perimeter_faces": []}
        rr = _rings(cp)
        parts = [p for p in _parts(ring.Intersection(cell)) if len(p) >= 2]
        # the ring's start point cuts one piece in two: join pieces that share an end
        merged = True
        while merged and len(parts) > 1:
            merged = False
            for i in range(len(parts)):
                for j in range(i + 1, len(parts)):
                    a, b = parts[i], parts[j]
                    if np.allclose(a[-1], b[0]):
                        parts[i] = np.vstack([a, b[1:]])
                    elif np.allclose(b[-1], a[0]):
                        parts[i] = np.vstack([b, a[1:]])
                    else:
                        continue
                    del parts[j]
                    merged = True
                    break
                if merged:
                    break
        lens = [float(np.hypot(*np.diff(p, axis=0).T).sum()) for p in parts]
        return {"sides": len(rr[0][1]) if rr else 0, "pieces": len(rr),
                "perimeter_faces": lens}

    return clip


def _dist_to_dam(line, dam_poly, cell):
    """The least distance from a line to the dam line."""
    from . import hecras_breaklines as hbl
    q = hbl.resample(np.asarray(line, float), 0.1 * cell)
    return float(hbl.poly_dist(q[:, 0], q[:, 1], np.asarray(dam_poly, float)).min())


def _band_read_lines(paths, srs):
    """3.10.76: the line features of vector files (band_lines), in the model CRS."""
    gdal, ogr, osr = _ogr()
    out = []
    for path in paths or []:
        ds = ogr.Open(str(path))
        if ds is None:
            raise HecRasModelError("band_lines: cannot open {}".format(path))
        for li in range(ds.GetLayerCount()):
            lyr = ds.GetLayer(li)
            lsr = lyr.GetSpatialRef()
            ct = None
            if lsr is not None:
                for s_ in (lsr, srs):
                    try:
                        s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    except AttributeError:                 # pragma: no cover - GDAL 2
                        pass
                ct = osr.CoordinateTransformation(lsr, srs)
            for ft in lyr:
                g = ft.GetGeometryRef()
                if g is None:
                    continue
                g = g.Clone()
                if ct is not None:
                    g.Transform(ct)
                parts = [g] if g.GetGeometryCount() == 0 else [
                    g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
                for p in parts:
                    xy = np.asarray(p.GetPoints() or [], float)
                    if len(xy) >= 2:
                        out.append(xy[:, :2])
        ds = None
    return out


def _line_bands(bcfg, o, report, settings, to_model, srs, units, perim, cell, pts, prot,
                dam_poly, breach_lines, band_wet, path_m, terrain_tif, log):
    """3.10.76: the line bands (hecras_bands) in the computation points: the lines of each
    class in bcfg['classes'] (structures: the connection lines the build makes later; roads:
    the run's road_lines; rail: report rail_lines_xy; breakline: band_lines files; channel:
    the flow path), clipped to the 2D area, away from the dam line and the breach cut, and
    (extent 'wet') to the run's wet area plus the pad. Returns (points, protected, info,
    BreakLine blocks for the .g, {structure name: line} banded)."""
    from . import hecras_bands as hband
    from . import hecras_breaklines as hbl
    from . import hecras_structures as hstr
    s = units.from_ft(bcfg["cell_ft"])
    rows, grading = int(bcfg["rows"]), float(bcfg["grading"])
    edge = hband.band_edge(s, rows, grading, cell)
    u = units.len_name
    pts = np.asarray(pts, float)
    pr = np.zeros(len(pts), bool) if prot is None else np.asarray(prot, bool)
    # away from the dam line and the breach cut: they keep their own rows (hecras_breaklines)
    avoid = [np.asarray(dam_poly, float)] + [np.asarray(b, float) for b in breach_lines or []]
    clear = max(hstr.DAM_CLEAR_CELLS * cell, edge + 0.5 * cell)
    tests = [hband.far_from(avoid, clear)]
    wet_test = None
    if bcfg["extent"] == "wet" and band_wet is not None:
        wet, x0, ytop, acell = band_wet
        wm = hband.dilate(wet, units.from_ft(bcfg["wet_pad_ft"]) / acell)
        wet_test = hband.mask_test(wm, x0, ytop, acell)
    lines, ranks, per, g_out, banded, skipped = [], [], {}, [], {}, []

    def tr(ls):
        out = []
        for ln in ls or []:
            a = np.asarray(ln, float).reshape(-1, 2)
            if len(a) >= 2:
                out.append(np.asarray(to_model(a.tolist()), float).reshape(-1, 2)[:, :2])
        return out
    for cls in hband.CLASSES:
        if cls not in bcfg["classes"]:
            continue
        raw, names = [], None
        if cls == "structure":
            names = []
            for c in _seed_structures(o, report, to_model, perim, cell, terrain_tif, srs,
                                      units, log):
                ln = np.asarray(c["line"], float)
                if _dist_to_dam(ln, dam_poly, cell) < hstr.DAM_CLEAR_CELLS * cell:
                    skipped.append("{} (near the dam line)".format(c.get("name")))
                    continue
                ext = hstr.extended_line(ln, hstr.BREAKLINE_END_EXT * s)
                pc = hband.clip([ext], perim, s, tests)
                if len(pc) == 1 and hbl.length(pc[0]) >= 0.99 * hbl.length(ext):
                    raw.append(ext)
                    names.append(c.get("name"))
                    banded[c.get("name")] = ln.tolist()
                else:
                    skipped.append("{} (at the edge of the 2D area or the breach)".format(
                        c.get("name")))
            pcs = raw
        else:
            if to_model is None and cls in ("road", "rail"):
                continue
            if cls == "road":
                raw = tr(settings.get("road_lines"))
            elif cls == "rail":
                raw = tr(report.get("rail_lines_xy"))
            elif cls == "breakline":
                raw = _band_read_lines(bcfg["lines"], srs)
            elif cls == "channel" and path_m is not None:
                raw = [np.asarray(path_m, float)]
            pcs = hband.clip(raw, perim, s, tests + ([wet_test] if wet_test else []))
            g_out += hband.g_lines(pcs, cls, s, cell, rows)
        per[cls] = {"lines": len(raw), "pieces": len(pcs),
                    "length": round(sum(hbl.length(p) for p in pcs), 1)}
        lines += pcs
        ranks += [hband.CLASSES.index(cls)] * len(pcs)
    info = {"on": True, "spacing": s, "rows": rows, "grading": grading,
            "classes": per, "extent": bcfg["extent"],
            "wet_pad": (units.from_ft(bcfg["wet_pad_ft"]) if bcfg["extent"] == "wet" else None),
            "skipped": skipped, "structures_banded": sorted(banded)}
    if not lines:
        info.update(points_removed=0, points_added=0, points=int(len(pts)),
                    note="Line bands: no {} lines in the 2D area{}.".format(
                        "/".join(bcfg["classes"]),
                        " (wet area)" if wet_test else ""))
        return pts, prot, info, [], {}
    hard = pr & ~hband.far_from(avoid, 2.0 * clear)(pts)
    pts2, prot2, st = hband.apply(pts, perim, lines, s, rows, grading, cell, pr, hard,
                                  ranks=ranks)
    info.update(st)
    L = sum(v["length"] for v in per.values())
    info["note"] = (
        "Line bands: {rows} row(s) of {s:g} {u} cells each side of the lines ({cls}; {L:,.0f} "
        "{u} in all{wet}), graded out at {g:g}:1 to the {c:g} {u} cells ({e:,.0f} {u} each "
        "side in all); {r:,} points removed, {a:,} added ({p:,} points now).{sk}".format(
            rows=rows, s=round(s, 2), u=u, cls=", ".join(
                "{} {:,.0f} {}".format(k, v["length"], u) for k, v in per.items()), L=L,
            wet=(", in the wet area + {:,.0f} {}".format(info["wet_pad"], u) if wet_test else ""),
            g=grading, c=round(cell, 2), e=edge, r=st["points_removed"], a=st["points_added"],
            p=st["points"], sk=(" Not banded: " + "; ".join(skipped) + "." if skipped else "")))
    return pts2, prot2, info, g_out, banded
