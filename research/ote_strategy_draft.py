"""
ICT OTE (Optimal Trade Entry) Reversion — 5m / 15m.

Architecture (per FXNX 2026 OTE framework + finding 6f154881):

  1. DISPLACEMENT (mandatory filter):
     - Find recent swing high/low (pivot, 2-bar either side).
     - A displacement is a bar that CLOSES THROUGH the prior swing point
       in the opposite direction, with body_ratio >= body_ratio_min and
       range >= disp_atr_mult * ATR(14).
  2. DEALING RANGE:
     - DOWN displacement: range = swing_high - displacement_low
     - UP displacement: range = displacement_high - swing_low
  3. OTE ZONE: 62-79% retracement of dealing range from the displacement
     extreme toward the origin.
  4. ENTRY at 70.5% (sweet spot midpoint). Stop beyond 100% (origin side).
     Target back at the displacement extreme. RR ~ 2.4R (favorable).
  5. KILL-ZONE FILTER: NY (13-16 UTC) or London (08-11 UTC).
  6. HTF BIAS (optional, default ON): only enter with HTF agreement.
"""
import numpy as np
import pandas as pd


def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def evaluate(bars, params):
    if bars is None or len(bars) < 80:
        return None

    struct_lookback = int(params.get("struct_lookback", 30))
    disp_lookback = int(params.get("disp_lookback", 20))
    body_ratio_min = float(params.get("body_ratio_min", 0.55))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.0))
    fib_low = float(params.get("fib_low", 0.62))
    fib_high = float(params.get("fib_high", 0.79))
    entry_fib = float(params.get("entry_fib", 0.705))
    tick = float(params.get("tick", 0.10))
    atr_period = int(params.get("atr_period", 14))
    stop_buf_ticks = float(params.get("stop_buf_ticks", 4))
    use_killzone = int(params.get("use_killzone", 1))
    session_ny_start = int(params.get("session_ny_start", 13))
    session_ny_end = int(params.get("session_ny_end", 16))
    session_ldn_start = int(params.get("session_ldn_start", 8))
    session_ldn_end = int(params.get("session_ldn_end", 11))
    use_htf_bias = int(params.get("use_htf_bias", 1))
    htf_ema_period = int(params.get("htf_ema_period", 60))

    n = len(bars)
    cur_i = n - 1

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)

    # Time filter
    try:
        cur_ts = pd.to_datetime(bars["ts"].iloc[cur_i], utc=True, errors="coerce")
        if pd.isna(cur_ts):
            return None
        hr = cur_ts.hour
    except Exception:
        return None

    if use_killzone == 1:
        in_ny = session_ny_start <= hr < session_ny_end
        in_ldn = session_ldn_start <= hr < session_ldn_end
        if not (in_ny or in_ldn):
            return None

    # ATR
    atr_series = _atr(bars, period=atr_period).values
    if cur_i < atr_period:
        return None
    cur_atr = atr_series[cur_i]
    if np.isnan(cur_atr) or cur_atr <= 0:
        return None

    # Find swing points in struct_lookback (pivots with 2-bar confirmation either side).
    swing_high = -np.inf
    swing_high_i = -1
    swing_low = np.inf
    swing_low_i = -1
    lo_idx = max(2, cur_i - struct_lookback)
    hi_idx = cur_i - 2
    for j in range(hi_idx, lo_idx - 1, -1):
        if j - 2 < 0 or j + 2 >= n:
            continue
        if h[j] >= max(h[j-2], h[j-1], h[j+1], h[j+2]):
            if h[j] > swing_high:
                swing_high = float(h[j])
                swing_high_i = j
        if l[j] <= min(l[j-2], l[j-1], l[j+1], l[j+2]):
            if l[j] < swing_low:
                swing_low = float(l[j])
                swing_low_i = j

    if swing_high_i < 0 or swing_low_i < 0:
        return None
    if swing_high <= swing_low:
        return None

    # Find most recent displacement (within disp_lookback).
    disp_i = -1
    disp_dir = 0
    disp_extreme = 0.0
    disp_origin = 0.0

    for j in range(cur_i - 2, max(0, cur_i - disp_lookback), -1):
        rng_j = h[j] - l[j]
        if rng_j <= 0:
            continue
        body_j = abs(c[j] - o[j])
        if body_j / rng_j < body_ratio_min:
            continue
        atr_j = atr_series[j]
        if np.isnan(atr_j) or atr_j <= 0:
            continue
        if rng_j < disp_atr_mult * atr_j:
            continue
        if c[j] < swing_low and swing_low_i > j:
            disp_i = j
            disp_dir = -1
            disp_extreme = float(l[j])
            disp_origin = swing_high
            break
        if c[j] > swing_high and swing_high_i > j:
            disp_i = j
            disp_dir = 1
            disp_extreme = float(h[j])
            disp_origin = swing_low
            break

    if disp_i < 0:
        return None

    # Compute OTE zone.
    dealing_range = disp_extreme - disp_origin
    if dealing_range <= 0:
        return None

    if disp_dir == -1:
        # DOWN displacement: extreme = low, origin = high
        # OTE zone = extreme + fib * (-dealing_range) [retracement up]
        ote_low = disp_extreme + fib_low * (-dealing_range)
        ote_high = disp_extreme + fib_high * (-dealing_range)
        entry_price = disp_extreme + entry_fib * (-dealing_range)
    else:
        # UP displacement: extreme = high, origin = low
        # OTE zone = origin + fib * dealing_range [retracement up from origin]
        ote_low = disp_origin + fib_low * dealing_range
        ote_high = disp_origin + fib_high * dealing_range
        entry_price = disp_origin + entry_fib * dealing_range

    cur_close = float(c[cur_i])
    buffer = 0.05 * abs(dealing_range)
    if cur_close < ote_low - buffer or cur_close > ote_high + buffer:
        return None

    # HTF bias filter.
    if use_htf_bias == 1:
        if cur_i < htf_ema_period:
            return None
        htf_ema = float(np.mean(c[max(0, cur_i - htf_ema_period):cur_i + 1]))
        if disp_dir == -1 and htf_ema < cur_close:
            return None
        if disp_dir == 1 and htf_ema > cur_close:
            return None

    # Build entry/stop/target.
    if disp_dir == -1:
        # SHORT: stop above origin (swing_high + buf), target at displacement extreme
        stop_price = disp_origin + stop_buf_ticks * tick
        risk = stop_price - entry_price
        if risk <= 0.5 * tick:
            return None
        target_price = disp_extreme
        rr = (entry_price - target_price) / risk
        if rr < 1.0:
            return None
        return {"direction": "short", "entry": entry_price, "stop": stop_price, "target": target_price}
    else:
        # LONG: stop below origin (swing_low - buf), target at displacement extreme
        stop_price = disp_origin - stop_buf_ticks * tick
        risk = entry_price - stop_price
        if risk <= 0.5 * tick:
            return None
        target_price = disp_extreme
        rr = (target_price - entry_price) / risk
        if rr < 1.0:
            return None
        return {"direction": "long", "entry": entry_price, "stop": stop_price, "target": target_price}