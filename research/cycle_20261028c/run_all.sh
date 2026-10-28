#!/bin/bash
cd /home/rob/judas-agentic-crew
export OMP_NUM_THREADS=1
export SEG_WORKERS=6
for spec in "MGC 15m" "MGC 5m" "MNQ 15m" "MNQ 5m" "ZF 15m" "ZF 5m" "6J 15m" "6J 5m" "MCL 15m" "MCL 5m"; do
  set -- $spec
  echo "=== $1 $2 $(date -u +%H:%M:%S) ==="
  .venv/bin/python research/cycle_20261028c/screen_eqpool.py $1 $2 \
     > outputs/eqpool_log_${1}_${2}.txt 2>&1
  tail -5 outputs/eqpool_log_${1}_${2}.txt
done
echo "ALL DONE $(date -u +%H:%M:%S)"
