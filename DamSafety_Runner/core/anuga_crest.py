"""Road / rail crests held at their 1 m elevation in the ANUGA gmsh mesh (3.10.91, DWR-2801
review Q3).

Before 3.10.91 only the road / rail pieces on a raised fill (anuga_model.find_embankments)
became gmsh mesh lines, and their mesh vertices took the bed elevation sampled at the vertex
(bilinear on the fine terrain). A road that was not picked as an embankment, or a mesh line
moved a few metres off the crown by gmsh_mesher's thinning and snapping, left triangles
straddling the crest with one vertex near the crown and two on the side slopes. ANUGA's
centroid (the mean of the three vertices) then sat metres below the road: on DWR-2801 the
effective Highway 18 crest east of the pond was 938.80 ft where the 1 m road crest is about
941.5 ft, and water crossed a road it should not have crossed.

With the crest hold on (the default for gmsh meshes):

* crest lines = every road / rail centreline inside the mesh within crest_pad_m (152.4 m,
  500 ft) of the LISFLOOD-FP wet area. They become embedded gmsh lines (instead of the
  embankment pieces, which they include), so no triangle crosses them: the crest is a row of
  mesh edges;
* crest profile = along each crest line every crest_cell_m (1 m), the highest ground within
  +-crest_half_width_m (3 m) across it, read from the terrain ANUGA's bed comes from (the fine
  DEM with the model's conditioning and structure cuts), else the finer terrain;
* every mesh node on a crest edge (gmsh_mesher.crest_edges) gets the highest crest-profile
  value along the line between the midpoints to its neighbouring crest nodes (the stretch it
  stands for). The worker raises those vertices to it (never lowers them;
  anuga_worker.crest_vertex_values), so the crest edges carry the crown;
* not held: nodes in a bridge or notch cut corridor (road_profile.cut_corridors: the water
  goes through the opening) and nodes near the inlet / breach line (KEEP_OUT_INLET_M).
  Culverts are not masked: the road over a buried culvert is the crest and the culvert
  carries the flow under it.

Off: anuga_gmsh {"crest": "off"} or NDBREACH_ANUGA_CREST=off (the mesh lines and bed are then
as in 3.10.90). The line band (NDBREACH_ANUGA_BAND, off by default) is separate. The default
ANUGA mesher does not use this module.

Pure numpy (the caller gives the terrain reader)."""
import math

import numpy as np

from . import conditioning as cond

CREST_DEFAULTS = {"crest": "on", "crest_pad_m": 152.4, "crest_half_width_m": 3.0,
                  "crest_cell_m": 1.0}
CREST_ENV = (("NDBREACH_ANUGA_CREST", "crest"),
             ("NDBREACH_ANUGA_CREST_PAD_M", "crest_pad_m"),
             ("NDBREACH_ANUGA_CREST_HALF_WIDTH_M", "crest_half_width_m"),
             ("NDBREACH_ANUGA_CREST_CELL_M", "crest_cell_m"))
CREST_ON = ("on", "yes", "true", "1")
CREST_OFF = ("off", "no", "false", "0")
LINE_STEP_M = 5.0           # crest lines are densified this much for the wet test
MIN_PIECE_M = 20.0          # shorter crest pieces are dropped
CHUNK_M = 1000.0            # the crest profile is read in pieces of about this length
KEEP_OUT_INLET_M = 50.0     # no crest hold this close to the inlet / breach line


class CrestError(ValueError):
    """A bad crest setting."""


def crest_options(gmsh=None):
    """CREST_DEFAULTS merged with the anuga_gmsh dict: {'on', 'crest', 'crest_pad_m',
    'crest_half_width_m', 'crest_cell_m'}. CrestError for a bad value."""
    o = dict(CREST_DEFAULTS)
    o.update({k: v for k, v in (gmsh or {}).items() if k in CREST_DEFAULTS and v is not None})
    c = o["crest"]
    if isinstance(c, bool):
        o["on"] = c
    else:
        v = str(c).strip().lower()
        if v not in CREST_ON + CREST_OFF:
            raise CrestError("crest must be 'on' or 'off', not {!r} (setting anuga_gmsh"
                             "['crest'] or NDBREACH_ANUGA_CREST)".format(c))
        o["on"] = v in CREST_ON
    for k in ("crest_pad_m", "crest_half_width_m", "crest_cell_m"):
        try:
            x = float(o[k])
        except (TypeError, ValueError):
            x = float("nan")
        bad = not math.isfinite(x) or x < 0 or (x == 0 and k != "crest_pad_m")
        if bad:
            var = next(v for v, kk in CREST_ENV if kk == k)
            raise CrestError("{} must be a positive number, not {!r} (setting anuga_gmsh"
                             "['{}'] or {})".format(k, o[k], k, var))
        o[k] = x
    return o


