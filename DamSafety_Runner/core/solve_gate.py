"""Checks that must pass before a flood solve starts.

A failure here is cheaper than a solver that dies an hour in. The same
functions are used by the screener and the runner. None of them import QGIS.
"""
import csv
import os
import shutil
import subprocess
import tempfile

CFS = 35.31466672148859
ACFT = 1233.48183754752
# A 10-mile box at 10 m is about 3,200 pixels. The 3DEP export timed out as one
# tile at that size, so downloads are cut into tiles no wider than this.
TILE_PX = 1200


def check(status, title, detail="", fix=""):
    return {"status": status, "title": title, "detail": detail, "fix": fix}


def one_crs(codes):
    """Fail when the terrain tiles are not all the same CRS."""
    found = []
    for c in codes or []:
        c = str(c or "").strip()
        if c and c not in found:
            found.append(c)
    if not found:
        return check("fail", "One CRS", "no CRS on the terrain",
                     "Warp the DEM to one work CRS before the solve.")
    if len(found) > 1:
        return check("fail", "One CRS", "mixed CRS: {}".format(", ".join(found)),
                     "Warp every tile to one CRS. Do not mix feet grids with metre tiles.")
    return check("ok", "One CRS", found[0])


def dem_covers(origin, shape, xy, nodata_fraction=None):
    """The dam point must fall on the DEM, and at least half the clip must have data."""
    if origin is None or shape is None or xy is None:
        return check("fail", "DEM covers the dam", "no DEM extent",
                     "Point the run at the terrain tile that contains the dam.")
    x0, y0, dx, dy = origin
    ny, nx = shape
    x1, y1 = x0 + nx * dx, y0 + ny * dy
    x, y = xy
    inside = (min(x0, x1) <= x <= max(x0, x1)) and (min(y0, y1) <= y <= max(y0, y1))
    if not inside:
        return check("fail", "DEM covers the dam",
                     "dam {:.0f},{:.0f} is outside the DEM".format(x, y),
                     "Download or clip terrain that contains the dam.")
    if nodata_fraction is not None and nodata_fraction > 0.5:
        return check("fail", "DEM covers the dam",
                     "less than half of the clip has data ({:.0%} empty)".format(nodata_fraction),
                     "The dam is on the edge of the tile. Use a clip that covers the valley.")
    return check("ok", "DEM covers the dam", "the dam is on the terrain")


def hydro_stats(times_s, discharge_m3s):
    """Peak and volume of a breach hydrograph. A missing or empty curve fails."""
    t = [] if times_s is None else list(times_s)
    q = [] if discharge_m3s is None else list(discharge_m3s)
    if len(t) < 2 or len(q) < 2 or len(t) != len(q):
        return check("fail", "Hydrograph", "no hydrograph",
                     "Run the breach hydrograph (tool 5) before the flood.")
    peak_i = max(range(len(q)), key=lambda i: float(q[i]))
    peak = float(q[peak_i])
    vol = 0.0
    for i in range(1, len(t)):
        dt = float(t[i]) - float(t[i - 1])
        vol += 0.5 * (float(q[i]) + float(q[i - 1])) * dt
    if peak <= 0 or vol <= 0:
        return check("fail", "Hydrograph", "peak or volume is zero",
                     "The curve is empty. Check the storage and the breach parameters.")
    n = len(t)
    straight = n <= 2
    detail = "peak {:.0f} cfs at {:.2f} h, volume {:.0f} ac-ft, {} points".format(
        peak * CFS, float(t[peak_i]) / 3600.0, vol / ACFT, n)
    if straight:
        return check("warn", "Hydrograph", detail + " (a straight line, not a DEM stage-storage curve)",
                     "Estimate stage-storage from the DEM. A line from empty to full is not a reservoir.")
    return check("ok", "Hydrograph", detail)


