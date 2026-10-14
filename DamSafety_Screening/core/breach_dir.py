"""Breach location and flow direction from a dam line (embankment centreline).

Pure Python/numpy, planar metric coordinates (EPSG:5070), no QGIS imports.

Logic
  1. Dam axis  = principal direction of the dam line (PCA of its vertices).
  2. Downstream side = the side of the axis AWAY from the reservoir.
       primary : waterbody distance function (0 inside water)
       backup  : terrain (downstream side has lower ground)
     If both are available and disagree, the result is flagged low confidence.
  3. Breach point = the point along the line where the ground at the downstream
     toe is lowest, i.e. where the embankment is tallest (the old channel).
     Falls back to the point on the line nearest the dam's reference point.
  4. Flow direction = the downstream normal of the axis, as a compass bearing.
"""
import math

import numpy as np


# ---------------------------------------------------------------- geometry
def _seg_nearest(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / l2))
    return (ax + t * dx, ay + t * dy)


def polyline_length(v):
    return float(sum(math.dist(v[i], v[i + 1]) for i in range(len(v) - 1)))


def nearest_on_polyline(p, v):
    best = (float("inf"), None)
    for i in range(len(v) - 1):
        q = _seg_nearest(p, v[i], v[i + 1])
        d = math.dist(p, q)
        if d < best[0]:
            best = (d, q)
    return best


def resample(v, step):
    """Points every `step` metres along the polyline (always includes the end)."""
    out, carry = [v[0]], 0.0
    for i in range(len(v) - 1):
        a, b = v[i], v[i + 1]
        seg = math.dist(a, b)
        if seg == 0:
            continue
        s = step - carry
        while s <= seg:
            t = s / seg
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
            s += step
        carry = seg - (s - step)
    if math.dist(out[-1], v[-1]) > 1e-6:
        out.append(v[-1])
    return out


def bearing(dx, dy):
    """Compass bearing (deg clockwise from north) of a vector."""
    return math.degrees(math.atan2(dx, dy)) % 360.0


def axis_direction(v, step=5.0):
    """Unit vector of the principal direction of the line (sign arbitrary)."""
    pts = np.array(resample(v, step))
    c = pts - pts.mean(axis=0)
    w, vec = np.linalg.eigh(np.cov(c.T))
    d = vec[:, int(np.argmax(w))]
    return (float(d[0]), float(d[1]))


def _normals(axis):
    tx, ty = axis
    return (ty, -tx), (-ty, tx)


def _mean_dem(dem_fn, p, n, d0, d1, step=10.0):
    vals = []
    d = d0
    while d <= d1:
        z = dem_fn(p[0] + n[0] * d, p[1] + n[1] * d)
        if z is not None and np.isfinite(z):
            vals.append(z)
        d += step
    return float(np.mean(vals)) if vals else float("nan")


# ----------------------------------------------------------------- main API

def _steepest(dem_fn, origin, step=40.0, reach=200.0):
    """Unit vector of the steepest DEM drop from origin. None if the trace cannot leave."""
    z0 = dem_fn(origin[0], origin[1])
    if not np.isfinite(z0):
        return None
    best, best_drop = None, 0.2
    for i in range(8):
        ang = i * math.pi / 4.0
        ux, uy = math.sin(ang), math.cos(ang)
        z = dem_fn(origin[0] + ux * reach, origin[1] + uy * reach)
        if not np.isfinite(z):
            z = dem_fn(origin[0] + ux * step, origin[1] + uy * step)
            dist = step
        else:
            dist = reach
        if not np.isfinite(z):
            continue
        drop = (z0 - z) / dist
        if drop > best_drop:
            best, best_drop = (ux, uy), drop
    return best


