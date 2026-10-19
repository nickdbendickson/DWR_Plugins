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
    """Pure: a curved flow path makes two bank lines one full cell apart (v2c; v2b: half a
    cell, which held HEC-RAS's time step at half of v2's) with a row of cells between them;
    the dam (on the perimeter) gets an even row half a spacing inside it, finer across the
    breach: cells centred on the breach centre and on both bottom corners (v2d; v2c put a
    face on each corner and HEC-RAS warned 'weir breach lower than the cells')."""
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
    assert np.median(gap) > 0.8 * 100.0 and np.median(gap) < 1.2 * 100.0, np.median(gap)
    assert plan["strip"]["h"] == 100.0 and plan["lines"][2]["near"] == 100.0
    # v2d: 40 ft cells (80 ft bottom / BREACH_DAM_CELLS), centred on the breach centre and on
    # both bottom corners (v2c: 20 ft cells with a face on each corner)
    assert plan["dam"]["breach"]["near"] == 40.0, plan["dam"]["breach"]
    out, st = hbl.enforce(pts, per, plan)
    for b in plan["lines"]:
        f = hbl.share_on_faces(out, b["line"])
        assert f > 0.95, (b["name"], f)
    # the dam row: points half the dam spacing inside the dam line, evenly spaced
    row = out[np.abs(out[:, 1] - (4000.0 - 25.0)) < 2.5]       # half the (fitted) spacing
    row = row[(row[:, 0] > 1000.0) & (row[:, 0] < 2000.0)]
    assert len(row) >= 12, len(row)
    assert (np.abs(row[:, 0] - 1500.0) > 60.0).all()           # coarse away from the breach
    fine = out[(np.abs(out[:, 1] - (4000.0 - 20.0)) < 1e-6) & (np.abs(out[:, 0] - 1500.0) < 45.0)]
    assert np.allclose(np.sort(fine[:, 0]), [1460.0, 1500.0, 1540.0]), fine
    # its cells grow by at most 1.3 a cell from the breach row to the dam spacing, and no face
    # lies on a bottom corner
    c, w = hbl.dam_stations(1000.0, 50.0, 500.0, 40.0, 40.0)
    assert abs(w.sum() - 1000.0) < 1e-6 and w.min() == 40.0 and w.max() <= 50.0 + 1e-6
    assert (w[1:] / w[:-1]).max() <= 1.3 + 1e-6, w
    faces = np.r_[0.0, np.cumsum(w)]
    assert np.abs(faces[:, None] - np.array([460.0, 540.0])[None, :]).min() >= 20.0 - 1e-6
    for half, ds in ((40.4, 50.0), (40.4, 25.0), (10.0, 50.0), (150.0, 50.0), (3.9, 20.0)):
        sb = hbl.breach_row_spacing(half, ds, 100.0 if ds >= 25.0 else 40.0)
        c, w = hbl.dam_stations(1700.0, ds, 850.0, half, sb)
        assert abs(w.sum() - 1700.0) < 1e-6
        faces = np.r_[0.0, np.cumsum(w)]
        gap = np.abs(faces[:, None] - np.array([850.0 - half, 850.0 + half])).min()
        k = round(half / sb)
        if k >= 1:          # cells centred on the centre and on both corners
            assert sb <= ds + 1e-9 and abs(half / sb - k) < 1e-9, (half, ds, sb)
            assert gap >= 0.5 * sb - 1e-6, (half, ds)
            assert np.abs(c[:, None] - np.array([850.0 - half, 850.0, 850.0 + half])).min(
                axis=0).max() < 1e-6, (half, ds)
        else:               # a narrow bottom: one cell holds both corners, well inside it
            assert sb > 2 * half and gap >= 0.2 * ds - 1e-6, (half, ds, sb, gap)
        assert w.min() >= min(ds, 0.4 * ds) - 1e-6 or w.min() >= sb - 1e-6, (half, ds, w.min())
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
            if name.startswith("Channel"):                       # v2c: a full-cell strip
                assert far == cell and near == cell, (name, near, far)
            else:
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


