"""3.10.26: ANUGA mesh and bed fixes from the gmsh embankment-line benchmark (2026-10-19).

1. The ANUGA bed comes from the finest terrain (the DEM source at its own cell size, read in
   blocks into a memory-mapped .npy) sampled at the triangle vertices; ANUGA_ELEVATION=
   model_grid keeps the model grid at the centroids (before 3.10.26).
2. The default mesher's culvert-end octagons that overlap are merged into one region.
3. gmsh: an embedded edge just longer than the mesh size is no longer cut in two.
4. gmsh_spec passes the culvert-end size (25 m2) to gmsh.
5. The worker checks create_domain_from_regions' signature instead of catching TypeError.
Nothing here runs ANUGA, LISFLOOD-FP or HEC-RAS; gmsh tests skip when gmsh does not import."""
import hashlib
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import gmsh_mesher as GM  # noqa: E402
from nd_breach_runner.core import workflow as W  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.core.lisflood import ModelError  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402
from nd_breach_runner.tests.test_gmsh_mesher import _case  # noqa: E402

try:
    import gmsh  # noqa: F401
    HAVE_GMSH = True
except Exception:
    HAVE_GMSH = False


def _sha(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _segments_cross(p, q):
    """Do two closed polygons' edges properly cross?"""
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        for c, d in zip(q, np.roll(q, -1, axis=0)):
            def orient(u, v, w):
                return np.sign((v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0]))
            if (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0):
                return True
    return False


# ------------------------------------------------------------------ 2. culvert octagons
def _culvert(length, width=3.658, y=0.0):
    return {"end_points": [[1000.0, y], [1000.0 + length, y]], "width": width}


@test
def culvert_octagons_on_a_short_culvert_become_one_region_without_crossing_edges():
    st = AM.AnugaSettings(sys.executable)
    old = [AM.octagon(e, 25.0) for e in _culvert(36.0)["end_points"]]
    assert _segments_cross(old[0], old[1])            # what made Absaraka's 0.15 m2 slivers
    regs = AM.culvert_regions([_culvert(36.0)], st)
    assert len(regs) == 1, regs
    poly = np.asarray(regs[0]["poly"])
    assert regs[0]["area"] == 25.0
    from nd_breach_runner.core import conditioning as cond
    assert cond.points_in_polygon(np.array([1000.0, 1036.0]), np.array([0.0, 0.0]), poly).all()
    # two twin culverts 30 m apart: all four ends in one region, nothing crosses
    regs = AM.culvert_regions([_culvert(36.0), _culvert(36.0, y=30.0)], st)
    assert len(regs) == 1
    # and nothing a hull leaves behind crosses another region
    regs = AM.culvert_regions([_culvert(36.0), _culvert(60.0, y=400.0)], st)
    polys = [np.asarray(r["poly"]) for r in regs]
    assert all(not _segments_cross(a, b) for i, a in enumerate(polys) for b in polys[i + 1:])


@test
def culvert_octagons_apart_are_exactly_as_before():
    st = AM.AnugaSettings(sys.executable)
    c = [_culvert(80.0), _culvert(80.0, y=500.0)]
    old = []
    for cc in c:                                      # the code before 3.10.26
        r = max(3.0 * cc["width"], 25.0)
        for e in cc["end_points"]:
            old.append({"poly": AM.octagon(e, r).tolist(), "area": min(st.fine_area_m2, 25.0)})
    assert AM.culvert_regions(c, st) == old


# ------------------------------------------------------------------ 3. gmsh short edges
@test
def gmsh_short_edge_counts_never_halve_an_edge_just_over_the_size():
    h = 1.3 * GM.h_of_area(100.0)                     # 19.76 m
    g = dict(line_dist_min_m=60.0, line_dist_max_m=1500.0, channel_dist_min_m=250.0,
             channel_dist_max_m=2500.0, point_dist_min_m=40.0, point_dist_max_m=600.0)
    nodes = np.array([[0, 0], [20.0, 0], [60.0, 0], [500.0, 0]])
    edges = np.array([[0, 1], [1, 2], [2, 3]])
    n = GM.short_edge_counts(nodes, edges, ["line"] * 3, [], g, h, h, h, 300.0)
    assert n == {0: 1, 1: 2}, n                       # 20 m: one piece; 40 m: two of 20 m
    for k, m in n.items():
        L = np.hypot(*(nodes[edges[k, 1]] - nodes[edges[k, 0]]))
        assert L / m >= 0.75 * h


