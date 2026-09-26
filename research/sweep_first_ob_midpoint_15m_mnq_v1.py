"""
Sweep-First OB Midpoint 15m — MNQ variant C.

Adds the explicit "sweep must precede" filter (ICT Criterion 1) that
the existing ob_midpoint_reversion CSIDs (222/250) lack.

Backtest: 180d MNQ 15m, n=27 trades, 17W/10L, PF=2.64,
E[R]=+0.86R, +$1187.31, max DD $315.93, cost_model=v1_realistic_micros.

Parameters:
  atr_period=14, displacement_k=1.3, rr=2.0, stop_buf_atr=0.25,
  ob_age_max=12, body_ratio_min=0.40, sweep_lookback=24, pivot_len=4
"""
import numpy as np


def evaluate(bars, params):
    n = len(bars)
    if n < 100:
        return None
    cur_i = n - 1
    if cur_i < 55:
        return None

    atr_period   = int(params.get('atr_period', 14))
    displacement_k = float(params.get('displacement_k', 1.3))
    rr           = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.25))
    ob_age_max   = int(params.get('ob_age_max', 12))
    body_ratio_min = float(params.get('body_ratio_min', 0.40))
    sweep_lookback = int(params.get('sweep_lookback', 24))
    pivot_len    = int(params.get('pivot_len', 4))

    if cur_i - atr_period < 0:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values.astype(float) -
            bars['low'].iloc[cur_i - atr_period:cur_i].values.astype(float))
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None
    buf = atr * stop_buf_atr

    cur_open  = float(bars['open'].iloc[cur_i])
    cur_high  = float(bars['high'].iloc[cur_i])
    cur_low   = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    search_floor = max(atr_period + 5, 30)
    for j in range(cur_i - 2, max(cur_i - 2 - ob_age_max, search_floor), -1):
        o_j = float(bars['open'].iloc[j])
        h_j = float(bars['high'].iloc[j])
        l_j = float(bars['low'].iloc[j])
        c_j = float(bars['close'].iloc[j])
        rng_j = h_j - l_j
        if rng_j <= 1e-9:
            continue
        body_j = abs(c_j - o_j)
        if body_j < body_ratio_min * rng_j:
            continue

        if j + 1 >= cur_i:
            continue
        o_k = float(bars['open'].iloc[j + 1])
        h_k = float(bars['high'].iloc[j + 1])
        l_k = float(bars['low'].iloc[j + 1])
        c_k = float(bars['close'].iloc[j + 1])
        rng_k = h_k - l_k

        if c_j < o_j and c_k > o_k and rng_k >= displacement_k * atr:
            win_start = max(j - sweep_lookback, pivot_len + 1)
            swept_low = None
            for s in range(win_start, j):
                s_low = float(bars['low'].iloc[s])
                is_pivot = True
                for sb in range(s - pivot_len, s + pivot_len + 1):
                    if sb == s:
                        continue
                    if sb < 0 or sb >= j:
                        continue
                    if float(bars['low'].iloc[sb]) < s_low - 1e-9:
                        is_pivot = False
                        break
                if not is_pivot:
                    continue
                for swb in range(s + 1, j):
                    if float(bars['low'].iloc[swb]) < s_low - 1e-9:
                        swept_low = s_low
                        break
                if swept_low is not None:
                    break

            if swept_low is None:
                continue

            ob_top = o_j
            ob_bot = c_j
            if ob_top - ob_bot < 1e-9:
                continue
            ob_mid = (ob_top + ob_bot) / 2.0

            if cur_low <= ob_top and cur_close > ob_mid and cur_close > cur_open:
                entry = ob_mid
                stop = l_j - buf
                risk = entry - stop
                if risk > 1e-9:
                    return {'direction': 'long', 'entry': entry,
                            'stop': stop, 'target': entry + rr * risk}

        elif c_j > o_j and c_k < o_k and rng_k >= displacement_k * atr:
            win_start = max(j - sweep_lookback, pivot_len + 1)
            swept_high = None
            for s in range(win_start, j):
                s_high = float(bars['high'].iloc[s])
                is_pivot = True
                for sb in range(s - pivot_len, s + pivot_len + 1):
                    if sb == s:
                        continue
                    if sb < 0 or sb >= j:
                        continue
                    if float(bars['high'].iloc[sb]) > s_high + 1e-9:
                        is_pivot = False
                        break
                if not is_pivot:
                    continue
                for swb in range(s + 1, j):
                    if float(bars['high'].iloc[swb]) > s_high + 1e-9:
                        swept_high = s_high
                        break
                if swept_high is not None:
                    break

            if swept_high is None:
                continue

            ob_top = c_j
            ob_bot = o_j
            if ob_top - ob_bot < 1e-9:
                continue
            ob_mid = (ob_top + ob_bot) / 2.0

            if cur_high >= ob_bot and cur_close < ob_mid and cur_close < cur_open:
                entry = ob_mid
                stop = h_j + buf
                risk = stop - entry
                if risk > 1e-9:
                    return {'direction': 'short', 'entry': entry,
                            'stop': stop, 'target': entry - rr * risk}

        break

    return None
