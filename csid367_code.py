"""
PROPULSION BLOCK 15m v1 — parent OB gradient borrowed by child.

ICT concept (per finding 8835 / 8840 prototype plan):
  Parent OB on 15m = candle immediately BEFORE a displacement candle that breaks
  structure (close > swing high OR < swing low). Displacement filter:
  body_ratio >= 0.6 AND range >= 1.2 * ATR(14).
  Quadrant filter: parent OB midpoint must lie in DISCOUNT half of recent N-bar
  range (for longs) or PREMIUM half (for shorts). Octant mode optional.
  Child propulsion block = CURRENT bar retraces INTO parent OB zone.
  Child BORROWS parent gradient — no separate displacement requirement.
  Entry = parent OB midpoint, stop = parent OB edge +/- 0.2*ATR buffer,
  target = 2R by default.

Distinguishing from existing families:
  - ict_mitigation_block (csid 253/333/326): trades the candle BEFORE the
    displacement also, but requires Local Structure Shift confirmation on
    the LTF retrace. THIS strategy has no separate LSS requirement — child
    borrows parent gradient directly.
  - ob_midpoint_reversion: mean-reversion to any OB midpoint without
    displacement+structure-break prequalification.
  - propulsion block is the 'child borrows parent' mechanic described in
    YT:Xpc8-ieBdDk (ICT Gems 'Order Block to Mitigation Block' 2026).

Backtest intent (90d, 15m, v1_realistic_micros): target MGC + MNQ + ZF.
"""
import numpy as np
import pandas as pd


def _atr_series(high, low, close, period=14):
    pc = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - pc), np.abs(low - pc)))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().values


def evaluate(bars, params):
    pivot_len = int(params.get("pivot_len", 8))
    range_lookback = int(params.get("range_lookback", 50))
    min_disp_body_ratio = float(params.get("min_disp_body_ratio", 0.6))
    min_disp_atr = float(params.get("min_disp_atr", 1.2))
    quadrant_mode = params.get("quadrant_mode", "halfrange")  # "halfrange" or "octant"
    octant_pct = float(params.get("octant_pct", 0.25))
    max_parent_age = int(params.get("max_parent_age", 30))
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))
    atr_period = int(params.get("atr_period", 14))
    require_zone_touch = bool(params.get("require_zone_touch", True))
    max_risk_atr = float(params.get("max_risk_atr", 4.0))

    n = len(bars)
    if n < max(range_lookback + 10, pivot_len + 5, max_parent_age + 30):
        return None

    high = bars["high"].astype(float).values
    low = bars["low"].astype(float).values
    open_ = bars["open"].astype(float).values
    close = bars["close"].astype(float).values

    atr = _atr_series(high, low, close, period=atr_period)
    rw_high = pd.Series(high).rolling(range_lookback, min_periods=10).max().values
    rw_low = pd.Series(low).rolling(range_lookback, min_periods=10).min().values

    sw_high = pd.Series(high).rolling(pivot_len, min_periods=1).max().shift(1).values
    sw_low = pd.Series(low).rolling(pivot_len, min_periods=1).min().shift(1).values

    i = n - 1
    cur_atr = float(atr[i])
    if cur_atr <= 0:
        return None

    # Walk backwards looking for parent OB setup.
    parent_h, parent_l, direction, disp_idx = None, None, None, -1
    for j in range(i - 2, max(0, i - max_parent_age) - 1, -1):
        if j < 1:
            continue
        cur_atr_j = float(atr[j])
        if cur_atr_j <= 0:
            continue
        body = float(close[j] - open_[j])
        rng = float(high[j] - low[j])
        if rng <= 0:
            continue
        body_ratio = abs(body) / rng
        if body_ratio < min_disp_body_ratio:
            continue
        if rng < min_disp_atr * cur_atr_j:
            continue
        if body > 0:  # bullish disp
            sh = float(sw_high[j])
            if np.isnan(sh) or float(close[j]) <= sh:
                continue
            parent_h = float(high[j - 1])
            parent_l = float(low[j - 1])
            direction = "long"
            disp_idx = j
            break
        else:  # bearish disp
            sl = float(sw_low[j])
            if np.isnan(sl) or float(close[j]) >= sl:
                continue
            parent_h = float(high[j - 1])
            parent_l = float(low[j - 1])
            direction = "short"
            disp_idx = j
            break

    if direction is None:
        return None

    parent_mid = 0.5 * (parent_h + parent_l)

    # Quadrant filter on parent OB midpoint.
    rw_hi = float(rw_high[i])
    rw_lo = float(rw_low[i])
    if np.isnan(rw_hi) or np.isnan(rw_lo) or rw_hi <= rw_lo:
        return None
    midpoint = 0.5 * (rw_hi + rw_lo)
    if direction == "long" and parent_mid > midpoint:
        return None
    if direction == "short" and parent_mid < midpoint:
        return None
    if quadrant_mode == "octant":
        span = rw_hi - rw_lo
        if direction == "long":
            oct_lo = rw_lo + octant_pct * span
            if parent_mid > oct_lo:
                return None
        else:
            oct_hi = rw_hi - octant_pct * span
            if parent_mid < oct_hi:
                return None

    # Child trigger: current bar retraces INTO parent OB zone.
    cur_h = float(high[i])
    cur_l = float(low[i])
    cur_c = float(close[i])
    if require_zone_touch:
        if direction == "long":
            # child must dip into zone (low <= parent_h), not break through (close > parent_l)
            if cur_l > parent_h:
                return None
            if cur_c < parent_l:
                return None
        else:
            if cur_h < parent_l:
                return None
            if cur_c > parent_h:
                return None

    entry = parent_mid
    if direction == "long":
        stop = parent_l - stop_buf_atr * cur_atr
        risk = entry - stop
        if risk <= 0:
            return None
        if risk > max_risk_atr * cur_atr:
            return None
        target = entry + target_r * risk
    else:
        stop = parent_h + stop_buf_atr * cur_atr
        risk = stop - entry
        if risk <= 0:
            return None
        if risk > max_risk_atr * cur_atr:
            return None
        target = entry - target_r * risk

    return {
        "direction": direction,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
    }

