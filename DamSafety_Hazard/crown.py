# -*- coding: utf-8 -*-
"""Road / rail overtopping on the centreline crown (DWR-2801 review, Hazard 2.18.65).

REG_05.2026 5.2.1.2 / 5.2.1.3 count a road or railroad that is OVERTOPPED. Up to 2.18.64 a
stretch counted wherever the 30 m 'Depth (Max)' raster was wet under the line, so a cell
holding the embankment toe, or a bridge cut, counted as the track or road being overtopped:
in DWR-2801 all 9 BNSF stretches were toe cells 2.6 - 8.7 ft below the 1 m track crest, or
bridge cuts, and a 0.03 ft cell with no arrival time counted as well.

A wet stretch is now overtopped only where the maximum water surface is above the CROWN on
the line's centreline (the L13 rule: WSE (Max) - the 1 m DEM on the centreline), read with the
Runner's road_profile (vendored here, Runner 3.10.90 / PR #198):

* the centreline of the wet stretch, plus EXTEND_M of the line past each end (so the deck
  stand-in has its approaches and an opening its shoulders), at 1 m steps;
* crown = the run's own 1 m DEM (run_report.json settings.input_files.DEM, in its recorded
  elevation unit), WSE = 'WSE (Max).tif' next to the depth raster;
* bridges and notches of the run (structures.json, cut through the embankment as open
  channels) are read against a deck stand-in from the approaches, as the Runner's ND 5.2
  road check (cut_corridors / deck_standin); culverts are not masked (buried: the DEM is the
  road over them);
* wet points with no 1 m surface or no WSE take the depth raster there.

Before that, a stretch is skipped when its deepest grid depth is below ARRIVAL_FT (0.1 ft,
the Runner's arrival depth) or, when an arrival raster is given, it has no arrival time (the
flood never reached the arrival depth there).

When the run has no WSE raster or no 1 m DEM with a recorded unit (a HEC-RAS plan, user
rasters, an older run, a geographic analysis CRS), the depth raster is used as before and
every stretch's Depth_Basis / Notes say so.

Pure GDAL + numpy (no QGIS): the callers give the depth reader for the fallback points."""
import json
import math
import os

import numpy as np

from . import road_profile as RP

FT = 0.3048
US_FT = 1200.0 / 3937.0
ARRIVAL_FT = 0.1             # Runner ARRIVAL_FT: shallower grid depth is not counted
EXTEND_M = 60.0              # centreline read this far past each end of a wet stretch
WSE_NAME = "WSE (Max).tif"

BASIS_CROWN = "WSE - 1 m centreline crown"
BASIS_RASTER = "depth raster (centreline points with no 1 m crown / WSE)"
BASIS_GRID = "depth raster (no crown check)"

DECISION_COUNTED = "counted"
DECISION_SHALLOW = "skipped: grid depth below {:g} ft".format(ARRIVAL_FT)
DECISION_NO_ARRIVAL = "skipped: no arrival time"
DECISION_BELOW_CROWN = "skipped: WSE not above the centreline crown"


def _json(path):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        return got
    except (OSError, ValueError):
        return None


def dem_unit(rep):
    """Metres per unit of the run's DEM from run_report.json (dem_z_to_m, else its units
    note), or None: no guess (the caller falls back to the depth raster)."""
    try:
        if rep.get("dem_z_to_m"):
            return float(rep["dem_z_to_m"])
    except (TypeError, ValueError):
        return None
    note = str(rep.get("dem_elevation_units") or "").strip().lower()
    if note.startswith("us survey feet"):
        return US_FT
    if note.startswith("feet"):
        return FT
    if note.startswith("metres") or note.startswith("meters"):
        return 1.0
    return None


def _srs(text):
    from osgeo import osr
    sr = osr.SpatialReference()
    t = str(text or "")
    if t.upper().startswith("EPSG:"):
        sr.ImportFromEPSG(int(t.split(":")[1].split()[0]))
    else:
        sr.SetFromUserInput(t)
    try:
        sr.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        pass
    return sr


