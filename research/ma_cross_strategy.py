"""
Custom buffet_zoo MA-cross evaluate function for run_custom_backtest.

Implements the same logic as the live buffet_zoo ma_cross subtype:
- Compute fast and slow SMAs on close
- Go long when fast crosses above slow (and no open position)
- Go short when fast crosses below slow
- Stop = entry -/+ stop_atr_mult * ATR
- Target = entry +/- target_r * (entry - stop)

Usage:
    run_custom_backtest(code=open(path).read(), symbol='MCL', days=90, timeframe='15m')
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


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    """MA-cross: long on fast-above-slow crossover, short on opposite."""
    fast_n = int(params.get("fast", 9))
    slow_n = int(params.get("slow", 21))
    target_r = float(params.get("target_r", 2.0))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.5))

    if len(bars) < slow_n + 5:
        return None

    close = bars["close"].astype(float)
    fast = close.rolling(fast_n).mean()
    slow = close.rolling(slow_n).mean()

    if pd.isna(fast.iloc[-1]) or pd.isna(slow.iloc[-1]) or pd.isna(fast.iloc[-2]) or pd.isna(slow.iloc[-2]):
        return None

    atr = _atr(bars).iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    cur_fast = float(fast.iloc[-1])
    cur_slow = float(slow.iloc[-1])
    prev_fast = float(fast.iloc[-2])
    prev_slow = float(slow.iloc[-2])

    direction = None
    if prev_fast <= prev_slow and cur_fast > cur_slow:
        direction = "long"
    elif prev_fast >= prev_slow and cur_fast < cur_slow:
        direction = "short"
    if direction is None:
        return None

    entry = float(close.iloc[-1])
    if direction == "long":
        stop = entry - stop_atr_mult * float(atr)
        target = entry + target_r * (entry - stop)
    else:
        stop = entry + stop_atr_mult * float(atr)
        target = entry - target_r * (stop - entry)

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}