def _in_mask(pts, grid, mask):
    c = np.floor((pts[:, 0] - grid.x0) / grid.dx).astype(int)
    r = np.floor((grid.y0 - pts[:, 1]) / grid.dx).astype(int)
    ok = (r >= 0) & (r < mask.shape[0]) & (c >= 0) & (c < mask.shape[1])
    out = np.zeros(len(pts), bool)
    out[ok] = mask[r[ok], c[ok]]
    return out


def _runs(flag):
    i = 0
    while i < len(flag):
        if not flag[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(flag) and flag[j + 1]:
            j += 1
        yield i, j
        i = j + 1


def crest_lines(lines, outer, wet=None, step_m=LINE_STEP_M, min_len_m=MIN_PIECE_M):
    """Pieces of the road / rail centrelines inside `outer` and, with wet = (Grid, bool
    array) given, inside that mask (the wet area already padded). Returns (pieces, info)."""
    out, km = [], 0.0
    outer = np.asarray(outer, float)
    for ln in lines or []:
        ln = np.asarray(ln, float).reshape(-1, 2)
        if len(ln) < 2:
            continue
        p = cond.densify(ln, step_m)
        ins = cond.points_in_polygon(p[:, 0], p[:, 1], outer)
        if wet is not None:
            ins &= _in_mask(p, wet[0], wet[1])
        for i, j in _runs(ins):
            piece = p[i:j + 1]
            if len(piece) < 2:
                continue
            L = float(np.hypot(*np.diff(piece, axis=0).T).sum())
            if L >= min_len_m:
                out.append(piece)
                km += L / 1000.0
    return out, {"crest_lines": len(out), "crest_km": round(km, 2)}


def _sampler(grid):
    zz = grid.masked()

    def sample(x, y):
        fc = (x - grid.x0) / grid.dx - 0.5
        fr = (grid.y0 - y) / grid.dx - 0.5
        c0, r0 = np.floor(fc).astype(int), np.floor(fr).astype(int)
        tc, tr = fc - c0, fr - r0
        c0c, c1c = np.clip(c0, 0, grid.nx - 1), np.clip(c0 + 1, 0, grid.nx - 1)
        r0c, r1c = np.clip(r0, 0, grid.ny - 1), np.clip(r0 + 1, 0, grid.ny - 1)
        v = (zz[r0c, c0c] * (1 - tc) * (1 - tr) + zz[r0c, c1c] * tc * (1 - tr)
             + zz[r1c, c0c] * (1 - tc) * tr + zz[r1c, c1c] * tc * tr)
        off = (fc < -0.5) | (fr < -0.5) | (fc > grid.nx - 0.5) | (fr > grid.ny - 0.5)
        return np.where(off, np.nan, v)
    return sample


def crest_profile(piece, read, cell_m=1.0, half_width_m=3.0, chunk_m=CHUNK_M):
    """(s, z): along `piece` every cell_m, the highest ground within +-half_width_m across
    it (NaN where the terrain has none). read(bounds (x0, y0, x1, y1), cell) -> Grid."""
    piece = np.asarray(piece, float)
    p = cond.densify(piece, cell_m)
    seg = np.hypot(*np.diff(p, axis=0).T)
    s = np.concatenate([[0.0], np.cumsum(seg)])
    t = np.gradient(p, axis=0)
    nrm = np.hypot(t[:, 0], t[:, 1])
    nrm[nrm == 0] = 1.0
    nx, ny = -t[:, 1] / nrm, t[:, 0] / nrm
    offs = np.arange(-half_width_m, half_width_m + 0.5 * cell_m, cell_m)
    if not np.isclose(offs[-1], half_width_m):
        offs = np.append(offs, half_width_m)
    z = np.full(len(p), np.nan)
    n_chunk = max(1, int(round(chunk_m / max(cell_m, 1e-6))))
    pad = half_width_m + 3.0 * cell_m
    for a in range(0, len(p), n_chunk):
        b = min(len(p), a + n_chunk + 1)
        q = p[a:b]
        lo, hi = q.min(axis=0) - pad, q.max(axis=0) + pad
        g = read((float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])), float(cell_m))
        if g is None:
            continue
        smp = _sampler(g)
        with np.errstate(invalid="ignore"):
            v = np.nanmax(np.vstack([smp(q[:, 0] + nx[a:b] * d, q[:, 1] + ny[a:b] * d)
                                     for d in offs]), axis=0)
        z[a:b] = np.where(np.isfinite(v), v, z[a:b])
    return s, z


