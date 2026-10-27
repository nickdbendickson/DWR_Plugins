"""3.10.72: pre-run culvert invert check (no model, no QGIS; GDAL/OGR + numpy).

Before a dam's ANUGA run, lists what the Runner (3.10.71 logic) will do to each culvert end:
  * a given invert more than 0.3 m above the lowest fine-grid ground within
    max(apron, 5 m) of the end -> the Runner lowers it to ground + 0.3 m ("given_capped");
  * an end with no fine-grid data within that radius -> the Runner falls back to the
    coarse model DEM (30 m) value ("model_dem");
  * a missing invert -> the lowest fine ground ("fine_grid").
The decision itself is structures.fine_ground_inverts (and, first, structures.reconcile_inverts,
as in workflow.run_dam), so this report cannot drift from the run. Only light window reads of
the DEM are made around each culvert end; nothing is written next to the inputs.

Layer conventions are the Runner's structures layer (STRUCTURES): sizes and inverts in feet,
line from upstream to downstream, field names found through the same synonyms as the loader.
"""
import csv
import glob
import hashlib
import json
import math
import os
import xml.etree.ElementTree as ET

import numpy as np

from ..damsuite import gdalsafe as GS
from . import gdal_io
from . import structures as structmod
from .units import FT

# keep in step with algorithms.STRUCT_FIELD_SYNONYMS / scripts/ndbreach_cli.py
STRUCT_FIELD_SYNONYMS = {
    "S_KIND": ("type", "kind", "structure_type", "structtype"),
    "S_WIDTH": ("width", "span", "width_ft", "diameter", "dia", "span_ft"),
    "S_HEIGHT": ("height", "rise", "height_ft", "rise_ft"),
    "S_BARRELS": ("barrels", "n_barrels", "num_barrels", "cells"),
    "S_SHAPE": ("shape", "culvert_shape"),
    "S_INV_UP": ("invert_up", "inv_up", "upstream_invert", "invert_us"),
    "S_INV_DN": ("invert_dn", "inv_dn", "downstream_invert", "invert_ds"),
    "S_NAME": ("name", "id", "structure_id", "label"),
}
DEFAULT_W_FT = DEFAULT_H_FT = 4.0
RECONCILE_TOL_M = 3.0 * FT          # workflow: S_INVERT_TOL_FT default 3 ft
MODEL_CELL_M = 30.0                 # workflow: reconcile cell = min(model cell, 3 m)

FLAG_HIGH = "HIGH"                  # given invert > fine ground + 0.3 m (Runner caps it)
FLAG_FALLBACK = "FALLBACK"          # no fine ground within the radius (Runner: model DEM)
FLAG_SKIPPED = "SKIPPED"            # the Runner leaves this culvert out of ANUGA

CSV_COLUMNS = ("dam", "culvert_id", "fid", "name", "end", "x", "y", "given_invert_ft",
               "given_invert_m", "fine_ground_m", "delta_m", "fallback", "suggested_invert_m",
               "suggested_invert_ft", "flag", "runner_invert_m", "runner_source",
               "reconcile_note", "search_m", "fine_source", "fine_cell_m", "on_top_source", "width_ft",
               "rise_ft", "barrels", "shape", "note")


def _gdal():
    # no gdal/ogr.UseExceptions(): that is process-wide and would change QGIS and the other
    # plugins (test_v341); opens go through damsuite.gdalsafe, which raises in both modes
    from osgeo import gdal, ogr, osr
    return gdal, ogr, osr


