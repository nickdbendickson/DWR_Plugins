"""Boann sub-grid local inertial's dry-tile skip on the GPU: before every SKIP_CHUNK steps the
GPU marks the 16 x 16 coarse tiles of the wet window that hold water (volume or depth), a face
still carrying flow or a source, and turns on those tiles and their 8 neighbours. The grid kernels
then leave the other tiles out.

A face between two dry cells stays at exactly 0 (each sub-segment of the common edge is at least
either cell's lowest sub-cell), and water moves at most one coarse cell a step. A tile left out is
at least 17 coarse cells from any seed, more than the 9 cells water can reach in a chunk of 8, so
its cells stay dry and its faces carry exactly 0, which is what the whole window computes there.
k_limit_subli and k_cont_subli keep the left-out cells in their SIMD-group reductions (with 0), so
the groups add the same numbers in the same order. The answer must be the same bit for bit with
the skip off.

The cloud has no Metal GPU, so the CPU build runs the same tile kernels (DAMSAFETY_MF_CPU_TILES=1)
and is checked against the whole window bit for bit. On a Mac the GPU itself is checked with the
skip on and off, and against the CPU build within the usual GPU tolerances. On the diagonal deck
only the depths are compared: its 4 mm film gives the two builds velocities up to 0.6 m/s apart
with or without the skip.
"""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine_subli as mfl
from nd_breach_runner.core.metalflood import subli_backend as lb
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.test_boann_dry_skip import _files, _need_cpu_build
from nd_breach_runner.tests.test_subli_speed import _deck

ENVS = ("DAMSAFETY_MF_SKIP", "DAMSAFETY_MF_CPU_TILES", "DAMSAFETY_MF_TILES")
STATS = ("steps", "batches", "injected_m3", "outflow_m3", "final_volume_m3", "clipped_m3",
         "mass_error_m3")
# the speed test's duration: the breach has to reach an open edge (the east-edge film is one
# fine cell of a coarse cell, so it does not by itself book an outflow)
SIM_S = 2.5 * 3600.0


def _run(par, backend, **env):
    """run_par with only the given skip switches set (the others cleared), restored after."""
    keep = {k: os.environ.get(k) for k in ENVS}
    try:
        for k in ENVS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return mfl.run_par(par, backend=backend)
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
        assert sa[k] == sb[k], (what, k, sa[k], sb[k])


def _need_metal():
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))


def _cpu_pair(d, name, extra, sim_s=SIM_S):
    fa, fb = os.path.join(d, name + "_tiles"), os.path.join(d, name + "_full")
    sa = _run(_deck(fa, sim_s, extra), "cpu", DAMSAFETY_MF_CPU_TILES="1")
    sb = _run(_deck(fb, sim_s, extra), "cpu", DAMSAFETY_MF_SKIP="0")
    _same(_files(fa), _files(fb), sa, sb, name)
    assert sa["injected_m3"] > 0.0 and sa["outflow_m3"] > 0.0, (name, sa)
    assert sa["cell_steps"] < sb["cell_steps"], (name, sa["cell_steps"], sb["cell_steps"])
    return sa, sb