@test
def hecras_breaklines_perimeter_precheck_flags_and_fixes_the_v2b_gmsh_failure():
    """v2c: Absaraka package_v2b_gmsh stopped in HEC-RAS with "1 face errors detected on the
    perimeter of the mesh". Its points near the two bad cells (tests/data): the check flags
    cell 54811, which the perimeter cuts in two, and cell 54967, whose perimeter face is 0.6
    ft; fix_faces clears them by nudging or removing a few points nearby."""
    if p0._skip() or not _has_scipy():
        print("  (skipped: no GDAL / scipy)")
        return
    import json
    with open(os.path.join(os.path.dirname(__file__), "data",
                           "abs2801_v2bg_perimeter_cells.json")) as f:
        d = json.load(f)
    for case in d["cases"]:
        per, pts = np.asarray(case["perimeter"]), np.asarray(case["points"])
        clip = hm.perimeter_clip(per)
        a = int(np.argmin(np.hypot(*(pts - case["cell"]).T)))
        chk = hbl.voronoi_precheck(pts, per, clip=clip)
        if case["kind"] == "split":
            assert a in chk["split"], chk["split"]
        else:
            assert a in chk["short"], chk["short"]
            assert chk["min_perimeter_face"] < 1.0, chk["min_perimeter_face"]
        s = hbl.precheck_summary(chk)
        assert s["split_cells"] + s["short_perimeter_faces"] >= 1
        assert "split" not in s and "near_vertex" not in s
        out, _, info = hbl.fix_faces(pts, per, None, clip=clip)
        assert info["perimeter_left"] == 0 and info["perimeter_corners_left"] == 0, info
        assert info["perimeter_removed"] + info["perimeter_nudged"] <= 6, info
        c2 = info["check"]
        assert c2["split_cells"] == 0 and c2["short_perimeter_faces"] == 0
        assert c2["min_perimeter_face"] >= hbl.PERIM_SHORT_MIN, c2
    # a sound perimeter: nothing to do
    sq = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)]
    g = _grid(0, 1000, 0, 1000, 100.0)
    out2, _, i2 = hbl.fix_faces(g, sq, None, clip=hm.perimeter_clip(sq))
    assert len(out2) == len(g) and i2["perimeter_found"] == {
        "split_cells": 0, "short_perimeter_faces": 0, "perimeter_corners_near_edge": 0}, i2


@test
def hecras_breaklines_perimeter_corner_on_a_cell_edge_is_moved_off():
    """v2c: a perimeter corner 0.1 ft from the face between two points (half a spacing from
    each) is flagged and the nearer point nudged so the corner is clear of the edge."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    g = _grid(0, 1000, 0, 1000, 100.0)
    # the face between (450, 450) and (550, 450) is x = 500; a corner at x = 500.1
    per = [(0.0, 0.0), (500.1, 400.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)]
    keep = hbl.inside(g[:, 0], g[:, 1], per) & (hbl.ring_dist(g[:, 0], g[:, 1], per) > 30.0)
    g = g[keep]
    pc = hbl.perimeter_check(g, per)
    assert pc["perimeter_corners_near_edge"] >= 1 and 1 in pc["near_vertex"][:, 0], pc
    out, _, info = hbl.fix_faces(g, per, None)
    assert info["perimeter_nudged"] >= 1 and info["perimeter_corners_left"] == 0, info
    assert hbl.perimeter_check(out, per)["min_corner_to_edge"] >= hbl.PERIM_VERTEX_MIN


def _abs_case(name):
    import json
    with open(os.path.join(os.path.dirname(__file__), "data", "abs2801_breach_cells.json")) as f:
        c = json.load(f)["cases"][name]
    r = c["raster"]
    ras = (np.asarray(r["z"], float), r["x0"], r["y0"], r["pix"])
    return c, ras


def _abs_check(c, ras):
    b = c["breach"]
    half = 0.5 * b["bottom_width"]
    return hbl.breach_cell_check(np.asarray(c["points"]), np.asarray(c["perimeter"]),
                                 np.asarray(c["conn_line"]), b["centre_station"] - half,
                                 b["centre_station"] + half, b["invert"], ras, top=b["top"],
                                 side_slope=b["side_slope"])


@test
def hecras_breach_cells_precheck_flags_the_v2c_cells_hec_ras_warned_about():
    """v2d: Absaraka v2c and v2c_gmsh (volume errors 38.7 % and >= 12 %) both had HEC-RAS's
    'SA 2D Connection at Dam has a weir breach lower than the cells they are connected to' at
    weir station 809.6 (invert 1,046.43 ft): cell 62974 at 1,049.26 ft and cell 64879 at
    1,049.78 ft. The pre-check (points near the breach, the package terrain; tests/data) finds
    exactly those cells, 0.3-0.6 ft higher (strict side), plus the cell at the other corner;
    v2b (0.5 % volume error, no warning) passes."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    for name, hec_cell, hec_z in (("v2c", 62974, 1049.256), ("v2c_gmsh", 64879, 1049.783)):
        c, ras = _abs_case(name)
        r = _abs_check(c, ras)
        assert not r["ok"] and r["bad"] == 2, (name, r["bad"], r["cells"])
        hec = [c["hec_index"][x["cell"]] for x in r["bad_cells"]]
        assert hec_cell in hec, (name, hec)
        row = [x for x in r["bad_cells"] if c["hec_index"][x["cell"]] == hec_cell][0]
        assert hec_z - 0.05 <= row["min_elev"] <= hec_z + 0.75, (name, row)
        assert abs(row["to"] - 809.6) < 0.2, row                  # its face ends on the corner
    c, ras = _abs_case("v2b")
    r = _abs_check(c, ras)
    assert r["ok"] and r["bad"] == 0 and r["bottom_cells"] == 2, r["cells"]