def _num(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------ layer
def field_map(names, chosen=None):
    low = {n.lower(): n for n in names}
    chosen = dict(chosen or {})
    out = {}
    for key, syns in STRUCT_FIELD_SYNONYMS.items():
        c = chosen.get(key) or next((low[s] for s in syns if s in low), "")
        if c and c not in names:
            raise ValueError("field {} = '{}' is not in the layer ({})".format(
                key, c, ", ".join(names)))
        out[key] = c
    return out


def structure_from_attrs(line, at, fld, i):
    """One Structure exactly as the Runner's loader builds it (feet -> metres)."""
    g = lambda k: at.get(fld[k]) if fld.get(k) else None        # noqa: E731
    ktxt = str(g("S_KIND") or "").lower()
    kind = (structmod.KIND_BRIDGE if "bridge" in ktxt else
            structmod.KIND_NOTCH if any(w in ktxt for w in ("notch", "breach", "open"))
            else structmod.KIND_CULVERT)
    stxt = str(g("S_SHAPE") or "").lower()
    shape = "pipe" if any(w in stxt for w in ("pipe", "round", "circ", "cmp")) else "box"
    w_ft = _num(g("S_WIDTH"), DEFAULT_W_FT)
    h_ft = w_ft if shape == "pipe" else _num(g("S_HEIGHT"), DEFAULT_H_FT)
    iu, idn = _num(g("S_INV_UP")), _num(g("S_INV_DN"))
    return structmod.Structure(
        kind, np.asarray(line, dtype=float), w_ft * FT, h_ft * FT,
        int(_num(g("S_BARRELS"), 1) or 1), shape,
        iu * FT if iu is not None else None, idn * FT if idn is not None else None,
        name=str(g("S_NAME") or "{} {}".format(kind, i + 1)))


def read_structures(path, layer=None, fields=None):
    """[(fid, attrs, Structure)], layer CRS WKT. Multi-part lines become one per part."""
    _g, ogr, _o = _gdal()
    ds = GS.open_vector(path)
    if layer:
        lyr = ds.GetLayerByName(layer)
    else:
        lyr = None
        for i in range(ds.GetLayerCount()):
            cand = ds.GetLayerByIndex(i)
            if ogr.GT_Flatten(cand.GetGeomType()) in (ogr.wkbLineString, ogr.wkbMultiLineString):
                lyr = cand
                if cand.GetName() == "runner_culverts":
                    break
    if lyr is None:
        raise ValueError("{}: no line layer{}".format(path, " '{}'".format(layer) if layer else ""))
    srs = lyr.GetSpatialRef()
    if srs is None or not srs.IsProjected():
        raise ValueError("{}: the structures layer needs a projected CRS in metres".format(path))
    defn = lyr.GetLayerDefn()
    names = [defn.GetFieldDefn(k).GetName() for k in range(defn.GetFieldCount())]
    fld = field_map(names, fields)
    out, i = [], 0
    for f in lyr:
        geom = f.GetGeometryRef()
        if geom is None:
            continue
        at = {n: f.GetField(n) for n in names}
        parts = ([geom.GetGeometryRef(k) for k in range(geom.GetGeometryCount())]
                 if ogr.GT_Flatten(geom.GetGeometryType()) == ogr.wkbMultiLineString else [geom])
        for p in parts:
            pts = [(p.GetX(k), p.GetY(k)) for k in range(p.GetPointCount())]
            if len(pts) < 2:
                continue
            out.append((f.GetFID(), at, structure_from_attrs(pts, at, fld, i)))
            i += 1
    return out, srs.ExportToWkt(), lyr.GetName()


def read_polygons(path):
    """Polygon geometries of a layer (same CRS as the structures assumed)."""
    _g, ogr, _o = _gdal()
    ds = GS.open_vector(path)
    lyr = ds.GetLayerByIndex(0)
    out = [f.GetGeometryRef().Clone() for f in lyr if f.GetGeometryRef() is not None]
    if not out:
        raise ValueError("{}: no polygons".format(path))
    return out


# ------------------------------------------------------------------------------ rasters
class FineRaster:
    """Lowest ground within a radius of a point, like anuga_model.fine_lowest_ground: cell
    centres within the radius, no-data and non-finite cells left out. Points are given in
    the structures CRS and transformed to the raster's."""

    def __init__(self, path, work_wkt, z_choice=gdal_io.Z_AUTO, label=None):
        gdal, _ogr, osr = _gdal()
        self.path = path
        self.label = label or os.path.basename(path)
        self.ds = GS.open_raster(path)
        self.band = self.ds.GetRasterBand(1)
        self.nodata = self.band.GetNoDataValue()
        gt = self.ds.GetGeoTransform()
        if abs(gt[2]) > 0 or abs(gt[4]) > 0:
            raise ValueError("{}: rotated rasters are not supported".format(path))
        self.gt = gt
        self.cell = abs(gt[1])
        self.nx, self.ny = self.ds.RasterXSize, self.ds.RasterYSize
        rs = osr.SpatialReference()
        rs.ImportFromWkt(self.ds.GetProjection())
        if not rs.IsProjected():
            raise ValueError("{}: the fine DEM needs a projected CRS".format(path))
        ws = osr.SpatialReference()
        ws.ImportFromWkt(work_wkt)
        for s in (rs, ws):
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.unit = rs.GetLinearUnits()          # raster CRS units -> metres
        self.tr = None if rs.IsSame(ws) else osr.CoordinateTransformation(ws, rs)
        # 3.10.89: the unit as the run resolves it (rundam.json DEM_Z 0 = Detect: the unit the
        # raster records, never a guess from its CRS; an explicit unit its record contradicts
        # stops) - before, z_to_m_factor('auto') guessed from the CRS units
        self.z_choice, how = gdal_io.resolve_dem_z(path, z_choice)
        self.z_to_m, self.z_note = gdal_io.z_to_m_factor(path, self.z_choice)
        how, self.z_warning = gdal_io.z_how_parts(how)   # review round 3: chosen against the tag
        if how:
            self.z_note = gdal_io.detected_z_note(self.z_choice, how)

    def _xy(self, xy):
        x, y = float(xy[0]), float(xy[1])
        if self.tr is not None:
            x, y = self.tr.TransformPoint(x, y)[:2]
        return x, y

    def window(self, xy, radius_m):
        """(values in m, distances in m) of the cells whose centres are within the radius."""
        x, y = self._xy(xy)
        r = radius_m / self.unit
        gt = self.gt
        c0 = int(math.floor((x - r - gt[0]) / gt[1]))
        c1 = int(math.floor((x + r - gt[0]) / gt[1]))
        ra, rb = (y - r - gt[3]) / gt[5], (y + r - gt[3]) / gt[5]
        r0, r1 = int(math.floor(min(ra, rb))), int(math.floor(max(ra, rb)))
        c0, r0 = max(c0, 0), max(r0, 0)
        c1, r1 = min(c1, self.nx - 1), min(r1, self.ny - 1)
        if c1 < c0 or r1 < r0:
            return np.empty(0), np.empty(0)
        a = GS.checked(self.band.ReadAsArray(c0, r0, c1 - c0 + 1, r1 - r0 + 1),
                       "reading {}".format(self.path)).astype(float)
        xc = gt[0] + (np.arange(c0, c1 + 1) + 0.5) * gt[1]
        yc = gt[3] + (np.arange(r0, r1 + 1) + 0.5) * gt[5]
        X, Y = np.meshgrid(xc, yc)
        d = np.hypot(X - x, Y - y) * self.unit
        ok = (d <= radius_m) & np.isfinite(a)
        if self.nodata is not None:
            ok &= a != self.nodata
        return a[ok] * self.z_to_m, d[ok]

    def lowest(self, xy, radius_m):
        v, _d = self.window(xy, radius_m)
        return float(v.min()) if v.size else None

    def has_data(self, xy, radius_m):
        return self.window(xy, radius_m)[0].size > 0


def vrt_sources(path):
    """The rasters of a composite VRT, top priority first (a later source paints over an
    earlier one), e.g. [native 1 m, 5 m fill]."""
    root = ET.parse(path).getroot()
    out = []
    base = os.path.dirname(os.path.abspath(path))
    band = root.find("VRTRasterBand")
    if band is None:
        return out
    for src in band:
        if not src.tag.endswith("Source"):
            continue
        fn = src.find("SourceFilename")
        if fn is None or not (fn.text or "").strip():
            continue
        p = fn.text.strip()
        if fn.get("relativeToVRT") == "1" or not os.path.isabs(p):
            p = os.path.normpath(os.path.join(base, p))
        if p not in out:
            out.append(p)
    return out[::-1]


class _NoModelDem:
    """Stands in for the model DEM in anuga_culverts: the precheck never reads the 30 m
    grid; an end that falls back to it is reported as FALLBACK."""

    def sample_bilinear(self, x, y):
        return np.zeros(len(np.atleast_1d(x)))


# ------------------------------------------------------------------------------ check
def check(structures_path, fine_dem, dam="", coverage=(), layer=None, fields=None,
          z_choice=gdal_io.Z_AUTO, reconcile=True, model_cell=MODEL_CELL_M,
          tol_m=structmod.INVERT_CAP_TOL_M, bbox=None, resample=None,
          reconcile_tol_m=RECONCILE_TOL_M, mesh_polygons=None):
    """Rows (one per culvert end, CSV_COLUMNS) and a summary dict. Read-only."""
    feats, wkt, lname = read_structures(structures_path, layer, fields)
    fine = FineRaster(fine_dem, wkt, z_choice)
    cov = []
    for p in coverage or ():
        lab, _, path = p.partition("=") if "=" in p and not os.path.exists(p) else ("", "", p)
        cov.append(FineRaster(path, wkt, z_choice, label=lab or None))
    culverts = [(fid, at, s) for fid, at, s in feats if s.kind == structmod.KIND_CULVERT]
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        culverts = [c for c in culverts
                    if all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in (c[2].line[0], c[2].line[-1]))]
    outside = set()
    if mesh_polygons:
        _g, ogr, _o = _gdal()
        for fid, _a, s in culverts:
            for p in (s.line[0], s.line[-1]):
                pt = ogr.Geometry(ogr.wkbPoint)
                pt.AddPoint_2D(float(p[0]), float(p[1]))
                if not any(g.Contains(pt) for g in mesh_polygons):
                    outside.add(id(s))
    given_raw = {id(s): (s.invert_up_m, s.invert_dn_m) for _f, _a, s in culverts}
    rec_notes = {}
    if reconcile and any(s.invert_up_m is not None or s.invert_dn_m is not None
                         for _f, _a, s in culverts):
        terr = gdal_io.GdalTerrain(fine_dem, wkt, fine.z_choice, resample=resample)
        structmod.reconcile_inverts([s for _f, _a, s in culverts], terr.read, reconcile_tol_m,
                                    min(float(model_cell), 3.0))
        rec_notes = {id(s): s.invert_note for _f, _a, s in culverts}
    rows = []
    n_skip = 0
    for fid, at, s in culverts:
        specs, notes = structmod.anuga_culverts([s], _NoModelDem())
        if id(s) in outside:
            specs, notes = [], ["an end lies outside the mesh polygon; the Runner leaves it "
                                "out of ANUGA"]
        g_up, g_dn = given_raw[id(s)]
        base = dict(dam=dam, culvert_id=at.get("src_id") or s.name, fid=fid, name=s.name,
                    width_ft=round(s.width_m / FT, 2), rise_ft=round(s.height_m / FT, 2),
                    barrels=s.barrels, shape=s.shape, reconcile_note=rec_notes.get(id(s), ""))
        if not specs:
            n_skip += 1
            for i, (end, gv) in enumerate((("upstream", g_up), ("downstream", g_dn))):
                p = s.line[0] if i == 0 else s.line[-1]
                r = max(max(2.0, s.width_m), structmod.INVERT_SEARCH_MIN_M)
                gr = fine.lowest(p, r)
                rows.append(_row(base, end, p, gv, gr, r, None, "", FLAG_SKIPPED,
                                 fine, cov, tol_m, " ".join(notes)))
            continue
        spec = specs[0]
        structmod.fine_ground_inverts(specs, fine.lowest, tol_m)
        r = spec["invert_search_m"]
        for i, end in enumerate(("upstream", "downstream")):
            gv = (g_up, g_dn)[i]
            gr = spec["invert_ground_m"][i]
            src = spec["invert_source"][i]
            rinv = None if src == structmod.INV_MODEL_DEM else spec["invert_elevations"][i]
            rows.append(_row(base, end, spec["end_points"][i], gv, gr, r, rinv, src, None,
                             fine, cov, tol_m, ""))
    summ = summarize(rows, dam, structures_path, lname, fine, cov, len(feats), len(culverts),
                     n_skip, reconcile)
    return rows, summ


