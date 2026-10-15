"""Culverts the model can use.

A county inventory is surveyed. A road crossing a channel or a depression is
only a guess: the name starts with 'guess:' and surveyed is false. A guess is
not a culvert that was measured.

Inventory matching (3.x, culvert matching): a guess within MATCH_M (75 m by default,
an engineer's screening value) of an inventory culvert takes that culvert's real size:
span, rise, barrels and shape are copied, source becomes "NDDOT <BRIDGE_ID>" (or
"NBI <structure number>"), surveyed becomes True and match_m records the distance.
The NDDOT bridge inventory's CULVERT text ("DOUBLE, 10X7X88' RCB") is read with ND Dam
Hazard's parser (nddot_text.py, a verbatim copy). NBI records are culverts when item
43B (STRUCTURE_TYPE_043B) is 19 and bridges otherwise; bridges never size a guess.
NDDOT Center Line Pipes (CLP; every pipe under a state highway, NAD83 lon/lat; download
https://www.dot.nd.gov/gis/downloads/centerlinepipes.gdb.zip, refreshed weekly, convert the
FileGDB to a GeoPackage, e.g. ogr2ogr -f GPKG clp.gpkg centerlinepipes.gdb) are detected by
their fields (DIAMETER_E with PIPE_DESC / ROUTE_MEASURE / LEFT_ELEV_E); see clp_record.
Priority when several records are within the match distance: NDDOT bridge inventory, then
CLP, then NBI (no rise, no shape), then CLP records whose LOC_ERROR is not "NO ERROR" (their
position is not trusted); distance decides within a rank.
A guess near a structure from the STRUCTURES layer is dropped: that crossing is
already modelled. Matched guesses are cut into the DEM only when the engineer asks
for it (Settings.culvert_burn_matched); unmatched guesses never are.
"""

import math

from . import nddot_text as _nt

FT = 0.3048
MATCH_M = 75.0                 # default guess-to-inventory match distance (m)
NBI_CULVERT_43B = "19"         # NBI item 43B: 19 = culvert


def _cross(a, b, c, d):
    """Intersection of segments a-b and c-d, or None."""
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    if abs(den) < 1e-9:
        return None
    t = ((cx - ax) * sy - (cy - ay) * sx) / den
    u = ((cx - ax) * ry - (cy - ay) * rx) / den
    if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
        return (ax + t * rx, ay + t * ry)
    return None


def _first_cross(line_a, line_b, with_seg=False):
    for i in range(len(line_a) - 1):
        for j in range(len(line_b) - 1):
            hit = _cross(line_a[i], line_a[i + 1], line_b[j], line_b[j + 1])
            if hit:
                return (hit, (line_b[j], line_b[j + 1])) if with_seg else hit
    return (None, None) if with_seg else None


def _unit(a, b):
    dx, dy = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 0 else None


def write_candidates_csv(path, rows):
    """Write road-crossing guesses. They are not surveyed and are not model structures."""
    import os
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("name,kind,shape,width_m,height_m,barrels,source,surveyed,match_m,"
                 "x1,y1,x2,y2,note\n")
        for s in rows or []:
            line = [] if s.get("line") is None else list(s.get("line"))  # may be an ndarray
            a = line[0] if line else ("", "")
            b = line[-1] if line else ("", "")
            fh.write("{},{},{},{},{},{},{},{},{},{},{},{},{},{}\n".format(
                (s.get("name") or "").replace(",", ";"), s.get("kind") or "culvert",
                s.get("shape") or "", s.get("width_m"), s.get("height_m"),
                s.get("barrels") or "", s.get("source") or "guess",
                "yes" if s.get("surveyed") else "no",
                "" if s.get("match_m") is None else round(float(s["match_m"]), 1),
                a[0], a[1], b[0], b[1],
                (s.get("note") or "not surveyed").replace(",", ";")))
    return path


