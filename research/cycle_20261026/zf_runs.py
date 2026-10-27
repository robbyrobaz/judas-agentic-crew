import sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from src import bar_archive
for sym,tf in (("ZF","15m"),("ZF","5m"),("6J","5m"),("MCL","5m")):
    df = bar_archive.read_archive(sym,tf); df=df.copy()
    df["ts"]=pd.to_datetime(df["ts"]); df=df.sort_values("ts").reset_index(drop=True)
    rng=df["high"]-df["low"]
    ok=(rng>0)&(df["volume"]>0)
    grp=(ok!=ok.shift()).cumsum()
    runs=[]
    for _,idx in df.groupby(grp).groups.items():
        runs.append((len(idx), bool(ok.loc[idx].iloc[0]), df.loc[idx,'ts'].iloc[0], df.loc[idx,'ts'].iloc[-1]))
    runs.sort(reverse=True)
    print(f"=== {sym} {tf}: top 6 contiguous runs (len, is_clean, start, end)")
    for r in runs[:6]:
        print("   ", r)