def _row(base, end, p, given_m, ground, r, rinv, src, flag, fine, cov, tol_m, note):
    d = dict(base)
    d.update(end=end, x=round(float(p[0]), 2), y=round(float(p[1]), 2), search_m=round(r, 2),
             given_invert_m=None if given_m is None else round(given_m, 3),
             given_invert_ft=None if given_m is None else round(given_m / FT, 2),
             fine_ground_m=None if ground is None else round(ground, 3),
             runner_invert_m=None if rinv is None else round(rinv, 3),
             runner_source=src or "", note=note)
    d["fallback"] = "yes" if ground is None else "no"
    d["delta_m"] = (round(given_m - ground, 3) if given_m is not None and ground is not None
                    else None)
    lab, cell = "", None
    if ground is not None:
        lab, cell = fine.label, fine.cell * fine.unit
        for c in cov:                        # which composite source carries this end
            if c.has_data(p, r):
                lab, cell = c.label, c.cell * c.unit
                break
    d["fine_source"], d["fine_cell_m"] = lab, None if cell is None else round(cell, 3)
    d["on_top_source"] = ("" if not cov or ground is None else
                          "yes" if lab == cov[0].label else "no")
    if flag is None:
        flag = ""
        if ground is None:
            flag = FLAG_FALLBACK
            d["note"] = ("no fine-grid ground within {:.0f} m: the Runner uses the model DEM "
                         "(30 m) value here".format(r))
        elif given_m is not None and given_m > ground + tol_m:
            flag = FLAG_HIGH
    elif flag == FLAG_SKIPPED and not d["note"]:
        d["note"] = "left out of ANUGA by the Runner"
    d["flag"] = flag
    sug = None
    if flag == FLAG_HIGH or (flag == "" and given_m is None and ground is not None):
        sug = ground
    d["suggested_invert_m"] = None if sug is None else round(sug, 3)
    d["suggested_invert_ft"] = None if sug is None else round(sug / FT, 2)
    return d


