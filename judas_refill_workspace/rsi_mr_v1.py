"""RSI(10, 30/70) Mean Reversion — custom engine strategy.

Crossing-based entry: long when RSI crosses above 30 from below (oversold reversal),
short when RSI crosses below 70 from above (overbought reversal).

Holding: engine handles stop/target over subsequent bars.

Params:
  period: RSI period (default 10)
  lo_thr: oversold threshold (default 30)
  hi_thr: overbought threshold (default 70)
  target_r: R-multiple target (default 1.0 or 1.5)
  stop_atr_mult: ATR multiple for stop (default 1.0)
  atr_period: ATR period (default 14)
"""
import pandas as pd


def _rsi(close, period):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def _atr(bars, period=14):
    h = bars["high"]
    l = bars["low"]
    c = bars["close"]
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def evaluate(bars, params):
    period = int(params.get("period", 10))
    lo_thr = float(params.get("lo_thr", 30))
    hi_thr = float(params.get("hi_thr", 70))
    target_r = float(params.get("target_r", 1.0))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.0))
    atr_period = int(params.get("atr_period", 14))

    n = len(bars)
    if n < max(period + atr_period + 5, 30):
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    rsi = _rsi(close, period)
    atr = _atr(bars, atr_period)

    if pd.isna(rsi.iloc[-1]) or pd.isna(rsi.iloc[-2]):
        return None
    if pd.isna(atr.iloc[-1]) or atr.iloc[-1] <= 0:
        return None

    rsi_last = float(rsi.iloc[-1])
    rsi_prev = float(rsi.iloc[-2])
    price = float(close.iloc[-1])
    cur_atr = float(atr.iloc[-1])

    cross_up = (rsi_prev <= lo_thr) and (rsi_last > lo_thr)
    cross_dn = (rsi_prev >= hi_thr) and (rsi_last < hi_thr)

    if cross_up:
        stop = price - stop_atr_mult * cur_atr
        target = price + target_r * (price - stop)
        return {"direction": "long", "entry": price, "stop": stop, "target": target}
    if cross_dn:
        stop = price + stop_atr_mult * cur_atr
        target = price - target_r * (stop - price)
        return {"direction": "short", "entry": price, "stop": stop, "target": target}
    return None