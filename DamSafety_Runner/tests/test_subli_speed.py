"""Boann sub-grid local inertial runs faster with the same results (Runner 3.92.0).

1. The mass file's wet area (every massint) is counted on the block of coarse cells holding water
only: a coarse cell with no water has the same surface at every step, so its fine cells count what
they counted at the start. Before, the whole fine grid was downscaled every massint, which on the
DWR-2801 2 m LiDAR deck (88 M fine cells) was most of the run's time. Same count, checked against
the whole-grid count on random water.

2. The dry-cell skip: the CPU build runs, in each row of
the wet window, only the coarse cells within SKIP_CHUNK + 1 cells of water (or of a face still
carrying flow, or of a source), redrawn every SKIP_CHUNK steps, as the ACC engine has since 3.61.0
(test_boann_dry_skip). Between two cells with no water every face of this engine keeps exactly 0
(each sub-segment of a common edge is at least the lowest sub-cell of either cell), so the answer
must be the same bit for bit with the skip off (DAMSAFETY_MF_SKIP=0): on the diagonal valley of
test_boann_dry_skip at 2.5 m (open edges, a NoData hole, a far pond, a draining film) at r = 4
and r = 2,
with and without the paper's upwind weighting, on one core and on four."""
import os
import shutil
import tempfile

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine_subli as mfl
from nd_breach_runner.core.metalflood import subli_backend as lb
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.test_boann_dry_skip import _files, _need_cpu_build, diagonal_deck

KEYS = ("steps", "batches", "injected_m3", "outflow_m3", "final_volume_m3", "clipped_m3",
        "mass_error_m3")


def _run(par, skip):
    old = os.environ.get("DAMSAFETY_MF_SKIP")
    os.environ["DAMSAFETY_MF_SKIP"] = "1" if skip else "0"
    try:
        return mfl.run_par(par, backend="cpu")
    finally:
        if old is None:
            os.environ.pop("DAMSAFETY_MF_SKIP", None)
        else:
            os.environ["DAMSAFETY_MF_SKIP"] = old


def _deck(folder, sim_s, extra):
    """test_boann_dry_skip's diagonal valley at 2.5 m (640 x 1440 DEM cells): at r = 4 the solver's
    160 x 360 coarse cells are that test's grid, so the skip has as much dry ground to leave out"""
    return diagonal_deck(folder, ny=640, nx=1440, dx=2.5, sim_s=sim_s, extra=extra)


def _same(d, extra, sim_s=2.5 * 3600.0, min_saving=0.9):
    sa = _run(_deck(os.path.join(d, "on"), sim_s, extra), True)
    sb = _run(_deck(os.path.join(d, "off"), sim_s, extra), False)
    fa, fb = _files(os.path.join(d, "on")), _files(os.path.join(d, "off"))
    for ext in fa:
        assert fa[ext] == fb[ext], "run.{} differs with the dry-cell skip ({})".format(ext, extra)
    for k in KEYS:
        assert sa[k] == sb[k], (extra, k, sa[k], sb[k])
    assert sa["outflow_m3"] > 0.0 and sa["injected_m3"] > 0.0, sa
    assert sa["cell_steps"] < min_saving * sb["cell_steps"], (extra, sa["cell_steps"],
                                                             sb["cell_steps"])
    return sa, sb


@test
def subli_dry_skip_gives_the_same_answer_bit_for_bit():
    _need_cpu_build()
    for extra in (("subgrid 4",), ("subgrid 2",), ("subgrid 4", "upwind")):
        d = tempfile.mkdtemp()
        try:
            _same(d, extra)
        finally:
            shutil.rmtree(d, True)


@test
def subli_dry_skip_matches_one_core():
    """The skip's rows are spread over the cores with a dynamic schedule: one core must give the
    same numbers as four."""
    _need_cpu_build()
    keep = {k: os.environ.get(k) for k in ("DAMSAFETY_MF_OPENMP", "DAMSAFETY_MF_THREADS")}
    d = tempfile.mkdtemp()
    try:
        os.environ["DAMSAFETY_MF_OPENMP"] = "1"
        if not mfb.build_cpu_library(lb.SUBLI_SPEC).endswith("_omp" + mfb.library_ext()):
            raise Skip("this C++ compiler has no OpenMP")
        out = []
        for mode, threads in (("0", "1"), ("1", "4")):
            os.environ["DAMSAFETY_MF_OPENMP"] = mode
            os.environ["DAMSAFETY_MF_THREADS"] = threads
            f = os.path.join(d, mode)
            st = _run(_deck(f, 3600.0, ("subgrid 4",)), True)
            out.append((st, _files(f)))
        (s1, f1), (sn, fn) = out
        assert s1["steps"] == sn["steps"] and s1["cell_steps"] == sn["cell_steps"]
        for ext in f1:
            assert f1[ext] == fn[ext], "run.{} differs on one core".format(ext)
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(d, True)


@test
def subli_engine_asks_for_the_skip():
    """The engine passes SKIP_CHUNK to the backend, and the subli CPU source has the skip."""
    assert mfl.SKIP_CHUNK == 8
    with open(lb.SUBLI_CPU_SOURCE) as f:
        src = f.read()
    assert "long long mf_run_batch_skip(" in src and "mf_dispatch_rows<k_faces_subli>" in src


@test
def subli_mass_area_counts_the_wet_block_only_with_the_same_answer():
    """Engine.wet_fine_count against the whole fine grid downscaled (the count before 3.92.0), on
    the diagonal valley at r = 4 and r = 3 (a fine grid that is not a multiple of r, so the padded
    edge is in play), with water in a corner block, in scattered cells, in none and everywhere."""
    _need_cpu_build()
    import numpy as np
    from nd_breach_runner.core.metalflood import backends as be
    from nd_breach_runner.core.metalflood import subgrid as sg
    d = tempfile.mkdtemp()
    try:
        for r in (4, 3):
            par = diagonal_deck(os.path.join(d, str(r)), ny=161, nx=362,
                                extra=("subgrid {}".format(r),))
            deck = mfl.Deck(par)
            bk = be.make_backend("cpu", spec=lb.SUBLI_SPEC)
            eng = mfl.Engine(deck, bk)
            nyf, nxf = eng.nyf, eng.nxf
            dry = eng.dry_fine_counts(nyf, nxf)
            rng = np.random.default_rng(7)
            V = eng.volume2()
            cases = []
            V[:] = 0.0
            cases.append("none")
            for name in ("corner", "scattered", "all"):
                V[:] = 0.0
                if name == "corner":
                    V[3:11, 20:31] = rng.uniform(0.0, 400.0, (8, 11))
                elif name == "scattered":
                    pick = rng.random(V.shape) < 0.02
                    V[pick] = rng.uniform(0.0, 300.0, int(pick.sum()))
                else:
                    V[:] = rng.uniform(0.0, 500.0, V.shape)
                old = int((sg.fine_depth(eng.eta2(), eng.dem_fine, r)[:nyf, :nxf]
                           > mfl.DRY_M).sum())
                new = eng.wet_fine_count(nyf, nxf, dry)
                assert new == old, (r, name, new, old)
                cases.append(name)
            V[:] = 0.0
            old = int((sg.fine_depth(eng.eta2(), eng.dem_fine, r)[:nyf, :nxf] > mfl.DRY_M).sum())
            assert eng.wet_fine_count(nyf, nxf, dry) == old
    finally:
        shutil.rmtree(d, True)
