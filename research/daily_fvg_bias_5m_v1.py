"""
Daily FVG 50% Bias Filter Overlay (5m) — from YT:s1NXJT3FszM ICT Silver Bullet Hindi.

Distilled rule:
- Find a prior-session FVG (yesterday's most recent active FVG).
- Compute its 50% midpoint (the "kathora"/cutoff level).
- If current price is ABOVE that midpoint → daily bias LONG.
- If BELOW → daily bias SHORT.
- During the Silver Bullet killzone (14:00-15:00 UTC = 10:00-11:00 ET NY AM),
  only enter in the direction of the bias.

Entry trigger: displacement bar >= min_body_atr * ATR with body_ratio > 0.5
in bias direction, stop at recent swing, target 2R.

Source: YT:s1NXJT3FszM (Zero Drawdown channel, Hindi-English, 20:14).
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


def _ts(bars):
    if "ts" in bars.columns:
        return pd.to_datetime(bars["ts"], utc=True)
    if "timestamp" in bars.columns:
        return pd.to_datetime(bars["timestamp"], utc=True)
    if isinstance(bars.index, pd.DatetimeIndex):
        return bars.index
    return None


def _find_fvgs(high, low, max_lookback=60):
    fvgs = []
    n = len(high)
    start = max(2, n - max_lookback)
    for i in range(start, n):
        if i - 2 < 0:
            continue
        if float(high.iloc[i - 2]) < float(low.iloc[i]):
            fvgs.append((i, float(low.iloc[i]), float(high.iloc[i - 2]), "bull"))
        elif float(low.iloc[i - 2]) > float(high.iloc[i]):
            fvgs.append((i, float(low.iloc[i - 2]), float(high.iloc[i]), "bear"))
    return fvgs


def _is_mitigated(fvg_top, fvg_bot, fvg_dir, high, low, close, fvg_idx, end_idx):
    for j in range(fvg_idx + 1, end_idx + 1):
        if fvg_dir == "bull":
            if float(low.iloc[j]) <= fvg_bot:
                return True
        else:
            if float(high.iloc[j]) >= fvg_top:
                return True
    return False


def evaluate(bars, params):
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.15))
    min_body_atr = float(params.get("min_body_atr", 0.4))
    atr_period = int(params.get("atr_period", 14))
    swing_lookback = int(params.get("swing_lookback", 10))
    fvg_lookback_bars = int(params.get("fvg_lookback_bars", 240))
    use_sb_window = bool(params.get("use_sb_window", True))
    sb_start = int(params.get("sb_start_hour", 14))
    sb_end = int(params.get("sb_end_hour", 15))
    require_unfilled_fvg = bool(params.get("require_unfilled_fvg", True))

    n = len(bars)
    if n < 240:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    open_ = bars["open"].astype(float)

    ts = _ts(bars)
    if ts is None:
        return None

    cur_ts = ts.iloc[-1]
    cur_hour = int(cur_ts.hour)
    in_sb = use_sb_window and (sb_start <= cur_hour < sb_end)
    if not in_sb:
        return None

    atr = _atr(bars, period=atr_period)
    cur_atr = float(atr.iloc[-1])
    if pd.isna(cur_atr) or cur_atr <= 0:
        return None

    fvgs = _find_fvgs(high, low, max_lookback=fvg_lookback_bars)
    chosen = None
    for fvg_idx, fvg_top, fvg_bot, fvg_dir in reversed(fvgs):
        if require_unfilled_fvg and _is_mitigated(fvg_top, fvg_bot, fvg_dir, high, low, close, fvg_idx, n - 1):
            continue
        chosen = (fvg_idx, fvg_top, fvg_bot, fvg_dir)
        break

    if chosen is None:
        return None

    _, fvg_top, fvg_bot, fvg_dir = chosen
    midpoint = 0.5 * (fvg_top + fvg_bot)
    cur_close = float(close.iloc[-1])

    bias_long = cur_close > midpoint
    bias_short = cur_close < midpoint
    if not (bias_long or bias_short):
        return None

    bar_body = abs(float(close.iloc[-1]) - float(open_.iloc[-1]))
    bar_range = float(high.iloc[-1]) - float(low.iloc[-1])
    if bar_range <= 0 or bar_body / bar_range < 0.5:
        return None
    if bar_body < min_body_atr * cur_atr:
        return None

    if bias_long and float(close.iloc[-1]) > float(open_.iloc[-1]):
        direction = "long"
    elif bias_short and float(close.iloc[-1]) < float(open_.iloc[-1]):
        direction = "short"
    else:
        return None

    if direction == "long":
        recent_swing = float(low.iloc[-swing_lookback:].min())
        stop = recent_swing - stop_buf_atr * cur_atr
        risk = float(close.iloc[-1]) - stop
        if risk <= 0:
            return None
        entry = float(close.iloc[-1])
        target = entry + target_r * risk
    else:
        recent_swing = float(high.iloc[-swing_lookback:].max())
        stop = recent_swing + stop_buf_atr * cur_atr
        risk = stop - float(close.iloc[-1])
        if risk <= 0:
            return None
        entry = float(close.iloc[-1])
        target = entry - target_r * risk

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
    }
