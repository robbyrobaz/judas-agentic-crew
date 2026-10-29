"""Probe WHY clean segments break: inspect placeholder structure in the archive."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import bar_archive  # noqa: E402

for sym, tf in [("MGC", "5m"), ("ZF", "5m"), ("MGC", "1h")]:
    df = bar_archive.read_archive(sym, tf)
    print(f"\n{'='*70}\n{sym} {tf}  rows={len(df)}  {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")
    hi, lo, vol = df["high"].to_numpy(float), df["low"].to_numpy(float), df["volume"].to_numpy(float)
    zero_range = hi <= lo
    zero_vol = vol <= 0
    print(f"  zero_range={zero_range.sum()}  zero_vol={zero_vol.sum()}  both={((zero_range)&(zero_vol)).sum()}")
    print(f"  volume describe: {pd.Series(vol).describe().to_dict()}")
    # where does clean break? find first 12 break points with context
    step = pd.Timedelta(minutes={"5m": 5, "15m": 15, "1h": 60}[tf])
    ts = df["ts"].reset_index(drop=True)
    d = ts.diff().dt.total_seconds().div(60).to_numpy()
    gap = d > (step.total_seconds() * 1.5)
    notclean = (zero_range | zero_vol)
    prev_bad = np.concatenate([[False], notclean[:-1]])
    breaks = gap | prev_bad
    idx = np.flatnonzero(breaks)
    print(f"  n_breakpoints={len(idx)}")
    for i in idx[:8]:
        a = max(0, i - 2)
        b = min(len(df), i + 3)
        print(f"  --- break at idx {i} ---")
        print(df.iloc[a:b].to_string())
    # sample of a known-bad region
    if zero_vol.sum() > 50:
        badidx = np.flatnonzero(zero_vol)
        print(f"  first zero_vol idx {badidx[0]} last {badidx[-1]}  ts {ts.iloc[badidx[0]]} .. {ts.iloc[badidx[-1]]}")
