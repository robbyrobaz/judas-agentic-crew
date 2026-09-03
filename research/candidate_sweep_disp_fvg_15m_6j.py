"""6J 15m — Sweep + Displacement + FVG Midpoint Entry (tR=1.5, sweep_lookback=20, body_ratio=0.80)
Conservative variant for 6J 15m coverage (task #2342). Independent of CSID 156/260/262.
"""
_S = {'last_processed_idx': -1}

def evaluate(bars, params):
    n = len(bars)
    if n < 60: return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']: return None
    _S['last_processed_idx'] = cur_i

    atr_period = int(params.get('atr_period', 14))
    sweep_lookback = int(params.get('sweep_lookback', 20))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.5))
    body_ratio_min = float(params.get('body_ratio_min', 0.80))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.15))

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, n)]
    if len(trs) < atr_period + sweep_lookback + 1: return None
    atr = sum(trs[-atr_period:]) / atr_period
    if atr <= 0: return None

    disp_i = n - 1
    o = float(bars['open'].iloc[disp_i])
    h = float(bars['high'].iloc[disp_i])
    l = float(bars['low'].iloc[disp_i])
    cl = float(bars['close'].iloc[disp_i])
    rng = h - l
    body = abs(cl - o)
    if rng <= 0: return None
    body_r = body / rng
    if body_r < body_ratio_min: return None
    if rng < disp_atr_mult * atr: return None

    prior_window = highs[-(sweep_lookback + 2):-1]
    prior_high = max(prior_window) if len(prior_window) > 0 else h
    prior_window_l = lows[-(sweep_lookback + 2):-1]
    prior_low = min(prior_window_l) if len(prior_window_l) > 0 else l

    is_bull = cl > o
    is_sweep_long = h > prior_high
    is_sweep_short = l < prior_low

    if is_bull and not is_sweep_long: return None
    if not is_bull and not is_sweep_short: return None

    if is_bull:
        entry = (h + l) / 2
        stop = l - stop_buffer_atr * atr
        risk = entry - stop
        if risk <= 0: return None
        target = entry + target_r * risk
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}
    else:
        entry = (h + l) / 2
        stop = h + stop_buffer_atr * atr
        risk = stop - entry
        if risk <= 0: return None
        target = entry - target_r * risk
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}