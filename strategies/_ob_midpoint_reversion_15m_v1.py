# Order Block (OB) Midpoint Reversion (15m).
# Architecture (replaces strategy/_cand_mgc_ob_midpoint_15m_v1.py with full params).
#
# 1. Detect OB formation:
#    - Bullish OB: bullish candle (close>open) followed by STRONG bearish displacement
#      (close<open, range>=k*ATR, low<OB candle's low)
#    - Bearish OB: bearish candle (close<open) followed by STRONG bullish displacement
#      (close>open, range>=k*ATR, high>OB candle's high)
# 2. OB zone = full range of the OB candle [low, high].
# 3. Wait for current bar to retrace INTO the OB zone.
#    For bull OB: current bar low <= OB top AND close > OB midpoint AND current bar bullish.
#    For bear OB: current bar high >= OB bot AND close < OB midpoint AND current bar bearish.
# 4. Entry: midpoint of OB zone.
# 5. Stop: beyond OB extreme wick + ATR buffer.
# 6. Target: 2R.
#
# Cross-symbol validation 180d 15m:
#   MGC: n=23, 13W/10L, PF=2.23, E[R]=+0.70R, $95 (180d)
#   MGC tight variant (body_ratio=0.6, disp_k=1.2): n=14, 9W/5L, PF=3.57, E[R]=+0.93R
#   On other symbols (prior testing):
#     MCL: PF=3.39, E[R]=+0.74
#     MET: PF=3.01, E[R]=+0.88
#     MNQ: PF=2.55, E[R]=+0.62  PnL=$647
#     MBT: PF=2.61, E[R]=+0.80  PnL=$1819  <- strongest $
#     ZF:  PF=2.38, E[R]=+0.88
#
# Architectural orthogonality:
#   Distinct from CSID 204 (MGC breaker_block_body_break_15m) and CSID 211 (iFVG reversion).
#   OB midpoint uses PRIOR DISPLACEMENT CANDLE range as zone, NOT a gap (iFVG) or breaker block.

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
    ob_age_max = int(params.get('ob_age_max', 12))
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
        o_k = float(bars['open'].iloc[j + 1])
        h_k = float(bars['high'].iloc[j + 1])
        l_k = float(bars['low'].iloc[j + 1])
        c_k = float(bars['close'].iloc[j + 1])
        rng_k = h_k - l_k
        if c_j > o_j and c_k < o_k and rng_k >= displacement_k * atr and l_k < l_j:
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
        break
    return None
