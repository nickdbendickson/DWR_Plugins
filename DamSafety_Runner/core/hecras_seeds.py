"""v3.10.22: gmsh point layout as the HEC-RAS 2D area's computation points (optional).

HEC-RAS builds its own cells from the computation points ("Storage Area 2D Points" in the
.g01): it keeps every point it is given (Muncie test, 2026-10-18: 10,824 gmsh nodes gave
10,824 cells, volume error 0.0056 %), but it throws away a mesh written straight into the
geometry HDF. So gmsh only places the points here: small spacing along the dam, through the
breach and along the channel (the run's flow path), the 2D area's cell size elsewhere. HEC-RAS
still makes the cells (Voronoi), the faces and the property tables.

Off by default (build_model option mesh_seeds="gmsh", or NDBREACH_HECRAS_MESH_SEEDS=gmsh).
gmsh is an optional, separately installed program (GPL-2+, `pip install gmsh`); it is never
bundled. It is used in this Python when `import gmsh` works, else in another interpreter that
has it (option gmsh_python, NDBREACH_GMSH_PYTHON, then NDBREACH_ANUGA_PYTHON), run as

    python hecras_seeds.py seeds_in.json seeds_out.npz

When none works, build_model falls back to its regular grid and says why.
Only numpy (and gmsh for the mesh) are needed; coordinates are model units (ft or m).
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

ENV_MODE = "NDBREACH_HECRAS_MESH_SEEDS"
ENV_PYTHON = "NDBREACH_GMSH_PYTHON"
ENV_ANUGA_PYTHON = "NDBREACH_ANUGA_PYTHON"
MODES = ("grid", "gmsh")
TIMEOUT_S = 600.0
MIN_SEED_CAP = 25000          # a small 2D area may refine to more than max_seeds_ratio x its grid
# spacing along the lines, as a fraction of the 2D area's cell size
DEFAULT_REFINE = {"dam_frac": 0.25, "breach_frac": 0.25, "channel_frac": 1.0,
                  "dam_dist_cells": 8.0, "channel_dist_cells": 4.0,
                  "max_seeds_ratio": 4.0, "edge_frac": 0.45,
                  # v3.10.22 (Absaraka v2_gmsh: 58 cells of 9 sides, HEC-RAS refused 48):
                  # gmsh's Frontal-Delaunay (6) instead of Delaunay (5) gave 0 nodes of 9 or
                  # more neighbours there (Delaunay: 58), at the same sizes; a fixed random
                  # seed and one thread give the same points every run; the size grows at
                  # most `grading` from a point to its neighbour; points closer than
                  # min_spacing_frac x the smallest spacing are thinned
                  "algorithm": 6, "random_seed": 20261018, "grading": 1.3,
                  "min_spacing_frac": 0.5, "max_points": 2000000}
# equal point density to a square grid of the same cell: a triangle node carries
# sqrt(3)/2 h^2 of area, a grid point cell^2
GRID_EQUIV = math.sqrt(2.0 / math.sqrt(3.0))


class SeedError(Exception):
    """gmsh could not make the points; build_model falls back to its grid."""


def mode_of(options=None, env=None):
    """'grid' or 'gmsh' from the option mesh_seeds, else NDBREACH_HECRAS_MESH_SEEDS."""
    env = os.environ if env is None else env
    v = (options or {}).get("mesh_seeds")
    if v in (None, ""):
        v = env.get(ENV_MODE, "") or "grid"
    v = str(v).strip().lower()
    if v in ("0", "off", "false", "no", "default", "regular"):
        v = "grid"
    if v in ("1", "on", "true", "yes"):
        v = "gmsh"
    if v not in MODES:
        raise ValueError("mesh_seeds must be 'grid' or 'gmsh' (got {!r})".format(v))
    return v


def _area(h):
    """Area of the equilateral triangle with edge h (gmsh_mesher.h_of_area inverted)."""
    return math.sqrt(3.0) / 4.0 * h * h


def _offset_line(A, B, d, off, trim):
    A, B, d = (np.asarray(v, float) for v in (A, B, d))
    L = float(np.hypot(*(B - A)))
    if L <= 2 * trim:
        return None
    u = (B - A) / L
    return [(A + u * trim + d * off).tolist(), (B - u * trim + d * off).tolist()]


def spec_for(perimeter, cell, dam_line=None, downstream=None, breach_xy=None, breach_len=None,
             channel=None, refine=None):
    """The gmsh_mesher.build_mesh spec (model units) for HEC-RAS seeds, and the sizes used.
    dam_line: (A, B) of the SA/2D connection; downstream: unit vector; breach_xy: the
    breach centre on the dam line; channel: the run's flow path [(x, y)...]."""
    r = dict(DEFAULT_REFINE)
    r.update({k: v for k, v in (refine or {}).items() if v is not None})
    h_base = GRID_EQUIV * float(cell)
    h_dam = max(1e-6, float(r["dam_frac"]) * cell)
    h_br = max(1e-6, float(r["breach_frac"]) * cell)
    h_line = min(h_dam, h_br)
    h_ch = max(h_line, float(r["channel_frac"]) * cell)
    lines = []
    if dam_line is not None and downstream is not None:
        # the dam is on the perimeter (gmsh_mesher drops lines that touch the outer ring),
        # so the dam line is refined one step inside the 2D area
        ln = _offset_line(dam_line[0], dam_line[1], downstream, 2.0 * h_line, 2.0 * h_line)
        if ln:
            lines.append(ln)
    if breach_xy is not None and downstream is not None:
        p = np.asarray(breach_xy, float)
        d = np.asarray(downstream, float)
        L = float(breach_len or 4.0 * cell)
        lines.append([(p + d * 2.0 * h_line).tolist(), (p + d * (2.0 * h_line + L)).tolist()])
    chan = []
    if channel is not None and len(channel) >= 2:
        chan.append(np.asarray(channel, float).reshape(-1, 2).tolist())
    # smooth grading: from h_min the size grows by at most (grading - 1) per unit of
    # distance, i.e. neighbouring points differ in spacing by at most `grading`
    g1 = max(1e-3, float(r["grading"]) - 1.0)
    d_line_min, d_ch_min = 2.0 * h_line, 0.5 * cell
    d_line_max = max(float(r["dam_dist_cells"]) * cell, d_line_min + (h_base - h_line) / g1)
    d_ch_max = max(float(r["channel_dist_cells"]) * cell, d_ch_min + (h_base - h_ch) / g1)
    spec = {"outer": np.asarray(perimeter, float).reshape(-1, 2).tolist(),
            "lines": lines, "channel": chan, "points": [],
            # gmsh_mesher's sizes are triangle areas; its line size is 1.3 x the edge of
            # min_area, so min_area is set from h_line / 1.3
            "min_area_m2": _area(h_line / 1.3), "channel_area_m2": _area(h_ch),
            "max_area_m2": _area(h_base),
            "line_dist_min_m": d_line_min, "line_dist_max_m": d_line_max,
            "channel_dist_min_m": d_ch_min, "channel_dist_max_m": d_ch_max,
            "algorithm": int(r["algorithm"]), "threads": 1,
            "random_seed": None if r["random_seed"] is None else int(r["random_seed"]),
            # seeds, not ANUGA triangles: no quality gate (HEC-RAS makes its own cells)
            "min_angle_deg": 0.0, "min_area_tol": 0.0, "declutter": True}
    sizes = {"cell": float(cell), "base_spacing": round(h_base, 3), "dam_spacing": round(h_dam, 3),
             "breach_spacing": round(h_br, 3), "channel_spacing": round(h_ch, 3),
             "dam_dist": round(d_line_max, 3), "channel_dist": round(d_ch_max, 3),
             "grading": float(r["grading"]), "algorithm": int(r["algorithm"]),
             "random_seed": r["random_seed"],
             "lines": len(lines), "channel_lines": len(chan)}
    return spec, sizes, r


