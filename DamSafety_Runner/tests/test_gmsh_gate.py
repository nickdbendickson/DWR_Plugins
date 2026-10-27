"""gmsh ANUGA mesh: the quality gate, the strict / loud fallback (ANUGA_MESHER), the
mesher recorded in run_report, and road / rail embankments as mesh lines.

From the Absaraka 20 x 20 mi run: gmsh's smallest triangle was 49.93 m2 against a gate
of 0.5 x 100 m2, and the Runner quietly used the default mesh. Nothing here runs ANUGA
(a stand-in worker writes the result file); gmsh tests skip when gmsh does not import."""
import copy
import json
import os
import shutil
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
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

FAKE_WORKER = r'''
import json, sys, numpy as np
job = np.load(sys.argv[1])
z = job["dem"]
d = np.where(np.isfinite(z), 0.5, np.nan).astype("f4")
stats = {"triangles": int(len(job["mesh_triangles"])) if "mesh_triangles" in job.files else 1234,
         "max_edge_depth_m": 0.0, "external_mesh": "mesh_points" in job.files}
np.savez(sys.argv[2], stats=json.dumps(stats), depth=d, velocity=d, arrival_hr=d, wse=d + 300)
'''


def _scripts(d, mesher_body):
    w = os.path.join(d, "fake_worker.py")
    with open(w, "w") as fh:
        fh.write(FAKE_WORKER)
    m = os.path.join(d, "fake_mesher.py")
    with open(m, "w") as fh:
        fh.write(mesher_body)
    return w, m


REJECT = ("print('MESHERROR smallest triangle 44.00 m2 is below the 45.0 m2 gate "
          "(0.45 x the smallest intended 100 m2)'); raise SystemExit(3)\n")


def _run(mode, mesher_body=REJECT, timeout=None, gmsh=None, **kw):
    """anuga_model.run on the synthetic case with a stand-in mesher and worker."""
    d = tempfile.mkdtemp(prefix="gmshgate_")
    w, m = _scripts(d, mesher_body)
    dem, flooded, inflow, hy, st, roads = _case()
    st.mesher = mode
    if gmsh is not None:
        st.gmsh = gmsh
    if timeout:
        st.gmsh_timeout_s = timeout
    logs = []
    old = AM.WORKER, AM.MESHER
    AM.WORKER, AM.MESHER = w, m
    try:
        env, _, _ = AM.run(os.path.join(d, "a"), dem, flooded, inflow, (1.0, 0.0), 60.0, hy, st,
                           log=logs.append, refine_lines=roads, **kw)
        return env, logs, d
    except ModelError as e:
        return e, logs, d
    finally:
        AM.WORKER, AM.MESHER = old


# ------------------------------------------------------------------ 1. the gate
def _tri(area):
    """One right triangle of the given area, plus far points (finish() compacts)."""
    a = np.sqrt(2.0 * area)
    pts = np.array([[0, 0], [a, 0], [0, a]], float)
    return np.arange(1, 4), pts, np.array([[1, 2, 3]])


@test
def gmsh_gate_default_tolerance_is_045_and_passes_absaraka_4993():
    assert GM.MIN_AREA_TOL == 0.45
    g = dict(min_angle_deg=20.0, min_area_m2=100.0, min_area_tol=GM.MIN_AREA_TOL)
    g["min_angle_deg"] = 0.0          # a right triangle has 45 deg; only the area is tested
    m = GM.finish(*_tri(49.93), g)
    assert m["stats"]["gate_min_area_m2"] == 45.0 and m["stats"]["min_area_tol"] == 0.45
    try:
        GM.finish(*_tri(44.0), g)
    except GM.MeshError as e:
        assert "below the 45.0 m2 gate" in str(e) and "0.45" in str(e), e
    else:
        raise AssertionError("44 m2 must fail a 45 m2 gate")
    # the old 0.5 would have failed the same Absaraka triangle; it is still configurable
    try:
        GM.finish(*_tri(49.93), dict(g, min_area_tol=0.5))
    except GM.MeshError:
        pass
    else:
        raise AssertionError("min_area_tol 0.5 must reject 49.93 m2")


