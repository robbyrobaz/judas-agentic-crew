#!/bin/bash
cd /home/rob/judas-agentic-crew
export OMP_NUM_THREADS=1
export SEG_WORKERS=2
for spec in "MGC 15m" "MNQ 15m" "MCL 15m" "ZF 15m" "6J 15m" "MGC 5m" "MNQ 5m" "MCL 5m" "ZF 5m" "6J 5m"; do
  set -- $spec
  echo "=== $1 $2 start $(date -u +%H:%M:%S) ==="
  .venv/bin/python research/cycle_20261027/golden_screen.py "$1" "$2" > research/cycle_20261027/logs/golden_"$1"_"$2".log 2>&1
  echo "   done $1 $2 rc=$? $(date -u +%H:%M:%S)"
done
echo GOLDEN_CHAIN_DONE
