#!/usr/bin/env bash
# Download the USGS 3DEP 1 m LiDAR tiles a data list needs (instead of copying them
# from the box). Each file is checked against the size recorded on the box.
#
#   bash get_lidar_tiles.sh                                   # tiles for Absaraka (DWR-2801)
#   bash get_lidar_tiles.sh data_files_nickreview.tsv         # tiles for all NickReview dams
#   bash get_lidar_tiles.sh --dry-run                         # list what would be downloaded
#
# Files go to $DATA/DEM/lidar_1m_native/ (DATA defaults to ~/dwr-dam-suite/NickReview_data).
# Safe to re-run: complete files are skipped and partial ones resume.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0; TSV="$HERE/absaraka/data_files_DWR-2801.tsv"
DATA="${DATA:-$HOME/dwr-dam-suite/NickReview_data}"
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) TSV="$a" ;;
  esac
done
[[ -f "$TSV" ]] || { echo "ERROR: no data list $TSV" >&2; exit 1; }
BASE=https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects
n=0; bad=0
while IFS=$'\t' read -r bytes rel _; do
  [[ "$rel" == DEM/lidar_1m_native/USGS_1M_*.tif ]] || continue
  name="${rel##*/}"
  proj="${name#USGS_1M_*_x*y*_}"; proj="${proj%.tif}"   # e.g. ND_3DEPProcessing_D22
  dst="$DATA/$rel"; n=$((n + 1))
  have=0; [[ -f "$dst" ]] && have=$(wc -c < "$dst" | tr -d ' ')
  if [[ "$have" == "$bytes" ]]; then echo "ok    $name"; continue; fi
  url="$BASE/$proj/TIFF/$name"
  if [[ $DRY -eq 1 ]]; then echo "get   $name  ($((bytes / 1000000)) MB)  $url"; continue; fi
  mkdir -p "$(dirname "$dst")"
  echo "get   $name  ($((bytes / 1000000)) MB)"
  curl -fL --retry 5 --retry-delay 10 -C - -o "$dst" "$url" || true
  have=$(wc -c < "$dst" 2>/dev/null | tr -d ' ' || echo 0)
  if [[ "$have" != "$bytes" ]]; then echo "ERROR: $name is $have bytes, expected $bytes" >&2; bad=$((bad + 1)); fi
done < <(tail -n +2 "$TSV")
echo "$n LiDAR tiles in $TSV; $bad failed."
[[ $bad -eq 0 ]]
