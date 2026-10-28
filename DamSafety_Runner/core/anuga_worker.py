"""ANUGA worker: runs in the separate ANUGA Python environment, not inside
QGIS. It only needs numpy, scipy, matplotlib and anuga.

    python anuga_worker.py job.npz result.npz

job.npz holds (all SI, coordinates in the model CRS):
  dem, dem_x0, dem_y0, dem_dx          model DEM (row 0 = top)
  manning (optional grid) or manning_n
  boundary        (N, 2) outer polygon of the mesh
  fine            (M, 2) polygon for the refined near-dam area (may be empty)
  fine_area, coarse_area               maximum triangle areas, m2
  inlet           (2, 2) line across the flow just below the dam
  hyd_t, hyd_q    breach hydrograph, s and m3/s
  sim_time, yieldstep, arrival_depth, velocity_min_depth, algorithm, threads
                  (threads: ANUGA's OpenMP thread count, set with set_omp_num_threads and
                  OMP_NUM_THREADS; 0 = ANUGA's own default, which is ONE thread in ANUGA 4.
                  From 3.10.47 the Runner always writes a count > 0, resolve_threads)
  out_x0, out_y0, out_dx, out_nx, out_ny   grid the results are written on
optional:
  init_depth      grid like dem: initial water depth (m), ponds that start full
  culverts_json   list of culvert specs (end_points, invert_elevations, width,
                  height, barrels, shape, manning, losses, apron)
  breaklines_json list of polylines that become mesh edges (embankment crests)
  regions_json    extra refined polygons [{"poly": [[x, y]...], "area": m2}]
  mesh_points, mesh_triangles, mesh_boundary   ready-made mesh (ANUGA_MESHER=gmsh):
                  model-CRS nodes, CCW triangles, [triangle, edge] outer edges;
                  used with anuga.Domain instead of create_domain_from_regions
  elev_fine_file, elev_fine_x0, elev_fine_y0, elev_fine_dx   (3.10.31) a .npy float32
                  grid next to the job file (row 0 = top, NaN = no data) of the finest
                  terrain: the elevation is sampled from it at the triangle vertices
                  (sample_fine); the model DEM fills its gaps. Without it the elevation
                  is the model DEM at the centroids, as before.

  (3.10.35; absent = no checkpoints, no partial file, as before. From 3.10.38
  anuga_model.resolve_checkpoints adds them for a long run; results never change)
  ckpt_every_min  wall-clock minutes between ANUGA checkpoints (domain.set_checkpointing
                  checkpoint_time); ckpt_every_steps = every that many yield steps instead
                  (used when ckpt_every_min is 0). 0 / absent = no checkpoints.
  ckpt_keep       newest checkpoints kept (default 2; older pickles are deleted)
  ckpt_dir        folder next to the job (default "anuga_checkpoints"); the pickles go in
                  <ckpt_dir>/<job fingerprint>/ with checkpoint.json listing the complete ones
  partial_every_min  wall-clock minutes between flushes of anuga_partial.npz (the max
                  depth / velocity / WSE and arrival grids so far, written atomically); also
                  flushed after every checkpoint and when the worker gets SIGTERM
  resume          1 = continue from the newest complete checkpoint of the SAME job (same
                  fingerprint), envelope (max depth / velocity / WSE, arrival) included;
                  no usable checkpoint = a fresh start, said in stats["checkpoint"]

  (3.10.70; absent = off. Never part of the checkpoint fingerprint; results never change)
  dt_diag_every   yield steps between rows of timestep_diag.csv (core/anuga_dtdiag.py: the
                  internal dt and the triangle limiting it); NDBREACH_ANUGA_DT_DIAG=0|1 and
                  NDBREACH_ANUGA_DT_DIAG_EVERY=N in the worker's environment win over it
  dt_diag_crs     the model CRS (WKT) for the rows' lon / lat

result.npz: depth, velocity, arrival_hr, wse (NaN = dry / outside), plus
stats as a JSON string. Progress goes to stdout as lines starting "PROGRESS".
From 3.10.70 each yield step's line ends with utc=<wall clock> steps=<internal steps so far
in this process> dsteps=<since the last yield> wall_s=<wall seconds since the last yield>
rate=<model s per wall s> (appended; "PROGRESS <pct> t=<s>s" is unchanged).
"""
import hashlib
import inspect
import json
import os
import pickle
import shutil
import signal
import sys
import time

import numpy as np

try:                    # 3.10.50: the worker runs as a script (python anuga_worker.py ...)
    from . import omp_probe
    from . import anuga_dtdiag
except ImportError:
    import omp_probe
    import anuga_dtdiag


def crest_vertex_values(vertex_xy, vertex_values, crest_xy, crest_z, tol=1e-3):
    """3.10.91 (core/anuga_crest.py, numpy only): vertex_values (T, 3) of the bed with every vertex at a
    crest node (matched by coordinates within tol, vertex_xy (3T, 2) in the same frame as
    crest_xy) raised to its crest z (never lowered). Returns (new values, info)."""
    vv = np.array(vertex_values, float, copy=True)
    flat = vv.reshape(-1)
    vxy = np.asarray(vertex_xy, float).reshape(-1, 2)
    cxy = np.asarray(crest_xy, float).reshape(-1, 2)
    cz = np.asarray(crest_z, float).ravel()
    info = {"crest_nodes": int(len(cz)), "vertices_matched": 0, "vertices_raised": 0,
            "max_raise_m": 0.0, "mean_raise_m": 0.0}
    if not len(cz) or not len(flat):
        return vv, info
    key = lambda a: [tuple(r) for r in np.round(np.asarray(a) / tol).astype(np.int64)]
    zmap = {}
    for k, z in zip(key(cxy), cz):
        zmap[k] = max(z, zmap.get(k, -np.inf))
    vk = key(vxy)
    hit = np.array([k in zmap for k in vk], bool)
    if not hit.any():
        return vv, info
    zt = np.array([zmap[k] for k, h in zip(vk, hit) if h], float)
    old = flat[hit]
    with np.errstate(invalid="ignore"):
        new = np.where(np.isfinite(old), np.maximum(old, zt), zt)
    raise_ = new - np.where(np.isfinite(old), old, new)
    flat[hit] = new
    info.update(vertices_matched=int(hit.sum()), vertices_raised=int((raise_ > 1e-9).sum()),
                max_raise_m=round(float(raise_.max()), 3),
                mean_raise_m=round(float(raise_.mean()), 3))
    return vv, info


def bilinear(dem, x0, y0, dx, x, y):
    a = dem
    ny, nx = a.shape
    fc = (x - x0) / dx - 0.5
    fr = (y0 - y) / dx - 0.5
    c0 = np.clip(np.floor(fc).astype(int), 0, nx - 2)
    r0 = np.clip(np.floor(fr).astype(int), 0, ny - 2)
    tc = np.clip(fc - c0, 0.0, 1.0)
    tr = np.clip(fr - r0, 0.0, 1.0)
    v = (a[r0, c0] * (1 - tc) * (1 - tr) + a[r0, c0 + 1] * tc * (1 - tr)
         + a[r0 + 1, c0] * (1 - tc) * tr + a[r0 + 1, c0 + 1] * tc * tr)
    return v


def sample_fine(arr, x0, y0, dx, x, y, chunk=1000000):
    """Bilinear samples of a (possibly memory-mapped) grid at model-CRS points (x0 / y0 =
    left / top edge). NaN where the point is off the grid or all four cells are no data;
    where only some are, the valid ones are blended. Read in chunks, so a memory-mapped
    grid of any size is touched only at the cells needed."""
    x = np.asarray(x, float).ravel()
    y = np.asarray(y, float).ravel()
    ny, nx = arr.shape
    out = np.full(x.shape, np.nan)
    for i in range(0, len(x), chunk):
        xs, ys = x[i:i + chunk], y[i:i + chunk]
        fc = (xs - x0) / dx - 0.5
        fr = (y0 - ys) / dx - 0.5
        inside = (fc > -1.0) & (fr > -1.0) & (fc < nx) & (fr < ny)
        c0 = np.clip(np.floor(fc).astype(np.int64), 0, max(nx - 2, 0))
        r0 = np.clip(np.floor(fr).astype(np.int64), 0, max(ny - 2, 0))
        c1 = np.minimum(c0 + 1, nx - 1)
        r1 = np.minimum(r0 + 1, ny - 1)
        tc = np.clip(fc - c0, 0.0, 1.0)
        tr = np.clip(fr - r0, 0.0, 1.0)
        acc = np.zeros(len(xs))
        wsum = np.zeros(len(xs))
        for rr, cc, w in ((r0, c0, (1 - tc) * (1 - tr)), (r0, c1, tc * (1 - tr)),
                          (r1, c0, (1 - tc) * tr), (r1, c1, tc * tr)):
            v = np.asarray(arr[rr, cc], float)
            ok = np.isfinite(v)
            acc[ok] += v[ok] * w[ok]
            wsum[ok] += w[ok]
        good = inside & (wsum > 1e-9)
        res = np.full(len(xs), np.nan)
        res[good] = acc[good] / wsum[good]
        out[i:i + chunk] = res
    return out


