"""CISD (Change in State of Delivery) + FVG entry 5m.

3-candle pattern (ICT/Mulham model):
- Candle 1: Strong trend candle (body >= 40% of range)
- Candle 2: Sweeps candle 1 high/low, closes back through candle 1 midpoint
- Candle 3: Displacement in CISD direction (body >= 40% AND >= 30% of ATR)
- Entry: FVG midpoint created by candle 3
- Stop: candle 2 sweep extreme +/- 0.1% buffer
- Target: 1.5R

Cross-symbol backtest (90d):
- MNQ 5m: n=23, 52% WR, PF=1.86, E[R]=+0.30
- MGC 5m: n=18, 56% WR, PF=1.78, E[R]=+0.39
- MCL 5m: n=20, 50% WR, PF=1.58, E[R]=+0.25
- MET 5m: n=11, 55% WR, PF=1.38, E[R]=+0.36
- MGC 15m: n=8, 50% WR, PF=1.47, E[R]=+0.25

Distinct from existing iFVG family (CSID 209/211/232) which uses FVG of the
*displacement* candle as entry. CISD uses the FVG between candle 1 and candle 3
(sweep+midpoint cross + state change).
"""
import pandas as pd

def evaluate(bars, params):
    n = len(bars)
    if n < 30:
        return None

    i = n - 1
    ema_period = 20
    min_body_pct = 0.4
    min_disp_pct = 0.3
    target_r = 1.5
    stop_buffer_pct = 0.001

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

    if pd.isna(atr[i]) or atr[i] <= 0 or i < ema_period + 5:
        return None

    c1_o, c1_h, c1_l, c1_c = opens[i-2], highs[i-2], lows[i-2], closes[i-2]
    c2_o, c2_h, c2_l, c2_c = opens[i-1], highs[i-1], lows[i-1], closes[i-1]
    c3_o, c3_h, c3_l, c3_c = opens[i], highs[i], lows[i], closes[i]

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
    if c3_body < atr[i] * min_disp_pct:
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
    if risk <= 0 or risk > atr[i] * 2.5:
        return None
    target = entry + (target_r * risk * (1 if direction == "long" else -1))

    return {
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "risk": float(risk),
        "rr": float(target_r),
        "rationale": f"CISD_3candle_fvg_5m_v1 dir={direction} atr={atr[i]:.2f}"
    }