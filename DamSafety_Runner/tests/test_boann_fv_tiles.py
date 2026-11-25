"""Boann FV1 / FV2 / DG2 dry-tile skip on the GPU. Same shape as the ACC skip
(tests/test_boann_gpu_tiles.py): tiles of 16 x 16, redrawn every chunk, a ring around each seed.
A cell left out stays exactly dry, which is what the whole window computes there, so every result
file and every stat is the same bit for bit with the skip off. The cloud has no Metal GPU, so the
CPU build runs the same tile kernels (DAMSAFETY_MF_CPU_TILES=1). On a Mac the GPU is checked too.

FV1 only until FV2 and DG2 land: one faces-and-cont sweep per step, so water moves one cell a
step (R = 1) and a 1-tile ring with a chunk of 8 is enough (17 > 8). FV2 does two stages a step
and its slopes read one more neighbour (R = 2, stencil 2), so the chunk is 4 (10 < 17)."""
import os
import re
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine_fv as fve
from nd_breach_runner.core.metalflood.cases import valley_deck, write_asc
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.test_boann_dry_skip import _files, _need_cpu_build, diagonal_deck
from nd_breach_runner.tests.test_boann_rain import _deck as rain_deck

# name, order, chunk, cells of influence per step, extra stencil cells the ring must cover
SOLVERS = (("fv", 1, fve.SKIP_CHUNK_FV, 1, 0),
           ("fv2", 2, fve.SKIP_CHUNK_FV2, 2, 2))
_SPEC = {"fv": mfb.FV_SPEC, "fv2": mfb.FV2_SPEC}
_LAYOUT = {"fv": mfb.fv_buffer_layout, "fv2": mfb.fv2_buffer_layout}
_KERNEL = {"fv": mfb.FV_KERNEL_SOURCE, "fv2": mfb.FV2_KERNEL_SOURCE}
_TON = {"fv": 25, "fv2": 29}          # buffer index of ton (ACC is 24)
ENVS = ("DAMSAFETY_MF_SKIP", "DAMSAFETY_MF_CPU_TILES", "DAMSAFETY_MF_TILES")
STATS = ("steps", "batches", "injected_m3", "outflow_m3", "final_volume_m3", "clipped_m3",
         "mass_error_m3", "rain_m3")
# GPU against the CPU build: the tolerances test_metalflood already uses for these solvers
GPU_TOL = (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01))


def _src(name):
    with open(_KERNEL[name], encoding="utf-8") as f:
        return f.read()


def _define(src, what):
    m = re.search(r"#define {} (\d+)u".format(what), src)
    assert m, what
    return int(m.group(1))


def _run(par, order, backend, **env):
    keep = {k: os.environ.get(k) for k in ENVS}
    try:
        for k in ENVS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return fve.run_par(par, backend=backend, order=order)
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _same(fa, fb, sa, sb, what):
    for ext in fa:
        assert fa[ext] == fb[ext], "run.{} differs with {}".format(ext, what)
    for k in STATS:
        if k in sa or k in sb:
            assert sa.get(k) == sb.get(k), (what, k, sa.get(k), sb.get(k))


def _need_metal():
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))


def _cases(d):
    """The ACC tile test's two decks: a diagonal valley (open edges, a NoData hole) and a long
    valley of 300 x 450 cells, most of whose window is dry ground."""
    return (("diag", lambda f: diagonal_deck(f, sim_s=1.5 * 3600.0)),
            ("valley", lambda f: valley_deck(f, ny=300, nx=450, dx=8.0, sim_s=1800.0)))


