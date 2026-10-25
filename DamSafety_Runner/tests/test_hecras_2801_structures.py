"""3.10.75 (DWR-2801 NickReview): HEC-RAS connection names without "/" (and other characters
HEC-RAS refuses), unique within 16 characters, with the original kept; every connection line
on the cell faces HEC-RAS makes (spacing <= half the line, point pairs past the ends, the
strict aligned_share check) and the gmsh seeds graded down to that spacing. Tiny synthetic
areas only (a few hundred points); no HEC-RAS, no Wine, no solver."""
import sys

import numpy as np

from nd_breach_runner.core import gmsh_mesher as gm
from nd_breach_runner.core import hecras_breaklines as hb
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import hecras_seeds as hsd
from nd_breach_runner.core import hecras_structures as hs
from nd_breach_runner.tests.registry import test

H, CELL = 1500.0, 150.0           # a 3,000 ft square, 150 ft cells (the DWR-2801 cell): 400


def _has_scipy():
    try:
        import scipy.spatial                           # noqa: F401
        return True
    except ImportError:
        return False


def _perimeter():
    return np.array([[-H, -H], [H, -H], [H, H], [-H, H]])


def _grid():
    g = np.arange(-H + 0.5 * CELL, H, CELL)
    X, Y = np.meshgrid(g + 13.0, g + 37.0)             # rows off every line below
    return np.column_stack([X.ravel(), Y.ravel()])


def _seg(c, L, ang):
    u = np.array([np.cos(np.radians(ang)), np.sin(np.radians(ang))])
    c = np.asarray(c, float)
    return [tuple(c - u * L / 2.0), tuple(c + u * L / 2.0)]


def _conns():
    """Short lines like DWR-2801's ('NBI 000000009128' 97 ft, 'OSM way/254643 2' 126 ft), a
    584 ft one, a 1,300 ft one and a 60 ft one, at odd angles, apart from each other."""
    return [{"name": "a97", "line": _seg((-700, -600), 97.0, 30)},
            {"name": "b126", "line": _seg((600, -650), 126.1, 73)},
            {"name": "c584", "line": _seg((-500, 500), 584.0, 10)},
            {"name": "d1300", "line": _seg((300, 350), 1300.0, 47)},
            {"name": "e60", "line": _seg((-900, 1000), 60.0, -20)}]


# ------------------------------------------------------------------------------ names ------
@test
def hecras_2801_connection_names_lose_slashes_and_stay_unique():
    used = {hm.CONN_NAME, hm.AREA_NAME, hm.SA_NAME}
    a = hm.connection_name("OSM way/254643123", used)
    b = hm.connection_name("OSM way/254643124", used)      # the same 16 characters
    c = hm.connection_name("OSM node/1185946", used)
    d = hm.connection_name("NBI 0010009014 (A, B=C)", used)
    e = hm.connection_name("St. Mary's #2 / Elm", used)
    f = hm.connection_name("osm WAY/254643123", used)      # HEC-RAS compares case-blind
    names = [a, b, c, d, e, f]
    for nm in names:
        assert 1 <= len(nm) <= hm.CONN_NAME_MAX, nm
        assert not hm.CONN_NAME_BAD.search(nm), nm
        assert "/" not in nm and "," not in nm and "=" not in nm, nm
    assert a == "OSM way_25464312", a
    assert b == "OSM way_254643 2" and f == "osm WAY_254643 3", (b, f)
    assert c == "OSM node_1185946", c
    assert len({n.lower() for n in names}) == len(names), names
    assert hm.connection_name("///", set()) == "Structure"
    # what HEC-RAS took on DWR-2801 stays as it was
    assert hm.connection_name("NDDOT 09-122-22.1", set()) == "NDDOT 09-122-22."


