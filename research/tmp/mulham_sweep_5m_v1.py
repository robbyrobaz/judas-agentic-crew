"""
Mulham 1-min liquidity sweep + 3-case entry model — adapted to 5m.
Source: YT video 9oSQ_XsnMcI (Mulham Trading, 2026 SMC 1-min liquidity sweep).

3 cases:
  A: wick-only sweep -> enter at sweep candle close
  B: body sweep -> wait for next 5m candle to close back inside -> enter at next candle close
  C: wide momentum sweep -> wait for FVG to form post-sweep -> enter at FVG midpoint (50%)

Killzones (UTC):
  London: 06:00-11:00 (broad catch for EST/EDT)
  NY:     12:00-16:00

Liquidity level = highest high / lowest low over lookback (session swing H/L).
"""
import numpy as np

def evaluate(bars, params):
    n = len(bars)
    if n < 50:
        return None

    lookback = int(params.get('lookback', 30))
    min_eq_tol = float(params.get('min_eq_tol', 0.3))
    atr_period = int(params.get('atr_period', 14))
    target_r = float(params.get('target_r', 2.0))
    case = str(params.get('case', 'B')).upper()
    stop_atr = float(params.get('stop_atr', 0.2))
    killzone_filter = bool(params.get('killzone_filter', True))
    check_window = int(params.get('check_window', 5))
    require_conf_close = bool(params.get('require_conf_close', True))

    if n < lookback + atr_period + 5:
        return None

    highs = bars['high'].values.astype(float)
    lows = bars['low'].values.astype(float)
    closes = bars['close'].values.astype(float)
    opens = bars['open'].values.astype(float)

    # ATR (Wilder, RMA)
    tr = np.zeros(n)
    for j in range(n):
        if j == 0:
            tr[j] = highs[j] - lows[j]
        else:
            tr[j] = max(highs[j]-lows[j], abs(highs[j]-closes[j-1]), abs(lows[j]-closes[j-1]))
    atr_vals = np.zeros(n)
    if n >= atr_period:
        cum = 0.0
        for j in range(atr_period):
            cum += tr[j]
        atr_vals[atr_period - 1] = cum / atr_period
        for j in range(atr_period, n):
            atr_vals[j] = (atr_vals[j-1] * (atr_period - 1) + tr[j]) / atr_period

    cur_i = n - 1
    if cur_i < atr_period:
        return None
    atr = atr_vals[cur_i]
    if atr <= 0:
        return None

    eq_tol = atr * min_eq_tol

    # Session killzone (UTC minutes-of-day)
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

    # Find session swing H/L over lookback (must END before current bar)
    sw_high = -np.inf
    sw_low = np.inf
    for j in range(max(0, cur_i - lookback), cur_i):
        if highs[j] > sw_high: sw_high = highs[j]
        if lows[j] < sw_low: sw_low = lows[j]
    if sw_high < 0 or sw_low == np.inf:
        return None

    # Search recent bars for a sweep BEYOND swing H or L (with tolerance)
    direction = None
    sweep_bar = -1
    for j in range(max(2, cur_i - check_window), cur_i + 1):
        if highs[j] > sw_high + eq_tol and closes[j] > sw_high - eq_tol:
            # Sweep above + close still above tolerance = potential short setup
            # But for a true SWEEP we need price to come BACK below
            pass
        if lows[j] < sw_low - eq_tol and closes[j] < sw_low + eq_tol:
            pass
        # A sweep happens when high exceeds swing high by > eq_tol
        if highs[j] > sw_high + eq_tol:
            direction = 'short'
            sweep_bar = j
            break
        if lows[j] < sw_low - eq_tol:
            direction = 'long'
            sweep_bar = j
            break
    if direction is None or sweep_bar < 0:
        return None

    sb_h = highs[sweep_bar]
    sb_l = lows[sweep_bar]
    sb_c = closes[sweep_bar]
    sb_o = opens[sweep_bar]

    # Mulham terminology (carefully re-read):
    #   Case A: sweep candle CLOSES BACK INSIDE the level (wick-only beyond).
    #          Enter IMMEDIATELY at sweep-candle close.
    #   Case B: sweep candle CLOSES BEYOND the level (body beyond = failed level).
    #          Wait for NEXT 5m candle to close BACK INSIDE the level (reclaim)
    #          -> enter at the next candle's close (CISD-style reversal).
    #   Case C: massive momentum candle engulfs — wait for FVG to form below/above
    #          sweep bar, then enter at FVG midpoint (50%).
    #
    # For SHORT (against high sweep):
    #   Case A short: high > sw_high, close <= sw_high  (wick-only above)
    #   Case B short: close > sw_high (failed resistance), next close < sw_high (reclaim below)
    wick_short = (sb_h > sw_high + eq_tol) and (sb_c <= sw_high + eq_tol)
    fail_short = (sb_h > sw_high + eq_tol) and (sb_c > sw_high + eq_tol)
    wick_long = (sb_l < sw_low - eq_tol) and (sb_c >= sw_low - eq_tol)
    fail_long = (sb_l < sw_low - eq_tol) and (sb_c < sw_low - eq_tol)

    if direction == 'short':
        if case == 'A' and wick_short:
            entry = sb_c
            stop = sb_h + atr * stop_atr
            risk = stop - entry
            target = entry - target_r * risk
            return {'direction': 'short', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
        if case == 'B' and fail_short:
            # Need confirmation: next candle close BACK BELOW sw_high (reclaim below)
            if sweep_bar + 1 <= cur_i:
                c1 = closes[sweep_bar + 1]
                if c1 < sw_high - eq_tol * 0.5:  # meaningful reclaim
                    entry = c1
                    stop = sb_h + atr * stop_atr
                    risk = stop - entry
                    target = entry - target_r * risk
                    return {'direction': 'short', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
        if case == 'C' and sweep_bar + 1 <= cur_i:
            # Bearish FVG: high[k-1] < low[k+1] (gap down between the two after the sweep)
            # Looking for FVG in the 3 bars AFTER the sweep bar
            for k in range(sweep_bar + 2, min(cur_i + 1, sweep_bar + 8)):
                if (k - 2) < 0: continue
                # bearish FVG exists when highs[k-2] < lows[k]
                if highs[k-2] < lows[k] and (lows[k] - highs[k-2]) > atr * 0.1:
                    fvg_top = lows[k]
                    fvg_bot = highs[k-2]
                    mid = (fvg_top + fvg_bot) / 2
                    # current bar retests into the FVG (low touches mid ± buffer)
                    if lows[k] <= mid + atr * 0.1:
                        entry = mid
                        stop = sb_h + atr * stop_atr
                        risk = stop - entry
                        target = entry - target_r * risk
                        return {'direction': 'short', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
    else:  # long
        if case == 'A' and wick_long:
            entry = sb_c
            stop = sb_l - atr * stop_atr
            risk = entry - stop
            target = entry + target_r * risk
            return {'direction': 'long', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
        if case == 'B' and fail_long:
            if sweep_bar + 1 <= cur_i:
                c1 = closes[sweep_bar + 1]
                if c1 > sw_low + eq_tol * 0.5:  # meaningful reclaim
                    entry = c1
                    stop = sb_l - atr * stop_atr
                    risk = entry - stop
                    target = entry + target_r * risk
                    return {'direction': 'long', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
        if case == 'C' and sweep_bar + 1 <= cur_i:
            for k in range(sweep_bar + 2, min(cur_i + 1, sweep_bar + 8)):
                if (k - 2) < 0: continue
                # bullish FVG: lows[k-2] > highs[k]
                if lows[k-2] > highs[k] and (lows[k-2] - highs[k]) > atr * 0.1:
                    fvg_top = lows[k-2]
                    fvg_bot = highs[k]
                    mid = (fvg_top + fvg_bot) / 2
                    if highs[k] >= mid - atr * 0.1:
                        entry = mid
                        stop = sb_l - atr * stop_atr
                        risk = entry - stop
                        target = entry + target_r * risk
                        return {'direction': 'long', 'entry': float(entry), 'stop': float(stop), 'target': float(target)}
    return None