def resolve_breach(line, ref_pt, water_dist_fn=None, dem_fn=None,
                   known_height_m=None, offset=40.0):
    """Returns a dict describing breach location and flow direction.

    line          : [(x, y), ...] dam line vertices
    ref_pt        : (x, y) dam point from the inventory
    water_dist_fn : (x, y) -> distance to reservoir polygon (0 inside)
    dem_fn        : (x, y) -> elevation in metres, or None / nan if unavailable
    """
    notes = []
    length = polyline_length(line)
    axis = axis_direction(line)
    n1, n2 = _normals(axis)
    mid = resample(line, max(length / 2.0, 1.0))[1] if length > 0 else line[0]

    # ---- which normal points downstream -------------------------------
    votes = {}
    if water_dist_fn is not None:
        d1 = water_dist_fn(mid[0] + n1[0] * offset, mid[1] + n1[1] * offset)
        d2 = water_dist_fn(mid[0] + n2[0] * offset, mid[1] + n2[1] * offset)
        if abs(d1 - d2) > 1.0:
            votes["waterbody"] = n1 if d1 > d2 else n2
    if dem_fn is not None:
        z1 = _mean_dem(dem_fn, mid, n1, 60, 300)
        z2 = _mean_dem(dem_fn, mid, n2, 60, 300)
        if np.isfinite(z1) and np.isfinite(z2) and abs(z1 - z2) > 0.2:
            votes["terrain"] = n1 if z1 < z2 else n2

    if not votes and dem_fn is not None:
        down = _steepest(dem_fn, mid)
        if down is not None:
            votes["trace"] = down
            notes.append("downstream from the DEM trace; waterbody and side test were blank")
    if not votes:
        return {"ok": False, "reason": "cannot tell upstream from downstream",
                "length_m": length, "axis_az": bearing(*axis) % 180}
    if "waterbody" in votes:
        down, dir_method = votes["waterbody"], "waterbody"
    elif "terrain" in votes:
        down, dir_method = votes["terrain"], "terrain"
    else:
        down, dir_method = votes["trace"], "terrain trace"
    if dir_method == "terrain trace":
        conf = "low"
        notes.append("ASSUMPTION: direction from steepest DEM drop only; verify")
    else:
        conf = "medium" if len(votes) == 1 else "high"
    if len(votes) == 2 and "waterbody" in votes and "terrain" in votes and votes["waterbody"] != votes["terrain"]:
        conf = "low"
        notes.append("waterbody and terrain disagree on downstream side")

    # ---- breach point ---------------------------------------------------
    cand = resample(line, max(3.0, length / 200.0))
    breach, b_method = None, "nearest to dam point"
    if dem_fn is not None:
        start = max(15.0, 3.0 * known_height_m) if known_height_m else 15.0
        toe = np.array([_mean_dem(dem_fn, q, down, start, start + 30.0, 10.0)
                        for q in cand])
        if np.isfinite(toe).sum() >= max(3, len(cand) // 2):
            filled = np.where(np.isfinite(toe), toe, np.nanmax(toe))
            k = np.convolve(filled, np.ones(3) / 3.0, mode="same")
            k[0], k[-1] = filled[0], filled[-1]
            breach = cand[int(np.argmin(k))]
            b_method = "lowest downstream toe"
    if breach is None:
        _d, breach = nearest_on_polyline(ref_pt, line)
        notes.append("breach at point nearest inventory location")

    return {"ok": True, "breach": breach, "flow_az": bearing(*down),
            "axis_az": bearing(*axis) % 180.0, "length_m": length,
            "dir_method": dir_method, "dir_conf": conf,
            "breach_method": b_method, "notes": notes, "down": down}


def fallback_direction(dam_pt, water_centroid):
    """No dam line: assume flow runs from the reservoir centroid through the dam."""
    dx, dy = dam_pt[0] - water_centroid[0], dam_pt[1] - water_centroid[1]
    if dx == 0 and dy == 0:
        return None
    return bearing(dx, dy)


def names_match(a, b):
    """Loose dam-name comparison: case, spacing and punctuation ignored."""
    def norm(x):
        return "".join(ch for ch in str(x or "").lower() if ch.isalnum())
    na, nb = norm(a), norm(b)
    return bool(na) and na == nb
