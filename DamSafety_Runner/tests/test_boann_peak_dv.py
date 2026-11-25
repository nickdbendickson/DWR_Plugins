"""Boann 6.5.0 / Runner 3.99.0: peak depth x velocity (.maxDV).

Each solver records, per cell, the largest depth times speed at one moment. No existing
result moves: the same decks on origin/main, one core, match every pre-existing result
file byte for byte. The Runner raster is off unless write_peak_dv is on, and it is not
part of the checkpoint key.

    python3 -m nd_breach_runner.tests.test_boann_peak_dv
"""
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types

import numpy as np

from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import workflow
from nd_breach_runner.core.breach import Hydrograph
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood.cases import level_pool_lake, read_asc, valley_deck
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.synthetic import npy_writer


OLD_EXTS = ("max", "maxVc", "inittm", "maxtm", "mxe", "mass")
SOLVERS = ("acc", "sgc", "fv", "fv2", "dg2", "subfv", "subli")
# 3-decimal ASCII (deckio._write_asc): dv <= h*v plus half a count in each factor, plus one count
ROUND = 0.0005


def _need_cpu():
    if os.name == "nt" and not mfb.prebuilt_available():
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.prebuilt_available() and not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


def _env(**kv):
    class _E:
        def __enter__(self_):
            self_.was = {k: os.environ.get(k) for k in kv}
            for k, v in kv.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        def __exit__(self_, *exc):
            for k, v in self_.was.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    return _E()


def _small_deck(folder):
    # hole=False: a NoData hole makes the SGC solver rebuild channels, and that fill asks for
    # scipy when a cell is NoData. The Apple GPU job installs numpy only.
    return valley_deck(folder, ny=18, nx=28, dx=20.0, sim_s=400.0, peak_cms=120.0,
                       hole=False, pond=True, nfile=False, gauge=False)


def _bytes(folder, ext):
    with open(os.path.join(folder, "results", "run." + ext), "rb") as f:
        return f.read()


@test
def boann_peak_dv_is_the_concurrent_value_on_every_solver():
    """Where the water is over 1 mm, .maxDV is finite and no larger than peak depth times peak
    speed (allowing the 3-decimal write), and strictly smaller somewhere: the two peaks are
    not from the same moment. A lake at rest stays at 0."""
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_dv_")
    try:
        with _env(DAMSAFETY_MF_THREADS="1"):
            for solver in SOLVERS:
                folder = os.path.join(d, solver)
                par = _small_deck(folder)
                cli.run(par, solver=solver, backend="cpu", quiet=True)
                h = read_asc(os.path.join(folder, "results", "run.max"))
                vc = read_asc(os.path.join(folder, "results", "run.maxVc"))
                dv = read_asc(os.path.join(folder, "results", "run.maxDV"))
                nod = -9999.0
                wet = (h != nod) & (h > 0.001)
                assert wet.any(), solver
                assert np.isfinite(dv[wet]).all() and (dv[wet] != nod).all(), solver
                cap = h[wet] * vc[wet] + ROUND * (h[wet] + vc[wet]) + 0.0006
                assert np.all(dv[wet] <= cap + 1e-9), (solver, float((dv[wet] - cap).max()))
                assert np.any(dv[wet] < h[wet] * vc[wet] - 0.002), solver
            still = level_pool_lake(os.path.join(d, "still"), n=14, sim_s=80.0, free="",
                                    inflow_cms=0.0, h0=1.0)
            cli.run(still, solver="acc", backend="cpu", quiet=True)
            dv = read_asc(os.path.join(d, "still", "results", "run.maxDV"))
            assert float(np.max(dv[dv != -9999.0])) == 0.0, float(np.max(dv))
    finally:
        shutil.rmtree(d, True)