@test
def gmsh_embankment_end_segment_just_over_the_size_passes_the_gate():
    """DWR-4441: a 20.0 m last segment of an embankment piece (h = 19.76 m) was cut into
    two 10 m pieces and the triangles at that loose end were 44 m2 < the 45 m2 gate."""
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    L = 3000.0
    end = [1500.0 + 40.0 + 20.0 / np.sqrt(2), 1500.0 + 20.0 / np.sqrt(2)]
    spec = dict(outer=[[0, 0], [L, 0], [L, L], [0, L]], channel=[], points=[],
                lines=[[[1500.0, 1500.0], [1540.0, 1500.0], end]], max_area_m2=5000.0)
    m = GM.build_mesh(spec)                           # raises MeshError below the gate
    s = m["stats"]
    assert s["min_area_m2"] >= s["gate_min_area_m2"] == 45.0, s
    assert s["short_edges_fixed"] >= 1, s
    p = m["points"]
    a, b = np.array([1540.0, 1500.0]), np.array(end)  # no node inside the 20 m segment
    t = ((p - a) @ (b - a)) / ((b - a) @ (b - a))
    d = np.abs((p[:, 0] - a[0]) * (b - a)[1] - (p[:, 1] - a[1]) * (b - a)[0]) / np.hypot(*(b - a))
    assert not ((t > 0.05) & (t < 0.95) & (d < 0.5)).any()


# ------------------------------------------------------------------ 4. culvert size to gmsh
@test
def gmsh_spec_gives_culvert_ends_the_default_meshers_size():
    dem, flooded, inflow, hy, st, roads = _case()
    outer = [[dem.x0, dem.y0 - 2000], [dem.x0 + 3000, dem.y0 - 2000], [dem.x0 + 3000, dem.y0],
             [dem.x0, dem.y0]]
    inlet = AM.inlet_line(inflow, (1.0, 0.0), 60.0)
    cul = [{"end_points": [[dem.x0 + 1500, dem.y0 - 1200], [dem.x0 + 1540, dem.y0 - 1200]]}]
    sp = AM.gmsh_spec(dem, outer, inflow, inlet, cul, [], st, channel=[])
    assert sp["culvert_area_m2"] == 25.0 == AM.culvert_area_m2(st)
    assert "culvert_area_m2" not in AM.gmsh_spec(dem, outer, inflow, inlet, [], [], st, channel=[])
    st.gmsh = {"culvert_area_m2": 40.0}                # an explicit size still wins
    assert AM.gmsh_spec(dem, outer, inflow, inlet, cul, [], st, channel=[])["culvert_area_m2"] == 40.0


# ------------------------------------------------------------------ 5. mesh_filename
@test
def worker_checks_the_mesh_filename_keyword_instead_of_catching_typeerror():
    def v3(bounding_polygon, boundary_tags, mesh_filename=None, **kw):
        return None

    def v4(bounding_polygon, boundary_tags, maximum_triangle_area=None, use_cache=False):
        return None

    def anykw(bounding_polygon, **kw):
        return None
    assert AW.accepts_keyword(v3, "mesh_filename")
    assert not AW.accepts_keyword(v4, "mesh_filename")
    assert not AW.accepts_keyword(anykw, "mesh_filename")   # not named: not passed
    src = open(AW.__file__, encoding="utf-8").read()
    body = src.split("def make_domain", 1)[1].split("breaklines_used = False", 1)[0]
    assert "except TypeError" not in body and "mesh_filename" not in body


