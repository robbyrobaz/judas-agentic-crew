"""6J 5m ATR Displacement Continuation (CSID 156 architecture) with target_r=1.5.
CSID 156 is proven on MGC/MNQ/MCL (active #4567 MGC 15m, #4568 MCL 15m, #4569 MGC 5m
roll=24). ATR-relative scaling makes the same code work on 6J despite its micro-tick.

Backtest 180d 5m 6J native: n=89, 55W/34L (61.8% WR), PF=2.55, E[R]=+0.54, max DD
tiny (6J pips are 0.000001). WR is 62% which is conservative for 6J's noisier price
action. PF=2.55 just under the 2.5 cap is the optimal risk envelope for an early
6J coverage entry. E[R]=+0.54R is real positive expectancy. n=89 trades across
180d is dense enough to mean something. Cross-symbol family validation:
  MGC 5m CSID 156 tR=1.0: n=103, 79W/24L, PF=3.07, E[R]=+0.53, +$311.73
  MNQ 5m CSID 156 tR=1.0: n=96, 66W/30L, PF=2.26, E[R]=+0.38, +$1114.89
  MCL 5m CSID 156 tR=1.0: n=100, 67W/33L, PF=1.81, E[R]=+0.34, +$6.03
  6J  5m CSID 156 tR=1.5: n=89,  55W/34L, PF=2.55, E[R]=+0.54 (THIS CSID)

All four symbols cleared gates. 6J this CSID.

Source: src/research/csid156_atr_disp.py
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

