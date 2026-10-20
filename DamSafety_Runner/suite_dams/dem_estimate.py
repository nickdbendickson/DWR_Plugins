# -*- coding: utf-8 -*-
"""Screening estimate of a dam's height and storage from a DEM, for a record that has none.

Height  = crest elevation (along the dam line, else the high ground at the dam point) minus
          the lowest ground just downstream of the dam.
Storage = DEM volume below the crest elevation in the basin upstream of the dam: a flood fill
          from the lowest ground just upstream, kept on the upstream side of the dam and
          inside a search radius. A fill that reaches the edge of the largest radius has
          leaked into the landscape (no closed basin) and gives no storage.

Screening only: LiDAR sees the water surface, not the reservoir bottom, so the storage is
the volume above the water on the survey day (too low for a full pool), and the crest
includes a road or the tie-in ground where the line runs onto it.

Copied, per the suite convention (plugins do not import each other), from
DamSafety_Runner 3.10.17+ (#79):
  _sample, _flood_fill          core/hecras_model.py (reservoir outline / site checks)
  the reservoir mask            core/hecras_model.build_model: ground at or below the top of
                                dam, upstream half-plane, within the radius, seeded at the
                                lowest ground near the dam
  z_to_m_factor and unit names  core/gdal_io.py
No Qt here: it runs in a QgsTask and is tested without a display.
"""
import math

import numpy as np

FT = 0.3048
US_FT = 1200.0 / 3937.0
M3_PER_ACFT = 1233.48183754752
M2_PER_AC = 4046.8564224

RADII_M = (1500.0, 3000.0, 6000.0, 12000.0)   # tried in turn until the basin closes
MAX_CELLS = 1600                      # per side of the window read (decimated above it)
LABEL = "estimated from DEM (screening only)"

_FEET_NAMES = ("ft", "foot", "feet", "international foot", "intl foot")
_US_FEET_NAMES = ("us survey foot", "us-ft", "ftus", "us_ft", "foot_us", "us foot",
                  "us survey feet", "ft (us)", "survey foot")
_METER_NAMES = ("m", "metre", "meter", "metres", "meters")


class EstimateError(ValueError):
    pass


def z_to_m_factor(ds, choice=""):
    """Metres per DEM elevation unit and how it was decided. The raster's own unit wins
    (band unit type, then a compound CRS's vertical unit); else the page's choice
    ('feet' / 'us_feet' / 'meters'); else metres. (gdal_io.z_to_m_factor, Runner.)"""
    unit = (ds.GetRasterBand(1).GetUnitType() or "").strip().lower()
    if unit in _US_FEET_NAMES:
        return US_FT, "US survey feet (raster unit '{}')".format(unit)
    if unit in _FEET_NAMES:
        return FT, "feet (raster unit '{}')".format(unit)
    if unit in _METER_NAMES:
        return 1.0, "metres (raster unit '{}')".format(unit)
    from osgeo import osr
    wkt = ds.GetProjection()
    if wkt:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        try:
            vert = srs.GetTargetLinearUnits("VERT_CS") if srs.IsCompound() else None
        except Exception:
            vert = None
        if vert and abs(vert - US_FT) < 1e-7:
            return US_FT, "US survey feet (vertical CRS)"
        if vert and abs(vert - FT) < 1e-7:
            return FT, "feet (vertical CRS)"
        if vert and abs(vert - 1.0) < 1e-9:
            return 1.0, "metres (vertical CRS)"
    choice = (choice or "").strip().lower()
    if choice in ("us_feet", "us-ft"):
        return US_FT, "US survey feet (chosen on the DEM page)"
    if choice in ("feet", "ft"):
        return FT, "feet (chosen on the DEM page)"
    if choice in ("meters", "metres", "m"):
        return 1.0, "metres (chosen on the DEM page)"
    return 1.0, "metres (no unit recorded in the raster; check)"


