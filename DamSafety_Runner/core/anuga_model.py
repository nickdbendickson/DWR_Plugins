"""ANUGA (Geoscience Australia / ANU) tier. ANUGA runs in its own Python
environment (conda-forge), launched as a separate process so nothing has to
be installed into QGIS's Python.

The mesh covers the area LISFLOOD-FP flooded, widened by a buffer, with
small triangles near the dam where the flow is fastest and larger ones
downstream."""
import json
import math
import os
import subprocess
import time

import numpy as np

from .lisflood import Envelopes, ModelError, Cancelled
from . import terrain, wallclock
from . import anuga_detach as detach   # 3.12.2: the worker runs detached from QGIS
from .units import ARRIVAL_DEPTH_FT, FT
from . import conditioning as cond
from . import structures as structmod
from . import gmsh_mesher as gmshmod     # numpy only; gmsh is imported in build_mesh
from . import anuga_dtdiag               # 3.10.70: numpy only (the worker's dt diagnostic)
from . import anuga_crest as crestmod    # 3.10.91: road / rail crests held (numpy only)

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
        # 3.12.6: resume from a named checkpoint folder whose key differs from this job's
        # (anuga_worker resume_from; "" = NDBREACH_ANUGA_RESUME_FROM, else none). The worker
        # checks it against this job and logs every difference; see docs/USER_GUIDE.md.
        self.resume_from = ""
        # 3.10.70: the worker's read-only time-step diagnostic (core/anuga_dtdiag.py):
        # 0 = off; N = a row of anuga/timestep_diag.csv every N yield steps
        self.dt_diag = 0


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


ENQUIRY_EDGE_FACTOR = 1.3      # gmsh's point size is 1.3 x the equilateral edge (hpt)


def enquiry_gap_m(st):
    """3.10.71: ANUGA's culvert operator reads the head at an enquiry point `apron +
    enquiry_gap` beyond each end; with enquiry_gap 0 (ANUGA's default) that point is the
    far edge of the inlet region, so with 25 m2 triangles (7.6 m edges) and a 2-3 m apron
    it fell in a triangle the operator drains or fills (DWR-2801: all 68 culvert ends
    warned "Enquiry point ... is in an inlet triangle"). The gap is one local triangle
    edge (gmsh's culvert-end size 1.3 x the equilateral edge of culvert_area_m2: 9.9 m
    at 25 m2), so the enquiry triangle lies past the inlet's."""
    a = culvert_area_m2(st)
    return round(ENQUIRY_EDGE_FACTOR * math.sqrt(4.0 * a / math.sqrt(3.0)), 2)


def fine_lowest_ground(path, x0, y0, dx):
    """3.10.71: lowest_ground(xy, radius) on the fine bed file (prepare_elevation; x0 / y0 =
    left / top edge, NaN = no data): the lowest cell centre within `radius`, or None."""
    arr = np.load(path, mmap_mode="r")
    ny, nx = arr.shape

    def lowest(xy, radius):
        x, y = float(xy[0]), float(xy[1])
        c0 = max(int(math.floor((x - radius - x0) / dx)), 0)
        c1 = min(int(math.ceil((x + radius - x0) / dx)) + 1, nx)
        r0 = max(int(math.floor((y0 - y - radius) / dx)), 0)
        r1 = min(int(math.ceil((y0 - y + radius) / dx)) + 1, ny)
        if c0 >= c1 or r0 >= r1:
            return None
        z = np.asarray(arr[r0:r1, c0:c1], float)
        xc = x0 + (np.arange(c0, c1) + 0.5) * dx
        yc = y0 - (np.arange(r0, r1) + 0.5) * dx
        X, Y = np.meshgrid(xc, yc)
        near = (np.hypot(X - x, Y - y) <= radius) & np.isfinite(z)
        return float(z[near].min()) if near.any() else None
    return lowest


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
                      or (getattr(st, "resume_with_run", False) and on)
                      or resume_from(st))
    return cst, info


RESUME_FROM_ENV = "NDBREACH_ANUGA_RESUME_FROM"          # = anuga_worker.RESUME_FROM_ENV
RESUME_REF_JOB_ENV = "NDBREACH_ANUGA_RESUME_REF_JOB"    # = anuga_worker.RESUME_REF_JOB_ENV


def resume_from(st):
    """3.12.6: the checkpoint folder an explicit resume names ('' = none): st.resume_from,
    else NDBREACH_ANUGA_RESUME_FROM."""
    return str(getattr(st, "resume_from", "") or os.environ.get(RESUME_FROM_ENV, "")).strip()


def resume_wanted(st):
    """True when this run may continue from a checkpoint (any of the resume settings)."""
    return bool(getattr(st, "resume", False) or getattr(st, "resume_with_run", False)
                or resume_from(st))


def checkpoint_job(st):
    """The worker's checkpoint keys (anuga_worker.CKPT_KEYS) for st; {} when none is on, so
    the job file is the one written before 3.10.35."""
    every_min = float(getattr(st, "checkpoint_min", 0.0) or 0.0)   # None (unresolved) = 0
    steps = int(getattr(st, "checkpoint_steps", 0) or 0)
    partial = float(getattr(st, "partial_min", 0.0) or 0.0)
    rfrom = resume_from(st)
    resume = bool(getattr(st, "resume", False)) or bool(rfrom)
    if every_min <= 0 and steps <= 0 and partial <= 0 and not resume:
        return {}
    out = {"ckpt_every_min": max(0.0, every_min), "ckpt_every_steps": max(0, steps),
           "ckpt_keep": max(1, int(getattr(st, "checkpoint_keep", 2) or 2)),
           "partial_every_min": max(0.0, partial), "resume": int(resume)}
    if rfrom:                    # 3.12.6; outside the worker's fingerprint like the rest
        out["resume_from"] = rfrom
        ref = os.environ.get(RESUME_REF_JOB_ENV, "").strip()
        if ref:
            out["resume_ref_job"] = ref
    return out


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
              culverts=None, breaklines=None, regions=None, mesh=None, elevation=None,
              crest=None):
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
    if crest is not None and len(crest[0]):  # 3.10.91: crest nodes (x, y) and their z
        job["crest_xy"] = np.asarray(crest[0], float).reshape(-1, 2)
        job["crest_z"] = np.asarray(crest[1], float).ravel()
    job.update(checkpoint_job(st))   # 3.10.35: opt-in checkpoints; absent = as before
    job.update(dt_diag_job(st, getattr(dem, "crs_wkt", None)))   # 3.10.70; off = as before
    np.savez(path, **job)