def structures_in_crs(structs, work_crs, crs_wkt):
    """(structures.json dicts with their lines moved from the run's work CRS to `crs_wkt`,
    note) - as the Runner's algorithms_ndhazard.structures_in_raster_crs."""
    structs = [s for s in structs or [] if isinstance(s, dict)]
    if not structs or not work_crs or not crs_wkt:
        return structs, ""
    try:
        from osgeo import osr
        src, dst = _srs(work_crs), _srs(crs_wkt)
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
        return out, "structures.json lines moved from {} into the analysis CRS".format(
            str(work_crs)[:40])
    except Exception as ex:                                     # noqa - reported
        return [], "structures.json lines could not be moved ({}); no bridge cut used".format(ex)


class CrownSource(object):
    """The run's WSE (Max) (ft) and 1 m DEM (m) read at analysis-CRS points, and its bridge /
    notch cut corridors in the analysis CRS. close() releases the rasters."""

    def __init__(self, wse, dem, cuts, unit_m, info):
        self.wse, self.dem, self.cuts, self.unit_m, self.info = wse, dem, cuts, unit_m, info

    def close(self):
        for r in (self.wse, self.dem):
            try:
                r.cache.clear()
                r.band = r.ds = None
            except Exception:
                pass

    def wse_ft(self, xy):
        w = np.asarray(self.wse(xy), dtype=float)
        return np.where(w > -9990.0, w, np.nan)


def _unit_m(crs_wkt):
    sr = _srs(crs_wkt)
    if not sr.IsProjected():
        return None
    return float(sr.GetLinearUnits() or 1.0)


def open_source(depth_path, crs_wkt, wse_path=None):
    """(CrownSource or None, note). The run is the Runner result next to the depth raster:
    WSE (Max).tif (or `wse_path`) and _breach_model/run_report.json's DEM / structures.json.
    None with the reason when anything needed is missing (the depth raster is used)."""
    if not depth_path:
        return None, "no depth raster path"
    folder = os.path.dirname(os.path.abspath(str(depth_path)))
    wse = wse_path or os.path.join(folder, WSE_NAME)
    if not os.path.isfile(wse):
        return None, "no '{}' next to the depth raster".format(WSE_NAME)
    rep = _json(os.path.join(folder, "_breach_model", "run_report.json"))
    if not isinstance(rep, dict):
        return None, "no Runner run_report.json (_breach_model) next to the depth raster"
    st = rep.get("settings") or {}
    dem = (st.get("input_files") or {}).get("DEM")
    if not dem or not os.path.exists(dem):
        return None, "the run's 1 m DEM ({}) is not on this computer".format(dem or "none recorded")
    z = dem_unit(rep)
    if z is None:
        return None, "run_report.json records no DEM elevation unit (a run before Runner 3.10.16)"
    unit_m = _unit_m(crs_wkt)
    if unit_m is None:
        return None, "the analysis CRS is not projected"
    try:
        wr = RP.PointDEM(wse, 1.0, crs_wkt)
        dr = RP.PointDEM(dem, z, crs_wkt)
    except Exception as ex:                                     # noqa - reported
        return None, "WSE / DEM could not be opened ({})".format(ex)
    structs = _json(os.path.join(folder, "_breach_model", "structures.json"))
    structs, snote = structures_in_crs(structs if isinstance(structs, list) else [],
                                       rep.get("work_crs"), crs_wkt)
    cell = st.get("cell_size_m") or 30.0
    cuts = RP.cut_corridors(structs, float(cell), unit_m=unit_m)
    info = {"wse": wse, "dem": dem, "dem_z_to_m": z, "cuts": len(cuts),
            "structures_note": snote, "cell_m": cell}
    return CrownSource(wr, dr, cuts, unit_m, info), \
        "centreline crown check: WSE '{}' - 1 m DEM '{}' (x{:g} m), {} bridge / notch cut(s)" \
        "{}".format(os.path.basename(wse), os.path.basename(dem), z, len(cuts),
                    "; " + snote if snote else "")


def _along(line):
    return np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))]) \
        if len(line) > 1 else np.zeros(1)


