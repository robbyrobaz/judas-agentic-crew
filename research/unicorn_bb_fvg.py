"""Unicorn Setup (BB + FVG overlap) - Stoic Capital / ICT concept.

From YT:mXtTEE4X4oI: the entry of the Judas Swing v2 mechanical is a 'unicorn' setup =
overlap of a breaker block (BB) and a fair value gap (FVG). Long: pull back into the
overlap zone after a bullish displacement. Short: pull back into the overlap zone after
a bearish displacement.

Single-symbol adaptation on 5m bars:
  - Displacement: bar with body ratio >= body_ratio_min AND range >= disp_atr_mult * ATR
    direction = CLOSE > OPEN
  - Breaker Block: the bar BEFORE the displacement (its entire range)
  - FVG: scan last 10 bars for a 3-bar pattern where middle candle body creates a gap
    (next bar's low > prev bar's high for bullish FVG / next bar's high < prev bar's low
     for bearish FVG)
  - Unicorn = BB range overlaps FVG range -> enter on pullback through that zone
  - Stop = max(BB_low, FVG_low) - stop_buf * ATR (long) / min(BB_high, FVG_high) + (short)
  - Target = entry + target_r * risk
  - Session filter: 13-20 UTC (NY morning + afternoon)
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    n = len(bars)
    if n < 50:
        return None
    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 2.0))
    body_ratio_min = float(params.get('body_ratio_min', 0.65))
    fvg_lookback = int(params.get('fvg_lookback', 10))
    stop_buf = float(params.get('stop_buf_atr', 0.5))
    target_r = float(params.get('target_r', 2.0))
    session_start = int(params.get('session_start_utc', 13))
    session_end = int(params.get('session_end_utc', 20))

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

    # Identify most recent displacement (within last 20 bars)
    disp_idx = None
    disp_dir = None
    for j in range(max(0, last - 20), last):
        rng = h[j] - l[j]
        body = abs(c[j] - o[j])
        if rng <= 0 or body / rng < body_ratio_min:
            continue
        if rng < disp_atr_mult * atr:
            continue
        if c[j] > o[j]:
            disp_dir = 'bull'
        elif c[j] < o[j]:
            disp_dir = 'bear'
        else:
            continue
        disp_idx = j
        break
    if disp_idx is None:
        return None

    # Breaker block = bar BEFORE displacement
    bb_high = max(h[disp_idx - 1], o[disp_idx - 1])
    bb_low = min(l[disp_idx - 1], o[disp_idx - 1])

    # Find the most recent FVG in the same direction as the displacement
    fvg_top = fvg_bot = None
    if disp_dir == 'bull':
        # bullish FVG: low[k] > high[k-2]  (gap up)
        for k in range(last - 1, max(0, last - fvg_lookback), -1):
            if k - 2 < 0:
                break
            if l[k] > h[k - 2]:
                fvg_top = l[k]
                fvg_bot = h[k - 2]
                break
    else:
        # bearish FVG: high[k] < low[k-2] (gap down)
        for k in range(last - 1, max(0, last - fvg_lookback), -1):
            if k - 2 < 0:
                break
            if h[k] < l[k - 2]:
                fvg_top = l[k - 2]
                fvg_bot = h[k]
                break

    if fvg_top is None or fvg_bot is None:
        return None

    # Unicorn = overlap of BB and FVG
    overlap_top = min(bb_high, fvg_top)
    overlap_bot = max(bb_low, fvg_bot)
    if overlap_top <= overlap_bot:
        return None

    entry_mid = (overlap_top + overlap_bot) / 2.0

    if disp_dir == 'bull':
        # Wait for pullback: low of last bar dips into the overlap zone
        if l[last] > overlap_top or c[last] < overlap_bot:
            return None
        entry = entry_mid
        stop = min(bb_low, fvg_bot) - stop_buf * atr
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_r * risk
        return {"ts": ts_v[last], "direction": "long",
                "entry": entry, "stop": stop, "target": target}
    else:
        # bearish displacement: wait for pullback up
        if h[last] < overlap_bot or c[last] > overlap_top:
            return None
        entry = entry_mid
        stop = max(bb_high, fvg_top) + stop_buf * atr
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_r * risk
        return {"ts": ts_v[last], "direction": "short",
                "entry": entry, "stop": stop, "target": target}
