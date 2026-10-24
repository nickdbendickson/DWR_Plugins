"""3.10.70: the ANUGA worker's read-only time-step diagnostic (core/anuga_dtdiag.py) and the
wall clock / internal steps appended to every PROGRESS line.

Unit tests for the limiting triangle (argmin radii / max_speed, the wet-state fallback) and
the nearest structure operator; the on / off rules (job key, NDBREACH_ANUGA_DT_DIAG[_EVERY],
anuga_dt_diag), an off job as before and the checkpoint fingerprint unchanged; the worker
with tests/fake_anuga.py (rows, summary, a stopped and resumed run appends without repeats,
the same results on and off). The model test (--models, NDBREACH_ANUGA_PYTHON) runs the
real worker on the 589-triangle checkpoint-test domain with a culvert, off and on, and
compares the final stage / xmomentum / ymomentum bit for bit. Seconds."""
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_dtdiag as DD  # noqa: E402
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.tests import test_anuga_checkpoint as TC  # noqa: E402
from nd_breach_runner.tests.registry import test, model_test  # noqa: E402

DIAG_ENV = ("NDBREACH_ANUGA_DT_DIAG", "NDBREACH_ANUGA_DT_DIAG_EVERY")


class _Env(TC._Env):
    def __enter__(self):
        for k in DIAG_ENV:
            self.kw.setdefault(k, None)
        return TC._Env.__enter__(self)


def _rows(d):
    with open(os.path.join(d, DD.CSV_NAME), newline="") as f:
        return list(csv.DictReader(f))


# ----------------------------------------------------------------- limiting triangle
@test
def dt_diag_limiting_triangle_is_argmin_radius_over_wave_speed():
    r = np.array([5.0, 2.0, 8.0, 1.0, 3.0])
    ms = np.array([1.0, 4.0, 2.0, 0.0, 10.0])      # tri 3 is dry (0): never the limit
    r0, ms0 = r.copy(), ms.copy()
    k, ldt, wave, src = DD.limiting_triangle(r, ms)
    assert (k, src) == (4, "max_speed") and abs(ldt - 0.3) < 1e-12 and wave == 10.0
    assert np.array_equal(r, r0) and np.array_equal(ms, ms0)      # read only
    # all-zero wave speeds (a dry first step): |v| + sqrt(g h) of the wet triangles
    h = np.array([0.0, 0.5, 2.0, 0.0, 0.1])
    sp = np.array([0.0, 1.0, 0.0, 9.0, 0.0])
    k, ldt, wave, src = DD.limiting_triangle(r, np.zeros(5), h, sp, g=9.8, h_dry=1e-3)
    want = r / np.where(h > 1e-3, sp + np.sqrt(9.8 * h), np.nan)
    assert src == "state" and k == int(np.nanargmin(want)) and abs(ldt - np.nanmin(want)) < 1e-12
    assert DD.limiting_triangle(r, np.zeros(5)) is None                  # dry, no state
    assert DD.limiting_triangle(r, np.zeros(5), np.zeros(5), np.zeros(5)) is None
    assert DD.limiting_triangle(r, np.ones(3)) is None                   # wrong length


@test
def dt_diag_limiting_triangle_large_mesh_is_vectorised():
    rng = np.random.default_rng(1)
    r = rng.uniform(1, 20, 300000)
    ms = rng.uniform(0, 5, 300000)
    ms[rng.integers(0, 300000, 1000)] = 0.0
    k, ldt, _, _ = DD.limiting_triangle(r, ms)
    with np.errstate(divide="ignore"):
        ref = np.where(ms > 0, r / ms, np.inf)
    assert k == int(np.argmin(ref)) and ldt == ref[k]


# ----------------------------------------------------------------- nearest operator
class _Inl:
    def __init__(self, idx):
        self.triangle_indices = np.array(idx)


class Parallel_Inlet_operator:
    def __init__(self, idx):
        self.inlet, self.label = _Inl(idx), "inlet_1_P0"


class Parallel_Boyd_box_operator:
    def __init__(self, a, b):
        self.inlets, self.label = [_Inl(a), _Inl(b)], "structure_0"


class Envelope:
    indices = None
    label = "envelope"


class boundary_flux_integral_operator:
    label = "operator_0"


class _Dom:
    def __init__(self, ops):
        self.centroid_coordinates = np.array([[0.0, 0.0], [100.0, 0.0], [200.0, 0.0],
                                              [300.0, 0.0], [400.0, 0.0]])
        self.fractional_step_operators = ops