def dt_diag_setting(value, environ=None):
    """3.10.70 anuga_dt_diag: yield steps between rows of the worker's timestep_diag.csv,
    0 = off. True / "on" = 1. NDBREACH_ANUGA_DT_DIAG=0|1 and NDBREACH_ANUGA_DT_DIAG_EVERY=N
    win over it (the same rule as the worker, anuga_dtdiag.dt_diag_every)."""
    if isinstance(value, str):
        v = value.strip().lower()
        value = 1 if v in ("on", "true", "yes") else 0 if v in ("", "off", "false", "no") \
            else value
    try:
        n = max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        raise ValueError("anuga_dt_diag must be 0 (off) or a number of yield steps, not "
                         "{!r}".format(value))
    return anuga_dtdiag.dt_diag_every({"dt_diag_every": n}, environ)


def dt_diag_job(st, crs_wkt=None):
    """The job keys of the time-step diagnostic; {} when it is off (the job is as before)."""
    n = int(getattr(st, "dt_diag", 0) or 0)
    if n <= 0:
        return {}
    return {"dt_diag_every": n, "dt_diag_crs": str(crs_wkt or "")}


def _clean_env():
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    return env


# 3.10.69 far-field sizing (gmsh_mesher module docstring): environment variable -> anuga_gmsh key
GMSH_SIZING_ENV = (("NDBREACH_ANUGA_GMSH_GRADE_DIST_M", "grade_dist_m"),
                   ("NDBREACH_ANUGA_GMSH_LINE_MIN_AREA_M2", "line_min_area_m2"),
                   ("NDBREACH_ANUGA_GMSH_MAX_AREA_M2", "max_area_m2"),
                   ("NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT", "refine_footprint"),
                   ("NDBREACH_ANUGA_GMSH_REFINE_PAD_M", "refine_footprint_pad_m"),
                   # 3.10.76: the line band (band_options)
                   ("NDBREACH_ANUGA_BAND", "band"),
                   ("NDBREACH_ANUGA_BAND_CELL_M", "band_cell_m"),
                   ("NDBREACH_ANUGA_BAND_DIST_M", "band_dist_m")) + crestmod.CREST_ENV
REFINE_PAD_M = 200.0          # refine_footprint "wet": the LISFLOOD-FP wet area padded this much
_FOOTPRINT_OFF = ("", "off", "none", "no", "false", "0")


def gmsh_env_options(gmsh=None, environ=None):
    """anuga_gmsh (a dict or None) with the NDBREACH_ANUGA_GMSH_* sizing variables applied;
    a variable that is set wins over the same key in the dict. Unset or empty variables
    change nothing, so with none set the result is `gmsh` itself (None stays None). Numbers
    are stored as floats; text that is not a number is kept as given and stopped with a
    clear error by check_gmsh_options when an ANUGA gmsh mesh is made. A footprint of
    'off' / 'none' removes refine_footprint."""
    env = os.environ if environ is None else environ
    out = None
    for var, key in GMSH_SIZING_ENV:
        raw = env.get(var)
        if raw is None or not str(raw).strip():
            continue
        out = dict(gmsh or {}) if out is None else out
        v = str(raw).strip()
        if key in ("band", "crest"):        # 3.10.91: crest (anuga_crest) as band
            out[key] = v.lower()
            continue
        if key == "refine_footprint":
            if v.lower() in _FOOTPRINT_OFF:
                out.pop(key, None)
            else:
                out[key] = v.lower()
            continue
        try:
            out[key] = float(v)
        except ValueError:
            out[key] = v
    if out is None or (not out and gmsh is None):
        return gmsh
    return out


def check_gmsh_options(st):
    """ModelError with a clear message for a bad far-field sizing value in st.gmsh (the
    anuga_gmsh setting or an NDBREACH_ANUGA_GMSH_* variable). Returns the footprint mode:
    None, "wet", or "rings" (a ring list given in the setting)."""
    o = getattr(st, "gmsh", None) or {}

    def bad(key, e):
        var = next((v for v, k in GMSH_SIZING_ENV if k == key), None)
        return ModelError("ANUGA gmsh sizing: {}{}".format(
            e, " (setting anuga_gmsh['{}'] or {})".format(key, var) if var else ""))
    bd = o.get("band")
    if bd is not None and not isinstance(bd, bool) and \
            str(bd).strip().lower() not in _BAND_ON + _FOOTPRINT_OFF:
        raise bad("band", "band must be 'on' or 'off', not {!r}".format(bd))
    for key in ("grade_dist_m", "line_min_area_m2", "max_area_m2", "refine_footprint_pad_m",
                "band_cell_m", "band_dist_m"):
        if o.get(key) is not None:
            try:
                gmshmod.positive_number(key, o[key])
            except gmshmod.MeshError as e:
                raise bad(key, e)
    try:
        crestmod.crest_options(o)           # 3.10.91: the crest hold's settings
    except crestmod.CrestError as e:
        raise ModelError("ANUGA gmsh crest hold: {}".format(e))
    fp = o.get("refine_footprint")
    if fp is None:
        return None
    if isinstance(fp, str):
        if fp.strip().lower() in _FOOTPRINT_OFF:
            return None
        if fp.strip().lower() == "wet":
            return "wet"
        raise bad("refine_footprint", "refine_footprint must be 'wet', 'off' or a list of "
                  "rings, not {!r}".format(fp))
    try:
        gmshmod.footprint_rings(fp)
    except gmshmod.MeshError as e:
        raise bad("refine_footprint", e)
    return "rings"


def wet_footprint(dem, flooded, inflow_xy, pad_m):
    """refine_footprint "wet": the outline of the LISFLOOD-FP wet cells (`flooded`) dilated
    by pad_m, the part joined to the inflow (which is always inside), as an open ring."""
    mask = terrain.dilate(np.asarray(flooded, bool), float(pad_m), dem.dx)
    mask |= terrain.disk_mask(dem, inflow_xy, max(float(pad_m), 2.5 * dem.dx))
    r, c = dem.rc(*inflow_xy)
    mask = terrain.largest_component(mask, (r, c) if dem.inside(r, c) else None)
    return terrain.mask_outline(dem, mask, simplify_m=max(2.0 * dem.dx, 20.0))


