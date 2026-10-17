"""
ICT iFVG midpoint reversion with Zircon body-filter (5m).
Built from YT:85wavG_B7lI "Only Model You Need in 2026" + YT:9SEtHGX3gfs Zircon model.

Consensus-ICT setup (the cleanest testable form):
  1. Liquidity sweep (swing low violated for longs / swing high for shorts)
     within last `sweep_age_bars` bars.
  2. MSS (Market Structure Shift): close recovers back through sweep level
     (close > swing_low for longs; close < swing_high for shorts).
  3. FVG forms in post-MSS displacement, then inverted by price.
  4. ZIRCON body filter: NO candle close in WRONG half of iFVG between
     formation and now (clean inversion).
  5. Entry: iFVG midpoint (consequent encroachment) — the classic ICT target
     reversion. (This is the "Limit at 50% of iFVG" rule from the video.)
  6. Stop: iFVG top/bottom + 0.2 ATR buffer.
  7. Target: 2.0R (full FVG fill if available, else 2R).

Distinct from CSID 295 (ifvg_midpoint_reversion_htf_bias_5m_LOOSE):
  - Adds explicit sweep+MSS gating (not just any FVG).
  - Adds Zircon body-filter (no candle closes in wrong half of iFVG).
  - 2R target vs 1.5R or 1R.
  - No HTF EMA bias filter (pure ICT model only).
"""

import numpy as np
import pandas as pd


def _atr_series(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _rolling_swing_low(low, lookback):
    return low.shift(1).rolling(lookback).min()


def _rolling_swing_high(high, lookback):
    return high.shift(1).rolling(lookback).max()


def evaluate(bars, params):
    swing_lookback = int(params.get("swing_lookback", 8))
    sweep_age_bars = int(params.get("sweep_age_bars", 20))
    fvg_min_atr = float(params.get("fvg_min_atr", 0.20))
    fvg_expiry = int(params.get("fvg_expiry_bars", 30))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))
    target_r = float(params.get("target_r", 2.0))
    atr_period = int(params.get("atr_period", 14))

    n = len(bars)
    min_bars = max(swing_lookback * 2 + fvg_expiry + 10, 60)
    if n < min_bars:
        return None

    close = bars["close"].astype(float).values
    high = bars["high"].astype(float).values
    low = bars["low"].astype(float).values
    open_ = bars["open"].astype(float).values
    atr = _atr_series(bars, period=atr_period).values
    swing_low = _rolling_swing_low(bars["low"], swing_lookback).values
    swing_high = _rolling_swing_high(bars["high"], swing_lookback).values

    i = n - 1
    if np.isnan(atr[i]) or atr[i] <= 0:
        return None
    cur_atr = float(atr[i])

    def find_setup(direction):
        if direction == "long":
            ref_series = swing_low
            mss_check = lambda c, ref: c > ref
            fvg_bullish = True
        else:
            ref_series = swing_high
            mss_check = lambda c, ref: c < ref
            fvg_bullish = False

        sweep_idx = None
        sweep_ref = None
        for k in range(i - 1, max(0, i - sweep_age_bars) - 1, -1):
            if np.isnan(ref_series[k]):
                continue
            ref = float(ref_series[k])
            if direction == "long" and float(low[k]) < ref:
                sweep_idx = k
                sweep_ref = ref
                break
            if direction == "short" and float(high[k]) > ref:
                sweep_idx = k
                sweep_ref = ref
                break
        if sweep_idx is None:
            return None

        mss_idx = None
        for k in range(sweep_idx + 1, min(n, i + 1)):
            ref_k = float(sweep_ref)
            if mss_check(float(close[k]), ref_k):
                mss_idx = k
                break
        if mss_idx is None:
            return None

        start_scan = max(mss_idx + 1, i - fvg_expiry)
        fvg_top = None
        fvg_bot = None
        fvg_idx = None
        for m in range(start_scan, i):
            if m < 2:
                continue
            c3h = float(high[m - 2])
            c3l = float(low[m - 2])
            c1h = float(high[m])
            c1l = float(low[m])
            c2c = float(close[m - 1])
            if fvg_bullish:
                gap = c1l - c3h
                if gap > 0 and gap >= fvg_min_atr * cur_atr and c2c > float(open_[m - 1]):
                    fvg_top = c1l
                    fvg_bot = c3h
                    fvg_idx = m
                    break
            else:
                gap = c3l - c1h
                if gap > 0 and gap >= fvg_min_atr * cur_atr and c2c < float(open_[m - 1]):
                    fvg_top = c3l
                    fvg_bot = c1h
                    fvg_idx = m
                    break
        if fvg_top is None:
            return None
        fvg_mid = 0.5 * (fvg_top + fvg_bot)

        # ZIRCON body filter
        for k in range(fvg_idx + 1, i + 1):
            ck = float(close[k])
            if fvg_bullish:
                if ck > fvg_mid:
                    return None
            else:
                if ck < fvg_mid:
                    return None

        # Entry at FVG midpoint, stop at FVG edge + buffer, target = 2R
        cur_h = float(high[i])
        cur_l = float(low[i])
        if direction == "long":
            if cur_l > fvg_mid:
                return None
            entry = fvg_mid
            stop = fvg_top + stop_buf_atr * cur_atr
            risk = entry - stop
            target = entry + target_r * abs(risk)
            return {"direction": "long", "entry": entry, "stop": stop, "target": target}
        else:
            if cur_h < fvg_mid:
                return None
            entry = fvg_mid
            stop = fvg_bot - stop_buf_atr * cur_atr
            risk = stop - entry
            target = entry - target_r * abs(risk)
            return {"direction": "short", "entry": entry, "stop": stop, "target": target}

    for d in ("long", "short"):
        res = find_setup(d)
        if res is not None:
            return res
    return None