def accepts_keyword(fn, name):
    """True when `fn` names `name` among its parameters (ANUGA 3.x's
    create_domain_from_regions has mesh_filename; ANUGA 4 does not)."""
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def enquiry_points(ends, apron, gap):
    """Where ANUGA's structure operator puts the two enquiry points of a straight culvert:
    end i + (2i - 1) (apron + enquiry_gap) u, u the unit vector from end 0 to end 1."""
    e = np.asarray(ends, float)
    u = e[1] - e[0]
    u = u / max(float(np.hypot(u[0], u[1])), 1e-12)
    d = float(apron) + float(gap)
    return [(e[0] - d * u).tolist(), (e[1] + d * u).tolist()]


def enquiry_point_in_mesh(domain, pt):
    """The two checks ANUGA's Inlet_enquiry makes before it accepts an enquiry point."""
    try:
        try:
            from anuga.geometry.polygon import is_inside_polygon
        except ImportError:
            is_inside_polygon = point_in_polygon
        if not is_inside_polygon(pt, domain.get_boundary_polygon()):
            return False
        domain.get_triangle_containing_point(pt)
        return True
    except Exception:
        return False


def point_in_polygon(pt, poly):
    """Even-odd test (no ANUGA: the unit tests)."""
    x, y = float(pt[0]), float(pt[1])
    p = np.asarray(poly, float)
    inside = False
    for (x1, y1), (x2, y2) in zip(p, np.roll(p, -1, axis=0)):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


ENQUIRY_CLEAR_M = 0.5      # how far past the last inlet-triangle vertex the point goes


def inlet_clear_gap(vertices, end, outward, width, apron, margin=ENQUIRY_CLEAR_M):
    """The smallest enquiry gap that puts the enquiry point past every triangle that could
    be in the inlet region at `end` - ANUGA's inlet is the width x apron rectangle beyond
    the end (triangles with a centroid inside or an edge crossing it), so every triangle
    that overlaps the rectangle is taken (a superset) and the point goes `margin` beyond the
    furthest vertex of those along `outward`. vertices: (n, 3, 2) triangle corners (the
    domain's coordinates). Returns the gap (m, >= 0) or None (no triangle overlaps)."""
    v = np.asarray(vertices, float)
    u = np.asarray(outward, float)
    u = u / max(float(np.hypot(u[0], u[1])), 1e-12)
    n = np.array([-u[1], u[0]])
    d = v - np.asarray(end, float)
    s = d @ u                                       # along, 0..apron is the inlet
    t = d @ n                                       # across, +-width/2
    a, hw = max(float(apron), 1e-6), 0.5 * float(width)
    hit = (s.max(1) >= 0) & (s.min(1) <= a) & (t.max(1) >= -hw) & (t.min(1) <= hw)
    if not hit.any():
        return None
    s, t = s[hit], t[hit]
    rect = np.array([[0, -hw], [a, -hw], [a, hw], [0, hw]], float)
    ok = np.ones(len(s), bool)
    for k in range(3):                              # the triangle edges as separating axes
        es, et = s[:, (k + 1) % 3] - s[:, k], t[:, (k + 1) % 3] - t[:, k]
        ns, nt = -et, es
        tri = s * ns[:, None] + t * nt[:, None]
        rp = rect[:, 0][None, :] * ns[:, None] + rect[:, 1][None, :] * nt[:, None]
        ok &= ~((tri.max(1) < rp.min(1)) | (rp.max(1) < tri.min(1)))
    if not ok.any():
        return None
    return max(0.0, float(s[ok].max()) - a + float(margin))


def local_enquiry_gap(domain, ends, width, apron, gap):
    """3.10.71: the job's gap (one triangle edge at the culvert-end size), raised where the
    triangles at an end are larger than that (a mesh line through a merged culvert region,
    ...) to inlet_clear_gap of that end. Returns (gap, raised: bool); the job's gap when the
    triangles cannot be read."""
    try:
        v = np.asarray(domain.get_vertex_coordinates(absolute=True), float).reshape(-1, 3, 2)
    except Exception:           # noqa: BLE001
        return gap, False
    e = np.asarray(ends, float)
    u = e[1] - e[0]
    need = 0.0
    for end, out in ((e[0], -u), (e[1], u)):
        g = inlet_clear_gap(v, end, out, width, apron)
        if g is not None:
            need = max(need, g)
    need = round(need, 2)
    return (need, True) if need > gap else (gap, False)


def add_boyd_culvert(anuga, domain, c, ends):
    """3.10.71: one job culvert as a Boyd box / pipe operator. As before, plus the enquiry
    gap (job key 'enquiry_gap', m): the head is read one triangle edge beyond the inlet
    region instead of at its edge, where the enquiry triangle was usually an inlet triangle.
    The gap is passed only to an ANUGA whose operator has `enquiry_gap` (others: as before,
    noted), and only when both enquiry points are inside the mesh (else 0, noted); a failed
    operator is never retried, since a half-built one stays registered in the domain.
    Returns (operator, record) - record: enquiry_gap_m used, enquiry_in_inlet per end,
    enquiry_note."""
    common = dict(losses=float(c["losses"]), barrels=float(c["barrels"]), end_points=ends,
                  invert_elevations=[float(v) for v in c["invert_elevations"]],
                  apron=float(c["apron"]), manning=float(c["manning"]),
                  use_momentum_jet=True, use_velocity_head=False, verbose=False)
    if c.get("shape") == "pipe":
        fn, size = anuga.Boyd_pipe_operator, dict(diameter=float(c["width"]))
    else:
        fn, size = anuga.Boyd_box_operator, dict(width=float(c["width"]),
                                                 height=float(c["height"]))
    gap = float(c.get("enquiry_gap") or 0.0)
    note = None
    raised = False
    if gap > 0.0 and accepts_keyword(fn, "enquiry_gap"):
        width = float(c["width"])
        g2, raised = local_enquiry_gap(domain, ends, width, common["apron"], gap)
        if raised and all(enquiry_point_in_mesh(domain, p) for p in
                          enquiry_points(ends, common["apron"], g2)):
            note = ("{}: triangles at an end are larger than the culvert-end size; enquiry "
                    "gap {:.1f} m instead of {:.1f} m".format(c.get("name") or "culvert",
                                                              g2, gap))
            gap = g2
        else:
            raised = False
    if gap > 0.0:
        if not accepts_keyword(fn, "enquiry_gap"):
            note = ("this ANUGA's culvert operator has no enquiry_gap; the head is read at the "
                    "inlet edge as before")
            gap = 0.0
        elif not all(enquiry_point_in_mesh(domain, p) for p in
                     enquiry_points(ends, common["apron"], gap)):
            note = ("{}: an enquiry point {:.1f} m past the inlet is outside the mesh; read at "
                    "the inlet edge as before".format(c.get("name") or "culvert", gap))
            gap = 0.0
        else:
            common["enquiry_gap"] = gap
    op = fn(domain, **size, **common)
    in_inlet = None
    try:
        in_inlet = [bool(int(i.enquiry_index) in set(np.asarray(i.triangle_indices).tolist()))
                    for i in op.inlets]
    except Exception:       # noqa: BLE001 - reporting only
        pass
    rec = {"enquiry_gap_m": gap, "enquiry_in_inlet": in_inlet}
    if note:
        rec["enquiry_note"] = note
    return op, rec


def fill_nan(a):
    """Replace NaN with the nearest valid value so the mesh never sees no-data."""
    from scipy.ndimage import distance_transform_edt
    bad = ~np.isfinite(a)
    if not bad.any():
        return a
    idx = distance_transform_edt(bad, return_distances=False, return_indices=True)
    return a[tuple(idx)]


# ------------------------------------------------------------- 3.10.35 checkpointing
DOMAIN_NAME = "anuga_breach"
CKPT_KEYS = ("ckpt_every_min", "ckpt_every_steps", "ckpt_keep", "ckpt_dir",
             "partial_every_min", "resume")
