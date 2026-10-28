"""MARKET-ENTRY DISPLACEMENT -> PULLBACK CONTINUATION (symmetric, hygiene port).

Entry = the CLOSE of the DECISION bar -- the price the broker can actually
pay after the decision (finding #9134: the live path submits a MARKET order;
there is no LIMIT entry path in this repo).  Stops/targets stay structural.

RULE
  1. a DISPLACEMENT bar j (within `pullback_bars` of the decision bar) with
     body ratio >= body_ratio_min and range >= disp_k * ATR;
  2. a PULLBACK that holds the 50%/retrace level of that displacement body
     (price traded back to `level` and closed back on the far side);
  3. entry = decision close, stop beyond the pullback low + ATR buffer,
     target at rr * risk.

DATA HYGIENE: placeholder rows (zero range / zero volume) are dropped before
the maths -- the MCL/ZF/6J archives are 19-44% placeholder rows in the
132-day window and a zero-range bar fabricates a sweep that never printed.

GEOMETRY GUARD (fixed 2026-10-26, finding #9145): the stop must sit on the
LOSING side of the entry and the target on the WINNING side.

Stateless; numpy only.
"""
import numpy as np

# --- CELL ---


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def evaluate(bars, params):
    p = dict(CELL)
    try:
        p.update({k: v for k, v in (params or {}).items() if k in p and v is not None})
    except Exception:
        pass

    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        if not bool(valid.iloc[-1]):
            return None
        b = bars[valid]
    except Exception:
        b = bars
    if len(b) < 60:
        return None
    cur = len(b) - 1

    O = b["open"].to_numpy(float); H = b["high"].to_numpy(float)
    L = b["low"].to_numpy(float); C = b["close"].to_numpy(float)

    a = _atr(H, L, C, int(p["atr_period"]))
    if not np.isfinite(a) or a <= 0:
        return None

    rr = float(p["rr"]); buf = float(p["stop_buf_atr"])
    min_body = float(p["body_ratio_min"]); disp_k = float(p["disp_k"])
    pb = int(p["pullback_bars"]); retrace = float(p["retrace"])
    allow_short = bool(p["allow_short"]); allow_long = bool(p["allow_long"])

    ocur, hcur, lcur, ccur = O[cur], H[cur], L[cur], C[cur]

    direction = None
    stop = None

    if allow_long:
        for j in range(cur - 1, max(0, cur - 1 - pb), -1):
            rng_j = float(H[j] - L[j])
            if rng_j <= 0:
                continue
            body_j = abs(float(C[j] - O[j]))
            if body_j / rng_j < min_body or rng_j < disp_k * a:
                continue
            if C[j] <= O[j]:
                continue
            level = float(C[j] - retrace * body_j)
            hit = -1
            for q in range(j + 1, cur + 1):
                if L[q] <= level and C[q] > level:
                    hit = q
                    break
            if hit < 0 or ccur <= level:
                continue
            direction = "long"
            stop = min(float(L[hit]), level) - buf * a
            break

    if direction is None and allow_short:
        for j in range(cur - 1, max(0, cur - 1 - pb), -1):
            rng_j = float(H[j] - L[j])
            if rng_j <= 0:
                continue
            body_j = abs(float(C[j] - O[j]))
            if body_j / rng_j < min_body or rng_j < disp_k * a:
                continue
            if C[j] >= O[j]:
                continue
            level = float(C[j] + retrace * body_j)
            hit = -1
            for q in range(j + 1, cur + 1):
                if H[q] >= level and C[q] < level:
                    hit = q
                    break
            if hit < 0 or ccur >= level:
                continue
            direction = "short"
            stop = max(float(H[hit]), level) + buf * a
            break

    if direction is None or stop is None:
        return None

    entry = float(ccur)
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": b["ts"].iloc[cur]}
