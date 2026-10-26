"""Run-report metrics (v3.10.27, issue #91): reporting only, the model never sees any of this.

The headline figures of a run report (`flooded_acres`, `max_velocity_fps`) can mislead: the
any-depth area is often mostly a thin film (or a lake surface in hydro-flattened LiDAR) and the
maximum velocity is often one shallow cell. This module adds, from the same final rasters:

- flooded area at or above 0.1, 1 and 2 ft (`flooded_acres_ge_0_1ft`, `_ge_1ft`, `_ge_2ft`)
- the 95th and 99th percentile velocity over wet cells (depth >= WET_MIN_DEPTH_FT, 0.1 ft)
- the depth and position of the maximum-velocity cell
- velocity at receptors: max and P95 over flooded structures, over overtopped road and rail
  samples (flooded = at or above the arrival depth, as in impacts.summarize)
- an optional water-body mask (existing lakes and ponds). Masked cells stay in the model and in
  the rasters; the masked figures are reported NEXT TO the unmasked ones (`*_masked`), with the
  masked area itself. Sources, all read only (nothing is written into ND_Dam_Suite_Pack):
    1. the run setting `water_mask` (a vector file path, `path|layername=x` works, several
       separated by ;;) or the NDBREACH_WATERBODY_MASK environment variable
    2. the dam folder's data section "waterbodies" layer (the Data plugin's per-dam step)
    3. hydro/3DHP_waterbodies.gpkg, another hydro/3DHP_*waterbod*.gpkg (3.10.27) or
       hydro/NHD_waterbodies.gpkg in the suite pack or the suite
       data folder, when that file exists (the pack does not hold it yet; the Data plugin has to
       add it). "off" in the setting or the variable turns the mask off.

Every function takes SI arrays (m, m/s) on a core Grid-like object (x0, y0, dx, nx, ny) whose
coordinates are in the grid's CRS. Field names are new; no existing report field changes.
"""
import glob
import os

import numpy as np

FT = 0.3048
DEPTH_THRESHOLDS_FT = (0.1, 1.0, 2.0)
WET_MIN_DEPTH_FT = 0.1
ENV_MASK = "NDBREACH_WATERBODY_MASK"
MASK_OFF = ("off", "none", "no", "0", "false")
SUITE_MASK_FILES = (os.path.join("hydro", "3DHP_waterbodies.gpkg"),
                    os.path.join("hydro", "NHD_waterbodies.gpkg"))
SUITE_MASK_GLOB = os.path.join("hydro", "3DHP_*waterbod*.gpkg")   # 3.10.27: the 3DHP file's own names
PACK_DIR = "ND_Dam_Suite_Pack"
MASK_GEOJSON = "Water Body Mask.geojson"
ACRE_M2 = 4046.8564224


# v3.10.27: the extra table / CSV columns (sensitivity, probabilistic, batch), in this order;
# appended after the existing columns so a script reading the old ones by position still works
TABLE_FIELDS = ("flooded_ac_ge_0_1ft", "flooded_ac_ge_1ft", "flooded_ac_ge_2ft",
                "velocity_p95_fps", "velocity_p99_fps", "max_velocity_depth_ft",
                "flooded_ac_masked", "flooded_ac_ge_1ft_masked")


def table_values(s):
    """{TABLE_FIELDS name: value} from a run summary (None where it has no such figure)."""
    s = s or {}
    return {"flooded_ac_ge_0_1ft": s.get(area_key(0.1)), "flooded_ac_ge_1ft": s.get(area_key(1.0)),
            "flooded_ac_ge_2ft": s.get(area_key(2.0)),
            "velocity_p95_fps": s.get("velocity_p95_fps"),
            "velocity_p99_fps": s.get("velocity_p99_fps"),
            "max_velocity_depth_ft": s.get("max_velocity_depth_ft"),
            "flooded_ac_masked": s.get("flooded_acres_masked"),
            "flooded_ac_ge_1ft_masked": s.get(area_key(1.0) + "_masked")}


def area_key(t_ft):
    """'flooded_acres_ge_0_1ft' for 0.1, 'flooded_acres_ge_1ft' for 1.0."""
    return "flooded_acres_ge_{}ft".format(("{:g}".format(t_ft)).replace(".", "_"))


def _r(v, n=2):
    return None if v is None or not np.isfinite(v) else round(float(v), n)


