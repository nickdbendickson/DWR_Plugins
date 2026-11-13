#!/usr/bin/env bash
# run_finder_statewide.sh - run the DamSafety Finder over the whole state on this Mac.
# A thin wrapper: it starts run_finder_statewide.py with the suite's QGIS Python
# (mamba run -n ndqgis python ...) and keeps the Mac awake while it runs.
#
#   bash run_finder_statewide.sh --dry-run      # tile count, time and disk estimate; writes nothing
#   bash run_finder_statewide.sh --limit 4      # a trial: 4 tiles, then the merge
#   bash run_finder_statewide.sh                # the whole state; run it again to go on after a stop
#   bash run_finder_statewide.sh --merge-only   # merge what is finished so far
#
# Every option is passed on as it is (see: bash run_finder_statewide.sh --help). Paths with
# spaces are fine: put them in quotes, e.g.
#   bash run_finder_statewide.sh --dem "/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt"
#
# Environment (all optional):
#   DS_QGIS_ENV        the conda env with QGIS                       [ndqgis]
#   ND_LOCAL_TERRAIN   the terrain VRT, when --dem is not given      [the Extreme SSD path]
#   NDSUITE_DATA       the suite data folder, when --data is not given [~/ND_Suite_Data]
#   FINDER_PYTHON      a Python to use instead of mamba run (it must have GDAL, numpy, scipy)
#
# Ctrl-C stops the run; finished tiles are kept and the same command goes on from there.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/run_finder_statewide.py"
ENV_NAME="${DS_QGIS_ENV:-ndqgis}"
[[ -f "$SCRIPT" ]] || { echo "ERROR: $SCRIPT is not next to this script" >&2; exit 1; }

# The suite's environment file, when the installer wrote one (DAMSAFETY_PLUGINS and friends).
ENVF="${DAMSAFETY_ENV_FILE:-$HOME/dwr-dam-suite/damsafety_env.sh}"
if [[ -f "$ENVF" ]]; then
  # shellcheck source=/dev/null
  source "$ENVF"
fi

RUN=()
if [[ -n "${FINDER_PYTHON:-}" ]]; then
  RUN=("$FINDER_PYTHON")
else
  TOOL=""
  for c in mamba "$HOME/miniforge3/condabin/mamba" "$HOME/miniforge3/bin/mamba" "$HOME/mambaforge/condabin/mamba" \
           micromamba conda "$HOME/miniforge3/condabin/conda" "$HOME/miniforge3/bin/conda"; do
    if command -v "$c" >/dev/null 2>&1; then TOOL="$(command -v "$c")"; break; fi
  done
  if [[ -z "$TOOL" ]]; then
    echo "ERROR: no mamba or conda found. Install the suite first (install_dwr_suite_mac.sh)," >&2
    echo "       or set FINDER_PYTHON to a Python that has GDAL, numpy and scipy." >&2
    exit 1
  fi
  RUN=("$TOOL" run -n "$ENV_NAME")
  # conda (and mamba 1.x, which hands 'run' to conda) holds the output back until the end
  # unless told not to; newer mamba / micromamba show it as it comes and have no such option.
  if "$TOOL" run --help 2>/dev/null | grep -q -- '--no-capture-output'; then
    RUN+=(--no-capture-output)
  fi
  RUN+=(python)
  if ! "${RUN[@]}" -c 'from osgeo import gdal; import numpy, scipy' >/dev/null 2>&1; then
    echo "ERROR: the Python of the '$ENV_NAME' env cannot import GDAL, numpy and scipy." >&2
    echo "       Tried: ${RUN[*]} -c 'from osgeo import gdal; import numpy, scipy'" >&2
    exit 1
  fi
fi

export PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

# keep the Mac awake while it runs (caffeinate is part of macOS); not for the options that only print
CAFF=()
AWAKE=1
for a in "$@"; do case "$a" in --dry-run|--list|-h|--help) AWAKE=0 ;; *) ;; esac; done
if [[ $AWAKE -eq 1 ]] && command -v caffeinate >/dev/null 2>&1; then CAFF=(caffeinate -i -s); fi

exec ${CAFF[@]+"${CAFF[@]}"} "${RUN[@]}" "$SCRIPT" "$@"
