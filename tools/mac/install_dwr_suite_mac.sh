#!/bin/bash
# =============================================================================
# DamSafety (DWR) Dam Suite — one-script installer for macOS, no Homebrew
# =============================================================================
#
# The DWR / DamSafety twin of the ND installer. It installs, entirely through
# conda-forge (no Homebrew), everything needed to run the DamSafety dam plugins
# on a Mac, and sets up a ready-to-run Absaraka Dam (DWR-2801). It detects your
# chip automatically and builds a native stack: Intel -> osx-64, Apple Silicon
# (M1/M2/M3, incl. M2 Pro) -> osx-arm64.
#
#   * Miniforge (conda) if you don't already have conda/mamba: a pinned release
#     (MF_VER below), sha256-checked before it is run
#   * QGIS 3.34                      conda-forge, env  "ndqgis"
#   * ANUGA + gmsh (the mesher)      conda-forge, env  "ndanuga"
#   * LISFLOOD-FP 8.3                the OFFICIAL source is downloaded from Zenodo
#                                    (size + sha256 checked), the Runner's small
#                                    patch is applied (the installed Runner's
#                                    scripts/patch_lisflood.py, else lisflood/'s
#                                    lisflood-8.3-ndbreach.patch), and it is built
#                                    with conda-forge clang (conda-forge has no
#                                    LISFLOOD-FP), OR reused from an existing
#                                    patched ND-set build, OR DS_LISFLOOD
#   * The DamSafety plugins (the zips in plugins/) into their OWN QGIS profile
#   * A ready-to-run Absaraka Dam (DWR-2801) folder + the settings Grok Bot uses
#   * damsafety_env.sh + a headless QGIS config for run_nickreview.sh
#
# No sudo, no Homebrew. Everything goes under your home folder. A log is
# written to ~/dwr-dam-suite/logs/. Safe to re-run: finished steps are skipped.
#
# WHY A SEPARATE PROFILE: the DamSafety and ND plugins register the SAME
# Processing provider ids (nddam / ndbreach / ndhazard), so if both load in one
# QGIS profile they clash and QGIS can hang. This installer therefore uses the
# "damsafety" profile by default, leaving the ND set's "default" profile alone.
# The conda envs (QGIS, ANUGA) are the SAME for both sets, so they are reused if
# you already ran the ND installer.
#
# WHY LISFLOOD MATTERS MORE HERE: the DamSafety Runner ships NO LISFLOOD-FP
# binary of its own and refuses an UNPATCHED build. Without a patched binary it
# falls back to the slow built-in engine (only if you tick "use built-in"), so
# this script builds/points at the patched engine for you.
#
# USAGE
#   bash install_dwr_suite_mac.sh                 # full install
#   bash install_dwr_suite_mac.sh --check         # report what is installed
#   bash install_dwr_suite_mac.sh --skip-lisflood # don't build LISFLOOD-FP (an already
#                                                 # installed patched one is still used)
#
# The only non-conda prerequisite is Apple's Command Line Tools (they provide
# the macOS SDK the compiler needs) — an Apple component, NOT Homebrew. Skip it
# with --skip-lisflood (built-in engine) or point DS_LISFLOOD at a patched build.
#
# Overrides (environment variables): DS_PREFIX, DS_MINIFORGE, DS_QGIS_ENV,
# DS_ANUGA_ENV, DS_QGIS_VER, DS_QGIS_PROFILE, DS_PLUGIN_DIR, DS_SUBDIR,
# DS_LISFLOOD (a patched binary), DS_LISFLOOD_ZIP (a local LISFLOOD-FP-8.3.zip,
# still sha256-checked), DS_MINIFORGE_SH (a local copy of the pinned Miniforge
# installer, still sha256-checked), DS_THREADS, DS_JOBS (compile jobs),
# DS_APPLE_CLANG=1 (compile with Apple's clang instead of conda-forge clang).
#
# GPU / Metal: the official LISFLOOD-FP 8.3 source has NO Metal back end (its
# GPU code is CUDA only, which Macs cannot use), so DS_ENABLE_METAL is ignored.
# =============================================================================

set -euo pipefail

# --- where this script and its bundled files live --------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="${DS_PLUGIN_DIR:-$HERE/plugins}"   # the DamSafety plugin zips ship next to this script
ABSARAKA_SRC="$HERE/absaraka"                  # the seeded Absaraka folder ships here too

# --- things you can override with environment variables ---------------------
PREFIX="${DS_PREFIX:-$HOME/dwr-dam-suite}"     # LISFLOOD-FP binary + run folder
MINIFORGE_DIR="${DS_MINIFORGE:-$HOME/miniforge3}"
QGIS_ENV="${DS_QGIS_ENV:-ndqgis}"              # shared with the ND installer (reused if present)
ANUGA_ENV="${DS_ANUGA_ENV:-ndanuga}"
QGIS_VER="${DS_QGIS_VER:-3.34}"
PROFILE="${DS_QGIS_PROFILE:-damsafety}"        # NOT "default" — keeps it apart from the ND set
PROFILE_DIR="$HOME/Library/Application Support/QGIS/QGIS3/profiles/$PROFILE"

