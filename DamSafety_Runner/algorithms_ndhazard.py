"""Hazard classification tool of the provider (id 'ndhazardclass'): ND Dam Safety Standards
REG_05.2026 Sections 5.2 / 5.3 with the ACER TM 11 depth-velocity charts, applied to the
rasters in a dam folder. The rules are in core/nd_hazard.py (shared with nd_dam_screening);
this module only reads the layers, samples the rasters and writes the results.

Import it through `algorithms` (which the provider does), like the other tool modules."""
import csv
import json
import math
import os
import re

import numpy as np
from qgis.core import (QgsCoordinateTransform, QgsProcessingException, QgsProcessingOutputFile,
                       QgsProcessingOutputString, QgsProcessingParameterEnum,
                       QgsProcessingParameterFeatureSource)

from . import qgis_io as qio
from .algorithms_consequences import ConsequencesAlgorithm, _attr, _txt
from .algorithms_tools import _ToolBase
from .compat import SOURCE_LINE, SOURCE_POINT, SOURCE_POLYGON, set_advanced
from .core import consequences as cons
from .core import aadt_join
from .core import nd_hazard as nh
from .core import road_profile as rprof
from .damsuite import structure_flags as sflags

OUTPUT_NAMES = {"markdown": "Hazard Classification (ND 5.2).md",
                "json": "Hazard Classification (ND 5.2).json",
                "csv": "Hazard Classification Findings.csv",
                "geojson": "Hazard Classification Findings.geojson",
                "svg_house": "ND Fig 5.1 Houses.svg",
                "svg_mobile": "ND Fig 5.2 Mobile Homes.svg"}

# ND 5.2.1.1 with 5.3: every structure is habitable unless its occupancy names an uninhabited
# building (nd_hazard.UNINHABITED_KEYWORDS, editable in the tool).
UNINHABITED_DEFAULT = ", ".join(nh.UNINHABITED_KEYWORDS)
UNKNOWN_WORDS = {"", "unknown", "unclassified", "unk", "none", "null", "n/a", "na", "other",
                 "-", "?"}
PASSENGER_DEFAULT = "A, B, C"

STRUCT_SYNONYMS = {
    "S_ID": ("id", "structure_id", "struct_id", "fd_id", "build_id", "bldg_id", "uuid",
             "objectid", "fid", "name"),
    "S_HABITABLE": ("habitable", "inhabited", "is_habitable", "occupied", "is_occupied"),
    "S_OCC": ("occtype", "occ_cls", "prim_occ", "occupancy", "occ_type", "occ", "st_damcat",
              "bldg_use", "use"),
    "S_MOBILE": ("mobile", "mobile_home", "is_mobile", "manufactured", "trailer", "prim_occ"),
}
ROAD_SYNONYMS = {
    "R_ID": ("name", "road_name", "fullname", "full_name", "rte_name", "route_name", "route",
             "id", "objectid"),
    "R_AADT": ("aadt", "adt", "aadt_2024", "aadt_2023", "aadt_2022", "aadt_vn", "traffic",
               "vehicles_per_day"),
    "R_CLASS": ("rttyp", "route_type", "rte_type", "shield", "road_class", "class", "fclass",
                "func_class", "functional_class", "route_sign", "route", "highway",
                "fullname", "name"),
    "R_PRIVATE": ("private", "is_private"),      # never ownership / access codes
    "R_SOLE": ("sole_access", "sole_acc", "only_access", "sole"),
}
RAIL_SYNONYMS = {
    "T_ID": ("name", "subdiv", "rrowner1", "fraarcid", "id", "objectid"),
    "T_TRAINS": ("trains_per_day", "trains_day", "trainsperday", "daily_trains", "trains",
                 "train_count", "tpd"),
    "T_PASSENGER": ("passngr", "passenger", "is_passenger", "amtrak"),
}
LIFELINE_SYNONYMS = {
    "L_ID": ("name", "fac_name", "facility", "id", "objectid"),
    "L_KIND": ("kind", "type", "facility_type", "fac_type", "category", "naics_desc", "use",
               "facname"),
}
DAM_SYNONYMS = {
    "D_ID": ("dam_id", "nidid", "nid_id", "state_id", "id", "dam_name", "name"),
    "D_CLASS": ("hazard_class", "hazard", "haz_class", "hazard_potential", "nid_hazard",
                "class"),
}
NID_CLASS = {"h": "High", "s": "Significant", "l": "Low", "m": "Medium"}

_YES = {"y", "yes", "true", "t", "1", "x", "on"}
_NO = {"n", "no", "false", "f", "0", "off"}

SAMPLING = ("Points: the raster cell under the point. Polygons: the deepest cell whose centre "
            "falls inside the footprint (the cell under the centroid when none does), with the "
            "maximum velocity over the footprint's wet cells, which is conservative (the "
            "velocity at the deepest cell is kept as well). Lines: sampled every half cell, the "
            "deepest sample. Roads (3.10.90, DWR-3034): the water surface over the road "
            "surface along the centreline (1 m); bridge / notch cuts, and bare-earth openings "
            "at a mapped bridge / notch, read on a deck stand-in from the approaches; culverts "
            "not masked; the depth raster where there is no road surface or water surface, and "
            "for the whole road when the road surface DEM cannot be used (see 'Road overtopping "
            "depth'). Only features inside the rasters are read.")


ROAD_BASIS_LABELS = ("Water surface over the road surface on the centreline (fine DEM; bridge "
                     "/ notch cuts on a deck stand-in from the approaches)",
                     "Model depth raster under the road (before 3.10.90)")
# 3.10.90 review: the road surface DEM's own elevation unit (never the run DEM's factor)
ROAD_DEM_UNITS = ("Detect (the raster's band unit or vertical CRS; unknown = depth raster)",
                  "metres", "feet", "US survey feet")
ROAD_DATUM_TOL_M = 1.0          # road surface DEM vs the run's own ground: median offset limit
ROAD_DATUM_MIN_POINTS = 5       # ... over at least this many points (cells)
ROAD_CHECK_STEP_M = 5.0         # centreline spacing of the datum check


def run_report_of(folder):
    """_breach_model/run_report.json as a dict ({} when it is not there or not readable)."""
    try:
        with open(os.path.join(folder, "_breach_model", "run_report.json"),
                  encoding="utf-8") as f:
            rep = json.load(f)
        return rep if isinstance(rep, dict) else {}
    except (OSError, ValueError):
        return {}


def _vertical_unit(path):
    """(metres per unit, how) from a compound CRS's vertical unit, or (None, '')."""
    try:
        from osgeo import osr
        from .damsuite import gdalsafe as GS
        ds = GS.open_raster(path)
        wkt = ds.GetProjection() if ds is not None else ""
        if not wkt:
            return None, ""
        sr = osr.SpatialReference()
        sr.ImportFromWkt(wkt)
        if not sr.IsCompound():
            return None, ""
        v = sr.GetTargetLinearUnits("VERT_CS")
    except Exception:
        return None, ""
    from .core.units import FT, US_FT
    for f, name in ((1.0, "metres"), (FT, "feet"), (US_FT, "US survey feet")):
        if v and abs(float(v) - f) < 1e-7:
            return f, "{} (vertical CRS)".format(name)
    return None, ""


def dem_unit_of(path):
    """(metres per DEM elevation unit, how) read from the raster itself: its band unit, else a
    compound CRS's vertical unit. (None, why) when neither records it - no metres default (a
    feet CRS or the elevation range alone is a guess, gdal_io.z_to_m_factor's 'check')."""
    from .core import gdal_io
    try:
        f, note = gdal_io.z_to_m_factor(path)
    except Exception as ex:                                       # noqa - reported
        return None, "its elevation unit could not be read ({})".format(ex)
    if "raster unit" in note:
        return float(f), note
    v, how = _vertical_unit(path)
    if v is not None:
        return v, how
    return None, "no unit recorded in the raster ({})".format(note)


def run_dem_unit(rep, path):
    """(metres per unit, how) of the run's own DEM: run_report.json's dem_z_to_m, then its
    dem_elevation_units (core/hecras_model._z_factor, the factor the run used, so the WSE
    matches it), else the raster's own unit (dem_unit_of). (None, why) when nothing records
    it: the caller reads the depth raster - never a metres default."""
    note = str(rep.get("dem_elevation_units") or "").strip().lower()
    if rep.get("dem_z_to_m") or note.startswith(("us survey feet", "feet", "metres")):
        from .core import hecras_model
        f, how = hecras_model._z_factor(rep, path)
        return float(f), "run_report.json: {}".format(
            how or "dem_z_to_m {:g}".format(float(f)))
    f, how = dem_unit_of(path)
    return f, ("run_report.json records no unit (written before Runner 3.10.16?); " + how
               if f is None else how)


def road_dem_of(folder, rep=None):
    """(DEM path or None, metres per unit or None, how, cell m) of the run's own DEM, from
    _breach_model/run_report.json (settings.input_files.DEM)."""
    rep = run_report_of(folder) if rep is None else rep
    st = rep.get("settings") or {}
    path = (st.get("input_files") or {}).get("DEM")
    cell = st.get("cell_size_m")
    if not path or not os.path.exists(path):
        return None, None, "", cell
    f, how = run_dem_unit(rep, path)
    return path, f, how, cell


