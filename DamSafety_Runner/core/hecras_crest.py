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
# 3.10.87: a dam line traced off the old rule (where that rule finds no crest: from other
# crest-high ground near the dam point - snap -, at the DEM's level off the record, or on the
# dam point's own ridge over a higher one) is used only with evidence() that it is the dam;
# one part is flow_check: of the run's flooded cells within two line lengths, at least
# FLOW_DOWN_FRAC on its downstream side, their centroid within FLOW_ANGLE_MAX_DEG of its
# downstream normal, no ridge between the line and the flood (a causeway across the pool has
# the dam between it and the flood)
FLOW_DOWN_FRAC = 0.8
FLOW_ANGLE_MAX_DEG = 60.0
FLOW_LOCAL_MAX_DEG = 65.0     # ... and no more than one of ten parts off it by more than this
FLOW_MIN_CELLS = 10
# 3.10.87 (evidence): the flow path below the breach (flow_path) within this of the line's
# downstream normal; its steps look this far either side of the last one
FLOW_PATH_MAX_DEG = 45.0
FLOW_PATH_CONE_DEG = 60.0
# ... and another crest-high ridge or level top beside the line, or crest-high ground behind
# it, in at most this fraction of its sections
PARALLEL_MAX_FRAC = 0.25


class CrestTraceError(ValueError):
    """The crest could not be traced; the message says why (build_model falls back).

    3.10.87: on_ridge is True when the dam point is on the embankment where the trace found no
    crest it may follow - on a ridge that stands above the ground on both sides of the guessed
    axis but is off the record's level (the crest is there and reads off the record: a coarse
    DEM, another datum), on ground at the record's level that is no clear crest, or within the
    toe distance of a level top (a wide crest) - so no start from other high ground nearby is
    tried; snap_tried records a start from other crest-high ground that was tried and given
    up."""

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
    """3.10.87: `wide` given (a trace off the old rule, see trace): see _ridge and _judge, and
    crest found past a stretch stepped over at the end of the walk counts only when it is at
    least two steps long and as long as that stretch (a high point or two past the end of the
    embankment - a knoll on the abutment - is not its crest)."""
    pts = [np.asarray(X0, float)]
    X, tau = pts[0], np.asarray(tau0, float) / (np.hypot(*tau0) or 1.0)
    walked, run, last_ok, why, nstep = 0.0, 0.0, 0, "the dam length", 0
    before_gap, since_gap, gap_len = 0, None, 0.0
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
            if run > 0.0:
                before_gap, since_gap, gap_len = last_ok, 0, run
            if since_gap is not None:
                since_gap += 1
            last_ok, run = len(pts) - 1, 0.0
        else:
            run += h
            if run > gap + 1e-9 or bad == "edge of the DEM":
                why = bad
                break
            X = Y                                   # straight on over the gap
            walked += h
            pts.append(Y)
    if wide is not None and since_gap is not None and \
            since_gap * step < max(2.0 * step, gap_len) - 1e-9:
        last_ok = before_gap
    return np.vstack(pts[:last_ok + 1]), why, walked


