"""LRM v2 — Abe Maruta magnet-attraction setup + HTF bias filter.

v1 failed E[R] gate on 15m (PF 1.6-1.85 with E[R]≈0). Hypothesis: a directional
filter from a longer lookback will boost WR enough to flip E[R] positive.

Architecture:
  1. Compute HTF trend: ratio of (current_price - SMA_long) / ATR_long
       If positive → HTF_bullish → only take LONG setups
       If negative → HTF_bearish → only take SHORT setups
       Magnitude threshold: |HTF_trend| > htf_min_strength to filter ranging regimes
  2. Otherwise identical magnet-attraction logic to v1

HTF SMA = 80 bars (vs the swing_window = 20). On 15m bars this is ~20 hours
(~3 RTH sessions) which captures multi-session directional bias.
"""


def evaluate(bars, params):
    if bars is None or len(bars) < 100:
        return None

    swing_window = int(params.get("swing_window", 20))
    proximity_atr_frac = float(params.get("proximity_atr_frac", 0.5))
    avoid_min_dist_atr = float(params.get("avoid_min_dist_atr", 0.0))
    reversal_body_min = float(params.get("reversal_body_min", 0.55))
    reapproach_bars_max = int(params.get("reapproach_bars_max", 6))
    reapproach_progress = float(params.get("reapproach_progress", 0.0))
    target_r_floor = float(params.get("target_r_floor", 0.8))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.25))
    atr_period = int(params.get("atr_period", 14))
    session_filter = params.get("session_filter", "none")
    # HTF bias
    htf_sma_period = int(params.get("htf_sma_period", 80))
    htf_atr_period = int(params.get("htf_atr_period", 80))
    htf_min_strength = float(params.get("htf_min_strength", 0.0))  # |price-SMA|/ATR must exceed
    allow_counter_trend = bool(params.get("allow_counter_trend", False))  # off by default

    h = bars["high"].astype(float).values
    l = bars["low"].astype(float).values
    c = bars["close"].astype(float).values
    o = bars["open"].astype(float).values
    n = len(c)
    min_bars = max(swing_window, htf_sma_period, htf_atr_period) + reapproach_bars_max + 10
    if n < min_bars:
        return None

    # Session filter
    if session_filter != "none":
        try:
            hr = int(bars.index[-1].hour)
        except Exception:
            hr = -1
        if session_filter == "ny" and not (12 <= hr <= 20):
            return None
        if session_filter == "london" and not (7 <= hr <= 11):
            return None

    # ATR (Wilder)
    tr = [0.0] * n
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr_v = [0.0] * n
    atr_v[atr_period - 1] = sum(tr[:atr_period]) / atr_period
    for i in range(atr_period, n):
        atr_v[i] = (atr_v[i - 1] * (atr_period - 1) + tr[i]) / atr_period
    atr_now = atr_v[-1]
    if atr_now <= 0:
        return None

    # HTF trend: simple SMA + long-window ATR
    htf_sma = sum(c[-htf_sma_period:]) / htf_sma_period
    htf_atr = sum(tr[-htf_atr_period:]) / htf_atr_period
    if htf_atr <= 0:
        htf_trend = 0.0
    else:
        htf_trend = (c[-1] - htf_sma) / htf_atr  # signed, in ATR units
    htf_bullish = htf_trend > htf_min_strength
    htf_bearish = htf_trend < -htf_min_strength

    # Find swing low in lookback
    hi_idx = max(0, n - swing_window - 1)
    lo_idx = n - 2
    if hi_idx >= lo_idx:
        recent_low_idx = lo_idx
        recent_high_idx = lo_idx
    else:
        recent_low_idx = min(range(hi_idx, lo_idx + 1), key=lambda i: l[i])
        recent_high_idx = max(range(hi_idx, lo_idx + 1), key=lambda i: h[i])
    swing_low = l[recent_low_idx]
    swing_high = h[recent_high_idx]

    # SHORT setup
    if htf_bearish or allow_counter_trend:
        rev_idx = -1
        for i in range(n - 2, hi_idx, -1):
            if l[i] <= swing_low:
                continue
            dist_to_swing = l[i] - swing_low
            if dist_to_swing > proximity_atr_frac * atr_now:
                continue
            if dist_to_swing < avoid_min_dist_atr * atr_now:
                continue
            rng = h[i] - l[i]
            if rng <= 0:
                continue
            body = c[i] - o[i]
            if body <= 0:
                continue
            upper_pos = (c[i] - l[i]) / rng
            if upper_pos < reversal_body_min:
                continue
            rev_idx = i
            break
        if rev_idx != -1:
            rev_low = l[rev_idx]
            for i in range(rev_idx + 1, n):
                rng_i = h[i] - l[i]
                if rng_i <= 0:
                    continue
                if c[i] >= o[i]:
                    continue
                new_dist = l[i] - swing_low
                old_dist = rev_low - swing_low
                if new_dist > old_dist - reapproach_progress * atr_now:
                    continue
                if i - rev_idx > reapproach_bars_max:
                    continue
                target_dist = c[i] - swing_low
                rev_high = h[rev_idx]
                risk = rev_high + stop_buf_atr * atr_now - c[i]
                if risk <= 0:
                    continue
                if target_dist < target_r_floor * atr_now:
                    continue
                target_by_r = c[i] - 1.5 * risk
                target = max(swing_low, target_by_r)
                return {
                    "direction": "short",
                    "entry": float(c[i]),
                    "stop": float(rev_high + stop_buf_atr * atr_now),
                    "target": float(target),
                }

    # LONG setup
    if htf_bullish or allow_counter_trend:
        rev_idx = -1
        for i in range(n - 2, hi_idx, -1):
            if h[i] >= swing_high:
                continue
            dist = swing_high - h[i]
            if dist > proximity_atr_frac * atr_now:
                continue
            if dist < avoid_min_dist_atr * atr_now:
                continue
            rng = h[i] - l[i]
            if rng <= 0:
                continue
            body = o[i] - c[i]
            if body <= 0:
                continue
            lower_pos = (h[i] - c[i]) / rng
            if lower_pos < reversal_body_min:
                continue
            rev_idx = i
            break
        if rev_idx != -1:
            rev_high = h[rev_idx]
            for i in range(rev_idx + 1, n):
                rng_i = h[i] - l[i]
                if rng_i <= 0:
                    continue
                if c[i] <= o[i]:
                    continue
                new_dist = swing_high - h[i]
                old_dist = swing_high - rev_high
                if new_dist > old_dist - reapproach_progress * atr_now:
                    continue
                if i - rev_idx > reapproach_bars_max:
                    continue
                target_dist = swing_high - c[i]
                rev_low = l[rev_idx]
                risk = c[i] - (rev_low - stop_buf_atr * atr_now)
                if risk <= 0:
                    continue
                if target_dist < target_r_floor * atr_now:
                    continue
                target_by_r = c[i] + 1.5 * risk
                target = min(swing_high, target_by_r)
                return {
                    "direction": "long",
                    "entry": float(c[i]),
                    "stop": float(rev_low - stop_buf_atr * atr_now),
                    "target": float(target),
                }

    return None
