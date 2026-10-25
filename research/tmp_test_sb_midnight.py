"""Local logic test for research/sb_midnight_bias_10am_ny_v1.py (synthetic bars)."""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from src.research import custom_strategy_runtime as csr  # noqa: E402

CODE = open("research/sb_midnight_bias_10am_ny_v1.py").read()


def build_bars(scenario):
    df = pd.DataFrame()
    start = pd.Timestamp("2026-10-20 12:00:00", tz="UTC")
    n = 240
    ts = [start + pd.Timedelta(minutes=5 * i) for i in range(n)]
    px = 100.0
    rows = []
    for t in ts:
        rows.append({"ts": t, "open": px, "high": px + 0.1, "low": px - 0.1,
                     "close": px, "volume": 100})
        px += 0.001
    df = pd.DataFrame(rows)
    idx = {t: i for i, t in enumerate(ts)}

    def bar(t):
        return int(idx[pd.Timestamp(t, tz="UTC")])

    b0 = bar("2026-10-20 13:00")
    if scenario.startswith("short"):
        for i in range(b0, b0 + 12):
            df.loc[i, ["open", "high", "low", "close"]] = [100.0, 100.0, 99.9, 99.95]
        s = bar("2026-10-20 14:00")          # sweep + displacement bar
        df.loc[s, ["open", "high", "low", "close"]] = [100.0, 100.4, 99.85, 99.95]
        t1 = bar("2026-10-20 14:05")          # third candle -> gap (99.85, 99.9)
        df.loc[t1, ["open", "high", "low", "close"]] = [99.95, 99.85, 99.70, 99.72]
        e = bar("2026-10-20 14:10")           # retrace into the gap
        df.loc[e, ["open", "high", "low", "close"]] = [99.72, 99.88, 99.70, 99.86]
        df.loc[e + 1, ["open", "high", "low", "close"]] = [99.86, 99.87, 99.80, 99.82]
        if scenario == "short_nogap":
            df.loc[t1, ["open", "high", "low", "close"]] = [99.95, 99.95, 99.70, 99.72]
    else:
        for i in range(b0, b0 + 12):
            df.loc[i, ["open", "high", "low", "close"]] = [100.0, 100.1, 100.0, 100.05]
        s = bar("2026-10-20 14:00")
        df.loc[s, ["open", "high", "low", "close"]] = [100.0, 100.05, 99.90, 100.02]
        d1 = bar("2026-10-20 14:05")
        df.loc[d1, ["open", "high", "low", "close"]] = [100.02, 100.30, 100.06, 100.28]
        d2 = bar("2026-10-20 14:10")
        df.loc[d2, ["open", "high", "low", "close"]] = [100.28, 100.32, 100.10, 100.12]
        e = bar("2026-10-20 14:15")
        df.loc[e, ["open", "high", "low", "close"]] = [100.12, 100.14, 100.06, 100.08]
        df.loc[e + 1, ["open", "high", "low", "close"]] = [100.08, 100.10, 100.06, 100.09]
    return df


for scen in ("short", "long", "short_nogap"):
    df = build_bars(scen)
    print("=" * 60)
    print("scenario", scen)
    for cut in range(len(df)):
        sig = csr.evaluate_custom_strategy(code=CODE, bars=df.iloc[: cut + 1], params={}, timeout_s=10)
        if sig:
            t = df["ts"].iloc[cut]
            print("  signal at", t, "NY", t.tz_convert("America/New_York"))
            print("   ", {k: v for k, v in sig.items() if k != "ts"})
            break
    else:
        print("  NO SIGNAL")

# bias-filter check: same short setup but with the day open BELOW price (day rising)
df = build_bars("short")
df.loc[0, "open"] = 99.0          # midnight NY open far below -> day is rising
found = None
for cut in range(len(df)):
    s = csr.evaluate_custom_strategy(code=CODE, bars=df.iloc[: cut + 1], params={}, timeout_s=10)
    if s:
        found = (cut, s)
        break
print("=" * 60)
print("bias-off test (day open below price) ->", found)
