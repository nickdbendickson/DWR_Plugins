"""ANUGA culvert inverts and enquiry points (Runner 3.10.71).

DWR-2801 (2026-10-24 review): with ANUGA_ELEVATION=fine the mesh bed is the 1-2 m terrain,
but missing culvert inverts were read from the 30 m model DEM, so 68 culvert ends sat a
median 0.69 m (up to 2.3 m) above the ground they drain; and every enquiry point fell in an
inlet triangle (enquiry_gap 0). No ANUGA needed: the worker's operator call is checked with
fake operators with and without `enquiry_gap`."""
import json
import os
import sys
import tempfile
import warnings

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import structures as S  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


def _model_dem():
    """30 m model DEM: a smeared 2 m road across x = 300 (the coarse cells average it in)."""
    x = 15.0 + 30.0 * np.arange(20)
    z = np.tile(100.0 + 2.0 * np.exp(-((x - 300.0) / 40.0) ** 2), (20, 1))
    return Grid(z.astype(float), 0.0, 600.0, 30.0)


def _fine(tmp, nan_right=False):
    """1 m fine bed: 100 m everywhere, a 3 m road only at 296 < x < 304."""
    z = np.full((600, 600), 100.0, np.float32)
    z[:, 296:304] = 103.0
    if nan_right:
        z[:, 308:] = np.nan            # nothing within 5 m of the x = 315 end
    p = os.path.join(tmp, "fine.npy")
    np.save(p, z)
    return p


def _structure(**kw):
    return S.Structure("culvert", np.array([[285.0, 300.0], [315.0, 300.0]]), 2.0, 1.5, 1,
                       "box", name=kw.pop("name", "C1"), **kw)


@test
def culvert_inverts_without_a_given_value_come_from_the_lowest_fine_ground():
    dem = _model_dem()
    specs, _ = S.anuga_culverts([_structure()], dem)
    assert specs[0]["invert_source"] == ["model_dem", "model_dem"]
    coarse = list(specs[0]["invert_elevations"])
    assert min(coarse) > 101.0, coarse                 # the old inverts: on the smeared road
    with tempfile.TemporaryDirectory() as tmp:
        low = AM.fine_lowest_ground(_fine(tmp), 0.0, 600.0, 1.0)
        notes = S.fine_ground_inverts(specs, low)
        del low
    c = specs[0]
    assert notes == [], notes
    assert c["invert_source"] == ["fine_grid", "fine_grid"]
    assert np.allclose(c["invert_elevations"], [100.0, 100.0])
    assert c["invert_ground_m"] == [100.0, 100.0]
    assert c["invert_search_m"] == 5.0                 # max(apron 2 m, 5 m)
    assert np.allclose(c["invert_model_dem_m"], coarse, atol=1e-3)


@test
def culvert_invert_falls_back_to_the_model_dem_where_there_is_no_fine_ground():
    specs, _ = S.anuga_culverts([_structure()], _model_dem())
    old = list(specs[0]["invert_elevations"])
    with tempfile.TemporaryDirectory() as tmp:
        notes = S.fine_ground_inverts(specs, AM.fine_lowest_ground(_fine(tmp, nan_right=True),
                                                                   0.0, 600.0, 1.0))
    c = specs[0]
    assert c["invert_source"] == ["fine_grid", "model_dem"]
    assert c["invert_elevations"][0] == 100.0 and c["invert_elevations"][1] == old[1]
    assert c["invert_ground_m"] == [100.0, None]
    assert len(notes) == 1 and "no fine ground" in notes[0] and "downstream" in notes[0], notes


@test
def given_culvert_invert_is_kept_within_the_tolerance_and_capped_with_a_warning_above_it():
    specs, _ = S.anuga_culverts([_structure(invert_up_m=100.2, invert_dn_m=101.5)], _model_dem())
    assert specs[0]["invert_source"] == ["given", "given"]
    with tempfile.TemporaryDirectory() as tmp:
        notes = S.fine_ground_inverts(specs, AM.fine_lowest_ground(_fine(tmp), 0.0, 600.0, 1.0))
    c = specs[0]
    assert c["invert_elevations"][0] == 100.2          # 0.2 m above ground: as given
    assert abs(c["invert_elevations"][1] - (100.0 + S.INVERT_CAP_TOL_M)) < 1e-9
    assert c["invert_source"] == ["given", "given_capped"]
    assert c["invert_model_dem_m"] == [None, None]
    assert len(notes) == 1 and notes[0].startswith("WARNING: Culvert C1: the given downstream "
                                                   "invert 101.50 m"), notes
    # a given invert below the ground is never raised
    specs, _ = S.anuga_culverts([_structure(invert_up_m=98.0, invert_dn_m=98.5)], _model_dem())
    S.fine_ground_inverts(specs, lambda xy, r: 100.0)
    assert specs[0]["invert_elevations"] == [98.0, 98.5]
    assert specs[0]["invert_source"] == ["given", "given"]


