"""ANUGA (Geoscience Australia / ANU) tier. ANUGA runs in its own Python
environment (conda-forge), launched as a separate process so nothing has to
be installed into QGIS's Python.

The mesh covers the area LISFLOOD-FP flooded, widened by a buffer, with
small triangles near the dam where the flow is fastest and larger ones
downstream."""
import json
import math
import os
import queue
import subprocess
import threading
import time

import numpy as np

from .lisflood import Envelopes, ModelError, Cancelled
from . import terrain, wallclock
from .units import ARRIVAL_DEPTH_FT, FT
from . import conditioning as cond
from . import structures as structmod

# Windows: no console window for a child program (QGIS is a GUI program, so each console
# program it starts opens its own window, and closing that window kills the run)
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW

WORKER = os.path.join(os.path.dirname(__file__), "anuga_worker.py")
MESHER = os.path.join(os.path.dirname(__file__), "gmsh_mesher.py")
PARTIAL_NAME = "anuga_partial.npz"       # = anuga_worker.PARTIAL_NAME
# 3.10.35: with checkpoints on, a cancel sends SIGTERM first so the worker writes
# anuga_partial.npz, then SIGKILL after this many seconds
STOP_GRACE_S = 60.0
# "gmsh" is strict: a rejected gmsh mesh stops the run. "gmsh_or_default" uses the default
# ANUGA mesh instead, with a WARNING in the log and a line in run_report.json / .md.
MESHERS = ("default", "gmsh", "gmsh_or_default")
GMSH_MESHERS = ("gmsh", "gmsh_or_default")

# 3.10.47: ANUGA's OpenMP threads. ANUGA 4 runs its kernels on ONE thread unless it is told
# otherwise (OMP_NUM_THREADS unset -> 1), so threads=0 ("0 = all cores", which is what
# LISFLOOD-FP's OpenMP does by itself) left ANUGA single-threaded. resolve_threads turns 0
# into a real count: NDBREACH_ANUGA_THREADS when set (> 0), else min(CPUs - 2, AUTO_THREADS_MAX)
# (at least 1; the box is shared, and 4 threads already give ~2x). An explicit count > 0 is
# used as given (3.10.56: at most the CPUs this process may use; inf / nan / decimals / text
# are ignored with a note). The thread count never changes the results (bit-identical, see
# tests/test_anuga_threads.py) and is not part of the checkpoint fingerprint.
THREADS_ENV = "NDBREACH_ANUGA_THREADS"
AUTO_THREADS_MAX = 4
AUTO_THREADS_SPARE = 2


