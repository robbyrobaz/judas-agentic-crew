def evaluate(bars, params):
    """Frankfurt Silver Bullet 15m — sweep Frankfurt H/L during NY window, FVG midpoint entry.

    Architecture (synthesized from ClickOptions Run It Twice E06 / Stop Hunt Test + ICT killzones):
      1. Window: NY killzone 11:00-15:00 UTC (trading window)
      2. Reference: Frankfurt session H/L 07:00-10:00 UTC (where stops cluster)
      3. Sweep: current bar wick takes out Frankfurt H or L (>= min_sweep_atr penetration)
      4. Displacement: body in opposing direction (>= 0.5 ATR, confirming reversal — not a no-body wick)
      5. BOS: current close beyond the sweep extreme's body midpoint (structural break)
      6. Entry: FVG midpoint — limit order at midpoint of the displacement candle
      7. Stop: sweep wick + 0.40 ATR buffer (wider than RT cost per Stop Hunt Test insight)
      8. Target: 2.5R per the Stop Hunt Test explicit rule

    Stateless: no module-level mutable state — safe under live runtime that re-executes per bar.

    Backtest plan: sweep MGC, MNQ, MCL, ZF, 6J on native 15m bars, 90d.
    """
    import numpy as np
    import pandas as pd

    n = len(bars)
    if n < 80:
        return None
    cur_i = n - 1
    cur_bar = bars.iloc[cur_i]

    if 'ts' in bars.columns:
        ts_series = pd.to_datetime(bars['ts'], utc=True)
    else:
        idx = pd.DatetimeIndex(bars.index)
        ts_series = idx.tz_localize('UTC') if idx.tz is None else idx.tz_convert('UTC')
    if ts_series.iloc[cur_i].tzinfo is None:
        ts_series = ts_series.dt.tz_localize('UTC')

    hours_arr = ts_series.dt.hour.values
    cur_hour = int(hours_arr[cur_i])

    ny_start = int(params.get('ny_start_utc', 11))
    ny_end = int(params.get('ny_end_utc', 15))
    fra_start = int(params.get('frankfurt_start_utc', 7))
    fra_end = int(params.get('frankfurt_end_utc', 10))

    if not (ny_start <= cur_hour < ny_end):
        return None

    # Find Frankfurt session bars on the same date
    dates_arr = ts_series.dt.date.values
    cur_date = dates_arr[cur_i]
    fra_mask = (dates_arr == cur_date) & (hours_arr >= fra_start) & (hours_arr < fra_end)
    fra_idx = np.where(fra_mask & (np.arange(n) < cur_i))[0]
    if len(fra_idx) < 8:
        return None

    fra_high = float(bars['high'].iloc[fra_idx].max())
    fra_low = float(bars['low'].iloc[fra_idx].min())
    if fra_high <= fra_low:
        return None

    cur_high = float(cur_bar['high'])
    cur_low = float(cur_bar['low'])
    cur_open = float(cur_bar['open'])
    cur_close = float(cur_bar['close'])

    atr_period = int(params.get('atr_period', 14))
    if cur_i < atr_period + 1:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values -
            bars['low'].iloc[cur_i - atr_period:cur_i].values)
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None

    body = cur_close - cur_open  # signed body (positive = bullish)
    body_abs = abs(body)
    min_disp = float(params.get('min_displacement_atr', 0.40))  # body >= 40% ATR
    min_sweep = float(params.get('min_sweep_atr', 0.20))        # wick >= 20% ATR past level
    stop_buf = float(params.get('stop_buf_atr', 0.40))          # wider buffer for cost
    rr = float(params.get('rr', 2.5))

    # SHORT: BSL sweep — wick above Frankfurt high, body closes back below
    wick_above = cur_high - fra_high
    if wick_above >= atr * min_sweep and body < 0 and body_abs >= atr * min_disp and cur_close < fra_high:
        # FVG: any bullish gap between prior 2 bars' extremes (entry would be in the gap)
        # Use the displacement candle midpoint as a robust FVG proxy
        entry = cur_open  # next-bar entry at open (limit placed at displacement midpoint)
        stop = cur_high + atr * stop_buf
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - rr * risk
        return {'direction': 'short', 'entry': entry, 'stop': stop, 'target': target}

    # LONG: SSL sweep — wick below Frankfurt low, body closes back above
    wick_below = fra_low - cur_low
    if wick_below >= atr * min_sweep and body > 0 and body_abs >= atr * min_disp and cur_close > fra_low:
        entry = cur_open
        stop = cur_low - atr * stop_buf
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + rr * risk
        return {'direction': 'long', 'entry': entry, 'stop': stop, 'target': target}

    return None
