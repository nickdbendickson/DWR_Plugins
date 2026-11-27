"""Boann in-terrain breach: a notch lowered into the bed between batches.

With no breach file every existing result is unchanged. The notch is opt-in, host-side, and
the bed only falls. SGC and the sub-grid solvers refuse a breach file.

    python3 -m nd_breach_runner.tests.test_boann_terrain_breach
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood import engine_fv
from nd_breach_runner.core.metalflood.cases import level_pool_lake, read_asc, write_asc
from nd_breach_runner.core.metalflood.deckio import NODATA
from nd_breach_runner.tests.registry import Skip, test


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


def _spec(cells, **k):
    spec = {
        "version": 1,
        "start_s": 0.0,
        "formation_s": 20.0,
        "invert_start_m": 12.0,
        "invert_final_m": 10.0,
        "bottom_width_final_m": 30.0,
        "side_slope_h": 1.0,
        "update_s": 10.0,
        "cells": cells,
    }
    spec.update(k)
    return spec


def _write_breach(folder, spec, pool=None):
    if pool is not None:
        write_asc(os.path.join(folder, "run.pool.asc"), pool, 0.0, 0.0, 10.0, fmt="%.0f")
        spec = dict(spec)
        spec["pool_mask"] = "run.pool.asc"
    with open(os.path.join(folder, "run.breach.json"), "w", encoding="utf-8") as f:
        json.dump(spec, f)
    par = os.path.join(folder, "run.par")
    with open(par, "a", encoding="utf-8") as f:
        f.write("breachfile run.breach.json\n")
    return par


def _ridge_deck(folder, sim_s=40.0, h0=1.5):
    """Closed 12 x 16 basin. A ridge at column 8 holds water on the west. Start depth on the west."""
    os.makedirs(folder, exist_ok=True)
    ny, nx, dx = 12, 16, 10.0
    z = np.full((ny, nx), 10.0)
    z[:, 8] = 12.0
    z[0, :] = z[-1, :] = z[:, 0] = z[:, -1] = 14.0
    write_asc(os.path.join(folder, "run.dem.asc"), z, 0.0, 0.0, dx, fmt="%.6f")
    h = np.zeros_like(z)
    h[1:-1, 1:8] = h0
    write_asc(os.path.join(folder, "run.start"), h, 0.0, 0.0, dx, fmt="%.6f")
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim_s), "initial_tstep 1.0",
             "massint 10.0", "saveint {:.1f}".format(sim_s + 10.0),
             "fpfric 0.04", "startfile run.start", "acceleration"]
    with open(os.path.join(folder, "run.par"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    cells = [[r, 8, (r - (ny // 2)) * dx] for r in range(1, ny - 1)]
    pool = np.zeros_like(z)
    pool[1:-1, 1:8] = 1.0
    return z, cells, pool


_SOLVERS = (("acc", lambda par: mfe.run_par(par, backend="cpu")),
            ("fv1", lambda par: engine_fv.run_par(par, backend="cpu", order=1)),
            ("fv2", lambda par: engine_fv.run_par(par, backend="cpu", order=2)),
            ("dg2", lambda par: engine_fv.run_par(par, backend="cpu", order=3)))


def _files(folder):
    res = os.path.join(folder, "results")
    out = {}
    for name in sorted(os.listdir(res)):
        with open(os.path.join(res, name), "rb") as f:
            out[name] = f.read()
    return out


@test
def boann_breach_law_only_falls_and_stops_at_the_final_invert():
    """At the centre the invert falls linearly; off the bottom the sides climb; the bed never
    rises and never goes below the final invert. A vertical side cuts only inside the bottom."""
    d = tempfile.mkdtemp(prefix="boann_br_law_")
    try:
        _ridge_deck(d, sim_s=1.0, h0=0.0)
        cells = [[4, 8, 0.0], [5, 8, 10.0], [6, 8, 40.0]]
        _write_breach(d, _spec(cells, formation_s=20.0, update_s=10.0, side_slope_h=1.0,
                               invert_start_m=12.0, invert_final_m=10.0, bottom_width_final_m=20.0))
        deck = mfe.Deck(os.path.join(d, "run.par"))
        br = deck.breach
        z0 = br.z0
        mid = br.candidate(10.0)          # frac 0.5: invert 11, half 5
        assert abs(float(mid[0]) - 11.0) < 1e-9
        assert abs(float(mid[1]) - 16.0) < 1e-9   # 11 + (10-5)/1
        full = np.minimum(z0, br.candidate(20.0))
        assert float(full.min()) >= 10.0 - 1e-9
        assert np.all(full <= z0 + 1e-12)
        vert = _spec(cells, side_slope_h=0.0, bottom_width_final_m=20.0, formation_s=0.0)
        # rebuild
    finally:
        shutil.rmtree(d, True)
    d = tempfile.mkdtemp(prefix="boann_br_vert_")
    try:
        _ridge_deck(d, sim_s=1.0, h0=0.0)
        cells = [[5, 8, 0.0], [6, 8, 15.0]]
        _write_breach(d, _spec(cells, side_slope_h=0.0, bottom_width_final_m=20.0,
                               formation_s=0.0, invert_start_m=12.0, invert_final_m=10.0))
        br = mfe.Deck(os.path.join(d, "run.par")).breach
        cand = br.candidate(0.0)
        assert abs(float(cand[0]) - 10.0) < 1e-9          # |a|=0 <= half 10, at the final invert
        assert np.isposinf(float(cand[1]))                # |a|=15, no cut
    finally:
        shutil.rmtree(d, True)


@test
def boann_breach_file_is_refused_when_it_is_malformed_or_the_solver_cannot_use_it():
    d = tempfile.mkdtemp(prefix="boann_br_bad_")
    try:
        _ridge_deck(d)
        _write_breach(d, _spec([[1, 8, 0.0]], formation_s=-1.0))
        try:
            mfe.Deck(os.path.join(d, "run.par"))
        except mfe.EngineError as e:
            assert "formation_s" in str(e)
        else:
            raise AssertionError("a negative formation time was accepted")
        # outside the grid
        os.remove(os.path.join(d, "run.breach.json"))
        with open(os.path.join(d, "run.par"), encoding="utf-8") as f:
            txt = f.read().replace("breachfile run.breach.json\n", "")
        with open(os.path.join(d, "run.par"), "w", encoding="utf-8") as f:
            f.write(txt)
        _write_breach(d, _spec([[1, 80, 0.0]]))
        try:
            mfe.Deck(os.path.join(d, "run.par"))
        except mfe.EngineError as e:
            assert "outside" in str(e)
        else:
            raise AssertionError("a cell off the grid was accepted")
        # numbers that are not finite: a NaN invert and an infinite along-crest distance
        for bad, word in ((_spec([[1, 8, 0.0]], invert_final_m=float("nan")), "finite"),
                          (_spec([[1, 8, float("inf")]]), "finite")):
            sub = tempfile.mkdtemp(dir=d)
            _ridge_deck(sub)
            with open(os.path.join(sub, "run.breach.json"), "w", encoding="utf-8") as f:
                f.write(json.dumps(bad, allow_nan=True))
            with open(os.path.join(sub, "run.par"), "a", encoding="utf-8") as f:
                f.write("breachfile run.breach.json\n")
            try:
                mfe.Deck(os.path.join(sub, "run.par"))
            except mfe.EngineError as e:
                assert word in str(e), str(e)
            else:
                raise AssertionError("a breach file with {} was accepted".format(bad))
        # a pool mask with rainfall: the pool outflow would not count the rain on the pool
        sub = tempfile.mkdtemp(dir=d)
        _z, cells, pool = _ridge_deck(sub)
        with open(os.path.join(sub, "run.rain"), "w", encoding="utf-8") as f:
            f.write("rain\n2 seconds\n10.0 0.0\n10.0 100.0\n")
        with open(os.path.join(sub, "run.par"), "a", encoding="utf-8") as f:
            f.write("rainfall run.rain\n")
        _write_breach(sub, _spec(cells), pool)
        try:
            mfe.Deck(os.path.join(sub, "run.par"))
        except mfe.EngineError as e:
            assert "rainfall" in str(e), str(e)
        else:
            raise AssertionError("a pool mask with rainfall was accepted")
        sub = tempfile.mkdtemp(dir=d)                       # the same deck with no pool mask runs
        _z, cells, _pool = _ridge_deck(sub)
        with open(os.path.join(sub, "run.rain"), "w", encoding="utf-8") as f:
            f.write("rain\n2 seconds\n10.0 0.0\n10.0 100.0\n")
        with open(os.path.join(sub, "run.par"), "a", encoding="utf-8") as f:
            f.write("rainfall run.rain\n")
        _write_breach(sub, _spec(cells))
        assert mfe.Deck(os.path.join(sub, "run.par")).rain is not None
        folder = os.path.join(d, "sgc")
        _ridge_deck(folder, sim_s=5.0, h0=0.0)
        _write_breach(folder, _spec([[1, 8, 0.0]], formation_s=0.0, update_s=1.0))
        try:
            cli.run(os.path.join(folder, "run.par"), solver="sgc", backend="cpu", quiet=True)
        except Exception as e:
            assert "breachfile" in str(e)
        else:
            raise AssertionError("SGC ran a breach deck")
        try:
            cli.run(os.path.join(folder, "run.par"), solver="subfv", backend="cpu", quiet=True)
        except Exception as e:
            assert "breachfile" in str(e)
        else:
            raise AssertionError("sub-grid FV ran a breach deck")
        try:
            cli.run(os.path.join(folder, "run.par"), solver="subli", backend="cpu", quiet=True)
        except Exception as e:
            assert "breachfile" in str(e)
        else:
            raise AssertionError("sub-grid local-inertial ran a breach deck")
    finally:
        shutil.rmtree(d, True)


@test
def boann_no_breach_file_matches_main_byte_for_byte():
    """A deck with no breach file keeps today's batches and today's result bytes."""
    _need_cpu()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    probe = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "origin/main"],
                           capture_output=True, text=True)
    if probe.returncode != 0:
        raise Skip("no git origin/main to compare against")
    wt = tempfile.mkdtemp(prefix="boann_br_main_")
    out = tempfile.mkdtemp(prefix="boann_br_cmp_")
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
from nd_breach_runner.core.metalflood.cases import level_pool_lake
for solver in ("acc", "fv"):
    folder = os.path.join(dest, solver)
    par = level_pool_lake(folder, n=14, sim_s=30.0, free="", inflow_cms=0.0, h0=0.4)
    stats = cli.run(par, solver=solver, backend="cpu", quiet=True)
    open(os.path.join(folder, "batches.txt"), "w").write(str(stats["batches"]))
print("old-ok")
"""
        old = os.path.join(out, "old")
        os.makedirs(old)
        run = subprocess.run([sys.executable, "-c", script, wt, old],
                             capture_output=True, text=True,
                             env=dict(os.environ, PYTHONPATH=wt, DAMSAFETY_MF_THREADS="1"))
        if run.returncode != 0 or "old-ok" not in run.stdout:
            raise AssertionError("origin/main run failed:\n{}\n{}".format(run.stdout, run.stderr))
        with _env(DAMSAFETY_MF_THREADS="1"):
            for solver in ("acc", "fv"):
                folder = os.path.join(out, "new", solver)
                par = level_pool_lake(folder, n=14, sim_s=30.0, free="", inflow_cms=0.0, h0=0.4)
                stats = cli.run(par, solver=solver, backend="cpu", quiet=True)
                assert "breach" not in stats
                old_b = open(os.path.join(old, solver, "batches.txt")).read().strip()
                assert str(stats["batches"]) == old_b, (solver, stats["batches"], old_b)
                for name in ("run.max", "run.maxVc", "run.mass", "run.mxe"):
                    with open(os.path.join(old, solver, "results", name), "rb") as f:
                        a = f.read()
                    with open(os.path.join(folder, "results", name), "rb") as f:
                        b = f.read()
                    assert a == b, (solver, name)
                assert not os.path.exists(os.path.join(folder, "results", "run.bed"))
    finally:
        subprocess.run(["git", "-C", root, "worktree", "remove", "--force", wt],
                       capture_output=True)
        shutil.rmtree(wt, True)
        shutil.rmtree(out, True)


