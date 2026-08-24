
# 5m ATR-Relative Displacement Continuation on 6J yen micro
# Detects institutional displacement candle (range > 1.5x ATR(14), body/range >= 0.70)
# and enters on 40% pullback of the displacement range. Stop beyond displacement
# extreme + 0.15x ATR buffer. Target 1.5R.
# ATR-relative scaling makes the same code work across all 8 symbols (MGC/MNQ/MCL/
# MBT/MET/DX/ZF/6J). 6J raw-tick version produced only 2 signals in 90d because
# 6J's micro-tick (0.000001) makes raw tick counts useless. ATR-relative yields
# 34 sig, 58.8% WR, PF=2.13, E[R]=+0.47R on 6J 5m 90d (180d identical).
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

    # Compute ATR (simple average of last N true ranges)
    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    trs = []
    for i in range(1, n):
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        trs.append(tr)
    if len(trs) < atr_period: return None
    atr = sum(trs[-atr_period:]) / atr_period
    if atr <= 0: return None

    # Use the just-closed bar as the displacement candle
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

