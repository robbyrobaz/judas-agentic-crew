"""How dense are Silver-Bullet FVGs in the NY windows, and how many survive
the full rule stack (sweep of the prior-hour level -> FVG -> edge entry -> bias)?

Counts per NY day, for each candidate window, on MGC native bars.
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from src.research import custom_strategy_runtime as csr  # noqa: E402


def ny_fields(ts):
    s = pd.to_datetime(ts, utc=True)
    ny = s.dt.tz_convert("America/New_York")
    return ny.dt.hour.to_numpy(), ny.dt.date.to_numpy()


def report(symbol="MGC", tf="5m", days=45):
    bars = csr._fetch_synthetic_or_live_bars(symbol, days, tf)
    ts = bars["ts"]
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    hh, dd = ny_fields(ts)
    n = len(bars)
    print(f"=== {symbol} {tf} bars={n} {ts.iloc[0]} .. {ts.iloc[-1]}")
    for win_start, win_end in ((3, 4), (10, 11), (14, 15), (10, 12), (9, 12), (3, 16)):
        stats = {"days": 0, "fvg_raw": 0, "fvg_with_sweep": 0, "entry": 0, "entry_bias": 0}
        for d in set(dd):
            mask = dd == d
            idx_today = np.where(mask)[0]
            if len(idx_today) == 0:
                continue
            idx_prior = np.where(mask & (hh == win_start - 1))[0]
            if len(idx_prior) == 0:
                continue
            stats["days"] += 1
            ph = float(np.max(h[idx_prior])); pl = float(np.min(l[idx_prior]))
            day_open = float(o[idx_today[0]])
            idx_win = np.where(mask & (hh >= win_start) & (hh < win_end))[0]
            if len(idx_win) == 0:
                continue
            win_set = set(int(x) for x in idx_win)
            swept = set()
            for j in idx_win:
                if h[j] > ph and c[j] < ph:
                    swept.add(("short", int(j)))
                if l[j] < pl and c[j] > pl:
                    swept.add(("long", int(j)))
            for i in idx_win:
                i = int(i)
                if i - 2 < 0:
                    continue
                bear = l[i - 2] > h[i]
                bull = h[i - 2] < l[i]
                if not (bear or bull):
                    continue
                stats["fvg_raw"] += 1
                direction = "short" if bear else "long"
                # was there a sweep of the prior-hour level in this window before it?
                j0 = None
                for (ddir, jj) in swept:
                    if jj < i and ddir == direction:
                        j0 = jj if j0 is None else min(j0, jj)
                if j0 is None:
                    continue
                stats["fvg_with_sweep"] += 1
                if direction == "short":
                    zl, zh = float(h[i]), float(l[i - 2])
                    if h[i] <= zl or c[i] > zh:
                        pass
                else:
                    zl, zh = float(h[i - 2]), float(l[i])
                stats["entry"] += 1
                if direction == "short" and c[i] < day_open:
                    stats["entry_bias"] += 1
                if direction == "long" and c[i] > day_open:
                    stats["entry_bias"] += 1
        print(f"  win {win_start:02d}-{win_end:02d} NY: {stats}")


if __name__ == "__main__":
    report(sys.argv[1] if len(sys.argv) > 1 else "MGC",
           sys.argv[2] if len(sys.argv) > 2 else "5m",
           int(sys.argv[3]) if len(sys.argv) > 3 else 45)
