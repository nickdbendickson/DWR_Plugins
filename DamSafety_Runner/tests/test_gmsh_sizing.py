"""3.10.69 gmsh far-field sizing (core/gmsh_mesher.py resolve_sizing / clip_to_rings,
anuga_model gmsh_env_options / check_gmsh_options / wet_footprint).

With nothing set, gmsh_mesh_in.json and every gmsh call build_mesh makes (options, points,
lines, embeds and the size fields) must be the same as before: pinned below against
origin/main ac697d7 with a stand-in gmsh module that records the calls and stops at
mesh.generate (so this part runs without gmsh). Then one test per option, and a tiny real
gmsh mesh (skipped without gmsh) with fewer triangles when the options are on.
Nothing here runs ANUGA."""
import hashlib
import json
import math
import os
import shutil
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import gmsh_mesher as GM  # noqa: E402
from nd_breach_runner.core import terrain  # noqa: E402
from nd_breach_runner.core import workflow as W  # noqa: E402
from nd_breach_runner.core.lisflood import ModelError  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402
from nd_breach_runner.tests.test_gmsh_mesher import HAVE_GMSH, _case, _spec  # noqa: E402
from nd_breach_runner.tests.test_gmsh_gate import REJECT, _run  # noqa: E402

# Pinned on origin/main ac697d7 (before 3.10.69), numbers rounded to 9 significant digits
# (the same in numpy 2.5.3 and 2.5.4):
# gmsh_mesh_in.json of _case() with ANUGA_MESHER=gmsh (without 'threads', which is the
# machine's resolved count; built-in outline tracer as in test_gmsh_mesher's job pin)
MAIN_SPEC = "79d75142bd6d86db5863d5523a2198e6e3801ee6623f6a57d77e1727c470ebc0"
# the gmsh calls build_mesh makes for that spec, for _spec() and for _spec() with culverts
MAIN_CALLS_SPEC = "db8398f5b87447aa124557e07cad73ff55ce95a6b7c5440bb584910dd0be4757"
MAIN_CALLS_FIXTURE = "b12c0ab8b0dd1e25a64d50f8527c37673699fdb77d3f87663a6ba2f2881ecc61"
MAIN_CALLS_FIXTURE_CULVERT = "6bf87f03efc96e8e0d61d33fe760783ad08a63309dea6291154262f8f74aa928"

ENV = [v for v, _ in AM.GMSH_SIZING_ENV] + ["NDBREACH_ANUGA_GMSH_MIN_AREA_TOL"]


# ------------------------------------------------------------------ helpers
class _Stop(Exception):
    pass


def _norm(v):
    if isinstance(v, dict):
        return {str(k): _norm(x) for k, x in sorted(v.items())}
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_norm(x) for x in list(v)]
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float("%.9g" % float(v))
    return v


class _Rec(object):
    """Stand-in gmsh: every call is logged as [name, args, kwargs]; mesh.generate stops."""
    def __init__(self, log, path):
        self._log, self._path = log, path

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Rec(self._log, self._path + "." + name)

    def __call__(self, *a, **k):
        self._log.append([self._path, _norm(a), _norm(k)])
        if self._path.endswith("mesh.generate"):
            raise _Stop()
        return len(self._log)


def calls(spec):
    log = []
    old = sys.modules.get("gmsh")
    sys.modules["gmsh"] = _Rec(log, "gmsh")
    try:
        GM.build_mesh(spec)
    except _Stop:
        pass
    finally:
        if old is None:
            sys.modules.pop("gmsh", None)
        else:
            sys.modules["gmsh"] = old
    return log


def digest(obj):
    return hashlib.sha256(json.dumps(_norm(obj), sort_keys=True).encode()).hexdigest()


def thresholds(log):
    """{field tag: {SizeMin, SizeMax, DistMin, DistMax}} of the Threshold fields."""
    out = {}
    for name, a, _ in log:
        if name.endswith("field.setNumber") and a[1] in ("SizeMin", "SizeMax", "DistMin",
                                                          "DistMax"):
            out.setdefault(a[0], {})[a[1]] = a[2]
    return [out[k] for k in sorted(out)]


