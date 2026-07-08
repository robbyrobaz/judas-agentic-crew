"""
Order Block (OB) midpoint 15m — OB midpoint reversion.

Architecture:
1. Detect a bullish OB: last bullish candle (close > open) followed by a
   strong bearish displacement (downward displacement candle with range > k*ATR
   and close < OB candle's low). Mark OB = range of the bullish candle.
2. Detect a bearish OB symmetrically.
3. After OB formed, wait for price to retrace INTO the OB zone (close must be
   inside).
4. Enter at midpoint of OB.
5. Stop: beyond OB wick + ATR buffer.
6. Target: rr-based.

Different from FVG midpoint primitives (CSID 211 etc.) — OB reversion uses
the prior candle's range as the zone rather than a 3-candle FVG gap. The OB
midpoint is a different swing primitive.

Timeframe: 15m (per task WS3 spec)
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    n = len(bars)
    if n < 40:
        return None
    cur_i = n - 1
    if cur_i < 25:
        return None

    atr_period = int(params.get('atr_period', 14))
    displacement_k = float(params.get('displacement_k', 1.0))
    rr = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.20))
    ob_age_max = int(params.get('ob_age_max', 8))
    body_ratio_min = float(params.get('body_ratio_min', 0.4))

    # ATR
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

    # Look back to find OB formation: bullish OB = bullish bar followed by
    # bearish displacement; bearish OB = bearish bar followed by bullish displacement.
    # Most recent event first.
    for j in range(cur_i - 2, max(cur_i - 2 - ob_age_max, atr_period), -1):
        o_j = float(bars['open'].iloc[j])
        h_j = float(bars['high'].iloc[j])
        l_j = float(bars['low'].iloc[j])
        c_j = float(bars['close'].iloc[j])
        rng_j = h_j - l_j
        if rng_j <= 0:
            continue
        body_j = abs(c_j - o_j)
        br_j = body_j / rng_j
        if br_j < body_ratio_min:
            continue

        # Check next bar (j+1) for displacement in opposite direction
        if j + 1 >= cur_i:
            continue
        o_k = float(bars['open'].iloc[j + 1])
        h_k = float(bars['high'].iloc[j + 1])
        l_k = float(bars['low'].iloc[j + 1])
        c_k = float(bars['close'].iloc[j + 1])
        rng_k = h_k - l_k

        # Bullish OB: j is bullish (c > o), j+1 is bearish displacement (close well below j's low)
        if c_j > o_j and c_k < o_k and rng_k >= displacement_k * atr and l_k < l_j:
            # OB zone = j's range [l_j, h_j], midpoint = (l_j+h_j)/2
            ob_top = h_j
            ob_bot = l_j
            ob_mid = (ob_top + ob_bot) / 2.0
            # Price must retrace INTO OB (current bar's range overlaps ob_bot..ob_top)
            # and close in/above the OB top? No — for continuation-style entry we want
            # the close to reverse direction from bearish displacement — bullish bar.
            if cur_low <= ob_top and cur_close > ob_mid and cur_open < cur_close:
                entry = ob_mid
                stop = ob_bot - buf
                risk = entry - stop
                if risk > 0:
                    return {'direction': 'long', 'entry': entry, 'stop': stop,
                            'target': entry + rr * risk}

        # Bearish OB: j is bearish (c < o), j+1 is bullish displacement above j's high
        if c_j < o_j and c_k > o_k and rng_k >= displacement_k * atr and h_k > h_j:
            ob_top = h_j
            ob_bot = l_j
            ob_mid = (ob_top + ob_bot) / 2.0
            if cur_high >= ob_bot and cur_close < ob_mid and cur_open > cur_close:
                entry = ob_mid
                stop = ob_top + buf
                risk = stop - entry
                if risk > 0:
                    return {'direction': 'short', 'entry': entry, 'stop': stop,
                            'target': entry - rr * risk}
        # only most recent OB
        break
    return None