def _ring_distance(px, py, ring):
    s = np.asarray(ring, float)
    s = np.vstack([s, s[:1]])
    best = np.full(np.shape(px), np.inf)
    for (x1, y1), (x2, y2) in zip(s[:-1], s[1:]):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = np.zeros_like(px) if L2 == 0 else np.clip(((px - x1) * dx + (py - y1) * dy) / L2,
                                                       0.0, 1.0)
        best = np.minimum(best, np.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def _inside(px, py, ring):
    """Even-odd point in polygon, vectorised over the points."""
    s = np.asarray(ring, float)
    x, y = s[:, 0], s[:, 1]
    xj, yj = np.roll(x, 1), np.roll(y, 1)
    ins = np.zeros(np.shape(px), bool)
    for a, b, c, e in zip(x, y, xj, yj):
        cond = ((b > py) != (e > py))
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = (c - a) * (py - b) / np.where(e - b == 0, 1e-300, e - b) + a
        ins ^= cond & (px < xi)
    return ins


def clean_seeds(points, triangles, perimeter, edge_frac=DEFAULT_REFINE["edge_frac"]):
    """gmsh nodes -> computation points: the nodes on (or within edge_frac x the local spacing
    of) the perimeter go (HEC-RAS makes the perimeter cells itself; a point on the perimeter
    makes a sliver cell), as do nodes outside it. Returns (points (n, 2), local spacing (n,))."""
    pts = np.asarray(points, float).reshape(-1, 2)
    tri = np.asarray(triangles, np.int64).reshape(-1, 3)
    h = np.full(len(pts), np.inf)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        L = np.hypot(*(pts[tri[:, i]] - pts[tri[:, j]]).T)
        np.minimum.at(h, tri[:, i], L)
        np.minimum.at(h, tri[:, j], L)
    ok = np.isfinite(h)
    d = _ring_distance(pts[:, 0], pts[:, 1], perimeter)
    ok &= d >= float(edge_frac) * np.where(np.isfinite(h), h, 0.0)
    ok &= _inside(pts[:, 0], pts[:, 1], perimeter)
    return pts[ok], h[ok]


def gmsh_version_inprocess():
    try:
        import gmsh                                    # noqa: F401
    except Exception:                                  # noqa: BLE001 - optional program
        return None
    return str(getattr(gmsh, "__version__", "") or "unknown")


def _mesh_inprocess(spec):
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        from . import gmsh_mesher as gm                # in the plugin package
    except ImportError:
        if here not in sys.path:
            sys.path.insert(0, here)
        import gmsh_mesher as gm                       # stand-alone script
    m = gm.build_mesh(spec)
    return m["points"], m["triangles"], m["stats"]


def _clean_env():
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    return env


def interpreters(options=None, env=None):
    """Other Pythons to try for gmsh, in order (option gmsh_python, NDBREACH_GMSH_PYTHON,
    NDBREACH_ANUGA_PYTHON), with where each came from."""
    env = os.environ if env is None else env
    out = []
    for v, src in (((options or {}).get("gmsh_python"), "option gmsh_python"),
                   (env.get(ENV_PYTHON), ENV_PYTHON), (env.get(ENV_ANUGA_PYTHON), ENV_ANUGA_PYTHON)):
        if v and v not in [p for p, _ in out]:
            out.append((str(v), src))
    return out


def _mesh_subprocess(spec, python, timeout_s):
    with tempfile.TemporaryDirectory(prefix="hecras_seeds_") as td:
        sp, op = os.path.join(td, "seeds_in.json"), os.path.join(td, "seeds_out.npz")
        with open(sp, "w", encoding="utf-8") as fh:
            json.dump(spec, fh)
        try:
            out = subprocess.run([python, os.path.abspath(__file__), sp, op], capture_output=True,
                                 text=True, env=_clean_env(), timeout=float(timeout_s),
                                 errors="replace")
        except subprocess.TimeoutExpired:
            raise SeedError("gmsh took over {:.0f} s (timeout)".format(timeout_s))
        except Exception as e:                         # noqa: BLE001
            raise SeedError("{} could not start ({})".format(python, e))
        msg = (out.stdout + out.stderr).strip().splitlines()
        if out.returncode != 0 or not os.path.exists(op):
            why = next((m[len("SEEDERROR "):] for m in msg if m.startswith("SEEDERROR ")),
                       msg[-1][:200] if msg else "code {}".format(out.returncode))
            raise SeedError(why)
        with np.load(op) as r:
            return (np.asarray(r["points"]), np.asarray(r["triangles"]),
                    json.loads(str(r["stats"])), str(r["gmsh_version"]))


def make_seeds(perimeter, cell, options=None, env=None, timeout_s=TIMEOUT_S, **lines):
    """gmsh computation points for the 2D area. Returns (points (n, 2), info). Raises
    SeedError with the reason when gmsh is not available or fails (build_model falls back)."""
    spec, sizes, r = spec_for(perimeter, cell, refine=(options or {}).get("gmsh_refine"), **lines)
    t0 = time.time()
    tried = []
    ver = gmsh_version_inprocess()
    res = None
    if ver is not None:
        try:
            p, t, stats = _mesh_inprocess(spec)
            res = (p, t, stats, ver, "this Python ({})".format(sys.executable))
        except Exception as e:                         # noqa: BLE001 - reported, then fallback
            tried.append("gmsh in this Python: {}: {}".format(type(e).__name__, e))
    else:
        tried.append("gmsh is not installed in this Python ({})".format(sys.executable))
    if res is None:
        for py, src in interpreters(options, env):
            try:
                p, t, stats, v = _mesh_subprocess(spec, py, timeout_s)
                res = (p, t, stats, v, "{} ({})".format(py, src))
                break
            except SeedError as e:
                tried.append("{} ({}): {}".format(py, src, e))
        if res is None and not interpreters(options, env):
            tried.append("no other Python with gmsh given (option gmsh_python, {} or {})".format(
                ENV_PYTHON, ENV_ANUGA_PYTHON))
    if res is None:
        raise SeedError("; ".join(tried))
    p, t, stats, ver, where = res
    pts, h = clean_seeds(p, t, perimeter, r["edge_frac"])
    # the same points in the same order every run (gmsh numbers its nodes by thread)
    order = np.lexsort((pts[:, 0], pts[:, 1]))
    pts, h = pts[order], h[order]
    pts, n_thin = decimate(pts, float(r["min_spacing_frac"]) * min(
        sizes["dam_spacing"], sizes["breach_spacing"], sizes["channel_spacing"]))
    grid_n = abs(_ring_area(perimeter)) / float(cell) ** 2
    if len(pts) < 10:
        raise SeedError("gmsh gave {} usable points".format(len(pts)))
    cap = min(max(float(r["max_seeds_ratio"]) * grid_n, MIN_SEED_CAP), float(r["max_points"]))
    if len(pts) > cap:
        raise SeedError("gmsh gave {:,} points, more than the cap of {:,.0f} (max_seeds_ratio "
                        "{:g} x the {:,.0f} points of the regular grid, at most max_points "
                        "{:,.0f}); raise gmsh_refine dam_frac / channel_frac or the cell "
                        "size".format(len(pts), cap, r["max_seeds_ratio"], grid_n,
                                      float(r["max_points"])))
    info = {"gmsh_version": ver, "python": where, "seeds": int(len(pts)),
            "gmsh_nodes": int(len(p)), "dropped_on_perimeter": int(len(p) - len(pts)),
            "thinned_min_spacing": int(n_thin),
            "grid_equivalent": int(round(grid_n)), "seconds": round(time.time() - t0, 2),
            "smallest_spacing": round(float(h.min()), 3) if len(h) else None,
            "sizes": sizes, "gmsh_stats": stats, "tried_first": tried}
    return pts, info


def decimate(points, min_dist):
    """Drop points closer than min_dist to a point kept before them (in their order).
    Returns (points, number dropped)."""
    pts = np.asarray(points, float).reshape(-1, 2)
    if min_dist <= 0 or len(pts) < 2:
        return pts, 0
    try:
        from scipy.spatial import cKDTree
    except ImportError:                                # pragma: no cover
        return pts, 0
    pairs = cKDTree(pts).query_pairs(float(min_dist), output_type="ndarray")
    if not len(pairs):
        return pts, 0
    keep = np.ones(len(pts), bool)
    nb = {}
    for a, b in pairs:
        nb.setdefault(int(min(a, b)), []).append(int(max(a, b)))
    for a in sorted(nb):
        if keep[a]:
            keep[nb[a]] = False
    return pts[keep], int((~keep).sum())


def spacing_stats(points, perimeter=None):
    """Spacing of the points: lengths of their Delaunay edges (the cell faces' normals),
    leaving out edges along the outside of the layout. {'median', 'p5', 'p95', 'min',
    'grading_p99', 'grading_max'} (grading: ratio of the mean spacing of two neighbouring
    points), or None without scipy or with fewer than 3 points."""
    pts = np.asarray(points, float).reshape(-1, 2)
    if len(pts) < 3:
        return None
    try:
        from scipy.spatial import Delaunay
    except ImportError:                                # pragma: no cover
        return None
    tri = Delaunay(pts)
    e = np.vstack([tri.simplices[:, [0, 1]], tri.simplices[:, [1, 2]], tri.simplices[:, [2, 0]]])
    e = np.unique(np.sort(e, axis=1), axis=0)
    hull = set(map(tuple, np.sort(tri.convex_hull, axis=1).tolist()))
    on_hull = np.array([tuple(x) in hull for x in e.tolist()])
    L = np.hypot(*(pts[e[:, 0]] - pts[e[:, 1]]).T)
    inner = ~on_hull
    if perimeter is not None:
        d = _ring_distance(pts[:, 0], pts[:, 1], perimeter)
        far = d > 2.0 * np.median(L[inner])
        inner &= far[e[:, 0]] & far[e[:, 1]]
    if not inner.any():
        inner = ~on_hull
    s = np.zeros(len(pts))
    c = np.zeros(len(pts))
    np.add.at(s, e[inner].ravel(), np.repeat(L[inner], 2))
    np.add.at(c, e[inner].ravel(), 1)
    loc = np.where(c > 0, s / np.maximum(c, 1), np.nan)
    a, b = loc[e[inner, 0]], loc[e[inner, 1]]
    ok = np.isfinite(a) & np.isfinite(b)
    g = np.maximum(a[ok], b[ok]) / np.minimum(a[ok], b[ok])
    Li = L[inner]
    return {"median": round(float(np.median(Li)), 2), "p5": round(float(np.percentile(Li, 5)), 2),
            "p95": round(float(np.percentile(Li, 95)), 2), "min": round(float(L.min()), 3),
            "grading_p99": round(float(np.percentile(g, 99)), 3) if len(g) else None,
            "grading_max": round(float(g.max()), 3) if len(g) else None}


def fidelity(seeds, centers, perimeter=None, tol=0.01):
    """After HEC-RAS compiled the geometry: do its cells sit on the points we gave it?
    seeds (n, 2) from the .g01 'Storage Area 2D Points', centers (m, 2) the geometry HDF's
    'Cells Center Coordinate' of the real cells (no ghost cells). One-to-one nearest match.
    Returns {'seeds', 'cells', 'matched', 'moved_over_tol', 'max_move', 'mean_move',
    'one_to_one', 'ok', 'spacing'}."""
    from scipy.spatial import cKDTree
    s = np.asarray(seeds, float).reshape(-1, 2)
    c = np.asarray(centers, float).reshape(-1, 2)
    d, j = cKDTree(c).query(s)
    back = cKDTree(s).query(c)[1]
    mutual = back[j] == np.arange(len(s))
    one = len(np.unique(j)) == len(s)
    out = {"seeds": int(len(s)), "cells": int(len(c)), "matched": int(mutual.sum()),
           "moved_over_tol": int((d > tol).sum()), "tol": tol,
           "max_move": round(float(d.max()), 6) if len(d) else None,
           "mean_move": float(d.mean()) if len(d) else None, "one_to_one": bool(one)}
    out["ok"] = bool(len(s) == len(c) and one and out["moved_over_tol"] == 0)
    out["spacing"] = spacing_stats(c, perimeter)
    return out


def _ring_area(pts):
    a = np.asarray(pts, float)
    x, y = a[:, 0], a[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def main(argv):
    with open(argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    ver = gmsh_version_inprocess()
    if ver is None:
        print("SEEDERROR gmsh is not installed in {}".format(sys.executable), flush=True)
        return 2
    try:
        p, t, stats = _mesh_inprocess(spec)
    except Exception as e:                             # noqa: BLE001
        print("SEEDERROR {}: {}".format(type(e).__name__, e), flush=True)
        return 3
    np.savez(argv[2], points=p, triangles=t, stats=json.dumps(stats), gmsh_version=ver)
    print("SEEDOK " + json.dumps({"nodes": int(len(p)), "gmsh_version": ver}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
