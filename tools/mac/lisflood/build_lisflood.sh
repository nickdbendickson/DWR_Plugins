#!/bin/bash
# Download the official LISFLOOD-FP 8.3 source, check it, add the ND Dam Breach Runner
# patch (the `inittm_depth` keyword the DamSafety Runner requires) and build it with the
# conda-forge compilers. Called by install_dwr_suite_mac.sh; can also be run by hand:
#
#   bash build_lisflood.sh --conda ~/miniforge3/bin/conda --env ndbuild --out ~/dwr-dam-suite
#
# Options
#   --conda EXE     conda or mamba executable                       (required)
#   --env NAME      conda env with the build tools                  [ndbuild]
#   --out DIR       install prefix: DIR/bin/lisflood, DIR/src        [~/dwr-dam-suite]
#   --jobs N        parallel compile jobs                            [all CPUs]
#   --zip FILE      use this LISFLOOD-FP-8.3.zip instead of downloading
#   --python EXE    python 3 used to run the patcher                 [python3]
#   --patcher FILE  the Runner's scripts/patch_lisflood.py           [the Runner next to tools/,
#                   (the installer passes the installed Runner's)     in a DWR_Plugins checkout]
#   --apple-clang   compile with Apple's clang (Command Line Tools) instead of conda-forge clang
#
# The patch: the Runner's own patcher (DamSafety_Runner/scripts/patch_lisflood.py, so there is
# no third copy of it to drift), else - no patcher found, or it fails - the same edits as a
# plain diff for 8.3, lisflood-8.3-ndbreach.patch next to this script.
#
# Source: Zenodo record 13121119 (doi 10.5281/zenodo.13121119), LISFLOOD-FP-8.3.zip,
# 73,900,738 bytes, sha256 below. Nothing is run except `lisflood -version`.
# Logs: OUT/src/lisflood-logs/<compiler>-cmake_{configure,build}.log, one pair per compiler,
# so a failed first attempt's logs survive the installer's Apple-clang retry.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LF_URL="https://zenodo.org/records/13121119/files/LISFLOOD-FP-8.3.zip?download=1"
LF_SHA256="f9ea2753482da1e730ba34645a2b9b773d05d0a401a3524f79825b76ec7a4e8d"
LF_BYTES=73900738

CONDA_EXE=""; BENV=ndbuild; OUT="$HOME/dwr-dam-suite"; JOBS=""; ZIP=""; PY=python3; APPLE=0
PATCHER="$HERE/../../../DamSafety_Runner/scripts/patch_lisflood.py"
PATCH="$HERE/lisflood-8.3-ndbreach.patch"
while [ $# -gt 0 ]; do
  case "$1" in
    --conda) CONDA_EXE="$2"; shift 2 ;;
    --env) BENV="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --zip) ZIP="$2"; shift 2 ;;
    --python) PY="$2"; shift 2 ;;
    --patcher) PATCHER="$2"; shift 2 ;;
    --apple-clang) APPLE=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "build_lisflood.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
say()  { printf '  [lisflood] %s\n' "$*" >&2; }
die()  { printf 'ERROR [lisflood]: %s\n' "$*" >&2; exit 1; }
[ -n "$CONDA_EXE" ] || die "--conda is required"
OS="$(uname -s)"
if [ -z "$JOBS" ]; then
  if [ "$OS" = Darwin ]; then JOBS="$(sysctl -n hw.ncpu)"; else JOBS="$(nproc 2>/dev/null || echo 2)"; fi
fi
sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi; }

BASE="$("$CONDA_EXE" info --base)"
EP="$BASE/envs/$BENV"
[ -x "$EP/bin/cmake" ] || die "cmake not found in conda env '$BENV' ($EP). Create it first."

# 1. the official source (cached in OUT/src/_download and re-checked every time)
DL="$OUT/src/_download"; mkdir -p "$DL"
if [ -z "$ZIP" ]; then
  ZIP="$DL/LISFLOOD-FP-8.3.zip"
  if [ ! -f "$ZIP" ] || [ "$(sha256_of "$ZIP")" != "$LF_SHA256" ]; then
    say "downloading LISFLOOD-FP 8.3 from Zenodo (74 MB) ..."
    curl -fL --retry 3 --connect-timeout 30 -o "$ZIP.part" "$LF_URL" || die "download failed: $LF_URL"
    mv "$ZIP.part" "$ZIP"
  fi
fi
[ "$(wc -c < "$ZIP" | tr -d ' ')" = "$LF_BYTES" ] || die "$ZIP has the wrong size (expected $LF_BYTES bytes)"
GOT="$(sha256_of "$ZIP")"
[ "$GOT" = "$LF_SHA256" ] || die "sha256 mismatch for $ZIP: got $GOT, expected $LF_SHA256"
say "source OK: $(basename "$ZIP") sha256 $GOT"

# 2. unpack into a fresh build folder (only this script's own folder is replaced)
WORK="$OUT/src/lisflood-8.3-ndbreach"
rm -rf "$WORK.old"; [ -d "$WORK" ] && mv "$WORK" "$WORK.old"
mkdir -p "$WORK"
unzip -q "$ZIP" -d "$WORK"
SRC="$WORK/LISFLOOD-FP-8.3/source"
[ -f "$SRC/CMakeLists.txt" ] && [ -f "$SRC/config.default.cmake" ] || die "unexpected zip layout (no LISFLOOD-FP-8.3/source)"
rm -rf "$WORK.old"

