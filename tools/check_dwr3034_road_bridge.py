"""READ-ONLY check of the two Runner 3.10.90 fixes on a finished run (DWR-3034 review).

    python tools/check_dwr3034_road_bridge.py DAM_FOLDER [--nbi 0025008224] [--radius 300]

Reads DAM_FOLDER/_breach_model/run_report.json (DEM, work CRS, road lines), structures.json and
the WSE / depth rasters, and prints:
  * the bridge cut's old and new location (core/bridge_snap) and the distance moved;
  * the overtopping depth over the road crown along the road(s) within --radius of the old
    cut (core/road_profile): with the run's cuts masked, without the mask, and the legacy
    deepest depth-raster sample.
Writes nothing: no file is created or changed anywhere (stdout only). Light: one process,
the DEM read in small windows."""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from nd_breach_runner.core import bridge_snap as bs            # noqa: E402
from nd_breach_runner.core import gdal_io                      # noqa: E402
from nd_breach_runner.core import road_profile as rp           # noqa: E402
from nd_breach_runner.core import structures as sm             # noqa: E402
from nd_breach_runner.core.grid import Grid                    # noqa: E402
from nd_breach_runner.core.workflow import FINAL_NAMES         # noqa: E402


def _raster(path):
    from osgeo import gdal
    ds = gdal.Open(path)
    if ds is None:
        raise SystemExit("cannot open " + path)
    gt, b = ds.GetGeoTransform(), ds.GetRasterBand(1)
    a = b.ReadAsArray().astype(float)
    nd = b.GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    return Grid(a, gt[0], gt[3], gt[1]), a


def _near(lines, c, radius):
    out = []
    for ln in lines:
        q = rp.densify(np.asarray(ln, float), 5.0)
        keep = np.hypot(q[:, 0] - c[0], q[:, 1] - c[1]) <= radius
        if keep.sum() >= 2:
            out.append(q[keep])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dam_folder")
    ap.add_argument("--nbi", default="0025008224")
    ap.add_argument("--radius", type=float, default=bs.SNAP_RADIUS_M)
    a = ap.parse_args(argv)
    bm = os.path.join(a.dam_folder, "_breach_model")
    rep = json.load(open(os.path.join(bm, "run_report.json"), encoding="utf-8"))
    st = rep.get("settings") or {}
    dem = (st.get("input_files") or {}).get("DEM")
    crs = (rep.get("work_crs") or st.get("work_crs") or "EPSG:5070").split()[0]
    z_to_m = float(rep.get("dem_z_to_m") or 1.0)
    cell = float(st.get("cell_size_m") or 30.0)
    roads = [np.asarray(ln, float) for ln in st.get("road_lines") or []]
    sj = json.load(open(os.path.join(bm, "structures.json"), encoding="utf-8"))
    mine = [d for d in sj if a.nbi in str(d.get("name", ""))]
    if not mine:
        raise SystemExit("no structure named like {} in structures.json".format(a.nbi))
    d = mine[0]
    print("run      ", a.dam_folder)
    print("DEM      ", dem, "(z x {:g}, {})".format(z_to_m, crs))
    print("bridge   ", d["name"])
    s = sm.Structure(d["kind"], np.asarray(d["line"], float), float(d.get("width_m", 1.0)),
                     float(d.get("height_m", 1.0)), int(d.get("barrels", 1) or 1),
                     name=d["name"])
    others = [np.asarray(o["line"], float) for o in sj if o is not d and len(o.get("line") or []) >= 2]
    old_line = np.asarray(s.line, float).copy()
    c_old = bs._mid(old_line)
    terr = gdal_io.GdalTerrain(dem, crs)
    row = bs.snap_bridge(s, roads, terr.read, (), a.radius, taken=[bs._mid(o) for o in others])
    print("\n(b) bridge cut location")
    print("  method   ", row["method"], "-", row["note"])
    print("  old cut  ", np.round(old_line, 1).tolist(), "centre", row["old_xy"])
    print("  new cut  ", np.round(np.asarray(s.line), 1).tolist(), "centre", row["new_xy"])
    print("  moved    {:.1f} m".format(row["moved_m"]))
    g, depth = _raster(os.path.join(a.dam_folder, FINAL_NAMES["depth"]))
    _g, wse = _raster(os.path.join(a.dam_folder, FINAL_NAMES["wse"]))
    parts = _near(roads, c_old, a.radius)
    wsef, depf = rp.grid_sampler(g, wse), rp.grid_sampler(g, depth)

    def wet(xy):
        return np.nan_to_num(depf(xy)) > 0

    ground = rp.PointDEM(dem, z_to_m, crs)
    print("\n(a) road overtopping within {:.0f} m of the old cut ({} road piece(s))".format(
        a.radius, len(parts)))
    for label, cuts in (("centreline, run's cuts masked", rp.cut_corridors(sj, cell)),
                        ("centreline, no mask", [])):
        r = rp.road_crown_depth(parts, wsef, ground, wet=wet, cuts=cuts)
        if r is None:
            print("  {:32s} no road surface on the DEM".format(label))
            continue
        print("  {:32s} depth {:.2f} ft  freeboard {}  wet {}  in cuts {}  in openings {}".format(
            label, r["depth_ft"], "-" if r["freeboard_ft"] is None else
            "{:.2f} ft".format(r["freeboard_ft"]), r["n_wet"], r["n_cut"], r["n_opening"]))
    best = (0.0, None, None)
    for p in parts:
        dd = np.nan_to_num(depf(p))
        k = int(np.argmax(dd))
        if dd[k] > best[0]:
            best = (float(dd[k]), float(p[k, 0]), float(p[k, 1]))
    print("  {:32s} depth {:.2f} ft at ({}, {})".format(
        "legacy deepest depth-raster cell", best[0],
        None if best[1] is None else round(best[1], 1),
        None if best[2] is None else round(best[2], 1)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
