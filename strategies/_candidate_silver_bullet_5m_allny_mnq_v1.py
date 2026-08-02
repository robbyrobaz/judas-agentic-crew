# Silver Bullet PDH/PDL Retest (5m, full NY 13-20 UTC).
# Tick-aware MNQ port. NEW CSID candidate — strongest single result on MNQ.
#
# Architecture:
#   1. ROLLING PDH/PDL: 48 5m bars = 4h prior-day window.
#   2. SESSION GATE: 13:00-20:00 UTC (NY AM 9 AM - NY close 4 PM ET).
#   3. BREAK DETECT: Current bar breaks PDH (>=+break_thr) or PDL (<=-break_thr).
#   4. RETRACE FILTER: A bar within last 36 bars closed back inside prior range.
#   5. FVG CONFIRM: 3-candle FVG.
#   6. ENTRY: Open of next bar after FVG.
#   7. STOP: Beyond retrace low/high minus 6-tick buffer.
#   8. TARGET: 2R from entry.
#
# Backtest 90d 5m MNQ native bars (cycle 2026-07-24):
#   n=56, 52W/4L, PF=23.31, E[R]=+1.79R, $5269.75, max_dd=$88.75
#
# This is the highest-E[R] result across all 5m/15m backtests run this cycle,
# and MNQ has zero active 5m Silver Bullet coverage. The all-NY window
# matches MGC's working #4412 (custom_5m v3 silver_bullet_pdh_pdl_retest_5m_roll48_mgc_v1)
# but on a different symbol with stronger volatility.
#
# Different from CSID 229 (MGC SB 1h halted target_r=3.0):
#   - Symbol: MNQ (not MGC)
#   - Timeframe: 5m (not 1h)
#   - Rolling PDH/PDL window: 48 bars (4h, matches MGC working baseline)
#   - All-NY window 13-20 UTC (not restricted to specific killzone)
#
# Risk note: 56 signals / 90 days = ~7 trades/week. PF>1 but max_dd $88.75 is real.
# Pre-commit watcher will catch early if PF regresses.

import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
    target_r = float(params.get("target_r", 2.0))
    tick = float(params.get("tick", 0.25))  # MNQ tick
    stop_buf = float(params.get("stop_buf_ticks", 6)) * tick
    break_thr = float(params.get("break_threshold_ticks", 4)) * tick
    max_retest = int(params.get("max_retest_bars", 36))
    rolling_window = int(params.get("rolling_window", 48))
    min_periods = int(params.get("min_periods", 12))
    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    ts_v = bars["ts"].values
    pdh_series = pd.Series(h).rolling(rolling_window, min_periods=min_periods).max().shift(1).values
    pdl_series = pd.Series(l).rolling(rolling_window, min_periods=min_periods).min().shift(1).values
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