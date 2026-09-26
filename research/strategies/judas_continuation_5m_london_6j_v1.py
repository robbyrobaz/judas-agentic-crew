"""
Judas Continuation 5m with London killzone session filter on 6J.

Architecture: CSID 271 judas_continuation logic ported to 5m with a London killzone
session window filter (06:00-10:00 UTC = 02:00-06:00 ET during EDT, 01:00-05:00 ET
during EST).

The London killzone is the institutional Judas swing window per the ICT Asian Range
Judas Swing video (5p-T-SG1dj4): when London desks log on at 2:00 AM Eastern, they
engineer a false move in the opposite direction of the day's intended path. The
algorithm activates and creates the Judas swing during this 3-hour window.

For 6J (Japanese yen futures), the Asian session is the home session, so the London
open transition is the prime Judas setup window:
  - Asian range = accumulation (Tokyo 00:00-08:00 UTC roughly)
  - London killzone = Judas swing window (06:00-10:00 UTC)
  - Judas = the false move against the day's intended direction
  - Entry = on the Judas reversal, sweep + displacement + pullback

5m timeframe: tighter stops, more frequent signals. Sweep lookback 6 bars = 30m.

Backtest envelope target: PF 1.3-2.5 (NOT BT-LURE), n >= 20, E[R] > 0.
Pre-commit retire: n=10 pf_net<0.9 OR 6 consec L.
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
    sweep_lookback = int(params.get("sweep_lookback", 6))
    atr_period = int(params.get("atr_period", 14))
    body_ratio_min = float(params.get("body_ratio_min", 0.65))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.0))
    pullback_pct = float(params.get("pullback_pct", 0.40))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.15))
    session_start = int(params.get("session_start_hour_utc", 6))
    session_end = int(params.get("session_end_hour_utc", 10))

    n = len(bars)
    min_bars = max(sweep_lookback + 5, atr_period + 5, 30)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    atr = _atr(bars, period=atr_period)
    swing_high = _rolling_swing_high(high, sweep_lookback)
    swing_low = _rolling_swing_low(low, sweep_lookback)

    # Session window check (London killzone)
    try:
        ts_v = bars["ts"].values
        ts_dt = pd.to_datetime(ts_v, utc=True, errors="coerce")
        hours = ts_dt.hour.values
        last_hour = hours[-1]
    except Exception:
        return None
    if pd.isna(last_hour) or not (session_start <= last_hour < session_end):
        return None

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

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

    if cur_high > sh and cur_close > sh:
        direction = "short"
        entry = cur_low + pullback_pct * rng
        stop = cur_high + stop_buffer_atr * cur_atr
    elif cur_low < sl and cur_close < sl:
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