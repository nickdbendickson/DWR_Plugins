#!/bin/bash
# Cross-compile LISFLOOD-FP for Windows x64 on Linux with MinGW-w64 (posix threads), with the
# DamSafety Runner patch (scripts/patch_lisflood.py: the inittm_depth arrival-depth keyword, NUMA
# optional, _aligned_malloc for MinGW). Used by .github/workflows/release.yml; runs anywhere
# with the Debian / Ubuntu packages below. 2026-11-07.
#
#   ./build_lisflood_windows_cross.sh /path/to/LISFLOOD-FP-source [install folder]
#
# Debian / Ubuntu: sudo apt install cmake g++-mingw-w64-x86-64-posix python3
# The result, <install folder>/lisflood.exe, needs no DLL: libstdc++, libgcc, libgomp (OpenMP) and
# winpthread are linked statically (-static), as the nd_hazard fork's bundled build was.
set -euo pipefail

SRC="${1:?give the LISFLOOD-FP source folder}"
OUT="${2:-$HOME/lisflood-fp-windows}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# the posix-threads flavour: libgomp needs pthreads, and the win32-threads g++ has no OpenMP
CXX_="${MINGW_CXX:-x86_64-w64-mingw32-g++-posix}"
CC_="${MINGW_CC:-x86_64-w64-mingw32-gcc-posix}"
command -v "$CXX_" >/dev/null || CXX_=x86_64-w64-mingw32-g++
command -v "$CC_" >/dev/null || CC_=x86_64-w64-mingw32-gcc
command -v "$CXX_" >/dev/null || { echo "MinGW-w64 g++ not found (Debian / Ubuntu: sudo apt install g++-mingw-w64-x86-64-posix)"; exit 1; }
command -v cmake >/dev/null || { echo "cmake is needed"; exit 1; }
command -v python3 >/dev/null || { echo "python3 is needed to apply the patch"; exit 1; }

# the Runner patch (safe to run twice); the MinGW edits are part of it
python3 "$HERE/patch_lisflood.py" "$SRC"

cd "$SRC"
# NetCDF off (rasters are ASCII grids), CUDA off
sed 's/set(_NETCDF 1)/set(_NETCDF 0)/' config.default.cmake > nd-breach.cmake
cat > mingw-w64-x86_64.cmake <<EOF
set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)
set(CMAKE_C_COMPILER $CC_)
set(CMAKE_CXX_COMPILER $CXX_)
set(CMAKE_RC_COMPILER x86_64-w64-mingw32-windres)
set(CMAKE_FIND_ROOT_PATH /usr/x86_64-w64-mingw32)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
EOF

# -static: libstdc++, libgcc, libgomp and winpthread go into the .exe. OpenMP is given by hand
# (FindOpenMP's try-compile cannot run a Windows program here).
cmake -S . -B build-win -DCMAKE_TOOLCHAIN_FILE="$SRC/mingw-w64-x86_64.cmake" \
  -D_CONFIG=nd-breach.cmake -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CUDA_COMPILER=CMAKE_CUDA_COMPILER-NOTFOUND \
  -DOpenMP_CXX_FLAGS=-fopenmp -DOpenMP_CXX_LIB_NAMES=gomp -DOpenMP_gomp_LIBRARY=gomp \
  -DOpenMP_C_FLAGS=-fopenmp -DOpenMP_C_LIB_NAMES=gomp \
  "-DCMAKE_EXE_LINKER_FLAGS=-static -static-libgcc -static-libstdc++"
cmake --build build-win -j "$(nproc 2>/dev/null || echo 2)" --target lisflood

BIN=build-win/lisflood.exe
[ -f "$BIN" ] || { echo "ERROR: no $BIN"; exit 1; }
# the executable must know the new keyword (otherwise the Runner refuses it)
grep -aq inittm_depth "$BIN" || { echo "ERROR: the patch is not in the build."; exit 1; }
# no DLL beyond Windows' own: objdump lists the imports
if command -v x86_64-w64-mingw32-objdump >/dev/null; then
  DLLS="$(x86_64-w64-mingw32-objdump -p "$BIN" | awk '/DLL Name/ {print $3}' | tr '\n' ' ')"
  echo "imports: $DLLS"
  case "$DLLS" in *libgomp*|*libstdc*|*libgcc*|*winpthread*) echo "ERROR: not statically linked: $DLLS"; exit 1 ;; esac
fi
mkdir -p "$OUT"
cp "$BIN" "$OUT/lisflood.exe"
echo
echo "Built: $OUT/lisflood.exe (Windows x64, no DLLs needed)"
