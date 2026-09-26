"""
Strict ICT Order Block + Sweep-First filter (15m timeframe).

Reference: ICT 4-criteria OB framework (ictkillzone.com, May 2026).
  Criterion 1: a liquidity sweep MUST precede the OB formation. No sweep, no valid OB.
  Criterion 2: displacement candle >= 1.5x ATR / leaves FVG / wick < 25%.
  Criterion 3: zone must be unmitigated (no body close through).
  Criterion 4: HTF alignment / premium-discount.

This implementation honours criteria 1 (the explicit sweep pre-filter that
the existing ob_midpoint_reversion CSIDs 222/250 lack) and 2 (displacement
threshold). Criterion 4 is approximated via sweep lookback window choice.

LONG setup:
  1. In bars [j-sweep_lookback, j): a swing LOW pivot s.
  2. Between s and j, some bar's low SWEEPS below s_low (institutional grab).
  3. j is a BEARISH candle (the candidate OB).
  4. j+1 is a BULLISH displacement candle with range >= displacement_k * ATR.
  5. Current bar retraces into j's BODY from above, close > midpoint, bullish bar.
     -> Enter LONG at midpoint of j's body, stop beyond l_j.

SHORT setup: mirror.
"""
import numpy as np


def evaluate(bars, params):
    n = len(bars)
    if n < 100:
        return None
    cur_i = n - 1
    if cur_i < 60:
        return None

    atr_period   = int(params.get('atr_period', 14))
    displacement_k = float(params.get('displacement_k', 1.5))
    rr           = float(params.get('rr', 2.0))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.25))
    ob_age_max   = int(params.get('ob_age_max', 10))
    body_ratio_min = float(params.get('body_ratio_min', 0.45))
    sweep_lookback = int(params.get('sweep_lookback', 16))
    pivot_len    = int(params.get('pivot_len', 4))
    require_fvg  = bool(params.get('require_fvg', False))

    # ATR
    if cur_i - atr_period < 0:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values.astype(float) -
            bars['low'].iloc[cur_i - atr_period:cur_i].values.astype(float))
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None
    buf = atr * stop_buf_atr

    cur_open  = float(bars['open'].iloc[cur_i])
    cur_high  = float(bars['high'].iloc[cur_i])
    cur_low   = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])

    # Walk back to find latest OB candidate
    search_floor = max(atr_period + 5, 30)
    for j in range(cur_i - 2, max(cur_i - 2 - ob_age_max, search_floor), -1):
        o_j = float(bars['open'].iloc[j])
        h_j = float(bars['high'].iloc[j])
        l_j = float(bars['low'].iloc[j])
        c_j = float(bars['close'].iloc[j])
        rng_j = h_j - l_j
        if rng_j <= 1e-9:
            continue
        body_j = abs(c_j - o_j)
        # body ratio only matters for the OB candle; OBs in ICT often have
        # modest body — do not reject too aggressively.
        if body_j < body_ratio_min * rng_j:
            continue

        if j + 1 >= cur_i:
            continue
        o_k = float(bars['open'].iloc[j + 1])
        h_k = float(bars['high'].iloc[j + 1])
        l_k = float(bars['low'].iloc[j + 1])
        c_k = float(bars['close'].iloc[j + 1])
        rng_k = h_k - l_k

        # CASE A — bullish OB (last bearish candle before bullish displacement)
        if c_j < o_j and c_k > o_k and rng_k >= displacement_k * atr:
            # sweep pre-filter: find a swing LOW pivot in [j - sweep_lookback, j)
            # whose level has been swept by some bar between pivot and j.
            win_start = max(j - sweep_lookback, pivot_len + 1)
            swept_low = None
            for s in range(win_start, j):
                s_low = float(bars['low'].iloc[s])
                # Pivot: min low within +/- pivot_len (excluding s).
                is_pivot = True
                for sb in range(s - pivot_len, s + pivot_len + 1):
                    if sb == s:
                        continue
                    if sb < 0 or sb >= j:
                        continue
                    if float(bars['low'].iloc[sb]) < s_low - 1e-9:
                        is_pivot = False
                        break
                if not is_pivot:
                    continue
                # Was this pivot SWEPT before j?
                for swb in range(s + 1, j):
                    if float(bars['low'].iloc[swb]) < s_low - 1e-9:
                        swept_low = s_low
                        break
                if swept_low is not None:
                    break

            if swept_low is None:
                continue

            # OB zone = j's body. For bearish candle, open is top, close is bottom.
            ob_top = o_j
            ob_bot = c_j
            if ob_top - ob_bot < 1e-9:
                continue
            ob_mid = (ob_top + ob_bot) / 2.0

            # Optional FVG check on displacement (k): bullish FVG means
            # bars[k].low > bars[k-2].high (gap between prior candle and
            # two-bar-after-OB). Here we approximate with: k's low > j's high
            # is too strict; instead require k's range >= fvg_k * atr.
            if require_fvg:
                # simple FVG: low of j+2 > high of j  (would need j+2 idx)
                if j + 2 < cur_i:
                    if not (float(bars['low'].iloc[j + 2]) > h_j + 1e-9):
                        continue

            # Current bar retraces into OB body, close above midpoint, bullish bar
            if cur_low <= ob_top and cur_close > ob_mid and cur_close > cur_open:
                entry = ob_mid
                stop = l_j - buf
                risk = entry - stop
                if risk > 1e-9:
                    return {
                        'direction': 'long',
                        'entry': entry,
                        'stop': stop,
                        'target': entry + rr * risk,
                    }
            # no continue here — fall through to short case attempt? No,
            # we're done with j in this case; break outer loop after this j.

        # CASE B — bearish OB (last bullish candle before bearish displacement)
        elif c_j > o_j and c_k < o_k and rng_k >= displacement_k * atr:
            win_start = max(j - sweep_lookback, pivot_len + 1)
            swept_high = None
            for s in range(win_start, j):
                s_high = float(bars['high'].iloc[s])
                is_pivot = True
                for sb in range(s - pivot_len, s + pivot_len + 1):
                    if sb == s:
                        continue
                    if sb < 0 or sb >= j:
                        continue
                    if float(bars['high'].iloc[sb]) > s_high + 1e-9:
                        is_pivot = False
                        break
                if not is_pivot:
                    continue
                for swb in range(s + 1, j):
                    if float(bars['high'].iloc[swb]) > s_high + 1e-9:
                        swept_high = s_high
                        break
                if swept_high is not None:
                    break

            if swept_high is None:
                continue

            # For bullish candle j: close is top, open is bottom.
            ob_top = c_j
            ob_bot = o_j
            if ob_top - ob_bot < 1e-9:
                continue
            ob_mid = (ob_top + ob_bot) / 2.0

            if require_fvg:
                # bearish FVG: high of j+2 < low of j
                if j + 2 < cur_i:
                    if not (float(bars['high'].iloc[j + 2]) < l_j - 1e-9):
                        continue

            if cur_high >= ob_bot and cur_close < ob_mid and cur_close < cur_open:
                entry = ob_mid
                stop = h_j + buf
                risk = stop - entry
                if risk > 1e-9:
                    return {
                        'direction': 'short',
                        'entry': entry,
                        'stop': stop,
                        'target': entry - rr * risk,
                    }

        # Only consider the most recent OB candidate per bar.
        break

    return None
