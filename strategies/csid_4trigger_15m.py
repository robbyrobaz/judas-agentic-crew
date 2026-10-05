"""ICT 4-Trigger 15m continuation setup.

Derived from YT:XDaiXFu6Xpg (ICT Market Theory, Oct 2 2026 NFP gold plan):
  1. SWEEP of internal buyside liquidity (REH — relative equal highs)
  2. AGGRESSIVE DOWNWARD DISPLACEMENT candle (large body, bearish close)
  3. MSS breaking recent swing low (lower-low formation)
  4. RETRACEMENT into fresh 15m FVG created by the displacement

For SHORT (sell continuation): the sequence is sweep high → big red candle →
break of swing low → price retraces up into the new gap → enter short.
For LONG (buy continuation): mirror logic with relative equal lows (REL).

Time filter: London (07-12 UTC) or NY (12-17 UTC) killzone only.

Params:
  target_r: R-multiple for TP (default 2.5; video suggested 3.5)
  tick: instrument tick size (0.10 for MGC)
  atr_period: ATR period for normalization (14)
  disp_atr_mult: displacement candle body must be >= this * ATR (1.5)
  disp_body_ratio: displacement body/range ratio threshold (0.70)
  fvg_min_size_atr: minimum FVG size in ATR units (0.10)
  stop_buf_atr: stop buffer above sweep high in ATR units (0.20)
  reh_tolerance_atr: REH max distance in ATR units (0.50)
  swing_length: bars each side for swing detection (5)
  lookback: max bars to look back for the 4-trigger sequence (60)
  direction_filter: 'both' | 'long' | 'short'
  killzone: 'london_ny' | 'ny_only' | 'london_only' | 'all'
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 120:
        return None

    target_r = float(params.get("target_r", 2.5))
    tick = float(params.get("tick", 0.10))
    atr_period = int(params.get("atr_period", 14))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.5))
    disp_body_ratio = float(params.get("disp_body_ratio", 0.70))
    fvg_min_size_atr = float(params.get("fvg_min_size_atr", 0.10))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.20))
    reh_tolerance_atr = float(params.get("reh_tolerance_atr", 0.50))
    swing_length = int(params.get("swing_length", 5))
    lookback = int(params.get("lookback", 60))
    direction_filter = params.get("direction_filter", "both")
    killzone = params.get("killzone", "london_ny")

    if "ts" in bars.columns:
        try:
            ts_dt = pd.to_datetime(bars["ts"], utc=True, errors="coerce")
            hours = ts_dt.dt.hour.values
            last_hour = hours[-1]
        except Exception:
            return None
    else:
        return None

    if killzone == "london_ny" and not ((7 <= last_hour < 12) or (12 <= last_hour < 17)):
        return None
    if killzone == "london_only" and not (7 <= last_hour < 12):
        return None
    if killzone == "ny_only" and not (12 <= last_hour < 17):
        return None

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)
    n = len(bars)
    last = n - 1

    if last < atr_period + swing_length * 2 + 5:
        return None

    # ATR (Wilder-style via rolling mean of TR)
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    atr_series = pd.Series(tr[1:last + 1]).rolling(atr_period, min_periods=atr_period).mean().values
    if np.isnan(atr_series[last]) or atr_series[last] <= 0:
        return None
    cur_atr = float(atr_series[last])

    # Try SHORT first, then LONG.  Return on first hit.
    directions_to_try = []
    if direction_filter in ("both", "short"):
        directions_to_try.append("short")
    if direction_filter in ("both", "long"):
        directions_to_try.append("long")

    for direction in directions_to_try:
        is_short = direction == "short"

        # Step 1: find REH (short) or REL (long) in lookback window BEFORE the last few bars
        pivot_levels = []
        lo = max(swing_length, last - lookback)
        hi = last - swing_length
        for j in range(lo, hi):
            if j < 0 or j >= n:
                continue
            is_pivot = True
            for k in range(j - swing_length, j + swing_length + 1):
                if k == j or k < 0 or k >= n:
                    continue
                if is_short:
                    if h[k] > h[j]:
                        is_pivot = False
                        break
                else:
                    if l[k] < l[j]:
                        is_pivot = False
                        break
            if is_pivot:
                pivot_levels.append((j, h[j] if is_short else l[j]))

        if len(pivot_levels) < 2:
            continue

        p1_idx, p1_val = pivot_levels[-1]
        p2_idx, p2_val = pivot_levels[-2]
        if abs(p1_val - p2_val) > reh_tolerance_atr * cur_atr:
            continue
        sweep_level = max(p1_val, p2_val) if is_short else min(p1_val, p2_val)

        # Step 2: find the SWEEP bar (h > sweep_level && c < sweep_level for short)
        sweep_bar = None
        for j in range(max(p1_idx, last - lookback) + 1, last):
            if j < 0 or j >= n:
                continue
            if is_short:
                if h[j] > sweep_level and c[j] < sweep_level:
                    sweep_bar = j
                    break
            else:
                if l[j] < sweep_level and c[j] > sweep_level:
                    sweep_bar = j
                    break

        if sweep_bar is None:
            continue

        # Step 3: find the DISPLACEMENT bar after the sweep
        disp_bar = None
        for j in range(sweep_bar + 1, last):
            if j < 0 or j >= n:
                continue
            body = abs(c[j] - o[j])
            rng = h[j] - l[j]
            if rng <= 0:
                continue
            if atr_series[j] <= 0 or np.isnan(atr_series[j]):
                continue
            if is_short:
                if c[j] >= o[j]:
                    continue
            else:
                if c[j] <= o[j]:
                    continue
            if body < disp_atr_mult * atr_series[j]:
                continue
            if body / rng < disp_body_ratio:
                continue
            disp_bar = j
            break

        if disp_bar is None:
            continue

        # Step 4: verify a FVG was created by the displacement
        if disp_bar < 1 or disp_bar + 1 >= n:
            continue
        # FVG formed by displacement (j-1, j, j+1 candles)
        # SHORT (bearish FVG): fvg_top = l[j-1], fvg_bottom = h[j+1]; gap is positive when h[j+1] < l[j-1]
        # LONG  (bullish FVG): fvg_top = l[j+1], fvg_bottom = h[j-1]; gap is positive when l[j+1] > h[j-1]
        if is_short:
            fvg_top = l[disp_bar - 1]
            fvg_bottom = h[disp_bar + 1]
            if fvg_top <= fvg_bottom:
                continue
            if fvg_top - fvg_bottom < fvg_min_size_atr * cur_atr:
                continue
        else:
            fvg_top = l[disp_bar + 1]
            fvg_bottom = h[disp_bar - 1]
            if fvg_top <= fvg_bottom:
                continue
            if fvg_top - fvg_bottom < fvg_min_size_atr * cur_atr:
                continue

        # Step 5: find MSS bar (break of recent swing low/high)
        # Recent swing = extremes in the 5 bars before the displacement
        if disp_bar < 5:
            continue
        recent_swing_extreme = (
            min(l[disp_bar - 5:disp_bar]) if is_short
            else max(h[disp_bar - 5:disp_bar])
        )
        mss_bar = None
        for j in range(disp_bar + 1, last):
            if j < 0 or j >= n:
                continue
            if is_short:
                if l[j] < recent_swing_extreme and c[j] < recent_swing_extreme:
                    mss_bar = j
                    break
            else:
                if h[j] > recent_swing_extreme and c[j] > recent_swing_extreme:
                    mss_bar = j
                    break

        if mss_bar is None:
            # No MSS confirmed yet, but the FVG may still be valid if it's a fresh FVG.
            # Allow signal if the FVG was just created (last few bars) and the LAST bar is retracing.
            mss_bar = disp_bar + 1  # treat as if MSS just happened

        # Step 6: the LAST bar (or recent bar) should be retracing into the FVG
        retrace_bar = None
        search_lo = max(mss_bar, disp_bar + 2)
        for j in range(search_lo, last + 1):
            if j < 0 or j >= n:
                continue
            if is_short:
                # Short entry: price retraces UP into the FVG (FVG top is resistance for shorts)
                if l[j] <= fvg_top and h[j] >= fvg_bottom:
                    retrace_bar = j
                    break
            else:
                # Long entry: price retraces DOWN into the FVG (FVG bottom is support for longs)
                if h[j] >= fvg_bottom and l[j] <= fvg_top:
                    retrace_bar = j
                    break

        if retrace_bar is None:
            continue

        # Cooldown: if the retrace bar is too old, skip (avoid stale FVG entries)
        if last - retrace_bar > 8:
            continue

        # Step 7: build the signal
        if is_short:
            entry = c[last]  # use last close as entry
            stop = h[sweep_bar] + stop_buf_atr * cur_atr
            risk = stop - entry
            if risk < tick:
                continue
            target = entry - target_r * risk
        else:
            entry = c[last]
            stop = l[sweep_bar] - stop_buf_atr * cur_atr
            risk = entry - stop
            if risk < tick:
                continue
            target = entry + target_r * risk

        return {
            "ts": bars["ts"].iloc[last] if "ts" in bars.columns else None,
            "direction": direction,
            "entry": float(entry),
            "stop": float(stop),
            "target": float(target),
        }

    return None
