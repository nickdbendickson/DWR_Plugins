"""DamSafety Hazard Lite: hmax / vmax at every 911-addressed structure, road and railroad the breach
flood reaches, and the ND hazard class from ND Dam Safety Standards REG_05.2026 Section 5.2.

Only GDAL / OGR and numpy, so it runs inside QGIS and from plain Python.

Inputs
  depth, velocity   breach-run maximum rasters (ft and ft/s; metres are converted)
  address points    ND NG9-1-1 site / structure address points (one point = one habitable site)
  outlines          FEMA USA Structures footprints; the outline holding (or nearest to) the
                    address point is sampled, every cell it touches
  roads             ND NG9-1-1 road centerlines; AADT from the nearest NDDOT count station
                    on the same named road
  railroads         NARN rail lines; trains/day from FRA grade crossings of the same railroad
  crown (0.1.1)     a road / rail stretch counts as overtopped only where WSE (Max) is above
                    the 1 m DEM on the centreline (bridge / notch cuts of the run read on the
                    approaches); found next to the depth raster in a Runner result (WSE (Max).tif,
                    _breach_model/run_report.json, structures.json) or given; without them the
                    depth raster, noted. No arrival raster here: only the 0.1 ft rule.

Rules (REG_05.2026 Section 5.2, as used by the ND Dam Hazard plugin)
  High    habitable structure in the high danger zone of Fig. 5.1 (houses) / 5.2 (mobile
          homes) [5.2.1.1]; judgment zone counted as High unless switched off [5.2.1.1, 5.3]
          overtopped Interstate or road with AADT > 2,000 [5.2.1.2]
          inundated passenger line or freight line with >= 10 trains/day [5.2.1.3]
  Medium  habitable structure inundated to any degree [5.2.2]; overtopped US / State highway
          or road with AADT 500 - 2,000 [5.2.1.2]; any other inundated railroad [5.2.1.3]
  Low     none of the above
"""
import csv
import math
import os
import re

import numpy as np
from osgeo import gdal, ogr, osr

from . import tm11

gdal.UseExceptions()
ogr.UseExceptions()

VERSION = "0.1.1"
REF = "ND Dam Safety Standards Policy REG_05.2026 (effective 04/01/2026), Section 5.2"
WET_FT = 0.1               # inundated "to any degree": the suite's consequence depth
OUTLINE_SNAP_FT = 30.0     # address point outside every outline: nearest outline this close
NO_OUTLINE_RADIUS_FT = 20.0
AADT_SEARCH_FT = 5280.0    # NDDOT station on the same road, this close to the stretch
STATION_SNAP_FT = 150.0    # a station belongs to the road it sits on
RAIL_SEARCH_FT = 3 * 5280.0
AADT_HIGH, AADT_MEDIUM, TRAINS_HIGH = 2000, 500, 10
FT = 3.28084

PACK_FILES = {
    "address": "structures/NDGISHUB_address_points.gpkg",
    "outlines": "structures/USA_Structures.gpkg",
    "roads": "roads/ND_roads_NG911.gpkg",
    "counts": "roads/NDDOT_traffic_counts.gpkg",
    "rail": "railroads/NARN_rail_lines.gpkg",
    "crossings": "railroads/FRA_grade_crossings.gpkg",
}


def pack_paths(pack):
    return {k: os.path.join(pack, v) for k, v in PACK_FILES.items()}


