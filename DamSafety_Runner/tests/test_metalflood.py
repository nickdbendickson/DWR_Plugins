"""The GPU flood engine (core/metalflood): its kernels, run through the CPU build of the same
kernel source, against the numpy built-in engine (same scheme with the 1-D friction term:
the same steps, depths within a few mm), conservation of water, the wet window (no effect on
the answer), the .par options it reads, and, where this computer has an Apple GPU, Metal
against the CPU build. With NDBREACH_LISFLOOD set (--models) it is also checked against
LISFLOOD-FP itself.

The diagnostic cases in cases.py (level-pool lake, nested basin, breach-inflow cell) are
exercised by metalflood_diagnostic_cases_run (they run and conserve mass) and checked against
the serial numpy oracle (builtin_lisflood, 1-D friction) by
metalflood_diagnostic_cases_match_serial_oracle. Metal vs that CPU build is in
metalflood_gpu_matches_cpu_build, which skips where there is no Apple GPU."""
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

from nd_breach_runner.core import builtin_lisflood as bl
from nd_breach_runner.core import metalflood as mf
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood.cases import (
    read_asc, valley_deck, level_pool_lake, nested_basin, breach_inflow_cell,
)
from nd_breach_runner.tests.registry import Skip, model_test, test


def _need_cpu_build():
    if mfb.prebuilt_available():
        return                                   # a release's shipped CPU build (any OS)
    if os.name == "nt":
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


def _tmp():
    d = tempfile.mkdtemp(prefix="mf_test_")
    return d


def _res(folder, ext):
    return read_asc(os.path.join(folder, "results", "run." + ext))


@test
def metalflood_cpu_build_matches_builtin_engine():
    """With LISFLOOD-FP's 1-D friction term (the built-in engine's), the kernels take the same
    time steps and give the same flood to float32 precision. (Where the two engines differ on
    purpose they follow LISFLOOD-FP: maxima and arrival only for water over 1 mm, inflow taken
    at the start of a step, the velocity LISFLOOD-FP records; so no start-depth pond here and
    velocity is not compared.)"""
    _need_cpu_build()
    d = _tmp()
    try:
        a = valley_deck(os.path.join(d, "a"), pond=False, extra=["1Dfriction"])
        b = valley_deck(os.path.join(d, "b"), pond=False, extra=["1Dfriction"])
        sa = bl.run_par(a)
        sb = mf.run_par(b, backend="cpu")
        assert sb["backend"] == "cpu"
        # the built-in engine lands a step on every .mass time; this engine (as LISFLOOD-FP)
        # steps past them
        assert abs(sa["steps"] - sb["steps"]) <= sa["steps"] // 50, (sa["steps"], sb["steps"])
        ha = _res(os.path.join(d, "a"), "max")
        hb = _res(os.path.join(d, "b"), "max")
        assert np.array_equal(ha == bl.NODATA, hb == bl.NODATA)
        assert np.abs(ha - hb).max() <= 0.002, np.abs(ha - hb).max()
        real = hb > 0.002                     # cells with more than a film of water
        for ext, tol in (("mxe", 0.002), ("inittm", 0.05), ("maxtm", 0.01)):
            x = _res(os.path.join(d, "a"), ext)[real]
            y = _res(os.path.join(d, "b"), ext)[real]
            assert (x != bl.NODATA).all() and (y != bl.NODATA).all(), ext
            assert np.percentile(np.abs(x - y), 99) <= tol, (ext, np.abs(x - y).max())
        for ext in ("mass", "discharge"):
            ta = np.loadtxt(os.path.join(d, "a", "results", "run." + ext), skiprows=1)
            tb = np.loadtxt(os.path.join(d, "b", "results", "run." + ext), skiprows=1)
            assert ta.shape == tb.shape, ext
            assert np.abs(ta[:, 0] - tb[:, 0]).max() <= 10.0, ext   # within a step
        rel = abs(sa["injected_m3"] - sb["injected_m3"]) / sa["injected_m3"]
        assert rel < 1e-3, rel
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_conserves_water():
    _need_cpu_build()
    d = _tmp()
    try:
        s = mf.run_par(valley_deck(d), backend="cpu")
        assert s["injected_m3"] > 1e6
        assert abs(s["mass_error_m3"]) < 1e-6 * s["injected_m3"], s
        assert s["clipped_m3"] < 1e-6 * s["injected_m3"], s
        assert s["outflow_m3"] > 0.5 * s["injected_m3"], s         # the FREE edges work
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_wet_window_does_not_change_the_answer():
    """Kernels run only on the wet box + margin; a window as big as the grid must give the
    same numbers bit for bit."""
    _need_cpu_build()
    d = _tmp()
    old = mfe.WINDOW_MARGIN
    try:
        a = valley_deck(os.path.join(d, "a"), ny=50, nx=160, sim_s=2 * 3600.0)
        b = valley_deck(os.path.join(d, "b"), ny=50, nx=160, sim_s=2 * 3600.0)
        mf.run_par(a, backend="cpu")
        mfe.WINDOW_MARGIN = 10 ** 6
        mf.run_par(b, backend="cpu")
        for ext in ("max", "maxVc", "inittm", "maxtm"):
            x = _res(os.path.join(d, "a"), ext)
            y = _res(os.path.join(d, "b"), ext)
            assert np.array_equal(x, y), ext
    finally:
        mfe.WINDOW_MARGIN = old
        shutil.rmtree(d, True)


@test
def metalflood_reads_lisflood_acc_options():
    """theta, cfl and 1Dfriction are LISFLOOD-FP 8.3's ACC keywords; the Runner's damped rerun
    writes theta 0.7 and cfl 0.5. The built-in engine ignores them; this engine must not."""
    _need_cpu_build()
    d = _tmp()
    try:
        deck = mfe.Deck(valley_deck(d, extra=["theta 0.7", "cfl 0.5"]))
        assert (deck.theta, deck.cfl, deck.fric2d) == (0.7, 0.5, True)
        deck = mfe.Deck(valley_deck(d, extra=["1Dfriction"]))
        assert (deck.theta, deck.cfl, deck.fric2d) == (1.0, mfe.ALPHA, False)
        bad = valley_deck(d, extra=["theta 1.5"])
        try:
            mfe.Deck(bad)
            assert False, "theta 1.5 accepted"
        except mf.EngineError:
            pass
        s1 = mf.run_par(valley_deck(os.path.join(d, "a"), sim_s=1800.0), backend="cpu")
        s2 = mf.run_par(valley_deck(os.path.join(d, "b"), sim_s=1800.0, extra=["cfl 0.5"]),
                        backend="cpu")
        assert s2["steps"] > 1.3 * s1["steps"], (s1["steps"], s2["steps"])
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_params_layout_matches_kernels():
    _need_cpu_build()
    import ctypes
    lib = ctypes.CDLL(mfb.build_cpu_library())
    assert lib.mf_params_size() == ctypes.sizeof(mfb.Params) == 96
    names = [n for n, _, _ in mfb.buffer_layout(3, 4, 1, 1, 2)]
    with open(mfb.KERNEL_SOURCE, encoding="utf-8") as f:
        src = f.read()
    for k, n in enumerate(names):
        assert "{} B({})".format(n, k) in src, (n, k)
    assert "constant Params& p B({})".format(len(names)) in src


@test
def metalflood_gpu_matches_cpu_build():
    """Runs only on a Mac with a Metal GPU (the macOS CI job, Nick's Mac)."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    d = _tmp()
    try:
        a = valley_deck(os.path.join(d, "a"))
        b = valley_deck(os.path.join(d, "b"))
        sa = mf.run_par(a, backend="cpu")
        sb = mf.run_par(b, backend="metal")
        assert sb["backend"] == "metal"
        assert abs(sa["steps"] - sb["steps"]) <= max(3, sa["steps"] // 200), (sa, sb)
        for ext, tol in (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01)):
            x = _res(os.path.join(d, "a"), ext)
            y = _res(os.path.join(d, "b"), ext)
            m = (x != bl.NODATA) & (y != bl.NODATA)
            assert np.abs(x - y)[m].max() <= tol, (ext, np.abs(x - y)[m].max())
        assert abs(sb["mass_error_m3"]) < 1e-5 * sb["injected_m3"], sb
        # the same comparison on the diagnostic cases (still pool, short breach, short nest).
        # These are the Mac half of the reference harness; cloud CI only has the CPU side.
        diag = (
            ("still", lambda f: level_pool_lake(
                f, n=20, sim_s=200.0, free="", inflow_cms=0.0, h0=1.25)),
            ("breach", lambda f: breach_inflow_cell(f, n=15, sim_s=300.0)),
            ("nested", lambda f: nested_basin(f, ny=16, nx=32, sim_s=400.0)),
        )
        for name, maker in diag:
            ca = maker(os.path.join(d, name + "c"))
            cb = maker(os.path.join(d, name + "m"))
            sc = mf.run_par(ca, backend="cpu")
            sm = mf.run_par(cb, backend="metal")
            assert sm["backend"] == "metal", name
            x = _res(os.path.join(d, name + "c"), "max")
            y = _res(os.path.join(d, name + "m"), "max")
            m = (x != bl.NODATA) & (y != bl.NODATA)
            assert np.abs(x - y)[m].max() <= 0.01, (name, np.abs(x - y)[m].max())
            assert abs(sc["final_volume_m3"] - sm["final_volume_m3"]) <= \
                0.01 * max(sc["final_volume_m3"], 1.0), name
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_deckio_reads_as_builtin_engine():
    """deckio.py is a copy of builtin_lisflood's readers (so the package stands alone for
    Screening and DamIT): both read a deck the same."""
    from nd_breach_runner.core.metalflood import deckio
    d = _tmp()
    try:
        par = valley_deck(d, nfile=True, gauge=True)
        pa, pb = bl.read_par(par), deckio.read_par(par)
        assert pa == pb
        folder = os.path.dirname(par)
        for key, ra, rb in (("demfile", bl._read_asc, deckio._read_asc),
                            ("manningfile", bl._read_asc, deckio._read_asc),
                            ("bcifile", bl.read_bci, deckio.read_bci),
                            ("bdyfile", bl.read_bdy, deckio.read_bdy),
                            ("gaugefile", bl.read_gauges, deckio.read_gauges)):
            a, b = ra(os.path.join(folder, pa[key])), rb(os.path.join(folder, pa[key]))
            assert repr(a) == repr(b), key
        for k in ("G", "ALPHA", "DRY_M", "MAX_HFLOW", "VEL_MIN_DEPTH_M", "NODATA"):
            assert getattr(bl, k) == getattr(deckio, k), k
    finally:
        shutil.rmtree(d, True)


@test
def gpu_engine_is_a_runner_engine_choice():
    """Runner 3.29.0: the word 'gpu' (or 'metal') and the FLOOD_ENGINE choice select the GPU
    engine; it runs in-process, is labelled as not LISFLOOD-FP everywhere, and passes the
    Runner's own engine check (arrival-depth keyword honoured, velocity written)."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.damsuite import record
    for w in ("gpu", "GPU", " metal ", "metalflood"):
        assert lf.resolve_exe(w) == lf.GPU_EXE, w
    assert os.path.isfile(lf.GPU_EXE)
    assert lf.is_gpu(lf.GPU_EXE) and lf.is_inprocess(lf.GPU_EXE) and not lf.is_builtin(lf.GPU_EXE)
    assert not lf.is_gpu(lf.BUILTIN_EXE) and lf.resolve_exe("builtin") == lf.BUILTIN_EXE
    assert "NOT LISFLOOD-FP" in lf.engine_label(lf.GPU_EXE)
    assert "NOT LISFLOOD-FP" in lf.describe_exe(lf.GPU_EXE)
    assert lf.engine_name(lf.GPU_EXE) == "Boann"
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_EXE}}})
    assert blk["model"].startswith("Boann") and "shock" not in blk["model"], blk
    blk = record._engine_block(None, {"software": {"lisflood_exe": {
        "path": "C:\\x\\core\\metalflood\\engine.py"}}})
    assert blk["model"].startswith("Boann") and "shock" not in blk["model"], blk
    try:                                                    # the Processing layer needs QGIS
        from nd_breach_runner import algorithms as algs     # (absent on the Metal-only CI)
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[0] == "lisflood" and algs.FLOOD_ENGINES[1] == "gpu"
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)
    _need_cpu_build()
    d = _tmp()
    try:
        r = lf.check_lisflood(lf.GPU_EXE, os.path.join(d, "chk"))
        assert r["ok"] and r["patched"] and r["velocity"], r
        assert "Boann" in r["message"], r
        with open(os.path.join(d, "chk", "lisflood.log")) as f:
            assert f.readline().startswith(mf.NAME)
    finally:
        shutil.rmtree(d, True)