@test
def boann_fv_tiles_kernels_match_the_backend():
    """Tile size, state word and ring agree with the backend in every solver that has the skip;
    the tile kernels are in the spec; no layout goes past 30 buffers plus Params; the ring covers
    the chunk."""
    assert mfb.TILE == 16 and mfb.S_TILES == 18
    for name, _order, chunk, reach, stencil in SOLVERS:
        src = _src(name)
        assert "#define MF_TILE {}u".format(mfb.TILE) in src, name
        assert "#define S_TILES {}".format(mfb.S_TILES) in src, name
        ring = _define(src, "MF_TILE_RING")
        spec = _SPEC[name]
        for k in ("k_tclear_" + name, "k_tseed_" + name, "k_tgrow_" + name):
            assert "kernel void {}(".format(k) in src and k in spec.kernels, (name, k)
        assert spec.tile_plan is not None, name
        lay = _LAYOUT[name](40, 70, 1, 1, 2)
        assert len(lay) <= 30, (name, len(lay))          # Params is the next slot, index <= 30
        names = [n for n, _, _ in lay]
        assert names[-2:] == ["tseed", "ton"], (name, names[-2:])
        assert mfb.tile_count(17, 33) == 2 * 3
        # an off tile starts this many cells from a seed; the chunk must not reach it
        assert ring * mfb.TILE + 1 > chunk * reach + stencil, (name, ring, chunk, reach, stencil)
        p = mfb.Params()
        p.wr, p.wc, p.rain = 40, 70, 0
        assert mfb._tiles_on(spec, p, chunk), name
        assert not mfb._tiles_on(spec, p, 0), name
    # FV2 and DG2 layouts stay inside the cap even before they grow the tile buffers
    assert len(mfb.fv2_buffer_layout(8, 8, 1, 1, 2)) <= 30
    assert len(mfb.dg2_buffer_layout(8, 8, 1, 1, 2)) <= 30
    # FV1's ton is the buffer the CPU tile runner reads
    names = [n for n, _, _ in mfb.fv_buffer_layout(4, 4, 1, 1, 2)]
    assert names.index("ton") == _TON["fv"]
    fv2_names = [n for n, _, _ in mfb.fv2_buffer_layout(4, 4, 1, 1, 2)]
    assert fv2_names.index("h0") == 24 and fv2_names.index("hs") == 27, fv2_names
    assert fv2_names.index("ton") == _TON["fv2"]
    assert len(fv2_names) == 30                      # Params at 30, the Metal cap
    # rain turns FV1's skip off (its Params carries the rain word, as the ACC engine's does)
    p = mfb.Params()
    p.wr, p.wc, p.rain = 40, 70, 1
    assert not mfb._tiles_on(mfb.FV_SPEC, p, fve.SKIP_CHUNK_FV)


@test
def boann_fv_tiles_give_the_same_answer_bit_for_bit_on_the_cpu_build():
    """The GPU's tile kernels on the CPU build match the whole window bit for bit, and leave
    cells out. The diagonal deck's NoData hole is a reflecting wall inside a tile the flood
    reaches."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        for name, order, _chunk, _reach, _stencil in SOLVERS:
            for case, mk in _cases(d):
                fa = os.path.join(d, name + "_" + case + "_tiles")
                fb = os.path.join(d, name + "_" + case + "_full")
                sa = _run(mk(fa), order, "cpu", DAMSAFETY_MF_CPU_TILES="1")
                sb = _run(mk(fb), order, "cpu", DAMSAFETY_MF_SKIP="0")
                what = "{} dry-tile skip ({})".format(name, case)
                _same(_files(fa), _files(fb), sa, sb, what)
                assert sa["injected_m3"] > 0.0, (what, sa)
                assert sa["cell_steps"] < sb["cell_steps"], (what, sa["cell_steps"],
                                                             sb["cell_steps"])
                if case == "valley":
                    assert sa["cell_steps"] < 0.6 * sb["cell_steps"], (name, sa["cell_steps"],
                                                                        sb["cell_steps"])
                else:
                    assert sa["outflow_m3"] > 0.0, (what, sa)
                    h = np.loadtxt(os.path.join(fa, "results", "run.max"), skiprows=6)
                    hole = h[h.shape[0] // 2 - 1:h.shape[0] // 2 + 2,
                             h.shape[1] // 2 + 6:h.shape[1] // 2 + 9]
                    assert hole.size and np.all(hole == -9999.0), (name, hole)
                    around = h[h.shape[0] // 2 - 4:h.shape[0] // 2 + 5,
                               h.shape[1] // 2 + 2:h.shape[1] // 2 + 13]
                    wet = around[around != -9999.0]
                    assert wet.size and wet.max() > 0.0, (name, "water never reached the NoData wall")
    finally:
        shutil.rmtree(d, True)


@test
def boann_fv_tiles_reach_stays_inside_the_ring():
    """One wet cell on dry sloping ground. After 1, 2, 4 and 8 steps the nonzero state is no
    farther than R cells per step. That measured reach is what the ring and the chunk cover."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        ny, nx = 48, 48
        yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
        z = 100.0 + 0.04 * xx + 0.02 * yy
        r0, c0 = ny // 2, nx // 2
        for name, order, chunk, reach, stencil in SOLVERS:
            seen = {}
            for nsteps in (1, 2, 4, 8):
                h0 = np.zeros((ny, nx))
                h0[r0, c0] = 1.5
                folder = os.path.join(d, "{}_{}".format(name, nsteps))
                os.makedirs(folder, exist_ok=True)
                write_asc(os.path.join(folder, "run.dem.asc"), z, 0.0, 0.0, 10.0)
                write_asc(os.path.join(folder, "run.start"), h0, 0.0, 0.0, 10.0)
                par = os.path.join(folder, "run.par")
                with open(par, "w") as f:
                    f.write("\n".join([
                        "DEMfile run.dem.asc", "resroot run", "dirroot results",
                        "startfile run.start", "sim_time 100000", "initial_tstep 1.0",
                        "massint 100000", "saveint 100010", "fpfric 0.03"]) + "\n")
                bk = mfb.make_backend("cpu", spec=_SPEC[name])
                try:
                    eng = fve.Engine(fve.Deck(par), bk)
                    out = eng.batch(0.0, 1.0e9, nsteps, False)
                    assert out["steps"] == nsteps, (name, nsteps, out["steps"])
                    h = eng.h2()
                    changed = h != 0.0
                    if name != "dg2":
                        changed = changed | (eng.a["hu"].reshape(ny, nx) != 0.0) \
                            | (eng.a["hv"].reshape(ny, nx) != 0.0)
                    ys, xs = np.nonzero(changed)
                    dist = int(max(np.maximum(np.abs(ys - r0), np.abs(xs - c0)))) if len(ys) else 0
                finally:
                    bk.close()
                seen[nsteps] = dist
                assert dist <= reach * nsteps, (name, nsteps, dist, reach)
                assert dist >= 1, (name, nsteps, dist)
            print("reach {} (R={}, chunk {}, stencil {}): {}".format(
                name, reach, chunk, stencil, seen), flush=True)
            far = max(seen.values())
            assert far <= reach * 8, (name, seen)
    finally:
        shutil.rmtree(d, True)


