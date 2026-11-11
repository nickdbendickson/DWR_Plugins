"""Where roads and railroads cross streams: the places a culvert or a bridge must be (Finder 0.1).

Pure Python. Every crossing of a road / rail centreline with a stream line is a crossing; the
LiDAR shows the fill but not the pipe under it. Each crossing is then checked against the
structure lists the engineer gives (NBI bridges and culverts, NDDOT bridge inventory and
centre-line pipes, USGS / SARP stream crossings; core/listed.py): a crossing with no listed
structure near it is a culvert nobody has on a list.

Segments are put in a bucket grid so a county of roads against a county of streams is not every
pair. Crossings closer than `dedupe_m` (a divided road, a line digitised twice) are one.
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