@test
def boann_instant_breach_matches_a_prelowered_bed():
    """formation_s = 0 at the start is the same run as a DEM that already has the notch."""
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_br_inst_")
    try:
        raw = os.path.join(d, "raw")
        z, cells, pool = _ridge_deck(raw, sim_s=25.0, h0=1.5)
        spec = _spec(cells, start_s=0.0, formation_s=0.0, update_s=5.0,
                     invert_start_m=12.0, invert_final_m=10.2, bottom_width_final_m=40.0,
                     side_slope_h=0.5)
        _write_breach(raw, spec, pool)
        deck = mfe.Deck(os.path.join(raw, "run.par"))
        lowered = np.array(deck.z, dtype=np.float64)
        br = deck.breach
        flat = lowered.ravel()
        flat[br.idx] = np.minimum(br.z0, br.candidate(0.0))
        pre = os.path.join(d, "pre")
        os.makedirs(pre)
        # same reference rule: the lowered bed's minimum is the breach run's zref when the notch
        # is the lowest ground, so write the notch and do not attach a breach file
        write_asc(os.path.join(pre, "run.dem.asc"), lowered, 0.0, 0.0, 10.0, fmt="%.17g")
        h = np.zeros_like(z)
        h[1:-1, 1:8] = 1.5
        write_asc(os.path.join(pre, "run.start"), h, 0.0, 0.0, 10.0, fmt="%.6f")
        with open(os.path.join(pre, "run.par"), "w", encoding="utf-8") as f:
            f.write("DEMfile run.dem.asc\nresroot run\ndirroot results\nsim_time 25.0\n"
                    "initial_tstep 1.0\nmassint 10.0\nsaveint 35.0\nfpfric 0.04\n"
                    "startfile run.start\nacceleration\n")
        with _env(DAMSAFETY_MF_THREADS="1"):
            st_b = mfe.run_par(os.path.join(raw, "run.par"), backend="cpu")
            st_p = mfe.run_par(os.path.join(pre, "run.par"), backend="cpu")
        assert st_b["batches"] == st_p["batches"]
        for name in ("run.max", "run.maxVc", "run.maxDV", "run.inittm", "run.maxtm", "run.mxe",
                     "run.mass"):
            with open(os.path.join(raw, "results", name), "rb") as f:
                a = f.read()
            with open(os.path.join(pre, "results", name), "rb") as f:
                b = f.read()
            assert a == b, name
        assert os.path.isfile(os.path.join(raw, "results", "run.bed"))
        assert os.path.isfile(os.path.join(raw, "results", "run.pool"))
        assert not os.path.isfile(os.path.join(pre, "results", "run.bed"))
        bed = read_asc(os.path.join(raw, "results", "run.bed"))
        expect = np.minimum(deck.z, deck.z)
        expect.ravel()[br.idx] = np.minimum(br.z0, br.candidate(br.start_s + br.formation_s))
        m = bed != NODATA
        assert np.max(np.abs(bed[m] - expect[m])) <= 1e-4
        assert np.all(bed[m] <= deck.z[m] + 1e-4)
    finally:
        shutil.rmtree(d, True)