def summarize(rows, dam, spath, lname, fine, cov, n_feat, n_culv, n_skip, reconcile):
    flagged = [r for r in rows if r["flag"] in (FLAG_HIGH, FLAG_FALLBACK)]
    deltas = [r["delta_m"] for r in rows if r["delta_m"] is not None]
    src_counts = {}
    for r in rows:
        if r["fine_source"]:
            src_counts[r["fine_source"]] = src_counts.get(r["fine_source"], 0) + 1
    return {
        "dam": dam, "structures": os.path.abspath(spath), "layer": lname,
        "structures_sha256": sha256(spath),
        "fine_dem": os.path.abspath(fine.path), "fine_cell_m": round(fine.cell * fine.unit, 3),
        "fine_z": fine.z_note, "fine_z_warning": fine.z_warning or None,
        "coverage": [c.path for c in cov],
        "features": n_feat, "culverts": n_culv, "culverts_skipped_by_runner": n_skip,
        "ends": len(rows),
        "ends_given": sum(r["given_invert_m"] is not None for r in rows),
        "ends_high": sum(r["flag"] == FLAG_HIGH for r in rows),
        "culverts_high": len({r["fid"] for r in rows if r["flag"] == FLAG_HIGH}),
        "ends_fallback": sum(r["flag"] == FLAG_FALLBACK for r in rows),
        "culverts_fallback": len({r["fid"] for r in rows if r["flag"] == FLAG_FALLBACK}),
        "culverts_flagged": len({r["fid"] for r in flagged}),
        "delta_given_median_m": round(float(np.median(deltas)), 3) if deltas else None,
        "delta_given_max_m": round(float(np.max(deltas)), 3) if deltas else None,
        "ends_by_fine_source": src_counts,
        "top_source": cov[0].label if cov else "",
        "ends_not_on_top_source": sum(r.get("on_top_source") == "no" for r in rows),
        "culverts_not_on_top_source": len({r["fid"] for r in rows
                                           if r.get("on_top_source") == "no"}), "reconcile": bool(reconcile),
        "tol_m": structmod.INVERT_CAP_TOL_M, "search_min_m": structmod.INVERT_SEARCH_MIN_M,
    }


