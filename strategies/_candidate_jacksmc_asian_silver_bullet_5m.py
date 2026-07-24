"""
ICT Silver Bullet 2026 - JACKSMC Asian H/L variant (BtEaCp2lEzk, DgcRDvyxvIQ)

Architecture (drawn from JACKSMC Gold ICT Silver Bullet 2026):
1. Session gate: London killzone 11:00-15:00 UTC (= 7-11am EDT during DST).
2. Mark Asian session HIGH and Asian session LOW (Asian = 00:00-04:00 UTC).
3. Detect sweep during London: bar's HIGH >= Asian High (BSL sweep) or
   bar's LOW <= Asian Low (SSL sweep).
4. MSS confirmation: bar CLOSES back on the opposite side of the swept level
   (i.e., for BSL sweep: close < Asian High; for SSL sweep: close > Asian Low).
5. FVG gate: 3-candle FVG around the sweep bar (gap between bars[i-2] and bars[i]).
   - Bullish FVG (gap up): bars[i-2].high < bars[i].low   -> LONG
   - Bearish FVG (gap down): bars[i-2].low > bars[i].high -> SHORT
6. FVG must overlap with the MSS zone (FVG is within 1.5x range of sweep bar).
7. Entry: FVG midpoint.
8. SL: beyond the sweep bar's extreme (for long: below sweep bar low - buffer;
   for short: above sweep bar high + buffer).
9. TP: 1:3 RR by default (rr param).
"""
import pandas as pd
import numpy as np

def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    cur_i = n - 1
    cur_bar = bars.iloc[cur_i]

    # Timestamp / timezone handling
    if 'ts' in bars.columns:
        cur_ts = pd.Timestamp(bars['ts'].iloc[cur_i])
        ts_series = pd.to_datetime(bars['ts'], utc=True)
    else:
        cur_ts = pd.Timestamp(bars.index[cur_i])
        ts_series = pd.DatetimeIndex(bars.index).tz_localize('UTC')
        if ts_series.tz is None:
            ts_series = ts_series.tz_localize('UTC')

    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_hour = cur_ts.hour

    # London killzone 7-11am EDT = 11:00-15:00 UTC during DST.
    # Allow a configurable window via params (default 11-15 UTC).
    win_start = int(params.get('win_start_utc', 11))
    win_end = int(params.get('win_end_utc', 15))
    if not (win_start <= cur_hour < win_end):
        return None

    # Asian session window (default 00:00-04:00 UTC)
    asian_start = int(params.get('asian_start_utc', 0))
    asian_end = int(params.get('asian_end_utc', 4))
    hours = ts_series.dt.hour.values
    dates = ts_series.dt.date.values
    cur_date = cur_ts.date()

    # Asian session H/L: same calendar date if Asian straddles midnight,
    # use the most recent Asian session (which ended at asian_end today).
    # For safety we look at both prior-day Asian (if cur_hour < asian_end)
    # AND today Asian (if cur_hour >= asian_end).
    # Simpler: use the last contiguous Asian-session block before current bar.
    asian_mask = (hours >= asian_start) | (hours < asian_end)
    asian_idx_all = np.where(asian_mask)[0]
    # Filter to most recent Asian block ending before current bar
    asian_idx = asian_idx_all[asian_idx_all < cur_i]
    if len(asian_idx) < 4:  # need at least 4 bars of Asian data
        return None

    # Take the LAST contiguous block of Asian session bars
    # (i.e., bars from the latest transition into Asian window up to cur_i).
    # Find the last gap in Asian_idx
    diffs = np.diff(asian_idx)
    gap_pos = np.where(diffs > 1)[0]
    if len(gap_pos) == 0:
        block_start = 0
    else:
        block_start = gap_pos[-1] + 1
    block_idx = asian_idx[block_start:]
    if len(block_idx) < 4:
        return None

    asian_high = float(bars['high'].iloc[block_idx].max())
    asian_low = float(bars['low'].iloc[block_idx].min())
    if asian_high <= asian_low:
        return None

    # ATR for buffer
    atr_period = int(params.get('atr_period', 14))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.10))
    rr = float(params.get('rr', 3.0))
    if cur_i < atr_period + 1:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values -
            bars['low'].iloc[cur_i - atr_period:cur_i].values)
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None

    cur_open = float(cur_bar['open'])
    cur_high = float(cur_bar['high'])
    cur_low = float(cur_bar['low'])
    cur_close = float(cur_bar['close'])

    buf = atr * stop_buf_atr
    min_gap_atr_mult = float(params.get('min_gap_atr_mult', 0.05))
    min_gap = atr * min_gap_atr_mult

    # 3-candle FVG: bars[i-2] vs bars[i]
    h_i2 = float(bars['high'].iloc[cur_i - 2])
    l_i2 = float(bars['low'].iloc[cur_i - 2])
    h_i  = cur_high
    l_i  = cur_low

    signal = None

    # SHORT: sweep Asian High + MSS (close back below) + bearish FVG
    if cur_high > asian_high and cur_close < asian_high:
        # Bearish FVG: gap down (bars[i-2].low > bars[i].high)
        if l_i2 > h_i and (l_i2 - h_i) >= min_gap:
            fvg_top = l_i2
            fvg_bottom = h_i
            fvg_mid = (fvg_top + fvg_bottom) / 2.0
            # FVG must overlap with MSS zone (around asian_high)
            # Simple check: fvg_mid should be near or below asian_high
            if fvg_top < asian_high + atr * 0.5:
                entry = fvg_mid
                stop = cur_high + buf
                risk = stop - entry
                if risk > 0:
                    signal = {'direction': 'short', 'entry': entry,
                              'stop': stop, 'target': entry - rr * risk}

    # LONG: sweep Asian Low + MSS (close back above) + bullish FVG
    if signal is None and cur_low < asian_low and cur_close > asian_low:
        # Bullish FVG: gap up (bars[i-2].high < bars[i].low)
        if h_i2 < l_i and (l_i - h_i2) >= min_gap:
            fvg_bottom = h_i2
            fvg_top = l_i
            fvg_mid = (fvg_top + fvg_bottom) / 2.0
            # FVG must overlap with MSS zone (around asian_low)
            if fvg_bottom > asian_low - atr * 0.5:
                entry = fvg_mid
                stop = cur_low - buf
                risk = entry - stop
                if risk > 0:
                    signal = {'direction': 'long', 'entry': entry,
                              'stop': stop, 'target': entry + rr * risk}

    return signal