@test
def gmsh_gate_is_relative_to_the_smallest_intended_size():
    assert GM.gate_area(dict(min_area_m2=100.0)) == (100.0, 0.45, 45.0)
    # culvert ends asked for 25 m2: 20 m2 triangles there are intended, 10 m2 are not
    g = dict(min_area_m2=100.0, culvert_area_m2=25.0, min_area_tol=0.45)
    i, t, gate = GM.gate_area(g)
    assert (i, t) == (25.0, 0.45) and abs(gate - 11.25) < 1e-9
    GM.finish(*_tri(20.0), dict(g, min_angle_deg=0.0))
    try:
        GM.finish(*_tri(10.0), dict(g, min_angle_deg=0.0))
    except GM.MeshError as e:
        assert "smallest intended 25 m2" in str(e), e
    else:
        raise AssertionError("expected MeshError")
    # a larger culvert size never loosens the base gate
    assert GM.gate_area(dict(min_area_m2=100.0, culvert_area_m2=400.0))[2] == 45.0


@test
def gmsh_culvert_area_option_refines_the_points_and_keeps_the_gate():
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    L = 3000.0
    base = dict(outer=[[0, 0], [L, 0], [L, L], [0, L]], lines=[], channel=[],
                points=[[1500.0, 1500.0]], max_area_m2=5000.0)
    a = GM.build_mesh(base)
    b = GM.build_mesh(dict(base, culvert_area_m2=25.0))

    def near(m):
        p, t = m["points"], m["triangles"]
        c = p[t].mean(axis=1)
        u, v = p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]]
        ar = 0.5 * np.abs(u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0])
        return ar[np.hypot(*(c - [1500.0, 1500.0]).T) < 20.0].mean()
    assert near(b) < 0.5 * near(a), (near(a), near(b))
    assert b["stats"]["gate_intended_area_m2"] == 25.0
    assert b["stats"]["min_area_m2"] >= b["stats"]["gate_min_area_m2"]


# ------------------------------------------------------------------ 2. strict / loud
@test
def gmsh_explicitly_requested_and_rejected_stops_the_run():
    e, logs, d = _run("gmsh")
    assert isinstance(e, ModelError), e
    msg = str(e)
    assert "ANUGA_MESHER=gmsh" in msg and "rejected" in msg and "44.00 m2" in msg, msg
    assert "gmsh_or_default" in msg and "gmsh_mesh_in.json" in msg, msg
    assert not os.path.exists(os.path.join(d, "a", "anuga_job.npz"))   # ANUGA never started
    shutil.rmtree(d, ignore_errors=True)


@test
def gmsh_or_default_falls_back_loudly_and_records_the_mesher():
    env, logs, d = _run("gmsh_or_default")
    assert not isinstance(env, Exception), env
    want = ("gmsh mesh rejected: smallest triangle 44.00 m2 is below the 45.0 m2 gate "
            "(0.45 x the smallest intended 100 m2); used the default mesh")
    assert ("WARNING: " + want) in logs, logs
    assert want in env.notes, env.notes
    m = env.stats["mesh"]
    assert env.stats["mesher"] == "default" and m["used"] == "default"
    assert m["requested"] == "gmsh_or_default" and "44.00 m2" in m["rejected"]
    with np.load(os.path.join(d, "a", "anuga_job.npz")) as r:
        assert not [k for k in r.files if k.startswith("mesh_")]       # the default mesh
    shutil.rmtree(d, ignore_errors=True)


@test
def gmsh_timeout_follows_the_same_strict_and_fallback_rules():
    slow = "import time; time.sleep(30)\n"
    e, _, d = _run("gmsh", slow, timeout=1.0)
    assert isinstance(e, ModelError) and "over 1 s (timeout)" in str(e), e
    shutil.rmtree(d, ignore_errors=True)
    env, logs, d = _run("gmsh_or_default", slow, timeout=1.0)
    assert not isinstance(env, Exception), env
    assert any(l.startswith("WARNING: gmsh mesh rejected: gmsh took over 1 s") for l in logs), logs
    assert env.stats["mesh"]["rejected"].startswith("gmsh took over 1 s")
    shutil.rmtree(d, ignore_errors=True)


