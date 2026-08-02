"""Generic buffet_zoo evaluator template — RSI / Bollinger / MA cross.

Params (passed via params dict):
  strategy_type: "rsi" | "bollinger" | "ma_cross"
  period: int (RSI length / Bollinger length / MA cross fast)
  lo_thr, hi_thr: float (RSI only)
  n_std: float (Bollinger only)
  fast, slow: int (MA cross only)
  target_r: float
  stop_atr_mult: float
  atr_period: int (default 14)
"""
import pandas as pd
import numpy as np

def evaluate(bars, params):
    if bars is None or len(bars) < 30:
        return None
    strategy_type = params.get("strategy_type", "rsi")
    target_r = float(params.get("target_r", 1.5))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.0))
    atr_period = int(params.get("atr_period", 14))
    closes = bars["close"].values
    highs = bars["high"].values
    lows = bars["low"].values
    n = len(closes)
    if n < atr_period + 2:
        return None
    # ATR
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))
    atr_series = pd.Series(tr).rolling(atr_period).mean().values
    atr_cur = atr_series[-1]
    if np.isnan(atr_cur) or atr_cur <= 0:
        return None
    direction = None
    if strategy_type == "rsi":
        period = int(params.get("period", 14))
        lo_thr = float(params.get("lo_thr", 30))
        hi_thr = float(params.get("hi_thr", 70))
        delta = np.diff(closes)
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        if len(gains) < period:
            return None
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])
        rsi_arr = np.zeros(len(delta))
        for i in range(period, len(delta)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / (avg_loss + 1e-12)
            rsi_arr[i] = 100.0 - (100.0 / (1.0 + rs))
        cur_rsi = rsi_arr[-1]
        if cur_rsi < lo_thr:
            direction = "long"
        elif cur_rsi > hi_thr:
            direction = "short"
    elif strategy_type == "bollinger":
        period = int(params.get("period", 20))
        n_std = float(params.get("n_std", 2.0))
        if n < period:
            return None
        sma = np.mean(closes[-period:])
        sd = np.std(closes[-period:])
        upper = sma + n_std * sd
        lower = sma - n_std * sd
        cur_close = closes[-1]
        if cur_close < lower:
            direction = "long"
        elif cur_close > upper:
            direction = "short"
    elif strategy_type == "ma_cross":
        fast = int(params.get("fast", 9))
        slow = int(params.get("slow", 21))
        if n < slow + 1:
            return None
        fast_ma = np.mean(closes[-fast:])
        slow_ma = np.mean(closes[-slow:])
        if fast_ma > slow_ma:
            direction = "long"
        elif fast_ma < slow_ma:
            direction = "short"
    if direction is None:
        return None
    cur_close = closes[-1]
    if direction == "long":
        stop = cur_close - atr_cur * stop_atr_mult
        risk = cur_close - stop
        target = cur_close + target_r * risk
    else:
        stop = cur_close + atr_cur * stop_atr_mult
        risk = stop - cur_close
        target = cur_close - target_r * risk
    return {
        "direction": direction,
        "entry": float(cur_close),
        "stop": float(stop),
        "target": float(target),
    }