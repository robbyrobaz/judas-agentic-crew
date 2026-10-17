"""ICT 2022 HTF Premium/Discount filter + 5m displacement continuation (CSID candidate).

Architecture (FRESH):
  1. Compute HTF (1h) 20-bar range. If current 1h close is in lower 50% (discount) -> only longs
     allowed; if upper 50% (premium) -> only shorts allowed. This is the ICT premium/discount
     filter.
  2. Detect 5m displacement: range >= 1.2 * ATR(14), body_ratio >= 0.65.
  3. Wait for 50% pullback of displacement range (midpoint) within next 4 bars.
  4. Enter at midpoint, stop beyond displacement extreme + 0.10 ATR buffer, target 1.5R.

Distinguishing from CSID 256/267 (atr_disp_continuation_5m_strict):
  - Adds HTF premium/discount bias filter (continuation aligned with ICT 2022).
  - Body ratio tighter (0.65 vs 0.60).
  - Same pullback + 1.5R target.

Distinguishing from CSID 362 (judas_nyam_disp_bias):
  - No NY AM session filter (works any time)
  - Adds the premium/discount regime filter instead of EMA(60)
"""
import numpy as np
import pandas as pd


_S = {"last_processed_idx": -1}


def _atr(high, low, close, period=14):
    pc = np.roll(close, 1)
    pc[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - pc), np.abs(low - pc)))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().values


def evaluate(bars, params):
    n = len(bars)
    if n < 200:
        return None
    cur_i = n - 1
    if cur_i == _S["last_processed_idx"]:
        return None
    _S["last_processed_idx"] = cur_i

    atr_period = int(params.get("atr_period", 14))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.2))
    body_ratio_min = float(params.get("body_ratio_min", 0.65))
    pullback_bars = int(params.get("pullback_bars", 4))
    htf_bars_per_ltf = int(params.get("htf_bars_per_ltf", 12))  # 5m * 12 = 1h
    htf_range_period = int(params.get("htf_range_period", 20))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.10))
    tick = float(params.get("tick", 0.10))

    h = bars["high"].astype(float).values
    l = bars["low"].astype(float).values
    c = bars["close"].astype(float).values
    o = bars["open"].astype(float).values

    atr = _atr(h, l, c, period=atr_period)
    cur_atr = atr[cur_i]
    if cur_atr <= 0:
        return None

    # HTF (1h) range from resampled 5m bars
    htf_n = n // htf_bars_per_ltf
    if htf_n < htf_range_period + 1:
        return None
    htf_h = np.array([h[k * htf_bars_per_ltf:(k + 1) * htf_bars_per_ltf].max() for k in range(htf_n)])
    htf_l = np.array([l[k * htf_bars_per_ltf:(k + 1) * htf_bars_per_ltf].min() for k in range(htf_n)])
    htf_c = np.array([c[(k + 1) * htf_bars_per_ltf - 1] for k in range(htf_n)])

    cur_htf_idx = htf_n - 1
    htf_high = float(htf_h[max(0, cur_htf_idx - htf_range_period + 1):cur_htf_idx + 1].max())
    htf_low = float(htf_l[max(0, cur_htf_idx - htf_range_period + 1):cur_htf_idx + 1].min())
    htf_mid = (htf_high + htf_low) / 2.0
    htf_close = float(htf_c[cur_htf_idx])

    in_premium = htf_close >= htf_mid
    in_discount = htf_close < htf_mid

    # Find most recent displacement candle within pullback_bars window
    disp_i = -1
    disp_dir = None
    for j in range(cur_i - 1, max(cur_i - 1 - pullback_bars - 2, atr_period - 1), -1):
        bar_range = h[j] - l[j]
        if bar_range < disp_atr_mult * atr[j]:
            continue
        body = abs(c[j] - o[j])
        if body < body_ratio_min * bar_range:
            continue
        if c[j] > o[j]:
            disp_dir = "long"
        else:
            disp_dir = "short"
        disp_i = j
        break

    if disp_i < 0:
        return None

    # Bias filter: long only in discount, short only in premium
    if disp_dir == "long" and not in_discount:
        return None
    if disp_dir == "short" and not in_premium:
        return None

    # Pullback midpoint of displacement range
    disp_high = h[disp_i]
    disp_low = l[disp_i]
    midpoint = (disp_high + disp_low) / 2.0

    # Current bar must reach into the midpoint zone
    if disp_dir == "long":
        # Pullback: low must be near or below midpoint, then close above it
        if l[cur_i] > midpoint:
            return None
        entry = c[cur_i]
        stop = disp_low - stop_buffer_atr * cur_atr
        if entry <= stop:
            return None
        risk = entry - stop
        if risk <= 0.5 * tick:
            return None
        target = entry + risk * target_r
    else:
        if h[cur_i] < midpoint:
            return None
        entry = c[cur_i]
        stop = disp_high + stop_buffer_atr * cur_atr
        if entry >= stop:
            return None
        risk = stop - entry
        if risk <= 0.5 * tick:
            return None
        target = entry - risk * target_r

    return {
        "ts": str(bars["ts"].iloc[cur_i]),
        "direction": "long" if disp_dir == "long" else "short",
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
    }