def from_inventory(rows):
    """Mark inventory culverts as surveyed. Rows are dicts with a line."""
    out = []
    for i, row in enumerate(rows or []):
        item = dict(row)
        item["kind"] = item.get("kind") or "culvert"
        item["source"] = "inventory"
        item["surveyed"] = True
        item["name"] = item.get("name") or "inventory culvert {}".format(i + 1)
        out.append(item)
    return out


def guess_crossings(roads, channels, length_m=12.0):
    """A road crossing a channel or a mapped depression is a guess, not a survey.

    roads and channels are lists of polylines, each a list of (x, y).
    """
    out = []
    for ri, road in enumerate(roads or []):
        if len(road) < 2:
            continue
        for ci, chan in enumerate(channels or []):
            if len(chan) < 2:
                continue
            hit, seg = _first_cross(road, chan, with_seg=True)
            if not hit:
                continue
            # a short line across the road, so the barrel has two ends
            ax, ay = road[0]
            bx, by = road[-1]
            dx, dy = bx - ax, by - ay
            n = (dx * dx + dy * dy) ** 0.5 or 1.0
            px, py = -dy / n * (length_m / 2.0), dx / n * (length_m / 2.0)
            out.append({
                "name": "guess: road {} crosses channel {}".format(ri + 1, ci + 1),
                "kind": "culvert",
                "shape": "box",
                "barrels": 1,
                "width_m": 1.2,
                "height_m": 0.9,
                "source": "guess",
                "surveyed": False,
                "line": [(hit[0] - px, hit[1] - py), (hit[0] + px, hit[1] + py)],
                "note": "not surveyed; a road crossing a depression or channel",
                "xy": (hit[0], hit[1]),
                "chan_dir": _unit(seg[0], seg[1]),
            })
    return out


# ---------------------------------------------------------------- inventory --
def _low(props):
    return {str(k).lower(): v for k, v in (props or {}).items()}


def _runner_shape(shape):
    """The Runner models box and pipe; an arch is a box of its span and rise."""
    return "pipe" if shape == "pipe" else "box"


def nbi_is_culvert(design_43b):
    """NBI item 43B = 19 is a culvert; every other main structure type is a bridge."""
    return str(design_43b or "").strip().strip("'").lstrip("0") == NBI_CULVERT_43B


def inventory_record(props, xy):
    """One inventory point as {"x", "y", "id", "source", "kind", "span_ft", "rise_ft",
    "barrels", "shape", "notes"} from NDDOT bridge inventory fields (BRIDGE_ID,
    DESIGNMAINDESC, CULVERT, TOTAL_SPANS) or NBI fields (STRUCTURE_NUMBER_008,
    STRUCTURE_TYPE_043B, MAIN_UNIT_SPANS_045, MAX_SPAN_LEN_MT_048); None for anything else."""
    p = _low(props)
    rec = {"x": float(xy[0]), "y": float(xy[1]), "span_ft": None, "rise_ft": None,
           "barrels": None, "shape": None, "notes": []}
    if "diameter_e" in p and ("pipe_desc" in p or "route_measure" in p or "left_elev_e" in p):
        return clp_record(props, xy)
    if "bridge_id" in p:
        bid = _nt._txt(p.get("bridge_id"))
        design = _nt._txt(p.get("designmaindesc"))
        ctext = _nt._txt(p.get("culvert"))
        is_culv = "culvert" in design.lower() or (not design and bool(ctext))
        rec.update(id=bid, source="NDDOT", kind="culvert" if is_culv else "bridge")
        if is_culv:
            c = _nt.parse_culvert_text(ctext)
            rec.update(span_ft=c["span_ft"], rise_ft=c["rise_ft"], shape=c["shape"],
                       barrels=c["barrels"], barrel_len_ft=c["barrel_len_ft"])
            rec["notes"] += c["notes"]
            if not rec["barrels"]:
                n = _nt._pos(p.get("total_spans"))
                rec["barrels"] = int(round(n)) if n else None
        return rec
    if "structure_number_008" in p or "structure_type_043b" in p:
        no = _nt._txt(p.get("structure_number_008"))
        is_culv = nbi_is_culvert(p.get("structure_type_043b"))
        rec.update(id=no.lstrip("0") or no, source="NBI",
                   kind="culvert" if is_culv else "bridge")
        span = _nt._pos(p.get("max_span_len_mt_048"))
        main = _nt._pos(p.get("main_unit_spans_045"))
        rec["span_ft"] = round(span / FT, 2) if span else None
        rec["barrels"] = int(round(main)) if main else None
        return rec
    return None


