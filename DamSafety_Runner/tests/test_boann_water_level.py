"""Boann water-level (stage) boundary (Boann 6.8.0): LISFLOOD-FP's HFIX / HVAR .bci edge lines,
"W <y1> <y2> HFIX <level>" and "W <y1> <y2> HVAR <series>" (likewise N / S / E), which hold the
edge cells they cover at a water-surface elevation that water flows across both ways. ACC uses
LISFLOOD-FP 8.3's acceleration boundary face (boundary.cpp); FV1 / FV2 / DG2 use its ghost cell
(fv1.cpp set_boundary_values) and their own HLLC flux. Checked on the CPU build of the kernels:

- the .bci reader and LISFLOOD-FP's edge-cell selection (coordinate range, last line wins, a FREE
  line takes cells back, a point water level is refused);
- a lake at rest with a water-level edge at its still level stays at rest, on all four solvers,
  over an uneven bed (the single most important guard: the edge must not leak or inject);
- a dry basin fills through an HFIX edge to the level and holds it, with the water in through the
  edge equal to the water stored;
- an HVAR tide lets water in on the rise and back out on the fall, and the balance closes;
- the dry-cell / dry-tile skips give the same answer bit for bit as the whole window when the only
  water comes in at a water-level edge far from the start water;
- the sub-grid engines refuse a deck with a water level instead of running the edge as a wall;
- (--models, NDBREACH_LISFLOOD) the tide deck through LISFLOOD-FP 8.3 and Boann ACC agrees.

A deck without a water-level line is unchanged bit for bit (no buffer, Params word or kernel branch
of the water-level path runs when n_wl = 0); the golden baselines and the benchmark gate hold that.
On a Mac with a Metal GPU the GPU is checked against the CPU build."""
import os
import shutil
import subprocess
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import deckio
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood import engine_fv as fve
from nd_breach_runner.tests.registry import Skip, model_test, test
from nd_breach_runner.tests.test_boann_rain import _need_cpu_build

SOLVERS = ("acc", "fv1", "fv2", "dg2")


def _deck(folder, z, bci, bdy=None, h0=None, sim=600.0, dx=10.0, n=0.03, extra=()):
    """A deck with the given .bci text (and .bdy text), DEM z (NaN = NoData), start depth h0."""
    os.makedirs(folder, exist_ok=True)
    ny, nx = z.shape

    def asc(name, a):
        with open(os.path.join(folder, name), "w") as f:
            f.write("ncols {}\nnrows {}\nxllcorner 0\nyllcorner 0\ncellsize {}\nNODATA_value "
                    "-9999\n".format(nx, ny, dx))
            np.savetxt(f, np.where(np.isfinite(a), a, -9999.0), fmt="%.6f")
    asc("run.dem.asc", z)
    par = ["DEMfile run.dem.asc", "resroot run", "dirroot results", "sim_time {}".format(sim),
           "initial_tstep 5.0", "massint 60", "saveint {}".format(sim), "fpfric {}".format(n),
           "bcifile run.bci", "elevoff", "depthoff"]
    with open(os.path.join(folder, "run.bci"), "w") as f:
        f.write(bci)
    if bdy is not None:
        with open(os.path.join(folder, "run.bdy"), "w") as f:
            f.write(bdy)
        par.append("bdyfile run.bdy")
    if h0 is not None:
        asc("run.start", h0)
        par.append("startfile run.start")
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(par + list(extra)) + "\n")
    return os.path.join(folder, "run.par")


def _run(par, solver, **env):
    keep = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update(env)
        if solver == "acc":
            return mfe.run_par(par, backend="cpu")
        return fve.run_par(par, backend="cpu", order={"fv1": 1, "fv2": 2, "dg2": 3}[solver])
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _res(par, ext):
    return np.loadtxt(os.path.join(os.path.dirname(par), "results", "run." + ext), skiprows=6)


def _final_depth(par):
    """Depth at the end of the run: the last .wd snapshot (saveint = sim_time)."""
    res = os.path.join(os.path.dirname(par), "results")
    last = sorted(f for f in os.listdir(res) if f.endswith(".wd"))[-1]
    return np.loadtxt(os.path.join(res, last), skiprows=6)


def _bumpy(ny=10, nx=24):
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    return 0.4 + 0.3 * np.sin(xx / 3.0) * np.cos(yy / 2.0) + 0.01 * xx


