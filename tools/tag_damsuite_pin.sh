#!/usr/bin/env bash
# Cut the annotated git tag that names the shared damsuite pin.
#
# The machine-checkable pin is tools/damsuite_pin.json (committed byte-identical into both forks;
# every repo's CI verifies its vendored copies against it). This tag is the human-facing, immutable
# reference for that same content. Run it in the DWR_Plugins repo, at the commit where
# shared/damsuite is in its pinned state (normally right AFTER the pin PR merges to main, so the tag
# points at a durable commit rather than a branch tip that may be squashed).
#
#   bash tools/tag_damsuite_pin.sh            # create the tag locally, print the push command
#   bash tools/tag_damsuite_pin.sh --push     # also push it to origin
#
# Pushing a tag is outward-facing and immutable; by default this script only creates it locally and
# prints the push command for you to run.
set -euo pipefail
cd "$(dirname "$0")/.."

PIN_FILE="tools/damsuite_pin.json"
[ -f "$PIN_FILE" ] || { echo "no $PIN_FILE (run: python3 tools/sync_damsuite.py --write-pin)"; exit 1; }

# the pin must match the master before we tag it
python3 tools/sync_damsuite.py --check

LABEL="$(python3 -c "import json;print(json.load(open('$PIN_FILE'))['pin'])")"
PINSHA="$(python3 -c "import json;print(json.load(open('$PIN_FILE'))['pin_sha256'])")"

if git rev-parse -q --verify "refs/tags/$LABEL" >/dev/null; then
  echo "tag $LABEL already exists at $(git rev-list -n1 "$LABEL"); not moving it."
  echo "(bump the pin label with: python3 tools/sync_damsuite.py --write-pin --pin damsuite-pin-vN)"
  exit 1
fi

git tag -a "$LABEL" -m "damsuite shared pin $LABEL (content $PINSHA)"
echo "created annotated tag $LABEL at $(git rev-parse --short HEAD)"

if [ "${1:-}" = "--push" ]; then
  git push origin "$LABEL"
  echo "pushed $LABEL"
else
  echo "to publish it:  git push origin $LABEL"
fi
