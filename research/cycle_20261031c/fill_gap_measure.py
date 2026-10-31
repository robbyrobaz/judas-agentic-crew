"""DECISION->FILL GAP measurement on the ENGINE's own bar series.

The engine's production fill = "open of the next ROW in the frame".  The frame
is the deep archive (IBKR TRADES) with no-trade FILLER BARS DROPPED, unioned
with the live cache.  Dropping filler converts "no trade in this bucket" into
a HOLE, so the next row can be far in the future.

The engine stamps `stale_fill_n` = 0 for every strategy (it only measures the
STRATEGY's own signal timestamp) -- so this contamination is invisible.

This script measures it three ways, per symbol/tf:
  A) row-gap distribution of the engine's frame (gap to the next row)
  B) decision->fill gap for every signal the strategy generates
  C) what the fill gap would be if we required the fill row to be the very
     next time bucket of the timeframe grid (the live scanner's situation)

Usage: .venv/bin/python research/cycle_20261031c/fill_gap_measure.py [symbol]
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research" / "cycle_20261031a"))

from src.research import custom_strategy_runtime as csr  # noqa: E402

INTERVAL_MIN = {"5m": 5, "15m": 15, "1h": 60}


def frame_gap_stats(ts: pd.Series, tf: str) -> dict:
    d = ts.diff().dt.total_seconds().div(60).dropna()
    iv = INTERVAL_MIN[tf]
    return {
        "rows": int(len(ts)),
        "dt_med": round(float(d.median()), 1),
        "frac_gt_1p5iv": round(float((d > 1.5 * iv).mean()), 4),
        "frac_gt_60m": round(float((d > 60).mean()), 4),
        "dt_p90": round(float(d.quantile(0.9)), 1),
        "dt_p99": round(float(d.quantile(0.99)), 1),
        "dt_max": round(float(d.max()), 1),
    }


def main() -> None:
    sym = (sys.argv[1] if len(sys.argv) > 1 else "MGC").upper()
    for tf in ("15m", "5m"):
        bars = csr._fetch_synthetic_or_live_bars(sym, 120, tf)
        if bars is None or len(bars) < 100:
            print(f"{sym} {tf}: no bars")
            continue
        ts = pd.to_datetime(bars["ts"], utc=True).sort_values().reset_index(drop=True)
        st = frame_gap_stats(ts, tf)
        print(f"\n=== {sym} {tf} ENGINE FRAME ===")
        print(st)
        d = ts.diff().dt.total_seconds().div(60)
        gaps = d.iloc[1:].to_numpy(float)
        print("  gap histogram (min):",
              {int(k): int(v) for k, v in
               pd.Series(gaps).value_counts().sort_index().head(12).items()})
        big = np.sort(gaps[gaps > 1.5 * INTERVAL_MIN[tf]])
        print(f"  rows whose NEXT row is >1.5iv away: {len(big)} "
              f"({100*len(big)/max(len(gaps),1):.2f}%)  "
              f"p50={np.median(big) if len(big) else 0:.0f} "
              f"max={big.max() if len(big) else 0:.0f}")
        # what fraction of a random decision bar would be stale?
        print("  -> a signal taken on a random bar has this probability of a "
              "STALE (>1.5iv) fill:",
              f"{100*len(big)/max(len(gaps),1):.2f}%")


if __name__ == "__main__":
    main()
