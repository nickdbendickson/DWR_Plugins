"""Traffic counts for the overtopped roads of tools 6 and 8 (ND REG_05.2026 5.2.1.2), 0.58.0.

The suite's roads layer (the offline pack's ND_roads_NG911) carries no traffic count, and the
county road layer (ND_county_roads, with AADT) is given as an extra roads layer. Each road was
therefore tested twice, and the copy with no count was rated by the 'unknown AADT' assumption
(Medium): an overtopped local road with a count of 83 still made the dam Medium (DWR-2827), and a
US / State highway with more than 2,000 a day was Medium instead of High (DWR-3271 on US 85,
DWR-2847 on ND 23). core/aadt_join.py, a byte-identical copy of the Runner's, matches a road to
the traffic line it runs along (HPMS, ND county roads) or the NDDOT count station on it. This
module is what the two Processing tools need around it:

  * sources()   the GeoPackages to read: AADT_LAYERS, else the ones next to the roads layer
                (aadt_join.siblings: HPMS_ND_<year>, NDDOT_traffic_counts, ND_county_roads and
                their per-dam pack_* clips), plus every ROADS_MORE GeoPackage (one without an
                AADT field is skipped);
  * Join        reads them once per run area: the TILE_M tiles around a road are read through
                each file's R-tree the first time a road there needs a count and kept for the
                run, so the next road (and the next dam nearby) costs only the match;
  * describe(), review_text()  the text the tools write.

A road keeps its own count; one that matches nothing stays 'AADT unknown' (the ND rule's
assumption, Medium by default): nd_hazard is not changed. QGIS-free: the tools give the
coordinate transforms."""
import math
import os
import re

from . import aadt_join as AJ

MAX_M = AJ.DEFAULT_MAX_M          # 30 m: a traffic line or count station this close to the road
TILE_M = 2000.0                   # the read unit (work CRS metres)
MAX_LISTED = 4                    # roads named in a review text (then '+N more')

_LABELS = ((re.compile(r"hpms", re.I), "HPMS"),
           (re.compile(r"traffic_count", re.I), "an NDDOT count station"),
           (re.compile(r"county_roads", re.I), "ND county roads"))


def sources(aadt_layers="", roads_path="", roads_more=""):
    """The traffic GeoPackages for the join (aadt_join.join_sources)."""
    return AJ.join_sources(aadt_layers or "", roads_path or "", roads_more or "")


def source_file(source):
    """'ND_county_roads.gpkg (AADT)' (aadt_join's source label) -> 'ND_county_roads.gpkg'."""
    return str(source or "").rsplit(" (", 1)[0]


def source_label(source):
    """What a reader calls the source: HPMS, an NDDOT count station, ND county roads, else the
    file's name without .gpkg."""
    f = source_file(source)
    for rx, label in _LABELS:
        if rx.search(f):
            return label
    return os.path.splitext(f)[0] or "a traffic layer"


def describe(m):
    """'AADT 83 (2023) from ND county roads (ND_county_roads.gpkg)' for an aadt_join.match."""
    yr = " ({})".format(m["year"]) if m.get("year") else ""
    nm = str(m.get("name") or "").strip()
    return "AADT {:,.0f}{} from {} ({}{})".format(m["aadt"], yr, source_label(m.get("source")),
                                                 source_file(m.get("source")),
                                                 ", " + nm if nm and nm != "None" else "")


def review_text(what, cls, without, items):
    """The review flag: '<what> <cls> rests on traffic counts matched from other layers for N
    road(s) (road 12: AADT 83 ...; ...); without them <without>: review'. items: ['road 12: AADT
    83 from ...', ...]."""
    items = [str(i) for i in items if i]
    shown = items[:MAX_LISTED]
    more = len(items) - len(shown)
    return ("{} {} rests on traffic counts matched from other layers for {} road(s) ({}{}); "
            "without them {}: review".format(what, cls, len(items), "; ".join(shown),
                                             "; +{} more".format(more) if more > 0 else "",
                                             without))


def bbox(parts, pad=0.0):
    xs = [p[0] for ln in parts for p in ln]
    ys = [p[1] for ln in parts for p in ln]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def wet_test(depth, x0, y_top, cell, min_depth):
    """is_wet(x, y) for aadt_join.match: the cell under the point holds at least min_depth
    (a north-up grid; NaN and outside the grid are dry)."""
    ny, nx = depth.shape

    def wet(x, y):
        c = int(math.floor((x - x0) / cell))
        r = int(math.floor((y_top - y) / cell))
        if 0 <= r < ny and 0 <= c < nx:
            v = depth[r, c]
            return bool(v == v and v >= min_depth)
        return False
    return wet


