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
def boann_mxe_uses_the_bed_from_the_period_the_peak_rose():
    """A cell whose peak is already set when the cut happens keeps the intact bed. A cell
    that is dry then, and wet after, records the lowered bed. Using the final bed for every
    cell fails the first check."""
    ny, nx = 4, 4

    class Deck:
        pass

    deck = Deck()
    deck.ny, deck.nx, deck.dx = ny, nx, 10.0
    deck.valid = np.ones((ny, nx), dtype=bool)
    deck.z = np.full((ny, nx), 12.0)
    spec = {"start_s": 10.0, "formation_s": 0.0, "invert_start_m": 12.0,
            "invert_final_m": 10.0, "bottom_width_final_m": 10.0, "side_slope_h": 0.0,
            "update_s": 5.0}
    br = mfe.TerrainBreach(deck, spec, [[1, 1, 0.0], [1, 2, 0.0]], None)
    # periods start (zeros, intact 12). One update then: depth at the cut, bed after it.
    br.zcur[:] = 10.0
    br.periods.append((np.array([1.5, 0.0], np.float32), np.array([10.0, 10.0])))
    hmax = np.zeros(ny * nx, np.float32)
    hmax[1 * nx + 1] = np.float32(1.5)    # peaked at the cut, not after
    hmax[1 * nx + 2] = np.float32(0.4)    # dry at the cut, wet after
    bed = br.bed_for_mxe(hmax)
    assert abs(float(bed[1, 1]) - 12.0) < 1e-6, bed[1, 1]
    assert abs(float(bed[1, 2]) - 10.0) < 1e-6, bed[1, 2]


@test
def boann_pool_times_increase_and_match_the_outflow():
    """The pool file's time column only moves forward, and each row's outflow accounts for
    the volume change since the previous row (no point source, so no extra inflow)."""
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_br_pool_")
    try:
        _z, cells, pool = _ridge_deck(d, sim_s=40.0, h0=1.5)
        _write_breach(d, _spec(cells, formation_s=20.0, update_s=10.0), pool)
        with _env(DAMSAFETY_MF_THREADS="1"):
            mfe.run_par(os.path.join(d, "run.par"), backend="cpu")
        rows = []
        for ln in open(os.path.join(d, "results", "run.pool"), encoding="utf-8"):
            if not ln.strip() or ln.startswith("time"):
                continue
            t, vol, q = (float(x) for x in ln.split(","))
            rows.append((t, vol, q))
        assert len(rows) >= 3, rows
        for a, b in zip(rows, rows[1:]):
            assert b[0] > a[0] + 1e-9, (a[0], b[0])
            expect = a[1] - b[2] * (b[0] - a[0])
            assert abs(b[1] - expect) < 1e-2, (a, b, expect)
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
        with _env(DAMSAFETY_MF_THREADS="1"):
            mfe.run_par(os.path.join(d, "run.par"), backend="cpu")
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
               boann_mxe_uses_the_bed_from_the_period_the_peak_rose,
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
