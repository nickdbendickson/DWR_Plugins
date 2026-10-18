"""v3.10.22: gmsh point layout as the HEC-RAS 2D computation points (core/hecras_seeds.py,
build_model option mesh_seeds='gmsh', off by default): the points are written as 'Storage Area
2D Points', finer along the dam and breach, none on the perimeter; the setting and the gmsh
version go to run_report.json and the package manifest; without gmsh the build falls back to
the regular grid and says why. The gmsh tests skip when this Python has no gmsh (the QGIS
Python usually has none); the fallback tests always run."""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import hecras_seeds as hs
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0


def _has_gmsh():
    return hs.gmsh_version_inprocess() is not None


def _g01_points(path):
    t = open(path, encoding="ascii").read().replace("\r", "").split("\n")
    k = next(i for i, ln in enumerate(t) if ln.startswith("Storage Area 2D Points="))
    n = int(t[k].split("=")[1])
    vals = []
    i = k + 1
    while len(vals) < 2 * n:
        s = t[i]
        vals += [float(s[j:j + 16]) for j in range(0, len(s), 16) if s[j:j + 16].strip()]
        i += 1
    return np.asarray(vals).reshape(-1, 2)


class _NoGmsh(object):
    """`import gmsh` fails in this Python while it is active."""

    def __enter__(self):
        self.old = sys.modules.get("gmsh", "missing")
        sys.modules["gmsh"] = None
        return self

    def __exit__(self, *a):
        if self.old == "missing":
            sys.modules.pop("gmsh", None)
        else:
            sys.modules["gmsh"] = self.old


@test
def hecras_seeds_mode_and_interpreters():
    assert hs.mode_of({}, env={}) == "grid"
    assert hs.mode_of({"mesh_seeds": "gmsh"}, env={}) == "gmsh"
    assert hs.mode_of({}, env={hs.ENV_MODE: "gmsh"}) == "gmsh"
    assert hs.mode_of({"mesh_seeds": "grid"}, env={hs.ENV_MODE: "gmsh"}) == "grid"
    assert hs.mode_of({"mesh_seeds": "off"}, env={}) == "grid"
    try:
        hs.mode_of({"mesh_seeds": "quads"}, env={})
    except ValueError:
        pass
    else:
        raise AssertionError("a bad mesh_seeds was accepted")
    assert hs.interpreters({"gmsh_python": "/a"}, env={hs.ENV_PYTHON: "/b",
                                                       hs.ENV_ANUGA_PYTHON: "/a"}) == \
        [("/a", "option gmsh_python"), ("/b", hs.ENV_PYTHON)]


@test
def hecras_seeds_clean_drops_perimeter_and_outside_nodes():
    ring = [(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)]
    pts = np.array([[0.0, 0.0], [50.0, 0.0], [50.0, 50.0], [50.0, 3.0], [150.0, 50.0],
                    [10.0, 10.0]])
    tri = np.array([[0, 1, 5], [1, 2, 5], [3, 2, 5], [2, 4, 3]])
    keep, h = hs.clean_seeds(pts, tri, ring)
    got = {tuple(p) for p in keep}
    assert (50.0, 50.0) in got and (10.0, 10.0) in got, got
    assert (0.0, 0.0) not in got and (50.0, 0.0) not in got and (150.0, 50.0) not in got
    assert (50.0, 3.0) not in got     # 3 from the edge, closer than 0.45 x its ~41 spacing


@test
def hecras_seeds_fallback_to_the_grid_without_gmsh():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        grid = hm.build_model(d, out_folder=os.path.join(d, "g"))
        with _NoGmsh():
            r = hm.build_model(d, out_folder=os.path.join(d, "m"),
                               options={"mesh_seeds": "gmsh",
                                        "gmsh_python": os.path.join(d, "no_such_python")})
        si = r["info"]["mesh_seeds"]
        assert si["asked"] == "gmsh" and si["used"] == "grid", si
        assert "not installed in this Python" in si["why"] and "no_such_python" in si["why"], si
        assert any("gmsh computation points were asked for but not used" in n
                   for n in r["notes"]), r["notes"]
        # the regular grid, as without the option
        a = _g01_points(r["paths"]["g01"])
        b = _g01_points(grid["paths"]["g01"])
        assert a.shape == b.shape and np.allclose(a, b)
        rep = json.load(open(os.path.join(d, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON)))
        assert rep["hecras_mesh_seeds"]["asked"] == "gmsh" and \
            rep["hecras_mesh_seeds"]["used"] == "grid"
        assert "gmsh asked for, not available" in open(r["paths"]["readme"]).read()
        # default off: nothing asked, the grid, recorded as such
        assert grid["info"]["mesh_seeds"] == {"asked": "grid", "used": "grid",
                                              "grid_points": grid["info"]["cells"]}
        with _NoGmsh():
            try:
                hs.make_seeds([(0, 0), (10, 0), (10, 10)], 1.0, options={}, env={})
            except hs.SeedError as e:
                assert "no other Python with gmsh given" in str(e), e
            else:
                raise AssertionError("no SeedError without gmsh")
    finally:
        shutil.rmtree(d)


