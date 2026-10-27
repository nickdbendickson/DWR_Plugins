"""Inventory bridges snapped to where the road actually crosses the channel (DWR-3034 review,
Runner 3.10.90).

An NBI point is the bridge's recorded location, often tens to hundreds of metres from the
opening, and the line built from it (Hazard plugin 'runnerculverts': the nearest mapped stream x
road crossing) inherits any offset of the stream layer. In DWR-3034 bridge 0025008224 (Highway
25 over Square Butte Creek) was cut about 190 m south of the real opening: the cut drained a
solid embankment while the opening stayed blocked.

Before the cuts are made, each inventory BRIDGE is checked against the road (or rail)
centreline it crosses, within SNAP_RADIUS_M of its line's midpoint:

1. the bare-earth DEM along the centreline (1 m): a bridge's deck is not in the LiDAR, so the
   opening is a steep notch well below the road on both sides (road_profile.openings);
2. a channel line (the run's CHANNEL_LINES) crossing the centreline.

When the cut already crosses an opening, or the target is within KEEP_M, the line stays. Else
the line moves to the nearest qualifying opening (preferred: it is the opening in the terrain
the model uses), or to the nearest channel crossing, oriented across the road (along the channel
when one crosses there; the upstream end kept upstream) and sized to keep its length across the
road. Every move is logged with its distance; a bridge with no crossing found keeps its line
and gets a warning.

NDBREACH_BRIDGE_SNAP_M sets the radius; 0 turns the snap off."""
import os

import numpy as np

from . import conditioning as cond
from . import road_profile as rp

SNAP_RADIUS_M = 300.0
KEEP_M = 15.0                # the cut already within this of the crossing stays
STREAM_NEAR_M = 50.0         # a channel crossing this close to an opening sets the orientation
TAKEN_M = 30.0               # an opening already cut by another structure is not a target
ON_ROAD_M = 30.0             # the bridge line must cross, or come this close to, the road
LOW_POINT_M = 60.0           # the cut at the road's low point within this distance stays
LOW_POINT_TOL_M = 0.3
STEP_M = 1.0


def radius_from_env(default=SNAP_RADIUS_M):
    v = os.environ.get("NDBREACH_BRIDGE_SNAP_M", "").strip()
    if not v:
        return default
    try:
        return max(0.0, float(v))
    except ValueError:
        return default


def _seg_intersections(p, q):
    """Points where polylines p and q cross: [(x, y, i_p, t_p)] (segment index and fraction
    along p)."""
    out = []
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)
    for i in range(len(p) - 1):
        a, b = p[i], p[i + 1]
        r = b - a
        for j in range(len(q) - 1):
            c, d = q[j], q[j + 1]
            s = d - c
            den = r[0] * s[1] - r[1] * s[0]
            if abs(den) < 1e-12:
                continue
            t = ((c[0] - a[0]) * s[1] - (c[1] - a[1]) * s[0]) / den
            u = ((c[0] - a[0]) * r[1] - (c[1] - a[1]) * r[0]) / den
            if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                x = a + t * r
                out.append((float(x[0]), float(x[1]), i, t, j))
    return out


def _tangent(pts, k):
    a = pts[max(0, k - 3)]
    b = pts[min(len(pts) - 1, k + 3)]
    v = b - a
    n = float(np.hypot(*v))
    return v / n if n > 0 else np.array([1.0, 0.0])


def _unit(v):
    n = float(np.hypot(*v))
    return np.asarray(v, dtype=float) / n if n > 0 else None


