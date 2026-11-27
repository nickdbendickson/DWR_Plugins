#!/bin/bash
# Build LISFLOOD-FP on macOS (Apple Silicon or Intel) with the ND Dam Breach
# Runner patch.
#
#   ./build_lisflood_macos.sh /path/to/LISFLOOD-FP-source [install folder]
#
# Get the source from the official release (University of Bristol), e.g.
# LISFLOOD-FP v8.3 on Zenodo: https://zenodo.org/records/13121119 (8.2, 13121102, also works)
# Needs Xcode command line tools (xcode-select --install) and Homebrew.
set -euo pipefail

SRC="${1:?give the LISFLOOD-FP source folder}"
OUT="${2:-$HOME/lisflood-fp}"
HERE="$(cd "$(dirname "$0")" && pwd)"

command -v brew >/dev/null || { echo "Homebrew is needed: https://brew.sh"; exit 1; }
brew list cmake >/dev/null 2>&1 || brew install cmake
brew list libomp >/dev/null 2>&1 || brew install libomp
OMP="$(brew --prefix libomp)"
if [ ! -f "$OMP/include/omp.h" ] || [ ! -f "$OMP/lib/libomp.dylib" ]; then
  echo "libomp files not found under $OMP (need include/omp.h and lib/libomp.dylib)."
  echo "Try: brew reinstall libomp"; exit 1
fi

python3 "$HERE/patch_lisflood.py" "$SRC"

cd "$SRC"
# Build without NetCDF output (not needed; rasters are written as ASCII grids).
sed 's/set(_NETCDF 1)/set(_NETCDF 0)/' config.default.cmake > nd-breach.cmake

# The source's unconditional "if (APPLE)" block builds the Metal (GPU) FV1 back end whenever
# Apple's Metal compiler works, and that back end needs its compiled kernels beside the
# executable at run time. This script builds the plain CPU / OpenMP executable (the one the
# release ships), so the block goes behind NDBREACH_ENABLE_METAL, off unless asked for
# (tools/mac/lisflood/build_lisflood.sh does the same and probes the Metal compiler first).
# CMakeLists.txt has CRLF line ends, so the edit touches the start of the line only.
if grep -q '^if (APPLE)' CMakeLists.txt; then
  sed 's/^if (APPLE)/if (APPLE AND NDBREACH_ENABLE_METAL)/' CMakeLists.txt > CMakeLists.txt.nd
  mv CMakeLists.txt.nd CMakeLists.txt
fi
# With NetCDF off, metal_fv1_simulate.mm does not compile ("use of undeclared identifier
# 'total_rain_mass'": rain/rain.tpp declares it inside "#if _NETCDF == 1" and uses it after the
# "#endif"). The CPU and CUDA code include rain_stub.tpp in that case; make the Metal solver do
# the same, so a build with -DNDBREACH_ENABLE_METAL=ON compiles too.
MM=metal/fv1/metal_fv1_simulate.mm
if [ -f "$MM" ] && [ -f rain/rain_stub.tpp ] && ! grep -q 'rain_stub.tpp' "$MM"; then
  awk '{ l = $0; sub(/\r$/, "", l)
         if (l == "#include \"../../rain/rain.tpp\"") {
           print "#if _NETCDF == 1"; print l; print "#else"
           print "#include \"../../rain/rain_stub.tpp\""; print "#endif"
         } else print }' "$MM" > "$MM.nd"
  mv "$MM.nd" "$MM"
fi

cmake -S . -B build-nd -D_CONFIG=nd-breach.cmake -DCMAKE_BUILD_TYPE=Release \
  -DOpenMP_C_FLAGS="-Xpreprocessor -fopenmp -I$OMP/include" -DOpenMP_C_LIB_NAMES=omp \
  -DOpenMP_CXX_FLAGS="-Xpreprocessor -fopenmp -I$OMP/include" -DOpenMP_CXX_LIB_NAMES=omp \
  -DOpenMP_omp_LIBRARY="$OMP/lib/libomp.dylib"
cmake --build build-nd -j "$(sysctl -n hw.ncpu)" --target lisflood

# The executable must link libomp (otherwise it runs on one core) and know the new keyword.
otool -L build-nd/lisflood | grep -q libomp || echo "WARNING: not linked against libomp; runs will be single-threaded."
grep -q inittm_depth build-nd/lisflood || { echo "ERROR: the patch is not in the build."; exit 1; }
grep -aq "nd-breach-runner: SGC arrival depth" build-nd/lisflood || { echo "ERROR: the sub-grid channel (SGC) part of the patch is not in the build."; exit 1; }

mkdir -p "$OUT"
cp build-nd/lisflood "$OUT/lisflood"
echo
echo "Built: $OUT/lisflood"
echo "In QGIS: Processing > Options > Providers > DamSafety Runner >"
echo "  LISFLOOD-FP executable = $OUT/lisflood"
echo "Then run 'Check the model installation' from the plugin menu."