# ------------------------------------------------------------------------------ output
def _fmt(v, nd=2):
    if v is None or v == "":
        return "–"
    return "{:.{}f}".format(v, nd) if isinstance(v, float) else str(v)


def markdown(rows, s):
    L = ["# Culvert pre-check: {}".format(s["dam"] or os.path.basename(s["structures"])), "",
         "- Structures: `{}` (layer `{}`, sha256 `{}`)".format(s["structures"], s["layer"],
                                                             s["structures_sha256"]),
         "- Fine ground: `{}` (cell {} m, z {})".format(s["fine_dem"], s["fine_cell_m"], s["fine_z"])]
    if s.get("fine_z_warning"):
        L.append("- WARNING, elevation unit: " + s["fine_z_warning"])
    if s["coverage"]:
        L.append("- Source labels from: " + ", ".join("`{}`".format(c) for c in s["coverage"]))
    L += ["- Rule (Runner 3.10.71): lowest fine-grid ground within max(apron, {:.0f} m) of each "
          "end; a given invert more than {:.1f} m above it is capped; no fine data there means "
          "the 30 m model DEM.{}".format(s["search_min_m"], s["tol_m"],
                                     " reconcile_inverts (3 ft, 3 m cells) applied first, as in a run."
                                     if s["reconcile"] else ""), "",
          "| | count |", "|---|---|",
          "| features in layer | {} |".format(s["features"]),
          "| culverts | {} |".format(s["culverts"]),
          "| culverts the Runner skips (too short) | {} |".format(s["culverts_skipped_by_runner"]),
          "| ends with a given invert | {} |".format(s["ends_given"]),
          "| **HIGH** ends (given > ground + {:.1f} m) / culverts | {} / {} |".format(
              s["tol_m"], s["ends_high"], s["culverts_high"]),
          "| **FALLBACK** ends (no fine grid) / culverts | {} / {} |".format(
              s["ends_fallback"], s["culverts_fallback"]),
          "| given minus ground, median / max (m) | {} / {} |".format(
              _fmt(s["delta_given_median_m"]), _fmt(s["delta_given_max_m"])),
          "| ends not on the top source ({}) / culverts | {} / {} |".format(
              s["top_source"] or "–", s["ends_not_on_top_source"],
              s["culverts_not_on_top_source"]),
          "| ends by fine source | {} |".format(
              ", ".join("{} {}".format(k, v) for k, v in s["ends_by_fine_source"].items()) or "–"),
          ""]
    fl = [r for r in rows if r["flag"] in (FLAG_HIGH, FLAG_FALLBACK)]
    if fl:
        L += ["## Flagged ends", "",
              "| culvert | name | end | given (ft / m) | fine ground (m) | delta (m) | flag | "
              "Runner uses (m, source) | suggested (m / ft) | fine source |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for r in fl:
            L.append("| {} | {} | {} | {} / {} | {} | {} | {} | {} {} | {} / {} | {} |".format(
                r["culvert_id"], str(r["name"]).replace("|", "/"), r["end"],
                _fmt(r["given_invert_ft"]), _fmt(r["given_invert_m"]), _fmt(r["fine_ground_m"]),
                _fmt(r["delta_m"]), r["flag"], _fmt(r["runner_invert_m"]), r["runner_source"],
                _fmt(r["suggested_invert_m"]), _fmt(r["suggested_invert_ft"]), r["fine_source"] or "–"))
        L.append("")
    else:
        L += ["No flagged ends.", ""]
    L += ["Suggested invert = the lowest fine-grid ground within the search radius (not surveyed); "
          "the Runner itself would use ground + {:.1f} m for a HIGH end.".format(s["tol_m"]), ""]
    return "\n".join(L)


def write(rows, summ, out_dir, stem):
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    p = os.path.join(out_dir, stem + ".csv")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in CSV_COLUMNS})
    paths["csv"] = p
    p = os.path.join(out_dir, stem + ".md")
    with open(p, "w") as f:
        f.write(markdown(rows, summ))
    paths["md"] = p
    p = os.path.join(out_dir, stem + ".json")
    with open(p, "w") as f:
        json.dump(summ, f, indent=1)
    paths["json"] = p
    return paths


