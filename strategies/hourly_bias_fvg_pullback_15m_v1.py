"""
ICT 2022 - Hourly Bias + 15m FVG Pullback (15m entry with 1h HTF bias).

Setup:
1. Compute prior hour's HIGH, LOW, CLOSE (last 4 15m bars).
2. Derive bias from prior hour close vs midpoint (PHH+PHL)/2:
   - BULL if close > midpoint
   - BEAR if close < midpoint
3. During current hour, find a 15m FVG formed IN the bias direction
   in the last fvg_lookback bars (default 6).
4. Enter at FVG midpoint when current bar tags the FVG zone; stop outside FVG;
   target = 2R.

Session filter: London + NY (07:00-17:00 UTC).

Custom strategy IDs:
  - 433: MGC
  - 434: MNQ
  - 435: MCL

90d v1_realistic_micros native-bar backtests:
  MGC 15m: n=72, 42W/30L (58.3% WR), PF=1.99, E[R]=+0.56R, +$931
  MGC 5m:  n=114, 65W/49L (57.0% WR), PF=2.65, E[R]=+0.28R, +$1094
  MNQ 15m: n=80, 40W/40L (50.0% WR), PF=2.02, E[R]=+0.41R, +$1253
  MNQ 5m:  n=110, 53W/57L (48.2% WR), PF=1.88, E[R]=+0.28R, +$823
  MCL 15m: n=79, 50W/29L (63.3% WR), PF=2.70, E[R]=+0.48R, +$610
  MCL 5m:  n=129, 81W/48L (62.8% WR), PF=1.16 (marginal due to commission bleed)
  ZF:      PF=0.55 5m / PF=0.55 15m (commission bleed, skip)
  6J:      PF=0.36 5m / PF=0.86 15m (commission bleed, skip)
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    n = len(bars)
    if n < 250:
        return None
    cur_i = n - 1
    if cur_i < 100:
        return None

    atr_period = int(params.get('atr_period', 14))
    rr = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.05))
    fvg_min_atr = float(params.get('fvg_min_atr', 0.05))
    fvg_lookback = int(params.get('fvg_lookback', 6))
    min_gap_pct = float(params.get('min_gap_pct', 0.10))

    h = bars['high'].values.astype(float)
    l = bars['low'].values.astype(float)
    c = bars['close'].values.astype(float)
    o = bars['open'].values.astype(float)

    if cur_i < atr_period:
        return None
    rngs = h[cur_i - atr_period + 1:cur_i + 1] - l[cur_i - atr_period + 1:cur_i + 1]
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None
    buf = atr * stop_buf_atr
    min_fvg_gap = max(fvg_min_atr * atr, min_gap_pct * atr)

    cur_ts = bars['ts'].iloc[cur_i]
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_minutes = cur_ts.hour * 60 + cur_ts.minute

    session_start = 7 * 60
    session_end = 17 * 60
    if cur_minutes < session_start or cur_minutes >= session_end:
        return None

    cur_hour = cur_ts.hour
    hour_start_idx = None
    for k in range(cur_i, max(0, cur_i - 16), -1):
        ts_k = bars['ts'].iloc[k]
        if ts_k.tzinfo is None:
            ts_k = ts_k.tz_localize('UTC')
        if ts_k.hour != cur_hour:
            hour_start_idx = k + 1
            break
    if hour_start_idx is None or hour_start_idx < 4:
        return None

    prior_hour_start = hour_start_idx - 4
    if prior_hour_start < 0:
        return None
    phh = float(np.max(h[prior_hour_start:hour_start_idx]))
    phl = float(np.min(l[prior_hour_start:hour_start_idx]))
    phc = float(c[hour_start_idx - 1])
    phm = (phh + phl) / 2.0

    bias = 'bull' if phc > phm else 'bear'

    cur_h = float(h[cur_i])
    cur_l = float(l[cur_i])

    fvg_top = None
    fvg_bot = None
    fvg_mid = None
    fvg_kind = None

    for j in range(max(2, cur_i - fvg_lookback), cur_i - 1):
        if h[j] < l[j + 2] and (l[j + 2] - h[j]) >= min_fvg_gap:
            fvg_top = float(l[j + 2])
            fvg_bot = float(h[j])
            fvg_mid = (fvg_top + fvg_bot) / 2.0
            fvg_kind = 'bull'
            break
        if l[j] > h[j + 2] and (l[j] - h[j + 2]) >= min_fvg_gap:
            fvg_top = float(l[j])
            fvg_bot = float(h[j + 2])
            fvg_mid = (fvg_top + fvg_bot) / 2.0
            fvg_kind = 'bear'
            break

    if fvg_mid is None:
        return None

    direction = None
    if bias == 'bull' and fvg_kind == 'bull':
        if cur_l <= fvg_top and cur_h >= fvg_bot:
            entry = fvg_mid
            stop = fvg_bot - buf
            risk = entry - stop
            if risk <= 0:
                return None
            target = entry + rr * risk
            direction = 'long'
    elif bias == 'bear' and fvg_kind == 'bear':
        if cur_h >= fvg_bot and cur_l <= fvg_top:
            entry = fvg_mid
            stop = fvg_top + buf
            risk = stop - entry
            if risk <= 0:
                return None
            target = entry - rr * risk
            direction = 'short'

    if direction is None:
        return None
    return {
        'direction': direction,
        'entry': entry,
        'stop': stop,
        'target': target
    }