def structure_cuts_of(folder):
    """The run's structures (_breach_model/structures.json); [] when none."""
    try:
        with open(os.path.join(folder, "_breach_model", "structures.json"),
                  encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def structures_in_raster_crs(structs, work_crs, raster_wkt):
    """(structures with their lines moved from the run's work CRS into the rasters' CRS, note).
    structures.json lines are in the work CRS (structures.Structure.line); the final rasters are
    in the DEM's CRS when that is a projected non-metre CRS (State Plane ft). No work_crs: the
    lines are taken as they are (a metre DEM: the rasters are in the work CRS)."""
    structs = [s for s in structs or [] if isinstance(s, dict)]
    if not structs:
        return [], ""
    if not work_crs:
        return structs, ("run_report.json has no work_crs: structures.json lines taken in the "
                         "rasters' CRS" if raster_wkt else "")
    if not raster_wkt:
        return structs, "the rasters have no CRS: structures.json lines taken as they are"
    try:
        from osgeo import osr
        src, dst = osr.SpatialReference(), osr.SpatialReference()
        try:
            src.SetFromUserInput(str(work_crs))
        except Exception:
            src.SetFromUserInput(str(work_crs).split()[0])
        dst.SetFromUserInput(str(raster_wkt))
        for sr in (src, dst):
            try:
                sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except AttributeError:
                pass
        if src.IsSame(dst):
            return structs, ""
        tf = osr.CoordinateTransformation(src, dst)
        out = []
        for s in structs:
            d = dict(s)
            line = np.asarray(s.get("line") or [], dtype=float).reshape(-1, 2)
            d["line"] = [list(p[:2]) for p in tf.TransformPoints(line.tolist())] \
                if len(line) else []
            out.append(d)
        return out, ("structures.json lines moved from the run's work CRS ({}) into the "
                     "rasters' CRS".format(str(work_crs).split("[")[0][:40]))
    except Exception as ex:                                       # noqa - reported
        return [], ("structures.json lines could not be moved into the rasters' CRS ({}); no "
                    "structure corridor used".format(ex))


def road_depth_rows(rd):
    """Report lines (markdown sub-bullets) for each road read lower than the depth raster or
    read from it (road_depth.dropped / lowered / raster_roads)."""
    out = []

    def xy(x, y):
        return "?" if x is None or y is None else "{:.1f}, {:.1f}".format(x, y)

    def row(label, rec):
        return ("  - {}: {} - depth raster {:.2f} ft at ({}), centreline {:.2f} ft at ({}), "
                "freeboard {} ft; wet points: {} in bridge / notch cuts, {} on an opening at "
                "one, {} with no road surface, {} with no water surface, {} in a cut with no "
                "approach: {}".format(
                    label, rec["id"], rec["raster_depth_ft"],
                    xy(rec["raster_x"], rec["raster_y"]), rec["centreline_depth_ft"],
                    xy(rec.get("x"), rec.get("y")), rec.get("freeboard_ft"), rec.get("n_cut", 0),
                    rec.get("n_opening", 0), rec.get("n_nodem", 0), rec.get("n_nowse", 0),
                    rec.get("n_no_approach", 0), rec.get("reason", "")))
    for rec in rd.get("dropped") or []:
        out.append(row("Not counted as overtopped", rec))
    for rec in rd.get("lowered") or []:
        out.append(row("Overtopped, shallower than the depth raster", rec))
    for rec in rd.get("raster_roads") or []:
        out.append("  - Depth raster used: {} - {:.2f} ft at ({}): {}".format(
            rec["id"], rec["raster_depth_ft"], xy(rec["raster_x"], rec["raster_y"]),
            rec.get("reason", "")))
    return out


def _median_offset(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    if not ok.any():
        return None, 0
    return float(np.median(a[ok] - b[ok])), int(ok.sum())


def model_ground_offset(road_dem, grid, wse_ft, depth_ft, pts, unit_m=1.0, pixel_m=1.0,
                        average=True, exclude=None, max_cells=2000):
    """(median of road surface DEM - the run's model ground (m), cells) over the wet cells
    under `pts` (raster CRS): model ground = WSE (Max) - Depth (Max) of the cell (ft -> m), the
    road surface DEM averaged over the cell (the run's default 'average' resample) or at its
    centre. `exclude` (bool per point): points left out (inside structure cuts, whose cells
    hold the cut ground)."""
    pts = np.asarray(pts, dtype=float).reshape(-1, 2)
    if exclude is not None:
        pts = pts[~np.asarray(exclude, dtype=bool)]
    if not len(pts):
        return None, 0
    c = np.floor((pts[:, 0] - grid.x0) / grid.dx).astype(int)
    r = np.floor((grid.y0 - pts[:, 1]) / grid.dx).astype(int)
    w, d = np.asarray(wse_ft, dtype=float), np.asarray(depth_ft, dtype=float)
    ok = (r >= 0) & (c >= 0) & (r < w.shape[0]) & (c < w.shape[1])
    cells = sorted(set(zip(r[ok].tolist(), c[ok].tolist())))
    cells = [(i, j) for i, j in cells if np.isfinite(w[i, j]) and np.isfinite(d[i, j])
             and d[i, j] > 0]
    if len(cells) > max_cells:
        cells = [cells[k] for k in np.linspace(0, len(cells) - 1, max_cells).astype(int)]
    if not cells:
        return None, 0
    cell_m = grid.dx * float(unit_m or 1.0)
    k = int(min(5, max(1, round(cell_m / max(float(pixel_m or 1.0), 1e-6))))) if average else 1
    off = (np.arange(k) + 0.5) / k
    out_a, out_b = [], []
    for i, j in cells:
        xs = grid.x0 + (j + off) * grid.dx
        ys = grid.y0 - (i + off) * grid.dx
        X, Y = np.meshgrid(xs, ys)
        z = np.asarray(road_dem(np.column_stack([X.ravel(), Y.ravel()])), dtype=float)
        if not np.isfinite(z).any():
            continue
        out_a.append(float(np.nanmean(z)))
        out_b.append(float(w[i, j] - d[i, j]) * rprof.FT)
    return _median_offset(np.asarray(out_a), np.asarray(out_b))


# ------------------------------------------------------------------------------ helpers
def _yesno(v):
    """True / False from a boolean-ish value, None when it says neither."""
    if v is None or (hasattr(v, "isNull") and v.isNull()):
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return None if v != v else v != 0
    t = str(v).strip().lower()
    if t in _YES:
        return True
    if t in _NO:
        return False
    try:
        x = float(t)
    except ValueError:
        return None
    return None if x != x else x != 0


def habitable_from(flag_value, occ_text, uninhabited=nh.UNINHABITED_KEYWORDS):
    """(habitable, how): a habitable field that says yes or no (or 'uninhabited' /
    'unoccupied'), else the occupancy text (habitable unless it names an uninhabited building:
    nd_hazard.habitable_from_text, shared with the screening plugin), else habitable."""
    yn = _yesno(flag_value)
    if yn is None and flag_value is not None:
        t = _txt(flag_value).lower()
        if re.search(r"uninhab|unoccup|not habitable", t):
            yn = False
        elif re.search(r"habitable|inhabited|occupied", t):
            yn = True
    if yn is not None:
        return yn, "field"
    occ = (occ_text or "").strip()
    if occ.lower() in UNKNOWN_WORDS:
        return True, "unknown"
    return nh.habitable_from_text(occ, uninhabited), "class"


def is_mobile(flag_value, occ_text):
    if _yesno(flag_value):
        return True
    return any(nh.mobile_from_text(t) for t in (_txt(flag_value), occ_text or "") if t)


def is_interstate(class_text, name_text=""):
    return nh.interstate_from_text(class_text) or nh.interstate_from_text(name_text)


def is_us_state(class_text, name_text="", class_field=""):
    """3.10.53 (nd_hazard 1.2.0): a US / State highway from the class field or the name
    (5.2.1.2(b)). class_field: the class field's name; an OSM highway / fclass field also counts
    'primary'."""
    osm = str(class_field or "").strip().lower() in ("highway", "fclass")
    return nh.us_state_highway_from_text(class_text, osm=osm) or \
        nh.us_state_highway_from_text(name_text)


def _flagged_entry(folder):
    """3.10.53: the structure_flags entry of a dam folder (its name, or its dam.json dam_id /
    nid_id), else None. Never raises."""
    try:
        rec = None
        try:
            from .core import damjson
            rec, _problem = damjson.load(folder)
        except Exception:                                  # noqa  (no record: the name only)
            rec = None
        return sflags.lookup_folder(folder, rec)
    except Exception:                                      # noqa  (a flag check never fails)
        return None


def is_private(v):
    """Only an explicit private flag: yes/true/1 or text containing 'private'."""
    return nh.yes_no(v) is True or "private" in _txt(v).lower()


def is_passenger(v, codes):
    toks = [t for t in re.split(r"[,;/\s]+", _txt(v)) if t] or [_txt(v)]
    return any(nh.passenger_from_value(t, tuple(codes)) for t in toks)


def dam_class(v):
    return nh.class_from_text(_txt(v)) or _txt(v)


def _inside_ring(px, py, ring):
    """Even-odd test of many points against one ring (vectorised over the points)."""
    x, y = ring[:, 0], ring[:, 1]
    x2, y2 = np.roll(x, -1), np.roll(y, -1)
    inside = np.zeros(px.shape, dtype=bool)
    for i in range(len(x)):
        if y[i] == y2[i]:
            continue
        cond = (y[i] > py) != (y2[i] > py)
        xi = x[i] + (py - y[i]) * (x2[i] - x[i]) / (y2[i] - y[i])
        inside ^= cond & (px < xi)
    return inside


class Sampler:
    """Maximum depth (ft) and velocity (ft/s) at points, polygons and lines on the dam
    folder's grid. Depth and velocity are arrays on `grid`; NaN / no-data count as dry."""

    def __init__(self, grid, depth, velocity=None, wet_ft=0.1):
        self.g = grid
        d = np.asarray(depth, dtype=float)
        bad = ~np.isfinite(d) | (d == grid.nodata) | (d < 0)
        self.d = np.where(bad, 0.0, d)
        if velocity is None:
            self.v = np.zeros_like(self.d)
        else:
            v = np.asarray(velocity, dtype=float)
            # no-data velocity stays unknown (NaN): nd_hazard counts it potentially lethal
            self.v = np.where(~np.isfinite(v) | (v == grid.nodata) | (v < 0), np.nan, v)
        self.wet = float(wet_ft)

    def _rc(self, x, y):
        c = np.floor((np.asarray(x, dtype=float) - self.g.x0) / self.g.dx).astype(int)
        r = np.floor((self.g.y0 - np.asarray(y, dtype=float)) / self.g.dx).astype(int)
        ok = (r >= 0) & (r < self.g.ny) & (c >= 0) & (c < self.g.nx)
        return r, c, ok

    def points(self, pts):
        """{depth_ft, vel_fts, vel_at_max_fts, x, y, cells, how} of the deepest of the
        points, or None when none is on the grid."""
        pts = np.asarray(pts, dtype=float).reshape(-1, 2)
        r, c, ok = self._rc(pts[:, 0], pts[:, 1])
        if not ok.any():
            return None
        r, c, pts = r[ok], c[ok], pts[ok]
        d = self.d[r, c]
        k = int(np.argmax(d))
        v = float(self.v[r[k], c[k]])
        v = v if math.isfinite(v) else None
        return {"depth_ft": float(d[k]), "vel_fts": v, "vel_at_max_fts": v,
                "x": float(pts[k, 0]), "y": float(pts[k, 1]), "cells": 1, "how": "point"}

    def polygon(self, rings, centroid):
        """Deepest cell whose centre is inside any of the outer rings; velocity is the
        maximum over the footprint's wet cells (conservative)."""
        g = self.g
        rr, cc = [], []
        for ring in rings:
            ring = np.asarray(ring, dtype=float)
            if len(ring) < 3:
                continue
            x0, y0 = ring.min(axis=0)
            x1, y1 = ring.max(axis=0)
            c0 = max(0, int(math.floor((x0 - g.x0) / g.dx - 0.5)))
            c1 = min(g.nx - 1, int(math.ceil((x1 - g.x0) / g.dx - 0.5)))
            r0 = max(0, int(math.floor((g.y0 - y1) / g.dx - 0.5)))
            r1 = min(g.ny - 1, int(math.ceil((g.y0 - y0) / g.dx - 0.5)))
            if c1 < c0 or r1 < r0:
                continue
            R, C = np.meshgrid(np.arange(r0, r1 + 1), np.arange(c0, c1 + 1), indexing="ij")
            R, C = R.ravel(), C.ravel()
            px = g.x0 + (C + 0.5) * g.dx
            py = g.y0 - (R + 0.5) * g.dx
            m = _inside_ring(px, py, ring)
            rr.append(R[m])
            cc.append(C[m])
        cells = sorted(set(zip(np.concatenate(rr).tolist(), np.concatenate(cc).tolist()))) \
            if rr else []
        if not cells:
            s = self.points([centroid])
            if s is not None:
                s["how"] = "polygon smaller than a cell: the cell under the centroid"
            return s
        r = np.array([a for a, _ in cells])
        c = np.array([b for _, b in cells])
        d = self.d[r, c]
        v = self.v[r, c]
        k = int(np.argmax(d))
        wet = (d >= self.wet) & np.isfinite(v)
        vk = float(v[k]) if np.isfinite(v[k]) else None
        vmax = float(v[wet].max()) if wet.any() else vk
        if d[k] > 0:
            x, y = g.xy(int(r[k]), int(c[k]))
        else:
            x, y = float(centroid[0]), float(centroid[1])
        return {"depth_ft": float(d[k]), "vel_fts": vmax,
                "vel_at_max_fts": vk, "x": float(x), "y": float(y),
                "cells": len(cells), "how": "polygon: deepest of {} cell(s)".format(len(cells))}

    def is_wet(self, x, y):
        """Overtopped / inundated at (x, y): depth at least the wet depth (off the grid: no)."""
        r, c, ok = self._rc(x, y)
        return bool(ok) and float(self.d[int(r), int(c)]) >= self.wet

    def lines(self, parts):
        """Deepest sample along the parts, sampled every half cell."""
        step = 0.5 * self.g.dx
        pts = []
        for part in parts:
            part = np.asarray(part, dtype=float)
            for a, b in zip(part[:-1], part[1:]):
                n = max(1, int(math.ceil(float(np.hypot(*(b - a))) / step)))
                t = np.arange(n + 1)[:, None] / float(n)
                pts.append(a + t * (b - a))
        if not pts:
            return None
        s = self.points(np.vstack(pts))
        if s is not None:
            s["how"] = "line: deepest sample"
        return s


def _geom_kind(g):
    try:
        t = g.type()
        t = int(getattr(t, "value", t))
    except Exception:
        return ""
    return {0: "point", 1: "line", 2: "polygon"}.get(t, "")


def _parts(g, to_r):
    """(kind, parts, centroid) in the raster CRS: points [(x, y)], lines [array], polygons
    [outer ring array] (interior rings are ignored)."""
    tf = lambda p: (lambda q: (q.x(), q.y()))(to_r.transform(p))
    kind = _geom_kind(g)
    multi = g.isMultipart()
    if kind == "polygon":
        polys = g.asMultiPolygon() if multi else [g.asPolygon()]
        parts = [np.asarray([tf(p) for p in rings[0]], dtype=float) for rings in polys
                 if rings and len(rings[0]) >= 3]
        return kind, parts, tf(g.centroid().asPoint())
    if kind == "line":
        lines = g.asMultiPolyline() if multi else [g.asPolyline()]
        return kind, [np.asarray([tf(p) for p in ln], dtype=float) for ln in lines
                      if len(ln) >= 2], None
    pts = g.asMultiPoint() if multi else [g.asPoint()]
    return "point", [tf(p) for p in pts], None


def _source_path(parameters, key):
    """File path of a layer parameter (a path string or a layer), without the |layername part."""
    v = parameters.get(key)
    try:
        v = v.source() if hasattr(v, "source") else v
    except Exception:
        return ""
    return str(v).split("|")[0] if isinstance(v, str) and v else ""


def _layer_name(src, parameters, key):
    try:
        n = src.sourceName()
        if n:
            return str(n)
    except Exception:
        pass
    v = parameters.get(key)
    if isinstance(v, str) and v:
        return os.path.basename(v)
    return key.lower()


def _dam_id(folder):
    try:
        with open(os.path.join(folder, "_breach_model", "run_report.json"), encoding="utf-8") as f:
            rid = (json.load(f).get("dam") or {}).get("dam_id")
        if rid:
            return str(rid)
    except (OSError, ValueError, AttributeError):
        pass
    return os.path.basename(os.path.normpath(folder)) if folder else ""


def not_digitized_message(cs):
    return ("The ACER TM 11 depth-velocity charts (ND Fig. 5.1 houses, Fig. 5.2 mobile homes) "
            "are not digitized and checked in {} (status '{}', digitized by '{}', checked by "
            "'{}'), so the chart method cannot run. These are 2D model results: until the "
            "charts are digitized and checked, use the lethality method '{}' (it "
            "over-classifies), or give a digitized and checked chart file. The 2 ft depth rule "
            "is only allowed for a 1D model (5.2.1.1).".format(
                cs.path or "the chart file", cs.status, cs.digitized_by or "-",
                cs.checked_by or "-", nh.LETHALITY_LABELS[nh.LETHALITY_ALL_WET]))


# ------------------------------------------------------------------------------ algorithm
class NDHazardAlgorithm(_ToolBase):
    _raster_crs = staticmethod(ConsequencesAlgorithm._raster_crs)

    def name(self):
        return "ndhazardclass"

    def displayName(self):
        return "ND hazard classification (Section 5.2, ACER TM 11 charts)"

    def shortHelpString(self):
        return ("Classifies the dam Low / Medium / High from what its breach flood reaches, "
                "following the ND Dam Safety Standards Policy REG_05.2026 Sections 5.2 and 5.3 "
                "as core/nd_hazard.py applies them (the same module as the ND dam screening "
                "plugin). Reads 'Depth (Max).tif' (ft) and 'Velocity (Max).tif' (ft/s) from the "
                "dam folder.\n\n"
                "Habitable structures (5.2.1.1; unknown or blank occupancy counts as habitable): "
                "2D model: potentially lethal by the ACER TM 11 depth-velocity charts (ND Fig. "
                "5.1 houses, 5.2 mobile homes) -> High; the judgment zone is borderline and "
                "takes the higher class (5.3). 1D model: deeper than 2 ft -> High. Inundated "
                "at any depth -> at least Medium (economic loss). Roads (5.2.1.2): AADT > 2,000 "
                "or interstate -> High, US / State highway (5.2.1.2(b), any AADT) or 500-2,000 -> "
                "Medium, private or < 500 (local / county roads) -> Low, sole "
                "access to an occupied location -> Medium. Railroads (5.2.1.3): Medium, 10 or "
                "more trains a day or passenger -> High. Lifeline facilities (5.2.2) inundated "
                "-> Medium (access roads are not traced). A downstream dam the flood reaches "
                "(depth at its point at least the wet depth) passes its class on (5.3). 'Any "
                "depth' means the wet depth (default 0.1 ft). Missing AADT or train counts are "
                "data gaps, classified by the assumption you choose.\n\n"
                "READ FIRST. The chart method needs digitized and checked charts "
                "(core/acer_tm11_charts.json or your own chart file); the file shipped is not "
                "digitized, so the chart method stops with a message until it is. The result is "
                "a screening classification for the engineer; the official classification is "
                "made by the ND Department of Water Resources.\n\n" + SAMPLING + "\n\n"
                "Writes 'Hazard Classification (ND 5.2).md' and '.json', 'Hazard Classification "
                "Findings.csv' and '.geojson', and with the chart method 'ND Fig 5.1 Houses.svg' "
                "and 'ND Fig 5.2 Mobile Homes.svg' into the dam folder.")

    def initAlgorithm(self, config=None):
        self._file("DAM_FOLDER", "Dam folder with 'Depth (Max).tif' and 'Velocity (Max).tif'",
                   folder=True, optional=False)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "STRUCTURES", "Structures (points, or polygons: deepest cell inside)",
            [SOURCE_POINT, SOURCE_POLYGON], optional=True))
        for key, desc in (("S_ID", "identifier field"),
                          ("S_HABITABLE", "habitable field (yes/no, true/false, 1/0; blank = "
                                          "use the occupancy class)"),
                          ("S_OCC", "occupancy class field (NSI OCCTYPE, USA Structures "
                                    "OCC_CLS / PRIM_OCC \u2026)"),
                          ("S_MOBILE", "mobile home field (yes/no, or text containing "
                                       "manufactured / mobile / trailer)")):
            self._field(key, "Structures: " + desc, "STRUCTURES", numeric=False, advanced=True)
        self._text("UNINHABITED_WORDS", "Structures: occupancy words that mark an UNINHABITED "
                   "building (ND 5.2.1.1; every other structure, and a blank or unknown class, "
                   "is habitable - 5.3)", UNINHABITED_DEFAULT, advanced=True)
        self._layer("ROADS", "Roads (lines)", SOURCE_LINE)
        self._field("R_ID", "Roads: identifier / name field", "ROADS", numeric=False,
                    advanced=True)
        self._field("R_AADT", "Roads: AADT field (vehicles per day)", "ROADS", advanced=True)
        self._field("R_CLASS", "Roads: class / route field (interstate when it contains "
                    "'interstate' or starts 'I-' or 'I ' and a number)", "ROADS", numeric=False,
                    advanced=True)
        self._field("R_PRIVATE", "Roads: private road field (yes/no, or text 'private')",
                    "ROADS", numeric=False, advanced=True)
        self._field("R_SOLE", "Roads: sole access to an occupied location field (yes/no)",
                    "ROADS", numeric=False, advanced=True)
        self._layer("RAIL", "Railroads (lines)", SOURCE_LINE)
        self._field("T_ID", "Railroads: identifier field", "RAIL", numeric=False, advanced=True)
        self._field("T_TRAINS", "Railroads: trains a day field", "RAIL", advanced=True)
        self._field("T_PASSENGER", "Railroads: passenger field (yes/no, or a code below)",
                    "RAIL", numeric=False, advanced=True)
        self._text("PASSENGER_CODES", "Railroads: passenger field values meaning an active "
                   "passenger line (FRA PASSNGR codes), comma separated", PASSENGER_DEFAULT,
                   advanced=True)
        self.addParameter(QgsProcessingParameterFeatureSource(
            "LIFELINES", "Lifeline facilities: utilities, emergency response (points or "
            "polygons)", [SOURCE_POINT, SOURCE_POLYGON], optional=True))
        self._field("L_ID", "Lifelines: identifier field", "LIFELINES", numeric=False,
                    advanced=True)
        self._field("L_KIND", "Lifelines: kind of facility field", "LIFELINES", numeric=False,
                    advanced=True)
        self._text("STRUCTURES_MORE", "More structure GeoPackages, separated by ;; "
                   "(address points, USA Structures, and any other individual file)",
                   advanced=True)
        self._text("ROADS_MORE", "More road GeoPackages, separated by ;; "
                   "(county roads and any other individual file)", advanced=True)
        self._text("AADT_LAYERS", "Traffic (AADT) GeoPackages joined to roads that carry no AADT, "
                   "separated by ;; (HPMS, NDDOT traffic counts, county roads; empty = those found "
                   "next to the roads file, as the Hazard plugin uses)", advanced=True)
        self._num("AADT_JOIN_FT", "AADT join: a traffic line or count station within (ft) of "
                  "the road (the starting value 100 ft is about the Hazard plugin's 30 m; a "
                  "screening value you choose)", 100.0, minimum=3.0, advanced=True)
        self._text("LIFELINES_MORE", "More lifeline GeoPackages, separated by ;; "
                   "(nursing, care, assisted living, and any other individual file)",
                   advanced=True)
        self._layer("DOWNSTREAM_DAMS", "Downstream dams (points; affected when the flood depth "
                    "at the point is at least the wet depth)", SOURCE_POINT)
        self._field("D_ID", "Downstream dams: identifier field", "DOWNSTREAM_DAMS",
                    numeric=False, advanced=True)
        self._field("D_CLASS", "Downstream dams: hazard class field (Low / Significant / "
                    "Medium / High, or NID L / S / H)", "DOWNSTREAM_DAMS", numeric=False,
                    advanced=True)
        self.addParameter(QgsProcessingParameterEnum(
            "LETHALITY", "Potentially lethal to occupants of habitable structures",
            [nh.LETHALITY_LABELS[m] for m in nh.LETHALITY_METHODS],
            defaultValue=nh.LETHALITY_METHODS.index(nh.LETHALITY_CHARTS)))
        d = nh.Settings()
        self._num("WET_FT", "'Any depth': inundated or overtopped from a depth of (ft; at "
                  "least {:g}: a dry cell reads 0; the starting value {:g} is the engineer's "
                  "allowance for model noise, a screening value you choose)".format(
                      nh.MIN_WET_FT, d.wet_ft), d.wet_ft, minimum=nh.MIN_WET_FT)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "UNKNOWN_AADT", "Class of an overtopped road with no AADT (a data gap)",
            list(nh.CLASSES), defaultValue=nh.CLASSES.index(d.unknown_aadt))))
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "UNKNOWN_TRAINS", "Class of an overtopped railroad with no trains a day (a data "
                              "gap; at least Medium)",
            list(nh.CLASSES), defaultValue=nh.CLASSES.index(d.unknown_trains))))
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "ROAD_DEPTH_BASIS", "Road overtopping depth",
            list(ROAD_BASIS_LABELS), defaultValue=0)))
        self._file("ROAD_DEM", "Road surface DEM (empty = the run's DEM from "
                   "_breach_model/run_report.json; checked against the run's own ground)",
                   advanced=True)
        self.addParameter(set_advanced(QgsProcessingParameterEnum(
            "ROAD_DEM_UNITS", "Road surface DEM elevation units",
            list(ROAD_DEM_UNITS), defaultValue=0)))
        self._file("CHART_FILE", "Digitized ACER TM 11 chart file (.json; empty = the one "
                   "shipped with the plugin)", ext="json", advanced=True)
        self.addOutput(QgsProcessingOutputString("CLASS", "Hazard class"))
        self.addOutput(QgsProcessingOutputFile("REPORT", OUTPUT_NAMES["markdown"]))
        self.addOutput(QgsProcessingOutputFile("JSON", OUTPUT_NAMES["json"]))
        self.addOutput(QgsProcessingOutputFile("CSV", OUTPUT_NAMES["csv"]))
        self.addOutput(QgsProcessingOutputFile("GEOJSON", OUTPUT_NAMES["geojson"]))

    # ------------------------------------------------------------------------------
    def _settings(self, parameters, context):
        e = lambda n: self.parameterAsEnum(parameters, n, context)
        try:
            return nh.Settings(
                lethality=nh.LETHALITY_METHODS[e("LETHALITY")],
                wet_ft=self.parameterAsDouble(parameters, "WET_FT", context),
                unknown_aadt=nh.CLASSES[e("UNKNOWN_AADT")],
                unknown_trains=nh.CLASSES[e("UNKNOWN_TRAINS")]).validate()
        except (ValueError, IndexError) as ex:
            raise QgsProcessingException(str(ex))

    def _charts(self, parameters, context):
        path = self._fileparam(parameters, "CHART_FILE", context)
        try:
            cs = nh.load_charts(path or None)
        except (OSError, ValueError) as ex:
            raise QgsProcessingException("Could not read the chart file {}: {}".format(
                path or nh.CHART_FILE, ex))
        if not cs.ready:
            raise QgsProcessingException(not_digitized_message(cs))
        return cs

    def _fields(self, parameters, context, names, synonyms):
        """{key: field name}: the chosen field, else a name match on the synonyms, else ''."""
        low = {n.lower(): n for n in names}
        out = {}
        for k in synonyms:
            chosen = self._str(parameters, k, context)
            if not chosen:
                chosen = next((low[s] for s in synonyms[k] if s in low), "")
            out[k] = chosen
        return out

    def _source(self, parameters, context, key, synonyms, grid, crs, feedback, label):
        """(features in the box, fields, to-raster transform, layer name), or None."""
        src = self.parameterAsSource(parameters, key, context)
        if src is None:
            return None
        fld = self._fields(parameters, context, qio.field_names(src), synonyms)
        used = ["{} = {}".format(k[2:].lower(), v) for k, v in fld.items() if v]
        feedback.pushInfo("{} fields: {}".format(label, ", ".join(used) or "none matched"))
        to_r = QgsCoordinateTransform(src.sourceCrs(), crs, context.transformContext())
        to_l = QgsCoordinateTransform(crs, src.sourceCrs(), context.transformContext())
        xmin, ymin, xmax, ymax = grid.bounds
        center = (0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
        radius = 0.5 * float(np.hypot(xmax - xmin, ymax - ymin))
        return (qio._features(src, center, radius, to_l), fld, to_r,
                _layer_name(src, parameters, key))

    @staticmethod
    def _sample(sampler, g, to_r):
        if g is None or g.isEmpty():
            return None
        kind, parts, centroid = _parts(g, to_r)
        if not parts:
            return None
        if kind == "polygon":
            return sampler.polygon(parts, centroid)
        if kind == "line":
            return sampler.lines(parts)
        return sampler.points(parts)

    def _read_all(self, parameters, context, grid, crs, sampler, feedback, own_id):
        rec = {"structures": [], "roads": [], "railroads": [], "lifelines": [], "dams": []}
        extra, inputs, fields, outside = {}, {}, {}, {}
        # structures
        got = self._source(parameters, context, "STRUCTURES", STRUCT_SYNONYMS, grid, crs,
                           feedback, "Structures")
        if got:
            feats, fld, to_r, lname = got
            inputs["structures"], fields["structures"] = lname, fld
            words = tuple(w.strip() for w in re.split(r"[,;\n]+", self._str(
                parameters, "UNINHABITED_WORDS", context) or UNINHABITED_DEFAULT) if w.strip())
            how_n, out_n = {}, 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                ident = _txt(_attr(f, fld["S_ID"])) or str(f.id())
                occ = _txt(_attr(f, fld["S_OCC"]))
                prim = _txt(_attr(f, fld["S_MOBILE"])) if fld["S_MOBILE"] != fld["S_OCC"] else ""
                # uninhabited when either occupancy text names it (same as the screening plugin)
                hab, how = habitable_from(_attr(f, fld["S_HABITABLE"]),
                                          " | ".join(t for t in (occ, prim) if t), words)
                how_n[how] = how_n.get(how, 0) + 1
                mob = is_mobile(_attr(f, fld["S_MOBILE"]), occ)
                rec["structures"].append(nh.Structure(
                    ident, s["depth_ft"], s["vel_fts"], habitable=hab, mobile=mob, x=s["x"],
                    y=s["y"], note=s["how"]))
                extra[("structure", ident, s["x"], s["y"])] = {
                    "vel_at_max_depth_fts": None if s["vel_at_max_fts"] is None else round(
                        s["vel_at_max_fts"], 2), "habitable": hab,
                    "habitable_from": how, "mobile": mob, "occupancy": occ,
                    "sampled": s["how"]}
            outside["structures"] = out_n
            feedback.pushInfo("Structures: {} inside the rasters; habitable from the field {}, "
                              "from the occupancy class {}, unknown (taken as habitable) {}"
                              .format(len(rec["structures"]), how_n.get("field", 0),
                                      how_n.get("class", 0), how_n.get("unknown", 0)))
        # roads
        got = self._source(parameters, context, "ROADS", ROAD_SYNONYMS, grid, crs, feedback,
                           "Roads")
        if got:
            feats, fld, to_r, lname = got
            inputs["roads"], fields["roads"] = lname, fld
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                name = _txt(_attr(f, fld["R_ID"]))
                rec["roads"].append(nh.Road(
                    name or "road {}".format(f.id()), s["depth_ft"],
                    aadt=qio.num(_attr(f, fld["R_AADT"])),
                    interstate=is_interstate(_txt(_attr(f, fld["R_CLASS"])), name),
                    us_state=is_us_state(_txt(_attr(f, fld["R_CLASS"])), name, fld["R_CLASS"]),
                    private=is_private(_attr(f, fld["R_PRIVATE"])),
                    sole_access=bool(_yesno(_attr(f, fld["R_SOLE"]))), x=s["x"], y=s["y"]))
                self._keep_parts(rec["roads"][-1], f.geometry(), to_r)
            outside["roads"] = out_n
        # railroads
        got = self._source(parameters, context, "RAIL", RAIL_SYNONYMS, grid, crs, feedback,
                           "Railroads")
        if got:
            feats, fld, to_r, lname = got
            inputs["railroads"], fields["railroads"] = lname, fld
            codes = {t.strip().upper() for t in re.split(r"[,;\s]+", self._str(
                parameters, "PASSENGER_CODES", context) or PASSENGER_DEFAULT) if t.strip()}
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                rec["railroads"].append(nh.Railroad(
                    _txt(_attr(f, fld["T_ID"])) or "railroad {}".format(f.id()), s["depth_ft"],
                    trains_per_day=qio.num(_attr(f, fld["T_TRAINS"])),
                    passenger=is_passenger(_attr(f, fld["T_PASSENGER"]), codes) if
                    fld["T_PASSENGER"] else False, x=s["x"], y=s["y"]))
            outside["railroads"] = out_n
        # lifelines
        got = self._source(parameters, context, "LIFELINES", LIFELINE_SYNONYMS, grid, crs,
                           feedback, "Lifelines")
        if got:
            feats, fld, to_r, lname = got
            inputs["lifelines"], fields["lifelines"] = lname, fld
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                rec["lifelines"].append(nh.Lifeline(
                    _txt(_attr(f, fld["L_ID"])) or "lifeline {}".format(f.id()), s["depth_ft"],
                    kind=_txt(_attr(f, fld["L_KIND"])), x=s["x"], y=s["y"]))
            outside["lifelines"] = out_n
        # downstream dams
        dam_xy = {}
        got = self._source(parameters, context, "DOWNSTREAM_DAMS", DAM_SYNONYMS, grid, crs,
                           feedback, "Downstream dams")
        if got:
            feats, fld, to_r, lname = got
            inputs["downstream_dams"], fields["downstream_dams"] = lname, fld
            out_n = 0
            for f in feats:
                s = self._sample(sampler, f.geometry(), to_r)
                if s is None:
                    out_n += 1
                    continue
                ident = _txt(_attr(f, fld["D_ID"])) or "dam {}".format(f.id())
                if own_id and ident.lower() == own_id.lower():
                    feedback.pushInfo("Downstream dams: {} is this dam; left out.".format(ident))
                    continue
                aff = s["depth_ft"] >= sampler.wet
                rec["dams"].append(nh.DownstreamDam(
                    ident, dam_class(_attr(f, fld["D_CLASS"])), affected=aff,
                    note="flood depth {:.1f} ft at the dam".format(s["depth_ft"])))
                dam_xy[ident] = (s["x"], s["y"])
            outside["downstream_dams"] = out_n
            feedback.pushInfo("Downstream dams: {} inside the rasters, {} reached by the "
                              "flood".format(len(rec["dams"]),
                                             sum(1 for x in rec["dams"] if x.affected)))
        self._read_more(parameters, context, grid, crs, sampler, feedback, rec, extra, inputs,
                        fields, outside)
        for k, n in outside.items():
            if n:
                feedback.pushInfo("{}: {} feature(s) outside the rasters were not used."
                                  .format(k.replace("_", " ").capitalize(), n))
        return rec, extra, dam_xy, inputs, fields

    def _read_more(self, parameters, context, grid, crs, sampler, feedback, rec, extra, inputs,
                   fields, outside):
        """Sample individual GeoPackages named in STRUCTURES_MORE, ROADS_MORE, LIFELINES_MORE."""
        specs = (("STRUCTURES_MORE", STRUCT_SYNONYMS, "structures", "Structures"),
                 ("ROADS_MORE", ROAD_SYNONYMS, "roads", "Roads"),
                 ("LIFELINES_MORE", LIFELINE_SYNONYMS, "lifelines", "Lifelines"))
        for key, synonyms, bucket, label in specs:
            if self.parameterDefinition(key) is None:
                continue
            text = parameters.get(key) or ""
            if not str(text).strip():
                continue
            from qgis.core import QgsVectorLayer
            for uri in str(text).split(";;"):
                uri = uri.strip()
                if not uri:
                    continue
                stem = os.path.splitext(os.path.basename(uri.split("|", 1)[0]))[0] or label
                lyr = QgsVectorLayer(uri, stem, "ogr")
                try:
                    ok = bool(lyr.isValid())
                except Exception:
                    ok = False
                if not ok:
                    feedback.reportError("Could not open extra {} file: {}".format(label, uri))
                    continue
                fld = self._fields(parameters, context, qio.field_names(lyr), synonyms)
                used = ["{} = {}".format(k[2:].lower(), v) for k, v in fld.items() if v]
                feedback.pushInfo("Extra {} fields ({}): {}".format(
                    label, stem, ", ".join(used) or "none matched"))
                to_r = QgsCoordinateTransform(lyr.sourceCrs(), crs, context.transformContext())
                to_l = QgsCoordinateTransform(crs, lyr.sourceCrs(), context.transformContext())
                xmin, ymin, xmax, ymax = grid.bounds
                center = (0.5 * (xmin + xmax), 0.5 * (ymin + ymax))
                radius = 0.5 * float(np.hypot(xmax - xmin, ymax - ymin))
                feats = qio._features(lyr, center, radius, to_l)
                inputs[bucket] = (inputs[bucket] + " + " if inputs.get(bucket) else "") + stem
                fields[bucket] = fld
                out_n = 0
                if bucket == "structures":
                    words = tuple(w.strip() for w in re.split(r"[,;\n]+", self._str(
                        parameters, "UNINHABITED_WORDS", context) or UNINHABITED_DEFAULT) if w.strip())
                    for f in feats:
                        s = self._sample(sampler, f.geometry(), to_r)
                        if s is None:
                            out_n += 1
                            continue
                        ident = stem + ":" + (_txt(_attr(f, fld["S_ID"])) or str(f.id()))
                        occ = _txt(_attr(f, fld["S_OCC"]))
                        prim = _txt(_attr(f, fld["S_MOBILE"])) if fld["S_MOBILE"] != fld["S_OCC"] else ""
                        hab, how = habitable_from(_attr(f, fld["S_HABITABLE"]),
                                                  " | ".join(t for t in (occ, prim) if t), words)
                        mob = is_mobile(_attr(f, fld["S_MOBILE"]), occ)
                        rec["structures"].append(nh.Structure(
                            ident, s["depth_ft"], s["vel_fts"], habitable=hab, mobile=mob, x=s["x"],
                            y=s["y"], note=s["how"]))
                        extra[("structure", ident, s["x"], s["y"])] = {
                            "vel_at_max_depth_fts": None if s["vel_at_max_fts"] is None else round(
                                s["vel_at_max_fts"], 2), "habitable": hab,
                            "habitable_from": how, "mobile": mob, "occupancy": occ,
                            "sampled": s["how"]}
                elif bucket == "roads":
                    for f in feats:
                        s = self._sample(sampler, f.geometry(), to_r)
                        if s is None:
                            out_n += 1
                            continue
                        name = _txt(_attr(f, fld["R_ID"]))
                        rec["roads"].append(nh.Road(
                            stem + ":" + (name or "road {}".format(f.id())), s["depth_ft"],
                            aadt=qio.num(_attr(f, fld["R_AADT"])),
                            interstate=is_interstate(_txt(_attr(f, fld["R_CLASS"])), name),
                            us_state=is_us_state(_txt(_attr(f, fld["R_CLASS"])), name, fld["R_CLASS"]),
                            private=is_private(_attr(f, fld["R_PRIVATE"])),
                            sole_access=bool(_yesno(_attr(f, fld["R_SOLE"]))), x=s["x"], y=s["y"]))
                        self._keep_parts(rec["roads"][-1], f.geometry(), to_r)
                else:
                    for f in feats:
                        s = self._sample(sampler, f.geometry(), to_r)
                        if s is None:
                            out_n += 1
                            continue
                        rec["lifelines"].append(nh.Lifeline(
                            stem + ":" + (_txt(_attr(f, fld["L_ID"])) or "lifeline {}".format(f.id())),
                            s["depth_ft"], kind=_txt(_attr(f, fld["L_KIND"])), x=s["x"], y=s["y"]))
                outside[bucket] = outside.get(bucket, 0) + out_n
                feedback.pushInfo("Extra {}: {} feature(s) from {} inside the rasters".format(
                    label, len(feats) - out_n, stem))

    def _road_dem_units(self, parameters, context, path):
        """(metres per unit or None, how) of the 'Road surface DEM': its own unit, never the
        run DEM's factor."""
        from .core.units import FT, US_FT
        k = self.parameterAsEnum(parameters, "ROAD_DEM_UNITS", context)
        if k == 1:
            return 1.0, "metres (chosen)"
        if k == 2:
            return FT, "feet (chosen)"
        if k == 3:
            return US_FT, "US survey feet (chosen)"
        return dem_unit_of(path)

    @staticmethod
    def _sample_parts(f, pts_list):
        """f at each array of points, NaN for an array whose read fails (OSError, RuntimeError,
        ValueError: a missing VRT tile); one array of all."""
        out = []
        for q in pts_list:
            try:
                out.append(np.asarray(f(q), dtype=float))
            except (OSError, RuntimeError, ValueError):
                out.append(np.full(len(q), np.nan))
        return np.concatenate(out) if out else np.zeros(0)

    def _road_dem_check(self, ground, ghow, roads, parts, grid, wse, depth, wet, unit_m, cuts_all,
                        run_dem, run_z, given, rep):
        """3.10.90 review: a 'Road surface DEM' is used only when it matches the run's own ground
        (units and vertical datum): the median offset against the run's DEM at the wet centreline
        points (all centreline points of the roads when fewer than ROAD_DATUM_MIN_POINTS are wet),
        else against the run's model ground (WSE - depth) of the wet cells under them, within
        ROAD_DATUM_TOL_M. Returns (why not, or '', the check's record)."""
        step = ROAD_CHECK_STEP_M / unit_m
        wet_l, all_l = [], []
        for r in roads:
            for p in parts.get(id(r)) or []:
                q = rprof.densify(p, step)
                all_l.append(q)
                m = wet(q)
                if m.any():
                    wet_l.append(q[m])
        if not wet_l:
            return "", {"how": "no wet centreline point: nothing to check"}
        med, n, against = None, 0, ""
        if run_dem and run_z and os.path.abspath(run_dem) != os.path.abspath(given):
            try:
                ref = rprof.PointDEM(run_dem, run_z, grid.crs_wkt)
            except Exception:                                    # noqa - the next reference
                ref = None
            if ref is not None:
                use = wet_l if sum(len(q) for q in wet_l) >= ROAD_DATUM_MIN_POINTS else all_l
                med, n = _median_offset(self._sample_parts(ground, use),
                                        self._sample_parts(ref, use))
                against = "the run's DEM {} at {} {}centreline point(s)".format(
                    os.path.basename(run_dem), n, "wet " if use is wet_l else "")
        if med is None or n < ROAD_DATUM_MIN_POINTS:
            W = np.vstack(wet_l)
            excl = rprof.in_cuts(W, cuts_all) if cuts_all else None
            avg = str((rep.get("dem_resample") or {}).get("method") or "average") == "average"
            try:
                med, n = model_ground_offset(ground, grid, wse, depth, W, unit_m,
                                             getattr(ground, "pixel_m", 1.0), average=avg,
                                             exclude=excl)
            except (OSError, RuntimeError, ValueError) as ex:
                med, n = None, 0
                against = "the run's model ground (read failed: {})".format(ex)
            else:
                against = "the run's model ground (WSE - depth) of {} wet cell(s) under the " \
                          "roads, structure cuts left out".format(n)
        chk = {"against": against, "points": n,
               "median_offset_m": None if med is None else round(med, 3),
               "tolerance_m": ROAD_DATUM_TOL_M, "units": ghow}
        if med is None or n < ROAD_DATUM_MIN_POINTS:
            return ("the road surface DEM could not be checked against the run's own ground "
                    "({} comparable point(s); {}), so its units and vertical datum are not "
                    "confirmed".format(n, against or "no reference"), chk)
        if abs(med) > ROAD_DATUM_TOL_M:
            return ("the road surface DEM reads {:+.2f} m from {} (median; more than {:g} m): "
                    "its elevation units ({}) or vertical datum differ from the run's".format(
                        med, against, ROAD_DATUM_TOL_M, ghow), chk)
        return "", chk

    @staticmethod
    def _lower_reason(got):
        out = []
        fb = got.get("freeboard_ft")
        if fb is not None:
            out.append("the water surface is {:.2f} ft {} the road surface on the centreline"
                       .format(abs(fb), "below" if fb >= 0 else "over"))
        if got.get("n_cut"):
            out.append("{} wet point(s) in bridge / notch cut corridors read on the deck "
                       "stand-in (the lower approach), not the trench".format(got["n_cut"]))
        if got.get("n_opening"):
            out.append("{} on a bare-earth opening at a mapped bridge / notch read on the "
                       "stand-in".format(got["n_opening"]))
        nr = sum(got.get(k) or 0 for k in ("n_nodem", "n_nowse", "n_no_approach"))
        if nr:
            out.append("{} with no road surface, no water surface or no approach surface took "
                       "the depth raster ({} ft)".format(
                           nr, round(float(got.get("raster_points_ft") or 0.0), 2)))
        out.append("the depth raster's cell holds the cell's averaged (or cut) ground, not the "
                   "road surface")
        return "; ".join(out)

    def _road_crown(self, parameters, context, folder, grid, depth, roads, extra, feedback,
                    wet_ft=nh.MIN_WET_FT):
        """3.10.90 (DWR-3034): each road's overtopping depth from the water surface over the
        road surface along its centreline (core/road_profile), in place of the deepest cell of
        the depth raster. `depth` is the cleaned depth array (no-data 0). Every road read lower
        than the depth raster, and every road that falls back to it, gets a Check and a row in
        the report (road_depth.dropped / lowered / raster_roads). Returns the report entry."""
        from .core import gdal_io, workflow
        basis = rprof.BASIS_RASTER if self.parameterAsEnum(
            parameters, "ROAD_DEPTH_BASIS", context) == 1 else rprof.BASIS_CENTRELINE
        info = {"basis": basis, "dem": "", "dem_units": "", "roads_centreline": 0,
                "roads_raster": 0, "dropped": [], "lowered": [], "raster_roads": [],
                "text": ""}
        parts = getattr(self, "_road_parts", {})
        if not roads:
            info["text"] = "no road read"
            return info
        if basis == rprof.BASIS_RASTER:
            info["text"] = "the model depth raster under the road (chosen)"
            info["roads_raster"] = len(roads)
            return info
        rep = run_report_of(folder)
        wse_path = os.path.join(folder, workflow.FINAL_NAMES["wse"])
        given = self._str(parameters, "ROAD_DEM", context)
        run_dem, run_z, run_how, cell = road_dem_of(folder, rep)
        why, z, zhow = "", None, ""
        if given:
            dem = given if os.path.exists(given) else None
            if dem is None:
                why = "the road surface DEM {} is not there".format(given)
            else:
                z, zhow = self._road_dem_units(parameters, context, dem)
                if z is None:
                    why = ("the road surface DEM {}: {}; set 'Road surface DEM elevation "
                           "units'".format(os.path.basename(dem), zhow))
        else:
            dem, z, zhow = run_dem, run_z, run_how
            if dem is None:
                why = "no road surface DEM (the run's DEM is not in run_report.json or not there)"
            elif z is None:
                why = ("the run's DEM {}: {}; give it as 'Road surface DEM' with its elevation "
                       "units".format(os.path.basename(dem), zhow))
        if not os.path.exists(wse_path):
            why = "no '{}' in the dam folder".format(workflow.FINAL_NAMES["wse"])
        unit_m = 1.0
        if not why and grid.crs_wkt:
            try:
                lu = gdal_io.linear_unit_m(grid.crs_wkt)
            except Exception:                                    # noqa - metres assumed
                lu = 1.0
            if lu is None:
                why = "the rasters' CRS is not projected"
            else:
                unit_m = float(lu)
        ground = wse = None
        if not why:
            try:
                wse = gdal_io.read_like(wse_path, grid, "near")
                ground = rprof.PointDEM(dem, z, grid.crs_wkt)
            except Exception as ex:                              # noqa - fall back
                why = "the water surface or road DEM could not be read ({})".format(ex)
        structs, cut_note = structures_in_raster_crs(structure_cuts_of(folder),
                                                     rep.get("work_crs"), grid.crs_wkt)
        if not why and cut_note.startswith("structures.json lines could not be moved"):
            feedback.reportError("Check: road overtopping: " + cut_note + " (a bridge cut's "
                                 "opening reads as it is).", fatalError=False)
        cell_m = float(cell or grid.dx * unit_m)
        cuts = rprof.cut_corridors(structs, cell_m, 0.0, unit_m)
        cuts_all = rprof.cut_corridors(structs, cell_m, 0.0, unit_m, kinds=None)
        depf = rprof.grid_sampler(grid, depth)

        def wet(xy):
            return np.nan_to_num(depf(xy)) > 0.0
        if not why and given:
            why, info["datum_check"] = self._road_dem_check(
                ground, zhow, roads, parts, grid, wse, depth, wet, unit_m, cuts_all, run_dem,
                run_z, given, rep)
        if why:
            info["basis"] = rprof.BASIS_RASTER
            info["roads_raster"] = len(roads)
            info["fallback"] = why
            info["text"] = ("the model depth raster under the road for every road: " + why +
                            ". A structure cut's trench can read as road overtopping.")
            feedback.reportError("Check: road overtopping read from the depth raster for every "
                                 "road: " + why, fatalError=False)
            return info
        wsef = rprof.grid_sampler(grid, wse)
        info.update(dem=dem, dem_units=zhow)
        n_c = n_hi = 0
        n_cut_pts = n_open_pts = n_unm = n_ras_pts = 0
        matched = set()
        for r in roads:
            pr = parts.get(id(r))
            raster_ft = float(r.max_depth_ft or 0.0)
            rx, ry = r.x, r.y
            base = {"id": str(r.id), "raster_depth_ft": round(raster_ft, 2), "raster_x": rx,
                    "raster_y": ry}
            reason = ""
            got = None
            if not pr:
                reason = "no centreline geometry"
            else:
                try:
                    got = rprof.road_crown_depth(pr, wsef, ground, wet=wet, cuts=cuts,
                                                 depth_ft=depf, unit_m=unit_m)
                except (OSError, RuntimeError, ValueError) as ex:
                    reason = "the road surface DEM could not be read ({})".format(ex)
                if got is None and not reason:
                    reason = "no wet centreline point has both a water surface and a road surface"
            if got is None:
                rec = dict(base, reason=reason)
                info["raster_roads"].append(rec)
                if raster_ft >= wet_ft:
                    feedback.reportError("Check: road {}: overtopping read from the depth raster "
                                         "({:.2f} ft): {}.".format(r.id, raster_ft, reason),
                                         fatalError=False)
                continue
            n_c += 1
            new = round(float(got["depth_ft"]), 2)
            r.max_depth_ft = new
            if got["x"] is not None:
                r.x, r.y = got["x"], got["y"]
            fb = None if got["freeboard_ft"] is None else round(float(got["freeboard_ft"]), 2)
            n_cut_pts += got["n_cut"]
            n_open_pts += got["n_opening"]
            n_unm += got["n_opening_unmatched"]
            n_ras = got["n_nodem"] + got["n_nowse"] + got["n_no_approach"]
            n_ras_pts += n_ras
            if got["n_cut"] or got["n_opening"]:
                matched.add(str(r.id))
            rec = dict(base, centreline_depth_ft=new, x=r.x, y=r.y, freeboard_ft=fb,
                       n_cut=got["n_cut"], n_opening=got["n_opening"], n_nodem=got["n_nodem"],
                       n_nowse=got["n_nowse"], n_no_approach=got["n_no_approach"])
            extra[("road", str(r.id), r.x, r.y)] = {
                "depth_basis": "centreline", "raster_depth_ft": round(raster_ft, 2),
                "raster_x": rx, "raster_y": ry, "freeboard_ft": fb,
                "centreline_points_in_cuts": got["n_cut"],
                "centreline_points_in_openings": got["n_opening"],
                "centreline_points_no_road_surface": got["n_nodem"],
                "centreline_points_no_water_surface": got["n_nowse"],
                "centreline_points_no_approach": got["n_no_approach"], "sampled": got["how"]}
            if raster_ft >= wet_ft and new < round(raster_ft, 2):
                status = "dropped" if new < wet_ft else "lowered"
                rec.update(status=status, reason=self._lower_reason(got))
                info[status].append(rec)
                feedback.reportError(
                    "Check: road {}: {} - the depth raster reads {:.2f} ft at ({}, {}); the road "
                    "centreline reads {:.2f} ft{} ({}). Check it, or set 'Road overtopping depth' "
                    "to the depth raster.".format(
                        r.id, "not counted as overtopped" if status == "dropped" else
                        "overtopped, shallower than the depth raster", raster_ft,
                        None if rx is None else round(rx, 1),
                        None if ry is None else round(ry, 1), new,
                        "" if fb is None else " (freeboard {:.2f} ft)".format(fb),
                        rec["reason"]), fatalError=False)
            elif new > round(raster_ft, 2) + 0.005:
                n_hi += 1
            if n_ras and new >= wet_ft:
                feedback.reportError(
                    "Check: road {}: {} wet centreline point(s) read the depth raster ({:.2f} ft): "
                    "{} with no road surface (DEM no-data or outside it), {} with no water "
                    "surface, {} in a bridge / notch cut with no approach on the road line (a road "
                    "layer split at the bridge).".format(
                        r.id, n_ras, float(got["raster_points_ft"] or 0.0), got["n_nodem"],
                        got["n_nowse"], got["n_no_approach"]),
                    fatalError=False)
        n_r = len(info["raster_roads"])
        n_culv = sum(1 for s in structs if str(s.get("kind", "")).lower()
                     not in rprof.MASK_KINDS)
        info.update(roads_centreline=n_c, roads_raster=n_r, roads_higher=n_hi,
                    bridge_notch_cuts=len(cuts), culverts_not_masked=n_culv,
                    roads_on_a_stand_in=len(matched), points_on_cut_stand_in=n_cut_pts,
                    points_on_opening_stand_in=n_open_pts, openings_unmatched=n_unm,
                    points_from_depth_raster=n_ras_pts, unit_m=unit_m,
                    structures_note=cut_note)
        info["text"] = (
            "water surface ('{}') minus the road surface ({}, {}) along the centreline every "
            "{:g} m, for {} road(s); {} bridge / notch cut(s) in structures.json, {} road(s) "
            "with wet points in one ({} point(s) read on the deck stand-in from the approaches)"
            "{}; {} culvert(s) not masked (the fine DEM is the road over them); bare-earth "
            "openings at a mapped bridge / notch: {} point(s) on the stand-in, {} other "
            "opening(s) read as they are; {} wet point(s) with no road surface or water "
            "surface took the depth raster; {} road(s) from the depth raster; {} road(s) no "
            "longer overtopped and {} shallower than the depth raster (listed); {} deeper."
        ).format(workflow.FINAL_NAMES["wse"], os.path.basename(dem), zhow, rprof.STEP_M, n_c,
                 len(cuts), len(matched), n_cut_pts, " - " + cut_note if cut_note else "",
                 n_culv, n_open_pts, n_unm, n_ras_pts, n_r, len(info["dropped"]),
                 len(info["lowered"]), n_hi)
        feedback.pushInfo("Road overtopping depth: " + info["text"])
        return info

    def _keep_parts(self, road, geom, to_r):
        try:
            kind, parts, _c = _parts(geom, to_r)
        except Exception:
            return
        if kind == "line" and parts:
            self.__dict__.setdefault("_road_parts", {})[id(road)] = parts

    def _join_aadt(self, parameters, context, grid, crs, roads, feedback):
        """DWR-3161: give roads without an AADT field value the AADT of the traffic layers the
        Hazard plugin uses (HPMS, NDDOT counts, county roads), so 5.2.1.2 is the same check."""
        self._aadt_log = []
        parts = getattr(self, "_road_parts", {})
        need = [r for r in roads if r.aadt is None and id(r) in parts]
        if not need:
            return
        paths = [p.strip() for p in self._str(parameters, "AADT_LAYERS", context).split(";;")
                 if p.strip()] or aadt_join.siblings(_source_path(parameters, "ROADS"))
        if not paths:
            feedback.reportError("Check: no AADT layer given or found next to the roads file; "
                                 "{} road(s) have no AADT.".format(len(need)), fatalError=False)
            return
        from qgis.core import QgsCoordinateReferenceSystem, QgsPointXY
        tc = context.transformContext()

        def make_transform(srs):
            t = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:{}".format(srs)), crs, tc)
            return lambda x, y: (lambda q: (q.x(), q.y()))(t.transform(QgsPointXY(x, y)))

        def make_bbox(srs):
            t = QgsCoordinateTransform(crs, QgsCoordinateReferenceSystem("EPSG:{}".format(srs)), tc)
            xmin, ymin, xmax, ymax = grid.bounds
            pts = [t.transform(QgsPointXY(x, y)) for x in (xmin, xmax) for y in (ymin, ymax)]
            return (min(p.x() for p in pts), min(p.y() for p in pts),
                    max(p.x() for p in pts), max(p.y() for p in pts))
        try:
            traffic = aadt_join.read_layers(paths, make_transform, make_bbox)
        except Exception as ex:
            feedback.reportError("Check: could not read the AADT layers ({}).".format(ex),
                                 fatalError=False)
            return
        ft = self.parameterAsDouble(parameters, "AADT_JOIN_FT", context)
        max_m = ft * 0.3048 if ft else aadt_join.DEFAULT_MAX_M
        # M13: aadt_join.match/apply measure distances in the grid's own coordinate units, while
        # max_m is metres. On a feet CRS those units are feet, so the metre radius must be
        # expressed in grid units (metres / metres-per-unit) or the ~30 m search radius collapses
        # to ~9 m and overtopped roads stop matching their traffic layer. (metres-per-unit idiom
        # shared with algorithms_tools / products / algorithms_hecras: linear_unit_m or 1.0.)
        from .core import gdal_io
        try:
            unit_m = gdal_io.linear_unit_m(grid.crs_wkt) or 1.0
        except Exception:
            unit_m = 1.0            # GDAL unavailable / CRS not projected: assume metre units
        max_units = max_m / unit_m
        self._aadt_log = aadt_join.apply(need, parts, traffic, max_units,
                                         is_wet=getattr(self, "_is_wet", None))
        for j in self._aadt_log:
            feedback.pushInfo("AADT joined: {road} = {aadt:g} ({year}) from {source}".format(**j))
        feedback.pushInfo("AADT join: {} of {} road(s) without AADT matched in {}".format(
            len(self._aadt_log), len(need), ", ".join(os.path.basename(p) for p in paths)))

    def processAlgorithm(self, parameters, context, feedback):
        folder = self.parameterAsFile(parameters, "DAM_FOLDER", context)
        st = self._settings(parameters, context)
        charts = self._charts(parameters, context) if st.lethality == nh.LETHALITY_CHARTS \
            else None
        try:
            grid, with_f, _base, _notes = cons.read_dam_folder(folder)
        except ImportError:
            raise QgsProcessingException("GDAL's Python bindings are not available; run this "
                                         "from QGIS.")
        except (OSError, ValueError, RuntimeError) as ex:
            raise QgsProcessingException(str(ex))
        vel = with_f.get("velocity")
        if vel is None:
            if charts is not None:
                raise QgsProcessingException(
                    "The chart method needs 'Velocity (Max).tif' in the dam folder; it is "
                    "missing. Re-run the model, or use the conservative method.")
            feedback.reportError("Check: no 'Velocity (Max).tif'; velocities are reported as "
                                 "0 (not used by the chosen method).", fatalError=False)
        if st.lethality == nh.LETHALITY_1D:
            feedback.reportError("Check: ND 5.2.1.1 allows lethality from depth alone only for a "
                                 "1D hydraulic model. This plugin's results are 2D (LISFLOOD-FP / "
                                 "ANUGA): the policy expects the depth-velocity charts here.",
                                 fatalError=False)
        dam_id = _dam_id(folder)
        crs = self._raster_crs(grid.crs_wkt)
        sampler = Sampler(grid, with_f["depth"], vel, st.wet_ft)
        self._road_parts = {}
        self._is_wet = sampler.is_wet
        rec, extra, dam_xy, inputs, fields = self._read_all(
            parameters, context, grid, crs, sampler, feedback, dam_id)
        if not inputs:
            feedback.reportError("Check: no receptor layer was given; the class can only come "
                                 "out Low.", fatalError=False)
        road_info = self._road_crown(parameters, context, folder, grid, sampler.d,
                                     rec["roads"], extra, feedback, st.wet_ft)
        self._join_aadt(parameters, context, grid, crs, rec["roads"], feedback)
        try:
            c = nh.classify(rec["structures"], rec["roads"], rec["railroads"], rec["lifelines"],
                            rec["dams"], settings=st, charts=charts)
        except nh.ChartsNotDigitized as ex:
            raise QgsProcessingException(not_digitized_message(charts) if charts else str(ex))
        except ValueError as ex:
            raise QgsProcessingException(str(ex))
        status = aadt_join.class_status(c.hazard_class, c.data_gaps, c.governing)
        hz = aadt_join.hazard_module_class(folder)
        road_check = {"hazard_class_status": status, "hazard_module_class": hz,
                      "class_to_use": hz or c.hazard_class,
                      "aadt_joined": getattr(self, "_aadt_log", []),
                      "road_depth": road_info}
        flag = _flagged_entry(folder)
        if flag:
            # 3.10.53: a flagged structure (damsuite/structure_flags.py, e.g. DWR-3161, the ND-23
            # road fill over backwater) keeps its hazard of record; no road rule (5.2.1.2(b)
            # included) or computed class replaces it - this class is for information only
            road_check["flagged_structure"] = "{} ({})".format(sflags.note(flag),
                                                               sflags.short_ref(flag))
            road_check["class_to_use"] = flag.get("hazard")
        for f in c.findings:
            if f.kind == "dam" and f.x is None and f.id in dam_xy:
                f.x, f.y = dam_xy[f.id]
        try:
            paths = self._write(folder, dam_id, c, rec, extra, inputs, fields, charts, grid,
                                road_check)
        except OSError as ex:
            raise QgsProcessingException("Could not write the results: {}".format(ex))
        feedback.pushInfo("Hazard class: {} ({}; {}).".format(
            c.hazard_class.upper(), nh.POLICY, nh.LETHALITY_LABELS[st.lethality]))
        if status != aadt_join.STATUS_COMPLETE:
            feedback.reportError("Check: hazard class {} is {}: an overtopped road has no AADT, "
                                 "so the ND 5.2.1.2 road rule rests on an assumption.".format(
                                     c.hazard_class.upper(), status), fatalError=False)
        if flag:
            feedback.reportError("Check: {} is a flagged structure: {}. The hazard of record ({}) "
                                 "stands; the class computed here ({}) is for information only "
                                 "(docs/Flagged_structures.md).".format(
                                     sflags.short_ref(flag), sflags.note(flag), flag.get("hazard"),
                                     c.hazard_class.upper()), fatalError=False)
        if hz:
            msg = "Hazard plugin result ({}): {}.".format(aadt_join.HAZARD_SUMMARY, hz)
            if hz.lower() != c.hazard_class.lower():
                feedback.reportError("Check: this class ({}) differs from the ".format(
                    c.hazard_class.upper()) + msg + " Use the Hazard plugin's class.",
                    fatalError=False)
            else:
                feedback.pushInfo(msg + " Same class.")
        for r in c.reasons()[:30]:
            feedback.pushInfo("Governing: " + r)
        if len(c.governing) > 30:
            feedback.pushInfo("Governing: ... and {} more".format(len(c.governing) - 30))
        if c.borderline_raised:
            feedback.reportError("Check: every governing finding is borderline or rests on an "
                                 "assumption; the higher class was assigned (5.3).",
                                 fatalError=False)
        for gap in c.data_gaps:
            feedback.reportError("Data gap: " + gap, fatalError=False)
        for n in c.notes:
            feedback.pushInfo("Note: " + n)
        feedback.pushInfo("Screening classification for the engineer; the official "
                          "classification is made by the ND Department of Water Resources.")
        # v3.2.0: dam.json gets a "classified" status marked as a PREVIEW (never the authority)
        from .core import damjson
        state, text = damjson.classified_preview(folder)
        if state == "error":
            feedback.reportError("Check: " + text, fatalError=False)
        else:
            feedback.pushInfo(text)
        if paths["geojson"]:
            self.load(context, paths["geojson"], "Hazard classification findings")
        return {"CLASS": c.hazard_class, "REPORT": paths["markdown"], "JSON": paths["json"],
                "CSV": paths["csv"], "GEOJSON": paths["geojson"]}

    # ------------------------------------------------------------------------------
    @staticmethod
    def _write(folder, dam_id, c, rec, extra, inputs, fields, charts, grid, road_check=None):
        from .core import products
        out = {k: os.path.join(folder, v) for k, v in OUTPUT_NAMES.items()}
        figures = []
        if charts is not None:
            wet = [s for s in rec["structures"] if s.habitable and s.depth_ft is not None
                   and s.depth_ft >= c.settings["wet_ft"]]
            for key, mobile in (("svg_house", False), ("svg_mobile", True)):
                ch = charts.charts[nh.CHART_MOBILE if mobile else nh.CHART_HOUSE]
                pts = [s for s in wet if s.mobile == mobile]
                with open(out[key], "w", encoding="utf-8", newline="") as f:
                    f.write(nh.chart_svg(ch, pts, title="{}{} ({} structure(s))".format(
                        dam_id + ": " if dam_id else "", ch.title, len(pts))))
                figures.append(OUTPUT_NAMES[key])
        else:
            for key in ("svg_house", "svg_mobile"):     # stale figures of an earlier chart run
                if os.path.exists(out[key]):
                    os.remove(out[key])
                out[key] = ""
        md = nh.markdown(c, dam_id, title="Hazard classification (ND 5.2)")
        road_check = road_check or {}
        md = md + aadt_join.markdown_lines(road_check) + "\n"
        lines = ["", "## Inputs", "", "- Dam folder: {}".format(folder)]
        for k in ("structures", "roads", "railroads", "lifelines", "downstream_dams"):
            if k in inputs:
                used = ", ".join("{} = {}".format(a[2:].lower(), b)
                                 for a, b in fields[k].items() if b) or "no field matched"
                lines.append("- {}: {} ({})".format(k.replace("_", " ").capitalize(),
                                                    inputs[k], used))
            else:
                lines.append("- {}: not given".format(k.replace("_", " ").capitalize()))
        lines += ["- Sampling: " + SAMPLING]
        rd = road_check.get("road_depth") or {}
        if rd:
            lines += ["- Road overtopping depth: " + rd.get("text", "")]
            lines += road_depth_rows(rd)
        if figures:
            lines += ["- Chart figures: " + ", ".join(figures)]
        with open(out["markdown"], "w", encoding="utf-8", newline="") as f:
            f.write(md + "\n".join(lines) + "\n")
        d = c.to_dict()
        d.update({"dam_id": dam_id, "dam_folder": folder, "inputs": inputs, "fields": fields,
                  "sampling": SAMPLING, "figures": figures,
                  "lethality_label": nh.LETHALITY_LABELS.get(c.settings.get("lethality"), ""),
                  "tool": "ndbreach:ndhazardclass",
                  "hazard_class_status": road_check.get("hazard_class_status",
                                                        aadt_join.STATUS_COMPLETE),
                  "hazard_module_class": road_check.get("hazard_module_class"),
                  "class_to_use": road_check.get("class_to_use", c.hazard_class),
                  "flagged_structure": road_check.get("flagged_structure", ""),
                  "aadt_joined": road_check.get("aadt_joined") or [],
                  "road_depth": road_check.get("road_depth") or {},
                  "official": "The official classification is made by the ND Department of "
                              "Water Resources."})
        with open(out["json"], "w", encoding="utf-8", newline="") as f:
            json.dump(d, f, indent=1, default=str)
        with open(out["csv"], "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=nh.FINDING_COLUMNS)
            w.writeheader()
            w.writerows(nh.finding_rows(c))
        feats = []
        for f in c.findings:
            if f.x is None or f.y is None:
                continue
            props = {k: getattr(f, k) for k in nh.FINDING_COLUMNS if k not in ("x", "y")}
            props.update(extra.get((f.kind, f.id, f.x, f.y), {}))
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Point", "coordinates": [f.x, f.y]}})
        epsg = None
        try:
            from .core import gdal_io
            epsg = gdal_io.epsg_of(grid.crs_wkt) if grid.crs_wkt else None
        except Exception:
            epsg = None
        products.write_geojson(out["geojson"], feats, epsg)
        return out


NDHAZARD_ALGORITHMS = (NDHazardAlgorithm,)
