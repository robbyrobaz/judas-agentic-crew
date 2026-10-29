"""Profile the 'dirty' (placeholder) bars in the deep archive.

WHY
---
`archive_readiness_20261029.json` says ZF 5m has 13,257 clean bars but ZERO
contiguous clean runs >= 300 bars.  A cell with 13k usable bars that cannot
produce a single 300-bar honest window is not a data-availability problem --
it is a *structure* problem, and ZF is one of only three broker-confirmed
PROFITABLE symbols (+$656.29).  Before we declare ZF "NOT_TESTABLE" we need
to know WHAT the dirty rows actually are:

  * zero-range (high == low) with zero volume  -> a真 placeholder row
  * zero-range but NON-zero volume             -> a real bar in a quiet tape
  * non-zero range but zero volume             -> a real bar, IBKR just didn't
                                                  report volume
  * NaN / missing OHLC
  * duplicate timestamps (a merge artefact)
  * a different bar SPACING (the row is not a 5m step)

The last two are the interesting ones: a duplicate-timestamp merge artefact is
fixable by de-duplication, and a spacing mismatch means the archive contains a
different timeframe than the filename claims.

Usage:
    .venv/bin/python research/cycle_20261029b/profile_dirty.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import bar_archive  # noqa: E402

CELLS = [("MGC", "5m"), ("ZF", "5m"), ("ZF", "15m"), ("MNQ", "5m"),
         ("MCL", "5m"), ("6J", "5m"), ("ZF", "1h")]

STEP_MIN = {"5m": 5, "15m": 15, "1h": 60}


def profile(symbol: str, tf: str) -> None:
    df = bar_archive.read_archive(symbol, tf)
    if df is None or df.empty:
        print(f"\n### {symbol} {tf}: MISSING")
        return
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)

    n = len(df)
    hi, lo = df["high"], df["low"]
    vol = df["volume"]
    rng = (hi - lo)

    nan_ohlc = df[["open", "high", "low", "close"]].isna().any(axis=1)
    zero_range = (rng <= 0)
    zero_vol = (vol <= 0)

    dups = df["ts"].duplicated().sum()
    diffs = df["ts"].diff().dt.total_seconds() / 60.0
    step = STEP_MIN[tf]
    off_step = diffs.notna() & (diffs != step)

    clean = (~zero_range) & (~zero_vol) & (~nan_ohlc)

    print(f"\n### {symbol} {tf}: raw={n}  span={df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
    print(f"  zero_range={int(zero_range.sum())}  zero_vol={int(zero_vol.sum())}  "
          f"nan_ohlc={int(nan_ohlc.sum())}  dup_ts={int(dups)}  off_step_rows={int(off_step.sum())}")
    # cross-tab: what makes a row dirty
    ct = pd.crosstab(zero_range.astype(int), zero_vol.astype(int))
    print("  crosstab zero_range x zero_vol:\n" +
          "".join(f"    range={i} vol={j}: {v}\n"
                  for i in ct.index for j in ct.columns
                  for v in [ct.loc[i, j]]))
    # zero-range but real volume -> a genuine quiet bar, NOT a placeholder
    zr_vol = int((zero_range & (vol > 0)).sum())
    print(f"  zero_range BUT volume>0 (real quiet bar, not placeholder): {zr_vol}")
    print(f"  zero_range AND volume==0 (true placeholder): {int((zero_range & zero_vol).sum())}")
    print(f"  range>0 BUT volume==0 (real bar, no volume reported): {int((~zero_range & zero_vol).sum())}")

    # the decisive question: does requiring volume>0 matter, or is it range alone?
    for name, mask in (("range>0 only", rng > 0),
                       ("volume>0 only", vol > 0),
                       ("range>0 & vol>0", clean)):
        runs = []
        start = None
        for i, v in enumerate(mask):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append(i - start)
                start = None
        if start is not None:
            runs.append(len(mask) - start)
        runs = [r for r in runs if r >= 300]
        top = sorted(runs, reverse=True)[:5]
        print(f"  [{name:<16}] clean={int(mask.sum()):>6}  runs>=300: {len(runs):>3}  longest5={top}")

    # gap structure of the whole archive
    gap = diffs.value_counts().head(6)
    print("  top ts gaps (minutes): " +
          ", ".join(f"{float(k):g}x{v}" for k, v in gap.items()))


def main() -> int:
    for s, tf in CELLS:
        profile(s, tf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
