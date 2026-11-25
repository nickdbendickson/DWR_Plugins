#!/usr/bin/env python3
"""Check Boann's prebuilt CPU libraries for this computer (the release workflow runs it on Windows,
Linux and macOS before the libraries ship): each engine's library under <dir>/<platform>/ opens,
agrees with Python on the Params layout, reports its cores, and runs a short valley flood that
conserves water. BoannGrid's libraries are checked too: the priority-flood fill opens and gives
the same numbers as its pure-Python twin and the relaxation kernel, and the D4 and D8 flow
libraries open and route that filled surface with no dead ends. The libraries are copied into DamSafety_Core/boann/hydro/prebuilt/<platform>/ for the run,
exactly where a release zip puts them, and removed afterwards.

    python tools/boann_cpu_smoke.py build/boann"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# Boann lives once, in DamSafety Core (DamSafety_Core/boann: hydro = the flood engine, grid = BoannGrid)
sys.path.insert(0, REPO)
from DamSafety_Core.boann.hydro import backends as be                                   # noqa: E402
from DamSafety_Core.boann.hydro import engine as mfe, engine_fv as fve, engine_fv2 as fv2e  # noqa: E402
from DamSafety_Core.boann.hydro import engine_dg2 as dg2e, engine_sgc as sgce             # noqa: E402
from DamSafety_Core.boann.hydro import engine_subfv as sfve, subfv_backend as sfb         # noqa: E402
from DamSafety_Core.boann.hydro import engine_subli as slie, subli_backend as slb         # noqa: E402
from DamSafety_Core.boann.hydro.cases import valley_deck                                # noqa: E402
from DamSafety_Core.boann.grid import cases as mt_cases                                 # noqa: E402
from DamSafety_Core.boann.grid import fill as mt_fill                                   # noqa: E402
from DamSafety_Core.boann.grid import flow as mt_flow                                   # noqa: E402

ENGINES = (("acc", mfe.run_par, be.FLOOD_SPEC), ("fv", fve.run_par, be.FV_SPEC),
           ("fv2", fv2e.run_par, be.FV2_SPEC), ("dg2", dg2e.run_par, be.DG2_SPEC),
           ("sgc", sgce.run_par, be.SGC_SPEC), ("subfv", sfve.run_par, sfb.SUBFV_SPEC),
           ("subli", slie.run_par, slb.SUBLI_SPEC))


def _boanngrid_checks(dest):
    """(name, function) pairs; each raises on a failure. Everything must load from `dest`."""
    import numpy as np
    mbe = mt_fill.be                                     # metalflood.backends as the plugin sees it

    def fill():
        mt_fill._PF_LIB = None
        lib = mt_fill._pf_library()
        assert os.path.dirname(lib._name) == dest, lib._name
        z = mt_cases.pitted_plane(ny=40, nx=60)
        valid = np.isfinite(z) & (z != mt_fill.NODATA)
        for conn in (4, 8):
            f, info = mt_fill.fill_dem(z, method="priority-flood", conn=conn)
            assert info["backend"] == "cpu" and info["unreached"] == 0, info
            py, _ = mt_fill._fill_pf_python(z, valid, mt_fill._outlets(valid, conn), 1e-5, conn)
            assert np.array_equal(f, py, equal_nan=True), "C++ and Python fills differ"
            old, iold = mt_fill.fill_dem(z, method="relax", backend="cpu", conn=conn)
            assert iold["converged"], iold
            assert float(np.nanmax(np.abs(f - old))) < 1e-4, "fill moved off the relaxation"
        return "priority-flood + relax kernel"

    def flow(conn):
        def run():
            bk = mbe.CpuBackend(mt_flow._SPEC_FOR[conn])
            assert os.path.dirname(bk.path) == dest, bk.path
            bk.close()
            z = mt_cases.pitted_plane(ny=40, nx=60)
            _d, acc, info = mt_flow.flow_accum(z, backend="cpu", conn=conn)
            assert info["interior_sinks"] == 0, info
            assert int(np.nanmax(acc)) > 1
            return "{} cells drain to the largest outlet".format(int(np.nanmax(acc)))
        return run

    return (("fill", fill), ("flow4", flow(4)), ("flow8", flow(8)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        raise SystemExit(__doc__)
    src = os.path.join(argv[0], be.platform_tag())
    if not os.path.isdir(src):
        raise SystemExit("no {} in {}".format(be.platform_tag(), argv[0]))
    dest = os.path.join(be.PREBUILT_DIR, be.platform_tag())
    if os.path.exists(dest):
        raise SystemExit(dest + " already exists; refusing to overwrite it")
    os.environ["DAMSAFETY_MF_CACHE"] = tempfile.mkdtemp(prefix="boann_cache_")
    os.environ["CXX"] = "no-such-compiler"                 # prove nothing is compiled here
    shutil.copytree(src, dest)
    tmp = tempfile.mkdtemp(prefix="boann_smoke_")
    bad = 0
    try:
        for name, run, spec in ENGINES:
            try:
                bk = be.CpuBackend(spec)
                assert os.path.dirname(bk.path) == dest, bk.path
                device = bk.device
                bk.close()
                p = valley_deck(os.path.join(tmp, name), ny=40, nx=60, sim_s=900.0, hole=False,
                                pond=False, gauge=False, free="E")
                st = run(p, backend="cpu")
                err = abs(st["mass_error_m3"]) / max(st["injected_m3"], 1.0)
                assert err < 1e-3, err
                print("ok   {:6s} {}  ({} steps, mass error {:.1e})".format(
                    name, device, st["steps"], err))
            except Exception as e:                          # report every engine, then fail
                bad += 1
                print("FAIL {:6s} {}: {}".format(name, type(e).__name__, e))
        for name, check in _boanngrid_checks(dest):
            try:
                print("ok   {:6s} BoannGrid {}".format(name, check()))
            except Exception as e:
                bad += 1
                print("FAIL {:6s} BoannGrid {}: {}".format(name, type(e).__name__, e))
    finally:
        shutil.rmtree(dest, True)
        if not os.listdir(be.PREBUILT_DIR):
            shutil.rmtree(be.PREBUILT_DIR, True)
        shutil.rmtree(tmp, True)
    if bad:
        raise SystemExit("{} of {} libraries failed".format(bad, len(ENGINES) + 3))


if __name__ == "__main__":
    main()
