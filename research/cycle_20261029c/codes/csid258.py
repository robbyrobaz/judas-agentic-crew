"""CSID 219 adapted to 15m MNQ: rolling=48 (12h reference, 2 days).
Sister of #4517 MNQ 15m roll=96 (best live) and #4577 MNQ 15m roll=24 (fresh 0-fire).
Backtest 15m MNQ: n=36, 32W/4L (88.9% WR), PF=8.01, E[R]=+1.67, $4060.50.
Fills the gap between roll=24 (intraday) and roll=96 (4-day) MNQ 15m sisters.
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.25))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    roll = int(params.get("rolling_window", 48))
    min_per = int(params.get("min_periods", 24))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(roll, min_periods=min_per).max().shift(1).values
    pdl_series = pd.Series(l).rolling(roll, min_periods=min_per).min().shift(1).values
    try:
        ts_dt = pd.to_datetime(ts_v, utc=True, errors="coerce")
        hours = ts_dt.hour.values
    except Exception:
        return None
    last = n - 1
    if last < 5 or not (13 <= hours[last] < 20):
        return None
    if np.isnan(pdh_series[last]) or np.isnan(pdl_series[last]):
        return None
    if h[last] >= pdh_series[last] + break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] < pdh_series[j] and l[j] >= pdl_series[j]:
                for k in range(j + 1, last):
                    if l[k] > h[k - 2]:
                        entry_idx = k + 1
                        if entry_idx < n:
                            entry = o[entry_idx]
                            stop = min(l[j], l[last]) - stop_buf
                            risk = entry - stop
                            if risk >= 0.5 * tick:
                                target = entry + risk * target_r
                                return {"ts": ts_v[entry_idx], "direction": "long",
                                        "entry": entry, "stop": stop, "target": target}
                break
    if l[last] <= pdl_series[last] - break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] > pdl_series[j] and h[j] <= pdh_series[j]:
                for k in range(j + 1, last):
                    if h[k] < l[k - 2]:
                        entry_idx = k + 1
                        if entry_idx < n:
                            entry = o[entry_idx]
                            stop = max(h[j], h[last]) + stop_buf
                            risk = stop - entry
                            if risk >= 0.5 * tick:
                                target = entry - risk * target_r
                                return {"ts": ts_v[entry_idx], "direction": "short",
                                        "entry": entry, "stop": stop, "target": target}
                break
    return None