@model_test
def metalflood_matches_lisflood_fp():
    """The same deck through LISFLOOD-FP (NDBREACH_LISFLOOD) and this engine."""
    exe = os.environ.get("NDBREACH_LISFLOOD")
    if not exe or not os.path.isfile(exe):
        raise Skip("NDBREACH_LISFLOOD is not set")
    _need_cpu_build()
    d = _tmp()
    try:
        extra = ["hazard", "inittm_depth 0.003"]
        a = valley_deck(os.path.join(d, "lf"), hole=False, extra=extra)
        b = valley_deck(os.path.join(d, "mf"), hole=False, extra=extra)
        subprocess.run([exe, os.path.basename(a)], cwd=os.path.dirname(a),
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True,
                       env=dict(os.environ, OMP_NUM_THREADS="1"))
        mf.run_par(b, backend="cpu")
        x = _res(os.path.join(d, "lf"), "max")
        y = _res(os.path.join(d, "mf"), "max")
        wet = (x > 0.003) | (y > 0.003)
        assert np.abs(x - y)[wet].mean() < 0.03, np.abs(x - y)[wet].mean()
        a1, b1 = x > 0.1, y > 0.1
        assert (a1 & b1).sum() / (a1 | b1).sum() > 0.98
        vx = _res(os.path.join(d, "lf"), "maxVc")
        vy = _res(os.path.join(d, "mf"), "maxVc")
        assert abs(vx[wet].max() - vy[wet].max()) < 0.1 * vx[wet].max()
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_diagnostic_cases_match_serial_oracle():
    """CPU build of the kernels against the serial numpy oracle (builtin_lisflood), which is
    the same local-inertial scheme in float64 with 1-D friction. This is the cloud pass/fail
    the later number-moving steps (subgrid channels, a near-field FV window) have to point at.

    Still water must match to the millimetre and stay flat. The breach-inflow cell, with the
    point source added in LISFLOOD-FP's order (the Runner 3.30.0 default: the DryCheck limiter
    runs before the source), tracks the oracle to ~2 cm on this coarse 21x21 grid, where the
    limiter's per-step inflow quantisation separates the float32 kernels from the float64 oracle
    at the few cells beside the breach; the proper-grid fidelity is the valley at 0.002
    (metalflood_cpu_build_matches_builtin_engine). The nested basin must keep its water in the
    inner pool (the rim is not an invented wall and not an early spill) and agree on volume.
    """
    _need_cpu_build()
    d = _tmp()
    try:
        def pair(name, maker):
            a = maker(os.path.join(d, name + "o"))
            b = maker(os.path.join(d, name + "c"))
            so = bl.run_par(a)
            sc = mf.run_par(b, backend="cpu")
            ho = _res(os.path.join(d, name + "o"), "max")
            hc = _res(os.path.join(d, name + "c"), "max")
            return so, sc, ho, hc

        one_d = ["1Dfriction"]
        so, sc, ho, hc = pair("still", lambda f: level_pool_lake(
            f, n=24, sim_s=300.0, free="", inflow_cms=0.0, h0=1.25, extra=one_d))
        assert np.abs(ho - hc).max() <= 0.001, np.abs(ho - hc).max()
        assert float(hc.max() - hc.min()) <= 0.001, (hc.min(), hc.max())
        assert abs(sc["mass_error_m3"]) < 1e-4, sc
        # a second CPU run is the same grid: the reference is deterministic
        again = level_pool_lake(os.path.join(d, "still2"), n=24, sim_s=300.0, free="",
                                inflow_cms=0.0, h0=1.25, extra=one_d)
        mf.run_par(again, backend="cpu")
        assert np.array_equal(hc, _res(os.path.join(d, "still2"), "max"))

        _so, _sc, ho, hc = pair("breach", lambda f: breach_inflow_cell(
            f, n=21, sim_s=600.0, extra=one_d))
        # Runner 3.30.0 adds the point source in LISFLOOD-FP's order by default (DryCheck before
        # the source): on this coarse 21x21 grid the limiter's per-step inflow quantisation
        # separates the f64 oracle from the f32 kernel at the few cells beside the breach, up to
        # ~1 cm on a ~0.6 m depth. Proper-grid fidelity is the valley at 0.002; bound the coarse
        # breach cluster at 2 cm here.
        assert np.abs(ho - hc).max() <= 0.02, np.abs(ho - hc).max()

        so, sc, ho, hc = pair("nested", lambda f: nested_basin(f, extra=one_d))
        # outer basin is columns [17, 31); water has not reached the outer rim
        outer = slice(17, 31)
        assert (ho[1:-1, outer] <= 0.05).all(), ho[1:-1, outer].max()
        assert (hc[1:-1, outer] <= 0.05).all(), hc[1:-1, outer].max()
        assert (ho[1:-1, 6:16] > 0.05).all() and (hc[1:-1, 6:16] > 0.05).all()
        rel = abs(so["final_volume_m3"] - sc["final_volume_m3"]) / so["final_volume_m3"]
        assert rel < 0.005, (rel, so["final_volume_m3"], sc["final_volume_m3"])
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_diagnostic_cases_run():
    """Reference harness for the diagnostic cases added in the Boann plan.
    CPU-build only. Checks that each case runs, conserves mass to a tight tolerance,
    and produces finite max-depth results. These are the baselines later number-moving
    steps must not regress."""
    _need_cpu_build()
    d = _tmp()
    try:
        cases = [
            ("lake", level_pool_lake),
            ("nested", nested_basin),
            ("breach", breach_inflow_cell),
        ]
        for name, maker in cases:
            folder = os.path.join(d, name)
            par = maker(folder)
            s = mf.run_par(par, backend="cpu")
            assert s["backend"] == "cpu", name
            assert s["injected_m3"] > 0, name
            assert abs(s["mass_error_m3"]) < 1e-5 * s["injected_m3"], (name, s)
            hmax = _res(folder, "max")
            assert np.isfinite(hmax).all() or (hmax == bl.NODATA).any(), name
            # at least some wet cells for the cases that have inflow
            assert (hmax > 0.001).sum() > 0, name
    finally:
        shutil.rmtree(d, True)


from nd_breach_runner.core.metalflood import engine_fv as fve  # noqa: E402
from nd_breach_runner.core.metalflood.cases import write_asc as _write_asc  # noqa: E402

_G = bl.G


def _fv_deck(folder, z, h0, dx=5.0, sim=100.0, n=0.0, free=""):
    """A minimal start-depth deck for the FV engine (no inflow): bed z, start depth h0."""
    os.makedirs(folder, exist_ok=True)
    xll, yll = 500000.0, 5200000.0
    _write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    _write_asc(os.path.join(folder, "run.start"), h0, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results", "startfile run.start",
             "sim_time {:.1f}".format(sim), "initial_tstep 1.0", "massint {:.1f}".format(sim),
             "saveint {:.1f}".format(sim + 10.0), "fpfric {}".format(n)]
    if free:
        ny, nx = z.shape
        with open(os.path.join(folder, "run.bci"), "w") as f:
            for e in free:
                lo, hi = (xll, xll + nx * dx) if e in "NS" else (yll, yll + ny * dx)
                f.write("{} {:.3f} {:.3f} FREE\n".format(e, lo, hi))
        lines.append("bcifile run.bci")
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.join(folder, "run.par")


@test
def metalflood_clock_and_clip_total_survive_a_clipping_batch():
    """The run's clock must be the time the engine really simulated, and clipped_m3 the sum of
    every batch's clip. Both once broke on the same reused name: `clip` was the end-cut flag AND
    the clipped-volume total, so a batch that clipped any water (a) threw away the time its last
    step ran past the output time (the clock fell 112 s behind in 6 h of the real Lake Delhi
    ACC run, so inflow and arrival times were read off a slow clock) and (b) reset the total, so
    only the last batch's clip was reported. Every batch here reports a small clip; the clock and
    the total must still add up, for ACC and for the shock-capturing (FV) engines."""
    _need_cpu_build()
    for mod, extra in ((mfe, ["acceleration"]), (fve, [])):
        d = _tmp()
        seen = {"t": 0.0, "clip": 0.0, "n": 0}
        orig = mod.Engine.batch

        def batch(self, t, t_event, nsteps, clip, _o=orig):
            out = dict(_o(self, t, t_event, nsteps, clip))
            if out["steps"]:
                out["clip"] = out["clip"] + 0.25          # as if the depth >= 0 clip had acted
            seen["t"] += out["t_local"]
            seen["clip"] += out["clip"] if out["steps"] else 0.0
            seen["n"] += 1
            return out
        mod.Engine.batch = batch
        try:
            z = np.tile(10.0 - 0.01 * (np.arange(40) + 0.5) * 5.0, (6, 1))
            h0 = np.zeros_like(z); h0[:, :8] = 1.0
            p = _fv_deck(os.path.join(d, "c"), z, h0, dx=5.0, sim=120.0, n=0.03, free="E")
            txt = open(p).read().replace("massint 120.0", "massint 7.0")   # many output times
            with open(p, "w") as f:
                f.write(txt + "".join(x + "\n" for x in extra))
            st = mod.run_par(p, backend="cpu")
        finally:
            mod.Engine.batch = orig
            shutil.rmtree(d, True)
        assert seen["n"] > 10, seen
        assert abs(st["sim_time_s"] - seen["t"]) < 1e-6, (
            "{}: the clock says {:.4f} s but the engine simulated {:.4f} s".format(
                mod.__name__, st["sim_time_s"], seen["t"]))
        assert abs(st["clipped_m3"] - seen["clip"]) < 1e-6, (
            "{}: clipped_m3 {:.4f} but the batches clipped {:.4f}".format(
                mod.__name__, st["clipped_m3"], seen["clip"]))


