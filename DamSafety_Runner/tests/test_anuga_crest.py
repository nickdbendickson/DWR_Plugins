"""3.10.91: ANUGA gmsh crest hold (core/anuga_crest.py, gmsh_mesher.crest_edges,
anuga_worker.crest_vertex_values).

DWR-2801 Q3 (2026-10-24 rerun): Hwy 18's crest in the ANUGA bed was 938.80 ft where the
1 m DEM has 941.5 ft, because the gmsh triangles straddled the crest and the bed was the
mean across them. Road / rail centrelines in the wet area (+500 ft) now become gmsh mesh
lines, the nodes on them are held at the 1 m crest profile (the highest ground within
+-crest_half_width_m across, the most along between neighbouring nodes) and the worker
raises those vertices (never lowers them). Bridge / notch cut corridors and the inlet are
not held. NDBREACH_ANUGA_CREST=off (or anuga_gmsh {"crest": "off"}) turns it off.
Tiny synthetic data only; gmsh tests skip when gmsh does not import; nothing runs ANUGA."""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_crest as AC  # noqa: E402
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import gmsh_mesher as GM  # noqa: E402
from nd_breach_runner.core import structures as SM  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.core.lisflood import ModelError  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402
from nd_breach_runner.tests.test_gmsh_mesher import _case  # noqa: E402

try:
    import gmsh  # noqa: F401
    HAVE_GMSH = True
except Exception:
    HAVE_GMSH = False

TOP = 2.5          # embankment height above the ground (m)
HALF_TOP = 2.0     # half the crest's flat top (m); side slopes 1V:2H


def _ground(x, y, dem):
    """_case's DEM surface (continuous)."""
    xx = (x - dem.x0) / dem.dx - 0.5
    yy = (dem.y0 - y) / dem.dx - 0.5
    return 300.0 - 0.002 * xx * dem.dx + 0.5 * np.abs(yy - dem.ny / 2) * dem.dx / 100.0


def _dist(x, y, road):
    a, b = np.asarray(road[0], float), np.asarray(road[1], float)
    ab = b - a
    t = np.clip(((x - a[0]) * ab[0] + (y - a[1]) * ab[1]) / ab.dot(ab), 0.0, 1.0)
    return np.hypot(x - a[0] - t * ab[0], y - a[1] - t * ab[1])


def _embankment(x, y, dem, road, dip=None):
    d = _dist(x, y, road)
    z = _ground(x, y, dem) + np.clip(TOP - np.maximum(d - HALF_TOP, 0.0) / 2.0, 0.0, None)
    if dip is not None:                  # a bridge opening: the road dips to the ground
        z = np.where(np.hypot(x - dip[0], y - dip[1]) < 30.0, _ground(x, y, dem), z)
    return z


