"""
Bollinger Squeeze + HTF Trend Filter — Custom Backtest

Strategy: volatility contraction followed by directional bounce with HTF bias.

Why this is a NEW family vs failed MBT/MET approaches:
  - IFVG midpoint reversion (failed): counter-trend reversion
  - Judas 1h swing: MGC only, trend day Judas
  - RSI zoo (failed): mean reversion
  - This: HTF-trend-aligned pullback into BB after contraction

Rules:
  1) Compute Bollinger Bands (period=N, k=2.0) on 15m bars
  2) Compute Bollinger Width (BBW) = (upper - lower) / mid
  3) Lookback N bars, find the max BBW over the last 30 bars; require BBW < fraction * max_BBW
     -> "volatility contraction" regime
  4) HTF filter (1h bars): 1h close > 1h 50-EMA for longs, < for shorts
  5) Entry: bar closes ABOVE lower BB (for long) or BELOW upper BB (for short) AFTER contraction
     AND in direction of HTF trend
  6) Stop: beyond recent swing (last 5 bars) +/- ATR(14)*buf
  7) Target: 1.5R

Backtest targets: 90d, 15m bars on MBT, MET, DX.

The HTF bias is what separates this from naive Bollinger reversion.
ATR-relative stop and target make it cross-symbol-portable.
"""

import numpy as np
import pandas as pd


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _bbands(close: pd.Series, period: int, k: float):
    mid = close.ewm(span=period, adjust=False).mean()
    sd = close.ewm(span=period, adjust=False).std(bias=False)
    return mid, mid + k * sd, mid - k * sd


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    period = int(params.get("period", 20))
    k = float(params.get("k", 2.0))
    squeeze_lookback = int(params.get("squeeze_lookback", 30))
    squeeze_ratio = float(params.get("squeeze_ratio", 0.55))
    target_r = float(params.get("target_r", 1.5))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.20))
    htf_ema_period = int(params.get("htf_ema_period", 50))
    swing_lookback = int(params.get("swing_lookback", 5))

    n = len(bars)
    if n < period + squeeze_lookback + swing_lookback + 5:
        return None

    cur_i = n - 1
    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    mid, upper, lower = _bbands(close, period, k)
    if pd.isna(mid.iloc[cur_i]) or pd.isna(upper.iloc[cur_i]):
        return None

    bbw = (upper - lower) / mid.replace(0, np.nan)
    if pd.isna(bbw.iloc[cur_i]):
        return None

    # Squeeze check: current BBW below fraction of recent max
    look_start = max(0, cur_i - squeeze_lookback)
    recent_max = float(bbw.iloc[look_start:cur_i + 1].max())
    if recent_max <= 0:
        return None
    cur_bbw = float(bbw.iloc[cur_i])
    if cur_bbw > squeeze_ratio * recent_max:
        return None

    # HTF trend: resample 15m bars to 1h if DatetimeIndex available.
    if not isinstance(bars.index, pd.DatetimeIndex):
        # If no datetime index, skip HTF filter (most backtests pass df with integer index)
        htf_bull = True
        htf_bear = True
    else:
        htf_close_1h = close.resample("1h").last().dropna()
        if len(htf_close_1h) < htf_ema_period:
            htf_bull = True
            htf_bear = True
        else:
            htf_ema = _ema(htf_close_1h, htf_ema_period)
            last_close = float(htf_close_1h.iloc[-1])
            last_ema = float(htf_ema.iloc[-1])
            htf_bull = last_close > last_ema
            htf_bear = last_close < last_ema

    atr = _atr(bars).iloc[cur_i]
    if pd.isna(atr) or atr <= 0:
        return None
    atr = float(atr)

    cur_close = float(close.iloc[cur_i])
    cur_high = float(high.iloc[cur_i])
    cur_low = float(low.iloc[cur_i])

    swing_low = float(low.iloc[cur_i - swing_lookback:cur_i].min())
    swing_high = float(high.iloc[cur_i - swing_lookback:cur_i].max())
    buf = stop_buf_atr * atr

    direction = None
    # Long: close > lower BB (bouncing up), close > mid (in upper half of range)
    if htf_bull and cur_close > float(lower.iloc[cur_i]) and cur_close > float(mid.iloc[cur_i]):
        if cur_low <= float(lower.iloc[cur_i]):
            direction = "long"
    # Short: close < upper BB, close < mid, HTF bearish
    elif htf_bear and cur_close < float(upper.iloc[cur_i]) and cur_close < float(mid.iloc[cur_i]):
        if cur_high >= float(upper.iloc[cur_i]):
            direction = "short"

    if direction is None:
        return None

    entry = cur_close
    if direction == "long":
        stop = swing_low - buf
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_r * risk
    else:
        stop = swing_high + buf
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_r * risk

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}