"""READ-ONLY, MESH-ONLY check of the Runner 3.10.91 ANUGA crest hold on DWR-2801 (Hwy 18, Q3).

    LD_LIBRARY_PATH=<env with libGLU> nice -n 19 python tools/check_dwr2801_anuga_crest.py \
        RUN_FOLDER/_breach_model DEM_1M.vrt [--out SCRATCH_DIR]

No ANUGA solve. Reads the finished run's anuga/gmsh_mesh_in.json, anuga/anuga_job.npz (the
mesh it ran), run_report.json (road / rail lines), lisflood_hmax_ft.tif (the wet area the
ANUGA mesh was cut to), structures.json (bridge / notch cuts) and the 1 m DEM, then:

  BEFORE  the run's own mesh, its bed = the 1 m DEM bilinear at the vertices (fine mode);
  AFTER   the same gmsh input plus the 3.10.91 crest lines (road / rail centrelines in the
          wet area + 152.4 m) meshed again with gmsh (one thread), crest nodes held at the
          1 m crest profile (anuga_crest.hold_crest) and raised (crest_vertex_values).

For each it prints the pour level from the pond west of Hwy 18 to the two points east of it
the 2026-10-27 review used (mesh_pour.txt): the lowest possible highest bed along any path of
triangles sharing an edge, measured two ways: "centroid" (the triangle centroid bed, the
review's measure) and "edge" (also the bed on each edge crossed, the mean of its two vertices,
which is where the water passes in ANUGA's DE0 fluxes). Only the Hwy 18 window (2.5 km) is
read at 1 m. Writes only into --out (the new mesh npz); never into the run folder."""
import argparse
import heapq
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("OMP_NUM_THREADS", "1")

from nd_breach_runner.core import anuga_crest as AC  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import gmsh_mesher as GM  # noqa: E402
from nd_breach_runner.core import road_profile as RP  # noqa: E402
from nd_breach_runner.core import terrain  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402

FT = 0.3048
POND = (-93405.0, 2657925.0)
EAST = ((-93315.0, 2657985.0), (-93285.0, 2658315.0))
WIN = (-94600.0, 2656800.0, -92100.0, 2659300.0)      # x0, y0, x1, y1 (EPSG:5070 m)


class Dem1m:
    def __init__(self, path):
        from osgeo import gdal
        gdal.UseExceptions()
        self.ds = gdal.Open(path)
        self.gt = self.ds.GetGeoTransform()
        self.band = self.ds.GetRasterBand(1)
        self.nd = self.band.GetNoDataValue()

    def read(self, bounds, cell=1.0):
        """Grid of the native 1 m cells covering bounds (the cell argument is ignored: 1 m)."""
        x0, y0, x1, y1 = bounds
        gx, dx, _, gy, _, dy = self.gt
        c0 = max(0, int(np.floor((x0 - gx) / dx)))
        r0 = max(0, int(np.floor((gy - y1) / -dy)))
        c1 = min(self.ds.RasterXSize, int(np.ceil((x1 - gx) / dx)) + 1)
        r1 = min(self.ds.RasterYSize, int(np.ceil((gy - y0) / -dy)) + 1)
        a = self.band.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float64)
        if self.nd is not None:
            a[a == self.nd] = np.nan
        a[a < -1000] = np.nan
        return Grid(a, gx + c0 * dx, gy + r0 * dy, dx)


def bed_at(win, xy):
    return AW.bilinear(win.data, win.x0, win.y0, win.dx, xy[:, 0], xy[:, 1])


