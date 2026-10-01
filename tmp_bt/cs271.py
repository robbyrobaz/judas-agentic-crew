import numpy as np
import pandas as pd

def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()

def _rolling_swing_high(high, lookback):
    return high.shift(1).rolling(lookback).max()

def _rolling_swing_low(low, lookback):
    return low.shift(1).rolling(lookback).min()

def evaluate(bars, params):
    sweep_lookback = int(params.get("sweep_lookback", 5))
    atr_period = int(params.get("atr_period", 14))
    body_ratio_min = float(params.get("body_ratio_min", 0.70))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.3))
    pullback_pct = float(params.get("pullback_pct", 0.40))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.10))

    n = len(bars)
    min_bars = max(sweep_lookback + 5, atr_period + 5, 30)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    atr = _atr(bars, period=atr_period)
    swing_high = _rolling_swing_high(high, sweep_lookback)
    swing_low = _rolling_swing_low(low, sweep_lookback)

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    sh = float(swing_high.iloc[i]) if not pd.isna(swing_high.iloc[i]) else None
    sl = float(swing_low.iloc[i]) if not pd.isna(swing_low.iloc[i]) else None
    if sh is None or sl is None or sh <= sl:
        return None

    cur_close = float(close.iloc[i])
    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])
    rng = cur_high - cur_low
    if rng <= 0:
        return None
    body = abs(cur_close - float(bars["open"].iloc[i]))
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * cur_atr:
        return None

    direction = None
    entry = None
    stop = None

    if cur_high > sh and cur_close > sh:
        direction = "long"
        entry = cur_high - pullback_pct * rng
        stop = cur_low - stop_buffer_atr * cur_atr
    elif cur_low < sl and cur_close < sl:
        direction = "short"
        entry = cur_low + pullback_pct * rng
        stop = cur_high + stop_buffer_atr * cur_atr

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}