def depth_areas(depth_m, cell_ac, keep=None, thresholds_ft=DEPTH_THRESHOLDS_FT):
    """{area_key(t): acres at or above t ft} and 'any' (finite depth). `keep` (bool array)
    limits the count to those cells (the unmasked ones)."""
    d = np.asarray(depth_m, dtype=float)
    fin = np.isfinite(d)
    if keep is not None:
        fin = fin & keep
    out = {"flooded_acres": round(float(fin.sum()) * cell_ac, 1)}
    with np.errstate(invalid="ignore"):
        for t in thresholds_ft:
            out[area_key(t)] = round(float((fin & (d >= t * FT)).sum()) * cell_ac, 1)
    return out


def velocity_percentiles(depth_m, vel_ms, keep=None, wet_ft=WET_MIN_DEPTH_FT, q=(95, 99)):
    """{95: ft/s, 99: ft/s} over cells at least `wet_ft` deep with a velocity (None when no
    such cell), and the number of those cells."""
    d = np.asarray(depth_m, dtype=float)
    v = np.asarray(vel_ms, dtype=float)
    with np.errstate(invalid="ignore"):
        m = np.isfinite(d) & np.isfinite(v) & (d >= wet_ft * FT)
    if keep is not None:
        m = m & keep
    if not m.any():
        return {k: None for k in q}, 0
    vals = np.percentile(v[m], list(q))
    return {k: _r(x / FT) for k, x in zip(q, vals)}, int(m.sum())


def max_velocity_cell(grid, depth_m, vel_ms, keep=None):
    """(max ft/s, depth ft at that cell, [x, y] of the cell centre) or (None, None, None)."""
    v = np.asarray(vel_ms, dtype=float)
    m = np.isfinite(v)
    if keep is not None:
        m = m & keep
    if not m.any():
        return None, None, None
    vv = np.where(m, v, -np.inf)
    r, c = np.unravel_index(int(np.argmax(vv)), vv.shape)
    d = float(np.asarray(depth_m, dtype=float)[r, c])
    x = grid.x0 + (c + 0.5) * grid.dx
    y = grid.y0 - (r + 0.5) * grid.dx
    return (_r(v[r, c] / FT), (_r(d / FT) if np.isfinite(d) else None),
            [round(float(x), 1), round(float(y), 1)])


def receptor_velocities(rec, grid, fin, arrival_m):
    """Max and P95 velocity (ft/s) at flooded structures and at overtopped road / rail samples
    (depth at or above the arrival depth). {} without receptors."""
    if rec is None or not len(rec):
        return {}
    from . import impacts as impmod
    inb = rec.in_bounds(grid.bounds)
    d = rec.sample(grid, fin["depth"])
    v = rec.sample(grid, fin["velocity"])
    out = {}
    with np.errstate(invalid="ignore"):
        wet = inb & np.isfinite(d) & (d >= arrival_m) & np.isfinite(v)
    for kind, name in ((impmod.KIND_STRUCTURE, "structures"), (impmod.KIND_ROAD, "roads"),
                       (impmod.KIND_RAIL, "rail")):
        k = wet & (rec.kind == kind)
        if not k.any():
            continue
        out["velocity_at_{}_max_fps".format(name)] = _r(float(np.max(v[k])) / FT)
        out["velocity_at_{}_p95_fps".format(name)] = _r(float(np.percentile(v[k], 95)) / FT)
    return out


