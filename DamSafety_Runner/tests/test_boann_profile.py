"""Runner 3.80.0 / Boann 5.2.0: the profile (core/metalflood/backends.py Profile, cli --profile,
bench --profile). It measures; it must change nothing.

- boann_profile_wet_shares_count_what_a_skip_keeps: wet_shares on hand-made windows.
- boann_profile_changes_no_result_on_the_cpu_build: every solver, profile on against off: every
  result file byte for byte, and the profile carries what it says.
- boann_profile_cli_flag_and_stats_json: --profile turns the full profile on for that run only
  and writes it into --stats-json; without it the stats carry the basic host times.
- boann_profile_cases_bracket_the_wet_share: the speed test's diagonal valley is mostly dry
  window, its compact plain mostly wet.
- boann_profile_changes_no_result_on_the_gpu (Metal only): the same on the Apple GPU, where the
  full profile runs the first steps of each batch one kernel per command buffer; every kernel
  of the step is timed.
- boann_threadgroup_size_changes_no_grid (Metal only): DAMSAFETY_MF_TG moves no depth, velocity
  or arrival time."""
import filecmp
import json
import os
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood import subfv_backend as sfb
from nd_breach_runner.core.metalflood.cases import (
    compact_deck, diagonal_valley_deck, valley_deck)
from nd_breach_runner.tests.registry import Skip, test

RESULTS = ("max", "maxVc", "inittm", "maxtm", "mxe", "mass")
SOLVERS = ("acc", "sgc", "fv", "fv2", "dg2", "subfv")
SPECS = {"acc": mfb.FLOOD_SPEC, "sgc": mfb.SGC_SPEC, "fv": mfb.FV_SPEC, "fv2": mfb.FV2_SPEC,
         "dg2": mfb.DG2_SPEC, "subfv": sfb.SUBFV_SPEC}


def _need_cpu_build():
    if mfb.prebuilt_available():
        return
    if os.name == "nt":
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


def _need_metal():
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))


class _Env:
    """Set (or, with None, unset) environment variables for a block, then put them back."""

    def __init__(self, **kv):
        self.kv = kv

    def __enter__(self):
        self.was = {k: os.environ.get(k) for k in self.kv}
        for k, v in self.kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _same_files(a, b, exts=RESULTS):
    for ext in exts:
        fa = os.path.join(a, "results", "run." + ext)
        fb = os.path.join(b, "results", "run." + ext)
        assert os.path.isfile(fa) and os.path.isfile(fb), ext
        assert filecmp.cmp(fa, fb, shallow=False), "run.{} differs with the profile on".format(ext)


@test
def boann_profile_wet_shares_count_what_a_skip_keeps():
    # nothing wet: nothing kept
    sh = mfb.wet_shares(np.zeros((40, 70), bool))
    assert sh["wet"] == 0.0 and sh["row_ranges"] == 0.0 and sh["tiles_16"] == 0.0, sh
    # everything wet: everything kept
    sh = mfb.wet_shares(np.ones((40, 70), bool))
    assert all(abs(v - 1.0) < 1e-12 for v in sh.values()), sh
    # one wet cell in the middle of a 96 x 160 window
    w = np.zeros((96, 160), bool)
    w[40, 70] = True
    sh = mfb.wet_shares(w)
    n = 96.0 * 160.0
    assert abs(sh["wet"] - 1.0 / n) < 1e-12, sh
    # 16-cell tiles: the cell's tile and its 8 neighbours, 9 x 256 cells
    assert abs(sh["tiles_16"] - 9 * 256 / n) < 1e-12, sh
    # 32-cell tiles: tile (1, 2) and its neighbours are tile rows 0..2 x tile columns 1..3
    assert abs(sh["tiles_32"] - 9 * 1024 / n) < 1e-12, sh
    # 64-cell tiles: tile (0, 1); rows 0..1 (the second holds 32 of the 96 rows), columns 0..2
    # (the third holds 32 of the 160 columns)
    assert abs(sh["tiles_64"] - 1.0) < 1e-12, sh
    # row ranges: the rows within 9 of row 40, each the columns within 9 of column 70
    assert abs(sh["row_ranges"] - 19 * 19 / n) < 1e-12, sh
    # a cell near a corner: the ranges and the tiles stop at the window's edge
    w = np.zeros((96, 160), bool)
    w[2, 157] = True
    sh = mfb.wet_shares(w)
    assert abs(sh["row_ranges"] - 12 * 12 / n) < 1e-12, sh
    assert abs(sh["tiles_16"] - 4 * 256 / n) < 1e-12, sh
    # a short last tile counts its own cells only: 40 x 70 with the far corner wet
    w = np.zeros((40, 70), bool)
    w[39, 69] = True
    sh = mfb.wet_shares(w)
    # 32-cell tiles: rows 32..39 and 0..31, columns 64..69 and 32..63: the whole height, 38 columns
    assert abs(sh["tiles_32"] - (40 * 38) / (40.0 * 70.0)) < 1e-12, sh
    # two wet cells far apart in one row: the range spans both (a hull, as the CPU skip keeps)
    w = np.zeros((30, 200), bool)
    w[10, 20] = w[10, 180] = True
    sh = mfb.wet_shares(w)
    assert abs(sh["row_ranges"] - 19 * (180 - 20 + 19) / (30.0 * 200.0)) < 1e-12, sh
    # an empty window is not an error
    assert mfb.wet_shares(np.zeros((0, 0), bool))["wet"] == 0.0