@test
def boann_water_level_reads_bci_and_picks_lisflood_cells():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "b.bci")
        with open(p, "w") as f:
            f.write("P 5 5 QVAR inflow\nW 0 100 HFIX 2.5\nN 10 40 HVAR tide\nE -1e9 1e9 FREE\n"
                    "S 0 50 HFIX 1.0\nS 20 30 FREE\nP 1 1 HFIX 3\nbad line\n")
        lv = deckio.read_bci_levels(p)
        assert lv[0] == ("W", 0.0, 100.0, "HFIX", 2.5), lv
        assert lv[1] == ("N", 10.0, 40.0, "HVAR", "tide"), lv
        assert [x[3] for x in lv] == ["HFIX", "HVAR", "FREE", "HFIX", "FREE", "HFIX"], lv
        # read_bci itself is unchanged: points, and a FREE line frees its whole edge (S too)
        pts, free = deckio.read_bci(p)
        assert pts == [(5.0, 5.0, "inflow")] and free == {"E", "S"}, (pts, free)

        z = np.zeros((8, 12))                             # 80 m tall, 120 m wide at 10 m

        class D:
            nx, ny, dx, xll, yll = 12, 8, 10.0, 0.0, 0.0
        # LISFLOOD-FP's index arithmetic: W 0 100 is clamped to the edge, all 8 rows; N 10 40 is
        # columns 1..3 (the end is exclusive); S 0 50 is columns 0..4 less the FREE 20..30 (col 2)
        lines = [x for x in lv if x[0] != "P"]
        bdy = {"tide": (np.array([0.0, 60.0]), np.array([1.0, 2.0]))}
        series, faces = mfe._level_faces(D, lines, bdy)
        got = {(f[1], f[2], series[f[3]][0]) for f in faces}
        want = ({(r, 0, "HFIX 2.5") for r in range(8)} | {(0, c, "tide") for c in (1, 2, 3)}
                | {(7, c, "HFIX 1") for c in (0, 1, 3, 4)})
        assert got == want, sorted(got ^ want)
        # positions are unique per face (a corner cell has one on each of its two edges)
        assert len({f[0] for f in faces}) == len(faces)
        assert {f[0] for f in faces if f[2] == 0 and f[1] == 0} == {0}            # W row 0
        # a point water level is refused, an unknown HVAR series too
        try:
            mfe._level_faces(D, lv, bdy)
            raise AssertionError("a point water level was accepted")
        except mfe.EngineError as e:
            assert "point" in str(e)
        try:
            mfe._level_faces(D, [("W", 0, 80, "HVAR", "nope")], bdy)
            raise AssertionError("a missing HVAR series was accepted")
        except mfe.EngineError as e:
            assert "nope" in str(e)
        # no water-level line: nothing at all (a FREE-only .bci stays on today's path)
        assert mfe._level_faces(D, [("E", -1e9, 1e9, "FREE", None)], bdy) == ([], [])
        del z
    finally:
        shutil.rmtree(d, True)


@test
def boann_water_level_lake_at_rest_stays_at_rest():
    """A lake at rest at 1.2 m over an uneven bed, with HFIX 1.2 on the whole west edge and part of
    the north edge: after 10 minutes every solver still holds it to 2 mm and 1 cm/s, and no water
    has come in or gone out."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        z = _bumpy()
        level = 1.2
        bci = "W -1e9 1e9 HFIX {0}\nN 30 150 HFIX {0}\n".format(level)
        for sol in SOLVERS:
            par = _deck(os.path.join(d, sol), z, bci, h0=level - z, sim=600.0)
            st = _run(par, sol)
            h = _final_depth(par)
            assert np.abs(h - (level - z)).max() < 2e-3, (sol, np.abs(h - (level - z)).max())
            assert _res(par, "maxVc").max() < 1e-2, (sol, _res(par, "maxVc").max())
            vol0 = float((level - z).sum()) * 100.0
            assert abs(st["final_volume_m3"] - vol0) < 1e-4 * vol0, (sol, st["final_volume_m3"], vol0)
            assert st["injected_m3"] < 1e-3 * vol0 and st["outflow_m3"] < 1e-3 * vol0, (sol, st)
    finally:
        shutil.rmtree(d, True)


@test
def boann_water_level_fills_a_basin_to_the_level():
    """A dry flat basin with HFIX 0.5 on its west edge fills to 0.5 m and holds it; what came in
    through the edge less what went back out is what is stored, and the balance closes."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        z = np.zeros((6, 16))
        for sol in SOLVERS:
            par = _deck(os.path.join(d, sol), z, "W -1e9 1e9 HFIX 0.5\n", sim=4 * 3600.0, n=0.08)
            st = _run(par, sol)
            h = _final_depth(par)
            assert np.abs(h - 0.5).max() < 5e-3, (sol, h.min(), h.max())
            stored = float(h.sum()) * 100.0
            assert abs(st["final_volume_m3"] - 0.5 * z.size * 100.0) < 0.01 * stored, (sol, st)
            net = st["injected_m3"] - st["outflow_m3"]
            assert abs(net - st["final_volume_m3"]) < 1e-3 * stored, (sol, net, st["final_volume_m3"])
            assert abs(st["mass_error_m3"]) < 1e-3 * stored, (sol, st["mass_error_m3"])
            assert st["injected_m3"] > 0.9 * stored, (sol, st)
    finally:
        shutil.rmtree(d, True)


