"""
ICT Judas Swing 5m — Fresh Family for Uncovered Symbols (DX, MBT, MET).

Why this is NEW vs the burned-out families:
  - judas_1h (3 retirements on MCL): 1h bars, native engine, IL hours filter
  - custom_5m MSS+Fib (4 ZOMBIE rejections on MGC/MCL/ZF/MBT): 5m MSS+0.618 Fib
  - custom_5m atr_disp (4 retirements on MNQ): 5m ATR displacement continuation
  - This: pure 5m Judas swing — sweep of recent pivot + reversal confirmation

Rules (5m bars):
  1) Compute rolling swing pivots: max(high, last `pivot_len` bars) for swing-high,
     min(low, last `pivot_len` bars) for swing-low.
  2) Detect sweep: bar high > swing-high by >= `min_sweep_ticks` ticks
     (bullish sweep = false breakout UP) OR bar low < swing-low by >= ticks (bearish sweep).
  3) Confirm reversal within `confirmation_bars` (close back inside prior range).
  4) Entry: at confirmation close.
  5) Stop: beyond sweep wick +/- stop_buffer_ticks.
  6) Target: target_r * risk.

Why 5m for DX: DX trends cleanly in NY session (12:30-15:00 UTC killzone).
The Judas pattern — initial false break that traps — is the textbook ICT
London/NY setup applied to a 5m timeframe.

Backtest: 90d, 5m native bars, on DX, MBT, MET, MCL (all uncovered or burned-out).
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


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    pivot_len = int(params.get("pivot_len", 4))
    min_sweep_ticks = float(params.get("min_sweep_ticks", 3.0))
    confirmation_bars = int(params.get("confirmation_bars", 3))
    stop_buf_ticks = float(params.get("stop_buf_ticks", 2.0))
    target_r = float(params.get("target_r", 2.0))
    # tick size for the symbol (in price units); the runtime will pass `tick_size`
    # if the engine supports it; otherwise default to 0.01 for micro futures
    tick_size = float(params.get("tick_size", 0.01))
    sweep_ticks = min_sweep_ticks * tick_size
    buf_ticks = stop_buf_ticks * tick_size

    n = len(bars)
    if n < pivot_len + confirmation_bars + 5:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)

    cur_i = n - 1

    # Rolling swing pivots (look BACK `pivot_len` bars; current bar is the test)
    # Swing high reference: max of highs over [cur_i - pivot_len, cur_i - 1]
    # Swing low reference:  min of lows over [cur_i - pivot_len, cur_i - 1]
    pivot_high = float(high.iloc[cur_i - pivot_len:cur_i].max())
    pivot_low = float(low.iloc[cur_i - pivot_len:cur_i].min())

    # Confirm pivots are not nan/equal (degenerate when flat)
    if pivot_high <= pivot_low:
        return None

    cur_high = float(high.iloc[cur_i])
    cur_low = float(low.iloc[cur_i])
    cur_close = float(close.iloc[cur_i])

    direction = None
    sweep_wick = None

    # Bullish sweep (false breakout UP -> reversal DOWN): high pierces pivot high
    if cur_high >= pivot_high + sweep_ticks:
        # Reversal confirmation: close back BELOW pivot high
        if cur_close < pivot_high:
            direction = "short"
            sweep_wick = cur_high
    # Bearish sweep (false breakout DOWN -> reversal UP): low pierces pivot low
    elif cur_low <= pivot_low - sweep_ticks:
        if cur_close > pivot_low:
            direction = "long"
            sweep_wick = cur_low

    if direction is None:
        return None

    # Use ATR as a sanity stop (in case swing-based stop is degenerate)
    atr = _atr(bars).iloc[cur_i]
    if pd.isna(atr) or atr <= 0:
        return None
    atr = float(atr)

    entry = cur_close
    if direction == "short":
        # Stop above the sweep wick
        stop = sweep_wick + buf_ticks
        risk = stop - entry
        if risk <= 0 or risk > 2.5 * atr:
            return None
        target = entry - target_r * risk
    else:  # long
        stop = sweep_wick - buf_ticks
        risk = entry - stop
        if risk <= 0 or risk > 2.5 * atr:
            return None
        target = entry + target_r * risk

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}
