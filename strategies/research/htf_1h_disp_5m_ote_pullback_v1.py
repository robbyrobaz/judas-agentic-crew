"""ICT 1h HTF Displacement + 5m OTE (62-79%) Pullback — fresh architecture.

Sister of CSID 371 (htf_1h_disp_5m_fvg_pullback_v1) — uses the SAME 1h
displacement detection trigger, but trades the OTE (Optimal Trade Entry)
zone instead of the FVG midpoint. Per ICT 2026 teachings, OTE is the
61.8%-78.6% Fibonacci zone of the most recent pullback — a deeper, more
selective re-entry than midpoint.

DISTINCT FROM EXISTING FAMILIES:
- ict_mitigation_block_5m_v2_disp1.5: uses 5m displacement, not 1h
- ifvg_midpoint_reversion_htf_bias: uses midpoint of iFVG (inverted FVG)
- htf_1h_disp_5m_fvg_pullback (CSID 371): same trigger, midpoint entry
- ob_midpoint_reversion_*: OB midpoint, not OTE fib

Architecture:
1. Resample 5m -> 1h bars. Detect the most recent 1h displacement candle
   (body > 65% of range, range > 1.0 ATR).
2. Direction = direction of that displacement.
3. Scan 5m bars in last 24 (2h) for an FVG aligned with HTF direction.
4. Compute FVG high/low. OTE zone = [FVG_low + 0.382*zh, FVG_low + 0.618*zh]
   for long (where zh = FVG high - FVG low).
   For short: OTE zone = [FVG_high - 0.618*zh, FVG_high - 0.382*zh].
5. Entry at the OTE zone edge (62% mark), target = 2R, stop = beyond FVG.
6. Only fire if current price is WITHIN the OTE zone.

Timeframe: 5m native; 1h resampled inside. Symbol-agnostic.
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None
    n = len(bars)
    htf_bars_per_ltf = int(params.get("htf_bars_per_ltf", 12))
    htf_lookback = int(params.get("htf_lookback", 12))
    disp_min_atr = float(params.get("disp_min_atr", 1.0))
    disp_body_min = float(params.get("disp_body_min", 0.65))
    fvg_min_factor = float(params.get("fvg_min_factor", 0.10))
    fvg_max_age = int(params.get("fvg_max_age", 24))
    rr = float(params.get("rr", 2.0))
    stop_buf_factor = float(params.get("stop_buf_factor", 0.20))
    ote_low = float(params.get("ote_low", 0.50))  # 50% = midpoint, 0.618 = classic OTE
    ote_high = float(params.get("ote_high", 0.79))
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

    pc = np.concatenate([[htf_c[0]], htf_c[:-1]])
    tr = np.maximum(htf_h - htf_l, np.maximum(np.abs(htf_h - pc), np.abs(htf_l - pc)))
    htf_atr = pd.Series(tr).ewm(alpha=1.0 / atr_period, adjust=False).mean().values
    if htf_atr[-1] <= 0:
        return None

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

    if bull_disp_idx > bear_disp_idx:
        direction = "long"
    elif bear_disp_idx > bull_disp_idx:
        direction = "short"
    else:
        return None

    avg_range_5m = np.mean(h_v[max(1, last - 50):last + 1] - l_v[max(1, last - 50):last + 1])
    if avg_range_5m <= 0:
        return None
    min_gap_5m = avg_range_5m * fvg_min_factor

    fvg_top = None
    fvg_bot = None
    for i in range(last - fvg_max_age, last - 2):
        if i < 2 or i >= last:
            continue
        if direction == "long":
            if l_v[i] > h_v[i - 2] and (l_v[i] - h_v[i - 2]) >= min_gap_5m:
                fvg_top = l_v[i]
                fvg_bot = h_v[i - 2]
                if l_v[last] <= fvg_top and c_v[last] >= fvg_bot:
                    break
                else:
                    fvg_top = None
                    fvg_bot = None
        else:
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

    zh = fvg_top - fvg_bot
    if zh <= 0:
        return None

    # OTE zone calculation. For LONG: zone is in the lower portion of the FVG
    # (deeper discount). For SHORT: zone is in the upper portion.
    if direction == "long":
        ote_zone_hi = fvg_bot + zh * ote_high
        ote_zone_lo = fvg_bot + zh * ote_low
        entry = fvg_bot + zh * ote_high  # Use upper OTE edge for tighter stop, R:R 2R
        stop = fvg_bot - max(tick, zh * stop_buf_factor)
        risk = entry - stop
        if risk <= 0.5 * tick:
            return None
        target = entry + risk * rr
        # Only fire if price is currently touching/inside the OTE zone
        cur_low = l_v[last]
        cur_high = h_v[last]
        if cur_low > ote_zone_hi or cur_high < ote_zone_lo:
            return None
        return {"ts": ts_v[last], "direction": "long",
                "entry": entry, "stop": stop, "target": target}
    else:
        ote_zone_hi = fvg_top - zh * ote_low
        ote_zone_lo = fvg_top - zh * ote_high
        entry = fvg_top - zh * ote_high  # Lower OTE edge for short
        stop = fvg_top + max(tick, zh * stop_buf_factor)
        risk = stop - entry
        if risk <= 0.5 * tick:
            return None
        target = entry - risk * rr
        cur_low = l_v[last]
        cur_high = h_v[last]
        if cur_high < ote_zone_lo or cur_low > ote_zone_hi:
            return None
        return {"ts": ts_v[last], "direction": "short",
                "entry": entry, "stop": stop, "target": target}