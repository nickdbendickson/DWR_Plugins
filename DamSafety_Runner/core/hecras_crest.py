"""v3.10.22: the HEC-RAS dam line along the embankment crest, traced from the DEM.

Before this the SA/2D connection was a straight line through the dam point, at right angles
to the flow direction (the dam point to the run's inflow point). On a bent or skewed
embankment that line is not the crest: on Absaraka (DWR-2801) the crest at the breach runs
about 28 degrees off it, the breach cut crossed the embankment obliquely and a strip of the
pool (about 2 acres at 1051.4 ft) lay inside the 2D area, downstream of the line.

dam_axis='crest' (the default for HEC-RAS builds) traces the crest instead: from the dam
point snapped to the ridge, a walk along the ridge in both directions (a cross-section every
step, the highest ground near the walk's heading), while the ridge stays within the crest
tolerance of the top of dam and stands above the ground on both sides (an embankment, not an
abutment hillside), stepping over short gaps (a spillway, a low spot), up to the dam length.
The walk is smoothed and simplified to a vertex spacing of about a cell, and made straight
across the breach so that the breach is a rectangle in the crest's own frame there.
dam_axis='straight' is the old line. A trace that fails falls back to it, with a warning.

The breach is put at the deepest dam section along the traced crest (the lowest ground at the
downstream toe: where the old channel crosses), else the lowest crest; the option
breach_station_ft (along the dam line from its first end) or breach_at ('toe',
'lowest_crest', 'inflow' = the run's inflow point projected, the old rule) override it.

Only numpy; the DEM comes in as a sampler f(xy (n, 2)) -> z (n,) in model units.
"""
import math

import numpy as np

DAM_AXES = ("crest", "straight")
BREACH_AT = ("auto", "toe", "lowest_crest", "inflow")
TURN_MAX_DEG = 30.0          # the ridge is looked for within this angle of the heading
TURN_FIRST_DEG = 55.0        # ... for the first steps from the dam point (the line's axis is a guess)
FIRST_STEPS = 3
PROM_MIN_FT = 2.0            # the crest stands at least this (or PROM_FRAC x H) above both sides
PROM_FRAC = 0.1
CENTRE_BIAS = 1e-3           # ft of elevation per ft off the heading: the nearer of equal highs
PLATEAU_FRAC = 0.05          # level within 5 % of the sample spacing: the same flat crest
# 3.10.87: a DEM crest this far above the recorded top of dam (at least DATUM_MIN_FT) that
# stands above the ground on both sides is the embankment on another vertical datum (DWR
# crests are often on a local datum; DWR-2801's DEM crest is 1066-1069 ft against 1063.2 ft on
# record), not an abutment: the walk follows the DEM's crest level instead
DATUM_FRAC = 0.25
DATUM_MIN_FT = 5.0
# 3.10.87: a dam line traced off the old rule (from other crest-high ground near the dam point
# - snap -, at the DEM's level off the record, or on the dam point's own ridge over a higher
# one) is used only when it crosses the run's flow (flow_check): of the run's flooded cells
# within two line lengths, at least FLOW_DOWN_FRAC on its downstream side, their centroid
# within FLOW_ANGLE_MAX_DEG of its downstream normal and of every part's, no ridge between the
# line and the flood (a causeway across the pool has the dam between it and the flood)
FLOW_DOWN_FRAC = 0.8
FLOW_ANGLE_MAX_DEG = 60.0
FLOW_LOCAL_MAX_DEG = 65.0     # ... and every part of the line within this (no arm along the flow)
FLOW_MIN_CELLS = 10


class CrestTraceError(ValueError):
    """The crest could not be traced; the message says why (build_model falls back).

    3.10.87: on_ridge is True when the dam point sits on a ridge that stands above the ground on
    both sides of the guessed axis but is not at the top of dam on record (the crest is there
    and reads off the record: a coarse DEM, another datum), so no start from other high ground
    nearby is tried; snap_tried records a start from other crest-high ground that was tried and
    given up."""

    def __init__(self, msg, on_ridge=False, snap_tried=None):
        super().__init__(msg)
        self.on_ridge = bool(on_ridge)
        self.snap_tried = snap_tried


# --- polylines ---------------------------------------------------------------------------
def cumlen(line):
    p = np.asarray(line, float).reshape(-1, 2)
    if len(p) < 2:
        return np.zeros(len(p))
    return np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]


