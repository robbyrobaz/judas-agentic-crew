"""Placeholder / zero-range bar audit for the 15m archives.

The 5m archives had zero-range zero-volume placeholder rows (finding #7383
rationale).  The 15m archives still carry ~17% zero-range bars AFTER the
volume<=0 & high<=low filter -- find out what they are, because a zero-range
bar collapses the ATR and makes 'displacement' trivially false (or, worse,
trivially true if the filter is inverted).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

for sym in ["MGC", "MNQ", "MCL", "ZF", "6J"]:
    p = Path("data/bars_archive") / f"{sym}_15m.parquet"
    df = pd.read_parquet(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.sort_values("ts").reset_index(drop=True)
    zr = df["high"] <= df["low"]
    zv = df["volume"] <= 0
    print(f"== {sym} rows={len(df)} zero_range={int(zr.sum())} "
          f"zero_vol={int(zv.sum())} both={int((zr & zv).sum())} "
          f"zero_range_with_vol={int((zr & ~zv).sum())} "
          f"cols={list(df.columns)}")
    if (zr & ~zv).any():
        s = df.loc[zr & ~zv]
        print(f"   zero-range-with-volume example rows: {len(s)}, "
              f"volume stats {s['volume'].describe()[['min','50%','max']].to_dict()}")
        print("   sample:")
        print(s[[c for c in df.columns if c in ("ts", "open", "high", "low", "close", "volume")]].head(5).to_string(index=False))
        print("   time-of-day histogram (UTC hour):",
              dict(s["ts"].dt.hour.value_counts().sort_index()))
