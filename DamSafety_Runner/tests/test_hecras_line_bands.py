"""3.10.76: bands of 20 ft cells (3 rows each side, faces on the line, graded out at 1.5:1)
along roads, rail, structure connections and breaklines in the HEC-RAS computation points
(core/hecras_bands.py), the 2 x 50 ft option, and the optional (off by default) ANUGA gmsh
band."""
import os
import re
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_bands as hband
from nd_breach_runner.core import hecras_breaklines as hbl
from nd_breach_runner.core import hecras_structures as hstr
from nd_breach_runner.tests.registry import test

CELL, S = 150.0, 20.0
PERIM = np.array([(0, 0), (4000, 0), (4000, 3000), (0, 3000), (0, 0)], float)


def _has_scipy():
    try:
        import scipy.spatial  # noqa: F401
        return True
    except ImportError:
        return False


def _domain(s=S):
    """A 150 ft grid; a road (y = 1500), a winding stream crossing it with a culvert at the
    crossing, a short bridge on the road and two parallel lines 80 ft apart."""
    X, Y = np.meshgrid(np.arange(75, 4000, CELL), np.arange(75, 3000, CELL))
    pts = np.column_stack([X.ravel(), Y.ravel()])
    road = np.array([(300, 1500), (3700, 1500)], float)
    yy = np.linspace(300, 2700, 200)
    stream = np.column_stack([2000 + 200 * np.sin(yy / 500.0), yy])
    xc = float(stream[np.argmin(np.abs(stream[:, 1] - 1500)), 0])
    culv = np.array([(xc - 60, 1500), (xc + 60, 1500)])
    bridge = np.array([(3000, 1500), (3150, 1500)])
    par = [np.array([(500, 2500), (1800, 2500)], float),
           np.array([(500, 2580), (1800, 2580)], float)]
    lines = [hstr.extended_line(culv, s), hstr.extended_line(bridge, s), road] + par + [stream]
    ranks = [0, 0, 1, 1, 1, 3]
    return pts, lines, ranks, dict(culvert=culv, bridge=bridge, road=road, stream=stream,
                                   par=par)


def _apply(s=S, rows=3, hard=None):
    pts, lines, ranks, geo = _domain(s)
    lines = hband.clip(lines, PERIM, s)
    out, prot, st = hband.apply(pts, PERIM, lines, s, rows, 1.5, CELL,
                                hard=hard, ranks=ranks)
    return pts, out, prot, st, geo


@test
def hecras_bands_row_offsets_and_settings():
    offs, edge = hband.row_offsets(20.0, 3, 1.5, 150.0)
    assert np.allclose([o[0] for o in offs], [10, 30, 50, 75, 112.5, 168.75, 253.125])
    assert abs(hband.band_edge(20.0, 3, 1.5, 150.0) - 303.75) < 1e-9
    o2, e2 = hband.row_offsets(50.0, 2, 1.5, 150.0)
    assert e2 == 287.5 and edge == 303.75
    assert np.allclose([o[0] for o in o2], [25, 75, 137.5, 231.25])
    c = hband.settings({}, environ={})
    assert c["on"] and c["cell_ft"] == 20.0 and c["rows"] == 3 and c["grading"] == 1.5
    assert c["classes"] == ("structure", "road", "rail", "breakline") and c["extent"] == "wet"
    assert c["wet_pad_ft"] == 500.0
    e = hband.settings({}, environ={"NDBREACH_BAND_CELL_FT": "50", "NDBREACH_BAND_ROWS": "2",
                                    "NDBREACH_BAND_CLASSES": "road,channel",
                                    "NDBREACH_BAND_LINES": "a.shp;;b.gpkg"})
    assert e["cell_ft"] == 50.0 and e["rows"] == 2 and e["classes"] == ("road", "channel")
    assert e["lines"] == ["a.shp", "b.gpkg"]
    # options win over the environment; off
    assert not hband.settings({"line_bands": "off"}, environ={"NDBREACH_BAND": "on"})["on"]
    assert not hband.settings({}, environ={"NDBREACH_BAND": "off"})["on"]
    for bad in ({"band_grading": 2.5}, {"band_grading": 1.0}, {"band_classes": "roads"},
                {"band_extent": "dry"}, {"band_cell_ft": 0}, {"band_rows": "x"}):
        try:
            hband.settings(bad, environ={})
        except hband.BandError:
            pass
        else:
            raise AssertionError("accepted {}".format(bad))


