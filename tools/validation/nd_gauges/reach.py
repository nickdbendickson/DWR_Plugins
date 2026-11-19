#!/usr/bin/env python3
"""Manning's n against measured flood stages at North Dakota streamgages.

    <QGIS python> tools/validation/nd_gauges/reach.py build GAUGE_ID --out FOLDER
    <QGIS python> tools/validation/nd_gauges/reach.py run FOLDER --n 0.04 [--lisflood EXE]
    <QGIS python> tools/validation/nd_gauges/reach.py run FOLDER --nlcd
    <QGIS python> tools/validation/nd_gauges/reach.py score ROOT        (every reach under ROOT)

Each gauge in gauges.json has one open-water flood measurement: a discharge and the gage height
read with it. `build` makes a reach around the gauge: the river line from the USGS National
Hydrography Dataset (5 km up, 12 km down), the USGS 3DEP terrain at 1 m averaged to 10 m model
cells, and NLCD land cover. `run` puts the measured discharge in at the top of the reach,
steady, and runs LISFLOOD-FP through the Runner's own LISFLOOD-FP code (core/lisflood.py: the
same point inflow, free edges and acceleration solver as a dam run) until the reach is full.
`score` reads the highest water surface at the gauge and compares it with the measured stage.

It tests the roughness alone: no breach, no reservoir, no inflow estimate. The model has no
bridges and no channel bed below the water surface of the day the LiDAR was flown."""
import argparse
import concurrent.futures as cf
import glob
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, REPO)

FT = 0.3048
CFS = FT ** 3
EPSG = 26914                       # UTM 14N, NAD83: the DWR convention for the whole state
CELL = 10.0                        # model cell, m
SRC_CELL = 1.0                     # terrain cell asked of 3DEP, m (averaged to CELL)
TILE_M = 2000.0                    # one 3DEP request: 2000 x 2000 cells of 1 m
UP_M, DOWN_M, BUFFER_M = 5000.0, 12000.0, 2500.0
READ_RADIUS_M = 100.0
CONV_TOL = 0.1                     # |outflow/inflow - 1| within this = a steady run; others non-converged
NHD ="https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6/query"
DEP = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer/exportImage"
NLCD_WCS = "https://www.mrlc.gov/geoserver/mrlc_download/wcs"
UA = {"User-Agent": "DamSafety-suite"}


def gauges():
    with open(os.path.join(HERE, "gauges.json"), encoding="utf-8") as f:
        return {g["id"]: g for g in json.load(f)["gauges"]}


def fetch(url, timeout=180, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:     # a real 4xx won't improve on retry
                raise
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))
        except OSError:                            # timeout, reset, DNS, URLError
            if k == tries - 1:
                raise
            time.sleep(5 * (k + 1))


def _ct(src, dst):
    from osgeo import osr
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromEPSG(src)
    b.ImportFromEPSG(dst)
    a.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    b.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return osr.CoordinateTransformation(a, b)


def to_utm(lon, lat):
    return _ct(4326, EPSG).TransformPoint(lon, lat)[:2]


# ------------------------------------------------------------------------ the river line
def _nhd_box(river, box, timeout=180):
    """All flowlines of `river` intersecting one lon/lat envelope, paginated, in EPSG:26914.
    Raises OSError (through fetch) if the service times out so the caller can tile the box."""
    parts, offset = [], 0
    while True:
        q = {"where": "gnis_name = '{}'".format(river), "geometry": "{},{},{},{}".format(*box),
             "geometryType": "esriGeometryEnvelope", "inSR": 4326, "outSR": EPSG,
             "spatialRel": "esriSpatialRelIntersects", "outFields": "gnis_name",
             "returnGeometry": "true", "maxAllowableOffset": 5, "resultOffset": offset,
             "resultRecordCount": 200, "f": "json"}
        d = json.loads(fetch(NHD + "?" + urllib.parse.urlencode(q), timeout=timeout).decode("utf-8"))
        if "error" in d:
            raise SystemExit("NHD: {}".format(d["error"]))
        for ft in d.get("features", []):
            for path in ft["geometry"]["paths"]:
                parts.append(np.asarray(path, float)[:, :2])
        if not d.get("exceededTransferLimit"):
            return parts
        offset += len(d.get("features", []))


