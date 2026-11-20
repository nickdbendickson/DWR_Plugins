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
    if os.name == "nt":
        raise Skip("the engine's CPU build is not made on Windows")
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


if __name__ == "__main__":
    fails = 0
    for fn in (metalflood_cpu_build_matches_builtin_engine, metalflood_conserves_water,
               metalflood_wet_window_does_not_change_the_answer,
               metalflood_reads_lisflood_acc_options, metalflood_params_layout_matches_kernels,
               metalflood_gpu_matches_cpu_build, metalflood_deckio_reads_as_builtin_engine,
               gpu_engine_is_a_runner_engine_choice, metalflood_matches_lisflood_fp,
               metalflood_diagnostic_cases_match_serial_oracle,
               metalflood_diagnostic_cases_run,
               metalflood_fv_lake_at_rest_is_well_balanced,
               metalflood_fv_reproduces_the_ritter_dam_break, metalflood_fv_conserves_water,
               metalflood_fv2_lake_at_rest_is_well_balanced,
               metalflood_fv2_reproduces_the_ritter_dam_break, metalflood_fv2_conserves_water,
               fv_engine_is_a_runner_engine_choice,
               fv2_engine_is_a_runner_engine_choice):
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
