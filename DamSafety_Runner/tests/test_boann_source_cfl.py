"""Boann's inflow-aware time step (Boann 6.13.0, "sourcecfl" in the .par; Runner model.source_cfl).

LISFLOOD-FP 8.3's step, cfl dx / sqrt(g h_max), looks only at the deepest water. A large point
source pushes water out of its cell faster than that: at 10 m on Lake Delhi (9 breach cells, about
443 m3/s each at the peak) the breach cells overshot and checkerboarded 3.5 km of valley at CFL 0.7.
With "sourcecfl" the step is also held to cfl dx / (q / h_s + sqrt(g h_s)) at each source cell
(q = Q / dx, h_s the cell's depth, at least critical depth), so CFL 0.7 gives the converged flood.

Checked here on the CPU build: the keyword is read and is off by default; a single 500 m3/s source
cell on a 10 m plain, where CFL 0.7 puts 5.0 m in the source cell against the converged 1.93 m
(CFL 0.1), comes within 0.3 m of it with the bound (the plain engine) and to the mm (sub-grid local
inertial, r = 2), with the rest of the grid unchanged to the mm and the mass balance closed. On a
Mac the Metal GPU runs it too and agrees with the CPU build."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood.cases import _inflow_deck, read_asc
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.test_boann_dry_skip import _need_cpu_build

N = 61
DX = 10.0
PEAK = 500.0          # m3/s into one 10 m cell
SRC = (N // 2, 3)


def _deck(folder, extra):
    z = 100.0 + 0.001 * DX * np.arange(N)[None, :] + np.zeros((N, 1))
    return _inflow_deck(folder, z, DX, SRC, PEAK, 1.5 * 3600.0, "NSEW", 0.05, extra)


def _max(folder):
    h = read_asc(os.path.join(folder, "results", "run.max"))
    return np.asarray(h[0] if isinstance(h, tuple) else h, float)


def _runs(run_par, extra=(), backend="cpu"):
    """{name: (peak depth grid, stats)} for the reference (CFL 0.1), plain CFL 0.7 and CFL 0.7 with
    the inflow-aware step, each in its own folder."""
    root = tempfile.mkdtemp()
    out = {}
    try:
        for name, keys in (("ref", ("cfl 0.1",)), ("plain", ("cfl 0.7",)),
                           ("src", ("cfl 0.7", "sourcecfl"))):
            d = os.path.join(root, name)
            st = run_par(_deck(d, keys + tuple(extra)), backend=backend)
            out[name] = (_max(d), st)
    finally:
        shutil.rmtree(root, True)
    return out


def _source_cell(ref, plain):
    """The cell where the plain run is furthest from the reference (the source cell) and a mask of
    every cell more than one cell from it."""
    r, c = np.unravel_index(np.argmax(np.abs(plain - ref)), ref.shape)
    far = np.ones(ref.shape, bool)
    far[max(r - 1, 0):r + 2, max(c - 1, 0):c + 2] = False
    return (r, c), far


@test
def boann_source_cfl_is_read_and_off_by_default():
    d = tempfile.mkdtemp()
    try:
        assert not mfe.Deck(_deck(os.path.join(d, "a"), ("cfl 0.7",))).source_cfl
        assert mfe.Deck(_deck(os.path.join(d, "b"), ("cfl 0.7", "sourcecfl"))).source_cfl
        p = mfb.Params()
        assert p.src_cfl == 0
    finally:
        shutil.rmtree(d, True)


@test
def boann_source_cfl_brings_the_inflow_cell_to_the_converged_depth():
    _need_cpu_build()
    r = _runs(mfe.run_par)
    ref, plain, src = r["ref"][0], r["plain"][0], r["src"][0]
    cell, far = _source_cell(ref, plain)
    # the plain step overshoots the source cell by metres; the bound brings it close
    assert plain[cell] - ref[cell] > 2.0, (plain[cell], ref[cell])
    assert abs(src[cell] - ref[cell]) < 0.4, (src[cell], ref[cell])
    # the rest of the grid is the same flood to the mm
    assert np.abs(src - ref)[far].max() < 0.005, np.abs(src - ref)[far].max()
    # the step is cut only while the source pours in: more steps than plain, fewer than CFL 0.1
    assert r["plain"][1]["steps"] < r["src"][1]["steps"] < r["ref"][1]["steps"]
    st = r["src"][1]
    assert abs(st["mass_error_m3"]) < 1e-4 * st["injected_m3"], st["mass_error_m3"]


@test
def boann_source_cfl_subli_matches_the_converged_run():
    _need_cpu_build()
    from nd_breach_runner.core.metalflood import engine_subli
    r = _runs(engine_subli.run_par, extra=("subgrid 2",))
    ref, plain, src = r["ref"][0], r["plain"][0], r["src"][0]
    cell, far = _source_cell(ref, plain)
    assert plain[cell] - ref[cell] > 1.0, (plain[cell], ref[cell])
    assert np.abs(src - ref).max() < 0.005, np.abs(src - ref).max()
    assert r["src"][1]["steps"] < r["ref"][1]["steps"]


@test
def runner_source_cfl_writes_sourcecfl_for_boann_acc_and_subli_only():
    """Runner 3.104.0: model.source_cfl (CLI) / RunSettings.user_source_cfl -> LisfloodSettings
    source_cfl -> 'sourcecfl' in the .par of Boann's ACC and sub-grid local-inertial engines only
    (LISFLOOD-FP itself and the other engines never get the keyword); off by default."""
    from nd_breach_runner.core import lisflood as lf, workflow
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    import nd_breach_runner.scripts.ndbreach_cli as cli
    assert cli.MODEL_KEYS["source_cfl"] == ("user_source_cfl", ("bool",))
    assert workflow.RunSettings().user_source_cfl is False
    dem = Grid(np.full((4, 4), 10.0), 0.0, 40.0, 10.0)
    hyd = Hydrograph(np.array([0.0, 60.0]), np.array([1.0, 0.0]), "t")
    d = tempfile.mkdtemp(prefix="srccfl_")
    try:
        def par(exe, **kw):
            wd = tempfile.mkdtemp(dir=d)
            st = lf.LisfloodSettings(exe, sim_time_s=60.0, **kw)
            with open(lf.write_inputs(wd, dem, (5.0, 20.0), hyd, st), encoding="utf-8") as f:
                return f.read().split("\n")
        assert "sourcecfl" not in par(lf.GPU_EXE)
        assert "sourcecfl" in par(lf.GPU_EXE, source_cfl=True)
        assert "sourcecfl" in par(lf.GPU_SUBLI_EXE, source_cfl=True)
        for exe in (lf.BUILTIN_EXE, lf.GPU_FV_EXE, lf.GPU_SUBFV_EXE):
            assert "sourcecfl" not in par(exe, source_cfl=True), exe
    finally:
        shutil.rmtree(d, True)


@test
def boann_source_cfl_gpu_matches_cpu_build():
    """Runs only on a Mac with a Metal GPU."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        keys = ("cfl 0.7", "sourcecfl")
        sa = mfe.run_par(_deck(os.path.join(d, "a"), keys), backend="cpu")
        sb = mfe.run_par(_deck(os.path.join(d, "b"), keys), backend="metal")
        assert sb["backend"] == "metal"
        assert abs(sa["steps"] - sb["steps"]) <= max(2, sa["steps"] // 1000), (sa["steps"], sb["steps"])
        assert np.abs(_max(os.path.join(d, "a")) - _max(os.path.join(d, "b"))).max() < 0.01
    finally:
        shutil.rmtree(d, True)


@test
def boann_source_cfl_opencl_matches_cpu_build():
    """Runs only where OpenCL lists a device (the OpenCL workflow's PoCL, an Intel Arc)."""
    from nd_breach_runner.core.metalflood import opencl_backend
    ok, why = opencl_backend.opencl_available()
    if not ok:
        raise Skip("no OpenCL device here ({})".format(why))
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        keys = ("cfl 0.7", "sourcecfl")
        sa = mfe.run_par(_deck(os.path.join(d, "a"), keys), backend="cpu")
        sb = mfe.run_par(_deck(os.path.join(d, "b"), keys), backend="opencl")
        assert abs(sa["steps"] - sb["steps"]) <= max(2, sa["steps"] // 1000), (sa["steps"], sb["steps"])
        assert np.abs(_max(os.path.join(d, "a")) - _max(os.path.join(d, "b"))).max() < 0.01
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_source_cfl_is_read_and_off_by_default,
               runner_source_cfl_writes_sourcecfl_for_boann_acc_and_subli_only,
               boann_source_cfl_brings_the_inflow_cell_to_the_converged_depth,
               boann_source_cfl_subli_matches_the_converged_run,
               boann_source_cfl_gpu_matches_cpu_build,
               boann_source_cfl_opencl_matches_cpu_build):
        try:
            fn()
            print("ok  ", fn.__name__)
        except Skip as e:
            print("skip", fn.__name__, e)