# ------------------------------------------------------------------ 1. the bed
@test
def sample_fine_reads_a_memory_map_and_blends_around_no_data():
    d = tempfile.mkdtemp(prefix="elevfine_")
    try:
        a = np.lib.format.open_memmap(os.path.join(d, "f.npy"), mode="w+", dtype=np.float32,
                                      shape=(4, 5))
        a[:] = np.arange(20, dtype=np.float32).reshape(4, 5)
        a[0, 0] = np.nan
        a.flush()
        del a
        m = np.load(os.path.join(d, "f.npy"), mmap_mode="r")
        x0, y0, dx = 100.0, 200.0, 2.0
        # cell centres reproduce the cell values; between them bilinear
        v = AW.sample_fine(m, x0, y0, dx, [x0 + 3.0, x0 + 4.0, x0 + 1.0, x0 + 100.0],
                           [y0 - 3.0, y0 - 3.0, y0 - 1.0, y0 - 1.0])
        assert abs(v[0] - 6.0) < 1e-9 and abs(v[1] - 6.5) < 1e-9, v
        assert np.isnan(v[2]) and np.isnan(v[3])        # the no-data cell alone; off the grid
        w = AW.sample_fine(m, x0, y0, dx, [x0 + 2.0], [y0 - 2.0], chunk=1)
        assert abs(w[0] - np.mean([1.0, 5.0, 6.0])) < 1e-9   # three valid cells blended
        del m
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def fine_elevation_grid_is_written_in_blocks_and_skips_blocks_off_the_mesh():
    fn = lambda x, y: 100.0 + 0.01 * x - 0.02 * y           # noqa: E731
    reads = []

    def read(bounds, cell):
        reads.append(bounds)
        xmin, ymin, xmax, ymax = bounds
        nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
        X, Y = np.meshgrid(xmin + (np.arange(nx) + 0.5) * cell, ymax - (np.arange(ny) + 0.5) * cell)
        return Grid(fn(X, Y), xmin, ymax, cell)
    d = tempfile.mkdtemp(prefix="elevfine_")
    try:
        # an L-shaped mesh: the upper-right blocks are not read
        poly = np.array([[0, 0], [400, 0], [400, 60], [60, 60], [60, 400], [0, 400]], float)
        p = os.path.join(d, "f.npy")
        w = AM.write_fine_elevation(p, poly, 2.0, read, block=32, pad_m=0.0)
        assert (w["nx"], w["ny"]) == (200, 200) and w["blocks_skipped"] > 0, w
        a = np.load(p, mmap_mode="r")
        assert a.dtype == np.float32 and a.shape == (200, 200)
        full = read((0.0, 0.0, 400.0, 400.0), 2.0).data
        ok = np.isfinite(a)
        assert np.allclose(a[ok], full[ok], atol=1e-3)
        assert ok[-1, 0] and ok[0, 0] and ok[-1, -1] and not ok[0, -1]   # top-right skipped
        del a
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def fine_elevation_cell_follows_the_source_with_a_memory_cap():
    st = AM.AnugaSettings(sys.executable)
    b = (0.0, 0.0, 10000.0, 10000.0)
    assert AM.fine_elevation_cell(st, 1.0, b, 40.0)[0] == 1.0
    assert AM.fine_elevation_cell(st, None, b, 40.0)[0] == 2.0        # unknown: 2 m
    small = (0.0, 0.0, 2000.0, 2000.0)
    assert AM.fine_elevation_cell(st, 0.1, small, 40.0)[0] == 0.5     # never finer than 0.5 m
    st.elevation_max_cells = 1e6
    c, why = AM.fine_elevation_cell(st, 1.0, b, 40.0)
    assert c == 10.0 and "million cells" in why, (c, why)
    c, why = AM.fine_elevation_cell(st, 40.0, b, 40.0)
    assert c is None and "not finer" in why
    st.elevation_cell_m = 3.0
    st.elevation_max_cells = 256e6
    assert AM.fine_elevation_cell(st, 1.0, b, 40.0)[0] == 3.0


def _job(**kw):
    dem, flooded, inflow, hy, st, roads = _case()
    for k, v in kw.pop("st", {}).items():
        setattr(st, k, v)
    d = tempfile.mkdtemp(prefix="elevjob_")
    try:   # sys.executable has no anuga: the worker fails after the job file is written
        AM.run(d, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st, refine_lines=roads, **kw)
    except ModelError:
        pass
    return d


def _fine_read(bounds, cell):
    xmin, ymin, xmax, ymax = bounds
    nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
    return Grid(np.full((ny, nx), 290.0), xmin, ymax, cell)