def option(log, key):
    return [a[1] for name, a, _ in log if name == "gmsh.option.setNumber" and a[0] == key][-1]


class _env(object):
    """Only the given NDBREACH_ANUGA_GMSH_* variables set (the others removed)."""
    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.keep = {k: os.environ.pop(k, None) for k in ENV}
        os.environ.update({k: str(v) for k, v in self.kv.items()})

    def __exit__(self, *a):
        for k in ENV:
            os.environ.pop(k, None)
            if self.keep[k] is not None:
                os.environ[k] = self.keep[k]


def _run_spec(gmsh=None, contourpy=True):
    """gmsh_mesh_in.json written by anuga_model.run for _case() (stand-in mesher / worker)."""
    real = terrain._have_contourpy
    if not contourpy:
        terrain._have_contourpy = lambda: False
    try:
        e, logs, d = _run("gmsh", REJECT) if gmsh is None else _run_with(gmsh)
    finally:
        terrain._have_contourpy = real
    p = os.path.join(d, "a", "gmsh_mesh_in.json")
    spec = json.load(open(p)) if os.path.exists(p) else None
    shutil.rmtree(d, ignore_errors=True)
    return spec, e, logs


def _run_with(gmsh):
    keep = AM.AnugaSettings.__init__

    def init(self, *a, **k):
        keep(self, *a, **k)
        self.gmsh = dict(gmsh)
    AM.AnugaSettings.__init__ = init
    try:
        return _run("gmsh", REJECT)
    finally:
        AM.AnugaSettings.__init__ = keep


def _raises(fn, *words):
    try:
        fn()
    except (GM.MeshError, ModelError, ValueError) as e:
        for w in words:
            assert w in str(e), (w, str(e))
        return str(e)
    raise AssertionError("no error for " + " ".join(words))


# ------------------------------------------------------------------ defaults = main
@test
def gmsh_sizing_defaults_leave_the_spec_and_the_size_fields_as_on_main():
    with _env():
        assert W.RunSettings().anuga_gmsh is None
        assert W.RunSettings(anuga_gmsh={"min_area_tol": 0.4}).anuga_gmsh == {"min_area_tol": 0.4}
        assert AM.gmsh_env_options(None, {}) is None
        spec, _, _ = _run_spec(contourpy=False)
    assert "threads" in spec
    spec.pop("threads")
    assert sorted(spec) == ["channel", "lines", "outer", "points"], sorted(spec)
    assert digest(spec) == MAIN_SPEC, digest(spec)
    assert digest(calls(spec)) == MAIN_CALLS_SPEC
    assert digest(calls(_spec())) == MAIN_CALLS_FIXTURE
    assert digest(calls(dict(_spec(), culvert_area_m2=25.0))) == MAIN_CALLS_FIXTURE_CULVERT
    # the new keys given as None (unset) change nothing either
    none = dict(_spec(), grade_dist_m=None, line_min_area_m2=None, refine_footprint=None)
    assert digest(calls(none)) == MAIN_CALLS_FIXTURE
    assert GM.resolve_sizing(_spec(), GM.default_sizes()) == {}
    # the field setup itself, spelled out: lines, channel, points
    f = thresholds(calls(_spec()))
    assert [(t["DistMin"], t["DistMax"]) for t in f] == [(60, 1500), (250, 2500), (40, 600)], f
    assert abs(f[0]["SizeMin"] - 1.3 * GM.h_of_area(100.0)) < 1e-6
    assert abs(f[0]["SizeMax"] - GM.h_of_area(20000.0)) < 1e-6