def _dedupe(parts):
    """Drop a flowline returned by more than one tile (one feature intersects both)."""
    seen, out = set(), []
    for p in parts:
        k = (round(p[0, 0]), round(p[0, 1]), round(p[-1, 0]), round(p[-1, 1]), len(p))
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


def nhd_parts(g, half_deg=0.22, say=print):
    """The named river's NHD flowlines within about 30 km of the gauge, in EPSG:26914.

    One envelope query first. A long river makes a heavy gnis_name query that the service can
    time out on (504); when that happens, split the envelope into a 3x3 grid of smaller boxes
    and union the lines, so no single query has to cover the whole river."""
    box = (g["lon"] - half_deg, g["lat"] - half_deg * 0.7, g["lon"] + half_deg, g["lat"] + half_deg * 0.7)
    try:
        parts = _nhd_box(g["river"], box)
    except OSError as e:                            # 5xx/timeout after retries -> tile the box
        say("NHD whole-box query failed ({}); tiling the envelope".format(e))
        x0, y0, x1, y1 = box
        xs, ys, parts, failed = np.linspace(x0, x1, 4), np.linspace(y0, y1, 4), [], 0
        for i in range(3):
            for j in range(3):
                try:
                    parts += _nhd_box(g["river"], (xs[i], ys[j], xs[i + 1], ys[j + 1]))
                except OSError as te:
                    failed += 1
                    say("  NHD tile {},{} failed: {}".format(i, j, te))
        if failed:
            say("  NHD: {} of 9 tiles failed".format(failed))
        parts = _dedupe(parts)
    if not parts:
        raise SystemExit("NHD has no '{}' near {}".format(g["river"], g["id"]))
    return parts


def chain(parts, gxy):
    """One downstream-ordered polyline through the part nearest the gauge (NHD lines run
    downstream; at a split the longer branch is kept)."""
    key = lambda p: (round(p[0]), round(p[1]))
    starts, ends = {}, {}
    for i, p in enumerate(parts):
        starts.setdefault(key(p[0]), []).append(i)
        ends.setdefault(key(p[-1]), []).append(i)
    dist = [np.min(np.hypot(p[:, 0] - gxy[0], p[:, 1] - gxy[1])) for p in parts]
    i0 = int(np.argmin(dist))
    if dist[i0] > 500:
        raise SystemExit("the river line is {:.0f} m from the gauge".format(dist[i0]))
    line, used = [parts[i0]], {i0}
    cur = key(parts[i0][0])
    while True:                                   # upstream
        nxt = [i for i in ends.get(cur, []) if i not in used]
        if not nxt:
            break
        i = max(nxt, key=lambda j: len(parts[j]))
        used.add(i)
        line.insert(0, parts[i][:-1])
        cur = key(parts[i][0])
    cur = key(parts[i0][-1])
    while True:                                   # downstream
        nxt = [i for i in starts.get(cur, []) if i not in used]
        if not nxt:
            break
        i = max(nxt, key=lambda j: len(parts[j]))
        used.add(i)
        line.append(parts[i][1:])
        cur = key(parts[i][-1])
    return np.vstack(line)


def chainage(line):
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(line, axis=0).T))]


def cut(line, gxy, up=UP_M, down=DOWN_M):
    s = chainage(line)
    k = int(np.argmin(np.hypot(line[:, 0] - gxy[0], line[:, 1] - gxy[1])))
    sg = s[k]
    a, b = max(0.0, sg - up), min(s[-1], sg + down)
    keep = (s >= a) & (s <= b)
    seg = line[keep]
    return seg, sg - a, b - sg


