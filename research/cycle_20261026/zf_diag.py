import sys
from pathlib import Path
import pandas as pd
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT))
from src import bar_archive
for sym in ("ZF","6J","MCL"):
    for tf in ("15m","5m"):
        df = bar_archive.read_archive(sym, tf)
        df = df.copy(); df["ts"]=pd.to_datetime(df["ts"]); df=df.sort_values("ts").reset_index(drop=True)
        rng = df["high"]-df["low"]
        print(f"--- {sym} {tf} bars={len(df)}")
        print("   volume: zeros=%d nan=%d min=%s max=%s" % (
            int((df["volume"]==0).sum()), int(df["volume"].isna().sum()),
            df["volume"].min(), df["volume"].max()))
        print("   range: zeros=%d nan=%d  p50=%.6f p99=%.6f" % (
            int((rng<=0).sum()), int(rng.isna().sum()), rng.quantile(.5), rng.quantile(.99)))
        ok = (rng>0)
        grp=(ok!=ok.shift()).cumsum()
        best=None
        for _,idx in df.groupby(grp).groups.items():
            if len(idx)<1200: continue
            if best is None or len(idx)>len(best): best=idx
        if best is not None:
            print("   RANGE-ONLY longest clean run: %d bars (%s -> %s)" % (
                len(best), df.loc[best,'ts'].iloc[0], df.loc[best,'ts'].iloc[-1]))
        else:
            print("   RANGE-ONLY: no run >= 1200")
