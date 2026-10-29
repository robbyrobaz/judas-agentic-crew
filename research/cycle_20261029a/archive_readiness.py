"""ARCHIVE READINESS AUDIT — 2026-10-29 cycle (task #2892).

WHY THIS FILE
-------------
Task #2892: "candidate inflow is dry for a MECHANICAL reason" — the bar
archive could not be backtested for ZF / 6J / MGC-1h / MCL-15m / MCL-1h.
Prior cycles (#2861, #2869, cycle_20261028e) have since restored most of
those cells.  This script produces the SINGLE AUTHORITATIVE readiness table
so the team knows exactly which symbol-timeframe cells are testable, with
what depth, and how trustworthy that depth is.

WHAT IS REPORTED PER CELL
--------------------------
  raw_bars      - rows in data/bars_archive/<SYM>_<tf>.parquet
  first/last    - timestamp of the first / last raw bar
  clean_bars    - rows with (high-low) > 0 AND volume > 0  ("real" bars)
  dirty_pct     - share of raw rows that are placeholder (zero range or zero
                  volume) -- these are IBKR filler rows, not trades
  segments      - contiguous runs of clean bars, >= MIN_SEG bars
  longest_seg   - (bars, first_ts, last_ts) of the longest clean run
  longest_days  - calendar days the longest run spans
  regime        - does the longest run contain a VOLATILITY REGIME CHANGE?
                  measured as p95(ATR14)/p05(ATR14) inside the run; a ratio
                  >= REGIME_RATIO means the window contains both a calm and a
                  turbulent regime, so a backtest on it mixes market states.

WHY TWO CLEAN MODES ARE REPORTED
--------------------------------
`contiguous`  : runs split at every dirty bar (seg_sweep.clean_segments).
                Strictest -- a trade can never jump a time gap.
`dropped`     : dirty rows dropped from the maths, remaining rows kept in
                order (the "drop placeholder rows INSIDE the maths" reading).
                More bars, but a fill can step over a placeholder gap.

Both are reported so the team can choose; `contiguous` is the one the honest
screen uses and the one that gates "is this cell testable".

Usage:
    .venv/bin/python research/cycle_20261029a/archive_readiness.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

SYMBOLS = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]
MIN_SEG = 300          # same threshold the honest screens use
REGIME_RATIO = 3.0     # p95/p05 ATR inside the longest run
OUT = ROOT / "outputs" / "archive_readiness_20261029.json"


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                     (df["high"] - prev).abs(),
                     (df["low"] - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def contiguous_runs(ok: pd.Series) -> list[tuple[int, int]]:
    """(start_idx, end_idx) inclusive runs where ok is True."""
    runs, start = [], None
    for i, v in enumerate(ok):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(ok) - 1))
    return runs


def audit_cell(symbol: str, tf: str) -> dict:
    df = bar_archive.read_archive(symbol, tf)
    res: dict = {"symbol": symbol, "tf": tf}
    if df is None or df.empty:
        res.update({"available": False, "raw_bars": 0})
        return res

    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    ok = ((df["high"] - df["low"]) > 0) & (df["volume"] > 0)
    n_raw = int(len(df))
    n_clean = int(ok.sum())

    runs = contiguous_runs(ok)
    segs = [(a, b) for a, b in runs if (b - a + 1) >= MIN_SEG]

    # ---- longest contiguous clean run ----
    if segs:
        a, b = max(segs, key=lambda r: r[1] - r[0])
        seg = df.iloc[a:b + 1].reset_index(drop=True)
        atr = _atr(seg).dropna()
        if len(atr) >= 50:
            p95 = float(np.percentile(atr, 95))
            p05 = float(np.percentile(atr, 5))
            ratio = (p95 / p05) if p05 > 0 else float("inf")
        else:
            ratio = float("nan")
        longest = {
            "bars": int(b - a + 1),
            "first": str(seg["ts"].iloc[0]),
            "last": str(seg["ts"].iloc[-1]),
            "days": int((seg["ts"].iloc[-1] - seg["ts"].iloc[0]).days),
            "atr_p95_p05_ratio": None if ratio != ratio else round(float(ratio), 2),
            "regime_change": bool(ratio == ratio and ratio >= REGIME_RATIO),
        }
    else:
        longest = {"bars": 0, "first": None, "last": None, "days": 0,
                   "atr_p95_p05_ratio": None, "regime_change": None}

    # ---- 'dropped' mode: dirty rows removed, remainder kept in order ----
    dropped = df.loc[ok].reset_index(drop=True)
    dropped_bars = int(len(dropped))

    # ---- gaps inside the raw archive (missing sessions) ----
    span_days = int((df["ts"].iloc[-1] - df["ts"].iloc[0]).days)
    # expected bars/day for a 24h instrument: 288 / 96 / 24
    per_day = {"5m": 288, "15m": 96, "1h": 24}[tf]
    coverage = (n_clean / (per_day * max(span_days, 1))) if span_days else 0.0

    res.update({
        "available": True,
        "raw_bars": n_raw,
        "clean_bars": n_clean,
        "dirty_pct": round(100.0 * (1 - n_clean / max(n_raw, 1)), 1),
        "first": str(df["ts"].iloc[0]),
        "last": str(df["ts"].iloc[-1]),
        "span_days": span_days,
        "dropped_mode_bars": dropped_bars,
        "n_segments_ge_300": len(segs),
        "segment_list": [[str(df["ts"].iloc[a]), str(df["ts"].iloc[b]), int(b - a + 1)]
                         for a, b in sorted(segs, key=lambda r: -(r[1] - r[0]))],
        "longest_seg": longest,
        "clean_coverage_pct_of_span": round(100.0 * coverage, 1),
    })

    # ---- testability verdict ----
    if longest["bars"] >= 5000:
        verdict = "TESTABLE"
    elif longest["bars"] >= 1000:
        verdict = "TESTABLE_THIN"
    elif longest["bars"] >= MIN_SEG:
        verdict = "MARGINAL"
    else:
        verdict = "NOT_TESTABLE"
    res["verdict"] = verdict
    return res


def main() -> int:
    rows = []
    for sym in SYMBOLS:
        for tf in TFS:
            rows.append(audit_cell(sym, tf))

    hdr = (f"{'CELL':<9} {'raw':>7} {'clean':>7} {'dirty%':>7} {'segs':>5} "
           f"{'longest':>8} {'days':>5} {'cover%':>7} {'regime':>7}  verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if not r.get("available"):
            print(f"{r['symbol']+'_'+r['tf']:<9} {'MISSING':>7}")
            continue
        lg = r["longest_seg"]
        print(f"{r['symbol']+'_'+r['tf']:<9} {r['raw_bars']:>7} {r['clean_bars']:>7} "
              f"{r['dirty_pct']:>7} {r['n_segments_ge_300']:>5} {lg['bars']:>8} "
              f"{lg['days']:>5} {r['clean_coverage_pct_of_span']:>7} "
              f"{str(lg['regime_change']):>7}  {r['verdict']}")

    OUT.write_text(json.dumps({"generated": "2026-10-29", "min_seg": MIN_SEG,
                               "regime_ratio": REGIME_RATIO, "cells": rows},
                              indent=2, default=str))
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
