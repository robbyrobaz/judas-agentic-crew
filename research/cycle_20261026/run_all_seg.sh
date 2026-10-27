#!/bin/bash
cd /home/rob/judas-agentic-crew
for spec in "MGC 5m" "MNQ 15m" "MNQ 5m" "MCL 15m" "MCL 5m" "6J 15m" "6J 5m" "ZF 15m" "ZF 5m"; do
  set -- $spec
  echo "=== $1 $2 ==="
  .venv/bin/python research/cycle_20261026/seg_sweep.py $1 $2 > research/cycle_20261026/logs/seg_$1_$2.log 2>&1
  echo "   done $1 $2 rc=$?"
done
echo ALL_DONE
