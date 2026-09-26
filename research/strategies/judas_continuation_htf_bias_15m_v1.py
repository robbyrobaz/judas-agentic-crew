"""
Judas Continuation 15m with HTF Bias Pre-Filter.

Architecture: takes the proven judas_continuation_15m_* family (CSID 271/274/275)
and adds a Higher Time Frame bias filter (per ICT Trading Tiger Judas swing video):
only fire in the direction of the underlying HTF trend.

HTF bias: rolling N-bar EMA of close on the same 15m bars (15m trend proxy).
  - HTF bullish: close > EMA -> only take longs (SSL sweep -> continuation long)
  - HTF bearish: close < EMA -> only take shorts (BSL sweep -> continuation short)

Judas continuation logic (unchanged from CSID 271):
  - Sweep of prior N-bar swing high (BSL) or swing low (SSL)
  - Displacement bar in the continuation direction (body_ratio + ATR mult)
  - Entry on pullback; SL outside sweep extreme + ATR buffer; target = target_r * risk

Pre-commit retire: n=10 pf_net<0.9 OR 6 consec L.

Why this matters (from video HSMiW6qCce4):
  "trading with daily bias is going to increase your win rate even more"
  "if we are in a buy program then we have to mark all of the sell side liquidity levels"
  Existing judas_continuation_* variants fire BOTH directions on every sweep; the bias
  filter should remove counter-trend sweeps where the Judas fails (continuation in
  wrong direction).
"""
import numpy as np
import pandas as pd


def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _rolling_swing_high(high, lookback):
    return high.shift(1).rolling(lookback).max()


def _rolling_swing_low(low, lookback):
    return low.shift(1).rolling(lookback).min()


def evaluate(bars, params):
    sweep_lookback = int(params.get("sweep_lookback", 8))
    atr_period = int(params.get("atr_period", 14))
    body_ratio_min = float(params.get("body_ratio_min", 0.65))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.0))
    pullback_pct = float(params.get("pullback_pct", 0.40))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.15))
    htf_ema_period = int(params.get("htf_ema_period", 60))

    n = len(bars)
    min_bars = max(sweep_lookback + 5, atr_period + 5, htf_ema_period + 5, 50)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    atr = _atr(bars, period=atr_period)
    swing_high = _rolling_swing_high(high, sweep_lookback)
    swing_low = _rolling_swing_low(low, sweep_lookback)

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    # HTF bias: rolling mean of closes (15m bars) as a 15m-trend proxy.
    # 60 bars * 15m = 15h = full NY + London trading session
    if i < htf_ema_period:
        return None
    htf_mean = float(close.iloc[i - htf_ema_period + 1:i + 1].mean())
    cur_close_prev = float(close.iloc[i])
    htf_bullish = cur_close_prev > htf_mean
    htf_bearish = cur_close_prev < htf_mean

    sh = float(swing_high.iloc[i]) if not pd.isna(swing_high.iloc[i]) else None
    sl = float(swing_low.iloc[i]) if not pd.isna(swing_low.iloc[i]) else None
    if sh is None or sl is None or sh <= sl:
        return None

    cur_close = float(close.iloc[i])
    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])
    cur_open = float(bars["open"].iloc[i])
    rng = cur_high - cur_low
    if rng <= 0:
        return None
    body = abs(cur_close - cur_open)
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * cur_atr:
        return None

    direction = None
    entry = None
    stop = None

    # BSL sweep (high taken) -> reversal short. Only if HTF bearish.
    if cur_high > sh and cur_close > sh:
        if not htf_bearish:
            return None
        direction = "short"
        entry = cur_low + pullback_pct * rng
        stop = cur_high + stop_buffer_atr * cur_atr
    # SSL sweep (low taken) -> reversal long. Only if HTF bullish.
    elif cur_low < sl and cur_close < sl:
        if not htf_bullish:
            return None
        direction = "long"
        entry = cur_high - pullback_pct * rng
        stop = cur_low - stop_buffer_atr * cur_atr

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}