"""Funnel instrumentation for the Silver-Bullet + midnight-bias strategy.

Counts, per NY day inside the 10:00-11:00 window:
  window bars -> sweeps -> FVG found -> entry reached -> bias allowed.
Runs on the SAME bars the backtest tool uses (csr._fetch_synthetic_or_live_bars).
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from src.research import custom_strategy_runtime as csr  # noqa: E402


def ny_fields(ts):
    s = pd.to_datetime(ts, utc=True)
    ny = s.dt.tz_convert("America/New_York")
    return ny.dt.hour.to_numpy(), ny.dt.minute.to_numpy(), ny.dt.date.to_numpy()


def funnel(symbol="MGC", timeframe="5m", days=45, win_start=10, win_end=11,
           use_bias=True, verbose_days=False):
    bars = csr._fetch_synthetic_or_live_bars(symbol, days, timeframe)
    if bars is None or len(bars) == 0:
        print("NO BARS")
        return
    ts = bars["ts"]
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    hh, mm, dd = ny_fields(ts)
    n = len(bars)
    print(f"--- {symbol} {timeframe} bars={n} span {ts.iloc[0]} .. {ts.iloc[-1]}")

    days_seen = {}
    for cur_i in range(n):
        hour = int(hh[cur_i])
        if not (win_start <= hour < win_end):
            continue
        today = dd[cur_i]
        rec = days_seen.setdefault(today, {"win_bars": 0, "sweeps": 0, "fvg": 0,
                                           "entry": 0, "bias_ok": 0})
        rec["win_bars"] += 1
        mask = dd == today
        idx_today = np.where(mask)[0]
        idx_prior = np.where(mask & (hh == win_start - 1))[0]
        if len(idx_prior) == 0:
            continue
        prior_high = float(np.max(h[idx_prior]))
        prior_low = float(np.min(l[idx_prior]))
        idx_win = np.where(mask & (hh >= win_start) & (hh < win_end))[0]
        day_open = float(o[idx_today[0]])
        for k in range(len(idx_win)):
            j = int(idx_win[k])
            if j >= cur_i:
                break
            direction = None
            extreme = None
            if h[j] > prior_high and c[j] < prior_high:
                direction = "short"
                extreme = float(h[j])
            elif l[j] < prior_low and c[j] > prior_low:
                direction = "long"
                extreme = float(l[j])
            if direction is None:
                continue
            rec["sweeps"] += 1
            for k2 in range(k + 1, len(idx_win)):
                i = int(idx_win[k2])
                if i >= cur_i or (i - j) > 12:
                    break
                if i - 2 < 0:
                    continue
                if direction == "short":
                    if not (l[i - 2] > h[i]):
                        continue
                    zl, zh = float(h[i]), float(l[i - 2])
                    entry_px = zl
                    if h[cur_i] < entry_px:
                        continue
                    if np.max(h[i + 1:cur_i + 1]) >= zh:
                        continue
                    if c[cur_i] > zh:
                        continue
                else:
                    if not (h[i - 2] < l[i]):
                        continue
                    zl, zh = float(h[i - 2]), float(l[i])
                    entry_px = zh
                    if l[cur_i] > entry_px:
                        continue
                    if np.min(l[i + 1:cur_i + 1]) <= zl:
                        continue
                    if c[cur_i] < zl:
                        continue
                rec["fvg"] += 1
                rec["entry"] += 1
                if use_bias:
                    if direction == "short" and not (c[cur_i] < day_open):
                        continue
                    if direction == "long" and not (c[cur_i] > day_open):
                        continue
                rec["bias_ok"] += 1
                break
    tot = {"win_bars": 0, "sweeps": 0, "fvg": 0, "entry": 0, "bias_ok": 0}
    for d, rec in days_seen.items():
        for k in tot:
            tot[k] += rec[k]
    print("  NY days in window:", len(days_seen), tot)
    if verbose_days:
        for d in sorted(days_seen):
            rec = days_seen[d]
            if rec["sweeps"] or rec["entry"]:
                print("   ", d, rec)


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "MGC"
    tf = sys.argv[2] if len(sys.argv) > 2 else "5m"
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 45
    funnel(sym, tf, days, verbose_days=("-v" in sys.argv))
