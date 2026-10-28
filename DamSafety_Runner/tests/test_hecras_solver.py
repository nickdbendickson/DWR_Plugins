"""3.10.85: tighter HEC-RAS solver / plan settings in the generated model (core/hecras_solver):
2D and 1D/2D iterations, the computation step cap, the dam / breach connection rows, the weir
and culvert coefficients, and the outflow (2D area clipped to the ANUGA mesh outline, with
normal-depth outflow lines wherever the run's flood reaches the 2D area's edge). Every setting
can be overridden with NDBREACH_HECRAS_<NAME> or the build option 'solver'.

Synthetic data only (test_hecras_p0's valley); runs in the Runner suite (run_tests.py)."""
import json
import os
import shutil
import tempfile

from nd_breach_runner.core import hecras
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import hecras_solver as hs
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests import test_hecras_p0 as p0


def _raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except hs.SolverSettingError as e:
        return str(e)
    raise AssertionError("no SolverSettingError")


def _lines(path, *prefixes):
    with open(path, encoding="ascii", newline="") as f:
        t = f.read()
    assert "\r\n" in t
    return [ln for ln in t.replace("\r", "").split("\n") if ln.startswith(prefixes)]


def _files(out, r):
    return {e: os.path.join(out, r["model"].name + e) for e in (".g01", ".u01", ".p01")}


class _Env:
    """Set NDBREACH_HECRAS_* variables for one block, then restore them."""

    def __init__(self, **kv):
        self.kv = {hs.ENV[k]: str(v) for k, v in kv.items()}

    def __enter__(self):
        self.old = {k: os.environ.get(k) for k in self.kv}
        os.environ.update(self.kv)

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mesh(folder, half_w=300.0, top=400.0, bottom=-3000.0):
    """An 'ANUGA mesh outline' (work CRS, header x,y) narrower than the run's flood."""
    a = os.path.join(folder, hecras.MODEL_FOLDER, "anuga")
    os.makedirs(a, exist_ok=True)
    p = os.path.join(a, "mesh_boundary.csv")
    with open(p, "w") as f:
        f.write("x,y\n")
        for x, y in ((-half_w, top), (half_w, top), (half_w, bottom), (-half_w, bottom)):
            f.write("{!r},{!r}\n".format(p0.X0 + x, p0.Y0 + y))
    return p