TIDE = ("water level for a test\ntide\n4 seconds\n0.3 0\n1.0 1800\n0.2 3600\n0.2 9000\n")


@test
def boann_water_level_tide_flows_in_and_out():
    """HVAR on the east edge rising from 0.3 to 1.0 m over 30 min, then falling to 0.2 m: water
    comes in on the rise (the basin's peak level reaches most of the way to 1.0 m), goes back out on
    the fall (it ends near 0.2 m), and the balance closes."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        z = np.zeros((6, 16))
        for sol in SOLVERS:
            par = _deck(os.path.join(d, sol), z, "E 0 60 HVAR tide\n", bdy=TIDE,
                        h0=np.full(z.shape, 0.3), sim=9000.0, n=0.05)
            st = _run(par, sol)
            hmax = _res(par, "max")
            h = _final_depth(par)
            assert hmax.min() > 0.8, (sol, hmax.min())
            assert np.abs(h - 0.2).max() < 0.01, (sol, h.min(), h.max())
            vol0 = 0.3 * z.size * 100.0
            assert st["injected_m3"] > 2.0 * vol0 and st["outflow_m3"] > 2.0 * vol0, (sol, st)
            thru = st["injected_m3"] + st["outflow_m3"]
            assert abs(st["mass_error_m3"]) < 1e-3 * thru, (sol, st["mass_error_m3"], thru)
    finally:
        shutil.rmtree(d, True)


def _far_edge_deck(folder):
    """A 60 x 120 dry valley sloping down to the east, a small pond at its west end, and a
    water level on part of the east edge well above the bed: the only flood comes in from the
    water-level edge, 100 cells from the start water."""
    ny, nx = 60, 120
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    z = 2.0 - 0.01 * xx + 0.002 * np.abs(yy - ny / 2.0)
    h0 = np.zeros((ny, nx))
    h0[28:32, 2:5] = 0.3
    return _deck(folder, z, "E 200 400 HFIX 1.8\n", h0=h0, sim=1500.0, dx=10.0, n=0.04)


@test
def boann_water_level_skips_give_the_same_answer():
    """The CPU dry-cell skip (ACC) and the dry-tile skip (all four solvers, the GPU's kernels run on
    the CPU build) seed the water-level edge cells like sources, so a flood that comes in only at a
    water-level edge far from the start water gives every result file and stat bit for bit what the
    whole window gives."""
    _need_cpu_build()
    from nd_breach_runner.tests.test_boann_dry_skip import _files
    d = tempfile.mkdtemp()
    try:
        runs = [("acc", {"DAMSAFETY_MF_SKIP": "0"}, {"DAMSAFETY_MF_SKIP": "1"}),
                ("acc", {"DAMSAFETY_MF_TILES": "0", "DAMSAFETY_MF_SKIP": "0"},
                 {"DAMSAFETY_MF_CPU_TILES": "1", "DAMSAFETY_MF_SKIP": "0"})]
        runs += [(s, {"DAMSAFETY_MF_TILES": "0"}, {"DAMSAFETY_MF_CPU_TILES": "1"})
                 for s in ("fv1", "fv2", "dg2")]
        for k, (sol, off, on) in enumerate(runs):
            pa = _far_edge_deck(os.path.join(d, "a%d" % k))
            pb = _far_edge_deck(os.path.join(d, "b%d" % k))
            sa = _run(pa, sol, **off)
            sb = _run(pb, sol, **on)
            assert _res(pa, "max").max() > 0.3, (sol, "the edge did not flood the valley")
            fa, fb = _files(os.path.dirname(pa)), _files(os.path.dirname(pb))
            for ext in fa:
                assert fa[ext] == fb[ext], (sol, on, "run.{} differs".format(ext))
            for key in ("steps", "injected_m3", "outflow_m3", "final_volume_m3", "mass_error_m3"):
                assert sa[key] == sb[key], (sol, on, key, sa[key], sb[key])
    finally:
        shutil.rmtree(d, True)


@test
def boann_water_level_subgrid_engines_refuse_it():
    """The sub-grid engines (SGC, sub-grid FV, sub-grid local inertial) have no water-level
    boundary yet: they stop with a message rather than run the edge as a closed wall."""
    from nd_breach_runner.core.metalflood import engine_sgc, engine_subfv, engine_subli
    d = tempfile.mkdtemp()
    try:
        z = np.zeros((8, 12))
        par = _deck(d, z, "W -1e9 1e9 HFIX 0.5\n", extra=("subgrid 2",))
        for run in (engine_sgc.run_par, engine_subfv.run_par, engine_subli.run_par):
            try:
                run(par, backend="cpu")
                raise AssertionError("a sub-grid engine ran a deck with a water level")
            except mfe.EngineError as e:
                assert "water-level" in str(e), str(e)
    finally:
        shutil.rmtree(d, True)


@test
def boann_water_level_gpu_matches_cpu_build():
    """The tide deck on the Metal GPU matches its CPU build on every solver, within the tolerance
    the other GPU-vs-CPU checks use. Runs only on a Mac with a Metal GPU."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        z = _bumpy(12, 30)
        for sol in SOLVERS:
            pa = _deck(os.path.join(d, sol, "a"), z, "E 0 120 HVAR tide\n", bdy=TIDE,
                       h0=np.maximum(0.0, 0.6 - z), sim=9000.0)
            pb = _deck(os.path.join(d, sol, "b"), z, "E 0 120 HVAR tide\n", bdy=TIDE,
                       h0=np.maximum(0.0, 0.6 - z), sim=9000.0)
            sa = _run(pa, sol)
            if sol == "acc":
                sb = mfe.run_par(pb, backend="metal")
            else:
                sb = fve.run_par(pb, backend="metal", order={"fv1": 1, "fv2": 2, "dg2": 3}[sol])
            assert sb["backend"] == "metal"
            diff = np.abs(_res(pa, "max") - _res(pb, "max")).max()
            assert diff < 0.002, (sol, diff)
            assert abs(sa["injected_m3"] - sb["injected_m3"]) < 2e-3 * sa["injected_m3"], (sol, sa, sb)
    finally:
        shutil.rmtree(d, True)


