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
#   --metal MODE    macOS: the Metal (GPU) back end of the FV1 solver: auto | on | off  [auto]
#                   auto = build it when Apple's Metal compiler works on this Mac, else leave
#                   it out and say how to get it, and when it then fails to compile build
#                   again without it; on = stop if it cannot be built
#
# Metal: the 8.3 source builds a Metal back end on every Mac ("if (APPLE)"), and its kernels
# need Apple's `metal` compiler, which is NOT part of the Command Line Tools and, from Xcode 26,
# not part of Xcode either until  xcodebuild -downloadComponent MetalToolchain  is run. Without
# it the whole build stopped at "[ 2%] Compiling Metal FV1 kernels". This script now puts that
# block behind NDBREACH_ENABLE_METAL and tries the compiler first. The back end only runs when
# a .par file holds the `metal` (or `cuda`) keyword with the fv1 solver, and it loads
# metal_fv1_kernels.metallib from the folder of the executable: keep the two files together.
#
# The patch: the Runner's own patcher (DamSafety_Runner/scripts/patch_lisflood.py, so there is
# no third copy of it to drift), else - no patcher found, or it fails - the same edits as a
# plain diff for 8.3, lisflood-8.3-ndbreach.patch next to this script.
#
# Source: Zenodo record 13121119 (doi 10.5281/zenodo.13121119), LISFLOOD-FP-8.3.zip,
# 73,900,738 bytes, sha256 below. Nothing is run except `lisflood -version`.
# Logs: OUT/src/lisflood-logs/<compiler>-cmake_{configure,build}.log, one pair per compiler,
# so a failed first attempt's logs survive the installer's Apple-clang retry; a build with the
# Metal back end logs to <compiler>-metal-cmake_{configure,build}.log.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LF_URL="https://zenodo.org/records/13121119/files/LISFLOOD-FP-8.3.zip?download=1"
LF_SHA256="f9ea2753482da1e730ba34645a2b9b773d05d0a401a3524f79825b76ec7a4e8d"
LF_BYTES=73900738

CONDA_EXE=""; BENV=ndbuild; OUT="$HOME/dwr-dam-suite"; JOBS=""; ZIP=""; PY=python3; APPLE=0
METAL=auto
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
    --metal) METAL="$2"; shift 2 ;;
    -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
    *) echo "build_lisflood.sh: unknown option $1" >&2; exit 2 ;;
  esac
done
say()  { printf '  [lisflood] %s\n' "$*" >&2; }
die()  { printf 'ERROR [lisflood]: %s\n' "$*" >&2; exit 1; }
[ -n "$CONDA_EXE" ] || die "--conda is required"
case "$METAL" in auto|on|off) ;; *) die "--metal takes auto, on or off (got '$METAL')" ;; esac
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

# 3b. the Metal back end (macOS only): put the source's unconditional "if (APPLE)" block behind
#     NDBREACH_ENABLE_METAL, then switch it on only when Apple's Metal compiler really compiles
#     the kernels here (the same command the build runs). CMakeLists.txt has CRLF line ends,
#     so the edit touches the start of the line only.
METAL_FLAG=OFF; METAL_GATED=0
if grep -q '^if (APPLE)' "$SRC/CMakeLists.txt"; then
  sed 's/^if (APPLE)/if (APPLE AND NDBREACH_ENABLE_METAL)/' "$SRC/CMakeLists.txt" > "$SRC/CMakeLists.txt.nd"
  mv "$SRC/CMakeLists.txt.nd" "$SRC/CMakeLists.txt"
fi
if grep -q '^if (APPLE AND NDBREACH_ENABLE_METAL)' "$SRC/CMakeLists.txt"; then METAL_GATED=1; fi
# The Metal solver includes rain/rain.tpp whatever NetCDF is, and with NetCDF off (as built
# here) that file does not compile ("use of undeclared identifier 'total_rain_mass'": the
# variable is declared inside "#if _NETCDF == 1" and used after its "#endif"). The CPU and CUDA
# code include rain_stub.tpp in that case; make the Metal solver do the same.
MM="$SRC/metal/fv1/metal_fv1_simulate.mm"
if [ -f "$MM" ] && [ -f "$SRC/rain/rain_stub.tpp" ] && ! grep -q 'rain_stub.tpp' "$MM"; then
  awk '{ l = $0; sub(/\r$/, "", l)
         if (l == "#include \"../../rain/rain.tpp\"") {
           print "#if _NETCDF == 1"; print l; print "#else"
           print "#include \"../../rain/rain_stub.tpp\""; print "#endif"
         } else print }' "$MM" > "$MM.nd"
  mv "$MM.nd" "$MM"
  if grep -q 'rain_stub.tpp' "$MM"; then say "Metal solver: rain_stub.tpp when NetCDF is off (as the CPU and CUDA code do)"; fi
