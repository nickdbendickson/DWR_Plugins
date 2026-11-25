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
coordinate transforms.

Around the Runner's match (its file is not changed), Join.match makes three checks (0.58.0 review):
  * a count station counts only where it is on the road's OVERTOPPED part (within 30 m of it) and
    on this road: its name matches the road's, or the traffic line nearest it is one the road runs
    along (when the road runs along none: one that runs the same way), so a station on a crossing
    road or on the highway beside a frontage road is not taken; aadt_join measured a station to the
    whole road;
  * the road's overtopped part decides: when none of the join's 15 m samples is wet (a narrow
    crossing), the samples next to the point the tool found overtopped are used, not the whole road;
  * a lower count never wins over a higher one on the same road: when another traffic line the
    road runs along (one aadt_join would also accept, or one named as the road), or a count station
    on it, puts the road in a higher 5.2.1.2 AADT band (> 2,000; 500-2,000; < 500), the higher count
    is taken (newest year, then the higher count, as the Hazard plugin). aadt_join keeps a traffic
    line before a station whatever their years and the nearest line before a farther one, so a
    modelled county count of 311 hid the 2025 NDDOT count of 2,265 on ASH COULEE DR, and county
    roads' 1,394 hid HPMS '9THSTE' 22,966 on 9TH ST E.
