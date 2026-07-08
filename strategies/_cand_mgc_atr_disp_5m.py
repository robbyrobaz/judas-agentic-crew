"""
ATR Displacement 5m with explicit session filter.

Architecture (ICT displacement primitive):
1. Session gate: London 06:00-16:00 UTC OR NY 13:00-22:00 UTC (session union = 06-22 UTC).
   EXPLICITLY EXCLUDES Asia overnight (00-06 UTC) and after-hours (22-24 UTC).
2. Compute ATR(14) on 5m bars.
3. Detect displacement candle: range > k*ATR AND body_ratio > 0.6
4. Direction = close > open (bull) or close < open (bear)
5. After displacement: enter pullback to displacement candle midpoint on next
   bar pullback to the displacement's 50% level.
6. Stop: beyond displacement wick by atr buffer.
7. Target: rr-based.

Different from existing CSID 209/211 (which are iFVG reversion at midpoint).
This is a DISPLACEMENT CONTINUATION primitive — it expects the move to continue,
not mean-revert. Session filter is explicit (not post-hoc).

Timeframe: 5m (per task WS3 spec)
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    import pandas as pd
    n = len(bars)
    if n < 30:
        return None
    cur_i = n - 1
    if cur_i < 16:
        return None

    if 'ts' in bars.columns:
        ts = pd.to_datetime(bars['ts'], utc=True)
    else:
        ts = pd.DatetimeIndex(bars.index)
        if ts.tz is None:
            ts = ts.tz_localize('UTC')
    cur_hour = ts.iloc[cur_i].hour if hasattr(ts, 'iloc') else ts[cur_i].hour

    # Session gate: London + NY = 06:00 - 22:00 UTC
    if cur_hour < 6 or cur_hour >= 22:
        return None

    atr_period = int(params.get('atr_period', 14))
    displacement_k = float(params.get('displacement_k', 1.5))
    body_ratio_min = float(params.get('body_ratio_min', 0.60))
    rr = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.25))
    lookback_displacement = int(params.get('lookback', 5))

    # ATR on prior bars
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values.astype(float) -
            bars['low'].iloc[cur_i - atr_period:cur_i].values.astype(float))
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    buf = atr * stop_buf_atr

    cur_open = float(bars['open'].iloc[cur_i])
    cur_high = float(bars['high'].iloc[cur_i])
    cur_low = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    # Walk back to find the most recent displacement candle
    for j in range(cur_i - 1, max(cur_i - 1 - lookback_displacement, atr_period), -1):
        o = float(bars['open'].iloc[j])
        h = float(bars['high'].iloc[j])
        l = float(bars['low'].iloc[j])
        c = float(bars['close'].iloc[j])
        rng = h - l
        if rng <= 0:
            continue
        body = abs(c - o)
        br = body / rng
        if rng < displacement_k * atr:
            continue
        if br < body_ratio_min:
            continue
        # Found displacement. Direction:
        if c > o:
            # bullish displacement. Wait for current bar to pull back INTO
            # displacement zone's upper half but not beyond top.
            disp_mid = (o + c) / 2.0
            disp_top = h
            disp_bot = l
            # pullback requirement: current bar low <= disp_mid, current close > disp_top
            if cur_low <= disp_mid and cur_close > disp_top and cur_open < cur_close:
                entry = disp_top  # conservative entry at the breakout level
                stop = disp_bot - buf
                risk = entry - stop
                if risk > 0:
                    return {'direction': 'long', 'entry': entry, 'stop': stop,
                            'target': entry + rr * risk}
        else:
            # bearish displacement
            disp_mid = (o + c) / 2.0
            disp_top = h
            disp_bot = l
            if cur_high >= disp_mid and cur_close < disp_bot and cur_open > cur_close:
                entry = disp_bot
                stop = disp_top + buf
                risk = stop - entry
                if risk > 0:
                    return {'direction': 'short', 'entry': entry, 'stop': stop,
                            'target': entry - rr * risk}
        # only check the most recent displacement
        break
    return None
