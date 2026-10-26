"""ICT LOCATION -> SWEEP -> MSS -> FVG -> CONFIRMATION  (honest market entry).

SOURCE
------
YT:ZdsSR4WJcKg (Skyline Trader, "ICT Trading Made Simple: A Complete Entry
Model").  Extracted rules, in the video's own order:

  1. LOCATION first.  Mark the higher-time-frame key area and the target.
     "A small pattern in the middle of nowhere is not enough."
  2. SWEEP.  On the lower time frame price takes the low of a small
     consolidation (sell-side liquidity).  A brief wick through is NOT the
     same as a decisive close below -- the video separates the two.
  3. DISPLACEMENT + MSS.  After the sweep, buyers push through the most
     recent clear lower high and CLOSE above it -> bullish market structure
     shift.
  4. FVG.  That displacement leaves a bullish fair value gap.
  5. ENTRY.  Do not chase.  Wait for the pullback INTO the new gap, then a
     green candle closes above the previous candle's high -> entry on that
     confirmation.
  6. STOP below the swept low (the invalidation point).
  7. TARGET the higher-time-frame buy-side liquidity marked in step 1; the
     R:R comes from those distances, never forced.

WHY THE ENTRY IS THE DECISION-BAR CLOSE
--------------------------------------
Findings #9126/#9133: the house backtester fills at the price the strategy
NAMES.  Every family in the book names a structural price belonging to a bar
that had already closed, so the reported PF is a fill artefact.  This file has
NO structural entry price: the entry is the close of the decision bar, so
  base fill == fair fill == decision close,
  live fill == next bar open (production, finding #9134).
Stops and targets ARE structural -- that is where the edge information lives.

Finding #9126's geometry rule is enforced explicitly: the stop must be on the
losing side of the entry, else the trade is a coin flip dressed as an edge.
"""
import numpy as np


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def _hours(bars):
    ts = bars["ts"].values
    return (ts.astype("datetime64[h]").astype("int64") % 24).astype(float)


def _chain_long(O, H, L, C, cur, a, p):
    """Return (swept_low, swing_high, gap_bot, gap_top) for a long chain."""
    lb = int(p.get("lookback", 24))
    max_age = int(p.get("max_age", 24))
    ms = float(p.get("min_sweep_atr", 0.25)) * a
    disp_k = float(p.get("disp_k", 0.8))
    body_min = float(p.get("body_ratio_min", 0.35))
    fvg_min = float(p.get("min_fvg_atr", 0.0)) * a
    mss_window = int(p.get("mss_window", 30))
    fvg_look = int(p.get("fvg_look", 3))

    for k in range(cur - 1, max(1, cur - max_age), -1):
        if k - lb < 1:
            break
        prior_low = float(np.min(L[k - lb:k]))
        if not (float(L[k]) < prior_low - ms):
            continue                       # no decisive sweep of the low
        if not (float(C[k]) > prior_low):
            continue                       # must reclaim the level
        swept_low = min(float(L[k]), prior_low)
        swing_high = float(np.max(H[k - lb:k]))

        m = -1
        for j in range(k + 1, min(cur, k + mss_window)):
            if float(C[j]) > swing_high:
                m = j
                break
        if m < 0:
            continue                       # no market structure shift
        rng_m = float(H[m] - L[m])
        body_m = abs(float(C[m] - O[m]))
        if rng_m < disp_k * a or rng_m <= 0 or body_m / rng_m < body_min:
            continue                       # not a displacement bar

        gap = None
        lo_g = max(k + 1, m - fvg_look)
        for g in range(lo_g, min(m + 1, cur)):
            if g - 2 < 1:
                continue
            if float(L[g]) > float(H[g - 2]):
                gap = (float(H[g - 2]), float(L[g]))
                break
        if gap is None:
            continue                       # no fair value gap left behind
        gap_bot, gap_top = gap
        if (gap_top - gap_bot) < fvg_min:
            continue

        touched = False
        for q in range(lo_g + 1, cur):
            if float(L[q]) <= gap_top and float(H[q]) >= gap_bot:
                touched = True
                break
        if not touched:
            continue                       # no pullback into the gap yet
        return swept_low, swing_high, gap_bot, gap_top
    return None