def flood_metrics(fin, grid, cell_ac, arrival_m=None, receptors=None, mask=None,
                  mask_source=None, crs=None):
    """The new summary fields from SI arrays `fin` ('depth', 'velocity') on `grid`.
    `mask`: bool array (True = existing water body) or None. Returns a flat dict."""
    d = np.asarray(fin["depth"], dtype=float)
    v = np.asarray(fin.get("velocity")) if fin.get("velocity") is not None \
        else np.full(d.shape, np.nan)
    s = {}
    a = depth_areas(d, cell_ac)
    a.pop("flooded_acres")
    s.update(a)
    pct, n = velocity_percentiles(d, v)
    s["velocity_p95_fps"] = pct[95]
    s["velocity_p99_fps"] = pct[99]
    s["velocity_wet_min_depth_ft"] = WET_MIN_DEPTH_FT
    vmax, dmax, xy = max_velocity_cell(grid, d, v)
    s["max_velocity_depth_ft"] = dmax
    s["max_velocity_xy"] = xy
    if crs:
        s["max_velocity_xy_crs"] = str(crs)
    if receptors is not None and arrival_m is not None:
        s.update(receptor_velocities(receptors, grid, {"depth": d, "velocity": v}, arrival_m))
    if mask is not None and np.shape(mask) == d.shape:
        mask = np.asarray(mask, dtype=bool)
        keep = ~mask
        s["water_mask_source"] = mask_source or "mask"
        s["water_mask_acres"] = round(float(mask.sum()) * cell_ac, 1)
        s["water_mask_flooded_acres"] = round(float((mask & np.isfinite(d)).sum()) * cell_ac, 1)
        am = depth_areas(d, cell_ac, keep)
        s["flooded_acres_masked"] = am.pop("flooded_acres")
        if arrival_m is not None:
            with np.errstate(invalid="ignore"):
                s["flooded_acres_over_arrival_depth_masked"] = round(
                    float((keep & np.isfinite(d) & (d >= arrival_m)).sum()) * cell_ac, 1)
        for k, val in am.items():
            s[k + "_masked"] = val
        pm, _ = velocity_percentiles(d, v, keep)
        s["velocity_p95_fps_masked"] = pm[95]
        s["velocity_p99_fps_masked"] = pm[99]
        vm, dm, xym = max_velocity_cell(grid, d, v, keep)
        s["max_velocity_fps_masked"] = vm
        s["max_velocity_depth_ft_masked"] = dm
        s["max_velocity_xy_masked"] = xym
    return s


# ------------------------------------------------------------------ the mask --
def _suite_files_in(root):
    """The waterbody files under one suite root, best first: hydro/3DHP_waterbodies.gpkg, any
    other hydro/3DHP_*waterbod*.gpkg (the 3DHP download's own names, sorted), then
    hydro/NHD_waterbodies.gpkg."""
    exact = [os.path.join(root, rel) for rel in SUITE_MASK_FILES]
    extra = sorted(glob.glob(os.path.join(root, SUITE_MASK_GLOB)))
    order = exact[:1] + [p for p in extra if p not in exact] + exact[1:]
    return [p for p in order if os.path.isfile(p)]


def _suite_roots(environ=None):
    """The suite pack folder and the data folder (and its ND_Dam_Suite_Pack) to look in."""
    env = os.environ if environ is None else environ
    roots = []
    pack = env.get("NDSUITE_PACK", "")
    data = env.get("NDSUITE_DATA", "") or env.get("NDSUITE_DATA_FOLDER", "")
    if environ is None:          # the suite settings too (a test passes its own environ)
        try:
            from ..damsuite import suitedata
            pack = pack or suitedata._get(suitedata.KEY_PACK, suitedata.ENV_PACK)
            data = data or suitedata.data_folder()
        except Exception:        # noqa: BLE001 - outside the plugin: environment only
            pass
    if pack:
        roots.append(pack)
    if data:
        roots += [os.path.join(data, PACK_DIR), data]
    return roots


_MASK_BAD_KINDS = ("missing", "table", "unreadable", "manifest")


def _pack_mask_check(root):
    """3.10.86: (PackCheck or None, blocked) for a suite root: None when the root is not an
    offline pack folder (no manifest.json). blocked: True (no suite file of this root is
    used) when the check FAILED - a manifest.json that cannot be read, an error of the check;
    else the set of the files of the water-body layers it lists that are missing or
    unreadable (a cut-short GeoPackage), which are then not used for the mask; else False.
    Read-only, never raises."""
    try:
        from ..damsuite import packcheck as PC
    except Exception:            # noqa: BLE001 - outside the plugin
        return None, False
    try:
        if not root or not os.path.isdir(root):
            return None, False
        if not (PC.pack_folder(root) or PC.manifest_problem(root)):
            return None, False
        res = PC.check_pack(root, deep=False)
        if res.failed:
            return res, True
        names = {i.layer for i in res.issues if i.level != PC.NOTE and i.layer
                 and "waterbod" in i.layer.lower() and i.kind in _MASK_BAD_KINDS}
        if not names:
            return res, False
        m = PC._read_json(os.path.join(root, PC.MANIFEST)) or {}
        files = set()
        for sect in ("datasets", "rasters"):
            for name, e in PC._entries(m, sect).items():
                rel = PC._rel_file(e)
                if name in names and rel:
                    files.add(os.path.normcase(os.path.abspath(os.path.join(root, *rel.split("/")))))
        return res, files
    except Exception:            # noqa: BLE001 - check_pack never raises; belt and braces
        return None, True