def _run_pair(d, solver, backend, sim_s=900.0):
    """The same deck with the profile off, then on. Returns (stats off, stats on), or None when
    this Python cannot run the solver (sgc builds its channels with the Runner's core/sgc, which
    the numpy-only Metal CI job cannot import)."""
    out = []
    for tag, prof in (("off", False), ("on", True)):
        par = valley_deck(os.path.join(d, solver, tag), sim_s=sim_s)
        with _Env(DAMSAFETY_MF_PROFILE=None):
            try:
                out.append(cli.run(par, solver=solver, backend=backend, quiet=True,
                                   profile=prof))
            except (cli.CliError, ImportError):
                if solver != "sgc":
                    raise
                return None
    _same_files(os.path.join(d, solver, "off"), os.path.join(d, solver, "on"))
    return out


@test
def boann_profile_changes_no_result_on_the_cpu_build():
    _need_cpu_build()
    d = tempfile.mkdtemp(prefix="boann_profile_")
    try:
        ran = []
        for solver in SOLVERS:
            pair = _run_pair(d, solver, "cpu")
            if pair is None:
                continue
            ran.append(solver)
            off, on = pair
            assert off["steps"] == on["steps"] and off["steps"] > 0, (solver, off, on)
            assert off["mass_error_m3"] == on["mass_error_m3"], (solver, off, on)
            po, pn = off["profile"], on["profile"]
            assert po["mode"] == "basic" and "wet" not in po and "kernels" not in po, (solver, po)
            assert po["host"]["run_s"] > 0 and po["batches"] == off["batches"], (solver, po)
            assert pn["mode"] == "full" and pn["backend"] == "cpu", (solver, pn)
            assert pn["batches"] == on["batches"], (solver, pn)
            assert pn["steps_dispatched"] >= on["steps"], (solver, pn)
            assert pn["window_cell_steps"] > 0, (solver, pn)
            wet = pn["wet"]
            assert wet["looks"] >= 1, (solver, wet)
            assert 0.0 < wet["wet"] < 1.0, (solver, wet)
            assert wet["wet"] <= wet["row_ranges"] <= 1.0, (solver, wet)
            assert wet["wet"] <= wet["tiles_16"] <= wet["tiles_32"] <= wet["tiles_64"] <= 1.0, \
                (solver, wet)
            assert 0.0 < wet["window_share_of_grid"] <= 1.0, (solver, wet)
            assert len(wet["series"]) <= mfb.PROFILE_SERIES_MAX, solver
            assert len(wet["series"][0]) == len(wet["series_columns"]), solver
            assert cli.profile_lines(on) and cli.profile_lines(off), solver
            json.dumps(pn)                                   # JSON-ready
        assert len(ran) >= len(SOLVERS) - 1, ran
    finally:
        shutil.rmtree(d, True)


@test
def boann_profile_cli_flag_and_stats_json():
    _need_cpu_build()
    d = tempfile.mkdtemp(prefix="boann_profile_cli_")
    try:
        with _Env(DAMSAFETY_MF_PROFILE=None):
            par = valley_deck(os.path.join(d, "a"), sim_s=300.0)
            js = os.path.join(d, "a.json")
            assert cli.main(["boann", "run", par, "--backend", "cpu", "--quiet", "--profile",
                             "--stats-json", js]) == 0
            assert mfb.PROFILE_ENV not in os.environ          # on for that run only
            with open(js, encoding="utf-8") as f:
                st = json.load(f)
            assert st["profile"]["mode"] == "full" and "wet" in st["profile"], st["profile"]
            par = valley_deck(os.path.join(d, "b"), sim_s=300.0)
            js = os.path.join(d, "b.json")
            assert cli.main(["boann", "run", par, "--backend", "cpu", "--quiet",
                             "--stats-json", js]) == 0
            with open(js, encoding="utf-8") as f:
                st = json.load(f)
            assert st["profile"]["mode"] == "basic", st["profile"]
        _same_files(os.path.join(d, "a"), os.path.join(d, "b"))
        # an environment that already asks for the profile is left as it was
        with _Env(DAMSAFETY_MF_PROFILE="1"):
            par = valley_deck(os.path.join(d, "c"), sim_s=300.0)
            st = cli.run(par, backend="cpu", quiet=True, profile=True)
            assert os.environ.get(mfb.PROFILE_ENV) == "1"
            assert st["profile"]["mode"] == "full"
        # the threadgroup size is refused when it is not WIDTHxHEIGHT
        for bad in ("32", "0x8", "axb", "32x8x1"):
            with _Env(DAMSAFETY_MF_TG=bad):
                try:
                    mfb.threadgroup_env()
                    assert False, "expected BackendError for " + bad
                except mfb.BackendError:
                    pass
        with _Env(DAMSAFETY_MF_TG="16x4"):
            assert mfb.threadgroup_env() == (16, 4)
        with _Env(DAMSAFETY_MF_TG=None):
            assert mfb.threadgroup_env() is None
    finally:
        shutil.rmtree(d, True)