@test
def hecras_bands_synthetic_road_stream_culvert_bridge_and_parallel_lines():
    """Faces on every line (structures, road, parallel lines, stream), no point closer than
    half a band cell, neighbour spacing growing no faster than 2:1, and between two lines
    80 ft apart a single set of 20 ft rows."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    from scipy.spatial import cKDTree, Delaunay
    pts, out, prot, st, geo = _apply()
    assert st["points_added"] > 0 and st["points"] == len(out)
    road_part = np.array([(400, 1500), (1500, 1500)], float)
    for nm, ln in [("culvert", geo["culvert"]), ("bridge", geo["bridge"]), ("road", road_part),
                   ("par1", geo["par"][0]), ("par2", geo["par"][1]),
                   ("stream", geo["stream"][10:60])]:
        f = hbl.share_on_faces(out, ln)
        assert f > 0.95, (nm, f)
    d = cKDTree(out).query(out, k=2)[0][:, 1]
    assert d.min() > 0.45 * S, d.min()
    tri = Delaunay(out)
    e = np.vstack([tri.simplices[:, [0, 1]], tri.simplices[:, [1, 2]], tri.simplices[:, [0, 2]]])
    inner = hbl.ring_dist(out[:, 0], out[:, 1], PERIM) > CELL
    e = e[inner[e[:, 0]] & inner[e[:, 1]]]
    r = np.maximum(d[e[:, 0]] / d[e[:, 1]], d[e[:, 1]] / d[e[:, 0]])
    assert np.percentile(r, 99.5) <= 2.0 + 1e-6, np.percentile(r, 99.5)
    q = out[(np.abs(out[:, 0] - 1110) < 0.5) & (out[:, 1] > 2500) & (out[:, 1] < 2580)]
    assert np.allclose(sorted(q[:, 1] - 2500), [10, 30, 50, 70], atol=0.6), q
    assert len(prot) == len(out) and prot.sum() > 0


@test
def hecras_bands_two_rows_of_50_ft():
    """The 2 x 50 ft option (NDBREACH_BAND_CELL_FT=50, NDBREACH_BAND_ROWS=2): 25 and 75 ft
    rows off the road, faces on it, and far fewer points than 3 x 20 ft."""
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    _, o20, _, s20, _ = _apply()
    _, o50, _, s50, geo = _apply(s=50.0, rows=2)
    assert s50["points_added"] < 0.5 * s20["points_added"], (s50, s20)
    assert hbl.share_on_faces(o50, np.array([(400, 1500), (1500, 1500)], float)) > 0.95
    q = o50[(np.abs(o50[:, 0] - 1025) < 0.5) & (o50[:, 1] > 1500) & (o50[:, 1] < 1600)]
    assert np.allclose(sorted(q[:, 1] - 1500)[:2], [25, 75], atol=0.6), q
    assert s50["width_each_side"] == 287.5


@test
def hecras_bands_keep_the_hard_points_and_g_lines():
    if not _has_scipy():
        print("  (skipped: no scipy)")
        return
    pts, lines, ranks, geo = _domain()
    hard = np.zeros(len(pts), bool)
    near = np.abs(pts[:, 1] - 1575) < 1
    hard[near] = True
    _, out, _, st, _ = _apply(hard=hard)
    for p in pts[hard]:
        assert (np.hypot(*(out - p).T) < 1e-9).any(), p
    g = hband.g_lines([geo["road"]], "road", S, CELL, 3)
    assert len(g) == 1 and g[0]["name"] == "Band road 1"
    assert g[0]["near"] == S and g[0]["far"] == CELL and g[0]["repeats"] == 3
    assert len(g[0]["line"]) >= 2


def _road_case(d):
    from nd_breach_runner.tests import test_hecras_p0 as p0
    import json
    from nd_breach_runner.core import hecras
    p0._run(d, dam_json_len_ft=1500.0)
    rp = os.path.join(d, hecras.MODEL_FOLDER, hecras.RUN_REPORT_JSON)
    with open(rp, encoding="utf-8") as f:
        rep = json.load(f)
    y = p0.Y0 - 2500.0
    rep["settings"]["road_lines"] = [[(p0.X0 - 2500.0, y), (p0.X0 + 2500.0, y)]]
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(rep, f)
    return p0


@test
def hecras_bands_in_build_model_and_the_off_switch():
    """build_model: a road across the wet valley gets 'Band road' BreakLine blocks (20 ft
    near, Near Repeats 3) with faces on it, only in the wet area + 500 ft by default;
    line_bands='off' gives the grid without bands; a bad grading stops the build."""
    from nd_breach_runner.core import hecras_model as hm
    from nd_breach_runner.tests import test_hecras_seeds as ts
    from nd_breach_runner.tests import test_hecras_p0 as p0
    if p0._skip() or not _has_scipy():
        print("  (skipped: no GDAL / scipy)")
        return
    d = tempfile.mkdtemp()
    try:
        _road_case(d)
        r = hm.build_model(d, out_folder=os.path.join(d, "m"))
        info = r["info"]["line_bands"]
        assert info and info["on"], info
        g = r["paths"]["g01"]
        txt = open(g, encoding="latin-1").read()
        assert p0.unknown_keywords(g) == set(), p0.unknown_keywords(g)
        assert "BreakLine Name=Band road 1" in txt
        assert re.search(r"BreakLine Name=Band road 1\r?\n(.*\r?\n){0,4}BreakLine Near "
                         r"Repeats=3", txt), "Near Repeats"
        pts = ts._g01_points(g)
        assert len(pts) == r["info"]["cells"]
        assert info["points_added"] > 0
        lw = info["classes"]["road"]["length"]
        # wet: 800 m wide + 500 ft each side (~3,625 ft)
        assert 3400 < lw < 4000, lw
        assert any(n.startswith("Line bands: 3 row(s) of 20") for n in r["notes"])
        ra = hm.build_model(d, out_folder=os.path.join(d, "ma"), options={"band_extent": "all"})
        assert ra["info"]["line_bands"]["classes"]["road"]["length"] > lw + 500
        r0 = hm.build_model(d, out_folder=os.path.join(d, "m0"), options={"line_bands": "off"})
        assert r0["info"]["line_bands"] is None
        assert "Band road" not in open(r0["paths"]["g01"], encoding="latin-1").read()
        assert r0["info"]["cells"] == info["points_before"]
        try:
            hm.build_model(d, out_folder=os.path.join(d, "mb"), options={"band_grading": 3})
        except hm.HecRasModelError as e:
            assert "grading" in str(e)
        else:
            raise AssertionError("bad grading accepted")
    finally:
        shutil.rmtree(d)


@test
def anuga_band_is_off_by_default_and_adds_size_lines_when_on():
    from nd_breach_runner.core import anuga_model as am
    from nd_breach_runner.core import gmsh_mesher as gm

    class St:
        threads = 1
        gmsh = None
        fine_area_m2 = 25.0
    st = St()
    outer = [[0, 0], [2000, 0], [2000, 2000], [0, 2000]]
    emb = [np.array([[100, 1000], [1900, 1000]], float)]
    cul = [{"end_points": [[1000, 990], [1000, 1010]]}]
    args = (None, outer, (100.0, 100.0), [[50, 50], [150, 50]], cul, [], st)
    s0 = am.gmsh_spec(*args, channel=[], embankments=emb)
    assert "size_lines" not in s0 and "band" not in s0
    st.gmsh = am.gmsh_env_options(None, environ={"NDBREACH_ANUGA_BAND": "on"})
    assert st.gmsh == {"band": "on"}
    am.check_gmsh_options(st)
    s1 = am.gmsh_spec(*args, channel=[], embankments=emb)
    assert "band" not in s1 and len(s1["size_lines"]) == 2, s1.get("size_lines")
    assert all(sl["size"] == 6.1 and sl["dist_min"] == 18.3 and sl["dist_max"] > 18.3
               for sl in s1["size_lines"])
    assert {k: v for k, v in s1.items() if k != "size_lines"} == s0
    gm.size_lines_of(s1)
    st.gmsh = {"band": "maybe"}
    try:
        am.check_gmsh_options(st)
    except am.ModelError as e:
        assert "band" in str(e)
    else:
        raise AssertionError("bad band accepted")
    st.gmsh = {"band": "on", "band_cell_m": -1}
    try:
        am.check_gmsh_options(st)
    except am.ModelError:
        pass
    else:
        raise AssertionError("bad band_cell_m accepted")
    # the gate follows the size lines: 6.1 m edges ask for (6.1 / 1.3) m equilateral triangles
    g = dict(gm.default_sizes(), min_area_m2=100.0, size_line_min_m=6.1)
    intended, tol, gate = gm.gate_area(g)
    assert abs(intended - np.sqrt(3) / 4 * (6.1 / 1.3) ** 2) < 1e-9 and gate < 7.0
    assert gm.gate_area(dict(gm.default_sizes(), min_area_m2=100.0))[0] == 100.0


@test
def workflow_report_keeps_the_rail_coordinates():
    import inspect
    from nd_breach_runner.core import workflow
    src = inspect.getsource(workflow)
    assert 'report["rail_lines_xy"]' in src
