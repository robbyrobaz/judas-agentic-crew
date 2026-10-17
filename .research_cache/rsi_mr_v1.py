"""
RSI Mean Reversion — multi-bar hold via signal dict.

Convention (matches run_custom_backtest_on_bars):
- evaluate(bars, params) looks at bars.iloc[-1] (current bar)
- If RSI crosses lo_thr upward (long) or hi_thr downward (short),
  return a signal dict with direction/entry/stop/target.
- The runtime holds the trade until stop OR target is hit (one trade at a time).

Params:
  period          RSI period (default 10)
  lo_thr          long entry when RSI crosses up through this level (default 30)
  hi_thr          short entry when RSI crosses down through this level (default 70)
  atr_period      ATR period (default 14)
  stop_atr_mult   stop distance in ATR multiples (default 1.0)
  target_r        target distance in R-multiples (default 1.0)
  atr_min_floor   skip entries when ATR is below this (low-vol filter)
"""


def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _rsi(close, period=10):
    n = len(close)
    if n < period + 2:
        return [None] * n
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Wilder smoothing
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, 1e-9)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.values


def evaluate(bars, params):
    period = int(params.get("period", 10))
    lo_thr = float(params.get("lo_thr", 30.0))
    hi_thr = float(params.get("hi_thr", 70.0))
    atr_period = int(params.get("atr_period", 14))
    stop_atr_mult = float(params.get("stop_atr_mult", 1.0))
    target_r = float(params.get("target_r", 1.0))
    atr_min_floor = float(params.get("atr_min_floor", 0.0))

    n = len(bars)
    if n < max(period + 3, atr_period + 3):
        return None

    close = bars["close"].astype(float)
    atr = _atr(bars, period=atr_period)
    rsi_vals = _rsi(close, period=period)

    if rsi_vals[-1] is None or rsi_vals[-2] is None:
        return None
    cur_rsi = float(rsi_vals[-1])
    prev_rsi = float(rsi_vals[-2])
    cur_atr = float(atr.iloc[-1])
    if cur_atr is None or cur_atr <= 0 or (atr_min_floor > 0 and cur_atr < atr_min_floor):
        return None

    cur_close = float(close.iloc[-1])
    long_cross = (prev_rsi <= lo_thr) and (cur_rsi > lo_thr)
    short_cross = (prev_rsi >= hi_thr) and (cur_rsi < hi_thr)
    if not (long_cross or short_cross):
        return None

    risk = stop_atr_mult * cur_atr
    if long_cross:
        return {
            "direction": "long",
            "entry": cur_close,
            "stop": cur_close - risk,
            "target": cur_close + target_r * risk,
        }
    return {
        "direction": "short",
        "entry": cur_close,
        "stop": cur_close + risk,
        "target": cur_close - target_r * risk,
    }
