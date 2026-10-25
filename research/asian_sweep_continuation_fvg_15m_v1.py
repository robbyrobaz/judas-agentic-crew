"""Asian-range sweep -> displacement continuation (FVG retest) -- 15m.

NEW ARCHITECTURE for this roster. The book already contains
`asian_range_sweep_mitigation_block` (sweep of the Asian range, then fade BACK
INSIDE the range). This is the other half of the same ICT idea and it does not
exist in the book: the sweep of the Asian high/low is a liquidity grab, price
REJECTS back through the level with displacement, and the resulting FVG is
retested in the direction of the rejection.

Logic
  1. Build the most recent COMPLETED Asian range (UTC hour window).
  2. Find a SWEEP bar: it pokes beyond the Asian high/low by
     min_sweep_atr x ATR but CLOSES back inside -- a failed breakout, i.e.
     liquidity taken from the resting stop orders.
  3. Within confirm_bars, require a REJECTION displacement bar that closes
     back beyond the swept level (through the liquidity) with a clean body
     (body_ratio >= min) and a real range (>= disp_k x ATR).
  4. Enter at the midpoint of the 3-bar FVG left by the rejection, but only
     when the signal bar actually trades through that midpoint
     (entry-touch validation, see finding 9133).
  5. Stop beyond the sweep bar's extreme -/+ stop_buf_atr x ATR;
     target = rr x risk.

Stateless; numpy only.
"""
import numpy as np


def _utc_hours(bars):
    try:
        ts = bars['ts'].values
        return (ts.astype('datetime64[h]').astype('int64') % 24).astype(float)
    except Exception:  # noqa: BLE001
        return np.zeros(len(bars))


def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    cur = n - 1

    atr_period = int(params.get('atr_period', 14))
    min_sweep_atr = float(params.get('min_sweep_atr', 0.15))
    disp_k = float(params.get('disp_k', 0.9))
    body_ratio_min = float(params.get('body_ratio_min', 0.50))
    confirm_bars = int(params.get('confirm_bars', 4))
    sweep_lookback = int(params.get('sweep_lookback', 24))
    fvg_expiry = int(params.get('fvg_expiry', 8))
    rr = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.15))
    asia_start = int(params.get('asia_start_utc', 0))
    asia_end = int(params.get('asia_end_utc', 7))
    trade_start = float(params.get('trade_start_utc', 7))
    trade_end = float(params.get('trade_end_utc', 20))
    min_asian_range_atr = float(params.get('min_asian_range_atr', 1.0))

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    opens = bars['open'].values.astype(float)
    closes = bars['close'].values.astype(float)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    if len(tr) < atr_period:
        return None
    atr = float(np.mean(tr[-atr_period:]))
    if atr <= 1e-9:
        return None

    hrs = _utc_hours(bars)
    if not (trade_start <= hrs[cur] < trade_end):
        return None

    # --- most recent COMPLETED Asian session ---
    asia_end_i = -1
    floor = max(1, cur - 220)
    for i in range(cur, floor, -1):
        if (hrs[i] >= asia_start and hrs[i] < asia_end
                and not (hrs[i - 1] >= asia_start and hrs[i - 1] < asia_end)):
            asia_end_i = i
            break
    if asia_end_i < 0:
        return None
    s = asia_end_i
    while s > 0 and hrs[s - 1] >= asia_start and hrs[s - 1] < asia_end:
        s -= 1
    a_high = float(np.max(highs[s:asia_end_i + 1]))
    a_low = float(np.min(lows[s:asia_end_i + 1]))
    if (a_high - a_low) < min_asian_range_atr * atr:
        return None

    # --- sweep bar: poked beyond the Asian range, closed back inside ---
    lo = max(asia_end_i + 1, cur - sweep_lookback)
    for sw in range(cur, lo - 1, -1):
        rng = highs[sw] - lows[sw]
        if rng <= 1e-9:
            continue
        bull = False
        bear = False
        if highs[sw] > a_high + min_sweep_atr * atr and closes[sw] < a_high:
            bull = True
        elif lows[sw] < a_low - min_sweep_atr * atr and closes[sw] > a_low:
            bear = True
        if not (bull or bear):
            continue

        # --- rejection displacement back through the level ---
        d_lo = sw + 1
        d_hi = min(cur, sw + confirm_bars)
        for d in range(d_lo, d_hi + 1):
            drng = highs[d] - lows[d]
            if drng <= 1e-9:
                continue
            if abs(closes[d] - opens[d]) < body_ratio_min * drng:
                continue
            if drng < disp_k * atr:
                continue
            if bull and closes[d] <= a_high:
                continue
            if bear and closes[d] >= a_low:
                continue

            sweep_high = float(np.max(highs[sw:d + 1]))
            sweep_low = float(np.min(lows[sw:d + 1]))

            hi_k = min(cur, d + fvg_expiry)
            for k in range(d + 1, hi_k + 1):
                if k - 2 < 0:
                    continue
                if bull:
                    zone_hi = lows[k]
                    zone_lo = highs[k - 2]
                else:
                    zone_hi = highs[k - 2]
                    zone_lo = lows[k]
                if zone_hi <= zone_lo:
                    continue
                mid = (zone_hi + zone_lo) / 2.0
                if not (lows[cur] <= mid <= highs[cur]):
                    continue
                if bull:
                    entry = mid
                    stop = sweep_low - stop_buf_atr * atr
                    risk = entry - stop
                    if risk <= 1e-9:
                        continue
                    target = entry + rr * risk
                else:
                    entry = mid
                    stop = sweep_high + stop_buf_atr * atr
                    risk = stop - entry
                    if risk <= 1e-9:
                        continue
                    target = entry - rr * risk
                return {
                    'direction': 'long' if bull else 'short',
                    'entry': float(entry),
                    'stop': float(stop),
                    'target': float(target),
                    'ts': bars['ts'].iloc[cur],
                }
    return None
