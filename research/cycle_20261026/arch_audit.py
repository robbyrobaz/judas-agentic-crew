"""Archive clean-run audit per symbol/timeframe (v2, 2026-10-26)."""
import sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src import bar_archive

MIN_RUN = 1200
for sym in ("MGC","MNQ","MCL","ZF","6J"):
    for tf in ("5m","15m","1h"):
        df = bar_archive.read_archive(sym, tf)
        if df is None or len(df) == 0:
            print(f"{sym:4s} {tf:3s} NO ARCHIVE"); continue
        df = df.copy(); df["ts"] = pd.to_datetime(df["ts"])
        df = df.sort_values("ts").reset_index(drop=True)
        clean = ((df["high"]-df["low"]) > 0) & (df["volume"] > 0)
        grp = (clean != clean.shift()).cumsum()
        best = None
        for _, idx in df.groupby(grp).groups.items():
            if len(idx) < MIN_RUN: continue
            seg = df.loc[idx]
            if best is None or len(seg) > len(best): best = seg
        span = f"{df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}"
        if best is None:
            print(f"{sym:4s} {tf:3s} bars={len(df):6d} span={span} CLEAN_RUN=NONE")
        else:
            print(f"{sym:4s} {tf:3s} bars={len(df):6d} span={span} clean={len(best):6d} "
                  f"({best['ts'].iloc[0]} -> {best['ts'].iloc[-1]})")
