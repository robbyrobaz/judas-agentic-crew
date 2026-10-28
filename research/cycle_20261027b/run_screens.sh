#!/bin/bash
cd /home/rob/judas-agentic-crew
export OMP_NUM_THREADS=1
export SEG_WORKERS=5
LOG=research/cycle_20261027b/logs
run () {
  s=$1; tf=$2
  echo "=== $s $tf start $(date -u +%H:%M:%S) ===" >> $LOG/chain.log
  .venv/bin/python research/cycle_20261027b/honest_live_screen.py $s $tf > $LOG/hlive_${s}_${tf}.log 2>&1
  echo "=== $s $tf done rc=$? $(date -u +%H:%M:%S) ===" >> $LOG/chain.log
}
run MGC 15m & run MCL 15m & wait
run ZF 15m & run 6J 15m & wait
run MNQ 15m &
wait
echo ALL_DONE >> $LOG/chain.log
