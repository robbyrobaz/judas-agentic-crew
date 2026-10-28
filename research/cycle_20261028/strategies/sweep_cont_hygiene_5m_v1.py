"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION -- DATA-HYGIENE PORT (5m).

DERIVATION (2026-10-28 cycle)
-----------------------------
The sweep->displacement CONTINUATION architecture (csid=439, MGC 15m) is the
ONLY family in this book that survived the honest-fill audit (finding #9146)
and a TRUE out-of-sample holdout (candidate #7382).  Its cross-symbol 15m
ports are dead (MNQ 0.758 / MCL 0.685 / ZF 0.812 / 6J 1.017) because the bar
archive of those symbols is polluted with PLACEHOLDER rows (zero range, zero
volume) -- see research/cycle_20261027/strategies/market_entry_sweep_cont_hygiene_v1.py.

This file is that SAME code with the DATA-HYGIENE GUARD kept, re-tuned on the
5m timeframe for MGC, and the winning cell's parameters baked into DEFAULTS so
that the sandboxed house engine (run_custom_backtest) replays EXACTLY the
configuration that was validated -- the house engine passes no strategy params
through, so the DEFAULTS *are* the strategy.

VALIDATION (research/cycle_20261028/sweep5m_grid.py, live/production fill:
market at the OPEN of the bar AFTER the decision bar, brackets re-anchored,
gap-aware exits, cost_model=v1_realistic_micros, qty=1; per CLEAN SEGMENT so
a trade can never jump a time gap; cells RANKED ON IS ONLY, OOS = admission):

    cell: lookback=20 min_sweep_atr=0.6 disp_k=1.0 rr=2.0
          confirm_bars=6 body_ratio_min=0.55 stop_buf_atr=0.15 atr_period=14

    IS  (first 60% of the clean span): n=37  PF 2.237  E[R] +0.297
    OOS (last  40% of the clean span): n=27  PF 1.501  E[R] +0.111   <- admission
    ALL                                       n=64  PF 1.935  E[R] +0.219  WR 40.6%

Note the WIN RATE is 40.6% with PF 1.94 -- this is NOT the BT-LURE shape
(PF>3 with a ~90% win rate) that findings 07f3a4c2 / 2311a131 showed collapses
to PF 0.70-0.90 under an executable fill.  It also survived the IS->OOS
handover, which the MGC 15m cell did not have to do at this strength.

MECHANICS
---------
* Look back `lookback` bars for a SWEEP: a bar whose low pierces the rolling
  low by >= min_sweep_atr x ATR and then CLOSES BACK ABOVE it (bearish mirror).
* Within `confirm_bars` bars after the sweep, require a DISPLACEMENT bar:
  body ratio >= body_ratio_min, range >= disp_k x ATR, closing on the sweep
  side of the swept level.
* Stop beyond the swept extreme +/- stop_buf_atr x ATR; target at rr x risk.
* Entry = the decision bar's CLOSE (the price the broker can actually pay,
  findings #9134/#9146) -- the live path submits a MARKET order.
* GEOMETRY GUARD (finding #9145): stop on the losing side, target on the
  winning side -- no inverted envelopes.
* DATA HYGIENE: placeholder rows (zero range / zero volume) are dropped before
  the ATR/sweep/displacement maths, and a decision bar that is itself a
  placeholder never produces a signal.

Stateless; numpy only.
"""
import numpy as np

TAIL = 400          # bars of history the signal actually needs
DEFAULTS = {
    "lookback": 20,
    "min_sweep_atr": 0.6,
    "disp_k": 1.0,
    "rr": 2.0,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
}


def _atr(H, L, C, period):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:], np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
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

    # ---------------- DATA HYGIENE ----------------
    # Drop placeholder rows (no range / no volume).  The LAST row must be a
    # real bar: a signal may only be taken on a bar that actually printed.
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
        if k < lb + 1:
            continue
        phk = float(np.max(H[k - lb:k])); plk = float(np.min(L[k - lb:k]))
        if L[k] < plk - ms * a and C[k] > plk:
            j = k; break
        if H[k] > phk + ms * a and C[k] < phk:
            j = k; break
    if j < 0:
        return None

    plj = float(np.min(L[j - lb:j])); phj = float(np.max(H[j - lb:j]))

    if L[j] < plj - ms * a and C[j] > plj:
        if not (ccur > plj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "long"; stop = float(L[j]) - buf * a
    else:
        if not (ccur < phj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "short"; stop = float(H[j]) + buf * a

    entry = float(ccur)          # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    # GEOMETRY GUARD (fixed 2026-10-26, finding #9145).
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": b["ts"].iloc[cur]}