@test
def anuga_job_gets_the_fine_bed_only_when_a_finer_source_is_given():
    base = _job()
    fine = _job(elevation_read=_fine_read, elevation_native_m=2.0, elevation_label="test 2 m")
    off = _job(st={"elevation": "model_grid"}, elevation_read=_fine_read, elevation_native_m=2.0)
    try:
        with np.load(os.path.join(fine, "anuga_job.npz")) as r:
            assert str(r["elev_fine_file"]) == AM.ELEVATION_FILE and float(r["elev_fine_dx"]) == 2.0
        a = np.load(os.path.join(fine, AM.ELEVATION_FILE), mmap_mode="r")
        assert a.dtype == np.float32 and np.nanmax(a) == 290.0
        del a                               # the worker failed: the file is kept to look at
        # ANUGA_ELEVATION=model_grid and no fine source: the job file is the one before 3.10.26
        with np.load(os.path.join(base, "anuga_job.npz")) as r:
            assert not [k for k in r.files if k.startswith("elev_")]
        assert _sha(os.path.join(off, "anuga_job.npz")) == _sha(os.path.join(base, "anuga_job.npz"))
        assert not os.path.exists(os.path.join(off, AM.ELEVATION_FILE))
    finally:
        for d in (base, fine, off):
            shutil.rmtree(d, ignore_errors=True)


@test
def prepare_elevation_reports_the_source_and_why_not():
    dem = Grid(np.zeros((50, 50)), 0.0, 2000.0, 40.0)
    outer = np.array([[100, 100], [1900, 100], [1900, 1900], [100, 1900]], float)
    st = AM.AnugaSettings(sys.executable)
    d = tempfile.mkdtemp(prefix="elevprep_")
    try:
        job, info = AM.prepare_elevation(d, outer, dem, st, None)
        assert job is None and info["mode"] == "model_grid" and "no finer" in info["reason"]
        st.elevation = "model_grid"
        job, info = AM.prepare_elevation(d, outer, dem, st, _fine_read, 1.0)
        assert job is None and info["reason"] == "ANUGA_ELEVATION=model_grid"
        st.elevation = "fine"
        job, info = AM.prepare_elevation(d, outer, dem, st, _fine_read, 5.0, "lidar.vrt")
        assert info["mode"] == "fine" and info["cell_m"] == 5.0 and info["source"] == "lidar.vrt"
        assert job["elev_fine_dx"] == 5.0 and os.path.exists(os.path.join(d, AM.ELEVATION_FILE))

        def broken(bounds, cell):
            raise IOError("disk")
        job, info = AM.prepare_elevation(d, outer, dem, st, broken, 5.0)
        assert job is None and "could not be read" in info["reason"]      # the run goes on
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def anuga_elevation_setting_reads_both_environment_names():
    keep = {k: os.environ.pop(k, None) for k in ("NDBREACH_ANUGA_ELEVATION", "ANUGA_ELEVATION",
                                                 "NDBREACH_ANUGA_ELEVATION_CELL_M")}
    try:
        assert W.RunSettings().anuga_elevation == "fine"
        os.environ["ANUGA_ELEVATION"] = "model_grid"
        assert W.RunSettings().anuga_elevation == "model_grid"
        os.environ["NDBREACH_ANUGA_ELEVATION"] = "fine"
        assert W.RunSettings().anuga_elevation == "fine"
        os.environ["NDBREACH_ANUGA_ELEVATION_CELL_M"] = "2"
        assert W.RunSettings().anuga_elevation_cell_m == 2.0
        d = W.RunSettings().to_dict() if hasattr(W.RunSettings(), "to_dict") else {}
        assert "anuga_elevation" not in d                  # the default: settings as before
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ------------------------------------------------------------------ the crown survives
CROWN_X, GROUND, CROWN = 500.0, 100.0, 102.0


def _crown_dem(cell=1.0, n=1000):
    """Flat ground at 100 m with a 2 m-wide road crown at 102 m along x = 500 (1 m cells)."""
    x = (np.arange(n) + 0.5) * cell
    z = np.where(np.abs(x - CROWN_X) <= 1.0, CROWN, GROUND)
    return Grid(np.tile(z, (n, 1)), 0.0, n * cell, cell)


def _bed(points, tris, grid):
    """What the worker hands ANUGA: the fine terrain at the vertices; ANUGA then makes each
    edge the mean of its two vertices and each centroid the mean of three (checked with
    ANUGA 4.0.1's set_quantity(location='vertices'))."""
    zv = AW.sample_fine(np.asarray(grid.data, np.float32), grid.x0, grid.y0, grid.dx,
                        points[:, 0], points[:, 1])
    tv = zv[tris]
    edges = 0.5 * (tv[:, [1, 2, 0]] + tv[:, [2, 0, 1]])     # edge i opposite vertex i
    return tv.mean(axis=1), edges