@test
def hecras_solver_settings_defaults_env_option_and_bad_values():
    s = hs.settings({}, environ={})
    assert {k: s[k] for k in hs.DEFAULTS} == hs.DEFAULTS
    assert s["max_iter"] == 40 and s["dt_mode"] == "cap" and s["dt_s"] == 3.0
    assert s["d1d2_maxiter"] == 3 and s["conn_spacing"] is None and s["outflow"] == "auto"
    assert set(s["_source"].values()) == {"default"}
    assert sorted(hs.ENV.values()) == sorted("NDBREACH_HECRAS_" + k.upper() for k in hs.DEFAULTS)
    env = {"NDBREACH_HECRAS_MAX_ITER": "25", "NDBREACH_HECRAS_DT_MODE": "Fixed",
           "NDBREACH_HECRAS_DT_S": "2", "NDBREACH_HECRAS_OUTFLOW": "single",
           "NDBREACH_HECRAS_CULVERT_KE_BOX": "0.7", "NDBREACH_HECRAS_D1D2_MAXITER": " "}
    s = hs.settings({}, environ=env)
    assert (s["max_iter"], s["dt_mode"], s["dt_s"], s["outflow"], s["culvert_ke_box"]) == \
        (25, "fixed", 2.0, "single", 0.7)
    assert s["d1d2_maxiter"] == 3 and s["_source"]["d1d2_maxiter"] == "default"
    assert s["_source"]["max_iter"] == "env"
    # the build option wins over the environment
    s = hs.settings({"solver": {"max_iter": 30}}, environ=env)
    assert s["max_iter"] == 30 and s["_source"]["max_iter"] == "option"
    # the old (3.10.84) behaviour, all in one
    s = hs.settings({"solver": {"max_iter": 20, "dt_mode": "courant", "d1d2_maxiter": 0,
                                "outflow": "single", "cores": "all",
                                "ic_time_hr": 0, "ic_ramp_frac": 0.1,
                                "breach_progression": "linear"}}, environ={})
    assert [k for k, _, _ in hs.changed(s)] == ["dt_s", "outflow_min_cells"]
    # bad values name the variable
    for env_, word in (({"NDBREACH_HECRAS_MAX_ITER": "50"}, "MAX_ITER"),
                       ({"NDBREACH_HECRAS_MAX_ITER": "2.5"}, "MAX_ITER"),
                       ({"NDBREACH_HECRAS_DT_MODE": "sometimes"}, "DT_MODE"),
                       ({"NDBREACH_HECRAS_DT_S": "0"}, "DT_S"),
                       ({"NDBREACH_HECRAS_D1D2_MAXITER": "21"}, "D1D2_MAXITER"),
                       ({"NDBREACH_HECRAS_CONN_SPACING": "abc"}, "CONN_SPACING"),
                       ({"NDBREACH_HECRAS_CONN_SPACING": "0.05"}, "CONN_SPACING"),
                       ({"NDBREACH_HECRAS_OUTFLOW": "everywhere"}, "OUTFLOW"),
                       ({"NDBREACH_HECRAS_ADAPTIVE": "maybe"}, "ADAPTIVE"),
                       ({"NDBREACH_HECRAS_COURANT_MAX": "9"}, "COURANT_MAX"),
                       ({"NDBREACH_HECRAS_COURANT_MAX": "1", "NDBREACH_HECRAS_COURANT_MIN": "0.6"},
                        "COURANT_MIN"),
                       ({"NDBREACH_HECRAS_CORES": "many"}, "CORES"),
                       ({"NDBREACH_HECRAS_CORES": "0"}, "CORES"),
                       ({"NDBREACH_HECRAS_CORES_SMALL": "8"}, "CORES_SMALL"),
                       ({"NDBREACH_HECRAS_IC_TIME_HR": "-1"}, "IC_TIME_HR"),
                       ({"NDBREACH_HECRAS_IC_RAMP_FRAC": "1.5"}, "IC_RAMP_FRAC"),
                       ({"NDBREACH_HECRAS_BREACH_PROGRESSION": "cubic"}, "BREACH_PROGRESSION")):
        assert word in _raises(hs.settings, {}, environ=env_), env_
    assert "unknown" in _raises(hs.settings, {"solver": {"maxiter": 40}}, environ={})


@test
def hecras_solver_time_step_cap_fixed_and_courant():
    c = {"adaptive": True, "max_doubling": 3, "max_halving": 3}
    s = hs.settings({}, environ={})
    # cap: a 4 s Courant step becomes 3 s and may not double above it
    sec, lab, c2, how = hs.time_step(4.0, s, hm.comp_interval, c)
    assert (sec, lab, c2["max_doubling"], c2["adaptive"]) == (3, "3SEC", 0, True), how
    assert "capped at 3SEC" in how
    # cap: a 1 s Courant step stays, doubling once more keeps it at 2 s <= 3 s
    sec, lab, c2, how = hs.time_step(1.0, s, hm.comp_interval, c)
    assert (sec, lab, c2["max_doubling"]) == (1, "1SEC", 1), how
    # cap with no adaptive step: nothing to limit
    sec, lab, c2, how = hs.time_step(10.0, s, hm.comp_interval, {"adaptive": False})
    assert (sec, lab) == (3, "3SEC") and "max_doubling" not in c2
    # fixed: the step itself, adaptive off
    f = hs.settings({"solver": {"dt_mode": "fixed", "dt_s": 2}}, environ={})
    sec, lab, c2, how = hs.time_step(0.5, f, hm.comp_interval, c)
    assert (sec, lab, c2["adaptive"]) == (2, "2SEC", False)
    # courant: the 3.10.84 rule (no cap)
    o = hs.settings({"solver": {"dt_mode": "courant"}}, environ={})
    sec, lab, c2, how = hs.time_step(4.4, o, hm.comp_interval, c)
    assert (sec, lab, c2) == (4, "4SEC", c)
    assert c["max_doubling"] == 3      # the caller's dict is not changed