# ----------------------------------------------------------------- rasters --
class Grid:
    """Depth and velocity maxima on one grid (velocity resampled onto depth's grid if needed)."""

    def __init__(self, depth_path, vel_path, metres=False):
        ds = gdal.Open(depth_path)
        self.gt = ds.GetGeoTransform()
        self.nx, self.ny = ds.RasterXSize, ds.RasterYSize
        self.srs = osr.SpatialReference(wkt=ds.GetProjection())
        self.srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.depth = self._read(ds)
        vs = gdal.Open(vel_path)
        if vs.GetGeoTransform() != self.gt or (vs.RasterXSize, vs.RasterYSize) != (self.nx, self.ny):
            vs = gdal.Warp("", vs, format="MEM", outputBounds=self.bounds(), width=self.nx,
                           height=self.ny, dstSRS=self.srs.ExportToWkt(), resampleAlg="near")
        self.vel = self._read(vs)
        if metres:
            self.depth *= FT
            self.vel *= FT
        self.cell = abs(self.gt[1])
        self.unit_ft = FT * self.srs.GetLinearUnits()     # raster CRS unit -> ft

    @staticmethod
    def _read(ds):
        b = ds.GetRasterBand(1)
        a = b.ReadAsArray().astype(np.float64)
        nd = b.GetNoDataValue()
        if nd is not None:
            a[a == nd] = 0.0
        a[~np.isfinite(a)] = 0.0
        return np.clip(a, 0.0, None)

    def bounds(self):
        x0, y1 = self.gt[0], self.gt[3]
        return x0, y1 + self.gt[5] * self.ny, x0 + self.gt[1] * self.nx, y1

    def wet_bounds(self):
        rows, cols = np.nonzero(self.depth >= WET_FT)
        x0, dx, _, y0, _, dy = self.gt
        return (x0 + cols.min() * dx, y0 + (rows.max() + 1) * dy,
                x0 + (cols.max() + 1) * dx, y0 + rows.min() * dy)

    def at(self, xs, ys):
        """Depth and velocity at points (nearest cell; 0 off the grid)."""
        c = np.floor((np.asarray(xs) - self.gt[0]) / self.gt[1]).astype(int)
        r = np.floor((np.asarray(ys) - self.gt[3]) / self.gt[5]).astype(int)
        ok = (c >= 0) & (c < self.nx) & (r >= 0) & (r < self.ny)
        d, v = np.zeros(len(c)), np.zeros(len(c))
        d[ok], v[ok] = self.depth[r[ok], c[ok]], self.vel[r[ok], c[ok]]
        return d, v

    def under(self, geom):
        """Depth and velocity of every cell the polygon touches."""
        x0, x1, y0, y1 = geom.GetEnvelope()
        c0 = max(int(math.floor((x0 - self.gt[0]) / self.gt[1])), 0)
        c1 = min(int(math.floor((x1 - self.gt[0]) / self.gt[1])) + 1, self.nx)
        r0 = max(int(math.floor((y1 - self.gt[3]) / self.gt[5])), 0)
        r1 = min(int(math.floor((y0 - self.gt[3]) / self.gt[5])) + 1, self.ny)
        if c1 <= c0 or r1 <= r0:
            return np.zeros(0), np.zeros(0)
        mem = gdal.GetDriverByName("MEM").Create("", c1 - c0, r1 - r0, 1, gdal.GDT_Byte)
        mem.SetGeoTransform((self.gt[0] + c0 * self.gt[1], self.gt[1], 0,
                             self.gt[3] + r0 * self.gt[5], 0, self.gt[5]))
        lyr_ds = ogr.GetDriverByName("Memory").CreateDataSource("")
        lyr = lyr_ds.CreateLayer("g", self.srs)
        f = ogr.Feature(lyr.GetLayerDefn())
        f.SetGeometry(geom)
        lyr.CreateFeature(f)
        gdal.RasterizeLayer(mem, [1], lyr, burn_values=[1], options=["ALL_TOUCHED=TRUE"])
        m = mem.GetRasterBand(1).ReadAsArray().astype(bool)
        return self.depth[r0:r1, c0:c1][m], self.vel[r0:r1, c0:c1][m]


# ------------------------------------------------------------------ vectors --
def _open(path):
    path, _, name = path.partition("|layername=")
    ds = ogr.Open(path)
    lyr = ds.GetLayerByName(name) if name else ds.GetLayer(0)
    return ds, lyr


def _transform(src, dst):
    src = src.Clone()
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(src, dst)


def read_layer(path, grid, bounds, fields, pad_ft=0.0):
    """Features of a layer inside `bounds` (grid CRS) padded by pad_ft, in the grid CRS."""
    ds, lyr = _open(path)
    lsrs = lyr.GetSpatialRef()
    pad = pad_ft / grid.unit_ft
    x0, y0, x1, y1 = bounds[0] - pad, bounds[1] - pad, bounds[2] + pad, bounds[3] + pad
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)):
        ring.AddPoint_2D(x, y)
    box = ogr.Geometry(ogr.wkbPolygon)
    box.AddGeometry(ring)
    to_l, to_g = _transform(grid.srs, lsrs), _transform(lsrs, grid.srs)
    fbox = box.Clone()
    fbox.Transform(to_l)
    lyr.SetSpatialFilter(fbox)
    names = [lyr.GetLayerDefn().GetFieldDefn(i).GetName() for i in range(lyr.GetLayerDefn().GetFieldCount())]
    have = {n.lower(): n for n in names}
    out = []
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.Transform(to_g)
        if not g.Intersects(box):
            continue
        out.append((g, {k: f.GetField(have[k.lower()]) if k.lower() in have else None for k in fields}))
    return out


def _txt(v):
    return "" if v is None else str(v).strip()


# --------------------------------------------------------------- structures --
RESIDENTIAL_PLACE = ("RESIDENCE", "MOBILE HOME", "SINGLE FAMILY", "MULTI-FAMILY",
                     "MULTI - FAMILY", "DUPLEX", "APARTMENT")