@test
def boann_breach_bed_at_halfway_is_the_law_and_mass_holds():
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_br_half_")
    try:
        z, cells, pool = _ridge_deck(d, sim_s=15.0, h0=1.5)
        # formation 20, update 10: at sim 15 the last applied time is 10 (halfway of 20)
        spec = _spec(cells, formation_s=20.0, update_s=10.0, start_s=0.0)
        _write_breach(d, spec, pool)
        with _env(DAMSAFETY_MF_THREADS="1", DAMSAFETY_MF_SKIP="0"):
            stats = mfe.run_par(os.path.join(d, "run.par"), backend="cpu")
        deck = mfe.Deck(os.path.join(d, "run.par"))
        bed = read_asc(os.path.join(d, "results", "run.bed"))
        expect = np.array(deck.z, dtype=np.float64)
        expect.ravel()[deck.breach.idx] = np.minimum(deck.breach.z0, deck.breach.candidate(10.0))
        m = bed != NODATA
        assert np.max(np.abs(bed[m] - expect[m])) <= 1e-4
        assert np.all(bed[m] <= deck.z[m] + 1e-9)
        vol0 = float(np.sum(deck.h0) * deck.dx ** 2)
        assert abs(stats["mass_error_m3"]) < 1e-5 * max(vol0, 1.0)
        assert stats["clipped_m3"] < 1e-6 * max(vol0, 1.0)
        assert stats["breach"]["updates"] >= 2
        # two one-core runs, and one core against four, match
        with _env(DAMSAFETY_MF_THREADS="1"):
            mfe.run_par(os.path.join(d, "run.par"), backend="cpu")
            a = _files(d)
        d2 = os.path.join(d, "b")
        shutil.copytree(d, d2, ignore=shutil.ignore_patterns("results", "b"))
        with _env(DAMSAFETY_MF_THREADS="1"):
            mfe.run_par(os.path.join(d2, "run.par"), backend="cpu")
        b = _files(d2)
        for name in a:
            if name.startswith("run."):
                assert a[name] == b[name], name
        d3 = os.path.join(d, "omp")
        shutil.copytree(d, d3, ignore=shutil.ignore_patterns("results", "b", "omp"))
        with _env(DAMSAFETY_MF_THREADS="4"):
            mfe.run_par(os.path.join(d3, "run.par"), backend="cpu")
        c = _files(d3)
        for name in ("run.max", "run.maxVc", "run.bed", "run.mxe", "run.mass"):
            assert a[name] == c[name], name
    finally:
        shutil.rmtree(d, True)


