"""MARKET-ENTRY SILVER BULLET (previous-session H/L sweep + displacement).

The biggest family in the book is `silver_bullet_pdh_pdl_retest_*` (dozens of
active rows, legacy stamps PF 3-5).  Every one of those rows names a
STRUCTURAL entry price (the retest level inside the sweep bar), so the house
stamp buys a retroactive fill that a live market order cannot reproduce
(findings #9126 / #9133 / #9146).

This file is the HONEST rebuild of the same setup:

  * the swept level is the PREVIOUS SESSION's high/low (the canonical
    "PDH / PDL" level, grouped by session day from the bar timestamps),
  * a sweep = a bar that pokes through that level and CLOSES BACK INSIDE
    (rejection, not acceptance),
  * the decision bar is the DISPLACEMENT: it closes back through the level
    with a full body and a range >= disp_k * ATR,
  * ENTRY = the decision bar's CLOSE (the price the broker can actually pay
    after the decision; the live path submits a MARKET order),
  * STOP = beyond the sweep extreme + buffer,
  * TARGET = rr * risk (or the opposite-session liquidity level when
    target_mode == "liq").

Session gate: the classic New York silver-bullet window, in UTC
(default 13:00-16:00 UTC = 09:00-12:00 ET).  `session_start_utc` /
`session_end_utc` override it.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

TAIL = 700          # 15m x 700 bars ~ 7 days: enough for the previous session
DEFAULTS = {
    "disp_k": 1.0,
    "rr": 2.0,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "min_sweep_atr": 0.0,
    "atr_period": 14,
    "session_start_utc": 13.0,
    "session_end_utc": 16.0,
    "roll_hour_utc": 22.0,
    "target_mode": "rr",
    "min_rr": 1.5,
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


def _day_ids(ts, roll_hour):
    """Session-day id per bar: a session day starts at `roll_hour` UTC."""
    hours = ts.astype("datetime64[h]").astype("int64")
    return (hours + int(roll_hour)) // 24


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:  # noqa: BLE001
        pass

    disp_k = float(p["disp_k"])
    rr = float(p["rr"])
    confirm = int(p["confirm_bars"])
    min_body = float(p["body_ratio_min"])
    buf = float(p["stop_buf_atr"])
    min_sweep = float(p["min_sweep_atr"])
    atr_period = int(p["atr_period"])
    s0 = float(p["session_start_utc"])
    s1 = float(p["session_end_utc"])
    roll_hour = float(p["roll_hour_utc"])
    target_mode = str(p["target_mode"])
    min_rr = float(p["min_rr"])

    bars = bars.tail(TAIL) if len(bars) > TAIL else bars
    n = len(bars)
    if n < 120:
        return None
    cur = n - 1

    H = bars["high"].to_numpy(float)
    L = bars["low"].to_numpy(float)
    C = bars["close"].to_numpy(float)
    O = bars["open"].to_numpy(float)
    ts = bars["ts"].to_numpy()

    a = _atr(H, L, C, atr_period)
    if not np.isfinite(a) or a <= 0:
        return None

    # ---- session gate ----
    hour = float(ts[cur].astype("datetime64[h]").astype("int64") % 24)
    if not (s0 <= hour < s1):
        return None

    # ---- previous session high / low ----
    day = _day_ids(ts, roll_hour)
    cur_day = day[cur]
    prev_day = None
    for k in range(cur - 1, -1, -1):
        if day[k] != cur_day:
            prev_day = day[k]
            break
    if prev_day is None:
        return None
    mask = day == prev_day
    pdh = float(np.max(H[mask]))
    pdl = float(np.min(L[mask]))
    if not (np.isfinite(pdh) and np.isfinite(pdl)) or pdh <= pdl:
        return None

    hcur, lcur, ccur, ocur = H[cur], L[cur], C[cur], O[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

    # ---- find the most recent sweep of PDH / PDL ----
    direction = None
    stop = None
    swept_extreme = None
    for k in range(cur - 1, max(0, cur - confirm - 1), -1):
        if H[k] > pdh + min_sweep * a and C[k] < pdh:
            direction, stop = "short", float(H[k]) + buf * a
            swept_extreme = float(H[k])
            break
        if L[k] < pdl - min_sweep * a and C[k] > pdl:
            direction, stop = "long", float(L[k]) - buf * a
            swept_extreme = float(L[k])
            break
    if direction is None:
        return None

    # ---- displacement on the decision bar ----
    if direction == "long":
        if not (ccur > pdl and br >= min_body and rng >= disp_k * a):
            return None
    else:
        if not (ccur < pdh and br >= min_body and rng >= disp_k * a):
            return None

    entry = ccur                     # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0

    if target_mode == "liq":
        pool = float(np.min(L[mask])) if direction == "long" else float(np.max(H[mask]))
        if sign * (pool - entry) < min_rr * risk:
            return None
        target = pool
    else:
        target = entry + sign * rr * risk

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