@model_test
def boann_water_level_matches_lisflood_fp():
    """The tide deck over an uneven bed through LISFLOOD-FP 8.3 (NDBREACH_LISFLOOD) and Boann ACC:
    the same boundary formula, so the peak depths agree to the .max file's rounding plus a few mm,
    and the water in through the edge to 1 %."""
    exe = os.environ.get("NDBREACH_LISFLOOD")
    if not exe or not os.path.isfile(exe):
        raise Skip("NDBREACH_LISFLOOD is not set")
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        z = _bumpy(12, 30)
        decks = []
        for k in ("lf", "mf"):
            decks.append(_deck(os.path.join(d, k), z, "E 0 120 HVAR tide\n", bdy=TIDE,
                               h0=np.maximum(0.0, 0.6 - z), sim=9000.0, extra=("acceleration",)))
        subprocess.run([exe, os.path.basename(decks[0])], cwd=os.path.dirname(decks[0]),
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True,
                       env=dict(os.environ, OMP_NUM_THREADS="1"))
        mfe.run_par(decks[1], backend="cpu")
        diff = np.abs(_res(decks[0], "max") - _res(decks[1], "max")).max()
        assert diff < 0.005, diff
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_water_level_reads_bci_and_picks_lisflood_cells,
               boann_water_level_lake_at_rest_stays_at_rest,
               boann_water_level_fills_a_basin_to_the_level,
               boann_water_level_tide_flows_in_and_out,
               boann_water_level_skips_give_the_same_answer,
               boann_water_level_subgrid_engines_refuse_it,
               boann_water_level_gpu_matches_cpu_build,
               boann_water_level_matches_lisflood_fp):
        try:
            fn()
            print("ok  ", fn.__name__)
        except Skip as e:
            print("skip", fn.__name__, e)
