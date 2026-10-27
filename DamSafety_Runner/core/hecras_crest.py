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
# 3.10.87 (evidence): the run's flow at the valley's scale - the direction from the line's
# middle and from the dam point's place on it to the run's flooded cells within
# max(VALLEY_MIN_FT, 1.5 line lengths), and the flow path from the breach's downstream toe
# (flow_path, as long, never cut short by the DEM's edge) - each less than FLOW_PATH_MAX_DEG
# off the line's downstream normal, the first and the last no more than FLOW_AGREE_DEG apart;
# the flow path's steps look FLOW_PATH_CONE_DEG either side of the last one
FLOW_PATH_MAX_DEG = 40.0
FLOW_AGREE_DEG = 40.0
FLOW_PATH_CONE_DEG = 60.0
VALLEY_MIN_FT = 1000.0
# ... another ridge or level top beside the line, another embankment between it and the pool
# (looked for max(UPSTREAM_SCAN_FT, 1.5 line lengths) upstream of it and as far again where
# the DEM goes on, back to the pool), crest-high ground behind it, or another ridge between it
# and the flood, in at most this fraction of its sections
PARALLEL_MAX_FRAC = 0.25
UPSTREAM_SCAN_FT = 1500.0
# ... and the walk of a trace off the old rule stops at a junction - where its heading turns
# more than JUNCTION_TURN_DEG from the one it settled on while the crest also goes on straight
# past the bend's footprint (back to where the turn began), where a level top is wider than
# JUNCTION_WIDE_FRAC x the crest width on record (another embankment joins the crest there:
# the walk does not turn onto it), or where another crest at its level lies beside it (DWR
# #195 audit round 4: a road at a small angle, see _walk)
JUNCTION_TURN_DEG = 20.0
JUNCTION_WIDE_FRAC = 1.5
# ... and its breach goes at its deepest section, also looked for along normals turned up to
# FAN_DEG (in FAN_STEP_DEG steps; used where they find ground lower by more than FAN_GAIN ft than
# the plain normals do), which must be clear
FAN_DEG = 30.0
FAN_STEP_DEG = 10.0
FAN_GAIN = 0.25
# 3.10.87 (DWR #195 audit round 4), measured from no dam line and at any level:
# - the run's flow at the valley's scale is the direction from the dam point to the centroid of
#   all the run's flooded cells (valley_axis); a line off the old rule is no more than
#   VALLEY_SQUARE_MAX_DEG off square to it, and where the DEM shows no pool on its upstream side
#   the flooded cells' long axis must be clear (its spread at least VALLEY_AXIS_RATIO times the
#   spread across it) and no more than that off square to it too
VALLEY_SQUARE_MAX_DEG = 45.0
VALLEY_AXIS_RATIO = 1.5
# - the pool on the upstream side: water (a level surface, within POOL_FLAT_FT over at least
#   POOL_FLAT_LEN_FT: LiDAR's flattened water) at least the crest prominence below the crest,
#   or no data inside the DEM (a pool left out of it), within twice the toe distance plus the
#   crest width of the line
POOL_FLAT_FT = 0.01
POOL_FLAT_LEN_FT = 100.0
# - another embankment is a top standing at least EMB_RISE_FRAC x the reference height (the
#   lower of the dam height on record and the line's own height above its downstream toe)
#   above the lowest ground within EMB_SPAN_TOES toe distances on one side, and at least half
#   the crest prominence on the other (_tops)
EMB_RISE_FRAC = 0.5
EMB_SPAN_TOES = 1.5
# - a line no longer than FRAGMENT_FRAC of the crest length on record, or one whose crest goes
#   on at its level past its ends for longer than the line itself, is a part of the embankment
FRAGMENT_FRAC = 0.5


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
def _ridge(sample, Y, nu, half, ds, wide=None, rise=None):
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
        # more than the window allows (the turn limit) - only while the top is about as wide
        # as the crest on record: a wider one (or one whose edges never show past that) is the
        # crest with another embankment joining it, a junction (returned as ('junction',
        # width)), where the middle of the combined top leads onto the other one - DWR #195
        # audit round 4: a junction when the top falls on both sides; one that runs to the
        # widest window's end into ground more than `rise` above the crest is the abutment's
        # hillside ('abutment', width); one that runs on at its level past that window's end
        # is the abutment's shoulder or an embankment along the section ('level', width)
        lim = max(JUNCTION_WIDE_FRAC * float(wide), float(wide) + 2.0 * ds)

        def wider(a, b, zw, tw):
            w_ = float(tw[b] - tw[a])
            if a > 0 and b < len(zw) - 1:
                return "junction", w_
            hill = rise is not None and ((a == 0 and zw[0] > zb[k] + rise) or
                                         (b == len(zw) - 1 and zw[-1] > zb[k] + rise))
            return ("abutment" if hill else "level"), w_
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
                if tw[b] - tw[a] > lim:
                    return wider(a, b, zwb, tw)
                R = Y + float(np.clip(0.5 * (tw[a] + tw[b]), -half, half)) * nu
                zr = float(sample(R[None, :])[0])
                if np.isfinite(zr):
                    return R, zr
                break
            if f == 8.0 and tw[b] - tw[a] > lim:
                # its edges never show: classified at the widest window (a top that fills a
                # narrower one may still fall on both sides past it)
                return wider(a, b, zwb, tw)
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


def _crest_ahead(sample, X, tau, step, top, tol, toe, prom, past):
    """3.10.87: the crest goes on straight from X along tau: a ridge at the walk's level that
    stands above both sides from `past` ahead on for a toe distance more, at every step (within
    the normal turn limit) - past the footprint of a bend (a crest that turns is still crest a
    step or two straight on, across its own width) and past a spur of the abutment, so only a
    crest that goes on (a road crossing it)."""
    nu = left_normal(tau)
    for k in range(0, 2 + int(math.ceil(float(toe) / float(step)))):
        Y = X + tau * (float(past) + k * step)
        rr = _ridge(sample, Y, nu, step * math.tan(math.radians(TURN_MAX_DEG)), 0.5 * step)
        if rr is None or _judge(sample, rr[0], nu, rr[1], top, tol, toe, prom, True):
            return False
    return True


