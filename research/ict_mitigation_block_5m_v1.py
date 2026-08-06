"""
ICT Mitigation Block Continuation (5m).

Source: YT:SRESnt0r7DU + YT:Nkp29cVQf28 (FXNX 2026)

Distilled rules:
- A MITIGATION BLOCK is a candle range where institutional positions are
  underwater — the candle immediately BEFORE an aggressive directional break.
- Bearish example (mirror for bullish):
    1. Bank drives price UP into a swing-high trap (fails to take a prior high
       cleanly OR fails and reverses). Last green candle before the dump = block.
    2. Price breaks DOWN through prior swing low (LL confirmed).
    3. Price retraces BACK UP into the mitigation block zone.
    4. Institutions close losing longs at breakeven → price rejects → continues down.
  Bullish: mirror.

Entry execution:
- USE 5m chart.
- Detect: a swing low/high was broken in the last N bars with displacement
  (close moved through by >= min_break_atr * ATR).
- Mitigation block candle = the candle immediately preceding the break candle.
- Wait for price to retrace INTO the block range (block_low <= close <= block_high)
  — but NOT a fresh break-through.
- Enter at 50% equilibrium of the block (mitigation block midpoint).
- Stop: just outside the block edge (top of block for shorts, bottom for longs).
- Target: 2.0R (configurable).

Key distinguisher from existing strategies:
- Existing ifvg_midpoint_reversion / ob_midpoint_reversion are mean-reversion to
  midpoint of ANY FVG/OB — no requirement of a prior break before the retracement.
- Mitigation block is *continuation-biased*: requires a break first, then trades
  the retrace back to the candle before the break.
- This is the "rebalancing maneuver" pattern — institutions forced to close
  underwater positions at breakeven.

Symbols to test: MGC, MNQ, MCL, 6J, ZF (DX/MBT/MET broker-blocked).
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


def _rolling_swing_low(low: pd.Series, lookback: int) -> pd.Series:
    """Swing low = min of `lookback` bars back, shifted so it represents the prior N bars."""
    return low.shift(1).rolling(lookback).min()


def _rolling_swing_high(high: pd.Series, lookback: int) -> pd.Series:
    return high.shift(1).rolling(lookback).max()


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    # --- params ---
    pivot_len = int(params.get("pivot_len", 5))
    lookback = int(params.get("lookback", 40))         # how far back to scan for break
    min_break_atr = float(params.get("min_break_atr", 1.0))  # break displacement
    require_ll_after = bool(params.get("require_ll_after", True))  # require LL made after break
    require_hh_after = bool(params.get("require_hh_after", True))  # mirror
    target_r = float(params.get("target_r", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.2))  # stop buffer beyond block edge
    use_equilibrium_entry = bool(params.get("use_equilibrium_entry", True))
    entry_at_zone_edge = bool(params.get("entry_at_zone_edge", False))  # alt: enter on first touch
    max_retrace_bars = int(params.get("max_retrace_bars", 12))  # block must be revisited within N bars
    atr_period = int(params.get("atr_period", 14))

    n = len(bars)
    min_bars = max(lookback + pivot_len + 5, 60)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    open_ = bars["open"].astype(float) if "open" in bars.columns else close.shift(1).fillna(close)

    atr = _atr(bars, period=atr_period)
    swing_low = _rolling_swing_low(low, pivot_len)
    swing_high = _rolling_swing_high(high, pivot_len)

    i = n - 1  # signal bar is the most recent closed bar
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    # Scan the window [i - lookback, i] for a recent BEARISH mitigation setup:
    #   - There exists bar j where close[j] broke below swing_low[j] by >= min_break_atr * ATR
    #   - The mitigation block is bar (j-1) (or the bar immediately before the break).
    #   - If require_ll_after: there must be a bar k in (j, i] where low[k] < low[j] (new LL made)
    #   - Currently: price is retracing UP into the block zone (low[i] <= block_high, close[i] >= block_low)
    #     AND price has NOT broken above the block top with displacement.
    direction = None
    entry = None
    stop = None
    block_low = None
    block_high = None

    # === BEARISH PATH (short) ===
    # Find most recent bearish break in window.
    for j in range(i - 3, max(0, i - lookback) - 1, -1):
        if pd.isna(swing_low.iloc[j]) or pd.isna(atr.iloc[j]) or atr.iloc[j] <= 0:
            continue
        ref_sl = float(swing_low.iloc[j])
        cj = float(close.iloc[j])
        if cj <= ref_sl - min_break_atr * float(atr.iloc[j]):
            # Bearish break found at bar j. Mitigation block = bar (j-1).
            if j - 1 < 0:
                continue
            bl = float(low.iloc[j - 1])
            bh = float(high.iloc[j - 1])
            # Require a new LL between (j, i] if requested.
            ll_made = True
            if require_ll_after:
                ll_made = any(float(low.iloc[k]) < cj for k in range(j + 1, i + 1))
            if not ll_made:
                continue
            # Require that price is currently retracing into the block zone.
            # "Into the zone" = current bar's high enters from below the block top.
            if float(high.iloc[i]) >= bl and float(close.iloc[i]) <= bh * 1.001:
                # Price has retraced into block — signal short.
                direction = "short"
                block_low = bl
                block_high = bh
                mid = 0.5 * (bl + bh)
                if use_equilibrium_entry:
                    entry = mid
                elif entry_at_zone_edge:
                    entry = bh  # enter at top of block (worst case, confirms rejection)
                else:
                    entry = float(close.iloc[i])
                stop = bh + stop_buf_atr * cur_atr
                break

    if direction is None:
        # === BULLISH PATH (long) ===
        for j in range(i - 3, max(0, i - lookback) - 1, -1):
            if pd.isna(swing_high.iloc[j]) or pd.isna(atr.iloc[j]) or atr.iloc[j] <= 0:
                continue
            ref_sh = float(swing_high.iloc[j])
            cj = float(close.iloc[j])
            if cj >= ref_sh + min_break_atr * float(atr.iloc[j]):
                if j - 1 < 0:
                    continue
                bl = float(low.iloc[j - 1])
                bh = float(high.iloc[j - 1])
                hh_made = True
                if require_hh_after:
                    hh_made = any(float(high.iloc[k]) > cj for k in range(j + 1, i + 1))
                if not hh_made:
                    continue
                if float(low.iloc[i]) <= bh * 1.001 and float(close.iloc[i]) >= bl:
                    direction = "long"
                    block_low = bl
                    block_high = bh
                    mid = 0.5 * (bl + bh)
                    if use_equilibrium_entry:
                        entry = mid
                    elif entry_at_zone_edge:
                        entry = bl
                    else:
                        entry = float(close.iloc[i])
                    stop = bl - stop_buf_atr * cur_atr
                    break

    if direction is None or entry is None or stop is None:
        return None

    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None

    if direction == "long":
        target = entry + target_r * risk
    else:
        target = entry - target_r * risk

    return {"direction": direction, "entry": entry, "stop": stop, "target": target}