@test
def fine_lowest_ground_searches_a_disc_and_ignores_no_data():
    with tempfile.TemporaryDirectory() as tmp:
        z = np.full((50, 50), 10.0, np.float32)
        z[25, 31] = 2.0                                # cell centre (31.5, 24.5): 6.5 m east
        z[25, 26] = np.nan
        p = os.path.join(tmp, "f.npy")
        np.save(p, z)
        low = AM.fine_lowest_ground(p, 0.0, 50.0, 1.0)
        assert low((25.0, 24.5), 5.0) == 10.0
        assert low((25.0, 24.5), 7.0) == 2.0
        assert low((500.0, 500.0), 5.0) is None        # off the grid
        z2 = np.full((10, 10), np.nan, np.float32)
        np.save(p, z2)
        assert AM.fine_lowest_ground(p, 0.0, 10.0, 1.0)((5.0, 5.0), 5.0) is None


@test
def enquiry_gap_is_one_local_triangle_edge():
    st = AM.AnugaSettings(sys.executable)
    st.fine_area_m2 = 50.0
    assert AM.enquiry_gap_m(st) == round(1.3 * np.sqrt(4 * 25.0 / np.sqrt(3)), 2) == 9.88
    st.fine_area_m2 = 9.0
    assert AM.enquiry_gap_m(st) == round(1.3 * np.sqrt(4 * 9.0 / np.sqrt(3)), 2)
    pts = AW.enquiry_points([[0.0, 0.0], [30.0, 0.0]], 2.0, 9.88)
    assert np.allclose(pts, [[-11.88, 0.0], [41.88, 0.0]])


class _Inlet:
    def __init__(self, e, tri):
        self.enquiry_index, self.triangle_indices = e, np.asarray(tri)


class _Domain:
    def __init__(self, inside=True):
        self.inside = inside

    def get_boundary_polygon(self):
        return [[-1e3, -1e3], [1e3, -1e3], [1e3, 1e3], [-1e3, 1e3]] if self.inside else \
            [[0.0, -5.0], [30.0, -5.0], [30.0, 5.0], [0.0, 5.0]]

    def get_triangle_containing_point(self, p):
        return 0


class _FakeAnuga:
    """Boyd operators with (ANUGA >= 2.x) or without (a stripped build) enquiry_gap."""
    def __init__(self, with_gap):
        self.calls = []
        fa = self

        def with_kw(domain, losses, width, height=None, barrels=1.0, end_points=None,
                    invert_elevations=None, apron=0.1, manning=0.013, enquiry_gap=0.0,
                    use_momentum_jet=True, use_velocity_head=True, verbose=False):
            fa.calls.append(dict(width=width, enquiry_gap=enquiry_gap, apron=apron,
                                 invert_elevations=invert_elevations))
            return type("Op", (), {"inlets": [_Inlet(7, [1, 2]), _Inlet(9, [3, 9])]})()

        def without_kw(domain, losses, width, height=None, barrels=1.0, end_points=None,
                       invert_elevations=None, apron=0.1, manning=0.013,
                       use_momentum_jet=True, use_velocity_head=True, verbose=False):
            fa.calls.append(dict(width=width, apron=apron))
            return object()
        self.Boyd_box_operator = with_kw if with_gap else without_kw
        self.Boyd_pipe_operator = with_kw if with_gap else without_kw


def _job_culvert(gap=9.88):
    return {"end_points": [[0.0, 0.0], [30.0, 0.0]], "invert_elevations": [100.0, 99.5],
            "width": 2.0, "height": 1.5, "barrels": 1, "shape": "box", "manning": 0.013,
            "losses": 1.0, "apron": 2.0, "name": "C1", "enquiry_gap": gap}