# ------------------------------------------------------------------------------ dam folder
ZUNITS = gdal_io.Z_CHOICES      # the DEM_Z enum (3.10.89: with the '... tag is wrong' choices)


def find_dam_inputs(folder):
    """Inputs of a dam folder as a dict: structures, layer, fine_dem, coverage, dam, z_choice,
    model_cell, resample, reconcile_tol_m. A rundam.json ({"inputs": {...}}, the Runner's
    saved parameters) is used when its paths exist; otherwise structures*.gpkg and
    *composite*.vrt (else *_1m*.vrt). A composite VRT's own sources label each end."""
    folder = os.path.abspath(folder)
    out = dict(dam=os.path.basename(folder.rstrip("/")), structures=None, layer=None,
               fine_dem=None, coverage=[], z_choice=gdal_io.Z_AUTO, model_cell=MODEL_CELL_M,
               resample=None, reconcile_tol_m=RECONCILE_TOL_M, rundam=None)
    inp = {}
    rj = os.path.join(folder, "rundam.json")
    if os.path.isfile(rj):
        try:
            with open(rj) as f:
                cfg = json.load(f)
            inp = cfg.get("inputs", cfg) if isinstance(cfg, dict) else {}
            out["rundam"] = rj
        except (OSError, ValueError):
            inp = {}
    out["dam"] = str(inp.get("DAM_ID") or out["dam"])
    st = inp.get("STRUCTURES")
    if isinstance(st, str) and st:
        path, _, opt = st.partition("|")
        if os.path.isfile(path):
            out["structures"] = path
            if opt.startswith("layername="):
                out["layer"] = opt.split("=", 1)[1]
    if not out["structures"]:
        c = sorted(glob.glob(os.path.join(folder, "structures*.gpkg")))
        out["structures"] = c[0] if c else None
    dem = inp.get("DEM")
    if not (isinstance(dem, str) and os.path.isfile(dem)):
        c = (sorted(glob.glob(os.path.join(folder, "*composite*.vrt")))
             or sorted(glob.glob(os.path.join(folder, "*_1m*.vrt"))))
        dem = c[0] if c else None
    out["fine_dem"] = dem
    z = inp.get("DEM_Z")
    if isinstance(z, int) and 0 <= z < len(ZUNITS):
        out["z_choice"] = ZUNITS[z]
    elif isinstance(z, str) and z in ZUNITS:
        out["z_choice"] = z
    out["model_cell"] = _num(inp.get("CELL"), MODEL_CELL_M)
    if isinstance(inp.get("DEM_RESAMPLE"), str) and inp["DEM_RESAMPLE"].strip():
        out["resample"] = inp["DEM_RESAMPLE"].strip()
    tol = _num(inp.get("S_INVERT_TOL_FT"))
    if tol is not None:
        out["reconcile_tol_m"] = tol * FT
    if dem and dem.lower().endswith(".vrt") and "composite" in os.path.basename(dem).lower():
        try:
            out["coverage"] = [p for p in vrt_sources(dem) if os.path.exists(p)]
        except ET.ParseError:
            out["coverage"] = []
    return out