@test
def dt_diag_nearest_operator_names_inlet_and_culverts():
    dom = _Dom([boundary_flux_integral_operator(), Parallel_Inlet_operator([0]), Envelope(),
                Parallel_Boyd_box_operator([2], [3]), Parallel_Boyd_box_operator([4], [4])])
    regs = DD.operator_regions(dom, ["Main St culvert"], x_off=1000.0, y_off=5000.0)
    assert [(n, k) for n, k, _ in regs] == [("breach inlet", "breach inlet"),
                                            ("Main St culvert", "culvert"),
                                            ("structure_0", "culvert")]   # no name: label
    assert np.allclose(regs[1][2], [[1200.0, 5000.0], [1300.0, 5000.0]])  # model CRS
    name, kind, d = DD.nearest_operator(1290.0, 5030.0, regs)
    assert (name, kind) == ("Main St culvert", "culvert") and abs(d - np.hypot(10, 30)) < 1e-9
    assert DD.nearest_operator(0.0, 0.0, []) is None
    # "near" = within max(3 inradii, 50 m)
    assert DD.near_limit(5.0) == 50.0 and DD.near_limit(40.0) == 120.0


# ----------------------------------------------------------------- on / off rules
@test
def dt_diag_off_by_default_env_and_job_rules():
    assert DD.dt_diag_every({}, {}) == 0
    assert DD.dt_diag_every({"dt_diag_every": np.array(3)}, {}) == 3
    assert DD.dt_diag_every({}, {"NDBREACH_ANUGA_DT_DIAG": "1"}) == 1
    assert DD.dt_diag_every({}, {"NDBREACH_ANUGA_DT_DIAG": "1",
                                 "NDBREACH_ANUGA_DT_DIAG_EVERY": "4"}) == 4
    assert DD.dt_diag_every({}, {"NDBREACH_ANUGA_DT_DIAG_EVERY": "4"}) == 0    # not on
    assert DD.dt_diag_every({"dt_diag_every": 3}, {"NDBREACH_ANUGA_DT_DIAG": "0"}) == 0
    assert DD.dt_diag_every({}, {"NDBREACH_ANUGA_DT_DIAG": "on",
                                 "NDBREACH_ANUGA_DT_DIAG_EVERY": "x"}) == 1
    assert AM.dt_diag_setting(0, {}) == 0 and AM.dt_diag_setting(True, {}) == 1
    assert AM.dt_diag_setting("on", {}) == 1 and AM.dt_diag_setting("5", {}) == 5
    assert AM.dt_diag_setting(None, {"NDBREACH_ANUGA_DT_DIAG": "1"}) == 1
    try:
        AM.dt_diag_setting("sometimes", {})
        raise AssertionError("bad value accepted")
    except ValueError:
        pass


@test
def dt_diag_job_keys_only_when_on_and_never_in_the_fingerprint():
    st = AM.AnugaSettings("python")
    d = tempfile.mkdtemp()
    try:
        g = Grid(np.full((4, 5), 10.0), 0.0, 40.0, 10.0, crs_wkt="EPSG:26914")
        hy = type("H", (), {"t_s": np.array([0.0, 60.0]), "q_cms": np.array([0.0, 1.0])})()
        sq = np.array([[0, 0], [50, 0], [50, 40], [0, 40]], float)
        p = os.path.join(d, "j.npz")
        AM.write_job(p, g, sq, np.zeros((0, 2)), sq[:2], hy, st, g)
        with np.load(p) as z:
            off = dict(z)
        assert not set(off) & set(DD.DT_DIAG_KEYS), sorted(off)
        st.dt_diag = 2
        AM.write_job(p, g, sq, np.zeros((0, 2)), sq[:2], hy, st, g)
        with np.load(p) as z:
            on = dict(z)
        assert int(on["dt_diag_every"]) == 2 and str(on["dt_diag_crs"]) == "EPSG:26914"
        assert AW.job_fingerprint(off) == AW.job_fingerprint(on)
    finally:
        shutil.rmtree(d)