@test
def boann_mxe_is_the_peak_water_level_over_the_lowering_bed():
    """.mxe on a breach cell is max over time of (depth + bed). Three cells, one cut at 10 s:
    A stands 2.5 m over the intact crest (14.5) and is deeper but lower after the cut (3.0 m
    on 10 = 13.0): 14.5. B is dry before the cut and 0.4 m deep after: 10.4, not the intact 12.
    C is 1.0 m deep before (13.0) and rises to 3.5 m after (13.5): 13.5. "Peak depth plus the
    bed of the period it rose in" gives 13.0 at A, too low."""
    ny, nx = 4, 4

    class Deck:
        pass

    deck = Deck()
    deck.ny, deck.nx, deck.dx = ny, nx, 10.0
    deck.valid = np.ones((ny, nx), dtype=bool)
    deck.z = np.full((ny, nx), 12.0)
    spec = {"start_s": 10.0, "formation_s": 0.0, "invert_start_m": 12.0,
            "invert_final_m": 10.0, "bottom_width_final_m": 40.0, "side_slope_h": 0.0,
            "update_s": 5.0}
    cells = [[1, 1, 0.0], [1, 2, 0.0], [1, 3, 0.0]]
    br = mfe.TerrainBreach(deck, spec, cells, None)
    ia, ib, ic = 1 * nx + 1, 1 * nx + 2, 1 * nx + 3

    class Eng:
        zref = 0.0
        a = {"hmax": np.zeros(ny * nx, np.float32), "z": np.full(ny * nx, 12.0, np.float32)}

    eng = Eng()
    eng.a["hmax"][ia] = 2.5
    eng.a["hmax"][ic] = 1.0
    br.apply(eng)                      # the cut at 10 s
    assert np.all(br.zcur == 10.0), br.zcur
    eng.a["hmax"][ia] = 3.0            # deeper after the cut, but lower
    eng.a["hmax"][ib] = 0.4
    eng.a["hmax"][ic] = 3.5
    mxe = br.mxe(eng.a["hmax"])
    assert abs(float(mxe[1, 1]) - 14.5) < 1e-6, mxe[1, 1]
    assert abs(float(mxe[1, 2]) - 10.4) < 1e-6, mxe[1, 2]
    assert abs(float(mxe[1, 3]) - 13.5) < 1e-6, mxe[1, 3]
    # off the notch: bed + peak depth, as with no breach file
    assert abs(float(mxe[0, 0]) - 12.0) < 1e-9


