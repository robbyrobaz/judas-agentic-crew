"""
Post-NY / Asian Session Silver Bullet with Fibonacci FVG Discount/Premium Filter.

Architecture (Smart Risk Silver Bullet, ytuOZW3LNXs):
- Timeframe: 5m
- Time window: 02:00 UTC (post-NY / start of next-day Asian) -> 09:00 UTC (Asia close)
- Liquidity reference levels: PRIOR calendar day's HIGH and LOW
  (the previous Asian + London + NY H/L - known by 02:00 UTC)
- Setup:
  1) Wait for price to sweep prior-day high or low (during 02-09 UTC Asian session)
  2) MSS: 2 consecutive closes in the reversal direction
  3) Form/identify a 3-candle FVG in the reversal direction
  4) Fibonacci filter: FVG must lie in DISCOUNT (<50% of recent swing range) for longs,
     in PREMIUM (>50%) for shorts
- Entry: limit at the midpoint of the FVG on the next pullback
- Stop: just beyond the first candle of the 3-candle FVG (with buffer)
- Target: 2R (matches existing CSID 229 cluster)
"""
import pandas as pd
import numpy as np


def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None

    n = len(bars)

    # --- params ---
    window_start_utc = int(params.get('window_start_utc', 2))   # 02:00 UTC
    window_end_utc = int(params.get('window_end_utc', 9))       # 09:00 UTC
    sweep_lookback = int(params.get('sweep_lookback', 24))      # 2h lookback
    sweep_min_ticks = int(params.get('sweep_min_ticks', 3))
    min_gap_factor = float(params.get('min_gap_factor', 0.20))
    fib_thr = float(params.get('fib_thr', 0.50))
    stop_buf_ticks = int(params.get('stop_buf_ticks', 6))
    target_r = float(params.get('target_r', 2.0))
    tick = float(params.get('tick', 0.10))
    max_loss_ticks = int(params.get('max_loss_ticks', 400))

    if n < 200:
        return None

    highs = bars['high'].astype(float).values
    lows = bars['low'].astype(float).values
    closes = bars['close'].astype(float).values
    opens = bars['open'].astype(float).values
    times = bars['time']

    t = pd.to_datetime(times)
    hour_utc = t.dt.hour.values
    date_arr = t.dt.date

    i = n - 1
    h_utc = hour_utc[i]
    if h_utc < window_start_utc or h_utc >= window_end_utc:
        return None

    cur_date = date_arr[i]
    # Find prior calendar date (most recent date < cur_date in the data)
    prev_date_target = None
    for j in range(i - 1, -1, -1):
        if date_arr[j] < cur_date:
            prev_date_target = date_arr[j]
            break
    if prev_date_target is None:
        return None
    mask_prev = date_arr == prev_date_target
    if not mask_prev.any():
        return None
    prev_high = float(np.nanmax(highs[mask_prev]))
    prev_low = float(np.nanmin(lows[mask_prev]))

    if not np.isfinite(prev_high) or not np.isfinite(prev_low) or prev_high <= prev_low:
        return None

    avg_range = float(np.mean(highs[max(0, i - 20):i + 1] - lows[max(0, i - 20):i + 1]))
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    sweep_threshold = sweep_min_ticks * tick

    if i < sweep_lookback + 5:
        return None

    cur_close = closes[i]
    cur_open = opens[i]

    recent_high = float(np.nanmax(highs[i - sweep_lookback:i + 1]))
    recent_low = float(np.nanmin(lows[i - sweep_lookback:i + 1]))

    swing_range = recent_high - recent_low
    if swing_range <= 0:
        return None
    fib_50 = recent_low + swing_range * fib_thr

    # === LONG SETUP: sweep of prior day LOW, MSS up ===
    swept_low = recent_low < prev_low - sweep_threshold
    if swept_low:
        local_min_idx = int(np.argmin(lows[i - sweep_lookback:i + 1])) + (i - sweep_lookback)
        mss_confirmed = (
            cur_close > closes[local_min_idx] and
            cur_close > cur_open and
            closes[i - 1] > closes[local_min_idx]
        )
        if mss_confirmed:
            fvg_mid = None
            fvg_bot = None
            fvg_top = None
            for j in range(i - 1, max(i - 12, 4), -1):
                if lows[j - 1] + min_gap < highs[j + 1]:
                    fvg_bot_cand = highs[j + 1]
                    fvg_top_cand = lows[j - 1]
                    if fvg_top_cand - fvg_bot_cand >= min_gap:
                        fvg_top = fvg_top_cand
                        fvg_bot = fvg_bot_cand
                        fvg_mid = (fvg_top + fvg_bot) / 2.0
                        break
            if fvg_mid is not None:
                # Fibonacci filter: FVG must be in DISCOUNT for long
                if fvg_top < fib_50 and cur_close <= fvg_top:
                    entry = fvg_mid
                    stop = fvg_bot - stop_buf_ticks * tick
                    risk = entry - stop
                    if risk > 0 and risk < max_loss_ticks * tick:
                        target = entry + risk * target_r
                        return {'direction': 'long', 'entry': entry, 'stop': stop, 'target': target}

    # === SHORT SETUP: sweep of prior day HIGH, MSS down ===
    swept_high = recent_high > prev_high + sweep_threshold
    if swept_high:
        local_max_idx = int(np.argmax(highs[i - sweep_lookback:i + 1])) + (i - sweep_lookback)
        mss_confirmed = (
            cur_close < closes[local_max_idx] and
            cur_close < cur_open and
            closes[i - 1] < closes[local_max_idx]
        )
        if mss_confirmed:
            fvg_mid = None
            fvg_bot = None
            fvg_top = None
            for j in range(i - 1, max(i - 12, 4), -1):
                if highs[j - 1] > lows[j + 1] + min_gap:
                    fvg_top_cand = lows[j + 1]
                    fvg_bot_cand = highs[j - 1]
                    if fvg_top_cand - fvg_bot_cand >= min_gap:
                        fvg_top = fvg_top_cand
                        fvg_bot = fvg_bot_cand
                        fvg_mid = (fvg_top + fvg_bot) / 2.0
                        break
            if fvg_mid is not None:
                if fvg_bot > fib_50 and cur_close >= fvg_bot:
                    entry = fvg_mid
                    stop = fvg_top + stop_buf_ticks * tick
                    risk = stop - entry
                    if risk > 0 and risk < max_loss_ticks * tick:
                        target = entry - risk * target_r
                        return {'direction': 'short', 'entry': entry, 'stop': stop, 'target': target}

    return None
