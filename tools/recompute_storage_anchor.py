#!/usr/bin/env python3
"""Read-only: recompute tool 3's storage for chosen dams, before and after 0.17.47's anchoring.

Screening 0.17.47 places each inventory storage at its own elevation (core/storage.py
anchor_inventory_storage). This script redoes tool 3's storage for some dams of an existing
screening work folder from the inputs already there (1_dams.gpkg, 2_terrain/dam_<id>.tif, the
water-body layer) and the DWR inventory record, and prints:

  old   the logic before 0.17.47 (calibrate_below_pool at the water surface with the work
        folder's inv_norm_m3, then ensure_top_of_dam with the maximum), with the work folder's
        inventory values
  new   anchor_inventory_storage with the work folder's inventory values (what a rerun with the
        same tool 1 field map gives) and with the DWR record's own normal / max storage
        (normal_vol / max_pool_vol, normal_elev / max_pool_elev)

It opens every input read-only and writes nothing unless --out is given (a JSON report). It
runs no model. Needs GDAL/OGR, numpy (and scipy for the shoreline slope, as tool 3).

    python tools/recompute_storage_anchor.py --work <screen folder> \\
        --wb "<layers.gpkg>|layername=waterbodies" --dwr "<DWR_dams.gpkg>|layername=DWR_dams" \\
        DWR-2865 DWR-3034
"""
import argparse
import re
import json
import os
import sys

import numpy as np
from osgeo import gdal, ogr, osr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "DamSafety_Screening", "core"))
import storage as S  # noqa: E402  (pure numpy/scipy, no QGIS)

FT, AF = 0.3048, 1233.4818375
gdal.UseExceptions()
ogr.UseExceptions()


def _open_vector(uri):
    path, layer = (uri.split("|layername=", 1) + [None])[:2]
    ds = ogr.Open(path, 0)                                  # 0 = read-only
    if ds is None:
        raise SystemExit("cannot open " + path)
    return ds, (ds.GetLayerByName(layer) if layer else ds.GetLayer(0))