# ------------------------------------------------------------------------------ CLI
USAGE = """culvert-precheck: before a dam's run, list culvert ends the Runner will cap (given
invert > fine ground + 0.3 m) or put on the 30 m DEM (no fine grid). Read-only.

  ndbreach_cli.py culvert-precheck --dam-folder DIR [DIR ...] --out OUT
  ndbreach_cli.py culvert-precheck --structures S.gpkg --fine-dem DEM [--coverage [LABEL=]R ...]
                                   [--dam ID] --out OUT
"""


def _parser():
    import argparse
    ap = argparse.ArgumentParser(prog="ndbreach_cli.py culvert-precheck", description=USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dam-folder", nargs="+", default=[], help="dam folder(s): rundam.json "
                    "and/or structures*.gpkg + *composite*.vrt")
    ap.add_argument("--structures", help="structures layer (GeoPackage or any OGR line layer)")
    ap.add_argument("--layer", help="layer name (default: runner_culverts, else the first "
                    "line layer)")
    ap.add_argument("--fine-dem", help="the fine terrain ANUGA uses (the run's DEM; e.g. the "
                    "composite VRT)")
    ap.add_argument("--coverage", nargs="*", default=[], help="rasters that label which "
                    "source carries each end, top priority first ([LABEL=]PATH)")
    ap.add_argument("--dam", default="", help="dam id for the report")
    ap.add_argument("--dem-z", choices=ZUNITS, default=None, help="DEM elevation units "
                    "(default: rundam.json DEM_Z, else auto)")
    ap.add_argument("--model-cell", type=float, default=None, help="model cell (m) for "
                    "reconcile_inverts' 3 m cap (default 30)")
    ap.add_argument("--no-reconcile", action="store_true", help="skip the reconcile_inverts "
                    "step the run applies to given inverts first")
    ap.add_argument("--bbox", type=float, nargs=4, metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
                    help="only culverts with both ends inside (structures CRS)")
    ap.add_argument("--mesh-polygon", help="polygon layer of the ANUGA mesh area: culverts "
                    "with an end outside are listed as SKIPPED, as in the run")
    ap.add_argument("--out", required=True, help="output folder (never the dam folder)")
    return ap


