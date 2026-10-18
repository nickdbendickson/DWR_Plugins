"""v3.10.22: 2D breaklines (dam row, breach sides, channel banks) written in the .g01 and built
into the computation points, with and without gmsh points (core/hecras_breaklines.py)."""
import os
import re
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_breaklines as hbl
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0
from nd_breach_runner.tests import test_hecras_seeds as ts


def _has_scipy():
    try:
        import scipy.spatial  # noqa: F401
        return True
    except ImportError:
        return False


def _grid(x0, x1, y0, y1, c):
    X, Y = np.meshgrid(np.arange(x0 + c / 2, x1, c), np.arange(y0 + c / 2, y1, c))
    return np.column_stack([X.ravel(), Y.ravel()])


def _blocks(g01):
    """[(name, min, max, repeats, radius, points)] of the BreakLine blocks of a .g01."""
    L = open(g01, encoding="latin-1").read().replace("\r", "").split("\n")
    out = []
    for i, ln in enumerate(L):
        if ln.startswith("BreakLine Name="):
            v = {k: L[i + j].split("=", 1)[1] for j, k in enumerate(
                ("name", "min", "max", "rep", "rad", "poly"))}
            assert [L[i + j].split("=")[0] for j in range(6)] == [
                "BreakLine Name", "BreakLine CellSize Min", "BreakLine CellSize Max",
                "BreakLine Near Repeats", "BreakLine Protection Radius", "BreakLine Polyline"]
            n = int(v["poly"])
            vals = []
            k = i + 6
            while len(vals) < 2 * n:
                s = L[k]
                vals += [float(s[j:j + 16]) for j in range(0, len(s), 16) if s[j:j + 16].strip()]
                k += 1
            out.append((v["name"], float(v["min"]), float(v["max"]), v["rep"], v["rad"],
                        np.array(vals).reshape(-1, 2)))
    return out


@test
def hecras_breaklines_pairs_put_the_faces_on_the_line():
    """Pure: grid points, a slanted line, HEC's enforcement (a pair across the line every
    spacing): the Voronoi faces follow the line; no point is left on or next to it.
    (Absaraka v2b: 98-100 % of each line on a face, HEC's own Muncie mesh 96-98 %, the gmsh
    Muncie seeds 20 %.)"""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    per = [(0.0, 0.0), (2000.0, 0.0), (2000.0, 2000.0), (0.0, 2000.0)]
    pts = _grid(0, 2000, 0, 2000, 100.0)
    line = [(300.0, 400.0), (1500.0, 1300.0)]
    assert hbl.share_on_faces(pts, line) < 0.6            # the grid alone does not follow it
    bl = {"dam": None, "lines": [{"name": "X", "line": line, "near": 50.0, "far": 100.0}]}
    out, st = hbl.enforce(pts, per, bl)
    assert hbl.share_on_faces(out, line) > 0.95, hbl.share_on_faces(out, line)
    assert st["points_added"] > 2 * 1500 / 50 - 4 and st["points_removed"] > 0, st
    d = hbl.poly_dist(out[:, 0], out[:, 1], line)
    assert d.min() > 0.45 * 50.0, d.min()                  # nothing on the line
    from scipy.spatial import cKDTree
    assert cKDTree(out).query(out, k=2)[0][:, 1].min() > 0.4 * 50.0