def _mask_blocked(blocked, path):
    """True when `path` is a suite file that _pack_mask_check said not to use."""
    if blocked is True:
        return True
    return bool(blocked) and os.path.normcase(os.path.abspath(path)) in blocked


def _suite_candidates(environ=None):
    """Suite pack / data folder waterbody files that exist (read only). 3.10.86: not the
    files of a suite pack whose check fails for its water-body layer (_pack_mask_check)."""
    out = []
    for root in _suite_roots(environ):
        blocked = _pack_mask_check(root)[1]
        for p in _suite_files_in(root):
            if _mask_blocked(blocked, p):
                continue
            if p not in out:
                out.append(p)
                break
    return out[:1]


def pack_gap_notes(environ=None):
    """3.10.77: the water-body layers that a suite pack's manifest.json lists but that are
    missing or unreadable on disk (damsuite.packcheck, files only, read-only), as log lines.
    The mask then falls back silently otherwise (October 2026: NHD_waterbodies.gpkg gone)."""
    out = []
    try:
        from ..damsuite import packcheck as PC
    except Exception:            # noqa: BLE001 - outside the plugin
        return out
    seen = set()
    for root in _suite_roots(environ):
        folder = os.path.abspath(root) if root else ""
        if not folder or folder in seen:
            continue
        seen.add(folder)
        res, blocked = _pack_mask_check(folder)
        if res is None:
            if blocked:
                out.append("Water-body mask: offline pack {}: the pack check could not run - "
                           "its water-body file is not used".format(folder))
            continue
        if res.failed:           # 3.10.86: a broken manifest.json - the pack is not used
            out.append("Water-body mask: offline pack {}: {} - its water-body file is not "
                       "used".format(folder, res.summary()))
            continue
        for i in res.issues:
            if i.level != PC.NOTE and "waterbod" in (i.layer or "").lower():
                out.append("Water-body mask: offline pack {}: {}{}".format(
                    folder, i.line(), " - not used for the mask" if blocked and i.kind in (
                        "table", "unreadable", "manifest") else ""))
    return out


def _dam_folder_candidates(out_folder):
    if not out_folder:
        return []
    try:
        from ..damsuite import damdata
        p = damdata.dam_data(out_folder).get("waterbodies")
        return [p] if p else []
    except Exception:            # noqa: BLE001
        return []


def mask_sources(setting=None, environ=None, out_folder=None):
    """The water-body files to mask with, first the user's, then the dam folder's, then the
    suite's. [] when the mask is off. Polygons given as a list (work CRS) pass through."""
    env = os.environ if environ is None else environ
    val = setting if setting not in (None, "") else env.get(ENV_MASK, "")
    if isinstance(val, (list, tuple)):
        return [val]
    if str(val).strip().lower() in MASK_OFF:
        return []
    out = [p.strip() for p in str(val or "").split(";;") if p.strip()]
    out += [p for p in _dam_folder_candidates(out_folder) if p not in out]
    out += [p for p in _suite_candidates(environ) if p not in out]
    return out


def rasterize_polygons(grid, polygons):
    """Bool array: cell centres inside any polygon (rings in the grid's coordinates).
    Pure numpy (conditioning.points_in_polygon); for user polygons given as lists."""
    from .conditioning import points_in_polygon
    X, Y = np.meshgrid(grid.x0 + (np.arange(grid.nx) + 0.5) * grid.dx,
                       grid.y0 - (np.arange(grid.ny) + 0.5) * grid.dx)
    m = np.zeros((grid.ny, grid.nx), dtype=bool)
    for poly in polygons:
        rings = poly if (len(poly) and np.ndim(poly[0]) == 2) else [poly]
        inside = np.zeros_like(m)
        for k, ring in enumerate(rings):
            ring = np.asarray(ring, dtype=float)
            if len(ring) < 3:
                continue
            p = points_in_polygon(X, Y, ring)
            inside = (inside | p) if k == 0 else (inside & ~p)      # later rings are holes
        m |= inside
    return m