CLP_CATTLE_FT_BELOW = 12.0     # a cattle pass "diameter" below this is feet (4 = 4 ft box)


def _clp_elev(v):
    x = _nt._num(v)
    return round(x, 2) if x is not None and 500.0 <= x <= 4000.0 else None


def clp_record(props, xy):
    """An NDDOT Center Line Pipes point: PIPE_DESC -> shape and barrels (DOUBLE / TRIPLE /
    FOUR ROW ... = 2 / 3 / 4), DIAMETER_E (inches) -> span and rise; an arch / elliptical pipe
    takes ARCH_E (inches) as the other size (span = the larger); a cattle pass is a box when
    it has a size (a DIAMETER_E below 12 is feet - how NDDOT records them - ARCH_E the rise),
    else skipped (None). LENGTH_E (ft) is the barrel length. LEFT_ELEV_E / RIGHT_ELEV_E (ft) are
    kept as plan-datum inverts (clp_inverts_ft, invert_datum "plan"): NDDOT says they come
    from the original grading plans, possibly an old vertical datum, so the burn uses the
    LiDAR inverts (lidar_inverts) and these are only reported. LOC_ERROR other than
    "NO ERROR" -> loc_error set (lower priority in match_inventory)."""
    p = _low(props)
    desc = _nt._norm_text(p.get("pipe_desc"))
    rid, meas = _nt._num(p.get("route_id")), _nt._num(p.get("route_measure"))
    rid_t = "{:g}".format(rid) if rid is not None else "?"
    meas_t = "{:.4f}".format(meas).rstrip("0").rstrip(".") if meas is not None else "?"
    rec = {"x": float(xy[0]), "y": float(xy[1]), "source": "NDDOT CLP",
           "id": "{}:{}".format(rid_t, meas_t), "kind": "culvert", "span_ft": None,
           "rise_ft": None, "barrels": None, "shape": None, "notes": [], "material": desc}
    loc = _nt._txt(p.get("loc_error")).upper()
    rec["loc_error"] = bool(loc) and loc != "NO ERROR"
    if rec["loc_error"]:
        rec["notes"].append("NDDOT LOC_ERROR: {}".format(loc))
    d_in = _nt._pos(p.get("diameter_e"))
    a_in = _nt._pos(p.get("arch_e"))
    n, _at = _nt._count_prefix(desc)
    rec["barrels"] = n or 1
    cattle = "CATTLE" in desc
    if cattle:
        if not d_in:
            return None
        span = d_in if d_in < CLP_CATTLE_FT_BELOW else d_in / 12.0
        rise = (a_in if a_in < CLP_CATTLE_FT_BELOW else a_in / 12.0) if a_in else span
        rec.update(shape="box", span_ft=round(span, 2), rise_ft=round(rise, 2))
        rec["notes"].append("cattle pass: written as a box")
    else:
        if not d_in:
            return rec                                   # no size: not used for sizing
        shape = _nt._shape_words(desc) or "pipe"
        if "BOX" in desc:
            shape = "box"
        if shape == "arch" and a_in:
            span, rise = max(d_in, a_in) / 12.0, min(d_in, a_in) / 12.0
        elif shape == "box" and a_in:
            span, rise = d_in / 12.0, a_in / 12.0
        else:
            span = rise = d_in / 12.0
            shape = "pipe" if shape == "arch" else shape
        rec.update(shape=shape, span_ft=round(span, 3), rise_ft=round(rise, 3))
    ln = _nt._pos(p.get("length_e"))
    rec["barrel_len_ft"] = round(ln, 1) if ln else None
    le, re_ = _clp_elev(p.get("left_elev_e")), _clp_elev(p.get("right_elev_e"))
    if le is not None or re_ is not None:
        rec["clp_inverts_ft"] = (le, re_)
        rec["invert_datum"] = "plan"
    return rec