@test
def metalflood_fv_lake_at_rest_is_well_balanced():
    """The shock-capturing FV engine holds a lake at rest over a bumpy bed to float32 round-off:
    no spurious velocity (maxVc ~ 0) and the still-water depth is unchanged (max == initial)."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx = 20, 20
        rng = np.random.default_rng(0)
        z = rng.uniform(0.0, 2.0, size=(ny, nx))
        h0 = np.maximum(3.0 - z, 0.0)                 # flat surface eta = 3
        p = _fv_deck(os.path.join(d, "lake"), z, h0, dx=5.0, sim=200.0, n=0.0)   # closed walls
        s = fve.run_par(p, backend="cpu")
        assert s["scheme"] == "fv" and s["backend"] == "cpu"
        vc = _res(os.path.join(d, "lake"), "maxVc")
        hm = _res(os.path.join(d, "lake"), "max")
        vmax = vc[vc != bl.NODATA].max()
        drift = np.abs(hm[h0 > 1e-6] - h0[h0 > 1e-6]).max()
        assert vmax < 1e-3, "spurious speed {:.2e} m/s".format(vmax)
        assert drift < 1e-3, "depth drift {:.2e} m".format(drift)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv_reproduces_the_ritter_dam_break():
    """A dry-bed dam break: the FV engine's depth at the dam approaches Ritter's 4h0/9, the fan
    matches the analytical rarefaction, and the front advances (a first-order scheme sits a few
    cells behind the exact h=0 front, never ahead of it)."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx, h0, dam = 1, 801, 1.0, 1.0, 400
        z = np.zeros((ny, nx))
        hinit = np.zeros((ny, nx)); hinit[0, :dam] = h0
        p = _fv_deck(os.path.join(d, "ritter"), z, hinit, dx=dx, sim=8.0, n=0.0)
        deck = fve.Deck(p)
        bk = mfb.CpuBackend(mfb.FV_SPEC)
        eng = fve.Engine(deck, bk)
        t, T = 0.0, 8.0
        while t < T - 1e-6:
            out = eng.batch(t, T, fve.BATCH_STEPS, clip=True)
            if out["steps"] == 0:
                break
            t += out["t_local"]
        h = eng.h2().astype(np.float64)[0].copy()
        bk.close()
        c0 = math.sqrt(_G * h0)
        xc = (np.arange(nx) + 0.5 - dam) * dx

        def ritter_h(x):
            xi = x / T
            if xi <= -c0:
                return h0
            if xi >= 2 * c0:
                return 0.0
            return (2 * c0 - xi) ** 2 / (9 * _G)
        ha = np.array([ritter_h(x) for x in xc])
        # depth at the dam -> 4 h0 / 9
        assert abs(h[dam] - 4 * h0 / 9) < 0.02, "h@dam {:.4f}".format(h[dam])
        # fan, where the analytical depth is well above the near-front sliver
        inner = ha > 0.05
        assert np.abs(h[inner] - ha[inner]).mean() < 0.02, np.abs(h[inner] - ha[inner]).mean()
        # the front: behind the exact h=0 front, not grossly stalled, never ahead
        front = xc[h > 1e-3].max()
        assert 0.70 * (2 * c0 * T) < front <= 2 * c0 * T + dx, "front {:.1f} m".format(front)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv_conserves_water():
    """The FV engine keeps mass: a closed valley filled by an inflow ends with a volume error far
    under a tenth of a percent of what was injected."""
    _need_cpu_build()
    d = _tmp()
    try:
        a = valley_deck(os.path.join(d, "v"), free="", hole=False, pond=False, gauge=False)
        s = fve.run_par(a, backend="cpu")
        assert s["scheme"] == "fv"
        assert abs(s["mass_error_m3"]) < 1e-3 * max(s["injected_m3"], 1.0), s["mass_error_m3"]
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv_free_edge_drains_a_sloping_basin():
    """A basin pre-filled with water sloping down to a FREE east edge drains off the boundary (the
    transmissive outflow with its Manning normal-depth floor) and keeps mass, for every HLLC order
    (FV1/FV2/DG2). Exercises the FREE-edge path the lake/Ritter/closed-valley cases never reach."""
    _need_cpu_build()
    for order in (1, 2, 3):
        d = _tmp()
        try:
            ny, nx, dx = 16, 40, 10.0
            z = (40.0 - 0.02 * np.arange(nx))[None, :] * np.ones((ny, 1))   # bed tilts down to the east
            hi = z[:, 0].max() + 3.0
            lo = z[:, -1].max() + 0.5
            surf = lo + (hi - lo) * np.linspace(1.0, 0.0, nx)              # water surface sloping east
            h0 = np.maximum(surf[None, :] - z, 0.0) * np.ones((ny, 1))
            vol0 = float(h0.sum()) * dx * dx
            a = _fv_deck(os.path.join(d, "fe"), z, h0, dx=dx, sim=1800.0, n=0.035, free="E")
            s = fve.run_par(a, backend="cpu", order=order)
            assert s["outflow_m3"] > 0.3 * vol0, \
                "order {}: little drained off the FREE edge: {:.3g} of {:.3g}".format(
                    order, s["outflow_m3"], vol0)
            # the 2nd-order engines book the edge outflow over both RK2 stages (it once took only the
            # corrector's, mis-booking up to ~1 % of the basin), so every order balances to round-off
            assert abs(s["mass_error_m3"]) < 1e-4 * max(vol0, 1.0), (order, s["mass_error_m3"])
        finally:
            shutil.rmtree(d, True)


@test
def metalflood_fv_free_edge_lets_no_water_in():
    """An open (FREE) edge lets water OUT, never IN. A mound of water against the open east edge
    of a flat basin spreads both ways, so the edge cell's velocity turns inward. The transmissive
    edge once copied that inward velocity to its outside ghost and so drew water in from outside
    the model, uncounted: here twice the start volume appeared from nowhere, and on the real Lake
    Delhi gate deck (one column wider than the deck the baseline was made on) the sub-grid FV run
    made 3.5 % of its inflow this way. Every HLLC order must now balance to round-off, with less
    water leaving than was there."""
    _need_cpu_build()
    for order in (1, 2, 3):
        d = _tmp()
        try:
            ny, nx, dx = 8, 24, 5.0
            z = np.zeros((ny, nx))
            h0 = np.full((ny, nx), 0.2)
            h0[:, -3:] = 2.0                                   # the mound against the open edge
            vol0 = float(h0.sum()) * dx * dx
            a = _fv_deck(os.path.join(d, "in"), z, h0, dx=dx, sim=120.0, n=0.03, free="E")
            s = fve.run_par(a, backend="cpu", order=order)
            assert 0.0 < s["outflow_m3"] < vol0, (order, s["outflow_m3"], vol0)
            assert abs(s["mass_error_m3"]) < 1e-5 * vol0, \
                "order {}: {:+.3g} m3 of {:.0f} m3 came from outside the model".format(
                    order, s["mass_error_m3"], vol0)
            assert s["final_volume_m3"] < vol0, (order, s["final_volume_m3"], vol0)
        finally:
            shutil.rmtree(d, True)


