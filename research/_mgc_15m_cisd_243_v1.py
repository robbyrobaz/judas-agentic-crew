"""MGC 15m — CSID 243 (cisd_3candle_fvg) variant.

3-candle CISD pattern (Change In State of Delivery) + FVG midpoint entry.
Original CSID 243 was tested on MGC 5m with 0 signals. This adapts the
same logic to 15m timeframe (slower, fewer but more substantive patterns).

Note: CISD is when a strong opposite candle appears after a directional
move. We look for: candle 1 strong in direction A (body_pct, displacement),
candle 2 sweeps beyond c1's extreme AND closes past c1's midpoint (CISD),
candle 3 confirms in original direction. FVG is between c1 and c3 — entry
midpoint.
"""
import pandas as pd

def evaluate(bars, params):
    n = len(bars)
    if n < 30: return None

    confirmation_bars = int(params.get("confirmation_bars", 1))
    min_body_pct = float(params.get("min_body_pct", 0.4))
    min_disp_pct = float(params.get("min_disp_pct", 0.3))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_pct = float(params.get("stop_buffer_pct", 0.001))
    ema_period = int(params.get("ema_period", 20))

    if confirmation_bars < 1: confirmation_bars = 1
    if n < ema_period + 5 + confirmation_bars: return None

    highs = bars["high"].astype(float).values
    lows = bars["low"].astype(float).values
    closes = bars["close"].astype(float).values
    opens = bars["open"].astype(float).values

    tr = []
    for j in range(n):
        if j == 0:
            tr.append(highs[j] - lows[j])
        else:
            tr.append(max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1])))
    atr_series = pd.Series(tr).rolling(ema_period).mean().values
    if pd.isna(atr_series[-1]) or atr_series[-1] <= 0: return None
    cur_atr = atr_series[-1]

    # Three-bar CISD pattern at indices [n-3, n-2, n-1] (latest)
    c1_o, c1_h, c1_l, c1_c = opens[-3], highs[-3], lows[-3], closes[-3]
    c2_o, c2_h, c2_l, c2_c = opens[-2], highs[-2], lows[-2], closes[-2]
    c3_o, c3_h, c3_l, c3_c = opens[-1], highs[-1], lows[-1], closes[-1]

    c1_body = abs(c1_c - c1_o)
    c1_range = c1_h - c1_l
    if c1_range <= 0 or c1_body / c1_range < min_body_pct: return None
    c1_dir = 1 if c1_c > c1_o else -1
    c1_mid = (c1_o + c1_c) / 2

    bull_cisd = (c1_dir == -1 and c2_l < c1_l and c2_c > c1_mid)
    bear_cisd = (c1_dir == 1 and c2_h > c1_h and c2_c < c1_mid)
    if not (bull_cisd or bear_cisd): return None

    c3_body = abs(c3_c - c3_o)
    c3_range = c3_h - c3_l
    if c3_range <= 0 or c3_body / c3_range < min_body_pct: return None
    c3_dir = 1 if c3_c > c3_o else -1
    if bull_cisd and c3_dir != 1: return None
    if bear_cisd and c3_dir != -1: return None
    if c3_body < cur_atr * min_disp_pct: return None

    if bull_cisd:
        fvg_top = c1_h
        fvg_bot = c3_o
        if fvg_top >= fvg_bot: return None
        entry = (fvg_top + fvg_bot) / 2
        stop = c2_l * (1 - stop_buffer_pct)
        direction = "long"
    else:
        fvg_top = c3_o
        fvg_bot = c1_l
        if fvg_top <= fvg_bot: return None
        entry = (fvg_top + fvg_bot) / 2
        stop = c2_h * (1 + stop_buffer_pct)
        direction = "short"

    risk = abs(entry - stop)
    if risk <= 0 or risk > cur_atr * 2.5: return None
    target = entry + (target_r * risk * (1 if direction == "long" else -1))

    return {
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "risk": float(risk),
        "rr": float(target_r),
    }