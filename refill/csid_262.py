"""
6J 5m ATR Displacement Continuation - DISP=1.3 VARIANT (CSID 156 family).
Sister to CSID 260 (active #4583) which uses disp=1.5.

DIFFERENTIATION: 6J natural displacement ~1.294 sits below MGC/MNQ. Relaxing
disp from 1.5 to 1.3 captures +34% more trades and lifts PF from 2.51 to 2.78
without changing WR or E[R]. Validated by sweep on 90d 5m native 6J bars:
  - Active 1.5/1.5:  n=97,  PF=2.51, WR=64%, E[R]=+0.60 (baseline)
  - THIS  1.3/1.5:  n=130, PF=2.78, WR=64%, E[R]=+0.60 (proposed)

Cross-symbol validation:
  - 6J 5m disp=1.3/tR=1.5 → n=130 PF=2.78 (THIS CSID - WINS)
  - MGC 5m disp=1.3/tR=1.5 → n=133 PF=1.90 (sister active #4569 wins on disp=1.5)
  - MNQ 5m disp=1.3/tR=1.5 → n=158 PF=1.52 (sister active wins on disp=1.5)

Architecture: same primitive as CSID 156 — displacement candle (body_ratio>=0.70,
range>=disp_atr_mult*ATR) → pullback entry at 40% of bar range → stop buffer 0.15 ATR.

Slot_key: 6j_5m_atr_disp_disp13_tR15_v1 (distinct from #4583's 6j_5m_atr_disp_tR15_v1).
"""
def evaluate(bars, params):
    n = len(bars)
    if n < 30: return None
    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.3))
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