@test
def hecras_2801_connection_problems_reject_a_slash():
    c = {"name": "OSM way/2546", "line": [(0.0, 0.0), (10.0, 0.0)], "culvert": None}
    probs = hs.connection_problems([c], {}, _perimeter())
    assert any("no / , =" in p for p in probs), probs
    c["name"] = "OSM way_2546"
    assert not any("no / , =" in p for p in hs.connection_problems([c], {}, _perimeter()))


@test
def hecras_2801_renamed_connections_keep_the_osm_id_in_the_report():
    m = type("M", (), {})()
    m.units = hm.Units(False)
    sinfo = {"source": "the run", "exported": [], "skipped": [{"name": "x", "kind": "culvert",
                                                                "why": "outside"}],
             "renamed": [{"name": "OSM way_254643 2", "source_name": "OSM way/254643999",
                          "label": "OSM way/254643999"}],
             "aligned": {"a": 1.0, "b": 0.5}}
    txt = "\n".join(hm.structures_readme(m, sinfo))
    assert "`OSM way_254643 2` = `OSM way/254643999`" in txt, txt
    assert "1 of 2 at 90 % or more; under 90 %: b 50%" in txt, txt


# -------------------------------------------------------------------- faces on the lines ---
@test
def hecras_2801_structure_spacing_is_at_most_half_the_line():
    u = hm.Units(False)
    assert hs.structure_spacing(97.0, CELL, u) == 48.5
    assert hs.structure_spacing(2000.0, CELL, u) == CELL
    assert hs.structure_spacing(2000.0, CELL, u, local=10.0) == 0.25 * CELL
    # the floor: max(20 ft, 0.2 cell) = 30 ft
    assert abs(hs.structure_spacing(20.0, CELL, u) - 30.0) < 1e-9
    ext = hs.extended_line([(0.0, 0.0), (100.0, 0.0)], 25.0)
    assert np.allclose(ext, [(-25.0, 0.0), (125.0, 0.0)])


@test
def hecras_2801_aligned_share_is_strict_and_full_with_the_new_pairs():
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    # one point pair across a 126 ft line (the old layout of a line shorter than its spacing),
    # with the face tilted 8 ft off the line by a neighbour (what HEC-RAS made on DWR-2801)
    g = _grid()
    ln = [(0.0, 0.0), (126.0, 0.0)]
    pts = np.vstack([g[hb.poly_dist(g[:, 0], g[:, 1], ln) > 150.0],
                     [[63.0, 75.0], [63.0, -75.0]]])
    on_faces = hb.share_on_faces(pts, ln)
    strict = hs.aligned_share(pts, ln, 150.0, 0.5)
    # pairs on the line, ext and <= L/2: all of it on faces
    s = 0.5 * 126.0
    q = np.vstack([g[hb.poly_dist(g[:, 0], g[:, 1], ln) > 2 * s],
                   hb._pairs(hs.extended_line(ln, s), s)])
    good = hs.aligned_share(q, ln, s, 0.5)
    assert good >= 0.99, good
    assert strict <= on_faces, (strict, on_faces)
    warn, fail = hs.alignment_notes({"x": 0.0, "y": 0.5, "z": 0.95})
    assert warn == ["y 50%"] and len(fail) == 1 and "'x'" in fail[0], (warn, fail)


