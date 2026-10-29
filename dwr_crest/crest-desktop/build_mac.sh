#!/bin/sh
# Run this on a Mac, from this folder, with Python 3.10–3.12 (3.12 preferred; PyInstaller fails on 3.13).
# The web desk bundle (crest/web/) is committed; rebuild it first with `npm run build:desktop` if you changed src/.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-$(command -v python3.12 || command -v python3)}"
"$PY" -c 'import sys; v=sys.version_info[:2]; sys.exit(0 if (3,10) <= v <= (3,12) else "Need Python 3.10-3.12, found %d.%d" % v)'
"$PY" -m pip install -r requirements.txt
"$PY" -m PyInstaller crest.spec
echo "Built dist/Crest.app (unsigned: right-click > Open on first launch)"
