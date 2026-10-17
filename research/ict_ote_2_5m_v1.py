"""
ICT OTE 2.0 (FXNX 2026 framework) — Displacement + 70.5% Fib Pullback + -0.27 Extension Target (5m)

Architecture (FRESH — distinct from CSID 298 ote_displacement_reversion_15m):

  1. Find displacement (body_ratio >= 0.55, range >= 1.0 * ATR(14))
     - Displacement CLOSES THROUGH a prior swing pivot (creates MSS)
  2. Build dealing_range = |displacement extreme - swing pivot|
  3. Compute Fib levels (retracement from extreme toward origin):
     - 62% retracement = extreme - 0.62 * dealing_range (in discount zone for longs)
     - 70.5% retracement = extreme - 0.705 * dealing_range (sweet spot — midpoint of cluster)
     - 79% retracement = extreme - 0.79 * dealing_range
  4. WAIT for price to retrace INTO the 62-79% zone
  5. ENTRY: at 70.5% level (only fire when current bar touches the 70.5% level)
  6. STOP: at 100% (pivot origin) + 0.10 * ATR buffer
  7. TARGET: -0.27 extension = origin + 1.27 * dealing_range
     (i.e., 27% beyond displacement extreme — higher RR than CSID 298's "target = extreme")
  8. KILLZONE filter: NY 13-16 UTC OR London 8-11 UTC
  9. HTF BIAS: 1h EMA(60) must agree with direction
  10. COOLDOWN: avoid same-bar duplicate-fire across symbols

Differentiation from CSID 298:
  - 5m bars (vs 15m)
  - Specific 70.5% sweet spot (vs 62%)
  - -0.27 extension target (vs displacement extreme target — 27% more upside)
  - Tighter killzone filter (NY 13-16 + LDN 8-11)
  - Higher target_R per trade

Source: FXNX 2026 OTE tutorial (https://www.youtube.com/watch?v=JtOxMfcrESE)
"""
import numpy as np
import pandas as pd