interstate_system() names a piece of the interstate system the name does not show (FUNC_SYS 1:
'RAMP', 'EXIT 63'), which tools 6 and 8 rate Interstate as the Hazard plugin does; interstate_shield()
names an NG9-1-1 SHIELD 'I' / 'EXIT' road outside it, which a matched count never lowers."""
import math
import os
import re

from . import aadt_join as AJ

MAX_M = AJ.DEFAULT_MAX_M          # 30 m: a traffic line or count station this close to the road
TILE_M = 2000.0                   # the read unit (work CRS metres)
MAX_LISTED = 4                    # roads named in a review text (then '+N more')
MAX_FEATS = 60000                 # traffic features kept for a run (about 160 MB); then the cache restarts

_LABELS = ((re.compile(r"hpms", re.I), "HPMS"),
           (re.compile(r"traffic_count", re.I), "an NDDOT count station"),
           (re.compile(r"county_roads", re.I), "ND county roads"))


_ABBR = {"STREET": "ST", "AVENUE": "AVE", "ROAD": "RD", "DRIVE": "DR", "HIGHWAY": "HWY", "BOULEVARD": "BLVD",
         "PARKWAY": "PKWY", "EXPRESSWAY": "EXPY", "LANE": "LN", "PLACE": "PL", "COURT": "CT", "CIRCLE": "CIR",
         "TRAIL": "TRL", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "NORTHEAST": "NE",
         "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
_SHIELDS = {"USHY", "STHY", "CORD", "BIA", "FSRD", "NONE", "NULL"}       # NG9-1-1 SHIELD codes tools append
_RE_ROUTE_CODE = re.compile(r"^(?:I|U|US|S|ND)-?(\d+)[NSEW]?$")          # NDDOT stations: I94E, U85N, S23E
_RE_ROUTE = re.compile(r"\b(?:INTERSTATE|HIGHWAY|HWY|US|ND|I)[\s-]*(\d+)(?:\s*(?:AND|&|/)\s*(\d+))?\b")
_BANDS = (2000.0, 500.0)            # ND 5.2.1.2: over 2,000 High, 500 - 2,000 Medium, under 500 Low


def band(aadt):
    """The 5.2.1.2 AADT band of a count: 2 over 2,000, 1 from 500 to 2,000, 0 under 500."""
    return 2 if aadt > _BANDS[0] else 1 if aadt >= _BANDS[1] else 0


def _tokens(text):
    s = str(text or "").upper()
    return [_ABBR.get(t, t) for t in re.findall(r"[A-Z0-9]+", s) if t not in _SHIELDS]


_NOT_ROUTE = {"FRONTAGE", "RAMP", "SERVICE", "EXIT", "BYP", "BYPASS", "BUS", "BUSINESS", "SPUR", "LOOP", "ACCESS",
              "CONNECTOR", "OLD"}     # 'HIGHWAY 2 FRONTAGE RD', 'HIGHWAY 2 ON RAMP': not the route's own count


def _routes(text):
    """Route numbers a name gives: 'HIGHWAY 85 N' -> {85}, 'HIGHWAY 2 AND 52 W' -> {2, 52}, 'U85N' -> {85};
    none for a frontage road, ramp, bypass or business route of it."""
    toks = _tokens(text)
    if _NOT_ROUTE & set(toks):
        return set()
    s = " ".join(toks)
    m = _RE_ROUTE_CODE.match(s.replace(" ", ""))
    if m:
        return {int(m.group(1))}
    return {int(n) for g in _RE_ROUTE.findall(s) for n in g if n}


def same_name(a, b):
    """True when two names clearly describe the same road: one inside the other once spaces and long
    forms are dropped ('ASH COULEE DR' / HPMS 'ASHCOULEEDR'; '158th Street Northeast' / '158TH ST NE';
    'JOHNSON ST' in 'JOHNSON ST S'), or the same route number ('HIGHWAY 85 N' / station 'U85N').
    An empty name is never the same."""
    sa, sb = "".join(_tokens(a)), "".join(_tokens(b))
    if not sa or not sb:
        return False
    short, long_ = sorted((sa, sb), key=len)
    if len(short) >= 4 and short in long_:
        return True
    return bool(_routes(a) & _routes(b))


def interstate_system(values):
    """True for a piece of the interstate system its name may not show, by the Hazard plugin's rule
    (DamSafety_Hazard/linear.py road_class): F_SYSTEM (HPMS), else FUNC_SYS (ND NG9-1-1), 1 - ramps named
    'RAMP', 'EXIT 63' or 'UNNAMED', 'MEMORIAL HWY SE', 'VILLARD ST' - or HPMS ROUTE_SIGNING 2; or an
    NG9-1-1 SHIELD 'I' with the route number in SHIELD_LBL (Hazard reads 'I 29' from the two; the 21
    Grand Forks I-29 'RAMP' pieces coded FUNC_SYS 7). Tools 6 and 8 rate it Interstate (High whatever
    its count). values: {lower-case field name: value}."""
    fs = _num(values.get("f_system"))
    if fs is None:
        fs = _num(values.get("func_sys"))
    rs = _num(values.get("route_signing"))
    if rs is None:
        rs = _num(values.get("routesigning"))
    if fs == 1 or rs == 2:
        return True
    return _shield(values) == "I" and bool(re.match(r"\s*\d", _text(values.get("shield_lbl"))))


def interstate_shield(values):
    """True for an NG9-1-1 SHIELD 'I' / 'EXIT' that interstate_system() does not take (SHIELD EXIT, or I with
    no route number). The Hazard plugin does not rate it Interstate, so tools 6 and 8 do not either; but a
    matched count never lowers it."""
    return _shield(values) in ("I", "EXIT")


def _text(v):
    return "" if v is None or str(v).strip() == "NULL" else str(v)


def _shield(values):
    return _text(values.get("shield")).strip().upper()


def _num(v):
    """A field value as a finite number, else None (NULL, '', text, inf), as the Hazard plugin's _num."""
    try:
        f = float(_text(v).strip() or "nan")
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


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


def grouped(items):
    """(entries, roads, segments) for review texts: items [(road, ident, text)] - road its name ('' when it
    has none), ident the feature id - or plain strings. A road cut into several NG9-1-1 segments that took
    the same count is ONE entry ('98TH ST SE (3 segments): AADT 28 ...'), a single segment
    '87TH ST NW (road 2348): AADT 60 ...', a road with no name 'road 12: ...'."""
    groups, order = {}, []
    for it in items:
        if not it:
            continue
        if isinstance(it, (tuple, list)):
            road, ident, text = (list(it) + ["", "", ""])[:3]
        else:
            road, ident, text = "", "", str(it)
        road = str(road or "").strip()
        key = (road.upper(), text) if road else ("#", ident, text, len(order))
        if key not in groups:
            groups[key] = [road, [], text]
            order.append(key)
        groups[key][1].append(ident)
    out = []
    for k in order:
        road, ids, text = groups[k]
        ids = [str(i) for i in ids if str(i) != ""]
        if road and len(ids) > 1:
            who = "{} ({} segments)".format(road, len(ids))
        elif road:
            who = "{} (road {})".format(road, ids[0]) if ids else road
        else:
            who = "road {}".format(ids[0]) if ids else ""
        out.append("{}: {}".format(who, text) if who else text)
    return out, len(order), sum(len(groups[k][1]) for k in order)