@test
def gmsh_mesh_used_is_recorded_and_default_runs_say_default():
    if HAVE_GMSH:
        d0 = tempfile.mkdtemp(prefix="gmshok_")
        w, _ = _scripts(d0, "")
        dem, flooded, inflow, hy, st, roads = _case()
        st.mesher = "gmsh"
        old = AM.WORKER
        AM.WORKER = w
        try:
            env, _, _ = AM.run(os.path.join(d0, "a"), dem, flooded, inflow, (1.0, 0.0), 60.0,
                               hy, st, refine_lines=roads)
        finally:
            AM.WORKER = old
        assert env.stats["mesher"] == "gmsh" and env.stats["external_mesh"], env.stats
        assert env.stats["mesh"]["gmsh"]["triangles"] == env.stats["triangles"]
        assert env.stats["mesh"]["rejected"] is None
        shutil.rmtree(d0, ignore_errors=True)
    env, logs, d = _run("default")
    assert env.stats["mesher"] == "default" and env.stats["mesh"]["requested"] == "default"
    assert not [l for l in logs if "WARNING" in l]
    shutil.rmtree(d, ignore_errors=True)


@test
def gmsh_mesher_setting_reads_both_environment_names():
    keep = {k: os.environ.pop(k, None) for k in
            ("NDBREACH_ANUGA_MESHER", "ANUGA_MESHER", "NDBREACH_ANUGA_GMSH_MIN_AREA_TOL")}
    try:
        assert W.RunSettings().anuga_mesher == "default"
        assert W.RunSettings().anuga_gmsh is None
        os.environ["ANUGA_MESHER"] = "gmsh_or_default"
        assert W.RunSettings().anuga_mesher == "gmsh_or_default"
        os.environ["NDBREACH_ANUGA_MESHER"] = "gmsh"          # the Runner's own name wins
        assert W.RunSettings().anuga_mesher == "gmsh"
        os.environ["NDBREACH_ANUGA_GMSH_MIN_AREA_TOL"] = "0.4"
        assert W.RunSettings().anuga_gmsh == {"min_area_tol": 0.4}
    finally:
        for k, v in keep.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
    assert AM.mesher_mode(type("S", (), {"mesher": " GMSH_or_default "})()) == "gmsh_or_default"


@test
def run_report_shows_the_mesher_and_a_prominent_rejected_line():
    from nd_breach_runner.tests.test_culvert_match import _MIN_REPORT
    rej = {"requested": "gmsh_or_default", "used": "default", "rejected": "smallest triangle 49.93 m2"}
    ok = {"requested": "gmsh", "used": "gmsh", "rejected": None,
          "gmsh": {"triangles": 173805, "min_area_m2": 49.93, "min_angle_deg": 31.29}}
    am = W._anuga_mesh_report([{"stats": {"mesh": rej}}])
    assert am["used"] == "default" and am["requested"] == "gmsh_or_default"
    assert am["rejected_line"] == ("gmsh mesh rejected: smallest triangle 49.93 m2; "
                                   "used the default mesh")
    r = copy.deepcopy(_MIN_REPORT)
    r["anuga_mesh"] = am
    r["summary"].update(anuga_mesher="default", anuga_mesh_rejected=am["rejected_line"])
    md = W.render_report(r)
    assert "| ANUGA mesh | default (asked for gmsh_or_default) |" in md, md
    assert "## ANUGA mesh\n\n**gmsh mesh rejected: smallest triangle 49.93 m2; used the default " \
           "mesh**" in md, md
    assert md.index("## ANUGA mesh") < md.index("## Breach")
    json.dumps(am)
    # gmsh used: one table row with its size, no rejected section
    am = W._anuga_mesh_report([{"stats": {"mesh": rej}}, {"stats": {"mesh": ok}}])
    assert am["used"] == "gmsh" and am["gmsh"]["triangles"] == 173805
    assert "rejected_line" in am                    # pass 1 fell back: still said
    r = copy.deepcopy(_MIN_REPORT)
    am2 = W._anuga_mesh_report([{"stats": {"mesh": ok}}])
    assert "rejected_line" not in am2
    r["anuga_mesh"] = am2
    r["summary"]["anuga_mesher"] = "gmsh"
    md = W.render_report(r)
    assert "| ANUGA mesh | gmsh (173,805 triangles, smallest 49.93 m2, minimum angle 31.29 deg) |" \
        in md, md
    assert "## ANUGA mesh" not in md
    # the default mesher asked for: the report says so and nothing else
    assert "rejected_line" not in W._anuga_mesh_report(
        [{"stats": {"mesh": {"requested": "default", "used": "default", "rejected": None}}}])
    assert W._anuga_mesh_report([{"stats": {}}])["used"] == "default"   # older result files


