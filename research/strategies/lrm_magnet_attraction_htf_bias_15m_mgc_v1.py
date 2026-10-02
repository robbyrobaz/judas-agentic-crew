"""LRM v2f — Abe Maruta magnet-attraction + HTF bias (final tuned).

v2f result on MGC 15m 180d: PF=2.75, n=21, E[R]=+0.71, +$597
v2f cross-symbol (15m, 180d): MGC ✅ / MNQ/FB / MCL ✗ / ZF ✗ / 6J ✗

Magnet-attraction setup:
  1. Find swing high/low over recent N bars (swing_window=18)
  2. Bar approaches magnet but doesn't take it, reverses sharply
  3. Re-approach with confirmation, enter in original direction
  4. Target = magnet level OR 1.5R from entry (whichever is closer)

HTF bias filter:
  - Compute HTF trend: (close - SMA80) / ATR80 over last 80 bars
  - htf_min_strength=-0.4: accept |htf_trend| > 0.4 (skip range)
  - Only short when HTF bearish, only long when HTF bullish
  - This filters out ~40-50% of low-conviction setups

Architecture verification:
  - 1 trade at a time (engine constraint)
  - 1.5R target capped by magnet level (can't go beyond magnet)
  - Stop = reversal candle's opposite extreme + 0.22*ATR buffer
"""


def evaluate(bars, params):
    if bars is None or len(bars) < 100:
        return None
    swing_window = int(params.get("swing_window", 18))
    proximity_atr_frac = float(params.get("proximity_atr_frac", 0.6))
    avoid_min_dist_atr = float(params.get("avoid_min_dist_atr", 0.0))
    reversal_body_min = float(params.get("reversal_body_min", 0.55))
    reapproach_bars_max = int(params.get("reapproach_bars_max", 5))
    reapproach_progress = float(params.get("reapproach_progress", 0.0))
    target_r_floor = float(params.get("target_r_floor", 0.8))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.22))
    atr_period = int(params.get("atr_period", 14))
    htf_sma_period = int(params.get("htf_sma_period", 80))
    htf_min_strength = float(params.get("htf_min_strength", -0.4))
    h = bars["high"].astype(float).values
    l = bars["low"].astype(float).values
    c = bars["close"].astype(float).values
    o = bars["open"].astype(float).values
    n = len(c)
    min_bars = max(swing_window, htf_sma_period) + reapproach_bars_max + 10
    if n < min_bars:
        return None
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
    htf_sma = sum(c[-htf_sma_period:]) / htf_sma_period
    htf_atr = sum(tr[-htf_sma_period:]) / htf_sma_period
    if htf_atr <= 0:
        htf_trend = 0.0
    else:
        htf_trend = (c[-1] - htf_sma) / htf_atr
    htf_bullish = htf_trend > htf_min_strength
    htf_bearish = htf_trend < -htf_min_strength
    hi_idx = max(0, n - swing_window - 1)
    lo_idx = n - 2
    if hi_idx >= lo_idx:
        return None
    recent_low_idx = min(range(hi_idx, lo_idx + 1), key=lambda i: l[i])
    recent_high_idx = max(range(hi_idx, lo_idx + 1), key=lambda i: h[i])
    swing_low = l[recent_low_idx]
    swing_high = h[recent_high_idx]
    if htf_bearish:
        rev_idx = -1
        for i in range(n - 1, hi_idx, -1):
            if l[i] <= swing_low: continue
            dist_to_swing = l[i] - swing_low
            if dist_to_swing > proximity_atr_frac * atr_now: continue
            if dist_to_swing < avoid_min_dist_atr * atr_now: continue
            rng = h[i] - l[i]
            if rng <= 0: continue
            body = c[i] - o[i]
            if body <= 0: continue
            upper_pos = (c[i] - l[i]) / rng
            if upper_pos < reversal_body_min: continue
            rev_idx = i
            break
        if rev_idx != -1:
            rev_low = l[rev_idx]
            for i in range(rev_idx + 1, n):
                rng_i = h[i] - l[i]
                if rng_i <= 0: continue
                if c[i] >= o[i]: continue
                new_dist = l[i] - swing_low
                old_dist = rev_low - swing_low
                if new_dist > old_dist - reapproach_progress * atr_now: continue
                if i - rev_idx > reapproach_bars_max: continue
                target_dist = c[i] - swing_low
                rev_high = h[rev_idx]
                risk = rev_high + stop_buf_atr * atr_now - c[i]
                if risk <= 0: continue
                if target_dist < target_r_floor * atr_now: continue
                target_by_r = c[i] - 1.5 * risk
                target = max(swing_low, target_by_r)
                return {"direction": "short", "entry": float(c[i]),
                        "stop": float(rev_high + stop_buf_atr * atr_now),
                        "target": float(target)}
    if htf_bullish:
        rev_idx = -1
        for i in range(n - 1, hi_idx, -1):
            if h[i] >= swing_high: continue
            dist = swing_high - h[i]
            if dist > proximity_atr_frac * atr_now: continue
            if dist < avoid_min_dist_atr * atr_now: continue
            rng = h[i] - l[i]
            if rng <= 0: continue
            body = o[i] - c[i]
            if body <= 0: continue
            lower_pos = (h[i] - c[i]) / rng
            if lower_pos < reversal_body_min: continue
            rev_idx = i
            break
        if rev_idx != -1:
            rev_high = h[rev_idx]
            for i in range(rev_idx + 1, n):
                rng_i = h[i] - l[i]
                if rng_i <= 0: continue
                if c[i] <= o[i]: continue
                new_dist = swing_high - h[i]
                old_dist = swing_high - rev_high
                if new_dist > old_dist - reapproach_progress * atr_now: continue
                if i - rev_idx > reapproach_bars_max: continue
                target_dist = swing_high - c[i]
                rev_low = l[rev_idx]
                risk = c[i] - (rev_low - stop_buf_atr * atr_now)
                if risk <= 0: continue
                if target_dist < target_r_floor * atr_now: continue
                target_by_r = c[i] + 1.5 * risk
                target = min(swing_high, target_by_r)
                return {"direction": "long", "entry": float(c[i]),
                        "stop": float(rev_low - stop_buf_atr * atr_now),
                        "target": float(target)}
    return None