def check_stretch(src, pts, i0, i1, depth_ft=None, extend_m=EXTEND_M):
    """Centreline crown check of the wet run pts[i0..i1] of the densified line part `pts`
    (analysis CRS). Returns a dict: basis, overtop_ft (>= 0), freeboard_ft (crown - WSE at the
    least-freeboard point; negative when overtopped), crown_ft / wse_ft there, x, y, n_cut,
    n_opening_unmatched, how; basis BASIS_GRID with a note when nothing could be read."""
    pts = np.asarray(pts, dtype=float).reshape(-1, 2)
    s = _along(pts)
    u = src.unit_m
    ext = extend_m / u
    a = int(np.searchsorted(s, s[i0] - ext, side="left"))
    b = int(np.searchsorted(s, s[i1] + ext, side="right")) - 1
    seg = pts[max(0, a):max(b, i1) + 1]
    seg_s = s[max(0, a):max(b, i1) + 1]
    # the stretch: from half-way to the dry sample before it to half-way to the one after
    lo = 0.5 * (s[i0] + s[i0 - 1]) if i0 > 0 else s[i0]
    hi = 0.5 * (s[i1] + s[i1 + 1]) if i1 + 1 < len(s) else s[i1]
    base = seg_s[0]

    def wet(xy):
        _d, al = RP.cond.polyline_distance(xy[:, 0], xy[:, 1], seg)
        al = al + base
        return (al >= lo - 1e-9) & (al <= hi + 1e-9)

    try:
        res = RP.road_crown_depth([seg], src.wse_ft, src.dem, wet=wet, cuts=src.cuts,
                                  depth_ft=depth_ft, unit_m=u)
    except Exception as ex:                                     # noqa - reported
        return {"basis": BASIS_GRID, "note": "crown check failed ({})".format(ex)}
    if res is None or res.get("freeboard_ft") is None:
        return {"basis": BASIS_GRID,
                "note": "no wet centreline point with both a 1 m crown and a WSE"}
    out = {"overtop_ft": float(res["depth_ft"]), "freeboard_ft": float(res["freeboard_ft"]),
           "x": res["x"], "y": res["y"], "n_cut": res["n_cut"], "n_opening": res["n_opening"],
           "n_opening_unmatched": res["n_opening_unmatched"], "how": res["how"],
           "crown_ft": None, "wse_ft": None}
    rp = res.get("raster_points_ft")
    # the depth raster governs only where it gives water over the line that the crown does not
    out["basis"] = BASIS_RASTER if (rp is not None and rp > 0.0
                                    and rp > -res["freeboard_ft"]) else BASIS_CROWN
    # crown / WSE at the least-freeboard centreline point (the crown is the road surface
    # there: the DEM, or the deck stand-in inside a bridge / notch cut)
    bx, by = res.get("best_x", res["x"]), res.get("best_y", res["y"])
    w = float(src.wse_ft(np.array([[bx, by]], dtype=float))[0])
    if math.isfinite(w):
        out["wse_ft"] = w
        out["crown_ft"] = w + float(res["freeboard_ft"])
        out["x"], out["y"] = (res["x"], res["y"]) if out["basis"] == BASIS_RASTER else (bx, by)
    return out


def line_check(st, arrival_ok, src=None, pts=None, i0=0, i1=0, depth_ft=None):
    """(decision, crown dict or None) of one wet stretch (a linear._stretches dict, before it
    is counted): below ARRIVAL_FT or (with an arrival raster) no arrival time -> skipped; else,
    with a CrownSource, counted only when the centreline overtopping is above 0."""
    if not (st.get("max_depth") or 0.0) >= ARRIVAL_FT:
        return DECISION_SHALLOW, None
    if arrival_ok and st.get("arrival") is None:
        return DECISION_NO_ARRIVAL, None
    if src is None or pts is None:
        return DECISION_COUNTED, None
    c = check_stretch(src, pts, i0, i1, depth_ft)
    if c.get("basis") in (BASIS_CROWN, BASIS_RASTER) and not c.get("overtop_ft", 0.0) > 0.0:
        return DECISION_BELOW_CROWN, c
    return DECISION_COUNTED, c
