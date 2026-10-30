#!/bin/sh
# Honest-fill grids for the 2026-10-29e cycle (task #2893).
set -e
cd "$(dirname "$0")/../.."
for s in a b c; do
  echo "=== stage $s $(date -u +%FT%TZ) ==="
  .venv/bin/python research/cycle_20261029e/grid3.py \
      research/cycle_20261029e/spec_$s.json \
      research/cycle_20261029e/out_$s.json
done
echo "=== done $(date -u +%FT%TZ) ==="