def _sample(a, x0, y0, cell, xy):
    """Bilinear sample of a north-up array at points (NaN outside). (Runner hecras_model)"""
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
    bad = ~np.isfinite(v)
    if bad.any():
        rr = np.clip(np.round(fy).astype(int), 0, ny - 1)
        cc = np.clip(np.round(fx).astype(int), 0, nx - 1)
        v[bad] = a[rr[bad], cc[bad]]
    out = (fx < -0.5) | (fy < -0.5) | (fx > nx - 0.5) | (fy > ny - 0.5)
    v[out] = np.nan
    return v


def _flood_fill(mask, seed_rc):
    """Cells of `mask` 4-connected to the seed. scipy when QGIS has it (fast); else the
    Runner's numpy-only fill."""
    r, c = seed_rc
    if not mask[r, c]:
        return np.zeros_like(mask, dtype=bool)
    try:
        from scipy import ndimage
        lab, _n = ndimage.label(mask)
        return lab == lab[r, c]
    except ImportError:
        pass
    reach = np.zeros_like(mask, dtype=bool)
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


def _to_dem(ds, lonlat):
    """[(x, y)] in the DEM's CRS for [(lon, lat)] in EPSG:4326."""
    from osgeo import osr
    dst = osr.SpatialReference()
    dst.ImportFromWkt(ds.GetProjection())
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    for s in (src, dst):
        try:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except AttributeError:
            pass
    tr = osr.CoordinateTransformation(src, dst)
    return [tuple(tr.TransformPoint(float(x), float(y))[:2]) for x, y in lonlat]


def read_window(ds, cx, cy, half, factor):
    """(z metres, x0, y0, cell): the DEM around (cx, cy) +- half, decimated (averaged) to at
    most MAX_CELLS a side. NaN for nodata and outside the DEM."""
    from osgeo import gdal
    gt = ds.GetGeoTransform()
    if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12 or gt[5] >= 0:
        raise EstimateError("The DEM is rotated or not north-up.")
    cell0 = abs(gt[1])
    c0 = int(math.floor((cx - half - gt[0]) / gt[1]))
    r0 = int(math.floor((gt[3] - (cy + half)) / -gt[5]))
    n = int(math.ceil(2 * half / cell0)) + 1
    xs, ys = ds.RasterXSize, ds.RasterYSize
    a0, b0 = max(c0, 0), max(r0, 0)
    a1, b1 = min(c0 + n, xs), min(r0 + n, ys)
    if a1 <= a0 or b1 <= b0:
        raise EstimateError("The dam is outside the DEM.")
    w, h = a1 - a0, b1 - b0
    k = max(1, int(math.ceil(max(w, h) / float(MAX_CELLS))))
    bw, bh = max(1, w // k), max(1, h // k)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray(a0, b0, w, h, buf_xsize=bw, buf_ysize=bh,
                           resample_alg=gdal.GRIORA_Average if k > 1 else gdal.GRIORA_NearestNeighbour)
    if arr is None:
        raise EstimateError("The DEM could not be read.")
    z = arr.astype(float)
    nd = band.GetNoDataValue()
    if nd is not None:
        z[np.isclose(z, nd)] = np.nan
    z[(z < -1e4) | (z > 1e5)] = np.nan
    z *= factor
    cell = cell0 * w / float(bw)
    return z, gt[0] + a0 * cell0, gt[3] - b0 * cell0, cell


