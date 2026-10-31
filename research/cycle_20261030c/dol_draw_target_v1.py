"""DRAW ON LIQUIDITY (DOL) -- prior-session side read + Asia-range stop run
+ displacement FVG retrace, with the DRAW itself as the profit target.

Source: FundedEnigma, "How to Find the Draw on Liquidity Like a Pro (ICT DOL
Explained)" (YT ksYP7NpMP9U).

EXTRACTED RULES
  R1  On 16y of daily NQ futures only ~1 session in 8 stayed inside the prior
      session; ~7 of 8 took the prior session's HIGH or LOW.  Those are the
      liquidity pools.
  R2  SIDE (trend read): in a bullish rhythm price keeps taking the prior
      session's HIGH; in a bearish rhythm the prior session's LOW.
  R3  CLOSE BEYOND (displacement read): if the prior session ran the
      session-before high and CLOSED STRONGLY ABOVE it -> more upside, the
      draw stays on the HIGH side.  If it ran the high and CLOSED BACK BELOW
      it -> FAILURE, the draw flips to the LOW side.  (Mirror for lows.)
  R4  "If the draw is already hit in the first hours of Asia the job for that
      day is done" -> skip when the draw was already taken earlier today.
  R5  EXECUTION (5m): Asia builds a range; price pokes beyond it after Asia
      (the stop run); then DISPLACES in the opposite direction leaving a 5m
      FVG; price RETRACES INTO the FVG and holds -> entry; the objective is
      delivery INTO the draw.

TWO WORKED EXAMPLES FROM THE VIDEO (NQ, EST)
  bear (Nov 13 2025): Asia range -> 01:44 poke ABOVE the Asia high (buy
      stops) -> 04:00 displacement DOWN leaving a 5m FVG -> 05:30 retrace into
      the gap and roll over -> 09:00 yesterday's LOW taken (the draw).
  bull (Apr 9 2026): 03:42 run under the Asia low, 04:15 low of the day ->
      11:10 displacement UP leaving a 5m FVG -> dip back into it and hold ->
      12:03 yesterday's HIGH taken (the draw).

DESIGN NOTES (production-fill safe)
  The decision is taken on the close of the signal bar and the runtime fills
  at the OPEN of the next bar, so nothing here may depend on the exact
  derived entry price.  Stop and target are anchored to STRUCTURE (the FVG,
  the draw level) and the R filter uses the signal-bar close only as a proxy
  for the achievable fill.
"""

import numpy as np

TAIL = 1600

