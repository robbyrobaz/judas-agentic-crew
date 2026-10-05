"""ICT Post-Sweep FVG Midpoint Reversion (5m) - research artifact"""
_S = {'last_signal_key': None}


def evaluate(bars, params):
    n = len(bars)
    if n < 50: return None

    atr_period = int(params.get('atr_period', 14))
    swing_lookback = int(params.get('swing_lookback', 15))
    pivot_bars = int(params.get('pivot_bars', 2))
    sweep_min_range_atr = float(params.get('sweep_min_range_atr', 0.8))
    sweep_max_age_bars = int(params.get('sweep_max_age_bars', 12))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.10))
    target_r = float(params.get('target_r', 2.0))
    killzone_filter = bool(params.get('killzone_filter', False))
    max_fvg_distance_atr = float(params.get('max_fvg_distance_atr', 6.0))

    h = bars['high'].values.astype(float)
    l = bars['low'].values.astype(float)
    c = bars['close'].values.astype(float)
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
            if not (13 <= hour < 17):
                return None
        except Exception:
            pass

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

    scan_start = max(pivot_bars * 2 + 1, cur_i - sweep_max_age_bars)
    scan_end = cur_i - 3

    sweep_event = None
    for i in range(scan_end, scan_start - 1, -1):
        rng = h[i] - l[i]
        if rng < sweep_min_range_atr * atr:
            continue
        if l[i] < swing_low and c[i] > swing_low and swing_low_i < i - 1:
            sweep_event = ('long', i, float(l[i]))
            break
        if h[i] > swing_high and c[i] < swing_high and swing_high_i < i - 1:
            sweep_event = ('short', i, float(h[i]))
            break

    if sweep_event is None:
        return None

    direction, sweep_i, sweep_extreme = sweep_event
    fvg_event = None
    for fvg_end in range(sweep_i + 3, cur_i + 1):
        i = fvg_end - 2
        if direction == 'long':
            gap_low = float(l[fvg_end])
            gap_high = float(h[i])
            if gap_high < gap_low - 1e-9:
                gap_mid = (gap_high + gap_low) / 2.0
                if sweep_extreme - 1e-9 < gap_mid < (sweep_extreme + max_fvg_distance_atr * atr):
                    fvg_event = (gap_mid, sweep_extreme - stop_buf_atr * atr)
                    break
        else:
            gap_high = float(h[fvg_end])
            gap_low = float(l[i])
            if gap_low > gap_high + 1e-9:
                gap_mid = (gap_low + gap_high) / 2.0
                if (sweep_extreme - max_fvg_distance_atr * atr) < gap_mid < (sweep_extreme + 1e-9):
                    fvg_event = (gap_mid, sweep_extreme + stop_buf_atr * atr)
                    break

    if fvg_event is None:
        return None

    entry, stop = fvg_event
    risk = abs(stop - entry)
    if risk <= 0 or risk > 3.0 * atr:
        return None

    sig_key = (sweep_i, fvg_end)
    if _S['last_signal_key'] == sig_key:
        return None
    _S['last_signal_key'] = sig_key

    if direction == 'long':
        target = entry + target_r * risk
    else:
        target = entry - target_r * risk

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}
