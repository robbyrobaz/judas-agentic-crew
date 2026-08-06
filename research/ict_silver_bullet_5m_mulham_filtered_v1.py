"""
ICT Silver Bullet 5m — Mulham 3-layer filter:
1. Inside Silver Bullet session (10-11am ET, 2-3pm ET, 3-4am ET London)
2. First FVG in the session (skip subsequent FVGs)
3. Extreme FVG position — the FVG must be on the deeper-discount/premium side
   of the impulsive move (closer to the 0%/100% extremes, not 50% mid)

Source: YT:6ZMZcChkoHo (Mulham Trading Silver Bullet 13:19)
- "FVG inside SB session = higher probability than outside"
- "First FVG in the session = highest probability"
- "Extreme FVG (deepest retracement) = highest probability"

Architecture: 3-candle FVG detection (c1=before, c2=displacement, c3=after).
On c3 close: detect FVG, check it's the first since session start, compute retracement
depth relative to the impulsive move (c1.low → c3.high for bull, c1.high → c3.low for bear).

Symbol-aware defaults (when no params passed):
  MGC: tick=0.10, min_gap=0.20, target_r=2.0
  MNQ: tick=0.25, min_gap=0.50, target_r=1.5
  MCL: tick=0.01, min_gap=0.02, target_r=2.0
  MBT: tick=5.0,  min_gap=10.0, target_r=2.5
  MET: tick=0.50, min_gap=1.0,  target_r=1.5
  DX:  tick=0.005,min_gap=0.01, target_r=1.5
  ZF:  tick=0.015625,min_gap=0.03125, target_r=1.5
  6J:  tick=0.0000005, min_gap=0.000001, target_r=1.5

Session detection (UTC):
  - 14:00-15:00 UTC = 10:00-11:00 ET (NY AM Silver Bullet)
  - 18:00-19:00 UTC = 14:00-15:00 ET (NY PM Silver Bullet)
  - 08:00-09:00 UTC = 03:00-04:00 ET (London Silver Bullet)
"""

_S = {
    'fired_this_window': 0,
    'last_window_id': None,
    'last_processed_idx': -1,
    'fvg_count_this_window': 0,
    'last_window_fvg_id': None,
}


# Symbol-specific defaults — auto-detected from price magnitude if no params given
SYMBOL_DEFAULTS = {
    'MGC': {'tick_size': 0.10, 'min_gap': 0.20, 'target_r': 2.0, 'min_depth': 0.30, 'stop_buffer': 0.20},
    'MNQ': {'tick_size': 0.25, 'min_gap': 0.50, 'target_r': 1.5, 'min_depth': 0.30, 'stop_buffer': 0.50},
    'MCL': {'tick_size': 0.01, 'min_gap': 0.02, 'target_r': 2.0, 'min_depth': 0.30, 'stop_buffer': 0.02},
    'MBT': {'tick_size': 5.0,  'min_gap': 10.0, 'target_r': 2.5, 'min_depth': 0.30, 'stop_buffer': 10.0},
    'MET': {'tick_size': 0.50, 'min_gap': 1.0,  'target_r': 1.5, 'min_depth': 0.30, 'stop_buffer': 1.0},
    'DX':  {'tick_size': 0.005,'min_gap': 0.01, 'target_r': 1.5, 'min_depth': 0.30, 'stop_buffer': 0.01},
    'ZF':  {'tick_size': 0.015625,'min_gap': 0.03125,'target_r': 1.5,'min_depth': 0.30,'stop_buffer': 0.03125},
    '6J':  {'tick_size': 0.0000005, 'min_gap': 0.000001, 'target_r': 1.5, 'min_depth': 0.30, 'stop_buffer': 0.000001},
}


