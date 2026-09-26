"""
Silver Bullet PDH/PDL Retest 5m on 6J with NY session window.

Architecture: CSID 217 silver_bullet ported to 6J (micro yen futures).
- Rolling PDH/PDL over 288 bars (24h on 5m) of the prior session
- Detect break of rolling PDH (long setup) or PDL (short setup) by >= break_thr
- Look back max_retest_bars for a retrace bar that closed back inside the range
- Confirm with FVG (current bar low > bar 2-back high for long, etc.)
- Entry on next bar open, stop below sweep extreme, target = entry + target_r * risk

NY session window filter: 13:00-20:00 UTC = 09:00-16:00 ET (during EDT) or
08:00-15:00 ET (during EST). This is when 6J sees NY-driven flow.

Why 6J + silver_bullet is novel:
  - 6J actives are ATR_DISP and JUDAS families, NO silver_bullet
  - Cross-symbol sweep showed 6J 5m silver_bullet PF=1.44 marginal (24/7)
  - Adding NY window + tighter break_thr should improve signal quality

Backtest envelope target: PF 1.3-2.5 (NOT BT-LURE), n >= 20, E[R] > 0.
Pre-commit retire: n=10 pf_net<0.9 OR 6 consec L.
"""
import numpy as np
import pandas as pd

TICK = 0.000001  # 6J tick (1 micro yen per USD)


def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    stop_buf = float(params.get("stop_buf_ticks", 8)) * TICK
    break_thr = float(params.get("break_threshold_ticks", 5)) * TICK
    max_retest = int(params.get("max_retest_bars", 16))
    rolling_window = int(params.get("rolling_pdhl_bars", 288))
    session_start = int(params.get("session_start_hour_utc", 13))
    session_end = int(params.get("session_end_hour_utc", 20))

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=50).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=50).min().shift(1).values

    # NY session window check
    try:
        ts_dt = pd.to_datetime(ts_v, utc=True, errors="coerce")
        hours = ts_dt.hour.values
    except Exception:
        return None
    last = n - 1
    if last < 5 or pd.isna(hours[last]) or not (session_start <= hours[last] < session_end):
        return None
    if np.isnan(pdh_series[last]) or np.isnan(pdl_series[last]):
        return None

    # Long setup: PDH break, then retrace, then FVG
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
                            if risk >= 0.5 * TICK:
                                target = entry + risk * target_r
                                return {"ts": ts_v[entry_idx], "direction": "long",
                                        "entry": entry, "stop": stop, "target": target}
                break

    # Short setup: PDL break, then retrace, then FVG
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
                            if risk >= 0.5 * TICK:
                                target = entry - risk * target_r
                                return {"ts": ts_v[entry_idx], "direction": "short",
                                        "entry": entry, "stop": stop, "target": target}
                break
    return None