def rasterize_file(grid, path, crs_wkt):
    """Bool array of a vector file's polygons on `grid` (CRS `crs_wkt`), via GDAL: the
    features within the grid are reprojected into memory and burnt at cell centres."""
    from osgeo import gdal, osr
    p, _, lay = str(path).partition("|")
    layer = lay.split("=", 1)[1] if lay.startswith("layername=") else None
    if not os.path.exists(p):
        raise OSError("water-body mask file not found: {}".format(p))
    srs = osr.SpatialReference()
    srs.SetFromUserInput(crs_wkt)
    xmin, ymin, xmax, ymax = (grid.x0, grid.y0 - grid.ny * grid.dx,
                              grid.x0 + grid.nx * grid.dx, grid.y0)
    kw = dict(format="Memory", dstSRS=srs.ExportToWkt(), spatFilter=(xmin, ymin, xmax, ymax),
              spatSRS=srs.ExportToWkt(), geometryType="PROMOTE_TO_MULTI")
    if layer:
        kw["layers"] = [layer]
    vec = gdal.VectorTranslate("", p, options=gdal.VectorTranslateOptions(**kw))
    if vec is None:
        raise OSError("could not read the water-body mask {}".format(path))
    ras = gdal.GetDriverByName("MEM").Create("", grid.nx, grid.ny, 1, gdal.GDT_Byte)
    ras.SetGeoTransform((grid.x0, grid.dx, 0.0, grid.y0, 0.0, -grid.dx))
    ras.SetProjection(srs.ExportToWkt())
    for i in range(vec.GetLayerCount()):
        gdal.RasterizeLayer(ras, [1], vec.GetLayer(i), burn_values=[1])
    return ras.GetRasterBand(1).ReadAsArray().astype(bool)


def build_mask(grid, setting=None, crs_wkt=None, environ=None, out_folder=None, log=None):
    """(bool array or None, info dict) for the run report's 'water_mask'. Never raises:
    a file that cannot be read is a note in info['problems']."""
    srcs = mask_sources(setting, environ, out_folder)
    if log and not any(isinstance(p, str) and "waterbod" in os.path.basename(p).lower()
                       for p in srcs):
        try:
            for line in pack_gap_notes(environ):     # 3.10.77: say why the suite file is not used
                log(line)
        except Exception:        # noqa: BLE001 - 3.10.86: a note only, the mask never raises
            pass
    if not srcs:
        return None, None
    m = np.zeros((grid.ny, grid.nx), dtype=bool)
    used, problems = [], []
    for src in srcs:
        try:
            if isinstance(src, (list, tuple)):
                m |= rasterize_polygons(grid, src)
                used.append("polygons given ({})".format(len(src)))
            else:
                if not crs_wkt:
                    raise ValueError("the grid has no CRS to reproject the mask into")
                m |= rasterize_file(grid, src, crs_wkt)
                used.append(str(src))
        except Exception as e:   # noqa: BLE001 - the mask is reporting only
            problems.append("Water-body mask {} not used: {}: {}".format(
                src if isinstance(src, str) else "polygons", type(e).__name__, e))
    for p in problems:
        if log:
            log(p)
    info = {"sources": used, "problems": problems,
            "note": "Reporting only: masked cells (existing lakes and ponds) stay in the model "
                    "and in the rasters; the *_masked figures leave them out."}
    if not used:
        return None, info
    return m, info


def write_mask_geojson(grid, mask, out_folder, crs=None):
    """The mask as polygons (products.mask_polygons) for GIS and the QA map; path or ''."""
    try:
        from . import products
        polys = products.mask_polygons(grid, mask, min_cells=1)
        if not polys:
            return ""
        feat = products.geojson_polygons(polys, {"kind": "existing water body (mask)",
                                                 "note": "reporting only"})
        return products.write_geojson(os.path.join(out_folder, MASK_GEOJSON), [feat], crs=crs)
    except Exception:            # noqa: BLE001 - an extra
        return ""


