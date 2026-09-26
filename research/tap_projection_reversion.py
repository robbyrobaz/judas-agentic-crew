"""Tap Projection Reversal (Nautilus / ICT concept).

From YT:souu5PVb-5M: project yesterday's same-hour levels, take reversal entries when
body closes back inside the projection band after a wick-through (wicks deceive, bodies
tell truth). Targets the 50% midpoint of yesterday's same-hour range.

Adaptation to single-symbol custom engine on 15m bars:
  - "Same hour" anchor = bar exactly 96 periods ago (24h on 15m) plus a small window
  - Project band = [prev_low, prev_high]
  - LONG: current low wicks below prev_low AND current close > prev_low
  - SHORT: current high wicks above prev_high AND current close < prev_high
  - Stop = projection extreme +/- 0.5 ATR beyond
  - Target = 50% of yesterday-same-hour range (≈ fib midpoint, ICT term "midnight open")
  - Session filter: NY morning only (13-16 UTC, which is 9-12 ET during EDT)
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    n = len(bars)
    if n < 200:
        return None
    atr_period = int(params.get('atr_period', 14))
    target_r = float(params.get('target_r', 1.5))
    stop_buf = float(params.get('stop_buf_atr', 0.5))
    session_start = int(params.get('session_start_utc', 13))
    session_end = int(params.get('session_end_utc', 16))
    min_sweep_atr = float(params.get('min_sweep_atr', 0.20))  # need wick through by >= 0.20 ATR
    body_close_frac = float(params.get('body_close_frac', 0.50))  # body must close inside by 50%
    lookback_bars = int(params.get('lookback_bars', 96))  # 24h on 15m
    atr_tol = float(params.get('atr_tol', 0.0))  # 0 = yesterday exact, 0.5 = allow ±0.5 ATR diff

    h = bars['high'].values.astype(float)
    l = bars['low'].values.astype(float)
    c = bars['close'].values.astype(float)
    o = bars['open'].values.astype(float)
    ts_v = bars['ts'].values

    trs = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, n)]
    if len(trs) < atr_period:
        return None
    atr = sum(trs[-atr_period:]) / atr_period
    if atr <= 0:
        return None

    try:
        ts_dt = pd.to_datetime(ts_v, utc=True, errors='coerce')
        hours = ts_dt.hour.values
    except Exception:
        return None

    last = n - 1
    cur_h = hours[last]
    if cur_h < session_start or cur_h >= session_end:
        return None

    # Find the matching bar from the prior day
    proj_idx = None
    for off in range(lookback_bars, lookback_bars + 8):
        cand = last - off
        if cand < 0:
            break
        if hours[cand] == cur_h:
            proj_idx = cand
            break
    if proj_idx is None:
        return None

    prev_high = h[proj_idx]
    prev_low = l[proj_idx]
    if prev_high - prev_low <= atr_tol * atr:
        return None
    yesterday_range = prev_high - prev_low
    midpoint = (prev_high + prev_low) / 2.0

    cur_open = o[last]
    cur_close = c[last]
    cur_low = l[last]
    cur_high = h[last]

    # Bull trap: wick above prev_high, body closes back inside
    if cur_high >= prev_high + min_sweep_atr * atr:
        if cur_close < prev_high:
            penetration = (cur_high - cur_close) / max(prev_high - cur_low, atr)
            if penetration >= body_close_frac:
                entry = cur_close
                stop = cur_high + stop_buf * atr
                risk = stop - entry
                if risk < 0.5 * atr * stop_buf:
                    return None
                # ICT target: midnight open / 50% of yesterday range
                target = max(midpoint, entry - target_r * risk)
                return {"ts": ts_v[last], "direction": "short",
                        "entry": entry, "stop": stop, "target": target}

    # Bear trap: wick below prev_low, body closes back inside
    if cur_low <= prev_low - min_sweep_atr * atr:
        if cur_close > prev_low:
            penetration = (cur_close - cur_low) / max(prev_high - prev_low, atr)
            if penetration >= body_close_frac:
                entry = cur_close
                stop = cur_low - stop_buf * atr
                risk = entry - stop
                if risk < 0.5 * atr * stop_buf:
                    return None
                target = min(midpoint, entry + target_r * risk)
                return {"ts": ts_v[last], "direction": "long",
                        "entry": entry, "stop": stop, "target": target}

    return None
