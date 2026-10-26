#!/usr/bin/env bash
# Run the NickReview dams on this Mac (after install_dwr_suite_mac.sh), with the same
# settings the Grok Bot box runs use (see absaraka/grok_settings_DWR-2801.json).
#
#   ./run_nickreview.sh --dry-run                 # show what would run, change nothing
#   ./run_nickreview.sh --dam DWR-2801            # one dam
#   ./run_nickreview.sh                           # all dams in nickreview_dams.txt (resumes)
#
# Options:
#   --data DIR       NickReview data copied from the box (read only)  [~/dwr-dam-suite/NickReview_data]
#   --out DIR        results                                          [~/dwr-dam-suite/Dams/NickReview_mac]
#   --dam ID         only this dam (repeat for more)
#   --dams FILE      dam list                                         [nickreview_dams.txt]
#   --threads N      CPU threads for LISFLOOD-FP / ANUGA              [8]
#   --redo           run stages that finished before again (default: skip them): the stages
#                    in --stages and every stage after them; earlier ones are kept
#   --stages LIST    subset of prepare,structures,precheck,record,rundam,hazclass,hazbatch
#   --offline        NHD flowlines from the suite pack instead of downloading 3DHP streams
#   --allow-no-record  run rundam even if the Screening 7b record (dam.json) could not be
#                    written (the crest length then differs from the box runs)
#   --pack FILE      offline pack .sqlite (default: the one in --data, if there)
#   --no-kits        prepare DWR-2801/2865/3034 here instead of using the box preparation
#   --map BOX=MAC    extra path mapping for box paths in the kit files (repeatable)
#   --dry-run        print the commands and planned inputs; run and write nothing
#   --list-data TSV  (box side) list every file the dams need (bytes, copy_to, source) and
#                    write the DATA_TO_COPY_*.md summary next to it; implies --dry-run
#
# Ctrl-C stops the run; the log, the summary and the manifest of the dams so far are kept.
# shellcheck disable=SC2154  # DAMSAFETY_* / QGIS_PROCESS / NDBREACH_* come from the sourced env file (checked below)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for a in "$@"; do case "$a" in -h|--help) sed -n '2,28p' "$0"; exit 0 ;; *) ;; esac; done
ENVF="${DAMSAFETY_ENV_FILE:-$HOME/dwr-dam-suite/damsafety_env.sh}"
if [[ ! -f "$ENVF" ]]; then
  echo "ERROR: $ENVF not found. Run install_dwr_suite_mac.sh first." >&2; exit 1
fi
# shellcheck source=/dev/null
source "$ENVF"

DATA="${DAMSAFETY_WORK:-$HOME/dwr-dam-suite}/NickReview_data"
OUT="${DAMSAFETY_WORK:-$HOME/dwr-dam-suite}/Dams/NickReview_mac"
DAMS="$HERE/nickreview_dams.txt"
THREADS=8
DRY=0
PASS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --data) DATA="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    --dams) DAMS="$2"; shift 2 ;;
    --threads) THREADS="$2"; shift 2 ;;
    --dam|--stages|--pack|--map) PASS+=("$1" "$2"); shift 2 ;;
    --redo|--offline|--no-kits|--allow-no-record) PASS+=("$1"); shift ;;
    --dry-run) DRY=1; PASS+=("$1"); shift ;;
    --list-data) DRY=1; PASS+=("$1" "$2"); shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "ERROR: unknown option $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ "$THREADS" =~ ^[0-9]+$ && "$THREADS" -ge 1 ]] || { echo "ERROR: --threads must be a number" >&2; exit 2; }
[[ -d "$DATA" ]] || { echo "ERROR: data folder not found: $DATA (see README_MAC.md, 'Copy the data')" >&2; exit 1; }
[[ -f "$DAMS" ]] || { echo "ERROR: dam list not found: $DAMS" >&2; exit 1; }

for v in DAMSAFETY_QGIS_PYTHON QGIS_PROCESS DAMSAFETY_PLUGINS NDBREACH_LISFLOOD NDBREACH_ANUGA_PYTHON DAMSAFETY_HEADLESS_CONFIG; do
  [[ -n "${!v:-}" ]] || { echo "ERROR: $v is not set in $ENVF (rerun the installer)" >&2; exit 1; }
done

# Same environment as the box reruns (env_nr.sh)
export QT_QPA_PLATFORM=offscreen PYTHONDONTWRITEBYTECODE=1
export QGIS_CUSTOM_CONFIG_PATH="$DAMSAFETY_HEADLESS_CONFIG"
export NDBREACH_ANUGA_MESHER=gmsh NDBREACH_DEM_RESAMPLE=average
unset NDBREACH_HECRAS_TIMEOUT_S ALLOW_FLAGGED_STRUCTURE NDBREACH_ANUGA_THREADS OMP_NUM_THREADS
SETTINGS_SRC="$HERE/damsuite_settings_nickreview.csv"
if [[ $DRY -eq 0 ]]; then
  mkdir -p "$OUT/logs"
  cp -n "$SETTINGS_SRC" "$OUT/damsuite_settings_nickreview.csv" 2>/dev/null || true
  export NDSUITE_SETTINGS="$OUT/damsuite_settings_nickreview.csv"
  LOG="$OUT/logs/run_nickreview_$(date +%Y%m%d_%H%M%S).log"
  echo "Log: $LOG"
  # keep the Mac awake while running (caffeinate is part of macOS)
  CAFF=()
  if command -v caffeinate >/dev/null 2>&1; then CAFF=(caffeinate -i -s); fi
  # tee ignores Ctrl-C (SIGINT goes to the whole process group), so it keeps logging while
  # the Python script writes the "stopped" line, the summary and the manifest
  "${CAFF[@]}" "$DAMSAFETY_QGIS_PYTHON" "$HERE/nickreview_mac.py" \
    --data "$DATA" --out "$OUT" --dams-file "$DAMS" --threads "$THREADS" \
    --plugins "$DAMSAFETY_PLUGINS" --qgis-process "$QGIS_PROCESS" \
    --anuga-python "$NDBREACH_ANUGA_PYTHON" --lisflood "$NDBREACH_LISFLOOD" \
    ${PASS[@]+"${PASS[@]}"} 2>&1 | (trap '' INT; exec tee -a "$LOG")
  exit "${PIPESTATUS[0]}"
else
  export NDSUITE_SETTINGS="$SETTINGS_SRC"
  exec "$DAMSAFETY_QGIS_PYTHON" "$HERE/nickreview_mac.py" \
    --data "$DATA" --out "$OUT" --dams-file "$DAMS" --threads "$THREADS" \
    --plugins "$DAMSAFETY_PLUGINS" --qgis-process "$QGIS_PROCESS" \
    --anuga-python "$NDBREACH_ANUGA_PYTHON" --lisflood "$NDBREACH_LISFLOOD" \
    ${PASS[@]+"${PASS[@]}"}
fi
