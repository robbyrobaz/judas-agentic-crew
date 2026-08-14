"""
ICT Judas-Continuation 5m — sweep + displacement + pullback.

Source pattern: finding 7481 (5m Judas-Continuation sweep+displacement+pullback).
Tested 90d at tight params: MNQ PF=9.26, MGC PF=3.30. Both rejected for n<20.

This version is the WINDOW-EXTENSION variant for 180d/252d testing
to clear the n>=20 gate.

Rules (5m bars):
  1. Look BACK `sweep_lookback` 5m bars; find the rolling swing high/low.
  2. SWEEP: current bar high > swing_high  OR  low < swing_low.
  3. DISPLACEMENT: the current bar must be a displacement candle —
     range > `disp_atr_mult` * ATR(14) AND body_ratio >= `body_ratio_min`.
  4. CONTINUATION: the close must be ON the sweep side of the swing
     (sweep-up + close above swing_high → long continuation;
     sweep-down + close below swing_low → short continuation).
  5. ENTRY: pullback to (1 - pullback_pct) * range from the sweep extreme.
     For long: entry = high - pullback_pct * range.
     For short: entry = low + pullback_pct * range.
  6. STOP: beyond the displacement extreme + `stop_buffer_atr` * ATR.
     For long: stop = low - stop_buffer_atr * atr.
     For short: stop = high + stop_buffer_atr * atr.
  7. TARGET: target_r * risk.

Holy grail alignment: this combines Judas (sweep) + CSID-156 (displacement
continuation) + the 5m NLP pullback entry. The novelty vs CSID-156 is the
SWEEP FILTER: CSID-156 fires on every displacement; this only fires
WHEN the displacement is preceded by a pivot sweep. That filter is what
takes PF from 2.13 → 9.26 on MNQ per finding 7481.

DISTINGUISHED FROM existing 5m actives:
- silver_bullet_pdh_pdl_retest: requires prior-day high/low, not 5m pivot
- cisd_3candle_fvg: requires 3-candle pattern + FVG
- mulham_liquidity_sweep_5m: continuation but no body_ratio filter
- ict_mitigation_block_5m: ENTRY at mitigation block midpoint (retrace),
  not pullback of displacement — different signal architecture
- ifvg_midpoint_reversion: reversion to FVG midpoint, not continuation
- ob_midpoint_reversion: reversion to OB midpoint, not continuation
- atr_disp_continuation (CSID 156 base): displacement + pullback but NO
  sweep filter — that's the NEW addition here.

Symbols to test: MGC, MNQ, MCL, 6J, ZF (DX/MBT/MET broker-blocked per LFE).
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


def _rolling_swing_high(high: pd.Series, lookback: int) -> pd.Series:
    return high.shift(1).rolling(lookback).max()


def _rolling_swing_low(low: pd.Series, lookback: int) -> pd.Series:
    return low.shift(1).rolling(lookback).min()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    sweep_lookback = int(params.get("sweep_lookback", 5))
    atr_period = int(params.get("atr_period", 14))
    body_ratio_min = float(params.get("body_ratio_min", 0.70))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.3))
    pullback_pct = float(params.get("pullback_pct", 0.40))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.10))

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
    rng = cur_high - cur_low
    if rng <= 0:
        return None
    body = abs(cur_close - float(bars["open"].iloc[i]))
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * cur_atr:
        return None

    direction = None
    entry = None
    stop = None

    # BULLISH continuation: sweep above swing high + close above swing high
    if cur_high > sh and cur_close > sh:
        direction = "long"
        entry = cur_high - pullback_pct * rng
        stop = cur_low - stop_buffer_atr * cur_atr
    # BEARISH continuation: sweep below swing low + close below swing low
    elif cur_low < sl and cur_close < sl:
        direction = "short"
        entry = cur_low + pullback_pct * rng
        stop = cur_high + stop_buffer_atr * cur_atr

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}