@test
def hecras_2801_breaklines_put_every_short_line_on_faces():
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    try:
        clip = hm.perimeter_clip(_perimeter())
    except ImportError:
        print("  (skipped: no GDAL)")
        return
    u = hm.Units(False)
    per, pts0 = _perimeter(), _grid()
    # the old rule (spacing between a quarter cell and a cell, pairs only along the line):
    # the strict check catches what the old share_on_faces did not
    sv = (hs.structure_spacing, hs.BREAKLINE_END_EXT)
    hs.structure_spacing = lambda L, cell, units, local=None: float(min(max(local or cell,
                                                                            0.25 * cell), cell))
    hs.BREAKLINE_END_EXT = 0.0
    try:
        _, old = hs.connection_breaklines(pts0, per, _conns(), CELL, u, clip=clip)
    finally:
        hs.structure_spacing, hs.BREAKLINE_END_EXT = sv
    assert min(old["on_faces"].values()) >= 0.95, old["on_faces"]
    assert min(old["aligned"].values()) < hs.ALIGN_WARN, old["aligned"]
    conns = _conns()
    pts, info = hs.connection_breaklines(pts0, per, conns, CELL, u, clip=clip)
    assert info["lines"] == 5, info
    assert min(info["aligned"].values()) >= hs.ALIGN_WARN, info["aligned"]
    assert info["aligned"]["a97"] >= 0.99 and info["aligned"]["e60"] >= 0.99, info["aligned"]
    assert info["check"]["max_sides"] <= hb.MAX_FACES
    sp = {c["name"]: c["breakline_spacing"] for c in conns}
    assert sp["a97"] <= 97.0 / 2 + 0.01 and sp["e60"] == 30.0 and sp["d1300"] == CELL, sp
    # the cost: a few points per line (400 grid points here)
    assert len(pts) - len(pts0) < 60, (len(pts0), len(pts))
    assert hs.alignment(pts, conns, u) == info["aligned"]


# ---------------------------------------------------------------------------- gmsh seeds ---
@test
def hecras_2801_gmsh_size_lines_are_checked_and_optional():
    for bad in ({"line": [(0, 0)], "size": 1.0}, {"line": [(0, 0), (1, 0)], "size": 0},
                {"line": [(0, 0), (1, 0)], "size": 1.0, "dist_min": 5.0, "dist_max": 2.0}):
        try:
            gm.size_lines_of({"size_lines": [bad]})
            raise AssertionError("accepted {}".format(bad))
        except gm.MeshError:
            pass
    assert gm.size_lines_of({}) == []
    sl = gm.size_lines_of({"size_lines": [{"line": [(0, 0), (10, 0)], "size": 2.0}]})
    assert sl[0]["dist_min"] == 4.0 and sl[0]["dist_max"] > 4.0
    spec, sizes, _ = hsd.spec_for(_perimeter(), CELL, structures=[
        {"line": _seg((0, 0), 97.0, 30), "size": 48.5}, {"line": _seg((0, 500), 2000, 0),
                                                         "size": CELL}])
    assert len(spec["size_lines"]) == 1 and sizes["structure_lines"] == 1, spec["size_lines"]
    assert sizes["structure_spacing"] == 48.5
    spec0, sizes0, _ = hsd.spec_for(_perimeter(), CELL)
    assert spec0["size_lines"] == [] and "structure_lines" not in sizes0


@test
def hecras_2801_gmsh_seeds_grade_down_at_short_structures():
    if hsd.gmsh_version_inprocess() is None or not _has_scipy():
        print("  (skipped: no gmsh / scipy in {})".format(sys.executable))
        return
    u = hm.Units(False)
    st = [{"line": c["line"], "size": hs.structure_spacing(hb.length(np.asarray(c["line"])),
                                                           CELL, u)} for c in _conns()]
    opts = {"gmsh_refine": {"random_seed": 1}}
    p0, i0 = hsd.make_seeds(_perimeter(), CELL, options=opts)
    p1, i1 = hsd.make_seeds(_perimeter(), CELL, options=opts, structures=st)
    assert i1["sizes"]["structure_lines"] == 3, i1["sizes"]     # the 3 short ones
    assert i1["gmsh_stats"].get("size_lines") == 3, i1["gmsh_stats"]
    from scipy.spatial import cKDTree
    for c in _conns()[:2]:
        mid = np.mean(np.asarray(c["line"]), axis=0)
        d0 = cKDTree(p0).query(mid, k=4)[0].mean()
        d1 = cKDTree(p1).query(mid, k=4)[0].mean()
        assert d1 < 0.75 * d0, (c["name"], d0, d1)
    print("  gmsh seeds {} -> {} with 3 short structure lines (400-cell area)".format(
        len(p0), len(p1)))
