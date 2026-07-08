import numpy as np
import pandas as pd

TICK = 0.1


def evaluate(bars, params):
    """Silver Bullet PDH/PDL break + retrace + FVG (5m, NY 13-20 UTC).

    Per-bar evaluation: at each bar, detect if a PDH/PDL break is currently
    active (last bar high >= pdh + break_thr, or low <= pdl - break_thr).
    If so, look back N bars for the retrace and FVG. Returns a single dict
    per the sandbox API.
    """
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    stop_buf = float(params.get("stop_buf_ticks", 8)) * TICK
    break_thr = float(params.get("break_threshold_ticks", 5)) * TICK
    max_retest = int(params.get("max_retest_bars", 16))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(288, min_periods=50).max().shift(1).values
    pdl_series = pd.Series(l).rolling(288, min_periods=50).min().shift(1).values
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
                            if risk >= 0.5:
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
                            if risk >= 0.5:
                                target = entry - risk * target_r
                                return {"ts": ts_v[entry_idx], "direction": "short",
                                        "entry": entry, "stop": stop, "target": target}
                break
    return None