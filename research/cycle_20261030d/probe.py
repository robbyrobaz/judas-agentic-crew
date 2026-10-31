import sys, os
sys.path.insert(0, os.path.abspath("."))
from src.research import custom_strategy_runtime as csr
for sym in ("MGC","MNQ","ZF","MCL","6J"):
    for tf in ("5m","15m"):
        try:
            df = csr._fetch_synthetic_or_live_bars(sym, 120, tf)
        except Exception as e:
            print(sym, tf, "ERR", type(e).__name__, e); continue
        if df is None or len(df)==0:
            print(sym, tf, "NO BARS"); continue
        print(sym, tf, "n=",len(df), "cols=", list(df.columns),
              "first=", df["ts"].iloc[0], "last=", df["ts"].iloc[-1],
              "span_d=", round((df["ts"].iloc[-1]-df["ts"].iloc[0]).total_seconds()/86400,1))