@test
def worker_passes_enquiry_gap_only_to_an_anuga_operator_that_has_it():
    ends = [[0.0, 0.0], [30.0, 0.0]]
    fa = _FakeAnuga(True)
    op, rec = AW.add_boyd_culvert(fa, _Domain(), _job_culvert(), ends)
    assert fa.calls[-1]["enquiry_gap"] == 9.88 and fa.calls[-1]["apron"] == 2.0
    assert fa.calls[-1]["invert_elevations"] == [100.0, 99.5]
    assert rec == {"enquiry_gap_m": 9.88, "enquiry_in_inlet": [False, True]}
    fa = _FakeAnuga(False)                             # no keyword: the call as before
    op, rec = AW.add_boyd_culvert(fa, _Domain(), _job_culvert(), ends)
    assert "enquiry_gap" not in fa.calls[-1]
    assert rec["enquiry_gap_m"] == 0.0 and "no enquiry_gap" in rec["enquiry_note"]
    assert rec["enquiry_in_inlet"] is None
    fa = _FakeAnuga(True)                              # an old job (no key): ANUGA's default
    op, rec = AW.add_boyd_culvert(fa, _Domain(), dict(_job_culvert(), enquiry_gap=None), ends)
    assert fa.calls[-1]["enquiry_gap"] == 0.0 and "enquiry_note" not in rec


@test
def worker_drops_the_enquiry_gap_when_the_point_would_leave_the_mesh():
    fa = _FakeAnuga(True)
    op, rec = AW.add_boyd_culvert(fa, _Domain(inside=False), _job_culvert(),
                                  [[0.0, 0.0], [30.0, 0.0]])
    assert fa.calls[-1]["enquiry_gap"] == 0.0 and len(fa.calls) == 1   # one operator, not two
    assert rec["enquiry_gap_m"] == 0.0 and "outside the mesh" in rec["enquiry_note"]


class _MeshDomain(_Domain):
    def __init__(self, tris):
        _Domain.__init__(self)
        self.tris = np.asarray(tris, float)

    def get_vertex_coordinates(self, absolute=True):
        return self.tris.reshape(-1, 2)


@test
def inlet_clear_gap_goes_past_every_triangle_over_the_inlet_rectangle():
    # end (30, 0) facing +x, 2 m wide, 2 m apron: the rectangle x 30..32, y -1..1
    tris = [[[29, -6], [45, 0], [29, 6]],          # over it, reaches 15 m past the end
            [[31, 3], [40, 3], [35, 9]],           # beside it (y > 1): not an inlet triangle
            [[33, -1], [36, 0], [33, 1]]]          # beyond the apron: not one either
    g = AW.inlet_clear_gap(tris, [30, 0], [1, 0], 2.0, 2.0)
    assert abs(g - (15.0 - 2.0 + AW.ENQUIRY_CLEAR_M)) < 1e-9, g
    assert AW.inlet_clear_gap(tris[1:], [30, 0], [1, 0], 2.0, 2.0) is None
    # a triangle whose box overlaps the rectangle but which does not (separating edge)
    assert AW.inlet_clear_gap([[[31, 3], [35, -1], [35, 3]]], [30, 0], [1, 0],
                              2.0, 2.0) is None
    # rotated: end (0, 0) facing -x
    g = AW.inlet_clear_gap([[[1, -3], [-6, 0], [1, 3]]], [0, 0], [-1, 0], 2.0, 2.0)
    assert abs(g - (6.0 - 2.0 + AW.ENQUIRY_CLEAR_M)) < 1e-9, g


@test
def worker_raises_the_enquiry_gap_where_the_end_triangles_are_large():
    small = [[[-1, -1], [1, -1], [0, 1]], [[29, -1], [31, -1], [30, 1]]]
    fa = _FakeAnuga(True)
    op, rec = AW.add_boyd_culvert(fa, _MeshDomain(small), _job_culvert(),
                                  [[0.0, 0.0], [30.0, 0.0]])
    assert fa.calls[-1]["enquiry_gap"] == 9.88 and "enquiry_note" not in rec   # job gap kept
    big = small + [[[29, -6], [55, 0], [29, 6]]]        # 25 m past the downstream end
    fa = _FakeAnuga(True)
    op, rec = AW.add_boyd_culvert(fa, _MeshDomain(big), _job_culvert(),
                                  [[0.0, 0.0], [30.0, 0.0]])
    assert fa.calls[-1]["enquiry_gap"] == 23.5 and rec["enquiry_gap_m"] == 23.5, fa.calls
    assert "larger than the culvert-end size" in rec["enquiry_note"]
    fa = _FakeAnuga(False)                               # no keyword: nothing raised
    op, rec = AW.add_boyd_culvert(fa, _MeshDomain(big), _job_culvert(),
                                  [[0.0, 0.0], [30.0, 0.0]])
    assert "enquiry_gap" not in fa.calls[-1] and rec["enquiry_gap_m"] == 0.0


