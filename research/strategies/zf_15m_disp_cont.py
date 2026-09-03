"""ZF 15m displacement+continuation probe.

Tests a single-side continuation after a strong displacement candle in the
direction of the prior 4-bar trend, with target_r=1.5 and an ATR-based stop.
Looking for: positive E[R], PF > 1.5, WR not >90% (overfit red flag).
"""
def evaluate(bars, params):
    if len(bars) < 30:
        return None
    atr_p = int(params.get("atr_period", 14))
    disp_mult = float(params.get("disp_atr_mult", 1.3))
    body_min = float(params.get("body_ratio_min", 0.7))
    target_r = float(params.get("target_r", 1.5))
    stop_buf_atr = float(params.get("stop_buffer_atr", 0.15))
    side_filter = params.get("side_filter", "both")

    h = bars["high"].astype(float).values
    l = bars["low"].astype(float).values
    c = bars["close"].astype(float).values
    o = bars["open"].astype(float).values
    n = len(c)
    if n < atr_p + 6:
        return None

    # TR/ATR via Wilder
    tr = [0.0] * n
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr = [0.0] * n
    atr[atr_p - 1] = sum(tr[:atr_p]) / atr_p
    for i in range(atr_p, n):
        atr[i] = (atr[i - 1] * (atr_p - 1) + tr[i]) / atr_p
    if atr[-1] <= 0:
        return None

    body = c[-1] - o[-1]
    rng = h[-1] - l[-1]
    if rng <= 0:
        return None
    body_ratio = abs(body) / rng
    if body_ratio < body_min:
        return None
    disp_strength = abs(body) / atr[-1]
    if disp_strength < disp_mult:
        return None

    sma = sum(c[-5:-1]) / 4 if n >= 5 else sum(c[:-1]) / max(1, n - 1)
    if c[-1] > sma:
        direction = "long"
    elif c[-1] < sma:
        direction = "short"
    else:
        return None
    if side_filter == "long_only" and direction != "long":
        return None
    if side_filter == "short_only" and direction != "short":
        return None

    entry = c[-1]
    if direction == "long":
        stop = l[-1] - stop_buf_atr * atr[-1]
        risk = entry - stop
        target = entry + target_r * risk
    else:
        stop = h[-1] + stop_buf_atr * atr[-1]
        risk = stop - entry
        target = entry - target_r * risk
    if risk <= 0:
        return None
    return {"direction": direction, "entry": float(entry), "stop": float(stop), "target": float(target)}