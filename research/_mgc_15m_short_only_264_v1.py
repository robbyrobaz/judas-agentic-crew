"""MGC 15m — CSID 264 atr_disp_continuation SHORT-only variant.

Architecture: same as CSID 264 (atr_disp_continuation 5m long_only) but with
side_filter='short_only'. Mirrors the regime-rotation finding 8195 that
CSID 264 SHORT side is strong on MNQ 5m and MGC 5m; this extends the SHORT
edge to MGC 15m.

Note: this variant suppresses BULL signals (long direction). The runner
delivers one-shot trades; consecutive SHORTs only fire after the previous
trade closes (one position at a time).
"""
def evaluate(bars, params):
    n = len(bars)
    if n < 30: return None
    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.5))
    body_ratio_min = float(params.get('body_ratio_min', 0.70))
    pullback_pct = float(params.get('pullback_pct', 0.40))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.15))
    side_filter = params.get('side_filter', 'short_only')

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, n)]
    if len(trs) < atr_period: return None
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

    is_bull = cl > o
    # Side filter
    if side_filter == 'long_only' and not is_bull: return None
    if side_filter == 'short_only' and is_bull: return None

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