class Join:
    """The traffic counts of one run. paths: sources(). to_work(srs_id) -> f(x, y) giving work-CRS
    (EPSG:5070) coordinates; to_layer_box(srs_id, box) -> the work-CRS box in the layer's CRS.
    Without them the files are taken to be in the work CRS. A file that cannot be read is
    reported once in .errors and left out; the join never stops a tool."""

    def __init__(self, paths, to_work=None, to_layer_box=None, max_m=MAX_M, tile_m=TILE_M):
        self.paths = [p for p in (paths or []) if p]
        self.to_work, self.to_layer_box = to_work, to_layer_box
        self.max_m, self.tile_m = float(max_m), float(tile_m)
        self.errors = []
        self.reads = 0              # tile reads (each one R-tree query per file)
        self.tried = 0              # roads matched against the counts
        self.matched = 0
        self._bad = set()
        self._done = set()          # tiles read
        self._feats, self._boxes, self._keys = [], [], set()
        self._grid = {}             # tile -> [feature index]

    def __bool__(self):
        return bool(self.paths)

    def _tiles(self, box):
        t = self.tile_m
        i0, i1 = int(math.floor(box[0] / t)), int(math.floor(box[2] / t))
        j0, j1 = int(math.floor(box[1] / t)), int(math.floor(box[3] / t))
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]

    def _add(self, f):
        pts = f["parts"] if f["kind"] == "point" else [p for ln in f["parts"] for p in ln]
        if not pts:
            return
        key = (f["source"], f["kind"], f["aadt"], f.get("year"), f.get("name"), len(pts),
               round(pts[0][0], 2), round(pts[0][1], 2), round(pts[-1][0], 2), round(pts[-1][1], 2))
        if key in self._keys:                     # read again with a neighbouring tile
            return
        self._keys.add(key)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        b = (min(xs), min(ys), max(xs), max(ys))
        k = len(self._feats)
        self._feats.append(f)
        self._boxes.append(b)
        for tk in self._tiles(b):
            self._grid.setdefault(tk, []).append(k)

    def _load(self, tiles):
        miss = [k for k in tiles if k not in self._done]
        if not miss:
            return
        t = self.tile_m
        q = (min(k[0] for k in miss) * t, min(k[1] for k in miss) * t,
             (max(k[0] for k in miss) + 1) * t, (max(k[1] for k in miss) + 1) * t)
        box = (lambda srs: self.to_layer_box(srs, q)) if self.to_layer_box else (lambda srs: q)
        for p in self.paths:
            if p in self._bad:
                continue
            try:
                feats = AJ.read_layers([p], self.to_work, box)
            except Exception as e:                 # noqa  (an unreadable file: left out, said once)
                self._bad.add(p)
                self.errors.append("{}: {}".format(os.path.basename(p), e))
                continue
            for f in feats:
                self._add(f)
        self._done.update(miss)
        self.reads += 1

    def features(self, box):
        """The traffic features whose box meets `box` (work CRS), reading what is not read yet."""
        tiles = self._tiles(box)
        self._load(tiles)
        seen, out = set(), []
        for tk in tiles:
            for k in self._grid.get(tk, ()):
                if k in seen:
                    continue
                seen.add(k)
                b = self._boxes[k]
                if b[2] >= box[0] and b[0] <= box[2] and b[3] >= box[1] and b[1] <= box[3]:
                    out.append(self._feats[k])
        return out

    def match(self, parts, is_wet=None):
        """aadt_join.match for a road ([[(x, y)...]] in the work CRS), with 'text' (describe()),
        or None. is_wet(x, y): the road's overtopped part takes the count where the water crosses."""
        parts = [ln for ln in (parts or []) if len(ln) >= 2]
        if not self.paths or not parts:
            return None
        self.tried += 1
        feats = self.features(bbox(parts, self.max_m + 1.0))
        m = AJ.match(parts, feats, max_m=self.max_m, is_wet=is_wet) if feats else None
        if m:
            self.matched += 1
            m["text"] = describe(m)
        return m

    def summary(self):
        return "{} of {} road(s) without AADT matched in {}".format(
            self.matched, self.tried, ", ".join(os.path.basename(p) for p in self.paths) or "-")
