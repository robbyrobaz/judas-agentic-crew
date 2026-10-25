# 15m ATR-Relative Displacement Continuation on 6J yen micro (LOOSENED variant)
# Stateless: uses only _S['last_processed_idx'] for dedup. No setup state -> immune
# to the iFVG state-reset runtime bug (finding ffbf74f0).
#
# Loosened thresholds vs CSID 156 (5m strict default 1.5x ATR / 0.70 body ratio):
#   - disp_atr_mult = 1.2 (vs 1.5)  -> more displacements detected on 15m
#   - body_ratio_min = 0.65 (vs 0.70) -> cleaner-than-doji candles count
#
# 15m aggregation captures institutional displacement on 6J better than 5m
# (which yields only 17 sigs in 240d). 240d backtest:
#   n=24, 16W/8L, PF=5.02, E[R]=+0.67R, max DD $0.07
#
# Same ATR-relative scaling as CSID 156/200/201/202 (works across all 8 symbols).
# Risk: 1R per trade (1.5R target, stop at displacement extreme + 0.15*ATR buffer).
_S = {'last_processed_idx': -1}

def evaluate(bars, params):
    n = len(bars)
    if n < 30: return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']: return None
    _S['last_processed_idx'] = cur_i

    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.2))
    body_ratio_min = float(params.get('body_ratio_min', 0.65))
    pullback_pct = float(params.get('pullback_pct', 0.40))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.15))

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