def gmsh_spec(dem, outer, inflow_xy, inlet, culverts, breaklines, st, channel=None,
              embankments=None, footprint=None, crest_lines=None):
    """The gmsh_mesher input: outer ring, breaklines (REFINE_LINES, road and rail
    centrelines on embankments, culverts, the inlet/breach line), the flow path and the
    points that need small triangles (culvert ends, inflow). footprint: the ring for
    refine_footprint "wet" (wet_footprint); the spec then carries it instead of the word.
    With no sizing option set the spec is the same as before 3.10.69.
    crest_lines (3.10.91, anuga_crest): the road / rail crest lines; given, they are the
    mesh lines instead of `embankments` (which they include) and go in the spec as
    "crest_lines" too, so gmsh_mesher returns the nodes on them (crest_nodes)."""
    lines = [np.asarray(b, float).tolist() for b in (breaklines or [])]
    if crest_lines:
        embankments = crest_lines
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
    spec.update({k: v for k, v in (st.gmsh or {}).items()
                 if not k.startswith("embankment") and k not in BAND_DEFAULTS
                 and k not in crestmod.CREST_DEFAULTS})
    if crest_lines:
        spec["crest_lines"] = [np.asarray(c, float).tolist() for c in crest_lines]
    bo = band_options(st)
    if bo["on"]:
        # 3.10.76: the line band, size_lines (gmsh_mesher): edges of band_cell_m within
        # band_dist_m of the embankment lines, breaklines and culverts, graded out at
        # BAND_GRADE to the line size (line_min_area_m2, else min_area_m2)
        size = float(bo["band_cell_m"])
        dmin = float(bo["band_dist_m"])
        la = (st.gmsh or {}).get("line_min_area_m2") or gmshmod.default_sizes()["min_area_m2"]
        h_line = 1.3 * gmshmod.h_of_area(float(la))
        dmax = dmin + max(size, (max(h_line, size) - size) / BAND_GRADE)
        bl = [np.asarray(b, float) for b in (breaklines or [])]
        bl += [np.asarray(b, float) for b in (embankments or [])]
        bl += [np.asarray(c["end_points"], float).reshape(-1, 2) for c in culverts or []]
        spec["size_lines"] = [{"line": b.tolist(), "size": size, "dist_min": dmin,
                               "dist_max": dmax} for b in bl if len(b) >= 2]
    fp = spec.get("refine_footprint")
    if isinstance(fp, str):
        if fp.strip().lower() == "wet" and footprint is not None:
            spec["refine_footprint"] = np.asarray(footprint, float).tolist()
        else:
            spec.pop("refine_footprint")         # 'off', or no wet area given
    return spec


# 3.10.76: the ANUGA line band (gmsh meshes only), the ANUGA side of the HEC-RAS line bands
# (hecras_bands): triangles with edges of band_cell_m (6.1 m = 20 ft) within band_dist_m
# (18.3 m = 3 rows of 20 ft) of the road / rail embankment lines, breaklines and culverts.
# Off by default: on DWR-2801 (314k triangles) the embankment lines alone add about 184k
# triangles (+58 %) and the smallest inradius falls ~3x (ANUGA's time step x ~0.67, run
# time x ~2.4). band_cell_m 15.2 / band_dist_m 30.5 (the 2 x 50 ft option) costs almost
# nothing, the line size (100 m2) being about that already.
BAND_DEFAULTS = {"band": "off", "band_cell_m": 6.1, "band_dist_m": 18.3}
BAND_GRADE = 0.3              # the band's size growth per unit distance out to the line size
_BAND_ON = ("on", "yes", "true", "1")


def band_options(st):
    """The ANUGA line band settings (BAND_DEFAULTS merged with st.gmsh): {'on', 'band_cell_m',
    'band_dist_m'}."""
    o = dict(BAND_DEFAULTS)
    o.update({k: v for k, v in (getattr(st, "gmsh", None) or {}).items()
              if k in o and v is not None})
    b = o["band"]
    o["on"] = b if isinstance(b, bool) else str(b).strip().lower() in _BAND_ON
    return o


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


def crest_lines_for(lines, outer, dem, flooded, st):
    """3.10.91 (anuga_crest): the road / rail crest lines for the gmsh mesh, every centreline
    inside `outer` within crest_pad_m of the wet area (`flooded` on `dem`; all of them inside
    `outer` without it). Returns (pieces or None, info for run_report anuga_mesh.crest)."""
    o = crestmod.crest_options(getattr(st, "gmsh", None))
    if not o["on"]:
        return None, {"on": False, "reason": "off (anuga_gmsh crest / NDBREACH_ANUGA_CREST)"}
    if not lines:
        return None, {"on": True, "crest_lines": 0, "reason": "no road or rail lines"}
    wet = None
    if flooded is not None and np.asarray(flooded).shape == (dem.ny, dem.nx):
        wet = (dem, terrain.dilate(np.asarray(flooded, bool), float(o["crest_pad_m"]), dem.dx))
    try:
        pieces, info = crestmod.crest_lines(lines, outer, wet)
    except Exception as e:                      # noqa - the mesh is made without them
        return None, {"on": True, "crest_lines": 0, "error": str(e)[:200]}
    info.update(on=True, pad_m=float(o["crest_pad_m"]),
                half_width_m=float(o["crest_half_width_m"]), cell_m=float(o["crest_cell_m"]))
    return (pieces or None), info


def crest_hold(mesh, pieces, spec, dem, structures, inlet, st, elevation_read=None,
               terrain_read=None, native_m=None):
    """3.10.91: ((x, y) of the held crest nodes, their crest z) and an info dict. The crest
    profile is read from elevation_read (the fine bed's terrain), else terrain_read, else
    the model DEM; bridge / notch cut corridors and the inlet are kept out."""
    from . import road_profile
    o = crestmod.crest_options(getattr(st, "gmsh", None))
    info = {}
    cn = mesh.get("crest_nodes")
    if cn is None:
        return None, {"held": 0, "reason": "the mesher returned no crest nodes"}
    cell = max(float(o["crest_cell_m"]), float(native_m or 0.0))
    read = elevation_read or terrain_read
    if read is None:
        def read(bounds, c, _g=dem):         # the model DEM (coarse): better than nothing
            return _g
        cell = max(cell, dem.dx)
        info["profile_source"] = "model DEM"
    else:
        info["profile_source"] = "fine terrain" if elevation_read else "terrain"
    t0 = time.time()
    try:
        profiles = [crestmod.crest_profile(p, read, cell, float(o["crest_half_width_m"]))
                    for p in pieces]
    except Exception as e:                      # noqa - the bed is then as before
        return None, dict(info, held=0, error="crest profile: " + str(e)[:200])
    pts = np.asarray(mesh["points"], float)
    cn = np.asarray(cn, np.int64)
    cuts = road_profile.cut_corridors(structures or [], dem.dx)
    ko = crestmod.keep_out_mask(pts[cn], cuts, inlet)
    la = spec.get("line_min_area_m2") or spec.get("min_area_m2") or \
        gmshmod.default_sizes()["min_area_m2"]
    hmin = 1.3 * gmshmod.h_of_area(float(la))
    idx, z, hinfo = crestmod.hold_crest(pts, cn, pieces, profiles,
                                        hmin + float(o["crest_half_width_m"]), ko)
    info.update(hinfo, profile_cell_m=round(cell, 2), cut_corridors=len(cuts),
                profile_s=round(time.time() - t0, 1))
    return (pts[idx], z), info


def crest_line(c):
    """One log line for run_report anuga_mesh.crest."""
    if c.get("error"):
        return "WARNING: gmsh crest hold not applied ({}).".format(c["error"])
    return ("gmsh crest hold: {} of {} crest node(s) held at the {:g} m crest profile "
            "({} in a bridge / notch cut or at the inlet, {} off the line, {} with no "
            "terrain).".format(c.get("held", 0), c.get("crest_nodes", 0),
                               c.get("profile_cell_m", c.get("cell_m", 1.0)),
                               c.get("kept_out", 0), c.get("off_line", 0),
                               c.get("no_profile", 0)))


