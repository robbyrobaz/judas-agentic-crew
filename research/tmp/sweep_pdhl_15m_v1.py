"""
ICT-style Liquidity Sweep entry at PRIOR-SESSION HIGH/LOW (PDH/PDL).
Built on Mulham (9oSQ_XsnMcI) Case A pattern but using ROLLING PRIOR-SESSION
H/L instead of arbitrary lookback H/L. The level is the high/low of N bars ago
(default N = 48 bars = 4h on 5m, roughly one US session).

ENTRY: At the close of a 5m bar that:
  - exceeded the prior-session high (short setup) OR
  - exceeded the prior-session low (long setup)
  AND CLOSED BACK INSIDE (wick-only sweep, Mulham Case A)
EXIT: target_r * risk above/below entry
STOP: beyond the sweep extreme (with ATR buffer)

Filter:  killzone UTC (London 06-11, NY 12-16), min_eq_tol (require sweep > tolerance)
"""
import numpy as np

def evaluate(bars, params):
    n = len(bars)
    if n < 100:
        return None

    session_bars = int(params.get('session_bars', 48))   # 48 * 5m = 4h
    atr_period = int(params.get('atr_period', 14))
    target_r = float(params.get('target_r', 2.0))
    stop_atr = float(params.get('stop_atr', 0.15))
    sweep_window = int(params.get('sweep_window', 3))    # 3 bars back max
    min_sweep_atr_mult = float(params.get('min_sweep_atr_mult', 0.0))  # min sweep distance (in ATR multiples)
    killzone_filter = bool(params.get('killzone_filter', True))
    skip_zone_filter = bool(params.get('skip_zone_filter', False))

    if n < session_bars + atr_period + 10:
        return None

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)

    # ATR
    tr = np.zeros(n)
    for j in range(n):
        if j == 0:
            tr[j] = highs[j] - lows[j]
        else:
            tr[j] = max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1]))
    atr_vals = np.zeros(n)
    cum = 0.0
    for j in range(atr_period):
        cum += tr[j]
    atr_vals[atr_period-1] = cum / atr_period
    for j in range(atr_period, n):
        atr_vals[j] = (atr_vals[j-1]*(atr_period-1) + tr[j]) / atr_period

    cur_i = n - 1
    if cur_i < atr_period + session_bars:
        return None
    atr = atr_vals[cur_i]
    if atr <= 0:
        return None

    # Killzone
    in_kz = True
    if killzone_filter:
        try:
            if 'ts' in bars.columns:
                ts = bars['ts'].iloc[cur_i]
            else:
                ts = bars.index[cur_i]
            mod_min = ts.hour * 60 + ts.minute
            in_kz = (6*60 <= mod_min < 11*60) or (12*60 <= mod_min < 16*60)
        except Exception:
            in_kz = True
    if not in_kz:
        return None

    # Compute rolling PRIOR-session H/L (session_bars old window, NOT including current bar)
    prior_hi = float(np.max(highs[max(0, cur_i - session_bars - 1):max(1, cur_i)]))
    prior_lo = float(np.min(lows[max(0, cur_i - session_bars - 1):max(1, cur_i)]))

    # Look in last `sweep_window` bars for a sweep event
    short_setup = False
    long_setup = False
    sweep_bar = -1
    for j in range(max(2, cur_i - sweep_window), cur_i + 1):
        # SHORT: bar high pierces prior_hi AND bar close is back below prior_hi (wick-only sweep)
        if highs[j] > prior_hi and closes[j] <= prior_hi and (highs[j] - prior_hi) >= atr * min_sweep_atr_mult:
            short_setup = True
            sweep_bar = j
            break
        # LONG: bar low pierces prior_lo AND bar close is back above prior_lo
        if lows[j] < prior_lo and closes[j] >= prior_lo and (prior_lo - lows[j]) >= atr * min_sweep_atr_mult:
            long_setup = True
            sweep_bar = j
            break

    if not (short_setup or long_setup):
        return None

    sb_h = highs[sweep_bar]
    sb_l = lows[sweep_bar]
    sb_c = closes[sweep_bar]

    if short_setup:
        # only enter if current bar's close is STILL below prior_hi (i.e., sweep confirmed and price turned)
        if closes[cur_i] >= prior_hi:
            return None
        entry = sb_c  # enter at sweep candle close (Mulham Case A)
        stop = sb_h + atr * stop_atr
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - target_r * risk
        return {'direction': 'short', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
    else:  # long
        if closes[cur_i] <= prior_lo:
            return None
        entry = sb_c
        stop = sb_l - atr * stop_atr
        risk = entry - stop
        if risk <= 0:
            return None
        target = entry + target_r * risk
        return {'direction': 'long', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
