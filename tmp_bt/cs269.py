"""ZF 15m CSID 156 ATR Displacement Continuation - DIFFERENTIATED variant.

Architecture mirrors CSID 156 (proven on MGC/MNQ/MCL/6J with PF 1.81-3.07):
- 14-period ATR
- Displacement candle: range >= 1.3x ATR(14), body/range >= 0.75
- 40% pullback entry of the displacement range
- Stop beyond displacement extreme + 0.50x ATR buffer (DIFFERENT from retired #4591's 0.15)
- Target 1.5R
- side_filter = long_only (matches retired #4591)

DIFFERENTIATION FROM RETIRED #4591 (CSID 156 ZF 15m disp=1.3 tR=1.5 stop=0.15):
1. stop_buffer_atr = 0.50 (vs 0.15) - wider stop buffer gives the trade more room
   to survive noise / wicks beyond the displacement extreme, which was the failure
   mode observed in the duplicate-fire audit (finding a60cd4bd): 6 paired fires
   with 3 losing trades totaling ~-$179 of variance pain concentrated in
   noise-stop events on the tighter 0.15 buffer.
2. Self-cooldown via module-state last_fire_idx. If cur_i - last_fire_idx < cooldown_bars
   (default 2 bars = 30 minutes on 15m), no signal is emitted. This directly
   addresses the same-bar duplicate-fire pattern that retired #4591 (paired
   with #4607 CSID 268 strict variant). Even if a sibling strategy fires on the
   same bar, THIS strategy will not co-fire within 30 minutes.

Source: CSID 156 backbone (proven on MGC/MNQ/MCL/6J), ZF 15m port with
guard per operator escalation finding 4364b0a0 + registrar finding ab713f10.
"""
_S = {'last_processed_idx': -1, 'last_fire_idx': -1000}


def evaluate(bars, params):
    n = len(bars)
    if n < 30:
        return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']:
        return None
    _S['last_processed_idx'] = cur_i

    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.3))
    body_ratio_min = float(params.get('body_ratio_min', 0.75))
    pullback_pct = float(params.get('pullback_pct', 0.40))
    target_r = float(params.get('target_r', 1.5))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.50))
    cooldown_bars = int(params.get('cooldown_bars', 2))  # 2*15m = 30 min guard

    # CO-FIRE GUARD: skip if a fire happened within cooldown_bars
    if cur_i - _S['last_fire_idx'] < cooldown_bars:
        return None

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
               abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    if len(trs) < atr_period:
        return None
    atr = sum(trs[-atr_period:]) / atr_period
    if atr <= 0:
        return None

    disp_i = n - 1
    o = float(bars['open'].iloc[disp_i])
    h = float(bars['high'].iloc[disp_i])
    l = float(bars['low'].iloc[disp_i])
    cl = float(bars['close'].iloc[disp_i])
    rng = h - l
    body = abs(cl - o)
    if rng <= 0:
        return None
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * atr:
        return None

    is_bull = cl > o
    # side_filter = long_only: skip shorts
    if not is_bull:
        return None

    entry = h - pullback_pct * rng
    stop = l - stop_buffer_atr * atr
    risk = entry - stop
    if risk <= 0:
        return None
    target = entry + target_r * risk

    # Mark fire (only after we have decided to emit a signal)
    _S['last_fire_idx'] = cur_i
    return {"direction": "long", "entry": entry, "stop": stop, "target": target}
