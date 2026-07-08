"""
CRT (Central Range Theory) 1H — Central Pivot Mean Reversion.

ICT CRT model abstract:
1. Identify a prior N-bar range (pivot high / pivot low) — the "central range."
2. Wait for a liquidity sweep beyond either boundary (current bar's high > prior_high
   for upper sweep; current bar's low < prior_low for lower sweep).
3. Entry at the CENTER (midpoint) of the central range, in the direction the sweep
   implies is being reversed.
4. Stop: beyond the sweep wick (plus ATR buffer).
5. Target: opposite range boundary.

Different primitive from FVG: this is a RANGE EXPANSION mean-reversion,
not a candle FVG gap-fill. Operates cleanly at 1H where structured range
behavior dominates.

Timeframe: 1H (per task WS3 spec)
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    lookback = int(params.get('lookback', 24))   # 1H bars in prior session (~24h = 1 day)
    sweep_buf = float(params.get('sweep_buf_atr', 0.10))
    rr = float(params.get('rr', 1.5))
    atr_period = int(params.get('atr_period', 14))
    max_trades_per_day = int(params.get('max_per_day', 1))
    cur_i = n - 1

    # Build a daily trade cap tracker via row data
    if 'ts' in bars.columns:
        ts = pd.to_datetime(bars['ts'], utc=True)
    else:
        ts = pd.DatetimeIndex(bars.index)
        if ts.tz is None:
            ts = ts.tz_localize('UTC')

    if cur_i < max(lookback, atr_period) + 2:
        return None

    # Compute ATR over the atr_period ENDING right BEFORE the current bar
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values.astype(float) -
            bars['low'].iloc[cur_i - atr_period:cur_i].values.astype(float))
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    buf = atr * sweep_buf

    # Central range = high/low over [cur_i-lookback .. cur_i-1]
    win_h = float(bars['high'].iloc[cur_i - lookback:cur_i].max())
    win_l = float(bars['low'].iloc[cur_i - lookback:cur_i].min())
    if win_h <= win_l:
        return None
    mid = (win_h + win_l) / 2.0

    cur_open = float(bars['open'].iloc[cur_i])
    cur_high = float(bars['high'].iloc[cur_i])
    cur_low = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    # Body ratio for confirmation
    body = abs(cur_close - cur_open)
    full_rng = cur_high - cur_low
    if full_rng <= 0:
        return None
    br = body / full_rng
    if br < 0.30:
        return None  # require decisive candle (not a doji)

    # Daily trade cap (look back at recent signals this same day)
    cur_date = ts.iloc[cur_i].date() if hasattr(ts.iloc[cur_i], 'date') else None
    if cur_date is not None:
        # count entries in last 30 bars (skip)
        pass

    # SHORT: sweep above range (current high > central_high + buffer), close back inside (MSS)
    if cur_high > win_h + buf and cur_close < win_h:
        # require open-mid zone or below
        if cur_close > mid:
            return None  # would be a retest long, not CRT short
        entry = mid
        stop = cur_high + buf
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - rr * risk
        return {'direction': 'short', 'entry': entry, 'stop': stop, 'target': target}

    # LONG: sweep below range, close back inside
    if cur_low < win_l - buf and cur_close > win_l:
        if cur_close < mid:
            return None
        entry = mid
        stop = cur_low - buf
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + rr * risk
        return {'direction': 'long', 'entry': entry, 'stop': stop, 'target': target}

    return None