MESH_KEY_NAME = "gmsh_mesh_key.json"


def _canon(spec):
    """The mesh input as gmsh_mesher reads it, without "threads" (how many threads gmsh
    uses is not what it meshes), as sorted JSON."""
    d = json.loads(json.dumps(spec))
    d.pop("threads", None)
    return json.dumps(d, sort_keys=True)


def _sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


def _mesher_sha():
    with open(MESHER, "rb") as fh:
        return _sha(fh.read())


def reusable_gmsh_mesh(workdir, spec):
    """3.12.6: None when the gmsh_mesh.npz in workdir was made from this same mesh input
    (and, when recorded, the same gmsh_mesher.py), else why not. gmsh with several threads
    does not give the same mesh twice (DWR-2801 r3: 344,866 then 344,856 triangles from the
    same input), and the mesh is part of the worker's checkpoint key, so a resumed run
    re-uses the mesh rather than make a new one. The npz is safe to re-use: make_gmsh_mesh
    deletes it before each mesh and gmsh_mesher writes it only on success."""
    sp = os.path.join(workdir, "gmsh_mesh_in.json")
    op = os.path.join(workdir, "gmsh_mesh.npz")
    kp = os.path.join(workdir, MESH_KEY_NAME)
    if not (os.path.isfile(sp) and os.path.isfile(op)):
        return "no earlier gmsh mesh"
    try:
        with open(sp, encoding="utf-8") as fh:
            old = _canon(json.load(fh))
    except Exception as e:                 # noqa - unreadable: mesh again
        return "earlier mesh input unreadable ({})".format(str(e)[:100])
    if old != _canon(spec):
        return "the mesh input changed"
    if os.path.isfile(kp):
        try:
            with open(kp, encoding="utf-8") as fh:
                key = json.load(fh)
        except Exception:                  # noqa
            return "unreadable " + MESH_KEY_NAME
        if key.get("spec_sha") != _sha(_canon(spec).encode()):
            return "the mesh input changed"
        if key.get("mesher_sha") != _mesher_sha():
            return "gmsh_mesher.py changed"
    return None


def _load_gmsh_npz(op):
    with np.load(op) as r:
        mesh = {"points": r["mesh_points"], "triangles": r["mesh_triangles"],
                "boundary": r["mesh_boundary"], "stats": json.loads(str(r["mesh_stats"]))}
        if "mesh_crest_nodes" in r.files:   # 3.10.91: nodes on the crest lines
            mesh["crest_nodes"] = np.asarray(r["mesh_crest_nodes"], np.int64)
    return mesh


def _gmsh_note(mesh):
    s = mesh["stats"]
    note = ("ANUGA mesh from gmsh: {} triangles, smallest {:.0f} m2, minimum angle "
            "{:.1f} deg.".format(s.get("triangles"), s.get("min_area_m2", 0),
                                 s.get("min_angle_deg", 0)))
    if s.get("sizing"):                     # 3.10.69: only when a sizing option is set
        note += " Sizing: " + sizing_line(s["sizing"]) + "."
    return note


def make_gmsh_mesh(workdir, spec, st, reuse=False):
    """Run gmsh_mesher.py in the ANUGA interpreter with a timeout. Returns
    (mesh dict, summary note), or (None, reason) when gmsh failed, timed out or the
    mesh failed the quality gate; run() then stops or falls back (ANUGA_MESHER).
    reuse (3.12.6, a resumed run): the mesh already in workdir is used when it was made
    from the same input (reusable_gmsh_mesh), so the checkpoint key stays the same."""
    sp = os.path.join(workdir, "gmsh_mesh_in.json")
    op = os.path.join(workdir, "gmsh_mesh.npz")
    if reuse and reusable_gmsh_mesh(workdir, spec) is None:
        try:
            mesh = _load_gmsh_npz(op)
            mesh["reused"] = True
            return mesh, _gmsh_note(mesh) + (" Re-used the mesh already made from the same "
                                             "input (a resumed run keeps its checkpoint key).")
        except Exception:                   # noqa - unreadable: mesh again
            pass
    kp = os.path.join(workdir, MESH_KEY_NAME)
    if os.path.exists(kp):
        os.remove(kp)
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
    mesh = _load_gmsh_npz(op)
    try:                                    # 3.12.6: what this mesh was made from
        with open(kp, "w", encoding="utf-8") as fh:
            json.dump({"spec_sha": _sha(_canon(spec).encode()), "mesher_sha": _mesher_sha()},
                      fh)
    except OSError:                         # noqa - only re-use on resume needs it
        pass
    return mesh, _gmsh_note(mesh)


def _mesher_sizes(spec):
    """gmsh_mesher.build_mesh's size defaults updated with the spec (for resolve_sizing's
    check before the mesher is started; the mesher does the same)."""
    g = gmshmod.default_sizes()
    g.update({k: v for k, v in spec.items() if k in g})
    return g


def sizing_line(z):
    """One line for the log and the mesh note from gmsh_mesher's "sizing" record."""
    parts = ["lines {:g} m2".format(z["min_area_m2"]),
             "far field {:g} m2".format(z["max_area_m2"]),
             "grading {:g} / {:g} / {:g} m (lines / channel / points)".format(
                 z["line_dist_max_m"], z["channel_dist_max_m"], z["point_dist_max_m"])]
    if z.get("refine_footprint_km2") is not None:
        parts.append("refined inside a {:g} km2 footprint".format(z["refine_footprint_km2"]))
    return ", ".join(parts)


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


def invert_source_line(culverts):
    """'34 culverts, 68 ends: 66 fine grid, 2 given' (log / report)."""
    n = {}
    for c in culverts:
        for v in c.get("invert_source") or []:
            n[v] = n.get(v, 0) + 1
    return "{} culvert(s), {} end(s): {}".format(
        len(culverts), sum(n.values()),
        ", ".join("{} {}".format(k_, v.replace("_", " ")) for v, k_ in sorted(n.items())))


