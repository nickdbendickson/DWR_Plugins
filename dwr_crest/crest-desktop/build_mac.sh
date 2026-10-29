#!/bin/sh
# Run this on a Mac, from this folder, after Python 3 is available.
set -e
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
python3 -m PyInstaller crest.spec
echo "Built dist/Crest.app"
