#!/bin/bash
cd /home/rob/judas-agentic-crew
export OMP_NUM_THREADS=1
export SEG_WORKERS=3
nohup bash -c 'for spec in "MNQ 15m" "ZF 15m" "MGC 15m" "6J 15m"; do set -- $spec; echo "=== $1 $2 start $(date -u +%H:%M:%S) ==="; .venv/bin/python research/cycle_20261027/seg_sweep.py "$1" "$2" > research/cycle_20261027/logs/seg_"$1"_"$2".log 2>&1; echo "   done $1 $2 rc=$? $(date -u +%H:%M:%S)"; done; echo CHAIN_A_DONE' > research/cycle_20261027/logs/chainA.log 2>&1 &
nohup bash -c 'for spec in "MGC 5m" "MNQ 5m" "MCL 5m" "ZF 5m" "6J 5m"; do set -- $spec; echo "=== $1 $2 start $(date -u +%H:%M:%S) ==="; .venv/bin/python research/cycle_20261027/seg_sweep.py "$1" "$2" > research/cycle_20261027/logs/seg_"$1"_"$2".log 2>&1; echo "   done $1 $2 rc=$? $(date -u +%H:%M:%S)"; done; echo CHAIN_B_DONE' > research/cycle_20261027/logs/chainB.log 2>&1 &
sleep 2; echo LAUNCHED; cat research/cycle_20261027/logs/chainA.log research/cycle_20261027/logs/chainB.log