# ------------------------------------------------------------------ the options
@test
def gmsh_grade_dist_m_sets_the_threshold_distances():
    f = thresholds(calls(dict(_spec(), grade_dist_m=500.0)))
    assert [(t["DistMin"], t["DistMax"]) for t in f] == [(60, 500), (250, 1000), (40, 300)], f
    assert abs(f[0]["SizeMax"] - GM.h_of_area(20000.0)) < 1e-6     # grades to the far field
    # an explicit DistMax still wins
    f = thresholds(calls(dict(_spec(), grade_dist_m=500.0, channel_dist_max_m=2500.0)))
    assert [t["DistMax"] for t in f] == [500, 2500, 300], f
    rec = GM.resolve_sizing({"grade_dist_m": 800}, GM.default_sizes())
    assert rec["grade_dist_m"] == 800 and rec["line_dist_max_m"] == 800
    assert rec["channel_dist_max_m"] == 1600 and abs(rec["point_dist_max_m"] - 480) < 1e-9
    # 100 m: the channel's 200 m is not past its DistMin (250 m)
    _raises(lambda: GM.resolve_sizing({"grade_dist_m": 100}, GM.default_sizes()),
            "channel_dist_max_m = 200 m", "channel_dist_min_m = 250 m", "grade_dist_m = 100 m")


@test
def gmsh_line_min_area_m2_sets_the_line_size_and_the_gate():
    log = calls(dict(_spec(), line_min_area_m2=200.0))
    f = thresholds(log)
    h200 = 1.3 * GM.h_of_area(200.0)
    assert abs(f[0]["SizeMin"] - h200) < 1e-6, f
    assert abs(f[1]["SizeMin"] - max(h200, GM.h_of_area(250.0))) < 1e-6     # channel floor
    assert abs(option(log, "Mesh.MeshSizeMin") - h200) < 1e-6
    g = GM.default_sizes()
    rec = GM.resolve_sizing({"line_min_area_m2": 200}, g)
    assert g["min_area_m2"] == 200 and rec["line_min_area_m2"] == 200
    assert GM.gate_area(dict(g, culvert_area_m2=200.0))[2] == 90.0          # 0.45 x 200
    assert GM.gate_area(dict(g, culvert_area_m2=25.0))[2] == 0.45 * 25.0   # culverts keep 25
    # wins over min_area_m2 given in the same spec
    g = dict(GM.default_sizes(), min_area_m2=100.0)
    GM.resolve_sizing({"min_area_m2": 100.0, "line_min_area_m2": 150.0}, g)
    assert g["min_area_m2"] == 150.0


@test
def gmsh_sizing_rejects_bad_values_with_the_key_name():
    for bad in ("abc", 0, -5, float("nan"), float("inf"), True, None if False else [1]):
        for key in ("grade_dist_m", "line_min_area_m2"):
            _raises(lambda: GM.resolve_sizing({key: bad}, GM.default_sizes()), key,
                    "positive number")
    _raises(lambda: GM.resolve_sizing({"grade_dist_m": 500, "max_area_m2": -1},
                                      dict(GM.default_sizes(), max_area_m2=-1)),
            "max_area_m2", "positive number")
    for fp in ([[0, 0], [1, 1]], "wet", [[0, 0], [1, float("nan")], [2, 0]], [[1, 2, 3]], []):
        _raises(lambda: GM.resolve_sizing({"refine_footprint": fp}, GM.default_sizes()),
                "refine_footprint")
    # a bad spec is reported before gmsh is imported (also where gmsh is missing)
    _raises(lambda: GM.build_mesh(dict(_spec(), grade_dist_m="x")), "grade_dist_m")
    # the Runner side names the setting and the variable
    st = AM.AnugaSettings(sys.executable)
    st.gmsh = {"grade_dist_m": "500m"}
    _raises(lambda: AM.check_gmsh_options(st), "grade_dist_m", "NDBREACH_ANUGA_GMSH_GRADE_DIST_M")
    st.gmsh = {"refine_footprint_pad_m": -10.0}
    _raises(lambda: AM.check_gmsh_options(st), "NDBREACH_ANUGA_GMSH_REFINE_PAD_M")
    st.gmsh = {"refine_footprint": "dry"}
    _raises(lambda: AM.check_gmsh_options(st), "'wet', 'off'", "NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT")
    st.gmsh = {"refine_footprint": "OFF"}
    assert AM.check_gmsh_options(st) is None
    st.gmsh = {"refine_footprint": "wet", "grade_dist_m": 500.0, "line_min_area_m2": 200}
    assert AM.check_gmsh_options(st) == "wet"
    st.gmsh = None
    assert AM.check_gmsh_options(st) is None


