# ICT NY Macro Windows + CISD FVG strategy for 6J
# Source: YT:p-3gVwrpIRY (Faith Trades - ICT NY Session Macros)
# 5 NY macro windows (UTC):
#   AM1:  13:50-14:10
#   AM2:  14:50-15:10
#   Lunch: 15:50-16:10
#   PM1:  17:10-17:40
#   PM2:  19:15-19:45
#
# Pattern per window:
#   - Look for sweep of recent swing high/low (over prior N bars)
#   - Confirm CISD (close back on opposite side)
#   - Enter at close; SL beyond sweep wick; TP at target_r * risk
#
# 6J is the priority uncovered symbol. The CISD 15m baseline got n=3 in 20d (rejected).
# This 5m version with macro-window gating may produce different frequency.

_S = {'last_processed_idx': -1, 'last_signal_i': -10}

# NY Macro windows (start_minute, end_minute) in UTC
_MACRO_WINDOWS = [
    (13*60+50, 14*60+10),  # AM1: 13:50-14:10
    (14*60+50, 15*60+10),  # AM2: 14:50-15:10
    (15*60+50, 16*60+10),  # Lunch: 15:50-16:10
    (17*60+10, 17*60+40),  # PM1: 17:10-17:40
    (19*60+15, 19*60+45),  # PM2: 19:15-19:45
]


def _in_macro_window(ts):
    """Check if timestamp falls within any NY macro window. ts is a pandas Timestamp."""
    try:
        # pandas Timestamp has .hour and .minute directly
        h = int(ts.hour)
        m = int(ts.minute)
        minute_of_day = h * 60 + m
        for start, end in _MACRO_WINDOWS:
            if start <= minute_of_day <= end:
                return True
    except Exception:
        return False
    return False


def evaluate(bars, params):
    n = len(bars)
    if n < 30:
        return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']:
        return None
    _S['last_processed_idx'] = cur_i
    if cur_i < 25:
        return None
    if cur_i - _S['last_signal_i'] < 5:
        return None

    swing_lookback = int(params.get('swing_lookback', 8))
    min_range_pct = float(params.get('min_range_pct', 0.0008))
    cisd_min_close_pct = float(params.get('cisd_min_close_pct', 0.30))
    target_r = float(params.get('target_r', 1.5))
    sl_buffer_pct = float(params.get('sl_buffer_pct', 0.0003))

    # Gate: only fire within NY Macro windows
    cur_ts = bars.index[cur_i]
    if not _in_macro_window(cur_ts):
        return None

    # Find swing high/low over prior 2*lookback bars
    start_i = max(0, cur_i - swing_lookback * 2)
    window_highs = bars['high'].iloc[start_i:cur_i].values.astype(float)
    window_lows = bars['low'].iloc[start_i:cur_i].values.astype(float)
    swing_high = float(np.max(window_highs))
    swing_low = float(np.min(window_lows))
    range_size = swing_high - swing_low

    last_close = float(bars['close'].iloc[cur_i])
    if range_size < last_close * min_range_pct:
        return None

    cur_high = float(bars['high'].iloc[cur_i])
    cur_low = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    buf = last_close * sl_buffer_pct

    # SHORT: sweep above swing_high + CISD (close back below)
    if cur_high > swing_high and cur_close < swing_high:
        bar_range = cur_high - cur_low
        if bar_range > 0:
            close_pct = (cur_high - cur_close) / bar_range
            if close_pct >= cisd_min_close_pct:
                entry = cur_close
                risk = (cur_high + buf) - entry
                if risk > 0:
                    sl = entry + risk
                    tp = entry - risk * target_r
                    _S['last_signal_i'] = cur_i
                    return {'direction': 'short', 'entry': entry, 'stop': sl, 'target': tp}

    # LONG: sweep below swing_low + CISD (close back above)
    if cur_low < swing_low and cur_close > swing_low:
        bar_range = cur_high - cur_low
        if bar_range > 0:
            close_pct = (cur_close - cur_low) / bar_range
            if close_pct >= cisd_min_close_pct:
                entry = cur_close
                risk = entry - (cur_low - buf)
                if risk > 0:
                    sl = entry - risk
                    tp = entry + risk * target_r
                    _S['last_signal_i'] = cur_i
                    return {'direction': 'long', 'entry': entry, 'stop': sl, 'target': tp}
    return None