# conda-forge, strict, native subdir (set in step 0 once the chip is known)
CF=(-c conda-forge --override-channels)
STRICT=(--strict-channel-priority)

SKIP_LISFLOOD=0
MODE="install"
for a in "$@"; do
  case "$a" in
    --check)         MODE="check" ;;
    --skip-lisflood) SKIP_LISFLOOD=1 ;;
    -h|--help)       sed -n '2,62p' "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# the DamSafety plugin folder names (as each zip unpacks) — used to enable them
DS_PLUGINS=(DamSafety_Setup DamSafety_Data DamSafety_Screening DamSafety_Runner \
            DamSafety_Hazard DamSafety_HazardLite DamSafety_PMP)

# ---------------------------------------------------------------------------
# 0. sanity + chip detection
# ---------------------------------------------------------------------------
OS="$(uname -s)"
if [ "$OS" != "Darwin" ]; then
  # DS_TEST_LINUX=1 is only for testing this script on a Linux build machine.
  [ "${DS_TEST_LINUX:-0}" = "1" ] || die "This script is for macOS."
  PROFILE_DIR="$HOME/.local/share/QGIS/QGIS3/profiles/$PROFILE"
fi
[ "$(id -u)" != "0" ] || die "Do not run this with sudo; run it as yourself."
ncpu() { if [ "$OS" = Darwin ]; then sysctl -n hw.ncpu; else nproc; fi; }
# performance cores (M2 Pro: 6 or 8); E-cores slow OpenMP solvers down
pcores() { if [ "$OS" = Darwin ]; then sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || sysctl -n hw.ncpu; else nproc; fi; }

# --- log everything to a file as well as the screen -------------------------
mkdir -p "$PREFIX/logs"
LOG="$PREFIX/logs/install_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
echo "DamSafety Mac installer $(date)  log: $LOG"
ARCH="$(uname -m)"
case "${DS_SUBDIR:-$ARCH}" in
  x86_64|osx-64)   SUBDIR="osx-64";    MF_ARCH="x86_64"; CHIP="Intel"
                   [ "$OS" = Darwin ] || { SUBDIR="linux-64"; CHIP="Linux test"; } ;;
  arm64|osx-arm64) SUBDIR="osx-arm64"; MF_ARCH="arm64";  CHIP="Apple Silicon" ;;
  *) die "Unsupported arch '$ARCH'. Set DS_SUBDIR=osx-64 or osx-arm64 to override." ;;
esac
export CONDA_SUBDIR="$SUBDIR"
if [ "$OS" = Darwin ] && [ "$ARCH" = arm64 ] && [ "$SUBDIR" = osx-64 ]; then
  warn "DS_SUBDIR=osx-64 on Apple Silicon runs everything under Rosetta (slower)."
fi
say "Mac chip: $CHIP ($ARCH) -> conda subdir $SUBDIR (native, strict conda-forge)."

# ---------------------------------------------------------------------------
# 1. conda (Miniforge) — installed only if you don't already have it
# ---------------------------------------------------------------------------
locate_conda() {
  if [ -n "${DS_MINIFORGE:-}" ] && [ -x "$DS_MINIFORGE/bin/conda" ]; then CONDA_EXE="$DS_MINIFORGE/bin/conda"; CONDA_KIND=conda; return; fi
  if command -v mamba >/dev/null 2>&1; then CONDA_EXE="$(command -v mamba)"; CONDA_KIND=mamba; return; fi
  if command -v conda >/dev/null 2>&1; then CONDA_EXE="$(command -v conda)"; CONDA_KIND=conda; return; fi
  if [ -x "$MINIFORGE_DIR/bin/conda" ]; then CONDA_EXE="$MINIFORGE_DIR/bin/conda"; CONDA_KIND=conda; return; fi
  CONDA_EXE=""; CONDA_KIND=""
}
locate_conda

# Miniforge is pinned to one release and its installer's sha256 is checked before it runs.
# The hashes are the release's own published <asset>.sha256 files
# (github.com/conda-forge/miniforge/releases/download/26.9.4-1/Miniforge3-26.9.4-1-MacOSX-<arch>.sh.sha256).
MF_VER="26.9.4-1"
case "$MF_ARCH" in
  arm64)  MF_SHA256="63f8f5367cd3e52d1dba3e22d8ad91d10ea0e93675ba5b0822312a4342d962e0" ;;
  x86_64) MF_SHA256="66b9e771fa268f30ce97c4f69bbce64a7e610dfd4cb993fb3b424466c3e42b50" ;;
  *)      MF_SHA256="" ;;
esac
sha256_of() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi; }
MF_TMP=""
cleanup_mf() { if [ -n "$MF_TMP" ]; then rm -rf "$MF_TMP"; fi; }
trap cleanup_mf EXIT

