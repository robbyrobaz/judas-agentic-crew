"""Archive span + filler stats per symbol/timeframe."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

for tf in ("5m", "15m", "1h"):
    for sym in ("MGC", "MNQ", "MCL", "ZF", "6J"):
        p = Path("data/bars_archive") / f"{sym}_{tf}.parquet"
        if not p.exists():
            print(f"{sym} {tf}: MISSING")
            continue
        df = pd.read_parquet(p)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values("ts").reset_index(drop=True)
        if "volume" in df.columns:
            filler = (df["volume"] <= 0) & (df["high"] <= df["low"])
            nfill = int(filler.sum())
        else:
            nfill = 0
        df = df.loc[~((df["volume"] <= 0) & (df["high"] <= df["low"]))].reset_index(drop=True) if "volume" in df.columns else df
        span = (df["ts"].iloc[-1] - df["ts"].iloc[0]).days
        # zero-range but volume>0 (single print) bars
        zr = int(((df["high"] - df["low"]) <= 0).sum()) if "volume" in df.columns else 0
        print(f"{sym:4s} {tf:3s} n={len(df):7d} span={span:4d}d  {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}  filler={nfill} zerorange={zr}")
