#!/usr/bin/env python3
"""Freeze a real dam's classification as a golden regression fixture (audit P1, 2026-11-08).

Takes a finished DamSafety Runner dam folder (the rasters 'Depth (Max).tif', 'Velocity
(Max).tif', 'Arrival Time.tif') and the DamSafety Hazard classification written into it
(<dam>/outputs/Classification/Habitable_Structures.gpkg and
<dam>/outputs/Hazard_Classification_Summary.csv) and writes a compact fixture folder

    DamSafety_Hazard/tests/data/golden_dams/<dam_id>/
        depth.tif, velocity.tif, arrival.tif   the rasters CLIPPED to the structures' bounding
                                               box (+ 100 ft), downsampled (nearest, by an
                                               integer factor) only when a clipped file would be
                                               larger than 5 MB
        structures.gpkg                        the Hazard's Habitable_Structures layer (every
                                               structure it evaluated, dry ones included when
                                               the run kept them)
        structures.csv                         the same attributes as text (readable in a diff)
        expected.json                          ND class, TM 11 class, lives in jeopardy, the
                                               inundated / cut-off counts, the per-structure
                                               zone / depth / velocity, and how the fixture was
                                               made (plugin versions, raster cell, SHA-256 of
                                               the sources)

that DamSafety_Hazard/tests/test_golden_dams.py re-classifies and compares. Run it on the
maintainer's QGIS box (needs GDAL; no QGIS):

    python3 tools/freeze_golden_dam.py <dam folder> [--dam-id DWR-2801] [--out <fixtures root>]
            [--max-mb 5] [--pad-ft 100]

The fixture is data, not a result: the classification of record stays the Hazard's output in
the dam folder. Commit the fixture folder; the test skips with a notice while none exists.
"""
import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "DamSafety_Hazard", "tests", "data", "golden_dams")
RASTERS = {"depth": "Depth (Max).tif", "velocity": "Velocity (Max).tif",
           "arrival": "Arrival Time.tif"}
STRUCTURES = os.path.join("outputs", "Classification", "Habitable_Structures.gpkg")
STRUCTURES_OLD = os.path.join("outputs", "Habitable_Structures.gpkg")
SUMMARY = os.path.join("outputs", "Hazard_Classification_Summary.csv")
SNAPSHOT = os.path.join("outputs", "QA", "Last_Run_Snapshot.json")
# the Habitable_Structures columns the test compares (structures.STRUCTURE_FIELDS names)
KEEP = ("Struct_ID", "Habitable", "ACER_Type", "Persons", "Max_Depth_ft", "Max_Vel_fps",
        "First_Arrival_hr", "ACER_Zone", "ACER_Zone_Label", "Danger_Zone", "ND_Outcome",
        "Near_Threshold", "Access_Cut_Off", "Notes")
EXPECTED_VERSION = 1


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _gdal():
    try:
        from osgeo import gdal, ogr, osr  # noqa: F401
    except ImportError:
        sys.exit("GDAL's Python bindings (osgeo) are needed to clip the rasters: run this with "
                 "the QGIS Python (python-qgis / OSGeo4W shell).")
    gdal.UseExceptions()
    return gdal


def structures_path(folder):
    for rel in (STRUCTURES, STRUCTURES_OLD):
        p = os.path.join(folder, rel)
        if os.path.isfile(p):
            return p
    return None


def read_structures(gpkg):
    """(layer name, [(fid, {field: value}, (xmin, ymin, xmax, ymax))]) of the Hazard's
    Habitable_Structures GeoPackage - sqlite3 and the suite's pure-Python GeoPackage reader,
    so this runs without GDAL's vector side."""
    sys.path.insert(0, os.path.join(ROOT, "DamSafety_Hazard"))
    from damsuite import suitegpkg as G
    layers = [l for l in G.list_layers(gpkg) if l["data_type"] == "features"]
    if not layers:
        raise SystemExit("{}: no feature layer".format(gpkg))
    name = layers[0]["name"]
    out = []
    con = sqlite3.connect(gpkg)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info({})".format(G._qi(name)))]
        gcol = con.execute("SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?",
                           (name,)).fetchone()[0]
        srs = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name=?",
                          (name,)).fetchone()[0]
        for row in con.execute("SELECT {} FROM {} ORDER BY fid".format(
                ", ".join(G._qi(c) for c in cols), G._qi(name))):
            d = dict(zip(cols, row))
            blob = d.pop(gcol)
            fid = d.pop("fid", None)
            geom = G.parse_blob(blob)[0] if blob is not None else None
            bb = G.bbox(geom) if geom is not None else None
            out.append((fid, d, bb))
    finally:
        con.close()
    return name, srs, out