def point_at(line, s):
    """Points at stations s (scalar or array) along the polyline (clamped to its ends)."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    s_ = np.clip(np.asarray(s, float), 0.0, c[-1])
    out = np.column_stack([np.interp(np.ravel(s_), c, p[:, 0]),
                           np.interp(np.ravel(s_), c, p[:, 1])])
    return out[0] if np.ndim(s) == 0 else out


def segment_at(line, s):
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    return int(np.clip(np.searchsorted(c, float(s), side="right") - 1, 0, len(p) - 2))


def tangent_at(line, s):
    """Unit tangent of the segment that holds station s."""
    p = np.asarray(line, float).reshape(-1, 2)
    k = segment_at(p, s)
    t = p[k + 1] - p[k]
    return t / (np.hypot(*t) or 1.0)


def left_normal(t):
    t = np.asarray(t, float)
    return np.array([-t[1], t[0]])


def station_of(line, xy):
    """Station of the point's projection on the polyline."""
    p = np.asarray(line, float).reshape(-1, 2)
    q = np.asarray(xy, float)
    best, st, acc = np.inf, 0.0, 0.0
    for a, b in zip(p[:-1], p[1:]):
        ab = b - a
        L2 = float(ab @ ab)
        t = 0.0 if L2 <= 0 else float(np.clip((q - a) @ ab / L2, 0.0, 1.0))
        dd = float(np.hypot(*(a + t * ab - q)))
        if dd < best:
            best, st = dd, acc + t * math.sqrt(L2)
        acc += math.sqrt(L2)
    return st