def _mid(line):
    line = np.asarray(line, dtype=float)
    seg = np.hypot(*np.diff(line, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    h = 0.5 * s[-1]
    return np.array([np.interp(h, s, line[:, 0]), np.interp(h, s, line[:, 1])])


def _clip_near(pts, c, radius):
    """Contiguous runs of densified points within radius of c: [(i0, i1)]."""
    d = np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1])
    near = d <= radius
    runs, i, n = [], 0, len(pts)
    while i < n:
        if not near[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and near[j + 1]:
            j += 1
        runs.append((i, j + 1))
        i = j + 1
    return runs


def snap_bridge(s, embankments, read_dem, streams=(), radius_m=SNAP_RADIUS_M, keep_m=KEEP_M,
                taken=(), step=STEP_M):
    """Check and move one bridge (structures.Structure, kind bridge). read_dem(bounds, cell)
    -> Grid in metres (bare earth, no structure cuts). Returns a dict: name, method
    ('kept' / 'dem_opening' / 'channel_crossing' / 'none'), moved_m, old_xy, new_xy, note;
    the structure's line is replaced in place when it moves."""
    line = np.asarray(s.line, dtype=float)
    name = s.name or s.kind
    row = {"name": name, "method": "none", "moved_m": 0.0, "old_xy": None, "new_xy": None,
           "note": ""}
    if len(line) < 2:
        row["note"] = "line has fewer than 2 points; not checked"
        return row
    c = _mid(line)
    row["old_xy"] = [round(float(c[0]), 1), round(float(c[1]), 1)]
    u0 = _unit(line[-1] - line[0])
    L0 = float(np.hypot(*(line[-1] - line[0])))
    # the embankment the bridge carries: the one its line crosses, else the nearest
    best = None
    for e in embankments or []:
        e = np.asarray(e, dtype=float)
        if len(e) < 2:
            continue
        d, _a = cond.polyline_distance(np.array([c[0]]), np.array([c[1]]), e)
        d = float(d[0])
        if d > radius_m:
            continue
        crosses = bool(_seg_intersections(line, e))
        if not crosses and d > max(ON_ROAD_M, 0.5 * L0):
            continue
        key = (0 if crosses else 1, d)
        if best is None or key < best[0]:
            best = (key, e)
    if best is None:
        row["note"] = ("no road or rail centreline at the bridge (crossing its line or within "
                       "{:.0f} m): the cut stays at the inventory location").format(
                           max(ON_ROAD_M, 0.5 * L0))
        return row
    emb = best[1]
    pts = cond.densify(emb, step)
    d_c = np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1])
    k_old = int(np.argmin(d_c))
    hits = _seg_intersections(line, emb)
    p_old = np.array(hits[0][:2]) if hits else pts[k_old]
    k_old = int(np.argmin(np.hypot(pts[:, 0] - p_old[0], pts[:, 1] - p_old[1])))
    # the DEM along the centreline near the bridge
    cand = []                      # (how, k, depth, width)
    z_all = np.full(len(pts), np.nan)
    runs = _clip_near(pts, c, radius_m + 60.0)
    try:
        sel = np.concatenate([np.arange(a, b) for a, b in runs]) if runs else np.arange(0)
        if len(sel):
            q = pts[sel]
            pad = 5.0
            g = read_dem((float(q[:, 0].min()) - pad, float(q[:, 1].min()) - pad,
                          float(q[:, 0].max()) + pad, float(q[:, 1].max()) + pad), step)
            z_all[sel] = g.sample_bilinear(q[:, 0], q[:, 1])
            for a, b in runs:
                sub = pts[a:b]
                ss = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(sub, axis=0).T))])
                _m, ops = rp.openings(ss, z_all[a:b])
                for kk, dep, wid in ops:
                    k = a + kk
                    if np.hypot(*(pts[k] - c)) > radius_m:
                        continue
                    cand.append(("dem_opening", k, dep, wid))
    except Exception as ex:                                       # noqa - never stop a run
        row["note"] = "DEM not read along the road ({}); ".format(ex)
    # the cut already crosses an opening: keep it
    opening_ks = [x for x in cand if x[0] == "dem_opening"]
    if opening_ks:
        mx = max(x[2] for x in opening_ks)
        qual = [x for x in opening_ks if x[2] >= max(rp.OPENING_MIN_M, 0.5 * mx)]
        free = [x for x in qual if not any(np.hypot(*(pts[x[1]] - np.asarray(t))) <= TAKEN_M
                                           for t in taken)]
        near_old = [x for x in opening_ks
                    if np.hypot(*(pts[x[1]] - p_old)) <= keep_m + 0.5 * x[3]]
    else:
        free, near_old = [], []
    stream_hits = []
    for sl in streams or []:
        for h in _seg_intersections(emb, sl):
            p = np.array(h[:2])
            if np.hypot(*(p - c)) <= radius_m:
                sl = np.asarray(sl, dtype=float)
                stream_hits.append((p, _unit(sl[h[4] + 1] - sl[h[4]])))
    target = None
    if near_old:
        row.update(method="kept", note=row["note"] + "the cut already crosses the opening in "
                   "the DEM ({:.1f} m deep)".format(near_old[0][2]))
        row["new_xy"] = row["old_xy"]
        return row
    if free:
        x = min(free, key=lambda x: float(np.hypot(*(pts[x[1]] - p_old))))
        target = (pts[x[1]], "dem_opening",
                  "DEM opening {:.1f} m below the road, {:.0f} m wide".format(x[2], x[3]))
    elif stream_hits:
        p, _v = min(stream_hits, key=lambda h: float(np.hypot(*(h[0] - p_old))))
        target = (p, "channel_crossing", "channel line crossing")
    if target is None:
        near = np.hypot(pts[:, 0] - p_old[0], pts[:, 1] - p_old[1]) <= LOW_POINT_M
        zn = z_all[near]
        z_old = z_all[k_old]
        if np.isfinite(z_old) and np.isfinite(zn).any() and \
                z_old <= float(np.nanmin(zn)) + LOW_POINT_TOL_M:
            row.update(method="kept", new_xy=row["old_xy"],
                       note=row["note"] + "no opening in the DEM (deck or water surface in the "
                       "LiDAR, or a flat road); the cut is at the road's low point: kept")
            return row
        row["note"] += ("no channel crossing or DEM opening on the road within {:.0f} m: the "
                        "cut stays at the inventory location").format(radius_m)
        return row
    p_new, how, txt = target
    moved = float(np.hypot(*(p_new - p_old)))
    if moved <= keep_m:
        row.update(method="kept", new_xy=row["old_xy"], moved_m=round(moved, 1),
                   note=row["note"] + "{} {:.0f} m from the cut: kept".format(txt, moved))
        return row
    k_new = int(np.argmin(np.hypot(pts[:, 0] - p_new[0], pts[:, 1] - p_new[1])))
    t_new = _tangent(pts, k_new)
    t_old = _tangent(pts, k_old)
    # length across the road kept: L0 * |sin(old angle)| / |sin(new angle)|
    sin_old = abs(float(u0[0] * t_old[1] - u0[1] * t_old[0])) if u0 is not None else 1.0
    across = L0 * sin_old if sin_old > 0.2 else L0
    v = None
    for p, sv in stream_hits:
        if sv is not None and np.hypot(*(p - p_new)) <= STREAM_NEAR_M:
            v = sv
            break
    if v is None or abs(float(v[0] * t_new[1] - v[1] * t_new[0])) < 0.3:
        v = np.array([-t_new[1], t_new[0]])
    if u0 is not None and float(np.dot(v, u0)) < 0:
        v = -v
    sin_new = abs(float(v[0] * t_new[1] - v[1] * t_new[0]))
    L = across / max(sin_new, 0.5)
    s.line = np.array([p_new - 0.5 * L * v, p_new + 0.5 * L * v])
    s.invert_up_m = s.invert_dn_m = None                         # the old ends' inverts
    row.update(method=how, moved_m=round(moved, 1),
               new_xy=[round(float(p_new[0]), 1), round(float(p_new[1]), 1)],
               note=row["note"] + "{}: cut moved {:.0f} m along the road".format(txt, moved))
    return row