@test
def boann_peak_dv_leaves_existing_results_identical_to_main():
    """The same decks, one core, on this tree and on origin/main: every result file that
    existed before .maxDV matches byte for byte. Skips when git or origin/main is missing
    (the CPU library's libm differs between Linux and macOS, so the bytes are not pinned)."""
    _need_cpu()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    probe = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "origin/main"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        raise Skip("no git origin/main to compare against ({})".format(
            probe.stderr.strip() or probe.returncode))
    wt = tempfile.mkdtemp(prefix="boann_dv_main_")
    out = tempfile.mkdtemp(prefix="boann_dv_cmp_")
    try:
        add = subprocess.run(["git", "-C", root, "worktree", "add", "--detach", wt, "origin/main"],
                             capture_output=True, text=True)
        if add.returncode != 0:
            raise Skip("git worktree add failed: {}".format(add.stderr.strip()))
        script = r"""
import os, sys
os.environ["DAMSAFETY_MF_THREADS"] = "1"
root, dest = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood.cases import valley_deck
solvers = ("acc", "sgc", "fv", "fv2", "dg2", "subfv", "subli")
for solver in solvers:
    folder = os.path.join(dest, solver)
    par = valley_deck(folder, ny=18, nx=28, dx=20.0, sim_s=400.0, peak_cms=120.0,
                      hole=False, pond=True, nfile=False, gauge=False)
    cli.run(par, solver=solver, backend="cpu", quiet=True)
print("old-ok")
"""
        old_dest = os.path.join(out, "old")
        os.makedirs(old_dest)
        run = subprocess.run([sys.executable, "-c", script, wt, old_dest],
                             capture_output=True, text=True,
                             env=dict(os.environ, PYTHONPATH=wt, DAMSAFETY_MF_THREADS="1"))
        if run.returncode != 0 or "old-ok" not in run.stdout:
            raise AssertionError("origin/main run failed:\n{}\n{}".format(run.stdout, run.stderr))
        with _env(DAMSAFETY_MF_THREADS="1"):
            for solver in SOLVERS:
                folder = os.path.join(out, "new", solver)
                cli.run(_small_deck(folder), solver=solver, backend="cpu", quiet=True)
                for ext in OLD_EXTS:
                    a = _bytes(os.path.join(old_dest, solver), ext)
                    b = _bytes(folder, ext)
                    assert a == b, "{} run.{} differs from origin/main".format(solver, ext)
                assert os.path.isfile(os.path.join(folder, "results", "run.maxDV")), solver
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt],
                       capture_output=True)
        shutil.rmtree(wt, True)
        shutil.rmtree(out, True)


