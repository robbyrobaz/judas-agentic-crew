"""MARKET-ENTRY SWEEP -> DISPLACEMENT CONTINUATION (template).

Rendered per configuration by research/cycle_20261026/make_code.py; the
rendered file's DEFAULTS are the promoted configuration, because the
sanctioned `run_custom_backtest` tool takes NO params argument (the house
engine calls evaluate(bars, {})), and the live path merges the ACTIVE ROW's
params_json over the code defaults (portfolio_runtime.py:535), so both paths
agree.

WHY MARKET ENTRY (2026-10-25/26 cycles, findings #9126/#9133/#9139):
The house backtester fills at the price the strategy NAMES.  Every legacy
family names a STRUCTURAL price (FVG midpoint, OB midpoint, the sweep
extreme) that belongs to an already-closed bar, so the move from the named
price to the decision price is never risked -- the fill is retroactive.
Here the ENTRY is the CLOSE OF THE DECISION BAR (the price the broker can
actually pay after the decision), so:
    house fill == decision close == fair fill
    live fill  == next bar open (production market order)
and the two books agree to the third decimal.  Stops/targets stay structural
(that is where the edge information lives); only the ENTRY is honest.

Signal:
  1. a SWEEP: a bar k in the last `confirm_bars` takes out a rolling
     `lookback` high/low by at least `min_sweep_atr` * ATR and closes back
     inside the range (liquidity grab);
  2. a DISPLACEMENT: the decision bar closes back through the swept level
     with body ratio >= `body_ratio_min` and range >= `disp_k` * ATR;
  3. entry at the decision close, stop beyond the sweep extreme + buffer,
     target at `rr` * risk.

Stateless; numpy only (sandbox-safe).
"""
import numpy as np

TAIL = 400          # bars of history the signal actually needs
DEFAULTS = {
    "lookback": 32,
    "min_sweep_atr": 0.30,
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
    tr = np.maximum(H[1:] - L[1:],
                    np.maximum(np.abs(H[1:] - prev_c), np.abs(L[1:] - prev_c)))
    p = int(min(period, len(tr)))
    return float(np.mean(tr[-p:]))


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

    entry = float(ccur)          # HONEST: the decision bar's close
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    sign = 1.0 if direction == "long" else -1.0
    target = entry + sign * rr * risk
    # GEOMETRY GUARD (2026-10-26, fixed): the stop must sit on the LOSING
    # side of the entry (sign*(stop-entry) < 0) and the target on the
    # WINNING side (sign*(target-entry) > 0).  The first version of this
    # guard had the comparison inverted -- it skipped every correctly
    # oriented trade and kept only the broken ones (finding 9145).
    if sign * (stop - entry) >= 0 or sign * (target - entry) <= 0:
        return None

    return {"direction": direction, "entry": entry,
            "stop": float(stop), "target": float(target),
            "ts": bars["ts"].iloc[cur]}
