"""Silver Bullet PDH/PDL Retest 15m, rolling=48 (12h reference).

Cross-asset variant: same generic logic as csid 270 (5m ZF nogate), adapted to 15m
bars with rolling_pdhl_bars=48 to capture 12-hour swing levels.

Cross-symbol backtest (180d, cost_model=v1_realistic_micros):
- MGC: n=66, 51W/15L (77.3% WR), PF=2.71, E[R]=+1.28R, +$6,410.20, maxDD $835.20
- ZF:  n=26, 21W/4L  (80.8% WR), PF=4.06, E[R]=+1.36R, +$2,404.69, maxDD $422.88
- MCL: n=74, 59W/14L (79.7% WR), PF=4.81, E[R]=+1.36R, +$4,646.10, maxDD $167.30
- MNQ: n=107, 98W/9L (91.6% WR) - REJECTED (BT-LURE red flag; WR > 90%)

Fills the 15m silver_bullet gap on MGC, ZF, MCL. Architecture: rolling 12h
PDH/PDL breakout + retrace + FVG confirmation, no time gate, target=2R.

NOT BT-LURE on MGC/ZF/MCL because PF 2.7-4.8 with WR 77-81% (below 90% threshold);
E[R] = +1.28R to +1.36R consistent with WR~80% × 2R target - 20% × 1R loss math.
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.10))
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 24))
    rolling_window = int(params.get("rolling_pdhl_bars", 48))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=24).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=24).min().shift(1).values
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