def _corner_deck(folder, sim_s):
    """The same valley with the breach inflow on coarse cell (16, 16), a tile corner at r = 4
    (fine cell 66: 66 // 4 == 16) while the window still starts at the grid origin."""
    par = _deck(folder, sim_s, ("subgrid 4",))
    dx, ny = 2.5, 640
    x = 66.5 * dx
    y = (ny - 66.5) * dx
    bci = os.path.join(folder, "run.bci")
    lines = open(bci, encoding="utf-8").read().splitlines()
    lines[0] = "P {:.3f} {:.3f} QVAR breach".format(x, y)
    with open(bci, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return par


@test
def subli_gpu_tiles_kernels_match_the_backend():
    """The tile size and state word are the backend's; the three tile kernels are in the source
    and in SUBLI_KERNELS; the spec has a tile plan; the layout ends with tseed, ton, one word per
    coarse-grid tile, and that is the 31st slot."""
    with open(lb.SUBLI_KERNEL_SOURCE, encoding="utf-8") as f:
        src = f.read()
    assert "#define MF_TILE {}u".format(mfb.TILE) in src
    assert "#define S_TILES {}".format(mfb.S_TILES) in src
    for k in ("k_tclear_subli", "k_tseed_subli", "k_tgrow_subli"):
        assert "kernel void {}(".format(k) in src and k in lb.SUBLI_KERNELS, k
    assert lb.SUBLI_SPEC.tile_plan is not None
    names = [n for n, _, _ in lb.subli_buffer_layout(40, 70, 1, 1, 2, 4)]
    assert names[-2:] == ["tseed", "ton"], names[-2:]
    assert len(names) + 1 == 31
    ntiles = mfb.tile_count(40, 70)
    lay = dict((n, c) for n, _, c in lb.subli_buffer_layout(40, 70, 1, 1, 2, 4))
    assert lay["tseed"] == lay["ton"] == ntiles == 3 * 5
    # a tile left off is beyond the cells water can reach in a chunk
    assert mfb.TILE + 1 > mfl.SKIP_CHUNK + 1


@test
def subli_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_cpu_build():
    """The tile kernels on the CPU build against the whole window: r = 4, r = 4 with the upwind
    weighting, and r = 2. Every result file and the mass sums match, and the tiles leave cells out."""
    _need_cpu_build()
    cases = (("r4", ("subgrid 4",)),
             ("r4-upwind", ("subgrid 4", "upwind")),
             ("r2", ("subgrid 2",)))
    for name, extra in cases:
        d = tempfile.mkdtemp()
        try:
            _cpu_pair(d, name, extra)
        finally:
            shutil.rmtree(d, True)


@test
def subli_gpu_tiles_mark_a_source_on_a_tile_corner():
    """The breach inflow sits on coarse cell (16, 16), the corner where four tiles meet. The tiles
    still match the whole window bit for bit and still leave cells out."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        fa, fb = os.path.join(d, "tiles"), os.path.join(d, "full")
        sa = _run(_corner_deck(fa, SIM_S), "cpu", DAMSAFETY_MF_CPU_TILES="1")
        sb = _run(_corner_deck(fb, SIM_S), "cpu", DAMSAFETY_MF_SKIP="0")
        _same(_files(fa), _files(fb), sa, sb, "a source on a tile corner")
        assert sa["injected_m3"] > 0.0 and sa["outflow_m3"] > 0.0, sa
        assert sa["cell_steps"] < sb["cell_steps"], (sa["cell_steps"], sb["cell_steps"])
    finally:
        shutil.rmtree(d, True)


@test
def subli_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_gpu():
    """Runs only on a Mac with a Metal GPU: the GPU with the skip on and off gives the same files
    and sums, and the skip leaves cells out. The three decks of the CPU test."""
    _need_metal()
    cases = (("r4", ("subgrid 4",)),
             ("r4-upwind", ("subgrid 4", "upwind")),
             ("r2", ("subgrid 2",)))
    for name, extra in cases:
        d = tempfile.mkdtemp()
        try:
            fa, fb = os.path.join(d, name + "_on"), os.path.join(d, name + "_off")
            sa = _run(_deck(fa, SIM_S, extra), "metal")
            sb = _run(_deck(fb, SIM_S, extra), "metal", DAMSAFETY_MF_TILES="0")
            assert sa["backend"] == sb["backend"] == "metal"
            _same(_files(fa), _files(fb), sa, sb, "the GPU's dry-tile skip ({})".format(name))
            assert sa["cell_steps"] < sb["cell_steps"], (name, sa["cell_steps"], sb["cell_steps"])
        finally:
            shutil.rmtree(d, True)


@test
def subli_gpu_tiles_match_the_cpu_build():
    """Runs only on a Mac with a Metal GPU: the GPU with the skip against the CPU build running
    the tile kernels, on the diagonal deck. Depths within 0.01 m (the 4 mm film's velocities are
    not compared: the two builds already differ there by up to 0.6 m/s). Steps within 0.5 percent
    or 3, and the mass error under 1e-5 of the inflow."""
    _need_metal()
    _need_cpu_build()
    from nd_breach_runner.core import builtin_lisflood as bl
    d = tempfile.mkdtemp()
    try:
        fg, fc = os.path.join(d, "gpu"), os.path.join(d, "cpu")
        sg = _run(_deck(fg, SIM_S, ("subgrid 4",)), "metal")
        sc = _run(_deck(fc, SIM_S, ("subgrid 4",)), "cpu", DAMSAFETY_MF_CPU_TILES="1")
        assert abs(sg["steps"] - sc["steps"]) <= max(3, sc["steps"] // 200), (sg, sc)
        x = np.loadtxt(os.path.join(fg, "results", "run.max"), skiprows=6)
        y = np.loadtxt(os.path.join(fc, "results", "run.max"), skiprows=6)
        m = (x != bl.NODATA) & (y != bl.NODATA)
        assert np.abs(x - y)[m].max() <= 0.01, float(np.abs(x - y)[m].max())
        assert abs(sg["mass_error_m3"]) < 1e-5 * sg["injected_m3"], sg
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (subli_gpu_tiles_kernels_match_the_backend,
               subli_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_cpu_build,
               subli_gpu_tiles_mark_a_source_on_a_tile_corner,
               subli_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_gpu,
               subli_gpu_tiles_match_the_cpu_build):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
        except Exception as e:
            print("FAIL", fn.__name__, type(e).__name__, e, flush=True)
            raise