# ------------------------------------------------------------ report text --
def report_lines(s):
    """Markdown table rows of the new figures (empty when the summary has none)."""
    if area_key(1.0) not in s:
        return []

    def f(v, unit=""):
        return "-" if v is None else ("{:,}".format(v) + unit)
    lines = ["| Flooded area at or above 0.1 / 1 / 2 ft | {} / {} / {} ac |".format(
        f(s.get(area_key(0.1))), f(s.get(area_key(1.0))), f(s.get(area_key(2.0))))]
    lines.append("| Velocity P95 / P99 (cells at least {:g} ft deep) | {} / {} ft/s |".format(
        s.get("velocity_wet_min_depth_ft", WET_MIN_DEPTH_FT), f(s.get("velocity_p95_fps")),
        f(s.get("velocity_p99_fps"))))
    xy = s.get("max_velocity_xy")
    lines.append("| Maximum-velocity cell | {} ft/s in {} ft of water{} |".format(
        f(s.get("max_velocity_fps")), f(s.get("max_velocity_depth_ft")),
        " at ({:.0f}, {:.0f})".format(*xy) if xy else ""))
    for name, label in (("structures", "flooded structures"), ("roads", "overtopped roads"),
                        ("rail", "overtopped rail")):
        k = "velocity_at_{}_max_fps".format(name)
        if s.get(k) is not None:
            lines.append("| Velocity at {} (max / P95) | {} / {} ft/s |".format(
                label, f(s[k]), f(s.get("velocity_at_{}_p95_fps".format(name)))))
    if "flooded_acres_masked" in s:
        lines.append("| Water-body mask (reporting only) | {} ac masked, {} ac of it wet |".format(
            f(s.get("water_mask_acres")), f(s.get("water_mask_flooded_acres"))))
        lines.append("| Flooded area unmasked / masked | {} / {} ac (at or above 1 ft: {} / {} ac) |"
                     .format(f(s.get("flooded_acres")), f(s.get("flooded_acres_masked")),
                             f(s.get(area_key(1.0))), f(s.get(area_key(1.0) + "_masked"))))
        lines.append("| Velocity P99 / max unmasked vs masked | {} / {} vs {} / {} ft/s |".format(
            f(s.get("velocity_p99_fps")), f(s.get("max_velocity_fps")),
            f(s.get("velocity_p99_fps_masked")), f(s.get("max_velocity_fps_masked"))))
    return lines


def qa_title_lines(s):
    """Second and third title line of the QA depth map."""
    if not s or area_key(1.0) not in s:
        return []

    def f(v):
        return "-" if v is None else "{:,.0f}".format(v)

    def g(v):
        return "-" if v is None else "{:.2f}".format(v)
    out = ["any {} ac | >=0.1 ft {} | >=1 ft {} | >=2 ft {} ac".format(
        f(s.get("flooded_acres")), f(s.get(area_key(0.1))), f(s.get(area_key(1.0))),
        f(s.get(area_key(2.0)))),
        "velocity P99 {} ft/s, max {} ft/s in {} ft of water".format(
            g(s.get("velocity_p99_fps")), g(s.get("max_velocity_fps")),
            g(s.get("max_velocity_depth_ft")))]
    if "water_mask_acres" in s:
        out.append("water-body mask {} ac (reporting only); masked: any {} ac, >=1 ft {} ac".format(
            f(s.get("water_mask_acres")), f(s.get("flooded_acres_masked")),
            f(s.get(area_key(1.0) + "_masked"))))
    return out


_METRIC_PREFIXES = ("flooded_acres_ge_", "velocity_p9", "velocity_wet_", "velocity_at_",
                    "max_velocity_depth", "max_velocity_xy", "water_mask_")


def is_metric_key(k):
    """A summary key this module writes (so a recomputed summary drops the old value)."""
    return k.startswith(_METRIC_PREFIXES) or k.endswith("_masked")


def pass_stats(grid, depth_m, vel_ms, wet_floor_m=0.003):
    """The new figures for one model pass (lisflood_runs[].stats, anuga_runs[].stats), where
    dry cells may be 0 instead of NaN: area at any depth (>= 0.01 ft) and at or above 0.1 / 1 /
    2 ft, P95 / P99 velocity over wet cells and the max-velocity cell's depth and position."""
    d = np.asarray(depth_m, dtype=float)
    with np.errstate(invalid="ignore"):
        d = np.where(np.isfinite(d) & (d >= wet_floor_m), d, np.nan)
    v = np.asarray(vel_ms, dtype=float) if vel_ms is not None else np.full(d.shape, np.nan)
    cell_ac = grid.dx ** 2 / ACRE_M2
    a = depth_areas(d, cell_ac)
    out = {"flooded_acres_any": a.pop("flooded_acres")}
    out.update(a)
    pct, _ = velocity_percentiles(d, v)
    out["velocity_p95_fps"] = pct[95]
    out["velocity_p99_fps"] = pct[99]
    _vm, dm, xy = max_velocity_cell(grid, d, v)
    out["max_velocity_depth_ft"] = dm
    out["max_velocity_xy"] = xy
    return out
