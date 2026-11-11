"""Where roads and railroads cross streams: the places a culvert or a bridge must be (Finder 0.1).

Pure Python. Every crossing of a road / rail centreline with a stream line is a crossing; the
LiDAR shows the fill but not the pipe under it. Each crossing is then checked against the
structure lists the engineer gives (NBI bridges and culverts, NDDOT bridge inventory and
centre-line pipes, USGS / SARP stream crossings; core/listed.py): a crossing with no listed
structure near it is a culvert nobody has on a list.

Segments are put in a bucket grid so a county of roads against a county of streams is not every
pair. Crossings closer than `dedupe_m` (a divided road, a line digitised twice) are one.

EXPECTED PIPE SIZE (with a DEM): the drainage area above each crossing and its longest flow path
(core/terrain.py), the Kirpich time of concentration, the rational-method peak flow Q = C i A for
YOUR runoff coefficient C and rainfall intensity i (NOAA Atlas 14 at that duration and the design
return period), and the round pipe that carries Q flowing full at YOUR velocity. An order of
magnitude for screening - the rational method is meant for small areas (about 200 acres); above
that use a regional regression (USGS StreamStats) - not a hydraulic design.
"""
import math


def _cross(a, b, c, d):
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    t = ((cx - ax) * sy - (cy - ay) * sx) / den
    u = ((cx - ax) * ry - (cy - ay) * rx) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return ax + t * rx, ay + t * ry
    return None


def _segments(lines):
    for li, line in enumerate(lines):
        for k in range(len(line) - 1):
            a, b = line[k], line[k + 1]
            if a != b:
                yield li, (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))


def find(roads, streams, bucket_m=250.0, dedupe_m=15.0):
    """[{'x', 'y', 'road', 'stream', 'angle'}]: every crossing of a road line with a stream line
    (both lists of [(x, y), ...] in one projected CRS, metres). `road` / `stream` are the line
    indices; `angle` the crossing angle in degrees (90 = square)."""
    grid = {}
    for si, a, b in _segments(streams):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        for i in range(int(math.floor(x0 / bucket_m)), int(math.floor(x1 / bucket_m)) + 1):
            for j in range(int(math.floor(y0 / bucket_m)), int(math.floor(y1 / bucket_m)) + 1):
                grid.setdefault((i, j), []).append((si, a, b))
    out = []
    for ri, a, b in _segments(roads):
        x0, x1 = sorted((a[0], b[0]))
        y0, y1 = sorted((a[1], b[1]))
        seen = set()
        for i in range(int(math.floor(x0 / bucket_m)), int(math.floor(x1 / bucket_m)) + 1):
            for j in range(int(math.floor(y0 / bucket_m)), int(math.floor(y1 / bucket_m)) + 1):
                for si, c, d in grid.get((i, j), ()):
                    key = (si, c, d)
                    if key in seen:
                        continue
                    seen.add(key)
                    hit = _cross(a, b, c, d)
                    if hit is None:
                        continue
                    v1 = (b[0] - a[0], b[1] - a[1])
                    v2 = (d[0] - c[0], d[1] - c[1])
                    cosv = abs(v1[0] * v2[0] + v1[1] * v2[1]) / (math.hypot(*v1) * math.hypot(*v2))
                    ang = math.degrees(math.acos(min(1.0, cosv)))
                    out.append({"x": hit[0], "y": hit[1], "road": ri, "stream": si,
                                "angle": round(ang, 1)})
    return dedupe(out, dedupe_m)


def dedupe(rows, dist_m):
    keep = []
    grid = {}
    for r in rows:
        bx, by = int(math.floor(r["x"] / dist_m)), int(math.floor(r["y"] / dist_m))
        dup = False
        for i in (bx - 1, bx, bx + 1):
            for j in (by - 1, by, by + 1):
                for o in grid.get((i, j), ()):
                    if math.hypot(o["x"] - r["x"], o["y"] - r["y"]) <= dist_m:
                        dup = True
                        break
        if not dup:
            keep.append(r)
            grid.setdefault((bx, by), []).append(r)
    return keep


# --------------------------------------------------------------------- expected pipe size --
PIPE_SIZES_IN = (18, 24, 30, 36, 42, 48, 54, 60, 66, 72, 84, 96, 108, 120)


def kirpich_tc_min(length_ft, slope):
    """Kirpich (1940) time of concentration, minutes: 0.0078 L^0.77 S^-0.385 (L ft, S ft/ft)."""
    s = max(float(slope), 1e-4)
    return 0.0078 * max(float(length_ft), 1.0) ** 0.77 * s ** -0.385


def rational_cfs(c, intensity_in_hr, area_ac):
    """Rational-method peak flow Q = C i A (cfs, with i in in/hr and A in acres)."""
    return float(c) * float(intensity_in_hr) * float(area_ac)


def pipe_for(q_cfs, v_fps):
    """(diameter needed to carry q_cfs flowing full at v_fps, inches; the next standard round pipe
    size, or None when even the largest is too small: a box culvert or a bridge)."""
    d_in = 12.0 * math.sqrt(4.0 * max(q_cfs, 0.0) / (math.pi * max(v_fps, 0.1)))
    return d_in, next((s for s in PIPE_SIZES_IN if s >= d_in), None)