@test
def boann_mxe_matches_the_sampled_peak_water_level_in_a_full_run():
    """A full run on every solver that lowers the bed (ACC, FV1, FV2, DG2; ridge deck, 2.5 m
    start depth, notch forming over 10-30 s): .mxe on every breach cell is at least the
    highest depth + bed seen after any batch and no higher than the peak depth over the
    intact crest. Some cells must stand higher than the peak depth over the final bed, so a
    writer that adds the peak depth to one bed (final or intact) fails here."""
    _need_cpu()
    orig = mfe.TerrainBreach.catch_up
    for name, run in _SOLVERS:
        d = tempfile.mkdtemp(prefix="boann_br_mxe_{}_".format(name))
        seen = {}

        def sample(br, eng):
            h = np.asarray(eng.h2(), dtype=np.float64).ravel()[br.idx]
            w = np.where(h > 0, h + br.zcur, -np.inf)
            seen["wse"] = np.maximum(seen.get("wse", w), w)

        def spy(br, eng, t):
            sample(br, eng)
            orig(br, eng, t)
            sample(br, eng)

        try:
            _z, cells, pool = _ridge_deck(d, sim_s=60.0, h0=2.5)
            _write_breach(d, _spec(cells, start_s=10.0, formation_s=20.0, update_s=5.0), pool)
            mfe.TerrainBreach.catch_up = spy
            try:
                with _env(DAMSAFETY_MF_THREADS="1"):
                    run(os.path.join(d, "run.par"))
            finally:
                mfe.TerrainBreach.catch_up = orig
            deck = mfe.Deck(os.path.join(d, "run.par"))
            br = deck.breach
            mxe = read_asc(os.path.join(d, "results", "run.mxe")).ravel()[br.idx]
            hmax = read_asc(os.path.join(d, "results", "run.max")).ravel()[br.idx]
            bed = read_asc(os.path.join(d, "results", "run.bed")).ravel()[br.idx]
            wse = seen["wse"]
            assert np.isfinite(wse).all(), (name, wse)
            assert np.all(mxe >= wse - 1e-3), (name, mxe, wse)
            assert np.all(mxe <= hmax + br.z0 + 1e-3), (name, mxe, hmax + br.z0)
            # the case this guards: the final bed under the peak depth is well short...
            assert float(np.max(wse - (hmax + bed))) > 0.1, (name, wse, hmax + bed)
            # ...and the intact bed overstates where the water peaked after the cut
            assert float(np.max((hmax + br.z0) - mxe)) > 0.1, (name, mxe, hmax + br.z0)
        finally:
            shutil.rmtree(d, True)