@test
def hecras_breaklines_channel_strip_and_dam_row():
    """Pure: a curved flow path makes two bank lines one spacing apart with a row of cells
    between them; the dam (on the perimeter) gets an even row half a spacing inside it."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    per = [(0.0, 0.0), (3000.0, 0.0), (3000.0, 4000.0), (0.0, 4000.0)]
    pts = _grid(0, 3000, 0, 4000, 100.0)
    y = np.linspace(3990.0, 10.0, 300)
    path = np.column_stack([1500.0 + 300.0 * np.sin((4000.0 - y) / 600.0), y])
    A, B = (2000.0, 4000.0), (1000.0, 4000.0)       # left to right looking downstream (south)
    plan = hbl.plan(per, 100.0, (A, B), (0.0, -1.0), breach=((1500.0, 4000.0), 40.0, 250.0),
                    path=path)
    names = [b["name"] for b in plan["lines"]]
    assert names == ["Breach left", "Breach right", "Channel left", "Channel right"], names
    assert plan["skipped"] == []
    # left looking downstream (south) is east
    bl_, br_ = (np.asarray(b["line"]) for b in plan["lines"][:2])
    assert bl_[:, 0].mean() > br_[:, 0].mean()
    cl, cr = (np.asarray(b["line"]) for b in plan["lines"][2:])
    # the channel starts below the breach cut and stops two cells from the edge
    assert cl[:, 1].max() < 4000.0 - 250.0 and cl[:, 1].min() >= 200.0 - 1e-6
    gap = hbl.poly_dist(cl[:, 0], cl[:, 1], cr)
    assert np.median(gap) > 0.8 * 50.0 and np.median(gap) < 1.2 * 50.0, np.median(gap)
    out, st = hbl.enforce(pts, per, plan)
    for b in plan["lines"]:
        f = hbl.share_on_faces(out, b["line"])
        assert f > 0.95, (b["name"], f)
    # the dam row: points half the dam spacing inside the dam line, evenly spaced
    row = out[np.abs(out[:, 1] - (4000.0 - 25.0)) < 1e-6]
    row = row[(row[:, 0] > 1000.0) & (row[:, 0] < 2000.0)]
    assert len(row) >= 18, len(row)
    assert hbl.inside(out[:, 0], out[:, 1], per).all()
    # no breaklines asked: no plan lines when the run has no flow path or cut
    p2 = hbl.plan(per, 100.0, (A, B), (0.0, -1.0), breach=None, path=None)
    assert p2["lines"] == [] and "channel: the run has no flow path" in p2["skipped"]


@test
def hecras_breaklines_no_cell_over_8_faces():
    """Pure: a coarse point ringed by fine points (where breakline points meet the grid) would
    be a cell with more than 8 faces, which HEC-RAS refuses: it goes; protected points stay."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    from scipy.spatial import Voronoi
    per = [(0.0, 0.0), (2000.0, 0.0), (2000.0, 2000.0), (0.0, 2000.0)]
    pts = _grid(0, 2000, 0, 2000, 100.0)
    c = np.array([1050.0, 1050.0])
    ang = np.linspace(0, 2 * np.pi, 13)[:-1]
    ring = c + 60.0 * np.column_stack([np.cos(ang), np.sin(ang)])
    far = np.hypot(*(pts - c).T) > 90.0
    allp = np.vstack([pts[far], c, ring])
    prot = np.r_[np.zeros(far.sum() + 1, bool), np.ones(len(ring), bool)]
    cnt = np.bincount(Voronoi(allp).ridge_points.ravel(), minlength=len(allp))
    assert cnt[far.sum()] > 8                               # the coarse centre point
    out, removed, left = hbl.limit_faces(allp, per, 100.0, prot)
    assert removed >= 1 and left == 0, (removed, left)
    assert not (np.hypot(*(out - c).T) < 1e-9).any()        # the centre went
    for q in ring:                                          # the protected ring stayed
        assert (np.hypot(*(out - q).T) < 1e-9).any()
    inner = hbl.ring_dist(out[:, 0], out[:, 1], per) > 75.0
    cnt = np.bincount(Voronoi(out).ridge_points.ravel(), minlength=len(out))
    assert cnt[inner].max() <= 8, cnt[inner].max()


def _with_path(d):
    y = np.linspace(p0.Y0 - 60, p0.Y0 - 5000, 200)
    p0._flow_line(d, [(p0.X0 + 60 * np.sin((p0.Y0 - v) / 400.0), v) for v in y])


@test
def hecras_breaklines_written_and_built_into_the_grid():
    """build_model: BreakLine blocks for the breach sides and the channel banks with HEC's
    keywords and the cell sizes, Conn CellSize on the dam, and the points follow them;
    breaklines=False writes neither and keeps the old grid."""
    if p0._skip() or not _has_scipy():
        print("  (skipped: no GDAL / scipy)")
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        _with_path(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "m"))
        m, info = r["model"], r["info"]
        cell = info["cell"]
        g = r["paths"]["g01"]
        assert p0.unknown_keywords(g) == set(), p0.unknown_keywords(g)
        bl = _blocks(g)
        assert [b[0] for b in bl] == ["Breach left", "Breach right", "Channel left",
                                      "Channel right"], [b[0] for b in bl]
        for name, near, far, rep, rad, line in bl:
            assert far == cell and 0 < near <= 0.5 * cell, (name, near, far)
            assert rep == "0" and rad == "-1"
            assert hm.cond.points_in_polygon(line[:, 0], line[:, 1],
                                             np.asarray(m.perimeter)).all(), name
        txt = open(g, encoding="latin-1").read()
        assert "Conn CellSize Min={}".format(hm.num(0.5 * cell, 2)) in txt
        assert re.search(r"Conn CellSize Max=.*\r?\nConn Near Repeats=1", txt)
        # the order in the file: storage areas, breaklines, then the dam connection
        assert txt.index("Storage Area Is2D=0") < txt.index("BreakLine Name=") < \
            txt.index("Connection=")
        pts = ts._g01_points(g)
        assert len(pts) == info["cells"] == info["breaklines"]["points"]
        for name, near, far, rep, rad, line in bl:
            f = hbl.share_on_faces(pts, line)
            assert f > 0.95, (name, f)
        assert info["breaklines"]["points_added"] > 0
        assert "| Breaklines | Breach left, Breach right, Channel left, Channel right |" in \
            open(r["paths"]["readme"], encoding="utf-8").read()
        assert any(n.startswith("Breaklines: ") for n in r["notes"])
        # off: the old geometry
        r0 = hm.build_model(d, out_folder=os.path.join(d, "m0"), options={"breaklines": False})
        t0 = open(r0["paths"]["g01"], encoding="latin-1").read()
        assert "BreakLine" not in t0 and "Conn CellSize" not in t0
        assert r0["info"]["breaklines"] is None
        assert r0["info"]["cells"] == info["breaklines"]["points_before"]
    finally:
        shutil.rmtree(d)


