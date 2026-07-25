"""
Custom buffet_zoo Bollinger evaluate function for run_custom_backtest.
Mirrors research/rsi_zoo_strategy.py and ma_cross_strategy.py pattern.

Enter long when close crosses above upper band (breakout), short on cross below lower band.
Or mean-reversion variant: long when close re-enters band from below, short when re-enters from above.
"""
import numpy as np
import pandas as pd


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _bbands(close: pd.Series, period: int, n_std: float):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    return mid, mid + n_std * sd, mid - n_std * sd


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    period = int(params.get("period", 20))
    n_std = float(params.get("n_std", 2.0))
    target_r = float(params.get("target_r", 1.5))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.5))

    if len(bars) < period + 5:
        return None

    close = bars["close"].astype(float)
    mid, upper, lower = _bbands(close, period, n_std)

    if pd.isna(upper.iloc[-1]) or pd.isna(upper.iloc[-2]) or pd.isna(lower.iloc[-1]) or pd.isna(lower.iloc[-2]):
        return None

    atr = _atr(bars).iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    cur = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    cur_upper = float(upper.iloc[-1])
    prev_upper = float(upper.iloc[-2])
    cur_lower = float(lower.iloc[-1])
    prev_lower = float(lower.iloc[-2])

    direction = None
    # Breakout: cross above upper band -> long
    if prev <= prev_upper and cur > cur_upper:
        direction = "long"
    # Breakdown: cross below lower band -> short
    elif prev >= prev_lower and cur < cur_lower:
        direction = "short"
    if direction is None:
        return None

    entry = float(cur)
    if direction == "long":
        stop = entry - stop_atr_mult * float(atr)
        target = entry + target_r * (entry - stop)
    else:
        stop = entry + stop_atr_mult * float(atr)
        target = entry - target_r * (stop - entry)

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}