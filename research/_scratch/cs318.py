"""
OB Midpoint Reversion 15m — 6J yen micro port.
Architecture: prior candle forms OB (body_ratio>=0.5). Next candle displaces
in opposite direction by >=1.0*ATR AND sweeps OB extreme. Current bar
retraces INTO OB zone and shows directional body. Entry at OB midpoint,
stop at OB extreme + 20% ATR buffer, target = 2R.

6J-specific notes: tick=0.0000005, dollars_per_point=12,500,000.
6J has chronic commission bleed (finding 068c1fe6) — the 15m aggregation
reduces fire frequency (~188 trades/90d ≈ 2/day) and per-trade dollar magnitude
($35k R-multiple) compared to 5m.

Fresh 6J 15m OB midpoint slot fill — no ob_midpoint variants active on 6J.
Cross-symbol validation: PF=2.18 on 6J native bars; PF=2.18-3.00 family on
MGC/MNQ/MCL siblings per finding 5fecff16.
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
    ob_age_max = int(params.get('ob_age_max', 24))
    body_ratio_min = float(params.get('body_ratio_min', 0.5))
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
        if j + 1 >= cur_i:
            continue
        if c_j > o_j and cur_low <= h_j and cur_close > (h_j + l_j) / 2.0 and cur_open < cur_close:
            ob_top = h_j
            ob_bot = l_j
            ob_mid = (ob_top + ob_bot) / 2.0
            if cur_low <= ob_top and cur_close > ob_mid and cur_open < cur_close:
                entry = ob_mid
                stop = ob_bot - buf
                risk = entry - stop
                if risk > 0:
                    return {'direction': 'long', 'entry': entry, 'stop': stop,
                            'target': entry + rr * risk}
        if c_j < o_j and cur_high >= l_j and cur_close < (h_j + l_j) / 2.0 and cur_open > cur_close:
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
        break
    return None