def _stem(dam):
    return "culvert_precheck_" + "".join(c if c.isalnum() or c in "-_." else "_" for c in dam)


def main(argv=None):
    import sys
    args = _parser().parse_args(argv)
    jobs = []
    for d in args.dam_folder:
        jobs.append(find_dam_inputs(d))
    if args.structures:
        jobs.append(dict(dam=args.dam or os.path.splitext(os.path.basename(args.structures))[0],
                         structures=args.structures, layer=args.layer, fine_dem=args.fine_dem,
                         coverage=list(args.coverage), z_choice=gdal_io.Z_AUTO,
                         model_cell=MODEL_CELL_M, resample=None,
                         reconcile_tol_m=RECONCILE_TOL_M))
    if not jobs:
        sys.stderr.write("culvert-precheck: give --dam-folder or --structures\n")
        return 2
    out = os.path.abspath(args.out)
    for j in jobs:
        for k in ("structures", "fine_dem"):
            p = j.get(k)
            if p and os.path.abspath(os.path.dirname(p)) == out:
                sys.stderr.write("culvert-precheck: --out must not be the input folder\n")
                return 2
    meshes = read_polygons(args.mesh_polygon) if args.mesh_polygon else None
    status, index = 0, []
    for j in jobs:
        if args.layer and not j.get("layer"):
            j["layer"] = args.layer
        if args.dem_z:
            j["z_choice"] = args.dem_z
        if args.model_cell:
            j["model_cell"] = args.model_cell
        if args.coverage and not j.get("coverage"):
            j["coverage"] = list(args.coverage)
        dam = j["dam"]
        if not j.get("structures"):
            index.append(dict(dam=dam, status="no structures layer"))
            print("{}: no structures layer; nothing to check".format(dam))
            continue
        if not j.get("fine_dem"):
            index.append(dict(dam=dam, status="no fine DEM"))
            print("{}: no fine DEM found; every end would fall back".format(dam))
            status = max(status, 1)
            continue
        try:
            rows, summ = check(j["structures"], j["fine_dem"], dam=dam,
                               coverage=j.get("coverage") or (), layer=j.get("layer"),
                               z_choice=j["z_choice"], reconcile=not args.no_reconcile,
                               model_cell=j["model_cell"], bbox=args.bbox,
                               resample=j.get("resample"),
                               reconcile_tol_m=j.get("reconcile_tol_m", RECONCILE_TOL_M),
                               mesh_polygons=meshes)
        except (ValueError, RuntimeError, OSError) as e:
            index.append(dict(dam=dam, status="error: {}".format(e)))
            sys.stderr.write("{}: {}\n".format(dam, e))
            status = 2
            continue
        paths = write(rows, summ, out, _stem(dam))
        index.append(dict(dam=dam, status="checked", **{k: summ[k] for k in (
            "culverts", "ends_high", "culverts_high", "ends_fallback", "culverts_fallback",
            "culverts_skipped_by_runner")}, report=paths["md"]))
        print("{}: {} culverts; HIGH {} ends ({} culverts); FALLBACK {} ends ({} culverts); "
              "skipped {} -> {}".format(dam, summ["culverts"], summ["ends_high"],
                                        summ["culverts_high"], summ["ends_fallback"],
                                        summ["culverts_fallback"],
                                        summ["culverts_skipped_by_runner"], paths["md"]))
    if len(jobs) > 1:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "culvert_precheck_index.json"), "w") as f:
            json.dump(index, f, indent=1)
    return status