@test
def metalflood_fv2_lake_at_rest_is_well_balanced():
    """The 2nd-order FV2 (MUSCL + SSP-RK2) engine holds a lake at rest over a bumpy bed: no
    spurious velocity and the still-water depth unchanged to float32 round-off. The minmod limiter
    collapses the free-surface slope to zero at rest, so FV2 reduces exactly to FV1 there."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx = 20, 20
        rng = np.random.default_rng(0)
        z = rng.uniform(0.0, 2.0, size=(ny, nx))
        h0 = np.maximum(3.0 - z, 0.0)
        p = _fv_deck(os.path.join(d, "lake"), z, h0, dx=5.0, sim=200.0, n=0.0)
        s = fve.run_par(p, backend="cpu", order=2)
        assert s["scheme"] == "fv2" and s["backend"] == "cpu"
        vc = _res(os.path.join(d, "lake"), "maxVc")
        hm = _res(os.path.join(d, "lake"), "max")
        vmax = vc[vc != bl.NODATA].max()
        drift = np.abs(hm[h0 > 1e-6] - h0[h0 > 1e-6]).max()
        assert vmax < 1e-3, "spurious speed {:.2e} m/s".format(vmax)
        assert drift < 1e-3, "depth drift {:.2e} m".format(drift)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv2_reproduces_the_ritter_dam_break():
    """The 2nd-order FV2 engine on a dry-bed dam break: depth at the dam approaches Ritter's
    4h0/9, the fan matches the analytical rarefaction, and the front is correctly placed."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx, h0, dam = 1, 801, 1.0, 1.0, 400
        z = np.zeros((ny, nx))
        hinit = np.zeros((ny, nx)); hinit[0, :dam] = h0
        p = _fv_deck(os.path.join(d, "ritter"), z, hinit, dx=dx, sim=8.0, n=0.0)
        deck = fve.Deck(p)
        bk = mfb.CpuBackend(mfb.FV2_SPEC)
        eng = fve.Engine(deck, bk)
        t, T = 0.0, 8.0
        while t < T - 1e-6:
            out = eng.batch(t, T, fve.BATCH_STEPS, clip=True)
            if out["steps"] == 0:
                break
            t += out["t_local"]
        h = eng.h2().astype(np.float64)[0].copy()
        bk.close()
        c0 = math.sqrt(_G * h0)
        xc = (np.arange(nx) + 0.5 - dam) * dx

        def ritter_h(x):
            xi = x / T
            if xi <= -c0:
                return h0
            if xi >= 2 * c0:
                return 0.0
            return (2 * c0 - xi) ** 2 / (9 * _G)
        ha = np.array([ritter_h(x) for x in xc])
        assert abs(h[dam] - 4 * h0 / 9) < 0.02, "h@dam {:.4f}".format(h[dam])
        inner = ha > 0.05
        assert np.abs(h[inner] - ha[inner]).mean() < 0.02, np.abs(h[inner] - ha[inner]).mean()
        front = xc[h > 1e-3].max()
        assert 0.70 * (2 * c0 * T) < front <= 2 * c0 * T + dx, "front {:.1f} m".format(front)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv2_conserves_water():
    """The 2nd-order FV2 engine keeps mass: a closed valley filled by an inflow ends with a volume
    error far under a tenth of a percent of what was injected."""
    _need_cpu_build()
    d = _tmp()
    try:
        a = valley_deck(os.path.join(d, "v"), free="", hole=False, pond=False, gauge=False)
        s = fve.run_par(a, backend="cpu", order=2)
        assert s["scheme"] == "fv2"
        assert abs(s["mass_error_m3"]) < 1e-3 * max(s["injected_m3"], 1.0), s["mass_error_m3"]
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv2_gpu_matches_cpu_build_and_repeats_exactly():
    """FV2 on the Metal GPU against the CPU build, and two GPU runs of the same deck against each other.
    Before Runner 3.52.0 k_cont_fv2 read its neighbours' h while overwriting its own, a read-after-write
    race: the GPU answer depended on the order the threads happened to run in. It now reads the
    stage-start copy hs, so the GPU repeats bit for bit and agrees with the CPU build to float
    round-off. Runs only on a Mac with a Metal GPU (the macOS CI job, Nick's Mac)."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    d = _tmp()
    try:
        runs = {}
        for tag, backend in (("cpu", "cpu"), ("gpu1", "metal"), ("gpu2", "metal")):
            deck = valley_deck(os.path.join(d, tag))
            s = fve.run_par(deck, backend=backend, order=2)
            assert s["scheme"] == "fv2" and s["backend"] == backend, (tag, s)
            runs[tag] = s
        for ext in ("max", "maxVc", "inittm"):
            g1 = _res(os.path.join(d, "gpu1"), ext)
            g2 = _res(os.path.join(d, "gpu2"), ext)
            assert np.array_equal(g1, g2), (ext, "GPU FV2 runs differ: a cell-order race is back")
        assert runs["gpu1"]["steps"] == runs["gpu2"]["steps"], runs
        sa, sb = runs["cpu"], runs["gpu1"]
        assert abs(sa["steps"] - sb["steps"]) <= max(3, sa["steps"] // 200), (sa, sb)
        for ext, tol in (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01)):
            x = _res(os.path.join(d, "cpu"), ext)
            y = _res(os.path.join(d, "gpu1"), ext)
            m = (x != bl.NODATA) & (y != bl.NODATA)
            assert np.abs(x - y)[m].max() <= tol, (ext, np.abs(x - y)[m].max())
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_dg2_gpu_matches_cpu_build_and_repeats_exactly():
    """DG2 on the Metal GPU against the CPU build, and two GPU runs against each other (repo audit
    2026-11-23). Runner 3.67.0 made DG2 reconstruct velocities as primitives so it no longer swings
    with the last bit of rounding; on Nick's M2 Pro the GPU then matched the CPU build. Same
    tolerances as the FV2 test. Runs only on a Mac with a Metal GPU (the macOS CI job, Nick's Mac)."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    d = _tmp()
    try:
        runs = {}
        for tag, backend in (("cpu", "cpu"), ("gpu1", "metal"), ("gpu2", "metal")):
            deck = valley_deck(os.path.join(d, tag))
            s = fve.run_par(deck, backend=backend, order=3)
            assert s["scheme"] == "dg2" and s["backend"] == backend, (tag, s)
            runs[tag] = s
        for ext in ("max", "maxVc", "inittm"):
            g1 = _res(os.path.join(d, "gpu1"), ext)
            g2 = _res(os.path.join(d, "gpu2"), ext)
            assert np.array_equal(g1, g2), (ext, "GPU DG2 runs differ: a cell-order race is back")
        assert runs["gpu1"]["steps"] == runs["gpu2"]["steps"], runs
        sa, sb = runs["cpu"], runs["gpu1"]
        assert abs(sa["steps"] - sb["steps"]) <= max(3, sa["steps"] // 200), (sa, sb)
        for ext, tol in (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01)):
            x = _res(os.path.join(d, "cpu"), ext)
            y = _res(os.path.join(d, "gpu1"), ext)
            m = (x != bl.NODATA) & (y != bl.NODATA)
            assert np.abs(x - y)[m].max() <= tol, (ext, np.abs(x - y)[m].max())
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_dg2_lake_at_rest_is_well_balanced():
    """The 2nd-order DG2 (RKDG2) engine holds a lake at rest over a bumpy bed: no spurious velocity
    and the still-water depth unchanged. DG2's slope limiter (on by default) collapses the evolved
    free-surface slopes to zero at rest, so the scheme stays well balanced in float32."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx = 20, 20
        rng = np.random.default_rng(0)
        z = rng.uniform(0.0, 2.0, size=(ny, nx))
        h0 = np.maximum(3.0 - z, 0.0)
        p = _fv_deck(os.path.join(d, "lake"), z, h0, dx=5.0, sim=200.0, n=0.0)
        s = fve.run_par(p, backend="cpu", order=3)
        assert s["scheme"] == "dg2" and s["backend"] == "cpu"
        vc = _res(os.path.join(d, "lake"), "maxVc")
        hm = _res(os.path.join(d, "lake"), "max")
        vmax = vc[vc != bl.NODATA].max()
        drift = np.abs(hm[h0 > 1e-6] - h0[h0 > 1e-6]).max()
        assert vmax < 1e-3, "spurious speed {:.2e} m/s".format(vmax)
        assert drift < 1e-3, "depth drift {:.2e} m".format(drift)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_dg2_reproduces_the_ritter_dam_break():
    """The 2nd-order DG2 engine on a dry-bed dam break: depth at the dam approaches Ritter's 4h0/9,
    the fan matches the analytical rarefaction, and the front is correctly placed."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx, h0, dam = 1, 801, 1.0, 1.0, 400
        z = np.zeros((ny, nx))
        hinit = np.zeros((ny, nx)); hinit[0, :dam] = h0
        p = _fv_deck(os.path.join(d, "ritter"), z, hinit, dx=dx, sim=8.0, n=0.0)
        deck = fve.Deck(p)
        bk = mfb.CpuBackend(mfb.DG2_SPEC)
        eng = fve.Engine(deck, bk)
        t, T = 0.0, 8.0
        while t < T - 1e-6:
            out = eng.batch(t, T, fve.BATCH_STEPS, clip=True)
            if out["steps"] == 0:
                break
            t += out["t_local"]
        h = eng.h2().astype(np.float64)[0].copy()
        bk.close()
        c0 = math.sqrt(_G * h0)
        xc = (np.arange(nx) + 0.5 - dam) * dx

        def ritter_h(x):
            xi = x / T
            if xi <= -c0:
                return h0
            if xi >= 2 * c0:
                return 0.0
            return (2 * c0 - xi) ** 2 / (9 * _G)
        ha = np.array([ritter_h(x) for x in xc])
        assert abs(h[dam] - 4 * h0 / 9) < 0.02, "h@dam {:.4f}".format(h[dam])
        inner = ha > 0.05
        assert np.abs(h[inner] - ha[inner]).mean() < 0.02, np.abs(h[inner] - ha[inner]).mean()
        front = xc[h > 1e-3].max()
        assert 0.70 * (2 * c0 * T) < front <= 2 * c0 * T + dx, "front {:.1f} m".format(front)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_dg2_conserves_water():
    """The 2nd-order DG2 engine keeps mass: a closed valley filled by an inflow ends with a volume
    error far under a tenth of a percent of what was injected."""
    _need_cpu_build()
    d = _tmp()
    try:
        a = valley_deck(os.path.join(d, "v"), free="", hole=False, pond=False, gauge=False)
        s = fve.run_par(a, backend="cpu", order=3)
        assert s["scheme"] == "dg2"
        assert abs(s["mass_error_m3"]) < 1e-3 * max(s["injected_m3"], 1.0), s["mass_error_m3"]
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_dg2_limiter_is_a_deck_keyword():
    """The DG2 slope limiter is on by default and 'nolimitslopes' in the .par turns it off (the raw
    unlimited scheme). The deck exposes the choice the way LISFLOOD-FP's limitslopes keyword does."""
    _need_cpu_build()
    d = _tmp()
    try:
        z = np.zeros((4, 4))
        h0 = np.ones((4, 4))
        p_on = _fv_deck(os.path.join(d, "on"), z, h0, dx=5.0, sim=1.0, n=0.0)
        assert fve.Deck(p_on).dg2_limiter is True
        p_off = _fv_deck(os.path.join(d, "off"), z, h0, dx=5.0, sim=1.0, n=0.0, free="")
        with open(p_off, "a") as f:
            f.write("nolimitslopes\n")
        assert fve.Deck(p_off).dg2_limiter is False
    finally:
        shutil.rmtree(d, True)


@test
def fv_engine_is_a_runner_engine_choice():
    """Runner 3.23.0: 'shock' (or 'gpu-fv') selects Boann's shock-capturing solver; it runs
    in-process, is labelled as not LISFLOOD-FP everywhere, and is a FLOOD_ENGINE choice of its
    own."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.damsuite import record
    for w in ("shock", "SHOCK", " gpu-fv ", "hllc"):
        assert lf.resolve_exe(w) == lf.GPU_FV_EXE, w
    assert os.path.isfile(lf.GPU_FV_EXE)
    assert lf.is_gpu_fv(lf.GPU_FV_EXE) and lf.is_inprocess(lf.GPU_FV_EXE)
    assert not lf.is_gpu(lf.GPU_FV_EXE) and not lf.is_gpu_fv(lf.GPU_EXE)
    assert "NOT LISFLOOD-FP" in lf.engine_label(lf.GPU_FV_EXE)
    assert "NOT LISFLOOD-FP" in lf.describe_exe(lf.GPU_FV_EXE)
    assert lf.engine_name(lf.GPU_FV_EXE) == "Boann (shock-capturing)"
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_FV_EXE}}})
    assert blk["model"].startswith("Boann, shock-capturing"), blk
    assert not blk["built_in"]
    blk = record._engine_block(None, {"software": {"lisflood_exe": {
        "path": "C:\\x\\core\\metalflood\\engine_fv.py"}}})
    assert blk["model"].startswith("Boann, shock-capturing"), blk
    # the ACC engine must still label as Boann ACC, not the shock-capturing one
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_EXE}}})
    assert blk["model"].startswith("Boann") and "shock" not in blk["model"], blk
    try:                                                    # the Processing layer needs QGIS
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[2] == "gpu-fv"
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)


def fv2_engine_is_a_runner_engine_choice():
    """Runner 3.33.0: 'fv2' (or 'gpu-fv2' / 'muscl') selects Boann's 2nd-order shock-capturing
    solver (MUSCL + SSP-RK2); it runs in-process, is labelled as not LISFLOOD-FP everywhere, is
    distinct from the 1st-order and ACC engines, and is a FLOOD_ENGINE choice of its own."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.damsuite import record
    for w in ("fv2", "FV2", " gpu-fv2 ", "muscl"):
        assert lf.resolve_exe(w) == lf.GPU_FV2_EXE, w
    assert os.path.isfile(lf.GPU_FV2_EXE)
    assert lf.is_gpu_fv2(lf.GPU_FV2_EXE) and lf.is_inprocess(lf.GPU_FV2_EXE)
    assert lf.is_gpu_fv_any(lf.GPU_FV2_EXE) and lf.is_gpu_any(lf.GPU_FV2_EXE)
    # distinct file from the 1st-order and ACC engines; the FV1/ACC predicates must not match it
    assert not lf.is_gpu_fv(lf.GPU_FV2_EXE) and not lf.is_gpu(lf.GPU_FV2_EXE)
    assert not lf.is_gpu_fv2(lf.GPU_FV_EXE) and not lf.is_gpu_fv2(lf.GPU_EXE)
    assert "NOT LISFLOOD-FP" in lf.engine_label(lf.GPU_FV2_EXE)
    assert "2nd order" in lf.engine_label(lf.GPU_FV2_EXE)
    assert "NOT LISFLOOD-FP" in lf.describe_exe(lf.GPU_FV2_EXE)
    assert lf.engine_name(lf.GPU_FV2_EXE) == "Boann (shock-capturing, 2nd order)"
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_FV2_EXE}}})
    assert blk["model"].startswith("Boann, shock-capturing 2nd order"), blk
    assert not blk["built_in"]
    blk = record._engine_block(None, {"software": {"lisflood_exe": {
        "path": "C:\\x\\core\\metalflood\\engine_fv2.py"}}})
    assert blk["model"].startswith("Boann, shock-capturing 2nd order"), blk
    # the 1st-order FV engine must still label as 1st order, not 2nd
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_FV_EXE}}})
    assert blk["model"].startswith("Boann, shock-capturing") and "2nd order" not in blk["model"], blk
    try:                                                    # the Processing layer needs QGIS
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[3] == "gpu-fv2"
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)