@test
def dt_diag_run_setting_not_in_run_key_and_report_only_when_on():
    from nd_breach_runner.core import workflow as W
    with _Env():
        st = W.RunSettings()
        assert st.anuga_dt_diag == 0 and "anuga_dt_diag" not in st.as_dict()
        k0 = W.key_settings(st)
        st2 = W.RunSettings(anuga_dt_diag=3)
        assert st2.anuga_dt_diag == 3 and st2.as_dict()["anuga_dt_diag"] == 3
        assert W.key_settings(st2) == k0
    with _Env(NDBREACH_ANUGA_DT_DIAG="1", NDBREACH_ANUGA_DT_DIAG_EVERY="2"):
        assert W.RunSettings().anuga_dt_diag == 2


# ----------------------------------------------------------------- PROGRESS lines
@test
def progress_line_keeps_its_format_with_clock_fields_appended():
    class D:
        number_of_steps = 17
    c = DD.YieldClock(0.0)
    c.wall -= 2.0
    f = c.tick(D(), 300.0)
    assert f["steps"] == 17 and f["steps_total"] == 17 and f["wall_s"] >= 2.0
    f = c.tick(D(), 600.0)
    assert f["steps_total"] == 34
    line = "PROGRESS {:.2f} t={:.0f}s{}".format(2.5, 600.0, c.suffix(f))
    parts = line.split(None, 2)                 # anuga_model._launch's parser
    assert parts[0] == "PROGRESS" and float(parts[1]) == 2.5 and parts[2].startswith("t=600s ")
    assert "t=600s" in line                     # the checkpoint model test's match
    m = re.search(r"utc=(\S+) steps=(\d+) dsteps=(\d+) wall_s=([\d.]+)", line)
    assert m and re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", m.group(1)), line
    assert (m.group(2), m.group(3)) == ("34", "17")
    msgs = []
    AM_line = parts                              # what the GUI progress bar gets
    msgs.append((float(AM_line[1]), "ANUGA " + AM_line[2].strip()))
    assert msgs[0][1].startswith("ANUGA t=600s utc=")


# ----------------------------------------------------------------- worker (fake ANUGA)
@test
def dt_diag_fake_worker_rows_summary_and_same_results():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        with _Env():
            g0, s0 = TC._grids(TC._fake_run(d0))
        assert not os.path.exists(os.path.join(d0, DD.CSV_NAME)) and "dt_diag" not in s0
        with _Env(NDBREACH_ANUGA_DT_DIAG="1", NDBREACH_ANUGA_DT_DIAG_EVERY="2"):
            g1, s1 = TC._grids(TC._fake_run(d1))
        TC._same(g0, g1)
        rows = _rows(d1)
        assert [float(r["t_s"]) for r in rows] == [120.0 * i for i in range(1, 16)]
        assert all(int(r["steps"]) == 6 and int(r["yields"]) == 2 for r in rows)
        assert all(r["source"] == "max_speed" and r["tri"] for r in rows[1:]), rows[1]
        r = rows[5]
        assert abs(float(r["dt_mean_s"]) - 20.0) < 1e-9 and float(r["dt_min_s"]) == 20.0
        assert float(r["x"]) > 500000 and float(r["y"]) > 5200000      # model CRS
        assert float(r["depth_m"]) > 0 and r["utc"].endswith("Z")
        dd = s1["dt_diag"]
        assert dd["samples"] == 15 and dd["every_yields"] == 2 and dd["top"], dd
        assert os.path.exists(os.path.join(d1, DD.SUMMARY_NAME))
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def dt_diag_fake_worker_resume_appends_without_repeats():
    d0, d1 = tempfile.mkdtemp(), tempfile.mkdtemp()
    try:
        with _Env(NDBREACH_ANUGA_DT_DIAG="1"):
            TC._fake_run(d0)
            assert TC._fake_run(d1, stop_at=1000.0, ckpt_every_steps=4,
                                partial_every_min=0.0) is None
            before = _rows(d1)
            assert float(before[-1]["t_s"]) == 960.0 or float(before[-1]["t_s"]) > 960.0
            _, s1 = TC._grids(TC._fake_run(d1, ckpt_every_steps=4, resume=1))
        assert s1["checkpoint"]["resumed_from_t_s"] == 960.0
        full, res = _rows(d0), _rows(d1)
        ts = [float(r["t_s"]) for r in res]
        assert ts == [float(r["t_s"]) for r in full] == [60.0 * i for i in range(1, 31)], ts
        assert [r["tri"] for r in res] == [r["tri"] for r in full]
        with open(os.path.join(d1, DD.CSV_NAME)) as f:
            assert f.read().count("utc,t_s,") == 1
        assert s1["dt_diag"]["samples"] == 30 and s1["dt_diag"]["samples_this_run"] == 14
    finally:
        shutil.rmtree(d0)
        shutil.rmtree(d1)


