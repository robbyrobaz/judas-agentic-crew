"""
VWAP Reversion (SHORT ONLY) 5m on MGC.

REGIME CONTEXT (2026-09-25):
- MGC has bled -2.15% over last 5d, close 4433 -> 4338
- 7d ledger shows MGC longs bleeding: 11L / 6S, longs avg -$36.18, shorts avg -$2.67
- Existing long-bias strategies (crt_1h, atr_disp) catching falling knives
- A SHORT-ONLY mean reversion aligned with the bearish regime could provide edge

ARCHITECTURE:
- Rolling VWAP over last N bars (default N=24 = 2 hours)
- ATR(14) on 5m close
- Entry SHORT: close > VWAP + k*ATR AND high of last bar > VWAP + k*ATR (rejection)
- Target: VWAP touch (mean reversion) OR fixed 1R if VWAP distance too small
- Stop: entry + 1.5*ATR
- Side filter: SHORT ONLY (regime-aligned with bearish bias)
- Session filter: NY-AM only (14:00-17:00 UTC) — high-volume mean reversion window
"""
def evaluate(bars, params):
    if bars is None or len(bars) < 50:
        return None
    n = len(bars)
    vwap_window = int(params.get('vwap_window', 24))
    atr_period = int(params.get('atr_period', 14))
    entry_k_atr = float(params.get('entry_k_atr', 1.0))
    target_r = float(params.get('target_r', 1.5))
    stop_atr_mult = float(params.get('stop_atr_mult', 1.5))
    tick = float(params.get('tick', 0.1))
    session_start = int(params.get('session_start_hour_utc', 14))
    session_end = int(params.get('session_end_hour_utc', 17))

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    volumes = bars['volume'].values.astype(float)
    ts = bars['ts'].values

    # Need enough data
    if n < max(vwap_window, atr_period) + 5:
        return None

    # Rolling VWAP over last vwap_window bars (exclude current bar to avoid lookahead)
    typical = (highs + lows + closes) / 3.0
    window_slice = slice(-(vwap_window + 1), -1)
    vwap_window_typ = typical[window_slice]
    vwap_window_vol = volumes[window_slice]
    sum_pv = (vwap_window_typ * vwap_window_vol).sum()
    sum_v = vwap_window_vol.sum()
    if sum_v <= 0:
        return None
    vwap = sum_pv / sum_v

    # ATR(atr_period) — Wilder-style, exclude current bar
    if n < atr_period + 5:
        return None
    trs = []
    # Use bars at indices [-atr_period-1, -atr_period, ..., -2] (exclude last)
    for offset in range(1, atr_period + 1):
        idx = -(offset + 1)  # -2, -3, ... -(atr_period+1)
        h = highs[idx]
        l = lows[idx]
        prev_close = closes[idx + 1]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    if len(trs) < atr_period:
        return None
    atr = sum(trs) / len(trs)
    if atr <= 0:
        return None

    # Current bar
    cur_close = closes[-1]
    cur_high = highs[-1]
    cur_ts = ts[-1]
    if hasattr(cur_ts, 'hour'):
        hr = cur_ts.hour
    else:
        hr = int(str(cur_ts)[11:13])

    # Session filter — NY AM (14:00-17:00 UTC = 10:00-13:00 EST)
    if hr < session_start or hr >= session_end:
        return None

    # SHORT only — regime filter
    # Entry: close > VWAP + entry_k_atr * ATR (price extended above VWAP, expect reversion)
    upper_threshold = vwap + entry_k_atr * atr
    if cur_high > upper_threshold and cur_close > vwap:
        entry = upper_threshold + tick
        stop = entry + stop_atr_mult * atr
        r_risk = stop - entry
        if r_risk <= 0 or r_risk > 50 * atr:
            return None
        vwap_target = vwap - tick
        vwap_distance = entry - vwap_target
        if vwap_distance >= 0.5 * r_risk:
            target = vwap_target
        else:
            target = entry - target_r * r_risk
        return {
            'direction': 'short',
            'entry': entry,
            'stop': stop,
            'target': target,
        }
    return None