if [ "$MODE" = "install" ] && [ -z "$CONDA_EXE" ]; then
  say "No conda found — installing Miniforge $MF_VER to $MINIFORGE_DIR (conda-forge's own installer, not Homebrew)."
  [ "$OS" = Darwin ] || [ -n "${DS_MINIFORGE_SH:-}" ] || die "No conda found (Linux test: set DS_MINIFORGE)."
  [ -n "$MF_SHA256" ] || die "No pinned Miniforge for arch '$MF_ARCH'."
  MF_NAME="Miniforge3-${MF_VER}-MacOSX-${MF_ARCH}.sh"
  MF_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dwr-miniforge.XXXXXX")"
  if [ -n "${DS_MINIFORGE_SH:-}" ]; then
    [ -f "$DS_MINIFORGE_SH" ] || die "DS_MINIFORGE_SH=$DS_MINIFORGE_SH is not a file."
    cp "$DS_MINIFORGE_SH" "$MF_TMP/$MF_NAME"
  else
    URL="https://github.com/conda-forge/miniforge/releases/download/${MF_VER}/${MF_NAME}"
    curl -fSL --retry 3 --connect-timeout 30 "$URL" -o "$MF_TMP/$MF_NAME" || die "Could not download Miniforge from $URL"
  fi
  GOT="$(sha256_of "$MF_TMP/$MF_NAME")"
  [ "$GOT" = "$MF_SHA256" ] || die "sha256 mismatch for $MF_NAME: got $GOT, expected $MF_SHA256. Nothing was run."
  echo "  $MF_NAME sha256 OK"
  [ "$OS" = Darwin ] || die "Linux test: the Miniforge check passed; not running a macOS installer here."
  bash "$MF_TMP/$MF_NAME" -b -p "$MINIFORGE_DIR"
  cleanup_mf; MF_TMP=""
  CONDA_EXE="$MINIFORGE_DIR/bin/conda"; CONDA_KIND=conda
fi
[ -n "$CONDA_EXE" ] || die "conda not found. Install Miniforge, or re-run without --check."

CONDA_BASE="$("$CONDA_EXE" info --base)"
say "conda: $CONDA_EXE ($CONDA_KIND, base $CONDA_BASE)"
env_prefix() { echo "$CONDA_BASE/envs/$1"; }
# DS_QGIS_PREFIX / DS_ANUGA_PREFIX: use an existing env at that path as is (never changed)
QGIS_PREFIX="${DS_QGIS_PREFIX:-$(env_prefix "$QGIS_ENV")}"
ANUGA_PREFIX="${DS_ANUGA_PREFIX:-$(env_prefix "$ANUGA_ENV")}"
qpy() {    # the ndqgis python with what the env's qgis-activate.sh sets (no `conda run`: it
           # does not reliably pass stdin); used for the settings step and the self-check
  env PYTHONPATH="$QGIS_PREFIX/share/qgis/python:$QGIS_PREFIX/share/qgis/python/plugins" \
      QT_PLUGIN_PATH="$QGIS_PREFIX/plugins" QGIS_PREFIX_PATH="$QGIS_PREFIX" \
      PROJ_DATA="$QGIS_PREFIX/share/proj" GDAL_DATA="$QGIS_PREFIX/share/gdal" \
      QT_QPA_PLATFORM=offscreen "$QGIS_PREFIX/bin/python" "$@"
}

if "$CONDA_EXE" run -n base mamba --version >/dev/null 2>&1; then
  SOLVER_PREFIX=("$CONDA_EXE" run -n base mamba)
else
  SOLVER_PREFIX=("$CONDA_EXE")
fi

create_env() {   # create_env <name> <spec...>
  local name="$1"; shift
  if [ "$name" = "$QGIS_ENV" ] && [ -n "${DS_QGIS_PREFIX:-}" ]; then say "Using the QGIS env at $DS_QGIS_PREFIX (DS_QGIS_PREFIX)."; return; fi
  if [ "$name" = "$ANUGA_ENV" ] && [ -n "${DS_ANUGA_PREFIX:-}" ]; then say "Using the ANUGA env at $DS_ANUGA_PREFIX (DS_ANUGA_PREFIX)."; return; fi
  if [ -d "$(env_prefix "$name")/conda-meta" ]; then
    say "conda env '$name' already exists — leaving it as is."
    return
  fi
  say "Creating conda env '$name' (conda-forge, strict, $SUBDIR): $*"
  "${SOLVER_PREFIX[@]}" create -y -n "$name" "${CF[@]}" "${STRICT[@]}" "$@"
}

# ---------------------------------------------------------------------------
# --check mode: report and stop
# ---------------------------------------------------------------------------
if [ "$MODE" = "check" ]; then
  say "conda:        $CONDA_EXE  (base $CONDA_BASE)"
  for p in "$QGIS_PREFIX" "$ANUGA_PREFIX"; do
    if [ -d "$p/conda-meta" ]; then echo "  env: present at $p"; else echo "  env: MISSING at $p"; fi
  done
  echo "  qgis:       $QGIS_PREFIX/bin/qgis  $( [ -x "$QGIS_PREFIX/bin/qgis" ] && echo OK || echo missing)"
  if [ -x "$PREFIX/bin/lisflood" ]; then
    if grep -aq inittm_depth "$PREFIX/bin/lisflood"; then st="OK (Runner patch present)"; else st="UNPATCHED - the Runner will refuse it"; fi
  else st=missing; fi
  echo "  LISFLOOD-FP: $PREFIX/bin/lisflood  $st"
  echo "  profile:    $PROFILE_DIR"
  for p in "${DS_PLUGINS[@]}"; do
    v="$(sed -n 's/^version=//p' "$PROFILE_DIR/python/plugins/$p/metadata.txt" 2>/dev/null | tr -d '\r' || true)"
    echo "    plugin $p: $( [ -d "$PROFILE_DIR/python/plugins/$p" ] && echo "installed ${v:-?}" || echo missing)"
  done
  echo "  Absaraka:   $PREFIX/absaraka-run  $( [ -f "$PREFIX/absaraka-run/Dams/DWR-2801/dam.json" ] && echo ready || echo missing)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. the conda environments (identical to the ND set, so reused if present)
