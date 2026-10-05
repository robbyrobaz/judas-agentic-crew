"""Zen Liquidity Sweep Model (5m) — sweep + iFVG entry.

Based on YT:pmexdsocN9c Zen Trades "Liquidity Sweep" model.

Concept (5m-bar approximation):
  1) Track the most recent swing H/L using a 3-bar pivot on 5m bars
     (mid-bar pivot high/low is the liquidity level).
  2) Sweep: a 5m bar trades BEYOND that swing (high > swing_high for shorts,
     low < swing_low for longs) AND closes back inside the range.
  3) Within K bars after the sweep, a 3-bar FVG (Fair Value Gap) forms and
     gets "inversed" — i.e., a subsequent bar closes THROUGH the FVG zone
     from the wrong side, signaling continuation against the sweep.
  4) Enter at the close of the IFVG confirmation bar.
  5) Stop = sweep wick (extreme of the sweep bar) +/- small buffer.
  6) Target = R-multiple of risk.

HTF filter: trade only when the recent bias aligns with the trade direction
(opposing-direction FVG must exist in last L bars, OR all recent 5m swings
form lower-highs for shorts / higher-lows for longs).

This is a high-frequency 5m pattern; expect ~30-80 trades / 90 days on MGC.
"""

import pandas as pd


def evaluate(bars, params):
    n = len(bars)
    if n < 60:
        return None

    i = n - 1

    pivot_len = int(params.get("pivot_len", 3))
    sweep_lookback = int(params.get("sweep_lookback", 30))
    ifvg_window = int(params.get("ifvg_window", 8))
    min_sweep_atr = float(params.get("min_sweep_atr", 0.10))
    target_r = float(params.get("target_r", 2.0))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.10))
    ema_period = int(params.get("ema_period", 20))
    bias_lookback = int(params.get("bias_lookback", 12))

    highs = bars["high"].astype(float).values
    lows = bars["low"].astype(float).values
    closes = bars["close"].astype(float).values
    opens = bars["open"].astype(float).values

    tr = []
    for j in range(n):
        if j == 0:
            tr.append(highs[j] - lows[j])
        else:
            tr.append(max(highs[j] - lows[j],
                          abs(highs[j] - closes[j - 1]),
                          abs(lows[j] - closes[j - 1])))
    atr_series = pd.Series(tr).rolling(ema_period).mean()
    atr = atr_series.values
    cur_atr = atr[i]
    if pd.isna(cur_atr) or cur_atr <= 0 or i < ema_period + 30:
        return None

    # --- Step 1: find recent swing highs/lows via 3-bar (pivot_len) pivots ---
    # Scan bars [i - sweep_lookback .. i - pivot_len - 1] for pivot highs/lows
    pivot_high = None
    pivot_low = None
    scan_start = max(ema_period + 5, i - sweep_lookback)
    scan_end = i - pivot_len  # must have `pivot_len` bars to right
    for j in range(scan_end, scan_start - 1, -1):
        # pivot high: high[j] is strictly greater than highs[j-pivot_len..j-1] and [j+1..j+pivot_len]
        left = all(highs[j] > highs[j - k] for k in range(1, pivot_len + 1))
        right = all(highs[j] > highs[j + k] for k in range(1, pivot_len + 1))
        if left and right:
            pivot_high = (j, highs[j])
            break
    for j in range(scan_end, scan_start - 1, -1):
        left = all(lows[j] < lows[j - k] for k in range(1, pivot_len + 1))
        right = all(lows[j] < lows[j + k] for k in range(1, pivot_len + 1))
        if left and right:
            pivot_low = (j, lows[j])
            break

    if pivot_high is None and pivot_low is None:
        return None

    # --- Step 2: detect sweep in the recent window ---
    # Bear sweep (short setup): bar trades above pivot_high AND closes inside.
    # Bull sweep (long setup): bar trades below pivot_low AND closes inside.
    sweep_dir = None
    sweep_bar = None
    sweep_extreme = None
    sweep_close = None

    for j in range(i - pivot_len, max(i - ifvg_window - pivot_len, scan_start) - 1, -1):
        if pivot_high is not None and highs[j] > pivot_high[1] and closes[j] < pivot_high[1]:
            if (highs[j] - pivot_high[1]) >= cur_atr * min_sweep_atr:
                sweep_dir = "short"
                sweep_bar = j
                sweep_extreme = highs[j]
                sweep_close = closes[j]
                break
        if pivot_low is not None and lows[j] < pivot_low[1] and closes[j] > pivot_low[1]:
            if (pivot_low[1] - lows[j]) >= cur_atr * min_sweep_atr:
                sweep_dir = "long"
                sweep_bar = j
                sweep_extreme = lows[j]
                sweep_close = closes[j]
                break

    if sweep_dir is None:
        return None

    # --- Step 3: in the bars AFTER the sweep, look for an IFVG confirmation ---
    # Bullish IFVG (for long setup):
    #   Prior move DOWN created a 3-bar bearish FVG (c1 high < c3 low).
    #   Then bar after closes ABOVE c1 high (inversing the FVG).
    # Bearish IFVG (for short setup):
    #   Prior move UP created a 3-bar bullish FVG (c3 low > c1 high).
    #   Then bar after closes BELOW c1 low.
    #
    # We look for this pattern in bars (sweep_bar+1 .. i).
    confirm_bar = None
    fvg_top, fvg_bot = None, None
    start = sweep_bar + 1
    end = i  # confirm bar = current bar i
    if end - start < 2:
        return None

    for j in range(start + 2, end + 1):
        a1_o, a1_h, a1_l, a1_c = opens[j - 2], highs[j - 2], lows[j - 2], closes[j - 2]
        a2_o, a2_h, a2_l, a2_c = opens[j - 1], highs[j - 1], lows[j - 1], closes[j - 1]
        a3_o, a3_h, a3_l, a3_c = opens[j], highs[j], lows[j], closes[j]

        if sweep_dir == "long":
            # prior move down created bearish FVG: a1_h < a3_l (gap up)
            # IFVG: a3 closes above a1_h
            if a1_h < a3_l and a3_c > a1_h:
                confirm_bar = j
                fvg_top = a1_h
                fvg_bot = a3_l
                break
        else:  # short
            # prior move up created bullish FVG: a3_l > a1_h (gap down)
            # IFVG: a3 closes below a1_l
            if a3_l > a1_h and a3_c < a1_l:
                confirm_bar = j
                fvg_top = a3_l
                fvg_bot = a1_h
                break

    if confirm_bar is None:
        return None
    if confirm_bar != i:
        return None  # only fire on confirmation bar close

    # --- Step 4: bias filter — opposing-direction bias should be in place ---
    bias_up = all(closes[j] > closes[j - 1] for j in range(i - bias_lookback + 1, i + 1))
    bias_dn = all(closes[j] < closes[j - 1] for j in range(i - bias_lookback + 1, i + 1))
    if sweep_dir == "long" and not bias_up:
        return None
    if sweep_dir == "short" and not bias_dn:
        return None

    # --- Step 5: entry, stop, target ---
    entry = closes[confirm_bar]
    if sweep_dir == "long":
        stop = sweep_extreme - cur_atr * stop_buffer_atr
    else:
        stop = sweep_extreme + cur_atr * stop_buffer_atr
    risk = abs(entry - stop)
    if risk <= 0 or risk > cur_atr * 3.0:
        return None
    target = entry + target_r * risk * (1 if sweep_dir == "long" else -1)

    return {
        "direction": sweep_dir,
        "entry": float(entry),
        "stop": float(stop),
        "target": float(target),
        "risk": float(risk),
        "rr": float(target_r),
        "rationale": (
            f"zen_sweep_iFVG_5m dir={sweep_dir} sweep_bar={sweep_bar} "
            f"sweep_ext={sweep_extreme:.4f} confirm={confirm_bar} "
            f"entry={entry:.4f} stop={stop:.4f} atr={cur_atr:.4f}"
        ),
    }