DEFAULTS = {
    "atr_period": 14,
    "disp_k": 1.0,          # displacement bar range in ATR
    "body_ratio_min": 0.55,  # displacement bar body / range
    "sweep_atr": 0.20,      # how far beyond the Asia range the stop run goes
    "fvg_min_atr": 0.05,    # min FVG height in ATR
    "sweep_win": 144,       # bars before the displacement to look for the run
    "disp_age": 48,         # max bars back to search for the displacement
    "asia_start_h": 0,      # UTC hour, Asia range start
    "asia_end_h": 6,        # UTC hour, Asia range end (exclusive)
    "entry_start_h": 6,     # UTC hour, earliest entry (London open)
    "entry_end_h": 20,      # UTC hour, latest entry
    "min_rr": 1.5,          # min distance to the draw in R
    "max_rr": 8.0,          # max distance to the draw in R
    "stop_buf_atr": 0.15,   # stop buffer beyond the structural level
    "close_zone": 0.25,     # "closes strongly beyond" = close in top/bottom X% of range
    "stop_ref": "fvg",      # "fvg" | "sweep"
}


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def _sess(high, low, close, day, d):
    """Return (high, low, last_close) of session `d`, or None."""
    idx = np.where(day == d)[0]
    if len(idx) == 0:
        return None
    return float(high[idx].max()), float(low[idx].min()), float(close[idx[-1]])


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS and v is not None})
    except Exception:
        pass

    atr_period = int(p["atr_period"])
    disp_k = float(p["disp_k"])
    body_min = float(p["body_ratio_min"])
    sweep_atr = float(p["sweep_atr"])
    fvg_min_atr = float(p["fvg_min_atr"])
    sweep_win = int(p["sweep_win"])
    disp_age = int(p["disp_age"])
    asia_s = int(p["asia_start_h"])
    asia_e = int(p["asia_end_h"])
    ent_s = int(p["entry_start_h"])
    ent_e = int(p["entry_end_h"])
    min_rr = float(p["min_rr"])
    max_rr = float(p["max_rr"])
    buf = float(p["stop_buf_atr"])
    close_zone = float(p["close_zone"])
    stop_ref = str(p["stop_ref"]).lower()

    try:
        # TAIL FIRST, then filter.  Filtering the full slice is O(i) per bar
        # and makes the whole replay O(n^2) -- it timed the engine out at 300s
        # on 90 days of 5m.  The tail is O(TAIL) and the decision bar is the
        # last row of the tail, so the staleness stamp still lines up.
        t = bars.tail(TAIL)
        valid = ((t["high"] - t["low"]) > 0) & (t["volume"] > 0)
        if not bool(valid.iloc[-1]):
            return None
        b = t[valid]
    except Exception:
        return None
    if len(b) < 300:
        return None
    n = len(b)
    cur = n - 1
    if cur < 120:
        return None

    H = b["high"].to_numpy(float)
    L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float)
    O = b["open"].to_numpy(float)

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    # UTC calendar without pandas .dt accessors (fast in the replay loop).
    #
    # NOTE 1: pandas 3.x resolves datetimes in MICROSECONDS, so
    # ``Series.astype("int64")`` is NOT nanoseconds -- dividing by 1e9
    # silently collapses a 10-day 5m sample into ONE session and every
    # session-scoped rule dies.
    # NOTE 2: numpy's ``datetime64 -> datetime64[D]`` cast costs ~2ms per
    # call and was 35s of a 54s replay.  Reading the raw int64 and scaling by
    # the array's own unit is ~1000x cheaper.
    try:
        tarr = b["ts"].to_numpy()
        if getattr(tarr, "dtype", None) is None or tarr.dtype.kind != "M":
            return None
        unit = np.datetime_data(tarr.dtype)[0]
        scale = {"ns": 1, "us": 1000, "ms": 1000000, "s": 1000000000}.get(unit)
        if scale is None:
            return None
        secs = tarr.astype("int64") // scale
        day = secs // 86400
        hour = (secs % 86400) // 3600
    except Exception:
        return None

    # ---- session bookkeeping -------------------------------------------
    last_day = day[cur]
    prev_days = np.unique(day[day < last_day])
    if len(prev_days) < 2:
        return None
    pd1 = int(prev_days[-1])   # last COMPLETED session
    pd2 = int(prev_days[-2])   # the one before it

    s1 = _sess(H, L, C, day, pd1)
    s2 = _sess(H, L, C, day, pd2)
    if s1 is None or s2 is None:
        return None
    p1h, p1l, p1c = s1
    p2h, p2l, p2c = s2
    p1_rng = max(p1h - p1l, 1e-9)

    # ---- R2/R3: pick the side of the draw -------------------------------
    # side "high" -> the draw is the prior session's HIGH (expect upside)
    # side "low"  -> the draw is the prior session's LOW  (expect downside)
    side = None
    took_up = p1h > p2h
    took_dn = p1l < p2l
    close_strong_up = (p1c > p2h) and (p1c >= p1h - close_zone * p1_rng)
    close_strong_dn = (p1c < p2l) and (p1c <= p1l + close_zone * p1_rng)
    if took_up and close_strong_up:
        side = "high"
    elif took_up and p1c < p2h:
        side = "low"        # failure up -> the draw flips down
    elif took_dn and close_strong_dn:
        side = "low"
    elif took_dn and p1c > p2l:
        side = "high"       # failure down -> the draw flips up
    if side is None:
        return None

    long_side = (side == "high")
    draw = p1h if long_side else p1l
    direction = "long" if long_side else "short"

    # ---- entry window ---------------------------------------------------
    if not (ent_s <= hour[cur] < ent_e):
        return None

    tod = (day == last_day)
    tod_idx = np.where(tod)[0]
    if len(tod_idx) == 0:
        return None

    # ---- R4: the draw must still be live --------------------------------
    if long_side:
        if float(H[tod_idx].max()) >= draw:
            return None
    else:
        if float(L[tod_idx].min()) <= draw:
            return None

    # ---- Asia range of the current session ------------------------------
    asia = tod & (hour >= asia_s) & (hour < asia_e)
    asia_idx = np.where(asia)[0]
    if len(asia_idx) < 4:
        return None
    asia_hi = float(H[asia_idx].max())
    asia_lo = float(L[asia_idx].min())
    # the stop run happens AFTER the Asia window closes
    post_asia = np.where(tod & (hour >= asia_e))[0]
    if len(post_asia) == 0:
        return None
    pa_start = int(post_asia[0])

    # ---- hunt for the stop run + displacement + FVG retrace --------------
    lo_d = max(3, cur - disp_age)
    for d in range(cur, lo_d - 1, -1):
        if d <= pa_start:
            break
        rng = float(H[d] - L[d])
        if rng < disp_k * a:
            continue
        body = abs(float(C[d] - O[d]))
        if body < body_min * rng:
            continue
        if long_side:
            if not (float(L[d]) > float(H[d - 2])):
                continue
            zone_bot = float(H[d - 2])
            zone_top = float(L[d])
        else:
            if not (float(H[d]) < float(L[d - 2])):
                continue
            zone_bot = float(H[d])
            zone_top = float(L[d - 2])
        if (zone_top - zone_bot) < fvg_min_atr * a:
            continue

        # the signal bar must have dipped INTO the gap and held it
        if long_side:
            if not (float(L[cur]) <= zone_top and float(L[cur]) >= zone_bot
                    and float(C[cur]) >= zone_bot):
                continue
            seg = L[d + 1:cur]
            if len(seg) and float(seg.min()) <= zone_bot:
                continue
        else:
            if not (float(H[cur]) >= zone_bot and float(H[cur]) <= zone_top
                    and float(C[cur]) <= zone_top):
                continue
            seg = H[d + 1:cur]
            if len(seg) and float(seg.max()) >= zone_top:
                continue

        # the stop run: price poked beyond the Asia range BEFORE the displacement
        w0 = max(pa_start, d - sweep_win)
        if long_side:
            sweep_low = float(L[w0:d].min())
            if not (sweep_low <= asia_lo - sweep_atr * a):
                continue
            struct_stop = sweep_low if stop_ref == "sweep" else zone_bot
            stop = struct_stop - buf * a
            entry_ref = float(C[cur])
            risk = entry_ref - stop
            if risk <= 0:
                continue
            dist = draw - entry_ref
        else:
            sweep_high = float(H[w0:d].max())
            if not (sweep_high >= asia_hi + sweep_atr * a):
                continue
            struct_stop = sweep_high if stop_ref == "sweep" else zone_top
            stop = struct_stop + buf * a
            entry_ref = float(C[cur])
            risk = stop - entry_ref
            if risk <= 0:
                continue
            dist = entry_ref - draw

        if dist <= 0:
            continue
        rr = dist / risk
        if rr < min_rr or rr > max_rr:
            continue

        return {"direction": direction, "entry": entry_ref,
                "stop": float(stop), "target": float(draw),
                "ts": b["ts"].iloc[cur]}

    return None