def _atr(bars, period=14):
    h = bars["high"].astype(float)
    l = bars["low"].astype(float)
    c = bars["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def _resample_1h(bars):
    df = bars[['ts', 'open', 'high', 'low', 'close']].copy()
    df['ts'] = pd.to_datetime(df['ts'], utc=True, errors='coerce')
    df = df.dropna(subset=['ts'])
    df = df.set_index('ts')
    h1 = df.resample('1h').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    return h1


def evaluate(bars, params):
    if bars is None or len(bars) < 80:
        return None

    session_ny_start = int(params.get("session_ny_start", 13))
    session_ny_end = int(params.get("session_ny_end", 16))
    session_ldn_start = int(params.get("session_ldn_start", 8))
    session_ldn_end = int(params.get("session_ldn_end", 11))
    atr_period = int(params.get("atr_period", 14))
    struct_lookback = int(params.get("struct_lookback", 20))
    body_ratio_min = float(params.get("body_ratio_min", 0.55))
    disp_atr_mult = float(params.get("disp_atr_mult", 1.0))
    htf_ema_period = int(params.get("htf_ema_period", 60))
    swing_pivots_back = int(params.get("swing_pivots_back", 4))
    stop_buffer_atr = float(params.get("stop_buffer_atr", 0.10))

    n = len(bars)
    cur_i = n - 1

    h = bars["high"].values.astype(float)
    l = bars["low"].values.astype(float)
    c = bars["close"].values.astype(float)
    o = bars["open"].values.astype(float)

    # Killzone filter
    try:
        cur_ts = pd.to_datetime(bars["ts"].iloc[cur_i], utc=True, errors="coerce")
        if pd.isna(cur_ts):
            return None
        hr = cur_ts.hour
    except Exception:
        return None
    in_ny = session_ny_start <= hr < session_ny_end
    in_ldn = session_ldn_start <= hr < session_ldn_end
    if not (in_ny or in_ldn):
        return None

    atr_series = _atr(bars, period=atr_period).values
    if cur_i < atr_period or pd.isna(atr_series[cur_i]) or atr_series[cur_i] <= 0:
        return None
    cur_atr = float(atr_series[cur_i])

    # HTF bias (1h EMA60)
    try:
        h1 = _resample_1h(bars)
        if len(h1) < htf_ema_period + 1:
            return None
        h1_close = h1['close'].values.astype(float)
        h1_ema = pd.Series(h1_close).ewm(span=htf_ema_period, adjust=False).mean().values
        cur_ts = pd.to_datetime(bars['ts'].iloc[cur_i], utc=True, errors='coerce')
        last_h1_idx = None
        for k in range(len(h1) - 1, -1, -1):
            if h1.index[k] <= cur_ts:
                last_h1_idx = k
                break
        if last_h1_idx is None or last_h1_idx < 1:
            return None
        h1_cur = float(h1_close[last_h1_idx])
        h1_ema_cur = float(h1_ema[last_h1_idx])
        htf_bull = h1_cur > h1_ema_cur
        htf_bear = h1_cur < h1_ema_cur
    except Exception:
        return None

    # Find recent swing pivots (last struct_lookback bars)
    # We use a 2-bar confirmation window on each side
    swing_high = None
    swing_high_i = -1
    swing_low = None
    swing_low_i = -1

    lo_idx = max(2, cur_i - struct_lookback)
    hi_idx = cur_i - 2

    for j in range(hi_idx, lo_idx - 1, -1):
        if j - 2 < 0 or j + 2 >= n:
            continue
        is_pivot_high = (h[j] >= h[j-2]) and (h[j] >= h[j-1]) and (h[j] >= h[j+1]) and (h[j] >= h[j+2])
        is_pivot_low = (l[j] <= l[j-2]) and (l[j] <= l[j-1]) and (l[j] <= l[j+1]) and (l[j] <= l[j+2])
        if is_pivot_high and swing_high_i < 0:
            swing_high = float(h[j])
            swing_high_i = j
        if is_pivot_low and swing_low_i < 0:
            swing_low = float(l[j])
            swing_low_i = j
        if swing_high_i >= 0 and swing_low_i >= 0:
            break

    if swing_high is None or swing_low is None:
        return None
    if swing_high <= swing_low:
        return None
    if cur_i <= max(swing_high_i, swing_low_i):
        return None

    # Find displacement CLOSING THROUGH prior pivot (MSS)
    # Search backwards from cur_i - 1 to the pivot
    search_start = max(swing_high_i, swing_low_i) + 1
    search_end = cur_i - 1

    disp_i = -1
    disp_dir = 0  # 1 = bullish displacement (close > swing_high), -1 = bearish (close < swing_low)
    disp_extreme = 0.0
    disp_origin = 0.0

    for j in range(search_end, search_start - 1, -1):
        rng_j = h[j] - l[j]
        if rng_j <= 0:
            continue
        body_j = abs(c[j] - o[j])
        if body_j / rng_j < body_ratio_min:
            continue
        atr_j = atr_series[j]
        if np.isnan(atr_j) or atr_j <= 0:
            continue
        if rng_j < disp_atr_mult * atr_j:
            continue
        # Bullish displacement closes above swing_high
        if c[j] > swing_high and swing_high_i < j:
            disp_i = j
            disp_dir = 1
            disp_extreme = float(h[j])
            disp_origin = swing_low  # origin is the swing LOW (start of upward move)
            break
        # Bearish displacement closes below swing_low
        if c[j] < swing_low and swing_low_i < j:
            disp_i = j
            disp_dir = -1
            disp_extreme = float(l[j])
            disp_origin = swing_high  # origin is the swing HIGH (start of downward move)
            break

    if disp_i < 0:
        return None

    dealing_range = abs(disp_extreme - disp_origin)
    if dealing_range <= 0:
        return None

    # HTF bias must agree
    if disp_dir == 1 and not htf_bull:
        return None
    if disp_dir == -1 and not htf_bear:
        return None

    # Compute Fib levels (retracement from extreme toward origin)
    if disp_dir == 1:
        # Long: displacement up; retracement DOWN from extreme
        fib_62 = disp_extreme - 0.62 * dealing_range
        fib_705 = disp_extreme - 0.705 * dealing_range
        fib_79 = disp_extreme - 0.79 * dealing_range
        entry = fib_705
        stop = disp_origin - stop_buffer_atr * cur_atr
        target = disp_origin + 1.27 * dealing_range  # -0.27 extension
        # Wait: current bar must touch 70.5% level
        # Bar's low <= entry <= bar's high
        if not (l[cur_i] <= entry <= h[cur_i]):
            return None
        # Confirmation: close in 62-79% zone (pullback)
        if not (fib_79 <= c[cur_i] <= fib_62):
            return None
        risk = entry - stop
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        return {"direction": "long", "entry": entry, "stop": stop, "target": target}
    else:
        # Short: displacement down; retracement UP from extreme
        fib_62 = disp_extreme + 0.62 * dealing_range
        fib_705 = disp_extreme + 0.705 * dealing_range
        fib_79 = disp_extreme + 0.79 * dealing_range
        entry = fib_705
        stop = disp_origin + stop_buffer_atr * cur_atr
        target = disp_origin - 1.27 * dealing_range
        if not (l[cur_i] <= entry <= h[cur_i]):
            return None
        if not (fib_62 <= c[cur_i] <= fib_79):
            return None
        risk = stop - entry
        if risk <= 0 or risk > 4.0 * cur_atr:
            return None
        return {"direction": "short", "entry": entry, "stop": stop, "target": target}