"""AADT for roads that carry none (ND REG_05.2026 Section 5.2.1.2).

The ND NG9-1-1 road centerlines the suite uses as receptors have no traffic counts, so the
5.2.1.2 rule (AADT > 2,000 -> High, 500-2,000 -> Medium, < 500 -> Low) could not run and every
overtopped road fell to the 'AADT unknown' assumption (Medium by default). ND Dam Hazard joins
AADT from separate layers (HPMS, NDDOT traffic counts, ND county roads). This module does the
same join for the Runner's classification tool, from the same GeoPackages:

  * line layers (HPMS_ND_<year>.gpkg, ND_county_roads.gpkg ...): a road takes the AADT of the
    traffic line that runs along it - at least MIN_SHARE of the road's sample points lie within
    max_m of that line and the two run the same way (within MAX_ANGLE_DEG);
  * point layers (NDDOT_traffic_counts.gpkg): a count station within max_m of the road;
  * several matches: a traffic LINE before a count station (a station describes a point of the
    route, a line its whole segment), then the one covering most of the road, then the newest
    year, then the highest AADT (the conservative pick on a tie).

AADT fields are found by name (aadt, aadt_<year>, adt ...); the year by aadt_yr / aadt_year /
data_year. A zero or negative count is 'not counted'. Pure Python; the caller supplies the
coordinate transform and the search box (like culvert_guess.read_inventory_gpkg).
"""
import math
import os
import re
import sqlite3

AADT_NAMES = ("aadt", "aadt_total", "adt", "aadt_vn", "annualaveragedailytrafficcount",
              "trafficvol", "traffic")
YEAR_NAMES = ("aadt_yr", "aadt_year", "data_year", "year", "count_year")
_RE_YEAR_COL = re.compile(r"^aadt_?(\d{4})$")
NAME_NAMES = ("routename", "name", "st_name", "fs_rd_name", "station_id", "routenumber")
DEFAULT_MAX_M = 30.0
MAX_ANGLE_DEG = 30.0
MIN_SHARE = 0.3
MIN_LEN_M = 150.0       # or this much road on one value (long segments, partial overlap)
SHORT_M = 100.0         # a road this short that no line follows takes the lines it touches
# sibling files ND_Dam_Suite_Pack/roads/ carries (found next to the ROADS layer)
SIBLING_PATTERNS = (re.compile(r"^hpms_nd_\d{4}\.gpkg$", re.I),
                    re.compile(r"^nddot_traffic_counts\.gpkg$", re.I),
                    re.compile(r"^nd_county_roads\.gpkg$", re.I))


def siblings(roads_path):
    """AADT GeoPackages in the folder of the roads layer (read only)."""
    p = str(roads_path or "").split("|", 1)[0]
    folder = os.path.dirname(p)
    if not folder or not os.path.isdir(folder):
        return []
    out = []
    for n in sorted(os.listdir(folder)):
        if any(r.match(n) for r in SIBLING_PATTERNS) and os.path.join(folder, n) != p:
            out.append(os.path.join(folder, n))
    return out


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pick_fields(cols):
    low = {c.lower(): c for c in cols}
    aadt = next((low[n] for n in AADT_NAMES if n in low), None)
    yearcols = sorted(((int(m.group(1)), low[n]) for n in low for m in [_RE_YEAR_COL.match(n)] if m),
                      reverse=True)
    if aadt is None and yearcols:
        aadt = yearcols[0][1]
    year = next((low[n] for n in YEAR_NAMES if n in low), None)
    name = next((low[n] for n in NAME_NAMES if n in low), None)
    return aadt, year, name, (yearcols[0][0] if yearcols and aadt == yearcols[0][1] else None)