def _walk(sample, X0, tau0, max_len, step, toe, top, tol, prom, gap, wide=None):
    """3.10.87: `wide` given (a trace off the old rule, see trace): see _ridge and _judge, and
    crest found past a stretch stepped over at the end of the walk counts only when it is at
    least two steps long and as long as that stretch (a high point or two past the end of the
    embankment - a knoll on the abutment - is not its crest); the walk stops at a junction
    (see JUNCTION_TURN_DEG; why starts with 'a junction'): a heading that turns more than
    JUNCTION_TURN_DEG from the one it settled on (after the first steps, or after a bend) where
    the crest also goes on straight past the bend's footprint (a road crossing it: the walk
    ends where the turn began), or a level top wider than the crest on record (_ridge). DWR
    #195 audit round 4: also where another crest at the walk's level, standing above the ground
    on both sides, lies across the section more than half the crest width on record and a step
    beside it, within the toe distance (another embankment joining, leaving or crossing it at a
    small angle, below the turn limit): the walk ends where it drifted off that other crest's
    line (the line through it along the heading the walk settled on: back to where it left the
    dam, when it has turned onto a road), or here when it never was on it; and a level top that
    runs into ground higher than the crest is the abutment ('abutment tie-in: ...'), not a
    junction."""
    pts = [np.asarray(X0, float)]
    X, tau = pts[0], np.asarray(tau0, float) / (np.hypot(*tau0) or 1.0)
    walked, run, last_ok, why, nstep = 0.0, 0.0, 0, "the dam length", 0
    before_gap, since_gap, gap_len = 0, None, 0.0
    ds = 0.5 * step
    t_ref, i_ref = None, 0
    t_prev = None                     # the heading before the last bend, and where it was
    sep_min = None if wide is None else 0.5 * float(wide) + step
    while walked < max_len - 1e-9:
        h = min(step, max_len - walked)
        if h < 0.25 * step:
            break
        Y = X + tau * h
        nu = left_normal(tau)
        ang = TURN_FIRST_DEG if nstep < FIRST_STEPS else TURN_MAX_DEG
        rr = _ridge(sample, Y, nu, h * math.tan(math.radians(ang)), ds, wide,
                    tol if wide is not None else None)
        nstep += 1
        if rr is not None and isinstance(rr[0], str):
            if rr[0] == "abutment":
                why = ("abutment tie-in: the crest's level top ({:.0f} wide) runs into ground "
                       "higher than the crest".format(rr[1]))
            elif rr[0] == "level":
                why = ("the crest's level top is more than {:.0f} wide, running on at its "
                       "level (the abutment's shoulder, or an embankment joining it)"
                       .format(rr[1]))
            else:
                why = ("a junction: the level top is {:.0f} wide, wider than the crest on "
                       "record (another embankment joins it)".format(rr[1]))
            break
        bad = "edge of the DEM" if rr is None else _judge(sample, rr[0], nu, rr[1], top, tol,
                                                          toe, prom, wide is not None)
        if not bad:
            R = rr[0]
            v = R - X
            v = v / (np.hypot(*v) or 1.0)
            tau_new = tau + v
            tau_new = tau_new / (np.hypot(*tau_new) or 1.0)
            if wide is not None and nstep > FIRST_STEPS:
                # the heading against the one it settled on (t_ref), and where it last was
                # within half the junction limit of it (i_ref: where a turn began)
                if t_ref is None:
                    t_ref, i_ref = tau_new.copy(), len(pts)
                else:
                    turn = float(np.degrees(np.arccos(np.clip(float(tau_new @ t_ref), -1.0,
                                                              1.0))))
                    if turn > JUNCTION_TURN_DEG:
                        if _crest_ahead(sample, pts[i_ref], t_ref, step, top, tol, toe, prom,
                                        toe + float(wide)):
                            why = ("a junction: the crest turns {:.0f} degrees where it also "
                                   "goes on straight (another embankment crosses it)".format(turn))
                            # back to where the turn began
                            last_ok, since_gap = min(last_ok, i_ref), None
                            break
                        t_prev = (t_ref, len(pts))
                        t_ref, i_ref = tau_new.copy(), len(pts)       # a bend: on from here
                    elif turn <= 0.5 * JUNCTION_TURN_DEG:
                        i_ref = len(pts)
                    other = [(abs(tq), Q) for Q, zq, tq in _ridges(sample, R, nu, 0.5 * float(
                        wide) + toe, ds) if abs(tq) > sep_min and abs(zq - top) <= tol and
                        not _judge(sample, Q, nu, zq, top, tol, toe, prom, True)]
                    if other:
                        d_o, Q = min(other, key=lambda o: o[0])
                        why = ("a junction: another crest at its level lies {:.0f} beside it "
                               "(another embankment joins, leaves or crosses it)".format(d_o))
                        # back to the last point within half that distance of the other
                        # crest's line (where the walk left it); none, and the walk bent since
                        # it settled: the line along the heading before that bend, through
                        # the points before it (the walk drifted onto a crest leaving the dam
                        # and bent where the two tops parted); else stop here
                        k = [i for i in range(len(pts)) if abs(float(np.cross(
                            t_ref, pts[i] - Q))) <= 0.5 * sep_min]
                        if not k and t_prev is not None:
                            k = [i for i in range(t_prev[1]) if abs(float(np.cross(
                                t_prev[0], pts[i] - Q))) <= 0.5 * sep_min]
                        if k:
                            last_ok, since_gap = min(last_ok, k[-1]), None
                        break
            tau = tau_new
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