@test
def culvert_invert_record_and_summary_for_the_report_and_manifest():
    specs, _ = S.anuga_culverts([_structure(), _structure(name="C2", invert_up_m=101.0)],
                                _model_dem())
    S.fine_ground_inverts(specs, lambda xy, r: 100.0)
    for c in specs:
        c["enquiry_gap"] = 9.88
    rec = S.invert_record(specs)
    assert rec[1]["invert_source"] == ["given_capped", "fine_grid"]
    assert rec[1]["invert_elevations_m"] == [100.3, 100.0]
    rec[0].update({"enquiry_gap_m": 9.88, "enquiry_in_inlet": [False, False]})
    rec[1].update({"enquiry_gap_m": 9.88, "enquiry_in_inlet": [False, True]})
    json.dumps(rec)                                    # goes into the npz stats / run report
    sm = AM.culvert_invert_summary(rec)
    assert sm["invert_source"] == {"fine_grid": 3, "given_capped": 1}
    assert sm["enquiry_in_inlet"] == 1 and sm["enquiry_ends"] == 4
    assert sm["line"] == ("2 culvert(s): inverts 3 fine grid, 1 given capped; enquiry point "
                          "9.88 m past the inlet (1 of 4 end(s) in an inlet triangle)"), sm["line"]
    assert AM.invert_source_line(specs) == "2 culvert(s), 4 end(s): 3 fine grid, 1 given capped"


@test
def model_grid_runs_keep_the_model_dem_inverts():
    """No fine bed (ANUGA_ELEVATION=model_grid): run() does not call fine_ground_inverts, so
    the inverts are the 3.10.69 ones; only the source is recorded."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        specs, _ = S.anuga_culverts([_structure(invert_dn_m=99.0)], _model_dem())
    z = _model_dem().sample_bilinear(np.array([285.0]), np.array([300.0]))
    assert specs[0]["invert_elevations"] == [float(z[0]), 99.0]
    assert specs[0]["invert_source"] == ["model_dem", "given"]


def _run_job(fine):
    """AM.run up to the job file (sys.executable has no anuga, so the worker then fails)."""
    from nd_breach_runner.core.lisflood import ModelError
    from nd_breach_runner.tests.test_gmsh_mesher import _case
    dem, flooded, inflow, hy, st, roads = _case()
    x = dem.x0 + 1600.0
    y = dem.y0 - 1210.0
    s = _structure(name="R1")
    s.line = np.array([[x - 15.0, y], [x + 15.0, y]])
    if not fine:
        st.elevation = "model_grid"

    def read(bounds, cell):                 # a 2 m terrain 6.5 m below the model DEM there
        xmin, ymin, xmax, ymax = bounds
        nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
        return Grid(np.full((ny, nx), 290.0), xmin, ymax, cell)
    d = tempfile.mkdtemp(prefix="culvinv_")
    try:
        AM.run(d, dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st, refine_lines=roads,
               structures=[s], elevation_read=read, elevation_native_m=2.0)
    except ModelError:
        pass
    with np.load(os.path.join(d, "anuga_job.npz")) as r:
        job = json.loads(str(r["culverts_json"]))
    zdem = dem.sample_bilinear(np.array([x - 15.0, x + 15.0]), np.array([y, y]))
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return job, zdem


@test
def anuga_job_culverts_get_fine_ground_inverts_and_an_enquiry_gap():
    job, zdem = _run_job(fine=True)
    c = job[0]
    assert c["invert_elevations"] == [290.0, 290.0], c
    assert c["invert_source"] == ["fine_grid", "fine_grid"]
    assert np.allclose(c["invert_model_dem_m"], zdem, atol=1e-3)
    assert c["enquiry_gap"] == 9.88
    job, zdem = _run_job(fine=False)                   # model grid bed: inverts as before
    c = job[0]
    assert np.allclose(c["invert_elevations"], zdem) and c["invert_source"] == ["model_dem"] * 2
    assert c["enquiry_gap"] == 9.88 and "invert_ground_m" not in c
