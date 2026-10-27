"""DWR dugouts (ND DWR's own list of excavated stock ponds) for screening. Pure Python.

Tool 1 keeps a DWR dugout out of the dams it screens; tool 16 labels a found pond that is a DWR
dugout and classes it a dugout (only what stands above the natural ground is released).

A place (x, y in metres, EPSG:5070) is a DWR dugout when it lies inside a DWR dugout polygon or
within `buffer_m` of a DWR dugout point.

Why DEFAULT_BUFFER_M = 30 m (about 100 ft), from ND DWR's DWR_Dugouts / DWR_Dugouts_Polygons
exports of 2026-10 (1767 points, 1897 polygon rows for 1748 ids; checked read-only):
- the DWR dugout point sits inside its own DWR polygon for 1608 of the 1748 dugouts that have
  one; DWR coordinates carry 5 decimals (about 1 m);
- a typical DWR dugout is 188 x 90 ft (57 x 27 m): a point at its centre is about 30 m from the
  ends, so a dam point placed on the pit's spoil bank still matches;
- going wider starts to catch real dams next to a dugout: of the 10,089 DWR dams, 5 match at
  10 m, 6 at 30 m (2 of them kept: a real hazard class / NID id), 9 at 50 m, 14 at 100 m; no
  DWR unknown structure matches up to 50 m (2 at 100 m).
It is the engineer's value (the tools' DUGOUT_BUF_FT parameter); 0 = polygons only.
"""
import math

DEFAULT_BUFFER_M = 30.0
DEFAULT_BUFFER_FT = 100.0         # the tools' parameter (ft): 30.48 m
REAL_HAZARD = ("high", "significant", "medium", "low")
_GRID = 250.0
MAX_RING_M = 20000.0      # a ring wider than this is a bad geometry, not a dugout


def reason(sid):
    return "DWR dugout {}".format(sid)


def protected(hazard, nid_id):
    """Why a dam that matches a dugout must still be screened ("" = it may be excluded): DWR gives
    it a real hazard class (high / significant / medium / low; 'Undetermined' or blank is not),
    or it has an NID id."""
    hz = str(hazard or "").strip().lower()
    why = []
    if hz and any(hz.startswith(h) for h in REAL_HAZARD):
        why.append("hazard class {}".format(str(hazard).strip()))
    nid = str(nid_id or "").strip()
    if nid:
        why.append("NID id {}".format(nid))
    return " and ".join(why)


def _in_ring(x, y, ring):
    ins = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            ins = not ins
    return ins


def _seg_dist(px, py, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / L))
    return math.hypot(px - a[0] - t * dx, py - a[1] - t * dy)


def _finite(x, y):
    try:
        return math.isfinite(float(x)) and math.isfinite(float(y))
    except (TypeError, ValueError):
        return False