def dg2_engine_is_a_runner_engine_choice():
    """Runner 3.34.0: 'dg2' (or 'gpu-dg2' / 'rkdg2' / 'galerkin') selects Boann's 2nd-order
    discontinuous-Galerkin solver; it runs in-process, is labelled as not LISFLOOD-FP everywhere, is
    distinct from the FV and ACC engines, and is a FLOOD_ENGINE choice of its own."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.damsuite import record
    for w in ("dg2", "DG2", " gpu-dg2 ", "rkdg2", "galerkin"):
        assert lf.resolve_exe(w) == lf.GPU_DG2_EXE, w
    assert os.path.isfile(lf.GPU_DG2_EXE)
    assert lf.is_gpu_dg2(lf.GPU_DG2_EXE) and lf.is_inprocess(lf.GPU_DG2_EXE)
    assert lf.is_gpu_shock(lf.GPU_DG2_EXE) and lf.is_gpu_any(lf.GPU_DG2_EXE)
    # distinct file from the FV and ACC engines; those predicates must not match it, nor it them
    assert not lf.is_gpu_fv(lf.GPU_DG2_EXE) and not lf.is_gpu_fv2(lf.GPU_DG2_EXE)
    assert not lf.is_gpu(lf.GPU_DG2_EXE) and not lf.is_gpu_fv_any(lf.GPU_DG2_EXE)
    assert not lf.is_gpu_dg2(lf.GPU_FV2_EXE) and not lf.is_gpu_dg2(lf.GPU_EXE)
    assert "NOT LISFLOOD-FP" in lf.engine_label(lf.GPU_DG2_EXE)
    assert "Galerkin" in lf.engine_label(lf.GPU_DG2_EXE)
    assert "NOT LISFLOOD-FP" in lf.describe_exe(lf.GPU_DG2_EXE)
    assert lf.engine_name(lf.GPU_DG2_EXE) == "Boann (discontinuous Galerkin, 2nd order)"
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_DG2_EXE}}})
    assert blk["model"].startswith("Boann, discontinuous Galerkin 2nd order"), blk
    assert not blk["built_in"]
    blk = record._engine_block(None, {"software": {"lisflood_exe": {
        "path": "C:\\x\\core\\metalflood\\engine_dg2.py"}}})
    assert blk["model"].startswith("Boann, discontinuous Galerkin 2nd order"), blk
    # the 2nd-order FV engine must still label as shock-capturing, not Galerkin
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_FV2_EXE}}})
    assert "Galerkin" not in blk["model"], blk
    try:                                                    # the Processing layer needs QGIS
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[4] == "gpu-dg2"
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)


from nd_breach_runner.core.metalflood import engine_sgc as sge  # noqa: E402
from nd_breach_runner.core.metalflood import sgc_ref as sgr  # noqa: E402


def _sgc_deck(folder, z, w, bed, dx=10.0, sim=600.0, inflow=None, sgcn=0.035, bank=None,
              physical=True, free=""):
    """A DEM + SGC rasters (SGCwidth / SGCbank / SGCbed, SGCn) deck, optionally with one constant
    point inflow and FREE edges."""
    os.makedirs(folder, exist_ok=True)
    xll, yll = 500000.0, 5200000.0
    ny, nx = z.shape
    bank = z if bank is None else bank
    _write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    _write_asc(os.path.join(folder, "run.sgcwidth.asc"), w, xll, yll, dx)
    _write_asc(os.path.join(folder, "run.sgcbank.asc"), bank, xll, yll, dx)
    _write_asc(os.path.join(folder, "run.sgcbed.asc"), bed, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim), "initial_tstep 1.0", "massint {:.1f}".format(sim),
             "saveint {:.1f}".format(sim + 10), "fpfric 0.06",
             "SGCwidth run.sgcwidth.asc", "SGCbank run.sgcbank.asc", "SGCbed run.sgcbed.asc",
             "SGCn {:.4f}".format(sgcn)]
    if physical:
        lines.append("physicalinflow")
    bci = []
    if inflow is not None:
        (r, c), q = inflow
        ytop = yll + ny * dx
        gx, gy = xll + (c + 0.5) * dx, ytop - (r + 0.5) * dx
        bci.append("P {:.3f} {:.3f} QVAR breach\n".format(gx, gy))
        # the ACC Deck multiplies a .bdy value by dx (m2/s -> m3/s per cell), so write q/dx to
        # inject the actual q m3/s.
        with open(os.path.join(folder, "run.bdy"), "w") as f:
            f.write("sgc inflow\nbreach\n2 seconds\n{:.6f} 0\n{:.6f} 1000000000\n".format(
                q / dx, q / dx))
        lines.append("bdyfile run.bdy")
    for e in free:
        lo, hi = (xll, xll + nx * dx) if e in "NS" else (yll, yll + ny * dx)
        bci.append("{} {:.3f} {:.3f} FREE\n".format(e, lo, hi))
    if bci:
        with open(os.path.join(folder, "run.bci"), "w") as f:
            f.writelines(bci)
        lines.append("bcifile run.bci")
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.join(folder, "run.par")


@test
def sgc_engine_is_a_runner_engine_choice():
    """Runner 3.36.0: 'sgc' (or 'gpu-sgc' / 'metal-sgc') selects Boann's sub-grid-channel solver; it
    runs in-process, is labelled as not LISFLOOD-FP everywhere, is ACC-family (NOT shock-capturing),
    is distinct from every other engine, and is a FLOOD_ENGINE choice of its own."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.damsuite import record
    for w in ("sgc", "SGC", " gpu-sgc ", "metal-sgc"):
        assert lf.resolve_exe(w) == lf.GPU_SGC_EXE, w
    assert os.path.isfile(lf.GPU_SGC_EXE)
    assert lf.is_gpu_sgc(lf.GPU_SGC_EXE) and lf.is_inprocess(lf.GPU_SGC_EXE)
    assert lf.is_gpu_any(lf.GPU_SGC_EXE)
    assert not lf.is_gpu_shock(lf.GPU_SGC_EXE)          # SGC is the local-inertial ACC scheme
    # distinct file from every other engine; predicates must not cross-match
    assert not lf.is_gpu(lf.GPU_SGC_EXE) and not lf.is_gpu_fv_any(lf.GPU_SGC_EXE)
    assert not lf.is_gpu_dg2(lf.GPU_SGC_EXE)
    assert not lf.is_gpu_sgc(lf.GPU_EXE) and not lf.is_gpu_sgc(lf.GPU_DG2_EXE)
    assert "NOT LISFLOOD-FP" in lf.engine_label(lf.GPU_SGC_EXE)
    assert "sub-grid channels" in lf.engine_label(lf.GPU_SGC_EXE)
    assert "NOT LISFLOOD-FP" in lf.describe_exe(lf.GPU_SGC_EXE)
    assert lf.engine_name(lf.GPU_SGC_EXE) == "Boann (sub-grid channels)"
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_SGC_EXE}}})
    assert blk["model"].startswith("Boann, sub-grid channels"), blk
    assert not blk["built_in"]
    blk = record._engine_block(None, {"software": {"lisflood_exe": {
        "path": "C:\\x\\core\\metalflood\\engine_sgc.py"}}})
    assert blk["model"].startswith("Boann, sub-grid channels"), blk
    # the plain ACC engine must NOT be read as sub-grid channels
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": lf.GPU_EXE}}})
    assert "sub-grid" not in blk["model"], blk
    try:
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[5] == "gpu-sgc"
        assert algs.FLOOD_ENGINES[4] == "gpu-dg2"       # the others did not move
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)
        # Runner 3.39.0: find_lisflood delegates the FLOOD_ENGINE dropdown to resolve_exe, so every
        # engine the dropdown offers must map to a distinct engine exe. gpu-sgc was missing from the
        # inlined mapping (#446), so picking "Boann, sub-grid channels" ran LISFLOOD-FP. This guards
        # that drift for every entry, not just sgc.
        exe_for = {"gpu": lf.GPU_EXE, "gpu-fv": lf.GPU_FV_EXE, "gpu-fv2": lf.GPU_FV2_EXE,
                   "gpu-dg2": lf.GPU_DG2_EXE, "gpu-sgc": lf.GPU_SGC_EXE,
                   "gpu-subfv": lf.GPU_SUBFV_EXE, "gpu-subli": lf.GPU_SUBLI_EXE}
        for _w in algs.FLOOD_ENGINES[1:]:               # every engine except LISFLOOD-FP (index 0)
            if _w == "auto":                            # 3.44.0: picked per computer, tested below
                continue
            assert lf.resolve_exe(_w) == exe_for[_w], _w
        assert lf.resolve_exe("lisflood") == "lisflood"  # not an engine word -> returned verbatim


@test
def recommended_auto_engine_choice():
    """Runner 3.44.0: 'Recommended (auto)' is appended (no saved index moves), labels are plain (no
    'Apple GPU' - the engine also runs on the CPU), and choose_auto picks Boann ACC on an Apple GPU,
    else an installed LISFLOOD-FP, else Boann on the CPU, else nothing (with a reason)."""
    import os
    import tempfile
    from unittest import mock
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core.metalflood import backends as mfb
    for w in ("auto", " Auto ", "recommended", "Recommended (auto)"):
        assert lf.is_auto_word(w), w
    assert not lf.is_auto_word("gpu") and not lf.is_auto_word("")
    fake_lf = os.path.join(tempfile.mkdtemp(prefix="autolf_"), "lisflood")
    open(fake_lf, "w").close()
    with mock.patch.object(mfb, "metal_available", return_value=(True, "M2 Pro")):
        exe, why = lf.choose_auto(fake_lf)
        assert exe == lf.GPU_EXE and "GPU" in why, why
    with mock.patch.object(mfb, "metal_available", return_value=(False, "not macOS")):
        exe, why = lf.choose_auto(fake_lf)
        assert exe == fake_lf and "LISFLOOD-FP" in why, why
        with mock.patch.object(mfb, "prebuilt_available", return_value=True):
            exe, why = lf.choose_auto("")
            assert exe == lf.GPU_EXE and "CPU" in why, why
        with mock.patch.object(mfb, "prebuilt_available", return_value=False), \
                mock.patch.object(mfb, "find_compiler", return_value=None):
            exe, why = lf.choose_auto(os.path.join(os.path.dirname(fake_lf), "missing"))
            assert exe == "" and "no flood engine" in why, why
    try:
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[:7] == ("lisflood", "gpu", "gpu-fv", "gpu-fv2", "gpu-dg2",
                                          "gpu-sgc", "gpu-subfv")   # saved indices unchanged
        assert algs.FLOOD_ENGINES[7] == "auto"
        assert algs.FLOOD_ENGINES[8] == "gpu-subli"                 # 3.79.0, appended
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)
        assert not any("Apple GPU" in t for t in algs.FLOOD_ENGINES_TEXT)
        assert algs.FLOOD_ENGINES_TEXT[7].startswith("Recommended (auto)")
        for i in (3, 4, 6):
            assert algs.FLOOD_ENGINES_TEXT[i].startswith("Advanced:"), i

@test
def boann_shock_engines_rerun_at_a_cfl_they_honour():
    """The mass-balance re-run (workflow: volume error over STABILITY_RETRY_PCT) must change
    something for Boann's shock-capturing engines. It used CFL 0.5, which FV1 / FV2 / DG2 / sub-grid
    FV clamp to their own ceiling (0.45 or 1/3), and theta, which they ignore, so it re-ran them
    unchanged (twice the time, the same 3.5 % error on the Lake Delhi gate). They now re-run at
    STABLE_CFL_SHOCK, below every ceiling, and the engines really take more steps at it; the ACC /
    LISFLOOD-FP re-run is unchanged."""
    import re
    from nd_breach_runner.core import lisflood as lf
    for e in (lf.GPU_FV_EXE, lf.GPU_FV2_EXE, lf.GPU_DG2_EXE, lf.GPU_SUBFV_EXE):
        assert lf.stable_cfl(e) == lf.STABLE_CFL_SHOCK, e
    for e in (lf.GPU_EXE, lf.GPU_SGC_EXE, "/usr/local/bin/lisflood", ""):
        assert lf.stable_cfl(e) == lf.STABLE_CFL, e
    here = lf.BOANN_DIR                    # DamSafety_Core/boann/hydro (Runner 3.97.2: one copy, in the Core)
    for k in ("kernels_fv.metal", "kernels_fv2.metal", "kernels_dg2.metal", "kernels_subfv.metal"):
        with open(os.path.join(here, k), encoding="utf-8") as f:
            m = re.search(r"#define MF_(?:DG2_CFL|CFL_CAP) ([0-9.]+)f", f.read())
        cap = float(m.group(1))
        assert lf.STABLE_CFL_SHOCK < cap, (k, cap)
    _need_cpu_build()
    d = _tmp()
    try:
        z = np.zeros((6, 30))
        h0 = np.where(np.arange(30)[None, :] < 10, 2.0, 0.5) * np.ones((6, 1))
        steps = []
        for cfl in (None, lf.STABLE_CFL_SHOCK):
            a = _fv_deck(os.path.join(d, str(cfl)), z, h0, dx=5.0, sim=30.0, n=0.03)
            if cfl is not None:
                with open(a, "a") as f:
                    f.write("cfl {}\n".format(cfl))
            steps.append(fve.run_par(a, backend="cpu", order=1)["steps"])
        assert steps[1] > 1.5 * steps[0], steps
    finally:
        shutil.rmtree(d, True)