def structures(grid, bounds, address_path, outline_path, judgment_high=True, exclude_ag=True):
    pts = read_layer(address_path, grid, bounds, ["ADDRESS", "Place_Type", "Site_NGUID", "Unit"])
    outl = read_layer(outline_path, grid, bounds, ["BUILD_ID", "PRIM_OCC", "OCC_CLS"],
                      pad_ft=OUTLINE_SNAP_FT)
    # spatial lookup: outlines by grid tile
    tile = 200.0 / grid.unit_ft
    index = {}
    for i, (g, _) in enumerate(outl):
        x0, x1, y0, y1 = g.GetEnvelope()
        for tx in range(int(x0 // tile), int(x1 // tile) + 1):
            for ty in range(int(y0 // tile), int(y1 // tile) + 1):
                index.setdefault((tx, ty), []).append(i)
    snap = OUTLINE_SNAP_FT / grid.unit_ft
    sites = {}      # key -> dict
    for g, a in pts:
        if _txt(a["Place_Type"]).upper() == "LOT":
            continue                                    # vacant lot, not a structure
        x, y = g.GetX(), g.GetY()
        best, bd = None, snap
        for tx in range(int((x - snap) // tile), int((x + snap) // tile) + 1):
            for ty in range(int((y - snap) // tile), int((y + snap) // tile) + 1):
                for i in index.get((tx, ty), ()):
                    d = outl[i][0].Distance(g)
                    if d < bd or (d == 0 and best is None):
                        best, bd = i, d
        key = ("o", best) if best is not None else ("p", a["Site_NGUID"] or id(g))
        s = sites.get(key)
        if s is None:
            if best is not None:
                geom, occ = outl[best][0], _txt(outl[best][1]["PRIM_OCC"])
                occ_cls = _txt(outl[best][1]["OCC_CLS"])
                how = "outline (point inside)" if bd * grid.unit_ft < 1 else "outline (nearest, {:.0f} ft)".format(bd * grid.unit_ft)
            else:
                geom, occ, occ_cls = g.Buffer(NO_OUTLINE_RADIUS_FT / grid.unit_ft, 8), "", ""
                how = "no outline: {:.0f} ft circle".format(NO_OUTLINE_RADIUS_FT)
            s = sites[key] = {"geom": geom, "addresses": [], "occ": occ, "occ_cls": occ_cls,
                              "how": how, "build_id": outl[best][1]["BUILD_ID"] if best is not None else None,
                              "place": set()}
        s["addresses"].append(_txt(a["ADDRESS"]) + (" #" + _txt(a["Unit"]) if _txt(a["Unit"]) else ""))
        s["place"].add(_txt(a["Place_Type"]).upper())
    out = []
    excluded_ag = 0
    for s in sites.values():
        d, v = grid.under(s["geom"])
        if not len(d) or d.max() < WET_FT:
            continue
        kind = "mobile" if ("MANUFACTURED" in s["occ"].upper() or "MOBILE HOME" in s["place"]) else "house"
        # FEMA Agriculture outlines (barns, sheds, bins) are out-buildings under 5.2.1.1 unless the
        # 911 point or the occupancy marks a dwelling. Excluded by default (exclude_ag).
        is_ag = "AGRICULTURE" in (s["occ"].upper() + " " + s["occ_cls"].upper())
        residential = kind == "mobile" or any(p in RESIDENTIAL_PLACE for p in s["place"])
        if exclude_ag and is_ag and not residential:
            excluded_ag += 1
            continue
        z = tm11.zones(kind, d, v)
        k = int(np.lexsort((d, z))[-1])                 # governing cell: worst zone, then deepest
        zone = int(z[k])
        if zone == tm11.HIGH or (zone == tm11.JUDGMENT and judgment_high):
            nd = "High"
        else:
            nd = "Medium"
        c = s["geom"].Centroid()
        out.append({"geom": s["geom"], "x": c.GetX(), "y": c.GetY(),
                    "address": "; ".join(sorted(set(s["addresses"])))[:250],
                    "units": len(s["addresses"]), "build_id": s["build_id"], "occupancy": s["occ"],
                    "place_type": ", ".join(sorted(p for p in s["place"] if p)), "matched": s["how"], "chart": "Fig. 5.2 mobile home" if kind == "mobile" else "Fig. 5.1 house",
                    "cells": len(d), "hmax_ft": round(float(d.max()), 2), "vmax_fps": round(float(v.max()), 2),
                    "gov_depth_ft": round(float(d[k]), 2), "gov_vel_fps": round(float(v[k]), 2),
                    "zone": tm11.ZONE_NAMES[zone], "nd_class": nd})
    out.sort(key=lambda r: (-{"High": 2, "Judgment": 1, "Low": 0}[r["zone"]], -r["hmax_ft"]))
    return out, excluded_ag


# --------------------------------------------------- centreline crown (0.1.1) --
CROWN_STEP_M = 1.0        # centreline read at 1 m steps
CROWN_EXTEND_M = 60.0     # and this far past each end of a wet stretch (the cut approaches)
CUT_BUFFER_M = 5.0
CUT_KINDS = ("bridge", "notch")   # culverts are buried: the DEM is the road over them
BASIS_CROWN = "WSE - 1 m centreline crown"
BASIS_GRID = "depth raster (no crown check)"


class _Points:
    """A raster read at points of another CRS (nearest cell, small windows, nodata -> NaN)."""

    def __init__(self, path, srs, scale=1.0):
        self.ds = gdal.Open(path)
        self.gt = self.ds.GetGeoTransform()
        self.band = self.ds.GetRasterBand(1)
        self.nd = self.band.GetNoDataValue()
        self.scale = scale
        own = osr.SpatialReference(wkt=self.ds.GetProjection())
        own.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self.tf = None if own.IsSame(srs) else osr.CoordinateTransformation(srs, own)

    def __call__(self, xs, ys):
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        if self.tf is not None and len(xs):
            t = np.array(self.tf.TransformPoints(np.c_[xs, ys].tolist()))
            xs, ys = t[:, 0], t[:, 1]
        out = np.full(len(xs), np.nan)
        if not len(xs):
            return out
        c = np.floor((xs - self.gt[0]) / self.gt[1]).astype(int)
        r = np.floor((ys - self.gt[3]) / self.gt[5]).astype(int)
        ok = (c >= 0) & (c < self.ds.RasterXSize) & (r >= 0) & (r < self.ds.RasterYSize)
        if ok.any():
            c0, c1, r0, r1 = c[ok].min(), c[ok].max(), r[ok].min(), r[ok].max()
            a = self.band.ReadAsArray(int(c0), int(r0), int(c1 - c0 + 1),
                                      int(r1 - r0 + 1)).astype(float)
            v = a[r[ok] - r0, c[ok] - c0]
            if self.nd is not None:
                v[v == self.nd] = np.nan
            v[v <= -9990.0] = np.nan
            out[ok] = v * self.scale
        return out


def _json(path):
    import json
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _dem_z_to_m(rep):
    if rep.get("dem_z_to_m"):
        return float(rep["dem_z_to_m"])
    note = str(rep.get("dem_elevation_units") or "").strip().lower()
    return 1200.0 / 3937.0 if note.startswith("us survey feet") else 0.3048 \
        if note.startswith("feet") else 1.0 if note.startswith(("metres", "meters")) else None


def open_crown(grid, depth_path, wse=None, dem=None, dem_z_to_m=None, structures=None,
               metres=False):
    """(crown source dict or None, note). Unset inputs are taken from a Runner result next to
    the depth raster (WSE (Max).tif, _breach_model/run_report.json, structures.json)."""
    folder = os.path.dirname(os.path.abspath(depth_path))
    bm = os.path.join(folder, "_breach_model")
    rep = _json(os.path.join(bm, "run_report.json"))
    rep = rep if isinstance(rep, dict) else {}
    wse = wse or os.path.join(folder, "WSE (Max).tif")
    if not os.path.isfile(wse):
        return None, "no WSE (Max) raster"
    if not dem:
        dem = ((rep.get("settings") or {}).get("input_files") or {}).get("DEM")
        if dem_z_to_m is None:
            dem_z_to_m = _dem_z_to_m(rep) if rep else None
    if not dem or not os.path.exists(dem):
        return None, "no 1 m DEM ({})".format(dem or "none given or recorded")
    if dem_z_to_m is None:
        return None, "the DEM's elevation unit is not recorded"
    if not grid.srs.IsProjected():
        return None, "the depth raster CRS is not projected"
    unit_m = float(grid.srs.GetLinearUnits() or 1.0)
    if structures is None:
        structures = _json(os.path.join(bm, "structures.json")) or []
    cuts, note = [], ""
    wsrs = None
    if rep.get("work_crs"):
        wsrs = osr.SpatialReference()
        wsrs.SetFromUserInput(str(rep["work_crs"]))
        wsrs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tf = osr.CoordinateTransformation(wsrs, grid.srs) \
        if wsrs is not None and not wsrs.IsSame(grid.srs) else None
    cell = float((rep.get("settings") or {}).get("cell_size_m") or 30.0)
    for st in structures if isinstance(structures, list) else []:
        if not isinstance(st, dict) or str(st.get("kind", "")).lower() not in CUT_KINDS:
            continue
        try:
            line = np.asarray(st.get("line"), float).reshape(-1, 2)
            if tf is not None:
                line = np.array(tf.TransformPoints(line.tolist()))[:, :2]
            w = float(st.get("width_m") or 1.0) * max(1, int(st.get("barrels") or 1)) * 1.1
        except (TypeError, ValueError):
            continue
        if len(line) and np.isfinite(line).all():
            cuts.append((line, (0.5 * max(cell, w) + CUT_BUFFER_M) / unit_m))
    try:
        src = {"wse": _Points(wse, grid.srs, FT if metres else 1.0),
               "dem": _Points(dem, grid.srs, dem_z_to_m * FT), "cuts": cuts, "unit_m": unit_m}
    except Exception as ex:                                     # noqa - reported
        return None, "WSE / DEM could not be opened ({})".format(ex)
    return src, "centreline crown check: {} - 1 m DEM {}, {} bridge / notch cut(s){}".format(
        os.path.basename(wse), os.path.basename(dem), len(cuts), note)


def _seg_dist(xs, ys, line):
    best = np.full(len(xs), np.inf)
    for (x0, y0), (x1, y1) in zip(line[:-1], line[1:]) if len(line) > 1 else [(line[0], line[0])]:
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = np.clip(((xs - x0) * dx + (ys - y0) * dy) / L2, 0, 1) if L2 > 0 else 0.0
        best = np.minimum(best, np.hypot(xs - (x0 + t * dx), ys - (y0 + t * dy)))
    return best


def crown_check(src, pts, cum, s0, s1):
    """Centreline check of the wet stretch s0..s1 (along-line distance) of the line `pts`:
    {basis, overtop_ft, crown_ft, wse_ft, freeboard_ft, n_cut} at the least-freeboard point,
    or None when no wet point has both a crown and a WSE."""
    u = src["unit_m"]
    ext = CROWN_EXTEND_M / u
    s = np.arange(max(0.0, s0 - ext), min(cum[-1], s1 + ext) + 1e-9, CROWN_STEP_M / u)
    if not len(s):
        return None
    xs, ys = np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])
    z = src["dem"](xs, ys)
    cut = np.zeros(len(s), bool)
    for line, half in src["cuts"]:
        cut |= _seg_dist(xs, ys, line) <= half
    if cut.any():
        # a bridge / notch opening: read the crown on the approaches (a straight deck)
        good = ~cut & np.isfinite(z)
        z = np.where(cut, np.interp(s, s[good], z[good]) if good.any() else np.nan, z)
    wet = (s >= s0 - 1e-9) & (s <= s1 + 1e-9)
    w = src["wse"](xs[wet], ys[wet])
    fb = z[wet] - w
    ok = np.isfinite(fb)
    if not ok.any():
        return None
    k = int(np.flatnonzero(ok)[np.argmin(fb[ok])])
    return {"basis": BASIS_CROWN, "overtop_ft": round(max(0.0, -float(fb[k])), 2),
            "crown_ft": round(float(z[wet][k]), 2), "wse_ft": round(float(w[k]), 2),
            "freeboard_ft": round(float(fb[k]), 2), "n_cut": int((cut & wet).sum())}


# ------------------------------------------------------------- lines -------
def _parts(g):
    if g.GetGeometryCount() and ogr.GT_Flatten(g.GetGeometryType()) in (ogr.wkbMultiLineString,):
        return [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())]
    return [g]


def wet_stretches(grid, g, crown=None, crown_note="", checked=None):
    """Contiguous runs of a line with depth >= WET_FT: length, hmax, vmax and the hmax spot.
    0.1.1: with a crown source (open_crown), a run counts only where the WSE is above the
    centreline crown (hmax_ft = that overtopping; grid_hmax_ft the raster's); every wet run
    goes to `checked` with its decision."""
    step = grid.cell / 2.0
    res = []
    for part in _parts(g):
        pts = np.array(part.GetPoints())[:, :2] if part.GetPointCount() > 1 else None
        if pts is None:
            continue
        seg = np.hypot(*np.diff(pts, axis=0).T)
        cum = np.concatenate([[0], np.cumsum(seg)])
        if cum[-1] <= 0:
            continue
        s = np.arange(0, cum[-1] + step, step).clip(max=cum[-1])
        xs, ys = np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])
        d, v = grid.at(xs, ys)
        wet = d >= WET_FT
        if not wet.any():
            continue
        edges = np.flatnonzero(np.diff(np.concatenate([[0], wet.astype(int), [0]])))
        for a, b in zip(edges[::2], edges[1::2]):
            k = a + int(np.argmax(d[a:b]))
            st = {"x": float(xs[k]), "y": float(ys[k]),
                  "length_ft": round(max(b - a, 1) * step * grid.unit_ft),
                  "hmax_ft": round(float(d[a:b].max()), 2),
                  "vmax_fps": round(float(v[a:b].max()), 2),
                  "xs": xs[a:b], "ys": ys[a:b]}
            st["grid_hmax_ft"] = st["hmax_ft"]
            st.update({"depth_basis": BASIS_GRID, "crown_ft": None, "wse_ft": None,
                       "freeboard_ft": None, "decision": "counted", "check_note": crown_note})
            if crown is not None:
                lo = 0.5 * (s[a] + s[a - 1]) if a > 0 else s[a]
                hi = 0.5 * (s[b - 1] + s[b]) if b < len(s) else s[b - 1]
                c = crown_check(crown, pts, cum, lo, hi)
                if c is None:
                    st["check_note"] = "no wet centreline point with both a 1 m crown and a WSE"
                else:
                    st.update({"depth_basis": c["basis"], "crown_ft": c["crown_ft"],
                               "wse_ft": c["wse_ft"], "freeboard_ft": c["freeboard_ft"],
                               "check_note": "bridge / notch cut read on its approaches"
                               if c["n_cut"] else ""})
                    if c["overtop_ft"] > 0:
                        st["hmax_ft"] = c["overtop_ft"]
                    else:
                        st["decision"] = "skipped: WSE not above the centreline crown"
            if checked is not None:
                checked.append(st)
            if st["decision"] == "counted":
                res.append(st)
    return res


def _near(items, x, y, maxd):
    best, bd = None, maxd
    for it in items:
        d = math.hypot(it["x"] - x, it["y"] - y)
        if d < bd:
            best, bd = it, d
    return best, bd


_INTERSTATE = re.compile(r"^(INTERSTATE|I[- ]?\d)", re.I)
_HIGHWAY = re.compile(r"(HIGHWAY|HWY|^US[- ]?\d|^ND[- ]?\d|^STATE)", re.I)


def roads(grid, bounds, roads_path, counts_path, crown=None, crown_note="", checked=None):
    pad = AADT_SEARCH_FT
    lines = read_layer(roads_path, grid, bounds, ["FULL_NAME", "RoadClass"], pad_ft=pad)
    stations = []
    for g, a in read_layer(counts_path, grid, bounds, ["station_id", "name", "aadt", "aadt_year"], pad_ft=pad):
        if a["aadt"] is None:
            continue
        # the road the station sits on
        best, bd = None, STATION_SNAP_FT / grid.unit_ft
        for lg, la in lines:
            d = lg.Distance(g)
            if d < bd:
                best, bd = la, d
        stations.append({"x": g.GetX(), "y": g.GetY(), "aadt": int(a["aadt"]), "year": a["aadt_year"],
                         "id": a["station_id"], "road": _txt(best["FULL_NAME"]).upper() if best else None})
    out = []
    for g, a in lines:
        name, rc = _txt(a["FULL_NAME"]).upper(), _txt(a["RoadClass"])
        if rc in ("Walkway", "Trail", "Parking Lot", "Alley"):
            continue
        for st in wet_stretches(grid, g, crown, crown_note, checked):
            same = [s for s in stations if name and s["road"] == name]
            stn, sd = _near(same, st["x"], st["y"], AADT_SEARCH_FT / grid.unit_ft)
            aadt = stn["aadt"] if stn else None
            if _INTERSTATE.search(name):
                cls, why = "High", "Interstate"
            elif aadt is not None and aadt > AADT_HIGH:
                cls, why = "High", "AADT {:,} > 2,000".format(aadt)
            elif _HIGHWAY.search(name):
                cls, why = "Medium", "US / State highway" + ("" if aadt is not None else "; no AADT - High if > 2,000")
            elif aadt is not None and aadt >= AADT_MEDIUM:
                cls, why = "Medium", "AADT {:,} (500 - 2,000)".format(aadt)
            elif aadt is not None:
                cls, why = "Low", "AADT {:,} < 500".format(aadt)
            else:
                cls, why = "Low", "no NDDOT count on this road - verify traffic"
            st.update({"road": name or "(unnamed)", "road_class": rc, "aadt": aadt,
                       "aadt_year": stn["year"] if stn else None, "station": stn["id"] if stn else None,
                       "station_ft": round(sd * grid.unit_ft) if stn else None,
                       "nd_class": cls, "basis": why, "verify": aadt is None})
            out.append(st)
    return out


def railroads(grid, bounds, rail_path, crossings_path, crown=None, crown_note="", checked=None):
    lines = read_layer(rail_path, grid, bounds, ["RROWNER1", "SUBDIV", "PASSNGR", "NET"])
    xings = []
    for g, a in read_layer(crossings_path, grid, bounds,
                           ["CrossingID", "RailroadCode", "TotalDaylightThruTrains",
                            "TotalNighttimeThruTrains", "TotalSwitchingTrains"], pad_ft=RAIL_SEARCH_FT):
        t = [a[k] for k in ("TotalDaylightThruTrains", "TotalNighttimeThruTrains", "TotalSwitchingTrains")]
        if all(v is None for v in t):
            continue
        xings.append({"x": g.GetX(), "y": g.GetY(), "rr": _txt(a["RailroadCode"]).upper(),
                      "trains": sum(int(v or 0) for v in t), "id": a["CrossingID"]})
    out = []
    for g, a in lines:
        if _txt(a["NET"]).upper() in ("A", "R", "T"):
            continue                                    # abandoned / removed / trail
        owner = _txt(a["RROWNER1"]).upper()
        for st in wet_stretches(grid, g, crown, crown_note, checked):
            near = [c for c in xings if c["rr"] == owner and c["trains"] > 0 and
                    math.hypot(c["x"] - st["x"], c["y"] - st["y"]) * grid.unit_ft <= RAIL_SEARCH_FT]
            trains = max((c["trains"] for c in near), default=None)
            passenger = bool(_txt(a["PASSNGR"]))
            if passenger:
                cls, why = "High", "passenger line"
            elif trains is not None and trains >= TRAINS_HIGH:
                cls, why = "High", "{} trains/day >= 10".format(trains)
            elif trains is not None:
                cls, why = "Medium", "{} trains/day".format(trains)
            else:
                cls, why = "Medium", "trains/day unknown - High if >= 10 or passenger (verify)"
            st.update({"railroad": owner, "subdivision": _txt(a["SUBDIV"]), "trains_day": trains,
                       "nd_class": cls, "basis": why, "verify": trains is None})
            out.append(st)
    return out


# ------------------------------------------------------------ classification --
def classify(structs, rds, rails, judgment_high=True):
    reasons = []
    hi = [s for s in structs if s["zone"] == "High"]
    jz = [s for s in structs if s["zone"] == "Judgment"]
    if hi:
        reasons.append(("High", "5.2.1.1", "{} habitable structure(s) in the high danger zone".format(len(hi))))
    if jz:
        reasons.append(("High" if judgment_high else "Judgment", "5.2.1.1",
                        "{} habitable structure(s) in the judgment zone{}".format(
                            len(jz), " (counted as High, 5.3 borderline rule)" if judgment_high else
                            " - engineer decides whether loss of life is probable")))
    for items, sec, what in ((rds, "5.2.1.2", "overtopped road stretch(es)"),
                             (rails, "5.2.1.3", "inundated railroad stretch(es)")):
        for cls in ("High", "Medium"):
            n = [r for r in items if r["nd_class"] == cls]
            if n:
                names = sorted({r.get("road") or "{} {}".format(r["railroad"], r["subdivision"]).strip() for r in n})
                reasons.append((cls, sec, "{} {} - {}: {}".format(len(n), what, n[0]["basis"] if len(n) == 1 else
                                                                   "see table", ", ".join(names[:6]))))
    if structs:
        reasons.append(("Medium", "5.2.2", "{} habitable structure(s) inundated (>= {} ft)".format(len(structs), WET_FT)))
    rank = {"Low": 0, "Judgment": 0, "Medium": 1, "High": 2}
    cls = max((r[0] for r in reasons), key=rank.get, default="Low")
    if rank[cls] == 0:
        cls = "Low"
    verify = []
    nr = sum(r["verify"] for r in rds)
    if nr:
        verify.append("{} overtopped road stretch(es) with no NDDOT count on the same road".format(nr))
    nl = sum(r["verify"] for r in rails)
    if nl:
        verify.append("{} rail stretch(es) with trains/day unknown".format(nl))
    if jz and not judgment_high:
        verify.append("judgment-zone structures decide High vs Medium")
    no_outline = sum(s["matched"].startswith("no outline") for s in structs)
    if no_outline:
        verify.append("{} inundated address point(s) with no outline (sampled on a {:.0f} ft circle)".format(
            no_outline, NO_OUTLINE_RADIUS_FT))
    verify.append("Not screened here (engineer review, REG_05.2026 5.2 / 5.3): sole-access roads, lifeline "
                  "facilities, imminent future development, dams in series")
    return cls, reasons, verify


# ------------------------------------------------------------------ outputs --
def _write_gpkg(path, name, srs, rows, geom_type, fields):
    drv = ogr.GetDriverByName("GPKG")
    ds = drv.Open(path, 1) if os.path.exists(path) else drv.CreateDataSource(path)
    if ds.GetLayerByName(name):
        ds.DeleteLayer(name)
    lyr = ds.CreateLayer(name, srs, geom_type)
    for f, t in fields:
        lyr.CreateField(ogr.FieldDefn(f, t))
    for r in rows:
        feat = ogr.Feature(lyr.GetLayerDefn())
        for f, _ in fields:
            if r.get(f) is not None:
                feat.SetField(f, r[f])
        if "geom" in r and geom_type == ogr.wkbMultiPolygon:
            feat.SetGeometry(ogr.ForceToMultiPolygon(r["geom"]))
        else:
            p = ogr.Geometry(ogr.wkbPoint)
            p.AddPoint_2D(r["x"], r["y"])
            feat.SetGeometry(p)
        lyr.CreateFeature(feat)
    ds = None


def _write_csv(path, rows, cols):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow(["" if r.get(c) is None else r[c] for c in cols])


S_COLS = ["address", "units", "occupancy", "place_type", "matched", "chart", "cells", "hmax_ft", "vmax_fps",
          "gov_depth_ft", "gov_vel_fps", "zone", "nd_class", "build_id"]
CHECK_COLS = ["depth_basis", "grid_hmax_ft", "crown_ft", "wse_ft", "freeboard_ft", "check_note"]
R_COLS = ["road", "road_class", "length_ft", "hmax_ft", "vmax_fps", "aadt", "aadt_year", "station",
          "station_ft", "nd_class", "basis"] + CHECK_COLS
L_COLS = ["railroad", "subdivision", "length_ft", "hmax_ft", "vmax_fps", "trains_day", "nd_class",
          "basis"] + CHECK_COLS
K_COLS = ["line", "x", "y", "length_ft", "grid_hmax_ft", "decision", "depth_basis", "crown_ft", "wse_ft",
          "freeboard_ft", "check_note"]


def run(depth, velocity, out_dir, layers, metres=False, judgment_high=True, exclude_ag=True,
        dam_name="", log=print, wse=None, dem=None, dem_z_to_m=None, cut_structures=None,
        crown_check=True):
    os.makedirs(out_dir, exist_ok=True)
    grid = Grid(depth, velocity, metres)
    bounds = grid.wet_bounds()
    log("Flooded extent {:.1f} x {:.1f} mi".format((bounds[2] - bounds[0]) * grid.unit_ft / 5280,
                                                   (bounds[3] - bounds[1]) * grid.unit_ft / 5280))
    s, excluded_ag = structures(grid, bounds, layers["address"], layers["outlines"], judgment_high,
                                exclude_ag)
    log("Structures inundated: {}{}".format(len(s), " (excluded {} FEMA agricultural out-building(s))"
                                            .format(excluded_ag) if excluded_ag else ""))
    crown, cnote = (open_crown(grid, depth, wse, dem, dem_z_to_m, cut_structures, metres)
                    if crown_check else (None, "turned off"))
    log("Roads / rail: " + (cnote if crown is not None else
                            "depth raster, no centreline crown check: " + cnote))
    if crown is None:
        cnote = "depth raster, no crown check: " + cnote
    checked_r, checked_l = [], []
    r = roads(grid, bounds, layers["roads"], layers["counts"], crown, cnote, checked_r)
    log("Road stretches overtopped: {} (of {} wet)".format(len(r), len(checked_r)))
    l = railroads(grid, bounds, layers["rail"], layers["crossings"], crown, cnote, checked_l)
    log("Rail stretches inundated: {} (of {} wet)".format(len(l), len(checked_l)))
    cls, reasons, verify = classify(s, r, l, judgment_high)
    if excluded_ag:
        verify.append("{} inundated FEMA agricultural out-building(s) were excluded (5.2.1.1 "
                      "out-buildings); confirm none is a regularly occupied shop or farmhouse".format(excluded_ag))

    gpkg = os.path.join(out_dir, "hazard_lite.gpkg")
    if os.path.exists(gpkg):
        os.remove(gpkg)
    S, R, I = ogr.OFTString, ogr.OFTReal, ogr.OFTInteger
    _write_gpkg(gpkg, "structures", grid.srs, s, ogr.wkbMultiPolygon,
                [(c, R if c.endswith(("_ft", "_fps")) else I if c in ("units", "cells", "build_id") else S)
                 for c in S_COLS])
    _write_gpkg(gpkg, "road_spots", grid.srs, r, ogr.wkbPoint,
                [(c, R if c.endswith(("_ft", "_fps")) else I if c in ("aadt", "aadt_year", "station_ft") else S)
                 for c in R_COLS])
    _write_gpkg(gpkg, "rail_spots", grid.srs, l, ogr.wkbPoint,
                [(c, R if c.endswith(("_ft", "_fps")) else I if c == "trains_day" else S) for c in L_COLS])
    _write_csv(os.path.join(out_dir, "structures.csv"), s, S_COLS)
    _write_csv(os.path.join(out_dir, "roads.csv"), r, R_COLS)
    _write_csv(os.path.join(out_dir, "railroads.csv"), l, L_COLS)
    _write_csv(os.path.join(out_dir, "line_crown_check.csv"),
               [dict(x, line="road") for x in checked_r] + [dict(x, line="rail")
                                                            for x in checked_l], K_COLS)

    md = ["# ND hazard class: **{}**".format(cls.upper()), "",
          "{}  ".format(dam_name) if dam_name else "",
          "Basis: {}. DamSafety Hazard Lite {}. Screening result; the engineer of record classifies the dam.".format(REF, VERSION),
          "", "| Class | Section | Reason |", "|---|---|---|"]
    md += ["| {} | {} | {} |".format(*x) for x in reasons] or ["| Low | 5.2 | nothing inundated |"]
    md += ["", "Roads / rail overtopping: {} (every wet stretch and why: line_crown_check.csv)."
           .format(cnote)]
    if verify:
        md += ["", "**Verify:**", ""] + ["- " + v for v in verify]
    md += ["", "## Structures (911 address points on their outline)", "",
           "| Address | Chart | hmax ft | vmax ft/s | Zone | Class |", "|---|---|---|---|---|---|"]
    md += ["| {address} | {chart} | {hmax_ft} | {vmax_fps} | {zone} | {nd_class} |".format(**x) for x in s[:60]]
    if len(s) > 60:
        md.append("| ... {} more in structures.csv | | | | | |".format(len(s) - 60))
    md += ["", "## Roads (NDDOT AADT)", "", "| Road | AADT | hmax ft | vmax ft/s | Length ft | Class |",
           "|---|---|---|---|---|---|"]
    md += ["| {} | {} | {} | {} | {} | {} |".format(x["road"], "-" if x["aadt"] is None else "{:,}".format(x["aadt"]),
                                                  x["hmax_ft"], x["vmax_fps"], x["length_ft"], x["nd_class"])
           for x in sorted(r, key=lambda x: (x["nd_class"] != "High", x["nd_class"] != "Medium", -x["hmax_ft"]))[:60]]
    md += ["", "## Railroads", "", "| Railroad | Trains/day | hmax ft | vmax ft/s | Length ft | Class |",
           "|---|---|---|---|---|---|"]
    md += ["| {} {} | {} | {} | {} | {} | {} |".format(x["railroad"], x["subdivision"],
                                                     "-" if x["trains_day"] is None else x["trains_day"],
                                                     x["hmax_ft"], x["vmax_fps"], x["length_ft"], x["nd_class"]) for x in l]
    with open(os.path.join(out_dir, "hazard_summary.md"), "w") as fh:
        fh.write("\n".join(md) + "\n")
    log("ND hazard class: {}".format(cls))
    return {"class": cls, "reasons": reasons, "verify": verify, "structures": s, "roads": r, "rail": l,
            "gpkg": gpkg, "line_checks": checked_r + checked_l, "crown_note": cnote}