@test
def boann_profile_cases_bracket_the_wet_share():
    _need_cpu_build()
    d = tempfile.mkdtemp(prefix="boann_profile_cases_")
    try:
        got = {}
        for name, make in (("diagonal", diagonal_valley_deck), ("compact", compact_deck)):
            par = make(os.path.join(d, name), ny=120, nx=180, dx=10.0, peak_cms=300.0,
                       sim_s=3600.0)
            with _Env(DAMSAFETY_MF_PROFILE=None):
                st = cli.run(par, backend="cpu", quiet=True, profile=True)
            assert abs(st["mass_error_m3"]) < 1e-3 * st["injected_m3"], (name, st)
            got[name] = st["profile"]["wet"]
        # the diagonal valley's window is mostly dry ground; the plain's holds several times
        # the water, and its row ranges cover most of what the window does
        assert got["diagonal"]["wet"] < 0.10, got
        assert got["compact"]["wet"] > 3.0 * got["diagonal"]["wet"], got
        assert got["diagonal"]["row_ranges"] < 0.5 * got["compact"]["row_ranges"], got
    finally:
        shutil.rmtree(d, True)


@test
def boann_profile_changes_no_result_on_the_gpu():
    _need_metal()
    d = tempfile.mkdtemp(prefix="boann_profile_gpu_")
    try:
        ran = []
        for solver in SOLVERS:
            pair = _run_pair(d, solver, "metal")
            if pair is None:
                continue
            ran.append(solver)
            off, on = pair
            assert off["backend"] == on["backend"] == "metal", (solver, off, on)
            assert off["steps"] == on["steps"] and off["steps"] > 0, (solver, off, on)
            po, pn = off["profile"], on["profile"]
            assert po["mode"] == "basic" and po["host"]["encode_s"] > 0, (solver, po)
            assert po["host"]["encode_steps"] == po["steps_dispatched"], (solver, po)
            assert "gpu" not in po and "kernels" not in po, (solver, po)
            assert pn["mode"] == "full", (solver, pn)
            assert pn["host"]["encode_steps"] + pn["split"]["steps"] == pn["steps_dispatched"], \
                (solver, pn)
            assert pn["gpu"]["gpu_s"] > 0 and pn["gpu"]["steps"] == pn["host"]["encode_steps"], \
                (solver, pn)
            # every kernel of the step is timed, in the step's order of first use
            names = [k["name"] for k in pn["kernels"]]
            assert set(names) == set(SPECS[solver].kernels), (solver, names)
            assert abs(sum(k["share"] for k in pn["kernels"]) - 1.0) < 1e-3, (solver, pn)
            assert all(k["calls"] > 0 and k["gpu_s"] >= 0 for k in pn["kernels"]), (solver, pn)
            assert pn["wet"]["looks"] >= 1 and 0.0 < pn["wet"]["wet"] < 1.0, (solver, pn)
            assert pn["threadgroup"], (solver, pn)
        assert len(ran) >= len(SOLVERS) - 1, ran
    finally:
        shutil.rmtree(d, True)


@test
def boann_threadgroup_size_changes_no_grid():
    _need_metal()
    d = tempfile.mkdtemp(prefix="boann_tg_")
    try:
        for solver in ("acc", "fv2"):
            sizes = {}
            for tag, tg in (("default", None), ("small", "16x4"), ("row", "32x1")):
                par = valley_deck(os.path.join(d, solver, tag), sim_s=900.0)
                with _Env(DAMSAFETY_MF_TG=tg, DAMSAFETY_MF_PROFILE=None):
                    st = cli.run(par, solver=solver, backend="metal", quiet=True)
                sizes[tag] = st["profile"]["threadgroup"]
            assert all(v == [16, 4] for v in sizes["small"].values()), sizes
            assert all(v[1] == 1 for v in sizes["row"].values()), sizes
            for tag in ("small", "row"):
                _same_files(os.path.join(d, solver, "default"), os.path.join(d, solver, tag),
                            exts=("max", "maxVc", "inittm", "maxtm", "mxe"))
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    # the macOS Metal CI job runs this file (as it does test_metalflood)
    from nd_breach_runner.tests.registry import TESTS
    fails = 0
    for fn in [f for f in TESTS if f.__module__ == __name__]:
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