def review_text(what, cls, without, items):
    """The review flag: '<what> <cls> rests on traffic counts matched from other layers for N
    road(s) (87TH ST NW (road 12): AADT 83 ...; ...); without them <without>: review'. items: see
    grouped(); a road cut into segments counts once ('for 4 road(s), 11 segments')."""
    entries, n_roads, n_segs = grouped(items)
    shown = entries[:MAX_LISTED]
    more = len(entries) - len(shown)
    return ("{} {} rests on traffic counts matched from other layers for {} road(s){} ({}{}); "
            "without them {}: review".format(what, cls, n_roads,
                                             ", {} segments".format(n_segs) if n_segs > n_roads else "",
                                             "; ".join(shown), "; +{} more".format(more) if more > 0 else "",
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

    def __init__(self, paths, to_work=None, to_layer_box=None, max_m=MAX_M, tile_m=TILE_M, max_feats=MAX_FEATS):
        self.paths = [p for p in (paths or []) if p]
        self.to_work, self.to_layer_box = to_work, to_layer_box
        self.max_m, self.tile_m = float(max_m), float(tile_m)
        self.step = max(5.0, self.max_m / 2.0)       # aadt_join.match's sample spacing
        self.max_feats = int(max_feats)
        self.errors = []
        self.reads = 0              # tile reads (each one R-tree query per file)
        self.resets = 0             # cache restarts (more than max_feats features kept)
        self.tried = 0              # roads matched against the counts
        self.matched = 0
        self.raised = 0             # matches where a higher count on the same road won
        self.near_point = 0         # roads with no wet 15 m sample: matched next to the overtopped point
        self._bad = set()
        self._reset()

    def _reset(self):
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
        if len(self._feats) > self.max_feats:     # a statewide run: keep the memory bounded
            self._reset()
            self.resets += 1
            miss = list(tiles)
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

    def _wet(self, parts, is_wet, at):
        """The join's samples (aadt_join._samples) on the road's overtopped part: those is_wet calls wet;
        none -> those within one sample step of `at` (the point the tool found overtopped); none either
        (or no is_wet) -> all of them."""
        samp = AJ._samples(parts, self.step)
        if not is_wet:
            return samp, samp
        wet = [s for s in samp if is_wet(s[0], s[1])]
        if not wet and at is not None:
            r = self.step + 1.0
            wet = [s for s in samp if math.hypot(s[0] - at[0], s[1] - at[1]) <= r]
            if wet:
                self.near_point += 1
        return samp, (wet or samp)

    def _on_road(self, t, parts, wet, lines, name, own):
        """A count station that describes THIS road where it is overtopped: within max_m of the road and of
        its wet samples (to sampling), and named as the road, or else the traffic line nearest it (within
        max_m) is one the road itself runs along (own: those lines' ids) - or, when the road runs along none,
        one that runs the same way. A station on a crossing road, or on the highway beside a frontage road,
        is not taken; one with no traffic line near it is."""
        x, y = t["parts"][0]
        best = None
        for ln in parts:
            for a, b in zip(ln[:-1], ln[1:]):
                d, v = AJ._seg_dist(x, y, a, b)
                if best is None or d < best[0]:
                    best = (d, v)
        if best is None or best[0] > self.max_m:
            return False
        if min(math.hypot(x - s[0], y - s[1]) for s in wet) > self.max_m + self.step / 2.0:
            return False                       # by the road, but not where the water crosses it
        if same_name(name, t.get("name")):
            return True
        near = None
        for ln in lines:
            d, v = AJ._near_line(x, y, ln)
            if d <= self.max_m and v is not None and (near is None or d < near[0]):
                near = (d, v, ln)
        if near is None:
            return True
        if own:
            return id(near[2]) in own
        rv, lv = best[1], near[1]
        la, lb = math.hypot(*rv), math.hypot(*lv)
        if not la or not lb:
            return True
        return abs(rv[0] * lv[0] + rv[1] * lv[1]) / (la * lb) >= math.cos(math.radians(AJ.MAX_ANGLE_DEG))

    def _line_options(self, wet, lines, name=""):
        """(options, own): every traffic-line value the overtopped part runs along, with its share - as
        aadt_join.match finds them (each sample takes the nearest same-way line within max_m), and also a line
        NAMED as the road that is not the nearest (a county-roads line 1 m off hid HPMS '9THSTE' 5 m off on
        9TH ST E); a value counts when it covers MIN_SHARE of the samples or MIN_LEN_M of road - and the ids
        of the lines some sample took (the road's own lines)."""
        cos_max = math.cos(math.radians(AJ.MAX_ANGLE_DEG))
        groups, named, own = {}, {}, set()
        key = lambda t: (t["aadt"], t.get("year") or 0, t["source"], t.get("name") or "")
        same = {id(t) for t in lines if same_name(name, t.get("name"))}
        for x, y, (ux, uy) in wet:
            best = None
            for t in lines:
                d, v = AJ._near_line(x, y, t)
                if d > self.max_m or v is None:
                    continue
                L = math.hypot(*v) or 1.0
                if abs(ux * v[0] / L + uy * v[1] / L) < cos_max:
                    continue
                if best is None or d < best[0]:
                    best = (d, t)
                if id(t) in same:
                    named.setdefault(key(t), [set(), t])[0].add((x, y))
            if best:
                t = best[1]
                own.add(id(t))
                g = groups.setdefault(key(t), [0, t])
                g[0] += 1
        for k, (pts, t) in named.items():
            if k not in groups or groups[k][0] < len(pts):
                groups[k] = [len(pts), t]
        n = float(len(wet)) or 1.0
        return [(t, cnt / n) for cnt, t in groups.values()
                if cnt / n >= AJ.MIN_SHARE or cnt * self.step >= AJ.MIN_LEN_M], own

    def match(self, parts, is_wet=None, name="", at=None):
        """aadt_join.match for a road ([[(x, y)...]] in the work CRS), with 'text' (describe()), or None.
        is_wet(x, y): the road's overtopped part takes the count where the water crosses it; at: the point
        the tool found overtopped (used when no sample is wet); name: the road's name (a count station
        named as the road is on it). A higher count on the same road wins (see the module text); the
        match then carries 'over', the count it replaced."""
        parts = [ln for ln in (parts or []) if len(ln) >= 2]
        if not self.paths or not parts:
            return None
        self.tried += 1
        feats = self.features(bbox(parts, self.max_m + 1.0))
        if not feats:
            return None
        samp, wet = self._wet(parts, is_wet, at)
        if not samp:
            return None
        lines = [t for t in feats if t["kind"] == "line"]
        opts, own = self._line_options(wet, lines, name)
        stations = [t for t in feats if t["kind"] == "point" and self._on_road(t, parts, wet, lines, name, own)]
        keys = {(s[0], s[1]) for s in wet}
        m = AJ.match(parts, lines + stations, max_m=self.max_m,
                     is_wet=(lambda x, y: (x, y) in keys) if len(wet) < len(samp) else None)
        if not m:
            return None
        alts = opts + [(t, 1.0) for t in stations]
        top = max((band(t["aadt"]) for t, _sh in alts), default=-1)
        if top > band(m["aadt"]):
            t, sh = max(((t, sh) for t, sh in alts if band(t["aadt"]) == top),
                        key=lambda o: (o[0].get("year") or 0, o[0]["aadt"]))
            over = m
            m = {"aadt": t["aadt"], "year": t.get("year"), "name": t.get("name") or "", "source": t["source"],
                 "share": round(sh, 2), "over": over}
            self.raised += 1
        self.matched += 1
        m["text"] = describe(m)
        if m.get("over"):
            o = m["over"]
            m["text"] += " (not the AADT {:,.0f} from {}: the higher count on the same road)".format(
                o["aadt"], source_label(o.get("source")))
        return m

    def summary(self):
        extra = []
        if self.raised:
            extra.append("{} took a higher count on the same road".format(self.raised))
        if self.near_point:
            extra.append("{} matched next to the overtopped point (no wet 15 m sample)".format(self.near_point))
        if self.resets:
            extra.append("the traffic cache restarted {} time(s)".format(self.resets))
        return "{} of {} road(s) without AADT matched in {}{}".format(
            self.matched, self.tried, ", ".join(os.path.basename(p) for p in self.paths) or "-",
            "".join("; " + e for e in extra))