# 3. the ND Dam Breach Runner patch: the Runner's own patcher first, else the same edits as a
#    plain diff (lisflood-8.3-ndbreach.patch, next to this script)
apply_diff() {   # on a fresh copy of the source, so no half-applied edit is left behind
  rm -rf "${WORK:?}/LISFLOOD-FP-8.3"; unzip -q "$ZIP" -d "$WORK"
  say "applying $(basename "$PATCH")"
  (cd "$WORK" && patch -p1 --forward < "$PATCH" >&2) || die "$(basename "$PATCH") did not apply"
}
if [ -f "$PATCHER" ]; then
  if "$PY" "$PATCHER" "$SRC" >&2; then
    say "patched with the Runner's patcher ($PATCHER)"
  else
    say "the Runner's patcher ($PATCHER) did not apply cleanly"
    apply_diff
  fi
else
  say "the Runner's patch_lisflood.py was not found (--patcher; looked at $PATCHER)"
  apply_diff
fi
grep -q '"inittm_depth"' "$SRC/pars.cpp" || die "patch check failed: pars.cpp has no inittm_depth keyword"
grep -q 'inittm_depth' "$SRC/lisflood.h" || die "patch check failed: lisflood.h has no inittm_depth"

# 4. configure + build (NetCDF off, CUDA off, OpenMP on). The logs are kept per compiler
#    outside $WORK (the next attempt replaces $WORK).
LOGDIR="$OUT/src/lisflood-logs"; mkdir -p "$LOGDIR"
sed 's/set(_NETCDF 1)/set(_NETCDF 0)/' "$SRC/config.default.cmake" > "$SRC/nd-breach.cmake"
CM=(-S "$SRC" -B "$SRC/build-nd" -D_CONFIG="$SRC/nd-breach.cmake" -DCMAKE_BUILD_TYPE=Release
    -DCMAKE_CUDA_COMPILER=CMAKE_CUDA_COMPILER-NOTFOUND -DCMAKE_PREFIX_PATH="$EP")
if [ "$OS" = Darwin ]; then
  SDK="$(xcrun --show-sdk-path 2>/dev/null || true)"
  [ -n "$SDK" ] || die "macOS SDK not found. Install Apple's Command Line Tools: xcode-select --install"
  if [ "$APPLE" = 1 ]; then CC_=/usr/bin/clang; CXX_=/usr/bin/clang++
  else CC_="$EP/bin/clang"; CXX_="$EP/bin/clang++"; fi
  [ -x "$CXX_" ] || die "compiler not found: $CXX_"
  [ -f "$EP/include/omp.h" ] && [ -f "$EP/lib/libomp.dylib" ] || die "llvm-openmp missing in $EP"
  CM+=(-DCMAKE_C_COMPILER="$CC_" -DCMAKE_CXX_COMPILER="$CXX_" -DCMAKE_OSX_SYSROOT="$SDK"
       -DOpenMP_C_FLAGS="-Xpreprocessor -fopenmp -I$EP/include" -DOpenMP_C_LIB_NAMES=omp
       -DOpenMP_CXX_FLAGS="-Xpreprocessor -fopenmp -I$EP/include" -DOpenMP_CXX_LIB_NAMES=omp
       -DOpenMP_omp_LIBRARY="$EP/lib/libomp.dylib"
       -DCMAKE_BUILD_RPATH="$EP/lib" -DCMAKE_EXE_LINKER_FLAGS="-L$EP/lib")
  # (the rpath comes from CMAKE_BUILD_RPATH only: a second -Wl,-rpath for the same folder
  #  gives a duplicate LC_RPATH, which recent macOS dyld refuses to load)
  if [ "$APPLE" = 1 ]; then TAG=apple-clang; else TAG=conda-clang; fi
else
  # Linux (only used to test this script): conda-forge gcc from the same env
  CC_=""; CXX_=""
  for f in "$EP"/bin/*-conda-linux-gnu-gcc; do [ -x "$f" ] && CC_="$f" && break; done
  for f in "$EP"/bin/*-conda-linux-gnu-g++; do [ -x "$f" ] && CXX_="$f" && break; done
  [ -n "$CXX_" ] || die "conda-forge g++ not found in $EP"
  CM+=(-DCMAKE_C_COMPILER="$CC_" -DCMAKE_CXX_COMPILER="$CXX_"
       -DCMAKE_EXE_LINKER_FLAGS="-Wl,-rpath,$EP/lib")
  TAG=linux-gcc
fi
CFG_LOG="$LOGDIR/$TAG-cmake_configure.log"; BLD_LOG="$LOGDIR/$TAG-cmake_build.log"
say "compiler: $CXX_  (jobs $JOBS)"
"$EP/bin/cmake" "${CM[@]}" > "$CFG_LOG" 2>&1 || { tail -30 "$CFG_LOG" >&2; die "cmake configure failed ($TAG; log $CFG_LOG)"; }
"$EP/bin/cmake" --build "$SRC/build-nd" -j "$JOBS" --target lisflood > "$BLD_LOG" 2>&1 || { tail -30 "$BLD_LOG" >&2; die "compile failed ($TAG; log $BLD_LOG)"; }
BIN="$SRC/build-nd/lisflood"
[ -x "$BIN" ] || die "no binary at $BIN"
grep -aq inittm_depth "$BIN" || die "the built binary has no inittm_depth (patch not compiled in)"
BANNER="$("$BIN" -version 2>&1 || true)"
grep -q "LISFLOOD-FP" <<<"$BANNER" || die "the built binary does not run: $BANNER"
say "built: $BIN ($(grep -m1 -o 'LISFLOOD-FP [0-9.]*' <<<"$BANNER"), Runner patch compiled in)"
echo "$BIN"
