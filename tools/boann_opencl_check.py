#!/usr/bin/env python3
"""Boann's OpenCL backend on this computer's GPU (Intel Arc, AMD, NVIDIA): which OpenCL devices
are here, the OpenCL tests (DamSafety_Runner/tests/test_boann_opencl.py: every solver against the
CPU build, repeat runs, the dry-tile skip), then a speed table: the medium test valley (1.12 M
cells of 5 m, 2 h) with ACC and FV1 on the CPU build and on OpenCL, timed, with the largest
max-depth difference. The CPU build needs a C++ compiler or Boann's prebuilt libraries under
DamSafety_Core/boann/hydro/prebuilt/<platform>/ (a Windows computer: the latter).

    python tools/boann_opencl_check.py [--size small|medium|large] [--skip-tests]"""
import argparse
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import numpy as np                                                   # noqa: E402

from DamSafety_Core.boann.hydro import backends as be                # noqa: E402
from DamSafety_Core.boann.hydro import bench                         # noqa: E402
from DamSafety_Core.boann.hydro import engine as mfe                 # noqa: E402
from DamSafety_Core.boann.hydro import engine_fv as fve              # noqa: E402
from DamSafety_Core.boann.hydro import opencl_backend as ob          # noqa: E402
from DamSafety_Core.boann.hydro.cases import read_asc                # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", default="medium", choices=sorted(bench.SIZES))
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    print("== OpenCL devices")
    try:
        ds = ob.devices()
    except be.BackendError as e:
        print("none:", e)
        return 1
    for i, d in enumerate(ds):
        print("  {}: {} | {} | {} | {:.1f} GB | {} compute units{}".format(
            i, d[2], d[0], d[3], d[5] / 2.0 ** 30, d[6], " | GPU" if d[4] else ""))
    if not ds:
        print("no OpenCL device: is the GPU driver installed?")
        return 1
    ok, name = ob.opencl_available()
    print("Boann uses:", name, "(DAMSAFETY_MF_CL_DEVICE=N picks another)")
    if not a.skip_tests:
        print("== OpenCL tests")
        from nd_breach_runner.tests import test_boann_opencl as t
        from nd_breach_runner.tests.registry import Skip
        for fn in (t.boann_opencl_translates_every_kernel_file,
                   t.boann_opencl_solvers_match_cpu_build,
                   t.boann_opencl_repeats_bit_for_bit,
                   t.boann_opencl_tile_skip_is_inert):
            try:
                fn()
                print("ok  ", fn.__name__)
            except Skip as e:
                print("skip", fn.__name__, e)
    print("== speed, {} valley".format(a.size))
    d = tempfile.mkdtemp(prefix="boann_ocl_speed_")
    rows = []
    try:
        for solver in ("acc", "fv1"):
            res = {}
            for backend in ("cpu", "opencl"):
                folder = os.path.join(d, solver + "_" + backend)
                os.makedirs(folder)
                par = bench._deck(folder, a.size)
                t0 = time.perf_counter()
                if solver == "acc":
                    st = mfe.run_par(par, backend=backend)
                else:
                    st = fve.run_par(par, backend=backend, order=1)
                wall = time.perf_counter() - t0
                res[backend] = (wall, st, read_asc(os.path.join(folder, "results", "run.max")))
                print("  {} {}: {:.1f} s, {} steps, {}".format(solver, backend, wall, st["steps"],
                                                              st.get("device", "")))
            (wc, sc, mc), (wo, so, mo) = res["cpu"], res["opencl"]
            m = (mc > 0.003) | (mo > 0.003)
            diff = float(np.abs(mc - mo)[m].max()) if m.any() else 0.0
            rows.append((solver, wc, wo, wc / max(wo, 1e-9), sc["steps"], so["steps"], diff))
    finally:
        shutil.rmtree(d, True)
    print()
    print("{:<6}{:>12}{:>12}{:>10}{:>11}{:>11}{:>16}".format(
        "solver", "CPU (s)", "OpenCL (s)", "speed-up", "CPU steps", "GPU steps", "max |dh| (m)"))
    for r in rows:
        print("{:<6}{:>12.1f}{:>12.1f}{:>9.1f}x{:>11}{:>11}{:>16.4f}".format(*r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
