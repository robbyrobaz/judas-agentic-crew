"""
ICT 50EMA Pullback with LTF CHOCH + Engulfing/Pin-Bar Confirmation.

Source: ICT Market Theory "ICT Trading Strategy: GOLD Analysis" (s_mNFAHe6Mc, May 25 2026).

Testable rules distilled from the 8-min top-down ICT gold breakdown:

  PRIMARY SETUP (long bias, mirrored for short):
    1. Recent structural break: within the last `struct_lookback` bars, price
       closed ABOVE a prior swing high (bullish CHOCH) — confirms institutional
       intent has flipped from sell-side to buy-side.
    2. Pullback: the most recent bar's low is within `pullback_atr_mult` * ATR
       of the 50 EMA (touched or dipped into the EMA zone).
    3. Confirmation candle: the current bar is a bullish reversal bar:
         - Bullish engulfing (close > prior close, open < prior open, close > open)
         OR
         - Pin bar / hammer (long lower wick >= `wick_ratio` * body).
    4. Stop: below the pullback swing low minus `stop_buf_ticks` * tick_size.
    5. Target: `target_r` * risk (default 2.5R).

  INVALIDATION safety: skip if recent bars show a clear bearish structure break
  (CHOCH down) that would override the bullish bias.

  WHY THIS DIFFERS FROM EXISTING 5m/15m FAMILIES:
    - ifvg_midpoint_reversion: enters ON FVG fill, mean-reversion to midpoint.
      This one enters on continuation after pullback, targeting BSL pool.
    - ob_midpoint_reversion: enters at OB midpoint, mean-reversion.
      This one uses 50 EMA as the dynamic POI (institutional defense line).
    - silver_bullet_pdh_pdl: uses PDH/PDL levels + 10-11 EST window filter.
      This one is session-agnostic, uses structural swing high + 50 EMA.

  TARGETS: DX, MBT, MET — all uncovered. DX trends cleanly; MBT/MET crypto
  volatility should give the 2.5R target room to breathe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _atr(bars: pd.DataFrame, period: int = 14) -> pd.Series:
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    # --- params ---
    ema_len = int(params.get("ema_len", 50))
    struct_lookback = int(params.get("struct_lookback", 20))   # bars to look back for CHOCH
    pivot_len = int(params.get("pivot_len", 5))                  # for swing high detection
    pullback_atr_mult = float(params.get("pullback_atr_mult", 1.2))
    wick_ratio = float(params.get("wick_ratio", 2.0))            # min wick:body for pin bar
    stop_buf_ticks = float(params.get("stop_buf_ticks", 3.0))
    target_r = float(params.get("target_r", 2.5))
    tick_size = float(params.get("tick_size", 0.01))
    atr_period = int(params.get("atr_period", 14))
    min_struct_break_atr = float(params.get("min_struct_break_atr", 0.5))  # min move to qualify

    n = len(bars)
    min_bars = max(ema_len + 5, struct_lookback + pivot_len + 5)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    open_ = bars["open"].astype(float) if "open" in bars.columns else close.shift(1).fillna(close)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    # Indicators
    ema = close.ewm(span=ema_len, adjust=False).mean()
    atr = _atr(bars, period=atr_period)

    i = n - 1
    if pd.isna(ema.iloc[i]) or pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None

    cur_close = float(close.iloc[i])
    cur_open = float(open_.iloc[i])
    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])
    cur_atr = float(atr.iloc[i])
    cur_ema = float(ema.iloc[i])

    # Find the most recent swing HIGH over [i - struct_lookback, i - pivot_len]
    # and check if any bar in [i - struct_lookback, i] closed ABOVE it (bullish CHOCH).
    # Only consider bars with enough displacement (close - swing_high >= min_struct_break_atr * ATR).
    look_start = max(0, i - struct_lookback)
    look_end = i  # include current bar's context

    # Compute a rolling swing-high reference at each bar in window: max(high, pivot_len back)
    # For simplicity: take a SINGLE recent swing-high = max high in [look_start, look_end - pivot_len]
    # (the structural level that price needs to break above).
    ref_start = look_start
    ref_end = max(look_start, look_end - pivot_len)
    swing_high_ref = float(high.iloc[ref_start:ref_end].max()) if ref_end > ref_start else float(high.iloc[look_start])
    swing_low_ref = float(low.iloc[ref_start:ref_end].min()) if ref_end > ref_start else float(low.iloc[look_start])

    if swing_high_ref <= swing_low_ref:
        return None

    # === BULLISH PATH ===
    # 1) Recent bullish CHOCH: any close in window closed above swing_high_ref by >= min displacement.
    bullish_choch = False
    for j in range(look_start, look_end + 1):
        if j >= n:
            continue
        cj = float(close.iloc[j])
        if cj >= swing_high_ref + min_struct_break_atr * cur_atr:
            bullish_choch = True
            break

    # 2) Pullback to 50 EMA: low within pullback_atr_mult * ATR of EMA.
    pullback_zone = cur_ema + pullback_atr_mult * cur_atr
    pulled_back = cur_low <= pullback_zone

    # 3) Bullish reversal candle: engulfing or pin bar.
    prev_close = float(close.iloc[i - 1]) if i >= 1 else cur_close
    prev_open = float(open_.iloc[i - 1]) if i >= 1 else cur_open
    body = cur_close - cur_open
    lower_wick = min(cur_open, cur_close) - cur_low
    upper_wick = cur_high - max(cur_open, cur_close)

    # Engulfing: current bar's body engulfs prior bar's body AND closes bullish.
    bullish_engulfing = (cur_close > cur_open) and (cur_open < prev_close) and (cur_close > prev_open)
    # Pin bar / hammer: small body, long lower wick.
    is_pin = (lower_wick >= wick_ratio * abs(body)) and (lower_wick >= upper_wick) and (body >= 0)

    bull_confirm = bullish_engulfing or is_pin

    # 4) Invalidation: if a more recent bearish CHOCH happened, skip.
    bearish_choch_recent = False
    for j in range(look_start, look_end + 1):
        if j >= n:
            continue
        cj = float(close.iloc[j])
        if cj <= swing_low_ref - min_struct_break_atr * cur_atr:
            bearish_choch_recent = True
            break

    # === SHORT PATH (mirror) ===
    bearish_choch = False
    for j in range(look_start, look_end + 1):
        if j >= n:
            continue
        cj = float(close.iloc[j])
        if cj <= swing_low_ref - min_struct_break_atr * cur_atr:
            bearish_choch = True
            break

    pull_up_zone = cur_ema - pullback_atr_mult * cur_atr
    pulled_up = cur_high >= pull_up_zone

    # Bearish engulfing: current body engulfs prior, closes bearish.
    bearish_engulfing = (cur_close < cur_open) and (cur_open > prev_close) and (cur_close < prev_open)
    is_shooting_star = (upper_wick >= wick_ratio * abs(body)) and (upper_wick >= lower_wick) and (body <= 0)
    bear_confirm = bearish_engulfing or is_shooting_star

    bullish_choch_recent = False  # we check bearish override for shorts, bullish for longs.
    for j in range(look_start, look_end + 1):
        if j >= n:
            continue
        cj = float(close.iloc[j])
        if cj >= swing_high_ref + min_struct_break_atr * cur_atr:
            bullish_choch_recent = True
            break

    direction = None
    entry = None
    stop = None

    if bullish_choch and pulled_back and bull_confirm and not bearish_choch_recent:
        direction = "long"
        entry = cur_close
        # Stop below the pullback bar's low (or below EMA zone).
        pullback_low = cur_low
        stop = pullback_low - stop_buf_ticks * tick_size
    elif bearish_choch and pulled_up and bear_confirm and not bullish_choch_recent:
        direction = "short"
        entry = cur_close
        pullback_high = cur_high
        stop = pullback_high + stop_buf_ticks * tick_size

    if direction is None or entry is None or stop is None:
        return None

    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        # Risk too tight or absurd — skip.
        return None

    if direction == "long":
        target = entry + target_r * risk
    else:
        target = entry - target_r * risk

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}