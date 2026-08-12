def evaluate(bars, params):
    """ICT Silver Bullet 5m — VARIANT: Asian+London COMBINED H/L as daily pool.

    Concrete refinement (YT:ytuOZW3LNXs, Smart Risk, Aug 10 2026):
      The "daily liquidity pool" is computed as the UNION of Asian session
      H/L and London session H/L (not just the immediately preceding
      session). This widens the pool slightly but keeps it tightly bound
      to fresh structure within the current trading day.

    Other rules (unchanged from base 5m SB):
      1. NY session window (13:00-21:00 UTC = 09:00-17:00 ET)
      2. Sweep: current bar HIGH > pool_high OR LOW < pool_low
      3. MSS: BODY close back inside pool range
      4. 3-candle FVG in direction of trade
      5. Entry: midpoint of FVG zone
      6. Stop: beyond sweep wick + atr buffer
      7. Target: rr * risk

    Optional premium/discount filter (params['use_premium_filter']=True):
      - For SHORT: FVG midpoint must be in UPPER 50% of impulse range
        (range = from sweep HIGH down to recent swing LOW before sweep)
      - For LONG: FVG midpoint must be in LOWER 50% of impulse range
      - Filter only applies when range is computable (>0).

    Time bands (UTC):
      Asia:       18:00 prev day - 02:00 today
      London:     02:00 - 10:00 today
      NY window:  13:00 - 21:00 today
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
    cur_date = cur_ts.date()

    # NY session window only
    ny_lo = int(params.get('ny_lo_hour', 13))
    ny_hi = int(params.get('ny_hi_hour', 21))
    if not (ny_lo <= cur_hour < ny_hi):
        return None

    # Build time arrays
    if 'ts' in bars.columns:
        ts_series = pd.to_datetime(bars['ts'], utc=True)
        hours = ts_series.dt.hour.values
        dates = ts_series.dt.date.values
    else:
        hours = pd.DatetimeIndex(bars.index).hour
        dates = pd.DatetimeIndex(bars.index).date

    # Compute COMBINED Asian+London daily pool for current trading day.
    # Asia:  18:00 prev day through 02:00 today
    # London: 02:00 through 10:00 today
    # We use bars dated cur_date whose hour in [02,10) OR bars from prior
    # date whose hour >= 18. Combined H/L across both.
    pool_mask = ((dates == cur_date) & (hours >= 2) & (hours < 10)) | \
                ((dates < cur_date) & (hours >= 18))
    pool_idx = np.where(pool_mask)[0]
    pool_idx = pool_idx[pool_idx < cur_i]
    if len(pool_idx) < 12:
        return None
    pool_high = float(bars['high'].iloc[pool_idx].max())
    pool_low  = float(bars['low'].iloc[pool_idx].min())
    if pool_high <= pool_low:
        return None

    # ATR
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

    cur_open  = float(cur_bar['open'])
    cur_high  = float(cur_bar['high'])
    cur_low   = float(cur_bar['low'])
    cur_close = float(cur_bar['close'])

    # 3-candle FVG detection (bars[i-2] vs bars[i])
    h_i2 = float(bars['high'].iloc[cur_i - 2])
    l_i2 = float(bars['low'].iloc[cur_i - 2])

    buf = atr * stop_buf_atr

    # Optional premium/discount filter on impulse range
    use_premium = bool(params.get('use_premium_filter', False))
    # Find recent swing low/high in last `swing_lookback` bars BEFORE this bar
    # for SHORT: impulse top = sweep high (cur_high), bottom = recent swing low
    # for LONG:  impulse bottom = sweep low (cur_low),  top = recent swing high
    swing_lookback = int(params.get('swing_lookback', 20))
    if use_premium and cur_i >= swing_lookback + 1:
        recent_lows  = bars['low'].iloc[cur_i - swing_lookback:cur_i].values
        recent_highs = bars['high'].iloc[cur_i - swing_lookback:cur_i].values
        recent_swing_low  = float(np.min(recent_lows))
        recent_swing_high = float(np.max(recent_highs))
    else:
        recent_swing_low  = None
        recent_swing_high = None

    # SHORT: sweep of buy-side (above pool_high) + MSS (close below) + bearish FVG
    if cur_high > pool_high and cur_close < pool_high:
        # Bearish FVG: gap down (l_i2 > h_i where h_i is current bar high)
        if l_i2 > cur_high:
            fvg_top = l_i2
            fvg_bottom = cur_high
            fvg_mid = (fvg_top + fvg_bottom) / 2.0
            # Premium filter: FVG must be in upper 50% of impulse (sweep top -> swing low)
            if use_premium and recent_swing_low is not None and recent_swing_low < cur_high:
                imp_top = cur_high
                imp_bot = recent_swing_low
                mid_50 = (imp_top + imp_bot) / 2.0
                if fvg_mid < mid_50:
                    return None  # not in premium zone
            entry = fvg_mid
            stop = cur_high + buf
            risk = stop - entry
            if risk > 0:
                return {'direction': 'short', 'entry': entry, 'stop': stop,
                        'target': entry - rr * risk}

    # LONG: sweep of sell-side (below pool_low) + MSS (close above) + bullish FVG
    if cur_low < pool_low and cur_close > pool_low:
        # Bullish FVG: gap up (h_i2 < l_i)
        if h_i2 < cur_low:
            fvg_top = cur_low
            fvg_bottom = h_i2
            fvg_mid = (fvg_top + fvg_bottom) / 2.0
            # Discount filter: FVG must be in lower 50% of impulse (swing high -> sweep low)
            if use_premium and recent_swing_high is not None and recent_swing_high > cur_low:
                imp_top = recent_swing_high
                imp_bot = cur_low
                mid_50 = (imp_top + imp_bot) / 2.0
                if fvg_mid > mid_50:
                    return None  # not in discount zone
            entry = fvg_mid
            stop = cur_low - buf
            risk = entry - stop
            if risk > 0:
                return {'direction': 'long', 'entry': entry, 'stop': stop,
                        'target': entry + rr * risk}

    return None
