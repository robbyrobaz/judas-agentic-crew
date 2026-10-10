"""1h Displacement Continuation + 5m FVG Midpoint Pullback — 5m.

ICT principle: When a higher timeframe (1h) prints a strong displacement candle, the move
has "intent" — expect continuation. The 5m retraces into a Fair Value Gap (the imbalance
left by the displacement) and offers a midpoint re-entry in the direction of the HTF move.

Setup:
1. Detect 1h displacement candle in last K hours (close > open + X * ATR for bull, reverse)
2. On 5m, detect an FVG that aligns with the HTF direction (in the discount zone for bull)
3. Wait for price to reach the FVG midpoint
4. Enter in HTF direction
5. Stop = FVG edge + buffer, target = 2R

This is essentially a "1h displacement → 5m FVG midpoint pullback" reversion entry.
Distinct from existing strategies:
- ict_mitigation_block_5m_v2_disp1.5 uses 5m displacement, not 1h HTF
- ifvg_midpoint_reversion_htf_bias uses HTF bias via close vs SMA, not displacement
- atr_disp_continuation uses 5m ATR displacement, entry at breakout not midpoint
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None
    n = len(bars)
    htf_bars_per_ltf = int(params.get("htf_bars_per_ltf", 12))  # 12*5m = 1h
    htf_lookback = int(params.get("htf_lookback", 12))          # last 12 HTF bars (12h)
    ltf_lookback = int(params.get("ltf_lookback", 24))          # last 24 LTF bars (2h)
    disp_min_atr = float(params.get("disp_min_atr", 1.0))
    disp_body_min = float(params.get("disp_body_min", 0.65))
    fvg_min_factor = float(params.get("fvg_min_factor", 0.10))
    fvg_max_age = int(params.get("fvg_max_age", 24))
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

    # Compute 1h HTF bars (resample 5m to 1h)
    htf_n = n // htf_bars_per_ltf
    if htf_n < 8:
        return None
    htf_h = np.empty(htf_n)
    htf_l = np.empty(htf_n)
    htf_o = np.empty(htf_n)
    htf_c = np.empty(htf_n)
    for k in range(htf_n):
        s = k * htf_bars_per_ltf
        e = s + htf_bars_per_ltf
        htf_h[k] = h_v[s:e].max()
        htf_l[k] = l_v[s:e].min()
        htf_o[k] = o_v[s]
        htf_c[k] = c_v[e - 1]

    # 1h ATR (for displacement threshold)
    pc = np.concatenate([[htf_c[0]], htf_c[:-1]])
    tr = np.maximum(htf_h - htf_l, np.maximum(np.abs(htf_h - pc), np.abs(htf_l - pc)))
    htf_atr = pd.Series(tr).ewm(alpha=1.0/atr_period, adjust=False).mean().values
    if htf_atr[-1] <= 0:
        return None

    # Find most recent 1h displacement candle (within htf_lookback)
    # displacement = body > disp_body_min of range, and range > disp_min_atr * ATR
    bull_disp_idx = -1
    bear_disp_idx = -1
    for i in range(htf_n - 1, max(htf_n - htf_lookback - 1, 0), -1):
        rng = htf_h[i] - htf_l[i]
        if rng <= 0 or htf_atr[i] <= 0:
            continue
        body = abs(htf_c[i] - htf_o[i])
        if rng < disp_min_atr * htf_atr[i]:
            continue
        if body / rng < disp_body_min:
            continue
        if htf_c[i] > htf_o[i]:
            if bull_disp_idx == -1:
                bull_disp_idx = i
        else:
            if bear_disp_idx == -1:
                bear_disp_idx = i
        if bull_disp_idx != -1 or bear_disp_idx != -1:
            break

    # direction: most recent displacement wins
    if bull_disp_idx > bear_disp_idx:
        direction = "long"
        disp_idx = bull_disp_idx
    elif bear_disp_idx > bull_disp_idx:
        direction = "short"
        disp_idx = bear_disp_idx
    else:
        return None

    # Find a 5m FVG that aligns with the displacement (in the right direction)
    # For bull displacement: bullish FVG (gap up) - price retraces down into it = midpoint re-entry long
    # For bear displacement: bearish FVG (gap down) - price retraces up into it = midpoint re-entry short
    avg_range_5m = np.mean(h_v[max(1, last-50):last+1] - l_v[max(1, last-50):last+1])
    if avg_range_5m <= 0:
        return None
    min_gap_5m = avg_range_5m * fvg_min_factor

    fvg_top = None
    fvg_bot = None
    for i in range(last - fvg_max_age, last - 2):
        if i < 2 or i >= last:
            continue
        if direction == "long":
            # bullish FVG: bar i low > bar i-2 high (gap up)
            if l_v[i] > h_v[i - 2] and (l_v[i] - h_v[i - 2]) >= min_gap_5m:
                fvg_top = l_v[i]
                fvg_bot = h_v[i - 2]
                # price should be inside the FVG (or have just touched it)
                if l_v[last] <= fvg_top and c_v[last] >= fvg_bot:
                    break
                else:
                    fvg_top = None
                    fvg_bot = None
        else:
            # bearish FVG: bar i high < bar i-2 low
            if h_v[i] < l_v[i - 2] and (l_v[i - 2] - h_v[i]) >= min_gap_5m:
                fvg_top = l_v[i - 2]
                fvg_bot = h_v[i]
                if h_v[last] >= fvg_bot and c_v[last] <= fvg_top:
                    break
                else:
                    fvg_top = None
                    fvg_bot = None

    if fvg_top is None or fvg_bot is None:
        return None

    cur_close = c_v[last]
    mid = 0.5 * (fvg_top + fvg_bot)
    zh = fvg_top - fvg_bot

    if direction == "long":
        entry = mid
        stop = fvg_bot - max(tick, zh * stop_buf_factor)
        risk = entry - stop
        if risk <= 0.5 * tick:
            return None
        target = entry + risk * rr
        return {"ts": ts_v[last], "direction": "long", "entry": entry, "stop": stop, "target": target}
    else:
        entry = mid
        stop = fvg_top + max(tick, zh * stop_buf_factor)
        risk = stop - entry
        if risk <= 0.5 * tick:
            return None
        target = entry - risk * rr
        return {"ts": ts_v[last], "direction": "short", "entry": entry, "stop": stop, "target": target}