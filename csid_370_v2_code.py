"""Zen Trades 4-step Liquidity Sweep Model — 5m (CSID candidate) v2.

FIX v2: Sweep direction was WRONG in v1. Zen Trades model requires the sweep to be OPPOSITE
the trade direction (it confirms the reversal intent):
  - LONG: sweep a swing LOW (sell-side liquidity raid) + tap a bullish HTF FVG (delivery into
    discount zone from below) + bullish 5m iFVG inversion.
  - SHORT: sweep a swing HIGH (buy-side liquidity raid) + tap a bearish HTF FVG (delivery
    into premium zone from above) + bearish 5m iFVG inversion.

v1 had this reversed, leading to 0 wins on MGC 120d 5m (PF=0.0 n=194 all losses).

Step 1: HTF liquidity sweep detected (5m swing high/low broken then closed back through).
Step 2: HTF FVG tap — current 5m bar is inside an unfilled 15m FVG.
Step 3: LTF iFVG inversion — a recent 5m displacement FVG was just closed through (iFVG).
Step 4: Enter on iFVG close, stop beyond swept swing, target at 2R.

Architecture contrasts with the existing ifvg_midpoint_reversion family:
  - Midpoint reversion ENTERS at midpoint after re-poke (mean-reversion bias).
  - Zen model ENTERS on the close that inverts the FVG (continuation bias).
  - Adds HTF FVG tap filter (Zen step 2).
  - Adds HTF sweep filter (Zen step 1) — opposite the trade direction.

Timeframe-native: 5m bars are native; 15m is resampled inside.
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None
    n = len(bars)
    htf_bars_per_ltf = int(params.get("htf_bars_per_ltf", 3))
    htf_lookback = int(params.get("htf_lookback", 24))
    ltf_lookback = int(params.get("ltf_lookback", 24))
    sweep_lookback = int(params.get("sweep_lookback", 36))
    min_gap_factor = float(params.get("min_gap_factor", 0.10))
    rr = float(params.get("rr", 2.0))
    stop_buf_factor = float(params.get("stop_buf_factor", 0.20))
    session_start = int(params.get("session_start", 8))
    session_end = int(params.get("session_end", 16))
    tick = float(params.get("tick", 0.10))

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
    if last < 20:
        return None

    htf_n = n // htf_bars_per_ltf
    if htf_n < 8:
        return None
    htf_h = np.empty(htf_n)
    htf_l = np.empty(htf_n)
    for k in range(htf_n):
        s = k * htf_bars_per_ltf
        e = s + htf_bars_per_ltf
        htf_h[k] = h_v[s:e].max()
        htf_l[k] = l_v[s:e].min()
    htf_fvgs_bull = []
    htf_fvgs_bear = []
    for i in range(2, htf_n):
        if htf_l[i] > htf_h[i - 2]:
            htf_fvgs_bull.append((htf_h[i - 2], htf_l[i]))
        if htf_h[i] < htf_l[i - 2]:
            htf_fvgs_bear.append((htf_h[i], htf_l[i - 2]))
    if len(htf_fvgs_bull) > htf_lookback:
        htf_fvgs_bull = htf_fvgs_bull[-htf_lookback:]
    if len(htf_fvgs_bear) > htf_lookback:
        htf_fvgs_bear = htf_fvgs_bear[-htf_lookback:]

    cur_close = c_v[last]
    cur_high = h_v[last]
    cur_low = l_v[last]

    in_bull_htf_fvg = any(lo <= cur_close <= hi or lo <= cur_low <= hi
                          for (lo, hi) in htf_fvgs_bull)
    in_bear_htf_fvg = any(lo <= cur_close <= hi or lo <= cur_high <= hi
                          for (lo, hi) in htf_fvgs_bear)
    htf_tap_required = bool(params.get("htf_tap_required", True))
    if htf_tap_required and not (in_bull_htf_fvg or in_bear_htf_fvg):
        return None

    piv = 3
    swing_highs = []
    swing_lows = []
    for j in range(piv, last - piv):
        if h_v[j] == max(h_v[j - piv:j + piv + 1]) and h_v[j] > h_v[j - piv] and h_v[j] > h_v[j + piv]:
            swing_highs.append((j, h_v[j]))
        if l_v[j] == min(l_v[j - piv:j + piv + 1]) and l_v[j] < l_v[j - piv] and l_v[j] < l_v[j + piv]:
            swing_lows.append((j, l_v[j]))
    if not swing_highs or not swing_lows:
        return None

    swept_high = None
    swept_low = None
    for (j, val) in swing_highs:
        if j > last - sweep_lookback:
            continue
        for k in range(j + 1, last + 1):
            if h_v[k] > val:
                if c_v[last] < val:
                    swept_high = (j, val, k)
                    break
        if swept_high:
            break
    for (j, val) in swing_lows:
        if j > last - sweep_lookback:
            continue
        for k in range(j + 1, last + 1):
            if l_v[k] < val:
                if c_v[last] > val:
                    swept_low = (j, val, k)
                    break
        if swept_low:
            break

    avg_range = np.mean(h_v[max(1,last-ltf_lookback*2):last+1] - l_v[max(1,last-ltf_lookback*2):last+1])
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    bullish_ifvg = None
    bearish_ifvg = None
    for i in range(last - ltf_lookback, last - 1):
        if i < 2 or i >= last:
            continue
        if l_v[i] > h_v[i - 2] and (l_v[i] - h_v[i - 2]) >= min_gap:
            gap_low = h_v[i - 2]
            gap_high = l_v[i]
            for k in range(i + 1, last + 1):
                if c_v[k] >= gap_high:
                    bullish_ifvg = (gap_low, gap_high, i, k)
                    break
            if bullish_ifvg:
                break
        if h_v[i] < l_v[i - 2] and (l_v[i - 2] - h_v[i]) >= min_gap:
            gap_low = h_v[i]
            gap_high = l_v[i - 2]
            for k in range(i + 1, last + 1):
                if c_v[k] <= gap_low:
                    bearish_ifvg = (gap_low, gap_high, i, k)
                    break
            if bearish_ifvg:
                break

    if bullish_ifvg is None and bearish_ifvg is None:
        return None

    # FIXED v2: sweep is OPPOSITE trade direction (confirms reversal intent)
    # LONG: swept LOW + tap bull HTF FVG + bullish iFVG
    if bullish_ifvg is not None and swept_low is not None and in_bull_htf_fvg:
        gap_low, gap_high, fi, ki = bullish_ifvg
        entry = c_v[last]
        zh = gap_high - gap_low
        stop = min(swept_low[1], l_v[last]) - max(tick, zh * stop_buf_factor)
        risk = entry - stop
        if risk <= 0.5 * tick:
            return None
        target = entry + risk * rr
        return {"ts": ts_v[last], "direction": "long", "entry": entry, "stop": stop, "target": target}

    # SHORT: swept HIGH + tap bear HTF FVG + bearish iFVG
    if bearish_ifvg is not None and swept_high is not None and in_bear_htf_fvg:
        gap_low, gap_high, fi, ki = bearish_ifvg
        entry = c_v[last]
        zh = gap_high - gap_low
        stop = max(swept_high[1], h_v[last]) + max(tick, zh * stop_buf_factor)
        risk = stop - entry
        if risk <= 0.5 * tick:
            return None
        target = entry - risk * rr
        return {"ts": ts_v[last], "direction": "short", "entry": entry, "stop": stop, "target": target}

    return None