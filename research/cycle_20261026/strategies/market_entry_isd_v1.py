"""MARKET-ENTRY ISD (INDUCEMENT -> SWEEP -> DISPLACEMENT) — 2026-10-26 cycle.

Source: YT:jmlDR7nop-8 (TradeWithAM, "Inducement -> Sweep -> Displacement
Explained (ICT Trading Model)"), finding 79b56fae.

THE RULE SET AS EXTRACTED FROM THE VIDEO
----------------------------------------
1. INDUCEMENT (the bait).  Price forms a MINOR swing high/low (or a double
   top/bottom) that looks like support/resistance.  Retail puts stops just
   beyond it and limit orders at it, creating a pool of liquidity.
2. SWEEP.  Price TAKES OUT that inducement level and closes back inside
   (a stop run above the double top / below the swing low).
3. DISPLACEMENT.  Price reverses violently in the ORIGINAL direction on
   strong candles; often leaves a 3-candle FVG (gap between the high of
   candle 1 and the low of candle 3).
4. ENTRY.  Do NOT chase the displacement: wait for price to RETRACE back
   into the FVG and then enter on that retrace.
5. STOP.  Beyond the SWEEP extreme (below the sweep low for a long).
6. TARGETS.  TP1 the previous swing high/low; TP2 the next higher-timeframe
   liquidity level.  (Here: a fixed R multiple, see `rr`.)

WHY EVERY ENTRY HERE IS THE DECISION BAR'S CLOSE
------------------------------------------------
Findings #9126/#9133/#9139/#9146: the house backtester FILLS AT THE PRICE
THE STRATEGY NAMES.  A "retrace into the FVG" entry names a price inside a
gap that belongs to already-closed bars -- that is a retro-fill, and the
stamp's PF is then a number the broker can never pay.  So the entry here is
the CLOSE OF THE DECISION BAR (the price we can pay right after the
decision), the stop is beyond the sweep extreme and the target is R-based:

    house fill == decision close == fair fill
    live  fill == next bar open (production MARKET order)

Stops/targets stay structural -- that is where the edge information lives;
only the ENTRY is honest.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

TAIL = 600          # bars of history the signal actually needs
DEFAULTS = {
    "setup": "isd",          # "isd" (FVG retrace) | "isd_nofvg" (body retrace)
    "pivot_p": 3,            # bars each side of the inducement pivot
    "ind_bars": 60,         # how far back the inducement may sit
    "pullback_atr": 0.25,   # the level must have been pulled back from
    "min_sweep_atr": 0.30,  # sweep penetration, in ATR
    "disp_k": 1.0,           # displacement range, in ATR
    "body_ratio_min": 0.55,  # displacement body ratio
    "retrace": 0.5,         # retrace depth for the no-FVG variant
    "rr": 1.5,              # target in R
    "stop_buf_atr": 0.15,    # stop buffer beyond the sweep extreme
    "atr_period": 14,
}


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def _body_ratio(O, H, L, C, i):
    rng = max(float(H[i] - L[i]), 1e-12)
    return abs(float(C[i] - O[i])) / rng


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:  # noqa: BLE001
        pass

    setup = str(p["setup"])
    pivot_p = int(p["pivot_p"])
    ind_bars = int(p["ind_bars"])
    pull_atr = float(p["pullback_atr"])
    ms = float(p["min_sweep_atr"])
    disp_k = float(p["disp_k"])
    min_body = float(p["body_ratio_min"])
    retrace = float(p["retrace"])
    rr = float(p["rr"])
    buf = float(p["stop_buf_atr"])
    atr_period = int(p["atr_period"])

    need = max(60, ind_bars + pivot_p + 4)
    bars = bars.tail(TAIL) if len(bars) > TAIL else bars
    n = len(bars)
    if n < need:
        return None
    cur = n - 1

    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    # ---- 1) inducement: the most recent PIVOT high / low ---------------
    # A pivot high at j: H[j] is the highest of its window AND price pulled
    # back from it afterwards (so it reads as a level, not a spike).
    ind_hi = -1        # index of the pivot high
    ind_lo = -1        # index of the pivot low
    j_hi = j_lo = -1
    lo_idx = max(pivot_p + 1, cur - ind_bars)
    for j in range(cur - 1, lo_idx - 1, -1):
        w_hi = float(np.max(H[j - pivot_p:j + pivot_p + 1]))
        w_lo = float(np.min(L[j - pivot_p:j + pivot_p + 1]))
        after = slice(j + pivot_p + 1, cur + 1)
        if ind_hi < 0 and H[j] >= w_hi and len(L[after]) > 0:
            if float(np.min(L[after])) <= H[j] - pull_atr * a:
                ind_hi, j_hi = float(H[j]), j
        if ind_lo < 0 and L[j] <= w_lo and len(H[after]) > 0:
            if float(np.max(H[after])) >= L[j] + pull_atr * a:
                ind_lo, j_lo = float(L[j]), j
        if ind_hi >= 0 and ind_lo >= 0:
            break
    if ind_hi < 0 and ind_lo < 0:
        return None

    # ---- 2) sweep of the inducement, then 3) displacement --------------
    direction = None
    stop = None
    disp_i = -1
    sweep_i = -1

    if ind_hi >= 0:
        # bearish: price sweeps ABOVE the inducement high, then displaces down
        for k in range(max(j_hi + pivot_p + 1, 1), cur):
            if H[k] > ind_hi + ms * a and C[k] < ind_hi:
                for d in range(k + 1, cur):
                    if (_body_ratio(O, H, L, C, d) >= min_body
                            and (H[d] - L[d]) >= disp_k * a and C[d] < ind_hi):
                        direction, stop = "short", float(H[k]) + buf * a
                        disp_i, sweep_i = d, k
                        break
                if disp_i >= 0:
                    break

    if disp_i < 0 and ind_lo >= 0:
        # bullish: price sweeps BELOW the inducement low, then displaces up
        for k in range(max(j_lo + pivot_p + 1, 1), cur):
            if L[k] < ind_lo - ms * a and C[k] > ind_lo:
                for d in range(k + 1, cur):
                    if (_body_ratio(O, H, L, C, d) >= min_body
                            and (H[d] - L[d]) >= disp_k * a and C[d] > ind_lo):
                        direction, stop = "long", float(L[k]) - buf * a
                        disp_i, sweep_i = d, k
                        break
                if disp_i >= 0:
                    break

    if direction is None or disp_i < 0:
        return None

    # ---- 4) entry trigger: retrace into the displacement's FVG ---------
    entry = float(C[cur])          # HONEST: the decision bar's close
    if setup == "isd":
        if direction == "long":
            # bullish FVG: low of bar d+2 > high of bar d
            fvg = -1
            for d2 in range(disp_i + 2, cur + 1):
                if L[d2] > H[disp_i]:
                    fvg = d2
                    break
            if fvg < 0:
                return None
            zlo, zhi = float(H[disp_i]), float(L[fvg])
            if not (L[cur] <= zhi and H[cur] >= zlo and C[cur] > zlo):
                return None
        else:
            fvg = -1
            for d2 in range(disp_i + 2, cur + 1):
                if H[d2] < L[disp_i]:
                    fvg = d2
                    break
            if fvg < 0:
                return None
            zlo, zhi = float(H[fvg]), float(L[disp_i])
            if not (H[cur] >= zlo and L[cur] <= zhi and C[cur] < zhi):
                return None
    else:
        # isd_nofvg: retrace into the displacement BODY and hold
        if direction == "long":
            body_hi = float(max(O[disp_i], C[disp_i]))
            body_lo = float(min(O[disp_i], C[disp_i]))
            level = C[disp_i] - retrace * (body_hi - body_lo)
            hit = -1
            for q in range(disp_i + 1, cur + 1):
                if L[q] <= level and C[q] > level:
                    hit = q
                    break
            if hit < 0 or C[cur] <= level:
                return None
        else:
            body_hi = float(max(O[disp_i], C[disp_i]))
            body_lo = float(min(O[disp_i], C[disp_i]))
            level = C[disp_i] + retrace * (body_hi - body_lo)
            hit = -1
            for q in range(disp_i + 1, cur + 1):
                if H[q] >= level and C[q] < level:
                    hit = q
                    break
            if hit < 0 or C[cur] >= level:
                return None

    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk

    # GEOMETRY GUARD: stop on the losing side, target on the winning side.
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
