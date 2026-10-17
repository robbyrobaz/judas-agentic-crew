"""Equal Highs/Lows Liquidity Sweep + Displacement Continuation — 5m.

ICT principle: Equal Highs (EQH) and Equal Lows (EQL) create concentrated liquidity.
When price sweeps above/below these levels and then reverses, the move is high-probability
because the EQH/EQL "absorbs" stops and the sweep represents forced liquidation.

Setup:
1. Detect recent EQH/EQL formation (2+ swing highs within tight tolerance in last N bars)
2. Wait for price to sweep above/below the EQH/EQL level
3. On the same bar OR within 1-2 bars, a displacement FVG forms
4. Wait for the FVG to be inverted (close through the gap)
5. Enter in continuation direction (opposite the sweep)
6. Stop beyond the swept EQH/EQL, target at 2R

Timeframe-native: 5m bars are native.
Symbol-agnostic: works on all 5 legal symbols with tick adjustment.

Fresh angle — distinct from existing strategies:
- ict_judas_swing_asian_range_5m uses Asian range (specific session)
- silver_bullet_pdh_pdl_retest uses prior day H/L (not EQH/EQL specifically)
- bsl_run_judas uses pivot breaks but doesn't require EQH/EQL clustering
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None
    n = len(bars)
    eqh_tolerance_atr = float(params.get("eqh_tolerance_atr", 0.30))
    eqh_min_count = int(params.get("eqh_min_count", 2))
    eqh_window = int(params.get("eqh_window", 50))
    sweep_lookback = int(params.get("sweep_lookback", 12))
    min_gap_factor = float(params.get("min_gap_factor", 0.10))
    rr = float(params.get("rr", 2.0))
    stop_buf_factor = float(params.get("stop_buf_factor", 0.20))
    session_start = int(params.get("session_start", 0))
    session_end = int(params.get("session_end", 24))
    tick = float(params.get("tick", 0.10))
    atr_period = int(params.get("atr_period", 14))

    h_v = bars["high"].values.astype(float)
    l_v = bars["low"].values.astype(float)
    c_v = bars["close"].values.astype(float)
    o_v = bars["open"].values.astype(float)
    ts_v = bars["ts"].values

    try:
        cur_ts = pd.to_datetime(bars["ts"].iloc[-1], utc=True, errors="coerce")
        if pd.isna(cur_ts):
            return None
        cur_h = int(cur_ts.hour)
    except Exception:
        return None
    if not (session_start <= cur_h < session_end):
        return None

    last = n - 1
    if last < 50:
        return None

    # ATR for normalization
    pc = np.concatenate([[c_v[0]], c_v[:-1]])
    tr = np.maximum(h_v - l_v, np.maximum(np.abs(h_v - pc), np.abs(l_v - pc)))
    atr = pd.Series(tr).ewm(alpha=1.0/atr_period, adjust=False).mean().values
    if atr[last] <= 0:
        return None

    # 1) Detect EQH/EQL in the last eqh_window bars (excluding current bar)
    eqh_level = None  # tuple of (level, count)
    eql_level = None
    tol = atr[last] * eqh_tolerance_atr

    # find clusters of swing highs within tol of each other
    piv = 2
    swing_high_indices = []
    swing_low_indices = []
    for j in range(piv, last - piv):
        if h_v[j] == max(h_v[max(0,j-piv):min(n,j+piv+1)]) and h_v[j] > h_v[max(0,j-1)] and h_v[j] > h_v[min(n-1,j+1)]:
            swing_high_indices.append(j)
        if l_v[j] == min(l_v[max(0,j-piv):min(n,j+piv+1)]) and l_v[j] < l_v[max(0,j-1)] and l_v[j] < l_v[min(n-1,j+1)]:
            swing_low_indices.append(j)

    # for each swing high, find nearby swing highs
    if swing_high_indices:
        # find a level touched by >= min_count swing highs within tol
        for sh in swing_high_indices:
            if sh > last - eqh_window:
                continue
            cluster = [sh]
            for sh2 in swing_high_indices:
                if sh2 != sh and abs(h_v[sh2] - h_v[sh]) <= tol:
                    cluster.append(sh2)
            if len(cluster) >= eqh_min_count:
                eqh_level = float(np.mean([h_v[s] for s in cluster]))
                break

    if swing_low_indices:
        for sl in swing_low_indices:
            if sl > last - eqh_window:
                continue
            cluster = [sl]
            for sl2 in swing_low_indices:
                if sl2 != sl and abs(l_v[sl2] - l_v[sl]) <= tol:
                    cluster.append(sl2)
            if len(cluster) >= eqh_min_count:
                eql_level = float(np.mean([l_v[s] for s in cluster]))
                break

    # 2) Detect sweep of EQH/EQL within sweep_lookback
    eqh_swept = False
    eql_swept = False
    if eqh_level is not None:
        for k in range(last - sweep_lookback, last + 1):
            if k < 0:
                continue
            if h_v[k] > eqh_level and c_v[last] < eqh_level:
                eqh_swept = True
                break
    if eql_level is not None:
        for k in range(last - sweep_lookback, last + 1):
            if k < 0:
                continue
            if l_v[k] < eql_level and c_v[last] > eql_level:
                eql_swept = True
                break

    # 3) Detect displacement FVG in the last 6 bars
    avg_range = np.mean(h_v[max(1, last-50):last+1] - l_v[max(1, last-50):last+1])
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    bullish_ifvg = None  # bullish displacement FVG just inverted up (long)
    bearish_ifvg = None  # bearish displacement FVG just inverted down (short)
    for i in range(last - 6, last - 1):
        if i < 2 or i >= last:
            continue
        # bullish displacement FVG: bar i low > bar i-2 high (gap up)
        if l_v[i] > h_v[i - 2] and (l_v[i] - h_v[i - 2]) >= min_gap:
            gap_low = h_v[i - 2]
            gap_high = l_v[i]
            # inverted up: current close >= gap_high
            if c_v[last] >= gap_high:
                bullish_ifvg = (gap_low, gap_high, i)
                break
        # bearish displacement FVG: bar i high < bar i-2 low (gap down)
        if h_v[i] < l_v[i - 2] and (l_v[i - 2] - h_v[i]) >= min_gap:
            gap_low = h_v[i]
            gap_high = l_v[i - 2]
            if c_v[last] <= gap_low:
                bearish_ifvg = (gap_low, gap_high, i)
                break

    cur_close = c_v[last]
    cur_high = h_v[last]
    cur_low = l_v[last]

    # 4) Combine:
    # LONG: EQL swept (we went BELOW lows, closed back above) + bullish iFVG (continuation up)
    if eql_swept and bullish_ifvg is not None and eql_level is not None:
        gap_low, gap_high, fi = bullish_ifvg
        zh = gap_high - gap_low
        stop = min(eql_level, cur_low) - max(tick, zh * stop_buf_factor)
        risk = cur_close - stop
        if risk <= 0.5 * tick:
            return None
        target = cur_close + risk * rr
        return {"ts": ts_v[last], "direction": "long", "entry": cur_close, "stop": stop, "target": target}

    # SHORT: EQH swept (we went ABOVE highs, closed back below) + bearish iFVG (continuation down)
    if eqh_swept and bearish_ifvg is not None and eqh_level is not None:
        gap_low, gap_high, fi = bearish_ifvg
        zh = gap_high - gap_low
        stop = max(eqh_level, cur_high) + max(tick, zh * stop_buf_factor)
        risk = stop - cur_close
        if risk <= 0.5 * tick:
            return None
        target = cur_close - risk * rr
        return {"ts": ts_v[last], "direction": "short", "entry": cur_close, "stop": stop, "target": target}

    return None