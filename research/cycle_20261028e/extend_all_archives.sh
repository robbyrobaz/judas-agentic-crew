#!/usr/bin/env bash
# 2026-10-28e cycle: extend the deep bar archive BACKWARDS for every legal
# symbol at 5m and 15m, so the honest screens stop being MGC-only.
#
# WHY: the archive audit this cycle showed that MGC 5m has 18444 CLEAN bars in
# one contiguous segment (337 days of history on the pinned 202612 contract)
# while MNQ 5m has ONE 8196-bar segment, MCL 5m only 3439, ZF 5m has NO
# segment >= 300 bars at all and ZF 15m only 2115.  Every honest screen the
# team has run since 2026-10-25 therefore reports "0 survivors" for 6J / MCL /
# ZF -- not because the edge is absent there, but because there is not enough
# CONTIGUOUS history to screen.  MGC 5m was extended the same way on
# 2026-10-27 (research/cycle_20261026/extend_archive.py) and that is the only
# symbol that ever produced an IS/OOS survivor.
#
# Additive and research-only: bar_archive.merge_into_archive APPENDS to
# data/bars_archive/*.parquet.  The live scanner reads src/bar_cache, which is
# untouched.
set -u
cd /home/rob/judas-agentic-crew

for spec in "MNQ 5m" "MNQ 15m" "MCL 5m" "MCL 15m" "ZF 5m" "ZF 15m" "6J 5m" "6J 15m"; do
  set -- $spec
  sym=$1; tf=$2
  echo "=== $sym $tf ==="
  timeout 1800 .venv/bin/python research/cycle_20261026/extend_archive.py "$sym" "$tf" 340 \
      > "outputs/extend_${sym}_${tf}_20261028e.log" 2>&1
  echo "exit=$? $(tail -c 400 outputs/extend_${sym}_${tf}_20261028e.log | tr '\n' ' ')"
  sleep 5
done
echo "ALL DONE"