def estimate(dem_path, point_lonlat, line_lonlat=None, z_choice="", radii=RADII_M,
             progress=None):
    """{'height_ft', 'storage_acft', 'crest_ft', 'toe_ft', 'area_ac', 'radius_m', 'closed',
    'notes': [...], 'how': '...'}; storage_acft is None when the basin does not close.
    point_lonlat (lon, lat); line_lonlat [(lon, lat), ...] or None. Raises EstimateError."""
    from osgeo import gdal
    prog = progress or (lambda pct, text="": None)
    ds = gdal.Open(str(dem_path).split("|", 1)[0])
    if ds is None:
        raise EstimateError("The DEM could not be opened: {}".format(dem_path))
    if not ds.GetProjection():
        raise EstimateError("The DEM has no coordinate system.")
    factor, unit_note = z_to_m_factor(ds, z_choice)
    notes = ["DEM elevations: {}.".format(unit_note)]
    line = None
    if line_lonlat and len(line_lonlat) >= 2:
        line = np.array(_to_dem(ds, line_lonlat), float)
    P = (np.array(_to_dem(ds, [point_lonlat])[0], float) if point_lonlat is not None
         else None)
    if line is not None:
        seg = np.hypot(*np.diff(line, axis=0).T)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        L = float(s[-1])
        if L <= 0:
            line = None
        else:
            P = np.array([np.interp(L / 2, s, line[:, 0]), np.interp(L / 2, s, line[:, 1])])
    if P is None:
        raise EstimateError("The dam has no point and no dam line.")
    prog(10, "reading the DEM")
    radii = sorted(radii)

    def window(R):
        z, x0, y0, cell = read_window(ds, P[0], P[1], R + 300.0, factor)
        ny, nx = z.shape
        GX, GY = np.meshgrid(x0 + (np.arange(nx) + 0.5) * cell,
                             y0 - (np.arange(ny) + 0.5) * cell)
        return z, x0, y0, cell, GX, GY, np.hypot(GX - P[0], GY - P[1])
    z, x0, y0, cell, GX, GY, dist = window(min(radii[0], 1000.0))
    if not np.isfinite(_sample(z, x0, y0, cell, [P])[0]):
        raise EstimateError("The DEM has no ground at the dam.")
    prog(30, "crest and toe")
    # --- crest, and the downstream direction -------------------------------------------
    if line is not None:
        st = np.arange(0.0, L + 1e-9, max(cell / 2.0, 0.5))
        pts = np.c_[np.interp(st, s, line[:, 0]), np.interp(st, s, line[:, 1])]
        zl = _sample(z, x0, y0, cell, pts)
        if not np.isfinite(zl).any():
            raise EstimateError("The DEM has no ground along the dam line.")
        crest = float(np.nanmedian(zl))
        A, B = line[0], line[-1]
        ab = (B - A) / max(np.hypot(*(B - A)), 1e-9)
        nrm = np.array([-ab[1], ab[0]])
        along = (GX - A[0]) * ab[0] + (GY - A[1]) * ab[1]
        off = (GX - P[0]) * nrm[0] + (GY - P[1]) * nrm[1]
        span = (along >= -cell) & (along <= np.hypot(*(B - A)) + cell)
        band = lambda sgn: span & (sgn * off > 2 * cell) & (sgn * off <= max(150.0, 6 * cell))
        lows = [np.nanmin(np.where(band(sg), z, np.nan)) if band(sg).any() else np.nan
                for sg in (1.0, -1.0)]
        if not np.isfinite(lows).any():
            raise EstimateError("The DEM has no ground beside the dam line.")
        sgn = 1.0 if np.nan_to_num(lows[0], nan=np.inf) <= np.nan_to_num(lows[1], nan=np.inf) \
            else -1.0
        d = nrm * sgn                       # downstream
        toe = float(np.nanmin([lows[0] if sgn > 0 else lows[1]]))
        dam_len = float(np.hypot(*(B - A)))
        how = "crest = median ground along the dam line ({:.0f} m)".format(L)
    else:
        near = dist <= max(40.0, 2 * cell)
        crest = float(np.nanpercentile(z[near], 95))
        ring = (dist > max(30.0, 2 * cell)) & (dist <= max(200.0, 8 * cell)) & np.isfinite(z)
        if not ring.any():
            raise EstimateError("The DEM has no ground around the dam point.")
        toe = float(np.nanmin(np.where(ring, z, np.nan)))
        low = ring & (z <= toe + 0.3)        # downstream: towards the middle of the low ground
        v = np.array([(GX[low] - P[0]).mean(), (GY[low] - P[1]).mean()])
        if np.hypot(*v) < 1e-6:
            raise EstimateError("The downstream side cannot be told from the ground around "
                                "the dam point.")
        d = v / np.hypot(*v)
        dam_len = 0.0
        how = "no dam line: crest = high ground within {:.0f} m of the dam point".format(
            max(40.0, 2 * cell))
        notes.append("No dam line: the crest and the downstream side are guessed from the "
                     "ground around the point.")
    height_m = crest - toe
    # --- storage: flood fill upstream ------------------------------------------------------
    prog(50, "reservoir basin")
    out = {"crest_ft": crest / FT, "toe_ft": toe / FT, "height_ft": height_m / FT,
           "storage_acft": None, "area_ac": None, "radius_m": None, "closed": False,
           "cell_m": cell, "how": how, "notes": notes}
    if height_m <= 0.5:
        notes.append("The DEM shows no embankment (the crest is not above the ground "
                     "downstream): no height and no storage. A low weir or a dam under "
                     "water is not visible in the DEM.")
        out["height_ft"] = None
        prog(100, "done")
        return out
    for i, R in enumerate(radii):
        prog(50 + 40 * (i + 1) // len(radii), "basin within {:.1f} km".format(R / 1000))
        z, x0, y0, cell, GX, GY, dist = window(R)
        upstream = ((GX - P[0]) * d[0] + (GY - P[1]) * d[1]) < -cell
        below = np.isfinite(z) & (z < crest)
        seed_zone = upstream & below & (dist <= max(dam_len, 10 * cell, 100.0))
        if not seed_zone.any():
            notes.append("No ground below the crest just upstream of the dam: no storage.")
            out["cell_m"] = cell
            return out
        cand = np.argwhere(seed_zone)
        seed = tuple(cand[int(np.argmin(z[seed_zone]))])
        can = below & upstream & (dist <= R)
        reach = _flood_fill(can, seed)
        edge = reach & (dist > R - 2 * cell)
        if not edge.any():
            vol = float(np.nansum(crest - z[reach])) * cell * cell
            out.update(storage_acft=vol / M3_PER_ACFT, area_ac=reach.sum() * cell * cell /
                       M2_PER_AC, radius_m=R, closed=True, cell_m=cell)
            notes.append("Storage is the DEM volume below the crest in the basin upstream "
                         "(closed within {:.1f} km); LiDAR sees the water surface, so a full "
                         "reservoir's water below it is missing.".format(R / 1000))
            break
    else:
        notes.append("The ground below the crest upstream runs out past {:.1f} km (no closed "
                      "basin, or the crest is too high): no storage estimate.".format(
                          max(radii) / 1000))
    prog(100, "done")
    return out


def source(kind, res):
    """The height_source / storage_source text for an estimate."""
    if kind == "height":
        return "{}: crest {:.1f} ft - toe {:.1f} ft ({})".format(
            LABEL, res["crest_ft"], res["toe_ft"], res["how"])
    return "{}: volume below the crest {:.1f} ft over {:.1f} ac, basin closed within " \
           "{:.1f} km".format(LABEL, res["crest_ft"], res["area_ac"] or 0,
                              (res["radius_m"] or 0) / 1000)


def dam_line(lines_uri, structure_id):
    """[(lon, lat), ...] of the dam's line (its longest part), or None. Read with OGR so it
    runs in a task; the layer is opened read-only."""
    if not lines_uri or not structure_id:
        return None
    from osgeo import ogr, osr
    path, layer = str(lines_uri), None
    if "|layername=" in path:
        path, layer = path.split("|layername=", 1)
    ds = ogr.Open(path.split("|", 1)[0], 0)
    if ds is None:
        return None
    lyr = ds.GetLayerByName(layer) if layer else ds.GetLayer(0)
    if lyr is None:
        return None
    lyr.SetAttributeFilter("structure_id = '{}'".format(str(structure_id).replace("'", "''")))
    tr = None
    srs = lyr.GetSpatialRef()
    if srs is not None:
        wgs = osr.SpatialReference()
        wgs.ImportFromEPSG(4326)
        for s in (srs, wgs):
            try:
                s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except AttributeError:
                pass
        if not srs.IsSame(wgs):
            tr = osr.CoordinateTransformation(srs, wgs)
    best = None
    for feat in lyr:
        g = feat.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        if tr is not None:
            g.Transform(tr)
        parts = [g.GetGeometryRef(i) for i in range(g.GetGeometryCount())] \
            if g.GetGeometryCount() else [g]
        for p in parts:
            pts = [(p.GetX(i), p.GetY(i)) for i in range(p.GetPointCount())]
            if len(pts) >= 2 and (best is None or p.Length() > best[0]):
                best = (p.Length(), pts)
    return best[1] if best else None
