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
DEFAULT_REFINE = {"dam_frac": 0.25, "breach_frac": 0.25, "channel_frac": 0.5,
                  "dam_dist_cells": 8.0, "channel_dist_cells": 4.0,
                  "max_seeds_ratio": 4.0, "edge_frac": 0.45}
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
    spec = {"outer": np.asarray(perimeter, float).reshape(-1, 2).tolist(),
            "lines": lines, "channel": chan, "points": [],
            # gmsh_mesher's sizes are triangle areas; its line size is 1.3 x the edge of
            # min_area, so min_area is set from h_line / 1.3
            "min_area_m2": _area(h_line / 1.3), "channel_area_m2": _area(h_ch),
            "max_area_m2": _area(h_base),
            "line_dist_min_m": 2.0 * h_line, "line_dist_max_m": float(r["dam_dist_cells"]) * cell,
            "channel_dist_min_m": 0.5 * cell,
            "channel_dist_max_m": float(r["channel_dist_cells"]) * cell,
            # seeds, not ANUGA triangles: no quality gate (HEC-RAS makes its own cells)
            "min_angle_deg": 0.0, "min_area_tol": 0.0, "declutter": True}
    sizes = {"cell": float(cell), "base_spacing": round(h_base, 3), "dam_spacing": round(h_dam, 3),
             "breach_spacing": round(h_br, 3), "channel_spacing": round(h_ch, 3),
             "dam_dist": round(float(r["dam_dist_cells"]) * cell, 3),
             "channel_dist": round(float(r["channel_dist_cells"]) * cell, 3),
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
    grid_n = abs(_ring_area(perimeter)) / float(cell) ** 2
    if len(pts) < 10:
        raise SeedError("gmsh gave {} usable points".format(len(pts)))
    cap = max(float(r["max_seeds_ratio"]) * grid_n, MIN_SEED_CAP)
    if len(pts) > cap:
        raise SeedError("gmsh gave {:,} points, more than {:,.0f} ({:g} x the {:,.0f} of the "
                        "regular grid)".format(len(pts), cap, r["max_seeds_ratio"], grid_n))
    info = {"gmsh_version": ver, "python": where, "seeds": int(len(pts)),
            "gmsh_nodes": int(len(p)), "dropped_on_perimeter": int(len(p) - len(pts)),
            "grid_equivalent": int(round(grid_n)), "seconds": round(time.time() - t0, 2),
            "smallest_spacing": round(float(h.min()), 3) if len(h) else None,
            "sizes": sizes, "gmsh_stats": stats, "tried_first": tried}
    return pts, info


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
