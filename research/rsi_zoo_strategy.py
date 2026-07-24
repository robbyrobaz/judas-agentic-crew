"""
Custom buffet_zoo RSI evaluate function for run_custom_backtest.

Implements RSI zoo: enter long when RSI crosses below lo_thr and back up,
enter short when RSI crosses above hi_thr and back down.

Usage:
    run_custom_backtest(code=open(path).read(), symbol='MET', days=90, timeframe='5m')
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


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    """RSI zoo mean-reversion: oversold->long, overbought->short."""
    period = int(params.get("period", 14))
    lo_thr = float(params.get("lo_thr", 25))
    hi_thr = float(params.get("hi_thr", 75))
    target_r = float(params.get("target_r", 1.5))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.0))

    if len(bars) < period + 5:
        return None

    close = bars["close"].astype(float)
    rsi = _rsi(close, period)

    if pd.isna(rsi.iloc[-1]) or pd.isna(rsi.iloc[-2]):
        return None

    atr = _atr(bars).iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    cur = float(rsi.iloc[-1])
    prev = float(rsi.iloc[-2])

    direction = None
    # Cross-up from oversold
    if prev <= lo_thr and cur > lo_thr:
        direction = "long"
    # Cross-down from overbought
    elif prev >= hi_thr and cur < hi_thr:
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