def read_summary(folder):
    """{item: value} of Hazard_Classification_Summary.csv ('ND reason' rows joined)."""
    p = os.path.join(folder, SUMMARY)
    if not os.path.isfile(p):
        raise SystemExit("{} not found: run the Hazard classification on this dam first".format(p))
    out, reasons = {}, []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if len(row) < 2:
                continue
            k, v = row[0].strip(), row[1].strip()
            if k == "ND reason":
                reasons.append(v)
            elif k not in out:
                out[k] = v
    out["ND reasons"] = reasons
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def clip_raster(gdal, src, dst, bounds, max_bytes):
    """Clip `src` to `bounds` (its own CRS) into `dst` (GeoTIFF, deflate); downsample by the
    smallest integer factor (nearest) that keeps the file under max_bytes. Returns a record."""
    ds = gdal.Open(src)
    gt = ds.GetGeoTransform()
    px, py = abs(gt[1]), abs(gt[5])
    xmin, ymin, xmax, ymax = bounds
    # snap the window to the source grid so the clipped cells ARE source cells
    c0 = max(0, int(math.floor((xmin - gt[0]) / gt[1])))
    c1 = min(ds.RasterXSize, int(math.ceil((xmax - gt[0]) / gt[1])))
    r0 = max(0, int(math.floor((ymax - gt[3]) / gt[5])))
    r1 = min(ds.RasterYSize, int(math.ceil((ymin - gt[3]) / gt[5])))
    if c1 <= c0 or r1 <= r0:
        raise SystemExit("{}: the structures' box misses the raster".format(src))
    w, h = c1 - c0, r1 - r0
    factor = 1
    band = ds.GetRasterBand(1)
    nbytes = gdal.GetDataTypeSize(band.DataType) // 8
    while (w // factor) * (h // factor) * nbytes > max_bytes:
        factor += 1
    win = [gt[0] + c0 * gt[1], gt[3] + r0 * gt[5], gt[0] + c1 * gt[1], gt[3] + r1 * gt[5]]
    opts = gdal.TranslateOptions(
        projWin=win, xRes=px * factor, yRes=py * factor, resampleAlg="nearest",
        creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=2", "TILED=YES"])
    gdal.Translate(dst, ds, options=opts)
    out = gdal.Open(dst)
    rec = {"source": os.path.basename(src), "source_sha256": sha256_file(src),
           "window_cells": [c0, r0, c1, r1], "downsample_factor": factor,
           "cell": [abs(out.GetGeoTransform()[1]), abs(out.GetGeoTransform()[5])],
           "size": [out.RasterXSize, out.RasterYSize],
           "bytes": os.path.getsize(dst), "crs": out.GetProjection()}
    out = ds = None
    return rec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", help="a finished Runner dam folder with the Hazard outputs")
    ap.add_argument("--dam-id", default=None, help="fixture folder name (default: the folder's)")
    ap.add_argument("--out", default=FIXTURES, help="fixtures root (default: {})".format(
        os.path.relpath(FIXTURES, ROOT)))
    ap.add_argument("--max-mb", type=float, default=5.0,
                    help="downsample a clipped raster larger than this (MB; default 5)")
    ap.add_argument("--pad-ft", type=float, default=100.0,
                    help="pad the structures' bounding box by this (ft; default 100)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing fixture")
    args = ap.parse_args(argv)
    folder = os.path.abspath(args.folder)
    dam_id = args.dam_id or os.path.basename(os.path.normpath(folder))
    gdal = _gdal()
    for k, name in RASTERS.items():
        if not os.path.isfile(os.path.join(folder, name)):
            sys.exit("{}: '{}' not found - a finished Runner dam folder is needed".format(
                folder, name))
    gpkg = structures_path(folder)
    if gpkg is None:
        sys.exit("{}: no Habitable_Structures.gpkg - run the Hazard classification first".format(
            folder))
    summary = read_summary(folder)
    layer, srs, structs = read_structures(gpkg)
    if not structs:
        sys.exit("{}: no structures".format(gpkg))
    boxes = [bb for _f, _d, bb in structs if bb is not None]
    xmin = min(b[0] for b in boxes)
    ymin = min(b[1] for b in boxes)
    xmax = max(b[2] for b in boxes)
    ymax = max(b[3] for b in boxes)
    # the structures layer is in the Hazard's analysis CRS (the depth raster's); its units
    # from the raster's CRS, so the pad is in the right units
    ds = gdal.Open(os.path.join(folder, RASTERS["depth"]))
    from osgeo import osr
    sr = osr.SpatialReference()
    sr.ImportFromWkt(ds.GetProjection())
    unit_m = sr.GetLinearUnits() if sr.IsProjected() else None
    ds = None
    if unit_m is None:
        sys.exit("the depth raster's CRS is not projected; a fixture needs a projected grid")
    pad = args.pad_ft * 0.3048 / unit_m
    bounds = (xmin - pad, ymin - pad, xmax + pad, ymax + pad)
    out = os.path.join(args.out, dam_id)
    if os.path.isdir(out):
        if not args.force:
            sys.exit("{} exists; --force to overwrite it".format(out))
        shutil.rmtree(out)
    os.makedirs(out)
    rasters = {}
    for k, name in RASTERS.items():
        rasters[k] = clip_raster(gdal, os.path.join(folder, name), os.path.join(out, k + ".tif"),
                                 bounds, int(args.max_mb * 1024 * 1024))
    shutil.copyfile(gpkg, os.path.join(out, "structures.gpkg"))
    with open(os.path.join(out, "structures.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(KEEP + ("xmin", "ymin", "xmax", "ymax"))
        for _fid, d, bb in structs:
            w.writerow([d.get(k, "") for k in KEEP] + list(bb or ("", "", "", "")))
    wet = [d for _f, d, _b in structs if d.get("ND_Outcome") not in (None, "", "Not inundated")]
    habitable_wet = [d for d in wet if d.get("Habitable") == "Yes"]
    per = []
    for _fid, d, bb in structs:
        per.append({"id": d.get("Struct_ID"), "habitable": d.get("Habitable"),
                    "acer_type": d.get("ACER_Type"), "persons": _num(d.get("Persons")),
                    "max_depth_ft": _num(d.get("Max_Depth_ft")),
                    "max_vel_fps": _num(d.get("Max_Vel_fps")),
                    "arrival_hr": _num(d.get("First_Arrival_hr")),
                    "zone": None if _num(d.get("ACER_Zone")) is None else int(_num(d.get("ACER_Zone"))),
                    "zone_label": d.get("ACER_Zone_Label"), "outcome": d.get("ND_Outcome"),
                    "near": d.get("Near_Threshold"), "cut_off": d.get("Access_Cut_Off"),
                    "bbox": list(bb) if bb else None})
    snap = {}
    sp = os.path.join(folder, SNAPSHOT)
    if os.path.isfile(sp):
        try:
            with open(sp, encoding="utf-8") as f:
                snap = json.load(f)
        except (OSError, ValueError):
            snap = {}
    hazard_version = snap.get("plugin_version") or ""
    settings = snap.get("settings") or {}
    expected = {
        "version": EXPECTED_VERSION, "dam_id": dam_id, "source_folder": folder,
        "frozen_by": "tools/freeze_golden_dam.py",
        "hazard_plugin_version": hazard_version,
        "hazard_settings": settings,
        "nd_class": summary.get("ND classification (screening)"),
        "nd_reasons": summary.get("ND reasons", []),
        "tm11_class": summary.get(
            "TM 11 classification (ACER TM 11 Table 1 reference, not the ND class)"),
        "lives_in_jeopardy": _num(summary.get("TM 11 lives in jeopardy")),
        "counts": {"structures": len(structs), "inundated": len(wet),
                   "habitable_inundated": len(habitable_wet),
                   "probable_loss_of_life": sum(1 for d in habitable_wet
                                                if d.get("ND_Outcome") == "Probable loss of life"),
                   "judgment_zone": sum(1 for d in habitable_wet
                                        if d.get("ND_Outcome") == "Judgment zone"),
                   "cut_off": sum(1 for _f, d, _b in structs if d.get("Access_Cut_Off") == "Yes"),
                   "persons_inundated": sum(_num(d.get("Persons")) or 0 for d in habitable_wet)},
        "structures_layer": layer, "structures_srs_id": srs,
        "structures_sha256": sha256_file(gpkg),
        "summary_sha256": sha256_file(os.path.join(folder, SUMMARY)),
        "rasters": rasters, "crs_unit_m": unit_m,
        "structures": per,
    }
    with open(os.path.join(out, "expected.json"), "w", encoding="utf-8") as f:
        json.dump(expected, f, indent=1, default=str)
    total = sum(os.path.getsize(os.path.join(out, n)) for n in os.listdir(out))
    print("fixture {}: {} structure(s), {} inundated, ND {} / TM 11 {} (LIJ {}), {:.1f} MB"
          .format(out, len(structs), len(wet), expected["nd_class"], expected["tm11_class"],
                  expected["lives_in_jeopardy"], total / 1048576.0))
    for k, r in rasters.items():
        print("  {}: {} x {} cells of {:g} x {:g}{}".format(
            k, r["size"][0], r["size"][1], r["cell"][0], r["cell"][1],
            " (downsampled x{})".format(r["downsample_factor"]) if r["downsample_factor"] > 1
            else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
