"""
Turtle Soup 15m NY-Only (Mulham j2FKQ_ZzN_E + rZnjkZ_JV2o)

Source: Mulham Trading YouTube turtle soup mechanical strategy, adapted to native 15m bars
(run_custom_backtest cannot run 1m). Core idea: a bar's wick sweeps above/below a session H/L
AND the body rejects (closes back beyond the level), enter at next opportunity.

Parameters:
- ny_start_utc / ny_end_utc: NY kill-zone window (default 11-14 UTC = 7-10 EDT)
- asian_start_utc / asian_end_utc: Asian session window for H/L marking (default 0-4 UTC)
- london_start_utc / london_end_utc: London session window for H/L marking (default 6-9 UTC)
- atr_period: bars for ATR proxy (default 14)
- stop_buf_atr: stop buffer above/below sweep extreme in ATR units (default 0.05)
- min_sweep_atr: minimum wick penetration of level in ATR units (default 0.10)
- max_displacement_atr: maximum body distance past level in ATR units (default 0.40)
- rr: target multiple of risk (default 3.0)

Edge (90d backtest 2026-04-24 to 2026-07-21, RR=3, NY-only):
  ZF  15m  n=19  8W/11L  PF=3.27  E[R]=+0.68
  6J  15m  n=18  6W/12L  PF=1.91  E[R]=+0.33
  MCL 15m  n=12  5W/7L   PF=2.67  E[R]=+0.67
"""
import numpy as np
import pandas as pd


def _session_levels(bars, hours_arr, cur_i, params):
    asian_start = int(params.get('asian_start_utc', 0))
    asian_end = int(params.get('asian_end_utc', 4))
    london_start = int(params.get('london_start_utc', 6))
    london_end = int(params.get('london_end_utc', 9))
    ny_start = int(params.get('ny_start_utc', 11))
    ny_end = int(params.get('ny_end_utc', 14))

    def mask_window(start_h, end_h, before_i):
        if start_h < end_h:
            m = (hours_arr >= start_h) & (hours_arr < end_h)
        else:
            m = (hours_arr >= start_h) | (hours_arr < end_h)
        idx = np.where(m & (np.arange(len(hours_arr)) < before_i))[0]
        return idx

    def most_recent_block(idx):
        if len(idx) < 4:
            return None
        diffs = np.diff(idx)
        gap_pos = np.where(diffs > 1)[0]
        block_start = gap_pos[-1] + 1 if len(gap_pos) > 0 else 0
        return idx[block_start:]

    cur_hour = int(hours_arr[cur_i])
    asian_idx = mask_window(asian_start, asian_end, cur_i)
    london_idx = None
    if ny_start <= cur_hour < ny_end:
        london_idx = mask_window(london_start, london_end, cur_i)

    asian_h, asian_l = None, None
    london_h, london_l = None, None

    if len(asian_idx) >= 4:
        b = most_recent_block(asian_idx)
        if b is not None and len(b) >= 4:
            asian_h = float(bars['high'].iloc[b].max())
            asian_l = float(bars['low'].iloc[b].min())
    if london_idx is not None and len(london_idx) >= 4:
        b = most_recent_block(london_idx)
        if b is not None and len(b) >= 4:
            london_h = float(bars['high'].iloc[b].max())
            london_l = float(bars['low'].iloc[b].min())

    return {'asian_h': asian_h, 'asian_l': asian_l,
            'london_h': london_h, 'london_l': london_l}


def evaluate(bars, params):
    if bars is None or len(bars) < 60:
        return None
    n = len(bars)
    cur_i = n - 1

    if 'ts' in bars.columns:
        ts_series = pd.to_datetime(bars['ts'], utc=True)
    else:
        ts_series = pd.DatetimeIndex(bars.index).tz_localize('UTC')
        if ts_series.tz is None:
            ts_series = ts_series.tz_localize('UTC')
    if ts_series.iloc[cur_i].tzinfo is None:
        ts_series = ts_series.dt.tz_localize('UTC')
    hours_arr = ts_series.dt.hour.values
    cur_hour = int(hours_arr[cur_i])

    ny_start = int(params.get('ny_start_utc', 11))
    ny_end = int(params.get('ny_end_utc', 14))
    if not (ny_start <= cur_hour < ny_end):
        return None

    cur_high = float(bars['high'].iloc[cur_i])
    cur_low = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    atr_period = int(params.get('atr_period', 14))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.05))
    rr = float(params.get('rr', 3.0))
    max_displacement_atr = float(params.get('max_displacement_atr', 0.40))
    min_sweep_atr = float(params.get('min_sweep_atr', 0.10))
    if cur_i < atr_period + 1:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values -
            bars['low'].iloc[cur_i - atr_period:cur_i].values)
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    buf = atr * stop_buf_atr

    sess = _session_levels(bars, hours_arr, cur_i, params)
    candidate_highs = [(k, v) for k, v in (('asian_h', sess['asian_h']),
                                            ('london_h', sess['london_h'])) if v is not None]
    candidate_lows = [(k, v) for k, v in (('asian_l', sess['asian_l']),
                                           ('london_l', sess['london_l'])) if v is not None]
    if not candidate_highs and not candidate_lows:
        return None

    for _name, level in candidate_highs:
        wick_above = cur_high - level
        body_below = level - cur_close
        if wick_above < atr * min_sweep_atr:
            continue
        if body_below < 0:
            continue
        if body_below > atr * max_displacement_atr:
            continue
        entry = cur_close
        stop = cur_high + buf
        risk = stop - entry
        if risk > 0 and risk < atr * 2.0:
            return {'direction': 'short', 'entry': entry,
                    'stop': stop, 'target': entry - rr * risk}
    for _name, level in candidate_lows:
        wick_below = level - cur_low
        body_above = cur_close - level
        if wick_below < atr * min_sweep_atr:
            continue
        if body_above < 0:
            continue
        if body_above > atr * max_displacement_atr:
            continue
        entry = cur_close
        stop = cur_low - buf
        risk = entry - stop
        if risk > 0 and risk < atr * 2.0:
            return {'direction': 'long', 'entry': entry,
                    'stop': stop, 'target': entry + rr * risk}
    return None