def hold_crest(points, crest_nodes, pieces, profiles, tol_m, keep_out=None):
    """The crest elevation of each mesh node on a crest edge.

    points (N, 2) mesh nodes; crest_nodes: indices of the nodes on crest edges
    (gmsh_mesher's crest_nodes); pieces: the crest lines; profiles: [(s, z)] per piece
    (crest_profile); tol_m: a node further than this from every crest line is not held;
    keep_out: bool per crest node, True = not held (a cut corridor, near the inlet).
    Each held node gets the highest profile value between the midpoints to its neighbouring
    held nodes on the same line (half its own spacing past an end node).
    Returns (node indices, z, info)."""
    pts = np.asarray(points, float)
    cn = np.asarray(crest_nodes, np.int64).ravel()
    info = {"crest_nodes": int(len(cn)), "held": 0, "kept_out": 0, "off_line": 0,
            "no_profile": 0}
    if not len(cn) or not pieces:
        return np.zeros(0, np.int64), np.zeros(0), info
    q = pts[cn]
    best_d = np.full(len(cn), np.inf)
    best_k = np.full(len(cn), -1)
    best_s = np.zeros(len(cn))
    for k, piece in enumerate(pieces):
        piece = np.asarray(piece, float)
        lo, hi = piece.min(axis=0) - tol_m, piece.max(axis=0) + tol_m
        sel = np.flatnonzero((q[:, 0] >= lo[0]) & (q[:, 0] <= hi[0]) &
                             (q[:, 1] >= lo[1]) & (q[:, 1] <= hi[1]))
        if not len(sel):
            continue
        d, s = cond.polyline_distance(q[sel, 0], q[sel, 1], piece)
        better = d < best_d[sel]
        best_d[sel[better]] = d[better]
        best_k[sel[better]] = k
        best_s[sel[better]] = s[better]
    on = best_d <= tol_m
    info["off_line"] = int((~on).sum())
    if keep_out is not None:
        ko = np.asarray(keep_out, bool) & on
        info["kept_out"] = int(ko.sum())
        on &= ~ko
    out_i, out_z = [], []
    for k in np.unique(best_k[on]):
        js = np.flatnonzero(on & (best_k == k))
        js = js[np.argsort(best_s[js])]
        ss = best_s[js]
        ps, pz = profiles[k]
        ps, pz = np.asarray(ps, float), np.asarray(pz, float)
        for m, j in enumerate(js):
            left = ss[m - 1] if m > 0 else None
            right = ss[m + 1] if m + 1 < len(js) else None
            half_l = 0.5 * (ss[m] - left) if left is not None else \
                (0.5 * (right - ss[m]) if right is not None else 0.0)
            half_r = 0.5 * (right - ss[m]) if right is not None else half_l
            a, b = ss[m] - half_l, ss[m] + half_r
            w = (ps >= a - 1e-9) & (ps <= b + 1e-9)
            vals = pz[w]
            if not np.isfinite(vals).any():
                near = int(np.argmin(np.abs(ps - ss[m]))) if len(ps) else -1
                vals = pz[near:near + 1] if near >= 0 else vals
            if not len(vals) or not np.isfinite(vals).any():
                info["no_profile"] += 1
                continue
            out_i.append(int(cn[j]))
            out_z.append(float(np.nanmax(vals)))
    info["held"] = len(out_i)
    return np.asarray(out_i, np.int64), np.asarray(out_z, float), info


def keep_out_mask(xy, cuts=(), inlet=None, inlet_m=KEEP_OUT_INLET_M):
    """True per point in a bridge / notch cut corridor (road_profile.cut_corridors) or
    within inlet_m of the inlet / breach line."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    out = np.zeros(len(xy), bool)
    if not len(xy):
        return out
    for line, half, _name in cuts or ():
        line = np.asarray(line, float).reshape(-1, 2)
        if len(line) == 1:
            d = np.hypot(xy[:, 0] - line[0, 0], xy[:, 1] - line[0, 1])
        else:
            d, _s = cond.polyline_distance(xy[:, 0], xy[:, 1], line)
        out |= d <= half
    if inlet is not None:
        il = np.asarray(inlet, float).reshape(-1, 2)
        if len(il) >= 2:
            d, _s = cond.polyline_distance(xy[:, 0], xy[:, 1], il)
        else:
            d = np.hypot(xy[:, 0] - il[0, 0], xy[:, 1] - il[0, 1])
        out |= d <= inlet_m
    return out
