#!/bin/bash
# Build patched LISFLOOD-FP on Linux. Set LISFLOOD_SRC to the source tree.
set -e
SRC="${LISFLOOD_SRC:-$HOME/lisflood-fp}"
if [ ! -d "$SRC" ]; then
  echo "Set LISFLOOD_SRC to the LISFLOOD-FP source tree. Not found: $SRC"
  exit 1
fi
cd "$SRC"
mkdir -p build
cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . -j"$(nproc)"
echo "Built: $SRC/build/lisflood"
echo "Set that path in Suite status."