def snap_bridges(structures, embankments, read_dem, streams=(), radius_m=None):
    """snap_bridge for every inventory bridge. Returns (rows, notes, warnings)."""
    radius_m = radius_from_env() if radius_m is None else float(radius_m)
    rows, notes, warns = [], [], []
    if radius_m <= 0:
        return rows, ["Bridge snap off (NDBREACH_BRIDGE_SNAP_M = 0)"], warns
    bridges = [s for s in structures or [] if s.kind == "bridge"
               and getattr(s, "source", "inventory") == "inventory"]
    if bridges and not embankments:
        warns.append("{} inventory bridge(s) not checked against a road crossing: the run has "
                     "no road or rail lines; their cuts stay at the inventory location".format(
                         len(bridges)))
        return rows, notes, warns
    for s in structures or []:
        if s.kind != "bridge" or getattr(s, "source", "inventory") != "inventory":
            continue
        taken = [_mid(o.line) for o in structures if o is not s and len(np.asarray(o.line)) >= 2]
        r = snap_bridge(s, embankments, read_dem, streams, radius_m, taken=taken)
        rows.append(r)
        msg = "Bridge {}: {}".format(r["name"], r["note"])
        if r["method"] == "none":
            notes.append(msg)
        elif r["method"] != "kept":
            notes.append(msg + " (from {} to {})".format(r["old_xy"], r["new_xy"]))
        else:
            notes.append(msg)
    lost = [r["name"] for r in rows if r["method"] == "none"]
    if lost:
        warns.append("{} inventory bridge(s) not checked against a road crossing (no road / rail "
                     "centreline at the bridge, or no opening, channel crossing or low point on "
                     "it within {:.0f} m); their cuts stay at the inventory location: {}".format(
                         len(lost), radius_m, "; ".join(lost[:12]) +
                         (" and {} more".format(len(lost) - 12) if len(lost) > 12 else "")))
    return rows, notes, warns