def culvert_invert_summary(records):
    """3.10.71: counts for the run summary / manifest from stats['culvert_inverts']
    (enquiry_gap_m: [smallest, largest] gap used)."""
    src, gaps, in_inlet, ends = {}, set(), 0, 0
    for r in records or []:
        for v in r.get("invert_source") or []:
            src[v] = src.get(v, 0) + 1
        if r.get("enquiry_gap_m") is not None:
            gaps.add(float(r["enquiry_gap_m"]))
        for b in r.get("enquiry_in_inlet") or []:
            ends += 1
            in_inlet += int(bool(b))
    line = "{} culvert(s): inverts {}".format(
        len(records or []), ", ".join("{} {}".format(k_, v.replace("_", " "))
                                      for v, k_ in sorted(src.items())) or "none")
    if gaps:
        line += "; enquiry point {} m past the inlet ({} of {} end(s) in an inlet triangle)".format(
            "/".join("{:g}".format(g) for g in sorted(gaps)) if len(gaps) <= 2 else
            "{:g}-{:g}".format(min(gaps), max(gaps)), in_inlet, ends)
    return {"culverts": len(records or []), "invert_source": src,
            "enquiry_gap_m": [min(gaps), max(gaps)] if gaps else [], "enquiry_in_inlet": in_inlet,
            "enquiry_ends": ends, "line": line}


def mesher_mode(st):
    return str(getattr(st, "mesher", "default") or "default").strip().lower()


def rejected_line(reason):
    """The report line for a gmsh mesh that was not used."""
    return "gmsh mesh rejected: {}; used the default mesh".format(reason)


TIMEOUT_ENV = "NDBREACH_ANUGA_TIMEOUT_S"     # 3.12.2: optional wall-clock limit (none by default)