def _chain_short(O, H, L, C, cur, a, p):
    lb = int(p.get("lookback", 24))
    max_age = int(p.get("max_age", 24))
    ms = float(p.get("min_sweep_atr", 0.25)) * a
    disp_k = float(p.get("disp_k", 0.8))
    body_min = float(p.get("body_ratio_min", 0.35))
    fvg_min = float(p.get("min_fvg_atr", 0.0)) * a
    mss_window = int(p.get("mss_window", 30))
    fvg_look = int(p.get("fvg_look", 3))

    for k in range(cur - 1, max(1, cur - max_age), -1):
        if k - lb < 1:
            break
        prior_high = float(np.max(H[k - lb:k]))
        if not (float(H[k]) > prior_high + ms):
            continue
        if not (float(C[k]) < prior_high):
            continue
        swept_high = max(float(H[k]), prior_high)
        swing_low = float(np.min(L[k - lb:k]))

        m = -1
        for j in range(k + 1, min(cur, k + mss_window)):
            if float(C[j]) < swing_low:
                m = j
                break
        if m < 0:
            continue
        rng_m = float(H[m] - L[m])
        body_m = abs(float(C[m] - O[m]))
        if rng_m < disp_k * a or rng_m <= 0 or body_m / rng_m < body_min:
            continue

        gap = None
        lo_g = max(k + 1, m - fvg_look)
        for g in range(lo_g, min(m + 1, cur)):
            if g - 2 < 1:
                continue
            if float(H[g]) < float(L[g - 2]):
                gap = (float(H[g]), float(L[g - 2]))
                break
        if gap is None:
            continue
        gap_bot, gap_top = gap
        if (gap_top - gap_bot) < fvg_min:
            continue

        touched = False
        for q in range(lo_g + 1, cur):
            if float(H[q]) >= gap_bot and float(L[q]) <= gap_top:
                touched = True
                break
        if not touched:
            continue
        return swept_high, swing_low, gap_bot, gap_top
    return None


def evaluate(bars, params):
    p = dict(params or {})
    n = len(bars)
    if n < 90:
        return None
    cur = n - 1

    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    a = _atr(H, L, C, int(p.get("atr_period", 14)))
    if not np.isfinite(a) or a <= 0:
        return None

    buf = float(p.get("stop_buf_atr", 0.15)) * a
    rr = float(p.get("rr", 2.0))
    htf_lb = int(p.get("htf_lb", 0))
    confirm_mode = str(p.get("confirm", "prev_high"))
    only_long = bool(p.get("only_long", False))
    only_short = bool(p.get("only_short", False))

    s_utc = p.get("session_start_utc")
    if s_utc is not None:
        hh = float(_hours(bars)[cur])
        if not (float(s_utc) <= hh < float(p.get("session_end_utc", 24))):
            return None

    ocur, hcur, lcur, ccur = O[cur], H[cur], L[cur], C[cur]
    direction = None
    stop = None
    target_level = None

    if not only_short:
        ch = _chain_long(O, H, L, C, cur, a, p)
        if ch is not None:
            swept_low, _sw_high, gap_bot, gap_top = ch
            ok = ccur > gap_bot and ccur > ocur
            if confirm_mode == "prev_high":
                ok = ok and ccur > float(H[cur - 1])
            else:
                ok = ok and ccur > gap_top
            if ok:
                direction = "long"
                stop = swept_low - buf
                if htf_lb > 0:
                    hi = float(np.max(H[max(0, cur - htf_lb):cur + 1]))
                    if hi > ccur:
                        target_level = hi

    if direction is None and not only_long:
        ch = _chain_short(O, H, L, C, cur, a, p)
        if ch is not None:
            swept_high, _sw_low, gap_bot, gap_top = ch
            ok = ccur < gap_top and ccur < ocur
            if confirm_mode == "prev_high":
                ok = ok and ccur < float(L[cur - 1])
            else:
                ok = ok and ccur < gap_bot
            if ok:
                direction = "short"
                stop = swept_high + buf
                if htf_lb > 0:
                    lo = float(np.min(L[max(0, cur - htf_lb):cur + 1]))
                    if lo < ccur:
                        target_level = lo

    if direction is None or stop is None:
        return None

    entry = float(ccur)                    # HONEST: decision-bar close
    sign = 1.0 if direction == "long" else -1.0
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if sign * (stop - entry) >= 0:         # geometry guard: stop on the
        return None                        # LOSING side of the entry

    if target_level is not None:
        target = float(target_level)
        if sign * (target - entry) / risk < float(p.get("min_rr_liquidity", 0.0)):
            target = entry + sign * rr * risk
    else:
        target = entry + sign * rr * risk
    if sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