@test
def gmsh_sizing_environment_variables_override_the_setting():
    env = {"NDBREACH_ANUGA_GMSH_GRADE_DIST_M": "500", "NDBREACH_ANUGA_GMSH_LINE_MIN_AREA_M2":
           " 200 ", "NDBREACH_ANUGA_GMSH_MAX_AREA_M2": "", "NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT":
           "Wet", "NDBREACH_ANUGA_GMSH_REFINE_PAD_M": "300"}
    o = AM.gmsh_env_options({"grade_dist_m": 1000.0, "min_area_tol": 0.4}, env)
    assert o == {"grade_dist_m": 500.0, "line_min_area_m2": 200.0, "min_area_tol": 0.4,
                 "refine_footprint": "wet", "refine_footprint_pad_m": 300.0}, o
    assert AM.gmsh_env_options(None, {"NDBREACH_ANUGA_GMSH_GRADE_DIST_M": "abc"}) == \
        {"grade_dist_m": "abc"}                     # stopped later, with the variable's name
    assert AM.gmsh_env_options(None, {"NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT": "off"}) is None
    assert AM.gmsh_env_options({"refine_footprint": "wet"},
                               {"NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT": "off"}) == {}
    with _env(NDBREACH_ANUGA_GMSH_GRADE_DIST_M="500", NDBREACH_ANUGA_GMSH_MIN_AREA_TOL="0.4"):
        assert W.RunSettings().anuga_gmsh == {"min_area_tol": 0.4, "grade_dist_m": 500.0}
        st = W.RunSettings(anuga_gmsh={"grade_dist_m": 2000.0, "line_min_area_m2": 150.0})
        assert st.anuga_gmsh == {"grade_dist_m": 500.0, "line_min_area_m2": 150.0}
        assert st.as_dict()["anuga_gmsh"]["grade_dist_m"] == 500.0     # in the run settings
    with _env(NDBREACH_ANUGA_GMSH_GRADE_DIST_M="abc"):
        spec, e, _ = _run_spec(gmsh=W.RunSettings().anuga_gmsh)
        assert isinstance(e, ModelError) and spec is None, e        # stopped before gmsh ran
        assert "NDBREACH_ANUGA_GMSH_GRADE_DIST_M" in str(e), e
    with _env(NDBREACH_ANUGA_GMSH_GRADE_DIST_M="500", NDBREACH_ANUGA_GMSH_LINE_MIN_AREA_M2="200"):
        spec, e, logs = _run_spec(gmsh=W.RunSettings().anuga_gmsh)
    assert spec["grade_dist_m"] == 500.0 and spec["line_min_area_m2"] == 200.0, spec
    assert any(l.startswith("gmsh sizing: lines 200 m2") for l in logs), logs


