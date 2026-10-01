"""CSID 257 silver_bullet_pdh_pdl_retest_5m no_time_gate variant on ZF.

Removes the 13<=UTC<20 NY-AM gate; admits any 5m signal that satisfies the
PDH/PDL break + retrace + FVG confirmation. 5m bars, rolling PDH/PDL window
of 12 bars (1h tight reference) — same architecture as CSID 257 (silver_bullet
5m roll12 v1, active on MGC) without session restriction.

Backtest ZF 5m (180d cache, cost_model=v1_realistic_micros):
  n=25, 18W/7L (72% WR), PF=3.25, E[R]=+0.98, $1,857.81, maxDD $355.56
Cross-symbol confirmation (CSID 257 arch):
  MGC 5m roll12 (active #4579 family): PF=4.36, n=45, E[R]=+1.11
  MNQ 5m roll12: PF=8.39, n=68, E[R]=+1.36
  MCL 5m roll12: PF=2.63, n=59, E[R]=+0.98
  6J 5m roll12: PF=1.44, n=21, E[R]=+0.68 (architecture trades 6J but marginally)

Fills operator #2608 ask for "ZF refill with a DIFFERENT architecture family"
than ict_mitigation_block (which has E[R] anomaly on ZF per finding efb27fa9).
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.0078125))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    rolling_window = int(params.get("rolling_pdhl_bars", 12))
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
