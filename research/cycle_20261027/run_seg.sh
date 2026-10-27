#!/bin/bash
# Cross-symbol honest market-entry screen (segment harness, LIVE fill).
cd /home/rob/judas-agentic-crew
export OMP_NUM_THREADS=1
export SEG_WORKERS=5
for spec in "$@"; do
  set -- $spec
  echo "=== $1 $2 start $(date -u +%H:%M:%S) ==="
  .venv/bin/python research/cycle_20261027/seg_sweep.py "$1" "$2" \
      > research/cycle_20261027/logs/seg_"$1"_"$2".log 2>&1
  echo "   done $1 $2 rc=$? $(date -u +%H:%M:%S)"
done
echo ALL_DONE
