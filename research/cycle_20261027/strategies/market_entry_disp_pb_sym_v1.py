"""MARKET-ENTRY DISPLACEMENT -> PULLBACK CONTINUATION (SYMMETRIC, long+short).

WHY THIS FILE
-------------
`research/cycle_20261025/strategies/market_entry_ict_v1.py` proved (finding
#9145) that the ONLY fill the broker can pay is the market order placed after
the decision bar closes (finding #9134: there is no LIMIT entry path in this
repo).  Its `disp_pullback` branch is LONG-ONLY -- the short mirror was never
written -- so the MCL 15m survivor found on 2026-10-27 (PF 1.38-1.41 live)
was measured on ONE side of the market only.  Halving the sample for no
reason is a waste of the one honest book we have.

This file is the symmetric version:
    * long  : bullish displacement -> shallow pullback that HOLDS -> long
    * short : bearish displacement -> shallow pullback that HOLDS -> short

Both sides use the SAME decision-bar-close entry, so `base == fair == live`
(house engine, market-on-close, and production market-on-next-open agree).
Stops and targets stay structural: the stop sits on the losing side of the
entry and the target on the winning side (the geometry guard that was
inverted on 2026-10-25 and fixed in finding #9145).

FILTERS (all optional, all decided on CLOSED data):
    ema_period   > 0 -> only take longs above / shorts below the EMA
    session_start_utc / session_end_utc -> UTC hour gate

Stateless; numpy only (sandbox-safe, and fast enough to grid).
"""
import numpy as np


def _atr(H, L, C, period):
    """Simple mean of the last `period` true ranges (numpy, no pandas)."""
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def _ema(v, period):
    """Simple EMA via a numpy recurrence (fast enough)."""
    alpha = 2.0 / (float(period) + 1.0)
    out = float(v[0])
    for x in v[1:]:
        out += alpha * (float(x) - out)
    return out


def _hours(bars):
    ts = bars["ts"].values
    return (ts.astype("datetime64[h]").astype("int64") % 24).astype(float)


def evaluate(bars, params):
    setup = str(params.get("setup", "disp_pullback_sym"))
    n = len(bars)
    if n < 60:
        return None
    cur = n - 1

    O = bars["open"].to_numpy(float)
    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)

    a = _atr(H, L, C, int(params.get("atr_period", 14)))
    if not np.isfinite(a) or a <= 0:
        return None

    ocur, hcur, lcur, ccur = O[cur], H[cur], L[cur], C[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

    rr = float(params.get("rr", 1.5))
    buf = float(params.get("stop_buf_atr", 0.15))
    min_body = float(params.get("body_ratio_min", 0.55))
    disp_k = float(params.get("disp_k", 1.0))

    s_utc = params.get("session_start_utc")
    if s_utc is not None:
        hh = float(_hours(bars)[cur])
        e_utc = float(params.get("session_end_utc", 24))
        if not (float(s_utc) <= hh < e_utc):
            return None

    ema_p = int(params.get("ema_period", 0))
    ema = _ema(C, ema_p) if ema_p > 0 else None

    pb = int(params.get("pullback_bars", 5))
    retrace = float(params.get("retrace", 0.5))
    allow_short = bool(params.get("allow_short", True))
    allow_long = bool(params.get("allow_long", True))

    direction = None
    stop = None

    # ------------------------------------------------- long (displacement up)
    if allow_long:
        for j in range(cur - 1, max(0, cur - 1 - pb), -1):
            rng_j = float(H[j] - L[j])
            if rng_j <= 0:
                continue
            body_j = abs(float(C[j] - O[j]))
            if body_j / rng_j < min_body or rng_j < disp_k * a:
                continue
            if C[j] <= O[j]:
                continue          # not a bullish displacement
            level = float(C[j] - retrace * body_j)
            hit = -1
            for p in range(j + 1, cur + 1):
                if L[p] <= level and C[p] > level:
                    hit = p
                    break
            if hit < 0 or ccur <= level:
                continue
            direction = "long"
            stop = min(float(L[hit]), level) - buf * a
            break

    # ----------------------------------------------- short (displacement down)
    if direction is None and allow_short:
        for j in range(cur - 1, max(0, cur - 1 - pb), -1):
            rng_j = float(H[j] - L[j])
            if rng_j <= 0:
                continue
            body_j = abs(float(C[j] - O[j]))
            if body_j / rng_j < min_body or rng_j < disp_k * a:
                continue
            if C[j] >= O[j]:
                continue          # not a bearish displacement
            level = float(C[j] + retrace * body_j)
            hit = -1
            for p in range(j + 1, cur + 1):
                if H[p] >= level and C[p] < level:
                    hit = p
                    break
            if hit < 0 or ccur >= level:
                continue
            direction = "short"
            stop = max(float(H[hit]), level) + buf * a
            break

    if direction is None or stop is None:
        return None

    if ema is not None:
        if direction == "long" and ccur < ema:
            return None
        if direction == "short" and ccur > ema:
            return None

    entry = ccur          # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk

    # GEOMETRY GUARD (fixed 2026-10-26, finding #9145): stop on the LOSING
    # side of the entry, target on the WINNING side.
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