@test
def hecras_seeds_gmsh_points_written_and_recorded():
    if p0._skip() or not _has_gmsh():
        print("  (skipped: no gmsh in {})".format(sys.executable))
        return
    from nd_breach_runner.core import hecras_package as hp
    import datetime
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        r = hm.build_model(d, out_folder=os.path.join(d, "m"), options={"mesh_seeds": "gmsh"})
        si, m = r["info"]["mesh_seeds"], r["model"]
        assert si["used"] == "gmsh" and si["gmsh_version"] == hs.gmsh_version_inprocess(), si
        pts = _g01_points(r["paths"]["g01"])
        assert len(pts) == si["seeds"] == r["info"]["cells"] == len(m.points)
        # none on or outside the perimeter
        per = np.asarray(m.perimeter)
        dist = hm.point_segment_distance(pts[:, 0], pts[:, 1],
                                         list(map(tuple, per)) + [tuple(per[0])])
        assert dist.min() > 0.05 * r["info"]["cell"], dist.min()
        assert hs._inside(pts[:, 0], pts[:, 1], per).all()
        # finer near the dam and breach than far down the valley
        A, B = np.asarray(m.conn_line[0]), np.asarray(m.conn_line[-1])
        dd = hm.point_segment_distance(pts[:, 0], pts[:, 1], [tuple(A), tuple(B)])
        cell = r["info"]["cell"]
        from scipy.spatial import cKDTree
        sp = cKDTree(pts).query(pts, k=2)[0][:, 1]           # spacing to the nearest point
        near, far = np.median(sp[dd < cell]), np.median(sp[dd > 30 * cell])
        assert near < 0.5 * far and 0.7 * cell < far < 1.4 * cell, (near, far, cell)
        assert si["sizes"]["dam_spacing"] == 0.25 * cell
        rep = json.load(open(os.path.join(d, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON)))
        assert rep["hecras_mesh_seeds"]["used"] == "gmsh"
        assert rep["hecras_mesh_seeds"]["gmsh_version"] == si["gmsh_version"]
        assert p0.unknown_keywords(r["paths"]["g01"]) == set()
        p = hp.build_package(d, when=datetime.date(2026, 10, 18), make_zip=False,
                             options={"mesh_seeds": "gmsh"})
        man = json.load(open(os.path.join(p["folder"], hp.MANIFEST)))
        assert man["mesh_seeds"]["used"] == "gmsh" and "gmsh_stats" not in man["mesh_seeds"]
        assert man["cells"] == man["mesh_seeds"]["seeds"]
    finally:
        shutil.rmtree(d)


@test
def hecras_seeds_gmsh_in_another_python():
    """gmsh is not in this Python, but in the one given (the ANUGA environment, say)."""
    if not _has_gmsh():
        print("  (skipped: no gmsh in {})".format(sys.executable))
        return
    ring = [(0.0, 0.0), (2000.0, 0.0), (2000.0, 1500.0), (0.0, 1500.0)]
    with _NoGmsh():
        pts, info = hs.make_seeds(ring, 100.0, options={"gmsh_python": sys.executable}, env={},
                                  dam_line=((0.0, 1500.0), (2000.0, 1500.0)),
                                  downstream=(0.0, -1.0), breach_xy=(1000.0, 1500.0))
    assert "option gmsh_python" in info["python"] and info["gmsh_version"], info
    assert info["tried_first"] and "not installed in this Python" in info["tried_first"][0]
    assert 300 < len(pts) < hs.MIN_SEED_CAP, len(pts)