@test
def subfv_engine_is_a_runner_engine_choice():
    """Runner 3.42.0: 'subfv' (or 'gpu-subfv' / 'metal-subfv' / 'sub-grid fv') selects Boann's
    sub-grid finite-volume solver; it runs in-process, is shock-capturing family, is labelled as not
    LISFLOOD-FP everywhere, is distinct from every other engine, is a FLOOD_ENGINE choice of its own,
    and the 'subgrid r' coarsening factor reaches the .par for that engine only."""
    import os
    import tempfile
    import numpy as np
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.damsuite import record
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    for w in ("subfv", "SUBFV", " gpu-subfv ", "metal-subfv", "sub-grid fv"):
        assert lf.resolve_exe(w) == lf.GPU_SUBFV_EXE, w
    e = lf.GPU_SUBFV_EXE
    assert os.path.isfile(e)
    assert lf.is_gpu_subfv(e) and lf.is_inprocess(e) and lf.is_gpu_any(e)
    assert lf.is_gpu_shock(e)                              # sub-grid FV is an HLLC (shock) scheme
    # distinct file from every other engine; predicates must not cross-match
    assert not (lf.is_gpu(e) or lf.is_gpu_fv_any(e) or lf.is_gpu_dg2(e) or lf.is_gpu_sgc(e))
    for other in (lf.GPU_EXE, lf.GPU_FV_EXE, lf.GPU_FV2_EXE, lf.GPU_DG2_EXE, lf.GPU_SGC_EXE):
        assert not lf.is_gpu_subfv(other), other
    assert "NOT LISFLOOD-FP" in lf.engine_label(e) and "sub-grid finite volume" in lf.engine_label(e)
    assert "NOT LISFLOOD-FP" in lf.describe_exe(e)
    assert lf.engine_name(e) == "Boann (sub-grid finite volume)"
    blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": e}}})
    assert blk["model"].startswith("Boann, sub-grid finite volume") and not blk["built_in"], blk
    blk = record._engine_block(None, {"software": {"lisflood_exe": {
        "path": "C:\\x\\core\\metalflood\\engine_subfv.py"}}})
    assert blk["model"].startswith("Boann, sub-grid finite volume"), blk
    # the sub-grid-CHANNEL engine must NOT be read as sub-grid finite volume, and vice versa
    assert "finite volume" not in record._engine_block(
        None, {"software": {"lisflood_exe": {"path": lf.GPU_SGC_EXE}}})["model"]
    try:
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[6] == "gpu-subfv"
        assert algs.FLOOD_ENGINES[5] == "gpu-sgc"          # the others did not move
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)
    # the 'subgrid r' coarsening factor reaches the .par for the sub-grid-FV engine only
    d = tempfile.mkdtemp(prefix="subfvpar_")
    dem = Grid(np.full((8, 8), 10.0), 0.0, 80.0, 10.0)
    hy = Hydrograph(np.array([0.0, 60.0]), np.array([1.0, 0.0]), "t")
    st = lf.LisfloodSettings(e, sim_time_s=60.0, subgrid_r=5)
    par = lf.write_inputs(d, dem, (5.0, 40.0), hy, st)
    assert "subgrid 5" in open(par, encoding="utf-8").read()
    # a non-subfv engine with the same setting does NOT get the keyword
    st2 = lf.LisfloodSettings(lf.GPU_FV_EXE, sim_time_s=60.0, subgrid_r=5)
    par2 = lf.write_inputs(tempfile.mkdtemp(prefix="subfvpar2_"), dem, (5.0, 40.0), hy, st2)
    assert "subgrid" not in open(par2, encoding="utf-8").read()


@test
def subfv_coarsening_factor_is_a_cli_config_key():
    """Runner 3.49.0: a headless run sets the sub-grid FV coarsening factor with model.subgrid_r
    (1-8), as the run tool does with SUBGRID_R. It lands on the same run setting the tool writes
    (user_subgrid_r), so workflow forwards it to the .par the same way. A config without the key
    leaves the settings - and so the resume key of every existing run - exactly as they were."""
    import importlib
    from nd_breach_runner.core import workflow
    cli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    assert cli.MODEL_KEYS["subgrid_r"] == ("user_subgrid_r", ("int", 1, 8))
    opts = type("O", (), {"lisflood": sys.executable})()
    st = cli.build_settings(cli.Config({"model": {"subgrid_r": 8}}, ""), opts, need_models=False)
    assert st.user_subgrid_r == 8 and isinstance(st.user_subgrid_r, int)
    assert workflow.key_settings(st)["user_subgrid_r"] == 8      # a different r is a different run
    plain = cli.build_settings(cli.Config({"model": {}}, ""), opts, need_models=False)
    assert plain.user_subgrid_r is None and workflow.RunSettings().user_subgrid_r is None
    assert "user_subgrid_r" not in workflow.key_settings(plain)   # earlier checkpoints still resume
    for bad in (0, 9, 2.5, "4"):
        try:
            cli._check_value("model.subgrid_r", bad, cli.MODEL_KEYS["subgrid_r"][1])
        except cli.CliError:
            continue
        raise AssertionError("model.subgrid_r accepted {!r}".format(bad))
    # the run tool writes the same attribute, and workflow reads it for the .par keyword
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert "st.user_subgrid_r = " in open(os.path.join(here, "algorithms.py"), encoding="utf-8").read()
    assert 'kw["subgrid_r"] = int(st.user_subgrid_r)' in open(
        os.path.join(here, "core", "workflow.py"), encoding="utf-8").read()


@test
def metalflood_sgc_params_layout_matches_kernels():
    """The SGC buffer layout and the kernel source agree: 30 data buffers, Params at index 30 (the
    31-slot Metal cap), and the CPU build's Params size equals Python's."""
    _need_cpu_build()
    import ctypes
    lib = ctypes.CDLL(mfb.build_cpu_library(mfb.SGC_SPEC))
    assert lib.mf_params_size() == ctypes.sizeof(mfb.Params) == 96
    names = [n for n, _, _ in mfb.sgc_buffer_layout(3, 4, 1, 1, 2)]
    assert len(names) == 30, len(names)
    assert names[:3] == ["z", "n2", "h"] and names[-3:] == ["w", "Dd", "nc2"]
    with open(mfb.SGC_KERNEL_SOURCE, encoding="utf-8") as f:
        src = f.read()
    assert "constant Params& p B({})".format(len(names)) in src   # Params at index 30