@test
def dt_diag_summary_weights_by_samples_steps_and_wall():
    rows = [{"tri": "7", "steps": "100", "model_s": "300", "wall_s": "10", "dt_tri_s": "0.3",
             "near_op": "Rd culvert", "near_op_type": "culvert", "x": "1", "y": "2"},
            {"tri": "7", "steps": "100", "model_s": "300", "wall_s": "10", "dt_tri_s": "0.28"},
            {"tri": "9", "steps": "300", "model_s": "300", "wall_s": "80", "dt_tri_s": "0.1"},
            {"tri": "", "steps": "1"}]
    s = DD.summarize(rows)
    assert [d["tri"] for d in s] == [7, 9]
    assert s[0]["samples"] == 2 and s[0]["dt_min_s"] == 0.28 and s[0]["share_steps"] == 0.4
    assert s[1]["share_wall"] == 0.8
    txt = DD.summary_text(s, 4, "timestep_diag.csv")
    assert "tri 7: 2 samples" in txt and "tri 9" in txt


# ----------------------------------------------------------------- real ANUGA
_CAPTURE = r'''
import sys, numpy as np
sys.path.insert(0, sys.argv[3])
import anuga_dtdiag, anuga_worker
_tick = anuga_dtdiag.YieldClock.tick
def tick(self, domain, t):
    q = domain.quantities
    np.savez(sys.argv[4], **{k: np.array(q[k].centroid_values, copy=True)
                             for k in ("stage", "xmomentum", "ymomentum")})
    return _tick(self, domain, t)
anuga_dtdiag.YieldClock.tick = tick
anuga_worker.main(sys.argv[1], sys.argv[2])
'''


def _culvert_job(path):
    x0, y0 = 500000.0, 5200600.0
    cul = [{"name": "Test Rd culvert", "end_points": [[x0 + 300, y0 - 300], [x0 + 340, y0 - 300]],
            "invert_elevations": [98.4, 98.2], "width": 1.5, "height": 1.0, "barrels": 1,
            "shape": "box", "manning": 0.013, "losses": 1.5, "apron": 0.0}]
    return TC._job(path, culverts_json=json.dumps(cul), dt_diag_crs="EPSG:26914")


@model_test
def dt_diag_real_anuga_on_and_off_are_bit_identical():
    """The 589-triangle domain with a culvert for 30 simulated minutes, the diagnostic off
    and on (every yield step): the final stage / xmomentum / ymomentum and the result grids
    are equal bit for bit; the rows' limiting triangle gives the step's CFL dt."""
    py = TC._anuga_python()
    if not py:
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    out = {}
    dirs = []
    try:
        for name, flag in (("off", "0"), ("on", "1")):
            d = tempfile.mkdtemp()
            dirs.append(d)
            _culvert_job(os.path.join(d, "j.npz"))
            with open(os.path.join(d, "cap.py"), "w") as f:
                f.write(_CAPTURE)
            env = dict(AM._clean_env(), OMP_NUM_THREADS="1", NDBREACH_ANUGA_DT_DIAG=flag)
            env.pop("NDBREACH_ANUGA_DT_DIAG_EVERY", None)
            subprocess.run([py, "-u", "cap.py", "j.npz", "r.npz",
                            os.path.dirname(AW.__file__), "final.npz"], cwd=d, env=env,
                           check=True, stdout=subprocess.DEVNULL, timeout=300)
            with np.load(os.path.join(d, "final.npz")) as z:
                out[name] = {k: z[k].copy() for k in z.files}
            out[name + "_grids"] = TC._grids(os.path.join(d, "r.npz"))[0]
        for k in ("stage", "xmomentum", "ymomentum"):
            assert out["on"][k].tobytes() == out["off"][k].tobytes(), k
        TC._same(out["off_grids"], out["on_grids"])
        assert not os.path.exists(os.path.join(dirs[0], DD.CSV_NAME))
        rows = _rows(dirs[1])
        assert len(rows) == 30 and all(r["source"] == "max_speed" for r in rows)
        for r in rows[2:]:        # the limiting triangle's CFL dt = the last step's
            assert abs(float(r["dt_tri_s"]) - float(r["dt_cfl_last_s"])) < 1e-9, r
            assert float(r["lon"]) < -98 and 46 < float(r["lat"]) < 48
    finally:
        for d in dirs:
            shutil.rmtree(d)
