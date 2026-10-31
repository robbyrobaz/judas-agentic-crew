"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION -- SESSION-GATED (15m).

Architecture: the ONLY family in this book that survived the honest-fill
audit (finding #9146 / #9169): decide on the close of bar T, the live runtime
fills at the OPEN of bar T+1, stops and targets stay structural.  Every
legacy family in the library (silver_bullet / ict_mitigation / ifvg_midpoint /
ob_midpoint) is a named-LIMIT-price fill and collapses to PF 0.1-0.5 under
executable fills.

WHAT IS NEW HERE (vs csid=439, the all-day MGC 15m version):
  1. a SESSION GATE on the decision bar (default 06:00-12:00 UTC = the London
     AM window).  The all-day version's edge is concentrated there: its last
     out-of-sample fold is PF 0.74 while the gated version is >1.3 in the SAME
     window.  Gating removes the diluting trades instead of adding new ones.
  2. DATA HYGIENE: bars that did not print (range == 0) are dropped before
     any indicator maths, so ATR is the ATR the live scanner will see
     (same guard as csid=441).

VALIDATION (research/cycle_20261031d/, production fill = market at the next
bar's open, v1_realistic_micros costs, 3-fold chronological walk-forward on
the MGC 15m deep archive 2025-11-12 -> 2026-10-26, 17434 clean bars):
  lookback=12 min_sweep_atr=0.30 disp_k=1.0 rr=1.5 confirm_bars=6
  body_ratio_min=0.55 stop_buf_atr=0.15 atr_period=14  sess 06:00-12:00 UTC
  hygiene ON : OOS min PF 1.818 / mean 3.216, n=41, full PF 2.635, n=54,
               +$6,021.03, folds [5.919, 1.911, 1.818]
  hygiene OFF: OOS min PF 1.661 / mean 2.177, n=50, full PF 2.926, n=58,
               +$7,116.08, folds [1.954, 2.917, 1.661]
  The edge is a PLATEAU, not a spike: sess 04-10 / 05-12 / 06-11 / 06-12 and
  disp_k 0.8-1.2 all clear every out-of-sample fold.
"""
import numpy as np

TAIL = 400          # bars of history the signal actually needs
DEFAULTS = {
    "lookback": 12,
    "min_sweep_atr": 0.30,
    "disp_k": 1.0,
    "rr": 1.5,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "atr_period": 14,
    "sess_lo": 6,          # decision bar hour (UTC), inclusive
    "sess_hi": 12,         # decision bar hour (UTC), exclusive
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


def _utc_hour(ts):
    """Hour (UTC) of the LAST row of a ts series; None if not derivable."""
    try:
        s = ts
        try:
            s = s.dt.tz_convert("UTC")
        except Exception:
            pass
        return int(s.iloc[-1].hour)
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
    sess_lo = int(p["sess_lo"])
    sess_hi = int(p["sess_hi"])

    # ---- hygiene: only bars that actually printed -----------------------
    try:
        b = bars.tail(TAIL) if len(bars) > TAIL else bars
        valid = ((b["high"] - b["low"]) > 0)
        if "volume" in b.columns:
            valid = valid & (b["volume"] > 0)
        b = b[valid]
    except Exception:
        b = bars.tail(TAIL) if len(bars) > TAIL else bars

    need = lb + confirm + 4
    n = len(b)
    if n < need or n < 60:
        return None
    cur = n - 1

    H = b["high"].to_numpy(float)
    L = b["low"].to_numpy(float)
    C = b["close"].to_numpy(float)
    O = b["open"].to_numpy(float)

    # ---- session gate on the DECISION bar --------------------------------
    if sess_lo >= 0 and sess_hi >= 0:
        h = _utc_hour(b["ts"])
        if h is None:
            return None
        if sess_lo <= sess_hi:
            if not (sess_lo <= h < sess_hi):
                return None
        else:
            if not (h >= sess_lo or h < sess_hi):
                return None

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    hcur, lcur, ccur, ocur = H[cur], L[cur], C[cur], O[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

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

    entry = float(ccur)          # decision bar close; the fill is the NEXT open
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    # geometry guard: stop on the losing side, target on the winning side
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": b["ts"].iloc[cur]}
