# 5m ATR-Relative Displacement Continuation on DX dollar index
# Adapted from csid156 architecture. Detects institutional displacement candle
# (range > 1.5x ATR(14), body/range >= 0.70) and enters on pullback.
# ATR-relative scaling makes the same code work across all symbols.
_S = {'last_processed_idx': -1}

def evaluate(bars, params):
    n = len(bars)
    if n < 30: return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']: return None
    _S['last_processed_idx'] = cur_i

    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.5))
    body_ratio_min = float(params.get('body_ratio_min', 0.70))
    pullback_pct = float(params.get('pullback_pct', 0.40))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.15))

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)

    trs = []
    for i in range(1, n):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    if len(trs) < atr_period: return None
    atr = sum(trs[-atr_period:]) / atr_period
    if atr <= 0: return None

    disp_i = n - 1
    o = float(opens[disp_i])
    h = float(highs[disp_i])
    l = float(lows[disp_i])
    cl = float(closes[disp_i])
    rng = h - l
    body = abs(cl - o)
    if rng <= 0: return None
    body_r = body / rng
    if body_r < body_ratio_min: return None
    if rng < disp_atr_mult * atr: return None

    is_bull = cl > o
    if is_bull:
        entry = h - pullback_pct * rng
        stop = l - stop_buffer_atr * atr
        risk = entry - stop
        if risk <= 0: return None
        target = entry + target_r * risk
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}
    else:
        entry = l + pullback_pct * rng
        stop = h + stop_buffer_atr * atr
        risk = stop - entry
        if risk <= 0: return None
        target = entry - target_r * risk
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}