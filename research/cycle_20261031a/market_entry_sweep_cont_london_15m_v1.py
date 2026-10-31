"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION -- LONDON SESSION (15m).

WHY THIS FILE EXISTS (2026-10-31 cycle, finding 9169):
  The custom backtest engine's retro-fill defect was fixed on 2026-10-30
  (commit 7c0106c).  Under the corrected PRODUCTION fill model -- decision on
  the close of bar T, fill at the OPEN of bar T+1 -- every legacy custom
  strategy whose entry NAMED A PRICE (FVG midpoint, OB midpoint, PDH/PDL
  retest) collapses to PF 0.1-0.5: those were fill artefacts.  The only
  architecture that survives is MARKET ENTRY: signal -> decide on the close of
  bar T -> the runtime sends a MARKET order -> it fills at the open of T+1.

  This strategy is that architecture (csid439 semantics, unchanged) PLUS ONE
  ADDITION: a SESSION GATE on the decision bar's UTC hour.

WHAT THE SESSION GATE FIXES (measured 2026-10-31, PRODUCTION fill):
  The ungated family is positive on MGC 15m overall (PF 1.26, n=213 over
  11 months) but it is NOT stable: in the 2026-09-02 -> 2026-10-26 fold it
  drops to PF 0.74, while the 06:00-12:00 UTC (London AM) slice is positive in
  EVERY out-of-sample fold (worst fold PF 1.52).  The same gate rescues MNQ
  15m (baseline PF 0.84 -> 1.15, all three OOS folds positive) and it does
  NOT rescue MCL/ZF/6J -- those are genuinely bad symbols for this family, not
  a timing problem.

  Neighbouring windows (04-10, 05-12, 06-11, 07-13 UTC) are ALSO positive on
  MGC, so the effect is a broad London-morning band, not a knife-edge
  parameter.

SIGNAL (stateless, numpy only, causal):
  1. SWEEP: a bar k within the last `confirm_bars` takes out a rolling
     `lookback` high/low by >= min_sweep_atr * ATR and closes back inside the
     range (liquidity grab).  The ATR used is the ATR of the DECISION bar.
  2. DISPLACEMENT: the decision bar closes back through the swept level with
     body ratio >= body_ratio_min and range >= disp_k * ATR.
  3. SESSION: the decision bar's UTC hour must be inside
     [session_start_hour_utc, session_end_hour_utc).
  4. entry = decision close (the price the broker can actually pay -- the live
     path submits a MARKET order, there is no LIMIT entry path here),
     stop beyond the sweep extreme + buffer, target at rr * risk.

GEOMETRY GUARD: the stop must sit on the LOSING side of the entry and the
target on the WINNING side (inverted comparisons once kept only broken
trades -- finding 9145).

NO LOOK-AHEAD: every input is read from bars[:i+1]; the session hour is the
decision bar's own timestamp; the sweep is searched backwards only.
"""
import numpy as np

TAIL = 400          # bars of history the signal actually needs
DEFAULTS = {
    "lookback": 12,
    "min_sweep_atr": 0.30,
    "disp_k": 1.0,
    "rr": 1.0,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "session_enabled": 1,
    "session_start_hour_utc": 6,
    "session_end_hour_utc": 12,
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


def _hour_utc(ts):
    """UTC hour of a timestamp, or None if it cannot be read."""
    try:
        if getattr(ts, "tzinfo", None) is not None:
            return int(ts.tz_convert("UTC").hour)
        return int(ts.hour)
    except Exception:
        return None


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

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    # ---- SESSION GATE on the DECISION bar's UTC hour -------------------
    hour = _hour_utc(bars["ts"].iloc[cur])
    if hour is None:
        return None
    if int(p["session_enabled"]):
        lo = int(p["session_start_hour_utc"]) % 24
        hi = int(p["session_end_hour_utc"]) % 24
        if lo <= hi:
            if not (lo <= hour < hi):
                return None
        else:                       # window wraps midnight
            if not (hour >= lo or hour < hi):
                return None

    hcur, lcur, ccur, ocur = H[cur], L[cur], C[cur], O[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

    # ---- most recent qualifying SWEEP inside the confirmation window ---
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
        # swept the rolling LOW -> long on the displacement back up
        if not (ccur > plj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "long"
        stop = float(L[j]) - buf * a
    else:
        # swept the rolling HIGH -> short on the displacement back down
        if not (ccur < phj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "short"
        stop = float(H[j]) + buf * a

    entry = float(ccur)
    risk = abs(entry - stop)
    if risk <= 0 or not np.isfinite(risk):
        return None

    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    # geometry guard: stop on the losing side, target on the winning side
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {
        "direction": direction,
        "entry": entry,
        "stop": float(stop),
        "target": float(target),
        "ts": bars["ts"].iloc[cur],
        "features": {
            "family": "sweep_disp_market_entry",
            "hour_utc": hour,
            "session": "london_am",
        },
    }
