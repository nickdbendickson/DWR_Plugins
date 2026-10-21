"""ANUGA_MESHER (core/gmsh_mesher.py, anuga_model gmsh path, anuga_worker external mesh).

The default mesher must leave the ANUGA job file byte-identical to main; the
gmsh mesher must join crossing lines, keep the quality gate (20 deg, 100 m2),
tag every outer edge, and report why on failure or timeout (test_gmsh_gate.py:
the strict / gmsh_or_default handling of that).
gmsh tests are skipped when gmsh is not importable. Nothing here runs ANUGA."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import gmsh_mesher as GM  # noqa: E402
from nd_breach_runner.core import breach as br  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.core.lisflood import ModelError  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402

try:
    import gmsh  # noqa: F401
    HAVE_GMSH = True
except Exception:
    HAVE_GMSH = False

# sha256 of anuga_job.npz written by origin/main 89cb655 for _case() (default mesher),
# with the built-in cell-edge outline tracer. terrain.mask_outline uses contourpy instead
# when it is installed (the qgis334 env has it, venv_scipy does not), and that traces the
# mesh polygons up to one cell differently, so the pin is checked with the tracer forced.
MAIN_JOB_SHA256 = "78c4969f0a72a424c859367b052e224c51d85d7984a46acb964ff873e3f03511"


def _case():
    ny, nx, dx = 120, 160, 20.0
    yy, xx = np.mgrid[0:ny, 0:nx]
    z = 300.0 - 0.002 * xx * dx + 0.5 * np.abs(yy - ny / 2) * dx / 100.0
    dem = Grid(z, 500000.0, 5200000.0, dx)
    flooded = np.zeros((ny, nx), bool)
    flooded[40:80, 10:150] = True
    inflow = dem.xy(60, 15)
    hy = br.Hydrograph(np.array([0.0, 600.0, 3600.0]), np.array([0.0, 100.0, 0.0]), "test")
    # main 89cb655's arrival depth (1 ft; the default is 0.1 ft since 3.10.45) so the pin still
    # checks the mesh and the job layout, not that default
    st = AM.AnugaSettings(sys.executable, sim_time_s=3600.0, arrival_depth_m=0.3048)
    road = [[dem.x0 + 1600.0, dem.y0 - 700.0], [dem.x0 + 1600.0, dem.y0 - 1700.0]]
    return dem, flooded, inflow, hy, st, [road]


def _job(st_mesher=None, workdir=None):
    dem, flooded, inflow, hy, st, roads = _case()
    if st_mesher is not None:
        st.mesher = st_mesher
    workdir = workdir or tempfile.mkdtemp(prefix="gmshjob_")
    try:   # sys.executable has no anuga: the worker fails after the job file is written
        AM.run(workdir, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st, refine_lines=roads)
    except ModelError:
        pass
    return os.path.join(workdir, "anuga_job.npz")


def _sha(p):
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@test
def anuga_default_mesher_job_file_is_byte_identical_to_main():
    a = _job()                       # settings without touching mesher
    b = _job("default")
    assert _sha(a) == _sha(b)
    with np.load(a) as r:
        assert not [k for k in r.files if k.startswith("mesh_")]
    from nd_breach_runner.core import terrain
    real = terrain._have_contourpy
    terrain._have_contourpy = lambda: False          # the outline tracer main's pin used
    try:
        c = _job()
        d = _job("default")
    finally:
        terrain._have_contourpy = real
    assert _sha(c) == _sha(d)
    assert _sha(c) == MAIN_JOB_SHA256, _sha(c)


@test
def anuga_unknown_mesher_uses_the_default_job():
    assert _sha(_job("nonsense")) == _sha(_job())


@test
def gmsh_node_lines_joins_crossings_and_snaps():
    nodes, edges, kinds = GM.node_lines(
        [[[0, 0], [100, 0]], [[50, -50], [50, 50]], [[0.3, 0.2], [0, -40]]],
        ["line", "line", "channel"], snap_m=1.0)
    assert any(np.allclose(n, [50, 0]) for n in nodes)          # shared crossing node
    deg = np.bincount(edges.ravel(), minlength=len(nodes))
    k = [i for i, n in enumerate(nodes) if np.allclose(n, [50, 0])][0]
    assert deg[k] == 4
    assert sum(np.allclose(n, [0, 0], atol=1.0) for n in nodes) == 1   # snapped
    assert len(edges) == 5 and kinds.count("channel") == 1


@test
def gmsh_boundary_edges_follow_anuga_edge_numbering():
    tri = np.array([[0, 1, 2], [0, 2, 3]])
    b = GM.boundary_edges(tri)
    # tri 0: edge0 (1-2), edge2 (0-1); tri 1: edge0 (2-3), edge1 (3-0)
    assert b.tolist() == [[0, 0], [0, 2], [1, 0], [1, 1]]


def _spec():
    L = 6000.0
    return dict(outer=[[0, 0], [L, 0], [L, L], [0, L]],
                lines=[[[3000, 300], [3000, 5700]], [[300, 2000], [5700, 2000]],
                       [[1000, 2900], [1000, 3100]]],
                channel=[[[600, 3000], [2000, 3400], [3500, 2600], [5500, 3000]]],
                points=[[1000, 2900], [1000, 3100], [600, 3000]], max_area_m2=20000.0)


@test
def gmsh_mesh_meets_the_quality_gate_and_is_coarser_than_uniform():
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    m = GM.build_mesh(_spec())
    s = m["stats"]
    assert s["min_angle_deg"] >= 20.0, s
    assert s["min_area_m2"] >= 50.0, s
    tri, pts = m["triangles"], m["points"]
    p0, p1, p2 = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    cr = (p1[:, 0] - p0[:, 0]) * (p2[:, 1] - p0[:, 1]) - (p1[:, 1] - p0[:, 1]) * (p2[:, 0] - p0[:, 0])
    assert (cr > 0).all()                                         # CCW for ANUGA
    assert abs(cr.sum() / 2.0 - 6000.0 ** 2) < 1.0                # covers the domain
    # every boundary edge lies on the outer square
    for t, e in m["boundary"]:
        a, b = pts[tri[t, (e + 1) % 3]], pts[tri[t, (e + 2) % 3]]
        on = lambda p: min(p[0], p[1], 6000 - p[0], 6000 - p[1]) < 1e-6
        assert on(a) and on(b)
    # a crossing of the two roads is a mesh node
    assert np.min(np.hypot(*(pts - [3000, 2000]).T)) < 1e-6
    # far fewer triangles than a uniform mesh at the 100 m2 line size
    assert s["triangles"] < 0.25 * 36e6 / 100.0, s


@test
def gmsh_quality_gate_rejects_small_triangles():
    pts = np.array([[0, 0], [5, 0], [0, 5], [100, 100]], float)
    try:
        GM.finish(np.arange(1, 5), pts, np.array([[1, 2, 3]]),
                  dict(min_angle_deg=20.0, min_area_m2=100.0, min_area_tol=0.5))
    except GM.MeshError as e:
        assert "below" in str(e)
    else:
        raise AssertionError("expected MeshError")


@test
def gmsh_mesher_runs_as_a_script_and_reaches_the_job():
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    d = tempfile.mkdtemp(prefix="gmshrun_")
    st = AM.AnugaSettings(sys.executable)
    st.mesher = "gmsh"
    dem, flooded, inflow, hy, _, roads = _case()
    outer, _ = AM.mesh_polygons(dem, flooded, inflow, st)
    spec = AM.gmsh_spec(dem, outer, inflow, AM.inlet_line(inflow, (1.0, 0.0), 60.0), [], roads, st)
    mesh, note = AM.make_gmsh_mesh(d, spec, st)
    assert mesh is not None, note
    assert "gmsh" in note
    job = _job("gmsh", d)
    with np.load(job) as r:
        assert r["mesh_triangles"].shape[1] == 3 and len(r["mesh_boundary"]) > 0
        assert r["mesh_points"].min() > 1e5          # model CRS, the worker shifts it
    shutil.rmtree(d, ignore_errors=True)


@test
def gmsh_failure_and_timeout_return_no_mesh_and_the_reason():
    d = tempfile.mkdtemp(prefix="gmshfail_")
    st = AM.AnugaSettings(sys.executable)
    mesh, why = AM.make_gmsh_mesh(d, dict(outer=[[0, 0], [1, 0]]), st)   # bad ring
    assert mesh is None and "fewer than 3 points" in why, why
    slow = os.path.join(d, "slow.py")
    with open(slow, "w") as fh:
        fh.write("import time; time.sleep(30)\n")
    st.python, st.gmsh_timeout_s = sys.executable, 1.0
    old = AM.MESHER
    AM.MESHER = slow
    try:
        mesh, note = AM.make_gmsh_mesh(d, {}, st)
    finally:
        AM.MESHER = old
    assert mesh is None and "over 1 s" in note and "timeout" in note
    shutil.rmtree(d, ignore_errors=True)


@test
def anuga_check_reports_gmsh_without_failing():
    r = AM.check_gmsh(sys.executable)
    assert "gmsh" in r and "gmsh_message" in r
    assert (r["gmsh"] is not None) == HAVE_GMSH, r


@test
def gmsh_mesh_builds_an_anuga_domain_when_anuga_is_installed():
    """Builds anuga.Domain from the gmsh arrays (no evolve). Runs only when anuga imports."""
    try:
        import anuga
    except Exception:
        print("skip: anuga not importable")
        return
    if not HAVE_GMSH:
        return
    m = GM.build_mesh(_spec())
    bnd = {(int(t), int(e)): "outflow" for t, e in m["boundary"]}
    dom = anuga.Domain(m["points"].tolist(), m["triangles"].tolist(), bnd,
                       geo_reference=anuga.Geo_reference(xllcorner=0.0, yllcorner=0.0))
    assert len(dom) == len(m["triangles"])
    assert set(dom.get_boundary_tags()) == {"outflow"}