def _pool_deck(folder):
    """The ridge deck with a 5 m3/s point source inside the pool, a start step and an update
    interval that do not line up with the mass interval (the old pool rows went backwards)."""
    z, cells, pool = _ridge_deck(folder, sim_s=45.0, h0=1.5)
    par = os.path.join(folder, "run.par")
    with open(par, encoding="utf-8") as f:
        text = f.read()
    text = text.replace("initial_tstep 1.0", "initial_tstep 0.37").replace("massint 10.0",
                                                                          "massint 5.0")
    text += "bcifile run.bci\nbdyfile run.bdy\n"
    with open(par, "w", encoding="utf-8") as f:
        f.write(text)
    with open(os.path.join(folder, "run.bci"), "w", encoding="utf-8") as f:
        f.write("P 35.000 65.000 QVAR inflow\n")
    with open(os.path.join(folder, "run.bdy"), "w", encoding="utf-8") as f:
        f.write("pool inflow\ninflow\n2 seconds\n0.5 0.0\n0.5 1000.0\n")
    return z, cells, pool


@test
def boann_pool_times_increase_and_match_the_outflow():
    """On ACC, FV1, FV2 and DG2: the pool file's time column only moves forward, every row is
    at a time the model actually reached (a bed update, a mass row or the end; never the
    nominal update time), and the outflow it reports, integrated between mass rows, is the
    volume that left the pool: the rise in the water outside the pool, from the .mass file's
    total volume (closed basin, all inflow into the pool). Stamping rows with the nominal
    update time, or dropping the inflow, fails this deck."""
    _need_cpu()
    orig = mfe.TerrainBreach.catch_up
    for name, run in _SOLVERS:
        d = tempfile.mkdtemp(prefix="boann_br_pool_{}_".format(name))
        reached = [0.0]

        def spy(br, eng, t):
            reached.append(float(t))
            orig(br, eng, t)

        try:
            _z, cells, pool = _pool_deck(d)
            _write_breach(d, _spec(cells, start_s=1.1, formation_s=20.0, update_s=3.0), pool)
            mfe.TerrainBreach.catch_up = spy
            try:
                with _env(DAMSAFETY_MF_THREADS="1"):
                    stats = run(os.path.join(d, "run.par"))
            finally:
                mfe.TerrainBreach.catch_up = orig
            rows = []
            for ln in open(os.path.join(d, "results", "run.pool"), encoding="utf-8"):
                if not ln.strip() or ln.startswith("time"):
                    continue
                rows.append(tuple(float(x) for x in ln.split(",")))
            assert len(rows) >= 8, (name, rows)
            assert rows[0][0] == 0.0 and rows[0][2] == 0.0
            for a, b in zip(rows, rows[1:]):
                assert b[0] > a[0] + 1e-9, (name, a[0], b[0])
            mass = {}
            for ln in open(os.path.join(d, "results", "run.mass"), encoding="utf-8"):
                parts = ln.split()
                if not parts or parts[0] == "Time":
                    continue
                mass[round(float(parts[0]), 3)] = float(parts[5])
            for t, _v, _q in rows:
                assert (min(abs(t - r) for r in reached) <= 1e-5
                        or min(abs(t - m) for m in mass) <= 1e-3), (name, t)
            vol0 = float(np.sum(mfe.Deck(os.path.join(d, "run.par")).h0)) * 100.0
            pool_at = {round(t, 3): (t, v) for t, v, _q in rows}
            common = sorted(k for k in mass if k in pool_at)
            assert len(common) >= 5, (name, sorted(mass), sorted(pool_at))
            prev = (0.0, rows[0][1], vol0)                  # t, pool volume, total volume
            for k in common:
                t, vp = pool_at[k]
                vt = mass[k]
                out_q = sum(q * (tb - ta) for (ta, _va, _qa), (tb, _vb, q) in zip(rows, rows[1:])
                            if prev[0] + 1e-9 < tb <= t + 1e-9)
                outside_rise = (vt - vp) - (prev[2] - prev[1])
                assert abs(out_q - outside_rise) <= 0.5 + 1e-3 * abs(outside_rise), (
                    name, k, out_q, outside_rise)
                prev = (t, vp, vt)
            assert stats["breach"]["updates"] >= 6
        finally:
            shutil.rmtree(d, True)