def _priority(r):
    src = r.get("source") or ""
    if src == "NDDOT":
        return 0
    if src == "NDDOT CLP":
        return 3 if r.get("loc_error") else 1
    return 2


def read_inventory_gpkg(path, make_transform=None, make_bbox=None):
    """Inventory records from every point layer of a GeoPackage (pure Python, sqlite3).
    make_transform(srs_id) -> transform(x, y) -> (x, y) maps a layer's CRS to the work CRS
    (None: coordinates as stored). make_bbox(srs_id) -> (xmin, ymin, xmax, ymax) in the
    layer's CRS keeps only the points inside it, filtered BEFORE parsing (the layer's R-tree
    when it has one) - the statewide CLP layer has 22,555 points."""
    import sqlite3
    from ..damsuite import suitegpkg as G
    out = []
    con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    try:
        layers = con.execute("SELECT table_name, column_name, srs_id "
                             "FROM gpkg_geometry_columns").fetchall()
        for table, gcol, srs_id in layers:
            transform = make_transform(srs_id) if make_transform is not None else None
            bb = make_bbox(srs_id) if make_bbox is not None else None
            qt = '"{}"'.format(table.replace('"', '""'))
            rt = "rtree_{}_{}".format(table, gcol)
            has_rt = bb is not None and con.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (rt,)).fetchone() is not None
            if has_rt:
                pk = [r[1] for r in con.execute("PRAGMA table_info({})".format(qt)) if r[5]]
                cur = con.execute(
                    'SELECT t.* FROM {} t JOIN "{}" r ON t."{}" = r.id WHERE r.maxx >= ? AND '
                    'r.minx <= ? AND r.maxy >= ? AND r.miny <= ?'.format(
                        qt, rt.replace('"', '""'), (pk[0] if pk else "fid")),
                    (bb[0], bb[2], bb[1], bb[3]))
            else:
                cur = con.execute("SELECT * FROM {}".format(qt))
            names = [d[0] for d in cur.description]
            for row in cur:
                props = dict(zip(names, row))
                geom, _srs = G.parse_blob(props.pop(gcol))
                pts = G.points_of(geom) if geom else []
                if not pts:
                    continue
                x, y = pts[0][0], pts[0][1]
                if bb is not None and not (bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3]):
                    continue
                if transform is not None:
                    x, y = transform(x, y)
                rec = inventory_record(props, (x, y))
                if rec is not None:
                    out.append(rec)
    finally:
        con.close()
    return out


def _mid(line):
    pts = list(line)
    if not pts:
        return None
    if len(pts) == 1:
        return (float(pts[0][0]), float(pts[0][1]))
    a, b = pts[0], pts[-1]
    return ((float(a[0]) + float(b[0])) / 2.0, (float(a[1]) + float(b[1])) / 2.0)


def _dist_to_line(x, y, line):
    pts = [(float(p[0]), float(p[1])) for p in line]
    if len(pts) == 1:
        return math.hypot(x - pts[0][0], y - pts[0][1])
    best = float("inf")
    for (ax, ay), (bx, by) in zip(pts[:-1], pts[1:]):
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
        best = min(best, math.hypot(x - (ax + t * dx), y - (ay + t * dy)))
    return best


