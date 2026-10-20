"""
ICT 2026 NY Lunch Macro Reversal (5m / 15m) - carryover FVG from previous day's lunch.

Concept (from ICT 2026 New York Lunch Algorithmic Theory):
1. During NY Lunch window (15:30-17:30 UTC), detect a liquidity sweep event:
   - BSL sweep: a recent swing high is taken out and closed back below
   - SSL sweep: a recent swing low is taken out and closed back above
2. Identify the FIRST FVG (3-candle pattern: candle[0].high < candle[2].low or
   candle[0].low > candle[2].high) formed in the bars immediately BEFORE the sweep candle.
3. Carry that FVG zone forward to the next day (it's now an inversion FVG).
4. During next-day NY AM session (13:30-17:00 UTC), when price trades INTO that
   iFVG zone, enter a turtle-soup reversal in the OPPOSITE direction of the sweep.
5. Stop: outside the FVG zone (+/- ATR buffer); target: 2R (configurable).

Fresh architecture - distinct from silver_bullet, mitigation_block, OB midpoint,
Judas continuation. The carryover FVG from prior session is the unique signature.
"""
import numpy as np
import pandas as pd


def _pivot_highs(h, l, idx, length):
    """Find all pivot highs in bars [idx-length*8 : idx-length]."""
    pivots = []
    lo = max(length, idx - length * 8)
    for k in range(lo, idx - length):
        is_ph = True
        for j in range(k - length, k + length + 1):
            if j == k or j < 0 or j >= len(h):
                continue
            if h[j] >= h[k]:
                is_ph = False
                break
        if is_ph:
            pivots.append(k)
    return pivots


def _pivot_lows(h, l, idx, length):
    pivots = []
    lo = max(length, idx - length * 8)
    for k in range(lo, idx - length):
        is_pl = True
        for j in range(k - length, k + length + 1):
            if j == k or j < 0 or j >= len(h):
                continue
            if l[j] <= l[k]:
                is_pl = False
                break
        if is_pl:
            pivots.append(k)
    return pivots


def _find_fvg_before(h, l, sweep_idx, lookback):
    """Find the first FVG (3-bar pattern) in bars [sweep_idx - lookback : sweep_idx - 1].
    Returns (FVG_top, FVG_bot, fvg_kind) or None.
    fvg_kind: 'bullish' = BSI (gap up), 'bearish' = SIB (gap down)."""
    lo = max(2, sweep_idx - lookback)
    for k in range(lo, sweep_idx - 2):
        if h[k] < l[k + 2]:
            fvg_top = float(l[k + 2])
            fvg_bot = float(h[k])
            return fvg_top, fvg_bot, 'bullish'
        if l[k] > h[k + 2]:
            fvg_top = float(l[k])
            fvg_bot = float(h[k + 2])
            return fvg_top, fvg_bot, 'bearish'
    return None


def evaluate(bars, params):
    n = len(bars)
    if n < 250:
        return None
    cur_i = n - 1

    pivot_len = int(params.get('pivot_length', 6))
    fvg_lookback = int(params.get('fvg_lookback', 5))
    target_r = float(params.get('target_r', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.20))
    atr_period = int(params.get('atr_period', 14))

    # Time windows (UTC) - generous to span EDT/EST
    lunch_start = 15 * 60 + 30   # 15:30 UTC
    lunch_end = 18 * 60 + 30     # 18:30 UTC
    entry_start = 13 * 60 + 30   # 13:30 UTC
    entry_end = 18 * 60          # 18:00 UTC

    cur_ts = bars['ts'].iloc[cur_i]
    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_minutes = cur_ts.hour * 60 + cur_ts.minute
    cur_date = cur_ts.normalize()

    if cur_minutes < entry_start or cur_minutes >= entry_end:
        return None

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

    # Find the most recent prior trading day's NY lunch bars
    target_date = cur_date - pd.Timedelta(days=1)
    lunch_bars = []
    for lookback_days in range(1, 5):
        target_date = cur_date - pd.Timedelta(days=lookback_days)
        lunch_bars = []
        for k in range(cur_i - 1, max(0, cur_i - 1500), -1):
            ts_k = bars['ts'].iloc[k]
            if ts_k.tzinfo is None:
                ts_k = ts_k.tz_localize('UTC')
            ts_k_date = ts_k.normalize()
            if ts_k_date == target_date:
                ts_k_minutes = ts_k.hour * 60 + ts_k.minute
                if lunch_start <= ts_k_minutes < lunch_end:
                    lunch_bars.append(k)
            elif ts_k_date < target_date:
                break
        if len(lunch_bars) >= 3:
            break
    if len(lunch_bars) < 3:
        return None

    lunch_first_idx = min(lunch_bars)

    pivot_h_indices = _pivot_highs(h, l, lunch_first_idx, pivot_len)
    pivot_l_indices = _pivot_lows(h, l, lunch_first_idx, pivot_len)

    if not pivot_h_indices and not pivot_l_indices:
        return None

    sweep_event = None
    for k in sorted(lunch_bars):
        for ph_idx in pivot_h_indices:
            if h[k] > h[ph_idx] and c[k] <= h[ph_idx]:
                fvg = _find_fvg_before(h, l, k, fvg_lookback)
                if fvg is not None:
                    sweep_event = ('short', k, fvg[0], fvg[1], (fvg[0] + fvg[1]) / 2.0, fvg[2])
                    break
        if sweep_event is not None:
            break
        for pl_idx in pivot_l_indices:
            if l[k] < l[pl_idx] and c[k] >= l[pl_idx]:
                fvg = _find_fvg_before(h, l, k, fvg_lookback)
                if fvg is not None:
                    sweep_event = ('long', k, fvg[0], fvg[1], (fvg[0] + fvg[1]) / 2.0, fvg[2])
                    break
        if sweep_event is not None:
            break

    if sweep_event is None:
        return None

    direction, sweep_idx, fvg_top, fvg_bot, fvg_ce, fvg_kind = sweep_event

    cur_h = float(h[cur_i])
    cur_l = float(l[cur_i])

    if direction == 'short':
        if cur_l <= fvg_top and cur_h >= fvg_bot:
            entry = float(fvg_ce)
            stop = float(fvg_top) + buf
            risk = stop - entry
            if risk <= 0:
                return None
            target = entry - target_r * risk
            return {'direction': 'short', 'entry': entry, 'stop': stop, 'target': target}
    elif direction == 'long':
        if cur_h >= fvg_bot and cur_l <= fvg_top:
            entry = float(fvg_ce)
            stop = float(fvg_bot) - buf
            risk = entry - stop
            if risk <= 0:
                return None
            target = entry + target_r * risk
            return {'direction': 'long', 'entry': entry, 'stop': stop, 'target': target}

    return None