# ------------------------------------------------------------------ 3. embankments
def _fill_case(dx=2.0):
    """Flat ground at 300 m, 800 x 800 m; a 1.2 m road fill along x=400 m for
    y=100..500 m, a road on a side-hill at x=650 m (ground rises 0.1 m/m east of 600 m)."""
    n = int(800 / dx)
    yy, xx = np.mgrid[0:n, 0:n]
    x = (xx + 0.5) * dx
    y = 800.0 - (yy + 0.5) * dx
    z = np.full((n, n), 300.0)
    z += np.where(x > 600.0, 0.1 * (x - 600.0), 0.0)
    z[(np.abs(x - 400.0) < 6.0) & (y > 100.0) & (y < 500.0)] += 1.2
    X0, Y0 = 500000.0, 5200000.0
    g = Grid(z, X0, Y0 + 800.0, dx)
    roads = [[[X0 + 400.0, Y0 + 30.0], [X0 + 400.0, Y0 + 770.0]],
             [[X0 + 650.0, Y0 + 30.0], [X0 + 650.0, Y0 + 770.0]]]
    outer = [[X0 + 10, Y0 + 10], [X0 + 790, Y0 + 10], [X0 + 790, Y0 + 790], [X0 + 10, Y0 + 790]]
    return g, roads, outer


@test
def embankment_breaklines_keep_only_raised_road_pieces():
    g, roads, outer = _fill_case()
    pieces, info = AM.embankment_breaklines(roads, g, outer)
    assert len(pieces) == 1, info
    p = pieces[0]
    assert np.allclose(p[:, 0], 500400.0)
    y = p[:, 1] - 5200000.0
    assert 95.0 <= y.min() <= 120.0 and 480.0 <= y.max() <= 505.0, (y.min(), y.max())
    assert info["lines_inside"] == 2 and info["embankment_pieces"] == 1
    # a 0.5 m culvert-sized dip is bridged; a fill lower than raise_m is not an embankment
    z = g.data.copy()
    z[(np.abs(np.arange(z.shape[0])[:, None] * 2.0 - 500.0) < 8.0) & (z > 300.5)] = 300.0
    p2, _ = AM.embankment_breaklines(roads, Grid(z, g.x0, g.y0, g.dx), outer)
    assert len(p2) == 1, len(p2)
    assert len(AM.embankment_breaklines(roads, Grid(z, g.x0, g.y0, g.dx), outer,
                                        gap_m=0.0)[0]) == 2          # the dip is really there
    assert not AM.embankment_breaklines(roads, g, outer, raise_m=1.5)[0]


@test
def find_embankments_reads_finer_ground_than_a_coarse_anuga_dem():
    g, roads, outer = _fill_case()
    coarse = Grid(np.full((20, 20), 300.0), g.x0, g.y0, 40.0)
    calls = []

    def read(bounds, cell):
        calls.append(cell)
        return g
    st = AM.AnugaSettings(sys.executable)
    pieces, info = AM.find_embankments(roads, outer, coarse, read, st)
    assert calls and calls[0] <= 5.0 and len(pieces) == 1, (calls, info)
    st.gmsh = {"embankment_lines": False}
    assert AM.find_embankments(roads, outer, coarse, read, st)[0] == []
    st.gmsh = None
    assert AM.find_embankments([], outer, coarse, read, st)[1]["reason"] == "no road or rail lines"