def match_inventory(guesses, inventory, match_m=MATCH_M):
    """Give each guess the size of the nearest inventory culvert within match_m metres.
    Returns (rows, notes). A matched row: span -> width_m, rise -> height_m (a pipe's rise
    is its span), barrels, shape (arch -> box), source "NDDOT <id>" / "NBI <id>",
    surveyed True, match_m, name "<source> (matched guess: ...)". An inventory culvert sizes
    at most one guess (the nearest). Rank (NDDOT bridge inventory, CLP, NBI, CLP with a
    LOC_ERROR) wins over distance within
    match_m. A record without a span is not used. Unmatched guesses are returned unchanged."""
    rows = [dict(g) for g in guesses or []]
    notes = []
    match_m = float(match_m)
    cands = []
    for gi, g in enumerate(rows):
        if g.get("match_m") is not None:
            continue                                   # already sized (matched upstream)
        c = _mid(g.get("line") or [])
        if c is None:
            continue
        for ri, r in enumerate(inventory or []):
            if r.get("kind") != "culvert":
                continue
            d = math.hypot(r["x"] - c[0], r["y"] - c[1])
            if d <= match_m:
                cands.append((_priority(r), d, gi, ri))
    cands.sort()
    used_g = set()
    used_r = {ri for ri, r in enumerate(inventory or []) for g in rows
              if g.get("match_m") is not None and g.get("source") == "{} {}".format(
                  r.get("source"), r.get("id"))}
    for _rank, d, gi, ri in cands:
        if gi in used_g or ri in used_r:
            continue
        r = inventory[ri]
        if not r.get("span_ft"):
            notes.append("{} {} is {:.0f} m from '{}' but its size was not read: not "
                         "used".format(r.get("source"), r.get("id"), d, rows[gi].get("name")))
            used_r.add(ri)
            continue
        g = rows[gi]
        shape = _runner_shape(r.get("shape"))
        span_m = float(r["span_ft"]) * FT
        rise_ft = r.get("rise_ft")
        if shape == "pipe":
            rise_m = span_m
        elif rise_ft:
            rise_m = float(rise_ft) * FT
        else:
            rise_m = g.get("height_m")
            notes.append("{} {}: no rise in the inventory; the guess rise {} m is kept".format(
                r.get("source"), r.get("id"), rise_m))
        src = "{} {}".format(r.get("source"), r.get("id"))
        g.update(width_m=round(span_m, 3), height_m=round(float(rise_m), 3),
                 barrels=int(r.get("barrels") or 1), shape=shape, source=src,
                 surveyed=True, match_m=round(d, 1),
                 name="{} (matched guess: {})".format(src, str(g.get("name") or "")
                                                     .replace("guess: ", "")),
                 note="size from {} {:.0f} m away{}".format(
                     src, d, "; inventory shape {}".format(r["shape"])
                     if r.get("shape") and r["shape"] != shape else ""))
        if r.get("clp_inverts_ft"):
            g["plan_inverts_ft"] = r["clp_inverts_ft"]
            g["note"] += "; NDDOT plan-datum end elevations {} / {} ft (left / right; not used: " \
                "the burn uses the LiDAR)".format(*r["clp_inverts_ft"])
        if r.get("barrel_len_ft"):
            g["barrel_len_m"] = round(float(r["barrel_len_ft"]) * FT, 2)
        used_g.add(gi)
        used_r.add(ri)
    return rows, notes


def drop_near_structures(guesses, structures, drop_m=MATCH_M):
    """Guesses farther than drop_m from every structure line (STRUCTURES layer).
    structures: Structure objects or dicts with a "line". Returns (kept, dropped)."""
    lines = []
    for s in structures or []:
        ln = getattr(s, "line", None)
        if ln is None and isinstance(s, dict):
            ln = s.get("line")
        if ln is not None and len(ln):
            lines.append(ln)
    kept, dropped = [], []
    for g in guesses or []:
        c = _mid(g.get("line") or [])
        if c is not None and any(_dist_to_line(c[0], c[1], ln) <= drop_m for ln in lines):
            dropped.append(g)
        else:
            kept.append(g)
    return kept, dropped


BURN_MARGIN_M = 10.0           # added on each side of the barrel for the side slopes
BURN_MIN_CELLS = 2             # the cut spans at least this many model cells