# ---------------------------------------------------------------------------
# QGIS 3.34 is built against NumPy 1.x -> numpy<2 (the solver picks 1.26). Same
# versions as the box Grok Bot runs on: qgis 3.34.6, numpy 1.26.4, gdal 3.8.5.
create_env "$QGIS_ENV" "python=3.12" "qgis=$QGIS_VER" "numpy<2" scipy h5py matplotlib-base
# ANUGA 4.0.1 + gmsh 4.15 (the box runs anuga 4.0.1, gmsh 4.15.2). The Runner
# meshes with gmsh (NDBREACH_ANUGA_MESHER=gmsh) and reads/writes NetCDF.
create_env "$ANUGA_ENV" "python=3.12" "anuga=4.0.1" "gmsh=4.15" python-gmsh numpy scipy gdal netcdf4 matplotlib-base

"$ANUGA_PREFIX/bin/python" -c "import anuga,gmsh;print('ANUGA',anuga.__version__,'| gmsh',gmsh.__version__)" \
  || warn "ANUGA/gmsh import check failed — see output above."
ANUGA_PY="$ANUGA_PREFIX/bin/python"
[ -x "$ANUGA_PY" ] || die "ANUGA python not found at $ANUGA_PY"
[ -x "$QGIS_PREFIX/bin/qgis_process" ] || die "qgis_process not found in $QGIS_PREFIX (QGIS env incomplete?)"

