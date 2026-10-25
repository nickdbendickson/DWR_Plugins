"""3.10.76: bands of small cells along roads, rail, structures and other breaklines in the
HEC-RAS computation points (Nick: "Roads, rivers, bridges, culverts and breaklines should
have 3 rows of 20ft grid in hec ras alongside both sides").

Each banded line gets, on both sides, `rows` rows of points `s` apart (default 3 rows of
20 ft: points at 10, 30 and 50 ft from the line, every 20 ft along it), so HEC-RAS's cells
there are about s x s and the faces between the first rows lie on the line (as HEC-RAS's own
breakline enforcement with a near spacing of s and `rows` near repeats). Outside the band
the rows grow by `grading` (default 1.5, at most 2: neighbouring cells never more than 2:1)
until they reach the base cell (20 ft: 30, 45, 67.5, 101 ft, then the 150 ft grid, 304 ft
each side in all; 2 rows of 50 ft: 75, 112.5 ft, 288 ft each side). Line ends get
half-circle caps with the same rows.

Where bands meet (crossings, junctions, lines closer than the band width), a point is kept
only where no other banded line (of the same or a higher rank) is nearer than its own row
offset (less a quarter row), so the bands merge; a lower-ranked line's points also go inside
the band rows of a higher-ranked line it crosses (structures, then roads, rail, breaklines,
the channel), so the winner's faces stay on it. Then no two new points are closer than
MIN_FRAC x their spacing (the inner rows win). Old points within the graded zone go. Lines
are clipped away from the dam line and the breach cut (those keep their own rows,
hecras_breaklines) and, by default, to the run's wet area plus a pad (extent 'wet'; 'all' =
every line in the 2D area).

Settings (options of hecras_model.build_model, else the environment variables):
line_bands / NDBREACH_BAND ('on' | 'off'), band_cell_ft / NDBREACH_BAND_CELL_FT (20),
band_rows / NDBREACH_BAND_ROWS (3; 0 = off), band_grading / NDBREACH_BAND_GRADING (1.5),
band_classes / NDBREACH_BAND_CLASSES ('road,rail,structure,breakline'; also 'channel', the
run's flow path), band_extent / NDBREACH_BAND_EXTENT ('wet' | 'all'), band_wet_pad_ft /
NDBREACH_BAND_WET_PAD_FT (500), band_lines / NDBREACH_BAND_LINES (vector file(s) of more
breaklines, separated by ';;', class 'breakline'). Off reproduces the points of 3.10.75.
The 2 rows x 50 ft option Nick compared: NDBREACH_BAND_CELL_FT=50, NDBREACH_BAND_ROWS=2.
"""
import math
import os

import numpy as np

from . import hecras_breaklines as hb

CLASSES = ("structure", "road", "rail", "breakline", "channel")   # earlier classes win
DEFAULTS = {"line_bands": "on", "band_cell_ft": 20.0, "band_rows": 3, "band_grading": 1.5,
            "band_classes": "road,rail,structure,breakline", "band_extent": "wet",
            "band_wet_pad_ft": 500.0, "band_lines": None}
ENV = (("line_bands", "NDBREACH_BAND"), ("band_cell_ft", "NDBREACH_BAND_CELL_FT"),
       ("band_rows", "NDBREACH_BAND_ROWS"), ("band_grading", "NDBREACH_BAND_GRADING"),
       ("band_classes", "NDBREACH_BAND_CLASSES"), ("band_extent", "NDBREACH_BAND_EXTENT"),
       ("band_wet_pad_ft", "NDBREACH_BAND_WET_PAD_FT"), ("band_lines", "NDBREACH_BAND_LINES"))