def hydro_stats_csv(path):
    times, flows = [], []
    with open(path, newline="", encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            key_t = "time_s" if "time_s" in row else "t"
            key_q = "discharge_m3s" if "discharge_m3s" in row else "q"
            times.append(float(row[key_t]))
            flows.append(float(row[key_q]))
    return hydro_stats(times, flows)


def structures_in_extent(points, extent):
    """Points are (x, y) in the study CRS. An empty hit when points were given is an
    extent mismatch, not a missing CRS."""
    xmin, ymin, xmax, ymax = extent
    pts = list(points or [])
    if not pts:
        return check("ok", "Structures in the study extent", "no structure layer")
    inside = 0
    for x, y in pts:
        if xmin <= x <= xmax and ymin <= y <= ymax:
            inside += 1
    outside = len(pts) - inside
    if inside == 0:
        return check("fail", "Structures in the study extent",
                     "all {} structure(s) are outside the study extent (they have coordinates; "
                     "this is an extent mismatch, not a missing CRS)".format(len(pts)),
                     "Use the structures that fall in this dam's flood extent.")
    if outside:
        return check("warn", "Structures in the study extent",
                     "{} inside, {} outside".format(inside, outside))
    return check("ok", "Structures in the study extent", "{} inside".format(inside))


def storage_check(dem_m3, inventory_m3, tol=0.5):
    """Compare a DEM stage-storage volume with an inventory volume. The DEM curve is kept."""
    if inventory_m3 is None:
        return ""
    try:
        inv = float(inventory_m3)
        dem = float(dem_m3)
    except (TypeError, ValueError):
        return ""
    if dem <= 0:
        return "DEM stage-storage is empty; inventory says {:.0f} ac-ft. The curve is not a straight line.".format(
            inv / ACFT)
    if abs(inv / dem - 1.0) > tol:
        return ("inventory {:.0f} ac-ft differs from the DEM curve {:.0f} ac-ft; "
                "the curve stays the DEM estimate, not a line from empty to full".format(
                    inv / ACFT, dem / ACFT))
    return "inventory {:.0f} ac-ft agrees with the DEM curve {:.0f} ac-ft".format(inv / ACFT, dem / ACFT)


_SMOKE = {}


def lisflood_smoke(exe, work=None):
    """Run LISFLOOD-FP for one second on a 5 by 5 grid. Proves the binary starts.

    A successful check is remembered for this process (same file, same mtime).
    Set ND_DAM_PRE_RUN_SMOKE=0 to skip it. The built-in engine is never smoked.
    """
    if str(os.environ.get("ND_DAM_PRE_RUN_SMOKE", "1")).strip().lower() in ("0", "false", "no", "off"):
        return check("ok", "LISFLOOD 1-second check", "skipped (ND_DAM_PRE_RUN_SMOKE=0)")
    exe = str(exe or "").strip()
    if not exe or not os.path.isfile(exe):
        return check("fail", "LISFLOOD 1-second check", "executable not found: {}".format(exe or "(none)"),
                     "Give the real lisflood file, not a symlink the file picker will reject.")
    try:
        real = os.path.realpath(exe)
        stamp = os.path.getmtime(real)
    except OSError:
        real, stamp = exe, None
    cached = _SMOKE.get(real)
    if cached and cached[0] == stamp:
        return cached[1]
    folder = work or tempfile.mkdtemp(prefix="lf1s_")
    own = work is None
    try:
        os.makedirs(os.path.join(folder, "out"), exist_ok=True)
        dem = os.path.join(folder, "dem.asc")
        with open(dem, "w", encoding="utf-8") as fh:
            fh.write("ncols 5\nnrows 5\nxllcorner 0\nyllcorner 0\ncellsize 10\nNODATA_value -9999\n")
            fh.write("\n".join(" ".join(["100"] * 5) for _ in range(5)) + "\n")
        par = os.path.join(folder, "run.par")
        with open(par, "w", encoding="utf-8") as fh:
            fh.write("DEMfile dem.asc\nresroot run\ndirroot out\nsim_time 1\ninitial_tstep 1\n"
                     "saveint 1\nmassint 1\nfpfric 0.05\nacceleration\n")
        try:
            r = subprocess.run([exe, par], cwd=folder, capture_output=True, text=True, timeout=60, errors="replace")
        except subprocess.TimeoutExpired:
            return check("fail", "LISFLOOD 1-second check", "no answer in 60 s",
                         "The binary did not finish a 5 by 5 grid. Do not start the dam.")
        max_file = os.path.join(folder, "out", "run.max")
        if r.returncode != 0 or not os.path.isfile(max_file):
            err = (r.stderr or r.stdout or "").strip().splitlines()
            detail = "exit {}: {}".format(r.returncode, err[-1] if err else "no message")
            if r.returncode == 0:
                detail = "ran but wrote no out/run.max"
            return check("fail", "LISFLOOD 1-second check", detail,
                         "Fix the executable before the solve.")
        hit = check("ok", "LISFLOOD 1-second check", "the binary ran for 1 s on a 5 by 5 grid")
        _SMOKE[real] = (stamp, hit)
        return hit
    finally:
        if own:
            shutil.rmtree(folder, ignore_errors=True)


def _epsg_text(wkt):
    import re
    text = str(wkt or "")
    codes = re.findall(r'(?:ID|AUTHORITY)\["EPSG",\s*"?(\d+)"?\]', text)
    if codes:
        return "EPSG:" + codes[-1]
    bare = re.search(r"EPSG[:\s\"]+(\d+)", text, re.I)
    return "EPSG:" + bare.group(1) if bare else ""


def _extent_from_gt(gt, nx, ny):
    x0, dx, _rx, y0, _ry, dy = gt
    xs = (x0, x0 + dx * nx)
    ys = (y0, y0 + dy * ny)
    return (min(xs), min(ys), max(xs), max(ys))


def raster_info(path):
    """EPSG code and (xmin, ymin, xmax, ymax). gdalinfo first, then the GDAL Python bindings.

    The self-test and a QGIS that has no gdalinfo on PATH still need a CRS and an extent.
    """
    import json
    try:
        r = subprocess.run(["gdalinfo", "-json", path], capture_output=True, text=True, timeout=30, errors="replace")
        info = json.loads(r.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        info = {}
    wkt = (info.get("coordinateSystem") or {}).get("wkt") or ""
    epsg = _epsg_text(wkt)
    cc = info.get("cornerCoordinates") or {}
    ul, lr = cc.get("upperLeft"), cc.get("lowerRight")
    if ul and lr:
        xs = (ul[0], lr[0])
        ys = (ul[1], lr[1])
        return {"epsg": epsg, "extent": (min(xs), min(ys), max(xs), max(ys))}
    try:
        from osgeo import gdal
        ds = gdal.Open(path)
    except Exception:
        ds = None
    if ds is None:
        return {"epsg": epsg, "extent": None}
    try:
        epsg = epsg or _epsg_text(ds.GetProjection() or "")
        gt = ds.GetGeoTransform()
        extent = _extent_from_gt(gt, ds.RasterXSize, ds.RasterYSize) if gt else None
    except Exception:
        return {"epsg": epsg, "extent": None}
    return {"epsg": epsg, "extent": extent}


def dem_covers_files(paths, xy):
    """The dam point is inside one tile, and every tile shares one CRS."""
    infos = [raster_info(p) for p in paths or []]
    crs = one_crs([i["epsg"] for i in infos])
    if crs["status"] == "fail":
        return crs
    if xy is None:
        return check("fail", "DEM covers the dam", "no dam point",
                     "The breach point is missing.")
    x, y = xy
    for info in infos:
        ext = info["extent"]
        if ext and ext[0] <= x <= ext[2] and ext[1] <= y <= ext[3]:
            return check("ok", "DEM covers the dam", "the dam is inside the {} terrain".format(crs["detail"]))
    return check("fail", "DEM covers the dam",
                 "dam {:.0f},{:.0f} is outside every terrain tile".format(x, y),
                 "Download or clip terrain that contains the dam.")


def union_extent(paths):
    boxes = [raster_info(p)["extent"] for p in paths or []]
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def raise_on_fail(checks):
    fails = [c for c in checks if c["status"] == "fail"]
    if not fails:
        return
    lines = ["{}: {}".format(c["title"], c["detail"]) for c in fails]
    raise RuntimeError("Preflight failed before the solve. " + " | ".join(lines))
