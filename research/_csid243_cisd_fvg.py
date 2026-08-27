import pandas as pd

def evaluate(bars, params):
    n = len(bars)
    if n < 30:
        return None

    confirmation_bars = int(params.get("confirmation_bars", 1))
    min_body_pct = float(params.get("min_body_pct", 0.4))
    min_disp_pct = float(params.get("min_disp_pct", 0.3))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_pct = float(params.get("stop_buffer_pct", 0.001))
    ema_period = int(params.get("ema_period", 20))

    if confirmation_bars < 1:
        confirmation_bars = 1
    if n < ema_period + 5 + confirmation_bars:
        return None

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
    atr_series = pd.Series(tr).rolling(ema_period).mean()
    atr = atr_series.values

    disp_idx = n - confirmation_bars
    if disp_idx < ema_period + 5:
        return None
    if pd.isna(atr[disp_idx]) or atr[disp_idx] <= 0:
        return None

    c1_o, c1_h, c1_l, c1_c = opens[disp_idx-2], highs[disp_idx-2], lows[disp_idx-2], closes[disp_idx-2]
    c2_o, c2_h, c2_l, c2_c = opens[disp_idx-1], highs[disp_idx-1], lows[disp_idx-1], closes[disp_idx-1]
    c3_o, c3_h, c3_l, c3_c = opens[disp_idx], highs[disp_idx], lows[disp_idx], closes[disp_idx]

    c1_body = abs(c1_c - c1_o)
    c1_range = c1_h - c1_l
    if c1_range <= 0 or c1_body / c1_range < min_body_pct:
        return None
    c1_dir = 1 if c1_c > c1_o else -1
    c1_mid = (c1_o + c1_c) / 2

    bull_cisd = (c1_dir == -1 and c2_l < c1_l and c2_c > c1_mid)
    bear_cisd = (c1_dir == 1 and c2_h > c1_h and c2_c < c1_mid)

    if not (bull_cisd or bear_cisd):
        return None

    c3_body = abs(c3_c - c3_o)
    c3_range = c3_h - c3_l
    if c3_range <= 0 or c3_body / c3_range < min_body_pct:
        return None
    c3_dir = 1 if c3_c > c3_o else -1

    if bull_cisd and c3_dir != 1:
        return None
    if bear_cisd and c3_dir != -1:
        return None
    if c3_body < atr[disp_idx] * min_disp_pct:
        return None

    if bull_cisd:
        fvg_top = c1_h
        fvg_bot = c3_o
        if fvg_top >= fvg_bot:
            return None
        entry = (fvg_top + fvg_bot) / 2
        stop = c2_l * (1 - stop_buffer_pct)
        direction = "long"
    else:
        fvg_top = c3_o
        fvg_bot = c1_l
        if fvg_top <= fvg_bot:
            return None
        entry = (fvg_top + fvg_bot) / 2
        stop = c2_h * (1 + stop_buffer_pct)
        direction = "short"

    risk = abs(entry - stop)
    if risk <= 0 or risk > atr[disp_idx] * 2.5:
        return None
    target = entry + (target_r * risk * (1 if direction == "long" else -1))

    return {
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "risk": float(risk),
        "rr": float(target_r),
        "rationale": f"CISD_3candle_fvg_5m_params confirmation_bars={confirmation_bars} dir={direction} atr={atr[disp_idx]:.2f}"
    }