@test
def metalflood_sgc_channel_at_rest_is_well_balanced():
    """A channel filled to a flat water surface below bank, over a flat bed, stays at rest to
    float32 round-off and keeps its mass (the SGC well-balanced property: flat eta -> zero slope ->
    zero discharge, in-channel)."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx = 16, 24, 10.0
        z = np.full((ny, nx), 40.0)
        w = np.zeros((ny, nx)); w[8, :] = 2.0
        D = np.zeros((ny, nx)); D[8, :] = 1.0
        bed = z - D
        p = _sgc_deck(os.path.join(d, "rest"), z, w, bed, dx=dx, sim=600.0)
        deck = sge.Deck(p, build_from_dem=False)
        bk = mfb.CpuBackend(mfb.SGC_SPEC)
        eng = sge.Engine(deck, bk)
        h_flat = np.where(w > 0, -0.4, 0.0)            # channel 0.6 m deep, flat surface eta=39.6
        eng.a["h"][:] = h_flat.ravel()
        eng.a["st"][mfe.S_HMAX] = np.float32(0.6).view(np.uint32)
        V0 = eng.volume()
        t, T = 0.0, 600.0
        while t < T - 1e-6:
            out = eng.batch(t, T, sge.BATCH_STEPS, clip=True)
            if out["steps"] == 0:
                break
            t += out["t_local"]
        h = eng.h2().astype(np.float64)
        bk.close()
        drift = np.abs(h - h_flat).max()
        assert drift < 1e-3, "rest drift {:.2e} m".format(drift)
        assert abs(eng.volume() - V0) < 1e-6 * max(V0, 1.0)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_sgc_matches_the_float64_reference():
    """The float32 SGC CPU engine matches the float64 numpy reference (sgc_ref) to float32 round-off
    on an inflow case: same channel geometry, same CFL, driven to the same end time."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx = 16, 50, 10.0
        z = (50.0 - 0.02 * np.arange(nx))[None, :] * np.ones((ny, 1))
        w = np.zeros((ny, nx)); w[8, :] = 3.0
        D = np.zeros((ny, nx)); D[8, :] = 1.5
        bed = z - D
        q = 20.0        # actual m3/s; overbanks the ~4.7 m3/s channel so both regimes are exercised
        p = _sgc_deck(os.path.join(d, "ref"), z, w, bed, dx=dx, sim=500.0, inflow=((8, 0), q))
        deck = sge.Deck(p, build_from_dem=False)
        bk = mfb.CpuBackend(mfb.SGC_SPEC)
        eng = sge.Engine(deck, bk)
        T = 500.0
        t = 0.0
        while t < T - 1e-6:
            out = eng.batch(t, T, sge.BATCH_STEPS, clip=True)
            if out["steps"] == 0:
                break
            t += out["t_local"]
        hk = eng.h2().astype(np.float64).copy()
        bk.close()
        r = sgr.SGCRef(z, w, D, 0.035, 0.06, dx)
        r.set_h(np.where(w > 0, -D, 0.0))
        srcarr = np.zeros((ny, nx)); srcarr[8, 0] = q
        r.run(srcarr, T=T, dt0=1.0, cfl=bl.ALPHA)
        diff = np.abs(hk - r.h).max()
        assert diff < 5e-3, "engine vs reference max |dh| = {:.3e} m".format(diff)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_sgc_conveys_below_bank_then_spills_overbank():
    """A sub-bankfull inflow runs down the channel and stays below bank (every h < 0); a large
    inflow exceeds the channel and spills onto the floodplain (some h > 0). Both conserve mass in a
    closed basin."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx = 16, 50, 10.0
        z = (50.0 - 0.02 * np.arange(nx))[None, :] * np.ones((ny, 1))
        w = np.zeros((ny, nx)); w[8, :] = 3.0
        D = np.zeros((ny, nx)); D[8, :] = 1.5
        bed = z - D
        # sub-bankfull: 2 m3/s, short enough not to back-fill the closed basin over bank
        p = _sgc_deck(os.path.join(d, "lo"), z, w, bed, dx=dx, sim=300.0, inflow=((8, 0), 2.0))
        s = sge.run_par(p, backend="cpu", build_from_dem=False)
        hlo = _res(os.path.join(d, "lo"), "max")           # depth ABOVE bank; 0 while in-channel
        assert hlo[hlo != bl.NODATA].max() < 1e-3, "sub-bankfull flow left the channel"
        assert abs(s["mass_error_m3"]) < 1e-3 * max(s["injected_m3"], 1.0), s["mass_error_m3"]
        assert (s["channel_cells"] == int((w > 0).sum()))
        # overbank: 300 m3/s spills
        p = _sgc_deck(os.path.join(d, "hi"), z, w, bed, dx=dx, sim=600.0, inflow=((8, 0), 300.0))
        s = sge.run_par(p, backend="cpu", build_from_dem=False)
        hhi = _res(os.path.join(d, "hi"), "max")
        assert hhi[hhi != bl.NODATA].max() > 0.0, "overbank inflow never spilled"
        assert abs(s["mass_error_m3"]) < 1e-3 * max(s["injected_m3"], 1.0), s["mass_error_m3"]
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_sgc_free_edge_drains_and_matches_reference():
    """A channel inflow drains off a FREE east edge: the SGC engine matches the float64 reference
    (which carries the same FREE-edge outflow) to float32, and its mass balances
    (injected = outflow + stored)."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx = 12, 50, 10.0
        z = (50.0 - 0.03 * np.arange(nx))[None, :] * np.ones((ny, 1))
        w = np.zeros((ny, nx)); w[6, :] = 3.0
        D = np.zeros((ny, nx)); D[6, :] = 1.5
        bed = z - D
        q = 6.0           # just over the channel's ~4-5 m3/s capacity so it drains steadily east
        p = _sgc_deck(os.path.join(d, "fe"), z, w, bed, dx=dx, sim=900.0, inflow=((6, 0), q),
                      free="E")
        deck = sge.Deck(p, build_from_dem=False)
        bk = mfb.CpuBackend(mfb.SGC_SPEC)
        eng = sge.Engine(deck, bk)
        T = 900.0
        t = 0.0
        while t < T - 1e-6:
            out = eng.batch(t, T, sge.BATCH_STEPS, clip=True)
            if out["steps"] == 0:
                break
            t += out["t_local"]
        hk = eng.h2().astype(np.float64).copy()
        bk.close()
        s = sge.run_par(p, backend="cpu", build_from_dem=False)   # for its mass balance
        assert s["outflow_m3"] > 0.3 * s["injected_m3"], "little drained off the FREE edge"
        assert abs(s["mass_error_m3"]) < 1e-3 * max(s["injected_m3"], 1.0), s["mass_error_m3"]
        r = sgr.SGCRef(z, w, D, 0.035, 0.06, dx, free_mask=sgr.FREE_E)
        r.set_h(np.where(w > 0, -D, 0.0))
        srcarr = np.zeros((ny, nx)); srcarr[6, 0] = q
        r.run(srcarr, T=T, dt0=1.0, cfl=bl.ALPHA)
        diff = np.abs(hk - r.h).max()
        assert diff < 1e-2, "engine vs reference (FREE edge) max |dh| = {:.3e} m".format(diff)
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_sgc_reduces_to_acc_without_channels():
    """With no channels (no SGC rasters, build_from_dem off) the SGC engine is plain ACC: a pond on
    a closed flat basin spreads and its depth field matches the ACC engine to float32."""
    _need_cpu_build()
    d = _tmp()
    try:
        ny, nx, dx = 20, 20, 10.0
        z = np.full((ny, nx), 30.0)
        h0 = np.zeros((ny, nx)); h0[8:12, 8:12] = 0.6
        # ACC engine
        aroot = _fv_like_start_deck(os.path.join(d, "acc"), z, h0, dx)
        sa = mfe.run_par(aroot, backend="cpu")
        ha = _res(os.path.join(d, "acc"), "max")
        # SGC engine, no channels
        sroot = _sgc_deck(os.path.join(d, "sgc"), z, np.zeros((ny, nx)), z, dx=dx, sim=200.0)
        # add the same start depth
        _write_asc(os.path.join(d, "sgc", "run.start"), h0, 500000.0, 5200000.0, dx)
        with open(sroot, "a") as f:
            f.write("startfile run.start\n")
        ss = sge.run_par(sroot, backend="cpu", build_from_dem=False)
        hs = _res(os.path.join(d, "sgc"), "max")
        both = (ha != bl.NODATA) & (hs != bl.NODATA)
        assert np.abs(ha[both] - hs[both]).max() < 1e-3, np.abs(ha[both] - hs[both]).max()
        assert ss["channel_cells"] == 0
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_sgc_floodplain_area_spans_the_full_cell_width():
    """The overbank floodplain discharge uses the FULL cell-width flow area, with the channel's
    share removed ONCE as q*(1 - w/dx) -- exactly LISFLOOD-FP SGC2 (sgm_fast.cpp SGC2_CalcPoint:
    A = cell_width*hflow, then q - channel_ratio*q). Guards against re-narrowing the area to
    (dx - w)*hflow, which applies the width fraction twice and starves the floodplain (the
    over-prediction near bankfull found in the 2026-11-20 LISFLOOD-FP head-to-head). Fast unit
    check on the reference face kernel; the float32 engine mirrors it (…matches_the_float64_reference).
    The reduces-to-ACC test can't catch this -- there w=0, so the width fraction vanishes."""
    dx, dt = 10.0, 1.0
    w, D = 4.0, 1.0                          # sub-cell channel, so (1 - w/dx) = 0.6
    nchan2, nfp2 = 0.035 ** 2, 0.06 ** 2
    one = np.ones(1)
    # two channel cells, both above bank (h > 0): the floodplain term is active on the face
    hl, hr = 0.5 * one, 0.3 * one
    zl, zr = 0.0 * one, 0.0 * one
    wl = wr = w * one
    Dl = Dr = D * one
    qc, qf = sgr._axis_Q(hl, hr, zl, zr, wl, wr, Dl, Dr, 0.0 * one, 0.0 * one,
                         nchan2, nfp2, dx, dt)
    wratio = w / dx
    hflow = max(hl[0], hr[0]) - max(zl[0], zr[0])            # etamax - max(z)
    S = ((zr[0] - zl[0]) + (hr[0] - hl[0])) / dx
    full = sgr._calcQ(0.0, dx * hflow, hflow, S, nfp2, dt) * (1.0 - wratio)        # LF-faithful
    narrowed = sgr._calcQ(0.0, dx * (1.0 - wratio) * hflow, hflow, S, nfp2, dt) * (1.0 - wratio)
    assert abs(qf[0] - full) < 1e-12, \
        "floodplain qf {:.6g} != full-width LF form {:.6g}".format(qf[0], full)
    # the two forms must be meaningfully different, or the guard would be vacuous
    assert abs(full - narrowed) > 1e-3, \
        "full-width and narrowed forms indistinguishable ({:.3e})".format(full - narrowed)


def _fv_like_start_deck(folder, z, h0, dx, sim=200.0):
    """A start-depth ACC deck (closed walls) for the reduces-to-ACC comparison."""
    os.makedirs(folder, exist_ok=True)
    xll, yll = 500000.0, 5200000.0
    _write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    _write_asc(os.path.join(folder, "run.start"), h0, xll, yll, dx)
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(["DEMfile run.dem.asc", "resroot run", "dirroot results",
                           "startfile run.start", "sim_time {:.1f}".format(sim),
                           "initial_tstep 1.0", "massint {:.1f}".format(sim),
                           "saveint {:.1f}".format(sim + 10), "fpfric 0.06",
                           "1Dfriction"]) + "\n")   # SGC uses LISFLOOD-FP SGC's 1-D CalculateQ
    return os.path.join(folder, "run.par")


@test
def metalflood_cpu_openmp_matches_one_core():
    """The multi-core (OpenMP) CPU build gives the same bits as the one-core build for every Boann
    engine (ACC, FV1, FV2, DG2, SGC, sub-grid FV, sub-grid local inertial): every reduction in the kernels is an integer
    add / max / min, so the order the cores finish in cannot change the answer."""
    _need_cpu_build()
    from nd_breach_runner.core.metalflood import engine_fv2 as fv2e, engine_dg2 as dg2e
    from nd_breach_runner.core.metalflood import engine_sgc as sgce, engine_subfv as sfve
    from nd_breach_runner.core.metalflood import subfv_backend as sfb
    from nd_breach_runner.core.metalflood import engine_subli as slie, subli_backend as slb
    keep = {k: os.environ.get(k) for k in ("DAMSAFETY_MF_OPENMP", "DAMSAFETY_MF_THREADS")}
    try:
        os.environ["DAMSAFETY_MF_OPENMP"] = "1"
        if not mfb.build_cpu_library(mfb.FLOOD_SPEC).endswith("_omp" + mfb.library_ext()):
            raise Skip("this C++ compiler has no OpenMP")
        engines = (("acc", mf.run_par, mfb.FLOOD_SPEC), ("fv", fve.run_par, mfb.FV_SPEC),
                   ("fv2", fv2e.run_par, mfb.FV2_SPEC), ("dg2", dg2e.run_par, mfb.DG2_SPEC),
                   ("sgc", sgce.run_par, mfb.SGC_SPEC), ("subfv", sfve.run_par, sfb.SUBFV_SPEC),
                   ("subli", slie.run_par, slb.SUBLI_SPEC))
        d = _tmp()
        try:
            for name, run, spec in engines:
                outs = []
                for mode, threads in (("0", "1"), ("1", "4")):
                    os.environ["DAMSAFETY_MF_OPENMP"] = mode
                    os.environ["DAMSAFETY_MF_THREADS"] = threads
                    lib = mfb.build_cpu_library(spec)
                    assert ("_omp" in os.path.basename(lib)) == (mode == "1"), lib
                    f = os.path.join(d, "{}_{}".format(name, mode))
                    p = valley_deck(f, ny=72, nx=110, sim_s=1800.0, hole=False, pond=False,
                                    gauge=False, free="E")
                    st = run(p, backend="cpu")
                    outs.append((st, {e: _res(f, e) for e in ("max", "maxVc", "inittm")}))
                (s1, r1), (sn, rn) = outs
                assert s1["steps"] == sn["steps"], (name, s1["steps"], sn["steps"])
                for e in r1:
                    assert np.array_equal(r1[e], rn[e]), (name, e, float(np.abs(r1[e] - rn[e]).max()))
                assert s1["final_volume_m3"] == sn["final_volume_m3"], name
        finally:
            shutil.rmtree(d, True)
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def metalflood_cpu_prefers_a_prebuilt_library():
    """A CPU build shipped in prebuilt/<platform>/ under these sources' name is used before the
    cache or a compiler; a file there that does not open is skipped."""
    _need_cpu_build()
    keep = {k: os.environ.get(k) for k in ("DAMSAFETY_MF_OPENMP", "DAMSAFETY_MF_CACHE")}
    lib_dir = os.path.join(mfb.PREBUILT_DIR, mfb.platform_tag())
    made_root = not os.path.isdir(mfb.PREBUILT_DIR)
    made_dir = not os.path.isdir(lib_dir)
    d = _tmp()
    made = []
    try:
        os.environ["DAMSAFETY_MF_CACHE"] = os.path.join(d, "cache")
        os.environ["DAMSAFETY_MF_OPENMP"] = "0"
        built = mfb.build_cpu_library(mfb.FLOOD_SPEC)            # one-core build, in the cache
        os.makedirs(lib_dir, exist_ok=True)
        serial = os.path.join(lib_dir, mfb.cpu_library_name(mfb.FLOOD_SPEC, mfb.library_ext(), False))
        omp = os.path.join(lib_dir, mfb.cpu_library_name(mfb.FLOOD_SPEC, mfb.library_ext(), True))
        for path in (serial, omp):
            if os.path.exists(path):
                raise Skip("a real prebuilt library is present")
        shutil.copy(built, serial)
        made.append(serial)
        with open(omp, "wb") as f:                                # does not open: skipped
            f.write(b"not a library")
        made.append(omp)
        os.environ["DAMSAFETY_MF_OPENMP"] = "1"
        assert mfb.build_cpu_library(mfb.FLOOD_SPEC) == serial
        assert mfb.prebuilt_available(mfb.FLOOD_SPEC)
        bk = mfb.CpuBackend(mfb.FLOOD_SPEC)
        assert bk.lib is not None
        bk.close()
    finally:
        for path in made:
            if os.path.exists(path):
                os.remove(path)
        if made_root:
            shutil.rmtree(mfb.PREBUILT_DIR, True)
        elif made_dir:
            shutil.rmtree(lib_dir, True)
        shutil.rmtree(d, True)
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v



