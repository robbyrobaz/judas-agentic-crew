"""MARKET-ENTRY SWEEP -> DISPLACEMENT -> OPPOSITE-LIQUIDITY TARGET (v1).

Extracted from YT:l27wF-EvDkw (TradeIQ, 2026-10-26): the target of a
sweep-and-shift trade is the NEXT POOL OF LIQUIDITY on the other side, not a
fixed R multiple ("your target? The next pool of liquidity on the other side.
The obvious highs where the short sellers have their stops.").

Everything else about the architecture follows the 2026-10-25/26 honest-fill
work (findings #9126/#9133/#9145/#9146):

  * ENTRY  = the DECISION BAR'S CLOSE (never a structural level: the house
    backtester fills at the price a strategy NAMES, so naming the FVG
    midpoint or the sweep extreme buys a retroactive fill that live cannot
    reproduce).
  * STOP   = beyond the sweep extreme + buffer (the edge information).
  * TARGET = the opposite-side liquidity pool, but only if that pool is at
    least `min_rr` * risk away -- otherwise the trade is not worth taking and
    the signal is dropped (no fallback to a fixed R target, so the stamp
    always describes a liquidity-pool target).

Optional `ema_period` gives the "trade with the higher timeframe" filter from
the same video (long only above the EMA, short only below).

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

TAIL = 500
DEFAULTS = {
    "setup": "sweep_liq",
    "lookback": 32,
    "min_sweep_atr": 0.30,
    "disp_k": 1.0,
    "confirm_bars": 6,
    "body_ratio_min": 0.55,
    "stop_buf_atr": 0.15,
    "liq_lookback": 96,
    "min_rr": 1.5,
    "atr_period": 14,
    "ema_period": 0,
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


def _ema(v, period):
    alpha = 2.0 / (float(period) + 1.0)
    out = float(v[0])
    for x in v[1:]:
        out += alpha * (float(x) - out)
    return out


def evaluate(bars, params):
    p = dict(DEFAULTS)
    try:
        p.update({k: v for k, v in (params or {}).items()
                  if k in DEFAULTS and v is not None})
    except Exception:  # noqa: BLE001
        pass

    lb = int(p["lookback"])
    confirm = int(p["confirm_bars"])
    ms = float(p["min_sweep_atr"])
    disp_k = float(p["disp_k"])
    min_body = float(p["body_ratio_min"])
    buf = float(p["stop_buf_atr"])
    liq_lb = int(p["liq_lookback"])
    min_rr = float(p["min_rr"])
    atr_period = int(p["atr_period"])
    ema_period = int(p["ema_period"])

    need = max(lb, liq_lb) + confirm + 4
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

    hcur, lcur, ccur, ocur = H[cur], L[cur], C[cur], O[cur]
    rng = max(hcur - lcur, 1e-9)
    br = abs(ccur - ocur) / rng

    # ---- find the most recent sweep inside the confirmation window ----
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

    direction = None
    stop = None
    if L[j] < plj - ms * a and C[j] > plj:
        # swept the rolling LOW -> long on the displacement back up
        if not (ccur > plj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "long"
        stop = float(L[j]) - buf * a
    else:
        if not (ccur < phj and br >= min_body and rng >= disp_k * a):
            return None
        direction = "short"
        stop = float(H[j]) + buf * a

    entry = ccur                       # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    # ---- target: the opposite-side liquidity pool ----
    lo = max(0, cur - liq_lb)
    if direction == "long":
        pool = float(np.max(H[lo:cur]))
    else:
        pool = float(np.min(L[lo:cur]))
    sign = 1.0 if direction == "long" else -1.0
    if sign * (pool - entry) < min_rr * risk:
        return None                      # the pool is not worth the risk
    target = pool

    if ema_period > 0:
        ema = _ema(C, ema_period)
        if direction == "long" and ccur < ema:
            return None
        if direction == "short" and ccur > ema:
            return None

    return {"direction": direction, "entry": float(entry),
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
