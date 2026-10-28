"""Inventory bridges checked against the road (or rail) they carry and, on positive evidence
only, moved to where that line actually crosses the channel (DWR-3034 review, Runner 3.10.90).

An NBI point is the bridge's recorded location, often tens to hundreds of metres from the
opening, and the line built from it (Hazard plugin 'runnerculverts': the nearest mapped stream x
road crossing) inherits any offset of the stream layer. In DWR-3034 bridge 0025008224 (Highway
25 over Square Butte Creek) was cut about 190 m south of the real opening: the cut drained a
solid embankment while the opening stayed blocked.

Main (3.10.89) cut every bridge where the inventory put it. That stays the default: a bridge
moves only when the evidence says its place is solid embankment AND a better crossing is
plain. Before the cuts are made, every inventory BRIDGE is checked within the radius of its
line's midpoint:

1. The line it carries: the road or rail centreline its line crosses (a line that crosses none
   matches one only when its midpoint is on that line's crown, within ON_ROAD_M). A rail bridge
   (facility_kind: NBI item 42A = 2, NDDOT "service on" 2, runner_culverts placed on / meant for
   rail, rail in the facility carried) is matched to rail lines only, a road bridge to roads
   only; with no rail lines a rail bridge is not checked. Road / rail pieces whose end points
   touch (within MERGE_M) are merged into one line first (a layer split at the bridge or at an
   intersection).
2. The bare-earth DEM along that line every 1 m, and beside it (SIDE_IN_M to SIDE_OUT_M each
   side).
3. The cut STAYS at the inventory location when, at the cut (the stretch the cut covers):
   - the DEM opens there (a bridge's deck is not in bare-earth LiDAR), or a channel line
     (CHANNEL_LINES) crosses the road there: kept, verified;
   - the ground beside the road dips at least CHANNEL_M there (a channel under a deck that is
     in the LiDAR, or a hydro-flattened water surface): kept, verified, whatever the nearest
     crossing is. A deck in the LiDAR hides the opening's depth, so a deeper opening elsewhere
     is no evidence against the cut; nor is a channel line crossing there and none at the cut:
     CHANNEL_LINES is often only the main stem, clipped to the run radius, and how much of the
     cut's surroundings it covers cannot be judged from the lines, so a missing line at the
     cut never moves it (PR #198 review of 9c646d6, probe R4). Low ground that runs ALONG the
     road (on either side, within CHANNEL_M of its lowest at the cut for DITCH_M past the cut,
     either way: a roadside ditch, a borrow area or a pond at the embankment toe) is not a
     crossing and keeps nothing (DWR-3034 0025008224, PR #198 follow-up); such a cut moves
     when the nearest crossing (4.) is CLEARLY BETTER - a DEM opening at least CLEAR_DEPTH_M
     and CLEAR_FACTOR x the beside-road dip deep, at least CLEAR_WIDTH_FRAC of the span wide
     (15 m cap), its bed down at the ground beside the cut (within CHANNEL_M);
   - the road dips at least DIP_M below the road on both sides, within the shoulder window
     (OPENING_SHOULDER_M plus half the span: wide openings) or along the grade line
     (GRADE_LINE_M each way): the road's low point, a sag, or an opening the opening rule cannot
     size: kept;
   - the DEM has no data at the cut or beside it (water, a gap), or the road line ends within
     the shoulder window (the profile cannot be judged): kept.
4. Otherwise the cut is on solid embankment, and the only target is the NEAREST crossing along
   the road within the radius: a DEM opening at least OPENING_MIN_M deep, or a channel line
   crossing. With channel lines in the run an opening counts only with a channel line within
   STREAM_NEAR_M of it (an opening without one is a grade separation). A DEM opening without a
   channel line is a target only when its bare-earth bed is at least BED_DROP_M below the ground
   beside the road at the cut (the valley bottom is there, not at the cut), or it is clearly
   better than a cut with low ground along the road beside it (3.). The bridge does not
   move when that nearest crossing is not a target, or another structure's line already crosses
   the road there (within TAKEN_M): it never moves past it.
5. Crossings are handed out once, nearest bridge first, independent of the inventory's order:
   a bridge whose nearest crossing goes to a nearer bridge stays.

A moved cut is centred on the crossing, oriented along the channel line there (else across the
road), keeps its length across the road, and its inverts are cleared (the inventory's NBI /
NDDOT bridges carry none; the cut then goes to the lowest ground near its new ends, as for any
bridge without inverts). Every bridge is a row in run_report.json bridge_snap and in
run_report.md; every move is a run warning (name, from, to, distance, why), and the bridges that
stay without an opening or a channel seen at the cut are named in one warning.

NDBREACH_BRIDGE_SNAP_M sets the radius in metres, read like the other NDBREACH_* numbers
(workflow._env_number): 0, off, no or false turn the check off (main's behaviour)."""
import math
import os
import re

import numpy as np

from . import conditioning as cond
from . import road_profile as rp

SNAP_RADIUS_M = 300.0
ENV = "NDBREACH_BRIDGE_SNAP_M"
KEEP_M = 15.0                # an opening this close to the cut (plus half its width) is the cut's
STREAM_NEAR_M = 50.0         # a channel line this close to an opening goes with it
TAKEN_M = 30.0               # a crossing another structure's line crosses within this is taken
ON_ROAD_M = 30.0             # a bridge line crossing no centreline: its midpoint this close ...
CROWN_TOL_M = 0.5            # ... and on that line's crown (DEM within this of the centreline's)
MERGE_M = 1.0                # road / rail pieces whose end points are this close are one line
MERGE_TURN_DEG = 60.0        # ... continuing with a turn of at most this
DIP_M = 0.3                  # the road this far below the road on both sides: its low point
GRADE_LINE_M = 150.0         # ... along the grade line this far each way (wide openings)
LOCAL_M = 60.0               # the ground beside the road compared with this far along the road
SIDE_IN_M = 10.0             # the ground beside the road: from this far off the centreline ...
SIDE_OUT_M = 50.0            # ... to this far, both sides
SIDE_STEP_M = 2.0
CHANNEL_M = 0.5              # the ground beside the road this much lower at the cut: a channel
BED_DROP_M = 1.0             # a target opening's bed this far below the ground beside the cut
# The 'ground beside the road is lower at the cut' keep is weak evidence (a ditch, a borrow pit
# or a pond along the embankment toe reads the same - DWR-3034's 0025008224, #198 follow-up):
DITCH_M = 40.0               # low side ground continuing this far along the road past the cut
#                              (on either side of the road, either way): parallel, not a crossing
CLEAR_DEPTH_M = 3.0          # a clearly better crossing: a DEM opening at least this deep, ...
CLEAR_FACTOR = 2.0           # ... at least this many times the beside-road signal, ...
CLEAR_WIDTH_FRAC = 0.5       # ... at least this fraction of the bridge's span wide (15 m cap) ...
#                              ... and its bed down at the ground beside the cut (within CHANNEL_M)
STEP_M = 1.0
METHODS_MOVED = ("dem_opening", "channel_crossing")
RAIL_WORDS = re.compile(r"\b(rail|rails|railway|railroad|railroads|rr|ry|bnsf|soo|cpkc|"
                        r"amtrak|dmvw|rrvw|rrv)\b", re.I)