def cpu_count():
    """CPUs this process may use (the affinity mask where the OS has one), at least 1. A
    container's cgroup CPU quota is not read (a full affinity mask with a 2-CPU quota still
    counts all CPUs); harmless where the box has no quota, set the threads explicitly there."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    return max(1, int(n))


def _whole_count(v):
    """(n, problem): v as a whole number, or (None, why not). 3.10.56: inf / nan / text /
    decimals such as 2.7 are invalid (2.7 used to become 2, inf raised OverflowError)."""
    if v is None or isinstance(v, bool):
        return None, "not a number"
    if isinstance(v, int):
        return v, ""
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None, "not a number"
    if f != f or f in (float("inf"), float("-inf")):
        return None, "not a finite number"
    if f != int(f):
        return None, "not a whole number"
    return int(f), ""


def resolve_threads(threads, env=None, cpus=None):
    """(n, rule): the ANUGA OpenMP thread count for a requested `threads` and the reason.
    threads > 0 -> threads ("set"). 0 / None -> NDBREACH_ANUGA_THREADS from `env` (default
    os.environ) when it is a whole number > 0, else max(1, min(cpus - 2, 4)) ("auto").
    3.10.56: an explicit or NDBREACH_ANUGA_THREADS count above the CPUs this process may use
    is clamped to them; a requested count or env value that is not a finite whole number
    (inf, nan, 2.7, text) is ignored with a note in the rule and the automatic count is used.
    Never raises."""
    env = os.environ if env is None else env
    n = cpu_count() if cpus is None else max(1, int(cpus))
    notes = ""
    if threads is None or (isinstance(threads, str) and not threads.strip()):
        t = 0
    else:
        t, why = _whole_count(threads)
        if t is None:
            notes += "; threads={!r} ignored ({})".format(threads, why)
            t = 0
        elif t < 0:
            notes += "; threads={!r} ignored (not > 0)".format(threads)
            t = 0
    if t > 0:
        if t > n:
            return n, "set ({}), clamped to the {} CPUs".format(t, n)
        return t, "set ({})".format(t)
    raw = str(env.get(THREADS_ENV) or "").strip()
    if raw:
        v, why = _whole_count(raw)
        if v is None:
            notes += "; {}={!r} ignored ({})".format(THREADS_ENV, raw, why)
        elif v <= 0:
            notes += "; {}={!r} ignored (not > 0)".format(THREADS_ENV, raw)
        elif v > n:
            return n, "{}={}, clamped to the {} CPUs{}".format(THREADS_ENV, v, n, notes)
        else:
            return v, "{}={}{}".format(THREADS_ENV, v, notes)
    used = max(1, min(n - AUTO_THREADS_SPARE, AUTO_THREADS_MAX))
    return used, "automatic: min({} CPUs - {}, {}){}".format(n, AUTO_THREADS_SPARE,
                                                             AUTO_THREADS_MAX, notes)


class AnugaSettings:
    def __init__(self, python, threads=0, manning_n=0.06, manning_grid=None,
                 arrival_depth_m=ARRIVAL_DEPTH_FT * FT, sim_time_s=6 * 3600.0, yieldstep_s=300.0,
                 near_field_radius_m=1609.344, fine_area_m2=100.0, coarse_area_m2=1000.0,
                 buffer_m=500.0, algorithm="DE0", velocity_min_depth_m=0.03):
        self.python = python
        self.threads = threads
        self.manning_n = manning_n
        self.manning_grid = manning_grid
        self.arrival_depth_m = arrival_depth_m
        self.sim_time_s = sim_time_s
        self.yieldstep_s = yieldstep_s
        self.near_field_radius_m = near_field_radius_m
        self.fine_area_m2 = fine_area_m2
        self.coarse_area_m2 = coarse_area_m2
        self.buffer_m = buffer_m
        self.algorithm = algorithm
        self.velocity_min_depth_m = velocity_min_depth_m
        # 3.10.55: the outer edges. "outflow" = water leaves freely, none enters (the normal
        # momentum of inward-moving water is mirrored); "transmissive" = anuga's
        # Transmissive_boundary as before 3.10.55, which feeds water in wherever the water by
        # the edge moves inward (NDDOT culvert run 2026-10-21: 4.15 x the breach volume)
        self.edge_bc = "outflow"
        # ANUGA_MESHER: "default" = ANUGA's own mesh (create_domain_from_regions, unchanged);
        # "gmsh" = breakline-graded mesh from core/gmsh_mesher.py, the run stops if it is
        # rejected; "gmsh_or_default" = the same, but a rejected mesh falls back (loudly)
        self.mesher = "default"
        self.gmsh = None                 # dict of gmsh_mesher sizes (None = its defaults)
        self.gmsh_timeout_s = 600.0
        # 3.10.31 ANUGA_ELEVATION: "fine" = the bed from the finest terrain (the DEM source at
        # its own cell size, read in blocks), sampled at the triangle vertices; "model_grid" =
        # the model DEM at the triangle centroids (before 3.10.31). "fine" needs run()'s
        # elevation_read; without it, or when the source is not finer, the model grid is used.
        self.elevation = "fine"
        self.elevation_cell_m = None     # None = the source DEM's own cell size (>= 0.5 m)
        self.elevation_max_cells = 256e6   # cap on the fine grid (float32 on disk, ~1 GB)
        self.elevation_block = 2048      # cells per side of one read
        # 3.10.38 ANUGA checkpoints (domain.set_checkpointing) and partial results, see
        # resolve_checkpoints. checkpoint_min: None = automatic (every AUTO_CHECKPOINT_MIN
        # wall-clock minutes for a long run, none for a short one), 0 = off, > 0 = always, that
        # often; checkpoint_steps > 0: every that many yield steps instead. The newest
        # checkpoint_keep are kept. partial_min: anuga_partial.npz (the max depth / velocity /
        # WSE and arrival grids so far) that often (None = AUTO_PARTIAL_MIN when checkpoints
        # are on); it is also written after each checkpoint. resume: continue from the newest
        # checkpoint of the same job; resume_with_run: the same, only when checkpoints are on
        # (the run's own Resume). None of them changes the hydraulics.
        self.checkpoint_min = None
        self.checkpoint_steps = 0
        self.checkpoint_keep = 2
        self.partial_min = None
        self.resume = False
        self.resume_with_run = False


def inlet_line(inflow_xy, direction, width_m):
    """A line across the flow, centred on the inflow point."""
    ux, uy = direction
    px, py = -uy, ux
    h = 0.5 * width_m
    x, y = inflow_xy
    return np.array([[x - px * h, y - py * h], [x + px * h, y + py * h]])


def mesh_polygons(dem, flooded, inflow_xy, st):
    """Outer mesh polygon (flooded area + buffer, inside the DEM) and the
    refined near-field polygon."""
    region = terrain.dilate(flooded, st.buffer_m, dem.dx)
    # The simplified outline can wander up to `tol` from the region edge, so keep
    # the region that far (plus a cell) away from the DEM edge and from no-data;
    # otherwise the mesh could cover terrain that was filled in, not measured.
    tol = min(st.buffer_m / 10.0, 3.0 * dem.dx)
    tol = max(tol, dem.dx)
    valid_pad = np.pad(dem.valid(), 1, constant_values=False)
    near_bad = terrain.dilate(~valid_pad, tol + dem.dx, dem.dx)[1:-1, 1:-1]
    region &= ~near_bad
    r, c = dem.rc(*inflow_xy)
    if not (dem.inside(r, c) and region[r, c]):
        # the inflow cell itself must be inside the mesh
        region |= terrain.disk_mask(dem, inflow_xy, 2.5 * dem.dx) & dem.valid()
    region = terrain.largest_component(region, (r, c))
    outer = terrain.mask_outline(dem, region, simplify_m=tol)
    near = terrain.disk_mask(dem, inflow_xy, st.near_field_radius_m)
    fine_mask = terrain.dilate(flooded & near, min(200.0, st.buffer_m / 2.0), dem.dx) & region
    shrink = terrain.dilate(~region, max(3 * dem.dx, st.buffer_m / 4.0), dem.dx)
    fine_mask &= ~shrink
    fine = np.zeros((0, 2))
    if fine_mask.sum() > 20:
        fine_mask = terrain.largest_component(fine_mask, (r, c) if fine_mask[r, c] else None)
        fine = terrain.mask_outline(dem, fine_mask, simplify_m=max(2 * dem.dx, 20.0))
    return outer, fine


def octagon(center, radius, n=8):
    a = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    return np.column_stack([center[0] + radius * np.cos(a), center[1] + radius * np.sin(a)])


def culvert_area_m2(st):
    """Largest triangle at the culvert ends (both meshers)."""
    return min(st.fine_area_m2, 25.0)


def convex_hull(pts):
    """Convex hull (counter-clockwise, no repeated first point) of 2-D points."""
    p = sorted(set(map(tuple, np.asarray(pts, float).tolist())))
    if len(p) < 3:
        return np.array(p)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lo, hi = [], []
    for q in p:
        while len(lo) >= 2 and cross(lo[-2], lo[-1], q) <= 0:
            lo.pop()
        lo.append(q)
    for q in reversed(p):
        while len(hi) >= 2 and cross(hi[-2], hi[-1], q) <= 0:
            hi.pop()
        hi.append(q)
    return np.array(lo[:-1] + hi[:-1])


def convex_polygons_near(a, b, gap):
    """True when two convex polygons overlap or come within `gap` of each other on every
    separating axis (their edge normals; conservative: a 'near' pair may be a bit
    further apart than gap along a diagonal)."""
    for poly in (a, b):
        e = np.roll(poly, -1, axis=0) - poly
        for nx, ny in np.column_stack([-e[:, 1], e[:, 0]]):
            n = np.hypot(nx, ny)
            if n == 0:
                continue
            pa = (a[:, 0] * nx + a[:, 1] * ny) / n
            pb = (b[:, 0] * nx + b[:, 1] * ny) / n
            if pa.min() > pb.max() + gap or pb.min() > pa.max() + gap:
                return False
    return True


def culvert_regions(culverts, st):
    """Refined regions for the default mesher: an octagon of radius max(3 x width, 25 m)
    around each culvert end, as before 3.10.30. Octagons that overlap or nearly touch (a
    culvert shorter than two radii - Absaraka's 36 m culverts - or the ends of culverts
    side by side) crossed each other's edges, and ANUGA's mesher put sliver triangles at
    the crossings (down to 0.15 m2, which set the time step). Such octagons are now
    merged into one region, the convex hull of their corners, so no region edges cross.
    Octagons apart from all others are unchanged (the same job file as before)."""
    area = culvert_area_m2(st)
    gap = math.sqrt(4.0 * area / math.sqrt(3.0))       # one triangle edge at that size
    polys = []
    for c in culverts:
        r = max(3.0 * c["width"], 25.0)
        for e in c["end_points"]:
            polys.append(octagon(e, r))
    merged = True
    while merged:
        merged = False
        for i in range(len(polys)):
            for j in range(i + 1, len(polys)):
                if convex_polygons_near(polys[i], polys[j], gap):
                    polys[i] = convex_hull(np.vstack([polys[i], polys[j]]))
                    del polys[j]
                    merged = True
                    break
            if merged:
                break
    return [{"poly": p.tolist(), "area": area} for p in polys]


def clip_lines_to_polygon(lines, polygon, min_len=1):
    """Pieces of the polylines that lie inside `polygon` (vertex test)."""
    out = []
    for ln in lines:
        ln = np.asarray(ln, dtype=float)
        if len(ln) < 2:
            continue
        ins = cond.points_in_polygon(ln[:, 0], ln[:, 1], polygon)
        run = []
        for p, ok in zip(ln, ins):
            if ok:
                run.append(p)
            else:
                if len(run) > min_len:
                    out.append(np.array(run))
                run = []
        if len(run) > min_len:
            out.append(np.array(run))
    return out


# 3.10.38: the automatic checkpoint rule. A run is "long" when it simulates at least
# AUTO_CHECKPOINT_HOURS, or its mesh is expected to have at least AUTO_CHECKPOINT_TRIANGLES
# triangles (the gmsh mesh's own count; for ANUGA's mesher 1.5 x the area over the largest
# triangle area of each region, near what ANUGA 4.0.1 makes). A long run checkpoints every
# AUTO_CHECKPOINT_MIN wall-clock minutes (a run that ends sooner writes none) and writes
# anuga_partial.npz every AUTO_PARTIAL_MIN minutes.
AUTO_CHECKPOINT_HOURS = 12.0
AUTO_CHECKPOINT_TRIANGLES = 50000
AUTO_CHECKPOINT_MIN = 30.0
AUTO_PARTIAL_MIN = 10.0
TRIANGLES_PER_MAX_AREA = 1.5


def polygon_area(poly):
    p = np.asarray(poly, float).reshape(-1, 2)
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def expected_triangles(st, outer, fine, mesh=None):
    """The triangle count to expect: the gmsh mesh's own, else an estimate from the outer and
    near-dam areas and their largest triangle areas."""
    if mesh is not None:
        return int(len(mesh["triangles"]))
    a_out, a_fine = polygon_area(outer), polygon_area(fine) if len(fine) >= 3 else 0.0
    n = (max(a_out - a_fine, 0.0) / max(st.coarse_area_m2, 1.0)
         + a_fine / max(st.fine_area_m2, 1.0))
    return int(round(TRIANGLES_PER_MAX_AREA * n))


def resolve_checkpoints(st, outer, fine, mesh=None):
    """A copy of st with the checkpoint settings resolved to numbers, and the decision
    ({"mode": "auto" | "set" | "off", ...}). checkpoint_min None = the automatic rule."""
    import copy
    cst = copy.copy(st)
    steps = int(getattr(st, "checkpoint_steps", 0) or 0)
    cmin = getattr(st, "checkpoint_min", None)
    sim_h = float(st.sim_time_s) / 3600.0
    info = {"rule": "simulated >= {:g} h or >= {:,} triangles expected".format(
        AUTO_CHECKPOINT_HOURS, AUTO_CHECKPOINT_TRIANGLES), "simulated_h": round(sim_h, 2)}
    if steps > 0 or (cmin is not None and float(cmin) > 0):
        info["mode"] = "set"
        cst.checkpoint_min = float(cmin or 0.0)
    elif cmin is not None:                       # 0: switched off
        info["mode"] = "off"
        cst.checkpoint_min = 0.0
    else:
        ntri = expected_triangles(st, outer, fine, mesh)
        info["expected_triangles"] = ntri
        long_run = sim_h >= AUTO_CHECKPOINT_HOURS or ntri >= AUTO_CHECKPOINT_TRIANGLES
        info["mode"] = "auto" if long_run else "off"
        cst.checkpoint_min = AUTO_CHECKPOINT_MIN if long_run else 0.0
    on = cst.checkpoint_min > 0 or steps > 0
    pmin = getattr(st, "partial_min", None)
    cst.partial_min = float(pmin) if pmin is not None else (AUTO_PARTIAL_MIN if on else 0.0)
    cst.resume = bool(getattr(st, "resume", False)
                      or (getattr(st, "resume_with_run", False) and on))
    return cst, info


def checkpoint_job(st):
    """The worker's checkpoint keys (anuga_worker.CKPT_KEYS) for st; {} when none is on, so
    the job file is the one written before 3.10.35."""
    every_min = float(getattr(st, "checkpoint_min", 0.0) or 0.0)   # None (unresolved) = 0
    steps = int(getattr(st, "checkpoint_steps", 0) or 0)
    partial = float(getattr(st, "partial_min", 0.0) or 0.0)
    resume = bool(getattr(st, "resume", False))
    if every_min <= 0 and steps <= 0 and partial <= 0 and not resume:
        return {}
    return {"ckpt_every_min": max(0.0, every_min), "ckpt_every_steps": max(0, steps),
            "ckpt_keep": max(1, int(getattr(st, "checkpoint_keep", 2) or 2)),
            "partial_every_min": max(0.0, partial), "resume": int(resume)}


def checkpointing(st):
    """True when the run writes checkpoints or partial results."""
    j = checkpoint_job(st)
    return bool(j) and (j["ckpt_every_min"] > 0 or j["ckpt_every_steps"] > 0
                        or j["partial_every_min"] > 0)


def partial_note(workdir):
    """'' or a line naming anuga_partial.npz (the envelope so far) after a stopped run."""
    p = os.path.join(workdir, PARTIAL_NAME)
    if not os.path.exists(p):
        return ""
    try:
        with np.load(p) as z:
            info = json.loads(str(z["info"]))
        return ("Partial ANUGA results up to t = {:.2f} h ({}% of the run, {}) are in {}."
                .format(info["t_s"] / 3600.0, info.get("percent"), info.get("reason"), p))
    except Exception:       # noqa: BLE001 - still name the file
        return "Partial ANUGA results are in {}.".format(p)


def write_job(path, dem, outer, fine, inlet, hydrograph, st, out_grid, initial_depth=None,
              culverts=None, breaklines=None, regions=None, mesh=None, elevation=None):
    job = {
        "dem": dem.masked().astype(np.float32), "dem_x0": dem.x0, "dem_y0": dem.y0,
        "dem_dx": dem.dx, "manning_n": st.manning_n,
        "manning": (st.manning_grid.masked().astype(np.float32) if st.manning_grid is not None
                    else np.zeros(0)),
        "boundary": outer, "fine": fine, "inlet": inlet,
        "fine_area": st.fine_area_m2, "coarse_area": st.coarse_area_m2,
        "hyd_t": hydrograph.t_s, "hyd_q": hydrograph.q_cms,
        "sim_time": st.sim_time_s, "yieldstep": st.yieldstep_s,
        "arrival_depth": st.arrival_depth_m, "velocity_min_depth": st.velocity_min_depth_m,
        "algorithm": st.algorithm, "threads": int(st.threads or 0),
        "edge_bc": str(getattr(st, "edge_bc", None) or "outflow"),
        "out_x0": out_grid.x0, "out_y0": out_grid.y0, "out_dx": out_grid.dx,
        "out_nx": out_grid.nx, "out_ny": out_grid.ny,
    }
    if initial_depth is not None:
        job["init_depth"] = initial_depth.masked().astype(np.float32)
    if culverts:
        job["culverts_json"] = json.dumps(culverts)
    if breaklines:
        job["breaklines_json"] = json.dumps([np.asarray(b).tolist() for b in breaklines])
    if regions:
        job["regions_json"] = json.dumps(regions)
    if mesh is not None:     # gmsh mesh: the worker uses anuga.Domain(points, vertices, boundary)
        job["mesh_points"] = np.asarray(mesh["points"], float)
        job["mesh_triangles"] = np.asarray(mesh["triangles"], np.int64)
        job["mesh_boundary"] = np.asarray(mesh["boundary"], np.int64)
        job["mesh_stats"] = json.dumps(mesh.get("stats", {}))
    if elevation:            # 3.10.31: the fine bed (prepare_elevation); absent = as before
        job.update(elevation)
    job.update(checkpoint_job(st))   # 3.10.35: opt-in checkpoints; absent = as before
    np.savez(path, **job)


def _clean_env():
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    return env


def gmsh_spec(dem, outer, inflow_xy, inlet, culverts, breaklines, st, channel=None,
              embankments=None):
    """The gmsh_mesher input: outer ring, breaklines (REFINE_LINES, road and rail
    centrelines on embankments, culverts, the inlet/breach line), the flow path and the
    points that need small triangles (culvert ends, inflow)."""
    lines = [np.asarray(b, float).tolist() for b in (breaklines or [])]
    lines += [np.asarray(b, float).tolist() for b in (embankments or [])]
    lines.append(np.asarray(inlet, float).tolist())
    pts = [list(map(float, inflow_xy))]
    for c in culverts or []:
        e = np.asarray(c["end_points"], float).reshape(-1, 2)
        lines.append(e.tolist())
        pts += e.tolist()
    if channel is None:
        try:
            path, _ = terrain.trace_downstream(dem, inflow_xy, 4.0 * max(dem.nx, dem.ny) * dem.dx)
            # clip first (vertex test), then simplify each inside piece
            channel = [terrain.douglas_peucker(c, max(dem.dx, 10.0))
                       for c in clip_lines_to_polygon([np.asarray(path, float)], outer)]
        except Exception:
            channel = []
    spec = dict(outer=np.asarray(outer, float).tolist(), lines=lines,
                channel=[np.asarray(c, float).tolist() for c in channel], points=pts,
                threads=int(st.threads or 0))
    if culverts:
        # 3.10.31: the culvert ends get the default mesher's size (its octagons around each
        # end, culvert_regions); without it they got min_area_m2 (100 m2). The quality gate
        # follows (gmsh_mesher.gate_area: 0.45 x the smallest size asked for).
        spec["culvert_area_m2"] = culvert_area_m2(st)
    spec.update({k: v for k, v in (st.gmsh or {}).items() if not k.startswith("embankment")})
    return spec


EMBANKMENT_DEFAULTS = dict(embankment_lines=True, embankment_raise_m=0.3,
                           embankment_offset_m=20.0, embankment_step_m=10.0,
                           embankment_min_len_m=100.0, embankment_gap_m=30.0,
                           embankment_cell_m=5.0, embankment_wet_pad_m=150.0)


def embankment_options(st):
    o = dict(EMBANKMENT_DEFAULTS)
    o.update({k: v for k, v in (getattr(st, "gmsh", None) or {}).items() if k in o})
    return o


def embankment_breaklines(lines, grid, outer, raise_m=0.3, offset_m=20.0, step_m=10.0,
                          min_len_m=100.0, gap_m=30.0, wet=None):
    """Pieces of road / rail centrelines that sit on an embankment, for the gmsh mesh.

    Each line is sampled every step_m inside `outer`; a sample is on an embankment when
    the crown (the highest ground within 3 m across the line) is at least raise_m above
    the ground offset_m to BOTH sides. Runs of such samples (gaps up to gap_m bridged, so a
    culvert cut does not split the road) at least min_len_m long are returned as
    polylines. Lines on flat ground or on a side-hill are left out: only a raised fill
    holds water back. `wet` (optional (Grid, bool array)): only where the first
    (LISFLOOD-FP) run flooded, widened; roads in the dry buffer only add triangles.
    Returns (pieces, info dict)."""
    out, n_in, km_in, km_out = [], 0, 0.0, 0.0
    outer = np.asarray(outer, float)
    zz = grid.masked()                    # once: Grid.sample_bilinear copies it per call

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
    for ln in lines or []:
        ln = np.asarray(ln, float).reshape(-1, 2)
        if len(ln) < 2:
            continue
        for piece in clip_lines_to_polygon([cond.densify(ln, step_m)], outer):
            if len(piece) < 3:
                continue
            n_in += 1
            seg = np.hypot(*np.diff(piece, axis=0).T)
            km_in += seg.sum() / 1000.0
            t = np.gradient(piece, axis=0)
            nrm = np.hypot(t[:, 0], t[:, 1])
            nrm[nrm == 0] = 1.0
            nx, ny = -t[:, 1] / nrm, t[:, 0] / nrm
            with np.errstate(invalid="ignore"):
                crown = np.nanmax(np.vstack([sample(piece[:, 0] + nx * d, piece[:, 1] + ny * d)
                                             for d in (-3.0, 0.0, 3.0)]), axis=0)
            left = sample(piece[:, 0] + nx * offset_m, piece[:, 1] + ny * offset_m)
            right = sample(piece[:, 0] - nx * offset_m, piece[:, 1] - ny * offset_m)
            with np.errstate(invalid="ignore"):
                raised = (crown - np.fmax(left, right)) >= raise_m
            raised &= np.isfinite(crown) & np.isfinite(left) & np.isfinite(right)
            if wet is not None:
                wg, wm = wet
                c = np.floor((piece[:, 0] - wg.x0) / wg.dx).astype(int)
                r = np.floor((wg.y0 - piece[:, 1]) / wg.dx).astype(int)
                ok = (r >= 0) & (r < wm.shape[0]) & (c >= 0) & (c < wm.shape[1])
                inw = np.zeros(len(piece), bool)
                inw[ok] = wm[r[ok], c[ok]]
                raised &= inw
            s = np.concatenate([[0.0], np.cumsum(seg)])
            # bridge short gaps (a culvert cut, a field approach)
            idx = np.flatnonzero(raised)
            for a, b in zip(idx[:-1], idx[1:]):
                if b > a + 1 and s[b] - s[a] <= gap_m + 1e-9:
                    raised[a:b] = True
            i = 0
            while i < len(raised):
                if not raised[i]:
                    i += 1
                    continue
                j = i
                while j + 1 < len(raised) and raised[j + 1]:
                    j += 1
                if s[j] - s[i] >= min_len_m:
                    out.append(piece[i:j + 1])
                    km_out += (s[j] - s[i]) / 1000.0
                i = j + 1
    return out, {"lines_inside": n_in, "km_inside": round(float(km_in), 2),
                 "embankment_pieces": len(out), "embankment_km": round(float(km_out), 2),
                 "raise_m": raise_m, "offset_m": offset_m, "cell_m": round(float(grid.dx), 2)}


def find_embankments(lines, outer, dem, terrain_read, st, flooded=None):
    """Road / rail embankment breaklines for the gmsh mesh (or ([], info) when off).
    Reads the terrain at <= embankment_cell_m over the mesh polygon (the ANUGA DEM is
    often too coarse to show a road fill); falls back to the ANUGA DEM. `flooded` (bool
    array on `dem`): only roads within embankment_wet_pad_m of it."""
    o = embankment_options(st)
    if not lines or not o["embankment_lines"]:
        return [], {"embankment_pieces": 0, "reason": "off" if lines else "no road or rail lines"}
    outer = np.asarray(outer, float)
    grid = dem
    if terrain_read is not None and dem.dx > float(o["embankment_cell_m"]):
        pad = float(o["embankment_offset_m"]) + 20.0
        x0, y0 = outer.min(axis=0) - pad
        x1, y1 = outer.max(axis=0) + pad
        # at most ~25 million cells
        cell = max(float(o["embankment_cell_m"]), math.sqrt((x1 - x0) * (y1 - y0) / 25e6))
        if cell < dem.dx:
            grid = terrain_read((float(x0), float(y0), float(x1), float(y1)), cell)
    wet = None
    if flooded is not None and np.asarray(flooded).shape == (dem.ny, dem.nx):
        wet = (dem, terrain.dilate(np.asarray(flooded, bool), float(o["embankment_wet_pad_m"]),
                                   dem.dx))
    return embankment_breaklines(lines, grid, outer, float(o["embankment_raise_m"]),
                                 float(o["embankment_offset_m"]), float(o["embankment_step_m"]),
                                 float(o["embankment_min_len_m"]), float(o["embankment_gap_m"]),
                                 wet=wet)


def make_gmsh_mesh(workdir, spec, st):
    """Run gmsh_mesher.py in the ANUGA interpreter with a timeout. Returns
    (mesh dict, summary note), or (None, reason) when gmsh failed, timed out or the
    mesh failed the quality gate; run() then stops or falls back (ANUGA_MESHER)."""
    sp = os.path.join(workdir, "gmsh_mesh_in.json")
    op = os.path.join(workdir, "gmsh_mesh.npz")
    with open(sp, "w", encoding="utf-8") as fh:
        json.dump(spec, fh)
    if os.path.exists(op):
        os.remove(op)
    try:
        # wall-clock timeout (a paused VM counts; core/wallclock.py), SIGTERM then SIGKILL
        out = wallclock.run([st.python, MESHER, sp, op], env=_clean_env(),
                            timeout=float(st.gmsh_timeout_s), grace=5.0, what="gmsh")
    except subprocess.TimeoutExpired:
        return None, "gmsh took over {:.0f} s (timeout)".format(st.gmsh_timeout_s)
    except Exception as e:
        return None, "gmsh could not start ({})".format(e)
    if out.returncode != 0 or not os.path.exists(op):
        msg = (out.stdout + out.stderr).strip().splitlines()
        why = next((m[len("MESHERROR "):] for m in msg if m.startswith("MESHERROR ")),
                   msg[-1][:200] if msg else "code {}".format(out.returncode))
        return None, why
    with np.load(op) as r:
        mesh = {"points": r["mesh_points"], "triangles": r["mesh_triangles"],
                "boundary": r["mesh_boundary"], "stats": json.loads(str(r["mesh_stats"]))}
    s = mesh["stats"]
    return mesh, ("ANUGA mesh from gmsh: {} triangles, smallest {:.0f} m2, minimum angle "
                  "{:.1f} deg.".format(s.get("triangles"), s.get("min_area_m2", 0),
                                       s.get("min_angle_deg", 0)))


ELEVATION_MODES = ("fine", "model_grid")
ELEVATION_FILE = "anuga_elevation_fine.npy"


def elevation_mode(st):
    return str(getattr(st, "elevation", "fine") or "fine").strip().lower()


def fine_elevation_cell(st, native_m, bounds, model_dx):
    """(cell, why) of the fine elevation grid over `bounds`: the source DEM's own cell size
    (2 m when unknown) or the elevation_cell_m override, at least 0.5 m, coarsened only so
    the grid stays within elevation_max_cells. cell is None when that is not finer than
    the model grid (why says so)."""
    over = getattr(st, "elevation_cell_m", None)
    if over:
        cell, why = float(over), "elevation_cell_m"
    elif native_m and native_m > 0:
        cell, why = float(native_m), "the source DEM's cell size"
    else:
        cell, why = 2.0, "2 m (the source DEM's cell size is unknown)"
    cell = max(cell, 0.5)
    x0, y0, x1, y1 = bounds
    cap = float(getattr(st, "elevation_max_cells", 256e6) or 256e6)
    need = math.sqrt(max((x1 - x0) * (y1 - y0), 0.0) / cap)
    if need > cell:
        cell = math.ceil(need * 100.0) / 100.0
        why += ", coarsened to keep the grid under {:.0f} million cells".format(cap / 1e6)
    if cell >= 0.999 * float(model_dx):
        return None, ("the source DEM ({:g} m) is not finer than the model grid ({:g} m)"
                      .format(cell, model_dx))
    return cell, why


def _rect_touches_polygon(rect, poly, pts):
    """Does the rectangle (x0, y0, x1, y1) meet the polygon? pts = the polygon's outline
    densified finer than the rectangle."""
    x0, y0, x1, y1 = rect
    xs = np.array([x0, x1, x0, x1, 0.5 * (x0 + x1)])
    ys = np.array([y0, y0, y1, y1, 0.5 * (y0 + y1)])
    if cond.points_in_polygon(xs, ys, poly).any():
        return True
    return bool(((pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0)
                 & (pts[:, 1] <= y1)).any())


def write_fine_elevation(path, polygon, cell, read, block=2048, pad_m=None, cancel=None):
    """Read the terrain over the polygon's bounding box at `cell` in blocks of block x block
    cells and write it to `path` as a float32 .npy (row 0 = top, NaN = no data) through a
    memory map, so a 20 x 20 mi domain at 1 m never sits in memory. Blocks that miss the
    polygon stay NaN (not read). read(bounds, cell) -> Grid. Returns the grid's x0, y0
    (left / top), shape and the counts."""
    poly = np.asarray(polygon, float)
    pad = float(pad_m if pad_m is not None else 4.0 * cell)
    x0 = math.floor((poly[:, 0].min() - pad) / cell) * cell
    y0 = math.floor((poly[:, 1].min() - pad) / cell) * cell
    x1 = math.ceil((poly[:, 0].max() + pad) / cell) * cell
    y1 = math.ceil((poly[:, 1].max() + pad) / cell) * cell
    nx = int(round((x1 - x0) / cell))
    ny = int(round((y1 - y0) / cell))
    edge = cond.densify(np.vstack([poly, poly[:1]]), max(cell, 0.25 * block * cell))
    mm = np.lib.format.open_memmap(path, mode="w+", dtype=np.float32, shape=(ny, nx))
    n_read = n_skip = n_valid = 0
    t0 = time.time()
    try:
        for r0 in range(0, ny, block):
            for c0 in range(0, nx, block):
                if cancel and cancel():
                    raise Cancelled("cancelled")
                h, w = min(block, ny - r0), min(block, nx - c0)
                bx0, bx1 = x0 + c0 * cell, x0 + (c0 + w) * cell
                by1, by0 = y1 - r0 * cell, y1 - (r0 + h) * cell
                if not _rect_touches_polygon((bx0, by0, bx1, by1), poly, edge):
                    mm[r0:r0 + h, c0:c0 + w] = np.nan
                    n_skip += 1
                    continue
                g = read((bx0, by0, bx1, by1), cell)
                a = np.full((h, w), np.nan, np.float32)
                d = np.asarray(g.data, np.float32)[:h, :w]
                a[:d.shape[0], :d.shape[1]] = d
                mm[r0:r0 + h, c0:c0 + w] = a
                n_valid += int(np.isfinite(a).sum())
                n_read += 1
        mm.flush()
    finally:
        del mm
    return {"x0": x0, "y0": y1, "nx": nx, "ny": ny, "cell_m": cell, "blocks_read": n_read,
            "blocks_skipped": n_skip, "valid_cells": n_valid,
            "file_mb": round(nx * ny * 4 / 1e6, 1), "read_s": round(time.time() - t0, 1)}


def prepare_elevation(workdir, outer, dem, st, elevation_read, native_m=None, label=None,
                      log=None, cancel=None):
    """(job keys or None, report dict) for the ANUGA bed (ANUGA_ELEVATION)."""
    mode = elevation_mode(st)
    info = {"mode": "model_grid", "cell_m": round(float(dem.dx), 3),
            "source": "model grid (the DEM at the model cell size, as LISFLOOD-FP uses)",
            "sampled_at": "centroids"}
    if mode == "model_grid":
        info["reason"] = "ANUGA_ELEVATION=model_grid"
        return None, info
    if mode != "fine":
        info["reason"] = "ANUGA_ELEVATION {!r} is unknown; used the model grid".format(mode)
        return None, info
    if elevation_read is None:
        info["reason"] = "no finer terrain source was given"
        return None, info
    o = np.asarray(outer, float)
    bounds = (o[:, 0].min(), o[:, 1].min(), o[:, 0].max(), o[:, 1].max())
    cell, why = fine_elevation_cell(st, native_m, bounds, dem.dx)
    if cell is None:
        info["reason"] = why
        return None, info
    path = os.path.join(workdir, ELEVATION_FILE)
    try:
        w = write_fine_elevation(path, o, cell, elevation_read,
                                 int(getattr(st, "elevation_block", 2048) or 2048), cancel=cancel)
    except Cancelled:
        raise
    except Exception as e:                      # noqa - the run goes on with the model grid
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        info["reason"] = "the fine terrain could not be read ({}: {}); used the model grid".format(
            type(e).__name__, str(e)[:200])
        if log:
            log("WARNING: ANUGA elevation: " + info["reason"])
        return None, info
    if not w["valid_cells"]:
        os.remove(path)
        info["reason"] = "the fine terrain has no data over the mesh; used the model grid"
        return None, info
    info = {"mode": "fine", "cell_m": cell, "cell_rule": why, "native_cell_m": native_m,
            "source": label or "the DEM source (conditioned as for the model grid)",
            "sampled_at": "triangle vertices (bilinear); the model grid fills gaps",
            "model_cell_m": round(float(dem.dx), 3), "file": ELEVATION_FILE}
    info.update({k: w[k] for k in ("nx", "ny", "blocks_read", "blocks_skipped", "valid_cells",
                                   "file_mb", "read_s")})
    if log:
        log("ANUGA elevation: {:g} m cells from {} ({} x {}, {:.0f} MB, {:.0f} s).".format(
            cell, info["source"], w["nx"], w["ny"], w["file_mb"], w["read_s"]))
    job = {"elev_fine_file": ELEVATION_FILE, "elev_fine_x0": w["x0"], "elev_fine_y0": w["y0"],
           "elev_fine_dx": cell}
    return job, info


def mesher_mode(st):
    return str(getattr(st, "mesher", "default") or "default").strip().lower()


def rejected_line(reason):
    """The report line for a gmsh mesh that was not used."""
    return "gmsh mesh rejected: {}; used the default mesh".format(reason)


def _launch(st, job, result, workdir, progress, cancel):
    """Run the worker in the ANUGA interpreter; returns (process, log path)."""
    env = dict(os.environ)
    # QGIS sets these for its own Python; the ANUGA interpreter must not inherit them.
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    if st.threads:          # run() passes the resolved count (resolve_threads), never 0
        env["OMP_NUM_THREADS"] = str(int(st.threads))
    log_path = os.path.join(workdir, "anuga.log")
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen([st.python, "-u", WORKER, job, result], cwd=workdir, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, errors="replace", **NO_WINDOW)
        lines = queue.Queue()

        def pump():
            for ln in proc.stdout:
                lines.put(ln)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        done = False
        while not done:
            if cancel and cancel():      # also while ANUGA is silent (meshing, start-up)
                if checkpointing(st) and os.name != "nt":   # 3.10.35: flush the partial file
                    proc.terminate()
                    try:
                        proc.wait(timeout=STOP_GRACE_S)
                    except subprocess.TimeoutExpired:
                        pass
                proc.kill()
                proc.wait()
                raise Cancelled("cancelled")
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                done = True
                continue
            logf.write(line)
            logf.flush()
            if line.startswith("PROGRESS") and progress:
                parts = line.split(None, 2)
                try:
                    progress(float(parts[1]), "ANUGA " + (parts[2].strip() if len(parts) > 2
                                                          else ""))
                except ValueError:
                    pass
        proc.wait()
    return proc, log_path


def _tail_text(path, chars=2000):
    """The last `chars` characters of a text file ('' when it cannot be read)."""
    try:
        with open(path, errors="replace", encoding="utf-8") as f:
            return f.read()[-chars:]
    except OSError:
        return ""


def run(workdir, dem, flooded, inflow_xy, direction, inlet_width_m, hydrograph, st,
        progress=None, cancel=None, log=None, structures=None, initial_depth=None,
        refine_lines=None, reuse=False, embankment_lines=None, terrain_read=None,
        elevation_read=None, elevation_native_m=None, elevation_label=None):
    """embankment_lines: road / rail centrelines (work CRS); with a gmsh mesher the
    pieces on an embankment become mesh lines (find_embankments). terrain_read(bounds,
    cell) reads finer ground for that test. Neither changes the default mesh.
    elevation_read(bounds, cell) -> Grid: the terrain ANUGA's bed is sampled from
    (ANUGA_ELEVATION=fine, prepare_elevation; st.elevation), elevation_native_m its own
    cell size and elevation_label its name for the report. Without it the bed is the
    model DEM `dem`, as before 3.10.31."""
    if not st.python or not os.path.isfile(st.python):
        raise ModelError("ANUGA Python interpreter not found: {}".format(st.python))
    os.makedirs(workdir, exist_ok=True)
    outer, fine = mesh_polygons(dem, flooded, inflow_xy, st)
    inlet = inlet_line(inflow_xy, direction, inlet_width_m)
    job = os.path.join(workdir, "anuga_job.npz")
    result = os.path.join(workdir, "anuga_result.npz")
    culverts, cnotes = structmod.anuga_culverts(structures or [], dem, outer)
    regions = culvert_regions(culverts, st)     # small triangles at both culvert ends
    step = max(5.0, float(np.sqrt(max(st.fine_area_m2, 1.0))))
    breaklines = clip_lines_to_polygon([cond.densify(np.asarray(b, float), step)
                                        for b in (refine_lines or [])], outer)
    mesh, mnote = None, None
    mesher = mesher_mode(st)
    minfo = {"requested": mesher, "used": "default", "rejected": None}
    if mesher in GMSH_MESHERS:
        emb, einfo = [], {}
        try:
            emb, einfo = find_embankments(embankment_lines, outer, dem, terrain_read, st,
                                          flooded=flooded)
        except Exception as e:                      # noqa - the mesh is made without them
            einfo = {"embankment_pieces": 0, "error": str(e)[:200]}
        minfo["embankments"] = einfo
        if log and einfo.get("embankment_pieces"):
            log("gmsh: {} road/rail embankment piece(s), {} km, become mesh lines.".format(
                einfo["embankment_pieces"], einfo.get("embankment_km")))
        spec = gmsh_spec(dem, outer, inflow_xy, inlet, culverts, breaklines, st,
                         embankments=emb)
        mesh, msg = make_gmsh_mesh(workdir, spec, st)
        if mesh is None:
            minfo["rejected"] = msg
            if mesher == "gmsh":
                raise ModelError(
                    "ANUGA_MESHER=gmsh: the gmsh mesh was rejected ({}). The run stopped "
                    "instead of quietly using the default mesh. Set ANUGA_MESHER="
                    "gmsh_or_default to fall back to the default ANUGA mesh, or change the "
                    "gmsh sizes (anuga_gmsh, e.g. min_area_tol). Mesh input: {}".format(
                        msg, os.path.join(workdir, "gmsh_mesh_in.json")))
            mnote = rejected_line(msg)
            if log:
                log("WARNING: " + mnote)
        else:
            minfo["used"] = "gmsh"
            minfo["gmsh"] = mesh.get("stats", {})
            mnote = msg
            if log:
                log(msg)
    elif mesher != "default":
        mnote = "ANUGA_MESHER {!r} is unknown; the default ANUGA mesh was used.".format(mesher)
        minfo["rejected"] = "unknown mesher {!r}".format(mesher)
        if log:
            log("WARNING: " + mnote)
    have = bool(reuse) and os.path.exists(result)
    if have:                 # the result is reused: the bed is not read again
        elev_job, elev_info = None, {"mode": "reused", "reason": "earlier result reused"}
    else:
        elev_job, elev_info = prepare_elevation(workdir, outer, dem, st, elevation_read,
                                                elevation_native_m, elevation_label, log, cancel)
    # 3.10.38: checkpoints on for a long run (resolve_checkpoints); the hydraulics are the same
    cst, ck_rule = resolve_checkpoints(st, outer, fine, mesh)
    # 3.10.47: a real OpenMP thread count for ANUGA (0 used to mean 1 thread); gmsh keeps
    # st.threads (gmsh_spec above), only the ANUGA job and its process get the resolved count
    cst.threads, thr_rule = resolve_threads(st.threads)
    if log:
        log("ANUGA threads: {} ({}).".format(cst.threads, thr_rule))
    write_job(job, dem, outer, fine, inlet, hydrograph, cst, dem, initial_depth, culverts,
              breaklines, regions, mesh=mesh, elevation=elev_job)
    np.savetxt(os.path.join(workdir, "mesh_boundary.csv"), outer, delimiter=",",
               header="x,y", comments="", fmt="%.3f")
    if len(fine):
        np.savetxt(os.path.join(workdir, "mesh_fine_area.csv"), fine, delimiter=",",
                   header="x,y", comments="", fmt="%.3f")
    if os.path.exists(result) and not have:
        os.remove(result)
    t0 = time.time()
    if have:
        log_path = os.path.join(workdir, "anuga.log")

        class _Done:
            returncode = 0
        proc = _Done()
    else:
        from ..damsuite.keepawake import keep_awake
        with keep_awake("ANUGA", log=log):
            proc, log_path = _launch(cst, job, result, workdir, progress, cancel)
    if proc.returncode != 0 or not os.path.exists(result):
        tail = _tail_text(log_path)
        note = partial_note(workdir) if checkpoint_job(cst) else ""
        raise ModelError("ANUGA stopped with code {}:\n{}{}".format(
            proc.returncode, tail, "\n" + note if note else ""))
    fpath = os.path.join(workdir, ELEVATION_FILE)
    if elev_job and os.path.exists(fpath):      # up to ~1 GB; the job only needs it once
        try:
            os.remove(fpath)
            elev_info["file_removed_after_run"] = True
        except OSError:
            pass
    # 3.4.1: read the archive inside `with` - an open .npz keeps the file locked on Windows
    # until garbage collection, which stopped the work folder from being removed
    with np.load(result) as r:
        stats = json.loads(str(r["stats"]))
        depth, vel, arr, wse = r["depth"], r["velocity"], r["arrival_hr"], r["wse"]
    if stats.get("checkpoint"):          # 3.10.38: why checkpoints were on
        stats["checkpoint"]["rule"] = ck_rule
    if not have:                         # 3.10.47; a reused result keeps its own record
        th = {"requested": int(st.threads or 0), "used": int(cst.threads), "rule": thr_rule}
        # 3.10.50: the OpenMP runtime's own count first (omp_probe); ANUGA's
        # get_omp_num_threads only echoes the count it was given
        rep = stats.get("omp_runtime_threads")
        if rep is None:
            rep = stats.get("omp_threads")
        if rep is not None:
            th["anuga_reported"] = rep
        if stats.get("omp_runtime"):
            th["omp_runtime"] = stats["omp_runtime"]
        if stats.get("omp_note"):
            th["note"] = stats["omp_note"]
        stats["threads"] = th
    stats.update({
        "mesher": minfo["used"], "mesh": minfo,
        "elevation_source": dict(elev_info, worker=stats.get("elevation")),
        "max_depth_ft": round(float(np.nanmax(depth)) / 0.3048, 2),
        "max_velocity_fps": round(float(np.nanmax(vel)) / 0.3048, 2),
        "flooded_acres": round(float(np.nansum(depth >= st.arrival_depth_m)) * dem.dx ** 2
                               / 4046.8564224, 1),
        "max_edge_depth_ft": round(stats["max_edge_depth_m"] / 0.3048, 2),
    })
    try:                    # v3.10.27 (#91): reporting only
        from . import report_metrics
        stats.update(report_metrics.pass_stats(dem, depth, vel))
    except Exception:       # noqa: BLE001
        pass
    if log:
        log("ANUGA finished in {:.1f} min ({} triangles, {} thread(s))".format(
            (time.time() - t0) / 60.0, stats.get("triangles"),
            (stats.get("threads") or {}).get("used", cst.threads)))
    notes = list(cnotes)
    if mnote:
        notes.append(mnote)
    vn = volume_note(stats)
    if vn:
        notes.append(vn)
    for e in stats.get("culvert_errors", []):
        notes.append("ANUGA culvert not applied: " + e)
    if refine_lines and not stats.get("breaklines_used") and breaklines and mesh is None:
        notes.append("ANUGA: the embankment lines could not be used as mesh edges (they cross or "
                     "touch each other); the mesh was made without them.")
    return Envelopes(dem.like(depth, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                     dem.like(wse, np.nan), stats, notes), outer, fine


VOLUME_GAIN_LIMIT = 1.02       # 3.10.55: more water in the domain than this x the supply
VOLUME_CHECK_LIMIT = 1.005     # a softer "check" line above this (a creeping gain)
SOLVER_RESIDUAL_LIMIT = 0.005  # |ANUGA conservation residual| / supply above this: check
POSITIVE_EDGE_INFLOW = 0.001   # net edge inflow / supply above this with outflow-only edges


def volume_note(stats, limit=VOLUME_GAIN_LIMIT):
    """3.10.55: a note when the water balance looks wrong. Fills stats['volume_balance'] for
    results written before 3.10.55. None when the balance is fine or unknown.
    - ratio (in domain / initial water plus breach hydrograph) > `limit`: a gain. The cause is
      named only as far as the numbers show it: ANUGA's net boundary flux integral says
      whether the extra water came in through the edge; without it the edge is the likely
      cause (old transmissive edges), but an instability or the inlet's integration could do it.
    - ratio > VOLUME_CHECK_LIMIT: a softer check line.
    - ANUGA's own conservation residual above SOLVER_RESIDUAL_LIMIT of the supply.
    - a positive net edge inflow with outflow-only edges (it should be <= 0).
    The ratio is net: gains at one edge can hide behind losses at another (ANUGA reports no
    gross edge flows); the residual and the edge-inflow line cover what it can."""
    vb = stats.get("volume_balance")
    if not vb:
        sup = float(stats.get("initial_volume_m3") or 0.0) + float(stats.get("injected_m3") or 0.0)
        vin = stats.get("volume_in_domain_m3")
        if vin is None or sup <= 0:
            return None
        vb = {"supplied_m3": round(sup, 1), "in_domain_m3": round(float(vin), 1),
              "ratio": round(float(vin) / sup, 4)}
        stats["volume_balance"] = vb
    r = vb.get("ratio")
    if r is None:
        return None
    sup = float(vb.get("supplied_m3") or 0.0)
    kind = stats.get("boundary", "transmissive")
    bi = vb.get("boundary_net_inflow_m3")
    out = []
    if r > limit:
        extra = vb["in_domain_m3"] - vb["supplied_m3"]
        if bi is not None and bi > 0.5 * extra:
            why = ("ANUGA's boundary flux integral shows {:,.0f} m3 net inflow through the mesh "
                   "edge ({} boundary), so the extra water came in there".format(bi, kind))
        elif bi is not None:
            why = ("ANUGA's boundary flux integral shows only {:,.0f} m3 net through the edge, so "
                   "the gain is probably not from the edge: check for an instability or the "
                   "inflow".format(bi))
        else:
            why = ("it most likely came in through the mesh edge ({} boundary), though an "
                   "instability could also cause it".format(kind))
        out.append("ANUGA volume check: the domain ends with {:.2f} x the water that went in "
                   "({:,.0f} m3 = {:,.0f} acre-ft more than the {:,.0f} m3 of initial water and "
                   "breach inflow); {}. Treat the depths near the mesh edge with caution until "
                   "this is resolved.".format(r, extra, extra / 1233.48184, vb["supplied_m3"],
                                              why))
    elif r > VOLUME_CHECK_LIMIT:
        out.append("ANUGA volume check: the domain ends with {:.3f} x the water that went in, a "
                   "small gain (above {:.3f}); worth a look.".format(r, VOLUME_CHECK_LIMIT))
    fr = vb.get("solver_residual_frac")
    if fr is not None and abs(fr) > SOLVER_RESIDUAL_LIMIT:
        out.append("ANUGA's own water balance is off by {:,.0f} m3 ({:+.1%} of the supply: end "
                   "volume - start - edge flux - inflow operators), which points at an "
                   "instability rather than a boundary.".format(vb["solver_residual_m3"], fr))
    if kind == "outflow" and bi is not None and sup > 0 and bi > POSITIVE_EDGE_INFLOW * sup:
        out.append("ANUGA reports a net inflow of {:,.0f} m3 through outflow-only edges, which "
                   "should be about zero or negative (a small inflow can pass where the outside stage is "
                   "copied); check the run.".format(bi))
    return " ".join(out) or None


def check_anuga(python, workdir):
    """Import ANUGA in the given interpreter and report its version."""
    if not python or not os.path.isfile(python):
        return {"ok": False, "message": "ANUGA Python interpreter not found: {}".format(python)}
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    code = ("import anuga, numpy, scipy, matplotlib; "
            "import sys; print(getattr(anuga,'__version__','?'), sys.version.split()[0])")
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True, env=env,
                             timeout=300, errors="replace", **NO_WINDOW)
    except Exception as e:
        return {"ok": False, "message": str(e)}
    if out.returncode != 0:
        return {"ok": False, "message": (out.stdout + out.stderr)[-1500:]}
    ver = out.stdout.strip().splitlines()[-1]
    res = {"ok": True, "message": "ANUGA {} (Python {})".format(*ver.split()[:2])}
    res.update(check_gmsh(python, env))
    return res


def check_gmsh(python, env=None):
    """Optional: is gmsh importable in the ANUGA interpreter (ANUGA_MESHER=gmsh)?
    Never makes the ANUGA check fail."""
    code = "import gmsh; print(gmsh.__version__)"
    try:
        out = subprocess.run([python, "-c", code], capture_output=True, text=True,
                             env=env or _clean_env(), timeout=120, errors="replace",
                             **NO_WINDOW)
    except Exception as e:
        return {"gmsh": None, "gmsh_message": "gmsh check failed: {}".format(e)}
    if out.returncode != 0:
        last = ((out.stdout + out.stderr).strip().splitlines() or ["?"])[-1][:200]
        hint = (" (Linux: install libglu1-mesa libxcursor1 libxinerama1 libxft2)"
                if "libGLU" in last or ".so" in last else " (pip install gmsh in the ANUGA env)")
        return {"gmsh": None, "gmsh_message": "gmsh not available, optional for "
                "ANUGA_MESHER=gmsh: {}{}".format(last, hint)}
    v = out.stdout.strip().splitlines()[-1]
    return {"gmsh": v, "gmsh_message": "gmsh {} (optional, for ANUGA_MESHER=gmsh)".format(v)}
