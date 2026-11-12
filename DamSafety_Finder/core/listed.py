"""Is a found structure already listed? (Finder 0.1)

Pure Python. Every candidate the Finder tools report (an embankment, a pond, a road crossing) is
compared with the known structures the engineer gives (DWR dams, dugouts and unknown structures,
the NID, NBI / NDDOT culverts, USGS / SARP stream crossings, ...):

  listed   a known point within `radius_m` of the candidate, or inside its outline (a pond
           polygon, buffered by `radius_m`)
  unsure   the nearest known point is within `unsure_factor` x radius_m: the inventory point may
           sit at the far end of the pool, on the outlet or the old location - check it
  new      nothing known within that distance: not in any list given

The nearest known point (its id, source and distance) is kept in every case. The status is only
as good as the lists given: a candidate is "new" to those lists, not necessarily to the state.
"""
import math


LISTED, UNSURE, NEW = "listed", "unsure", "new"


class Known:
    """Known structures as points in the grid CRS, with a bucket index for the nearest search."""

    def __init__(self, points, bucket_m=500.0):
        """points: iterable of (x, y, id, source)."""
        self.pts = [(float(x), float(y), str(i), str(s)) for x, y, i, s in points]
        self.b = float(bucket_m)
        self.grid = {}
        for k, (x, y, _i, _s) in enumerate(self.pts):
            self.grid.setdefault((int(math.floor(x / self.b)), int(math.floor(y / self.b))), []).append(k)

    def __len__(self):
        return len(self.pts)

    def near(self, x, y, dist):
        """[(distance, index)] of the points within dist of (x, y), nearest first."""
        n = int(math.ceil(dist / self.b))
        bx, by = int(math.floor(x / self.b)), int(math.floor(y / self.b))
        out = []
        for i in range(bx - n, bx + n + 1):
            for j in range(by - n, by + n + 1):
                for k in self.grid.get((i, j), ()):
                    px, py = self.pts[k][0], self.pts[k][1]
                    d = math.hypot(px - x, py - y)
                    if d <= dist:
                        out.append((d, k))
        out.sort()
        return out


def point_in_ring(x, y, ring):
    """Even-odd rule; ring = [(x, y), ...] (closed or not)."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            xc = xi + (y - yi) * (xj - xi) / ((yj - yi) or 1e-12)
            if x < xc:
                inside = not inside
        j = i
    return inside


def point_in_polygon(x, y, rings):
    """rings[0] = outer ring, the rest holes."""
    if not rings or not point_in_ring(x, y, rings[0]):
        return False
    return not any(point_in_ring(x, y, h) for h in rings[1:])


def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_polygon(x, y, rings):
    """0 inside, else the distance to the nearest edge."""
    if point_in_polygon(x, y, rings):
        return 0.0
    best = math.inf
    for ring in rings:
        for a, b in zip(ring, ring[1:] + ring[:1]):
            best = min(best, _seg_dist(x, y, a, b))
    return best


def check(x, y, known, radius_m, unsure_factor=2.0, polygons=None):
    """{'status', 'known_id', 'known_src', 'known_m'} for one candidate at (x, y). `polygons`
    (a list of polygons, each a list of rings) is the candidate's outline: a known point inside it
    or within radius_m of its edge makes it listed."""
    far = radius_m * max(1.0, unsure_factor)
    if polygons:
        # the pool may be large: search the whole outline, not only round the centre point
        xs = [p[0] for poly in polygons for p in poly[0]]
        ys = [p[1] for poly in polygons for p in poly[0]]
        reach = max(math.hypot(max(xs) - x, max(ys) - y), math.hypot(min(xs) - x, min(ys) - y)) + far
        best = None
        for _d, k in known.near(x, y, reach):
            px, py = known.pts[k][0], known.pts[k][1]
            d = min(dist_to_polygon(px, py, poly) for poly in polygons)
            if best is None or d < best[0]:
                best = (d, k)
        hits = [best] if best is not None and best[0] <= far else []
    else:
        hits = known.near(x, y, far)[:1]
    if not hits:
        return {"status": NEW, "known_id": "", "known_src": "", "known_m": None}
    d, k = hits[0]
    return {"status": LISTED if d <= radius_m else UNSURE, "known_id": known.pts[k][2],
            "known_src": known.pts[k][3], "known_m": round(d, 1)}


def status_counts(rows):
    out = {LISTED: 0, UNSURE: 0, NEW: 0}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out