# a street word AFTER the rail word names a street ('RAILROAD AVE', 'Soo Street', 'SOO LINE RD')
STREET_WORDS = re.compile(r"\b(ave|av|avenue|st|str|street|rd|road|dr|drive|blvd|boulevard|ln|"
                          r"lane|way|pl|place|ct|court|pkwy|parkway|hwy|highway|cir|circle|ter|"
                          r"terrace|trl|trail)\b\.?", re.I)
# a road named in another part of a compound name ('CO RD 12 / BNSF RY')
ROAD_MARKS = re.compile(r"\b(co\s*rd|cr|hwy|highway|us|nd|sh|i)\s*-?\s*\d+|\b(interstate|county "
                        r"road|township road|twp)\b", re.I)
NAME_PARTS = re.compile(r"\s*(?:/|\\|;|&|\+|\band\b|\bover\b|\bunder\b|\bat\b|,)\s*", re.I)


def radius_from_env(default=SNAP_RADIUS_M, with_note=False):
    """NDBREACH_BRIDGE_SNAP_M (or BRIDGE_SNAP_M) in metres, parsed as workflow._env_number:
    unset or empty -> default; off / no / false -> 0 (off); a number -> that (a negative one ->
    0); anything else -> default. with_note: (radius, raw value, note) - the note says when the
    value was not a number."""
    raw = os.environ.get(ENV) or os.environ.get("BRIDGE_SNAP_M")
    note = ""
    if raw is None or raw.strip() == "":
        r = default
    elif raw.strip().lower() in ("off", "no", "false"):
        r = 0.0
    else:
        try:
            r = float(raw)
        except ValueError:
            r = None
        if r is None or not math.isfinite(r):
            note = "{} = '{}' is not a number: the default {:g} m is used".format(
                ENV, raw, default)
            r = default
        r = max(0.0, r)
    return (r, raw, note) if with_note else r


def _txt(v):
    if v is None:
        return ""
    s = str(v).strip().strip("'").strip()
    return "" if s.upper() == "NULL" else s


def facility_kind(attrs):
    """'rail', 'road' or '' (unknown) for an inventory structure's attributes: NBI item 42A
    (service_on_042a: 2 railroad; 1, 5-8 highway), NDDOT's servtypondesc (the same codes),
    Hazard's runner_culverts (notes 'no rail / road line crosses a stream here' = the record was
    meant for that kind; else the embankment it was placed on), or rail words in the facility
    carried (carried, facility_carried_007, facility; name_kind: not a street named after the
    railway, not a compound road / rail name)."""
    low = {str(k).lower(): v for k, v in (attrs or {}).items()}
    for key in ("service_on_042a", "servtypondesc"):
        code = _txt(low.get(key)).split(" ")[0].lstrip("0")[:1]
        if code == "2":
            return "rail"
        if code in ("1", "5", "6", "7", "8"):
            return "road"
    notes = _txt(low.get("notes")).lower()
    m = re.search(r"no (rail|road) line crosses a stream here", notes)
    if m:
        return m.group(1)
    for key in ("carried", "facility_carried_007", "facility"):
        if name_kind(_txt(low.get(key))) == "rail":
            return "rail"
    emb = _txt(low.get("embankment")).lower()
    return emb if emb in ("road", "rail") else ""


def name_kind(name):
    """'rail' when a facility name says rail, else '' (the name does not decide).

    The name is split into parts at '/', '\\', ';', '&', '+', ',', 'and', 'over', 'under' and
    'at'. A part says rail when it has a rail word (RAIL_WORDS) with no street word
    (STREET_WORDS: AVE, ST, STREET, RD, DR, BLVD, LN, WAY ...) after it: 'BNSF RAILWAY' and
    'SOO LINE RAILROAD' are rail, 'RAILROAD AVE', 'Soo Street', 'N RAILROAD ST' are streets.
    A compound name with a rail part AND a road part (a street word or a road number:
    'CO RD 12 / BNSF RY', 'ND 25 OVER BNSF') is ambiguous - the record may be the road over the
    railway, or the railway beside the road - so the name does not decide: the bridge falls to
    the embankment Hazard placed it on, else unknown (matched to either kind of line; PR #198
    review, finding 2)."""
    rail = road = False
    for part in NAME_PARTS.split(name or ""):
        if not part.strip():
            continue
        hits = list(RAIL_WORDS.finditer(part))
        streets = list(STREET_WORDS.finditer(part))
        if hits and not any(st.start() >= hits[-1].end() for st in streets):
            rail = True
        elif streets or ROAD_MARKS.search(part):
            road = True
    return "rail" if rail and not road else ""


FACILITY_FIELDS = ("service_on_042a", "servtypondesc", "notes", "carried",
                   "facility_carried_007", "facility", "embankment")


def _seg_intersections(p, q):
    """Points where polylines p and q cross: [(x, y, i_p, t_p, j_q)] (segment index and
    fraction along p, segment index on q)."""
    out = []
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)
    if len(p) < 2 or len(q) < 2:
        return out
    # bounding boxes first (long road lines)
    if (p[:, 0].max() < q[:, 0].min() or q[:, 0].max() < p[:, 0].min() or
            p[:, 1].max() < q[:, 1].min() or q[:, 1].max() < p[:, 1].min()):
        return out
    a, r = p[:-1], np.diff(p, axis=0)
    cq, sq = q[:-1], np.diff(q, axis=0)
    # only the segments of q near p's box, in chunks (long stream lines)
    lo, hi = p.min(axis=0), p.max(axis=0)
    keep = ~((np.maximum(q[:-1, 0], q[1:, 0]) < lo[0]) | (np.minimum(q[:-1, 0], q[1:, 0]) > hi[0])
             | (np.maximum(q[:-1, 1], q[1:, 1]) < lo[1]) |
             (np.minimum(q[:-1, 1], q[1:, 1]) > hi[1]))
    js = np.flatnonzero(keep)
    for j0 in range(0, len(js), 256):
        jj = js[j0:j0 + 256]
        c, s = cq[jj][None, :, :], sq[jj][None, :, :]
        aa, rr = a[:, None, :], r[:, None, :]
        den = rr[..., 0] * s[..., 1] - rr[..., 1] * s[..., 0]
        ok = np.abs(den) >= 1e-12
        den = np.where(ok, den, 1.0)
        dx = c - aa
        t = (dx[..., 0] * s[..., 1] - dx[..., 1] * s[..., 0]) / den
        u = (dx[..., 0] * rr[..., 1] - dx[..., 1] * rr[..., 0]) / den
        hit = ok & (t >= 0.0) & (t <= 1.0) & (u >= 0.0) & (u <= 1.0)
        for i, jl in zip(*np.nonzero(hit)):
            x = p[i] + t[i, jl] * r[i]
            out.append((float(x[0]), float(x[1]), int(i), float(t[i, jl]), int(jj[jl])))
    out.sort(key=lambda h: (h[2], h[3]))
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


