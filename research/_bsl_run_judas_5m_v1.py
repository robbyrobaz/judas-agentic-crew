"""ICT BSL/SSL Run Judas 5m — sweep of clustered equal-highs/lows + MSS + FVG pullback.

Source: finding from YT:arAPaOchmnk: "ICT Liquidity Sweep & Bearish XAUUSD Setup"
(Sept 24, 2026). Distinct from CSID-156 base and Judas-Continuation 5m v1 because:

  1. SWEEP TARGET is a CLUSTER of relative equal highs/lows (BSL/SSL run)
     within an ATR tolerance over a window — i.e., the ICT "inducement"
     pattern (institutions engineer stops at obvious BSL/SSL pools).
     Judas-Continuation 5m v1 fires on ANY rolling pivot sweep; this only
     fires when at least N pivot highs/lows cluster tightly together.
  2. MSS FILTER: price must close back through the cluster midpoint after
     the sweep (judas swing = fake breakout + reversal close).
  3. ENTRY: pullback into the displacement bar's body (FVG/OB midpoint
     approximation). Same family as Judas-Continuation pullback but
     triggered by different signal.

Params:
  eq_window:           bars back to scan for equal-highs/lows cluster (default 20)
  eq_tol_atr:          tolerance band in ATR multiples for "equal" (default 0.30)
  eq_min_touches:      min number of swing pivots inside the band (default 2)
  pivot_len:           left/right bars for swing pivot detection (default 3)
  body_ratio_min:      min body/range on displacement bar (default 0.65)
  disp_atr_mult:       min range / ATR on displacement bar (default 1.2)
  pullback_pct:        entry offset from sweep extreme, fraction of range (default 0.50)
  target_r:            target multiple of risk (default 1.5)
  stop_buffer_atr:     stop distance beyond sweep extreme in ATR (default 0.10)
  allowed_directions:  list subset of ["long","short"]; None = both (default None)

Timeframe: 5m (works on 1h/15m too but tuned for 5m execution).
Symbols to test: MGC, MNQ, MCL, 6J, ZF.
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


def _swing_highs(high: pd.Series, k: int) -> pd.Series:
    """Mark True at bars that are the highest of a k-left/k-right window."""
    out = pd.Series(False, index=high.index)
    for i in range(k, len(high) - k):
        w = high.iloc[i - k:i + k + 1]
        if high.iloc[i] >= w.max():
            out.iloc[i] = True
    return out


def _swing_lows(low: pd.Series, k: int) -> pd.Series:
    out = pd.Series(False, index=low.index)
    for i in range(k, len(low) - k):
        w = low.iloc[i - k:i + k + 1]
        if low.iloc[i] <= w.min():
            out.iloc[i] = True
    return out


def evaluate(bars: pd.DataFrame, params: dict) -> dict | None:
    eq_window = int(params.get("eq_window", 20))
    eq_tol_atr = float(params.get("eq_tol_atr", 0.30))
    eq_min_touches = int(params.get("eq_min_touches", 2))
    pivot_len = int(params.get("pivot_len", 3))
    body_ratio_min = float(params.get("body_ratio_min", 0.65))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.2))
    pullback_pct = float(params.get("pullback_pct", 0.50))
    target_r = float(params.get("target_r", 1.5))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.10))
    allowed = params.get("allowed_directions")
    atr_period = int(params.get("atr_period", 14))

    n = len(bars)
    min_bars = max(eq_window + pivot_len + 5, atr_period + 5, 30)
    if n < min_bars:
        return None

    close = bars["close"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    opn = bars["open"].astype(float)
    atr = _atr(bars, period=atr_period)

    i = n - 1
    if pd.isna(atr.iloc[i]) or atr.iloc[i] <= 0:
        return None
    cur_atr = float(atr.iloc[i])

    tol = eq_tol_atr * cur_atr

    # === Find BSL cluster (equal highs) in the last eq_window bars before current ===
    # Use swing pivots over window [i-eq_window, i-1] to find cluster of highs.
    lo = max(pivot_len, i - eq_window)
    hi_idx = i - 1
    if hi_idx - lo < pivot_len * 2:
        return None

    sh_mask = _swing_highs(high.iloc[lo:hi_idx + 1], pivot_len)
    sl_mask = _swing_lows(low.iloc[lo:hi_idx + 1], pivot_len)

    bsl_highs = []
    ssl_lows = []
    for j in range(len(sh_mask)):
        if sh_mask.iloc[j]:
            bsl_highs.append(float(high.iloc[lo + j]))
        if sl_mask.iloc[j]:
            ssl_lows.append(float(low.iloc[lo + j]))

    # Filter clusters: need at least eq_min_touches pivots within tol of each other
    def _cluster(levels, tol):
        if not levels:
            return None, None
        levels_sorted = sorted(levels)
        best_run = 1
        best_lo = levels_sorted[0]
        best_hi = levels_sorted[0]
        run = 1
        run_lo = levels_sorted[0]
        run_hi = levels_sorted[0]
        for v in levels_sorted[1:]:
            if v - run_lo <= tol:
                run += 1
                run_hi = v
            else:
                if run > best_run:
                    best_run = run
                    best_lo = run_lo
                    best_hi = run_hi
                run = 1
                run_lo = v
                run_hi = v
        if run > best_run:
            best_run = run
            best_lo = run_lo
            best_hi = run_hi
        if best_run >= eq_min_touches:
            return (best_lo + best_hi) / 2.0, (best_hi - best_lo)
        return None, None

    bsl_level, bsl_spread = _cluster(bsl_highs, tol)
    ssl_level, ssl_spread = _cluster(ssl_lows, tol)

    cur_close = float(close.iloc[i])
    cur_high = float(high.iloc[i])
    cur_low = float(low.iloc[i])
    cur_open = float(opn.iloc[i])
    rng = cur_high - cur_low
    if rng <= 0:
        return None
    body = abs(cur_close - cur_open)
    body_r = body / rng
    if body_r < body_ratio_min:
        return None
    if rng < disp_atr_mult * cur_atr:
        return None

    direction = None
    entry = None
    stop = None

    # BEARISH setup: sweep BSL (cluster of equal highs) + close back below it
    if bsl_level is not None and cur_high > bsl_level and cur_close < bsl_level:
        if allowed is None or "short" in allowed:
            direction = "short"
            entry = cur_low + pullback_pct * rng  # retrace entry into the bar
            stop = cur_high + stop_buffer_atr * cur_atr
    # BULLISH setup: sweep SSL (cluster of equal lows) + close back above it
    elif ssl_level is not None and cur_low < ssl_level and cur_close > ssl_level:
        if allowed is None or "long" in allowed:
            direction = "long"
            entry = cur_high - pullback_pct * rng
            stop = cur_low - stop_buffer_atr * cur_atr

    if direction is None or entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0 or risk > 4.0 * cur_atr:
        return None
    target = entry + target_r * risk if direction == "long" else entry - target_r * risk
    return {"direction": direction, "entry": entry, "stop": stop, "target": target}
