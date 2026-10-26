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


class CrestTraceError(ValueError):
    """The crest could not be traced; the message says why (build_model falls back)."""


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
def _ridge(sample, Y, nu, half, ds):
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
    if j > i:
        R = Y + 0.5 * (tt[i] + tt[j]) * nu
        zr = float(sample(R[None, :])[0])
        if np.isfinite(zr):
            return R, zr
    return Y + tt[k] * nu, float(zz[k])


def _judge(sample, R, nu, zr, top, tol, toe, prom):
    """('', z) on the crest, else (why, z)."""
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
    sides = [np.nanmin(v) if np.isfinite(v).any() else np.nan for v in (lo_l, lo_r)]
    hi = np.nanmax(sides) if np.isfinite(sides).any() else np.nan
    if not np.isfinite(hi) or zr - hi < prom:
        return "abutment tie-in: the ground beside the crest is as high as the crest"
    return ""


def _walk(sample, X0, tau0, max_len, step, toe, top, tol, prom, gap):
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
        rr = _ridge(sample, Y, nu, h * math.tan(math.radians(ang)), ds)
        nstep += 1
        bad = "edge of the DEM" if rr is None else _judge(sample, rr[0], nu, rr[1], top, tol,
                                                          toe, prom)
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


def snap(sample, P, top, tol, search, half, step):
    """3.10.87: a dam point off the embankment (on the pool or the toe: DWR-2801's dam.json
    point is on the pool, about 100 ft from the crest) with no ridge across the guessed axis.
    When the ground at P is more than `tol` below the top of dam: the nearest ground within
    `search` that is at least that high, and the embankment's direction there (the long axis
    of that high ground within `half` of it). Returns (point, unit axis) or None (P on the
    crest, nothing high enough, or high ground with no clear long axis: a hill)."""
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
    dist = np.hypot(*(xy - P).T)
    cand = high & (dist <= search)
    if not cand.any():
        return None
    Q = xy[int(np.argmin(np.where(cand, dist, np.inf)))]
    loc = high & (np.hypot(*(xy - Q).T) <= half)
    if loc.sum() < 3:
        return None
    pts = xy[loc]
    w, v = np.linalg.eigh(np.cov((pts - pts.mean(axis=0)).T))
    if w[0] > 0 and w[1] / w[0] < 2.0:
        return None
    t = v[:, 1]
    return Q, t / np.hypot(*t)


def trace(sample, P, axis, top, height, step, toe, max_len, gap, tol=None, prom=None,
          search=None, ft=1.0):
    """The crest polyline through the dam point. sample(xy) -> z (model units); axis: unit
    vector along the dam from A (the left end looking downstream) to B; top, height: top of dam
    and dam height; step: walk step; toe: the distance from the crest to the toe (for the
    ridge test); max_len: the dam length; gap: stretches off the crest stepped over; ft: model
    units per foot. Returns {'line' (A..B, raw walk), 'p0', 'ends': {'A', 'B'}: (why, length),
    'snap'}. Raises CrestTraceError."""
    axis = np.asarray(axis, float) / (np.hypot(*axis) or 1.0)
    tol = float(tol if tol is not None else max(3.0 * ft, 0.1 * height))
    prom = float(prom if prom is not None else max(PROM_MIN_FT * ft, PROM_FRAC * height))
    search = float(search if search is not None else max(toe, 3.0 * step))
    P = np.asarray(P, float)
    nu = left_normal(axis)
    rr = _ridge(sample, P, nu, search, 0.5 * step)
    if rr is None:
        raise CrestTraceError("no DEM around the dam point")
    p0, z0 = rr
    why = _judge(sample, p0, nu, z0, top, tol, toe, prom)
    top_rec = top
    allow = max(DATUM_MIN_FT * ft, DATUM_FRAC * height)
    if why.startswith("abutment: the ridge rises") and z0 - top <= allow and not _judge(
            sample, p0, nu, z0, z0, tol, toe, prom):
        top, why = z0, ""
    if why:
        raise CrestTraceError("no embankment crest within {:.0f} of the dam point across the "
                              "dam ({}; highest ground {:.2f}, top of dam {:.2f})".format(
                                  search, why, z0, top))
    a, why_a, _ = _walk(sample, p0, -axis, max_len, step, toe, top, tol, prom, gap)
    b, why_b, _ = _walk(sample, p0, axis, max_len, step, toe, top, tol, prom, gap)
    line = np.vstack([a[::-1], b[1:]])
    la, lb = float(cumlen(a)[-1]), float(cumlen(b)[-1])
    if la + lb < 2.0 * step:
        raise CrestTraceError("the crest could not be followed from the dam point ({} / {})"
                              .format(why_a, why_b))
    return {"line": line, "p0": p0, "z0": z0, "snap": float(np.hypot(*(p0 - P))),
            "ends": {"A": (why_a, la), "B": (why_b, lb)}, "tol": tol, "prom": prom,
            "top_used": float(top), "top_offset": float(top - top_rec)}


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
