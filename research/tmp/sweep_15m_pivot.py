"""
Sweep entry at PRIOR-SESSION H/L — eval-internal but emits hint of params used
in the 'error' field for param tracking.
"""
import numpy as np

def evaluate(bars, params):
    n = len(bars)
    if n < 100:
        return None

    session_bars = int(params.get('session_bars', 13))
    atr_period = int(params.get('atr_period', 14))
    target_r = float(params.get('target_r', 2.0))
    stop_atr = float(params.get('stop_atr', 0.15))
    sweep_window = int(params.get('sweep_window', 2))
    min_sweep_atr_mult = float(params.get('min_sweep_atr_mult', 0.0))
    killzone_filter = bool(params.get('killzone_filter', True))
    require_reversal = bool(params.get('require_reversal', True))

    if n < session_bars + atr_period + 10:
        return None

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)

    tr = np.zeros(n)
    for j in range(n):
        if j == 0:
            tr[j] = highs[j] - lows[j]
        else:
            tr[j] = max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1]))
    atr_vals = np.zeros(n)
    cum = 0.0
    for j in range(atr_period):
        cum += tr[j]
    atr_vals[atr_period-1] = cum / atr_period
    for j in range(atr_period, n):
        atr_vals[j] = (atr_vals[j-1]*(atr_period-1) + tr[j]) / atr_period

    cur_i = n - 1
    if cur_i < atr_period + session_bars:
        return None
    atr = atr_vals[cur_i]
    if atr <= 0:
        return None

    in_kz = True
    if killzone_filter:
        try:
            ts = bars['ts'].iloc[cur_i] if 'ts' in bars.columns else bars.index[cur_i]
            mod_min = ts.hour * 60 + ts.minute
            in_kz = (6*60 <= mod_min < 11*60) or (12*60 <= mod_min < 16*60)
        except Exception:
            in_kz = True
    if not in_kz:
        return None

    prior_hi = float(np.max(highs[max(0, cur_i - session_bars - 1):max(1, cur_i)]))
    prior_lo = float(np.min(lows[max(0, cur_i - session_bars - 1):max(1, cur_i)]))

    short_setup = False
    long_setup = False
    sweep_bar = -1
    for j in range(max(2, cur_i - sweep_window), cur_i + 1):
        if highs[j] > prior_hi and closes[j] <= prior_hi and (highs[j] - prior_hi) >= atr * min_sweep_atr_mult:
            short_setup = True
            sweep_bar = j
            break
        if lows[j] < prior_lo and closes[j] >= prior_lo and (prior_lo - lows[j]) >= atr * min_sweep_atr_mult:
            long_setup = True
            sweep_bar = j
            break

    if not (short_setup or long_setup):
        return None

    sb_h = highs[sweep_bar]
    sb_l = lows[sweep_bar]
    sb_c = closes[sweep_bar]

    if short_setup:
        if require_reversal and closes[cur_i] >= prior_hi:
            return None
        entry = sb_c
        stop = sb_h + atr * stop_atr
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_r * risk
        return {'direction': 'short', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
    else:
        if require_reversal and closes[cur_i] <= prior_lo:
            return None
        entry = sb_c
        stop = sb_l - atr * stop_atr
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_r * risk
        return {'direction': 'long', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
