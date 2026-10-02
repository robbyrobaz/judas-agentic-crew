"""LRM (Liquidity Reaction Model) — Abe Teddy Maruta's magnet-attraction setup.
Concept: untaken liquidity levels act as magnets (attract AND repulse). When
price AVOIDS a swing low/high (bar approaches but doesn't take it, then reverses
sharply) and then re-approaches with a confirmation candle in the original
direction, take the trade with target = the untaken swing level.

States (all evaluated on bars[-1]):
  SHORT setup (target = untaken swing low):
    1. Look back N bars for the lowest swing low (= magnet)
    2. Find a "avoidance + reversal" bar where bar.low > swing_low (didn't take)
       AND bar was a sharp bullish reversal (close > open, close > midpoint)
    3. Confirm bar.bars since the reversal bar shows bearish re-approach
       (current bar.close < current bar.open, and bar.low is closer to swing
       low than the reversal bar's low was — re-approach progress)
    4. Emit SHORT signal with target=swing_low, stop=reversal_bar.high+buf

  LONG setup (mirror): target = untaken swing high; bearish reversal + bullish
  re-approach → LONG entry, stop = reversal bar.low - buf.

Bars expected as pandas DataFrame with cols open/high/low/close (vol optional).
"""
from __future__ import annotations


def evaluate(bars, params):
    if bars is None or len(bars) < 30:
        return None

    # ---- params
    swing_window = int(params.get("swing_window", 20))     # lookback for swing level
    proximity_atr_frac = float(params.get("proximity_atr_frac", 0.5))  # how close is "approach"
    avoid_min_dist_atr = float(params.get("avoid_min_dist_atr", 0.0))   # must be > this from swing
    reversal_body_min = float(params.get("reversal_body_min", 0.55))     # close must be in upper X of range
    reapproach_bars_max = int(params.get("reapproach_bars_max", 6))     # confirmation within N bars
    reapproach_progress = float(params.get("reapproach_progress", 0.0)) # bar.low must be closer to swing than reversal bar.low (frac of ATR)
    target_r_floor = float(params.get("target_r_floor", 0.8))            # min target distance (R units)
    stop_buf_atr = float(params.get("stop_buf_atr", 0.25))               # stop buffer beyond reversal extreme
    atr_period = int(params.get("atr_period", 14))
    session_filter = params.get("session_filter", "none")                 # none|ny|london

    h = bars["high"].astype(float).values
    l = bars["low"].astype(float).values
    c = bars["close"].astype(float).values
    o = bars["open"].astype(float).values
    n = len(c)
    if n < swing_window + reapproach_bars_max + 5:
        return None

    # ---- session filter (UTC hour-of-bar; bars carry ts implicitly via index)
    if session_filter != "none":
        try:
            ts = bars.index  # DatetimeIndex expected for bars
            hr = int(ts[-1].hour)
        except Exception:
            hr = -1
        if session_filter == "ny" and not (12 <= hr <= 20):
            return None
        if session_filter == "london" and not (7 <= hr <= 11):
            return None

    # ---- ATR (Wilder)
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

    # ---- Try SHORT first (target = untaken swing low)
    # swing low = min low over the lookback EXCLUDING the reversal candidate
    # We scan recent bars to find the magnet and reversal pattern

    # Find most recent swing low in lookback
    lo_idx = n - 2  # exclude current bar
    hi_idx = max(0, n - swing_window - 1)
    if hi_idx < lo_idx:
        recent_low_idx = min(range(hi_idx, lo_idx + 1), key=lambda i: l[i])
    else:
        recent_low_idx = lo_idx

    # Find the most recent "avoidance + reversal" bullish bar
    # = bar where low > swing_low + avoid_min_dist_atr*atr AND
    #   (low - swing_low) < proximity_atr_frac*atr AND
    #   close > open AND close > (high+low)/2*reversal_body_min (proxy: close in upper X)
    # Iterate backwards from current bar
    rev_idx = -1
    swing_low = l[recent_low_idx]
    for i in range(n - 2, hi_idx, -1):
        if l[i] <= swing_low:  # took the swing — not avoidance
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
        # Re-approach: between rev_idx+1 and n-1 inclusive, look for a bearish bar
        # that progresses closer to swing_low than rev_idx's bar did.
        rev_low = l[rev_idx]
        for i in range(rev_idx + 1, n):
            rng_i = h[i] - l[i]
            if rng_i <= 0:
                continue
            if c[i] >= o[i]:  # need bearish bar
                continue
            # progress check: bar.low is closer to swing_low than rev_low was
            new_dist = l[i] - swing_low
            old_dist = rev_low - swing_low
            if new_dist > old_dist - reapproach_progress * atr_now:
                continue
            # limit on bars since reversal
            if i - rev_idx > reapproach_bars_max:
                continue
            # min target distance: ensure target_r_floor*atr risk vs target dist
            target_dist = c[i] - swing_low
            rev_high = h[rev_idx]
            risk = rev_high + stop_buf_atr * atr_now - c[i]
            if risk <= 0:
                continue
            if target_dist < target_r_floor * atr_now:
                continue
            # use a tighter target: target = swing_low, OR c[i] - target_r*risk,
            # whichever is more conservative (closer)
            target_by_r = c[i] - 1.5 * risk
            target = max(swing_low, target_by_r)
            return {
                "direction": "short",
                "entry": float(c[i]),
                "stop": float(rev_high + stop_buf_atr * atr_now),
                "target": float(target),
            }

    # ---- Try LONG (target = untaken swing high) — mirror
    if hi_idx < lo_idx:
        recent_high_idx = min(range(hi_idx, lo_idx + 1), key=lambda i: h[i])
    else:
        recent_high_idx = lo_idx
    swing_high = h[recent_high_idx]

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