def burn_line(g, margin_m=BURN_MARGIN_M):
    """The cut line of a matched guess, upstream end first: along the channel through the
    crossing point (the guess's own line is across the road chord and only 12 m long),
    barrel length + 2 x margin long (the guess length when the inventory has no barrel
    length)."""
    line = [(float(p[0]), float(p[1])) for p in g["line"]]
    xy = g.get("xy") or _mid(line)
    d = g.get("chan_dir")
    if d is None:
        (ax, ay), (bx, by) = line[0], line[-1]
        d = _unit((ax, ay), (bx, by)) or (1.0, 0.0)
    L0 = math.hypot(line[-1][0] - line[0][0], line[-1][1] - line[0][1])
    L = float(g["barrel_len_m"]) + 2.0 * margin_m if g.get("barrel_len_m") else L0
    L = max(L, L0)
    h = 0.5 * L
    return [(xy[0] - d[0] * h, xy[1] - d[1] * h), (xy[0] + d[0] * h, xy[1] + d[1] * h)]


def matched_structures(rows, min_cells=BURN_MIN_CELLS):
    """structures.Structure objects for the matched (surveyed) rows, for cutting into the
    DEM. Unmatched guesses are never returned. The line follows the channel through the
    crossing; at each model's cell size the cut is lengthened to min_cells cells
    (Structure.cut_line) so its ends reach the ground beside the embankment - a short line
    inside one coarse cell sets both inverts to that cell and cuts nothing."""
    import numpy as np
    from . import structures as structmod
    out = []
    for g in rows or []:
        if not g.get("surveyed") or g.get("match_m") is None:
            continue
        s = structmod.Structure(
            "culvert", np.asarray(burn_line(g), dtype=float), float(g["width_m"]),
            float(g["height_m"]), int(g.get("barrels") or 1), g.get("shape") or "box",
            name=g.get("name") or "")
        s.source = g.get("source") or "inventory"
        s.surveyed = True
        s.min_cells = int(min_cells)
        out.append(s)
    return out


def lidar_inverts(structures, read_dem, fine_cell=1.0, radius_m=None):
    """Set the inverts of matched culverts that have none to the lowest LiDAR ground within
    radius_m (default: the barrel span x barrels, at least 5 m) of each end, read at
    fine_cell - BEFORE the model resamples the DEM. Without this a coarse model cell's
    default invert is the averaged embankment cell itself and the cut does nothing.
    read_dem(bounds, cell) -> Grid. Returns notes; the structures are updated in place."""
    import numpy as np
    notes = []
    for s in structures or []:
        if s.invert_up_m is not None or s.invert_dn_m is not None:
            continue
        line = np.asarray(s.line, dtype=float)
        if len(line) < 2:
            continue
        r = radius_m or max(5.0, float(s.width_m) * max(1, int(s.barrels)))
        inv = []
        for x, y in (line[0], line[-1]):
            g = read_dem((x - r - fine_cell, y - r - fine_cell, x + r + fine_cell,
                          y + r + fine_cell), fine_cell)
            z = np.asarray(g.data, dtype=float)
            ny, nx = z.shape
            xs = g.x0 + (np.arange(nx) + 0.5) * g.dx
            ys = g.y0 - (np.arange(ny) + 0.5) * g.dx
            X, Y = np.meshgrid(xs, ys)
            ok = np.isfinite(z) & (np.hypot(X - x, Y - y) <= r)
            nd = getattr(g, "nodata", None)
            if nd is not None:
                ok &= z != nd
            inv.append(float(z[ok].min()) if ok.any() else None)
        if None in inv:
            notes.append("{}: no LiDAR at an end; inverts left to the model grid".format(s.name))
            continue
        s.invert_up_m, s.invert_dn_m = inv[0], inv[1]
        s.invert_note = "inverts = lowest LiDAR ground within {:.0f} m of each end".format(r)
        notes.append("{}: inverts {:.2f} / {:.2f} m from the LiDAR at {:g} m".format(
            s.name, inv[0], inv[1], fine_cell))
    return notes
