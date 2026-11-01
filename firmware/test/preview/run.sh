#!/bin/bash
# Build the offline preview and render the example screens to PPM files.
# Run from firmware/:   bash test/preview/run.sh [scale] [lines-file ...]
# Each lines-file (one protocol message per line, e.g. `epaper_host.py --dry-run-v2` output) is also
# rendered, as out/<file name>.ppm. Build details (MSYS2 g++, PlatformIO libraries): see build.sh.
set -e
bash test/preview/build.sh
OUT=test/preview/out
mkdir -p "$OUT"
SCALE="${1:-1}"
shift || true
./test/preview/build/preview.exe "$OUT" "$SCALE" "$@"