# not part of the fingerprint: the thread count and gmsh's own report (timings) do not
# change the model, so a run can resume with another thread count
# 3.10.70: nor the read-only time-step diagnostic (a run resumes with it turned on or off)
FINGERPRINT_SKIP = CKPT_KEYS + ("threads", "mesh_stats") + anuga_dtdiag.DT_DIAG_KEYS
PARTIAL_NAME = "anuga_partial.npz"
META_NAME = "checkpoint.json"


class Hydrograph:
    """The inlet's Q(t) (the same np.interp as before); a module-level class, not a
    lambda, so ANUGA can pickle the domain for a checkpoint."""

    def __init__(self, t, q):
        self.t = np.asarray(t, float)
        self.q = np.asarray(q, float)

    def __call__(self, t):
        return float(np.interp(t, self.t, self.q, right=0.0))


INFLOW_MAX_DT_ENV = "NDBREACH_ANUGA_INFLOW_MAX_DT"
INFLOW_MAX_DT_FLOOR_S = 0.5     # never cap below this (a very finely sampled hydrograph)
INFLOW_MAX_DT_PER_DURATION = 100   # and at most 1/100 of the hydrograph's duration


def inflow_max_timestep(hyd_t, hyd_q, yieldstep, environ=None):
    """3.10.94 (DWR-3034 REVIEW 9.2, G5): the largest ANUGA time step allowed while the breach
    hydrograph is still flowing, or None (no cap).

    On a dry domain no CFL limit applies, so ANUGA's first step is the whole yield step (300 s
    on DWR-3034). Inlet_operator adds 0.5*(Q(t)+Q(t+dt))*dt per step, so one 300 s step over
    the rising limb booked 0.5*Q(300 s)*300 = 9,994 m3 against the hydrograph's true 5,929 m3
    (+4,065 m3, G5 ratio 1.0461). Capping the step at the hydrograph's own sampling interval
    makes the inlet integrate the hydrograph as sampled.

    Cap = min(median positive hyd_t interval, duration / 100, yieldstep), not below 0.5 s.
    NDBREACH_ANUGA_INFLOW_MAX_DT: a number of seconds overrides it; 0 / off = no cap (the
    pre-3.10.94 behaviour)."""
    env = os.environ if environ is None else environ
    raw = str(env.get(INFLOW_MAX_DT_ENV, "") or "").strip().lower()
    if raw in ("0", "off", "false", "no", "none"):
        return None
    if raw:
        try:
            v = float(raw)
        except ValueError:
            v = None
        if v is not None:
            return v if v > 0 else None
    t = np.asarray(hyd_t, float).ravel()
    q = np.asarray(hyd_q, float).ravel()
    if t.size < 2 or not np.any(q > 0):
        return None
    d = np.diff(t)
    d = d[d > 0]
    if not d.size:
        return None
    cap = min(float(np.median(d)), float(t[-1] - t[0]) / INFLOW_MAX_DT_PER_DURATION)
    if yieldstep and yieldstep > 0:
        cap = min(cap, float(yieldstep))
    return max(cap, INFLOW_MAX_DT_FLOOR_S)


def inflow_end_time(hyd_t, hyd_q):
    """The time after which the hydrograph adds no more water (its last Q > 0 point's next
    sample; np.interp(right=0) gives 0 after hyd_t[-1])."""
    t = np.asarray(hyd_t, float).ravel()
    q = np.asarray(hyd_q, float).ravel()
    nz = np.nonzero(q > 0)[0]
    if not nz.size:
        return float(t[0]) if t.size else 0.0
    return float(t[min(int(nz[-1]) + 1, t.size - 1)])


class InflowStepCap:
    """3.10.94: holds domain.evolve_max_timestep at `cap` while the hydrograph flows and gives
    the old limit back at the first yield step at or after its end (so the long recession
    tail is not slowed). ANUGA's set_evolve_max_timestep only ever lowers the limit, so the
    attribute is set directly. A resumed checkpoint carries the capped value; update() at the
    first yield after the resume releases it when the hydrograph has ended."""

    def __init__(self, domain, cap, t_end):
        self.cap, self.t_end = cap, float(t_end)
        self.old = getattr(domain, "evolve_max_timestep", None)
        self.active = False
        self.released_t = None
        if cap is not None and float(domain.get_time()) < self.t_end:
            if self.old is None or cap < float(self.old):
                domain.evolve_max_timestep = float(cap)
                self.active = True

    def update(self, domain, t):
        if self.active and float(t) >= self.t_end:
            if self.old is not None:
                domain.evolve_max_timestep = self.old
            self.active = False
            self.released_t = float(t)

    def record(self):
        return {"max_timestep_s": None if self.cap is None else round(float(self.cap), 4),
                "applied": self.cap is not None and (self.active or self.released_t is not None),
                "hydrograph_end_s": round(self.t_end, 3), "released_at_s": self.released_t}


class Constant:
    """A constant function of time (picklable; ANUGA turns a plain number into a lambda)."""

    def __init__(self, value):
        self.value = float(value)

    def __call__(self, t):
        return self.value


def envelope_update(stage, elev, xm, ym, hmax, vmax, wmax, arr, vmin_depth, arrival_depth,
                    get_time):
    """One time step of the Envelope operator, in place: max depth, max speed (where at
    least vmin_depth deep), max wet WSE, first time (h) at arrival_depth. 3.10.47: no full
    np.where / zeros arrays and the speed only on the deep triangles (the same numbers as
    before, bit for bit: tests/test_anuga_threads.py); serial numpy, ~20 % of a step at 8
    OpenMP threads before, most of what is left is np.hypot."""
    h = stage - elev
    np.maximum(hmax, h, out=hmax)
    np.maximum(wmax, stage, out=wmax, where=h > 0.001)
    idx = np.flatnonzero(h >= vmin_depth)
    if idx.size:
        sp = np.hypot(xm[idx], ym[idx])
        sp /= h[idx]
        vmax[idx] = np.maximum(vmax[idx], sp)
    new = np.isnan(arr)
    new &= h >= arrival_depth
    if new.any():
        arr[new] = get_time() / 3600.0


def envelope_class(anuga):
    """The Envelope operator class (needs anuga.Operator, imported only in the ANUGA
    Python). Defined once and bound at module level under its own name so that a pickled
    domain (checkpoint) refers to it by name and loads again."""
    cls = globals().get("Envelope")
    if cls is not None and issubclass(cls, anuga.Operator):
        return cls

    class Envelope(anuga.Operator):
        """Updates the maxima every time step, not just at yield steps."""

        def __init__(self, dom, arrival_depth, vmin_depth):
            anuga.Operator.__init__(self, dom, description="envelope", label="envelope")
            n = len(dom)
            self.arrival_depth = arrival_depth
            self.vmin_depth = vmin_depth
            self.hmax = np.zeros(n)
            self.vmax = np.zeros(n)
            self.wmax = np.full(n, -np.inf)
            self.arr = np.full(n, np.nan)
            self.xm = dom.quantities["xmomentum"].centroid_values
            self.ym = dom.quantities["ymomentum"].centroid_values

        def __call__(self):
            envelope_update(self.stage_c, self.elev_c, self.xm, self.ym, self.hmax,
                            self.vmax, self.wmax, self.arr, self.vmin_depth,
                            self.arrival_depth, self.domain.get_time)

        def parallel_safe(self):
            return True

        def statistics(self):
            return "Envelope operator"

        def timestepping_statistics(self):
            return ""

    Envelope.__module__ = __name__
    Envelope.__qualname__ = "Envelope"
    globals()["Envelope"] = Envelope
    return Envelope


# 3.10.55: the outer boundary. Until 3.10.54 every outer edge was anuga.Transmissive_boundary:
# the edge copies the stage AND the momentum of the triangle inside it, so the flux through the
# edge is the interior's own flux - inward wherever the water next to the edge moves inward
# (a pond against a clipped edge, an eddy, flow running along the edge). That edge then feeds
# water in at the interior depth: the NDDOT culvert run (2026-10-21, mesh clipped by the
# LISFLOOD-FP grid at its SE corner) ended with 4.15 x the breach volume in the domain (the
# 2026-10-15 structures run 1.51 x) and a 10 ft pond along the clipped edge.
# 'outflow' (default): transmissive where the water leaves; where its normal momentum points
# inward, the normal component is mirrored (as Reflective_boundary does), so no water enters.
BOUNDARY_KINDS = ("outflow", "transmissive")