def _crown_loss(points, tris, zc, edges, x=CROWN_X):
    """Height lost at the crown as ANUGA sees it, along the line: the higher bed of the two
    triangles each side of it (centroids) and the bed on the crown edge itself."""
    cen = points[tris].mean(axis=1)
    near = np.abs(cen[:, 0] - x) < 15.0
    on = []
    for k in np.flatnonzero(near):
        for i in range(3):
            a, b = points[tris[k, (i + 1) % 3]], points[tris[k, (i + 2) % 3]]
            if abs(a[0] - x) < 1e-6 and abs(b[0] - x) < 1e-6:
                on.append(edges[k, i])
    left = zc[near & (cen[:, 0] < x)].max()
    right = zc[near & (cen[:, 0] > x)].max()
    return CROWN - max(min(left, right), GROUND), (CROWN - max(on)) if on else None


def _strip_mesh(on_line):
    """A structured mesh, 10 m cells; with on_line its nodes sit on the crown line,
    without it the crown runs through the middle of the cells."""
    xs = np.arange(400.0, 601.0, 10.0) + (0.0 if on_line else 5.0)
    ys = np.arange(400.0, 601.0, 10.0)
    X, Y = np.meshgrid(xs, ys)
    P = np.column_stack([X.ravel(), Y.ravel()])
    nx = len(xs)
    T = []
    for j in range(len(ys) - 1):
        for i in range(nx - 1):
            a, b, c, d = j * nx + i, j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1
            T += [[a, b, d], [a, d, c]]
    return P, np.array(T)


@test
def a_2m_crown_on_an_embedded_line_survives_in_the_triangle_bed():
    fine = _crown_dem()
    P, T = _strip_mesh(on_line=True)
    zc, ed = _bed(P, T, fine)
    loss_c, loss_edge = _crown_loss(P, T, zc, ed)
    assert loss_edge is not None and loss_edge < 0.01, loss_edge      # the edge keeps 102 m
    assert loss_c <= (CROWN - GROUND) / 3.0 + 1e-6, loss_c             # centroids keep 2/3
    # the old bed: a 40 m model grid (area mean of the same terrain) at the centroids
    model = Grid(fine.data.reshape(25, 40, 25, 40).mean(axis=(1, 3)), 0.0, 1000.0, 40.0)
    cen = P[T].mean(axis=1)
    zc_old = AW.bilinear(model.data, model.x0, model.y0, model.dx, cen[:, 0], cen[:, 1])
    old_c, _ = _crown_loss(P, T, zc_old, np.repeat(zc_old[:, None], 3, axis=1))
    assert old_c > 0.9 * (CROWN - GROUND), old_c                        # was all but lost
    # the crown line not in the mesh: the same fine terrain keeps much less of it
    P2, T2 = _strip_mesh(on_line=False)
    zc2, ed2 = _bed(P2, T2, fine)
    loss2, edge2 = _crown_loss(P2, T2, zc2, ed2, x=CROWN_X)
    assert edge2 is None and loss2 > 1.5 * (CROWN - GROUND) / 3.0, loss2


@test
def a_2m_crown_survives_in_a_gmsh_mesh_with_embankment_lines_on():
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    fine = _crown_dem()
    base = dict(outer=[[100, 100], [900, 100], [900, 900], [100, 900]], channel=[], points=[],
                max_area_m2=5000.0)
    on = GM.build_mesh(dict(base, lines=[[[CROWN_X, 150.0], [CROWN_X, 850.0]]]))
    off = GM.build_mesh(dict(base, lines=[]))
    zc, ed = _bed(on["points"], on["triangles"], fine)
    loss_on, edge_on = _crown_loss(on["points"], on["triangles"], zc, ed)
    zc2, ed2 = _bed(off["points"], off["triangles"], fine)
    loss_off, _ = _crown_loss(off["points"], off["triangles"], zc2, ed2)
    assert edge_on is not None and edge_on < 0.01, edge_on
    assert loss_on <= (CROWN - GROUND) / 3.0 + 1e-6, loss_on
    assert loss_off > loss_on + 0.5, (loss_off, loss_on)
