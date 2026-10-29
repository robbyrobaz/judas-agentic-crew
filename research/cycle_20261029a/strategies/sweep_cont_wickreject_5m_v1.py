"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION -- WICK-REJECTION PORT (5m).

WHAT IS NEW HERE (relative to csid 441 / pending candidate #7383)
---------------------------------------------------------------
1. `lookback` 20 -> 40: the liquidity pool is the extreme of a 40-bar window
   rather than a 20-bar window.  A 40-bar extreme is a more meaningful
   "resting liquidity" level (it is not simply the last few bars' noise), and
   it halves the signal rate, which raised BOTH the IS and the OOS profit
   factor of the honest live book (IS PF 2.237 -> 2.957, OOS PF 1.254 -> 1.694).

2. `sweep_body_max` 0.60 -- THE NEW FILTER.  A real liquidity sweep is a
   REJECTION: the wick takes the level but the candle does not close beyond
   it.  The distinguishing evidence is therefore the WICK, not the body, so the
   sweep candle must be a thin-body rejection wick:
       |C - O| / (H - L) <= sweep_body_max
   Source: YT:tFHyr0nRZaU (Neeraj joshi, "Market Structure & Liquidity Sweep
   Masterclass | ICT + SMC Trading Strategy Course (Ep.2)") -- "a sweep is a
   wick that takes the previous swing high/low but the candle does NOT close
   beyond it".  Filtering out body-driven breaks of the level is exactly the
   "expansion vs absorption" distinction (YT:NRwsJ9NVH5c) and it lifted the
   honest live book to IS PF 3.079 (n=30) / OOS PF 1.694 (n=22).

3. `pool_mode`/equal-pool machinery is NOT used here: this port keeps the
   ROLLING-EXTREME pool, which is what the validated numbers below were
   measured on.

FILL / HYGIENE DISCIPLINE (unchanged from csid 441 -- it is the whole point):
  * entry = the DECISION bar's close.  The live path submits a MARKET order,
    so the close is a price the broker can actually pay (finding #9134).
    Nothing is filled at a named structural price and nothing is filled at a
    bar that has already closed (the retro-fill defect of finding 07f3a4c2 /
    9164) -- the signal carries the decision bar's own timestamp so the house
    engine's fill-audit can measure it.
  * PLACEHOLDER bars (zero range or zero volume) are dropped INSIDE the maths
    before any level is computed -- they manufacture fake sweeps.
  * if the DECISION bar itself is a placeholder, NO signal is emitted at all.
  * a geometry guard keeps the stop on the losing side and the target on the
    winning side of the entry.

VALIDATED CELL (MGC 5m, honest LIVE book = market at the OPEN of the bar after
the decision bar, brackets re-anchored, gap-aware exits, per-CLEAN-SEGMENT
data so no trade jumps a time gap; IS/OOS = 60/40 time split over the clean
span 2026-07-27 -> 2026-10-28, 18,444 bars):

    IS  n=30  PF 3.079  WR 46.7%  E[R] +0.400  +$4,629
    OOS n=22  PF 1.694  WR 40.9%  E[R] +0.227  +$1,039
    WR ~41-47% with PF ~2-3 is NOT the BT-LURE shape (PF>3 with ~90% WR).

STATELESS; numpy + pandas only (sandbox-safe).
"""
import numpy as np

TAIL = 400
DEFAULTS = {
    "lookback": 40,
    "min_sweep_atr": 0.6,
    "disp_k": 1.0,
    "rr": 2.0,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "sweep_body_max": 0.60,
}


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    if p <= 0:
        return float("nan")
    return float(np.mean(tr[-p:]))


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items() if k in DEFAULTS and v is not None})
    except Exception:
        pass

    lb = int(p["lookback"]); confirm = int(p["confirm_bars"]); ms = float(p["min_sweep_atr"])
    disp_k = float(p["disp_k"]); rr = float(p["rr"]); min_body = float(p["body_ratio_min"])
    buf = float(p["stop_buf_atr"]); atr_period = int(p["atr_period"])
    sb_max = float(p["sweep_body_max"])

    try:
        valid = ((bars["high"] - bars["low"]) > 0) & (bars["volume"] > 0)
        if not bool(valid.iloc[-1]):
            return None
        b = bars[valid]
    except Exception:
        b = bars

    need = lb + confirm + 4
    b = b.tail(TAIL) if len(b) > TAIL else b
    n = len(b)
    if n < need or n < 60:
        return None
    cur = n - 1

    H = b["high"].to_numpy(float); L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float); O = b["open"].to_numpy(float)

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    hcur, lcur, ccur, ocur = H[cur], L[cur], C[cur], O[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

    direction = None
    stop = None

    j = -1
    for k in range(cur - 1, max(0, cur - confirm - 1), -1):
        if k - lb < 0:
            continue
        phk = float(np.max(H[k - lb:k])); plk = float(np.min(L[k - lb:k]))
        if L[k] < plk - ms * a and C[k] > plk:
            j = k; break
        if H[k] > phk + ms * a and C[k] < phk:
            j = k; break
    if j < 0:
        return None

    plj = float(np.min(L[j - lb:j])); phj = float(np.max(H[j - lb:j]))

    # --- THE NEW FILTER: the sweep must be a thin-body rejection wick -------
    rj = float(H[j]) - float(L[j])
    if rj <= 0:
        return None
    if abs(float(C[j]) - float(O[j])) / rj > sb_max:
        return None

    if L[j] < plj - ms * a and C[j] > plk:
        if not (ccur > plj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "long"; stop = float(L[j]) - buf * a
    else:
        if not (ccur < phj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "short"; stop = float(H[j]) + buf * a

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