def read_layers(paths, make_transform=None, make_bbox=None):
    """Traffic features from GeoPackages: [{"kind": "line"|"point", "parts": [[(x, y)...]] or
    [(x, y)], "aadt", "year", "name", "source"}] in the work CRS. Layers without an AADT field
    are skipped. make_transform(srs_id) -> f(x, y); make_bbox(srs_id) -> (xmin, ymin, xmax,
    ymax) in the layer CRS, filtered through the R-tree first."""
    from ..damsuite import suitegpkg as G
    out = []
    for path in paths or []:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            layers = con.execute("SELECT table_name, column_name, srs_id "
                                 "FROM gpkg_geometry_columns").fetchall()
            for table, gcol, srs in layers:
                qt = '"{}"'.format(table.replace('"', '""'))
                cols = [r[1] for r in con.execute("PRAGMA table_info({})".format(qt))]
                aadt, year, name, ycol_year = _pick_fields(cols)
                if aadt is None:
                    continue
                tf = make_transform(srs) if make_transform else None
                bb = make_bbox(srs) if make_bbox else None
                sel = ['"{}"'.format(gcol), '"{}"'.format(aadt)] + \
                    ['"{}"'.format(c) for c in (year, name) if c]
                rt = "rtree_{}_{}".format(table, gcol)
                has_rt = bb is not None and con.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = ?", (rt,)).fetchone()
                if has_rt:
                    pk = [r[1] for r in con.execute("PRAGMA table_info({})".format(qt)) if r[5]]
                    cur = con.execute(
                        'SELECT {} FROM {} t JOIN "{}" r ON t."{}" = r.id WHERE r.maxx >= ? AND '
                        'r.minx <= ? AND r.maxy >= ? AND r.miny <= ?'.format(
                            ", ".join("t." + s for s in sel), qt, rt, pk[0] if pk else "fid"),
                        (bb[0], bb[2], bb[1], bb[3]))
                else:
                    cur = con.execute("SELECT {} FROM {}".format(", ".join(sel), qt))
                label = "{} ({})".format(os.path.basename(path), aadt)
                for row in cur:
                    v = _num(row[1])
                    if v is None or v <= 0 or row[0] is None:
                        continue
                    i = 2
                    yr = None
                    if year:
                        yr = _num(row[i])
                        i += 1
                    yr = int(yr) if yr and 1900 < yr < 2200 else ycol_year
                    nm = str(row[i]) if name and row[i] is not None else ""
                    geom, _s = G.parse_blob(row[0])
                    if not geom:
                        continue
                    lines = G.lines_of(geom)
                    fx = (lambda p: tf(p[0], p[1])) if tf else (lambda p: (p[0], p[1]))
                    if lines:
                        parts = [[fx(p) for p in ln] for ln in lines if len(ln) >= 2]
                        if parts:
                            out.append({"kind": "line", "parts": parts, "aadt": v, "year": yr,
                                        "name": nm, "source": label})
                        continue
                    pts = G.points_of(geom)
                    if pts:
                        out.append({"kind": "point", "parts": [fx(pts[0])], "aadt": v,
                                    "year": yr, "name": nm, "source": label})
        finally:
            con.close()
    return out


def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), (dx, dy)