def _fake_sys(root, cpus, p_cores=None):
    """A /sys/devices tree: cpus = [(cpu, package, core)]; p_cores = the cpu_core list."""
    c = os.path.join(root, "system", "cpu")
    os.makedirs(c)
    with open(os.path.join(c, "online"), "w") as f:
        f.write("0-{}\n".format(len(cpus) - 1))
    for i, pk, co in cpus:
        t = os.path.join(c, "cpu{}".format(i), "topology")
        os.makedirs(t)
        for n, v in (("physical_package_id", pk), ("core_id", co)):
            with open(os.path.join(t, n), "w") as f:
                f.write("{}\n".format(v))
    if p_cores:
        os.makedirs(os.path.join(root, "cpu_core"))
        with open(os.path.join(root, "cpu_core", "cpus"), "w") as f:
            f.write(p_cores + "\n")


@test
def hecras_solver_cores_physical_only_and_by_mesh_size():
    d = tempfile.mkdtemp()
    try:
        # 8 cores with hyper-threading (16 logical): 8 physical
        smt = os.path.join(d, "smt")
        _fake_sys(smt, [(i, 0, i % 8) for i in range(16)])
        n, how = hs.physical_cores(smt)
        assert n == 8 and "physical cores" in how, how
        # hybrid: 6 P-cores with HT (cpus 0-11) + 8 E-cores (12-19): 6 P-cores
        hyb = os.path.join(d, "hyb")
        _fake_sys(hyb, [(i, 0, i // 2) for i in range(12)] + [(i, 0, 100 + i) for i in
                                                               range(12, 20)], "0-11")
        n, how = hs.physical_cores(hyb)
        assert n == 6 and "E-cores left out" in how, how
        # no /sys: psutil or half the logical CPUs, never 0
        n, how = hs.physical_cores(os.path.join(d, "none"), logical=1)
        assert n >= 1
        s = hs.settings({}, environ={})
        assert hs.cores(5000, s, host=(8, "x"))[0] == 4          # small mesh: 4
        assert hs.cores(5000, s, host=(2, "x"))[0] == 2          # never above the host
        assert hs.cores(250000, s, host=(6, "x"))[0] == 6        # large: physical cores
        s6 = hs.settings({}, environ={"NDBREACH_HECRAS_CORES_SMALL": "6"})
        assert hs.cores(5000, s6, host=(16, "x"))[0] == 6
        assert hs.cores(5000, hs.settings({}, environ={"NDBREACH_HECRAS_CORES": "12"}),
                        host=(8, "x")) == (12, "set (NDBREACH_HECRAS_CORES=12)")
        assert hs.cores(5000, hs.settings({}, environ={"NDBREACH_HECRAS_CORES": "all"}),
                        host=(8, "x"))[0] == 0
    finally:
        shutil.rmtree(d)


@test
def hecras_solver_courant_overrides_and_sine_breach():
    s = hs.settings({}, environ={})
    base = dict(hm.COURANT_DEFAULTS)
    assert hs.apply_courant(base, s) == base                    # defaults: no change
    e = hs.settings({}, environ={"NDBREACH_HECRAS_COURANT_MAX": "0.9",
                                 "NDBREACH_HECRAS_COUNT_TO_DOUBLE": "6",
                                 "NDBREACH_HECRAS_ADAPTIVE": "off"})
    c = hs.apply_courant(base, e)
    assert (c["max"], c["min"], c["count_to_double"], c["adaptive"]) == (0.9, None, 6, False)
    sp = hs.sine_progression()
    assert len(sp) == 11 and sp[0] == (0.0, 0.0) and sp[-1] == (1.0, 1.0) and sp[5] == (0.5, 0.5)
    assert sp[1][1] < 0.1 * 0.5                                  # slow start
    b = hm.breach_progression_lines("sine")
    assert b[0] == "Breach Progression= 11 " and len(b) == 1 + 3, b   # 22 values, 10 a line
    assert hm.breach_progression_lines("linear") == ["Breach Progression= 2 ",
                                                     hm.f8(0) + hm.f8(0) + hm.f8(1) + hm.f8(1)]


@test
def hecras_solver_wet_runs_on_a_ring():
    n = 12
    reached = [False] * n
    for i in (0, 1, 2, 3, 6, 7, 10, 11):
        reached[i] = True
    blocked = [False] * n
    # 10, 11, 0, 1, 2, 3 wraps round the ring; 6, 7 is the other run
    assert hs.wet_runs(reached, blocked, 2) == [[10, 11, 0, 1, 2, 3], [6, 7]]
    assert hs.wet_runs(reached, blocked, 3) == [[10, 11, 0, 1, 2, 3]]
    blocked[1] = True
    assert hs.wet_runs(reached, blocked, 2) == [[10, 11, 0], [2, 3], [6, 7]]
    assert hs.wet_runs([False] * n, [False] * n, 2) == []
    full = hs.wet_runs([True] * n, [False] * n, 2)
    assert len(full) == 1 and len(full[0]) == n - 1       # never the whole ring


@test
def hecras_solver_plan_lines_default_and_env():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        out = os.path.join(d, "model")
        r = hm.build_model(d, out_folder=out, options={})
        f = _files(out, r)
        assert _lines(f[".p01"], "UNET MxIter=") == ["UNET MxIter= 40 "]
        assert _lines(f[".p01"], "UNET D2 Max Iterations=") == ["UNET D2 Max Iterations= 40 "] * 2
        assert _lines(f[".p01"], "UNET D1D2 MaxIter=") == ["UNET D1D2 MaxIter= 3 "]
        ci = _lines(f[".p01"], "Computation Interval=")
        assert ci == ["Computation Interval=3SEC"], ci          # synthetic Courant step 4 s
        assert _lines(f[".p01"], "Computation Time Step Max Doubling=") == \
            ["Computation Time Step Max Doubling=0"]
        # the variable time step block, Courant target 1.0 / 0.45 (SWE)
        ct = _lines(f[".p01"], "Computation Time Step")
        assert ct[0] == "Computation Time Step Use Courant=        -1", ct
        assert "Computation Time Step Max Courant=1" in ct and \
            "Computation Time Step Min Courant=0.45" in ct, ct
        assert "Computation Time Step Count To Double=4" in ct and \
            "Computation Time Step Max Halving=3" in ct, ct
        # cores: a small mesh gets 4 (or the host's physical cores if fewer), a large one
        # the host's physical cores
        n_cells = r["info"]["cells"]
        phys = hs.physical_cores()[0]
        want = min(4, phys) if n_cells < 10000 else phys
        assert _lines(f[".p01"], "UNET D2 Cores") == ["UNET D2 Cores= {} ".format(want)] + \
            ["UNET D2 Cores={}".format(want)] * 2, _lines(f[".p01"], "UNET D2 Cores")
        assert _lines(f[".p01"], "UNET D1 Cores=") == ["UNET D1 Cores= 0 "]
        assert _lines(f[".p01"], "UNET D2 TotalICTime=") == ["UNET D2 TotalICTime=0.5"] * 2
        assert _lines(f[".p01"], "UNET D2 RampUpFraction=") == ["UNET D2 RampUpFraction=0.5"] * 2
        bp = _lines(f[".p01"], "Breach Progression=")
        assert bp == ["Breach Progression= 11 "], bp
        info = r["info"]["solver"]
        assert info["d2_cores"] == want and "cells" in info["cores_how"], info
        assert info["settings"]["max_iter"] == 40 and "capped at 3SEC" in info["time_step_how"]
        assert ["max_iter", 20, 40] in info["changed_from_previous"]
        for p in f.values():
            assert p0.unknown_keywords(p) == set(), (p, p0.unknown_keywords(p))
        rd = open(os.path.join(out, "README_HECRAS.md")).read()
        assert "NDBREACH_HECRAS_" in rd and "3SEC" in rd
        # every setting can be set from the environment
        with _Env(max_iter=25, dt_mode="fixed", dt_s=2, d1d2_maxiter=0, outflow="single"):
            r = hm.build_model(d, out_folder=out, options={})
        assert _lines(f[".p01"], "UNET MxIter=") == ["UNET MxIter= 25 "]
        # the new ones: Courant, cores, IC ramp-up, breach progression
        with _Env(courant_max=0.9, max_halving=4, cores=6, ic_time_hr=0, ic_ramp_frac=0.2,
                  breach_progression="linear"):
            r2 = hm.build_model(d, out_folder=out, options={})
        ct = _lines(f[".p01"], "Computation Time Step")
        assert "Computation Time Step Max Courant=0.9" in ct and \
            "Computation Time Step Min Courant=0.405" in ct and \
            "Computation Time Step Max Halving=4" in ct, ct
        assert _lines(f[".p01"], "UNET D2 Cores") == ["UNET D2 Cores= 6 "] + \
            ["UNET D2 Cores=6"] * 2
        assert _lines(f[".p01"], "UNET D2 TotalICTime=") == ["UNET D2 TotalICTime="] * 2
        assert _lines(f[".p01"], "UNET D2 RampUpFraction=") == ["UNET D2 RampUpFraction=0.2"] * 2
        assert _lines(f[".p01"], "Breach Progression=") == ["Breach Progression= 2 "]
        assert p0.unknown_keywords(f[".p01"]) == set()
        assert r2["info"]["solver"]["d2_cores"] == 6
        with _Env(max_iter=25, dt_mode="fixed", dt_s=2, d1d2_maxiter=0, outflow="single"):
            r = hm.build_model(d, out_folder=out, options={})
        assert _lines(f[".p01"], "UNET D2 Max Iterations=") == ["UNET D2 Max Iterations= 25 "] * 2
        assert _lines(f[".p01"], "UNET D1D2 MaxIter=") == ["UNET D1D2 MaxIter= 0 "]
        assert _lines(f[".p01"], "Computation Interval=") == ["Computation Interval=2SEC"]
        assert _lines(f[".p01"], "Computation Time Step Use Courant=") == \
            ["Computation Time Step Use Courant=        0"]
        assert r["info"]["solver"]["source"]["max_iter"] == "env"
        # a bad value stops the build before any work, naming the variable
        with _Env(max_iter=99):
            try:
                hm.build_model(d, out_folder=out, options={})
                raise AssertionError("no error")
            except hm.HecRasModelError as e:
                assert "NDBREACH_HECRAS_MAX_ITER" in str(e), e
    finally:
        shutil.rmtree(d)


@test
def hecras_solver_clip_to_the_anuga_mesh_with_outflow_lines():
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        out = os.path.join(d, "model")
        # no ANUGA outline: the buffered flood outline, which the flood does not reach
        r = hm.build_model(d, out_folder=out, options={})
        o = r["info"]["outflow"]
        assert o["mode"] == "auto" and not o["clip"]["used"] and o["lines"] == []
        assert "no ANUGA mesh outline" in o["clip"]["why"]
        n_pts0 = len(r["model"].points)
        # 'anuga' with no outline: a warning, same model
        r = hm.build_model(d, out_folder=out, options={"solver": {"outflow": "anuga"}})
        assert any(n.startswith("WARNING: 2D area not clipped") for n in r["notes"]), r["notes"]
        # an outline 600 m wide where the flood is 800 m wide: clipped, outflow on both sides
        _mesh(d)
        r = hm.build_model(d, out_folder=out, options={})
        o = r["info"]["outflow"]
        assert o["clip"]["used"] and 0.0 < o["clip"]["area_kept"] < 1.0, o
        assert len(r["model"].points) < n_pts0
        names = [q["name"] for q in o["lines"]]
        assert names and names[0] == "Outflow 1" and len(names) <= hs.MAX_OUTFLOW_LINES, o
        assert o["perimeter_reached_frac"] > 0.3, o
        f = _files(out, r)
        g = _lines(f[".g01"], "BC Line Name=")
        assert len(g) == 1 + len(names), g
        assert ["BC Line Name=" + n for n in names] == [x.rstrip() for x in g[1:]], g
        assert len(_lines(f[".g01"], "LCMann Time=")) == 1
        u = open(f[".u01"]).read()
        for n in names:
            assert ",{}".format(n) in u.replace(" ", "") or n in u, n
        assert len(_lines(f[".u01"], "Friction Slope=")) == 1 + len(names)
        for p in f.values():
            assert p0.unknown_keywords(p) == set(), (p, p0.unknown_keywords(p))
        assert any(n.startswith("2D area clipped to the ANUGA mesh outline") for n in r["notes"])
        assert "Outflow" in open(os.path.join(out, "README_HECRAS.md")).read()
        # 'single': the 3.10.84 model (no clip, one boundary line) even with the outline there
        r = hm.build_model(d, out_folder=out, options={"solver": {"outflow": "single"}})
        assert not r["info"]["outflow"]["clip"]["used"] and r["info"]["outflow"]["lines"] == []
        assert len(_lines(f[".g01"], "BC Line Name=")) == 1
        assert len(r["model"].points) == n_pts0
        # 'edges': no clip, edges only where the flood reaches (here nowhere)
        r = hm.build_model(d, out_folder=out, options={"solver": {"outflow": "edges"}})
        assert not r["info"]["outflow"]["clip"]["used"]
        # an outline that leaves out the ground below the dam is not used
        _mesh(d, half_w=200.0, top=-2000.0, bottom=-3000.0)
        r = hm.build_model(d, out_folder=out, options={})
        assert not r["info"]["outflow"]["clip"]["used"], r["info"]["outflow"]
    finally:
        shutil.rmtree(d)


@test
def hecras_solver_structure_coefficients_from_the_settings():
    """The road weir coefficient and culvert losses of the connections come from the settings
    (defaults: HEC-RAS's 2.6, Ke 0.5 pipe / 0.4 box, exit 1.0, as before)."""
    if p0._skip():
        return
    d = tempfile.mkdtemp()
    try:
        p0._run(d)
        an = os.path.join(d, "anuga_run")
        bm = os.path.join(an, "_breach_model")
        os.makedirs(bm)
        yc = p0.Y0 - 1500.0
        used = [{"kind": "culvert", "line": [[p0.X0, yc + 15.0], [p0.X0, yc - 15.0]],
                 "width_m": 1.2, "height_m": 0.9, "shape": "box", "barrels": 1,
                 "invert_up_m": None, "invert_dn_m": None, "manning": 0.013, "losses": 1.0,
                 "name": "box 1"}]
        with open(os.path.join(bm, "run_report.json"), "w") as f:
            json.dump({"structures_used": used, "work_crs": "EPSG:26914"}, f)
        out = os.path.join(d, "model")
        r = hm.build_model(d, out_folder=out, options={"structures_from": an})
        c = r["model"].structures[0]
        assert c["weir_coef"] == 2.6 and c["culvert"]["entrance"] == 0.4 and \
            c["culvert"]["exit"] == 1.0, c
        with _Env(struct_weir_coef=3.0, culvert_ke_box=0.7, culvert_exit_loss=0.8,
                  dam_weir_coef=3.1):
            r = hm.build_model(d, out_folder=out, options={"structures_from": an})
        c = r["model"].structures[0]
        assert c["weir_coef"] == 3.0 and c["culvert"]["entrance"] == 0.7 and \
            c["culvert"]["exit"] == 0.8, c
        g = _files(out, r)[".g01"]
        w = _lines(g, "Conn Weir Coef=")
        assert any(x.startswith("Conn Weir Coef=3.1") for x in w), w
        assert any(x.rstrip() in ("Conn Weir Coef=3", "Conn Weir Coef=3.0") for x in w), w
        cl = _lines(g, "Connection Culv=")
        assert len(cl) == 1 and ",0.7,0.8," in cl[0], cl
        assert p0.unknown_keywords(g) == set()
    finally:
        shutil.rmtree(d)