fi
if [ "$OS" = Darwin ] && [ "$METAL" != off ]; then
  KERN="$SRC/metal/fv1/metal_fv1_kernels.metal"; PROBE="$WORK/metal-probe.air"
  LOGDIR="$OUT/src/lisflood-logs"; mkdir -p "$LOGDIR"
  if [ "$METAL_GATED" = 1 ] && [ -f "$KERN" ] \
     && xcrun -sdk macosx metal -fno-fast-math -c "$KERN" -o "$PROBE" > "$LOGDIR/metal-probe.log" 2>&1; then
    METAL_FLAG=ON; say "Metal compiler works: building the Metal (GPU) back end for FV1"
  else
    if [ "$METAL" = on ]; then die "--metal on, but Apple's Metal compiler does not work here (log $LOGDIR/metal-probe.log). With Xcode installed run:  xcodebuild -downloadComponent MetalToolchain"; fi
    say "no working Metal compiler: building WITHOUT the Metal (GPU) back end (CPU / OpenMP only)."
    say "to add it: install Xcode, run  xcodebuild -downloadComponent MetalToolchain  and re-run the installer."
  fi
  rm -f "$PROBE"
fi
if [ "$OS" = Darwin ] && [ "$METAL_GATED" = 0 ]; then
  say "this source has no 'if (APPLE)' Metal block to switch (unexpected for 8.3); building it as it is"
fi

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
say "compiler: $CXX_  (jobs $JOBS)"
# show_errors LOG: the compiler's own error lines (with -j they are rarely in the last lines
# of the log), then the end of the log
show_errors() {
  grep -n -A6 -E '(^|[ :])(fatal )?error[: ]' "$1" 2>/dev/null | head -60 >&2 || true
  echo "  ... end of $1:" >&2; tail -15 "$1" >&2
}
# build_once ON|OFF LOGTAG: configure and compile in a fresh build folder; 0 when it compiled
build_once() {
  local metal="$1" cfg="$LOGDIR/$2-cmake_configure.log" bld="$LOGDIR/$2-cmake_build.log"
  rm -rf "$SRC/build-nd"
  "$EP/bin/cmake" "${CM[@]}" -DNDBREACH_ENABLE_METAL="$metal" > "$cfg" 2>&1 \
    || { tail -30 "$cfg" >&2; die "cmake configure failed ($2; log $cfg)"; }
  BLD_LOG="$bld"
  "$EP/bin/cmake" --build "$SRC/build-nd" -j "$JOBS" --target lisflood > "$bld" 2>&1
}
if [ "$METAL_FLAG" = ON ]; then
  if build_once ON "$TAG-metal"; then :
  elif [ "$METAL" = on ]; then
    show_errors "$BLD_LOG"; die "compile failed with the Metal back end ($TAG; log $BLD_LOG)"
  else
    # --metal auto: the Metal back end is an extra. When it does not compile on this Mac,
    # build the same source without it instead of ending with no LISFLOOD-FP at all.
    show_errors "$BLD_LOG"
    say "the Metal (GPU) back end did not compile (log $BLD_LOG): building WITHOUT it (CPU / OpenMP only)"
    METAL_FLAG=OFF
    build_once OFF "$TAG" || { show_errors "$BLD_LOG"; die "compile failed ($TAG; log $BLD_LOG)"; }
  fi
else
  build_once OFF "$TAG" || { show_errors "$BLD_LOG"; die "compile failed ($TAG; log $BLD_LOG)"; }
fi
BIN="$SRC/build-nd/lisflood"
[ -x "$BIN" ] || die "no binary at $BIN"
grep -aq inittm_depth "$BIN" || die "the built binary has no inittm_depth (patch not compiled in)"
grep -aq "nd-breach-runner: SGC arrival depth" "$BIN" || die "the built binary has no SGC arrival-depth edit (sub-grid channels would be refused by the Runner)"
BANNER="$("$BIN" -version 2>&1 || true)"
grep -q "LISFLOOD-FP" <<<"$BANNER" || die "the built binary does not run: $BANNER"
say "built: $BIN ($(grep -m1 -o 'LISFLOOD-FP [0-9.]*' <<<"$BANNER"), Runner patch compiled in)"
if [ "$METAL_FLAG" = ON ]; then
  if [ -f "$SRC/build-nd/metal_fv1_kernels.metallib" ]; then
    say "Metal kernels: $SRC/build-nd/metal_fv1_kernels.metallib (keep it next to the executable)"
  else
    say "Metal was switched on but no metal_fv1_kernels.metallib was built; the binary runs on the CPU"
  fi
fi
echo "$BIN"
