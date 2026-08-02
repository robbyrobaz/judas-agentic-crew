"""MGC 1H buffet_zoo Bollinger(20, 2.0) target_r=1.5 — mean-reversion zoo alternative."""
import pandas as pd
import numpy as np

PARAMS = {"strategy_type": "bollinger", "period": 20, "n_std": 2.0,
          "target_r": 1.5, "stop_atr_mult": 1.0, "atr_period": 14}

def evaluate(bars, params):
    params = dict(PARAMS)
    if bars is None or len(bars) < 30:
        return None
    target_r = float(params.get("target_r", 1.5))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.0))
    atr_period = int(params.get("atr_period", 14))
    period = int(params.get("period", 20))
    n_std = float(params.get("n_std", 2.0))
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
    sma = np.mean(closes[-period:])
    sd = np.std(closes[-period:])
    upper = sma + n_std * sd
    lower = sma - n_std * sd
    cur_close = closes[-1]
    direction = None
    if cur_close < lower:
        direction = "long"
    elif cur_close > upper:
        direction = "short"
    if direction is None:
        return None
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