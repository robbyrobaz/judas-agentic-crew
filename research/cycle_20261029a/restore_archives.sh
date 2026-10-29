#!/usr/bin/env bash
# 2026-10-29a — task #2892 archive restoration, driven from the READINESS AUDIT.
#
# The readiness audit (research/cycle_20261029a/archive_readiness.py) shows the
# real remaining gaps, which are NOT the ones the task payload assumed:
#
#   MCL_1h  MISSING ENTIRELY (no parquet)            -> build from scratch
#   ZF_5m   12470 clean bars but ZERO contiguous runs >= 300  -> NOT_TESTABLE
#   ZF_15m  longest contiguous run = 735 bars          -> MARGINAL
#   MCL_15m longest contiguous run = 2519 bars         -> TESTABLE_THIN
#   MNQ_5m  longest contiguous run = 8196 bars / 41d   -> extend
#   6J_5m   longest contiguous run = 1341 bars / 6d    -> extend
#   MCL_5m  longest contiguous run = 2479 bars / 10d   -> extend
#
# ZF is the priority: it is one of only three broker-confirmed PROFITABLE
# symbols (+$656.29, 25W/43L) and its 5m cell currently cannot be screened at
# all because the placeholder bars chop the series into sub-300-bar shards.
#
# Technique is the one proven in finding c203259d / 9ee402c1: anchored
# endDateTime BACKWARD PAGINATION with chunk-sized requests, merged
# ADDITIVELY through bar_archive.merge_into_archive (archive writes only
# through the standard bar-cache path).  An oversized single request dies on
# IBKR's ~60s timer and returns an EMPTY frame that reads as "no history".
#
# Read-only on the registry; the live scanner (src/bar_cache) is untouched.
set -u
cd /home/rob/judas-agentic-crew

run_cell () {
  sym=$1; tf=$2; days=$3
  echo "=== $sym $tf target=${days}d start $(date -u +%H:%M:%S) ==="
  timeout 3000 .venv/bin/python research/cycle_20261026/extend_archive.py "$sym" "$tf" "$days" \
      > "outputs/extend_${sym}_${tf}_20261029a.log" 2>&1
  echo "exit=$? $(tail -c 500 outputs/extend_${sym}_${tf}_20261029a.log | tr '\n' ' ')"
  sleep 8
}

# 1. the missing cell
run_cell MCL 1h 340
# 2. ZF — the profitable-but-unscreenable symbol
run_cell ZF 5m 340
run_cell ZF 15m 340
# 3. MCL 15m / 1h
run_cell MCL 15m 340
# 4. thin cells that are cheap to extend
run_cell MNQ 5m 340
run_cell MCL 5m 340
run_cell 6J 5m 340
run_cell 6J 15m 340
echo "ALL DONE $(date -u +%H:%M:%S)"
