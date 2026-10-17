"""Zen Trades — sweep + iFVG inversion ONLY (v3 — test base signal).

Removed: HTF FVG tap filter (was generating too few signals).
Required: iFVG inversion MUST occur on the current bar (last bar in dataset).

Step 1: Liquidity sweep — swing high (low) recently broken then closed back.
Step 2: 5m iFVG inversion — a displacement FVG was just closed through on THIS bar.
Step 3: Enter on close (current bar = inversion bar), stop beyond swept swing, target 2R.

This is the simplest interpretation of the Zen Trades model without the HTF FVG filter.
Tests whether the sweep + immediate iFVG inversion combo has edge.
"""
import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None
    n = len(bars)
    sweep_lookback = int(params.get("sweep_lookback", 36))
    min_gap_factor = float(params.get("min_gap_factor", 0.10))
    rr = float(params.get("rr", 2.0))
    stop_buf_factor = float(params.get("stop_buf_factor", 0.20))
    session_start = int(params.get("session_start", 0))
    session_end = int(params.get("session_end", 24))
    tick = float(params.get("tick", 0.10))
    require_ifvg_on_last = bool(params.get("require_ifvg_on_last", True))

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

    # pivot detection
    piv = 3
    swing_highs = []
    swing_lows = []
    for j in range(piv, last - piv):
        if h_v[j] == max(h_v[j - piv:j + piv + 1]) and h_v[j] > h_v[j - piv] and h_v[j] > h_v[j + piv]:
            swing_highs.append((j, h_v[j]))
        if l_v[j] == min(l_v[j - piv:j + piv + 1]) and l_v[j] < l_v[j - piv] and l_v[j] < l_v[j + piv]:
            swing_lows.append((j, l_v[j]))

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

    # displacement FVG detection
    avg_range = np.mean(h_v[max(1, last-50):last+1] - l_v[max(1, last-50):last+1])
    if avg_range <= 0:
        return None
    min_gap = avg_range * min_gap_factor

    # check: was there a displacement FVG in the last 6 bars, and the current bar closed through it?
    bullish_ifvg = None
    bearish_ifvg = None
    look = 6 if require_ifvg_on_last else 24
    for i in range(last - look, last - 1):
        if i < 2 or i >= last:
            continue
        # bullish displacement FVG: bar i low > bar i-2 high (gap up)
        if l_v[i] > h_v[i - 2] and (l_v[i] - h_v[i - 2]) >= min_gap:
            gap_low = h_v[i - 2]
            gap_high = l_v[i]
            if require_ifvg_on_last:
                if c_v[last] >= gap_high:
                    bullish_ifvg = (gap_low, gap_high, i)
                    break
            else:
                for k in range(i + 1, last + 1):
                    if c_v[k] >= gap_high:
                        bullish_ifvg = (gap_low, gap_high, i)
                        break
                if bullish_ifvg:
                    break
        if h_v[i] < l_v[i - 2] and (l_v[i - 2] - h_v[i]) >= min_gap:
            gap_low = h_v[i]
            gap_high = l_v[i - 2]
            if require_ifvg_on_last:
                if c_v[last] <= gap_low:
                    bearish_ifvg = (gap_low, gap_high, i)
                    break
            else:
                for k in range(i + 1, last + 1):
                    if c_v[k] <= gap_low:
                        bearish_ifvg = (gap_low, gap_high, i)
                        break
                if bearish_ifvg:
                    break

    if bullish_ifvg is None and bearish_ifvg is None:
        return None

    cur_close = c_v[last]
    cur_high = h_v[last]
    cur_low = l_v[last]

    # LONG: swept LOW + bullish iFVG
    if bullish_ifvg is not None and swept_low is not None:
        gap_low, gap_high, fi = bullish_ifvg
        zh = gap_high - gap_low
        stop = min(swept_low[1], cur_low) - max(tick, zh * stop_buf_factor)
        risk = cur_close - stop
        if risk <= 0.5 * tick:
            return None
        target = cur_close + risk * rr
        return {"ts": ts_v[last], "direction": "long", "entry": cur_close, "stop": stop, "target": target}

    # SHORT: swept HIGH + bearish iFVG
    if bearish_ifvg is not None and swept_high is not None:
        gap_low, gap_high, fi = bearish_ifvg
        zh = gap_high - gap_low
        stop = max(swept_high[1], cur_high) + max(tick, zh * stop_buf_factor)
        risk = stop - cur_close
        if risk <= 0.5 * tick:
            return None
        target = cur_close - risk * rr
        return {"ts": ts_v[last], "direction": "short", "entry": cur_close, "stop": stop, "target": target}

    return None