def stations_of(line, pts):
    """3.10.87: station_of for many points at once (their projections on the polyline)."""
    p = np.asarray(line, float).reshape(-1, 2)
    q = np.asarray(pts, float).reshape(-1, 2)
    c = cumlen(p)
    best = np.full(len(q), np.inf)
    st = np.zeros(len(q))
    for k, (a, b) in enumerate(zip(p[:-1], p[1:])):
        ab = b - a
        L2 = float(ab @ ab)
        if L2 <= 0:
            continue
        t = np.clip((q - a) @ ab / L2, 0.0, 1.0)
        dd = np.hypot(q[:, 0] - a[0] - t * ab[0], q[:, 1] - a[1] - t * ab[1])
        m = dd < best
        best[m] = dd[m]
        st[m] = c[k] + t[m] * math.sqrt(L2)
    return st


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
               reach=None, step=None, drop=None, prom=None, dam_point=None, beyond=None,
               barrier_max=0.5, others=None):
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
    `crest` (a causeway across the pool has the dam between it and the flood), in no more than
    `barrier_max` of the sections that reach the flood; others (3.10.87, DWR #195 audit round
    4): f(X, u, t) -> offsets along u from X of another embankment at any level (evidence's
    _tops, not the line itself) - one between the line and the flood is a ridge there too.
    Returns (ok,
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
            if others is not None and others(X, n, tt):
                blocked += 1
                continue
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
        if hits and blocked > barrier_max * hits:
            return False, dict(info, why="a ridge near its crest level{} lies between it and the "
                               "run's flooded area along {} of {} sections (another embankment "
                               "is downstream of it)".format(
                                   ", or another embankment," if others is not None else "",
                                   blocked, hits))
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


def inside_polygon(poly, xy):
    """3.10.87: which points lie inside the polygon (even-odd rule)."""
    q = np.asarray(xy, float).reshape(-1, 2)
    v = np.asarray(poly, float).reshape(-1, 2)
    out = np.zeros(len(q), bool)
    for a, b in zip(v, np.roll(v, -1, axis=0)):
        c = (a[1] > q[:, 1]) != (b[1] > q[:, 1])
        with np.errstate(divide="ignore", invalid="ignore"):
            x = a[0] + (q[:, 1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
        out ^= c & (q[:, 0] < x)
    return out


def _ridges_along(zz, tt, fall, rise, lvl, start_off=True, first=False, hole=None):
    """3.10.87: the other ridges along a profile from a line or a point (zz at offsets tt,
    ascending; no-data samples are skipped): off the line's own top first (the ground falls at
    least `fall` below the highest ground before it; start_off=False: from the first sample),
    then ground that rises at least `rise` above the lowest ground before it to at least `lvl`
    and falls at least `fall` below its highest again behind it - a ridge, not a hillside. No
    data is no fall (the DEM's edge) unless `hole` says it lies inside the DEM (a pool left out
    of it: a crest the DEM shows no face of behind)."""
    out = []
    state = 0 if start_off else 1
    mx, mn, pk, pt = -np.inf, np.inf, -np.inf, None
    hole = np.zeros(len(zz), bool) if hole is None else np.asarray(hole, bool)
    for z, t, h in zip(np.asarray(zz, float), np.asarray(tt, float), hole):
        if not np.isfinite(z):
            if h and state == 0:
                state, mn = 1, np.inf
            elif h and state == 2:
                out.append(float(pt))
                if first:
                    break
                state, mn = 1, np.inf
            continue
        if state == 0:
            mx = max(mx, z)
            if mx - z >= fall:
                state, mn = 1, z
        elif state == 1:
            mn = min(mn, z)
            if z - mn >= rise and z >= lvl:
                state, pk, pt = 2, z, t
        elif z > pk:
            pk, pt = z, t
        elif pk - z >= fall:
            out.append(float(pt))
            if first:
                break
            state, mn = 1, z
    return out


def valley_axis(wet_xy, P):
    """3.10.87 (DWR #195 audit round 4): the run's flow at the valley's scale, from no dam line -
    the direction from the dam point P to the centroid of all the run's flooded cells ('dir'),
    and their long axis (their principal axis, pointed the same way: 'axis') with how clear it is
    ('ratio': the spread along it over the spread across it). {} with fewer than FLOW_MIN_CELLS
    cells or their centroid on P."""
    w = np.asarray(wet_xy if wet_xy is not None else np.zeros((0, 2)), float).reshape(-1, 2)
    if len(w) < FLOW_MIN_CELLS:
        return {}
    c = w.mean(axis=0)
    v = c - np.asarray(P, float)
    nv = float(np.hypot(*v))
    if nv <= 0:
        return {}
    ev, V = np.linalg.eigh(np.cov((w - c).T))
    e = V[:, 1] / (np.hypot(*V[:, 1]) or 1.0)
    if float(e @ v) < 0:
        e = -e
    ratio = float(np.sqrt(ev[1] / ev[0])) if ev[0] > 0 else np.inf
    return {"dir": v / nv, "axis": e, "ratio": ratio, "cells": int(len(w)),
            "centroid": c}


def _tops(zz, tt, hole, big, small, span):
    """3.10.87 (DWR #195 audit round 4): embankment-like tops along a profile, at any level -
    zz at offsets tt (ascending); no data is not ground (the DEM's edge, beyond the read) unless
    `hole` says it is inside the DEM (a pool left out of it: lower than any ground). A top is a
    local high (a level top counts once) whose ground falls at least `big` on one side and at
    least `small` on the other within `span`, each side only as far as the first ground higher
    than the top (its saddle: a lower embankment beside a higher one is still one), so a ridge,
    not a hillside or a terrace. Returns [(t, z)] of each top (its highest sample), in order."""
    z = np.asarray(zz, float)
    t = np.asarray(tt, float)
    n = len(z)
    if n < 3:
        return []
    fin = np.isfinite(z)
    h = np.zeros(n, bool) if hole is None else (np.asarray(hole, bool) & ~fin)
    low = np.where(fin, z, np.where(h, -np.inf, np.nan))      # nan: no ground there
    ds = float(np.median(np.diff(t)))
    m = max(1, int(math.ceil(float(span) / max(ds, 1e-9))))
    # a cheap first pass: the lowest ground within the span on each side, whatever lies
    # between (the saddle rule below can only make these drops smaller)
    lo_b = np.full(n, np.inf)
    lo_a = np.full(n, np.inf)
    for k in range(1, min(m, n - 1) + 1):
        lo_b[k:] = np.fmin(lo_b[k:], low[:-k])
        lo_a[:-k] = np.fmin(lo_a[:-k], low[k:])
    with np.errstate(invalid="ignore"):
        cand = fin & (np.maximum(z - lo_b, z - lo_a) >= big) & \
            (np.minimum(z - lo_b, z - lo_a) >= small)
    zb = np.where(fin, z, -np.inf)
    out, i = [], 0
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and cand[j + 1]:
            j += 1
        k = i + int(np.argmax(zb[i:j + 1]))
        zk = z[k]
        # the drop on each side, up to the first higher ground or the span
        a = k
        while a - 1 >= max(0, k - m) and not zb[a - 1] > zk + 1e-6:
            a -= 1
        b = k
        while b + 1 <= min(n - 1, k + m) and not zb[b + 1] > zk + 1e-6:
            b += 1
        sl, sr = low[a:k], low[k + 1:b + 1]
        dl = zk - float(np.nanmin(sl)) if np.isfinite(sl).any() or np.isinf(sl).any() else 0.0
        dr = zk - float(np.nanmin(sr)) if np.isfinite(sr).any() or np.isinf(sr).any() else 0.0
        if max(dl, dr) >= big and min(dl, dr) >= small:
            out.append((float(t[k]), float(zk)))
        i = j + 1
    return out


def _flat_runs(zz, tt, flat, length):
    """3.10.87 (DWR #195 audit round 4): the stretches of a profile at least `length` long whose
    ground varies by no more than `flat` (water, as LiDAR flattens it): [(t0, t1, z)]."""
    z = np.asarray(zz, float)
    t = np.asarray(tt, float)
    out, i, n = [], 0, len(z)
    while i < n:
        if not np.isfinite(z[i]):
            i += 1
            continue
        lo = hi = z[i]
        j = i
        while j + 1 < n and np.isfinite(z[j + 1]) and \
                max(hi, z[j + 1]) - min(lo, z[j + 1]) <= flat:
            j += 1
            lo, hi = min(lo, z[j]), max(hi, z[j])
        if t[j] - t[i] >= length - 1e-9:
            out.append((float(t[i]), float(t[j]), float(np.mean(z[i:j + 1]))))
        i = j + 1
    return out


def valley_flow(line, wet_xy, P=None, ft=1.0):
    """3.10.87: the run's flow at the valley's scale seen from a dam line: the direction from
    its middle (and from the dam point's place on it, the nearest point to P) to the centroid of
    the run's flooded cells within max(VALLEY_MIN_FT, 1.5 line lengths) of that point, and the
    angles of those directions to the line's downstream normal (its chord's; 0-180 degrees, more
    than 90 when the flood lies upstream). Returns {} with no flooded cells there."""
    p = np.asarray(line, float).reshape(-1, 2)
    w = np.asarray(wet_xy if wet_xy is not None else np.zeros((0, 2)), float).reshape(-1, 2)
    L = float(cumlen(p)[-1])
    if L <= 0 or not len(w):
        return {}
    R = max(VALLEY_MIN_FT * ft, 1.5 * L)
    ch = (p[-1] - p[0]) / (np.hypot(*(p[-1] - p[0])) or 1.0)
    down = left_normal(ch)
    out = {"radius": round(R, 1)}
    for key, X in (("mid", point_at(p, 0.5 * L)),
                   ("dam_point", None if P is None else point_at(p, station_of(p, P)))):
        if X is None:
            continue
        k = np.hypot(*(w - X).T) <= R
        if k.sum() < FLOW_MIN_CELLS:
            return {}
        v = w[k].mean(axis=0) - X
        n_ = float(np.hypot(*v))
        if n_ <= 0:
            return {}
        out[key] = {"dir": v / n_, "cells": int(k.sum()),
                    "angle_deg": float(np.degrees(np.arccos(np.clip(float(v @ down) / n_,
                                                                    -1.0, 1.0))))}
    return out


def evidence(line, sc, sample, P, top_rec, top_used, tol, prom, height, toe, crest_w, step,
             ft=1.0, wet_xy=None, wet_cell=None, far=None, inside=None, far_box=None,
             valley=None, rec_len=None):
    """3.10.87: the positive evidence a dam line traced off the old rule (trace's 'relaxed', or
    from a start found away from the dam point) needs before it is used - the old rule found no
    crest, so the line may be another embankment (a road or a dike beside, below or across the
    dam, a levee along the valley, a causeway across the pool, another dam). line: A -> B (the
    downstream side on its left), sc: the breach station on it, P: the dam point as given,
    sample: the DEM around the dam point; far: the DEM over the wider area the scans below need
    (default `sample`), far_box: its bounds (x0, y0, x1, y1; no data beyond them is outside the
    read, not ground), inside: f(xy) -> True inside the DEM's footprint (no data there is a pool
    left out of it, behind a crest: lower than any ground; no data outside it is the DEM's
    edge), valley: valley_axis (default from wet_xy and P), rec_len: the crest length on
    record. All of:
    (1) the dam point lies within the toe distance plus half the crest width and half a step
        of it (on its crest, its faces or the pool just above it);
    (2) the ground climbs from the dam point to it (no other ridge in between);
    (3) the run's flooded cells (wet_xy) lie on its downstream side (flow_check, with no ridge
        between it and the flood in more than PARALLEL_MAX_FRAC of its sections - near its
        crest level, or another embankment at any level, _tops);
    (4) the run's flow at the valley's scale crosses it: from no dam line, the direction from
        the dam point to the centroid of all the run's flooded cells (valley_axis) is no more
        than VALLEY_SQUARE_MAX_DEG off square to its chord; near it, the directions from its
        middle and from the dam point's place on it to the flood (valley_flow) and the flow
        path from its downstream toe at the breach (flow_path: max(VALLEY_MIN_FT, 1.5 line
        lengths) long - one cut short by the DEM's edge is no evidence) each less than
        FLOW_PATH_MAX_DEG off its downstream normal, and the flood's direction from its middle
        within FLOW_AGREE_DEG of the flow path's;
    (5) the pool is on its upstream side: water (a level surface) or a pool left out of the DEM
        within twice the toe distance plus the crest width, along all but PARALLEL_MAX_FRAC of
        its sections - where the DEM shows none (a dry pool, or the floodplain behind a levee
        along the valley), the run's flooded cells have a clear long axis (VALLEY_AXIS_RATIO)
        no more than VALLEY_SQUARE_MAX_DEG off square to it;
    (6) it is the only embankment there: no other ridge across the section through the dam
        point (along the line's normal there) within three toe distances plus the crest width;
        no other ridge or level top beside it within twice the toe distance plus the crest
        width, on either side, in more than PARALLEL_MAX_FRAC of its sections; neither end
        runs into another ridge (a levee or a road ending at the dam's face); it impounds (no
        crest-high ground behind it beyond the toe distance in more than that fraction); and
        no other embankment between it and the pool, looked for along its upstream normal and
        up the valley (against valley_axis's direction, unless the pool lies just upstream of
        it) all the way back - max(UPSTREAM_SCAN_FT, 1.5 line lengths, past the upstream end of
        the run's flooded area), and as far again where the DEM goes on - or to the pool, in
        more than that fraction; where the DEM ends short of both in more than that fraction,
        what lies between it and the pool is not known (no evidence). Another ridge here is a ridge at least the line's own ground less the
        tolerance and the datum allowance max(DATUM_MIN_FT, DATUM_FRAC x H) high, or another
        embankment at any level (_tops: standing half the reference height above the ground
        on one side, the reference the lower of H and the line's own height above its toe) -
        a dam that reads far below the line (another datum, a coarse DEM) is still one;
    (7) it is not a part of the embankment: at least FRAGMENT_FRAC of the crest length on
        record, and its crest does not go on at its level past its ends for longer than the
        line itself.
    Returns (ok, info): dam_point_to_line, flow_angle_deg (the line's angle at the breach to
    the run's flow at the valley's scale, valley_axis's direction: off square to the run's
    flow), flow_angle_local_deg (the same to the flood's direction from its middle,
    valley_flow), flow_path_angle_deg, flow_path_length, valley, valley_scale, pool_frac,
    parallel_ridge_frac, upstream_ridge_frac, upstream_unreached_frac, upstream_high_frac,
    flow (flow_check's info), why."""
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
    far_ = far or sample
    W = 2.0 * toe + crest_w
    ds = 0.5 * step
    own_r = 0.5 * crest_w + 2.0 * step
    span = EMB_SPAN_TOES * toe + step

    def prof(X0, u, t, smp=None):
        # the ground from X0 along u at offsets t: (z, hole: no data inside the DEM, end: the
        # index of the first sample beyond the read or the DEM's edge - none past it is ground)
        q = np.asarray(X0, float)[None, :] + t[:, None] * np.asarray(u, float)[None, :]
        z = (smp or far_)(q)
        nan = ~np.isfinite(z)
        hole = np.zeros(len(z), bool)
        out_ = nan.copy()
        if nan.any():
            if far_box is not None:
                inb = (q[:, 0] >= far_box[0]) & (q[:, 0] <= far_box[2]) & \
                    (q[:, 1] >= far_box[1]) & (q[:, 1] <= far_box[3])
            else:
                inb = np.ones(len(z), bool)
            if inside is not None:
                ins = np.zeros(len(z), bool)
                ins[nan & inb] = inside(q[nan & inb])
                hole = nan & inb & ins
            out_ = nan & ~hole
        k = np.nonzero(out_)[0]
        return z, hole, (int(k[0]) if len(k) else len(z))

    # the reference height of another embankment: the lower of the dam height on record and
    # the line's own height above its downstream toe (the median over its sections)
    secs = np.linspace(0.1 * L, 0.9 * L, 9)
    hs = []
    for s in secs:
        Xs, n_ = point_at(p, s), left_normal(tangent_at(p, s))
        zc = float(far_(Xs[None, :])[0])
        zt = far_(Xs[None, :] + np.linspace(0.5 * toe, 1.5 * toe, 6)[:, None] * n_[None, :])
        if np.isfinite(zc) and np.isfinite(zt).any():
            hs.append(zc - float(np.nanmin(zt)))
    h_own = float(np.median(hs)) if hs else float(height)
    href = max(prom, min(float(height), h_own))
    big, small = max(prom, EMB_RISE_FRAC * href), 0.5 * prom
    info["embankment_test"] = {"reference_height": round(href, 2), "rise": round(big, 2),
                               "other_side": round(small, 2), "span": round(span, 1)}

    def others(X0, u, t, before=np.inf):
        # another embankment's tops along the profile (_tops) short of `before`, not on the
        # line itself
        z, hole, _ = prof(X0, u, t)
        out = []
        for t0, _ in _tops(z, t, hole, big, small, span):
            if t0 >= before:
                break
            Q = np.asarray(X0, float) + t0 * np.asarray(u, float)
            if float(_seg_nearest(p, Q.reshape(1, 2))[0][0]) > own_r:
                out.append(t0)
        return out

    ok, fi = flow_check(p, wet_xy, wet_cell, sample=far_, crest=top_used, tol=tol, toe=toe,
                        step=0.25 * step, drop=max(tol, 0.5 * height) + tol, prom=0.5 * prom,
                        barrier_max=PARALLEL_MAX_FRAC, others=others)
    info["flow"] = fi
    if not ok:
        return False, dict(info, why=fi["why"])
    B = point_at(p, sc)
    nb = left_normal(tangent_at(p, sc))
    chord = (p[-1] - p[0]) / (np.hypot(*(p[-1] - p[0])) or 1.0)
    dn = left_normal(chord)
    # (4) the run's flow at the valley's scale: from no dam line ...
    va = valley if valley is not None else valley_axis(wet_xy, P)
    if not va:
        return False, dict(info, why="too few of the run's flooded cells to tell the valley's "
                           "flow")
    a_ch = float(np.degrees(np.arccos(np.clip(float(dn @ va["dir"]), -1.0, 1.0))))
    a_ax = float(np.degrees(np.arccos(np.clip(abs(float(dn @ va["axis"])), 0.0, 1.0))))
    info["valley_scale"] = {"bearing_deg": round(bearing_deg(va["dir"]), 1),
                            "off_square_deg": round(a_ch, 1),
                            "axis_bearing_deg": round(bearing_deg(va["axis"]), 1),
                            "axis_off_square_deg": round(a_ax, 1),
                            "axis_ratio": round(min(va["ratio"], 1e3), 2), "cells": va["cells"]}
    info["flow_angle_deg"] = round(angle_deg(nb, va["dir"]), 1)
    if a_ch > VALLEY_SQUARE_MAX_DEG:
        return False, dict(info, why="the run's flooded area lies {:.0f} degrees off square to "
                           "it at the valley's scale (the direction from the dam point to the "
                           "centroid of all the run's flooded cells): it runs along the "
                           "valley".format(a_ch))
    # ... and near it
    vf = valley_flow(p, wet_xy, P, ft)
    if not vf:
        return False, dict(info, why="too few of the run's flooded cells near it to tell the "
                           "valley's flow")
    vm, vp = vf["mid"], vf["dam_point"]
    info["valley"] = {"radius": vf["radius"], "angle_from_middle_deg": round(vm["angle_deg"], 1),
                      "angle_from_dam_point_deg": round(vp["angle_deg"], 1)}
    info["flow_angle_local_deg"] = round(angle_deg(nb, vm["dir"]), 1)
    for k_, v_ in (("its middle", vm), ("the dam point's place on it", vp)):
        if v_["angle_deg"] >= FLOW_PATH_MAX_DEG:
            return False, dict(info, why="the run's flooded area lies {:.0f} degrees off its "
                               "downstream side seen from {} (the valley's flow runs along "
                               "it)".format(v_["angle_deg"], k_))
    D = max(VALLEY_MIN_FT * ft, 1.5 * L)
    r = max(2.0 * step, D / 30.0)
    path = flow_path(far_, B + toe * nb, nb, D, r)
    walked = float(cumlen(path)[-1])
    info["flow_path_length"] = round(walked, 1)
    if walked < D - 1e-6 * max(D, 1.0):
        return False, dict(info, why="the flow path below its breach runs off the DEM after "
                           "{:,.0f} (it needs {:,.0f} to tell the valley's flow)".format(
                               walked, D))
    v = path[-1] - B
    nv = float(np.hypot(*v)) or 1.0
    ang = float(np.degrees(np.arccos(np.clip(float(v @ nb) / nv, -1.0, 1.0))))
    agree = float(np.degrees(np.arccos(np.clip(float(v @ vm["dir"]) / nv, -1.0, 1.0))))
    info.update(flow_path_angle_deg=round(ang, 1), flow_path_end=[round(float(q), 2)
                                                                  for q in path[-1]],
                flow_path_vs_flood_deg=round(agree, 1))
    if ang >= FLOW_PATH_MAX_DEG:
        return False, dict(info, why="the flow path below its breach leaves it {:.0f} degrees "
                           "off square (it runs along the flow)".format(ang))
    if agree > FLOW_AGREE_DEG:
        return False, dict(info, why="the flow path below its breach and the run's flooded "
                           "area disagree by {:.0f} degrees on the valley's flow (which way "
                           "the water goes is not clear)".format(agree))
    # (5) the pool on its upstream side
    zl = sample(point_at(p, np.linspace(0.0, L, max(5, int(L / ds) + 1))))
    if not np.isfinite(zl).any():
        return False, dict(info, why="no DEM along it")
    flat, flen = POOL_FLAT_FT * ft, max(POOL_FLAT_LEN_FT * ft, 2.0 * step)
    tp = np.arange(0.0, W + flen + 1e-9, 0.5 * ds)
    pooled = n_p = 0
    for s in secs:
        Xs, n_ = point_at(p, s), left_normal(tangent_at(p, s))
        zc = float(far_(Xs[None, :])[0])
        z_, h_, end = prof(Xs, -n_, tp)
        if not np.isfinite(zc) or end < 2:
            continue
        n_p += 1
        wet_ = any(t0 <= W and zc - zm >= prom for t0, _, zm in
                   _flat_runs(z_[:end], tp[:end], flat, flen))
        pooled += bool(wet_ or h_[:end][tp[:end] <= W].any())
    pf = pooled / n_p if n_p else 0.0
    info["pool_frac"] = round(pf, 3)
    if pf < 1.0 - PARALLEL_MAX_FRAC and (va["ratio"] < VALLEY_AXIS_RATIO or
                                         a_ax > VALLEY_SQUARE_MAX_DEG):
        return False, dict(info, why="the DEM shows the pool (water, or no data) on its "
                           "upstream side along only {:.0%} of it, and the run's flooded area "
                           "has no clear long axis square to it ({:.0f} degrees off square, "
                           "{:.1f} times as long as wide): a levee or a road along the valley "
                           "has no pool behind it".format(pf, a_ax, va["ratio"]))
    # (6) the only embankment there
    allow = max(DATUM_MIN_FT * ft, DATUM_FRAC * height)
    lvl_lo = float(np.nanmedian(zl)) - tol - allow
    lvl = min(top_rec, top_used) - tol
    info["other_ridge_level"] = round(lvl_lo, 2)
    # ... across the section through the dam point
    R4 = 3.0 * toe + crest_w
    t4 = np.arange(0.0, R4 + 1e-9, ds)
    u = left_normal(tangent_at(p, sP))
    for sg in (1.0, -1.0):
        zz, hz, end = prof(P, sg * u, t4)
        for t_ in _ridges_along(zz[:end], t4[:end], 0.5 * prom, 0.5 * prom, lvl_lo,
                                start_off=False, hole=hz[:end]):
            Q = P + sg * t_ * u
            dq = float(_seg_nearest(p, Q.reshape(1, 2))[0][0])
            if dq > own_r:
                return False, dict(info, why="another ridge at least {:.1f} high lies {:,.0f} "
                                   "from the dam point, {:,.0f} from the line (a road or a dike "
                                   "beside the dam, or the dam beside a road: which one is the "
                                   "dam is not clear)".format(lvl_lo, t_, dq))
    t4b = np.arange(-R4, R4 + 1e-9, ds)
    for t_ in others(P, u, t4b):
        return False, dict(info, why="another embankment lies {:,.0f} from the dam point across "
                           "the section through it (a road or a dike beside the dam, or the "
                           "dam beside a road: which one is the dam is not clear)".format(
                               abs(t_)))
    # ... at its ends
    te = np.arange(0.0, W + 1e-9, ds)
    for E, t_e in ((p[0], p[0] - p[1]), (p[-1], p[-1] - p[-2])):
        t_e = np.asarray(t_e, float) / (np.hypot(*t_e) or 1.0)
        zz, hz, end = prof(E, t_e, te)
        hit = _ridges_along(zz[:end], te[:end], 0.5 * prom, 0.5 * prom, lvl_lo, start_off=False,
                            first=True, hole=hz[:end]) or others(E, t_e, te)
        if hit:
            return False, dict(info, why="an end of it runs into another ridge {:,.0f} beyond "
                               "it (a levee or a road ending at another embankment)".format(
                                   hit[0]))
    # ... beside it, behind it, upstream of it
    up_end = 0.0
    w_ = np.asarray(wet_xy if wet_xy is not None else np.zeros((0, 2)), float).reshape(-1, 2)
    if len(w_):
        mid = point_at(p, 0.5 * L)
        rel = w_ - mid
        lat = np.abs(rel @ chord)
        k = lat <= 0.5 * L + W
        if k.any():
            up_end = max(0.0, float(np.max(-(rel[k] @ dn)))) + W
    U = max(UPSTREAM_SCAN_FT * ft, 1.5 * L, up_end)
    # ... looked for as far again where the DEM goes on (the scan must reach U, or the pool)
    tu = np.arange(0.0, 2.0 * U + span + 1e-9, ds)
    nU = int(np.searchsorted(tu, U - 1e-9)) + 1
    lpool = max(flen, 0.25 * U)
    tt = np.arange(-(W + toe), W + toe + 1e-9, ds)
    own_w = 0.5 * crest_w + step
    w_lt = max(0.5 * crest_w, 2.0 * step)
    sides = (np.nonzero(tt >= 0)[0], np.nonzero(tt <= 0)[0][::-1])    # from the line out
    par = high = upr = unr = n = 0
    for s in secs:
        Xs = point_at(p, s)
        n_ = left_normal(tangent_at(p, s))
        zz, hz, _ = prof(Xs, n_, tt)
        if not np.isfinite(zz[np.abs(tt) <= own_w]).any():
            continue
        n += 1
        up = (tt <= -toe) & (tt >= -W)
        high += bool(np.any(np.isfinite(zz[up]) & (zz[up] >= lvl)))
        blocked, reached, t_pool = False, True, np.inf
        zc = float(far_(Xs[None, :])[0])
        for k_, u_ in enumerate((-n_, -va["dir"])):
            if k_ == 1 and (float(va["dir"] @ n_) <= 0 or angle_deg(va["dir"], n_) <= 10.0 or
                            t_pool <= W):
                # up the valley as well, unless that is about the normal or the pool lies just
                # upstream of the line (nothing can be between them)
                break
            zu, hu, end = prof(Xs, u_, tu)
            # the pool: water (a level stretch the crest prominence below the crest - along
            # the normal at least a quarter of the scan long, not a pond between this line and
            # another embankment; up the valley, across the pool's arm, as long as the pool
            # test above) or no data inside the DEM as long
            lp = lpool if k_ == 0 else flen
            t_pool = [t0 for t0, _, zm in _flat_runs(zu[:end], tu[:end], flat, lp)
                      if zc - zm >= prom]
            hk = np.nonzero(hu[:end])[0]
            if len(hk):
                runs = np.split(hk, np.nonzero(np.diff(hk) > 1)[0] + 1)
                t_pool += [float(tu[r_[0]]) for r_ in runs
                           if tu[r_[-1]] - tu[r_[0]] >= (flen if r_[-1] == end - 1 else lp)
                           - 1e-9]
            t_pool = min(t_pool) if t_pool else np.inf
            if k_ == 0:
                rg = _ridges_along(zu[:end], tu[:end], 0.5 * prom, 0.5 * prom, lvl_lo,
                                   first=True, hole=hu[:end])
                blocked = blocked or bool(rg and rg[0] < t_pool)
            # ... another embankment between it and the pool, and the scan reaches the pool or
            # all the way back
            if others(Xs, u_, tu, t_pool):
                blocked = True
            if end < nU and not np.isfinite(t_pool):
                reached = False
        upr += blocked
        unr += not reached
        found = False
        for idx in sides:
            zs, ts, hs_ = zz[idx], np.abs(tt[idx]), hz[idx]
            # a ridge as high as the line's ground less the allowance (another embankment
            # that reads low), or another embankment at any level, within W
            found = found or any(t_ <= W for t_ in _ridges_along(
                zs, ts, 0.5 * prom, 0.5 * prom, lvl_lo, first=True, hole=hs_))
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
        found = found or any(abs(t_) <= W for t_ in others(Xs, n_, tt))
        par += found
    info.update(sections=n, parallel_ridge_frac=round(par / n, 3) if n else None,
                upstream_ridge_frac=round(upr / n, 3) if n else None,
                upstream_unreached_frac=round(unr / n, 3) if n else None,
                upstream_high_frac=round(high / n, 3) if n else None, upstream_scan=round(U, 1))
    if not n:
        return False, dict(info, why="no DEM beside it")
    if par > PARALLEL_MAX_FRAC * n:
        return False, dict(info, why="another ridge runs beside it within {:,.0f} along {} of "
                           "{} sections (a road or a dike beside the dam: which one is the dam "
                           "is not clear)".format(W, par, n))
    if upr > PARALLEL_MAX_FRAC * n:
        return False, dict(info, why="another embankment lies between it and the pool (within "
                           "{:,.0f} upstream) along {} of {} sections (it is below another "
                           "embankment)".format(U, upr, n))
    if unr > PARALLEL_MAX_FRAC * n:
        return False, dict(info, why="the DEM ends less than {:,.0f} upstream of it, short of "
                           "the pool, along {} of {} sections (what lies between it and the "
                           "pool is not known)".format(U, unr, n))
    if high > PARALLEL_MAX_FRAC * n:
        return False, dict(info, why="crest-high ground lies behind it (on its upstream side, "
                           "beyond the toe distance) along {} of {} sections: a dam has its "
                           "pool there".format(high, n))
    # (7) not a part of the embankment
    if rec_len and L < FRAGMENT_FRAC * float(rec_len):
        return False, dict(info, why="it is {:,.0f} long, less than {:.0%} of the crest length "
                           "on record ({:,.0f}): a part of the embankment".format(
                               L, FRAGMENT_FRAC, float(rec_len)))
    # ... the crest past each end, straight on: ground within the tolerance of the line's own
    # level that stands above the ground on both sides (more embankment crest; not the
    # abutment's hillside at that level)
    zcl = float(np.nanmedian(zl))
    tg = np.arange(0.0, L + 1e-9, ds)
    more = []
    for E, t_e in ((p[0], p[0] - p[1]), (p[-1], p[-1] - p[-2])):
        t_e = np.asarray(t_e, float) / (np.hypot(*t_e) or 1.0)
        zz, _, end = prof(E, t_e, tg)
        nu_e = left_normal(t_e)
        g = 0.0
        for t_, z_ in zip(tg[1:end], zz[1:end]):
            if not np.isfinite(z_) or _judge(far_, E + t_ * t_e, nu_e, z_, zcl, tol, toe, prom,
                                             True):
                break
            g = float(t_)
        more.append(g)
    info["crest_past_ends"] = [round(m_, 1) for m_ in more]
    if sum(more) > L:
        return False, dict(info, why="its crest goes on at its level {:,.0f} and {:,.0f} past "
                           "its ends, more than its own length ({:,.0f}): a part of the "
                           "embankment".format(more[0], more[1], L))
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
    wider than its window while that top is about as wide as the crest on record, stops at a
    junction (_walk: another embankment joining or crossing the crest), and takes a side of
    the ridge test that runs into no data (a pool left out of the DEM) as falling; its ends
    say why it stopped ('a junction: ...'). start_within: the crest the trace starts from must
    be at most this far from P (a start from crest-high ground found nearby: not a higher
    ridge across the section)."""
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
                step=None, apart=None, gain=FAN_GAIN):
    """(station, how, detail) of the breach centre along the crest line: 'toe' (default via
    'auto') the deepest dam section, i.e. the lowest ground at the downstream toe (toe..toe +
    band to the left of the line), averaged over the breach bottom; 'lowest_crest' the lowest
    crest (averaged the same way). Only stations at least half the breach's top width from the
    ends; the embankment part of the line (crest within tol of the top) first. 3.10.87: apart
    given (the deepest section of a line off the old rule): the toe is also looked for along
    normals turned up to FAN_DEG (a bend) - where they reach ground lower than the plain ones
    do by more than `gain` (model units), the breach goes where that place projects onto the
    line (detail turned_normal) - and detail also has the lowest toe ground (toe_low) and the
    lowest farther than `apart` from it both along the line and on the ground
    (toe_ground_apart, that far along it as toe_ground_apart_distance; None when there is
    none)."""
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
            sk = float(s[k])
            detail = {"toe_ground": round(float(za[k]), 2),
                      "toe_ground_range": [round(float(np.nanmin(za[pool])), 2),
                                           round(float(np.nanmax(za[pool])), 2)]}
            if apart is not None:
                # 3.10.87 (a line off the old rule): the toe also along normals turned up to
                # FAN_DEG either way - where the line bends its normals fan out and can miss
                # the old channel - and where the lowest of those samples is lower than any
                # the plain normals reach (by more than `gain`), the breach goes where that
                # place projects onto the line; and the lowest sample farther than `apart`
                # from it both along the line and on the ground (another place about as deep:
                # which one is the deepest section is not clear; one farther down the same
                # channel is the same place, and so is one beside it that a bend projects
                # elsewhere)
                q, zq = [], []
                for a_ in np.radians(np.arange(-FAN_DEG, FAN_DEG + 1e-9, FAN_STEP_DEG)):
                    ca, sa = math.cos(a_), math.sin(a_)
                    nr = np.column_stack([n[pool, 0] * ca - n[pool, 1] * sa,
                                          n[pool, 0] * sa + n[pool, 1] * ca])
                    for t in tt:
                        q.append(xy[pool] + nr * t)
                        zq.append(sample(q[-1]))
                q, zq = np.vstack(q), np.concatenate(zq)
                ok_ = np.isfinite(zq)
                q, zq = q[ok_], zq[ok_]
                if len(zq):
                    sq = stations_of(p, q)
                    g = int(np.argmin(zq))
                    if zq[g] < float(np.nanmin(zt[pool])) - float(gain):
                        ip = np.nonzero(pool)[0]
                        sk = float(s[ip[int(np.argmin(np.abs(s[ip] - sq[g])))]])
                        detail.update(toe_ground=round(float(zq[g]), 2), turned_normal=True)
                    far_ = (np.abs(sq - sq[g]) > float(apart)) & \
                        (np.hypot(*(q - q[g]).T) > float(apart))
                    j = int(np.nonzero(far_)[0][np.argmin(zq[far_])]) if far_.any() else None
                    detail.update(
                        toe_low=round(float(zq[g]), 2),
                        toe_ground_apart=None if j is None else round(float(zq[j]), 2),
                        toe_ground_apart_distance=None if j is None else
                        round(float(abs(sq[j] - sq[g])), 1))
            return sk, ("the deepest dam section along the crest (the lowest ground at "
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