class DugoutIndex(object):
    """points: [(structure_id, x, y)]; polygons: [(structure_id, [exterior ring [(x, y), ...], ...])]
    (one entry per part or per feature), all in metres."""

    def __init__(self, points=(), polygons=(), buffer_m=DEFAULT_BUFFER_M, above_grade_af=None):
        self.buffer_m = max(0.0, float(buffer_m or 0.0))
        # {structure_id: DWR's storage_above_grade_acft} (tool 16 compares it with the LiDAR)
        self.above_grade_af = dict((str(k), float(v)) for k, v in (above_grade_af or {}).items())
        self.n_points = self.n_polygons = self.n_skipped = 0
        self._pts, self._polys = {}, {}
        for sid, x, y in points:
            if not _finite(x, y):                       # an empty point (NaN) is skipped
                continue
            self._pts.setdefault(self._cell(x, y), []).append((str(sid), float(x), float(y)))
            self.n_points += 1
        for sid, rings in polygons:
            rings = [[(float(a), float(b)) for a, b in r if _finite(a, b)] for r in rings]
            rings = [r for r in rings if len(r) >= 3]
            used = False
            # each ring is indexed by its own box: a feature whose parts lie far apart (the DWR
            # export has one with a stray part ~4,600 km off) would otherwise cover millions of
            # grid cells. A ring larger than MAX_RING_M across is not a dugout and is skipped.
            for r in rings:
                xs = [p[0] for p in r]
                ys = [p[1] for p in r]
                if max(xs) - min(xs) > MAX_RING_M or max(ys) - min(ys) > MAX_RING_M:
                    self.n_skipped += 1
                    continue
                used = True
                item = (str(sid), [r], (min(xs), min(ys), max(xs), max(ys)))
                for i in range(int(math.floor(min(xs) / _GRID)), int(math.floor(max(xs) / _GRID)) + 1):
                    for j in range(int(math.floor(min(ys) / _GRID)),
                                   int(math.floor(max(ys) / _GRID)) + 1):
                        self._polys.setdefault((i, j), []).append(item)
            if used:
                self.n_polygons += 1

    def summary(self):
        """The log line of what was read."""
        t = "DWR dugouts: {} point(s), {} polygon(s), buffer {:.0f} m".format(
            self.n_points, self.n_polygons, self.buffer_m)
        if self.n_skipped:
            t += "; {} polygon ring(s) over {:.0f} km across skipped (bad geometry)".format(
                self.n_skipped, MAX_RING_M / 1000.0)
        return t

    def __len__(self):
        return self.n_points + self.n_polygons

    @staticmethod
    def _cell(x, y):
        return int(math.floor(x / _GRID)), int(math.floor(y / _GRID))

    def match(self, x, y):
        """(structure_id, how) of the DWR dugout at (x, y), or None. A polygon that contains the
        place wins; else the nearest point within the buffer."""
        if not _finite(x, y):
            return None
        i, j = self._cell(x, y)
        for sid, rings, bb in self._polys.get((i, j), ()):
            if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and any(_in_ring(x, y, r) for r in rings):
                return sid, "inside the DWR dugout polygon"
        best = None
        if self.buffer_m > 0:
            k = int(math.ceil(self.buffer_m / _GRID))
            for di in range(-k, k + 1):
                for dj in range(-k, k + 1):
                    for sid, px, py in self._pts.get((i + di, j + dj), ()):
                        d = math.hypot(px - x, py - y)
                        if d <= self.buffer_m and (best is None or d < best[0]):
                            best = (d, sid)
        if best:
            return best[1], "{:.0f} m from the DWR dugout point".format(best[0])
        return None

    def match_area(self, rings, inner=None):
        """(structure_id, how) for a water body (exterior rings in metres; `inner` a point inside
        it): its inside point is a dugout (match), or a DWR dugout point lies inside it, or a DWR
        dugout polygon's corner lies inside it. None otherwise."""
        if inner is not None:
            m = self.match(*inner)
            if m:
                return m
        rings = [[p for p in r if _finite(*p)] for r in rings]
        rings = [r for r in rings if len(r) >= 3]
        if not rings:
            return None
        xs = [p[0] for r in rings for p in r]
        ys = [p[1] for r in rings for p in r]
        i0, j0 = self._cell(min(xs), min(ys))
        i1, j1 = self._cell(max(xs), max(ys))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                for sid, px, py in self._pts.get((i, j), ()):
                    if any(_in_ring(px, py, r) for r in rings):
                        return sid, "the DWR dugout point is inside the water body"
                for sid, prs, _bb in self._polys.get((i, j), ()):
                    for pr in prs:
                        if any(_in_ring(a, b, r) for a, b in pr for r in rings):
                            return sid, "the DWR dugout polygon overlaps the water body"
        return None


def above_grade_note(dwr_af, lidar_af):
    """Tool 16: (note, warn) comparing DWR's storage_above_grade_acft with the LiDAR volume
    above the natural ground. The LiDAR volume is what is released (it is measured the same way
    for every pond, and DWR gives 0 for 1745 of its 1767 dugouts - not measured, not "nothing
    above grade"); DWR's value is recorded, and a DWR value clearly larger is a warning to check
    the dugout by hand. dwr_af None or <= 0: ("", False)."""
    if dwr_af is None or not dwr_af > 0:
        return "", False
    note = ("DWR storage_above_grade_acft {:.2f} ac-ft (LiDAR above the ground {:.2f} ac-ft; the "
            "LiDAR value is released)".format(dwr_af, lidar_af))
    return note, dwr_af > max(1.5 * lidar_af, lidar_af + 1.0)


def decide(index, x, y, hazard="", nid_id=""):
    """For one dam at (x, y) m: None (not a dugout), ("exclude", sid, text) or ("keep", sid, text)
    where text is the log line's reason."""
    if index is None or not len(index):
        return None
    m = index.match(x, y)
    if not m:
        return None
    sid, how = m
    why = protected(hazard, nid_id)
    if why:
        return ("keep", sid, "matches DWR dugout {} ({}) but kept: {}".format(sid, how, why))
    return ("exclude", sid, "{} ({})".format(reason(sid), how))