@test
def boann_fv_tiles_are_off_when_it_rains():
    """FV1 rain wets every cell: the skip runs the whole window and the answer does not move.
    FV2 and DG2 refuse a deck with rain, so this is FV1 only."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        yy, xx = np.mgrid[0:40, 0:60].astype(float)
        z = 100.0 + 0.01 * xx + 0.005 * yy
        fa, fb = os.path.join(d, "tiles"), os.path.join(d, "full")
        sa = _run(rain_deck(fa, z, free="E", sim=1800), 1, "cpu", DAMSAFETY_MF_CPU_TILES="1")
        sb = _run(rain_deck(fb, z, free="E", sim=1800), 1, "cpu", DAMSAFETY_MF_SKIP="0")
        _same(_files(fa), _files(fb), sa, sb, "FV1 rain and the tiles")
        assert sa["cell_steps"] == sb["cell_steps"], (sa["cell_steps"], sb["cell_steps"])
        assert sa.get("rain_m3", 0.0) > 0.0, sa
    finally:
        shutil.rmtree(d, True)


@test
def boann_fv_tiles_give_the_same_answer_bit_for_bit_on_the_gpu():
    """Mac only: the GPU with the skip on and off, bit for bit, and the skip leaves cells out."""
    _need_metal()
    d = tempfile.mkdtemp()
    try:
        for name, order, _c, _r, _s in SOLVERS:
            for case, mk in _cases(d):
                fa = os.path.join(d, name + "_" + case + "_on")
                fb = os.path.join(d, name + "_" + case + "_off")
                sa = _run(mk(fa), order, "metal")
                sb = _run(mk(fb), order, "metal", DAMSAFETY_MF_TILES="0")
                assert sa["backend"] == sb["backend"] == "metal"
                _same(_files(fa), _files(fb), sa, sb, "GPU {} ({})".format(name, case))
                assert sa["cell_steps"] < sb["cell_steps"], (name, case, sa["cell_steps"],
                                                             sb["cell_steps"])
    finally:
        shutil.rmtree(d, True)


@test
def boann_fv_tiles_match_the_cpu_build():
    """Mac only: the GPU with the skip against the CPU build running the same tile kernels,
    within the GPU-vs-CPU tolerances. The two builds are not bit for bit."""
    _need_metal()
    _need_cpu_build()
    from nd_breach_runner.core import builtin_lisflood as bl
    d = tempfile.mkdtemp()
    try:
        for name, order, _c, _r, _s in SOLVERS:
            for case, mk in _cases(d):
                fg = os.path.join(d, name + "_" + case + "_gpu")
                fc = os.path.join(d, name + "_" + case + "_cpu")
                sg = _run(mk(fg), order, "metal")
                sc = _run(mk(fc), order, "cpu", DAMSAFETY_MF_CPU_TILES="1")
                assert abs(sg["steps"] - sc["steps"]) <= max(3, sc["steps"] // 200), (name, case,
                                                                                       sg, sc)
                for ext, tol in GPU_TOL:
                    x = np.loadtxt(os.path.join(fg, "results", "run." + ext), skiprows=6)
                    y = np.loadtxt(os.path.join(fc, "results", "run." + ext), skiprows=6)
                    m = (x != bl.NODATA) & (y != bl.NODATA)
                    assert np.abs(x - y)[m].max() <= tol, (name, case, ext,
                                                           float(np.abs(x - y)[m].max()))
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_fv_tiles_kernels_match_the_backend,
               boann_fv_tiles_reach_stays_inside_the_ring,
               boann_fv_tiles_are_off_when_it_rains,
               boann_fv_tiles_give_the_same_answer_bit_for_bit_on_the_cpu_build,
               boann_fv_tiles_give_the_same_answer_bit_for_bit_on_the_gpu,
               boann_fv_tiles_match_the_cpu_build):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