def _detect_symbol_defaults(bars, params):
    """Auto-detect symbol from last close price if symbol param not given."""
    sym = params.get('symbol', None)
    if sym is not None and sym in SYMBOL_DEFAULTS:
        return SYMBOL_DEFAULTS[sym]
    # Heuristic: pick the closest SYMBOL_DEFAULTS match based on last close
    try:
        last_close = float(bars['close'].iloc[-1])
    except Exception:
        return SYMBOL_DEFAULTS['MGC']
    if last_close > 10000:
        return SYMBOL_DEFAULTS['6J']  # yen in micro-units
    if last_close > 3000:
        return SYMBOL_DEFAULTS['MNQ']
    if last_close > 1000:
        return SYMBOL_DEFAULTS['MGC']
    if last_close > 100:
        return SYMBOL_DEFAULTS['MET']
    if last_close > 10:
        return SYMBOL_DEFAULTS['DX']
    if last_close > 1:
        return SYMBOL_DEFAULTS['ZF']
    if last_close > 0.5:
        return SYMBOL_DEFAULTS['MCL']
    return SYMBOL_DEFAULTS['MBT']


def evaluate(bars, params):
    n = len(bars)
    if n < 5:
        return None
    cur_i = n - 1
    if cur_i == _S['last_processed_idx']:
        return None
    _S['last_processed_idx'] = cur_i

    defaults = _detect_symbol_defaults(bars, params)
    tick = float(params.get("tick_size", defaults['tick_size']))
    min_gap = float(params.get("min_gap", defaults['min_gap']))
    target_r = float(params.get("target_r", defaults['target_r']))
    min_depth = float(params.get("min_depth", defaults['min_depth']))
    stop_buffer = float(params.get("stop_buffer", defaults['stop_buffer']))

    cur_ts = bars["ts"].iloc[cur_i]
    hour = cur_ts.hour
    if hour == 14:
        window_id = f"{cur_ts.date()}_AM"
    elif hour == 18:
        window_id = f"{cur_ts.date()}_PM"
    elif hour == 8:
        window_id = f"{cur_ts.date()}_LON"
    else:
        return None

    if window_id != _S['last_window_id']:
        _S['fired_this_window'] = 0
        _S['fvg_count_this_window'] = 0
        _S['last_window_id'] = window_id

    c1 = bars.iloc[cur_i - 2]
    c2 = bars.iloc[cur_i - 1]
    c3 = bars.iloc[cur_i]
    c1h, c1l = float(c1["high"]), float(c1["low"])
    c2o, c2c = float(c2["open"]), float(c2["close"])
    c2_high, c2_low = float(c2["high"]), float(c2["low"])
    c3h, c3l = float(c3["high"]), float(c3["low"])

    c2_body = abs(c2c - c2o)
    c2_range = c2_high - c2_low
    body_ratio = c2_body / c2_range if c2_range > 0 else 0.0
    min_body_ratio = 0.5

    if c1h < c3l and (c3l - c1h) >= min_gap and c2c > c2o and body_ratio >= min_body_ratio:
        move_range = c3h - c1l
        if move_range <= 0:
            return None
        fvg_top = c3l
        fvg_bottom = c1h
        retracement_pos = (fvg_top - c1l) / move_range
        if retracement_pos > min_depth:
            return None
        if _S['fvg_count_this_window'] >= 1:
            return None
        entry = fvg_top
        stop = c1l - stop_buffer
        risk = entry - stop
        if risk > 0:
            target = entry + target_r * risk
            _S['fvg_count_this_window'] += 1
            _S['fired_this_window'] += 1
            return {"direction": "long", "entry": entry, "stop": stop, "target": target}

    if c1l > c3h and (c1l - c3h) >= min_gap and c2c < c2o and body_ratio >= min_body_ratio:
        move_range = c1h - c3l
        if move_range <= 0:
            return None
        fvg_top = c1l
        fvg_bottom = c3h
        retracement_pos = (c1h - fvg_bottom) / move_range
        if retracement_pos > min_depth:
            return None
        if _S['fvg_count_this_window'] >= 1:
            return None
        entry = fvg_bottom
        stop = c1h + stop_buffer
        risk = stop - entry
        if risk > 0:
            target = entry - target_r * risk
            _S['fvg_count_this_window'] += 1
            _S['fired_this_window'] += 1
            return {"direction": "short", "entry": entry, "stop": stop, "target": target}
    return None