MIN_FRAC = 0.5          # no two new points closer than this x their spacing
OWN_TOL = 0.25          # a row point may be this x its spacing nearer another line than its row
FACE_FRAC = 0.475       # ... but never nearer any banded line than this x s (the faces on it)
TREE_STEP_FRAC = 0.125  # lines sampled every this x s for the distances
CROSS_DEG = 20.0        # a lower-ranked line's point goes inside a higher-ranked band it crosses
CLEAR_EXTRA_FRAC = 0.25  # old points within the graded zone + this x the cell go
MIN_PIECE_FRAC = 2.0    # line pieces shorter than this x s are dropped
GRADE_STOP_FRAC = 0.9   # graded rows stop once the next would be this x the cell or more
_OFF = ("off", "no", "false", "0", "none", "")


class BandError(ValueError):
    """A bad band setting."""


def settings(o=None, environ=None):
    """The band settings: options win over the environment, then DEFAULTS. Returns a dict
    with on (bool), cell_ft, rows, grading, classes (tuple), extent, wet_pad_ft, lines."""
    env = os.environ if environ is None else environ
    o = o or {}
    raw = {}
    for key, var in ENV:
        v = o.get(key)
        if v is None:
            e = env.get(var)
            v = e if (e is not None and str(e).strip()) else None
        raw[key] = DEFAULTS[key] if v is None else v
    on = raw["line_bands"]
    on = bool(on) if isinstance(on, bool) else str(on).strip().lower() not in _OFF
    try:
        cell = float(raw["band_cell_ft"])
        rows = int(float(raw["band_rows"]))
        grading = float(raw["band_grading"])
        pad = float(raw["band_wet_pad_ft"])
    except (TypeError, ValueError) as e:
        raise BandError("line bands: not a number ({})".format(e))
    if not (cell > 0 and math.isfinite(cell)):
        raise BandError("line bands: band_cell_ft must be > 0, not {}".format(cell))
    if rows < 0:
        raise BandError("line bands: band_rows must be 0 or more, not {}".format(rows))
    if not 1.0 < grading <= 2.0:
        raise BandError("line bands: band_grading must be above 1 and at most 2 (2:1), not "
                        "{}".format(grading))
    if not (pad >= 0 and math.isfinite(pad)):
        raise BandError("line bands: band_wet_pad_ft must be >= 0")
    cl = raw["band_classes"]
    cl = [str(c).strip().lower() for c in (cl.split(",") if isinstance(cl, str) else cl)
          if str(c).strip()]
    bad = [c for c in cl if c not in CLASSES]
    if bad:
        raise BandError("line bands: unknown class(es) {} (use {})".format(
            ", ".join(bad), ", ".join(CLASSES)))
    ext = str(raw["band_extent"]).strip().lower()
    if ext not in ("wet", "all"):
        raise BandError("line bands: band_extent must be 'wet' or 'all', not {!r}".format(ext))
    lines = raw["band_lines"]
    if isinstance(lines, str):
        lines = [p.strip() for p in lines.split(";;") if p.strip()]
    return {"on": bool(on and rows > 0 and cl), "cell_ft": cell, "rows": rows,
            "grading": grading, "classes": tuple(c for c in CLASSES if c in cl),
            "extent": ext, "wet_pad_ft": pad, "lines": list(lines or [])}


def row_offsets(s, rows, grading, cell):
    """[(offset of the row's points from the line, spacing h, in the band)...] and the
    band's edge (each side): `rows` band rows at (k + 0.5) s (k = 0..rows-1), then graded
    rows, each `grading` x the last, while below GRADE_STOP_FRAC x cell."""
    out = [((k + 0.5) * s, float(s), True) for k in range(int(rows))]
    edge, h = rows * s, float(s)
    while True:
        h *= grading
        if h >= GRADE_STOP_FRAC * cell:
            break
        out.append((edge + 0.5 * h, h, False))
        edge += h
    return out, edge


def band_edge(s, rows, grading, cell):
    return row_offsets(s, rows, grading, cell)[1]