@test
def boann_fv_breach_conserves_mass_on_a_closed_basin():
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_br_fv_")
    try:
        z, cells, pool = _ridge_deck(d, sim_s=20.0, h0=1.5)
        _write_breach(d, _spec(cells, formation_s=10.0, update_s=5.0))
        with _env(DAMSAFETY_MF_THREADS="1"):
            stats = engine_fv.run_par(os.path.join(d, "run.par"), backend="cpu")
        deck = mfe.Deck(os.path.join(d, "run.par"))
        vol0 = float(np.sum(deck.h0) * deck.dx ** 2)
        assert abs(stats["mass_error_m3"]) < 1e-4 * max(vol0, 1.0), stats["mass_error_m3"]
        assert os.path.isfile(os.path.join(d, "results", "run.bed"))
        assert stats["breach"]["updates"] >= 1
    finally:
        shutil.rmtree(d, True)


@test
def boann_breach_cpu_tiles_match_the_whole_window():
    """The tile kernels and the whole window (skip off) write the same bytes, one core."""
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_br_tiles_")
    try:
        z, cells, pool = _ridge_deck(d, sim_s=20.0, h0=1.2)
        _write_breach(d, _spec(cells, formation_s=10.0, update_s=5.0), pool)
        whole = os.path.join(d, "whole")
        tiles = os.path.join(d, "tiles")
        shutil.copytree(d, whole, ignore=shutil.ignore_patterns("whole", "tiles", "results"))
        shutil.copytree(d, tiles, ignore=shutil.ignore_patterns("whole", "tiles", "results"))
        with _env(DAMSAFETY_MF_SKIP="0", DAMSAFETY_MF_CPU_TILES=None, DAMSAFETY_MF_THREADS="1"):
            mfe.run_par(os.path.join(whole, "run.par"), backend="cpu")
        with _env(DAMSAFETY_MF_CPU_TILES="1", DAMSAFETY_MF_THREADS="1"):
            mfe.run_par(os.path.join(tiles, "run.par"), backend="cpu")
        a, b = _files(whole), _files(tiles)
        for name in ("run.max", "run.maxVc", "run.maxDV", "run.mxe", "run.bed", "run.mass",
                     "run.pool"):
            assert a[name] == b[name], name
    finally:
        shutil.rmtree(d, True)