# ---------------------------------------------------------------------------- terrain
def _tile(t, folder):
    """One 3DEP tile at 1 m, averaged to 10 m; cached as .npy."""
    from osgeo import gdal
    gdal.UseExceptions()
    x0, y0 = t
    path = os.path.join(folder, "t_{:.0f}_{:.0f}.npy".format(x0, y0))
    if os.path.isfile(path):
        return t, np.load(path)
    n = int(TILE_M / SRC_CELL)
    q = {"bbox": "{},{},{},{}".format(x0, y0, x0 + TILE_M, y0 + TILE_M), "bboxSR": EPSG,
         "imageSR": EPSG, "size": "{},{}".format(n, n), "format": "tiff", "pixelType": "F32",
         "interpolation": "RSP_NearestNeighbor", "noData": -9999, "f": "image"}
    for k in range(4):
        raw = fetch(DEP + "?" + urllib.parse.urlencode(q), timeout=300)
        if raw[:4] in (b"II*\x00", b"MM\x00*"):
            break
        time.sleep(5 * (k + 1))
    else:
        raise SystemExit("3DEP did not send a picture for tile {}".format(t))
    mem = "/vsimem/t_{:.0f}_{:.0f}.tif".format(x0, y0)
    gdal.FileFromMemBuffer(mem, raw)
    a = gdal.Open(mem).ReadAsArray().astype(np.float64)
    gdal.Unlink(mem)
    a[(a < -1000) | (a > 9000)] = np.nan
    f = int(CELL / SRC_CELL)
    b = a.reshape(n // f, f, n // f, f)
    with np.errstate(all="ignore"):
        m = np.nanmean(b, axis=(1, 3))
        m[np.isnan(b).sum(axis=(1, 3)) > f * f // 2] = np.nan     # more than half missing
    np.save(path, m.astype(np.float32))
    return t, m.astype(np.float32)


def terrain(box, folder, workers=4, say=print):
    """The 10 m mean of 1 m 3DEP over box (snapped to whole tiles)."""
    x0 = math.floor(box[0] / TILE_M) * TILE_M
    y0 = math.floor(box[1] / TILE_M) * TILE_M
    x1 = math.ceil(box[2] / TILE_M) * TILE_M
    y1 = math.ceil(box[3] / TILE_M) * TILE_M
    nt_x, nt_y = int((x1 - x0) / TILE_M), int((y1 - y0) / TILE_M)
    m = int(TILE_M / CELL)
    out = np.full((nt_y * m, nt_x * m), np.nan, np.float32)
    tiles = [(x0 + i * TILE_M, y0 + j * TILE_M) for j in range(nt_y) for i in range(nt_x)]
    os.makedirs(folder, exist_ok=True)
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        for (tx, ty), a in ex.map(lambda t: _tile(t, folder), tiles):
            i, j = int((tx - x0) / TILE_M), int((ty - y0) / TILE_M)
            out[(nt_y - 1 - j) * m:(nt_y - j) * m, i * m:(i + 1) * m] = a
            done += 1
            if done % 10 == 0 or done == len(tiles):
                say("terrain: {} of {} tiles".format(done, len(tiles)))
    return out, (x0, y1)


def nlcd_codes(box, year, shape, origin):
    """NLCD classes of `year` on the 10 m model grid (nearest)."""
    from osgeo import gdal
    gdal.UseExceptions()
    ct = _ct(EPSG, 5070)
    xs = [box[0], box[2], box[0], box[2]]
    ys = [box[1], box[1], box[3], box[3]]
    pts = [ct.TransformPoint(x, y)[:2] for x, y in zip(xs, ys)]
    # pad well past the box: the 5070 -> 26914 rotation pulls the model grid's corners
    # outside a tight subset, and GDAL would fill those cells with 0 (then the default n)
    lo = [15 + 30 * math.floor((min(p[k] for p in pts) - 15) / 30) - 2000 for k in (0, 1)]
    hi = [15 + 30 * math.ceil((max(p[k] for p in pts) - 15) / 30) + 2000 for k in (0, 1)]
    q = [("service", "WCS"), ("request", "GetCoverage"), ("version", "2.0.1"),
         ("coverageId", "mrlc_download__NLCD_{}_Land_Cover_L48".format(int(year))),
         ("subset", "X({:.0f},{:.0f})".format(lo[0], hi[0])),
         ("subset", "Y({:.0f},{:.0f})".format(lo[1], hi[1])), ("format", "image/geotiff")]
    raw = fetch(NLCD_WCS + "?" + urllib.parse.urlencode(q))
    gdal.FileFromMemBuffer("/vsimem/nlcd.tif", raw)
    ny, nx = shape
    ds = gdal.Warp("", "/vsimem/nlcd.tif", format="MEM", dstSRS="EPSG:{}".format(EPSG),
                   outputBounds=(origin[0], origin[1] - ny * CELL, origin[0] + nx * CELL, origin[1]),
                   xRes=CELL, yRes=CELL, resampleAlg="near")
    a = ds.ReadAsArray()
    gdal.Unlink("/vsimem/nlcd.tif")
    return a


# ------------------------------------------------------------------------------ build
def build(gid, out, say=print, gauge=None):
    from DamSafety_Runner.core.grid import Grid
    g = gauge if gauge is not None else gauges()[gid]
    os.makedirs(out, exist_ok=True)
    gxy = to_utm(g["lon"], g["lat"])
    line = chain(nhd_parts(g, say=say), gxy)
    seg, up, down = cut(line, gxy)
    say("{}: river line {:.1f} km up, {:.1f} km down".format(gid, up / 1000, down / 1000))
    box = (seg[:, 0].min() - BUFFER_M, seg[:, 1].min() - BUFFER_M,
           seg[:, 0].max() + BUFFER_M, seg[:, 1].max() + BUFFER_M)
    z, origin = terrain(box, os.path.join(out, "tiles"), say=say)
    dem = Grid(np.where(np.isfinite(z), z, -9999.0), origin[0], origin[1], CELL)
    dem.write_asc(os.path.join(out, "dem.asc"), fmt="%.3f")
    year = {2011: 2011, 2013: 2013}.get(int(g["measured"]["time_utc"][:4]), 2019)
    codes = nlcd_codes(box, year, dem.data.shape, origin)
    np.save(os.path.join(out, "nlcd.npy"), codes.astype(np.uint8))
    # the inflow: a line 150 m across the river, 200 m below the top of the reach
    s = chainage(seg)
    k = int(np.searchsorted(s, 200.0))
    d = seg[min(k + 1, len(seg) - 1)] - seg[max(k - 1, 0)]
    d = d / (np.hypot(*d) or 1.0)
    nrm = np.array([-d[1], d[0]])
    p = seg[k]
    inflow = {"xy": [float(p[0]), float(p[1])],
              "line": [[float(v) for v in p - 75 * nrm], [float(v) for v in p + 75 * nrm]]}
    rc = dem.rc(*gxy)
    near = dem.data[max(rc[0] - 10, 0):rc[0] + 11, max(rc[1] - 10, 0):rc[1] + 11]
    lowest_ft = float(np.min(near[near > -9000])) / FT
    meta = {"gauge": g, "gauge_xy": list(gxy), "up_m": up, "down_m": down, "box": list(box),
            "origin": list(origin), "shape": list(dem.data.shape), "cell_m": CELL,
            "terrain": "USGS 3DEP 1 m (exportImage, nearest), mean of each 10 m cell",
            "nlcd_year": year, "inflow": inflow, "river_line": seg.round(1).tolist(),
            "lowest_terrain_within_100m_ft": round(lowest_ft, 2),
            "measured_stage_ft": round(g["datum_ft"] + g["measured"]["gage_height_ft"], 2)}
    with open(os.path.join(out, "reach.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    say("{}: {} x {} cells; lowest ground within 100 m of the gauge {:.2f} ft, gage datum "
        "{:.2f} ft, measured stage {:.2f} ft".format(gid, dem.nx, dem.ny, lowest_ft,
                                                  g["datum_ft"], meta["measured_stage_ft"]))
    return meta


# -------------------------------------------------------------------------------- run
def run(folder, n=None, nlcd=False, exe=None, threads=1, sim_hr=None, say=print):
    from DamSafety_Runner.core import lisflood as lf
    from DamSafety_Runner.core import nd_data
    from DamSafety_Runner.core.breach import Hydrograph
    from DamSafety_Runner.core.grid import Grid
    with open(os.path.join(folder, "reach.json"), encoding="utf-8") as f:
        meta = json.load(f)
    dem = Grid.read_asc(os.path.join(folder, "dem.asc"))
    tag = "nlcd" if nlcd else "n{:.3f}".format(n)
    man = None
    if nlcd:
        codes = np.load(os.path.join(folder, "nlcd.npy")).astype(float)
        man = dem.like(nd_data.nlcd_to_manning(codes, nd_data.manning_lut(default=0.06)))
    q = meta["gauge"]["measured"]["q_cfs"] * CFS
    length = meta["up_m"] + meta["down_m"]
    sim_s = (sim_hr or max(24.0, length / 1000.0 * 1.6)) * 3600.0
    hyd = Hydrograph(t_s=np.array([0.0, sim_s + 3600.0]), q_cms=np.array([q, q]), method="steady")
    st = lf.LisfloodSettings(exe or os.environ.get("LISFLOOD_EXE", "lisflood"), threads,
                             manning_n=n if n else 0.06, manning_grid=man, sim_time_s=sim_s)
    wd = os.path.join(folder, "runs", tag)
    line = [tuple(p) for p in meta["inflow"]["line"]]
    par = lf.write_inputs(wd, dem, tuple(meta["inflow"]["xy"]), hyd, st, inflow_line=line)
    t0 = time.time()
    lf.run(wd, par, st, log=lambda m: None)
    hmax = Grid.read_asc(os.path.join(wd, "results", "run.max")).data
    wse = np.where(hmax > 0.05, dem.data + hmax, np.nan)
    out = {"tag": tag, "n": n, "nlcd": bool(nlcd), "q_cms": q, "sim_hr": sim_s / 3600.0,
           "wall_s": round(time.time() - t0, 1)}
    out.update(read_gauge(meta, dem, wse))
    out.update(balance(os.path.join(wd, "results", "run.mass")))
    with open(os.path.join(wd, "result.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    g = lambda k: out[k] if out.get(k) is not None else float("nan")
    say("{} {}: model {:.2f} ft, measured {:.2f} ft, difference {:+.2f} ft; outflow/inflow at the "
        "end {:.3f} ({:.0f} s)".format(meta["gauge"]["id"], tag, g("model_ft"),
                                     meta["measured_stage_ft"], g("diff_ft"),
                                     g("qout_over_qin"), out["wall_s"]))
    return out


def read_gauge(meta, dem, wse):
    gx, gy = meta["gauge_xy"]
    ys = dem.y0 - (np.arange(dem.ny) + 0.5) * dem.dx
    xs = dem.x0 + (np.arange(dem.nx) + 0.5) * dem.dx
    r = np.where(np.abs(ys - gy) <= READ_RADIUS_M)[0]
    c = np.where(np.abs(xs - gx) <= READ_RADIUS_M)[0]
    sub = wse[np.ix_(r, c)]
    xx, yy = np.meshgrid(xs[c], ys[r])
    near = sub[(np.hypot(xx - gx, yy - gy) <= READ_RADIUS_M) & np.isfinite(sub)]
    if not near.size:
        return {"model_ft": None, "diff_ft": None, "wet_cells_read": 0}
    m = float(np.median(near)) / FT
    return {"model_ft": round(m, 2), "diff_ft": round(m - meta["measured_stage_ft"], 2),
            "wet_cells_read": int(near.size), "spread_ft": round(float(np.ptp(near)) / FT, 2)}


def balance(mass):
    """Outflow over inflow over the last tenth of the run (1.0 = steady)."""
    try:
        with open(mass, encoding="utf-8") as f:
            head = f.readline().split()
            rows = [list(map(float, l.split())) for l in f if l.strip()]
        a = np.asarray(rows)
        qi, qo = a[:, head.index("Qin")], a[:, head.index("Qout")]
        tail = a[:, 0] >= a[-1, 0] * 0.9
        return {"qout_over_qin": round(float(qo[tail].mean() / max(qi[tail].mean(), 1e-9)), 3)}
    except (OSError, ValueError, IndexError):
        return {}


# ------------------------------------------------------------------------------ score
def score(root, say=print):
    rows = []
    for rj in sorted(glob.glob(os.path.join(root, "*", "reach.json"))):
        with open(rj, encoding="utf-8") as f:
            meta = json.load(f)
        for res in sorted(glob.glob(os.path.join(os.path.dirname(rj), "runs", "*", "result.json"))):
            with open(res, encoding="utf-8") as f:
                r = json.load(f)
            rows.append((meta["gauge"]["name"], meta["gauge"]["measured"]["q_cfs"], r["tag"],
                         r.get("diff_ft"), r.get("qout_over_qin")))
    tags = sorted({r[2] for r in rows}, key=lambda t: (t == "nlcd", t))
    names = []
    for r in rows:
        if r[0] not in names:
            names.append(r[0])
    steady = lambda oi: oi is not None and abs(oi - 1.0) <= CONV_TOL
    lines = ["# Manning's n at North Dakota streamgages", "",
             "Model minus measured stage, ft, at the measured flood discharge (LISFLOOD-FP, "
             "1 m 3DEP terrain averaged to 10 m cells, steady inflow). Outflow over inflow at the "
             "end of each run in brackets (1.00 = steady). A run that did not reach steady state "
             "(outflow/inflow outside 1.00 +/- 0.10) is shown as n/c with its ratio and left out "
             "of the mean.", "",
             "| Gauge | Measured, cfs | " + " | ".join(tags) + " |",
             "|---|---|" + "---|" * len(tags)]
    for nm in names:
        cells = []
        q = None
        for t in tags:
            hit = [r for r in rows if r[0] == nm and r[2] == t]
            q = q or (hit[0][1] if hit else None)
            if not hit or hit[0][3] is None:
                cells.append("")
            elif steady(hit[0][4]):
                cells.append("{:+.2f} ({:.2f})".format(hit[0][3], hit[0][4]))
            elif hit[0][4] is not None:
                cells.append("n/c ({:.1f}x)".format(hit[0][4]))
            else:
                cells.append("{:+.2f} (?)".format(hit[0][3]))
        lines.append("| {} | {:,.0f} | ".format(nm, q or 0) + " | ".join(cells) + " |")
    means = []
    for t in tags:
        d = [r[3] for r in rows if r[2] == t and r[3] is not None and steady(r[4])]
        means.append("{:+.2f} / {:.2f}".format(np.mean(d), np.mean(np.abs(d))) if d else "")
    lines.append("| **Mean / mean size** | | " + " | ".join(means) + " |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(root, "ND_Gauges_Score.md"), "w", encoding="utf-8") as f:
        f.write(text)
    say(text)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("gauge")
    b.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("folder")
    r.add_argument("--n", type=float, default=None)
    r.add_argument("--nlcd", action="store_true")
    r.add_argument("--lisflood", default=None)
    r.add_argument("--threads", type=int, default=1)
    r.add_argument("--hours", type=float, default=None)
    s = sub.add_parser("score")
    s.add_argument("root")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(a.gauge, a.out)
    elif a.cmd == "run":
        if not a.nlcd and not a.n:
            ap.error("give --n or --nlcd")
        run(a.folder, a.n, a.nlcd, a.lisflood, a.threads, a.hours)
    elif a.cmd == "score":
        score(a.root)
    else:
        print(" ".join(gauges()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
