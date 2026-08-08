"""
ICT 5m Sweep-Fade Mean-Reversion (MGC).

Source: YT:ks6wOGghzoA (ICT FINATIC, Apr 2026). Counter-rule explicitly stated:
"When price takes a 5-minute swing high and I still take a long, the majority of
the time it actually gets stopped out." → After a 5m swing sweep in direction X,
the move tends to FAIL. The standard silver-bullet continuation is the wrong bet;
the edge is in FADING the sweep.

HYPO: A 5m swing sweep that fails to displace (no body continuation past the
sweep extreme) is a Judas-style institutional liquidity grab. Fade it back
toward the prior swing midpoint (the level the sweep breached from).

ENTRY (long version; mirror for short):
  1. WITHIN last `pivot_len` 5m bars, find the prior 5m swing HIGH.
  2. Current bar's high SWEEPS that swing high (high > swing_high) by at least
     `sweep_min_atr` * ATR — qualifies as a "sweep".
  3. SWEEP-FAIL confirmation: current bar CLOSES BACK below the prior swing
     high (not just a wick; the body must fail). This is the "failed sweep" /
     Judas signal.
  4. Counter-filter: if last 3 bars already broke the swing high with displacement
     (close - swing_high > `disp_atr` * ATR), this is genuine continuation, NOT
     a Judas — skip.
  5. Entry: at current close (the failed sweep close).
  6. Stop: sweep extreme + `stop_buf_atr` * ATR.
  7. Target: target_r * risk (default 1.5R — small R for high-WR fade setups).

WHY THIS DIFFERS FROM EXISTING 5m FAMILIES:
  - ifvg_midpoint_reversion / ob_midpoint_reversion: reversion to FVG/OB
    midpoint; require a prior FVG/OB formation. This is purely sweep-driven.
  - cisd_3candle_fvg: requires a 3-candle CISD pattern AND an FVG. This is
    a 1-2 candle sweep-fail.
  - silver_bullet_pdh_pdl_retest: continuation after sweep, not fade.
  - mulham_liquidity_sweep_5m: continuation pattern, not fade.
  - ict_mitigation_block_5m: continuation after break + retrace.

This strategy is the EXPLICIT inverse of the silver-bullet continuation family.
If ICT FINATIC's observation is correct (5m sweeps → continuation fails more
than half the time), this fade should harvest the failed sweeps.

DISTINGUISHED FROM: existing Judas / sweep strategies that ALWAYS enter in the
direction of the sweep. This one ALWAYS enters AGAINST the sweep direction
(sweep-fail → fade).
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


def _rolling_swing_high(high, pivot_len):
    return high.shift(1).rolling(pivot_len).max()


def _rolling_swing_low(low, pivot_len):
    return low.shift(1).rolling(pivot_len).min()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    pivot_len = int(params.get("pivot_len", 5))           # 5m swing lookback
    sweep_min_atr = float(params.get("sweep_min_atr", 0.05))  # min sweep extension (very small)
    target_r = float(params.get("target_r", 1.5))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.15))
    atr_period = int(params.get("atr_period", 14))
    disp_atr_skip = float(params.get("disp_atr_skip", 0.3))  # skip if displaced past sweep
    require_session = bool(params.get("require_session", False))  # if True, only fire in 10-11 / 14-15 ET
    session_window_utc = params.get("session_window_utc", None)  # optional list of (h_start, h_end)
    if session_window_utc is None:
        # default: any session
        session_window_utc = [(0, 24)]

    n = len(bars)
    min_bars = max(pivot_len + 10, atr_period + 5, 60)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    atr = _atr(bars, period=atr_period)
    swing_high = _rolling_swing_high(high, pivot_len)
    swing_low = _rolling_swing_low(low, pivot_len)

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    # Optional session filter — bars must have a UTC hour-attribute.
    if require_session and "hour_utc" in bars.columns:
        hour = float(bars["hour_utc"].iloc[i])
        if not any(h_start <= hour < h_end for h_start, h_end in session_window_utc):
            return None

    cur_close = float(close.iloc[i])
    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])
    cur_sh = float(swing_high.iloc[i]) if not pd.isna(swing_high.iloc[i]) else None
    cur_sl = float(swing_low.iloc[i]) if not pd.isna(swing_low.iloc[i]) else None

    if cur_sh is None or cur_sl is None or cur_sh <= cur_sl:
        return None

    direction = None
    entry = None
    stop = None

    # === LONG (fade an upside sweep) ===
    # Sweep: current bar high > prior 5m swing high by sweep_min_atr*ATR
    if cur_high >= cur_sh + sweep_min_atr * cur_atr:
        # Sweep-fail: close back BELOW the swing high (the body fails)
        if cur_close < cur_sh:
            # Counter-filter: skip if last 3 bars already displaced past swing high
            # (genuine continuation, not Judas)
            displaced = False
            for k in range(max(0, i - 3), i):
                if k < 0:
                    continue
                ck = float(close.iloc[k])
                if ck >= cur_sh + disp_atr_skip * cur_atr:
                    displaced = True
                    break
            if not displaced:
                direction = "long"
                entry = cur_close
                stop = cur_high + stop_buf_atr * cur_atr

    # === SHORT (fade a downside sweep) ===
    if direction is None and cur_low <= cur_sl - sweep_min_atr * cur_atr:
        if cur_close > cur_sl:
            displaced = False
            for k in range(max(0, i - 3), i):
                if k < 0:
                    continue
                ck = float(close.iloc[k])
                if ck <= cur_sl - disp_atr_skip * cur_atr:
                    displaced = True
                    break
            if not displaced:
                direction = "short"
                entry = cur_close
                stop = cur_low - stop_buf_atr * cur_atr

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}