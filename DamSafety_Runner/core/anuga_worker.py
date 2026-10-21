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

result.npz: depth, velocity, arrival_hr, wse (NaN = dry / outside), plus
stats as a JSON string. Progress goes to stdout as lines starting "PROGRESS".
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
except ImportError:
    import omp_probe


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
FINGERPRINT_SKIP = CKPT_KEYS + ("threads", "mesh_stats")
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

        # Free outflow on the whole outer boundary. A transmissive (zero-gradient)
        # boundary reports the depth at the edge without the bias of a drained edge
        # (a fixed stage far below the ground reads about 0.1 ft shallower).
        try:
            Bo = anuga.Transmissive_boundary(domain)
        except Exception:
            Bo = anuga.Dirichlet_boundary([float(np.nanmin(dem)) - 50.0, 0.0, 0.0])
        domain.set_boundary({"outflow": Bo})

        qfun = Hydrograph(hyd_t, hyd_q)      # = np.interp(t, hyd_t, hyd_q, right=0.0)
        # default = Inlet_operator's own 0.0, as a picklable function (checkpoints)
        anuga.Inlet_operator(domain, inlet.tolist(), Q=qfun, default=Constant(0.0))
        culverts = json.loads(str(job["culverts_json"])) if "culverts_json" in job else []
        culvert_errors = []
        for c in culverts:
            ends = to_local(c["end_points"]).tolist()
            common = dict(losses=float(c["losses"]), barrels=float(c["barrels"]), end_points=ends,
                          invert_elevations=[float(v) for v in c["invert_elevations"]],
                          apron=float(c["apron"]), manning=float(c["manning"]),
                          use_momentum_jet=True, use_velocity_head=False, verbose=False)
            try:
                if c.get("shape") == "pipe":
                    anuga.Boyd_pipe_operator(domain, diameter=float(c["width"]), **common)
                else:
                    anuga.Boyd_box_operator(domain, width=float(c["width"]),
                                            height=float(c["height"]), **common)
            except Exception as e:
                culvert_errors.append("{}: {}".format(c.get("name") or "culvert", str(e)[:200]))

        info = {"triangles": ntri, "breaklines_used": breaklines_used,
                "culverts": len(culverts) - len(culvert_errors), "culvert_errors": culvert_errors,
                "initial_volume_m3": vol_initial, "elevation": elev_stats}
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
    try:
        for t in domain.evolve(yieldstep=ystep, finaltime=sim_time):
            print("PROGRESS {:.2f} t={:.0f}s".format(100.0 * t / sim_time, t), flush=True)
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
    stats = {"triangles": ntri, "runtime_min": round((time.time() - t0) / 60.0, 2),
             "max_edge_depth_m": edge_depth, "volume_in_domain_m3": vol_left,
             "initial_volume_m3": info["initial_volume_m3"],
             "breaklines_used": info["breaklines_used"],
             "culverts": info["culverts"], "culvert_errors": info["culvert_errors"],
             "injected_m3": injected, "anuga_version": getattr(anuga, "__version__", "?"),
             "algorithm": str(job["algorithm"]), "elevation": info["elevation"]}
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
