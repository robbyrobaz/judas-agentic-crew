"""
ICT Silver Bullet 2026 - JACKSMC Asian H/L variant (BtEaCp2lEzk, DgcRDvyxvIQ)

Architecture (from JACKSMC Gold ICT Silver Bullet 2026):
1. Session gate: 8-16 UTC (covers London killzone 11-15 + NY open 13-15 UTC).
2. Mark Asian session HIGH and Asian session LOW (Asian = 0-4 UTC).
3. Detect sweep: bar HIGH > Asian High (BSL) or bar LOW < Asian Low (SSL).
4. MSS confirmation: bar CLOSES back on opposite side of swept level.
5. Entry: at the swept level (Asian H/L).
6. Stop: beyond the sweep extreme + ATR buffer.
7. Target: 3R default.

Distinctive from existing PDH/PDL silver bullets:
- Reference level = Asian H/L (intraday session range), not prior-day H/L
- Sweep detected via body-close back (MSS confirmation), not retest
- Time-of-day filter (8-16 UTC) targets London/NY institutional activity
"""
import pandas as pd
import numpy as np

_S = {'last_processed_idx': -1}


def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    cur_i = n - 1
    cur_bar = bars.iloc[cur_i]
    _S['last_processed_idx'] = cur_i

    if 'ts' in bars.columns:
        cur_ts = pd.Timestamp(bars['ts'].iloc[cur_i])
        ts_series = pd.to_datetime(bars['ts'], utc=True)
    else:
        cur_ts = pd.Timestamp(bars.index[cur_i])
        ts_series = pd.DatetimeIndex(bars.index).tz_localize('UTC')
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_hour = cur_ts.hour

    win_start = int(params.get('win_start_utc', 8))
    win_end = int(params.get('win_end_utc', 16))
    if not (win_start <= cur_hour < win_end):
        return None

    hours = ts_series.dt.hour.values
    asian_mask = (hours >= 0) & (hours < 4)
    asian_idx_all = np.where(asian_mask)[0]
    asian_idx = asian_idx_all[asian_idx_all < cur_i]
    if len(asian_idx) < 4:
        return None
    diffs = np.diff(asian_idx)
    gap_pos = np.where(diffs > 1)[0]
    block_start = gap_pos[-1] + 1 if len(gap_pos) > 0 else 0
    block_idx = asian_idx[block_start:]
    if len(block_idx) < 4:
        return None
    asian_high = float(bars['high'].iloc[block_idx].max())
    asian_low = float(bars['low'].iloc[block_idx].min())

    cur_high = float(bars['high'].iloc[cur_i])
    cur_low = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    atr_period = int(params.get('atr_period', 14))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.20))
    rr = float(params.get('rr', 3.0))
    if cur_i < atr_period + 1:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values -
            bars['low'].iloc[cur_i - atr_period:cur_i].values)
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    buf = atr * stop_buf_atr

    if cur_high > asian_high and cur_close < asian_high:
        entry = asian_high
        stop = cur_high + buf
        risk = stop - entry
        if risk > 0:
            return {'direction': 'short', 'entry': entry,
                    'stop': stop, 'target': entry - rr * risk}

    if cur_low < asian_low and cur_close > asian_low:
        entry = asian_low
        stop = cur_low - buf
        risk = entry - stop
        if risk > 0:
            return {'direction': 'long', 'entry': entry,
                    'stop': stop, 'target': entry + rr * risk}

    return None