# ------------------------------------------------------------------ the lines -----------
def pieces(line, ok, step, min_len):
    """The runs of a line (resampled every `step`) where ok(xy) holds, each at least min_len
    long."""
    c = hb.resample(np.asarray(line, float).reshape(-1, 2), step)
    if len(c) < 2:
        return []
    good = np.asarray(ok(c), bool)
    out, i, n = [], 0, len(c)
    while i < n:
        if not good[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and good[j + 1]:
            j += 1
        p = c[i:j + 1]
        if len(p) >= 2 and hb.length(p) >= min_len:
            out.append(p)
        i = j + 1
    return out


def mask_test(mask, x0, ytop, cell):
    """xy -> bool: inside a north-up raster mask (top-left x0, ytop; square cells)."""
    m = np.asarray(mask, bool)

    def test(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        c = np.floor((xy[:, 0] - x0) / cell).astype(np.int64)
        r = np.floor((ytop - xy[:, 1]) / cell).astype(np.int64)
        ok = (r >= 0) & (r < m.shape[0]) & (c >= 0) & (c < m.shape[1])
        out = np.zeros(len(xy), bool)
        out[ok] = m[r[ok], c[ok]]
        return out
    return test


def dilate(mask, cells):
    """The mask grown by `cells` cells (Euclidean)."""
    m = np.asarray(mask, bool)
    if cells <= 0 or not m.any():
        return m.copy()
    from scipy.ndimage import distance_transform_edt
    return distance_transform_edt(~m) <= float(cells)


def far_from(lines, dist):
    """xy -> bool: at least `dist` from every line (True everywhere without lines)."""
    ls = [np.asarray(l, float).reshape(-1, 2) for l in (lines or []) if len(l)]
    if not ls or dist <= 0:
        return lambda xy: np.ones(len(np.asarray(xy).reshape(-1, 2)), bool)
    from scipy.spatial import cKDTree
    step = max(0.1 * dist, 1e-6)
    tree = cKDTree(np.vstack([hb.resample(l, step) if len(l) > 1 else l for l in ls]))

    def test(xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        if not len(xy):
            return np.zeros(0, bool)
        return tree.query(xy)[0] >= dist - 0.5 * step
    return test


def clip(lines, perimeter, s, tests=()):
    """The pieces of the lines inside the 2D area (at least s from its edge) where every
    test(xy) holds."""
    ring = np.asarray(perimeter, float).reshape(-1, 2)

    def ok(xy):
        g = hb.inside(xy[:, 0], xy[:, 1], ring)
        if g.any():
            g[g] = hb.ring_dist(xy[g, 0], xy[g, 1], ring) >= s
        for t in tests:
            if g.any():
                g[g] = t(xy[g])
        return g
    out = []
    for ln in lines:
        ln = np.asarray(ln, float).reshape(-1, 2)
        if len(ln) >= 2:
            out += pieces(ln, ok, 0.5 * s, MIN_PIECE_FRAC * s)
    return out


# ------------------------------------------------------------------ the points ----------
def _rows_of(line, d, h):
    """Points d either side of a line every h along it (on its segments' normals, at the
    segment midpoints), and half-circle caps of radius d at both ends. Returns (points, unit
    tangents of the line there)."""
    c = hb.resample(line, h)
    if len(c) < 2:
        return np.zeros((0, 2)), np.zeros((0, 2))
    mid = 0.5 * (c[1:] + c[:-1])
    t = c[1:] - c[:-1]
    t /= np.maximum(np.hypot(*t.T), 1e-12)[:, None]
    n = np.column_stack([-t[:, 1], t[:, 0]])
    out = [mid + d * n, mid - d * n]
    tan = [t, t]
    for E, u in ((c[-1], t[-1]), (c[0], -t[0])):
        nu = np.array([-u[1], u[0]])
        k = max(1, int(math.ceil(math.pi * d / h)))
        th = -0.5 * math.pi + (np.arange(k) + 0.5) * math.pi / k
        out.append(E + d * (np.cos(th)[:, None] * u + np.sin(th)[:, None] * nu))
        tan.append(np.tile(u, (k, 1)))
    return np.vstack(out), np.vstack(tan)


def _line_tree(lines, step):
    """(KD-tree of the lines sampled every `step`, the samples, their unit tangents)."""
    from scipy.spatial import cKDTree
    dense, tan = [], []
    for l in lines:
        c = hb.resample(l, step)
        if len(c) < 2:
            continue
        t = np.diff(c, axis=0)
        t /= np.maximum(np.hypot(*t.T), 1e-12)[:, None]
        dense.append(c)
        tan.append(np.vstack([t, t[-1:]]))
    dense = np.vstack(dense) if dense else np.zeros((0, 2))
    tan = np.vstack(tan) if tan else np.zeros((0, 2))
    return cKDTree(dense), dense, tan


def band_points(lines, s, rows, grading, cell):
    """Candidate points of all the lines' bands: (xy, spacing, offset, in_band, line index,
    tangent)."""
    offs, _ = row_offsets(s, rows, grading, cell)
    xy, sp, off, band, li, tg = [], [], [], [], [], []
    for i, ln in enumerate(lines):
        for d, h, b in offs:
            p, t = _rows_of(ln, d, h)
            xy.append(p)
            tg.append(t)
            sp.append(np.full(len(p), h))
            off.append(np.full(len(p), d))
            band.append(np.full(len(p), b))
            li.append(np.full(len(p), i))
    if not xy:
        z = np.zeros(0)
        return (np.zeros((0, 2)), z, z, z.astype(bool), z.astype(int), np.zeros((0, 2)))
    return (np.vstack(xy), np.concatenate(sp), np.concatenate(off), np.concatenate(band),
            np.concatenate(li).astype(int), np.vstack(tg))


def _dedupe(xy, sp, order, frac, fixed=None):
    """Greedy in `order`: a point goes when a kept one (or one of `fixed`) is closer than
    frac x its spacing. Returns the kept indices (in `order`)."""
    from scipy.spatial import cKDTree
    n = len(xy)
    if not n:
        return np.zeros(0, int)
    alive = np.ones(n, bool)
    if fixed is not None and len(fixed):
        alive &= cKDTree(np.asarray(fixed, float)).query(xy)[0] >= frac * sp
    tree = cKDTree(xy)
    kept = np.zeros(n, bool)
    order = np.asarray(order, int)
    order = order[alive[order]]
    nb = tree.query_ball_point(xy[order], frac * sp[order])
    for i, hits in zip(order, nb):
        if not any(kept[j] for j in hits if j != i):
            kept[i] = True
    return order[kept[order]]


def apply(points, perimeter, lines, s, rows, grading, cell, protected=None, hard=None,
          ranks=None):
    """The computation points with bands along `lines` (pieces, already clipped). protected:
    the old points that are protected (bool); hard: those that must stay (the dam row and
    breach cells), the others within the band's reach go. ranks: per line, lower wins where
    lines cross (default all 0): a point of a line is dropped within the band rows of a
    lower-ranked line that it crosses (more than CROSS_DEG apart), so the winner's faces
    stay on it. Returns (points, protected, stats)."""
    pts = np.asarray(points, float).reshape(-1, 2)
    prot = np.zeros(len(pts), bool) if protected is None else np.asarray(protected, bool)
    hard = np.zeros(len(pts), bool) if hard is None else np.asarray(hard, bool)
    rk0 = [0] * len(lines) if ranks is None else list(ranks)
    keep_l = [i for i, l in enumerate(lines) if len(l) >= 2]
    lines = [np.asarray(lines[i], float).reshape(-1, 2) for i in keep_l]
    ranks = [rk0[i] for i in keep_l]
    stats = {"lines": len(lines), "length": round(sum(hb.length(l) for l in lines), 1),
             "spacing": s, "rows": rows, "grading": grading}
    if not lines or rows <= 0:
        stats.update(points_removed=0, points_added=0, points=int(len(pts)))
        return pts, prot, stats
    offs, edge = row_offsets(s, rows, grading, cell)
    stats["row_spacings"] = [round(h, 2) for _, h, _ in offs]
    stats["width_each_side"] = round(edge, 1)
    tree = _line_tree(lines, TREE_STEP_FRAC * s)[0]
    xy, sp, off, band, li, tg = band_points(lines, s, rows, grading, cell)
    # merged bands: no line (of the same or a higher rank: a lower-ranked line crossing does
    # not cut a band) nearer than the row's own offset (less a quarter spacing)
    rk = np.asarray(ranks, int)
    dl = np.zeros(len(xy))
    for r in sorted(set(rk.tolist())):
        sel = rk[li] == r
        if not sel.any():
            continue
        t_r = tree if r == rk.max() else _line_tree(
            [l for l, q in zip(lines, rk) if q <= r], TREE_STEP_FRAC * s)[0]
        dl[sel] = t_r.query(xy[sel])[0]
    own = (dl >= off - OWN_TOL * sp) & (dl >= FACE_FRAC * s)
    # crossings: a lower-ranked line's points go inside the band rows of a higher-ranked
    # line it crosses (colinear overlaps, e.g. a culvert on its road, are left to the dedupe)
    cos_x = math.cos(math.radians(CROSS_DEG))
    for r in sorted(set(rk.tolist()))[1:]:
        hi = [l for l, q in zip(lines, rk) if q < r]
        sel = np.nonzero(own & (rk[li] == r))[0]
        if not hi or not len(sel):
            continue
        t_hi, _, tan_hi = _line_tree(hi, TREE_STEP_FRAC * s)
        dh, jh = t_hi.query(xy[sel])
        cross = np.abs((tan_hi[jh] * tg[sel]).sum(1)) < cos_x
        own[sel[(dh < (rows - OWN_TOL) * s) & cross]] = False
    ring = np.asarray(perimeter, float)
    ok = own & hb.inside(xy[:, 0], xy[:, 1], ring)
    if ok.any():
        ok[ok] &= hb.ring_dist(xy[ok, 0], xy[ok, 1], ring) >= hb.PERIM_FRAC * sp[ok]
    idx = np.nonzero(ok)[0]
    xy, sp, off, band = xy[idx], sp[idx], off[idx], band[idx]
    # old points in the band's reach go (but the dam row and breach cells)
    reach = edge + CLEAR_EXTRA_FRAC * cell
    do = tree.query(pts)[0] if len(pts) else np.zeros(0)
    keep_old = ~((do < reach) & ~hard)
    order = np.lexsort((np.arange(len(xy)), off))
    kept = _dedupe(xy, sp, order, MIN_FRAC, fixed=pts[hard] if hard.any() else None)
    xy, sp, band = xy[kept], sp[kept], band[kept]
    # old points beyond the reach but too close to a new point (the outer row) go too
    rest_i = np.nonzero(keep_old)[0]
    if len(xy) and len(rest_i):
        from scipy.spatial import cKDTree
        d2, j2 = cKDTree(xy).query(pts[rest_i])
        close = (d2 < hb.MIN_PAIR_FRAC * sp[j2]) & ~hard[rest_i]
        keep_old[rest_i[close]] = False
    removed = int((~keep_old).sum())
    out = np.vstack([pts[keep_old], xy])
    oprot = np.r_[prot[keep_old], band]
    stats.update(points_removed=removed, points_added=int(len(xy)),
                 band_points=int(band.sum()), graded_points=int((~band).sum()),
                 points=int(len(out)))
    return out, oprot, stats


def g_lines(lines, cls, s, cell, rows, simplify_frac=0.1):
    """BreakLine blocks for the .g (name, near, far, repeats, line), the lines simplified."""
    out = []
    for i, ln in enumerate(lines):
        p = hb.simplify(np.asarray(ln, float), simplify_frac * s)
        out.append({"name": "Band {} {}".format(cls, i + 1), "near": float(s),
                    "far": float(cell), "repeats": int(rows),
                    "line": [tuple(map(float, q)) for q in p]})
    return out
