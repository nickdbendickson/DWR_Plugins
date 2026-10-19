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
import sys
import threading
import time

import numpy as np

from .grid import Grid
from .lisflood import Envelopes, ModelError, Cancelled
from . import terrain, wallclock
from . import conditioning as cond
from . import structures as structmod

WORKER = os.path.join(os.path.dirname(__file__), "anuga_worker.py")
MESHER = os.path.join(os.path.dirname(__file__), "gmsh_mesher.py")
# "gmsh" is strict: a rejected gmsh mesh stops the run. "gmsh_or_default" uses the default
# ANUGA mesh instead, with a WARNING in the log and a line in run_report.json / .md.
MESHERS = ("default", "gmsh", "gmsh_or_default")
GMSH_MESHERS = ("gmsh", "gmsh_or_default")


class AnugaSettings:
    def __init__(self, python, threads=0, manning_n=0.06, manning_grid=None,
                 arrival_depth_m=0.3048, sim_time_s=6 * 3600.0, yieldstep_s=300.0,
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
        # ANUGA_MESHER: "default" = ANUGA's own mesh (create_domain_from_regions, unchanged);
        # "gmsh" = breakline-graded mesh from core/gmsh_mesher.py, the run stops if it is
        # rejected; "gmsh_or_default" = the same, but a rejected mesh falls back (loudly)
        self.mesher = "default"
        self.gmsh = None                 # dict of gmsh_mesher sizes (None = its defaults)
        self.gmsh_timeout_s = 600.0


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


def write_job(path, dem, outer, fine, inlet, hydrograph, st, out_grid, initial_depth=None,
              culverts=None, breaklines=None, regions=None, mesh=None):
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
    if st.threads:
        env["OMP_NUM_THREADS"] = str(int(st.threads))
    log_path = os.path.join(workdir, "anuga.log")
    t0 = time.time()
    with open(log_path, "w", encoding="utf-8") as logf:
        proc = subprocess.Popen([st.python, "-u", WORKER, job, result], cwd=workdir, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, errors="replace")
        lines = queue.Queue()

        def pump():
            for ln in proc.stdout:
                lines.put(ln)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        done = False
        while not done:
            if cancel and cancel():      # also while ANUGA is silent (meshing, start-up)
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
        refine_lines=None, reuse=False, embankment_lines=None, terrain_read=None):
    """embankment_lines: road / rail centrelines (work CRS); with a gmsh mesher the
    pieces on an embankment become mesh lines (find_embankments). terrain_read(bounds,
    cell) reads finer ground for that test. Neither changes the default mesh."""
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
    write_job(job, dem, outer, fine, inlet, hydrograph, st, dem, initial_depth, culverts,
              breaklines, regions, mesh=mesh)
    np.savetxt(os.path.join(workdir, "mesh_boundary.csv"), outer, delimiter=",",
               header="x,y", comments="", fmt="%.3f")
    if len(fine):
        np.savetxt(os.path.join(workdir, "mesh_fine_area.csv"), fine, delimiter=",",
                   header="x,y", comments="", fmt="%.3f")
    have = bool(reuse) and os.path.exists(result)
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
            proc, log_path = _launch(st, job, result, workdir, progress, cancel)
    if proc.returncode != 0 or not os.path.exists(result):
        tail = _tail_text(log_path)
        raise ModelError("ANUGA stopped with code {}:\n{}".format(proc.returncode, tail))
    # 3.4.1: read the archive inside `with` - an open .npz keeps the file locked on Windows
    # until garbage collection, which stopped the work folder from being removed
    with np.load(result) as r:
        stats = json.loads(str(r["stats"]))
        depth, vel, arr, wse = r["depth"], r["velocity"], r["arrival_hr"], r["wse"]
    stats.update({
        "mesher": minfo["used"], "mesh": minfo,
        "max_depth_ft": round(float(np.nanmax(depth)) / 0.3048, 2),
        "max_velocity_fps": round(float(np.nanmax(vel)) / 0.3048, 2),
        "flooded_acres": round(float(np.nansum(depth >= st.arrival_depth_m)) * dem.dx ** 2
                               / 4046.8564224, 1),
        "max_edge_depth_ft": round(stats["max_edge_depth_m"] / 0.3048, 2),
    })
    if log:
        log("ANUGA finished in {:.1f} min ({} triangles)".format((time.time() - t0) / 60.0,
                                                               stats.get("triangles")))
    notes = list(cnotes)
    if mnote:
        notes.append(mnote)
    for e in stats.get("culvert_errors", []):
        notes.append("ANUGA culvert not applied: " + e)
    if refine_lines and not stats.get("breaklines_used") and breaklines and mesh is None:
        notes.append("ANUGA: the embankment lines could not be used as mesh edges (they cross or "
                     "touch each other); the mesh was made without them.")
    return Envelopes(dem.like(depth, np.nan), dem.like(vel, np.nan), dem.like(arr, np.nan),
                     dem.like(wse, np.nan), stats, notes), outer, fine


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
                             timeout=300, errors="replace")
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
                             env=env or _clean_env(), timeout=120, errors="replace")
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
