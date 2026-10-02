"""Turtle Soup 15m MGC v1 — Mulham stateless cross-session BSL/SSL sweep.

Adapted from CSID 191 (proven on MCL 15m, PF=3.11) for MGC 5m slot gap.
Backtest: MGC 15m 90d: n=25, 14W/11L (56% WR), PF=2.24, E[R]=+0.61, +$771.

Architecture:
  1. London (06:00-10:00 UTC) trades Asian session H/L (00:00-06:00 UTC)
  2. NY (11:00-15:00 UTC) trades London session H/L (06:00-11:00 UTC)
  3. Sweep: current bar high > prior session high (BSL) or low < prior session low (SSL)
  4. No-displacement filter: |close-open| < 0.30 * ATR (wick rejection, not full body)
  5. Close back inside the range confirms reversal
  6. Stop: sweep wick + 0.25 * ATR
  7. Target: 2.0R from entry
"""


def evaluate(bars, params):
    import pandas as pd
    n = len(bars)
    if n < 50:
        return None
    cur_idx = n - 1
    cur_bar = bars.iloc[cur_idx]
    if 'ts' in bars.columns:
        cur_ts = pd.Timestamp(bars['ts'].iloc[cur_idx])
    else:
        cur_ts = bars.index[cur_idx]
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_hour = cur_ts.hour
    cur_date = cur_ts.date()
    in_london = 6 <= cur_hour < 10
    in_ny = 11 <= cur_hour < 15
    if not (in_london or in_ny):
        return None
    if in_london:
        ps_lo, ps_hi = 0, 6
    else:
        ps_lo, ps_hi = 6, 11
    if 'ts' in bars.columns:
        ts_series = pd.to_datetime(bars['ts'], utc=True)
        hours = ts_series.dt.hour.values
        dates = ts_series.dt.date.values
    else:
        hours = pd.DatetimeIndex(bars.index).hour
        dates = pd.DatetimeIndex(bars.index).date
    prior_mask = (dates == cur_date) & (hours >= ps_lo) & (hours < ps_hi)
    prior_idx = np.where(prior_mask)[0]
    prior_idx = prior_idx[prior_idx < cur_idx]
    if len(prior_idx) < 4:
        return None
    prior_high = float(bars['high'].iloc[prior_idx].max())
    prior_low = float(bars['low'].iloc[prior_idx].min())
    if prior_high <= prior_low:
        return None
    cur_high = float(cur_bar['high'])
    cur_low = float(cur_bar['low'])
    cur_open = float(cur_bar['open'])
    cur_close = float(cur_bar['close'])
    atr_period = int(params.get('atr_period', 14))
    atr_mult = float(params.get('displacement_max_atr', 0.30))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.25))
    rr = float(params.get('rr', 2.0))
    if cur_idx < atr_period + 1:
        return None
    rngs = (bars['high'].iloc[cur_idx-atr_period:cur_idx].values -
            bars['low'].iloc[cur_idx-atr_period:cur_idx].values)
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    body = abs(cur_close - cur_open)
    displacement_ok = body < atr * atr_mult
    if cur_high > prior_high and cur_close < prior_high and displacement_ok:
        entry = prior_high
        stop = cur_high + stop_buf_atr * atr
        risk = stop - entry
        if risk <= 0:
            return None
        return {'direction': 'short', 'entry': entry, 'stop': stop,
                'target': entry - rr * risk}
    if cur_low < prior_low and cur_close > prior_low and displacement_ok:
        entry = prior_low
        stop = cur_low - stop_buf_atr * atr
        risk = entry - stop
        if risk <= 0:
            return None
        return {'direction': 'long', 'entry': entry, 'stop': stop,
                'target': entry + rr * risk}
    return None
