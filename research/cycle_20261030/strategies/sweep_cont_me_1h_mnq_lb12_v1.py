"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION -- MNQ 1h (2026-10-30).

DERIVATION
----------
Code = research/cycle_20261029c/codes/csid441.py (the data-hygiene market-entry
sweep -> displacement port).  Only the DEFAULTS block differs: the promoted
cell is

    lookback=12  min_sweep_atr=0.30  disp_k=0.8  rr=1.0
    confirm_bars=6  body_ratio_min=0.55  stop_buf_atr=0.15  atr_period=14

WHY THIS CELL (task #2893, honest/production fill)
---------------------------------------------------
Fill = MARKET at the OPEN of the bar AFTER the decision bar, brackets
re-anchored to the fill, gap-aware exits; costs = v1_realistic_micros,
MNQ $2/pt, qty 1; data = clean archive segments only.

MNQ 1h (single clean segment 2026-05-28 -> 2026-10-21), cells ranked on IS
ONLY, split "global_time" and "per_segment" (identical here -- one segment):
    IS  n=30  PF 1.645      OOS n=21  PF 1.508      ALL n=51  PF 1.592
    WR 56.9%   +$5,487 over the clean span   max DD $2,497
    ~2.4 trades/week, ~$42/day of clean-sample throughput.
The cell passes the OOS admission test (OOS n >= 20 AND OOS PF > 1.15).
Region shape: 5 of the 12 screened cells pass, all of them inside
(min_sweep_atr 0.30, rr 1.0) x lookback {6, 8, 12} x disp_k {0.8, 1.0} --
a plateau, not a single-cell spike.  The cells that fail are the deeper
sweeps (min_sweep_atr 0.45) and the long lookback (16).

SLOT: MNQ has NO active market-entry sweep -> displacement strategy at any
timeframe.  MNQ is the broker-confirmed #1 symbol (+$779.50 net, 84W/144L),
so this opens a NEW symbol-family-timeframe cell rather than superseding
anything.

BT-LURE check: ALL PF 1.592 with WR 56.9% -- NOT a lure shape.

ENTRY = THE DECISION BAR'S CLOSE (honest fill); the production path fills at
the next bar's open.  Stops and targets stay structural; only the ENTRY is
honest.
"""
import numpy as np

TAIL = 400
DEFAULTS = {
    "lookback": 12,
    "min_sweep_atr": 0.30,
    "disp_k": 0.8,
    "rr": 1.0,
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