@test
def hecras_breaklines_with_gmsh_points():
    """mesh_seeds='gmsh': gmsh puts its nodes ON its embedded lines (the Muncie test had the
    cell centres on 'Road 1'); the breaklines take those out and put pairs across the lines,
    so no point sits on a breakline and the faces follow them."""
    if p0._skip() or not _has_scipy() or not ts._has_gmsh():
        print("  (skipped: no GDAL / scipy / gmsh in {})".format(sys.executable))
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        _with_path(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "m"), options={"mesh_seeds": "gmsh"})
        info = r["info"]
        assert info["mesh_seeds"]["used"] == "gmsh"
        g = r["paths"]["g01"]
        assert p0.unknown_keywords(g) == set()
        bl = _blocks(g)
        sz = info["mesh_seeds"]["sizes"]
        near = {b[0]: b[1] for b in bl}
        assert near["Breach left"] == sz["breach_spacing"] and \
            near["Channel left"] == sz["channel_spacing"], near
        pts = ts._g01_points(g)
        for name, s, far, rep, rad, line in bl:
            dist = hbl.poly_dist(pts[:, 0], pts[:, 1], line)
            assert dist.min() > 0.4 * s, (name, dist.min(), s)      # nothing on the line
            assert hbl.share_on_faces(pts, line) > 0.95, name
        assert info["breaklines"]["points_removed"] > 0
        assert info["breaklines"]["dam_spacing"] == sz["dam_spacing"]
        # gmsh nodes of valence 9+ and coarse points among fine ones are gone: no cell with
        # more than 8 faces once clipped to the perimeter (as HEC-RAS builds them)
        per = np.asarray(r["model"].perimeter)
        poly = hm._polygon(per)

        def clip(corners):
            cp = hm._polygon(corners).Intersection(poly)
            rr = hm._rings(cp) if cp is not None and not cp.IsEmpty() else []
            return len(rr[0][1]) if rr else 0

        chk = hbl.voronoi_precheck(pts, per, clip=clip)
        assert chk["max_sides"] <= 8 and chk["facepoints_over"] == 0, chk
        assert info["mesh_seeds"]["over_8_faces_left"] == 0
        assert info["mesh_check"]["voronoi_precheck"]["max_sides"] == chk["max_sides"]
    finally:
        shutil.rmtree(d)


@test
def hecras_breaklines_voronoi_precheck_counts_sides_and_face_points():
    """Pure: the pre-check counts a cell of 9 sides (as HEC-RAS does: "48 cell(s) with more
    than 8 sides", Absaraka v2_gmsh), face points of a square grid (4 cells each, qhull splits
    them), and fix_faces nudges / removes until none is over 8; protected points stay."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    per = [(0.0, 0.0), (2000.0, 0.0), (2000.0, 2000.0), (0.0, 2000.0)]
    pts = _grid(0, 2000, 0, 2000, 100.0)
    chk = hbl.voronoi_precheck(pts, per)
    assert chk["cells"] == len(pts) and not len(chk["over"]), chk
    assert chk["max_cells_per_facepoint"] == 4 and chk["facepoints_over"] == 0, chk
    # a point with 9 neighbours evenly round it: 9 sides
    c = np.array([1000.0, 1000.0])
    ang = np.linspace(0, 2 * np.pi, 10)[:-1]
    ring = c + 80.0 * np.column_stack([np.cos(ang), np.sin(ang)])
    far = np.hypot(*(pts - c).T) > 150.0
    allp = np.vstack([pts[far], c, ring])
    chk = hbl.voronoi_precheck(allp, per)
    i = int(far.sum())
    assert i in chk["over"] and chk["over_interior"] >= 1 and chk["max_sides"] >= 9, chk
    prot = np.zeros(len(allp), bool)
    prot[i] = True                                          # e.g. a breakline pair point
    out, oprot, inf = hbl.fix_faces(allp, per, prot)
    assert inf["left"] == 0 and inf["check"]["max_sides"] <= 8, inf
    assert inf["nudged"] + inf["removed"] >= 1
    assert (np.hypot(*(out - c).T) < 1e-9).any() and oprot.sum() == 1   # it stayed put
    # clipped cells: the clip function decides their sides
    chk = hbl.voronoi_precheck(pts, per, clip=lambda corners: 12)
    assert chk["over_boundary"] == chk["boundary"] > 0 and chk["over_interior"] == 0