def _fv2_wetdry_case(ny=30, nx=48, dx=10.0):
    """A dam break onto a dry, rough 1 % slope with ridges and a pit, every edge closed."""
    rng = np.random.default_rng(1)
    xx = np.mgrid[0:ny, 0:nx][1].astype(float)
    z = 0.01 * dx * xx + rng.uniform(0.0, 0.6, (ny, nx))     # rising east, bumps and pits
    z[::7, 24:] += 1.5                                        # ridges across the slope
    z[5:9, 32:36] -= 2.0                                      # a pit
    h0 = np.zeros((ny, nx))
    h0[:, :10] = np.maximum(4.0 + z[:, :10].min() - z[:, :10], 0.0)   # the reservoir
    return z, h0, dx


@test
def metalflood_fv2_wet_dry_front_conserves_mass():
    """Runner 3.83.0 regression for the Runner 3.66.0 FV2 fix (#492; Absaraka DWR-2801, FV2 ended 58 %
    over the injected volume with Qout 0): a dam break onto a dry rough slope in a closed box must
    keep its volume to round-off, with no depth clip and no collapse of the time step. Before 3.66.0
    FV2 reconstructed the DISCHARGE hu and divided it by the reconstructed edge depth; on a thin edge
    that velocity was unbounded (tens of km/s on a 0.3 m cell here), dt fell below 1e-4 s and the
    depth >= 0 clip created water (+4.4 % by 217 s). The step budget makes that code fail rather
    than hang."""
    _need_cpu_build()
    d = _tmp()
    try:
        z, h0, dx = _fv2_wetdry_case()
        sim = 900.0
        p = _fv_deck(os.path.join(d, "wd"), z, h0, dx=dx, sim=sim, n=0.03, free="")
        deck = fve.Deck(p)
        eng = fve.Engine(deck, mfb.CpuBackend(mfb.FV2_SPEC))
        vol0 = float(eng.h2().sum(dtype=np.float64)) * dx * dx
        t, clip, min_dt, batches = 0.0, 0.0, 1e9, 0
        while t < sim - 1e-3 and batches < 120:          # ~20 batches of 64 steps are needed
            out = eng.batch(t, sim, 64, True)
            t += out["t_local"]
            clip += out["clip"]
            min_dt = min(min_dt, out["min_dt"])
            batches += 1
        vol = float(eng.h2().sum(dtype=np.float64)) * dx * dx
        err = (vol - vol0) / vol0
        assert t >= sim - 1e-3, "FV2 did not reach {:.0f} s in {} batches (t {:.1f} s, min dt "\
            "{:.2e} s, volume {:+.3%})".format(sim, batches, t, min_dt, err)
        assert abs(err) < 1e-5, "FV2 volume changed {:+.4%} over a closed wet/dry run".format(err)
        assert clip < 1e-6 * vol0, "the depth clip added {:.3g} m3".format(clip)
        assert min_dt > 0.05, "dt collapsed to {:.2e} s".format(min_dt)
        h = eng.h2()
        hu = eng.a["hu"].reshape(h.shape)
        hv = eng.a["hv"].reshape(h.shape)
        wet = h > 1e-3
        spd = np.hypot(hu, hv)[wet] / h[wet]
        assert spd.max() < 15.0, "unphysical speed {:.1f} m/s".format(spd.max())
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_fv2_has_no_velocity_spikes():
    """Runner 3.66.0: FV2 reconstructed the discharges and divided them by the reconstructed face
    depth eta_face - z, a few mm on a steep bank under a full cell's discharge. On the valley deck
    that made face velocities of 20-30 m/s and momentum spikes in 0.5-2 m of water (maxVc 30.4 m/s,
    43 cells over 5 m/s, where FV1 has 7.8 m/s and 1 cell), and the answer moved by up to 15 cm when
    only the last bit of rounding changed (FMA on, or the Apple GPU against the CPU build). FV2 now
    reconstructs velocities, so a face velocity lies between its cells' velocities: its fastest
    cell is within a quarter of FV1's and its depths stay close to FV1's."""
    _need_cpu_build()
    d = _tmp()
    try:
        v, h = {}, {}
        for order in (1, 2):
            deck = valley_deck(os.path.join(d, "o{}".format(order)))
            fve.run_par(deck, backend="cpu", order=order)
            v[order] = _res(os.path.join(d, "o{}".format(order)), "maxVc")
            h[order] = _res(os.path.join(d, "o{}".format(order)), "max")
        m = (v[1] != bl.NODATA) & (v[2] != bl.NODATA)
        assert v[2][m].max() <= 1.25 * v[1][m].max(), (v[2][m].max(), v[1][m].max())
        assert (v[2][m] > 5.0).sum() <= (v[1][m] > 5.0).sum() + 2, ((v[2][m] > 5.0).sum(),
                                                                     (v[1][m] > 5.0).sum())
        wet = m & (h[1] > 0.01)
        assert np.abs(h[2] - h[1])[wet].mean() < 0.05, np.abs(h[2] - h[1])[wet].mean()
    finally:
        shutil.rmtree(d, True)


@test
def metalflood_dg2_is_insensitive_to_fma_rounding():
    """Runner 3.67.0: DG2 reconstructed the edge/Gauss discharge and divided it by the reconstructed
    point depth eta_point - z, a few mm on a steep bank, so the velocity swung with the last bit of
    float32 rounding. On the Metal GPU vs the CPU build that was a ~1 cm max-depth gap (Nick's M2 Pro,
    2026-11-23); the same cause as the FV2 fix (Runner 3.66.0). DG2 now reconstructs the velocity as a
    primitive with the cell-mean depth as the denominator. As a cloud proxy for the GPU-vs-CPU gap
    (Metal fuses multiply-adds, the CPU build does not), build the DG2 kernels twice - once with the
    default -ffp-contract=off, once with FMA contraction on - and run the valley deck with each: the
    maximum depth must not move. On 3.66.0 it moved ~1 mm; it is now bit-identical."""
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")
    # Repo audit 2026-11-23: an x86-64 proxy only. On the macOS arm64 runner (where this file first
    # ran it) -mfma does not exist and clang's -ffp-contract=fast fusing moved one cell's written
    # max depth by one 1 mm output step; there the real check is the Metal GPU against the CPU
    # build, metalflood_dg2_gpu_matches_cpu_build_and_repeats_exactly.
    if not mfb.platform_tag().endswith("-x64"):
        raise Skip("the FMA proxy is for x86-64; on {} see the DG2 GPU-vs-CPU test".format(mfb.platform_tag()))
    d = _tmp()
    saved = mfb.CPU_FLAGS
    try:
        runs = {}
        fma = ("-O3", "-std=c++14", "-fno-strict-aliasing", "-ffp-contract=fast", "-mfma")
        for tag, flags in (("off", saved), ("fma", fma)):
            mfb.CPU_FLAGS = flags
            deck = valley_deck(os.path.join(d, tag), sim_s=10800.0)
            fve.run_par(deck, backend="cpu", order=3)
            runs[tag] = _res(os.path.join(d, tag), "max")
        a, b = runs["off"], runs["fma"]
        m = (a != bl.NODATA) & (b != bl.NODATA)
        assert np.abs(a - b)[m].max() <= 1e-4, np.abs(a - b)[m].max()
    finally:
        mfb.CPU_FLAGS = saved
        shutil.rmtree(d, True)



def _nodata_lake_deck(folder):
    """A 1 m lake at rest on a flat 20 x 20 grid (10 m cells) with a NoData column and a NoData
    block inside it: every NoData face is a wall the water rests against."""
    os.makedirs(folder, exist_ok=True)
    z = np.zeros((20, 20))
    z[:, 10] = bl.NODATA
    z[5:8, 3:6] = bl.NODATA
    h = np.where(z == bl.NODATA, 0.0, 1.0)
    bl._write_asc(os.path.join(folder, "dem.asc"), z, 0.0, 0.0, 10.0)
    bl._write_asc(os.path.join(folder, "start.asc"), h, 0.0, 0.0, 10.0)
    p = os.path.join(folder, "run.par")
    with open(p, "w") as f:
        f.write("DEMfile dem.asc\nstartfile start.asc\nfpfric 0.03\nsim_time 600\n"
                "initial_tstep 1\nsaveint 1000\nmassint 60\n")
    return p


@test
def metalflood_shock_solvers_hold_a_lake_against_nodata():
    """Runner 3.93.0: the shock-capturing solvers (FV1, FV2, DG2) gave a face between a wet cell
    and a NoData cell no flux at all - no wall pressure - while the cell's other faces and the
    bed source still pushed. Water resting against NoData was driven into it forever: a 1 m lake
    beside a NoData strip reached 5 m/s and 0.5-3.4 m depths in 10 minutes, and EA benchmark
    test 5 at 10 m (NoData around the valley) ran at 0.008 s steps with water at 500 m/s and an 11%
    volume gain. A NoData face is now a reflecting wall, as the domain edge already was (and as
    ACC and LISFLOOD-FP treat it): the lake stays at rest."""
    _need_cpu_build()
    from nd_breach_runner.core.metalflood import engine_dg2 as dge
    d = _tmp()
    try:
        runs = (("fv1", lambda p: fve.run_par(p, backend="cpu", order=1)),
                ("fv2", lambda p: fve.run_par(p, backend="cpu", order=2)),
                ("dg2", lambda p: dge.run_par(p, backend="cpu")))
        for name, run in runs:
            sub = os.path.join(d, name)
            run(_nodata_lake_deck(sub))
            h = _res(sub, "max")
            v = _res(sub, "maxVc")
            wet = h != bl.NODATA
            assert np.abs(h[wet] - 1.0).max() < 2e-3, (name, h[wet].min(), h[wet].max())
            assert v[wet].max() < 1e-3, (name, v[wet].max())
    finally:
        shutil.rmtree(d, True)

if __name__ == "__main__":
    # Repo audit 2026-11-23: every test this module registers, in file order. The hand-kept list
    # this replaced sat in the middle of the file, so the tests defined below it never ran here
    # (the macOS Metal CI job runs this file), and it had left out
    # metalflood_fv2_gpu_matches_cpu_build_and_repeats_exactly, the one GPU-vs-CPU check of FV2.
    from nd_breach_runner.tests.registry import MODEL_TESTS, TESTS
    fails = 0
    for fn in [f for f in TESTS + MODEL_TESTS if f.__module__ == __name__]:
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