def outflow_only_momentum(xm, ym, n1, n2):
    """Pure: boundary momentum for an outflow-only edge. (xm, ym) the interior edge momentum,
    (n1, n2) the edge's OUTWARD unit normal. Where xm*n1 + ym*n2 < 0 (inward) the normal
    component is mirrored (zero mass flux through the edge, as a wall); elsewhere it is kept
    (transmissive: the water leaves freely). Returns (xm, ym, inward mask) as new arrays."""
    xm = np.array(xm, dtype=float, copy=True)
    ym = np.array(ym, dtype=float, copy=True)
    n1 = np.asarray(n1, dtype=float)
    n2 = np.asarray(n2, dtype=float)
    un = xm * n1 + ym * n2
    inward = un < 0.0
    if np.any(inward):
        xm = np.where(inward, xm - 2.0 * un * n1, xm)
        ym = np.where(inward, ym - 2.0 * un * n2, ym)
    return xm, ym, inward


def apply_outflow_boundary(domain, ids, vol_ids, edge_ids, centroid=False):
    """Sets the boundary values of the edges `ids` (domain.boundary_* indices): every quantity
    that has boundary values copied from the triangle's edge (or centroid with
    domain.centroid_transmissive_bc, as Transmissive_boundary), then the momentum (and the
    velocity) made outflow-only. Returns the number of inward edges mirrored."""
    q = domain.quantities

    def src(Q):
        return Q.centroid_values[vol_ids] if centroid else Q.edge_values[vol_ids, edge_ids]
    for name in ("stage", "xmomentum", "ymomentum", "elevation", "height", "xvelocity",
                 "yvelocity"):
        Q = q.get(name)
        if Q is not None and getattr(Q, "boundary_values", None) is not None:
            Q.boundary_values[ids] = src(Q)
    nrm = domain.normals
    n1 = nrm[vol_ids, 2 * edge_ids]
    n2 = nrm[vol_ids, 2 * edge_ids + 1]
    xm, ym, inward = outflow_only_momentum(q["xmomentum"].boundary_values[ids],
                                           q["ymomentum"].boundary_values[ids], n1, n2)
    q["xmomentum"].boundary_values[ids] = xm
    q["ymomentum"].boundary_values[ids] = ym
    if "xvelocity" in q and "yvelocity" in q:
        xv, yv, _ = outflow_only_momentum(q["xvelocity"].boundary_values[ids],
                                          q["yvelocity"].boundary_values[ids], n1, n2)
        q["xvelocity"].boundary_values[ids] = xv
        q["yvelocity"].boundary_values[ids] = yv
    return int(np.count_nonzero(inward))


def outflow_boundary_class(anuga):
    """The outflow-only boundary class (needs anuga's Boundary base, imported only in the
    ANUGA Python); bound at module level under its own name like Envelope, so a checkpointed
    domain pickles and loads it by name."""
    cls = globals().get("Outflow_boundary")
    base = getattr(anuga, "Boundary", None)
    if base is None:
        try:
            from anuga.abstract_2d_finite_volumes.generic_boundary_conditions import Boundary \
                as base
        except ImportError:         # the tests' stand-in anuga
            base = object
    if cls is not None and issubclass(cls, base):
        return cls

    class Outflow_boundary(base):
        """Transmissive for water leaving the domain, a wall (mirrored normal momentum) for
        water that would enter it: a closed or clipped edge never gains water."""

        def __init__(self, domain=None):
            if base is not object:
                base.__init__(self)
            if domain is None:
                raise Exception("Domain must be specified for the outflow boundary")
            self.domain = domain
            self.inward_edges = 0          # edges mirrored at the last update (diagnostic)

        def __repr__(self):
            return "Outflow_boundary"

        def evaluate(self, vol_id, edge_id):
            d = self.domain
            if d.centroid_transmissive_bc:
                qv = np.array(d.get_conserved_quantities(vol_id), float)
            else:
                qv = np.array(d.get_conserved_quantities(vol_id, edge=edge_id), float)
            n = d.normals[vol_id, 2 * edge_id:2 * edge_id + 2]
            xm, ym, _ = outflow_only_momentum([qv[1]], [qv[2]], [n[0]], [n[1]])
            qv[1], qv[2] = xm[0], ym[0]
            return qv

        def evaluate_segment(self, domain, segment_edges):
            if segment_edges is None or domain is None:
                return
            ids = np.asarray(segment_edges)
            if ids.size == 0:
                return
            self.inward_edges = apply_outflow_boundary(
                domain, ids, domain.boundary_cells[ids], domain.boundary_edges[ids],
                centroid=bool(domain.centroid_transmissive_bc))

    Outflow_boundary.__module__ = __name__
    Outflow_boundary.__qualname__ = "Outflow_boundary"
    globals()["Outflow_boundary"] = Outflow_boundary
    return Outflow_boundary


def make_outer_boundary(anuga, domain, kind, dem_min):
    """(boundary object, what it is) for the outer edges: 'outflow' (default since 3.10.55),
    'transmissive' (before 3.10.55; can feed water in) or, if neither can be made, a stage
    far below the ground (drains, never adds water)."""
    kind = (kind or "outflow").strip().lower()
    try:
        if kind == "transmissive":
            return anuga.Transmissive_boundary(domain), "transmissive"
        return outflow_boundary_class(anuga)(domain), "outflow"
    except Exception as e:          # noqa: BLE001
        return (anuga.Dirichlet_boundary([float(dem_min) - 50.0, 0.0, 0.0]),
                "dirichlet stage {:.1f} m ({}: {})".format(float(dem_min) - 50.0, kind,
                                                          str(e)[:80]))


# 3.10.55 (review): ANUGA exposes the boundary flux only as ONE net number. Its DE flux kernel
# adds every boundary edge's flux into domain.boundary_flux_sum (one value per sub-step, all
# boundary tags together) and boundary_flux_integral_operator integrates that; there is no
# per-edge or per-tag flux, so gross edge inflow and outflow cannot be reported separately
# without re-computing the edge fluxes outside the solver. What the worker reports instead:
# the net boundary integral (+ = in), ANUGA's own conservation residual
# V_end - V_start - boundary integral - operators (inlet, culverts), and, with outflow-only
# edges, how many edges had inward-moving water that the boundary mirrored (where a
# transmissive edge would have let water in).
GROSS_EDGE_FLUX_NOTE = ("not available: ANUGA sums the flux of all boundary edges into one net "
                        "value per sub-step (domain.boundary_flux_sum)")


SOLVER_RESIDUAL_NOT_DE = ("not computed: the flow algorithm is not a DE algorithm (ANUGA "
                          "computes the boundary flux integral for DE algorithms only)")
MIRRORED_EDGES_NOTE = ("sampled at yield steps only; restarts from 0 when a run resumes from a "
                       "checkpoint")


def volume_balance(initial, injected, in_domain, boundary_in=None, operators=None,
                   solver_volume=None, solver_initial=None, de=True):
    """3.10.55: the end-of-run water balance (m3). 'ratio' = in domain / (initial + injected):
    above 1 the domain gained water (a boundary that fed water in, or instability), below 1
    water left through the edge. With ANUGA's boundary flux integral (DE algorithms; + = net
    inflow): 'boundary_net_inflow_m3', 'edge_net_outflow_m3' (its loss side) and 'residual_m3'
    = in domain - initial - injected - boundary_in. With the operators' fractional-step
    integral (the inlet; culverts move water inside) and ANUGA's own end volume:
    'solver_residual_m3' = solver_volume - initial - boundary_in - operators, ANUGA's
    conservation error (water_volume_statistics), and its fraction of the supply.
    3.10.55 review 2: solver_initial = ANUGA's own start volume (domain.volume_history[0], as in
    report_water_volume_statistics; else `initial`); de=False (a non-DE flow algorithm): the
    flux-integral lines are skipped and 'solver_residual' says 'not computed'."""
    supplied = float(initial or 0.0) + float(injected or 0.0)
    out = {"supplied_m3": round(supplied, 1), "in_domain_m3": round(float(in_domain), 1),
           "ratio": round(float(in_domain) / supplied, 4) if supplied > 0 else None,
           "gross_edge_flows": GROSS_EDGE_FLUX_NOTE}
    if not de:
        out["solver_residual"] = SOLVER_RESIDUAL_NOT_DE
        boundary_in = None
    if boundary_in is not None:
        b = float(boundary_in)
        out["boundary_net_inflow_m3"] = round(b, 1)
        out["edge_net_outflow_m3"] = round(-b, 1) if b < 0 else 0.0
        out["residual_m3"] = round(float(in_domain) - supplied - b, 1)
        if operators is not None:
            out["operators_m3"] = round(float(operators), 1)
            v = float(in_domain if solver_volume is None else solver_volume)
            v0 = float(initial or 0.0) if solver_initial is None else float(solver_initial)
            out["solver_initial_m3"] = round(v0, 1)
            res = v - v0 - b - float(operators)
            out["solver_residual_m3"] = round(res, 1)
            out["solver_residual_frac"] = round(res / supplied, 5) if supplied > 0 else None
    return out