def _level_tops(zz, tt, w_min, flat, fall, reach):
    """3.10.87: the level tops along a section (samples zz at offsets tt, ascending): stretches
    at least w_min long whose ground varies by no more than `flat`, beyond an end of which the
    ground falls at least `fall` below them within `reach` - a wide crest, also one a higher
    road on its edge keeps from being a ridge, or a bench at the crest's level; not a pool
    surface (level, but nothing falls beyond it) and not a sloping face. [(t0, t1, z_mean)]"""
    zz, tt = np.asarray(zz, float), np.asarray(tt, float)
    n, out, i = len(zz), [], 0
    while i < n:
        if not np.isfinite(zz[i]):
            i += 1
            continue
        lo = hi = zz[i]
        j = i
        while j + 1 < n and np.isfinite(zz[j + 1]) and \
                max(hi, zz[j + 1]) - min(lo, zz[j + 1]) <= flat:
            j += 1
            lo, hi = min(lo, zz[j]), max(hi, zz[j])
        if tt[j] - tt[i] >= w_min - 1e-9:
            zm = float(np.mean(zz[i:j + 1]))
            after = zz[(tt > tt[j]) & (tt <= tt[j] + reach)]
            before = zz[(tt < tt[i]) & (tt >= tt[i] - reach)]
            if any(np.any(np.isfinite(v) & (v <= zm - fall)) for v in (after, before)):
                out.append((float(tt[i]), float(tt[j]), zm))
        i = j + 1
    return out


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
    nothing high enough within reach, P on the face of another ridge on the side away from it -
    the embankment P is on reads below the record -, or high ground with no clear long axis: a
    hill)."""
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
    if prom is not None:
        # the dam point on the face of another ridge, on the side away from Q (an embankment
        # that reads below the record, a road beyond it at the record's level): it is on that
        # one - the ground rises from P by `prom` to a top and falls behind it within `search`
        u = (P - Q) / (np.hypot(*(P - Q)) or 1.0)
        tt = np.arange(0.0, float(search) + 1e-9, 0.5 * step)
        zb = sample(P[None, :] + tt[:, None] * u[None, :])
        zb = zb[np.isfinite(zb)]
        if len(zb) > 2:
            k = int(np.argmax(zb))
            if zb[k] - zb[0] >= prom and np.any(zb[k:] <= zb[k] - 0.5 * prom):
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
    FLOW_ANGLE_MAX_DEG of its downstream normal (seen from the line's middle), and of the
    normals of no more than one of ten parts along it more than FLOW_LOCAL_MAX_DEG (an arm along
    the flow) - a line along the valley with the flood on both sides fails; one with the flood
    on one side only can pass, so evidence() also follows the flow path. When `dam_point` is
    given: it is not more than `beyond`
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


def flow_path(sample, X0, heading, length, r, cone=FLOW_PATH_CONE_DEG):
    """3.10.87: the way the water goes from X0 over the DEM: steps of r, each to the lowest
    ground on an arc within `cone` degrees of the last step's direction (always forward: over a
    hollow or a low rise, not into it, so a thalweg is followed down a valley that the
    ground's local slope points across), until `length` or the edge of the DEM. Returns the
    points (X0 first)."""
    X = np.asarray(X0, float)
    h = np.asarray(heading, float) / (np.hypot(*heading) or 1.0)
    angs = np.radians(np.linspace(-cone, cone, 13))
    pts, walked = [X.copy()], 0.0
    while walked < length - 1e-9:
        c, s = np.cos(angs), np.sin(angs)
        dirs = np.column_stack([h[0] * c - h[1] * s, h[0] * s + h[1] * c])
        cand = X[None, :] + r * dirs
        z = sample(cand)
        if not np.isfinite(z).all():
            break
        k = int(np.argmin(z + 1e-4 * r * np.abs(angs)))   # straight on between equal lows
        X, h = cand[k], dirs[k]
        walked += r
        pts.append(X.copy())
    return np.vstack(pts)


def evidence(line, sc, sample, P, top_rec, top_used, tol, prom, height, toe, crest_w, step,
             ft=1.0, wet_xy=None, wet_cell=None):
    """3.10.87: the positive evidence a dam line traced off the old rule (trace's 'relaxed', or
    from a start found away from the dam point) needs before it is used - the old rule found no
    crest, so the line may be another embankment (a road or a dike beside the dam, a levee
    along the valley, a causeway across the pool). line: A -> B (the downstream side on its
    left), sc: the breach station on it, P: the dam point as given. All of:
    (1) the dam point lies within the toe distance plus half the crest width and half a step
        of it (on its crest, its faces or the pool just above it);
    (2) the ground climbs from the dam point to it (no other ridge in between);
    (3) the run's flooded cells (wet_xy) lie on its downstream side (flow_check);
    (4) the flow path from its downstream toe at the breach (flow_path, about 1.5 line lengths,
        at least 1,000 ft) leaves it within FLOW_PATH_MAX_DEG of square: a line along the
        valley has the flow path along it, however the flooded area lies;
    (5) no other ridge or level top (_level_tops) at least crest-high (the record or the traced
        level, whichever is lower, less the tolerance) within twice the toe distance plus the
        crest width of it, on either side, in more than PARALLEL_MAX_FRAC of its sections (a
        road or a dike beside it, a road on the edge of a wide crest: which one is the dam is
        not clear; the real dam upstream of a road), and no crest-high ground on its upstream
        side beyond the toe distance in more than that fraction (a levee along the valley has
        high ground behind it, a dam its pool).
    Returns (ok, info): dam_point_to_line, flow_path_angle_deg, flow_path_length,
    parallel_ridge_frac, upstream_high_frac, flow (flow_check's info), why."""
    p = np.asarray(line, float).reshape(-1, 2)
    P = np.asarray(P, float)
    L = float(cumlen(p)[-1])
    info = {}
    dd = float(_seg_nearest(p, P.reshape(1, 2))[0][0])
    info["dam_point_to_line"] = round(dd, 1)
    reach = toe + 0.5 * crest_w + 0.5 * step
    if dd > reach:
        return False, dict(info, why="the dam point is {:,.0f} from it, more than the toe "
                           "distance plus half the crest width ({:,.0f})".format(dd, reach))
    sP = station_of(p, P)
    X = point_at(p, sP)
    if dd > step and not _rises(sample, P, X, 0.5 * prom, 0.5 * step):
        return False, dict(info, why="the ground does not climb to it from the dam point "
                           "(another ridge lies between them)")
    ok, fi = flow_check(p, wet_xy, wet_cell, sample=sample, crest=top_used, tol=tol, toe=toe,
                        step=0.25 * step, drop=max(tol, 0.5 * height) + tol, prom=0.5 * prom,
                        dam_point=P, beyond=toe + 0.5 * crest_w)
    info["flow"] = fi
    if not ok:
        return False, dict(info, why=fi["why"])
    # (4) the flow path from the toe at the breach
    B = point_at(p, sc)
    nb = left_normal(tangent_at(p, sc))
    D = max(1000.0 * ft, 1.5 * L)
    path = flow_path(sample, B + toe * nb, nb, D, max(2.0 * step, D / 30.0))
    walked = float(cumlen(path)[-1])
    info["flow_path_length"] = round(walked, 1)
    if walked < min(D, max(300.0 * ft, 3.0 * toe)):
        return False, dict(info, why="no flow path below it on the DEM ({:,.0f} long)".format(
            walked))
    v = path[-1] - B
    ang = float(np.degrees(np.arccos(np.clip(float(v @ nb) / (np.hypot(*v) or 1.0), -1.0,
                                             1.0))))
    info.update(flow_path_angle_deg=round(ang, 1),
                flow_path_end=[round(float(q), 2) for q in path[-1]])
    if ang > FLOW_PATH_MAX_DEG:
        return False, dict(info, why="the flow path below its breach leaves it {:.0f} degrees "
                           "off square (it runs along the flow)".format(ang))
    # (5) other ridges beside it, high ground behind it
    W = 2.0 * toe + crest_w
    ds = 0.5 * step
    tt = np.arange(-(W + toe), W + toe + 1e-9, ds)
    lvl = min(top_rec, top_used) - tol
    own_w = 0.5 * crest_w + step
    w_lt = max(0.5 * crest_w, 2.0 * step)
    sides = (np.nonzero(tt >= 0)[0], np.nonzero(tt <= 0)[0][::-1])    # from the line out
    par = high = n = 0
    for s in np.linspace(0.1 * L, 0.9 * L, 9):
        Xs = point_at(p, s)
        n_ = left_normal(tangent_at(p, s))
        zz = sample(Xs[None, :] + tt[:, None] * n_[None, :])
        if not np.isfinite(zz[np.abs(tt) <= own_w]).any():
            continue
        n += 1
        up = (tt <= -toe) & (tt >= -W)
        high += bool(np.any(np.isfinite(zz[up]) & (zz[up] >= lvl)))
        found = False
        for idx in sides:
            zs, ts = zz[idx], np.abs(tt[idx])
            fin = np.isfinite(zs)
            zs, ts = zs[fin], ts[fin]
            # off the line's own top (its crest, its face) ...
            off = np.nonzero(np.maximum.accumulate(zs) - zs >= 0.5 * prom)[0] \
                if len(zs) > 2 else []
            if not len(off):
                continue
            zr, tr_ = zs[off[0]:], ts[off[0]:]
            # ... then up again onto another ridge at least crest-high, within W, or a level
            # top at least crest-high (a wide crest beside a road on its edge)
            rise = zr - np.minimum.accumulate(zr)
            found = found or bool(np.any((rise >= 0.5 * prom) & (zr >= lvl) & (tr_ <= W))) or \
                any(zm >= lvl and t0 <= W for t0, _, zm in
                    _level_tops(zr, tr_, w_lt, 0.25 * prom, prom, toe))
        par += found
    info.update(sections=n, parallel_ridge_frac=round(par / n, 3) if n else None,
                upstream_high_frac=round(high / n, 3) if n else None)
    if n and par > PARALLEL_MAX_FRAC * n:
        return False, dict(info, why="another ridge at least crest-high runs beside it within "
                           "{:,.0f} along {} of {} sections (a road or a dike beside the dam: "
                           "which one is the dam is not clear)".format(W, par, n))
    if n and high > PARALLEL_MAX_FRAC * n:
        return False, dict(info, why="crest-high ground lies behind it (on its upstream side, "
                           "beyond the toe distance) along {} of {} sections: a dam has its "
                           "pool there".format(high, n))
    return True, dict(info, why="")


def trace(sample, P, axis, top, height, step, toe, max_len, gap, tol=None, prom=None,
          search=None, ft=1.0, near=None, relax=True, crest_w=None, start_within=None,
          dam_ground=None):
    """The crest polyline through the dam point. sample(xy) -> z (model units); axis: unit
    vector along the dam from A (the left end looking downstream) to B; top, height: top of dam
    and dam height; step: walk step; toe: the distance from the crest to the toe (for the
    ridge test); max_len: the dam length; gap: stretches off the crest stepped over; ft: model
    units per foot. Returns {'line' (A..B, raw walk), 'p0', 'ends': {'A', 'B'}: (why, length),
    'snap', 'top_used' (the level the crest was followed at), 'top_offset' (that minus the top
    of dam on record), 'relaxed', 'how'}. Raises CrestTraceError.

    The old rule (the highest ridge across the section, at the record's level, standing above
    the ground on both sides) decides wherever it finds a crest: the result is then the old
    rule's trace, whatever `relax` says. 3.10.87, only where it finds none (relax, the
    default; relax=False is the old rule alone):
    - 'datum': the crest is followed at the DEM's own level when that is no more than
      max(DATUM_MIN_FT, DATUM_FRAC x H) above the record and the ridge stands above the ground
      on both sides at that level (another vertical datum: DWR-2801's DEM crest is 3-6 ft
      above its record) - the ridge the dam point is on (the nearest ridge across the section
      within `near`, default max(toe / 2, step), of it) if there is one, else the highest. Not
      when the ground at the dam point (dam_ground: the dam point's ground when P is a start
      found elsewhere; default the ground at P) is within the tolerance of the record, nor when
      a ridge at the record's level lies across the section within the toe distance of it: the
      dam's crest is there, and a higher ridge beside it is another embankment (a road).
    - 'own': the dam point's own ridge at the record's level, standing above both sides, over a
      higher ridge across the section that the datum rule would take.
    When the dam point is on a ridge that is not followed (off the record's level - below it:
    a narrow crest that a coarse DEM reads low, a record above the DEM's crest -, or at it but
    not standing above both sides), or within the toe distance of a level top no more than half
    the dam height below the record (_level_tops: a wide crest, also one that a road on its
    edge keeps from being a ridge), the error says on_ridge: the crest is there, so no start
    from other high ground nearby is tried. 'relaxed' / 'how' say which way the trace left the
    old rule ('' when it did not); such a line still needs positive evidence that it is the
    dam (evidence(), in crest_dam_line). crest_w: the crest width on record - the walk of a
    trace off the old rule (relaxed, or with start_within) keeps to the middle of a level top
    wider than its window, and takes a side of the ridge test that runs into no data (a pool
    left out of the DEM) as falling. start_within: the crest the trace starts from must be at
    most this far from P (a start from crest-high ground found nearby: not a higher ridge
    across the section)."""
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
    msg = ("no embankment crest within {:.0f} of the dam point across the dam ({}; highest "
           "ground {:.2f}, top of dam {:.2f})".format(search, why, z0, top))
    top_rec = top
    on_ridge, how = False, ""
    if why and relax:
        allow = max(DATUM_MIN_FT * ft, DATUM_FRAC * height)
        zP = float(sample(P[None, :])[0]) if dam_ground is None else float(dam_ground)
        datum_ok = not (np.isfinite(zP) and abs(zP - top) <= tol)
        rises = why.startswith("abutment: the ridge rises") and z0 - top <= allow
        ridges = _ridges(sample, P, nu, search, 0.5 * step)
        own = [r for r in ridges if abs(r[2]) <= near]
        # a ridge at the record's level across the section within the toe distance of the dam
        # point: the dam is there - a higher ridge is not followed at its own level
        rec_near = any(abs(z_ - top) <= tol and abs(t_) <= toe for _, z_, t_ in ridges)
        if own:
            pn, zn, _ = own[0]
            other = float(np.hypot(*(pn - p0))) > step
            if abs(zn - top) <= tol:
                if other and rises and datum_ok and \
                        not _judge(sample, pn, nu, zn, top, tol, toe, prom):
                    p0, z0, why, how = pn, zn, "", "own"
                else:
                    on_ridge = True
            elif not _judge(sample, pn, nu, zn, zn, tol, toe, prom):
                # a ridge off the record's level: only it may be followed, at its own level
                # (never another, higher one nearby: a road beside a low dam on a coarse DEM)
                on_ridge = True
                p0, z0 = pn, zn
                why = _judge(sample, p0, nu, z0, top, tol, toe, prom)
                rises = why.startswith("abutment: the ridge rises") and z0 - top <= allow
        if not how and datum_ok and rises and not rec_near and \
                not _judge(sample, p0, nu, z0, z0, tol, toe, prom):
            top, why, how = z0, "", "datum"
        on_ridge = on_ridge or (rec_near and not how)
        if not how and not on_ridge:
            # a level top within the toe distance of the dam point, no more than half the dam
            # height below the record (a wide crest, also one that a higher road on its edge
            # keeps from being a ridge): the dam point is on the embankment, its crest is there
            w = max(0.5 * float(crest_w or 0.0), 2.0 * step)
            r_ = 3.0 * toe + float(crest_w or 0.0)
            tt = np.arange(-r_, r_ + 1e-9, 0.5 * step)
            zz = sample(P[None, :] + tt[:, None] * nu[None, :])
            low = top - max(tol, 0.5 * height) - tol
            on_ridge = any(zm >= low and t0 <= toe and t1 >= -toe
                           for t0, t1, zm in _level_tops(zz, tt, w, 0.25 * prom, prom, toe))
    if not why and start_within is not None and float(np.hypot(*(p0 - P))) > start_within:
        why = "the crest across the section is {:.0f} away, another ridge".format(
            float(np.hypot(*(p0 - P))))
        msg = "no embankment crest within {:.0f} of the start ({})".format(start_within, why)
    if why:
        raise CrestTraceError(msg, on_ridge=on_ridge)
    # the walk keeps to the old rule unless the trace is off it (relaxed, or a start from
    # crest-high ground found nearby): only then the wide-crest and no-data rules (3.10.87)
    wide = crest_w if (how or start_within is not None) else None
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
            "relaxed": bool(how), "how": how}


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