@test
def boann_breach_gpu_matches_itself_and_the_cpu_build():
    """Two GPU runs, and GPU tiles on against off, match byte for byte. GPU against the CPU
    build stays inside the existing tolerances. The final bed is the law, so .bed matches."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_br_gpu_")
    try:
        def once(name, backend, tiles):
            folder = os.path.join(d, name)
            z, cells, pool = _ridge_deck(folder, sim_s=20.0, h0=1.2)
            _write_breach(folder, _spec(cells, formation_s=10.0, update_s=5.0), pool)
            with _env(DAMSAFETY_MF_TILES=tiles, DAMSAFETY_MF_THREADS="1"):
                cli.run(os.path.join(folder, "run.par"), solver="acc", backend=backend, quiet=True)
            return folder
        g1 = once("g1", "metal", None)
        g2 = once("g2", "metal", None)
        a, b = _files(g1), _files(g2)
        for name in a:
            if name.startswith("run."):
                assert a[name] == b[name], name
        off = once("off", "metal", "0")
        c = _files(off)
        for name in ("run.max", "run.maxVc", "run.maxDV", "run.bed", "run.mxe", "run.pool"):
            assert a[name] == c[name], name
        cpu = once("cpu", "cpu", "0")
        for ext, tol in (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01), ("maxDV", 0.1)):
            x = read_asc(os.path.join(g1, "results", "run." + ext))
            y = read_asc(os.path.join(cpu, "results", "run." + ext))
            m = (x != NODATA) & (y != NODATA)
            if m.any():
                assert float(np.abs(x - y)[m].max()) <= tol, (ext, float(np.abs(x - y)[m].max()))
        assert open(os.path.join(g1, "results", "run.bed"), "rb").read() == \
            open(os.path.join(cpu, "results", "run.bed"), "rb").read()
    finally:
        shutil.rmtree(d, True)


@test
def boann_nested_basin_pool_peak_is_near_the_routed_peak():
    """A full inner basin breached through its rim. The pool's peak outflow and the routed
    hydrograph for the same opening differ (tailwater and approach flow), but stay within
    a factor of two."""
    _need_cpu()
    from nd_breach_runner.core import breach as br
    from nd_breach_runner.core.metalflood.cases import nested_basin
    from nd_breach_runner.core.units import ACFT, ACRE, FT
    d = tempfile.mkdtemp(prefix="boann_br_nest_")
    try:
        ny, nx, dx = 30, 60, 10.0
        nested_basin(d, ny=ny, nx=nx, dx=dx, inflow_cms=0.0, outer_floor=300.0,
                     outer_rim=305.0, exit_low=290.0, sim_s=800.0)
        z = read_asc(os.path.join(d, "run.dem.asc"))
        h = np.zeros_like(z)
        h[1:-1, 6:16] = np.maximum(0.0, 325.0 - z[1:-1, 6:16])
        write_asc(os.path.join(d, "run.start"), h, 0.0, 0.0, dx, fmt="%.6f")
        pool = np.zeros_like(z)
        pool[1:-1, 6:16] = 1.0
        mid = (ny - 1) / 2.0
        cells = [[r, 16, (r - mid) * dx] for r in range(1, ny - 1)]
        _write_breach(d, _spec(
            cells, start_s=0.0, formation_s=180.0, update_s=10.0,
            invert_start_m=325.0, invert_final_m=310.0, bottom_width_final_m=30.0,
            side_slope_h=1.0), pool)
        with open(os.path.join(d, "run.par"), "a", encoding="utf-8") as f:
            f.write("startfile run.start\n")
        samples = []
        orig = mfe.TerrainBreach.catch_up

        def spy(br_, eng, t):
            orig(br_, eng, t)
            samples.append((t, float(eng.h2().flat[br_.pool_idx].sum()) * dx * dx))

        mfe.TerrainBreach.catch_up = spy
        try:
            with _env(DAMSAFETY_MF_THREADS="1"):
                mfe.run_par(os.path.join(d, "run.par"), backend="cpu")
        finally:
            mfe.TerrainBreach.catch_up = orig
        # time-to-empty (pool at 5% of the start) against the pool volume sampled after every
        # batch, interpolated: the deck's mass interval is 300 s, and rows only at mass times
        # put it ~150 s late
        from nd_breach_runner.core import terrain_breach as tbm
        got = tbm.read_pool(os.path.join(d, "results", "run.pool"))
        level = 0.05 * got["volume_start_m3"]
        ref = None
        for (ta, va), (tb_, vb) in zip(samples, samples[1:]):
            if va > level >= vb:
                ref = ta + (va - level) / (va - vb) * (tb_ - ta)
                break
        assert ref is not None and got["time_to_empty_s"] is not None, (ref, got)
        assert abs(got["time_to_empty_s"] - ref) <= 10.0, (got["time_to_empty_s"], ref)
        text = open(os.path.join(d, "results", "run.pool"), encoding="utf-8").read().splitlines()
        rows = []
        for ln in text[1:]:
            p = ln.split(",")
            rows.append((float(p[0]), float(p[1]), float(p[2])))
        peak = max(q for _t, _v, q in rows)
        ncell = int(pool.sum())
        vol = float(h[pool > 0].sum()) * dx * dx
        height = 15.0
        dam = br.DamData(
            dam_id="NEST", dam_height_ft=height / FT, storage_top_acft=vol / ACFT,
            top_of_dam_ft=325.0 / FT, breach_bottom_ft=310.0 / FT,
            surface_area_top_ac=(ncell * dx * dx) / ACRE,
            crest_width_ft=20.0, us_slope_h=1.0, ds_slope_h=1.0,
            crest_length_ft=((ny - 2) * dx) / FT)
        ov = br.BreachOverrides(avg_width_ft=(30.0 + 1.0 * height) / FT,
                                formation_hr=180.0 / 3600.0, side_slope_h=1.0)
        hy = br.routed_hydrograph(dam, method=br.METHOD_OVERTOPPING, overrides=ov, dt_s=10.0)
        routed = float(hy.peak_cms)
        ratio = peak / routed if routed else None
        print("nested pool peak {:.3f} cms, routed {:.3f} cms, ratio {}".format(
            peak, routed, None if ratio is None else round(ratio, 3)), flush=True)
        assert routed > 0.0 and peak > 0.0
        assert 0.5 <= ratio <= 2.0, (peak, routed, ratio)
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_breach_law_only_falls_and_stops_at_the_final_invert,
               boann_breach_file_is_refused_when_it_is_malformed_or_the_solver_cannot_use_it,
               boann_no_breach_file_matches_main_byte_for_byte,
               boann_instant_breach_matches_a_prelowered_bed,
               boann_breach_bed_at_halfway_is_the_law_and_mass_holds,
               boann_mxe_is_the_peak_water_level_over_the_lowering_bed,
               boann_mxe_matches_the_sampled_peak_water_level_in_a_full_run,
               boann_pool_times_increase_and_match_the_outflow,
               boann_fv_breach_conserves_mass_on_a_closed_basin,
               boann_breach_cpu_tiles_match_the_whole_window,
               boann_breach_gpu_matches_itself_and_the_cpu_build,
               boann_nested_basin_pool_peak_is_near_the_routed_peak):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
        except Exception as e:
            print("FAIL", fn.__name__, e, flush=True)
            raise