def _xy(p):
    return [round(float(p[0]), 1), round(float(p[1]), 1)]


def _nearest_on(line, c):
    """(distance, point) of the point of polyline `line` nearest c."""
    line = np.asarray(line, dtype=float)
    d, along = cond.polyline_distance(np.array([c[0]]), np.array([c[1]]), line)
    seg = np.hypot(*np.diff(line, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    a = float(along[0])
    return float(d[0]), np.array([np.interp(a, s, line[:, 0]), np.interp(a, s, line[:, 1])])


def _end_dir(line, back_m=5.0):
    """Unit direction at the END of a polyline (over about back_m)."""
    line = np.asarray(line, dtype=float)
    p = line[-1]
    for q in line[-2::-1]:
        if np.hypot(*(p - q)) >= back_m:
            return _unit(p - q)
    return _unit(p - line[0])


class _Pool:
    """Road or rail centrelines with their bounding boxes and end points, worked out once per
    run (a run's road layer can hold thousands of pieces)."""

    def __init__(self, kind, lines):
        self.kind = kind
        self.arr = []
        for e in lines or []:
            a = np.asarray(e, dtype=float)
            self.arr.append(a[:, :2] if a.ndim == 2 and a.shape[1] >= 2 else np.zeros((0, 2)))
        n = len(self.arr)
        self.bb = np.full((n, 4), np.nan)
        self.ends = np.full((n, 2, 2), np.inf)
        for i, a in enumerate(self.arr):
            if len(a) >= 2 and np.isfinite(a).all():
                self.bb[i] = (a[:, 0].min(), a[:, 1].min(), a[:, 0].max(), a[:, 1].max())
                self.ends[i, 0], self.ends[i, 1] = a[0], a[-1]

    def __len__(self):
        return len(self.arr)

    def near(self, box):
        """Indices of the lines whose bounding box meets box (xmin, ymin, xmax, ymax)."""
        b = self.bb
        if not len(b):
            return np.zeros(0, dtype=int)
        with np.errstate(invalid="ignore"):
            m = (b[:, 0] <= box[2]) & (b[:, 2] >= box[0]) & (b[:, 1] <= box[3]) & \
                (b[:, 3] >= box[1])
        return np.flatnonzero(m)


def merged_line(k, lines, tol=MERGE_M, max_turn_deg=MERGE_TURN_DEG, centre=None, reach=None):
    """lines[k] continued at both ends by the pieces whose end points touch its ends (within
    tol), the straightest continuation first (a turn of at most max_turn_deg: not a cross
    road), until `reach` metres from `centre`. Returns (line, open_start, open_end, used):
    open_* True where the line really ends there (no piece continues it); used = the indices of
    the pieces in it."""
    pool = lines if isinstance(lines, _Pool) else _Pool("", lines)
    arr, ends = pool.arr, pool.ends
    line = arr[k]
    used = {k}
    cos_max = math.cos(math.radians(max_turn_deg))
    opened = []
    for _side in (0, 1):
        is_open = False
        while True:
            p = line[-1]
            if centre is not None and reach is not None and \
                    float(np.hypot(*(p - np.asarray(centre)))) > reach:
                break
            d = _end_dir(line)
            best = None
            touch = np.flatnonzero((np.hypot(*(ends - p).transpose(2, 0, 1)) <= tol).any(axis=1))
            for j in touch:
                j = int(j)
                if j in used:
                    continue
                e = arr[j]
                for ee in (e, e[::-1]):
                    if float(np.hypot(*(ee[0] - p))) > tol:
                        continue
                    dn = _end_dir(ee[::-1])
                    dn = -dn if dn is not None else None
                    if d is None or dn is None:
                        continue
                    cosang = float(np.dot(d, dn))
                    if cosang >= cos_max and (best is None or cosang > best[0]):
                        best = (cosang, j, ee)
            if best is None:
                is_open = True
                break
            used.add(best[1])
            line = np.vstack([line, best[2][1:]])
        opened.append(is_open)
        line = line[::-1]
    # two reversals: the line is back in its own direction; opened = [end, start]
    return line, opened[1], opened[0], used


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


def _bbox(q, pad):
    return (float(q[:, 0].min()) - pad, float(q[:, 1].min()) - pad,
            float(q[:, 0].max()) + pad, float(q[:, 1].max()) + pad)


def _span_m(s):
    return max(0.0, float(getattr(s, "width_m", 0.0) or 0.0)) * max(
        1, int(getattr(s, "barrels", 1) or 1))


def _side_ground(g, pts, s_al, ks, both_sides=False):
    """The ground beside the road at the samples ks: per sample the higher of the two sides'
    lowest ground SIDE_IN_M..SIDE_OUT_M off the centreline (NaN where a side has less than half
    its samples). both_sides: the (len(ks), 2) lowest ground of each side instead."""
    off = np.arange(SIDE_IN_M, SIDE_OUT_M + 1e-9, SIDE_STEP_M)
    ks = np.asarray(ks, dtype=int)
    if not len(ks):
        return np.zeros(0)
    nrm = np.array([[-t[1], t[0]] for t in (_tangent(pts, int(k)) for k in ks)])
    both = np.concatenate([off, -off])                              # left, then right
    q = pts[ks][:, None, :] + both[None, :, None] * nrm[:, None, :]   # (k, 2 * n_off, 2)
    z = g.sample_bilinear(q[..., 0].ravel(), q[..., 1].ravel()).reshape(len(ks), 2, len(off))
    ok = np.isfinite(z)
    with np.errstate(invalid="ignore"):
        lowest = np.where(ok.sum(axis=2) * 2 >= len(off),
                          np.min(np.where(ok, z, np.inf), axis=2), np.nan)
    if both_sides:
        return lowest
    return np.max(lowest, axis=1)          # NaN when a side has too little data


def _parallel_low(lr, sk, s0, half_cut, tol=CHANNEL_M, reach_m=DITCH_M):
    """True when the low ground beside the road at the cut runs ALONG the road: on either side
    of the road, the side's ground stays within `tol` of its lowest at the cut for at least
    `reach_m` past the cut's stretch, either way (a roadside ditch, a borrow area or a pond
    along the embankment toe). A channel passing under a deck is low across the road, over
    about its own width along it."""
    inside = np.abs(sk - s0) <= half_cut
    for j in (0, 1):
        col = lr[:, j]
        if not np.isfinite(col[inside]).any():
            continue
        low = float(np.nanmin(col[inside])) + tol
        for sgn in (-1.0, 1.0):
            out = np.flatnonzero((sgn * (sk - s0) > half_cut))
            out = out[np.argsort(sgn * (sk[out] - s0))]
            run = 0.0
            for k in out:
                if not (np.isfinite(col[k]) and col[k] <= low):
                    break
                run = abs(sk[k] - s0) - half_cut
            if run >= reach_m:
                return True
    return False


def _clearly_better(t, ch, v_cut, span):
    """A candidate crossing clearly better than a cut with only the weak 'ground beside the
    road is lower' signal `ch`: a DEM opening at least CLEAR_DEPTH_M and CLEAR_FACTOR x ch
    deep, at least CLEAR_WIDTH_FRAC of the span wide (capped at 15 m), its bed down at the
    ground beside the cut (within CHANNEL_M), and with a channel line when the run has them
    (the candidate list already requires it). Used only where the low ground runs along the
    road (a ditch / borrow area / pond): low ground across the road at the cut is never
    overturned (_judge)."""
    if t["how"] != "dem_opening" or t["depth"] is None:
        return False
    return (t["depth"] >= max(CLEAR_DEPTH_M, CLEAR_FACTOR * ch)
            and t["width"] >= min(15.0, CLEAR_WIDTH_FRAC * span)
            and t["bed"] <= v_cut + CHANNEL_M)


def _window_max(z, s, lo, hi):
    sel = (s >= lo) & (s < hi) & np.isfinite(z)
    return float(np.max(z[sel])) if sel.any() else np.nan


def _window_min(z, s, lo, hi):
    sel = (s >= lo) & (s < hi) & np.isfinite(z)
    return float(np.min(z[sel])) if sel.any() else np.nan


def _crossings(line, pts, s_al):
    """[(along-road s, point, unit direction of `line` there)] where polyline `line` crosses
    the densified road `pts`."""
    out = []
    line = np.asarray(line, dtype=float)
    for h in _seg_intersections(pts, line):
        i, t, j = h[2], h[3], h[4]
        sa = float(s_al[i] + t * (s_al[i + 1] - s_al[i]))
        out.append((sa, np.array(h[:2]), _unit(line[j + 1] - line[j])))
    return out


def _pick_line(s, line, c, pools, read_dem):
    """(kind, index, pool, crossing point) of the road / rail centreline the bridge carries,
    or (None, reason). pools: {"road": _Pool, "rail": _Pool}."""
    carries = getattr(s, "carries", "") or ""
    use = [pools[k] for k in ("road", "rail") if carries != {"road": "rail",
                                                             "rail": "road"}[k]]
    if carries == "rail" and not np.isfinite(pools["rail"].bb).any():
        return None, ("a rail bridge and the run has no rail lines: not checked; the cut stays "
                      "at the inventory location")
    best, near = None, []
    reach = ON_ROAD_M + float(np.hypot(*(line[-1] - line[0]))) + 1.0
    box = (c[0] - reach, c[1] - reach, c[0] + reach, c[1] + reach)
    for lines in use:
        kind = lines.kind
        for k in lines.near(box):
            k = int(k)
            e = lines.arr[k]
            for h in _seg_intersections(line, e):
                d = float(np.hypot(h[0] - c[0], h[1] - c[1]))
                if best is None or d < best[0]:
                    best = (d, kind, k, lines, np.array(h[:2]))
            if best is None:
                d, p = _nearest_on(e, c)
                if d <= ON_ROAD_M:
                    near.append((d, kind, k, lines, p))
    if best is not None:
        return best[1:], ""
    for d, kind, k, lines, p in sorted(near, key=lambda x: x[0]):
        try:
            q = np.array([c, p])
            g = read_dem(_bbox(q, 3.0), STEP_M)
            zc, ze = g.sample_bilinear(q[:, 0], q[:, 1])
        except Exception:                                         # noqa - never stop a run
            continue
        if np.isfinite(zc) and np.isfinite(ze) and abs(float(zc) - float(ze)) <= CROWN_TOL_M:
            return (kind, k, lines, p), ""
    what = {"rail": "rail line", "road": "road"}.get(carries, "road or rail centreline")
    return None, ("no {} crosses the bridge line (or has its midpoint on its crown within "
                  "{:.0f} m): not checked; the cut stays at the inventory location").format(
                      what, ON_ROAD_M)


def _judge(s, pools, read_dem, streams, radius_m, keep_m=KEEP_M, step=STEP_M):
    """Phase 1 for one bridge: the report row, and when the cut is on solid embankment its one
    candidate crossing (the nearest), as a dict, else None."""
    line = np.asarray(s.line, dtype=float)
    name = s.name or s.kind
    row = {"name": name, "kind": s.kind, "carries": getattr(s, "carries", "") or "",
           "method": "none", "verified": False, "moved_m": 0.0, "old_xy": None,
           "new_xy": None, "line": "", "target_xy": None, "note": ""}
    if len(line) < 2:
        row["note"] = "line has fewer than 2 points: not checked"
        return row, None
    c = _mid(line)
    row["old_xy"] = _xy(c)
    span = _span_m(s)
    half_cut = max(keep_m, 0.5 * span * 1.1)          # the stretch of road the cut covers
    shoulder = rp.OPENING_SHOULDER_M + 0.5 * span      # wide openings: a window that grows
    L0 = float(np.hypot(*(line[-1] - line[0])))
    picked, why = _pick_line(s, line, c, pools, read_dem)
    if picked is None:
        row["note"] = why
        return row, None
    kind, k_line, lines, p_cross = picked
    row["line"] = kind
    reach = radius_m + shoulder + 10.0
    merged, open_a, open_b, used = merged_line(k_line, lines, centre=c, reach=reach)
    pts_all = cond.densify(merged, step)
    k_all = int(np.argmin(np.hypot(pts_all[:, 0] - p_cross[0], pts_all[:, 1] - p_cross[1])))
    run = next(((a, b) for a, b in _clip_near(pts_all, c, reach) if a <= k_all < b), None)
    if run is None:
        row["note"] = "the road line does not reach the bridge: not checked"
        return row, None
    a, b = run
    pts = pts_all[a:b]
    i0 = k_all - a
    s_al = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
    s0 = float(s_al[i0])
    # the road line's true ends near the crossing: the profile cannot be judged there
    ends = []
    if open_a and a == 0:
        ends.append(s0 - s_al[0])
    if open_b and b == len(pts_all):
        ends.append(s_al[-1] - s0)
    try:
        g = read_dem(_bbox(pts, SIDE_OUT_M + 5.0), step)
        z = g.sample_bilinear(pts[:, 0], pts[:, 1])
    except Exception as ex:                                       # noqa - never stop a run
        row["note"] = "DEM not read along the road ({}): the cut stays at the inventory " \
                      "location".format(ex)
        return row, None
    _m, ops = rp.openings(s_al, z, shoulder_m=shoulder, baseline_m=rp.OPENING_BASELINE_M)
    hits = []
    for sl in streams or []:
        hits.extend(_crossings(sl, pts, s_al))
    # --- the cut stays: an opening, a channel line ---------------------------------------
    at = [o for o in ops if abs(s_al[o[0]] - s0) <= keep_m + 0.5 * o[2]]
    if at:
        o = min(at, key=lambda o: abs(s_al[o[0]] - s0))
        row.update(method="kept", verified=True, new_xy=row["old_xy"],
                   note="the cut already crosses the opening in the DEM ({:.1f} m deep, {:.0f} "
                        "m wide)".format(o[1], o[2]))
        return row, None
    at = [h for h in hits if abs(h[0] - s0) <= keep_m + 0.5 * span]
    if at:
        row.update(method="kept", verified=True, new_xy=row["old_xy"],
                   note="a channel line crosses the road at the cut")
        return row, None
    # --- the cut stays: the profile cannot be judged --------------------------------------
    if ends and min(ends) < half_cut + shoulder:
        row["note"] = ("the {} line ends {:.0f} m from the crossing (no piece continues it "
                       "within {:g} m): the profile cannot be judged; the cut stays at the "
                       "inventory location").format(kind, min(ends), MERGE_M)
        return row, None
    w = np.abs(s_al - s0) <= half_cut
    if not np.isfinite(z[w]).all():
        row["note"] = ("no DEM data on the road at the cut (water or a gap): the cut stays at "
                       "the inventory location")
        return row, None
    wide = np.abs(s_al - s0) <= half_cut + shoulder
    if np.isfinite(z[wide]).sum() * 2 < wide.sum():
        row["note"] = ("too little DEM data along the road near the cut: the cut stays at the "
                       "inventory location")
        return row, None
    # --- the cut stays: the road dips there (its low point, a sag, a wide opening) ----------
    zw = float(np.min(z[w]))
    lo, hi = s0 - half_cut, s0 + half_cut
    # A dip counts only where the cut is the road's LOCAL LOW POINT within that reach: the
    # road on each side rises at least DIP_M somewhere AND nowhere drops more than DIP_M below
    # the cut. On a road sagging into a coulee a misplaced cut on the near approach grade has
    # the far (creek) side higher than itself, but the road keeps falling past it to the real
    # opening: that is not the cut's sag (PR #198 review, finding 1).
    dips = []
    for reach_m in (shoulder, GRADE_LINE_M):
        ml = _window_max(z, s_al, lo - reach_m, lo)
        mr = _window_max(z, s_al, hi + 1e-9, hi + reach_m + 1e-9)
        if not (np.isfinite(ml) and np.isfinite(mr)):
            continue
        nl = _window_min(z, s_al, lo - reach_m, lo)
        nr = _window_min(z, s_al, hi + 1e-9, hi + reach_m + 1e-9)
        low = min(v for v in (nl, nr) if np.isfinite(v)) if np.isfinite([nl, nr]).any() \
            else zw
        if low < zw - DIP_M:
            continue                      # the road goes lower within this reach: no sag here
        dips.append(min(ml, mr) - zw)
    dip = max(dips) if dips else 0.0
    if dip >= DIP_M:
        row.update(method="kept", new_xy=row["old_xy"],
                   note="the road dips {:.1f} m at the cut (its low point, a sag or an opening "
                        "the opening rule cannot size): kept".format(dip))
        return row, None
    # --- the cut stays: the ground beside the road has a channel there --------------------
    near = np.flatnonzero(np.abs(s_al - s0) <= half_cut + LOCAL_M)
    ks = near[::max(1, int(round(SIDE_STEP_M / step)))]
    if i0 not in ks:
        ks = np.sort(np.append(ks, i0))
    lr = _side_ground(g, pts, s_al, ks, both_sides=True)
    v = np.max(lr, axis=1)
    sk = s_al[ks]
    vw = v[np.abs(sk - s0) <= half_cut]
    if not np.isfinite(vw).any():
        row["note"] = ("no DEM data beside the road at the cut (water or a gap): the cut stays "
                       "at the inventory location")
        return row, None
    v_cut = float(np.nanmin(vw))
    sides = []
    for sel in ((sk < s0 - half_cut), (sk > s0 + half_cut)):
        vv = v[sel]
        if np.isfinite(vv).any():
            sides.append(float(np.nanmedian(vv)))
    if not sides:
        row["note"] = ("no DEM data beside the road along it near the cut: the cut stays at the "
                       "inventory location")
        return row, None
    ch = min(sides) - v_cut
    # 'lower beside the road' is weak evidence (#198 follow-up, DWR-3034 0025008224: ponds and
    # a borrow strip 28-50 m off the embankment toe read 2.3 m, the creek's 6.6 m deep, 30 m
    # wide opening is 187 m away). Low ground running along the road is a ditch / borrow area,
    # not a crossing; otherwise the cut stays unless the nearest crossing is clearly better.
    weak, keep_weak = None, False
    if ch >= CHANNEL_M:
        keep_weak = not _parallel_low(lr, sk, s0, half_cut)
        if not keep_weak:
            weak = ("the ground beside the road is {:.1f} m lower at the cut, but the low ground "
                    "runs along the road (a ditch, borrow area or pond at the embankment toe), "
                    "not across it").format(ch)
        else:
            weak = ("the ground beside the road is {:.1f} m lower at the cut than along the "
                    "road (a channel under a deck or water surface in the LiDAR)").format(ch)
    # --- solid embankment: the nearest crossing ------------------------------------------
    cand = []
    in_r = lambda p: float(np.hypot(*(p - c))) <= radius_m              # noqa: E731
    hits_r = [h for h in hits if in_r(h[1])]
    # other roads / rails crossing this one: an opening there is a grade separation
    box = _bbox(pts, 1.0)
    other = [lines.arr[int(kk)] for kk in lines.near(box) if int(kk) not in used] + \
        [pools[o].arr[int(kk)] for o in ("road", "rail") if pools[o] is not lines
         for kk in pools[o].near(box)]
    under = []
    for ln in other:
        under.extend(h[0] for h in _crossings(ln, pts, s_al))
    for k, dep, wid in ops:
        if not in_r(pts[k]):
            continue
        sv = [h for h in hits_r if abs(h[0] - s_al[k]) <= STREAM_NEAR_M]
        if hits_r and not sv:
            continue                                   # an opening with no channel line
        if not sv and any(abs(u - s_al[k]) <= 0.5 * wid + 5.0 for u in under):
            continue                                   # a road / rail passes under: no creek
        cand.append({"how": "dem_opening", "s": float(s_al[k]), "p": pts[k].copy(),
                     "depth": dep, "width": wid, "bed": float(z[k]),
                     "stream": min(sv, key=lambda h: abs(h[0] - s_al[k]))[2] if sv else None,
                     "channel": bool(sv)})
    for h in hits_r:
        if any(x["how"] == "dem_opening" and abs(x["s"] - h[0]) <= STREAM_NEAR_M for x in cand):
            continue
        cand.append({"how": "channel_crossing", "s": h[0], "p": h[1].copy(), "depth": None,
                     "width": 0.0, "bed": None, "stream": h[2], "channel": True})
    if not cand:
        if keep_weak:
            row.update(method="kept", verified=True, new_xy=row["old_xy"], note=weak)
            return row, None
        row["note"] = ("no opening or channel crossing on the {} within {:.0f} m: the cut stays "
                       "at the inventory location").format(kind, radius_m)
        if weak:
            row["note"] = weak + "; " + row["note"]
        return row, None
    cand.sort(key=lambda x: (abs(x["s"] - s0), -(x["depth"] or 0.0), x["s"]))
    t = cand[0]
    dist = abs(t["s"] - s0)
    row["target_xy"] = _xy(t["p"])
    txt = ("DEM opening {:.1f} m below the road, {:.0f} m wide{}".format(
        t["depth"], t["width"], ", with a channel line" if t["channel"] else "")
        if t["how"] == "dem_opening" else "channel line crossing")
    # Low ground ACROSS the road at the cut (not a parallel ditch) is a channel under a deck in
    # the LiDAR as often as not, and such a deck hides the opening's depth: nothing about the
    # nearest crossing overturns it. Not a deeper opening (300 random layouts: 288 correct
    # bridges would move to another creek's open bridge), and not a channel line there with
    # none at the cut either: CHANNEL_LINES is often only the main stem clipped to the run
    # radius (algorithms.py), so a missing line at the cut is no evidence, and the lines'
    # coverage of the cut's surroundings cannot be judged reliably (PR #198 review of 9c646d6,
    # probe R4: a correct bridge over a 1.5 m unmapped creek moved 145 m to the main stem).
    if keep_weak:
        row.update(method="kept", verified=True, new_xy=row["old_xy"], target_xy=None,
                   note=weak + "; the nearest crossing ({}, {:.0f} m along the road) does not "
                   "overturn low ground across the road at the cut (a deck in the LiDAR hides "
                   "the opening's depth; a channel line missing at the cut is no evidence)"
                   .format(txt, dist))
        return row, None
    clear = weak is not None and _clearly_better(t, ch, v_cut, span)
    if clear:
        t["txt_why"] = ("{}; the {} is clearly better (at least {:g} m and {:g} x deeper, "
                        "its bed down at the ground beside the cut)").format(
                            weak, "opening" if t["how"] == "dem_opening" else "crossing",
                            CLEAR_DEPTH_M, CLEAR_FACTOR)
    elif t["how"] == "dem_opening" and not t["channel"] and t["bed"] > v_cut - BED_DROP_M:
        row["note"] = ("the nearest crossing ({}, {:.0f} m along the road at {}) has its bed at "
                       "{:.1f} m, not {:g} m below the ground beside the road at the cut ({:.1f} "
                       "m): no evidence the channel is there rather than at the cut; the cut "
                       "stays at the inventory location").format(
                           txt, dist, row["target_xy"], t["bed"], BED_DROP_M, v_cut)
        return row, None
    t.update(dist=dist, txt=txt, pts=pts, s_al=s_al, i0=i0, c=c, L0=L0, line=line,
             p_old=pts[i0].copy(), kind=kind)
    return row, t


def _taken_by(t, occupants):
    """Name of an occupant structure whose line crosses the road within TAKEN_M (or half the
    opening) of the target t, else None."""
    reach = max(TAKEN_M, 0.5 * (t["width"] or 0.0))
    for name, ln in occupants:
        ln = np.asarray(ln, dtype=float)
        if len(ln) < 2 or _nearest_on(ln, t["p"])[0] > reach:
            continue                    # a line crossing within `reach` passes this close
        for sa, _p, _d in _crossings(ln, t["pts"], t["s_al"]):
            if abs(sa - t["s"]) <= reach:
                return name
    return None


def _move(s, t, row):
    pts, i0 = t["pts"], t["i0"]
    p_new, p_old = t["p"], t["p_old"]
    moved = float(np.hypot(*(p_new - p_old)))
    line = t["line"]
    u0 = _unit(line[-1] - line[0])
    k_new = int(np.argmin(np.hypot(pts[:, 0] - p_new[0], pts[:, 1] - p_new[1])))
    t_new = _tangent(pts, k_new)
    t_old = _tangent(pts, i0)
    # length across the road kept: L0 * |sin(old angle)| / |sin(new angle)|
    sin_old = abs(float(u0[0] * t_old[1] - u0[1] * t_old[0])) if u0 is not None else 1.0
    across = t["L0"] * sin_old if sin_old > 0.2 else t["L0"]
    v = t["stream"]
    if v is None or abs(float(v[0] * t_new[1] - v[1] * t_new[0])) < 0.3:
        v = np.array([-t_new[1], t_new[0]])
    if u0 is not None and float(np.dot(v, u0)) < 0:
        v = -v
    sin_new = abs(float(v[0] * t_new[1] - v[1] * t_new[0]))
    L = across / max(sin_new, 0.5)
    snap_from = {"xy": row["old_xy"], "line": np.asarray(s.line, dtype=float).tolist(),
                 "inverts": [s.invert_up_m, s.invert_dn_m]}
    s.line = np.array([p_new - 0.5 * L * v, p_new + 0.5 * L * v])
    s.invert_up_m = s.invert_dn_m = None      # cleared: the cut goes to the lowest ground there
    row.update(method=t["how"], verified=True, moved_m=round(moved, 1), new_xy=_xy(p_new),
               note="{}: cut moved {:.0f} m along the {}; inverts cleared (the cut goes to the "
                    "lowest ground near its new ends){}".format(
                        t["txt"], moved, t["kind"],
                        "; " + t["txt_why"] if t.get("txt_why") else ""))
    # the original location on the structure itself (PR #198 review, finding 3): sub-runs
    # (sensitivity, probabilistic, cascade, nested, refine) share this object through shallow
    # copies; they report the move from it instead of judging the moved cut ('kept, verified')
    snap_from.update(to_xy=row["new_xy"], moved_m=row["moved_m"], method=row["method"],
                     line_kind=row["line"], target_xy=row["target_xy"], note=row["note"])
    s.snap_from = snap_from


def _row_snapped(s):
    """The report row of a bridge an earlier pass of this run already moved (s.snap_from):
    the move as it was made, not judged again (idempotent)."""
    f = s.snap_from
    return {"name": s.name or s.kind, "kind": s.kind, "carries": getattr(s, "carries", "") or "",
            "method": f.get("method", "dem_opening"), "verified": True,
            "moved_m": float(f.get("moved_m") or 0.0), "old_xy": f.get("xy"),
            "new_xy": f.get("to_xy"), "line": f.get("line_kind", ""),
            "target_xy": f.get("target_xy"), "note": f.get("note", ""),
            "snapped_earlier": True}


def snap_bridges(structures, roads, read_dem, streams=(), radius_m=None, rails=()):
    """Check (and, on evidence, move) every inventory bridge. roads / rails: centrelines (work
    CRS, m); a bridge whose facility kind is unknown matches either. read_dem(bounds, cell) ->
    Grid in metres (bare earth, no structure cuts). streams: channel lines. Returns (rows,
    notes, warnings); moved structures' lines are replaced in place and the original location
    is kept on the structure (s.snap_from). Idempotent: a bridge with snap_from (moved by an
    earlier pass, e.g. the parent run of a sub-run sharing the object) is reported as moved,
    from its original location, and not judged again."""
    note_env = ""
    if radius_m is None:
        radius_m, _raw, note_env = radius_from_env(with_note=True)
    radius_m = float(radius_m)
    rows, notes, warns = [], [], []
    if note_env:
        warns.append(note_env)
    if radius_m <= 0:
        notes.append("Bridge snap off ({} = 0 / off): inventory bridges are cut where the "
                     "inventory puts them".format(ENV))
        return rows, notes, warns
    bridges = [s for s in structures or [] if s.kind == "bridge"
               and getattr(s, "source", "inventory") == "inventory"]
    if not bridges:
        return rows, notes, warns
    notes.append("Bridge check: {} inventory bridge(s) against the road / rail they carry, "
                 "radius {:g} m ({})".format(len(bridges), radius_m, ENV))
    pools = {"road": _Pool("road", roads), "rail": _Pool("rail", rails)}
    if not (np.isfinite(pools["road"].bb).any() or np.isfinite(pools["rail"].bb).any()):
        warns.append("{} inventory bridge(s) not checked against a road crossing: the run has "
                     "no road or rail lines; their cuts stay at the inventory location".format(
                         len(bridges)))
        return rows, notes, warns
    judged = []
    for s in bridges:
        if isinstance(getattr(s, "snap_from", None), dict):
            judged.append((s, _row_snapped(s), None))     # moved earlier in this run
            continue
        try:
            r, t = _judge(s, pools, read_dem, streams, radius_m)
        except Exception as ex:                                   # noqa - never stop a run
            r, t = {"name": s.name or s.kind, "kind": s.kind,
                    "carries": getattr(s, "carries", "") or "", "method": "none",
                    "verified": False, "moved_m": 0.0, "old_xy": None, "new_xy": None,
                    "line": "", "target_xy": None,
                    "note": "not checked ({}): the cut stays at the inventory location".format(
                        ex)}, None
        judged.append((s, r, t))
    movable = {id(s) for s, _r, t in judged if t is not None}
    occupants = [(o.name or o.kind, o.line) for o in structures or []
                 if id(o) not in movable and len(np.asarray(o.line)) >= 2]
    props = []
    for s, r, t in judged:
        if t is None:
            continue
        who = _taken_by(t, occupants)
        if who is not None:
            r["note"] = ("the nearest crossing ({}, {:.0f} m along the road at {}) is already "
                         "cut by {}: the cut stays at the inventory location").format(
                             t["txt"], t["dist"], r["target_xy"], who)
            continue
        props.append((t["dist"], str(r["name"]), tuple(r["old_xy"]), s, r, t))
    # crossings handed out once, nearest bridge first (not in the inventory's order)
    given = []
    for _d, _n, _o, s, r, t in sorted(props, key=lambda x: x[:3]):
        clash = None
        for g_t, g_name in given:
            same_line = float(np.min(np.hypot(t["pts"][:, 0] - g_t["p"][0],
                                              t["pts"][:, 1] - g_t["p"][1]))) <= 2.0 * STEP_M
            if same_line and float(np.hypot(*(t["p"] - g_t["p"]))) <= max(
                    TAKEN_M, 0.5 * ((t["width"] or 0.0) + (g_t["width"] or 0.0))):
                clash = g_name
                break
        if clash is not None:
            r["note"] = ("the nearest crossing ({}, {:.0f} m along the road at {}) is nearer "
                         "bridge {} (moved there): the cut stays at the inventory "
                         "location").format(t["txt"], t["dist"], r["target_xy"], clash)
            continue
        _move(s, t, r)
        given.append((t, r["name"]))
    for s, r, t in judged:
        rows.append(r)
        msg = "Bridge {}: {}".format(r["name"], r["note"])
        if r["method"] in METHODS_MOVED:
            msg += " (from {} to {}{})".format(r["old_xy"], r["new_xy"],
                                               "; moved earlier in this run, not judged again"
                                               if r.get("snapped_earlier") else "")
            warns.append("Inventory bridge {} moved {:.0f} m along the {} from {} to {} ({}: "
                         "{}); check it on the DEM".format(
                             r["name"], r["moved_m"], r["line"] or "road", r["old_xy"],
                             r["new_xy"], r["method"], r["note"].split(": cut moved")[0]))
        notes.append(msg)
    if rows:
        notes.append("Inventory bridges: {}".format(snap_counts_text(snap_counts(rows))))
    stay = [r for r in rows if r["method"] not in METHODS_MOVED and not r["verified"]]
    if stay:
        warns.append("{} inventory bridge(s) stay at the inventory location without an opening "
                     "or a channel seen at the cut (as before 3.10.90; check them): {}".format(
                         len(stay), "; ".join("{} ({})".format(r["name"], r["note"])
                                              for r in stay[:12]) +
                         (" and {} more".format(len(stay) - 12) if len(stay) > 12 else "")))
    return rows, notes, warns


def snap_bridge(s, roads, read_dem, streams=(), radius_m=SNAP_RADIUS_M, occupied=(), rails=()):
    """One bridge (tools/check_dwr3034_road_bridge.py): snap_bridges with the other structures'
    lines `occupied` [(name, line)] as fixed. Returns its row."""
    from .structures import Structure
    others = [Structure("notch", np.asarray(ln, dtype=float), name=str(n)) for n, ln in occupied]
    rows, _n, _w = snap_bridges([s] + others, roads, read_dem, streams, radius_m, rails)
    return rows[0] if rows else {"name": s.name, "method": "none", "note": "not checked"}


def check_run(st, read_dem):
    """The Runner's step (core/workflow.run_dam): st.structures' inventory bridges against
    st.road_lines / st.rail_lines and the channel lines. Returns (report entry or None, notes,
    warnings, moved: bool)."""
    structs = list(getattr(st, "structures", None) or [])
    if not any(s.kind == "bridge" and getattr(s, "source", "inventory") == "inventory"
               for s in structs):
        return None, [], [], False
    radius, raw, _note = radius_from_env(with_note=True)
    edits = getattr(st, "edits", None)
    chans = [c.line for c in (edits.channels if edits is not None else [])]
    try:
        rows, notes, warns = snap_bridges(structs, getattr(st, "road_lines", None) or [],
                                          read_dem, chans, None,
                                          getattr(st, "rail_lines", None) or [])
    except Exception as e:                                       # noqa - never stop the run
        rows, notes, warns = [], [], [
            "inventory bridges not checked against the road crossings ({}): their cuts stay at "
            "the inventory location".format(e)]
    entry = {"radius_m": radius, "env": raw, "off": radius <= 0, "bridges": rows,
             "note": "" if rows else " ".join(warns),
             "moved": sum(1 for r in rows if r["method"] in METHODS_MOVED),
             "kept_verified": sum(1 for r in rows if r["method"] == "kept" and r["verified"]),
             "not_verified": sum(1 for r in rows if r["method"] not in METHODS_MOVED
                                 and not r["verified"]),
             "counts": snap_counts(rows)}       # 3.10.95: the one breakdown (road / rail)
    return entry, notes, warns, entry["moved"] > 0


SNAP_CLASSES = ("kept_verified", "kept_unverified", "none", "moved")
SNAP_CLASS_LABELS = {"kept_verified": "kept, verified", "kept_unverified": "kept, not verified",
                     "none": "not matched to a line", "moved": "moved"}


def snap_class(r):
    """3.10.95: one of SNAP_CLASSES for a bridge row: moved (METHODS_MOVED), kept and verified,
    kept and not verified, or none (no line matched / not checked)."""
    m = r.get("method")
    if m in METHODS_MOVED:
        return "moved"
    if m == "kept":
        return "kept_verified" if r.get("verified") else "kept_unverified"
    return "none"


def snap_counts(rows):
    """3.10.95 (DWR-3034 REVIEW 9.8 item 6): the one breakdown of the bridge rows,
    {class: {"total", "road", "rail"[, "other"]}} over SNAP_CLASSES, with 'carries' (else the
    matched 'line') as road / rail. DWR-3034: kept_verified 49 = 24 + 25, kept_unverified
    6 = 5 + 1, none 2 = 2 + 0, moved 1 = 1 + 0."""
    out = {c: {"total": 0, "road": 0, "rail": 0} for c in SNAP_CLASSES}
    for r in rows or ():
        c = out[snap_class(r)]
        k = r.get("carries") or r.get("line") or ""
        k = k if k in ("road", "rail") else "other"
        c["total"] += 1
        c[k] = c.get(k, 0) + 1
    return out


def snap_counts_text(counts):
    """'49 kept, verified (24 road + 25 rail); 6 kept, not verified (5 road + 1 rail); ...'"""
    parts = []
    for c in SNAP_CLASSES:
        d = counts.get(c) or {}
        extra = " + {} unknown".format(d["other"]) if d.get("other") else ""
        parts.append("{} {} ({} road + {} rail{})".format(
            d.get("total", 0), SNAP_CLASS_LABELS[c], d.get("road", 0), d.get("rail", 0), extra))
    return "; ".join(parts)


def report_lines(entry):
    """run_report.md section from run_report.json bridge_snap."""
    if isinstance(entry, list):                    # the first 3.10.90 draft wrote the rows only
        entry = {"bridges": entry}
    rows = entry.get("bridges") or []
    out = ["## Inventory bridges checked against the road (3.10.90)", ""]
    if entry.get("off"):
        return out + ["Off ({} = {}): every inventory bridge is cut where the inventory puts "
                      "it.".format(ENV, entry.get("env")), ""]
    if not rows:
        return out + [entry.get("note") or "No inventory bridge was checked.", ""]
    counts = entry.get("counts") or snap_counts(rows)
    out += ["Radius {:g} m ({}). A bridge moves only when its inventory place is solid "
            "embankment and the nearest crossing is plain; 'verified' = an opening or a channel "
            "was seen at the final cut.".format(float(entry.get("radius_m") or 0.0), ENV), "",
            "{} bridge(s): {}.".format(len(rows), snap_counts_text(counts)), "",
            "| Bridge | Carries | Result | Verified | Moved (m) | From (x, y) | To (x, y) | Why |",
            "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        xy = lambda p: "" if not p else "{:.1f}, {:.1f}".format(p[0], p[1])     # noqa: E731
        out.append("| {} | {} | {} | {} | {:.0f} | {} | {} | {} |".format(
            str(r.get("name", "")).replace("|", "/"), r.get("carries") or r.get("line") or "",
            r.get("method", ""), "yes" if r.get("verified") else "no",
            float(r.get("moved_m") or 0.0), xy(r.get("old_xy")),
            xy(r.get("new_xy") if r.get("method") in METHODS_MOVED else None),
            str(r.get("note", "")).replace("|", "/")))
    return out + [""]