def sub_line(line, s0, s1):
    """The part of the polyline from station s0 to s1 (s0 < s1), its vertices in between kept."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    s0, s1 = max(0.0, float(s0)), min(float(c[-1]), float(s1))
    mid = [q for q, cc in zip(p, c) if s0 + 1e-9 < cc < s1 - 1e-9]
    return np.vstack([point_at(p, s0)] + mid + [point_at(p, s1)]) if mid else \
        np.vstack([point_at(p, s0), point_at(p, s1)])


def frame_at(line, s):
    """(origin, along, downstream): the straight frame of the segment that holds station s,
    with origin = the point at s minus along x s, so that origin + along x s' + downstream x t
    is the point s' along the line and t to its left (the 2D area side when the line runs from
    A, the left end looking downstream, to B)."""
    t = tangent_at(line, s)
    return point_at(line, s) - t * float(s), t, left_normal(t)


def simplify(line, tol):
    """Douglas-Peucker (ends kept)."""
    p = np.asarray(line, float).reshape(-1, 2)
    if len(p) < 3:
        return p
    keep = np.zeros(len(p), bool)
    keep[[0, -1]] = True
    stack = [(0, len(p) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a, b = p[i], p[j]
        ab = b - a
        L = float(np.hypot(*ab))
        q = p[i + 1:j]
        if L <= 0:
            dd = np.hypot(*(q - a).T)
        else:
            dd = np.abs((q[:, 0] - a[0]) * ab[1] - (q[:, 1] - a[1]) * ab[0]) / L
        k = int(np.argmax(dd))
        if dd[k] > tol:
            keep[i + 1 + k] = True
            stack += [(i, i + 1 + k), (i + 1 + k, j)]
    return p[keep]


def smooth(line, window):
    """Moving average of the vertices over `window` vertices (odd; the ends stay)."""
    p = np.asarray(line, float).reshape(-1, 2)
    w = int(window) // 2
    if w < 1 or len(p) < 3:
        return p
    out = p.copy()
    for i in range(1, len(p) - 1):
        k = min(w, i, len(p) - 1 - i)
        out[i] = p[i - k:i + k + 1].mean(axis=0)
    return out


def min_spacing(line, step, keep=()):
    """Vertices closer than `step` to the last kept one go (ends and `keep` stay; a last
    segment shorter than step loses the vertex before it)."""
    p = np.asarray(line, float).reshape(-1, 2)
    keep = set(int(k) for k in keep) | {0, len(p) - 1}
    out = [0]
    for i in range(1, len(p) - 1):
        if i in keep or np.hypot(*(p[i] - p[out[-1]])) >= step:
            out.append(i)
    while len(out) > 1 and out[-1] not in keep and np.hypot(*(p[-1] - p[out[-1]])) < step:
        out.pop()
    out.append(len(p) - 1)
    return p[out]


def straighten(line, s0, s1):
    """The polyline with its vertices strictly between stations s0 and s1 replaced by the
    chord (the points at s0 and s1 become vertices)."""
    p = np.asarray(line, float).reshape(-1, 2)
    c = cumlen(p)
    s0, s1 = max(0.0, float(s0)), min(float(c[-1]), float(s1))
    before = [q for q, cc in zip(p, c) if cc < s0 - 1e-9]
    after = [q for q, cc in zip(p, c) if cc > s1 + 1e-9]
    return np.vstack(before + [point_at(p, s0), point_at(p, s1)] + after)


def monotonic(line, axis):
    """True when the vertices advance along `axis` (unit vector) all the way."""
    p = np.asarray(line, float).reshape(-1, 2)
    return bool(np.all(np.diff(p @ np.asarray(axis, float)) > 0))


def angle_deg(u, v):
    """Angle between two directions as lines (0-90 degrees)."""
    u = np.asarray(u, float) / (np.hypot(*u) or 1.0)
    v = np.asarray(v, float) / (np.hypot(*v) or 1.0)
    return float(np.degrees(np.arccos(min(1.0, abs(float(u @ v))))))


def bearing_deg(v):
    return float(np.degrees(np.arctan2(v[0], v[1])) % 360.0)


# --- the trace ---------------------------------------------------------------------------
def _ridge(sample, Y, nu, half, ds, wide=None):
    tt = np.arange(-half, half + 1e-9, ds)
    zz = sample(Y[None, :] + tt[:, None] * nu[None, :])
    if not np.isfinite(zz).any():
        return None
    zb = np.where(np.isfinite(zz), zz, -np.inf)
    k = int(np.argmax(zb - CENTRE_BIAS * np.abs(tt)))
    # a flat crest (a road, a wide top): the middle of the level stretch around the highest
    # point, not whichever end the bias picks (the walk would zig-zag across the top)
    lvl = zb[k] - PLATEAU_FRAC * ds
    i, j = k, k
    while i > 0 and zb[i - 1] >= lvl:
        i -= 1
    while j < len(zb) - 1 and zb[j + 1] >= lvl:
        j += 1
    if (wide is not None and wide >= 3.0 * half and i == 0 and j == len(zb) - 1 and j > i
            and np.isfinite(zz).all()):
        # 3.10.87: a level top across the whole window on a crest that is wider than the
        # window on record (`wide`): the middle of the samples is off centre (they need not
        # end at +half) and the walk drifted across a wide crest by that much every step, or
        # walked off it when it was heading across it. The window is widened (symmetric, up to
        # 8 times) until the top's edges show, and the walk turns toward the top's middle by no
        # more than the window allows (the turn limit); straight ahead if they never show
        for f in (2.0, 4.0, 8.0):
            m = int(half * f / ds + 1e-9)
            tw = ds * np.arange(-m, m + 1)
            zw = sample(Y[None, :] + tw[:, None] * nu[None, :])
            zwb = np.where(np.isfinite(zw), zw, -np.inf)
            a = b = m
            while a > 0 and zwb[a - 1] >= lvl:
                a -= 1
            while b < len(zwb) - 1 and zwb[b + 1] >= lvl:
                b += 1
            if a > 0 and b < len(zwb) - 1:
                R = Y + float(np.clip(0.5 * (tw[a] + tw[b]), -half, half)) * nu
                zr = float(sample(R[None, :])[0])
                if np.isfinite(zr):
                    return R, zr
                break
        zr = float(sample(Y[None, :])[0])
        if np.isfinite(zr):
            return Y.copy(), zr
    if j > i:
        R = Y + 0.5 * (tt[i] + tt[j]) * nu
        zr = float(sample(R[None, :])[0])
        if np.isfinite(zr):
            return R, zr
    return Y + tt[k] * nu, float(zz[k])


def _ridges(sample, Y, nu, half, ds):
    """3.10.87: every ridge across the section through Y (a local high, the middle of a level
    top; not a window end, where the ground may still be rising): [(R, z, t)], t the offset of R
    from Y along nu, nearest Y first."""
    tt = np.arange(-half, half + 1e-9, ds)
    zz = sample(Y[None, :] + tt[:, None] * nu[None, :])
    fin = np.isfinite(zz)
    if fin.sum() < 3:
        return []
    zb = np.where(fin, zz, np.nanmin(zz[fin]) - 1e6)
    dz = np.diff(zb)
    # level within a hundredth of the sample spacing (a narrow crest on a coarse DEM stands
    # less than _ridge's plateau tolerance above its neighbours)
    sg = np.where(np.abs(dz) <= 0.01 * ds, 0, np.sign(dz)).astype(int)
    n = len(zb)
    prev = np.zeros(n, int)            # the last non-level step before each sample
    last = 0
    for i in range(1, n):
        if sg[i - 1]:
            last = sg[i - 1]
        prev[i] = last
    nxt = np.zeros(n, int)             # the first non-level step after each sample
    last = 0
    for i in range(n - 2, -1, -1):
        if sg[i]:
            last = sg[i]
        nxt[i] = last
    top = fin & (prev > 0) & (nxt < 0)
    top[0] = top[-1] = False
    out = []
    i = 0
    while i < n:
        if not top[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and top[j + 1]:
            j += 1
        tm = 0.5 * (tt[i] + tt[j])
        R = Y + tm * nu
        zr = float(sample(R[None, :])[0])
        if not np.isfinite(zr):
            zr = float(np.max(zb[i:j + 1]))
        out.append((R, zr, float(tm)))
        i = j + 1
    out.sort(key=lambda r: abs(r[2]))
    return out


def _judge(sample, R, nu, zr, top, tol, toe, prom, nan_falls=False):
    """('', z) on the crest, else (why, z). nan_falls: see below (3.10.87)."""
    if not np.isfinite(zr):
        return "edge of the DEM"
    if zr > top + tol:
        return "abutment: the ridge rises above the top of dam"
    if zr < top - tol:
        return "end of the embankment: the ridge falls below the crest"
    # each side: the lowest ground from half the toe distance to the toe (a side that only
    # rises is a hillside: the abutment)
    tt = np.linspace(0.5 * toe, toe, 4)
    lo_l = sample(R[None, :] + tt[:, None] * nu[None, :])
    lo_r = sample(R[None, :] - tt[:, None] * nu[None, :])
    # 3.10.87 (nan_falls, the walk of a trace off the old rule): a side that runs from ground
    # into no data (a pool left out of the DEM) falls: its one face sample above the water line
    # is not the ground beside the crest (from the middle of a wide crest only that sample was
    # left, and the crest read as a tie-in)
    sides = [-np.inf if (nan_falls and np.isfinite(v[0]) and not np.isfinite(v).all()) else
             (np.nanmin(v) if np.isfinite(v).any() else np.nan) for v in (lo_l, lo_r)]
    hi = np.nanmax(sides) if np.isfinite(sides).any() else np.nan
    if not np.isfinite(hi) or zr - hi < prom:
        return "abutment tie-in: the ground beside the crest is as high as the crest"
    return ""


def _walk(sample, X0, tau0, max_len, step, toe, top, tol, prom, gap, wide=None):
    pts = [np.asarray(X0, float)]
    X, tau = pts[0], np.asarray(tau0, float) / (np.hypot(*tau0) or 1.0)
    walked, run, last_ok, why, nstep = 0.0, 0.0, 0, "the dam length", 0
    ds = 0.5 * step
    while walked < max_len - 1e-9:
        h = min(step, max_len - walked)
        if h < 0.25 * step:
            break
        Y = X + tau * h
        nu = left_normal(tau)
        ang = TURN_FIRST_DEG if nstep < FIRST_STEPS else TURN_MAX_DEG
        rr = _ridge(sample, Y, nu, h * math.tan(math.radians(ang)), ds, wide)
        nstep += 1
        bad = "edge of the DEM" if rr is None else _judge(sample, rr[0], nu, rr[1], top, tol,
                                                          toe, prom, wide is not None)
        if not bad:
            R = rr[0]
            v = R - X
            v = v / (np.hypot(*v) or 1.0)
            tau = tau + v
            tau = tau / (np.hypot(*tau) or 1.0)
            walked += float(np.hypot(*(R - X)))
            X = R
            pts.append(R)
            last_ok, run = len(pts) - 1, 0.0
        else:
            run += h
            if run > gap + 1e-9 or bad == "edge of the DEM":
                why = bad
                break
            X = Y                                   # straight on over the gap
            walked += h
            pts.append(Y)
    return np.vstack(pts[:last_ok + 1]), why, walked


def _rises(sample, P, Q, prom, ds):
    """3.10.87: the ground from P to Q never drops more than `prom` below the highest ground
    before it on the way (no-data samples, a pool left out of the DEM, are skipped)."""
    P, Q = np.asarray(P, float), np.asarray(Q, float)
    n = max(2, int(math.ceil(float(np.hypot(*(Q - P))) / max(float(ds), 1e-9))) + 1)
    s = np.linspace(0.0, 1.0, n)
    z = sample(P[None, :] + s[:, None] * (Q - P)[None, :])
    z = z[np.isfinite(z)]
    if len(z) < 2:
        return True
    return float(np.max(np.maximum.accumulate(z) - z)) <= float(prom)


def snap(sample, P, top, tol, search, half, step, allow=None, prom=None):
    """3.10.87: a dam point off the embankment (on the pool or the face: DWR-2801's dam.json
    point is on the pool, 73-92 ft from the crest) with no ridge across the guessed axis.
    When the ground at P is more than `tol` below the top of dam: the nearest ground within
    `search` that is at least that high, at most `allow` above the top of dam (not an abutment
    or a hill far above it) and that the ground climbs to from P without dropping more than
    half of `prom` on the way (a pool or face point climbs the dam face; a road beyond a low
    valley floor does not, nor one beyond the shallow saddle a coarse DEM shows between it and
    a narrow crest that it reads low); and the embankment's direction there (the long axis of
    the high ground within `half` of it). Returns (point, unit axis) or None (P on the crest,
    nothing high enough within reach, or high ground with no clear long axis: a hill)."""
    P = np.asarray(P, float)
    zp = sample(P[None, :])[0]
    if np.isfinite(zp) and zp >= top - tol:
        return None
    r = float(search) + float(half)
    g = np.arange(-r, r + 0.5 * step, step)
    gx, gy = np.meshgrid(P[0] + g, P[1] + g)
    xy = np.column_stack([gx.ravel(), gy.ravel()])
    z = sample(xy)
    with np.errstate(invalid="ignore"):
        high = np.isfinite(z) & (z >= top - tol)
        band = high & (z <= top + allow) if allow is not None else high
    dist = np.hypot(*(xy - P).T)
    cand = np.nonzero(band & (dist <= search))[0]
    Q = None
    for i in cand[np.argsort(dist[cand], kind="stable")]:
        if prom is None or _rises(sample, P, xy[i], 0.5 * prom, 0.5 * step):
            Q = xy[i]
            break
    if Q is None:
        return None
    loc = high & (np.hypot(*(xy - Q).T) <= half)
    if loc.sum() < 3:
        return None
    pts = xy[loc]
    w, v = np.linalg.eigh(np.cov((pts - pts.mean(axis=0)).T))
    if w[0] > 0 and w[1] / w[0] < 2.0:
        return None
    t = v[:, 1]
    return Q, t / np.hypot(*t)


def _seg_nearest(line, pts):
    """For each point: (distance to the polyline, signed side: > 0 on the left of the
    segment nearest it)."""
    p = np.asarray(line, float).reshape(-1, 2)
    q = np.asarray(pts, float).reshape(-1, 2)
    best = np.full(len(q), np.inf)
    side = np.zeros(len(q))
    for a, b in zip(p[:-1], p[1:]):
        ab = b - a
        L2 = float(ab @ ab)
        if L2 <= 0:
            continue
        aq = q - a
        t = np.clip(aq @ ab / L2, 0.0, 1.0)
        dd = np.hypot(aq[:, 0] - t * ab[0], aq[:, 1] - t * ab[1])
        cr = ab[0] * aq[:, 1] - ab[1] * aq[:, 0]
        k = dd < best
        best[k] = dd[k]
        side[k] = cr[k]
    return best, side


def flow_check(line, wet_xy, wet_cell, sample=None, crest=None, tol=None, toe=None,
               reach=None, step=None, drop=None, prom=None, dam_point=None, beyond=None):
    """3.10.87: does a dam line (A -> B, the downstream side on its left) cross the run's flow?
    wet_xy: the centres of the run's flooded cells (model units; cells wet_cell wide). Of those
    within `reach` (default two line lengths) of the line: at least FLOW_DOWN_FRAC on its
    downstream side (the line extended straight past its ends), their centroid within
    FLOW_ANGLE_MAX_DEG of its downstream normal (seen from the line's middle; a line along the
    valley has the flood along it). When `dam_point` is given: it is not more than `beyond`
    downstream of the line (a dam point on the pool or the face is upstream of the crest or
    near it; one farther downstream is below another embankment). When `sample` is given: no
    ridge between the line and the flood along most of the line - after the ground has fallen
    at least `prom` (default `tol`) from the line's own top, ground that rises at least `prom`
    above the lowest ground before it to within `drop` (default `tol`) of the crest level
    `crest` (a causeway across the pool has the dam between it and the flood). Returns (ok,
    info: cells, downstream_frac, flow_angle_deg, flow_bearing_deg, dam_point_downstream,
    barrier_frac, why)."""
    p = np.asarray(line, float).reshape(-1, 2)
    w = np.asarray(wet_xy if wet_xy is not None else np.zeros((0, 2)), float).reshape(-1, 2)
    L = float(cumlen(p)[-1])
    info = {"cells": 0}
    if L <= 0 or not len(w):
        return False, dict(info, why="the run has no flooded cells to check it against")
    reach = float(reach if reach is not None else 2.0 * L)
    chord = (p[-1] - p[0]) / (np.hypot(*(p[-1] - p[0])) or 1.0)
    down = left_normal(chord)
    mid = point_at(p, 0.5 * L)
    ext = extend(p, 2.0 * reach + L, chord)
    if dam_point is not None and beyond is not None:
        dd, sd = _seg_nearest(ext, np.asarray(dam_point, float).reshape(1, 2))
        below = float(dd[0]) * (1.0 if sd[0] > 0 else -1.0)
        info["dam_point_downstream"] = round(below, 1)
        if below > float(beyond):
            return False, dict(info, why="the dam point is {:,.0f} downstream of it, beyond the "
                               "toe: it is another embankment upstream of the dam".format(below))
    lo, hi = p.min(axis=0) - reach, p.max(axis=0) + reach
    w = w[np.all((w >= lo) & (w <= hi), axis=1)]
    if len(w):
        w = w[_seg_nearest(p, w)[0] <= reach]
    info.update(cells=int(len(w)), reach=round(reach, 1))
    if len(w) < FLOW_MIN_CELLS:
        return False, dict(info, why="only {} of the run's flooded cells lie within {:,.0f} of it"
                           .format(len(w), reach))
    on_down = _seg_nearest(ext, w)[1] > 0
    frac = float(on_down.mean())
    cen = w.mean(axis=0) - mid
    cn = float(np.hypot(*cen))
    ang = float(np.degrees(np.arccos(np.clip(float(cen @ down) / cn, -1.0, 1.0)))) if cn > 0 \
        else 180.0
    info.update(downstream_frac=round(frac, 3), flow_angle_deg=round(ang, 1),
                flow_bearing_deg=round(bearing_deg(cen), 1) if cn > 0 else None)
    if frac < FLOW_DOWN_FRAC:
        return False, dict(info, why="only {:.0%} of the run's flooded cells within {:,.0f} of "
                           "it are on its downstream side".format(frac, reach))
    if ang > FLOW_ANGLE_MAX_DEG:
        return False, dict(info, why="the run's flooded area lies {:.0f} degrees off its "
                           "downstream side (the line runs along the flow)".format(ang))
    # every part of it across the flow (a line that turns onto a spur or a road down the
    # valley has an arm along the flow)
    if cn > 0:
        loc = [float(np.degrees(np.arccos(np.clip(float(left_normal(tangent_at(p, s)) @ cen)
                                                  / cn, -1.0, 1.0))))
               for s in np.linspace(0.05 * L, 0.95 * L, 10)]
        info["flow_angle_local_max_deg"] = round(max(loc), 1)
        if sum(a_ > FLOW_LOCAL_MAX_DEG for a_ in loc) > 1:
            return False, dict(info, why="parts of it run {:.0f} degrees off square to the "
                               "run's flow (an arm along the flow)".format(max(loc)))
    if sample is not None and crest is not None:
        wd = w[on_down]
        ds = float(step or max(0.5 * float(wet_cell), 1e-6))
        rise_min = float(prom if prom is not None else (tol or 0.0))
        high = float(crest) - float(drop if drop is not None else (tol or 0.0))
        hits, blocked = 0, 0
        for s in np.linspace(0.1 * L, 0.9 * L, 9):
            X = point_at(p, s)
            n = left_normal(tangent_at(p, s))
            rel = wd - X
            along = rel @ n
            perp = np.abs(rel[:, 0] * n[1] - rel[:, 1] * n[0])
            k = (along >= 0) & (perp <= float(wet_cell))
            if not k.any():
                continue
            hits += 1
            t1 = float(along[k].min())             # to the first flooded cell's centre
            if t1 <= ds:
                continue
            tt = np.arange(0.0, t1 + 1e-9, ds)
            zz = sample(X[None, :] + tt[:, None] * n[None, :])
            zz = zz[np.isfinite(zz)]
            if len(zz) < 3:
                continue
            # off the line's own top first (its crest, its downstream face) ...
            off = np.nonzero(np.maximum.accumulate(zz) - zz >= rise_min)[0]
            if not len(off):
                continue
            zr = zz[off[0]:]
            # ... then up again onto another ridge near the crest level
            rise = zr - np.minimum.accumulate(zr)
            if np.any((rise >= rise_min) & (zr >= high)):
                blocked += 1
        info["barrier_frac"] = round(blocked / hits, 3) if hits else None
        if hits and blocked > 0.5 * hits:
            return False, dict(info, why="a ridge near its crest level lies between it and the "
                               "run's flooded area along {} of {} sections (another embankment "
                               "is downstream of it)".format(blocked, hits))
    return True, dict(info, why="")


def trace(sample, P, axis, top, height, step, toe, max_len, gap, tol=None, prom=None,
          search=None, ft=1.0, near=None, relax=True, crest_w=None, start_within=None):
    """The crest polyline through the dam point. sample(xy) -> z (model units); axis: unit
    vector along the dam from A (the left end looking downstream) to B; top, height: top of dam
    and dam height; step: walk step; toe: the distance from the crest to the toe (for the
    ridge test); max_len: the dam length; gap: stretches off the crest stepped over; ft: model
    units per foot. Returns {'line' (A..B, raw walk), 'p0', 'ends': {'A', 'B'}: (why, length),
    'snap', 'top_used' (the level the crest was followed at), 'top_offset' (that minus the top
    of dam on record)}. Raises CrestTraceError.

    3.10.87: the ridge the dam point is on (the nearest ridge across the section within `near`,
    default max(toe / 2, step), of it) comes first: at the record's level it wins over a
    farther ridge that is higher (a road beside the dam); above the record but standing above
    the ground on both sides, the crest is followed at that ridge's own level when it is no
    more than max(DATUM_MIN_FT, DATUM_FRAC x H) above it (another vertical datum: DWR-2801),
    and never another ridge nearby. When the dam point is on a ridge that is not followed (off
    the record's level - below it: a narrow crest that a coarse DEM reads low, a record above
    the DEM's crest -, or at it but not standing above both sides), the error says on_ridge:
    the crest is there, so no start from other high ground nearby is tried.
    'relaxed' in the result says the trace took either way off the old rule (the highest ridge
    across the section, at the record's level); relax=False keeps to the old rule. crest_w:
    the crest width on record - the walk of a trace off the old rule (relaxed, or with
    start_within) keeps to the middle of a level top wider than its window, and takes a side
    of the ridge test that runs into no data (a pool left out of the DEM) as falling.
    start_within: the crest the trace starts from must be at most this far from P (a start from
    crest-high ground found nearby: not a higher ridge across the section)."""
    axis = np.asarray(axis, float) / (np.hypot(*axis) or 1.0)
    tol = float(tol if tol is not None else max(3.0 * ft, 0.1 * height))
    prom = float(prom if prom is not None else max(PROM_MIN_FT * ft, PROM_FRAC * height))
    search = float(search if search is not None else max(toe, 3.0 * step))
    near = float(near if near is not None else max(0.5 * toe, step))
    P = np.asarray(P, float)
    nu = left_normal(axis)
    rr = _ridge(sample, P, nu, search, 0.5 * step)
    if rr is None:
        raise CrestTraceError("no DEM around the dam point")
    p0, z0 = rr
    why = _judge(sample, p0, nu, z0, top, tol, toe, prom)
    top_rec = top
    allow = max(DATUM_MIN_FT * ft, DATUM_FRAC * height)
    on_ridge, relaxed = False, False
    own = [r for r in _ridges(sample, P, nu, search, 0.5 * step) if abs(r[2]) <= near] \
        if relax else []
    other = False
    if own:
        pn, zn, _ = own[0]
        other = float(np.hypot(*(pn - p0))) > step
        at_rec = abs(zn - top) <= tol
        if at_rec and not _judge(sample, pn, nu, zn, top, tol, toe, prom):
            # a crest at the record's level at the dam point wins over a farther ridge that
            # is higher (within the tolerance, or one the datum allowance would take: a road
            # beside the dam)
            if other and ((not why and abs(zn - top) < abs(z0 - top) - 0.5 * ft) or (
                    why.startswith("abutment: the ridge rises") and z0 - top <= allow)):
                p0, z0, why, other, relaxed = pn, zn, "", False, True
        elif why and (at_rec or not _judge(sample, pn, nu, zn, zn, tol, toe, prom)):
            # the dam point is on ground at the record's level that is no clear crest, or on a
            # ridge off the record's level: only that ridge may be followed at its own level
            # (never another, higher one nearby: a road beside a low dam on a coarse DEM)
            on_ridge = True
            if other:
                p0, z0 = pn, zn
                why = _judge(sample, p0, nu, z0, top, tol, toe, prom)
    if relax and why.startswith("abutment: the ridge rises") and z0 - top <= allow and \
            not _judge(sample, p0, nu, z0, z0, tol, toe, prom):
        top, why, relaxed = z0, "", True
    if not why and start_within is not None and float(np.hypot(*(p0 - P))) > start_within:
        why = "the crest across the section is {:.0f} away, another ridge".format(
            float(np.hypot(*(p0 - P))))
    if why:
        raise CrestTraceError("no embankment crest within {:.0f} of the dam point across the "
                              "dam ({}; highest ground {:.2f}, top of dam {:.2f})".format(
                                  search, why, z0, top), on_ridge=on_ridge)
    # the walk keeps to the old rule unless the trace is off it (relaxed, or a start from
    # crest-high ground found nearby): only then the wide-crest and no-data rules (3.10.87)
    wide = crest_w if (relaxed or start_within is not None) else None
    a, why_a, _ = _walk(sample, p0, -axis, max_len, step, toe, top, tol, prom, gap, wide)
    b, why_b, _ = _walk(sample, p0, axis, max_len, step, toe, top, tol, prom, gap, wide)
    line = np.vstack([a[::-1], b[1:]])
    la, lb = float(cumlen(a)[-1]), float(cumlen(b)[-1])
    if la + lb < 2.0 * step:
        raise CrestTraceError("the crest could not be followed from the dam point ({} / {})"
                              .format(why_a, why_b), on_ridge=on_ridge)
    return {"line": line, "p0": p0, "z0": z0, "snap": float(np.hypot(*(p0 - P))),
            "ends": {"A": (why_a, la), "B": (why_b, lb)}, "tol": tol, "prom": prom,
            "top_used": float(top), "top_offset": float(top - top_rec), "on_ridge": on_ridge,
            "relaxed": relaxed}


def tidy(line, step, cell, keep_len=None, centre=None):
    """The walk smoothed (over about half a cell), simplified (to a quarter of a step or 2 % of
    a cell) and thinned to vertices at least a cell apart; trimmed to keep_len centred on the
    station of `centre` (kept inside the line) when it is longer."""
    p = np.asarray(line, float).reshape(-1, 2)
    win = max(3, int(round(0.5 * cell / max(step, 1e-9))) | 1)
    q = smooth(np.vstack([p[0], *(point_at(p, np.arange(step, cumlen(p)[-1], step))), p[-1]])
               if cumlen(p)[-1] > 2 * step else p, win)
    q = simplify(q, max(0.25 * step, 0.02 * cell))
    # trimmed after smoothing: the raw walk zig-zags a little from step to step, so its
    # length overstates the crest's
    if keep_len is not None and cumlen(q)[-1] > keep_len + 1e-9:
        L = cumlen(q)[-1]
        s = station_of(q, centre) if centre is not None else 0.5 * L
        s = min(max(s, 0.5 * keep_len), L - 0.5 * keep_len)
        q = sub_line(q, s - 0.5 * keep_len, s + 0.5 * keep_len)
    return min_spacing(q, cell)


def pick_breach(line, sample, toe, band, half_bottom, half_top, top, tol, method="auto",
                step=None):
    """(station, how, detail) of the breach centre along the crest line: 'toe' (default via
    'auto') the deepest dam section, i.e. the lowest ground at the downstream toe (toe..toe +
    band to the left of the line), averaged over the breach bottom; 'lowest_crest' the lowest
    crest (averaged the same way). Only stations at least half the breach's top width from the
    ends; the embankment part of the line (crest within tol of the top) first."""
    p = np.asarray(line, float).reshape(-1, 2)
    L = float(cumlen(p)[-1])
    step = float(step or max(L / 400.0, 1e-6))
    s = np.arange(0.0, L + 1e-9, step)
    if len(s) < 3:
        return 0.5 * L, "the middle of the dam line (too short to choose)", {}
    xy = point_at(p, s)
    zc = sample(xy)
    win = max(1, int(round(half_bottom / step)))
    ker = np.ones(2 * win + 1)

    def avg(v):
        ok = np.isfinite(v).astype(float)
        num = np.convolve(np.where(np.isfinite(v), v, 0.0), ker, "same")
        den = np.convolve(ok, ker, "same")
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(den > 0, num / den, np.nan)

    lo, hi = half_top, L - half_top
    inner = (s >= lo - 1e-9) & (s <= hi + 1e-9) if hi > lo else np.abs(s - 0.5 * L) <= step
    crest = inner & (np.abs(zc - top) <= tol)
    pool = crest if crest.any() else inner
    m = str(method or "auto").lower()
    detail = {}
    if m in ("auto", "toe"):
        n = np.array([left_normal(tangent_at(p, v)) for v in s])
        tt = np.linspace(toe, toe + band, 5)
        zt = np.nanmin(np.vstack([sample(xy + n * t) for t in tt]), axis=0) \
            if np.isfinite(sample(xy + n * toe)).any() else np.full(len(s), np.nan)
        za = avg(zt)
        if np.isfinite(za[pool]).any():
            k = np.nonzero(pool)[0][int(np.nanargmin(za[pool]))]
            detail = {"toe_ground": round(float(za[k]), 2),
                      "toe_ground_range": [round(float(np.nanmin(za[pool])), 2),
                                           round(float(np.nanmax(za[pool])), 2)]}
            return float(s[k]), ("the deepest dam section along the crest (the lowest ground at "
                                 "the downstream toe, where the old channel crosses)"), detail
        m = "lowest_crest"
    za = avg(zc)
    if np.isfinite(za[pool]).any():
        k = np.nonzero(pool)[0][int(np.nanargmin(za[pool]))]
        return float(s[k]), "the lowest crest along the traced dam line", {
            "crest_ground": round(float(za[k]), 2)}
    return 0.5 * L, "the middle of the dam line (no ground to choose by)", {}


def extend(line, span, axis, min_dot=0.5):
    """The polyline with a straight extension `span` long at each end, along its end
    segments (along `axis` where an end segment turns more than acos(min_dot) from it), for
    cutting the 2D area."""
    p = np.asarray(line, float).reshape(-1, 2)
    axis = np.asarray(axis, float) / (np.hypot(*axis) or 1.0)
    ta = p[1] - p[0]
    ta = ta / (np.hypot(*ta) or 1.0)
    tb = p[-1] - p[-2]
    tb = tb / (np.hypot(*tb) or 1.0)
    if ta @ axis < min_dot:
        ta = axis
    if tb @ axis < min_dot:
        tb = axis
    return np.vstack([p[0] - ta * span, p, p[-1] + tb * span])