def _reader(dem, road, calls=None, dip=None):
    def read(bounds, cell):
        if calls is not None:
            calls.append((bounds, cell))
        x0, y0, x1, y1 = bounds
        nx, ny = int(np.ceil((x1 - x0) / cell)) + 1, int(np.ceil((y1 - y0) / cell)) + 1
        xs = x0 + (np.arange(nx) + 0.5) * cell
        ys = y1 - (np.arange(ny) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        return Grid(_embankment(X, Y, dem, road, dip), x0, y1, cell)
    return read


def _road(dem):
    # diagonal (not on the grid), inside _case's flooded rows 40-80
    return [[dem.x0 + 1200.0, dem.y0 - 850.0], [dem.x0 + 2600.0, dem.y0 - 1550.0]]


# ------------------------------------------------------------------ settings
@test
def crest_hold_is_on_by_default_and_has_its_own_toggle():
    o = AC.crest_options(None)
    assert o["on"] and o["crest_pad_m"] == 152.4 and o["crest_cell_m"] == 1.0, o
    assert not AC.crest_options({"crest": "off"})["on"]
    assert not AC.crest_options({"crest": False})["on"]
    # the line band is a different setting (off by default); the crest hold does not follow it
    assert AC.crest_options({"band": "off"})["on"]
    for bad in ({"crest": "maybe"}, {"crest_pad_m": -1}, {"crest_cell_m": 0},
                {"crest_half_width_m": "x"}):
        try:
            AC.crest_options(bad)
        except AC.CrestError:
            continue
        raise AssertionError(bad)
    keys = [v for v, _ in AC.CREST_ENV]
    assert "NDBREACH_ANUGA_CREST" in keys and "NDBREACH_ANUGA_BAND" not in keys
    old = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["NDBREACH_ANUGA_CREST"] = "OFF"
        os.environ["NDBREACH_ANUGA_CREST_PAD_M"] = "300"
        g = AM.gmsh_env_options()
        assert g.get("crest") == "off" and float(g["crest_pad_m"]) == 300.0, g
        o = AC.crest_options(g)
        assert not o["on"] and o["crest_pad_m"] == 300.0
        st = AM.AnugaSettings(sys.executable)
        st.gmsh = {"crest": "sideways"}
        try:
            AM.check_gmsh_options(st)
            raise AssertionError("a bad crest value passed")
        except ModelError as e:
            assert "crest" in str(e), e
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------ lines and profile
@test
def crest_lines_keep_the_road_inside_the_wet_area_plus_its_pad():
    dem, flooded, *_ = _case()
    road = [[dem.x0 + 100.0, dem.y0 - 1200.0], [dem.x0 + 3100.0, dem.y0 - 1200.0]]
    far = [[dem.x0 + 1000.0, dem.y0 - 100.0], [dem.x0 + 2000.0, dem.y0 - 100.0]]
    outer = [[dem.x0, dem.y0], [dem.x0 + 3200.0, dem.y0], [dem.x0 + 3200.0, dem.y0 - 2400.0],
             [dem.x0, dem.y0 - 2400.0]]
    wet = np.zeros_like(flooded)
    wet[50:70, 40:80] = True                     # x 800-1600 m
    pieces, info = AC.crest_lines([road, far], outer, (dem, wet))
    assert len(pieces) == 1 and info["crest_lines"] == 1, info
    xs = pieces[0][:, 0] - dem.x0
    assert 795.0 <= xs.min() <= 810.0 and 1590.0 <= xs.max() <= 1605.0, (xs.min(), xs.max())
    assert len(AC.crest_lines([road, far], outer)[0]) == 2        # no wet mask: both


@test
def crest_profile_is_the_1m_max_across_the_crest():
    dem, *_ = _case()
    road = _road(dem)
    calls = []
    s, z = AC.crest_profile(np.asarray(road), _reader(dem, road, calls), 1.0, 3.0,
                            chunk_m=400.0)
    L = float(np.hypot(*np.subtract(road[1], road[0])))
    assert abs(s[-1] - L) < 1e-6 and len(calls) >= 4 and all(c == 1.0 for _, c in calls)
    p = np.asarray(road[0]) + np.outer(s / L, np.subtract(road[1], road[0]))
    want = _ground(p[:, 0], p[:, 1], dem) + TOP
    assert np.nanmax(np.abs(z - want)) < 0.05, np.nanmax(np.abs(z - want))
    # a model-DEM-like coarse read misses most of the crown; that is what Q3 saw
    s2, z2 = AC.crest_profile(np.asarray(road), _reader(dem, road), 20.0, 0.0)
    assert np.nanmean(want[::20][:len(z2)] - z2[:len(want[::20])]) > 0.3


@test
def hold_crest_takes_the_window_max_and_keeps_cuts_and_the_inlet_out():
    piece = np.array([[0.0, 0.0], [100.0, 0.0]])
    s = np.arange(0.0, 100.5, 1.0)
    z = np.full_like(s, 10.0)
    z[47] = 12.0                                 # a narrow high spot between nodes
    pts = np.array([[0.0, 0.3], [25.0, -0.2], [50.0, 0.0], [75.0, 0.1], [100.0, 0.0],
                    [50.0, 30.0]])               # the last is not on the line
    idx, zz, info = AC.hold_crest(pts, [0, 1, 2, 3, 4, 5], [piece], [(s, z)], 5.0)
    got = dict(zip(idx.tolist(), zz.tolist()))
    assert info["held"] == 5 and info["off_line"] == 1, info
    assert got[2] == 12.0 and got[1] == 10.0 and got[3] == 10.0, got
    cuts = [(np.array([[50.0, -20.0], [50.0, 20.0]]), 8.0, "bridge")]
    ko = AC.keep_out_mask(pts[[0, 1, 2, 3, 4, 5]], cuts, inlet=np.array([[-30.0, -5.0],
                                                                          [-30.0, 5.0]]))
    assert ko.tolist() == [True, False, True, False, False, False], ko.tolist()
    idx, zz, info = AC.hold_crest(pts, [0, 1, 2, 3, 4, 5], [piece], [(s, z)], 5.0, ko)
    assert sorted(idx.tolist()) == [1, 3, 4] and info["kept_out"] == 2, (idx, info)
    nan = np.full_like(s, np.nan)
    assert AC.hold_crest(pts, [1], [piece], [(s, nan)], 5.0)[2]["no_profile"] == 1


@test
def worker_raises_crest_vertices_only_never_lowers():
    vxy = np.array([[0, 0], [10, 0], [5, 5], [10, 0], [10, 10], [5, 5]], float)
    vv = np.array([[1.0, 1.0, 2.0], [1.0, 1.0, 9.0]])
    new, info = AW.crest_vertex_values(vxy, vv, np.array([[5.0, 5.0], [99.0, 99.0]]),
                                       np.array([4.0, 7.0]))
    assert new.tolist() == [[1.0, 1.0, 4.0], [1.0, 1.0, 9.0]], new.tolist()   # 9 stays
    assert info["vertices_matched"] == 2 and info["vertices_raised"] == 1, info
    assert abs(info["max_raise_m"] - 2.0) < 1e-9
    assert vv[0, 2] == 2.0                                   # the input is not changed


# ------------------------------------------------------------------ mesher
@test
def gmsh_crest_edges_are_the_line_edges_along_a_crest_not_crossings():
    nodes = np.array([[0, 0], [20, 0], [40, 0], [20, -30], [20, 30], [0, 100], [40, 100]],
                     float)
    edges = np.array([[0, 1], [1, 2], [3, 1], [1, 4], [5, 6], [0, 1]])
    kinds = ["line", "line", "line", "line", "line", "channel"]
    crest = [[[-10.0, 0.5], [60.0, 0.5]]]
    m = GM.crest_edges(nodes, edges, kinds, crest, 19.8)
    assert m.tolist() == [True, True, False, False, False, False], m.tolist()
    assert not GM.crest_edges(nodes, edges, kinds, [], 19.8).any()


@test
def gmsh_spec_puts_crest_lines_in_the_mesh_lines_and_keeps_settings_out():
    dem, *_ = _case()
    outer = np.array([[dem.x0, dem.y0], [dem.x0 + 3200.0, dem.y0],
                      [dem.x0 + 3200.0, dem.y0 - 2400.0], [dem.x0, dem.y0 - 2400.0]])
    st = AM.AnugaSettings(sys.executable)
    st.gmsh = {"crest": "on", "crest_pad_m": 100.0}
    road = np.asarray(_road(dem))
    inflow = dem.xy(60, 15)
    spec = AM.gmsh_spec(dem, outer, inflow, AM.inlet_line(inflow, (1.0, 0.0), 40.0), [], [], st,
                        channel=[], embankments=[], crest_lines=[road])
    assert len(spec["crest_lines"]) == 1 and len(spec["lines"]) == 2, spec["lines"]
    assert not any(k in spec for k in AC.CREST_DEFAULTS), sorted(spec)
    spec0 = AM.gmsh_spec(dem, outer, inflow, AM.inlet_line(inflow, (1.0, 0.0), 40.0), [], [],
                         st, channel=[], embankments=[])
    assert "crest_lines" not in spec0 and len(spec0["lines"]) == 1


def _run(workdir, st, road, dip=None, structures=None):
    dem, flooded, inflow, hy, _st, refine = _case()
    try:   # sys.executable has no anuga: the worker fails after the job file is written
        AM.run(workdir, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st, refine_lines=refine,
               embankment_lines=[road], terrain_read=_reader(dem, road, dip=dip),
               structures=structures)
    except ModelError:
        pass
    return np.load(os.path.join(workdir, "anuga_job.npz"), allow_pickle=False), dem


@test
def gmsh_run_holds_the_crest_at_its_1m_elevation_and_no_triangle_straddles_it():
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    d = tempfile.mkdtemp(prefix="crest_")
    try:
        dem, flooded, inflow, hy, st, _ = _case()
        st.mesher = "gmsh"
        road = _road(dem)
        dip = (0.5 * (road[0][0] + road[1][0]), 0.5 * (road[0][1] + road[1][1]))
        bridge = [SM.Structure(kind=SM.KIND_BRIDGE, width_m=20.0, height_m=3.0, name="B1",
                               line=np.array([[dip[0] - 15.0, dip[1] + 30.0],
                                              [dip[0] + 15.0, dip[1] - 30.0]]))]
        job, dem = _run(os.path.join(d, "on"), st, road, dip=dip, structures=bridge)
        assert "crest_xy" in job.files and len(job["crest_z"]) >= 20, job.files
        cxy, cz = job["crest_xy"], job["crest_z"]
        dd = _dist(cxy[:, 0], cxy[:, 1], road)
        assert dd.max() < 1.0, dd.max()                       # on the centreline
        top = _ground(cxy[:, 0], cxy[:, 1], dem) + TOP
        assert np.max(np.abs(cz - top)) < 0.25, np.max(np.abs(cz - top))   # the 1 m crest
        assert np.min(np.hypot(cxy[:, 0] - dip[0], cxy[:, 1] - dip[1])) > 20.0  # bridge out
        # no triangle has corners well on both sides of the crest (away from its ends)
        P, T = job["mesh_points"], job["mesh_triangles"]
        a, b = np.asarray(road[0]), np.asarray(road[1])
        u = (b - a) / np.hypot(*(b - a))
        rel = P - a
        side = rel[:, 0] * u[1] - rel[:, 1] * u[0]
        along = rel @ u
        L = np.hypot(*(b - a))
        tri_in = np.all((along[T] > 40.0) & (along[T] < L - 40.0), axis=1)
        sd = side[T][tri_in]
        straddle = (sd.max(axis=1) > 0.5) & (sd.min(axis=1) < -0.5)
        assert tri_in.sum() > 20 and not straddle.any(), (tri_in.sum(), straddle.sum())
        # off: no crest keys; the road is only a mesh line where it is an embankment
        st2 = _case()[4]
        st2.mesher = "gmsh"
        st2.gmsh = {"crest": "off"}
        job2, _ = _run(os.path.join(d, "off"), st2, road, dip=dip, structures=bridge)
        assert "crest_xy" not in job2.files and "crest_z" not in job2.files
        spec = json.load(open(os.path.join(d, "on", "gmsh_mesh_in.json")))
        assert spec.get("crest_lines"), sorted(spec)
        spec2 = json.load(open(os.path.join(d, "off", "gmsh_mesh_in.json")))
        assert "crest_lines" not in spec2
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def default_mesher_job_has_no_crest_keys():
    d = tempfile.mkdtemp(prefix="crest_def_")
    try:
        dem, flooded, inflow, hy, st, _ = _case()
        job, _ = _run(d, st, _road(dem))
        assert "crest_xy" not in job.files and "mesh_points" not in job.files
    finally:
        shutil.rmtree(d, ignore_errors=True)
