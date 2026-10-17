"""ICT NY AM Intra-Session Judas Swing — 5m (fresh architecture).

DIFFERENT FROM existing Judas families which use ASIAN range / PDH/PDL as
the liquidity pool. This is an INTRA-NY-AM Judas swing:
- Window: 13:30-14:30 UTC (NY 9:30-10:30 ET, the "power hour")
- The first 30 min of NY establish the NY-AM opening range (ORH/ORL)
- Look for the FIRST sweep beyond ORH or ORL that REVERSES (Judas swing)
- Enter at midpoint of the Judas swing candle, in direction of reversal
- Stop beyond Judas extreme
- Target = 2R

The ICT concept: at the open of NY, the first 30 min often forms a range
that gets "trapped" by both sides (liquidity grab from stops). The Judas
swing is the false breakout that signals the true direction.

DISTINCT FROM EXISTING:
- ict_judas_swing_asian_range_5m: uses Asian range, not NY-AM range
- bsl_run_judas_*: uses HTF (PDH/PDL)
- judas_nyam_disp_bias_*: uses displacement direction as bias
- silver_bullet_pdh_pdl_retest: uses PDH/PDL with FVG pullback
- htf_1h_disp_5m_fvg_pullback (CSID 371): uses HTF displacement

Architecture is NEW: intra-session Judas swing on the NY-AM opening range.

Timeframe: 5m native. Symbol-agnostic (tick-adjustable).
"""
import numpy as np
import pandas as pd


def evaluate(bars, params):
    if bars is None or len(bars) < 200:
        return None
    n = len(bars)
    or_minutes = int(params.get("or_minutes", 30))  # opening range window
    session_start = int(params.get("session_start", 13))  # 13:30 NY start
    session_end = int(params.get("session_end", 16))  # 16:00 NY end
    bars_per_or = max(1, or_minutes // 5)
    rr = float(params.get("rr", 2.0))
    stop_buf_atr = float(params.get("stop_buf_atr", 0.15))
    atr_period = int(params.get("atr_period", 14))
    tick = float(params.get("tick", 0.10))
    judas_min_atr = float(params.get("judas_min_atr", 0.5))  # sweep >= 0.5 ATR
    judas_max_age = int(params.get("judas_max_age", 12))  # sweep within last 12 bars

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
    if last < atr_period + bars_per_or + judas_max_age + 5:
        return None

    rngs = h_v[last - atr_period + 1:last + 1] - l_v[last - atr_period + 1:last + 1]
    atr = float(np.mean(rngs))
    if atr <= 1e-9:
        return None

    # Locate today's opening range (start of session window)
    # We scan back through bars looking for the start of the NY-AM session
    or_start = None
    for k in range(last, max(0, last - 200), -1):
        try:
            ts_k = pd.to_datetime(ts_v[k], utc=True, errors="coerce")
            if pd.isna(ts_k):
                continue
            if int(ts_k.hour) == session_start and int(ts_k.minute) == 0:
                or_start = k
                break
            # Stop scanning if we've gone past today's session
            if int(ts_k.hour) < session_start:
                break
        except Exception:
            continue

    if or_start is None or (last - or_start) < bars_per_or:
        return None

    # OR High and Low from first `bars_per_or` bars of session
    or_high = float(np.max(h_v[or_start:or_start + bars_per_or]))
    or_low = float(np.min(l_v[or_start:or_start + bars_per_or]))

    if or_high <= or_low:
        return None

    # Scan recent bars for Judas swing (false breakout of OR)
    sweep_dir = None  # 'above' or 'below'
    sweep_bar = None
    for j in range(last - 1, max(or_start + bars_per_or, last - judas_max_age), -1):
        if h_v[j] > or_high and (h_v[j] - or_high) >= judas_min_atr * atr:
            sweep_dir = "above"
            sweep_bar = j
            break
        if l_v[j] < or_low and (or_low - l_v[j]) >= judas_min_atr * atr:
            sweep_dir = "below"
            sweep_bar = j
            break

    if sweep_dir is None:
        return None

    # After sweep, we need a reversal candle that closes back inside the OR
    # and the sweep must NOT have closed beyond the OR (otherwise it's a real breakout)
    sweep_close = c_v[sweep_bar]
    if sweep_dir == "above" and sweep_close > or_high:
        return None
    if sweep_dir == "below" and sweep_close < or_low:
        return None

    # Look for entry on the next bar (or current if sweep_bar == last - 1)
    entry_bar = sweep_bar + 1
    if entry_bar > last:
        return None

    if sweep_dir == "above":
        # Sweep above then close below ORH = bearish Judas
        # ENTER SHORT at midpoint of sweep candle
        entry = (h_v[sweep_bar] + l_v[sweep_bar]) / 2.0
        # For SHORT, entry < sweep_close required (price must still be at or below midpoint)
        if c_v[last] > entry:
            return None
        stop = h_v[sweep_bar] + atr * stop_buf_atr
        risk = stop - entry
        if risk <= 0.5 * tick:
            return None
        target = entry - risk * rr
        return {"ts": ts_v[entry_bar], "direction": "short",
                "entry": entry, "stop": stop, "target": target}
    else:
        # Sweep below then close above ORL = bullish Judas
        entry = (h_v[sweep_bar] + l_v[sweep_bar]) / 2.0
        # For LONG, entry > sweep_close required
        if c_v[last] < entry:
            return None
        stop = l_v[sweep_bar] - atr * stop_buf_atr
        risk = entry - stop
        if risk <= 0.5 * tick:
            return None
        target = entry + risk * rr
        return {"ts": ts_v[entry_bar], "direction": "long",
                "entry": entry, "stop": stop, "target": target}