def _samples(parts, step):
    """(x, y, unit direction) every `step` metres along the road parts."""
    out = []
    for ln in parts:
        for a, b in zip(ln[:-1], ln[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            L = math.hypot(dx, dy)
            if L == 0:
                continue
            n = max(1, int(L // step))
            for k in range(n):
                t = (k + 0.5) / n
                out.append((a[0] + t * dx, a[1] + t * dy, (dx / L, dy / L)))
    return out


def _bbox(parts, pad):
    xs = [p[0] for ln in parts for p in ln]
    ys = [p[1] for ln in parts for p in ln]
    return min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad


def _near_line(x, y, t):
    """(distance, segment vector) from (x, y) to the nearest segment of traffic line t."""
    dmin, dvec = float("inf"), None
    for ln in t["parts"]:
        for a, b in zip(ln[:-1], ln[1:]):
            dd, v = _seg_dist(x, y, a, b)
            if dd < dmin:
                dmin, dvec = dd, v
    return dmin, dvec


def _length(parts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for ln in parts for a, b in zip(ln[:-1], ln[1:]))


def match(road_parts, traffic, max_m=DEFAULT_MAX_M, max_angle_deg=MAX_ANGLE_DEG, is_wet=None):
    """The traffic feature that gives this road its AADT, or None. road_parts: [[(x, y)...]].

    The road is sampled every max(5 m, max_m / 2); is_wet(x, y) (optional) keeps only the
    overtopped samples, so a long segment takes the AADT where the water crosses it. Each
    sample takes the NEAREST traffic line within max_m that runs the same way (bearing within
    max_angle_deg: a crossing road is never taken). Lines are grouped by their value (HPMS and
    county files are cut into many short pieces), and a value is accepted when it covers
    MIN_SHARE of the samples or MIN_LEN_M of road. Order: a traffic LINE before a count
    station, then the larger share, then the newest year, then the higher AADT. A short road
    (<= SHORT_M; an intersection jog or a curved connector) that no line follows takes the
    traffic lines it touches (within max_m), the highest AADT on a tie (conservative). Count
    stations count when within max_m of the (overtopped part of the) road."""
    if not road_parts or not traffic:
        return None
    step = max(5.0, max_m / 2.0)
    allsamp = _samples(road_parts, step)
    if not allsamp:
        return None
    samp = [s for s in allsamp if is_wet(s[0], s[1])] if is_wet else allsamp
    samp = samp or allsamp
    rb = _bbox(road_parts, max_m)
    cos_max = math.cos(math.radians(max_angle_deg))

    def inbox(t):
        tb = _bbox(t["parts"], 0.0) if t["kind"] == "line" else (
            t["parts"][0][0], t["parts"][0][1], t["parts"][0][0], t["parts"][0][1])
        return not (tb[2] < rb[0] or tb[0] > rb[2] or tb[3] < rb[1] or tb[1] > rb[3])
    cand = [t for t in traffic if inbox(t)]
    lines = [t for t in cand if t["kind"] == "line"]
    vkey = lambda t: (t["aadt"], t.get("year") or 0, t["source"], t.get("name") or "")
    groups = {}                      # value -> [count of samples, a traffic feature]
    for x, y, (ux, uy) in samp:
        best = None
        for t in lines:
            d, v = _near_line(x, y, t)
            if d > max_m or v is None:
                continue
            L = math.hypot(*v) or 1.0
            if abs(ux * v[0] / L + uy * v[1] / L) < cos_max:
                continue
            if best is None or d < best[0]:
                best = (d, t)
        if best:
            g = groups.setdefault(vkey(best[1]), [0, best[1]])
            g[0] += 1
    n = float(len(samp))
    opts = []
    for _k, (cnt, t) in groups.items():
        share = cnt / n
        if share >= MIN_SHARE or cnt * step >= MIN_LEN_M:
            opts.append(((1, round(share, 3), t.get("year") or 0, t["aadt"]), t, share))
    if not opts and _length(road_parts) <= SHORT_M:
        for t in lines:
            d = min(_near_line(x, y, t)[0] for x, y, _u in allsamp)
            if d <= max_m:
                opts.append(((1, 0.0, t["aadt"], t.get("year") or 0), t, 0.0))
    if not opts:
        pts = [(x, y) for x, y, _u in samp]
        for t in cand:
            if t["kind"] != "point":
                continue
            x, y = t["parts"][0]
            d = min(min(_seg_dist(x, y, a, b)[0] for ln in road_parts
                        for a, b in zip(ln[:-1], ln[1:])),
                    min(math.hypot(x - px, y - py) for px, py in pts) if is_wet else float("inf"))
            if d <= max_m:
                opts.append(((0, 1.0, t.get("year") or 0, t["aadt"]), t, 1.0))
    if not opts:
        return None
    _k, t, share = max(opts, key=lambda o: o[0])
    return {"aadt": t["aadt"], "year": t.get("year"), "name": t.get("name") or "",
            "source": t["source"], "share": round(share, 2)}


# --------------------------------------------------------------------------------------
# Status of the Runner's class with respect to the 5.2.1.2 road / AADT check (DWR-3161)
STATUS_COMPLETE = "complete"
STATUS_PRELIMINARY = "preliminary (no AADT road check)"
AADT_GAP = "AADT not given"
HAZARD_SUMMARY = os.path.join("outputs", "Hazard_Classification_Summary.csv")
HAZARD_ROW = "ND classification (screening)"


def class_status(hazard_class, data_gaps, governing=None):
    """STATUS_PRELIMINARY when an overtopped road still has no AADT (its class rests on the
    assumption) and that matters: the class is not High, or it is High only through such
    assumed roads. governing: the governing findings (with .gap); when not given, a High class
    is taken as settled by other findings."""
    gaps = [g for g in data_gaps or () if AADT_GAP in str(g)]
    if not gaps:
        return STATUS_COMPLETE
    if str(hazard_class).lower() != "high":
        return STATUS_PRELIMINARY
    if governing is None:
        return STATUS_COMPLETE
    firm = [f for f in governing if AADT_GAP not in str(getattr(f, "gap", "") or "")]
    return STATUS_COMPLETE if firm else STATUS_PRELIMINARY


def hazard_module_class(dam_folder):
    """The Hazard plugin's ND class from <dam folder>/outputs/Hazard_Classification_Summary.csv,
    or None when that file (or the row) is not there."""
    import csv
    p = os.path.join(dam_folder or "", HAZARD_SUMMARY)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and row[0].strip().lower() == HAZARD_ROW.lower():
                    v = row[1].strip()
                    return v or None
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    return None


def apply(roads, parts_by_id, traffic, max_m=DEFAULT_MAX_M, is_wet=None):
    """Set .aadt on each road (aadt None) from its best traffic match; parts_by_id maps
    id(road) to its line parts. Returns the log [{road, aadt, year, source}]."""
    log = []
    for r in roads:
        if r.aadt is not None or id(r) not in parts_by_id:
            continue
        m = match(parts_by_id[id(r)], traffic, max_m=max_m, is_wet=is_wet)
        if m:
            r.aadt = m["aadt"]
            log.append({"road": r.id, "aadt": m["aadt"], "year": m["year"],
                        "source": m["source"]})
    return log


def markdown_lines(road_check):
    """The '## Road / AADT check' report section."""
    rc = road_check or {}
    out = ["", "## Road / AADT check (ND 5.2.1.2)", "",
           "- Status: {}".format(rc.get("hazard_class_status", STATUS_COMPLETE))]
    for j in rc.get("aadt_joined") or []:
        out.append("- AADT joined: {}: {:g} ({}) from {}".format(
            j["road"], j["aadt"], j.get("year") or "year n/a", j["source"]))
    if rc.get("flagged_structure"):
        out.append("- Flagged structure: {}. The hazard of record ({}) stands; the class above "
                   "is for information only.".format(rc["flagged_structure"],
                                                     rc.get("class_to_use")))
    if rc.get("hazard_module_class"):
        out.append("- Hazard plugin class ({}): {}. Use the Hazard plugin's class.".format(
            HAZARD_SUMMARY, rc["hazard_module_class"]))
    return "\n".join(out)
