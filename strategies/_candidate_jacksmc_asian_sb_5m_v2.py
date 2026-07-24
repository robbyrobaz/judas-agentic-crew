"""
ICT Silver Bullet 2026 - JACKSMC Asian H/L variant (BtEaCp2lEzk, DgcRDvyxvIQ)
V2: Stateful sweep -> MSS -> FVG sequence.
"""
import pandas as pd
import numpy as np

# Module-level state for stateful backtest
_S = {
    'sweep_high': None,    # tuple (asian_level, sweep_bar_idx, direction)
    'sweep_low': None,
    'mss_bar': None,        # idx of confirmed MSS bar
    'mss_dir': None,        # 'long' or 'short'
    'fvg_top': None,
    'fvg_bottom': None,
    'last_processed_idx': -1,
}


def evaluate(bars, params):
    n = len(bars)
    if n < 80:
        return None
    cur_i = n - 1
    cur_bar = bars.iloc[cur_i]

    # Reset state if we went backwards
    if _S['last_processed_idx'] >= 0 and cur_i < _S['last_processed_idx']:
        _S['sweep_high'] = None
        _S['sweep_low'] = None
        _S['mss_bar'] = None
        _S['mss_dir'] = None
        _S['fvg_top'] = None
        _S['fvg_bottom'] = None
    _S['last_processed_idx'] = cur_i

    # Timestamp handling
    if 'ts' in bars.columns:
        cur_ts = pd.Timestamp(bars['ts'].iloc[cur_i])
        ts_series = pd.to_datetime(bars['ts'], utc=True)
    else:
        cur_ts = pd.Timestamp(bars.index[cur_i])
        ts_series = pd.DatetimeIndex(bars.index).tz_localize('UTC')

    if cur_ts.tzinfo is None:
        cur_ts = cur_ts.tz_localize('UTC')
    cur_hour = cur_ts.hour

    # Session gate (default London killzone 11-15 UTC = 7-11am EDT)
    win_start = int(params.get('win_start_utc', 11))
    win_end = int(params.get('win_end_utc', 15))
    if not (win_start <= cur_hour < win_end):
        # Outside window, decay state
        if _S['mss_bar'] is not None and cur_i - _S['mss_bar'] > int(params.get('mss_expiry', 50)):
            _S['mss_bar'] = None
            _S['mss_dir'] = None
            _S['fvg_top'] = None
            _S['fvg_bottom'] = None
        if _S['sweep_high'] is not None and cur_i - _S['sweep_high'][1] > int(params.get('sweep_expiry', 30)):
            _S['sweep_high'] = None
        if _S['sweep_low'] is not None and cur_i - _S['sweep_low'][1] > int(params.get('sweep_expiry', 30)):
            _S['sweep_low'] = None
        return None

    hours = ts_series.dt.hour.values

    # Build Asian H/L from most recent prior 0-3 UTC block
    asian_mask = (hours >= 0) & (hours < 4)
    asian_idx_all = np.where(asian_mask)[0]
    asian_idx = asian_idx_all[asian_idx_all < cur_i]
    if len(asian_idx) < 4:
        return None
    diffs = np.diff(asian_idx)
    gap_pos = np.where(diffs > 1)[0]
    if len(gap_pos) == 0:
        block_start = 0
    else:
        block_start = gap_pos[-1] + 1
    block_idx = asian_idx[block_start:]
    if len(block_idx) < 4:
        return None
    asian_high = float(bars['high'].iloc[block_idx].max())
    asian_low = float(bars['low'].iloc[block_idx].min())

    cur_high = float(bars['high'].iloc[cur_i])
    cur_low = float(bars['low'].iloc[cur_i])
    cur_close = float(bars['close'].iloc[cur_i])
    cur_open = float(bars['open'].iloc[cur_i])

    # ATR for sizing
    atr_period = int(params.get('atr_period', 14))
    stop_buf_atr = float(params.get('stop_buf_atr', 0.10))
    rr = float(params.get('rr', 3.0))
    if cur_i < atr_period + 1:
        return None
    rngs = (bars['high'].iloc[cur_i - atr_period:cur_i].values -
            bars['low'].iloc[cur_i - atr_period:cur_i].values)
    atr = float(np.mean(rngs))
    if atr <= 0:
        return None
    buf = atr * stop_buf_atr

    min_gap_atr_mult = float(params.get('min_gap_atr_mult', 0.05))
    min_gap = atr * min_gap_atr_mult

    # ---- STATE MACHINE ----
    # Stage 1: Sweep detection (record for later MSS check)
    if cur_high > asian_high and cur_close < asian_high:
        # BSL sweep with body-close back below (rejection) -> MSS itself
        _S['sweep_high'] = (asian_high, cur_i)
    if cur_low < asian_low and cur_close > asian_low:
        _S['sweep_low'] = (asian_low, cur_i)

    # Stage 2: MSS = full body engulfing/close back, OR wick sweep + next bar close back
    # If current bar is a sweep + close-back, MSS is now confirmed (single-bar MSS)
    # If current bar is just a sweep wick (close didn't come back), check if next bar closes back
    sweep_expire = int(params.get('sweep_expiry', 30))

    # Look for FVG forming in the displacement AFTER MSS
    # Use a 3-bar window: bars[i-2..i] vs current

    # We process BOTH long and short paths

    signal = None

    # === SHORT path ===
    # MSS confirmation: current bar wicks above Asian High AND closes below
    mss_short_now = (cur_high > asian_high and cur_close < asian_high)
    # Or: prior bar was a wick-sweep (high > AsianH but close > AsianH), and current bar closes below
    mss_short_from_prev = False
    if cur_i >= 1:
        prev_high = float(bars['high'].iloc[cur_i - 1])
        prev_close = float(bars['close'].iloc[cur_i - 1])
        prev_low = float(bars['low'].iloc[cur_i - 1])
        # Prev bar wicked above but closed above; current closes below = MSS
        if prev_high > asian_high and prev_close > asian_high and cur_close < asian_high:
            mss_short_from_prev = True

    if mss_short_now or mss_short_from_prev:
        # Look for bearish FVG in current/prior bars
        # Bearish FVG: bars[i-2].low > bars[i].high (gap down between)
        # Also accept: any prior 3-bar FVG that hasn't been filled
        h_i2 = float(bars['high'].iloc[cur_i - 2])
        l_i2 = float(bars['low'].iloc[cur_i - 2])
        h_i = cur_high
        l_i = cur_low

        # Variant A: 3-bar FVG at current bar
        if l_i2 > h_i and (l_i2 - h_i) >= min_gap:
            fvg_top = l_i2
            fvg_bottom = h_i
            entry = (fvg_top + fvg_bottom) / 2.0
            # Stop above the sweep extreme
            stop = max(cur_high, h_i2) + buf
            risk = stop - entry
            if risk > 0:
                signal = {'direction': 'short', 'entry': entry,
                          'stop': stop, 'target': entry - rr * risk}
                # Reset state
                _S['sweep_high'] = None
                _S['sweep_low'] = None
                return signal

        # Variant B: FVG in bar[i-1..i+1] just before/around MSS bar
        # Check if bars[i-3..i-1] form FVG that we'd be at the boundary of
        if cur_i >= 3 and signal is None:
            h_i3 = float(bars['high'].iloc[cur_i - 3])
            l_i3 = float(bars['low'].iloc[cur_i - 3])
            l_i1 = float(bars['low'].iloc[cur_i - 1])
            h_i1 = float(bars['high'].iloc[cur_i - 1])
            # 3-bar FVG at i-1: bars[i-3].low > bars[i-1].high
            if l_i3 > h_i1 and (l_i3 - h_i1) >= min_gap:
                # The MSS bar (i) closed below the FVG bottom, confirming displacement
                if cur_close < h_i1:
                    fvg_top = l_i3
                    fvg_bottom = h_i1
                    entry = (fvg_top + fvg_bottom) / 2.0
                    stop = cur_high + buf
                    risk = stop - entry
                    if risk > 0:
                        signal = {'direction': 'short', 'entry': entry,
                                  'stop': stop, 'target': entry - rr * risk}
                        _S['sweep_high'] = None
                        _S['sweep_low'] = None
                        return signal

    # === LONG path ===
    mss_long_now = (cur_low < asian_low and cur_close > asian_low)
    mss_long_from_prev = False
    if cur_i >= 1:
        prev_low = float(bars['low'].iloc[cur_i - 1])
        prev_close = float(bars['close'].iloc[cur_i - 1])
        if prev_low < asian_low and prev_close < asian_low and cur_close > asian_low:
            mss_long_from_prev = True

    if mss_long_now or mss_long_from_prev:
        h_i2 = float(bars['high'].iloc[cur_i - 2])
        l_i2 = float(bars['low'].iloc[cur_i - 2])
        h_i = cur_high
        l_i = cur_low

        # Variant A: 3-bar FVG at current bar
        if h_i2 < l_i and (l_i - h_i2) >= min_gap:
            fvg_bottom = h_i2
            fvg_top = l_i
            entry = (fvg_top + fvg_bottom) / 2.0
            stop = min(cur_low, l_i2) - buf
            risk = entry - stop
            if risk > 0:
                signal = {'direction': 'long', 'entry': entry,
                          'stop': stop, 'target': entry + rr * risk}
                _S['sweep_high'] = None
                _S['sweep_low'] = None
                return signal

        # Variant B: FVG at i-1, current bar closes above
        if cur_i >= 3 and signal is None:
            h_i3 = float(bars['high'].iloc[cur_i - 3])
            l_i3 = float(bars['low'].iloc[cur_i - 3])
            h_i1 = float(bars['high'].iloc[cur_i - 1])
            l_i1 = float(bars['low'].iloc[cur_i - 1])
            # Bullish FVG at i-1: bars[i-3].high < bars[i-1].low
            if h_i3 < l_i1 and (l_i1 - h_i3) >= min_gap:
                if cur_close > l_i1:
                    fvg_bottom = h_i3
                    fvg_top = l_i1
                    entry = (fvg_top + fvg_bottom) / 2.0
                    stop = cur_low - buf
                    risk = entry - stop
                    if risk > 0:
                        signal = {'direction': 'long', 'entry': entry,
                                  'stop': stop, 'target': entry + rr * risk}
                        _S['sweep_high'] = None
                        _S['sweep_low'] = None
                        return signal

    # Expire sweep state if too old
    if _S['sweep_high'] is not None and cur_i - _S['sweep_high'][1] > sweep_expire:
        _S['sweep_high'] = None
    if _S['sweep_low'] is not None and cur_i - _S['sweep_low'][1] > sweep_expire:
        _S['sweep_low'] = None

    return None