def apply_threads(anuga, threads):
    """Set ANUGA's OpenMP thread count (a process setting, so also needed when a checkpoint
    resumes: a loaded domain does not run Domain.__init__, which re-reads OMP_NUM_THREADS).
    threads <= 0: nothing is set (ANUGA's own default; job files before 3.10.47). Returns
    (the count ANUGA reports or None, a note or ''). Never raises: a build without OpenMP
    (or without the setter) simply runs on one thread."""
    if threads <= 0:
        getter = getattr(anuga, "get_omp_num_threads", None)
        try:
            return (int(getter()) if getter else None), ""
        except Exception:        # noqa: BLE001
            return None, ""
    os.environ["OMP_NUM_THREADS"] = str(int(threads))   # Domain() reads it on creation
    setter = getattr(anuga, "set_omp_num_threads", None)
    note = ""
    if setter is None:
        note = "this ANUGA has no set_omp_num_threads; OMP_NUM_THREADS only"
    else:
        try:
            try:
                setter(threads, verbose=False)
            except TypeError:
                setter(threads)
        except Exception as e:   # noqa: BLE001 - e.g. no OpenMP extension: one thread
            note = "set_omp_num_threads failed ({}); OMP_NUM_THREADS only".format(
                str(e)[:120])
    getter = getattr(anuga, "get_omp_num_threads", None)
    try:
        return (int(getter()) if getter else None), note
    except Exception:            # noqa: BLE001
        return None, note


def runtime_note(threads, rt_n, rt_name, note=""):
    """3.10.50: add to `note` when the OpenMP runtime's count (omp_probe) is not the
    `threads` that were asked for (> 0), or when no runtime could be read."""
    if threads <= 0:
        return note
    extra = ""
    if rt_n is None:
        extra = "thread count not verified ({})".format(rt_name)
    elif int(rt_n) != int(threads):
        extra = "the OpenMP runtime ({}) reports {} thread(s), not {}".format(
            rt_name, rt_n, threads)
    return "; ".join(x for x in (note, extra) if x)


def checkpoint_config(job):
    """The opt-in settings from the job; None when none of them is on (the default: the
    worker then runs exactly as before 3.10.35)."""
    g = lambda k, d=0.0: float(job[k]) if k in job else d
    cfg = {"every_min": max(0.0, g("ckpt_every_min")),
           "every_steps": int(max(0, g("ckpt_every_steps"))),
           "keep": int(max(1, g("ckpt_keep", 2))),
           "dir": str(job["ckpt_dir"]) if "ckpt_dir" in job else "anuga_checkpoints",
           "partial_every_min": max(0.0, g("partial_every_min")),
           "resume": bool(g("resume"))}
    cfg["checkpoint"] = cfg["every_min"] > 0 or cfg["every_steps"] > 0
    cfg["partial"] = cfg["checkpoint"] or cfg["partial_every_min"] > 0
    if not (cfg["checkpoint"] or cfg["partial"] or cfg["resume"]):
        return None
    return cfg


def job_fingerprint(job):
    """sha256 (16 hex) of everything in the job except FINGERPRINT_SKIP (the checkpoint
    settings, threads, gmsh's timings): a checkpoint is only resumed by the job that wrote
    it."""
    h = hashlib.sha256()
    for k in sorted(job):
        if k in FINGERPRINT_SKIP:
            continue
        a = np.asarray(job[k])
        h.update(k.encode())
        h.update(str(a.dtype).encode())
        h.update(str(a.shape).encode())
        h.update(np.ascontiguousarray(a).tobytes())
    return h.hexdigest()[:16]


def checkpoint_path(ckdir, t):
    """ANUGA's own pickle name (Domain.evolve: <name>_<str(time)>.pickle)."""
    return os.path.join(ckdir, DOMAIN_NAME) + "_" + str(float(t)) + ".pickle"


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def savez_atomic(path, **arrays):
    """np.savez_compressed to <path>.tmp, fsync, then rename over `path`: a reader (or a
    kill at any moment) sees the old file or the new one, never half of one."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            np.savez_compressed(f, **arrays)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_meta(ckdir):
    try:
        with open(os.path.join(ckdir, META_NAME), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def prune_checkpoints(ckdir, times, keep):
    """Delete all but the newest `keep` checkpoint pickles listed in `times`; returns the
    times kept (oldest first)."""
    times = sorted(set(float(t) for t in times))
    for t in times[:-keep] if keep > 0 else []:
        try:
            os.remove(checkpoint_path(ckdir, t))
        except OSError:
            pass
    return times[-keep:] if keep > 0 else times


class _NullWriter:
    """A file that keeps nothing and counts the bytes written to it."""

    def __init__(self):
        self.n = 0

    def write(self, b):
        self.n += len(b)
        return len(b)


def pickler():
    """The module ANUGA's Domain.evolve pickles checkpoints with: dill when it imports
    (ANUGA 4.0.1 shallow_water_domain: `import dill as pickle`), else pickle."""
    try:
        import dill
        return dill
    except ImportError:
        return pickle


def trial_pickle(obj):
    """('', bytes) when ANUGA's pickler can serialise obj (nothing is kept, only the size is
    counted), else (the error, 0)."""
    w = _NullWriter()
    try:
        pickler().dump(obj, w)
        return "", w.n
    except Exception as e:      # noqa: BLE001 - any failure disables checkpointing
        return "{}: {}".format(type(e).__name__, str(e)[:200]), 0


def picklable(obj):
    """'' when ANUGA's pickler can serialise obj, else the error."""
    return trial_pickle(obj)[0]


DISK_MARGIN_B = 256 * 1024 ** 2      # left free besides the checkpoints


def free_bytes(path):
    """Free space where `path` is (None when unknown)."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def raster_index(domain, out_x0, out_y0, odx, onx, ony):
    """(inside, tidx): the triangle under each output cell centre."""
    import matplotlib.tri as mtri
    nodes = domain.nodes
    tris = domain.triangles
    tri = mtri.Triangulation(nodes[:, 0], nodes[:, 1], tris)
    finder = tri.get_trifinder()
    gx = out_x0 + (np.arange(onx) + 0.5) * odx
    gy = out_y0 - (np.arange(ony) + 0.5) * odx
    GX, GY = np.meshgrid(gx, gy)
    ti = finder(GX, GY)
    inside = ti >= 0
    tidx = np.where(inside, ti, 0)
    return inside, tidx


def rasterise(env, inside, tidx, vmin_depth):
    """depth, velocity, arrival_hr, wse grids from the envelope's triangle values."""
    def ras(v):
        return np.where(inside, v[tidx], np.nan)

    depth = ras(env.hmax)
    wet = depth >= 0.001
    depth = np.where(inside, np.where(wet, depth, 0.0), np.nan)
    vel = np.where(inside, np.where(depth >= vmin_depth, ras(env.vmax), 0.0), np.nan)
    arr = np.where(inside, ras(env.arr), np.nan)
    wse = np.where(wet, ras(env.wmax), np.nan)
    wse[~np.isfinite(wse)] = np.nan
    return depth, vel, arr, wse


def write_partial(path, env, inside, tidx, vmin_depth, t, sim_time, fingerprint, reason):
    """anuga_partial.npz: the grids so far (same layout as result.npz) plus the triangle
    envelope and the model time, written atomically."""
    depth, vel, arr, wse = rasterise(env, inside, tidx, vmin_depth)
    info = {"t_s": float(t), "sim_time_s": float(sim_time),
            "percent": round(100.0 * float(t) / float(sim_time), 2) if sim_time else None,
            "complete": False, "reason": reason, "fingerprint": fingerprint,
            "written_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    savez_atomic(path, depth=depth, velocity=vel, arrival_hr=arr, wse=wse,
                 tri_hmax=env.hmax, tri_vmax=env.vmax, tri_wmax=env.wmax, tri_arr=env.arr,
                 info=json.dumps(info))
    return info


def find_envelope(domain, cls):
    """The Envelope operator of a loaded domain. dill stores a class defined in __main__
    (the worker run as a script) by value, so the loaded one is a copy of `cls`, not `cls`:
    it is matched by name and its arrays."""
    for op in getattr(domain, "fractional_step_operators", []) or []:
        if isinstance(op, cls) or (type(op).__name__ == "Envelope" and all(
                hasattr(op, a) for a in ("hmax", "vmax", "wmax", "arr"))):
            return op
    return None


