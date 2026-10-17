"""Zen HTF FVG after sweep + iFVG (5m) - research artifact
Zen model (4 steps): (1) swing H/L, (2) sweep through level, (3) HTF FVG,
(4) 1m-5m iFVG entry. Stop above sweep wick. Target next liquidity.
Simplified for 5m: HTF FVG = any 5m FVG in past N bars that has formed after
the sweep. iFVG = candle close that closes back THROUGH (violates) the FVG.
"""
_S = {'last_signal_key': None}


def evaluate(bars, params):
    n = len(bars)
    if n < 80: return None

    atr_period = int(params.get('atr_period', 14))
    swing_lookback = int(params.get('swing_lookback', 15))
    pivot_bars = int(params.get('pivot_bars', 2))
    sweep_max_age_bars = int(params.get('sweep_max_age_bars', 8))
    fvg_max_age_bars = int(params.get('fvg_max_age_bars', 12))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.15))
    target_r = float(params.get('target_r', 1.5))
    killzone_filter = bool(params.get('killzone_filter', False))

    h = bars['high'].values.astype(float)
    l = bars['low'].values.astype(float)
    c = bars['close'].values.astype(float)
    o = bars['open'].values.astype(float)
    cur_i = n - 1

    trs = []
    for i in range(max(1, cur_i - atr_period + 1), cur_i + 1):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        trs.append(tr)
    if len(trs) < atr_period: return None
    atr = sum(trs) / len(trs)
    if atr <= 0: return None

    if killzone_filter and 'ts' in bars.columns:
        try:
            ts = bars['ts'].iloc[cur_i]
            hour = int(ts[11:13])
            # NY AM (10-11 EST = 14-15 UTC during winter, 13-14 UTC DST)
            if not (13 <= hour < 17):
                return None
        except Exception:
            pass

    # 1. Find prior swing high and swing low
    swing_high = None
    swing_high_i = -1
    swing_low = None
    swing_low_i = -1
    lo_idx = max(pivot_bars, cur_i - swing_lookback)
    hi_idx = cur_i - pivot_bars - 1
    for j in range(hi_idx, lo_idx - 1, -1):
        if swing_high is None:
            window_h = h[j - pivot_bars:j + pivot_bars + 1]
            if h[j] >= window_h.max() - 1e-9:
                swing_high = float(h[j])
                swing_high_i = j
        if swing_low is None:
            window_l = l[j - pivot_bars:j + pivot_bars + 1]
            if l[j] <= window_l.min() + 1e-9:
                swing_low = float(l[j])
                swing_low_i = j
        if swing_high is not None and swing_low is not None:
            break
    if swing_high is None or swing_low is None:
        return None
    if swing_high <= swing_low:
        return None

    # 2. Detect recent sweep — wick pierces through level (don't require body close beyond)
    scan_start = max(pivot_bars * 2 + 1, cur_i - sweep_max_age_bars)
    scan_end = cur_i
    sweep_event = None
    for i in range(scan_end, scan_start - 1, -1):
        # Sweep of swing high: wick above
        if h[i] > swing_high and swing_high_i < i - 1:
            sweep_event = ('short', i, float(h[i]))
            break
        if l[i] < swing_low and swing_low_i < i - 1:
            sweep_event = ('long', i, float(l[i]))
            break
    if sweep_event is None:
        return None
    direction, sweep_i, sweep_extreme = sweep_event

    # 3. After sweep, find an HTF FVG (in this 5m context, any 5m FVG that formed
    #    between sweep_i+1 and cur_i, NOT yet mitigated on the wrong side).
    fvg_event = None
    fvg_start = max(sweep_i + 2, cur_i - fvg_max_age_bars)
    for fvg_end in range(cur_i - 1, fvg_start - 1, -1):
        i = fvg_end - 2
        if i < max(2, fvg_start - 2):
            break
        if direction == 'short':
            # Bearish FVG: candle[i].low > candle[fvg_end].high (gap down)
            gap_high = float(h[fvg_end])
            gap_low = float(l[i])
            if gap_low > gap_high + 1e-9:
                # Check not mitigated (price hasn't traded back below gap_low)
                mitigated = False
                for k in range(i + 2, fvg_end + 1):
                    if l[k] < gap_high - 1e-9:
                        mitigated = True
                        break
                if not mitigated:
                    # 4. iFVG: at cur_i, candle closes back ABOVE the FVG (gap filled from below)
                    if c[cur_i] > gap_low + 1e-9 and c[cur_i] < gap_low + 0.5 * (gap_low - gap_high):
                        fvg_event = (gap_high, gap_low, fvg_end)
                        break
        else:  # long
            # Bullish FVG: candle[fvg_end].low > candle[i].high (gap up)
            gap_low = float(l[fvg_end])
            gap_high = float(h[i])
            if gap_high < gap_low - 1e-9:
                mitigated = False
                for k in range(i + 2, fvg_end + 1):
                    if h[k] > gap_low + 1e-9:
                        mitigated = True
                        break
                if not mitigated:
                    # iFVG: candle closes back BELOW the FVG (gap filled from above)
                    if c[cur_i] < gap_high - 1e-9 and c[cur_i] > gap_high - 0.5 * (gap_low - gap_high):
                        fvg_event = (gap_high, gap_low, fvg_end)
                        break
    if fvg_event is None:
        return None
    gap_high, gap_low, fvg_end = fvg_event

    entry = float(c[cur_i])
    if direction == 'short':
        stop = sweep_extreme + stop_buf_atr * atr
        risk = stop - entry
        if risk <= 0 or risk > 3.0 * atr: return None
        target = entry - target_r * risk
    else:
        stop = sweep_extreme - stop_buf_atr * atr
        risk = entry - stop
        if risk <= 0 or risk > 3.0 * atr: return None
        target = entry + target_r * risk

    sig_key = (sweep_i, fvg_end, cur_i)
    if _S['last_signal_key'] == sig_key:
        return None
    _S['last_signal_key'] = sig_key

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}