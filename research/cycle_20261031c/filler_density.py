"""FILLER DENSITY over TIME (cycle 20261031c).

IBKR's historical TRADES feed emits a flat bar (OHLC == last price, volume 0)
for buckets it has NO price data for.  The engine DROPS those bars
(`_drop_filler_bars`) so ATR is not deflated -- but dropping them turns
"no data" into a HOLE in the bar series.

Consequence for 5m/15m research: in the OLD archive region the series is a
SPARSE SAMPLE of the true tape (MNQ 15m keeps only ~30 of 96 buckets/day),
so "the sweep within the last 6 bars" spans hours, not 15 minutes.  A 15m
strategy evaluated on that is not a 15m strategy.

This probe measures, per symbol/tf/month: bars/day, filler %, and the share of
the series that is DENSE (>= 70% of the timeframe's slots present).  It tells
us which window is honest to research on.

Usage: .venv/bin/python research/cycle_20261031c/filler_density.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

LEGAL = ["MGC", "MNQ", "MCL", "ZF", "6J"]
TFS = ["5m", "15m", "1h"]
SLOTS = {"5m": 288, "15m": 96, "1h": 24}


def main() -> None:
    rows = []
    for sym in LEGAL:
        for tf in TFS:
            df = bar_archive.read_archive(sym, tf)
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            rng = df["high"] - df["low"]
            df["filler"] = (df["volume"] <= 0) & (rng <= 0)
            df["month"] = df["ts"].dt.to_period("M").astype(str)
            for m, g in df.groupby("month"):
                days = max((g["ts"].dt.date.nunique()), 1)
                rows.append({
                    "sym": sym, "tf": tf, "month": m,
                    "rows": len(g),
                    "days": days,
                    "slots_per_day": round(len(g) / days, 1),
                    "fill_pct": round(100 * g["filler"].mean(), 1),
                    "real_bars_per_day": round((~g["filler"]).sum() / days, 1),
                    "coverage_pct": round(100 * len(g) / (days * SLOTS[tf]), 1),
                })
    out = pd.DataFrame(rows)
    out.to_csv("research/cycle_20261031c/filler_density.csv", index=False)
    for tf in TFS:
        sub = out[out.tf == tf]
        print(f"\n########## {tf} ##########")
        piv = sub.pivot_table(index="month", columns="sym", values="real_bars_per_day")
        print("REAL (non-filler) bars/day   [slots/day: 5m=288 15m=96 1h=24]")
        print(piv.to_string())
        piv2 = sub.pivot_table(index="month", columns="sym", values="fill_pct")
        print("\nFILLER %")
        print(piv2.to_string())


if __name__ == "__main__":
    main()
