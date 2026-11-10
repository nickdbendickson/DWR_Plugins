"""3.17.7: the model terrain, saved for review.

The models do not run on the source DEM as it is. LISFLOOD-FP runs on a resampled grid with
the crest raised, road / rail crests burned in and gaps filled, written with its lower-left
corner moved to (0, 0) and no projection (lisflood.local_grid); ANUGA runs on a gmsh mesh
kept only as arrays (gmsh_mesh.npz). Neither opens in place in QGIS. This writes:

  <pass folder>/model_terrain.tif          each LISFLOOD-FP pass: its grid, in the work CRS
  <anuga folder>/gmsh_mesh.gpkg            each gmsh mesh: triangles (bed elevation, area)
                                           and the mesh lines it was built from
  <dam folder>/Model Terrain (LISFLOOD).tif   the last pass's grid       (publish())
  <dam folder>/ANUGA Mesh.gpkg                the last mesh               (publish())

Review files only: nothing reads them back, and a failure to write one is a log line,
never a failed run. NDBREACH_EXPORT_TERRAIN=0 turns them off.
"""
import glob
import os
import shutil

import numpy as np

FT = 0.3048
TERRAIN_NAME = "model_terrain.tif"
MESH_NAME = "gmsh_mesh.gpkg"
PUBLISHED_TERRAIN = "Model Terrain (LISFLOOD).tif"
PUBLISHED_MESH = "ANUGA Mesh.gpkg"
ENV = "NDBREACH_EXPORT_TERRAIN"


def enabled(st=None):
    if os.environ.get(ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(getattr(st, "export_model_terrain", True)) if st is not None else True


def write_lisflood_terrain(dem, workdir, log=None):
    """The grid LISFLOOD-FP runs on (`dem`, real coordinates, before local_grid) as a
    Float32 GeoTIFF in `workdir`. Returns the path or None."""
    from .reservoir_carve import write_tif
    path = os.path.join(workdir, TERRAIN_NAME)
    try:
        return write_tif(dem.like(dem.masked()), path, dem.crs_wkt)
    except Exception as e:                     # noqa - a review file never fails the run
        if log:
            log("Model terrain not saved ({}: {})".format(type(e).__name__, e))
        return None


def mesh_layers(mesh, dem, spec=None):
    """{layer: (kind, features)} for products.write_gpkg: every triangle with its bed
    elevation (the DEM at the centroid, bilinear) and area, and the spec's lines."""
    pts = np.asarray(mesh["points"], float)
    tris = np.asarray(mesh["triangles"], int)
    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    cen = (a + b + c) / 3.0
    area = 0.5 * np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1])
                        - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1]))
    bed = (np.asarray(dem.sample_bilinear(cen[:, 0], cen[:, 1]), float)
           if dem is not None and len(cen) else np.full(len(tris), np.nan))
    feats = []
    for i in range(len(tris)):
        ring = [tuple(a[i]), tuple(b[i]), tuple(c[i]), tuple(a[i])]
        bm = None if not np.isfinite(bed[i]) else round(float(bed[i]), 3)
        feats.append(([(ring, [])], {"tri": int(i), "area_m2": round(float(area[i]), 2),
                                     "bed_m": bm,
                                     "bed_ft": None if bm is None else round(bm / FT, 2)}))
    layers = {"mesh_triangles": ("polygon", feats)}
    lines = []
    for key in ("lines", "crest_lines", "channel"):
        for ln in (spec or {}).get(key) or []:
            if len(ln) >= 2:
                lines.append(([tuple(map(float, p)) for p in ln], {"kind": key}))
    for sl in (spec or {}).get("size_lines") or []:
        ln = sl.get("line") if isinstance(sl, dict) else None
        if ln and len(ln) >= 2:
            lines.append(([tuple(map(float, p)) for p in ln], {"kind": "size_line"}))
    if (spec or {}).get("outer"):
        ring = [tuple(map(float, p)) for p in spec["outer"]]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        lines.append((ring, {"kind": "outer"}))
    if lines:
        layers["mesh_lines"] = ("line", lines)
    return layers


def write_gmsh_mesh(mesh, dem, spec, workdir, log=None):
    """The gmsh mesh as a GeoPackage in `workdir` (work CRS). Returns the path or None."""
    from . import products
    path = os.path.join(workdir, MESH_NAME)
    try:
        crs = getattr(dem, "crs_wkt", None)
        return products.write_gpkg(path, mesh_layers(mesh, dem, spec), crs)
    except Exception as e:                     # noqa - a review file never fails the run
        if log:
            log("ANUGA mesh not saved for review ({}: {})".format(type(e).__name__, e))
        return None


def _newest(paths):
    paths = [p for p in paths if os.path.isfile(p)]
    return max(paths, key=os.path.getmtime) if paths else None


def publish(model_dir, out_folder, log=None):
    """Copy the newest pass terrain and mesh next to the results. Returns the copied paths."""
    done = []
    for pattern, name in ((os.path.join(model_dir, "lisflood*", "**", TERRAIN_NAME),
                           PUBLISHED_TERRAIN),
                          (os.path.join(model_dir, "anuga*", "**", MESH_NAME), PUBLISHED_MESH)):
        src = _newest(glob.glob(pattern, recursive=True))
        dst = os.path.join(out_folder, name)
        if not src:
            continue
        try:
            shutil.copyfile(src, dst)
            done.append(dst)
        except OSError as e:
            if log:
                log("Could not copy {} to {} ({})".format(src, dst, e))
    if done and log:
        log("Model terrain for review: " + ", ".join(os.path.basename(d) for d in done))
    return done
