#!/bin/bash
# Build LISFLOOD-FP on Linux with the ND Dam Breach Runner patch (the inittm_depth
# arrival-depth keyword; NUMA optional) - the same patch as build_lisflood_macos.sh.
#
#   ./build_lisflood_linux.sh /path/to/LISFLOOD-FP-source [install folder]
#   (or set LISFLOOD_SRC instead of the first argument)
#
# Get the source from the official release (University of Bristol), e.g.
# LISFLOOD-FP v8.3 on Zenodo: https://zenodo.org/records/13121119 (8.2, 13121102, also works)
# Needs cmake, a C++ compiler with OpenMP (g++), and python3.
# Debian / Ubuntu: sudo apt install cmake g++ python3   (libnuma-dev is optional)
set -euo pipefail

SRC="${1:-${LISFLOOD_SRC:-$HOME/lisflood-fp}}"
OUT="${2:-$HOME/lisflood-fp}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$SRC" ]; then
  echo "Give the LISFLOOD-FP source folder (or set LISFLOOD_SRC). Not found: $SRC"
  exit 1
fi
command -v cmake >/dev/null || { echo "cmake is needed (Debian / Ubuntu: sudo apt install cmake g++)"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is needed to apply the patch"; exit 1; }

# The ND Dam Breach Runner patch: without it .inittm records the first wetting (about 1 mm),
# not the arrival depth, and the Runner refuses the executable. Safe to run twice.
python3 "$HERE/patch_lisflood.py" "$SRC"

cd "$SRC"
# Build without NetCDF output (not needed; rasters are written as ASCII grids).
CONFIG=()
if [ -f config.default.cmake ]; then
  sed 's/set(_NETCDF 1)/set(_NETCDF 0)/' config.default.cmake > nd-breach.cmake
  CONFIG=(-D_CONFIG=nd-breach.cmake)
fi

cmake -S . -B build-nd "${CONFIG[@]}" -DCMAKE_BUILD_TYPE=Release
cmake --build build-nd -j "$(nproc 2>/dev/null || echo 2)" --target lisflood

# The executable must know the new keyword (otherwise the Runner refuses it again).
grep -q inittm_depth build-nd/lisflood || { echo "ERROR: the patch is not in the build."; exit 1; }
if command -v ldd >/dev/null && ! ldd build-nd/lisflood | grep -q -E "gomp|omp"; then
  echo "WARNING: not linked against OpenMP; runs will be single-threaded."
fi

mkdir -p "$OUT"
cp build-nd/lisflood "$OUT/lisflood"
echo
echo "Built: $OUT/lisflood"
echo "In QGIS: Processing > Options > Providers > ND Dam Breach Runner >"
echo "  LISFLOOD-FP executable = $OUT/lisflood"
echo "(or set it in Suite status). Then run 'Check the model installation' from the plugin menu."
