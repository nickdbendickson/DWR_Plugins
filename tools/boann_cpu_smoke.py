#!/usr/bin/env python3
"""Check Boann's prebuilt CPU libraries for this computer (the release workflow runs it on Windows,
Linux and macOS before the libraries ship): each engine's library under <dir>/<platform>/ opens,
agrees with Python on the Params layout, reports its cores, and runs a short valley flood that
conserves water. The libraries are copied into core/metalflood/prebuilt/<platform>/ for the run,
exactly where a release zip puts them, and removed afterwards.

    python tools/boann_cpu_smoke.py build/boann"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "DamSafety_Runner", "core"))

from metalflood import backends as be                                   # noqa: E402
from metalflood import engine as mfe, engine_fv as fve, engine_fv2 as fv2e  # noqa: E402
from metalflood import engine_dg2 as dg2e, engine_sgc as sgce             # noqa: E402
from metalflood import engine_subfv as sfve, subfv_backend as sfb         # noqa: E402
from metalflood import engine_subli as slie, subli_backend as slb         # noqa: E402
from metalflood.cases import valley_deck                                # noqa: E402

ENGINES = (("acc", mfe.run_par, be.FLOOD_SPEC), ("fv", fve.run_par, be.FV_SPEC),
           ("fv2", fv2e.run_par, be.FV2_SPEC), ("dg2", dg2e.run_par, be.DG2_SPEC),
           ("sgc", sgce.run_par, be.SGC_SPEC), ("subfv", sfve.run_par, sfb.SUBFV_SPEC),
           ("subli", slie.run_par, slb.SUBLI_SPEC))


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
    finally:
        shutil.rmtree(dest, True)
        if not os.listdir(be.PREBUILT_DIR):
            shutil.rmtree(be.PREBUILT_DIR, True)
        shutil.rmtree(tmp, True)
    if bad:
        raise SystemExit("{} of {} engines failed".format(bad, len(ENGINES)))


if __name__ == "__main__":
    main()