@test
def gmsh_refine_footprint_cuts_the_lines_to_the_wet_area():
    sq = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert GM.clip_to_rings([[-50, 50], [150, 50]], GM.footprint_rings(sq)) == \
        [[[0.0, 50.0], [100.0, 50.0]]]
    assert GM.clip_to_rings([[-50, 50], [50, 50], [50, 150]], GM.footprint_rings(sq)) == \
        [[[0.0, 50.0], [50.0, 50.0], [50.0, 100.0]]]
    two = GM.footprint_rings([sq, [[200, 0], [300, 0], [300, 100], [200, 100], [200, 0]]])
    assert len(GM.clip_to_rings([[-50, 50], [350, 50]], two)) == 2
    assert GM.clip_to_rings([[500, 500], [600, 600]], GM.footprint_rings(sq)) == []
    # build_mesh: the 3000 m road (y 300..5700) is cut to the 6 km x 2 km band (y 1500..3500)
    # before it goes into the line field; the culvert points stay (3.4 km outside: the gmsh
    # test below)
    band = [[0, 1500], [6000, 1500], [6000, 3500], [0, 3500]]
    full, cut = calls(_spec()), calls(dict(_spec(), refine_footprint=band))
    xy = lambda log: {tuple(a[:2]) for n, a, _ in log if n == "gmsh.model.geo.addPoint"}
    assert digest(cut) != digest(full) and xy(full) - xy(cut) and xy(cut) - xy(full)
    pts = lambda log: [a for n, a, _ in log if n.endswith("setNumbers") and a[1] == "PointsList"]
    assert pts(cut) == pts(full)
    rec = GM.resolve_sizing({"refine_footprint": band}, GM.default_sizes())
    assert rec["refine_footprint_km2"] == 12.0 and rec["refine_footprint_rings"] == 1
    # the Runner's "wet": LISFLOOD-FP's wet cells + the pad, around the inflow
    dem, flooded, inflow, _, _, _ = _case()
    ring = AM.wet_footprint(dem, flooded, inflow, 200.0)
    assert GM._inside(np.asarray(inflow), ring)
    x0, y0, x1, y1 = ring[:, 0].min(), ring[:, 1].min(), ring[:, 0].max(), ring[:, 1].max()
    # wet rows 40..79 (800 m) + 200 m each side, of the DEM's 2400 m
    assert abs((y1 - y0) - 1200.0) <= 3 * dem.dx, (y0, y1)
    with _env():
        spec, _, _ = _run_spec(gmsh={"refine_footprint": "wet", "refine_footprint_pad_m": 150})
    fp = np.asarray(spec["refine_footprint"])
    assert fp.ndim == 2 and fp.shape[1] == 2 and GM._inside(np.asarray(inflow), fp)
    # 'off' leaves the spec as before
    with _env():
        spec, _, _ = _run_spec(gmsh={"refine_footprint": "off"})
    assert "refine_footprint" not in spec


@test
def gmsh_sizing_record_and_note():
    z = GM.resolve_sizing({"grade_dist_m": 500, "line_min_area_m2": 200}, GM.default_sizes())
    line = AM.sizing_line(z)
    assert line == ("lines 200 m2, far field 20000 m2, grading 500 / 1000 / 300 m "
                    "(lines / channel / points)"), line
    rep = W._anuga_mesh_report([{"stats": {"mesh": {"requested": "gmsh", "used": "gmsh",
                                                     "gmsh": {"sizing": z}, "sizing": z}}}])
    assert rep["sizing"] == z and rep["gmsh"]["sizing"] == z
    rep = W._anuga_mesh_report([{"stats": {"mesh": {"requested": "gmsh", "used": "gmsh",
                                                     "gmsh": {"triangles": 5}}}}])
    assert "sizing" not in rep and "refine_footprint" not in rep


@test
def gmsh_sizing_options_make_fewer_triangles():
    if not HAVE_GMSH:
        print("skip: gmsh not importable")
        return
    n = {}
    band = [[0, 1500], [6000, 1500], [6000, 3500], [0, 3500]]
    for name, extra in (("default", {}), ("grade", {"grade_dist_m": 500.0}),
                        ("lines", {"line_min_area_m2": 200.0}),
                        ("both", {"grade_dist_m": 500.0, "line_min_area_m2": 200.0}),
                        ("footprint", {"refine_footprint": band})):
        m = GM.build_mesh(dict(_spec(), threads=1, **extra))
        s = m["stats"]
        assert s["min_angle_deg"] >= 20.0 and s["min_area_m2"] >= s["gate_min_area_m2"], s
        assert ("sizing" in s) == bool(extra), s
        if name == "footprint":
            assert s["sizing"]["refine_lines_km_outside"] == 3.4, s["sizing"]
        n[name] = s["triangles"]
    print("gmsh sizing triangles:", n)
    assert n["grade"] < 0.85 * n["default"] and n["lines"] < 0.85 * n["default"], n
    assert n["both"] < min(n["grade"], n["lines"]) and n["footprint"] < n["default"], n
