"""Boann ACC's dry-tile skip on the GPU (Runner 3.90.0, Boann 5.5.0): before every SKIP_CHUNK steps
the GPU marks the 16 x 16 tiles of the wet window that hold water, a face still carrying flow or a
source, and turns on those tiles and their 8 neighbours; the grid kernels then leave the other
tiles out. Water moves at most one cell per step, and a tile left out is at least 17 cells from
any of that, more than the SKIP_CHUNK + 1 = 9 cells the water can reach in a chunk, so its cells
stay dry and its faces carry exactly 0, which is what the whole window computes there. The
outflow and clip sums keep the left-out cells in their SIMD groups (with 0), so they add the same
numbers in the same order. The answer must be the same bit for bit with the skip off
(DAMSAFETY_MF_TILES=0).

The cloud has no Metal GPU, so the CPU build runs the same tile kernels (DAMSAFETY_MF_CPU_TILES=1)
and is checked against the whole window bit for bit; on a Mac the GPU itself is checked with the
skip on and off."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood.cases import valley_deck
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.test_boann_dry_skip import _files, _need_cpu_build, diagonal_deck
from nd_breach_runner.tests.test_boann_rain import _deck as rain_deck

ENVS = ("DAMSAFETY_MF_SKIP", "DAMSAFETY_MF_CPU_TILES", "DAMSAFETY_MF_TILES")
STATS = ("steps", "batches", "injected_m3", "outflow_m3", "final_volume_m3", "clipped_m3",
         "mass_error_m3")


def _run(par, backend, **env):
    """run_par with only the given skip switches set (the others cleared), restored after."""
    keep = {k: os.environ.get(k) for k in ENVS}
    try:
        for k in ENVS:
            os.environ.pop(k, None)
        os.environ.update(env)
        return mfe.run_par(par, backend=backend)
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


def _cases(d):
    """(name, maker): the diagonal valley of the CPU skip's test (open edges, a NoData hole, a far
    pond, a draining film; the wet window is nearly the whole grid) and a long valley of
    300 x 450 cells (most of the window's tiles are dry ground)."""
    return (("diag", lambda f: diagonal_deck(f, sim_s=1.5 * 3600.0)),
            ("valley", lambda f: valley_deck(f, ny=300, nx=450, dx=8.0, sim_s=1800.0)))


@test
def boann_gpu_tiles_kernels_match_the_backend():
    """The tile size and state word the kernels use are the backend's; the tile buffers hold the
    whole grid's tiles; the switch and rain turn the skip off."""
    with open(mfb.KERNEL_SOURCE) as f:
        src = f.read()
    assert "#define MF_TILE {}u".format(mfb.TILE) in src
    assert "#define S_TILES {}".format(mfb.S_TILES) in src
    for k in ("k_tclear", "k_tseed", "k_tgrow"):
        assert "kernel void {}(".format(k) in src and k in mfb.KERNELS, k
    assert mfb.tile_count(1, 1) == 1 and mfb.tile_count(16, 16) == 1
    assert mfb.tile_count(17, 33) == 2 * 3
    lay = dict((n, c) for n, _, c in mfb.buffer_layout(40, 70, 1, 1, 2))
    assert lay["tseed"] == lay["ton"] == 3 * 5
    p = mfb.Params()
    p.wr, p.wc = 40, 70
    keep = os.environ.get(mfb.TILES_ENV)
    try:
        os.environ.pop(mfb.TILES_ENV, None)
        assert mfb._tiles_on(mfb.FLOOD_SPEC, p, mfe.SKIP_CHUNK)
        assert not mfb._tiles_on(mfb.FLOOD_SPEC, p, 0)        # no chunks, no skip
        p.rain = 1
        assert not mfb._tiles_on(mfb.FLOOD_SPEC, p, mfe.SKIP_CHUNK)
        p.rain = 0
        os.environ[mfb.TILES_ENV] = "0"
        assert not mfb._tiles_on(mfb.FLOOD_SPEC, p, mfe.SKIP_CHUNK)
    finally:
        if keep is None:
            os.environ.pop(mfb.TILES_ENV, None)
        else:
            os.environ[mfb.TILES_ENV] = keep
    # a tile turned off must be beyond the cells the water can reach in a chunk
    assert mfb.TILE + 1 > mfe.SKIP_CHUNK + 1


@test
def boann_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_cpu_build():
    """The GPU's tile kernels run on the CPU build give the whole window's answer bit for bit
    (every result file, the .mass log included) and leave cells out."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        for name, mk in _cases(d):
            fa, fb = os.path.join(d, name + "_tiles"), os.path.join(d, name + "_full")
            sa = _run(mk(fa), "cpu", DAMSAFETY_MF_CPU_TILES="1")
            sb = _run(mk(fb), "cpu", DAMSAFETY_MF_SKIP="0")
            _same(_files(fa), _files(fb), sa, sb, "the dry-tile skip ({})".format(name))
            assert sa["injected_m3"] > 0.0, (name, sa)
            assert sa["cell_steps"] < sb["cell_steps"], (name, sa["cell_steps"], sb["cell_steps"])
            if name == "valley":
                # most of a long valley's window is dry ground
                assert sa["cell_steps"] < 0.6 * sb["cell_steps"], (sa["cell_steps"],
                                                                   sb["cell_steps"])
            else:
                assert sa["outflow_m3"] > 0.0, sa
    finally:
        shutil.rmtree(d, True)


@test
def boann_gpu_tiles_are_off_when_it_rains():
    """Rain wets every cell: the tiles run the whole window, and the answer is unchanged."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        yy, xx = np.mgrid[0:40, 0:60].astype(float)
        z = 100.0 + 0.01 * xx + 0.005 * yy
        fa, fb = os.path.join(d, "tiles"), os.path.join(d, "full")
        sa = _run(rain_deck(fa, z, free="E", sim=1800), "cpu", DAMSAFETY_MF_CPU_TILES="1")
        sb = _run(rain_deck(fb, z, free="E", sim=1800), "cpu", DAMSAFETY_MF_SKIP="0")
        _same(_files(fa), _files(fb), sa, sb, "rain and the tiles")
        assert sa["cell_steps"] == sb["cell_steps"], (sa["cell_steps"], sb["cell_steps"])
    finally:
        shutil.rmtree(d, True)


@test
def boann_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_gpu():
    """Runs only on a Mac with a Metal GPU (the macOS CI job, Nick's Mac): the GPU with its
    dry-tile skip on and off gives the same files and sums, and the skip leaves cells out."""
    _need_metal()
    d = tempfile.mkdtemp()
    try:
        for name, mk in _cases(d):
            fa, fb = os.path.join(d, name + "_on"), os.path.join(d, name + "_off")
            sa = _run(mk(fa), "metal")
            sb = _run(mk(fb), "metal", DAMSAFETY_MF_TILES="0")
            assert sa["backend"] == sb["backend"] == "metal"
            _same(_files(fa), _files(fb), sa, sb, "the GPU's dry-tile skip ({})".format(name))
            assert sa["cell_steps"] < sb["cell_steps"], (name, sa["cell_steps"], sb["cell_steps"])
    finally:
        shutil.rmtree(d, True)


@test
def boann_gpu_tiles_match_the_cpu_build():
    """Runs only on a Mac with a Metal GPU: the GPU with the skip agrees with the CPU build within
    the GPU-vs-CPU tolerances of test_metalflood, and the CPU build running the tile kernels
    agrees with the GPU the same way."""
    _need_metal()
    _need_cpu_build()
    from nd_breach_runner.core import builtin_lisflood as bl
    d = tempfile.mkdtemp()
    try:
        for name, mk in _cases(d):
            fg, fc = os.path.join(d, name + "_gpu"), os.path.join(d, name + "_cpu")
            sg = _run(mk(fg), "metal")
            sc = _run(mk(fc), "cpu", DAMSAFETY_MF_CPU_TILES="1")
            assert abs(sg["steps"] - sc["steps"]) <= max(3, sc["steps"] // 200), (name, sg, sc)
            for ext, tol in (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01)):
                x = np.loadtxt(os.path.join(fg, "results", "run." + ext), skiprows=6)
                y = np.loadtxt(os.path.join(fc, "results", "run." + ext), skiprows=6)
                m = (x != bl.NODATA) & (y != bl.NODATA)
                assert np.abs(x - y)[m].max() <= tol, (name, ext, np.abs(x - y)[m].max())
            assert abs(sg["mass_error_m3"]) < 1e-5 * sg["injected_m3"], (name, sg)
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_gpu_tiles_kernels_match_the_backend,
               boann_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_cpu_build,
               boann_gpu_tiles_are_off_when_it_rains,
               boann_gpu_tiles_give_the_same_answer_bit_for_bit_on_the_gpu,
               boann_gpu_tiles_match_the_cpu_build):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
