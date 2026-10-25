"""AMD (Asian-range midpoint) displacement continuation -- 15m.

NEW ARCHITECTURE for this roster. Not a silver-bullet / mitigation-block /
iFVG-midpoint variant: those all start from a sweep of a PDH/PDL or an order
block. This one starts from the ASIAN SESSION RANGE MIDPOINT ("AMD point"),
which is the draw on liquidity that London/NY price is pulled toward.

Logic
  1. Build the most recent COMPLETED Asian range (UTC hour window, default
     00:00-07:00). Require the range to be meaningful (>= min_asian_range_atr
     x ATR) -- a dead Asian session has no midpoint worth fading.
  2. After the Asian close, look for a DISPLACEMENT bar away from the AMD
     point: body_ratio >= body_ratio_min, range >= disp_k x ATR, and the close
     at least min_amp_atr x ATR beyond the midpoint.
  3. The impulse leaves a 3-bar FVG. Wait for price to trade back into that
     FVG (entry-touch validated on the signal bar) and enter at its midpoint.
  4. Stop at the impulse extreme -/+ stop_buf_atr x ATR; target = rr x risk.

ENTRY-TOUCH VALIDATION (finding 9133 -- "retro fill fiction"): the engine
fills at the price the strategy hands it, so a strategy that hands over a
price the bar never traded manufactures profit. This code only returns a
signal when the SIGNAL BAR'S OWN RANGE contains the entry price, so the fill
price provably existed on that bar. The signal also carries 'ts' so the
engine's stale-fill diagnostic can prove the signal is contemporaneous.

Stateless: no cross-call state, no imports beyond numpy.
"""
import numpy as np


def _utc_hours(bars):
    """Hour-of-day (UTC) per bar as a float array."""
    try:
        ts = bars['ts'].values
        return (ts.astype('datetime64[h]').astype('int64') % 24).astype(float)
    except Exception:  # noqa: BLE001 - defensive: a missing ts column kills the signal
        return np.zeros(len(bars))


def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    cur = n - 1

    atr_period = int(params.get('atr_period', 14))
    disp_k = float(params.get('disp_k', 1.0))
    body_ratio_min = float(params.get('body_ratio_min', 0.55))
    min_amp_atr = float(params.get('min_amp_atr', 0.40))
    rr = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.15))
    fvg_expiry = int(params.get('fvg_expiry', 8))
    disp_lookback = int(params.get('disp_lookback', 16))
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
    cur_h = hrs[cur]
    if not (trade_start <= cur_h < trade_end):
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
    amd = (a_high + a_low) / 2.0

    # --- displacement away from the AMD point ---
    start = max(asia_end_i + 1, cur - disp_lookback)
    for j in range(cur, start - 1, -1):
        rng = highs[j] - lows[j]
        if rng <= 1e-9:
            continue
        if abs(closes[j] - opens[j]) < body_ratio_min * rng:
            continue
        if rng < disp_k * atr:
            continue
        bull = closes[j] > opens[j]
        if bull:
            if closes[j] < amd + min_amp_atr * atr:
                continue
        else:
            if closes[j] > amd - min_amp_atr * atr:
                continue

        imp_high = float(np.max(highs[j:cur + 1]))
        imp_low = float(np.min(lows[j:cur + 1]))

        # --- 3-bar FVG left by the impulse ---
        hi_k = min(cur, j + fvg_expiry)
        for k in range(j + 1, hi_k + 1):
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
                continue  # entry price was never traded on this bar
            if bull:
                entry = mid
                stop = imp_low - stop_buf_atr * atr
                risk = entry - stop
                if risk <= 1e-9:
                    continue
                target = entry + rr * risk
            else:
                entry = mid
                stop = imp_high + stop_buf_atr * atr
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
