#!/usr/bin/env zsh
# download-library.zsh — download ONLY the vor library files (no ingest, no OCR, no eval).
#
# This is the download step on its own, pulled out of first-run.zsh. It runs fetch_asdso.py to
# fetch the curated catalog (reference_sources.json: FEMA 333, TM-11, ND REG_05, USBR/USACE/NRCS
# design references, ...) and the public ASDSO resource-center PDFs into ~/Vor/library,
# and it writes library-urls.txt — a plain-text list of every source and its web address.
#
#   ./download-library.zsh                 # catalog + ASDSO pages -> ~/Vor/library
#   ./download-library.zsh --dry-run       # list what would be downloaded, fetch nothing
#   ./download-library.zsh --out DIR        # write the library somewhere else
#   VOR_HOME=/path ./download-library.zsh   # change the library root
#
# It does NOT index the files. When it finishes, index them with:  vor ingest
# Nothing is installed, nothing is deleted, and files already downloaded are skipped.
set -e
HERE=${0:A:h}
PY=${PYTHON:-python3}
# Where vor.py reads from, by its own rule: a pre-rename ~/DamResearch that still holds the
# index wins over an empty ~/Vor (see vor_home in fetch_asdso.py).
HOME_DIR=${VOR_HOME:-$($PY "$HERE/fetch_asdso.py" --print-home)}

OUT=""
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    *) EXTRA+=("$1"); shift ;;
  esac
done

echo "== vor library download (no ingest) =="
echo "Library root: $HOME_DIR"

# 1) a plain-text index of every source and its URL (offline, always written)
URLS="${OUT:-$HOME_DIR/library}/library-urls.txt"
mkdir -p "${URLS:h}"
$PY "$HERE/fetch_asdso.py" --urls "$URLS"

# 2) download the curated catalog and the public ASDSO pages
CAT_OUT=()
[[ -n "$OUT" ]] && CAT_OUT=(--catalog-out "$OUT/references" --out "$OUT/asdso")
$PY "$HERE/fetch_asdso.py" --catalog --asdso $CAT_OUT $EXTRA

echo
echo "Done. Library in ${OUT:-$HOME_DIR/library}; source list in $URLS"
echo "Next, index what was downloaded:  vor ingest"
