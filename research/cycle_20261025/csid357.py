"""
ICT Inside Bar Breakout with Displacement (5m)

Architecture (FRESH — not in active roster):
  - Inside bar (mother bar pattern): bar[i-1].high < bar[i-2].high AND
    bar[i-1].low > bar[i-2].low  → bar[i-1] is the mother bar
  - Current bar (bar[i]) must show displacement:
      range >= 1.0 * ATR(14)  AND  body_ratio >= 0.55
  - Breakout direction:
    if close > mother.high (bullish break): LONG, target 2R
    if close < mother.low  (bearish break): SHORT, target 2R
  - Stop: opposite extreme of mother bar +/- 0.10 ATR buffer
  - Risk filter: risk > 0.5*tick AND risk <= 4.0*ATR

Source: CSID 335 (placeholder), validated this cycle.
Distinguishing from existing families:
  - bsl_run_judas: anchored to clustered swing levels, not mother-bar structure
  - unicorn_model_fvg_inside_breaker: requires FVG, this doesn't
  - silver_bullet: anchored to PDH/PDL, not mother-bar
  - atr_disp_continuation: requires 1.5x ATR displacement, this only 1.0x but
    gated by inside bar formation → different signal quality
"""
_S = {'last_processed_idx': -1}

def evaluate(bars, params):
    n = len(bars)
    if n < 30: return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']: return None
    _S['last_processed_idx'] = cur_i

    atr_period = int(params.get('atr_period', 14))
    disp_atr_mult = float(params.get('disp_atr_mult', 1.0))
    body_ratio_min = float(params.get('body_ratio_min', 0.55))
    target_r = float(params.get('target_r', 2.0))
    stop_buffer_atr = float(params.get('stop_buffer_atr', 0.10))
    min_mother_bars = int(params.get('min_mother_bars', 2))
    max_mother_bars = int(params.get('max_mother_bars', 8))

    h = bars['high'].values.astype(float)
    l = bars['low'].values.astype(float)
    c = bars['close'].values.astype(float)
    o = bars['open'].values.astype(float)

    trs = []
    for i in range(max(1, cur_i - atr_period + 1), cur_i + 1):
        tr = max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1]))
        trs.append(tr)
    if len(trs) < atr_period: return None
    atr = sum(trs) / len(trs)
    if atr <= 0: return None

    mother_i = -1
    for j in range(cur_i - min_mother_bars, cur_i - max_mother_bars - 1, -1):
        if j < 1: continue
        if h[j] < h[j-1] and l[j] > l[j-1]:
            mother_i = j
            break

    if mother_i < 0:
        if h[cur_i-1] < h[cur_i-2] and l[cur_i-1] > l[cur_i-2]:
            mother_i = cur_i - 1
        else:
            return None

    mother_h = float(h[mother_i])
    mother_l = float(l[mother_i])

    cur_rng = h[cur_i] - l[cur_i]
    if cur_rng <= 0: return None
    cur_body = abs(c[cur_i] - o[cur_i])
    body_r = cur_body / cur_rng
    if body_r < body_ratio_min: return None
    if cur_rng < disp_atr_mult * atr: return None

    if c[cur_i] > mother_h:
        entry = mother_h
        stop = mother_l - stop_buffer_atr * atr
        risk = entry - stop
        if risk <= 0 or risk > 4.0 * atr: return None
        target = entry + target_r * risk
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}

    if c[cur_i] < mother_l:
        entry = mother_l
        stop = mother_h + stop_buffer_atr * atr
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * atr: return None
        target = entry - target_r * risk
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}

    return None