def timeout_s(st=None):
    """The ANUGA run's wall-clock limit in seconds, or None (no limit, as before): st.timeout_s
    when set (> 0), else NDBREACH_ANUGA_TIMEOUT_S."""
    for v in (getattr(st, "timeout_s", None), os.environ.get(TIMEOUT_ENV)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            return v
    return None


def worker_env(st):
    env = dict(os.environ)
    # QGIS sets these for its own Python; the ANUGA interpreter must not inherit them.
    for k in ("PYTHONHOME", "PYTHONPATH", "QGIS_PREFIX_PATH", "PYTHONNOUSERSITE"):
        env.pop(k, None)
    if st.threads:          # run() passes the resolved count (resolve_threads), never 0
        env["OMP_NUM_THREADS"] = str(int(st.threads))
    return env


def _launch(st, job, result, workdir, progress, cancel, tag=None, log=None):
    """Run the worker in the ANUGA interpreter; returns (process, log path).

    3.12.2 (DWR-2801): the worker is detached (core/anuga_detach.py). It writes anuga.log
    itself (stdout + stderr to the file, not a pipe QGIS owns), starts in its own session /
    process group, and keeps anuga_worker.json (pid, state). QGIS tails the log for PROGRESS
    lines; Cancel and the optional timeout stop it as before. When QGIS dies the worker goes
    on and writes anuga_result.npz; a later run with the same settings adopts it (run())."""
    proc, log_path, spath = detach.launch([st.python, "-u", WORKER, job, result], workdir,
                                          worker_env(st), tag=tag, script=WORKER)
    try:
        _watch(proc, log_path, st, progress, cancel, log)
    finally:
        if proc.poll() is not None:
            detach.finish_status(spath, proc.pid, proc.returncode)
    return proc, log_path


def _stop(proc, st):
    """Cancel / timeout: SIGTERM first when checkpoints are on (the worker flushes
    anuga_partial.npz; 3.10.35), STOP_GRACE_S, then kill. Windows: kill (TerminateProcess)."""
    if checkpointing(st) and os.name != "nt":
        proc.terminate()
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            pass
    proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def _watch(proc, log_path, st, progress, cancel, log=None, start_at_end=False, poll_s=0.5):
    """Follow the worker's log until it exits: PROGRESS lines -> progress(pct, "ANUGA ..."),
    cancel() -> stop and Cancelled, the optional wall-clock limit -> stop and ModelError."""
    tail = detach.LogTail(log_path, start_at_end=start_at_end)
    limit = timeout_s(st)
    dl = wallclock.Deadline(limit, what="ANUGA", log=log)

    def feed(final=False):
        for line in tail.lines(final=final):
            p = detach.parse_progress(line)
            if p is not None and progress:
                progress(p[0], "ANUGA " + p[1])

    while True:
        if cancel and cancel():      # also while ANUGA is silent (meshing, start-up)
            _stop(proc, st)
            raise Cancelled("cancelled")
        feed()
        if proc.poll() is not None:
            feed(final=True)
            return proc.returncode
        if dl.expired():
            _stop(proc, st)
            feed(final=True)
            raise ModelError("ANUGA stopped: it ran longer than the {:.0f} s limit ({}).{}".format(
                limit, TIMEOUT_ENV, "\n" + partial_note(os.path.dirname(log_path))
                if checkpointing(st) else ""))
        time.sleep(poll_s)


def _attach(w, st, progress, cancel, log=None):
    """Wait for a worker an earlier session started (worker_state() dict `w`), following its
    log like our own. Returns (handle, log path)."""
    status = w["status"] or {}
    spath = detach.status_path(w["workdir"])
    log_path = status.get("log") or os.path.join(w["workdir"], detach.LOG_NAME)
    proc = detach.Attached(status, spath)
    if log:
        log("ANUGA: waiting for the worker an earlier session started (pid {}, started {}); "
            "its log: {}".format(proc.pid, status.get("started_utc"), log_path))
    _watch(proc, log_path, st, progress, cancel, log)
    return proc, log_path


def adoption(workdir, result, run_key, reuse=False):
    """What run() does with a worker an earlier session left in `workdir` (anuga_detach):
    "wait" (running, same run key), "use" (finished, same key, not yet built into products -
    or `reuse` asked for it), "busy" (running for other settings: a second worker would fight
    it over the folder), or None (start a fresh worker as before)."""
    w = detach.worker_state(workdir, result)
    tag = ((w["status"] or {}).get("tag") or {})
    same = run_key is not None and tag.get("run_key") == run_key
    if w["state"] == detach.RUNNING:
        return ("wait" if same else "busy"), w
    if w["state"] == detach.FINISHED and same and (reuse or not w["processed"]):
        return "use", w
    return None, w


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
        elevation_read=None, elevation_native_m=None, elevation_label=None, run_key=None):
    """embankment_lines: road / rail centrelines (work CRS); with a gmsh mesher the
    pieces on an embankment become mesh lines (find_embankments). terrain_read(bounds,
    cell) reads finer ground for that test. Neither changes the default mesh.
    elevation_read(bounds, cell) -> Grid: the terrain ANUGA's bed is sampled from
    (ANUGA_ELEVATION=fine, prepare_elevation; st.elevation), elevation_native_m its own
    cell size and elevation_label its name for the report. Without it the bed is the
    model DEM `dem`, as before 3.10.31.

    run_key (3.12.2): the run's checkpoint key (workflow). It is kept in the worker's status
    file; a worker an earlier QGIS session left in `workdir` with the same key is adopted
    (adoption()): still running -> this run waits for it; finished and not yet built into
    products -> its anuga_result.npz is used like `reuse`. A worker still running for other
    settings stops this run (ModelError) rather than start a second one in the folder."""
    if not st.python or not os.path.isfile(st.python):
        raise ModelError("ANUGA Python interpreter not found: {}".format(st.python))
    os.makedirs(workdir, exist_ok=True)
    job = os.path.join(workdir, "anuga_job.npz")
    result = os.path.join(workdir, "anuga_result.npz")
    t0 = time.time()
    act, w = adoption(workdir, result, run_key, reuse=reuse)
    if act == "busy":
        stt = w["status"] or {}
        raise ModelError(
            "An ANUGA worker started {} for other settings is still running in {} (pid {}). "
            "Nothing was started. Wait for it, or stop it (it is not this run's), then run "
            "again.".format(stt.get("started_utc"), workdir, stt.get("pid")))
    if act == "wait":
        _attach(w, st, progress, cancel, log)
        act, w = adoption(workdir, result, run_key, reuse=True)
        if act != "use":
            stt = w["status"] or {}
            raise ModelError("The ANUGA worker started {} (pid {}) ended without a result "
                             "({}):\n{}".format(stt.get("started_utc"), stt.get("pid"),
                                                w["state"], _tail_text(stt.get("log") or
                                                                       os.path.join(workdir, detach.LOG_NAME))))
    if act == "use":
        reuse = True
        tb = ((w["status"] or {}).get("tag") or {}).get("buffer_m")
        if tb is not None and abs(float(tb) - float(st.buffer_m)) > 1e-6:
            st.buffer_m = float(tb)        # the mesh the adopted result was computed on
        if log:
            log("ANUGA: using the result of the worker started {} (pid {}), which finished "
                "after its QGIS session ended: {}".format(
                    (w["status"] or {}).get("started_utc"), (w["status"] or {}).get("pid"), result))
    outer, fine = mesh_polygons(dem, flooded, inflow_xy, st)
    inlet = inlet_line(inflow_xy, direction, inlet_width_m)
    try:                                        # 3.10.96: opt-in culvert bed floor
        bed_floor = structmod.bed_floor_setting()
    except ValueError as e:
        raise ModelError(str(e))
    culverts, cnotes = structmod.anuga_culverts(structures or [], dem, outer, bed_floor=bed_floor)
    for c in culverts:                          # 3.10.71: enquiry point past the inlet triangles
        c["enquiry_gap"] = enquiry_gap_m(st)
    regions = culvert_regions(culverts, st)     # small triangles at both culvert ends
    step = max(5.0, float(np.sqrt(max(st.fine_area_m2, 1.0))))
    breaklines = clip_lines_to_polygon([cond.densify(np.asarray(b, float), step)
                                        for b in (refine_lines or [])], outer)
    mesh, mnote, crest_pieces = None, None, None
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
        fp_mode = check_gmsh_options(st)           # 3.10.69: a bad value stops here
        crest_pieces, minfo["crest"] = crest_lines_for(embankment_lines, outer, dem, flooded, st)
        if log and crest_pieces:
            log("gmsh crest hold: {} road/rail crest line(s), {} km within {:g} m of the wet "
                "area, become mesh lines (NDBREACH_ANUGA_CREST=off turns it off).".format(
                    minfo["crest"]["crest_lines"], minfo["crest"]["crest_km"],
                    minfo["crest"]["pad_m"]))
        footprint = None
        if fp_mode == "wet":
            pad = float((st.gmsh or {}).get("refine_footprint_pad_m") or REFINE_PAD_M)
            footprint = wet_footprint(dem, flooded, inflow_xy, pad)
            minfo["refine_footprint"] = {"mode": "wet", "pad_m": pad,
                                         "vertices": int(len(footprint))}
        spec = gmsh_spec(dem, outer, inflow_xy, inlet, culverts, breaklines, st,
                         embankments=emb, footprint=footprint, crest_lines=crest_pieces)
        if spec.get("size_lines"):
            bo = band_options(st)
            minfo["line_band"] = {"lines": len(spec["size_lines"]),
                                  "cell_m": float(bo["band_cell_m"]),
                                  "dist_m": float(bo["band_dist_m"])}
            if log:
                log("gmsh line band: {} line(s), {:g} m triangles within {:g} m of each "
                    "(NDBREACH_ANUGA_BAND).".format(len(spec["size_lines"]),
                                                    float(bo["band_cell_m"]),
                                                    float(bo["band_dist_m"])))
        try:
            sizing = gmshmod.resolve_sizing(spec, _mesher_sizes(spec))
        except gmshmod.MeshError as e:
            raise ModelError("ANUGA gmsh sizing: {}".format(e))
        if sizing:
            minfo["sizing"] = sizing
            if log:
                log("gmsh sizing: " + sizing_line(sizing))
        no_reuse = reusable_gmsh_mesh(workdir, spec) if resume_wanted(st) else None
        mesh, msg = make_gmsh_mesh(workdir, spec, st, reuse=resume_wanted(st))
        if no_reuse and log:                       # 3.12.6
            log("gmsh: a new mesh for this resumed run ({}); a checkpoint made on another "
                "mesh is only resumed with {}.".format(no_reuse, RESUME_FROM_ENV))
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
            minfo["reused"] = bool(mesh.get("reused"))     # 3.12.6
            mnote = msg
            if log:
                log(msg)
    elif mesher != "default":
        mnote = "ANUGA_MESHER {!r} is unknown; the default ANUGA mesh was used.".format(mesher)
        minfo["rejected"] = "unknown mesher {!r}".format(mesher)
        if log:
            log("WARNING: " + mnote)
    have = bool(reuse) and os.path.exists(result)
    crest = None
    if mesh is not None and crest_pieces and not have:
        crest, cinfo = crest_hold(mesh, crest_pieces, spec, dem, structures, inlet, st,
                                  elevation_read, terrain_read, elevation_native_m)
        minfo["crest"].update(cinfo)
        if log:
            log(crest_line(minfo["crest"]))
    elif crest_pieces and mesh is None:
        minfo["crest"].update(held=0, reason="no gmsh mesh (the default mesh has no crest hold)")
    if have:                 # the result is reused: the bed is not read again
        elev_job, elev_info = None, {"mode": "reused", "reason": "earlier result reused"}
    else:
        elev_job, elev_info = prepare_elevation(workdir, outer, dem, st, elevation_read,
                                                elevation_native_m, elevation_label, log, cancel)
        if elev_job and culverts:               # 3.10.71: inverts on the bed ANUGA uses
            cnotes = list(cnotes) + structmod.fine_ground_inverts(culverts, fine_lowest_ground(
                os.path.join(workdir, elev_job["elev_fine_file"]), elev_job["elev_fine_x0"],
                elev_job["elev_fine_y0"], elev_job["elev_fine_dx"]))
            if log:
                log("ANUGA culvert inverts: " + invert_source_line(culverts))
    # 3.10.38: checkpoints on for a long run (resolve_checkpoints); the hydraulics are the same
    cst, ck_rule = resolve_checkpoints(st, outer, fine, mesh)
    # 3.10.47: a real OpenMP thread count for ANUGA (0 used to mean 1 thread); gmsh keeps
    # st.threads (gmsh_spec above), only the ANUGA job and its process get the resolved count
    cst.threads, thr_rule = resolve_threads(st.threads)
    if log:
        log("ANUGA threads: {} ({}).".format(cst.threads, thr_rule))
    write_job(job, dem, outer, fine, inlet, hydrograph, cst, dem, initial_depth, culverts,
              breaklines, regions, mesh=mesh, elevation=elev_job, crest=crest)
    np.savetxt(os.path.join(workdir, "mesh_boundary.csv"), outer, delimiter=",",
               header="x,y", comments="", fmt="%.3f")
    if len(fine):
        np.savetxt(os.path.join(workdir, "mesh_fine_area.csv"), fine, delimiter=",",
                   header="x,y", comments="", fmt="%.3f")
    if os.path.exists(result) and not have:
        os.remove(result)
    if have:
        log_path = os.path.join(workdir, "anuga.log")

        class _Done:
            returncode = 0
        proc = _Done()
    else:
        from ..damsuite.keepawake import keep_awake
        with keep_awake("ANUGA", log=log):
            proc, log_path = _launch(cst, job, result, workdir, progress, cancel,
                                     tag={"run_key": run_key, "buffer_m": float(st.buffer_m)},
                                     log=log)
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
    detach.mark_processed(workdir)           # 3.12.2: no longer pending for the window
    if not have and culverts:            # 3.10.71: the inverts the operators got, and why
        rec = structmod.invert_record(culverts)
        for r_, w_ in zip(rec, stats.get("culvert_enquiry") or []):
            r_.update(w_)
        stats["culvert_inverts"] = rec
        ov = stats.get("operator_volumes") or []                       # 3.10.96
        for r_ in rec:
            k_ = r_.get("op_index")
            if k_ is not None and 0 <= k_ < len(ov):
                r_["operator_volume_m3"] = {k: ov[k_][k] for k in ("removed_m3", "added_m3",
                                                                   "net_m3", "calls")}
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
    for e in sorted({w.get("enquiry_note") for w in stats.get("culvert_enquiry") or []
                     if w.get("enquiry_note")}):
        notes.append("ANUGA culvert enquiry point: " + e)   # 3.10.71
    notes.extend(culvert_bed_notes(stats))                  # 3.10.96
    if refine_lines and not stats.get("breaklines_used") and breaklines and mesh is None:
        notes.append("ANUGA: the embankment lines could not be used as mesh edges (they cross or "
                     "touch each other); the mesh was made without them.")
    return Envelopes(dem.like(depth, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                     dem.like(wse, np.nan), stats, notes), outer, fine


FT_M = 0.3048                 # 3.11.1: the culvert bed notes give feet too
PHANTOM_HEAD_M = 0.3          # 3.10.96: an invert this far below its enquiry bed
OPERATOR_IMBALANCE = 0.001    # |added - removed| / removed above this: the operator made water


def culvert_bed_notes(stats):
    """3.10.96 (DWR-3034 REVIEW 9.8 items 3-4): notes from the worker's per-end bed record
    (stats['culvert_enquiry'][k]['ends']) and the per-operator volumes
    (stats['operator_volumes']). An invert well below the bed the operator reads its head on
    gives head that is not there (stage - invert on a dry end); NDBREACH_CULVERT_BED_FLOOR
    =enquiry|channel raises it. An operator that adds more than it removes made water."""
    out = []
    names = {v.get("op"): v.get("name") for v in stats.get("operator_volumes") or []}
    for k, w in enumerate(stats.get("culvert_enquiry") or []):
        name = names.get(w.get("op_index")) or "culvert {}".format(k + 1)
        for j, e in enumerate(w.get("ends") or []):
            inv, bed = e.get("invert_used_m"), e.get("enquiry_bed_m")
            side = "upstream" if j == 0 else "downstream"
            if e.get("raised_m"):
                out.append("ANUGA culvert {}: {} invert raised {:.2f} m ({:.1f} ft) to the {} "
                           "bed ({:.2f} m = {:.2f} ft; NDBREACH_CULVERT_BED_FLOOR).".format(
                               name, side, e["raised_m"], e["raised_m"] / FT_M,
                               e.get("floor_source"), e.get("floor_m"),
                               float(e.get("floor_m") or 0.0) / FT_M))
            elif inv is not None and bed is not None and inv < bed - PHANTOM_HEAD_M:
                out.append("ANUGA culvert {}: the {} invert ({:.2f} m = {:.2f} ft) is {:.2f} m "
                           "({:.1f} ft) below the bed the operator reads its head on ({:.2f} m "
                           "= {:.2f} ft): it sees that much water on a dry end. Check the "
                           "invert; NDBREACH_CULVERT_BED_FLOOR=enquiry or channel raises it."
                           .format(name, side, inv, inv / FT_M, bed - inv, (bed - inv) / FT_M,
                                   bed, bed / FT_M))
    for v in stats.get("operator_volumes") or []:
        rem, add = float(v.get("removed_m3") or 0.0), float(v.get("added_m3") or 0.0)
        if rem > 1.0 and abs(add - rem) > OPERATOR_IMBALANCE * rem:
            out.append("ANUGA culvert {}: the operator added {:,.0f} m3 and removed {:,.0f} m3 "
                       "({:+.2%}).".format(v.get("name") or v.get("op"), add, rem,
                                           (add - rem) / rem))
    return out


VOLUME_GAIN_LIMIT = 1.02       # 3.10.55: more water in the domain than this x the supply
VOLUME_CHECK_LIMIT = 1.005     # a softer "check" line above this (a creeping gain)
SOLVER_RESIDUAL_LIMIT = 0.005  # |ANUGA conservation residual| / supply above this: check
POSITIVE_EDGE_INFLOW = 0.001   # net edge inflow / supply above this with outflow-only edges


def structures_net(stats):
    """3.11.1: the culvert / bridge operators' net volume (added - removed, m3) from
    stats['operator_volumes'] (3.10.96, one cumulative row per operator), 0.0 when the run
    had no culvert operator, None when it had some but their volumes were not logged."""
    rows = stats.get("operator_volumes")
    if rows:
        last = {}
        for r in rows:
            last[r.get("op")] = r
        return float(sum(float(r.get("added_m3") or 0.0) - float(r.get("removed_m3") or 0.0)
                         for r in last.values()))
    if not stats.get("culverts"):
        return 0.0
    return None


def operator_share(stats, vb):
    """3.10.95: how much of a volume gain the inflow operators booked. ANUGA's fractional-step
    volume integral ('operators_m3') minus the hydrograph volume that entered ('injected_m3')
    = the operators' integration excess, and its share of the gain (in domain - supplied).
    3.11.1 (#209 wording): the integral covers EVERY fractional-step operator - the breach
    inlet and any culvert / bridge operators (which should net zero but need not). With
    their net known (structures_net) it is split off: 'inlet_excess_m3' is the inlet's own
    part, 'structures_net_m3' the culverts'; without it 'structures_net_m3' is None and the
    excess may be either. None without both numbers or without a gain."""
    ops, inj = vb.get("operators_m3"), stats.get("injected_m3")
    if ops is None or inj is None:
        return None
    extra = float(vb.get("in_domain_m3") or 0.0) - float(vb.get("supplied_m3") or 0.0)
    if extra <= 0:
        return None
    ex = float(ops) - float(inj)
    st = structures_net(stats)
    return {"operators_m3": float(ops), "injected_m3": float(inj), "excess_m3": ex,
            "share": ex / extra, "structures_net_m3": st,
            "inlet_excess_m3": None if st is None else ex - st}


def step_cap_used(vb):
    """3.11.1: True when the run held ANUGA's step at the first-step inflow cap (Runner
    3.10.94, volume_balance['inflow_step_cap']['applied']), False when the record says it
    was off, None for a run without the record (before 3.10.94)."""
    cap = vb.get("inflow_step_cap")
    if not isinstance(cap, dict):
        return None
    return bool(cap.get("applied"))


def first_step_cause(vb):
    """3.10.95: a clause naming a long first ANUGA step as the cause ('' when it is not), from
    the worker's volume_balance['first_step'] (anuga_worker.FirstStep)."""
    f = vb.get("first_step") or {}
    if f.get("dt_s") is None or not f.get("excess_m3") or f["excess_m3"] <= 0:
        return ""
    return ("ANUGA's first internal step on the dry domain was one {:,.0f} s step (no CFL limit "
            "without water), and the inlet's 0.5*(Q(t)+Q(t+dt))*dt booked {:,.0f} m3 for the "
            "{:,.0f} m3 the hydrograph delivers in that time: {:,.0f} m3 extra".format(
                f["dt_s"], f["inlet_booked_m3"], f["hydrograph_m3"], f["excess_m3"]))


def volume_note(stats, limit=VOLUME_GAIN_LIMIT):
    """3.10.55: a note when the water balance looks wrong. Fills stats['volume_balance'] for
    results written before 3.10.55. None when the balance is fine or unknown.
    - ratio (in domain / initial water plus breach hydrograph) > `limit`: a gain. The cause is
      named only as far as the numbers show it: ANUGA's net boundary flux integral says
      whether the extra water came in through the edge; without it the edge is the likely
      cause (old transmissive edges). 3.10.95 (DWR-3034): when the inflow operators'
      fractional-step integral holds most of the gain, the note says so (operator_share) and
      names a long first step (first_step_cause) instead of the edge or an instability; an
      instability is named only by the solver-residual line.
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
        share = operator_share(stats, vb)
        fs = first_step_cause(vb)
        resid_big = (vb.get("solver_residual_frac") is not None
                     and abs(vb["solver_residual_frac"]) > SOLVER_RESIDUAL_LIMIT)
        edge = False
        if share is not None and share["excess_m3"] > 0.5 * extra:
            # 3.10.95 (DWR-3034 REVIEW 9.8 item 2): the gain is in the inflow operators'
            # fractional-step integral, not the edge
            # 3.11.1 (#209): the integral is every operator's; the inlet is named alone only
            # when the culvert operators' net is known and small
            stn = share["structures_net_m3"]
            inlet_only = stn is not None and abs(stn) <= 0.1 * abs(share["excess_m3"])
            if inlet_only:
                why = ("the breach inlet added it: ANUGA's operators (fractional-step "
                       "integral) added {:,.0f} m3 against the hydrograph's {:,.0f} m3, so "
                       "{:,.0f} m3 = {:.0%} of the gain is the inlet's own time "
                       "integration".format(share["operators_m3"], share["injected_m3"],
                                            share["excess_m3"], share["share"]))
                if stn:
                    why += " (culvert operators: {:+,.0f} m3 net)".format(stn)
            else:
                why = ("ANUGA's operators added it: their fractional-step integral (the breach "
                       "inlet{}) added {:,.0f} m3 against the hydrograph's {:,.0f} m3, so "
                       "{:,.0f} m3 = {:.0%} of the gain is operator water".format(
                           " and the culvert / bridge operators" if stn is None or stn else "",
                           share["operators_m3"], share["injected_m3"], share["excess_m3"],
                           share["share"]))
                if stn is None:
                    why += (" (the culvert operators' own net was not logged, so the inlet's "
                            "part is not known)")
                else:
                    why += (" ({:,.0f} m3 from the breach inlet, {:+,.0f} m3 net from the "
                            "culvert / bridge operators)".format(share["inlet_excess_m3"], stn))
            if bi is not None:
                why += " (edge: {:,.0f} m3 net)".format(bi)
            if fs:
                why += "; " + fs
            why += ". This is extra water in the domain, so the depths are conservative, not " \
                   "an edge or stability problem"
            used = step_cap_used(vb)
            inlet_part = share["excess_m3"] if inlet_only else share.get("inlet_excess_m3")
            if used:
                why += ("; the first-step inflow cap (Runner 3.10.94) was on in this run, so a "
                        "rerun with it would not change this")
            elif inlet_part is None or inlet_part > 0.5 * share["excess_m3"]:
                why += ("; a run with the first-step inflow cap (Runner 3.10.94; it was {}) "
                        "should remove the inlet's part".format(
                            "off in this run" if used is False else
                            "not recorded for this run"))
        elif bi is not None and bi > 0.5 * extra:
            edge = True
            why = ("ANUGA's boundary flux integral shows {:,.0f} m3 net inflow through the mesh "
                   "edge ({} boundary), so the extra water came in there".format(bi, kind))
        elif bi is not None:
            why = ("ANUGA's boundary flux integral shows only {:,.0f} m3 net through the edge, so "
                   "the gain is probably not from the edge".format(bi))
            if share is not None:
                why += ("; the inflow operators account for {:,.0f} m3 ({:.0%})".format(
                    share["excess_m3"], share["share"]))
            if fs:
                why += "; " + fs
            why += ("; ANUGA's own balance is off too (see below)" if resid_big else
                    "; check the inflow")
        else:
            edge = True
            why = "it most likely came in through the mesh edge ({} boundary)".format(kind)
            if fs:
                why += ", unless the first step explains it: " + fs
        out.append("ANUGA volume check: the domain ends with {:.2f} x the water that went in "
                   "({:,.0f} m3 = {:,.0f} acre-ft more than the {:,.0f} m3 of initial water and "
                   "breach inflow); {}.{}".format(
                       r, extra, extra / 1233.48184, vb["supplied_m3"], why,
                       " Treat the depths near the mesh edge with caution until this is "
                       "resolved." if edge else ""))
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
