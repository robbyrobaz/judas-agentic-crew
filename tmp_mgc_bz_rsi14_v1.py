"""MGC 1H buffet_zoo RSI(14, 25, 75) target_r=1.5 amplification variant."""
import pandas as pd
import numpy as np

PARAMS = {"strategy_type": "rsi", "period": 14, "lo_thr": 25.0, "hi_thr": 75.0,
          "target_r": 1.5, "stop_atr_mult": 1.0, "atr_period": 14}

def evaluate(bars, params):
    params = dict(PARAMS)
    if bars is None or len(bars) < 30:
        return None
    target_r = float(params.get("target_r", 1.5))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.0))
    atr_period = int(params.get("atr_period", 14))
    period = int(params.get("period", 14))
    lo_thr = float(params.get("lo_thr", 30))
    hi_thr = float(params.get("hi_thr", 70))
    closes = bars["close"].values
    highs = bars["high"].values
    lows = bars["low"].values
    n = len(closes)
    if n < max(atr_period + 2, period + 2):
        return None
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))
    atr_series = pd.Series(tr).rolling(atr_period).mean().values
    atr_cur = atr_series[-1]
    if np.isnan(atr_cur) or atr_cur <= 0:
        return None
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
    direction = None
    if cur_rsi < lo_thr:
        direction = "long"
    elif cur_rsi > hi_thr:
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