@test
def boann_peak_dv_matches_ritter_at_the_dam():
    """case_ritter's dam, run on Boann's shock-capturing engine (lisflood.GPU_FV_EXE). At the
    dam the depth stays 4/9 h0 and the speed 2/3 sqrt(g h0), so depth x speed is
    (8/27) sqrt(g) h0^1.5 = 0.928 m2/s for h0 = 1 m. The two cells either side of the dam,
    within 5%."""
    _need_cpu()
    from nd_breach_runner.core import verification as ver
    from nd_breach_runner.core.metalflood import engine_fv
    g, h0, T, n = ver.G_LISFLOOD, 1.0, 20.0, 0.001
    dx = 2.0
    c0 = math.sqrt(g * h0)
    nr = int(math.ceil(1.3 * c0 * T / dx)) + 6
    nd = int(math.ceil(2.3 * c0 * T / dx)) + 6
    rows = 3
    ny, nx = rows + 2, nr + nd + 2
    x = (np.arange(nx) - 1 - nr + 0.5) * dx
    dem, _zw = ver._walled_dem(np.zeros((ny, nx)), dx, float(x[0] - 0.5 * dx))
    chan = dem.data == 0.0
    init = np.where(chan & (x[None, :] < 0.0), h0, 0.0)
    d = tempfile.mkdtemp(prefix="boann_dv_ritter_")
    try:
        st = lf.LisfloodSettings(lf.GPU_FV_EXE, manning_n=n, sim_time_s=T, massint_s=T,
                                 initial_tstep_s=0.05, arrival_depth_m=0.05 * h0,
                                 initial_depth=dem.like(init))
        par = lf.write_inputs(d, dem, dem.xy(ny // 2, 1), ver._zero_hydrograph(T), st)
        with _env(DAMSAFETY_MF_THREADS="1"):
            engine_fv.run_par(par, backend="cpu")
        dv = read_asc(os.path.join(d, "results", "run.maxDV"))
        left = int(np.argmin(np.abs(x + 0.5 * dx)))
        right = int(np.argmin(np.abs(x - 0.5 * dx)))
        expect = (8.0 / 27.0) * math.sqrt(g) * h0 ** 1.5
        r = ny // 2
        for c in (left, right):
            got = float(dv[r, c])
            assert abs(got - expect) <= 0.05 * expect, (c, got, expect, float(x[c]))
    finally:
        shutil.rmtree(d, True)


@test
def boann_peak_dv_gpu_matches_itself_and_the_cpu_build():
    """Apple GPU job: two GPU runs, and GPU tiles on against off, give the same .maxDV bytes.
    GPU against the CPU build agrees within 0.1 m2/s, the .maxVc tolerance."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_dv_gpu_")
    try:
        def run(name, backend, tiles):
            with _env(DAMSAFETY_MF_TILES=tiles, DAMSAFETY_MF_THREADS="1"):
                cli.run(_small_deck(os.path.join(d, name)), solver="acc", backend=backend,
                        quiet=True)
        run("g1", "metal", None)
        run("g2", "metal", None)
        assert _bytes(os.path.join(d, "g1"), "maxDV") == _bytes(os.path.join(d, "g2"), "maxDV")
        run("off", "metal", "0")
        assert _bytes(os.path.join(d, "g1"), "maxDV") == _bytes(os.path.join(d, "off"), "maxDV")
        run("cpu", "cpu", "0")
        a = read_asc(os.path.join(d, "g1", "results", "run.maxDV"))
        b = read_asc(os.path.join(d, "cpu", "results", "run.maxDV"))
        m = (a != -9999.0) & (b != -9999.0)
        assert float(np.abs(a - b)[m].max()) <= 0.1
    finally:
        shutil.rmtree(d, True)


def _mass(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("Time Tstep MinTstep NumTsteps Area Vol Qin Hds Qout Qerror Verror Rain\n")
        f.write("100.000 1.0000 1.0000 10 200.0 50.000 1.0000 0.0000 0.0000 0.000000 "
                "0.0000 0.0000\n")


def _results(folder, dv=None):
    res = os.path.join(folder, "results")
    os.makedirs(res)
    dem = Grid(np.zeros((3, 4)), 0.0, 30.0, 10.0)
    h = np.array([[0.0, 0.01, 0.5, 1.0], [0.0, 0.2, 0.8, 0.0], [0.0, 0.0, 0.0, 0.0]])
    v = np.array([[0.0, 1.0, 0.4, 0.2], [0.0, 0.3, 0.5, 0.0], [0.0, 0.0, 0.0, 0.0]])
    dem.like(h).write_asc(os.path.join(res, "run.max"))
    dem.like(v).write_asc(os.path.join(res, "run.maxVc"))
    dem.like(np.zeros_like(h)).write_asc(os.path.join(res, "run.inittm"))
    dem.like(h).write_asc(os.path.join(res, "run.mxe"))
    if dv is not None:
        dem.like(dv).write_asc(os.path.join(res, "run.maxDV"))
    _mass(os.path.join(res, "run.mass"))
    return dem


@test
def runner_reads_peak_dv_only_when_the_file_is_there():
    hy = Hydrograph(np.array([0.0, 200.0]), np.array([1.0, 0.0]), "t")
    st = lf.LisfloodSettings("/nonexistent")
    d = tempfile.mkdtemp(prefix="boann_dv_read_")
    try:
        dem = _results(os.path.join(d, "no"))
        env = lf.read_results(os.path.join(d, "no"), dem, st, hy)
        assert env.dv is None and env.stats["dv_written"] is False
        dv = np.array([[9.0, 9.0, 0.15, 0.10], [0.0, 0.04, 0.30, 0.0],
                       [0.0, 0.0, 0.0, 0.0]])
        dem = _results(os.path.join(d, "yes"), dv)
        env = lf.read_results(os.path.join(d, "yes"), dem, st, hy)
        assert env.dv is not None and env.stats["dv_written"] is True
        # velocity_min_depth_m is 0.03: the 0.01 m cell is masked, the 0.5 m cell is kept
        assert float(env.dv.data[0, 1]) == 0.0
        assert abs(float(env.dv.data[0, 2]) - 0.15) < 1e-5
    finally:
        shutil.rmtree(d, True)


@test
def runner_writes_the_peak_dv_raster_only_when_asked_and_not_in_the_key():
    dem = Grid(np.zeros((2, 3)), 0.0, 20.0, 10.0)
    dv = dem.like(np.array([[1.0, 0.0, 2.0], [0.5, 1.5, 0.0]]), nodata=np.nan)
    env = lf.Envelopes(dem, dem, dem, dem, dv=dv)
    wet = np.array([[True, False, True], [True, True, False]])
    d = tempfile.mkdtemp(prefix="boann_dv_ras_")
    try:
        st = workflow.RunSettings(write_peak_dv=True)
        path = workflow.write_peak_dv_raster(dem, env, st, d, npy_writer, wet, False)
        assert os.path.basename(path) == workflow.PEAK_DV_NAME
        got = np.load(os.path.splitext(path)[0] + ".npy")
        scale = 1.0 / (FT * FT)
        assert abs(float(got[0, 0]) - scale) < 1e-4
        assert abs(float(got[0, 2]) - 2.0 * scale) < 1e-4
        assert np.isnan(got[0, 1]) and np.isnan(got[1, 2])
        # off: a rerun removes the copy
        st.write_peak_dv = False
        assert workflow.write_peak_dv_raster(dem, env, st, d, npy_writer, wet, False) is None
        assert not os.path.exists(path)
        # asked, but this run has no grid: nothing written, and the log says so
        st.write_peak_dv = True
        env.dv = None
        notes = []
        open(path, "w").write("stale\n")
        assert workflow.write_peak_dv_raster(dem, env, st, d, npy_writer, wet, False,
                                             log=notes.append) is None
        assert notes and "no .maxDV" in notes[0]
        assert not os.path.exists(path)
        # a near-dam refine keeps only depth, velocity, WSE and arrival
        env.dv = dv
        assert workflow.write_peak_dv_raster(dem, env, st, d, npy_writer, wet, True) is None
    finally:
        shutil.rmtree(d, True)
    plain = workflow.key_settings(workflow.RunSettings())
    asked = workflow.key_settings(workflow.RunSettings(write_peak_dv=True))
    assert "write_peak_dv" not in plain and "write_peak_dv" not in asked
    assert plain == asked
    assert "write_peak_dv" not in workflow.RunSettings().as_dict()
    assert workflow.RunSettings(write_peak_dv=True).as_dict()["write_peak_dv"] is True
    import importlib
    ncli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    assert ncli.MODEL_KEYS["write_peak_dv"] == ("write_peak_dv", ("bool",))
    opts = type("O", (), {"lisflood": sys.executable})()
    st = ncli.build_settings(ncli.Config({"model": {"write_peak_dv": True}}, ""), opts,
                             need_models=False)
    assert st.write_peak_dv is True
    plain_st = ncli.build_settings(ncli.Config({"model": {}}, ""), opts, need_models=False)
    assert plain_st.write_peak_dv is False
    assert "write_peak_dv" not in workflow.key_settings(plain_st)


@test
def loading_results_names_the_peak_dv_layer():
    """WRITE_PEAK_DV puts "dv" in res.final. Loading that set of rasters must not raise,
    and the layer is named D x V (Max)."""
    from nd_breach_runner.tests import fake_qgis
    fake_qgis.install()
    from nd_breach_runner import algorithms as A
    d = tempfile.mkdtemp(prefix="boann_dv_load_")
    try:
        final = {}
        for key, leaf in (("depth", "Depth (Max).tif"), ("velocity", "Velocity (Max).tif"),
                          ("arrival", "Arrival Time.tif"), ("wse", "WSE (Max).tif"),
                          ("dv", workflow.PEAK_DV_NAME)):
            path = os.path.join(d, leaf)
            with open(path, "w", encoding="utf-8") as f:
                f.write("x\n")
            final[key] = path
        loaded = []
        alg = A.RunDamAlgorithm()
        alg.load = lambda ctx, path, name: loaded.append(name)
        res = types.SimpleNamespace(final=final, increment={}, vectors={})
        alg.load_results(None, res, "DWR-1")
    finally:
        shutil.rmtree(d, True)
    assert "DWR-1 D x V (Max)" in loaded
    assert "DWR-1 Depth (Max)" in loaded
    assert "DWR-1 Velocity (Max)" in loaded
    assert "DWR-1 Arrival Time" in loaded
    assert "DWR-1 WSE (Max)" in loaded


@test
def hecras_stage_drops_the_boann_peak_dv_raster():
    """After HEC-RAS replaces the finals, no Boann D x V file of any name is left in the dam
    folder: not the raster itself, and not a "LISFLOOD-FP" copy of it (run_stage keeps such a
    copy of every raster it is handed, and Hazard would read it as the dam's D x V). A stage
    that fails leaves the Boann finals, D x V included, as they were."""
    from nd_breach_runner.core import hecras_engine
    d = tempfile.mkdtemp(prefix="boann_dv_hr_")
    try:
        dv = os.path.join(d, workflow.PEAK_DV_NAME)
        with open(dv, "w", encoding="utf-8") as f:
            f.write("boann\n")
        depth = os.path.join(d, "Depth (Max).tif")
        with open(depth, "w", encoding="utf-8") as f:
            f.write("boann-depth\n")
        hr_depth = os.path.join(d, "hr_depth.tif")
        with open(hr_depth, "w", encoding="utf-8") as f:
            f.write("hr\n")

        def new_res():
            return types.SimpleNamespace(
                final={"depth": depth, "velocity": depth, "arrival": depth, "wse": depth,
                       "dv": dv},
                warnings=[], summary={})

        class Eng:
            fail = False
            given = None

            class HecRasEngineError(Exception):
                pass

            @classmethod
            def run_stage(cls, _folder, final_paths, *_a, **_k):
                cls.given = dict(final_paths)
                # what the real run_stage does first: keep a prefixed copy of every raster
                for p in final_paths.values():
                    if p and os.path.exists(p):
                        shutil.copy2(p, os.path.join(os.path.dirname(p),
                                                     hecras_engine.LISFLOOD_PREFIX
                                                     + os.path.basename(p)))
                if cls.fail:
                    raise cls.HecRasEngineError("no Wine here")
                return {"final": {"depth": hr_depth, "velocity": hr_depth,
                                  "arrival": hr_depth, "wse": hr_depth},
                        "warnings": []}

        notes = []
        old = workflow.adopt_final_engine
        workflow.adopt_final_engine = lambda *_a, **_k: {"engine": "HEC-RAS 2D"}
        try:
            # a failed stage: the Boann finals stay whole, and still no D x V copy is made
            Eng.fail = True
            res = new_res()
            try:
                workflow.run_hecras_stage(d, res, None, workflow.RunSettings(write_peak_dv=True),
                                          (0.0, 0.0), notes.append, Eng)
                raise AssertionError("a failed HEC-RAS stage must raise")
            except workflow.HecRasStageError:
                pass
            assert "dv" not in Eng.given, Eng.given
            assert res.final.get("dv") == dv and os.path.exists(dv)
            assert not os.path.exists(os.path.join(d, hecras_engine.LISFLOOD_PREFIX
                                                   + workflow.PEAK_DV_NAME))
            # the stage succeeds; the run manifest listed the D x V among the outputs
            from nd_breach_runner.core import provenance
            Eng.fail = False
            res = new_res()
            man = os.path.join(d, provenance.MANIFEST_NAME)
            provenance.write_manifest(man, provenance.build_manifest(
                "test", {}, {}, None, None, {}, outputs=dict(res.final)))
            assert "dv" in json.load(open(man, encoding="utf-8"))["outputs"]
            res.manifest = man
            workflow.run_hecras_stage(d, res, None, workflow.RunSettings(write_peak_dv=True),
                                      (0.0, 0.0), notes.append, Eng)
            chk = provenance.verify_manifest(man)
            assert not any("'dv'" in p for p in chk["problems"]), chk["problems"]
            assert not any("edited after" in p for p in chk["problems"]), chk["problems"]
            # a D x V that cannot be deleted (a locked file) is a warning, not just a log line
            with open(dv, "w", encoding="utf-8") as f:
                f.write("boann\n")
            res = new_res()
            real_remove = workflow.os.remove

            def locked(path, *a, **k):
                if os.path.basename(path) == workflow.PEAK_DV_NAME:
                    raise PermissionError("in use")
                return real_remove(path, *a, **k)

            workflow.os.remove = locked
            try:
                workflow.run_hecras_stage(d, res, None, workflow.RunSettings(write_peak_dv=True),
                                          (0.0, 0.0), notes.append, Eng)
            finally:
                workflow.os.remove = real_remove
            assert "dv" not in res.final
            assert any("could not be removed" in w for w in res.warnings), res.warnings
            real_remove(dv)
        finally:
            workflow.adopt_final_engine = old
        assert "dv" not in Eng.given, Eng.given
        assert "dv" not in res.final
        assert res.final["depth"] == hr_depth
        left = [n for n in os.listdir(d) if "d x v" in n.lower() or "dxv" in n.lower()]
        assert not left, left
        assert any("peak depth x velocity raster was removed" in n for n in notes), notes
        assert os.path.exists(os.path.join(d, hecras_engine.LISFLOOD_PREFIX + "Depth (Max).tif"))
    finally:
        shutil.rmtree(d, True)


@test
def rerun_clears_a_kept_lisflood_dv_copy():
    """A rerun into the same folder clears both the D x V raster and a "LISFLOOD-FP" copy left
    by an older HEC-RAS stage, so Hazard cannot pick up a stale Boann D x V. Checked on the
    parsed loop (a comment or a dead string cannot satisfy it)."""
    import ast
    tree = ast.parse(open(workflow.__file__, encoding="utf-8").read())
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)
             and isinstance(n.target, ast.Name) and n.target.id == "stale"
             and any(isinstance(c, ast.Name) and c.id == "STALE_ON_RERUN"
                     for c in ast.walk(n.iter))]
    assert loops, "the rerun cleanup loop is gone"
    names = set()
    for loop in loops:
        for c in ast.walk(loop.iter):
            if isinstance(c, ast.Name) and c.id == "PEAK_DV_NAME":
                names.add("plain")
            if (isinstance(c, ast.BinOp) and isinstance(c.op, ast.Add)
                    and isinstance(c.left, ast.Constant) and c.left.value == "LISFLOOD-FP "
                    and isinstance(c.right, ast.Name) and c.right.id == "PEAK_DV_NAME"):
                names.add("prefixed")
        removes = [c for c in ast.walk(loop) if isinstance(c, ast.Call)
                   and getattr(c.func, "attr", None) == "remove"]
        assert removes, "the cleanup loop no longer removes anything"
    assert names == {"plain", "prefixed"}, names


if __name__ == "__main__":
    for fn in (runner_reads_peak_dv_only_when_the_file_is_there,
               runner_writes_the_peak_dv_raster_only_when_asked_and_not_in_the_key,
               loading_results_names_the_peak_dv_layer,
               hecras_stage_drops_the_boann_peak_dv_raster,
               rerun_clears_a_kept_lisflood_dv_copy,
               boann_peak_dv_is_the_concurrent_value_on_every_solver,
               boann_peak_dv_matches_ritter_at_the_dam,
               boann_peak_dv_leaves_existing_results_identical_to_main,
               boann_peak_dv_gpu_matches_itself_and_the_cpu_build):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
