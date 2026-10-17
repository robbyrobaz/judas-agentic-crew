"""RSI(10, 30, 70) mean reversion 1H — REGEN of retired #4308 / #3969.

Architectural target: capture clean non-trend mean-reversion bars on gold-equity
symbols. Both-sides, ATR-based stop and target.
"""
import pandas as pd
import numpy as np

PARAMS = {"period": 10, "lo_thr": 30.0, "hi_thr": 70.0,
          "target_r": 1.5, "stop_atr_mult": 1.0, "atr_period": 14,
          "session_only_ny": False}


def evaluate(bars, params):
    p = dict(PARAMS)
    if isinstance(params, dict):
        for k in p:
            if k in params:
                p[k] = params[k]
    if bars is None or len(bars) < 30:
        return None
    target_r = float(p.get("target_r", 1.5))
    stop_atr_mult = float(p.get("stop_atr_mult", 1.0))
    atr_period = int(p.get("atr_period", 14))
    period = int(p.get("period", 10))
    lo_thr = float(p.get("lo_thr", 30))
    hi_thr = float(p.get("hi_thr", 70))
    session_only_ny = bool(p.get("session_only_ny", False))

    closes = bars["close"].values
    highs = bars["high"].values
    lows = bars["low"].values
    n = len(closes)
    if n < max(atr_period + 2, period + 2):
        return None

    # Session filter: bar timestamp UTC, 13:00-21:00 UTC = NY session (08:00-16:00 ET).
    if session_only_ny:
        ts = bars["ts_utc"].values if "ts_utc" in bars.columns else None
        if ts is None:
            return None
        try:
            bar_h = int(str(ts[-1])[11:13])
        except Exception:
            return None
        if bar_h < 13 or bar_h >= 21:
            return None

    # True Range / ATR
    tr = np.maximum(highs[1:] - lows[1:],
                    np.maximum(np.abs(highs[1:] - closes[:-1]),
                               np.abs(lows[1:] - closes[:-1])))
    atr_series = pd.Series(tr).rolling(atr_period).mean().values
    atr_cur = atr_series[-1]
    if np.isnan(atr_cur) or atr_cur <= 0:
        return None

    # Wilder RSI(10) — full-string smoothing
    delta = np.diff(closes)
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    if len(gains) < period:
        return None
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + float(gains[i])) / period
        avg_loss = (avg_loss * (period - 1) + float(losses[i])) / period
    rs = avg_gain / (avg_loss + 1e-12)
    cur_rsi = 100.0 - (100.0 / (1.0 + rs))

    direction = None
    if cur_rsi < lo_thr:
        direction = "long"
    elif cur_rsi > hi_thr:
        direction = "short"
    if direction is None:
        return None

    cur_close = float(closes[-1])
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
        "entry": cur_close,
        "stop": float(stop),
        "target": float(target),
    }