def load_latest_checkpoint(anuga, ckdir, meta):
    """(domain, t) from the newest complete checkpoint listed in checkpoint.json that loads,
    or (None, reason)."""
    errors = []
    for t in sorted(meta.get("times") or [], reverse=True):
        p = checkpoint_path(ckdir, t)
        if not os.path.exists(p):
            errors.append("{:g} s: missing".format(t))
            continue
        try:
            dom = anuga.load_checkpoint_file(domain_name=DOMAIN_NAME, checkpoint_dir=ckdir,
                                             time=t)
            return dom, float(t)
        except Exception as e:      # noqa: BLE001 - try the one before
            errors.append("{:g} s: {}".format(t, str(e)[:120]))
    return None, "; ".join(errors) or "no complete checkpoint listed"


def main(job_path, out_path):
    import anuga
    job = dict(np.load(job_path, allow_pickle=False))
    s = lambda k: float(job[k])
    x_ref, y_ref = s("dem_x0"), s("dem_y0") - job["dem"].shape[0] * s("dem_dx")
    # work in local coordinates (lower-left of the DEM = 0, 0)
    dem = fill_nan(job["dem"].astype(float))
    dx = s("dem_dx")
    lx0, ly0 = 0.0, dem.shape[0] * dx          # local left and top edges

    def to_local(p):
        p = np.asarray(p, dtype=float).reshape(-1, 2)
        return np.column_stack([p[:, 0] - x_ref, p[:, 1] - y_ref])

    t0 = time.time()
    hyd_t = job["hyd_t"].astype(float)
    hyd_q = job["hyd_q"].astype(float)
    arrival_depth = s("arrival_depth")
    vmin_depth = s("velocity_min_depth")
    sim_time = s("sim_time")
    ystep = s("yieldstep")
    threads = int(job["threads"]) if "threads" in job else 0
    omp_threads, omp_note = apply_threads(anuga, threads)
    if threads > 0:
        print("ANUGA OpenMP threads: {} (asked for {}){}".format(
            omp_threads if omp_threads is not None else "?", threads,
            "; " + omp_note if omp_note else ""), flush=True)
    Envelope = envelope_class(anuga)
    outflow_boundary_class(anuga)      # bound by name before a checkpoint is loaded

    def build_domain():
        """The mesh, bed, friction, initial water, boundary, inlet and culverts."""
        boundary = to_local(job["boundary"])
        fine = to_local(job["fine"]) if job["fine"].size else None
        inlet = to_local(job["inlet"])
        nb = len(boundary)
        regions = [[fine.tolist(), s("fine_area")]] if fine is not None and len(fine) >= 3 else []
        for extra in (json.loads(str(job["regions_json"])) if "regions_json" in job else []):
            regions.append([to_local(extra["poly"]).tolist(), float(extra["area"])])
        regions = regions or None
        breaklines = [to_local(b).tolist() for b in
                      (json.loads(str(job["breaklines_json"])) if "breaklines_json" in job else [])]
        # A zero geo-reference keeps ANUGA's internal coordinates equal to ours
        # (by default it shifts them to the polygon's lower-left corner).
        georef = anuga.Geo_reference(xllcorner=0.0, yllcorner=0.0)
        kw = dict(boundary_tags={"outflow": list(range(nb))},
                  maximum_triangle_area=s("coarse_area"), interior_regions=regions,
                  mesh_geo_reference=georef, use_cache=False, verbose=False)
        if accepts_keyword(anuga.create_domain_from_regions, "mesh_filename"):
            kw["mesh_filename"] = job_path.replace(".npz", ".msh")   # ANUGA 3.x writes the mesh

        def make_domain(extra):
            return anuga.create_domain_from_regions(boundary.tolist(), **dict(kw, **extra))

        breaklines_used = False
        if "mesh_points" in job and job["mesh_points"].size:
            # external (gmsh) mesh: triangles CCW, boundary = [triangle, edge] (edge i is
            # opposite vertex i), every outer edge tagged "outflow"
            pts = to_local(job["mesh_points"])
            tris = np.asarray(job["mesh_triangles"], dtype=int)
            bnd = {(int(t), int(e)): "outflow" for t, e in np.asarray(job["mesh_boundary"], int)}
            domain = anuga.Domain(pts.tolist(), tris.tolist(), bnd, geo_reference=georef)
        elif breaklines:
            try:
                domain = make_domain({"breaklines": breaklines})
                breaklines_used = True
            except Exception as e:      # e.g. crossing lines: mesh without them
                print("PROGRESS 0 breaklines dropped: {}".format(str(e)[:120]), flush=True)
                domain = make_domain({})
        else:
            domain = make_domain({})
        ntri = len(domain)
        print("PROGRESS 0 mesh {} triangles".format(ntri), flush=True)

        domain.set_name("anuga_breach")
        domain.set_store(False)
        try:
            domain.set_flow_algorithm(str(job["algorithm"]))
        except Exception:
            domain.set_flow_algorithm("DE0")
        domain.set_minimum_storable_height(0.001)

        elev_stats = {"mode": "model_grid", "cell_m": dx}
        if "elev_fine_file" in job:
            # 3.10.31: the elevation at the triangle VERTICES from the finest terrain. ANUGA
            # makes the edge values the mean of their two vertices and the centroid the mean of
            # three, so a crown on an embedded mesh line keeps its height on that edge and
            # weighs 2/3 in both triangles beside it (an area mean would spread it over the
            # triangle, and the old 40 m model grid had averaged it away before that).
            fpath = os.path.join(os.path.dirname(os.path.abspath(job_path)), str(job["elev_fine_file"]))
            farr = np.load(fpath, mmap_mode="r")
            fx0, fy0, fdx = s("elev_fine_x0"), s("elev_fine_y0"), s("elev_fine_dx")
            count = {"vertices": 0, "fallback": 0}

            def elevation(x, y):
                x = np.asarray(x, float)
                y = np.asarray(y, float)
                v = sample_fine(farr, fx0, fy0, fdx, x.ravel() + x_ref, y.ravel() + y_ref)
                bad = ~np.isfinite(v)
                if bad.any():                              # no fine data: the model DEM
                    v[bad] = bilinear(dem, lx0, ly0, dx, x.ravel()[bad], y.ravel()[bad])
                count["vertices"] += int(v.size)
                count["fallback"] += int(bad.sum())
                return v.reshape(x.shape)
            domain.set_quantity("elevation", elevation, location="vertices")
            elev_stats = {"mode": "fine", "cell_m": fdx, "sampled_at": "vertices",
                          "vertex_samples": count["vertices"],
                          "model_grid_fallback_samples": count["fallback"]}
        else:
            domain.set_quantity("elevation", lambda x, y: bilinear(dem, lx0, ly0, dx, x, y),
                                location="centroids")
        if "crest_z" in job and job["crest_z"].size:
            # 3.10.91: the road / rail crest nodes raised to their 1 m crest elevation
            # (anuga_crest); the edges along a crest then carry it, whatever the bed mode
            q = domain.quantities["elevation"]
            vxy = np.asarray(domain.get_vertex_coordinates(absolute=True), float)
            vv, cinfo = crest_vertex_values(vxy, q.vertex_values, to_local(job["crest_xy"]),
                                            job["crest_z"])
            if cinfo["vertices_matched"]:
                domain.set_quantity("elevation", vv, location="vertices")
            elev_stats["crest"] = cinfo
            print("PROGRESS 0 crest hold: {} vertices at {} crest nodes, {} raised (up to "
                  "{:.2f} m)".format(cinfo["vertices_matched"], cinfo["crest_nodes"],
                                     cinfo["vertices_raised"], cinfo["max_raise_m"]), flush=True)
        if "manning" in job and job["manning"].size:
            man = fill_nan(job["manning"].astype(float))
            domain.set_quantity("friction", lambda x, y: bilinear(man, lx0, ly0, dx, x, y),
                                location="centroids")
        else:
            domain.set_quantity("friction", s("manning_n"))
        init = None
        if "init_depth" in job and job["init_depth"].size:
            init = np.where(np.isfinite(job["init_depth"]), job["init_depth"], 0.0).astype(float)
            if elev_stats["mode"] == "fine":      # the depth on top of the mesh's own bed
                cxy = domain.centroid_coordinates
                zc = domain.quantities["elevation"].centroid_values
                domain.set_quantity("stage", zc + bilinear(init, lx0, ly0, dx, cxy[:, 0], cxy[:, 1]),
                                    location="centroids")
            else:
                domain.set_quantity("stage", lambda x, y: bilinear(dem, lx0, ly0, dx, x, y)
                                    + bilinear(init, lx0, ly0, dx, x, y), location="centroids")
        else:
            domain.set_quantity("stage", expression="elevation", location="centroids")
        h0 = domain.quantities["stage"].centroid_values - domain.quantities["elevation"].centroid_values
        vol_initial = float(np.sum(np.clip(h0, 0, None) * domain.areas))
        try:            # 3.10.55 review 2: ANUGA's own start volume (domain.volume_history[0])
            domain.get_water_volume()
        except Exception:                                        # noqa: BLE001
            pass

        # Free outflow on the whole outer boundary. A transmissive (zero-gradient)
        # boundary reports the depth at the edge without the bias of a drained edge
        # (a fixed stage far below the ground reads about 0.1 ft shallower).
        # 3.10.55: outflow only (see BOUNDARY_KINDS); 'transmissive' only when the job asks
        # (job key 'edge_bc'; 'boundary' is the outer polygon)
        Bo, bkind = make_outer_boundary(anuga, domain, str(job["edge_bc"]) if "edge_bc" in job
                                        else "outflow", np.nanmin(dem))
        # One boundary for the whole outer polygon: the breach enters through an Inlet_operator
        # inside the mesh, so there is no inflow boundary to protect. If a real boundary inflow is
        # ever added (a tributary hydrograph, a tailwater stage), edge_bc must become per tag.
        domain.set_boundary({"outflow": Bo})

        qfun = Hydrograph(hyd_t, hyd_q)      # = np.interp(t, hyd_t, hyd_q, right=0.0)
        # default = Inlet_operator's own 0.0, as a picklable function (checkpoints)
        anuga.Inlet_operator(domain, inlet.tolist(), Q=qfun, default=Constant(0.0))
        culverts = json.loads(str(job["culverts_json"])) if "culverts_json" in job else []
        culvert_errors = []
        culvert_enquiry = []      # 3.10.71: one record per job culvert, in job order
        culvert_names = []          # 3.10.70: the built ones, in order (dt diagnostic)
        for c in culverts:
            ends = to_local(c["end_points"]).tolist()
            try:
                _op, rec = add_boyd_culvert(anuga, domain, c, ends)
                culvert_names.append(str(c.get("name") or "culvert"))
            except Exception as e:
                culvert_errors.append("{}: {}".format(c.get("name") or "culvert", str(e)[:200]))
                rec = {"enquiry_gap_m": None, "enquiry_in_inlet": None}
            culvert_enquiry.append(rec)

        info = {"triangles": ntri, "breaklines_used": breaklines_used,
                "culverts": len(culverts) - len(culvert_errors), "culvert_errors": culvert_errors,
                "culvert_enquiry": culvert_enquiry,
                "initial_volume_m3": vol_initial, "elevation": elev_stats, "boundary": bkind,
                "culvert_names": culvert_names}
        return domain, info

    # 3.10.35: opt-in checkpoints / partial results / resume (ck None = as before)
    ck = checkpoint_config(job)
    domain = env = info = None
    ckinfo = ckdir = meta = partial_path = None
    if ck is not None:
        workdir = os.path.dirname(os.path.abspath(job_path))
        fp = job_fingerprint(job)
        ckdir = os.path.join(workdir, ck["dir"], fp)
        partial_path = os.path.join(workdir, PARTIAL_NAME)
        ckinfo = {"fingerprint": fp, "dir": ckdir, "every_min": ck["every_min"],
                  "every_steps": ck["every_steps"], "keep": ck["keep"],
                  "partial": partial_path if ck["partial"] else None,
                  "partial_every_min": ck["partial_every_min"], "resume": ck["resume"],
                  "checkpoints_saved": 0, "last_checkpoint_t_s": None,
                  "partial_flushes": 0, "last_partial_t_s": None, "resumed_from_t_s": None}
        if ck["resume"]:
            meta = read_meta(ckdir)
            if not meta or meta.get("fingerprint") != fp or not meta.get("times"):
                ckinfo["resume_skipped"] = "no complete checkpoint of this job in " + ckdir
            else:
                dom, t_or_err = load_latest_checkpoint(anuga, ckdir, meta)
                envop = find_envelope(dom, Envelope) if dom is not None else None
                if dom is None:
                    ckinfo["resume_skipped"] = "no checkpoint loaded: " + str(t_or_err)
                elif envop is None:
                    ckinfo["resume_skipped"] = "the checkpoint has no envelope operator"
                else:
                    domain, env, info = dom, envop, meta["info"]
                    ckinfo["resumed_from_t_s"] = t_or_err
                    # the uninterrupted run counts the yield step it was saved at
                    domain.yieldstep_counter += 1
                    print("PROGRESS {:.2f} resumed from the checkpoint at t={:.0f}s".format(
                        100.0 * t_or_err / sim_time, t_or_err), flush=True)
            if ckinfo.get("resume_skipped"):
                print("PROGRESS 0 resume: " + ckinfo["resume_skipped"][:200], flush=True)
        if domain is None:
            if os.path.isdir(ckdir):             # a fresh start: this job's old pickles go
                shutil.rmtree(ckdir, ignore_errors=True)
            meta = None
    if domain is None:
        domain, info = build_domain()
        env = Envelope(domain, arrival_depth, vmin_depth)
    ntri = info["triangles"]
    if ck is not None and ck["checkpoint"]:
        err, nbytes = trial_pickle(domain)
        ckinfo["checkpoint_mb"] = round(nbytes / 1024.0 ** 2, 3)
        free = free_bytes(os.path.dirname(os.path.dirname(ckdir)))
        if not err and free is not None and free < (ck["keep"] + 1) * nbytes + DISK_MARGIN_B:
            err = "not enough free disk: {:.0f} MB free, {} checkpoints of {:.0f} MB need " \
                  "{:.0f} MB".format(free / 1024.0 ** 2, ck["keep"] + 1, nbytes / 1024.0 ** 2,
                                     ((ck["keep"] + 1) * nbytes + DISK_MARGIN_B) / 1024.0 ** 2)
        if err:                                  # the run goes on without checkpoints
            ckinfo["checkpoint_error"] = err
            domain.set_checkpointing(checkpoint=False)
            print("PROGRESS 0 checkpointing off: {}".format(
                err if err.startswith("not enough") else
                "the domain cannot be pickled ({})".format(err)), flush=True)
        else:
            os.makedirs(ckdir, exist_ok=True)
            if meta is None:
                meta = {"fingerprint": ckinfo["fingerprint"], "times": [], "info": info,
                        "sim_time_s": sim_time, "yieldstep_s": ystep,
                        "anuga_version": getattr(anuga, "__version__", "?")}
                write_json_atomic(os.path.join(ckdir, META_NAME), meta)
            if ck["every_min"] > 0:
                domain.set_checkpointing(checkpoint=True, checkpoint_dir=ckdir,
                                         checkpoint_time=60.0 * ck["every_min"])
            else:
                domain.set_checkpointing(checkpoint=True, checkpoint_dir=ckdir,
                                         checkpoint_step=ck["every_steps"])
    elif ck is not None:
        domain.set_checkpointing(checkpoint=False)   # e.g. resumed without new checkpoints
    ox0, oy0, odx = s("out_x0") - x_ref, s("out_y0") - y_ref, s("out_dx")
    onx, ony = int(job["out_nx"]), int(job["out_ny"])
    rindex = None
    if ck is not None and ck["partial"]:
        rindex = raster_index(domain, ox0, oy0, odx, onx, ony)

    def flush(t, reason):
        pinfo = write_partial(partial_path, env, rindex[0], rindex[1], vmin_depth, t, sim_time,
                              ckinfo["fingerprint"], reason)
        ckinfo["partial_flushes"] += 1
        ckinfo["last_partial_t_s"] = pinfo["t_s"]
        return pinfo

    old_term = None
    if ck is not None:
        def _term(signum, frame):
            raise SystemExit(128 + signum)
        try:
            old_term = signal.signal(signal.SIGTERM, _term)
        except (ValueError, OSError, AttributeError):   # not the main thread / no SIGTERM
            old_term = None
    last_flush = time.time()
    # 3.10.55: outflow-only edges that mirrored inward-moving water (sampled at each yield step)
    mirror = {"boundaries": list({id(b): b for b in (getattr(domain, "boundary_map", None)
                                                     or {}).values()
                                  if hasattr(b, "inward_edges")}.values()), "max": 0, "steps": 0}
    # 3.10.70: wall clock / internal steps on every PROGRESS line (always on, reads only), and
    # the opt-in time-step diagnostic (timestep_diag.csv next to the job; off = None)
    clock = anuga_dtdiag.YieldClock(domain.get_time())
    dtdiag = None
    diag_every = anuga_dtdiag.dt_diag_every(job)
    if diag_every > 0:
        try:
            dtdiag = anuga_dtdiag.DtDiag(
                domain, os.path.dirname(os.path.abspath(job_path)), diag_every,
                resumed_t=ckinfo.get("resumed_from_t_s") if ckinfo else None,
                x_off=x_ref, y_off=y_ref,
                crs_wkt=str(job["dt_diag_crs"]) if "dt_diag_crs" in job else "",
                culvert_names=info.get("culvert_names") or ())
            print("DT_DIAG on: every {} yield step(s) -> {}".format(diag_every, dtdiag.path),
                  flush=True)
        except Exception as e:      # noqa: BLE001 - a diagnostic never stops the run
            print("DT_DIAG off: {}".format(str(e)[:200]), flush=True)
            dtdiag = None
    # 3.10.94: no single dry-domain step longer than the hydrograph interval (G5, DWR-3034)
    icap = InflowStepCap(domain, inflow_max_timestep(hyd_t, hyd_q, ystep),
                         inflow_end_time(hyd_t, hyd_q))
    if icap.active:
        print("PROGRESS 0 inflow step cap: {:.3g} s until t={:.0f}s".format(icap.cap, icap.t_end),
              flush=True)
    try:
        for t in domain.evolve(yieldstep=ystep, finaltime=sim_time):
            icap.update(domain, t)
            yc = clock.tick(domain, t)
            print("PROGRESS {:.2f} t={:.0f}s{}".format(100.0 * t / sim_time, t,
                                                       clock.suffix(yc)), flush=True)
            if dtdiag is not None:
                dtdiag.on_yield(domain, t, clock)
            if mirror["boundaries"]:
                n_in = int(sum(int(b.inward_edges) for b in mirror["boundaries"]))
                mirror["max"] = max(mirror["max"], n_in)
                mirror["steps"] += int(n_in > 0)
            if ck is None:
                continue
            saved = (ck["checkpoint"] and not ckinfo.get("checkpoint_error")
                     and float(t) not in meta["times"]
                     and os.path.exists(checkpoint_path(ckdir, t)))
            if saved:
                meta["times"] = prune_checkpoints(ckdir, meta["times"] + [float(t)], ck["keep"])
                write_json_atomic(os.path.join(ckdir, META_NAME), meta)
                ckinfo["checkpoints_saved"] += 1
                ckinfo["last_checkpoint_t_s"] = float(t)
                print("PROGRESS {:.2f} checkpoint t={:.0f}s".format(100.0 * t / sim_time, t),
                      flush=True)
            if ck["checkpoint"] and not ckinfo.get("checkpoint_error"):
                free = free_bytes(ckdir)          # room for the next one (plus the margin)?
                if free is not None and free < 1.2 * ckinfo["checkpoint_mb"] * 1024.0 ** 2 \
                        + DISK_MARGIN_B:
                    domain.set_checkpointing(checkpoint=False)
                    ckinfo["checkpoint_error"] = "checkpoints stopped at t={:.0f}s: {:.0f} MB " \
                        "free".format(float(t), free / 1024.0 ** 2)
                    print("PROGRESS {:.2f} {}".format(100.0 * t / sim_time,
                                                      ckinfo["checkpoint_error"]), flush=True)
            if rindex is not None and (saved or (
                    ck["partial_every_min"] > 0
                    and time.time() - last_flush >= 60.0 * ck["partial_every_min"])):
                flush(t, "checkpoint" if saved else "periodic")
                last_flush = time.time()
    except (SystemExit, KeyboardInterrupt):
        if rindex is not None:
            try:
                flush(domain.get_time(), "stopped")
                print("PROGRESS {:.2f} stopped: partial results in {}".format(
                    100.0 * domain.get_time() / sim_time, partial_path), flush=True)
            except Exception:       # noqa: BLE001 - never hide the stop itself
                pass
        raise
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if dtdiag is not None:          # the rows are on disk; the summary needs the end
            try:
                dtdiag.f.flush()
            except Exception:           # noqa: BLE001
                pass

    # volume check
    h = domain.quantities["stage"].centroid_values - domain.quantities["elevation"].centroid_values
    vol_left = float(np.sum(np.clip(h, 0, None) * domain.areas))

    # rasterise the piecewise-constant centroid values onto the output grid
    inside, tidx = rindex if rindex is not None else raster_index(domain, ox0, oy0, odx, onx, ony)
    depth, vel, arr, wse = rasterise(env, inside, tidx, vmin_depth)

    # largest depth next to the mesh boundary (flood leaving the domain)
    bnd = domain.boundary
    edge_tris = np.unique([k[0] for k in bnd.keys()])
    edge_depth = float(env.hmax[edge_tris].max()) if len(edge_tris) else 0.0

    tt = np.unique(np.concatenate([hyd_t[hyd_t < sim_time], [min(sim_time, hyd_t[-1])]]))
    qq = np.interp(tt, hyd_t, hyd_q)          # only what enters before the run ends
    injected = float(np.trapezoid(qq, tt)) if hasattr(np, "trapezoid") \
        else float(np.trapz(qq, tt))
    de = str(getattr(domain, "compute_fluxes_method", "DE")) == "DE"
    b_in = None
    if de:                      # DE algorithms only; + = net inflow through the boundary
        try:
            b_in = float(domain.get_boundary_flux_integral())
        except Exception:       # noqa: BLE001
            b_in = None
    try:                        # what the inlet (and any rate operator) actually added
        fs = float(domain.get_fractional_step_volume_integral())
    except Exception:           # noqa: BLE001
        fs = None
    try:
        v_solver = float(domain.get_water_volume())
    except Exception:           # noqa: BLE001
        v_solver = None
    try:                        # ANUGA's start volume (first get_water_volume, in the build)
        v0 = float(domain.volume_history[0]) if getattr(domain, "volume_history", None) \
            else None
    except Exception:           # noqa: BLE001
        v0 = None
    balance = volume_balance(info["initial_volume_m3"], injected, vol_left, b_in, fs, v_solver,
                             solver_initial=v0, de=de)
    balance["inflow_step_cap"] = icap.record()
    if mirror["boundaries"]:
        balance["inward_edges_mirrored"] = {"max_edges": mirror["max"],
                                            "yield_steps": mirror["steps"],
                                            "note": MIRRORED_EDGES_NOTE}
    stats = {"triangles": ntri, "runtime_min": round((time.time() - t0) / 60.0, 2),
             "max_edge_depth_m": edge_depth, "volume_in_domain_m3": vol_left,
             "initial_volume_m3": info["initial_volume_m3"],
             "breaklines_used": info["breaklines_used"],
             "culverts": info["culverts"], "culvert_errors": info["culvert_errors"],
             "culvert_enquiry": info.get("culvert_enquiry", []),
             "injected_m3": injected, "anuga_version": getattr(anuga, "__version__", "?"),
             "algorithm": str(job["algorithm"]), "elevation": info["elevation"],
             "boundary": info.get("boundary", "transmissive"), "volume_balance": balance}
    # 3.10.47: the OpenMP thread count ANUGA ran with (read back at the end: Domain() and a
    # resumed checkpoint both go through it)
    _getter = getattr(anuga, "get_omp_num_threads", None)
    try:
        stats["omp_threads"] = int(_getter()) if _getter else omp_threads
    except Exception:            # noqa: BLE001
        stats["omp_threads"] = omp_threads
    # 3.10.50: the count the OpenMP runtime itself reports (get_omp_num_threads above only
    # echoes what was asked for); a different count, or no runtime found, goes in the note
    rt_n, rt_name = omp_probe.omp_runtime_threads()
    stats["omp_runtime_threads"] = rt_n
    stats["omp_runtime"] = rt_name
    omp_note = runtime_note(threads, rt_n, rt_name, omp_note)
    if omp_note:
        stats["omp_note"] = omp_note
    if dtdiag is not None:
        stats["dt_diag"] = dtdiag.finish()
    if ck is not None:
        if ckdir and os.path.isdir(ckdir):       # finished: the pickles are not needed
            shutil.rmtree(ckdir, ignore_errors=True)
            ckinfo["checkpoints_removed_after_run"] = True
            try:
                os.rmdir(os.path.dirname(ckdir))     # the checkpoint folder, when empty
            except OSError:
                pass
        if partial_path and os.path.exists(partial_path):
            os.remove(partial_path)                  # superseded by result.npz
            ckinfo["partial_removed_after_run"] = True
        stats["checkpoint"] = ckinfo
    np.savez_compressed(out_path, depth=depth, velocity=vel, arrival_hr=arr, wse=wse,
                        stats=json.dumps(stats))
    print("PROGRESS 100 done", flush=True)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1], sys.argv[2])