@test
def hecras_breach_cells_corner_centred_row_passes_where_a_face_on_the_corner_fails():
    """v2d, pure: a dam on the top edge of a square 2D area, a breach cut 80 ft wide into a
    20 ft high embankment: the v2c row (a face on each bottom corner) leaves the embankment
    cell beside each corner above the invert; the v2d row (cells centred on the corners) with
    the cut a little past the corners passes."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    per = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0), (0.0, 1000.0)]
    A, B = (0.0, 1000.0), (1000.0, 1000.0)
    pix = 10.0
    xs = np.arange(0.0, 1000.0, pix) + 0.5 * pix
    X, Y = np.meshgrid(xs, 1000.0 - (np.arange(0.0, 1000.0, pix) + 0.5 * pix))
    z0 = np.where(Y > 900.0, 1020.0, 1000.0)                 # the embankment's downstream half
    tc, half, inv = 500.0, 40.0, 1000.0

    def check(row_x, margin):
        z = z0.copy()
        z[(np.abs(X - tc) <= half + margin) & (Y > 800.0)] = inv
        row = np.column_stack([row_x, np.full(len(row_x), 980.0)])
        g = _grid(0, 1000, 0, 1000, 100.0)
        pts = np.vstack([row, g[g[:, 1] < 900.0]])
        return hbl.breach_cell_check(pts, per, [A, B], tc - half, tc + half, inv,
                                     (z, 0.0, 1000.0, pix), top=1020.0, side_slope=0.7)
    # v2c: 20 ft cells with faces on the corners (460 and 540), 50 ft cells outside
    v2c = np.r_[np.arange(25.0, 440.0, 50.0), np.arange(450.0, 551.0, 20.0),
                np.arange(575.0, 1000.0, 50.0)]
    bad = check(v2c, 0.0)
    assert not bad["ok"] and bad["bad"] == 2 and bad["worst_above"] > 1.0, bad["cells"]
    assert sorted(round(c["to"] if c["to"] < 500 else c["from"]) for c in bad["bad_cells"]) == \
        [460, 540], bad["bad_cells"]
    # v2d: cells centred on the corners and the centre (dam_stations), the cut a little past
    sb = hbl.breach_row_spacing(half, 50.0, 100.0)
    cen, _ = hbl.dam_stations(1000.0, 50.0, tc, half, sb)
    assert sb == 40.0 and np.isclose(cen, 460.0).any() and np.isclose(cen, 540.0).any()
    good = check(cen, hm.breach_corner_margin(pix, sb))
    assert good["ok"] and good["bottom_cells"] == 3, good["cells"]
    # ... and the same row with the old cut (to the corners only) is caught too
    assert check(cen, -2.0 * pix)["bad"] >= 1
    # the station of each face end, and a corner within station_tol counts both cells
    runs = hbl.connection_cells(np.vstack([[[450.0, 990.0], [550.0, 990.0]]]), per, [A, B])
    assert [r[0] for r in runs] == [0, 1] and abs(runs[0][2] - 500.0) < 0.1, runs


@test
def hecras_breach_cells_precheck_in_the_build_and_run_report():
    """v2d build_model: the breach-cell pre-check runs on the final points and the written
    terrain; its result is in the README, the site checks and run_report.json
    ('hecras_breach_cells'); a bad breach_cell_check option is refused."""
    if p0._skip() or not _has_scipy():
        print("  (skipped: no GDAL / scipy)")
        return
    import json
    d = tempfile.mkdtemp()
    try:
        p0._run(d, dam_json_len_ft=1500.0)
        _with_path(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "m"))
        bc = r["info"]["site"]["breach_cells"]
        assert bc["action"] == "ok" and bc["ok"] and bc["bad"] == 0, bc
        assert bc["bottom_cells"] >= 2, bc
        cut = r["info"]["site"]["breach_cut"]
        assert cut["corner_margin"] > 0 and cut["flat_width"] > cut["width"], cut
        rep = json.load(open(os.path.join(d, hm.hecras.MODEL_FOLDER, hm.hecras.RUN_REPORT_JSON)))
        assert rep["hecras_breach_cells"]["ok"] is True, rep.get("hecras_breach_cells")
        assert rep["hecras_breach_cells"]["cells"], rep["hecras_breach_cells"]
        txt = open(r["paths"]["readme"], encoding="utf-8").read()
        assert "| Breach-cell pre-check | OK:" in txt
        assert any(n.startswith("Breach-cell pre-check: OK") for n in r["notes"])
        try:
            hm.build_model(d, out_folder=os.path.join(d, "m2"),
                           options={"breach_cell_check": "maybe"})
        except hm.HecRasModelError as e:
            assert "breach_cell_check" in str(e)
        else:
            raise AssertionError("a bad breach_cell_check option was accepted")
    finally:
        shutil.rmtree(d, ignore_errors=True)
