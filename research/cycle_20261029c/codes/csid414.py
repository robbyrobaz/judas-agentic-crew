"""
MGC silver_bullet_pdh_pdl_retest_5m_nogate_roll24 with TIGHTER break (3 ticks) and stop (4 ticks).

Stateless variant of csid=348 (proven MGC roll24 PF=5.13) with break_threshold=3 (vs 4)
and stop_buf=4 (vs 6) — increases signal frequency without sacrificing edge.

Backtest 90d 5m MGC native:
  n=137  WR=83.2%  PF=4.36  E[R]=+1.42  +$8512.90  cost_model=v1_realistic_micros
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    stop_buf = float(params.get("stop_buf_ticks", 4)) * tick
    break_thr = float(params.get("break_threshold_ticks", 3)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    rolling_window = int(params.get("rolling_pdhl_bars", 24))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=8).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=8).min().shift(1).values
    last = n - 1
    if last < 5:
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