@test
def gmsh_run_passes_road_embankments_into_the_mesh_spec_and_mesh():
    g, roads, outer = _fill_case(dx=4.0)
    # the default mesher never looks at them: the job is the same as without
    dem, flooded, inflow, hy, st, _ = _case()
    e, _, d = _run("gmsh", embankment_lines=[[[dem.x0 + 2000.0, dem.y0 - 900.0],
                                              [dem.x0 + 2000.0, dem.y0 - 1500.0]]],
                   terrain_read=None)
    spec = json.load(open(os.path.join(d, "a", "gmsh_mesh_in.json")))
    shutil.rmtree(d, ignore_errors=True)
    # the synthetic DEM there has no fill: no embankment line added; 3.10.91: the road is
    # in the wet area, so it is a crest line (anuga_crest) unless the crest hold is off
    assert len(spec["lines"]) == 3, len(spec["lines"])       # refine line + inlet + crest
    assert len(spec["crest_lines"]) == 1, spec.get("crest_lines")
    # NDBREACH_ANUGA_CREST=off: the Settings read it into anuga_gmsh (gmsh_env_options)
    off = AM.gmsh_env_options(None, {"NDBREACH_ANUGA_CREST": "OFF"})
    e, _, d = _run("gmsh", gmsh=off, terrain_read=None,
                   embankment_lines=[[[dem.x0 + 2000.0, dem.y0 - 900.0],
                                      [dem.x0 + 2000.0, dem.y0 - 1500.0]]])
    spec = json.load(open(os.path.join(d, "a", "gmsh_mesh_in.json")))
    shutil.rmtree(d, ignore_errors=True)
    assert len(spec["lines"]) == 2 and "crest_lines" not in spec, spec["lines"]
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    pieces, _ = AM.embankment_breaklines(roads, g, outer)
    st = AM.AnugaSettings(sys.executable)
    spec = AM.gmsh_spec(g, np.array(outer), (500200.0, 5200400.0),
                        AM.inlet_line((500200.0, 5200400.0), (1.0, 0.0), 40.0), [], [], st,
                        channel=[], embankments=pieces)
    assert len(spec["lines"]) == 2
    m = GM.build_mesh(dict(spec, max_area_m2=5000.0))
    pts = m["points"]
    for end in (pieces[0][0], pieces[0][-1]):
        assert np.min(np.hypot(*(pts - end).T)) < 1e-6, end    # the fill is a mesh line
    assert m["stats"]["n_lines"] >= 2


@test
def gmsh_declutter_drops_parallel_twins_and_joins_short_ends():
    gap = 20.0
    main = [[0.0, 0.0], [1000.0, 0.0]]
    twin = [[100.0, 4.0], [900.0, 6.0]]               # a siding 4-6 m beside the main track
    tee = [[500.0, 300.0], [500.0, 8.0]]              # a road stopping 8 m short of it
    far = [[0.0, 200.0], [1000.0, 200.0]]             # a parallel road 200 m away
    out = GM.declutter([main, twin, tee, far], gap)
    src = [i for _r, i in out]
    assert 1 not in src, src                            # the twin is gone
    assert src.count(0) == 1 and src.count(3) == 1 and src.count(2) == 1
    t = [r for r, i in out if i == 2][0]
    assert t[-1][1] < 0.0 < t[0][1]                     # the short end now crosses the track
    # and node_lines then makes the T a shared node
    nodes, edges, _ = GM.node_lines([r for r, _ in out], ["line"] * len(out), snap_m=15.0)
    assert np.min(np.hypot(*(nodes - [500.0, 0.0]).T)) < 1.0


@test
def gmsh_node_lines_splits_an_edge_passing_just_by_a_node():
    # line B's vertex sits 1.2 m from line A's edge without crossing it
    nodes, edges, _ = GM.node_lines([[[0.0, 0.0], [100.0, 0.0]],
                                     [[50.0, 1.2], [60.0, 80.0]]], ["line", "line"], snap_m=5.0)
    k = [i for i, n in enumerate(nodes) if np.allclose(n, [50.0, 1.2])][0]
    deg = np.bincount(edges.ravel(), minlength=len(nodes))
    assert deg[k] == 3, (nodes, edges)                 # A was split at B's vertex


@test
def embankments_only_where_the_first_run_flooded():
    g, roads, outer = _fill_case()
    wm = np.zeros(g.data.shape, bool)                  # wet only in the south half
    wm[g.data.shape[0] // 2:, :] = True
    pieces, info = AM.embankment_breaklines(roads, g, outer, wet=(g, wm))
    assert len(pieces) == 1
    assert (pieces[0][:, 1] - 5200000.0).max() <= 402.0, pieces[0][:, 1].max() - 5200000.0
    coarse = Grid(np.full((20, 20), 300.0), g.x0, g.y0, 40.0)
    st = AM.AnugaSettings(sys.executable)
    dry = np.zeros((20, 20), bool)
    assert AM.find_embankments(roads, outer, coarse, lambda b, c: g, st, flooded=dry)[0] == []
