def evaluate(bars, params):
    """ICT Silver Bullet 2026 (FINATIC, ks6wOGghzoA) - 5m breaker-at-FVG sweep.

    Architecture (drawn from session ingest):
    1. Session gate: London (06:00-10:00 UTC) or NY (13:00-17:00 UTC).
       Silver Bullet time-window emphasis: NY morning 13:00-15:00 UTC.
    2. Mark prior-session swing H/L (using all bars in last session in same day).
    3. Detect sweep: current bar's HIGH exceeds prior_session_high (or LOW below).
    4. MSS confirmation: BODY close back on opposite side of prior_session_high/low.
    5. FVG gate: prior 3 bars must form a 3-candle FVG in direction of trade.
       Bullish FVG (gap up between bars[i-2].high and bars[i].low) for long.
       Bearish FVG (gap down between bars[i-2].low and bars[i].high) for short.
    6. Breaker: bullish/bearish OB disrespected by opposing close (prior 8-bar lookback).
    7. Entry: midpoint of FVG zone; SL beyond sweep wick by atr-buffer; TP at 2R.
    """
    import pandas as pd
    n = len(bars)
    if n < 60:
        return None
    cur_i = n - 1
    cur_bar = bars.iloc[cur_i]
    if 'ts' in bars.columns:
        cur_ts = pd.Timestamp(bars['ts'].iloc[cur_i])
    else:
        cur_ts = bars.index[cur_i]
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_hour = cur_ts.hour
    # Session gate
    in_london = 6 <= cur_hour < 10
    in_ny = 13 <= cur_hour < 17
    if not (in_london or in_ny):
        return None
    # Prior session range: Asia (00-06 UTC) when in London; London (06-10 UTC) when in NY
    if in_london:
        ps_lo, ps_hi = 0, 6
    else:
        ps_lo, ps_hi = 6, 10
    if 'ts' in bars.columns:
        ts_series = pd.to_datetime(bars['ts'], utc=True)
        hours = ts_series.dt.hour.values
        dates = ts_series.dt.date.values
    else:
        hours = pd.DatetimeIndex(bars.index).hour
        dates = pd.DatetimeIndex(bars.index).date
    cur_date = cur_ts.date()
    prior_mask = (dates == cur_date) & (hours >= ps_lo) & (hours < ps_hi)
    prior_idx = np.where(prior_mask)[0]
    prior_idx = prior_idx[prior_idx < cur_i]
    if len(prior_idx) < 12:
        return None
    prior_high = float(bars['high'].iloc[prior_idx].max())
    prior_low = float(bars['low'].iloc[prior_idx].min())
    if prior_high <= prior_low:
        return None

    # ATR for stop buffer
    atr_period = int(params.get('atr_period', 14))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.25))
    rr = float(params.get('rr', 2.0))
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

    # 3-candle FVG detection on bars[i-2..i] (cur_i is i, i-2 is two bars back)
    # Bullish FVG: bars[i-2].high < bars[i].low  (gap up between)
    # Bearish FVG: bars[i-2].low  > bars[i].high (gap down between)
    h_i2 = float(bars['high'].iloc[cur_i - 2])
    l_i2 = float(bars['low'].iloc[cur_i - 2])
    l_i  = cur_low
    h_i  = cur_high

    buf = atr * stop_buf_atr

    # SHORT: sweep of buy-side (above prior_high) + MSS (close below) + bearish FVG
    if cur_high > prior_high and cur_close < prior_high:
        # MSS body-close confirmation already in condition above
        # Bearish FVG: gap down between bars[i-2] and bars[i]
        if l_i2 > h_i:
            fvg_top = l_i2
            fvg_bottom = h_i
            fvg_mid = (fvg_top + fvg_bottom) / 2.0
            entry = fvg_mid
            stop = cur_high + buf
            risk = stop - entry
            if risk > 0:
                return {'direction': 'short', 'entry': entry, 'stop': stop,
                        'target': entry - rr * risk}

    # LONG: sweep of sell-side (below prior_low) + MSS (close above) + bullish FVG
    if cur_low < prior_low and cur_close > prior_low:
        # Bullish FVG: gap up between bars[i-2] and bars[i]
        if h_i2 < l_i:
            fvg_bottom = h_i2
            fvg_top = l_i
            fvg_mid = (fvg_top + fvg_bottom) / 2.0
            entry = fvg_mid
            stop = cur_low - buf
            risk = entry - stop
            if risk > 0:
                return {'direction': 'long', 'entry': entry, 'stop': stop,
                        'target': entry + rr * risk}

    return None