"""ATR-QUALITY AUDIT for the market-entry sweep->displacement family.

WHY THIS FILE
-------------
The IBKR historical archive for the pinned contracts contains a lot of
NO-TRADE bars: the bar exists, the price is real, but IBKR had nothing to
report inside the interval, so high == low == open == close and the range is
0.  In the MGC 15m deep archive those bars are ~58% of the rows in the
2025-11-12 -> 2026-06-10 window that candidate #7382 used as its "TRUE
out-of-sample holdout".

A 0-range bar contributes TR = 0 to the ATR window, so the ATR is
UNDERSTATED exactly where the data is thin.  Every threshold in the family is
expressed in ATR units (min_sweep_atr, disp_k, stop_buf_atr), so a depressed
ATR silently LOOSENS the entry conditions and SHRINKS the stop.  The risk is
that the holdout "PF 1.452 / n=90" is partly produced by signals that exist
only because the ATR was deflated by no-trade bars.

This file implements the same architecture as csid=439 with ONE change:

    atr_real_only = True  ->  the ATR window uses only bars with a real
                              range (high > low); if fewer than `atr_period`
                              real bars are available, no signal is taken.

Everything else -- the sweep test, the displacement test, the structural
stop, the market entry at the decision-bar close -- is byte-identical to
csid=439, so the A/B is a clean measurement of the ATR contamination and
nothing else.
"""
import numpy as np

TAIL = 400
DEFAULTS = {
    "lookback": 12,
    "min_sweep_atr": 0.3,
    "disp_k": 1.0,
    "rr": 1.5,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "atr_real_only": False,
}


def _atr(H, L, C, period, real_only=False, R=None):
    n = len(C)
    if n < 3:
        return float("nan")
    prev_c = C[:-1]
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    if real_only:
        if R is None:
            R = np.ones(len(H), dtype=bool)
        m = R[1:] & (tr > 0)
        tr = tr[m]
        if len(tr) < 3:
            return float("nan")
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:
        pass

    lb = int(p["lookback"])
    confirm = int(p["confirm_bars"])
    ms = float(p["min_sweep_atr"])
    disp_k = float(p["disp_k"])
    rr = float(p["rr"])
    min_body = float(p["body_ratio_min"])
    buf = float(p["stop_buf_atr"])
    atr_period = int(p["atr_period"])
    real_only = bool(p.get("atr_real_only", False))

    need = lb + confirm + 4
    bars = bars.tail(TAIL) if len(bars) > TAIL else bars
    n = len(bars)
    if n < need or n < 60:
        return None
    cur = n - 1

    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    O = bars["open"].to_numpy(float)
    R = (H > L)

    a = _atr(H, L, C, atr_period, real_only=real_only, R=R)
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
        phk = float(np.max(H[k - lb:k]))
        plk = float(np.min(L[k - lb:k]))
        if L[k] < plk - ms * a and C[k] > plk:
            j = k
            break
        if H[k] > phk + ms * a and C[k] < phk:
            j = k
            break
    if j < 0:
        return None

    plj = float(np.min(L[j - lb:j]))
    phj = float(np.max(H[j - lb:j]))

    if L[j] < plj - ms * a and C[j] > plj:
        if not (ccur > plj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "long"
        stop = float(L[j]) - buf * a
    else:
        if not (ccur < phj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "short"
        stop = float(H[j]) + buf * a

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
            "ts": bars["ts"].iloc[cur]}