# ---------------------------------------------------------------------------
# 3. install the DamSafety plugins into their own QGIS profile
# ---------------------------------------------------------------------------
say "Installing the DamSafety plugins from $PLUGIN_DIR into profile '$PROFILE'."
PLUGINS_DEST="$PROFILE_DIR/python/plugins"
mkdir -p "$PLUGINS_DEST"
shopt -s nullglob
zips=("$PLUGIN_DIR"/*.zip)
if [ "${#zips[@]}" -lt 5 ]; then
  # The DamSafety (DWR_Plugins) repo publishes no plugin zips, so there is no
  # GitHub self-heal as there is for the ND set — the zips must ship in this
  # package. Point DS_PLUGIN_DIR at them, or run from inside the full package.
  die "Found only ${#zips[@]} plugin zips in $PLUGIN_DIR. Run this script from INSIDE the unpacked package (it ships the DamSafety zips in plugins/), or set DS_PLUGIN_DIR to a folder holding them. (In a DWR_Plugins checkout: python3 tools/mac/make_plugin_zips.py builds them.)"
fi
plugin_ver() { sed -n 's/^version=//p' "$1/metadata.txt" 2>/dev/null | tr -d '\r'; }
for z in "${zips[@]}"; do
  # each zip contains one top-level plugin folder; replace it cleanly.
  # (read the whole listing, then take the first entry — piping to `head` makes
  # `unzip` die with SIGPIPE, which `set -o pipefail` turns into a silent exit.)
  listing="$(unzip -Z1 "$z")"; first="${listing%%$'\n'*}"; top="${first%%/*}"
  zipver="$(unzip -p "$z" "$top/metadata.txt" 2>/dev/null | sed -n 's/^version=//p' | tr -d '\r')"
  if [ -n "$zipver" ] && [ "$(plugin_ver "$PLUGINS_DEST/$top")" = "$zipver" ]; then
    echo "  $top $zipver already installed"
    continue
  fi
  echo "  $top  <-  $(basename "$z")"
  if [ -e "$PLUGINS_DEST/${top:?}" ]; then
    # keep the old copy (outside plugins/, so QGIS does not load it twice)
    BK="$PROFILE_DIR/python/plugins_replaced/$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BK" && mv "$PLUGINS_DEST/$top" "$BK/"
    echo "    (old copy moved to $BK/$top)"
  fi
  unzip -q "$z" -d "$PLUGINS_DEST"
done
RUNNER_DIR="$PLUGINS_DEST/DamSafety_Runner"
[ -d "$RUNNER_DIR" ] || die "DamSafety_Runner plugin did not unpack to $RUNNER_DIR"

# ---------------------------------------------------------------------------
# 4. LISFLOOD-FP — reuse an existing patched build, or compile the bundled 8.3
# ---------------------------------------------------------------------------
# The DamSafety Runner refuses a LISFLOOD-FP build without its patch: at run time it
# looks for "Unknown parameter ignored: inittm_depth" in LISFLOOD's output
# (core/lisflood.py, PATCH_MISSING_MARKER). The patch adds the `inittm_depth`
# keyword, so a patched binary contains that string; we check it here up front.
is_patched() { [ -x "$1" ] && grep -aq inittm_depth "$1"; }
runs_ok()    { local b; b="$("$1" -version 2>&1 || true)"; grep -q "LISFLOOD-FP" <<<"$b"; }

# finalize_lisflood <binary> [lib dir]: copy it (and the libomp / libc++ it needs) into
# $PREFIX so this install is self-contained, then re-point and ad-hoc codesign it.
finalize_lisflood() {
  local src="$1" libdir="${2:-}" ref name from
  mkdir -p "$PREFIX/bin" "$PREFIX/lib"
  [ "$src" -ef "$PREFIX/bin/lisflood" ] || cp -f "$src" "$PREFIX/bin/lisflood"
  chmod 755 "$PREFIX/bin/lisflood"
  [ "$OS" = Darwin ] || return 0
  for ref in $(otool -L "$PREFIX/bin/lisflood" | awk 'NR>1 && /(libomp|libc\+\+|libc\+\+abi|libunwind)\.[0-9.]*dylib/{print $1}'); do
    case "$ref" in /usr/lib/*|/System/*) continue ;; esac
    name="$(basename "$ref")"; from=""
    case "$ref" in /*) [ -f "$ref" ] && from="$ref" ;; esac
    [ -z "$from" ] && [ -n "$libdir" ] && [ -f "$libdir/$name" ] && from="$libdir/$name"
    [ -z "$from" ] && [ -f "$(dirname "$src")/../lib/$name" ] && from="$(dirname "$src")/../lib/$name"
    [ -n "$from" ] && cp -fL "$from" "$PREFIX/lib/$name"
    install_name_tool -change "$ref" "@rpath/$name" "$PREFIX/bin/lisflood" 2>/dev/null || true
  done
  install_name_tool -add_rpath "$PREFIX/lib" "$PREFIX/bin/lisflood" 2>/dev/null || true
  codesign -f -s - "$PREFIX/bin/lisflood" 2>/dev/null || true
}

LISFLOOD_EXE=""
if [ -n "${DS_LISFLOOD:-}" ]; then
  [ -x "$DS_LISFLOOD" ] || die "DS_LISFLOOD=$DS_LISFLOOD is not an executable file."
  is_patched "$DS_LISFLOOD" || warn "DS_LISFLOOD has no 'inittm_depth' (not the Runner-patched build) - the Runner will refuse it."
  LISFLOOD_EXE="$DS_LISFLOOD"
  say "Using the LISFLOOD-FP you pointed me at (DS_LISFLOOD): $LISFLOOD_EXE"
elif is_patched "$PREFIX/bin/lisflood" && runs_ok "$PREFIX/bin/lisflood"; then
  LISFLOOD_EXE="$PREFIX/bin/lisflood"
  say "LISFLOOD-FP already installed and patched: $LISFLOOD_EXE (delete it to force a rebuild)"
elif [ "$SKIP_LISFLOOD" -eq 1 ]; then
  say "Skipping LISFLOOD-FP (--skip-lisflood). The Runner uses its built-in engine (tick 'use built-in' in it); run_nickreview.sh needs a patched LISFLOOD-FP."
elif [ -x "$HOME/nd-dam-suite/bin/lisflood" ] && runs_ok "$HOME/nd-dam-suite/bin/lisflood"; then
  if is_patched "$HOME/nd-dam-suite/bin/lisflood"; then
    say "Reusing the patched LISFLOOD-FP already built for the ND set ($HOME/nd-dam-suite/bin/lisflood) — copying it in."
    finalize_lisflood "$HOME/nd-dam-suite/bin/lisflood" "$HOME/nd-dam-suite/lib"
    if runs_ok "$PREFIX/bin/lisflood"; then LISFLOOD_EXE="$PREFIX/bin/lisflood"; say "LISFLOOD-FP ready: $LISFLOOD_EXE";
    else warn "Copied LISFLOOD-FP did not run; will build from source instead."; fi
  else
    warn "$HOME/nd-dam-suite/bin/lisflood is NOT Runner-patched (no inittm_depth) - building a patched one instead."
  fi
fi

if [ -z "$LISFLOOD_EXE" ] && [ "$SKIP_LISFLOOD" -ne 1 ]; then
  if [ "$OS" = Darwin ] && ! xcode-select -p >/dev/null 2>&1; then
    warn "Apple Command Line Tools are not installed, so LISFLOOD-FP cannot be compiled."
    warn "Install them with:   xcode-select --install"
    warn "then re-run this script. (This is an Apple tool, not Homebrew.)"
    warn "For now the Runner will use its slower built-in Python engine (tick 'use built-in')."
  else
    [ "${DS_ENABLE_METAL:-0}" = "1" ] && warn "DS_ENABLE_METAL ignored: LISFLOOD-FP 8.3 has no Metal back end (CUDA only)."
    say "Building LISFLOOD-FP 8.3: official Zenodo source + the Runner patch, conda-forge compilers."
    if [ "$OS" = Darwin ]; then
      create_env ndbuild cmake make llvm-openmp c-compiler cxx-compiler
    else
      create_env ndbuild cmake make c-compiler cxx-compiler
    fi
    BARGS=(--conda "$CONDA_EXE" --env ndbuild --out "$PREFIX" --jobs "${DS_JOBS:-$(ncpu)}"
           --python "$QGIS_PREFIX/bin/python" --patcher "$RUNNER_DIR/scripts/patch_lisflood.py")
    [ -n "${DS_LISFLOOD_ZIP:-}" ] && BARGS+=(--zip "$DS_LISFLOOD_ZIP")
    [ "${DS_APPLE_CLANG:-0}" = "1" ] && BARGS+=(--apple-clang)
    BUILT=""
    if BUILT="$(bash "$HERE/lisflood/build_lisflood.sh" "${BARGS[@]}" | tail -n 1)" && [ -x "$BUILT" ]; then :
    elif [ "$OS" = Darwin ] && [ "${DS_APPLE_CLANG:-0}" != "1" ]; then
      warn "conda-forge clang build failed - retrying once with Apple's clang."
      BUILT="$(bash "$HERE/lisflood/build_lisflood.sh" "${BARGS[@]}" --apple-clang | tail -n 1)" || BUILT=""
    else
      BUILT=""
    fi
    if [ -n "$BUILT" ] && [ -x "$BUILT" ]; then
      finalize_lisflood "$BUILT" "$(env_prefix ndbuild)/lib"
      if [ "$OS" = Darwin ]; then
        LINKS="$(otool -L "$PREFIX/bin/lisflood" 2>&1 || true)"
        grep -qi libomp <<<"$LINKS" || warn "lisflood is not linked against libomp — it may run single-threaded."
      fi
      if runs_ok "$PREFIX/bin/lisflood" && is_patched "$PREFIX/bin/lisflood"; then
        LISFLOOD_EXE="$PREFIX/bin/lisflood"
        say "LISFLOOD-FP built: $LISFLOOD_EXE (8.3 + Runner patch)"
      else
        warn "LISFLOOD-FP built but did not run (try '$PREFIX/bin/lisflood -version'); leaving the Runner on its built-in engine."
      fi
    else
      warn "LISFLOOD-FP build failed (see the [lisflood] lines above and the logs of each attempt in $PREFIX/src/lisflood-logs/)."
      warn "The Runner will use its built-in engine. Retry later with:  bash $(basename "$0")"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 5. write the QGIS settings (plugins on + Processing + the ndsuite/ suite keys)
# ---------------------------------------------------------------------------
say "Enabling the plugins and writing the DamSafety settings into the profile."
mkdir -p "$PROFILE_DIR/QGIS"
INI="$PROFILE_DIR/QGIS/QGIS3.ini"
THREADS="${DS_THREADS:-$(pcores)}"
RUN="$PREFIX/absaraka-run"
DAMS_FOLDER="$RUN/Dams"
# (the env's python is called directly: `conda run` does not reliably pass stdin)
# The script is read from stdin (python -) via the heredoc; the DamSafety plugin
# names ride along as argv after the five fixed arguments.
qpy - \
    "$INI" "$ANUGA_PY" "$LISFLOOD_EXE" "$THREADS" "$DAMS_FOLDER" "${DS_PLUGINS[@]}" <<'PY'
import sys
from qgis.PyQt.QtCore import QSettings
ini, anuga_py, lisflood_exe, threads, dams_folder = sys.argv[1:6]
plugins = sys.argv[6:]
s = QSettings(ini, QSettings.IniFormat)
# enable Processing + every DamSafety plugin
s.setValue("PythonPlugins/processing", True)
for p in plugins:
    s.setValue("PythonPlugins/%s" % p, True)
# Processing provider settings the Runner / Screening read
s.setValue("Processing/Configuration/NDBREACH_ANUGA_PYTHON", anuga_py)
s.setValue("Processing/Configuration/NDBREACH_THREADS", threads)
# NDBREACH_THREADS (> 0) is used for LISFLOOD-FP AND ANUGA (Runner resolve_threads). The
# separate ANUGA count NDBREACH_ANUGA_THREADS is an ENVIRONMENT variable only (read when
# THREADS is 0), so it is not written here. Thread counts never change the results.
# the same LISFLOOD-FP value as damsafety_env.sh, also when it is empty (an empty value is
# "not set" for the Runner), so an older path never stays behind in the profile
s.setValue("Processing/Configuration/NDBREACH_LISFLOOD_EXE", lisflood_exe)
# the DamSafety "suite" store (what Setup normally writes): group ndsuite/
s.setValue("ndsuite/anuga_python", anuga_py)
s.setValue("ndsuite/dams_folder", dams_folder)
s.setValue("ndsuite/setup_version", "mac-install 2026-10-26")
s.setValue("ndsuite/lisflood_exe", lisflood_exe)
s.sync()
print("wrote", ini)
print("  ANUGA_PYTHON =", anuga_py)
print("  THREADS      =", threads, "(LISFLOOD-FP and ANUGA)")
op = s.value("ND_Dam_Hazard_Helper/offline_pack") or ""
print("  Hazard offline_pack =", repr(op) if op else "empty (not set by this installer)")
print("  LISFLOOD_EXE =", lisflood_exe or "(unset -> built-in engine)")
print("  dams_folder  =", dams_folder)
PY

# ---------------------------------------------------------------------------
# 6. the ready-to-run Absaraka folder
# ---------------------------------------------------------------------------
say "Setting up the Absaraka (DWR-2801) run folder."
mkdir -p "$RUN"
if [ -d "$ABSARAKA_SRC" ]; then
  # copy the reference files; never overwrite anything already in the run folder
  (cd "$ABSARAKA_SRC" && find . -type d -exec mkdir -p "$RUN/{}" \; )
  (cd "$ABSARAKA_SRC" && find . -type f | while IFS= read -r f; do
     [ -e "$RUN/$f" ] || cp -p "$f" "$RUN/$f"; done)
  # a dam.json already in the run folder (an earlier install, or the Runner / Screening wrote
  # to it) keeps every field it has, null ones too (a null with its reason is the suite's
  # "no value"); a field it does not have is added from the package copy with its source
  # (e.g. dam_length_ft, so the Runner does not fall back to its 400 ft default crest length)
  if [ -f "$ABSARAKA_SRC/Dams/DWR-2801/dam.json" ] && [ -f "$RUN/Dams/DWR-2801/dam.json" ]; then
    qpy - "$ABSARAKA_SRC/Dams/DWR-2801/dam.json" "$RUN/Dams/DWR-2801/dam.json" <<'PY' \
      || warn "Could not add the package's fields to $RUN/Dams/DWR-2801/dam.json (left as it is)."
import json, os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as f:
    s = json.load(f)
with open(dst, encoding="utf-8") as f:
    d = json.load(f)
if d.get("format") != s.get("format") or d.get("format_version") != s.get("format_version") \
        or not isinstance(d.get("fields"), dict):
    print("  dam.json: not the same record format as the package's; left as it is")
    sys.exit(0)
add = sorted(k for k, v in s["fields"].items() if v is not None and k not in d["fields"])
if not add:
    sys.exit(0)
for k in add:
    d["fields"][k] = s["fields"][k]
    if k in (s.get("sources") or {}):
        d.setdefault("sources", {})[k] = s["sources"][k]
d["fields"] = {k: d["fields"][k] for k in sorted(d["fields"])}
d["sources"] = {k: d["sources"][k] for k in sorted(d.get("sources") or {})}
tmp = dst + ".tmp"
with open(tmp, "w", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps(d, indent=1, ensure_ascii=False) + "\n")
os.replace(tmp, dst)
print("  dam.json: added " + ", ".join(add) + " from the package copy")
PY
  fi
fi
mkdir -p "$RUN/suite" "$RUN/work" "$RUN/Dams/DWR-2801/data"
if [ ! -f "$RUN/Dams/DWR-2801/dam.json" ]; then
  cat > "$RUN/Dams/DWR-2801/dam.json" <<'JSON'
{
 "format": "nd_dam_suite/dam_record",
 "format_version": 1,
 "fields": {
  "dam_id": "DWR-2801", "dwr_structure_id": "2801", "nid_id": "ND00199",
  "name": "Swan Buffalo Detention Dam 12", "existing_hazard_class": "Significant",
  "height_ft": 27.5, "dam_length_ft": 1700.0, "storage_acft": 1615.496,
  "lat": 46.9830314, "lon": -97.3914888
 },
 "sources": {"_seed": {"tool": "DamSafety mac-install script", "when": "2026-10-26",
   "note": "Absaraka / DWR-2801 inventory identity; Screening overwrites/extends, Runner adds DEM + top-of-dam."}},
 "status": []
}
JSON
fi
echo "  run folder: $RUN"

# a double-clickable launcher that opens QGIS in the damsafety profile + env
LAUNCH="$PREFIX/Open DWR QGIS.command"
cat > "$LAUNCH" <<EOF
#!/bin/bash
# Opens QGIS (shared conda env) in the DamSafety "$PROFILE" profile with the
# DamSafety plugins loaded. The env vars are belt-and-suspenders fallbacks the
# suite reads if a Processing/QgsSettings value is ever missing.
source "$PREFIX/damsafety_env.sh"
export NDSUITE_DAMS_FOLDER="$DAMS_FOLDER"
if [ -x "$QGIS_PREFIX/QGIS.app/Contents/MacOS/QGIS" ]; then Q="$QGIS_PREFIX/QGIS.app/Contents/MacOS/QGIS"; else Q="$QGIS_PREFIX/bin/qgis"; fi
exec "$CONDA_EXE" run -p "$QGIS_PREFIX" "\$Q" --profile "$PROFILE" "\$@"
EOF
chmod +x "$LAUNCH"
[ "$OS" = Darwin ] && xattr -d com.apple.quarantine "$LAUNCH" 2>/dev/null || true

# damsafety_env.sh: the same environment the Grok Bot box runs use (env_nr.sh), for
# run_nickreview.sh and for Terminal use:  source ~/dwr-dam-suite/damsafety_env.sh
HEADLESS="$PREFIX/qgis_headless_config"
mkdir -p "$HEADLESS/profiles"
if [ ! -L "$HEADLESS/profiles/default" ]; then
  [ -e "$HEADLESS/profiles/default" ] && die "$HEADLESS/profiles/default exists and is not a link."
  ln -s "$PROFILE_DIR" "$HEADLESS/profiles/default"   # headless qgis_process sees the damsafety profile
fi
cat > "$PREFIX/damsafety_env.sh.new" <<EOF
# DamSafety environment, written by install_dwr_suite_mac.sh on $(date). Re-running rewrites it.
export DAMSAFETY_PREFIX="$PREFIX"
export DAMSAFETY_WORK="$PREFIX"
export DAMSAFETY_PROFILE="$PROFILE"
export DAMSAFETY_PROFILE_DIR="$PROFILE_DIR"
export DAMSAFETY_PLUGINS="$PLUGINS_DEST"
export DAMSAFETY_HEADLESS_CONFIG="$HEADLESS"
export DAMSAFETY_THREADS="$THREADS"
export DAMSAFETY_QGIS_PYTHON="$QGIS_PREFIX/bin/python"
export QGIS_PROCESS="$QGIS_PREFIX/bin/qgis_process"
export NDBREACH_ANUGA_PYTHON="$ANUGA_PY"
export NDBREACH_LISFLOOD="$LISFLOOD_EXE"
export NDBREACH_ANUGA_MESHER=gmsh
export NDBREACH_DEM_RESAMPLE=average
export QGIS_PREFIX_PATH="$QGIS_PREFIX"            # qgis_process outside an activated env (macOS)
export QT_PLUGIN_PATH="$QGIS_PREFIX/plugins"
export PROJ_DATA="$QGIS_PREFIX/share/proj"
export PROJ_LIB="$QGIS_PREFIX/share/proj"
export GDAL_DATA="$QGIS_PREFIX/share/gdal"
EOF
mv "$PREFIX/damsafety_env.sh.new" "$PREFIX/damsafety_env.sh"
echo "  env file:   $PREFIX/damsafety_env.sh"
echo "  launcher:   $LAUNCH   (double-click to open QGIS in the '$PROFILE' profile)"

# ---------------------------------------------------------------------------
# 7. self-check (starts no model: versions, imports, plugin folders only)
# ---------------------------------------------------------------------------
say "Self-check (no models are run)."
FAIL=0
QV="$(qpy -c 'import qgis.core as q, numpy; print(q.Qgis.QGIS_VERSION, "| numpy", numpy.__version__)' 2>/dev/null || true)"
case "$QV" in 3.34.*) echo "  OK  QGIS $QV" ;; *) warn "QGIS 3.34 does not import (got: '$QV')"; FAIL=1 ;; esac
AV="$("$ANUGA_PY" -c 'import anuga, gmsh; print(anuga.__version__, "| gmsh", gmsh.__version__)' 2>/dev/null || true)"
if [ -n "$AV" ]; then echo "  OK  ANUGA $AV"; else warn "anuga/gmsh do not import in $ANUGA_PREFIX"; FAIL=1; fi
for p in "${DS_PLUGINS[@]}"; do
  if [ -f "$PLUGINS_DEST/$p/metadata.txt" ]; then
    echo "  OK  $p $(awk -F= '/^version=/{print $2; exit}' "$PLUGINS_DEST/$p/metadata.txt")"
  else warn "plugin $p missing"; FAIL=1; fi
done
if [ -n "$LISFLOOD_EXE" ]; then
  if runs_ok "$LISFLOOD_EXE" && is_patched "$LISFLOOD_EXE"; then echo "  OK  LISFLOOD-FP $LISFLOOD_EXE (Runner patch present)"
  else warn "LISFLOOD-FP $LISFLOOD_EXE does not run or is unpatched"; FAIL=1; fi
elif [ "$SKIP_LISFLOOD" -eq 1 ]; then
  warn "no LISFLOOD-FP binary (--skip-lisflood: built-in engine only; run_nickreview.sh needs a patched LISFLOOD-FP)"
else
  warn "no LISFLOOD-FP binary: the build failed or could not run (see above). run_nickreview.sh needs one; the Runner dialog can only use its built-in engine."
  FAIL=1
fi
if [ "$FAIL" = 0 ]; then echo "  All checks passed."; else warn "Some checks failed - see the WARNING lines above and $LOG"; fi

# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------
say "Done."
cat <<EOF

  QGIS            ${QV:-$QGIS_VER}  (env '$QGIS_ENV')
  ANUGA python    $ANUGA_PY   (mesher: gmsh)
  LISFLOOD-FP     ${LISFLOOD_EXE:-built-in engine (no binary built)}
  CPU threads     $THREADS   (LISFLOOD + ANUGA)
  Plugins         ${#zips[@]} zips from $PLUGIN_DIR in profile '$PROFILE'
  Absaraka run    $RUN

  Open QGIS:      double-click  "$LAUNCH"
                  (or in Terminal:  "$LAUNCH"   - it sources damsafety_env.sh first)
  Then follow RUN-ABSARAKA.md (in $RUN).
  NickReview dams (Terminal):  bash run_nickreview.sh --dry-run    (see README_MAC.md)
  Install log:    $LOG

  NOTE: this uses the '$PROFILE' profile so it won't clash with the ND set's
  'default' profile. Keep each plugin set in its own profile.

  Re-run this script any time; it skips steps that are already done.
  Check an install:  bash $(basename "$0") --check
EOF
exit "$FAIL"
