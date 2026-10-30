"""Silver Bullet 15m MGC — 50% premium/discount FVG filter with proper fallthrough.

Architecture: PDH/PDL sweep + retest + MSS bullish/bearish + FVG entry.
Filter: after MSS, compute 50% midpoint of impulsive leg [sweep_low→MSS_high] for
longs and [MSS_low→sweep_high] for shorts. Only enter LONG if FVG midpoint is
BELOW 50% midpoint (discount zone). Only enter SHORT if FVG midpoint is ABOVE 50%
midpoint (premium zone). v2 fixes fallthrough: filter rejection on LONG falls
through to SHORT check (same bar could have SHORT setup).
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
    max_retest = int(params.get("max_retest_bars", 16))
    rolling_window = int(params.get("rolling_pdhl_bars", 24))
    USE_PD_FILTER = bool(params.get("premium_discount_filter", True))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=12).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=12).min().shift(1).values

    last = n - 1
    if last < 5:
        return None
    if np.isnan(pdh_series[last]) or np.isnan(pdl_series[last]):
        return None

    # ===== LONG path =====
    if h[last] >= pdh_series[last] + break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] < pdh_series[j] and l[j] >= pdl_series[j]:
                for k in range(j + 1, last):
                    if l[k] > h[k - 2]:
                        impulsive_high = h[k]
                        impulsive_low = min(l[j], l[last])
                        if impulsive_high > impulsive_low:
                            fvg_mid = (h[k-2] + l[k]) / 2.0
                            midpoint = (impulsive_high + impulsive_low) / 2.0
                            passes_filter = (not USE_PD_FILTER) or (fvg_mid < midpoint)
                            if passes_filter:
                                entry_idx = k + 1
                                if entry_idx < n:
                                    entry = o[entry_idx]
                                    stop = impulsive_low - stop_buf
                                    risk = entry - stop
                                    if risk >= 0.5 * tick:
                                        target = entry + risk * target_r
                                        return {"ts": ts_v[entry_idx], "direction": "long",
                                                "entry": entry, "stop": stop, "target": target}
                        break
                break

    # ===== SHORT path (fallthrough from LONG) =====
    if l[last] <= pdl_series[last] - break_thr:
        for j in range(max(0, last - max_retest), last):
            if c[j] > pdl_series[j] and h[j] <= pdh_series[j]:
                for k in range(j + 1, last):
                    if h[k] < l[k - 2]:
                        impulsive_high = max(h[j], h[last])
                        impulsive_low = l[k]
                        if impulsive_high > impulsive_low:
                            fvg_mid = (l[k-2] + h[k]) / 2.0
                            midpoint = (impulsive_high + impulsive_low) / 2.0
                            passes_filter = (not USE_PD_FILTER) or (fvg_mid > midpoint)
                            if passes_filter:
                                entry_idx = k + 1
                                if entry_idx < n:
                                    entry = o[entry_idx]
                                    stop = impulsive_high + stop_buf
                                    risk = stop - entry
                                    if risk >= 0.5 * tick:
                                        target = entry - risk * target_r
                                        return {"ts": ts_v[entry_idx], "direction": "short",
                                                "entry": entry, "stop": stop, "target": target}
                        break
                break

    return None