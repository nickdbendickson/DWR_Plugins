#!/usr/bin/env python3
"""Build Boann's CPU libraries (every engine's kernels as C++) for one platform, to ship in the
Runner and Screening zips under core/metalflood/prebuilt/<platform>/ so a computer without a C++
compiler (most Windows ones) can still run Boann, on every core.

    python3 tools/build_boann_cpu.py --out build/boann                       (this computer)
    python3 tools/build_boann_cpu.py --out build/boann --platform windows-x64 \\
        --cxx x86_64-w64-mingw32-g++-posix --extra=-static                 (cross-compiled)

For each engine (ACC, FV1, FV2, DG2, SGC, sub-grid FV, sub-grid local inertial, and BoannGrid's
fill and D4/D8 flow) it writes the OpenMP build
<spec>_cpu_<source hash>_omp<ext> (unless --no-openmp or the compiler has none) and the one-core
build <spec>_cpu_<source hash><ext>, with exactly the command backends.cpu_build_command gives the
Runner at run time, so the names match what backends.build_cpu_library looks for. A library built
from other sources never matches (the hash is in the name), so a stale one is simply ignored.
Used by .github/workflows/release.yml; tools/build_release.py --boann-cpu puts the files in the
zips. Pure stdlib apart from Boann itself (DamSafety_Core/boann, numpy)."""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# Boann lives once, in DamSafety Core (DamSafety_Core/boann: hydro = the flood engine, grid = BoannGrid)
sys.path.insert(0, REPO)
from DamSafety_Core.boann.hydro import backends as be          # noqa: E402
from DamSafety_Core.boann.hydro import subfv_backend as sfb    # noqa: E402
from DamSafety_Core.boann.hydro import subli_backend as slb    # noqa: E402
from DamSafety_Core.boann.grid import fill as mt_fill          # noqa: E402
from DamSafety_Core.boann.grid import flow as mt_flow          # noqa: E402

SPECS = (be.FLOOD_SPEC, be.FV_SPEC, be.FV2_SPEC, be.DG2_SPEC, be.SGC_SPEC, sfb.SUBFV_SPEC,
         slb.SUBLI_SPEC, mt_fill.SPEC, mt_flow.SPEC, mt_flow.SPEC8)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--out", required=True, help="folder; the files go into <out>/<platform>/")
    ap.add_argument("--platform", default=be.platform_tag(),
                    help="linux-x64, windows-x64, macos-arm64, macos-x64 [this computer: %(default)s]")
    ap.add_argument("--cxx", default=os.environ.get("CXX") or be.find_compiler() or "c++")
    ap.add_argument("--extra", action="append", default=[],
                    help="extra compiler argument (repeat), e.g. --extra=-static --extra=-arch --extra=x86_64")
    ap.add_argument("--no-openmp", action="store_true", help="one-core builds only")
    ap.add_argument("--require-openmp", action="store_true", help="fail if the OpenMP build fails")
    a = ap.parse_args(argv)
    osname = a.platform.split("-")[0]
    ext = be.library_ext(osname)
    dest = os.path.join(a.out, a.platform)
    os.makedirs(dest, exist_ok=True)
    made = []
    for spec in SPECS:
        for omp in ((False,) if a.no_openmp else (True, False)):
            out = os.path.join(dest, be.cpu_library_name(spec, ext, omp))
            cmd = be.cpu_build_command(spec, a.cxx, out, openmp=omp, extra=a.extra)
            print(" ".join(cmd), flush=True)
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True)
            if r.returncode != 0:
                print(r.stdout[-3000:])
                if omp and not a.require_openmp:
                    print("NOTE: no OpenMP build of {} (the one-core build ships alone)".format(spec.name))
                    continue
                raise SystemExit("the {} CPU kernels did not compile".format(spec.name))
            made.append(out)
    print("\n{} libraries in {}".format(len(made), dest))
    for m in made:
        print("  {}  {:,} bytes".format(os.path.basename(m), os.path.getsize(m)))


if __name__ == "__main__":
    main()
