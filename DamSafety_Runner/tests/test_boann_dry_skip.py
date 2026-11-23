"""Boann ACC's dry-cell skip (Runner 3.61.0): the CPU build runs, in each row of the wet window,
only the columns within SKIP_CHUNK + 1 cells of water (or of a face still carrying flow, or of
a source), redrawn every SKIP_CHUNK steps, as LISFLOOD-FP skips its dry cells. Water moves at
most one cell per step, so the cells it leaves out stay dry and the faces between them carry
exactly 0, which is what the full window computes there. The answer must be
the same bit for bit with the skip off (DAMSAFETY_MF_SKIP=0): checked on a diagonal valley (the
wet window is nearly the whole grid, each row holds water in a narrow band) with the q-centred
scheme, open edges, a NoData hole, a start-depth pond and a draining film at an open edge."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood.cases import write_asc
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if os.name == "nt" and not mfb.prebuilt_available():
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.prebuilt_available() and not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


def diagonal_deck(folder, ny=150, nx=360, dx=10.0, peak_cms=150.0, sim_s=2.5 * 3600.0,
                  extra=("theta 0.7",)):
    """A valley running corner to corner (north-west high, south-east low) with a breach inflow
    at the top, an open east and south edge, a NoData hole in the channel's path, rough banks,
    a start pond far from the channel and a thin film on the east edge that drains away."""
    os.makedirs(folder, exist_ok=True)
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    along = (xx / nx + yy / ny) / 2.0                              # 0 at NW, 1 at SE
    off = np.abs(yy / ny - xx / nx) * min(ny, nx) * dx             # distance from the diagonal
    z = 200.0 - 12.0 * along + 0.03 * off + 1.2 * np.tanh(off / 40.0)
    z += 0.1 * np.sin(xx * 0.7) * np.cos(yy * 0.9)
    z[ny // 2 - 1:ny // 2 + 2, nx // 2 + 6:nx // 2 + 9] = -9999.0
    xll, yll = 0.0, 0.0
    write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dx)
    n = np.where(off < 30.0, 0.035, 0.08)
    write_asc(os.path.join(folder, "run.n.asc"), n, xll, yll, dx)
    h0 = np.zeros_like(z)
    h0[ny - 30:ny - 26, 20:26] = 0.6                               # a pond, far off the channel
    h0[10:40, nx - 1] = 0.004                                     # a film that drains off the E edge
    write_asc(os.path.join(folder, "run.start"), h0, xll, yll, dx)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results",
             "sim_time {:.1f}".format(sim_s), "initial_tstep 10.0", "massint 300.0",
             "saveint {:.1f}".format(sim_s + 10.0), "fpfric 0.06", "manningfile run.n.asc",
             "startfile run.start", "bcifile run.bci", "bdyfile run.bdy", "acceleration"]
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines + list(extra)) + "\n")
    with open(os.path.join(folder, "run.bci"), "w") as f:
        f.write("P {:.3f} {:.3f} QVAR breach\n".format(xll + 3.5 * dx, yll + (ny - 2.5) * dx))
        f.write("E {:.3f} {:.3f} FREE\n".format(yll, yll + ny * dx))
        f.write("S {:.3f} {:.3f} FREE\n".format(xll, xll + nx * dx))
    t = np.array([0.0, 0.2, 0.5, 0.9, 1.4, 2.0, 10.0]) * 3600.0
    q = np.array([0.0, 0.3, 1.0, 0.55, 0.2, 0.0, 0.0]) * peak_cms
    with open(os.path.join(folder, "run.bdy"), "w") as f:
        f.write("test inflow\nbreach\n{} seconds\n".format(len(t)))
        for tv, qv in zip(t, q):
            f.write("{:.6f} {:.1f}\n".format(qv / dx, tv))
    return os.path.join(folder, "run.par")


def _run(par, skip):
    old = os.environ.get("DAMSAFETY_MF_SKIP")
    os.environ["DAMSAFETY_MF_SKIP"] = "1" if skip else "0"
    try:
        return mfe.run_par(par, backend="cpu")
    finally:
        if old is None:
            os.environ.pop("DAMSAFETY_MF_SKIP", None)
        else:
            os.environ["DAMSAFETY_MF_SKIP"] = old


def _files(folder):
    res = os.path.join(folder, "results")
    out = {}
    for ext in ("max", "maxVc", "inittm", "maxtm", "mxe", "mass"):
        with open(os.path.join(res, "run." + ext), "rb") as f:
            out[ext] = f.read()
    return out


@test
def boann_dry_skip_gives_the_same_answer_bit_for_bit():
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        sa = _run(diagonal_deck(os.path.join(d, "on")), True)
        sb = _run(diagonal_deck(os.path.join(d, "off")), False)
        fa, fb = _files(os.path.join(d, "on")), _files(os.path.join(d, "off"))
        for ext in fa:
            assert fa[ext] == fb[ext], "run.{} differs with the dry-cell skip".format(ext)
        for k in ("steps", "batches", "injected_m3", "outflow_m3", "final_volume_m3",
                  "clipped_m3", "mass_error_m3"):
            assert sa[k] == sb[k], (k, sa[k], sb[k])
        assert sa["outflow_m3"] > 0.0 and sa["injected_m3"] > 0.0, sa
        # the skip left cells out (the far pond shares rows with the channel, and a row runs the hull
        # of its water, so not more)
        assert sa["cell_steps"] < 0.7 * sb["cell_steps"], (sa["cell_steps"], sb["cell_steps"])
        h = np.loadtxt(os.path.join(d, "on", "results", "run.max"), skiprows=6)
        assert h[-1, -40:].max() > 0.05                # the flood reached the south-east corner
    finally:
        shutil.rmtree(d, True)


@test
def boann_dry_skip_matches_one_core():
    """The skip's rows are spread over the cores with a dynamic schedule: the one-core build must
    give the same numbers as four cores."""
    _need_cpu_build()
    keep = {k: os.environ.get(k) for k in ("DAMSAFETY_MF_OPENMP", "DAMSAFETY_MF_THREADS")}
    d = tempfile.mkdtemp()
    try:
        os.environ["DAMSAFETY_MF_OPENMP"] = "1"
        if not mfb.build_cpu_library(mfb.FLOOD_SPEC).endswith("_omp" + mfb.library_ext()):
            raise Skip("this C++ compiler has no OpenMP")
        out = []
        for mode, threads in (("0", "1"), ("1", "4")):
            os.environ["DAMSAFETY_MF_OPENMP"] = mode
            os.environ["DAMSAFETY_MF_THREADS"] = threads
            f = os.path.join(d, mode)
            st = _run(diagonal_deck(f, sim_s=3600.0), True)
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
def boann_dry_skip_is_off_with_rain_and_on_an_old_library():
    """Rain wets every cell, so the skip runs the whole window; a CPU library without
    mf_run_batch_skip (an older prebuilt one) runs the plain batch."""
    _need_cpu_build()
    b = mfb.CpuBackend()
    assert b.skip and hasattr(b.lib, "mf_run_batch_skip")
    with open(mfb.CPU_SOURCE) as f:
        src = f.read()
    assert "if (p.rain != 0u" in src                   # mf_rows: no skip when it rains
    b.skip = False
    called = []
    b.lib = type("L", (), {"mf_run_batch": lambda *a: called.append(a) or 0})()
    b._ptrs = None
    p = mfb.Params()
    p.wr, p.wc = 3, 4
    b.run_batch(p, 5, mfe.SKIP_CHUNK)
    assert len(called) == 1 and b.cells_run == 60