def pour(points, tris, vz, src, dsts):
    """{dst: (centroid pour, edge pour)} over the triangles fully inside WIN."""
    P = np.asarray(points)
    T = np.asarray(tris)
    inside = np.all((P[T][:, :, 0] > WIN[0]) & (P[T][:, :, 0] < WIN[2]) &
                    (P[T][:, :, 1] > WIN[1]) & (P[T][:, :, 1] < WIN[3]), axis=1)
    ti = np.flatnonzero(inside)
    sub = T[ti]
    zc = vz[sub].mean(axis=1)
    edges = {}
    for k, (a, b, c) in enumerate(sub):
        for u, v in ((a, b), (b, c), (c, a)):
            edges.setdefault((min(u, v), max(u, v)), []).append(k)
    nb = [[] for _ in range(len(sub))]
    for (u, v), ks in edges.items():
        if len(ks) == 2:
            ze = 0.5 * (vz[u] + vz[v])
            nb[ks[0]].append((ks[1], ze))
            nb[ks[1]].append((ks[0], ze))

    def locate(p):
        cen = P[sub].mean(axis=1)
        return int(np.argmin(np.hypot(cen[:, 0] - p[0], cen[:, 1] - p[1])))

    s = locate(src)
    cen = P[sub].mean(axis=1)
    out = {}
    for use_edge in (False, True):
        best = np.full(len(sub), np.inf)
        par = np.full(len(sub), -1)
        best[s] = zc[s]
        h = [(zc[s], s)]
        while h:
            z, k = heapq.heappop(h)
            if z > best[k]:
                continue
            for j, ze in nb[k]:
                nz = max(z, zc[j], ze if use_edge else -np.inf)
                if nz < best[j]:
                    best[j] = nz
                    par[j] = k
                    heapq.heappush(h, (nz, j))
        for d in dsts:
            k = locate(d)
            lvl = float(best[k])
            path = [k]
            while par[path[-1]] >= 0:
                path.append(int(par[path[-1]]))
            path = path[::-1]
            at = next((q for q in path if best[q] >= lvl - 1e-9), path[-1])  # where it is set
            out.setdefault(d, []).append((lvl, tuple(int(v) for v in np.round(cen[at]))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model_dir")
    ap.add_argument("dem_1m")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    t0 = time.time()
    md = a.model_dir
    spec = json.load(open(os.path.join(md, "anuga", "gmsh_mesh_in.json")))
    job = np.load(os.path.join(md, "anuga", "anuga_job.npz"), allow_pickle=False)
    rep = json.load(open(os.path.join(md, "run_report.json")))
    roads = rep["settings"].get("road_lines") or []
    roads = json.loads(roads) if isinstance(roads, str) else roads
    lines = [np.asarray(r, float) for r in roads] + \
        [np.asarray(r, float) for r in rep.get("rail_lines_xy") or []]
    from osgeo import gdal
    ds = gdal.Open(os.path.join(md, "lisflood_hmax_ft.tif"))
    gt = ds.GetGeoTransform()
    h = ds.GetRasterBand(1).ReadAsArray().astype(float)
    g = Grid(h, gt[0], gt[3], gt[1])
    wet_m = np.isfinite(h) & (h > 0)
    o = AC.crest_options(None)
    wet = (g, terrain.dilate(wet_m, o["crest_pad_m"], g.dx))
    outer = np.asarray(spec["outer"], float)
    pieces, cinfo = AC.crest_lines(lines, outer, wet)
    print("crest lines: {} pieces, {} km (wet area + {:g} m; {} road / rail lines)".format(
        cinfo["crest_lines"], cinfo["crest_km"], o["crest_pad_m"], len(lines)))

    dem = Dem1m(a.dem_1m)
    pad = 300.0
    win = dem.read((WIN[0] - pad, WIN[1] - pad, WIN[2] + pad, WIN[3] + pad))

    # BEFORE: the run's mesh
    P0, T0 = job["mesh_points"], job["mesh_triangles"]
    vz0 = np.full(len(P0), np.nan)
    inw = (P0[:, 0] > WIN[0] - 50) & (P0[:, 0] < WIN[2] + 50) & \
        (P0[:, 1] > WIN[1] - 50) & (P0[:, 1] < WIN[3] + 50)
    vz0[inw] = bed_at(win, P0[inw])
    before = pour(P0, T0, vz0, POND, EAST)
    print("BEFORE run mesh: {} triangles".format(len(T0)))

    # AFTER: the same input + crest lines
    spec2 = dict(spec, threads=1)
    spec2["lines"] = list(spec["lines"]) + [p.tolist() for p in pieces]
    spec2["crest_lines"] = [p.tolist() for p in pieces]
    t1 = time.time()
    m = GM.build_mesh(spec2)
    mesh_s = time.time() - t1
    P1, T1 = m["points"], m["triangles"]
    cn = m.get("crest_nodes", np.zeros(0, np.int64))
    print("AFTER mesh: {} triangles ({:+d}), {} nodes, {} crest nodes, gmsh {:.0f} s "
          "(1 thread)".format(len(T1), len(T1) - len(T0), len(P1), len(cn), mesh_s))
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        np.savez_compressed(os.path.join(a.out, "gmsh_mesh_crest.npz"), points=P1,
                            triangles=T1, crest_nodes=cn)
    vz1 = np.full(len(P1), np.nan)
    inw = (P1[:, 0] > WIN[0] - 50) & (P1[:, 0] < WIN[2] + 50) & \
        (P1[:, 1] > WIN[1] - 50) & (P1[:, 1] < WIN[3] + 50)
    vz1[inw] = bed_at(win, P1[inw])
    after_nohold = pour(P1, T1, vz1, POND, EAST)
    # crest profile only for the pieces in the window
    box = np.array([[WIN[0], WIN[1]], [WIN[2], WIN[1]], [WIN[2], WIN[3]], [WIN[0], WIN[3]]])
    wp, _ = AC.crest_lines(pieces, box)
    prof = [AC.crest_profile(p, lambda b, c: dem.read(b), o["crest_cell_m"],
                             o["crest_half_width_m"]) for p in wp]
    cnw = cn[inw[cn]]
    structs = json.load(open(os.path.join(md, "structures.json")))
    cuts = RP.cut_corridors(structs, 30.0)
    ko = AC.keep_out_mask(P1[cnw], cuts, job["inlet"])
    hmin = 1.3 * GM.h_of_area(float(spec.get("line_min_area_m2") or
                                    GM.default_sizes()["min_area_m2"]))
    idx, z, hinfo = AC.hold_crest(P1, cnw, wp, prof, hmin + o["crest_half_width_m"], ko)
    vz2 = vz1.copy()
    vz2[idx] = np.maximum(vz2[idx], z)
    after = pour(P1, T1, vz2, POND, EAST)
    print("crest hold in the window: {}".format(hinfo))
    print("raised: {} nodes, mean {:.2f} ft, max {:.2f} ft".format(
        int((vz2 > vz1 + 1e-9).sum()), float(np.nanmean((vz2 - vz1)[idx]) / FT),
        float(np.nanmax((vz2 - vz1)[idx]) / FT) if len(idx) else 0.0))
    print()
    print("pour level W pond {} -> east of Hwy 18 (ft; true 1 m pour 941.51-941.57 ft, "
          "pond max WSE 939.80 ft):".format(POND))
    print("  {:28s} {:>22s} {:>22s}".format("", "centroid / edge", "centroid / edge"))
    print("  {:28s} {:>22s} {:>22s}".format("to", str(EAST[0]), str(EAST[1])))
    for lab, r in (("BEFORE (run mesh)", before), ("AFTER, mesh lines only", after_nohold),
                   ("AFTER, mesh + crest hold", after)):
        print("  {:28s} {:>22s} {:>22s}".format(lab, *["{:.2f} / {:.2f}".format(
            r[d][0][0] / FT, r[d][1][0] / FT) for d in EAST]))
        print("  {:28s} {}".format("  set at (centroid, edge)", [(r[d][0][1], r[d][1][1])
                                                               for d in EAST]))
    print("elapsed {:.0f} s".format(time.time() - t0))


if __name__ == "__main__":
    main()