def _srs(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def _dam_rows(work, ids):
    ds, lyr = _open_vector(os.path.join(work, "1_dams.gpkg"))
    out = {}
    src = lyr.GetSpatialRef()
    if src is not None:
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    tr = osr.CoordinateTransformation(src or _srs(4326), _srs(5070))
    for f in lyr:
        did = f.GetField("dam_id")
        if did in ids:
            g = f.GetGeometryRef().Clone()
            g.Transform(tr)
            out[did] = {"x": g.GetX(), "y": g.GetY(),
                        **{k: f.GetField(k) for k in ("height_m", "inv_norm_m3", "inv_max_m3")}}
    ds = None
    return out


def _dwr_record(uri, dam_id):
    ds, lyr = _open_vector(uri)
    sid = dam_id.split("-", 1)[1]
    lyr.SetAttributeFilter("CAST(structure_id AS TEXT) = '{}'".format(sid.replace("'", "")))
    rec = None
    for f in lyr:
        rec = {k: f.GetField(k) for k in ("normal_vol", "normal_elev", "max_pool_vol", "max_pool_elev",
                                         "emergency_elev", "streambed_elev", "max_dam_height",
                                         "vertical_datum")}
        break
    ds = None
    return rec


def _waterbody_mask(wb_uri, ds, x, y, search_m):
    vds, lyr = _open_vector(wb_uri)
    src = lyr.GetSpatialRef()
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    to_alb = osr.CoordinateTransformation(src, _srs(5070))
    to_src = osr.CoordinateTransformation(_srs(5070), src)
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for px, py in ((x - search_m, y - search_m), (x + search_m, y - search_m),
                   (x + search_m, y + search_m), (x - search_m, y + search_m),
                   (x - search_m, y - search_m)):
        ring.AddPoint_2D(px, py)
    box = ogr.Geometry(ogr.wkbPolygon)
    box.AddGeometry(ring)
    box.Transform(to_src)
    lyr.SetSpatialFilter(box)
    pt = ogr.Geometry(ogr.wkbPoint)
    pt.AddPoint_2D(x, y)
    cands = []
    for f in lyr:
        g = f.GetGeometryRef().Clone()
        g.Transform(to_alb)
        d = g.Distance(pt)
        if d <= search_m:
            cands.append((round(d), -g.GetArea(), g.ExportToWkb()))
    vds = None
    if not cands:
        return None
    cands.sort(key=lambda c: (c[0], c[1]))
    mem = gdal.GetDriverByName("MEM").Create("", ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Byte)
    mem.SetGeoTransform(ds.GetGeoTransform())
    mem.SetProjection(ds.GetProjection())
    mds = ogr.GetDriverByName("MEM" if ogr.GetDriverByName("MEM") else "Memory").CreateDataSource("wb")
    ml = mds.CreateLayer("wb", _srs(5070), ogr.wkbPolygon)
    ft = ogr.Feature(ml.GetLayerDefn())
    ft.SetGeometry(ogr.CreateGeometryFromWkb(bytes(cands[0][2])))
    ml.CreateFeature(ft)
    gdal.RasterizeLayer(mem, [1], ml, burn_values=[1])
    return mem.ReadAsArray().astype(bool)


def _at(curve, e):
    return S._interp_curve(curve, e) if curve else None


def _acft(v):
    return None if v is None else round(v / AF, 1)


def recompute(work, wb, dwr_uri, dam_id, row, search_m=500 * FT, shape=1.0, extra_ft=()):
    dem_path = os.path.join(work, "2_terrain", "dam_{}.tif".format(dam_id))
    ds = gdal.Open(dem_path, gdal.GA_ReadOnly)
    arr = ds.GetRasterBand(1).ReadAsArray().astype("float64")
    nd = ds.GetRasterBand(1).GetNoDataValue()
    if nd is not None:
        arr[arr == nd] = np.nan
    gt = ds.GetGeoTransform()
    cell = abs(gt[1])
    col = int((row["x"] - gt[0]) / gt[1])
    rr = int((row["y"] - gt[3]) / gt[5])
    mask = _waterbody_mask(wb, ds, row["x"], row["y"], search_m)
    if mask is None:
        return {"dam_id": dam_id, "error": "no waterbody within the search distance"}
    h = float(row["height_m"]) if row["height_m"] not in (None, "") else None
    res = S.estimate_storage(arr, mask, cell, (rr, col), known_height_m=h, shape_factor=shape)
    w_norm = float(row["inv_norm_m3"] or 0.0)
    w_max = float(row["inv_max_m3"] or 0.0)
    pick = S.resolve_storage_choice(w_norm, w_max, S.STORAGE_MAX)

    # old: before 0.17.47
    old, _sc, _n = S.calibrate_below_pool(res.curve, res.below_pool_m3, w_norm or None)
    if pick["kind"] == "maximum":
        old, _t = S.ensure_top_of_dam(old, pick["volume_m3"], res.crest_elev)

    rec = _dwr_record(dwr_uri, dam_id) or {}
    ne = rec.get("normal_elev")
    me = rec.get("max_pool_elev")
    ne_m = ne * FT if ne else None
    me_m = me * FT if me else None
    new_w, _s1, info_w = S.anchor_inventory_storage(res.curve, res.below_pool_m3, res.pool_elev,
                                                    w_norm, w_max, pick["kind"], ne_m, me_m,
                                                    res.crest_elev)
    r_norm = (rec.get("normal_vol") or 0.0) * AF
    r_max = (rec.get("max_pool_vol") or 0.0) * AF
    pick_r = S.resolve_storage_choice(r_norm, r_max, S.STORAGE_MAX)
    new_r, _s2, info_r = S.anchor_inventory_storage(res.curve, res.below_pool_m3, res.pool_elev,
                                                    r_norm, r_max, pick_r["kind"], ne_m, me_m,
                                                    res.crest_elev)
    e_max = info_r["anchor_m"]
    leak = [n for n in res.notes if "spill" in n]
    levels = {"normal pool (DEM water surface)": res.pool_elev}
    if e_max is not None:
        levels["max pool (pool + inventory rise)"] = e_max
    sp = None
    for n in res.notes:
        mm = re.search(r"spill level ([0-9.]+) ft", n)
        if mm:
            sp = float(mm.group(1)) * FT
    if sp is not None:
        levels["spill (DEM leak guard)"] = sp
    for x in extra_ft:
        levels["{:.2f} ft (asked)".format(x)] = x * FT
    levels["end of curve (crest)"] = res.curve[-1][0]
    levels = dict(sorted(levels.items(), key=lambda kv: kv[1]))
    table = []
    for lab, e in levels.items():
        table.append({"level": lab, "elev_ft": round(e / FT, 2),
                      "old_acft": _acft(_at(old, e)),
                      "new_same_inputs_acft": _acft(_at(new_w, e)),
                      "new_dwr_record_acft": _acft(_at(new_r, e))})
    return {
        "dam_id": dam_id, "dem": dem_path, "cell_m": cell,
        "pool_ft": round(res.pool_elev / FT, 2), "crest_ft": round(res.crest_elev / FT, 2),
        "below_est_acft": _acft(res.below_pool_m3), "fill_to_end_acft": _acft(res.above_pool_m3),
        "leak_guard": leak,
        "work_inv_norm_acft": _acft(w_norm), "work_inv_max_acft": _acft(w_max),
        "dwr_record": rec,
        "old_curve_end_acft": _acft(old[-1][1]),
        "new_same_inputs_curve_end_acft": _acft(new_w[-1][1]),
        "new_dwr_record_curve_end_acft": _acft(new_r[-1][1]),
        "new_same_inputs_flags": info_w["flags"], "new_same_inputs_notes": info_w["notes"],
        "new_dwr_record_flags": info_r["flags"], "new_dwr_record_notes": info_r["notes"],
        "levels": table,
        "old_curve": [(round(e / FT, 2), _acft(v)) for e, v in old],
        "new_dwr_record_curve": [(round(e / FT, 2), _acft(v)) for e, v in new_r],
        "new_same_inputs_curve": [(round(e / FT, 2), _acft(v)) for e, v in new_w],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True, help="screening work folder (read only)")
    ap.add_argument("--wb", required=True, help="water-body layer uri (path|layername=...)")
    ap.add_argument("--dwr", required=True, help="DWR dams layer uri (path|layername=...)")
    ap.add_argument("--out", help="write the report as JSON here (never inside --work)")
    ap.add_argument("--level-ft", type=float, action="append", default=[],
                    help="also report the storage at this elevation (ft, DEM datum); repeatable")
    ap.add_argument("ids", nargs="+")
    a = ap.parse_args(argv)
    if a.out and os.path.abspath(a.out).startswith(os.path.abspath(a.work) + os.sep):
        raise SystemExit("--out must not be inside the work folder")
    rows = _dam_rows(a.work, set(a.ids))
    report = []
    for did in a.ids:
        if did not in rows:
            report.append({"dam_id": did, "error": "not in 1_dams.gpkg"})
            continue
        report.append(recompute(a.work, a.wb, a.dwr, did, rows[did], extra_ft=a.level_ft))
    for r in report:
        print("=" * 72)
        print(r["dam_id"], r.get("error", ""))
        if "error" in r:
            continue
        print("  pool {pool_ft} ft, crest {crest_ft} ft, cell {cell_m} m; leak guard: {leak_guard}".format(**r))
        print("  work folder inventory: normal {work_inv_norm_acft} / max {work_inv_max_acft} ac-ft".format(**r))
        d = r["dwr_record"]
        print("  DWR record: normal {} ac-ft @ {} ft, max {} ac-ft @ {} ft, datum {!r}".format(
            d.get("normal_vol"), d.get("normal_elev"), d.get("max_pool_vol"), d.get("max_pool_elev"),
            d.get("vertical_datum")))
        print("  curve end: old {old_curve_end_acft} | new, same inputs {new_same_inputs_curve_end_acft} | "
              "new, DWR record {new_dwr_record_curve_end_acft} ac-ft".format(**r))
        for t in r["levels"]:
            print("    {level:<36} {elev_ft:>9} ft  old {old_acft}  new(same) {new_same_inputs_acft}  "
                  "new(DWR) {new_dwr_record_acft}".format(**t))
        for fl in r["new_same_inputs_flags"]:
            print("  flag (same inputs):", fl)
        for fl in r["new_dwr_record_flags"]:
            print("  flag (DWR record):", fl)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
