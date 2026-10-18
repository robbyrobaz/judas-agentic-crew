"""
SNIPER CHAIN v3 - strict-mode 5-step sweep + FVG with equal-pool liquidity
requirement AND inversion filter (from YT:Hgui6jVRhro).

Architecture (vs csid=412 baseline):
  Base: sweep pool -> sweep wick -> MSS -> displacement -> 3-candle FVG midpoint entry.
  Strict additions:
    - EQUAL highs/lows liquidity pool (>= pool_min_touches within pool_tolerance_atr).
    - INVERSION filter: no candle BODY closes through the swing pool extreme
      between pool formation and sweep window.

Variants via param `mode`:
  - "baseline": no new filters (control, should match csid=412 numbers)
  - "pool_only": equal-pool requirement only
  - "inversion_only": inversion filter only
  - "strict": both filters
"""
import numpy as np
import pandas as pd


def _atr(high, low, close, period=14):
    pc = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - pc), np.abs(low - pc)))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().values


def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    cur_i = n - 1

    lookback = int(params.get("lookback", 50))
    sweep_lookback = int(params.get("sweep_lookback", 16))
    disp_lookback = int(params.get("disp_lookback", 8))
    fvg_expiry = int(params.get("fvg_expiry", 30))
    min_body = float(params.get("min_body", 0.55))
    min_disp_atr = float(params.get("min_disp_atr", 1.0))
    atr_period = int(params.get("atr_period", 14))
    target_r = float(params.get("target_r", 2.0))
    stop_buf = float(params.get("stop_buf", 0.20))
    mode = str(params.get("mode", "baseline"))

    pool_min_touches = int(params.get("pool_min_touches", 2))
    pool_tolerance_atr = float(params.get("pool_tolerance_atr", 0.5))
    invert_lookback = int(params.get("invert_lookback", 40))

    high = bars["high"].values.astype(float)
    low = bars["low"].values.astype(float)
    close = bars["close"].values.astype(float)
    open_ = bars["open"].values.astype(float)

    atr = _atr(high, low, close, period=atr_period)
    cur_atr = float(atr[cur_i])
    if cur_atr <= 0 or np.isnan(cur_atr):
        return None

    hi_start = max(0, cur_i - lookback)
    hi_end = cur_i - sweep_lookback
    if hi_end <= hi_start + 2:
        return None

    swing_h = float(np.max(high[hi_start:hi_end]))
    swing_l = float(np.min(low[hi_start:hi_end]))

    # EQUAL-pool counting: count how many bars touched each extreme within tolerance.
    tolerance = pool_tolerance_atr * cur_atr
    high_touches = 0
    low_touches = 0
    for j in range(hi_start, hi_end):
        if abs(high[j] - swing_h) <= tolerance:
            high_touches += 1
        if abs(low[j] - swing_l) <= tolerance:
            low_touches += 1

    # Inversion filter: between pool formation and current sweep window, did any
    # candle's BODY close through the swing pool extremes?
    inverted_high = False
    inverted_low = False
    if mode in ("inversion_only", "strict"):
        inv_start = max(0, cur_i - sweep_lookback - invert_lookback)
        inv_end = cur_i - sweep_lookback
        for k in range(inv_start, inv_end):
            if close[k] > swing_h:
                inverted_high = True
            if close[k] < swing_l:
                inverted_low = True

    swept_high = False
    swept_low = False
    sweep_wick_high = float("-inf")
    sweep_wick_low = float("inf")
    sweep_idx = -1

    for j in range(cur_i - sweep_lookback, cur_i):
        if high[j] > swing_h and close[j] < swing_h:
            swept_high = True
            if high[j] > swing_wick_high:
                sweep_wick_high = high[j]
            if sweep_idx < 0:
                sweep_idx = j
        if low[j] < swing_l and close[j] > swing_l:
            swept_low = True
            if low[j] < sweep_wick_low:
                sweep_wick_low = low[j]
            if sweep_idx < 0:
                sweep_idx = j

    if not swept_high and not swept_low:
        return None

    if swept_high and inverted_high and mode in ("inversion_only", "strict"):
        return None
    if swept_low and inverted_low and mode in ("inversion_only", "strict"):
        return None

    if mode in ("pool_only", "strict"):
        # Require the swept extreme itself to have >= pool_min_touches
        if swept_high and high_touches < pool_min_touches:
            return None
        if swept_low and low_touches < pool_min_touches:
            return None

    mss_idx = -1
    mss_bull = False
    mss_bear = False
    for k in range(max(sweep_idx + 1, 0), cur_i + 1):
        if close[k] > swing_h:
            mss_bull = True
            mss_idx = k
            break
        if close[k] < swing_l:
            mss_bear = True
            mss_idx = k
            break
    if mss_idx < 0:
        return None

    direction = None
    disp_idx = -1
    for d in range(mss_idx, min(cur_i + 1, mss_idx + disp_lookback + 1)):
        rng = high[d] - low[d]
        if rng <= 0:
            continue
        body = abs(close[d] - open_[d])
        if body / rng < min_body:
            continue
        if rng < min_disp_atr * atr[d]:
            continue
        if close[d] > open_[d] and mss_bull:
            direction = "long"
            disp_idx = d
            break
        if close[d] < open_[d] and mss_bear:
            direction = "short"
            disp_idx = d
            break
    if direction is None:
        return None

    fvg_top = None
    fvg_bot = None
    if direction == "long" and disp_idx >= 2:
        c3h = high[disp_idx - 2]
        c1l = low[disp_idx]
        if c1l > c3h:
            fvg_top = c1l
            fvg_bot = c3h
    elif direction == "short" and disp_idx >= 2:
        c3l = low[disp_idx - 2]
        c1h = high[disp_idx]
        if c3l > c1h:
            fvg_top = c3l
            fvg_bot = c1h
    if fvg_top is None:
        return None

    if cur_i > disp_idx + fvg_expiry:
        return None

    entry = 0.5 * (fvg_top + fvg_bot)
    if direction == "long":
        if low[cur_i] > fvg_top:
            return None
        if close[cur_i] < fvg_bot:
            return None
        sweep_wick = sweep_wick_low
        stop = min(sweep_wick, fvg_bot) - stop_buf * cur_atr
        risk = entry - stop
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry + target_r * risk
    else:
        if high[cur_i] < fvg_bot:
            return None
        if close[cur_i] > fvg_top:
            return None
        sweep_wick = sweep_wick_high
        stop = max(sweep_wick, fvg_top) + stop_buf * cur_atr
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        target = entry - target_r * risk

    return {
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
    }